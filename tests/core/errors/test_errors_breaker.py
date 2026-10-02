from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from svbg.core.clock import FrozenClock
from svbg.core.errors import BreakerState, CircuitBreaker

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def make(**kw: object) -> tuple[CircuitBreaker, FrozenClock, list[tuple[str, BreakerState, BreakerState]]]:
    clock = FrozenClock(T0)
    changes: list[tuple[str, BreakerState, BreakerState]] = []
    br = CircuitBreaker("lte", clock=clock, on_change=lambda m, o, n: changes.append((m, o, n)), **kw)  # type: ignore[arg-type]
    return br, clock, changes


def test_opens_after_more_than_threshold_in_window() -> None:
    br, clock, changes = make()
    for _ in range(5):
        br.record_error()
        clock.advance(10)
    assert br.state is BreakerState.CLOSED
    assert br.allows()
    br.record_error()  # 6th within 5 minutes
    assert br.state is BreakerState.OPEN
    assert br.degraded
    assert not br.allows()
    assert changes == [("lte", BreakerState.CLOSED, BreakerState.OPEN)]


def test_spread_out_errors_do_not_open() -> None:
    br, clock, _ = make()
    for _ in range(20):
        br.record_error()
        clock.advance(61)  # 5 errors per 5 min at most
    assert br.state is BreakerState.CLOSED


def test_closes_by_itself_after_quiet() -> None:
    br, clock, changes = make()
    for _ in range(6):
        br.record_error()
    clock.advance(minutes=9)
    assert br.state is BreakerState.OPEN
    clock.advance(minutes=1)
    assert br.state is BreakerState.HALF_OPEN
    assert br.allows()
    clock.advance(minutes=10)
    assert br.state is BreakerState.CLOSED
    assert [c[2] for c in changes] == [BreakerState.OPEN, BreakerState.HALF_OPEN, BreakerState.CLOSED]
    assert br.errors_in_window() == 0


def test_error_in_half_open_reopens_and_success_closes() -> None:
    br, clock, _ = make()
    for _ in range(6):
        br.record_error()
    clock.advance(minutes=10)
    assert br.state is BreakerState.HALF_OPEN
    br.record_error()
    assert br.state is BreakerState.OPEN
    clock.advance(minutes=10)
    assert br.state is BreakerState.HALF_OPEN
    br.record_success()
    assert br.state is BreakerState.CLOSED


def test_errors_while_open_extend_quiet_period() -> None:
    br, clock, _ = make()
    for _ in range(6):
        br.record_error()
    clock.advance(minutes=8)
    br.record_error()
    clock.advance(minutes=8)
    assert br.state is BreakerState.OPEN


def test_reset_and_custom_threshold() -> None:
    br, _, _ = make(threshold=1)
    br.record_error()
    assert br.state is BreakerState.CLOSED
    br.record_error()
    assert br.state is BreakerState.OPEN
    br.reset()
    assert br.state is BreakerState.CLOSED


def test_hook_failure_is_isolated() -> None:
    clock = FrozenClock(T0)

    def bad(m: str, o: BreakerState, n: BreakerState) -> None:
        raise RuntimeError("hook failed")

    br = CircuitBreaker("x", threshold=1, clock=clock, on_change=bad)
    br.record_error()
    br.record_error()
    assert br.state is BreakerState.OPEN


async def test_async_hook() -> None:
    clock = FrozenClock(T0)
    got: list[BreakerState] = []

    async def hook(m: str, o: BreakerState, n: BreakerState) -> None:
        got.append(n)

    br = CircuitBreaker("x", threshold=1, clock=clock, on_change=hook)
    br.record_error()
    br.record_error()
    for _ in range(3):
        await asyncio.sleep(0)
    assert got == [BreakerState.OPEN]
