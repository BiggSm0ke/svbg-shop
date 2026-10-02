from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone

import pytest

from svbg.core import clock


@pytest.fixture(autouse=True)
def _restore_clock() -> Iterator[None]:
    yield
    clock.reset_clock()


def test_now_is_aware_utc_and_close_to_system_time() -> None:
    value = clock.now()
    assert value.tzinfo is UTC
    assert abs((datetime.now(UTC) - value).total_seconds()) < 5


def test_set_clock_with_callable_and_reset() -> None:
    fixed = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    clock.set_clock(lambda: fixed)
    assert clock.now() == fixed
    clock.reset_clock()
    assert clock.now() != fixed


def test_set_clock_with_datetime() -> None:
    fixed = datetime(2030, 5, 1, tzinfo=UTC)
    clock.set_clock(fixed)
    assert clock.now() == fixed


def test_non_utc_aware_values_are_converted_to_utc() -> None:
    msk = timezone(timedelta(hours=3))
    clock.set_clock(lambda: datetime(2026, 1, 1, 12, 0, tzinfo=msk))
    value = clock.now()
    assert value.tzinfo is UTC
    assert value == datetime(2026, 1, 1, 9, 0, tzinfo=UTC)


def test_naive_datetime_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        clock.set_clock(datetime(2026, 1, 1))
    clock.set_clock(lambda: datetime(2026, 1, 1))
    with pytest.raises(ValueError, match="timezone-aware"):
        clock.now()


def test_set_clock_rejects_non_callable() -> None:
    with pytest.raises(TypeError):
        clock.set_clock(42)  # type: ignore[arg-type]


def test_frozen_clock_advance() -> None:
    start = datetime(2026, 10, 1, tzinfo=UTC)
    frozen = clock.FrozenClock(start)
    clock.set_clock(frozen)
    assert clock.now() == start
    frozen.advance(90)
    assert clock.now() == start + timedelta(seconds=90)
    frozen.advance(timedelta(days=1), hours=1)
    assert clock.now() == start + timedelta(days=1, hours=1, seconds=90)
    frozen.set(start)
    assert clock.now() == start


def test_monotonic_never_decreases() -> None:
    a = clock.monotonic()
    b = clock.monotonic()
    assert b >= a
