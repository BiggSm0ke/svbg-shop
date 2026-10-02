"""Remnawave webhooks: signature check and a light, tolerant envelope parser (02 §5.1–5.2).

The panel signs the **exact bytes** it sends: ``X-Remnawave-Signature = hex(HMAC_SHA256(secret, body))``.
Re-serializing the JSON (``json.dumps``) breaks on Cyrillic, emoji and key order, so the signature is checked
on the raw body only. Several secrets are accepted at once to allow rotation (current + previous).

Bodies carry secrets (``trojanPassword``, ``ssPassword``, ``vlessUuid``, ``loginAttempt.password``):
:func:`parse_envelope` strips them from ``data`` before anyone can store or log it.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

import msgspec

from svbg.core.clock import now

__all__ = [
    "MAX_BODY",
    "SECRET_FIELDS",
    "SIGNATURE_HEADER",
    "TIMESTAMP_HEADER",
    "WebhookEnvelope",
    "WebhookParseError",
    "body_hash",
    "parse_envelope",
    "sign",
    "strip_secrets",
    "timestamp_acceptable",
    "verify_signature",
]

SIGNATURE_HEADER: Final = "X-Remnawave-Signature"
TIMESTAMP_HEADER: Final = "X-Remnawave-Timestamp"
#: 256 KiB; a larger body is answered 413 without HMAC work.
MAX_BODY: Final = 262_144
#: Accept timestamps up to 5 min in the future and 7 days in the past (02 §0.1 п.4).
MAX_FUTURE: Final = timedelta(minutes=5)
MAX_AGE: Final = timedelta(days=7)
SECRET_FIELDS: Final = frozenset({"trojanPassword", "ssPassword", "vlessUuid", "password"})
_HEX_LEN: Final = 64
_HEX_CHARS: Final = frozenset("0123456789abcdef")


def sign(raw: bytes, secret: str) -> str:
    """Signature the panel would send for ``raw`` (lower-case hex HMAC-SHA256)."""
    return hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()


def verify_signature(raw: bytes, header: str | None, secrets: Iterable[str | None]) -> bool:
    """True when ``header`` is the HMAC-SHA256 of ``raw`` under any of ``secrets``.

    Constant time per secret (``hmac.compare_digest``) and every secret is always checked, so timing does
    not reveal which one matched. An empty/absent header, an empty secret list or empty secrets never match.
    """
    if not header or not isinstance(raw, bytes | bytearray | memoryview):
        return False
    candidate = header.strip().lower()
    if len(candidate) != _HEX_LEN or not _HEX_CHARS.issuperset(candidate):
        return False
    expected = candidate.encode("ascii")
    matched = False
    for secret in secrets:
        if not secret:
            continue
        digest = hmac.new(secret.encode("utf-8"), bytes(raw), hashlib.sha256).hexdigest().encode("ascii")
        matched |= hmac.compare_digest(digest, expected)
    return matched


def body_hash(raw: bytes) -> str:
    """Dedup key of a delivery: retries resend the same bytes (02 §5.1)."""
    return hashlib.sha256(raw).hexdigest()


class WebhookParseError(ValueError):
    """The body is not a Remnawave webhook envelope (not JSON / no scope, event or timestamp)."""


class _Raw(msgspec.Struct):
    scope: str
    event: str
    timestamp: datetime
    data: Any = None
    meta: Any = None


_raw_decoder = msgspec.json.Decoder(_Raw)


@dataclass(frozen=True, slots=True)
class WebhookEnvelope:
    scope: str
    event: str
    timestamp: datetime
    data: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] | None = None

    @property
    def panel_user_id(self) -> int | None:
        """``data.id`` for ``user`` events; ``data.user.id`` for ``user_hwid_devices``/``torrent_blocker``."""
        if self.scope == "user":
            return _positive_int(self.data.get("id"))
        user = self.data.get("user")
        if isinstance(user, Mapping):
            return _positive_int(user.get("id"))
        return None


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def strip_secrets(value: Any) -> Any:
    """Copy of ``value`` with secret keys removed at any depth."""
    if isinstance(value, Mapping):
        return {k: strip_secrets(v) for k, v in value.items() if k not in SECRET_FIELDS}
    if isinstance(value, list):
        return [strip_secrets(v) for v in value]
    return value


def parse_envelope(raw: bytes) -> WebhookEnvelope:
    """Parse ``{scope, event, timestamp, data, meta?}``; unknown fields/events are fine, secrets dropped."""
    if len(raw) > MAX_BODY:
        raise WebhookParseError("тело вебхука больше 256 КБ")
    try:
        parsed = _raw_decoder.decode(raw)
    except (msgspec.DecodeError, msgspec.ValidationError) as exc:
        raise WebhookParseError(f"не похоже на вебхук Remnawave: {exc}") from None
    if not parsed.scope or not parsed.event:
        raise WebhookParseError("в вебхуке нет scope или event")
    ts = parsed.timestamp
    if ts.tzinfo is None:
        raise WebhookParseError("timestamp вебхука без часового пояса")
    data = strip_secrets(parsed.data) if isinstance(parsed.data, dict) else {}
    meta = strip_secrets(parsed.meta) if isinstance(parsed.meta, dict) else None
    return WebhookEnvelope(parsed.scope, parsed.event, ts, data, meta)


def timestamp_acceptable(ts: datetime, at: datetime | None = None) -> bool:
    """Reject only timestamps > 5 min in the future or > 7 days old (late BullMQ deliveries are fine)."""
    current = at or now()
    return current - MAX_AGE <= ts <= current + MAX_FUTURE
