"""Periodic in-process tasks (07 §2.2 "реестр периодических задач").

* ``every(name, interval_s, fn)`` — fixed rate measured from the start of the previous run; a run that takes
  longer than the interval delays the next one (runs of the same task never overlap and never pile up);
* ``daily(name, at, tz, fn)`` — once a day at local wall time ``at`` in IANA zone ``tz`` (DST-aware);
* each run is isolated: an exception is logged, reported to the error hub (place ``scheduler:<name>``) and
  stored in ``scheduler_state.last_error``; other tasks keep running;
* ``scheduler_state`` keeps ``last_run_at``/``last_ok_at``/``next_run_at`` so restarts do not reset long
  intervals and the "Состояние" screen can show when a task last succeeded.

The scheduler is for cheap, idempotent "tick" work; heavy or must-not-lose work should enqueue a job.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from functools import partial
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from svbg.core import clock as core_clock
from svbg.jobs._support import ErrorCapture, describe_error, report
from svbg.jobs.tables import scheduler_state

if TYPE_CHECKING:
    from svbg.db.engine import Database

__all__ = ["Scheduler", "TaskFn", "TaskState", "next_daily_run", "resolve_tz"]

log = logging.getLogger("svbg.jobs.scheduler")

TaskFn = Callable[[], Awaitable[None]]
ClockFn = Callable[[], datetime]

#: Longest single sleep; wall-clock based waits re-check the clock at least this often (clock jumps, DST).
MAX_SLEEP_S = 60.0
_MAX_NAME_LEN = 100
_RNG = secrets.SystemRandom()

HANDLED_TEXT = "Периодическая задача «{name}» пропустила этот запуск; следующий — по расписанию."


@dataclass(slots=True)
class TaskState:
    """In-memory view of one registered task (mirrors ``scheduler_state``)."""

    name: str
    kind: Literal["every", "daily"]
    fn: TaskFn
    interval_s: float = 0.0
    jitter_s: float = 0.0
    run_at_start: bool = False
    at: time | None = None
    tz: tzinfo | None = None
    timeout_s: float | None = None
    catch_up_s: float = 0.0
    running: bool = False
    runs: int = 0
    failures: int = 0
    last_run_at: datetime | None = None
    last_ok_at: datetime | None = None
    last_error: str | None = None
    next_run_at: datetime | None = None
    trigger: asyncio.Event = field(default_factory=asyncio.Event)


def resolve_tz(tz: str | tzinfo) -> tzinfo:
    """``ZoneInfo`` for an IANA name; ``ValueError`` with a clear message if unknown."""
    if isinstance(tz, tzinfo):
        return tz
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ValueError(f"unknown time zone {tz!r} (expected an IANA name like 'Europe/Moscow')") from e


def _local_at(day: date, at: time, zone: tzinfo) -> datetime:
    """Aware UTC instant of wall time ``at`` on ``day`` in ``zone``.

    Ambiguous times (DST fall-back) resolve to the first occurrence; non-existent ones (spring-forward gap)
    to the instant right after the gap, i.e. the run is shifted forward, never skipped.
    """
    # PEP 495, fold=0: an ambiguous time takes the first occurrence; a time inside a gap takes the offset from
    # before the transition, which lands after the gap (02:30 on a 02:00->03:00 night becomes 03:30).
    naive = datetime.combine(day, at.replace(tzinfo=None, fold=0))
    return naive.replace(tzinfo=zone).astimezone(UTC)


def next_daily_run(now: datetime, at: time, tz: str | tzinfo) -> datetime:
    """First instant strictly after ``now`` when the local wall clock in ``tz`` shows ``at`` (UTC result)."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    zone = resolve_tz(tz)
    day = now.astimezone(zone).date() - timedelta(days=1)
    for _ in range(4):
        candidate = _local_at(day, at, zone)
        if candidate > now:
            return candidate
        day += timedelta(days=1)
    raise AssertionError("unreachable: a daily time always occurs within two days")


def previous_daily_run(now: datetime, at: time, tz: str | tzinfo) -> datetime:
    """Last instant ``<= now`` when the local wall clock in ``tz`` showed ``at`` (UTC result)."""
    zone = resolve_tz(tz)
    day = now.astimezone(zone).date() + timedelta(days=1)
    for _ in range(4):
        candidate = _local_at(day, at, zone)
        if candidate <= now:
            return candidate
        day -= timedelta(days=1)
    raise AssertionError("unreachable: a daily time always occurs within two days")


