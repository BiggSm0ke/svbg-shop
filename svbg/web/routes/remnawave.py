"""``POST /webhooks/remnawave`` — the fast intake path of panel webhooks (02 §5.2–5.3).

Order of checks (cheapest first, nothing is parsed before the signature is verified):

1. body size ≤ 256 KB (``413``);
2. HMAC-SHA256 of the **raw bytes** against the current and the previous secret, constant time (``401``);
   no secret configured → ``401`` as well (webhooks are off; the panel gives up after its retries).
   A rejected request counts as the panel's (``bad_signature``, which the setup wizard reports as «секрет не
   совпадает») only when it looks like one: ``User-Agent: Remnawave``, an ``X-Remnawave-Timestamp`` equal to
   the body's timestamp and inside the acceptance window. Anything else is internet noise
   (``bad_signature_foreign``) and cannot steer the owner into changing a working secret. The warning about
   rejected signatures is logged at most once a minute, with the number of rejections since the last one;
3. light envelope parse (``400`` for a signed non-envelope) and timestamp window (> 5 min in the future or
   > 7 days old → ``200`` without storing: a harmless late replay);
4. ``INSERT … ON CONFLICT DO NOTHING`` into ``rw_inbox`` keyed by ``sha256(body)`` → ``200``; database
   unavailable → ``503`` (the panel retries).

Raw bodies are never logged (they carry protocol passwords and the admin's login password).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Final

from aiohttp import web

from svbg.remnawave.inbox import store
from svbg.remnawave.webhooks import (
    MAX_BODY,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    WebhookParseError,
    body_hash,
    parse_envelope,
    timestamp_acceptable,
    verify_signature,
)

if TYPE_CHECKING:
    from svbg.db.engine import Database

__all__ = ["PATH", "WebhookStats", "looks_like_panel", "remnawave_routes"]

log = logging.getLogger("svbg.web.remnawave")

PATH: Final = "/webhooks/remnawave"
#: Remnawave sends ``User-Agent: Remnawave`` with every webhook (02 §5.1).
PANEL_USER_AGENT: Final = "remnawave"
#: At most one "bad signature" warning per this many seconds (each carries the count since the last one).
BAD_SIGNATURE_LOG_EVERY: Final = 60.0


@dataclass(slots=True)
class WebhookStats:
    """Counters for diagnostics («секрет не совпадает», «вебхуки доходят»)."""

    accepted: int = 0
    duplicates: int = 0
    bad_signature: int = 0  # rejected requests that look like the panel's (wrong secret)
    bad_signature_foreign: int = 0  # rejected requests that do not look like the panel at all
    no_secret: int = 0
    malformed: int = 0
    stale: int = 0
    too_large: int = 0
    db_errors: int = 0


def _reply(status: int, text: str) -> web.Response:
    return web.json_response({"status": text}, status=status)


def looks_like_panel(raw: bytes, headers: Mapping[str, str]) -> bool:
    """Does an unverified request look like a Remnawave delivery (02 §5.1)? Diagnostics only, never trust.

    Cheap header checks first; the body is parsed only when they pass (it is ≤ 256 KB and parsing never
    raises out of here).
    """
    agent = headers.get("User-Agent", "")
    if agent.strip().lower() != PANEL_USER_AGENT:
        return False
    stamp = headers.get(TIMESTAMP_HEADER, "").strip()
    if not stamp:
        return False
    try:
        env = parse_envelope(raw)
    except WebhookParseError:
        return False
    if not timestamp_acceptable(env.timestamp):
        return False
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")) == env.timestamp
    except ValueError:
        return False


class _Throttled:
    """One log line per ``every`` seconds; the lines in between are counted, not written."""

    __slots__ = ("_every", "_last", "_skipped")

    def __init__(self, every: float) -> None:
        self._every = every
        self._last: float | None = None
        self._skipped = 0

    def warning(self, message: str) -> None:
        moment = time.monotonic()
        if self._last is not None and moment - self._last < self._every:
            self._skipped += 1
            return
        skipped, self._skipped, self._last = self._skipped, 0, moment
        if skipped:
            log.warning("%s (+%d more since the last message)", message, skipped)
        else:
            log.warning("%s", message)


def remnawave_routes(
    *,
    db: Database,
    secrets: Callable[[], Sequence[str | None]],
    on_stored: Callable[[], None] | None = None,
    stats: WebhookStats | None = None,
) -> list[web.RouteDef]:
    """Routes to pass to ``build_web_app``. ``secrets()`` returns the accepted secrets (current, previous)."""
    counters = stats if stats is not None else WebhookStats()
    panel_log = _Throttled(BAD_SIGNATURE_LOG_EVERY)  # separate, so noise cannot hide the panel's rejections
    foreign_log = _Throttled(BAD_SIGNATURE_LOG_EVERY)

    async def handle(request: web.Request) -> web.Response:
        length = request.content_length
        if length is not None and length > MAX_BODY:
            counters.too_large += 1
            return _reply(413, "too_large")
        chunks: list[bytes] = []
        size = 0
        async for chunk in request.content.iter_chunked(64 * 1024):  # chunked bodies have no Content-Length
            size += len(chunk)
            if size > MAX_BODY:
                counters.too_large += 1
                return _reply(413, "too_large")
            chunks.append(chunk)
        raw = b"".join(chunks)
        accepted = [s for s in secrets() if s]
        if not accepted:
            counters.no_secret += 1
            return _reply(401, "webhooks_disabled")
        if not verify_signature(raw, request.headers.get(SIGNATURE_HEADER), accepted):
            if looks_like_panel(raw, request.headers):
                counters.bad_signature += 1
                panel_log.warning("remnawave webhook with a bad signature rejected (secrets differ?)")
            else:
                counters.bad_signature_foreign += 1
                foreign_log.warning("non-panel request to the webhook endpoint rejected: bad signature")
            return _reply(401, "bad_signature")
        try:
            env = parse_envelope(raw)
        except WebhookParseError:
            counters.malformed += 1
            return _reply(400, "malformed")
        if not timestamp_acceptable(env.timestamp):
            counters.stale += 1
            log.info("remnawave webhook %s with an out-of-window timestamp ignored", env.event[:64])
            return _reply(200, "stale")
        try:
            async with db.tx() as conn:
                fresh = await store(conn, body_hash(raw), env)
        except Exception:
            counters.db_errors += 1
            log.exception("remnawave webhook could not be stored")
            return _reply(503, "unavailable")
        if fresh:
            counters.accepted += 1
            if on_stored is not None:
                try:
                    on_stored()
                except Exception:
                    log.exception("inbox wake-up failed")
        else:
            counters.duplicates += 1
        return _reply(200, "ok" if fresh else "duplicate")

    return [web.post(PATH, handle)]
