"""Single source of "now" for the whole application.

Business code must call :func:`now` instead of ``datetime.now()`` so tests can freeze or move time.
:func:`monotonic` is for measuring durations and timeouts (never affected by wall-clock changes).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

__all__ = ["FrozenClock", "monotonic", "now", "reset_clock", "set_clock"]

ClockFn = Callable[[], datetime]

_clock: ClockFn | None = None


def _system_now() -> datetime:
    return datetime.now(UTC)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware datetime")
    if value.tzinfo is UTC:
        return value
    return value.astimezone(UTC)


def now() -> datetime:
    """Current time as an aware UTC ``datetime``."""
    fn = _clock
    if fn is None:
        return datetime.now(UTC)
    return _ensure_utc(fn())


def monotonic() -> float:
    """Monotonic seconds for durations, deadlines and rate limiting."""
    return time.monotonic()


def set_clock(fn: ClockFn | datetime) -> None:
    """Override :func:`now` (tests). Accepts a callable or a fixed aware ``datetime``."""
    global _clock  # noqa: PLW0603 - deliberate process-wide override
    if isinstance(fn, datetime):
        fixed = _ensure_utc(fn)
        _clock = lambda: fixed  # noqa: E731
        return
    if not callable(fn):
        raise TypeError("set_clock expects a callable returning datetime or a datetime")
    _clock = fn


def reset_clock() -> None:
    """Restore the real system clock."""
    global _clock  # noqa: PLW0603
    _clock = None


class FrozenClock:
    """Manually advanced clock for tests: ``set_clock(FrozenClock(start))`` then ``clock.advance(...)``."""

    __slots__ = ("_value",)

    def __init__(self, start: datetime | None = None) -> None:
        self._value = _ensure_utc(start) if start is not None else _system_now()

    def __call__(self) -> datetime:
        return self._value

    def advance(self, delta: timedelta | float = 0.0, /, **kwargs: float) -> datetime:
        """Move forward by ``delta`` (timedelta or seconds) and/or ``timedelta`` kwargs; returns new time."""
        step = delta if isinstance(delta, timedelta) else timedelta(seconds=delta)
        step += timedelta(**kwargs)
        self._value += step
        return self._value

    def set(self, value: datetime) -> None:
        self._value = _ensure_utc(value)
