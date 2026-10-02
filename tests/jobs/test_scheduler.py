from __future__ import annotations

import asyncio
import itertools
import time as _time
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa

from svbg.jobs import Scheduler, next_daily_run
from svbg.jobs.scheduler import previous_daily_run
from svbg.jobs.tables import scheduler_state
from tests.jobs.conftest import FakeHub, wait_until


async def _state(db: Any, task: str) -> dict[str, Any] | None:
    async with db.read() as conn:
        row = (
            (await conn.execute(sa.select(scheduler_state).where(scheduler_state.c.task == task)))
            .mappings()
            .first()
        )
    return dict(row) if row is not None else None


class OffsetClock:
    """Real time shifted by a fixed offset (lets a daily task fire "at 09:00" within a second)."""

    def __init__(self, target: datetime) -> None:
        self.offset = target - datetime.now(UTC)

    def __call__(self) -> datetime:
        return datetime.now(UTC) + self.offset


# ---------------------------------------------------------------- pure daily math


def test_next_daily_run_basic(tzdata: None) -> None:
    msk = ZoneInfo("Europe/Moscow")  # UTC+3, no DST
    now = datetime(2026, 10, 1, 5, 0, tzinfo=UTC)  # 08:00 MSK
    assert next_daily_run(now, time(9, 0), "Europe/Moscow") == datetime(2026, 10, 1, 6, 0, tzinfo=UTC)
    now2 = datetime(2026, 10, 1, 6, 0, tzinfo=UTC)  # exactly 09:00 MSK -> next day
    assert next_daily_run(now2, time(9, 0), msk) == datetime(2026, 10, 2, 6, 0, tzinfo=UTC)
    assert previous_daily_run(now2, time(9, 0), msk) == now2
    assert previous_daily_run(now, time(9, 0), msk) == datetime(2026, 9, 30, 6, 0, tzinfo=UTC)
    # Local date differs from the UTC date.
    late = datetime(2026, 10, 1, 22, 30, tzinfo=UTC)  # 01:30 MSK on Oct 2
    assert next_daily_run(late, time(1, 0), msk) == datetime(2026, 10, 2, 22, 0, tzinfo=UTC)


def test_next_daily_run_dst(tzdata: None) -> None:
    berlin = "Europe/Berlin"
    # Spring forward 2026-03-29: 02:00 -> 03:00. 02:30 does not exist -> run right after the gap (03:30 CEST).
    before = datetime(2026, 3, 28, 23, 0, tzinfo=UTC)
    got = next_daily_run(before, time(2, 30), berlin)
    assert got == datetime(2026, 3, 29, 1, 30, tzinfo=UTC)
    # Normal days keep local wall time across the switch: 09:00 CET = 08:00Z, 09:00 CEST = 07:00Z.
    assert next_daily_run(datetime(2026, 3, 28, 12, tzinfo=UTC), time(9), berlin) == datetime(
        2026, 3, 29, 7, 0, tzinfo=UTC
    )
    assert next_daily_run(datetime(2026, 3, 27, 12, tzinfo=UTC), time(9), berlin) == datetime(
        2026, 3, 28, 8, 0, tzinfo=UTC
    )
    # Fall back 2026-10-25: 02:30 happens twice -> first occurrence (CEST, 00:30Z), only once that day.
    fb = next_daily_run(datetime(2026, 10, 24, 12, tzinfo=UTC), time(2, 30), berlin)
    assert fb == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    assert next_daily_run(fb, time(2, 30), berlin) == datetime(2026, 10, 26, 1, 30, tzinfo=UTC)


def test_registration_validation(tzdata: None) -> None:
    s = Scheduler(None)

    async def fn() -> None:
        return None

    s.every("a", 1, fn)
    with pytest.raises(ValueError, match="already registered"):
        s.every("a", 1, fn)
    with pytest.raises(ValueError):
        s.every("b", 0, fn)
    with pytest.raises(ValueError, match="time zone"):
        s.daily("c", time(9), "Mars/Olympus", fn)
    with pytest.raises(ValueError):
        next_daily_run(datetime(2026, 1, 1), time(9), "UTC")


# ---------------------------------------------------------------- running


# Timing tests wait for events (``wait_until``) and assert one-sided bounds only (spacing >= interval minus
# the timer granularity, no overlap), never exact counts over a wall-clock window: Windows timers tick at
# ~15.6 ms and CI machines stall, so "N runs within T seconds" is inherently flaky.
TIMER_SLACK_S = 0.035


async def test_run_at_start_runs_immediately(db: Any) -> None:
    runs = 0

    async def tick() -> None:
        nonlocal runs
        runs += 1

    s = Scheduler(db)
    s.every("hourly", 3600, tick, run_at_start=True)  # only "at start" can explain a run within the test
    await s.start()
    try:
        await wait_until(lambda: runs == 1)
    finally:
        await s.stop()
    assert runs == 1


