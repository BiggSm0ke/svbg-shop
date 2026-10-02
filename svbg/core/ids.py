"""Externally visible identifiers.

* :func:`uuid7` — RFC 9562 UUID version 7 (48-bit Unix ms timestamp + random), canonical lowercase string.
  Within one process ids are strictly increasing, even for many ids in the same millisecond or when the
  wall clock steps backwards (RFC 9562 §6.2, method 2: the 74 random bits act as a counter seeded randomly).
* :func:`short_token` — random URL-safe token (``[A-Za-z0-9_-]``) for links, callback payloads, etc.
"""

from __future__ import annotations

import os
import secrets
import threading
import time
from datetime import UTC, datetime

__all__ = ["is_uuid7", "short_token", "uuid7", "uuid7_time"]

_RAND_BITS = 74  # 12 bits rand_a + 62 bits rand_b
_RAND_MASK = (1 << _RAND_BITS) - 1
# Seed with the top bit clear so at least 2**73 increments fit in one millisecond (RFC 9562 §6.2).
_SEED_MASK = (1 << (_RAND_BITS - 1)) - 1
_TS_MASK = (1 << 48) - 1
_MAX_TOKEN_LEN = 512

_lock = threading.Lock()
_last_ms = -1
_last_rand = 0


def _next_fields() -> tuple[int, int]:
    global _last_ms, _last_rand  # noqa: PLW0603 - generator state is process-global by design
    ms = time.time_ns() // 1_000_000
    with _lock:
        if ms > _last_ms:
            _last_ms = ms
            _last_rand = int.from_bytes(os.urandom(10)) & _SEED_MASK
        else:
            # Same millisecond, or the clock went backwards: keep the last timestamp and count up.
            # A small random step keeps successive ids hard to guess.
            _last_rand += 1 + (int.from_bytes(os.urandom(1)) & 0x3F)
            if _last_rand > _RAND_MASK:
                _last_ms += 1
                _last_rand = int.from_bytes(os.urandom(10)) & _SEED_MASK
        return _last_ms & _TS_MASK, _last_rand


def _reset_after_fork() -> None:
    global _last_ms, _last_rand, _lock  # noqa: PLW0603
    _lock = threading.Lock()
    _last_ms = -1
    _last_rand = 0


if hasattr(os, "register_at_fork"):  # POSIX only; a forked child must not replay the parent's sequence
    os.register_at_fork(after_in_child=_reset_after_fork)


def uuid7() -> str:
    """Return a new time-ordered UUIDv7 as ``xxxxxxxx-xxxx-7xxx-[89ab]xxx-xxxxxxxxxxxx``."""
    ms, rand = _next_fields()
    rand_a = rand >> 62
    rand_b = rand & ((1 << 62) - 1)
    value = (ms << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
    h = f"{value:032x}"
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def is_uuid7(value: object) -> bool:
    """True if ``value`` is a canonical (lowercase, dashed) UUIDv7 string."""
    if not isinstance(value, str) or len(value) != 36:
        return False
    if value[8] != "-" or value[13] != "-" or value[18] != "-" or value[23] != "-":
        return False
    hexpart = value.replace("-", "")
    if len(hexpart) != 32 or any(c not in "0123456789abcdef" for c in hexpart):
        return False
    return hexpart[12] == "7" and hexpart[16] in "89ab"


def uuid7_time(value: str) -> datetime:
    """Creation time (ms precision, aware UTC) embedded in a UUIDv7 string."""
    if not is_uuid7(value):
        raise ValueError("not a canonical UUIDv7 string")
    ms = int(value[:8] + value[9:13], 16)
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def short_token(n: int = 10) -> str:
    """Random URL-safe token of exactly ``n`` characters (6 bits of entropy per character)."""
    if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= _MAX_TOKEN_LEN:
        raise ValueError(f"token length must be an int in 1..{_MAX_TOKEN_LEN}")
    # token_urlsafe(n) yields ceil(4n/3) >= n characters of the base64url alphabet without padding.
    return secrets.token_urlsafe(n)[:n]
