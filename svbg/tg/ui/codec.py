"""``callback_data`` codec, format ``v1:<screen>:<action>[:<arg>]`` (≤ 64 bytes UTF-8, 04 D6).

* ``screen`` and ``action`` are short names ``[A-Za-z0-9_.-]{1,32}``; ``arg`` is any text without control
  characters (it is the last field, so it may contain ``:``).
* An argument that does not fit is stored in ``short_tokens`` and encoded as ``~<token>``
  (:meth:`CallbackCodec.encode_long`). The token is bound to its screen and action: replaying it under
  another screen/action resolves to nothing.
* :func:`decode` returns ``None`` for anything it does not understand (other versions, garbage, too long):
  the router then shows the menu with the toast "Меню обновилось". Buttons of any age therefore never crash.

``encode``/``decode`` are pure and synchronous; only long arguments touch the database, and resolved tokens
are cached in memory so a click normally costs no SQL.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import secrets
from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from svbg.core import clock
from svbg.tg.ui.tables import short_tokens

if TYPE_CHECKING:
    from svbg.db.engine import Database

__all__ = [
    "ACTION_OPEN",
    "MAX_BYTES",
    "PREFIX",
    "CallbackCodec",
    "CallbackTooLongError",
    "Decoded",
    "decode",
    "encode",
    "fits",
]

log = logging.getLogger("svbg.tg.ui.codec")

PREFIX: Final = "v1"
MAX_BYTES: Final = 64
ACTION_OPEN: Final = "o"  # "open the screen"
TOKEN_MARK: Final = "~"  # noqa: S105 - a separator, not a secret
MAX_PAYLOAD_BYTES: Final = 8192

_NAME_RE: Final = re.compile(r"^[A-Za-z0-9_.\-]{1,32}$")
_TOKEN_RE: Final = re.compile(r"^[A-Za-z0-9_\-]{8,32}$")
_CONTROL_RE: Final = re.compile(r"[\x00-\x1f\x7f]")


class CallbackTooLongError(ValueError):
    """The encoded data would exceed 64 bytes; use :meth:`CallbackCodec.encode_long`."""


@dataclass(frozen=True, slots=True)
class Decoded:
    screen: str
    action: str
    arg: Any = None  # str for inline args; any JSON value once a token is resolved
    token: str | None = None  # set while the argument is still a reference into ``short_tokens``


def _check_name(kind: str, value: str) -> None:
    if not isinstance(value, str) or not _NAME_RE.match(value):
        raise ValueError(f"invalid callback {kind} {value!r}: expected [A-Za-z0-9_.-]{{1,32}}")


def _compose(screen: str, action: str, arg: str | None) -> str:
    if arg is None or arg == "":
        return f"{PREFIX}:{screen}:{action}"
    return f"{PREFIX}:{screen}:{action}:{arg}"


def fits(screen: str, action: str = ACTION_OPEN, arg: str | None = None) -> bool:
    """True if :func:`encode` would succeed without a short token."""
    try:
        encode(screen, action, arg)
    except CallbackTooLongError:
        return False
    return True


def encode(screen: str, action: str = ACTION_OPEN, arg: str | None = None) -> str:
    """Encode a callback. Raises :class:`CallbackTooLongError` when the result exceeds 64 bytes."""
    _check_name("screen", screen)
    _check_name("action", action)
    if arg is not None:
        if not isinstance(arg, str):
            raise TypeError("inline callback arg must be str (use encode_long for other values)")
        if _CONTROL_RE.search(arg):
            raise ValueError("callback arg must not contain control characters")
        if arg.startswith(TOKEN_MARK):
            # '~' starts a token reference; such literal args must go through short_tokens.
            raise CallbackTooLongError("args starting with '~' are reserved for short tokens")
    data = _compose(screen, action, arg)
    if len(data.encode()) > MAX_BYTES:
        raise CallbackTooLongError(f"callback data is longer than {MAX_BYTES} bytes")
    return data


def decode(data: object) -> Decoded | None:
    """Parse callback data; ``None`` for unknown or old formats and for anything malformed."""
    if not isinstance(data, str) or not data or len(data) > MAX_BYTES:
        return None
    try:
        if len(data.encode()) > MAX_BYTES:
            return None
    except UnicodeEncodeError:  # lone surrogates cannot come from Telegram, treat as garbage
        return None
    parts = data.split(":", 3)
    if len(parts) < 3 or parts[0] != PREFIX:
        return None
    screen, action = parts[1], parts[2]
    if not _NAME_RE.match(screen) or not _NAME_RE.match(action):
        return None
    arg = parts[3] if len(parts) == 4 else None
    if not arg:
        return Decoded(screen, action)
    if _CONTROL_RE.search(arg):
        return None
    if arg.startswith(TOKEN_MARK):
        token = arg[1:]
        if not _TOKEN_RE.match(token):
            return None
        return Decoded(screen, action, None, token)
    return Decoded(screen, action, arg)


@dataclass(slots=True)
class _Cached:
    payload: dict[str, Any]
    expires_at: datetime
    stored_mono: float  # when we last wrote/refreshed the row (monotonic)


class CallbackCodec:
    """``encode``/``decode`` plus database-backed short tokens for long arguments.

    Tokens are a keyed hash of ``(screen, action, arg)``: the same long argument rendered again maps to the
    same row (its expiry is refreshed at most once per ``ttl / 30``), so re-rendering keyboards does not grow
    the table. Pass a stable ``key`` (derived from ``SECRET_KEY``) to keep tokens identical across restarts.
    """

    encode = staticmethod(encode)
    decode = staticmethod(decode)
    fits = staticmethod(fits)

    def __init__(
        self,
        db: Database,
        *,
        ttl: timedelta = timedelta(days=30),
        key: bytes | None = None,
        cache_size: int = 10_000,
    ) -> None:
        if ttl <= timedelta(0):
            raise ValueError("ttl must be positive")
        if key is not None and not 16 <= len(key) <= 64:
            raise ValueError("key must be 16..64 bytes")
        self._db = db
        self._ttl = ttl
        self._refresh_s = max(ttl.total_seconds() / 30, 1.0)
        self._key = key if key is not None else secrets.token_bytes(32)
        self._cache: OrderedDict[str, _Cached] = OrderedDict()
        self._cache_size = max(cache_size, 16)

    def _token_for(self, canonical: bytes) -> str:
        digest = hashlib.blake2b(canonical, key=self._key, digest_size=9).digest()
        return base64.urlsafe_b64encode(digest).decode().rstrip("=")  # 12 chars

    async def encode_long(self, screen: str, action: str, arg: Any) -> str:
        """Encode with any JSON-serializable ``arg``; short string args stay inline (no SQL)."""
        if isinstance(arg, str) and fits(screen, action, arg):
            return encode(screen, action, arg)
        _check_name("screen", screen)
        _check_name("action", action)
        payload = {"s": screen, "a": action, "v": arg}
        try:
            canonical = json.dumps(
                payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
        except (TypeError, ValueError) as e:
            raise TypeError(f"callback arg is not JSON-serializable: {e}") from None
        if len(canonical) > MAX_PAYLOAD_BYTES:
            raise ValueError(f"callback arg is too large (max {MAX_PAYLOAD_BYTES} bytes)")
        token = self._token_for(canonical)
        data = _compose(screen, action, TOKEN_MARK + token)
        if len(data.encode()) > MAX_BYTES:
            raise CallbackTooLongError("screen/action names are too long for a token reference")
        await self._store(token, payload)
        return data

    async def _store(self, token: str, payload: dict[str, Any]) -> None:
        mono = clock.monotonic()
        cached = self._cache.get(token)
        if cached is not None and cached.payload == payload and mono - cached.stored_mono < self._refresh_s:
            self._cache.move_to_end(token)
            return
        expires_at = clock.now() + self._ttl
        stmt = pg_insert(short_tokens).values(token=token, payload=payload, expires_at=expires_at)
        stmt = stmt.on_conflict_do_update(
            index_elements=[short_tokens.c.token],
            set_={"payload": stmt.excluded.payload, "expires_at": stmt.excluded.expires_at},
        )
        async with self._db.tx() as conn:
            await conn.execute(stmt)
        self._remember(token, _Cached(payload, expires_at, mono))

    def _remember(self, token: str, entry: _Cached) -> None:
        self._cache[token] = entry
        self._cache.move_to_end(token)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)

    async def resolve(self, decoded: Decoded) -> Decoded | None:
        """Replace a token reference by its stored argument; ``None`` if expired, unknown or foreign."""
        if decoded.token is None:
            return decoded
        token = decoded.token
        now = clock.now()
        cached = self._cache.get(token)
        if cached is not None and cached.expires_at > now:
            self._cache.move_to_end(token)
            payload = cached.payload
        else:
            async with self._db.read() as conn:
                row = (
                    (
                        await conn.execute(
                            sa.select(short_tokens.c.payload, short_tokens.c.expires_at).where(
                                short_tokens.c.token == token, short_tokens.c.expires_at > now
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
            if row is None:
                self._cache.pop(token, None)
                return None
            payload = row["payload"]
            if not isinstance(payload, dict):
                return None
            # stored_mono=-inf: a later encode_long of the same arg still refreshes the expiry once.
            self._remember(token, _Cached(payload, row["expires_at"], float("-inf")))
        if payload.get("s") != decoded.screen or payload.get("a") != decoded.action:
            log.info("short token used with a different screen/action; ignored")
            return None
        return replace(decoded, arg=payload.get("v"), token=None)

    async def purge_expired(self) -> int:
        """Delete expired tokens (run daily by the scheduler). Returns the number of rows removed."""
        now = clock.now()
        async with self._db.tx() as conn:
            result = await conn.execute(sa.delete(short_tokens).where(short_tokens.c.expires_at <= now))
        for token in [t for t, c in self._cache.items() if c.expires_at <= now]:
            del self._cache[token]
        return int(result.rowcount or 0)