async def test_every_keeps_the_interval(db: Any) -> None:
    interval = 0.15
    times: list[float] = []

    async def tick() -> None:
        times.append(_time.monotonic())

    s = Scheduler(db)
    s.every("tick", interval, tick, run_at_start=True)
    await s.start()
    try:
        await wait_until(lambda: len(times) >= 4, timeout=10)
    finally:
        await s.stop()
    gaps = [b - a for a, b in itertools.pairwise(times)]
    assert all(g >= interval - TIMER_SLACK_S for g in gaps), gaps
    st = await _state(db, "tick")
    assert st is not None
    assert st["last_ok_at"] is not None and st["last_error"] is None
    assert st["next_run_at"] > st["last_run_at"]


async def test_runs_never_overlap(db: Any) -> None:
    active = 0
    peak = 0
    runs = 0

    async def slow() -> None:
        nonlocal active, peak, runs
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.12)
        runs += 1
        active -= 1

    s = Scheduler(db)
    s.every("slow", 0.01, slow, run_at_start=True)
    s.trigger("slow")
    await s.start()
    try:
        await wait_until(lambda: active == 1)
        s.trigger("slow")  # triggering a running task does not start a parallel run
        await wait_until(lambda: runs >= 3, timeout=10)
    finally:
        await s.stop()
    assert peak == 1


async def test_failing_task_does_not_affect_others(db: Any, hub: FakeHub) -> None:
    good_runs = 0

    async def good() -> None:
        nonlocal good_runs
        good_runs += 1

    async def bad() -> None:
        raise RuntimeError("remnawave is down, token 123456789:AAH" + "x" * 32)

    s = Scheduler(db, hub)
    s.every("good", 0.05, good, run_at_start=True)
    s.every("bad", 0.05, bad, run_at_start=True)
    await s.start()
    try:
        await wait_until(lambda: good_runs >= 4 and s.tasks()["bad"].failures >= 4, timeout=10)
    finally:
        await s.stop()
    tasks = s.tasks()
    assert tasks["bad"].last_ok_at is None
    bad_state = await _state(db, "bad")
    assert bad_state is not None
    assert bad_state["last_error"].startswith("RuntimeError: remnawave is down")
    assert "AAH" not in bad_state["last_error"]
    assert bad_state["last_ok_at"] is None
    assert hub.captured and all(c.place == "scheduler:bad" for c in hub.captured)
    assert "следующий" in hub.captured[0].handled
    good_state = await _state(db, "good")
    assert good_state is not None and good_state["last_error"] is None


async def test_error_clears_after_success(db: Any) -> None:
    calls = 0

    async def flaky() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("first time fails")

    s = Scheduler(db)
    s.every("flaky", 0.05, flaky, run_at_start=True)
    await s.start()
    try:
        # stop() lets the loop finish the current run and save its state, so no extra sleep is needed.
        await wait_until(lambda: s.tasks()["flaky"].last_ok_at is not None)
    finally:
        await s.stop()
    st = await _state(db, "flaky")
    assert st is not None and st["last_error"] is None and st["last_ok_at"] is not None


async def test_task_timeout(db: Any, hub: FakeHub) -> None:
    async def hang() -> None:
        await asyncio.sleep(10)

    s = Scheduler(db, hub)
    s.every("hang", 60, hang, run_at_start=True, timeout_s=0.1)
    await s.start()
    await wait_until(lambda: s.tasks()["hang"].failures == 1)
    await s.stop()
    assert s.tasks()["hang"].last_error is not None


async def test_daily_fires_at_local_time_and_records_state(db: Any, tzdata: None) -> None:
    msk = ZoneInfo("Europe/Moscow")
    target_local = datetime(2026, 10, 1, 9, 0, tzinfo=msk)
    target = target_local.astimezone(UTC)
    clock = OffsetClock(target - timedelta(seconds=0.4))  # 08:59:59.6 MSK
    fired: list[datetime] = []

    async def report() -> None:
        fired.append(clock())

    s = Scheduler(db, clock=clock)
    s.daily("daily_report", time(9, 0), "Europe/Moscow", report)
    await s.start()
    try:
        await wait_until(lambda: len(fired) == 1, timeout=5)
        # The next run is tomorrow: wait until the loop has scheduled it, then nothing more may fire today.
        await wait_until(lambda: s.tasks()["daily_report"].next_run_at == target + timedelta(days=1))
    finally:
        await s.stop()
    assert len(fired) == 1
    assert fired[0] >= target  # never early (the wait is driven by the injected wall clock)
    assert fired[0].astimezone(msk).strftime("%H:%M") == "09:00"
    st = await _state(db, "daily_report")
    assert st is not None
    assert st["next_run_at"] == datetime(2026, 10, 2, 6, 0, tzinfo=UTC)
    assert st["last_ok_at"] is not None