class Scheduler:
    def __init__(
        self,
        db: Database | None,
        hub: ErrorCapture | None = None,
        *,
        clock: ClockFn | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._db = db
        self._hub = hub
        self._clock: ClockFn = clock or core_clock.now
        self._monotonic = monotonic or core_clock.monotonic
        self._tasks: dict[str, TaskState] = {}
        self._loops: dict[str, asyncio.Task[None]] = {}
        self._stop = asyncio.Event()
        self._started = False

    # ------------------------------------------------------------------ registration

    def _check_new(self, name: str) -> None:
        if not name or len(name) > _MAX_NAME_LEN:
            raise ValueError(f"task name must be 1..{_MAX_NAME_LEN} characters")
        if name in self._tasks:
            raise ValueError(f"task {name!r} is already registered")

    def every(
        self,
        name: str,
        interval_s: float,
        fn: TaskFn,
        *,
        jitter_s: float = 0,
        run_at_start: bool = False,
        timeout_s: float | None = None,
    ) -> None:
        """Run ``fn`` every ``interval_s`` seconds (plus random ``0..jitter_s``)."""
        self._check_new(name)
        if interval_s <= 0 or jitter_s < 0:
            raise ValueError("interval_s must be > 0 and jitter_s >= 0")
        st = TaskState(
            name, "every", fn, interval_s=interval_s, jitter_s=jitter_s, run_at_start=run_at_start,
            timeout_s=timeout_s,
        )  # fmt: skip
        self._add(st)

    def daily(
        self,
        name: str,
        at: time,
        tz: str | tzinfo,
        fn: TaskFn,
        *,
        catch_up_s: float = 3600,
        timeout_s: float | None = None,
    ) -> None:
        """Run ``fn`` every day at local time ``at`` in zone ``tz``.

        If the process was down at the scheduled moment and the task had run before, it is run on start when
        the missed moment is at most ``catch_up_s`` seconds ago (a restart at 09:01 still sends the 09:00
        report; a restart in the evening does not).
        """
        self._check_new(name)
        zone = resolve_tz(tz)
        st = TaskState(
            name, "daily", fn, at=at.replace(tzinfo=None), tz=zone, timeout_s=timeout_s,
            catch_up_s=max(0.0, catch_up_s),
        )  # fmt: skip
        self._add(st)

    def _add(self, st: TaskState) -> None:
        self._tasks[st.name] = st
        if self._started:
            self._loops[st.name] = asyncio.create_task(self._loop(st), name=f"sched:{st.name}")

    def tasks(self) -> dict[str, TaskState]:
        return dict(self._tasks)

    def trigger(self, name: str) -> None:
        """Run a task as soon as possible ("Запустить сейчас"); if it is running, once more right after."""
        self._tasks[name].trigger.set()

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._stop.clear()
        await self._load_state()
        for st in self._tasks.values():
            self._loops[st.name] = asyncio.create_task(self._loop(st), name=f"sched:{st.name}")
        log.info("scheduler started with %d task(s)", len(self._tasks))

    async def stop(self, timeout: float = 10.0) -> None:  # noqa: ASYNC109 - graceful deadline
        """Stop scheduling; wait up to ``timeout`` s for runs in progress, then cancel them."""
        if not self._started:
            return
        self._stop.set()
        loops = list(self._loops.values())
        self._loops.clear()
        if loops:
            _done, pending = await asyncio.wait(loops, timeout=timeout)
            for t in pending:
                t.cancel()
            await asyncio.gather(*loops, return_exceptions=True)
        self._started = False

    # ------------------------------------------------------------------ internals

    async def _load_state(self) -> None:
        if self._db is None or not self._tasks:
            return
        try:
            async with self._db.read() as conn:
                rows = (
                    (
                        await conn.execute(
                            sa.select(scheduler_state).where(scheduler_state.c.task.in_(list(self._tasks)))
                        )
                    )
                    .mappings()
                    .all()
                )
        except Exception as e:  # noqa: BLE001 - state is an optimization; schedule from scratch
            log.warning("scheduler: could not load state: %s", describe_error(e))
            return
        for row in rows:
            st = self._tasks[row["task"]]
            st.last_run_at = row["last_run_at"]
            st.last_ok_at = row["last_ok_at"]
            st.last_error = row["last_error"]

    def _first_delay_every(self, st: TaskState, now: datetime) -> float:
        if st.run_at_start:
            return 0.0
        if st.last_run_at is not None:  # survive restarts: keep the cadence recorded in scheduler_state
            since = (now - st.last_run_at).total_seconds()
            return max(0.0, st.interval_s - since) + self._jitter(st)
        return st.interval_s + self._jitter(st)

    def _first_due_daily(self, st: TaskState, now: datetime) -> datetime:
        assert st.at is not None and st.tz is not None
        if st.last_run_at is not None and st.catch_up_s > 0:
            prev = previous_daily_run(now, st.at, st.tz)
            if st.last_run_at < prev and (now - prev).total_seconds() <= st.catch_up_s:
                return now
        return next_daily_run(now, st.at, st.tz)

    def _jitter(self, st: TaskState) -> float:
        return _RNG.uniform(0, st.jitter_s) if st.jitter_s > 0 else 0.0

    async def _wait(self, st: TaskState, remaining: Callable[[], float]) -> bool:
        """Sleep until ``remaining()`` <= 0 or a trigger; ``False`` when the scheduler is stopping.

        ``remaining`` is re-evaluated at least every ``MAX_SLEEP_S`` so wall-clock jumps are noticed.
        """
        while not self._stop.is_set():
            left = remaining()
            if left <= 0 or st.trigger.is_set():
                return True
            stop_wait = asyncio.ensure_future(self._stop.wait())
            trig_wait = asyncio.ensure_future(st.trigger.wait())
            try:
                await asyncio.wait(
                    {stop_wait, trig_wait},
                    timeout=min(left, MAX_SLEEP_S),
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                stop_wait.cancel()
                trig_wait.cancel()
        return False

    def _left_monotonic(self, deadline: float) -> float:
        return deadline - self._monotonic()

    def _left_wall(self, due: datetime) -> float:
        return (due - self._clock()).total_seconds()

    async def _loop(self, st: TaskState) -> None:
        if st.kind == "every":
            await self._loop_every(st)
        else:
            await self._loop_daily(st)

    async def _loop_every(self, st: TaskState) -> None:
        delay = self._first_delay_every(st, self._clock())
        deadline = self._monotonic() + delay
        st.next_run_at = self._clock() + timedelta(seconds=delay)
        while await self._wait(st, partial(self._left_monotonic, deadline)):
            st.trigger.clear()
            started = self._monotonic()
            await self._run_once(st, self._clock())
            # Fixed rate from the start of this run; an overrunning run is followed by the next one at once.
            deadline = started + st.interval_s + self._jitter(st)
            st.next_run_at = self._clock() + timedelta(seconds=max(0.0, deadline - self._monotonic()))
            await self._save_state(st)

    async def _loop_daily(self, st: TaskState) -> None:
        assert st.at is not None and st.tz is not None
        due = self._first_due_daily(st, self._clock())
        st.next_run_at = due
        while await self._wait(st, partial(self._left_wall, due)):
            st.trigger.clear()
            started = self._clock()
            await self._run_once(st, started)
            due = next_daily_run(max(self._clock(), started), st.at, st.tz)
            st.next_run_at = due
            await self._save_state(st)

    async def _run_once(self, st: TaskState, started: datetime) -> None:
        st.running = True
        st.runs += 1
        st.last_run_at = started
        try:
            async with asyncio.timeout(st.timeout_s):
                await st.fn()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - isolation boundary: one task never affects others
            st.failures += 1
            st.last_error = describe_error(e)
            log.warning("scheduled task %s failed: %s", st.name, st.last_error)
            await report(
                self._hub,
                e,
                f"scheduler:{st.name}",
                handled=HANDLED_TEXT.format(name=st.name),
                context={"task": st.name, "runs": st.runs, "failures": st.failures},
            )
        else:
            st.last_ok_at = self._clock()
            st.last_error = None
        finally:
            st.running = False

    async def _save_state(self, st: TaskState) -> None:
        if self._db is None:
            return
        values = {
            "last_run_at": st.last_run_at,
            "last_ok_at": st.last_ok_at,
            "last_error": st.last_error,
            "next_run_at": st.next_run_at,
        }
        stmt = (
            pg_insert(scheduler_state)
            .values(task=st.name, **values)
            .on_conflict_do_update(index_elements=[scheduler_state.c.task], set_=values)
        )
        try:
            async with self._db.tx() as conn:
                await conn.execute(stmt)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - DB hiccup must not stop the schedule
            log.warning("scheduler: could not save state of %s: %s", st.name, describe_error(e))