async def test_daily_catches_up_after_restart_once(db: Any, tzdata: None) -> None:
    # Process was down at 09:00 and is started at 09:10; the task ran yesterday -> run now.
    now_target = datetime(2026, 10, 1, 6, 10, tzinfo=UTC)  # 09:10 MSK
    clock = OffsetClock(now_target)
    async with db.tx() as conn:
        await conn.execute(
            sa.insert(scheduler_state).values(
                task="daily_report", last_run_at=datetime(2026, 9, 30, 6, 0, tzinfo=UTC)
            )
        )
    fired = 0

    async def report() -> None:
        nonlocal fired
        fired += 1

    s = Scheduler(db, clock=clock)
    s.daily("daily_report", time(9, 0), "Europe/Moscow", report, catch_up_s=3600)
    await s.start()
    try:
        await wait_until(lambda: fired == 1)
        await wait_until(
            lambda: s.tasks()["daily_report"].next_run_at == datetime(2026, 10, 2, 6, 0, tzinfo=UTC)
        )
    finally:
        await s.stop()
    assert fired == 1
    # Started again later the same day: already done -> nothing to catch up.
    s2 = Scheduler(db, clock=clock)
    s2.daily("daily_report", time(9, 0), "Europe/Moscow", report, catch_up_s=3600)
    await s2.start()
    try:
        await wait_until(lambda: s2.tasks()["daily_report"].next_run_at is not None)
    finally:
        await s2.stop()
    assert s2.tasks()["daily_report"].next_run_at == datetime(2026, 10, 2, 6, 0, tzinfo=UTC)
    assert fired == 1


async def test_every_honours_persisted_last_run(db: Any) -> None:
    async with db.tx() as conn:
        await conn.execute(sa.insert(scheduler_state).values(task="backup", last_run_at=datetime.now(UTC)))
    backup_runs = 0
    fresh_runs = 0

    async def backup() -> None:
        nonlocal backup_runs
        backup_runs += 1

    async def fresh() -> None:
        nonlocal fresh_runs
        fresh_runs += 1

    s = Scheduler(db)
    s.every("backup", 3600, backup)
    s.every("fresh", 0.1, fresh)
    await s.start()
    try:
        await wait_until(lambda: s.tasks()["backup"].next_run_at is not None)
        await wait_until(lambda: fresh_runs >= 2)  # time passes; the persisted cadence still holds
        nxt = s.tasks()["backup"].next_run_at
    finally:
        await s.stop()
    assert nxt is not None and nxt > datetime.now(UTC) + timedelta(minutes=55)
    assert backup_runs == 0


async def test_stop_waits_for_running_task_and_cancels_after_timeout(db: Any) -> None:
    finished = asyncio.Event()
    started = asyncio.Event()

    async def short() -> None:
        started.set()
        await asyncio.sleep(0.3)
        finished.set()

    s = Scheduler(db)
    s.every("short", 60, short, run_at_start=True)
    await s.start()
    await asyncio.wait_for(started.wait(), 2)
    await s.stop(timeout=5)
    assert finished.is_set()

    cancelled = asyncio.Event()
    started2 = asyncio.Event()

    async def forever() -> None:
        started2.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    s2 = Scheduler(db)
    s2.every("forever", 60, forever, run_at_start=True)
    await s2.start()
    await asyncio.wait_for(started2.wait(), 2)
    t0 = _time.monotonic()
    await s2.stop(timeout=0.2)
    assert _time.monotonic() - t0 < 2
    assert cancelled.is_set()


async def test_trigger_runs_immediately(db: Any) -> None:
    runs = 0

    async def fn() -> None:
        nonlocal runs
        runs += 1

    s = Scheduler(db)
    s.every("manual", 3600, fn)
    await s.start()
    try:
        await wait_until(lambda: s.tasks()["manual"].next_run_at is not None)  # loop is waiting
        assert runs == 0
        s.trigger("manual")
        await wait_until(lambda: runs == 1)
    finally:
        await s.stop()


async def test_works_without_database() -> None:
    runs = 0

    async def fn() -> None:
        nonlocal runs
        runs += 1

    s = Scheduler(None)
    s.every("nodb", 0.05, fn, run_at_start=True)
    await s.start()
    try:
        await wait_until(lambda: runs >= 2)
    finally:
        await s.stop()


async def test_task_added_after_start_runs(db: Any) -> None:
    runs = 0

    async def fn() -> None:
        nonlocal runs
        runs += 1

    s = Scheduler(db)
    await s.start()
    try:
        s.every("late", 0.05, fn, run_at_start=True)
        await wait_until(lambda: runs >= 1)
    finally:
        await s.stop()
