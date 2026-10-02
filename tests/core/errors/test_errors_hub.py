from __future__ import annotations

import asyncio
import itertools
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa

from svbg.core.clock import FrozenClock
from svbg.core.errors import BreakerState, ErrorGroupView, ErrorHub, guard, render_report
from svbg.core.errors.classify import ClassifierRegistry
from svbg.core.errors.tables import error_events, error_groups
from svbg.core.log import SecretRegistry, register_secret

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
BOT_TOKEN = "123456789:AAH-abcdefghijklmnopqrstuvwxyz012345"


class RecSink:
    def __init__(self) -> None:
        self.new: list[ErrorGroupView] = []
        self.updates: list[tuple[ErrorGroupView, Any]] = []
        self.fail_new = False
        self.fail_update = False
        self.hub: ErrorHub | None = (
            None  # when set, a failing send also goes through a guard (recursion test)
        )

    async def send_new(self, view: ErrorGroupView) -> Any:
        self.new.append(view)
        render_report(view)  # every delivered view must render
        if self.hub is not None:
            async with guard("admin_chat:send", hub=self.hub):
                raise RuntimeError("telegram is down")
        if self.fail_new:
            raise RuntimeError("sink failed")
        return {"chat_id": 1, "message_id": len(self.new)}

    async def update(self, view: ErrorGroupView, msg_ref: Any) -> None:
        self.updates.append((view, msg_ref))
        render_report(view)
        if self.fail_update:
            raise ConnectionError("edit failed")


def boom(n: int) -> ValueError:
    try:
        raise ValueError(f"order {n} failed for user {n * 7}")
    except ValueError as e:
        return e


def other(n: int) -> KeyError:
    try:
        raise KeyError(f"k{n}")
    except KeyError as e:
        return e


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(T0)


@pytest.fixture
def sink() -> RecSink:
    return RecSink()


@pytest.fixture
def hub(errors_db: Any, sink: RecSink, clock: FrozenClock) -> ErrorHub:
    return ErrorHub(errors_db, sink, clock, version="test")


async def capture(hub: ErrorHub, exc: BaseException, place: str = "screen:home", **kw: Any) -> str | None:
    fp = await hub.capture(exc, place, **kw)
    await hub.drain()
    return fp


async def test_first_occurrence_sends_new_and_stores_ref(hub: ErrorHub, sink: RecSink) -> None:
    fp = await capture(hub, boom(1), user_id=5, context={"screen": "home"})
    assert fp is not None and len(fp) == 40
    assert len(sink.new) == 1
    view = sink.new[0]
    assert view.count == 1 and view.users_count == 1 and view.last_user_id == 5
    assert view.event_id is not None
    assert view.version == "test"
    stored = await hub.get(fp)
    assert stored is not None
    assert stored.chat_ref == {"chat_id": 1, "message_id": 1}


async def test_fifty_identical_one_message_bounded_updates(
    hub: ErrorHub, sink: RecSink, clock: FrozenClock
) -> None:
    fps = set()
    for i in range(50):
        fps.add(await capture(hub, boom(i), user_id=i % 3))
        clock.advance(2)  # 50 errors over 100 s
    assert len(fps) == 1
    assert len(sink.new) == 1
    assert 1 <= len(sink.updates) <= 2  # at most once per minute
    times = [v.last_seen for v, _ in sink.updates]
    assert all(b - a >= timedelta(seconds=60) for a, b in itertools.pairwise(times))
    assert all(ref == {"chat_id": 1, "message_id": 1} for _, ref in sink.updates)

    # trailing flush delivers the final count once the interval passes
    clock.advance(60)
    assert await hub.flush() == 1
    await hub.drain()
    last = sink.updates[-1][0]
    assert last.count == 50
    assert last.episode_count == 50
    assert last.users_count == 3
    assert "×50" in render_report(last)
    # nothing new → flush is a no-op
    clock.advance(120)
    assert await hub.flush() == 0


async def test_flush_waits_for_interval(hub: ErrorHub, sink: RecSink, clock: FrozenClock) -> None:
    await capture(hub, boom(1))
    clock.advance(10)
    await capture(hub, boom(2))
    assert sink.updates == []
    assert await hub.flush() == 0  # not due yet
    clock.advance(50)
    assert await hub.flush() == 1
    await hub.drain()
    assert sink.updates[-1][0].count == 2


async def test_different_errors_different_groups(hub: ErrorHub, sink: RecSink) -> None:
    a = await capture(hub, boom(1))
    b = await capture(hub, other(1))
    c = await capture(hub, boom(1), place="screen:buy")
    assert len({a, b, c}) == 3
    assert len(sink.new) == 3
    groups = await hub.open_groups()
    assert {g.fingerprint for g in groups} == {a, b, c}


async def test_mute(hub: ErrorHub, sink: RecSink, clock: FrozenClock) -> None:
    fp = await capture(hub, boom(1))
    assert fp is not None
    assert await hub.mute(fp, T0 + timedelta(hours=1))
    for _ in range(5):
        clock.advance(70)
        await capture(hub, boom(2))
    assert await hub.flush() == 0
    assert len(sink.new) == 1 and sink.updates == []
    view = await hub.get(fp)
    assert view is not None and view.count == 6 and view.status == "muted"
    # mute expires → delivery resumes
    clock.set(T0 + timedelta(hours=1, minutes=1))
    await capture(hub, boom(3))
    assert len(sink.updates) == 1
    assert (await hub.get(fp)).status == "open"  # type: ignore[union-attr]
    assert not await hub.mute("0" * 40, T0)


async def test_unmute(hub: ErrorHub, sink: RecSink, clock: FrozenClock) -> None:
    fp = await capture(hub, boom(1))
    assert fp is not None
    await hub.mute(fp, T0 + timedelta(days=1))
    await hub.unmute(fp)
    clock.advance(61)
    await capture(hub, boom(1))
    assert len(sink.updates) == 1


async def test_reopen_after_hour_of_silence(hub: ErrorHub, sink: RecSink, clock: FrozenClock) -> None:
    fp = await capture(hub, boom(1))
    clock.advance(minutes=30)
    await capture(hub, boom(1))
    clock.advance(minutes=61)
    await capture(hub, boom(1), user_id=9)
    assert len(sink.new) == 2
    again = sink.new[1]
    assert again.reopened
    assert again.episode_count == 1
    assert again.count == 3
    report = render_report(again)
    assert "🔁 снова" in report
    stored = await hub.get(fp)  # type: ignore[arg-type]
    assert stored is not None and stored.chat_ref == {"chat_id": 1, "message_id": 2}


async def test_resolved_group_reopens(hub: ErrorHub, sink: RecSink, clock: FrozenClock) -> None:
    fp = await capture(hub, boom(1))
    assert fp is not None
    assert await hub.resolve(fp)
    clock.advance(5)
    await capture(hub, boom(1))
    assert len(sink.new) == 2 and sink.new[1].reopened


async def test_users_counted_distinct(hub: ErrorHub, clock: FrozenClock) -> None:
    for uid in (1, 1, 2, None, 2, 3):
        fp = await capture(hub, boom(1), user_id=uid)
        clock.advance(1)
    view = await hub.get(fp)  # type: ignore[arg-type]
    assert view is not None
    assert view.users_count == 3
    assert view.count == 6
    assert view.last_user_id == 3


async def test_sink_failure_does_not_break_hub(hub: ErrorHub, sink: RecSink, clock: FrozenClock) -> None:
    sink.fail_new = True
    fp = await capture(hub, boom(1))
    assert fp is not None
    assert len(sink.new) == 1
    stored = await hub.get(fp)
    assert stored is not None and stored.chat_ref is None
    # within the interval nothing is retried; after it, send_new is retried (no ref to update)
    clock.advance(10)
    await capture(hub, boom(1))
    assert len(sink.new) == 1
    sink.fail_new = False
    clock.advance(60)
    await capture(hub, boom(1))
    assert len(sink.new) == 2
    assert sink.updates == []
    sink.fail_update = True
    clock.advance(61)
    await capture(hub, boom(1))  # update raises → swallowed
    assert len(sink.updates) == 1


async def test_sink_errors_are_not_reported_recursively(hub: ErrorHub, sink: RecSink) -> None:
    sink.hub = hub  # sink's own guarded send fails and calls hub.capture
    await capture(hub, boom(1))
    assert len(sink.new) == 1
    async with hub._db.read() as conn:  # type: ignore[union-attr]
        n = await conn.scalar(sa.select(sa.func.count()).select_from(error_groups))
    assert n == 1  # the admin_chat:send failure was only logged


async def test_secret_never_stored_or_delivered(
    hub: ErrorHub, sink: RecSink, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "PanelS3cretValue!"
    register_secret(secret)
    caplog.set_level(logging.DEBUG, logger="svbg.errors")
    try:
        fp = await capture(
            hub,
            RuntimeError(f"401 for token {secret} / {BOT_TOKEN}"),
            context={"url": f"https://x/bot{BOT_TOKEN}/getMe", "note": secret, "password": "hunter2"},
        )
        view = sink.new[0]
        report = render_report(view)
        for text in (view.message, view.stack, report):
            assert secret not in text and BOT_TOKEN not in text
        async with hub._db.read() as conn:  # type: ignore[union-attr]
            sample = await conn.scalar(
                sa.select(error_groups.c.sample).where(error_groups.c.fingerprint == fp)
            )
            ctx = await conn.scalar(sa.select(error_events.c.context).where(error_events.c.fingerprint == fp))
        dump = repr(sample) + repr(ctx)
        assert secret not in dump and BOT_TOKEN not in dump and "hunter2" not in dump
        assert "error at screen:home" in caplog.text
        assert secret not in caplog.text and BOT_TOKEN not in caplog.text
    finally:
        SecretRegistry.unregister(secret)


async def test_breaker_via_hub(errors_db: Any, sink: RecSink, clock: FrozenClock) -> None:
    changes: list[tuple[str, BreakerState, BreakerState]] = []
    hub = ErrorHub(errors_db, sink, clock, on_state_change=lambda m, o, n: changes.append((m, o, n)))
    for i in range(6):
        await capture(hub, boom(i), place="slot:lte.status", module="lte")
        clock.advance(5)
    assert hub.breaker("lte").state is BreakerState.OPEN
    assert hub.breakers() == {"lte": BreakerState.OPEN}
    clock.advance(minutes=10)
    await hub.flush()  # evaluates breakers on time
    assert changes[-1][2] is BreakerState.HALF_OPEN
    clock.advance(minutes=10)
    await hub.flush()
    assert hub.breaker("lte").state is BreakerState.CLOSED
    assert [c[2] for c in changes] == [BreakerState.OPEN, BreakerState.HALF_OPEN, BreakerState.CLOSED]


async def test_purge(hub: ErrorHub, clock: FrozenClock) -> None:
    hub.events_per_group = 3
    fp = await capture(hub, boom(1))
    for _ in range(5):
        clock.advance(1)
        await capture(hub, boom(1))
    fp2 = await capture(hub, other(1))
    clock.advance(days=15)
    fresh = await capture(hub, other(2), place="screen:x")
    deleted = await hub.purge()
    async with hub._db.read() as conn:  # type: ignore[union-attr]
        rows = (await conn.execute(sa.select(error_events.c.fingerprint))).mappings().all()
    assert [r["fingerprint"] for r in rows] == [fresh]
    assert deleted == 7
    assert await hub.get(fp) is not None  # group kept (90 days)
    clock.advance(days=100)
    await hub.purge()
    assert await hub.get(fp) is None and await hub.get(fp2) is None  # type: ignore[arg-type]


async def test_start_stop_loop(errors_db: Any, sink: RecSink, clock: FrozenClock) -> None:
    hub = ErrorHub(errors_db, sink, clock, flush_interval=0.01)
    await capture(hub, boom(1))
    clock.advance(1)
    await capture(hub, boom(1))
    clock.advance(61)
    await hub.start()
    for _ in range(200):
        if sink.updates:
            break
        await asyncio.sleep(0.01)
    await hub.stop()
    assert len(sink.updates) == 1


async def test_hub_without_sink(errors_db: Any, clock: FrozenClock) -> None:
    hub = ErrorHub(errors_db, None, clock)
    fp = await capture(hub, boom(1))
    assert fp is not None and (await hub.get(fp)) is not None
    hub.set_sink(RecSink())


class BrokenDb:
    def __init__(self) -> None:
        self.calls = 0

    @asynccontextmanager
    async def tx(self) -> Any:
        self.calls += 1
        raise OSError("connection refused")
        yield  # pragma: no cover

    read = tx


async def test_database_down_falls_back_to_memory(sink: RecSink, clock: FrozenClock) -> None:
    db = BrokenDb()
    hub = ErrorHub(db, sink, clock)  # type: ignore[arg-type]
    for _ in range(5):
        assert await capture(hub, boom(1)) is not None
        clock.advance(1)
    assert len(sink.new) == 1  # deduplicated in memory
    assert sink.new[0].extra == {"degraded": "database unavailable"}
    assert db.calls == 1  # DB is not hammered while marked down
    clock.advance(30)
    await capture(hub, boom(1))
    assert db.calls == 2  # retried after the pause
    clock.advance(minutes=61)
    await capture(hub, boom(1))
    assert len(sink.new) == 2 and sink.new[1].reopened


async def test_unreachable_database_is_bounded(sink: RecSink, clock: FrozenClock) -> None:
    class SlowDb:
        @asynccontextmanager
        async def tx(self) -> Any:
            await asyncio.sleep(10)  # a connection never arrives
            yield None  # pragma: no cover

    hub = ErrorHub(SlowDb(), sink, clock, db_timeout=0.05, busy_retry=0.01)  # type: ignore[arg-type]
    loop = asyncio.get_running_loop()
    began = loop.time()
    assert await hub.capture(boom(1), "screen:home") is not None
    assert loop.time() - began < 1  # the caller is not held
    assert sink.new == [] and hub._db_down_until is None  # busy, not down: queued for a retry
    clock.advance(31)  # still no connection after db_retry_after -> treated as down
    await hub.drain()
    assert len(sink.new) == 1
    assert sink.new[0].extra == {"degraded": "database unavailable"}
    assert hub._db_down_until is not None and hub._pending == 0


async def test_no_db_hub_and_hostile_sink(clock: FrozenClock) -> None:
    class HangingSink(RecSink):
        async def send_new(self, view: ErrorGroupView) -> Any:
            await asyncio.sleep(10)

    hub = ErrorHub(None, HangingSink(), clock, sink_timeout=0.05)
    assert await capture(hub, boom(1)) is not None
    assert await hub.get("x") is None
    assert await hub.open_groups() == []
    assert await hub.purge() == 0
    assert await hub.flush() == 0
    assert not await hub.mute("x", T0)


async def test_capture_never_raises_on_internal_failure(clock: FrozenClock, sink: RecSink) -> None:
    classifier = ClassifierRegistry()
    hub = ErrorHub(None, sink, clock, classifier=classifier)

    def broken_classify(exc: BaseException) -> Any:
        raise RuntimeError("bug in hub")

    classifier.classify = broken_classify  # type: ignore[method-assign]
    assert await hub.capture(boom(1), "p") is None


async def test_capture_propagates_cancellation(sink: RecSink, clock: FrozenClock) -> None:
    started = asyncio.Event()

    class BlockingDb:
        @asynccontextmanager
        async def tx(self) -> Any:
            started.set()
            await asyncio.sleep(10)
            yield None  # pragma: no cover

    hub = ErrorHub(BlockingDb(), sink, clock)  # type: ignore[arg-type]
    task = asyncio.create_task(hub.capture(boom(1), "p"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert hub._pending == 0


async def test_overload_goes_to_memory(errors_db: Any, sink: RecSink, clock: FrozenClock) -> None:
    hub = ErrorHub(errors_db, sink, clock, max_pending=0)
    await capture(hub, boom(1))
    assert hub.dropped_to_memory == 1
    assert len(sink.new) == 1


async def test_concurrent_first_occurrences_send_once(hub: ErrorHub, sink: RecSink) -> None:
    fps = await asyncio.gather(*(hub.capture(boom(i), "screen:home", user_id=i) for i in range(20)))
    await hub.drain()
    assert len(set(fps)) == 1
    assert len(sink.new) == 1
    view = await hub.get(fps[0])  # type: ignore[arg-type]
    assert view is not None and view.count == 20 and view.users_count == 20


# --- delivery retries (a report must not be lost when the first send fails) -----------------------------


class NoneSink(RecSink):
    """Sink that reports "not delivered" (``None``) like the owner-DM sink before the bot is ready."""

    def __init__(self, fails: int) -> None:
        super().__init__()
        self.fails = fails

    async def send_new(self, view: ErrorGroupView) -> Any:
        ref = await super().send_new(view)
        if self.fails > 0:
            self.fails -= 1
            return None
        return ref


async def test_failed_first_delivery_is_retried_by_flush(
    hub: ErrorHub, sink: RecSink, clock: FrozenClock
) -> None:
    sink.fail_new = True
    fp = await capture(hub, boom(1))
    assert fp is not None and len(sink.new) == 1
    sink.fail_new = False
    assert await hub.flush() == 0  # backoff not elapsed yet
    clock.advance(60)
    assert await hub.flush() == 1
    await hub.drain()
    assert len(sink.new) == 2 and sink.new[1].count == 1
    stored = await hub.get(fp)
    assert stored is not None and stored.chat_ref == {"chat_id": 1, "message_id": 2}
    clock.advance(600)
    assert await hub.flush() == 0  # delivered: nothing left to retry
    assert sink.updates == []


async def test_send_new_returning_none_is_retried(errors_db: Any, clock: FrozenClock) -> None:
    sink = NoneSink(fails=1)
    hub = ErrorHub(errors_db, sink, clock, version="test")
    fp = await capture(hub, boom(1))
    clock.advance(60)
    assert await hub.flush() == 1
    await hub.drain()
    assert len(sink.new) == 2
    stored = await hub.get(fp)  # type: ignore[arg-type]
    assert stored is not None and stored.chat_ref == {"chat_id": 1, "message_id": 2}


async def test_delivery_retry_backs_off_and_gives_up(errors_db: Any, clock: FrozenClock) -> None:
    sink = NoneSink(fails=100)
    hub = ErrorHub(errors_db, sink, clock, version="test", delivery_attempts=3)
    await capture(hub, boom(1))  # attempt 1 fails -> retry in 60 s
    clock.advance(59)
    assert await hub.flush() == 0
    clock.advance(1)
    assert await hub.flush() == 1  # attempt 2 fails -> retry in 120 s
    await hub.drain()
    clock.advance(119)
    assert await hub.flush() == 0
    clock.advance(1)
    assert await hub.flush() == 1  # attempt 3 fails -> give up
    await hub.drain()
    clock.advance(minutes=30)
    assert await hub.flush() == 0
    assert len(sink.new) == 3


async def test_failed_update_is_retried(hub: ErrorHub, sink: RecSink, clock: FrozenClock) -> None:
    await capture(hub, boom(1))
    sink.fail_update = True
    clock.advance(61)
    await capture(hub, boom(1))
    assert len(sink.updates) == 1
    sink.fail_update = False
    clock.advance(60)
    assert await hub.flush() == 1  # notified_count already equals count, the retry still re-sends
    await hub.drain()
    assert len(sink.updates) == 2 and sink.updates[-1][0].count == 2
    assert len(sink.new) == 1


async def test_retry_skips_resolved_group(hub: ErrorHub, sink: RecSink, clock: FrozenClock) -> None:
    sink.fail_new = True
    fp = await capture(hub, boom(1))
    sink.fail_new = False
    assert await hub.resolve(fp)  # type: ignore[arg-type]
    clock.advance(60)
    assert await hub.flush() == 0
    assert len(sink.new) == 1


async def test_in_memory_report_is_retried(sink: RecSink, clock: FrozenClock) -> None:
    hub = ErrorHub(BrokenDb(), sink, clock)  # type: ignore[arg-type]
    sink.fail_new = True
    await capture(hub, boom(1))
    sink.fail_new = False
    clock.advance(60)
    assert await hub.flush() == 1
    await hub.drain()
    assert len(sink.new) == 2 and sink.new[1].extra == {"degraded": "database unavailable"}
    clock.advance(600)
    assert await hub.flush() == 0


# --- load control (an error storm must not exhaust the application's DB pool) --------------------------


class CountingDb:
    """Wraps the test database: counts transactions and the peak of concurrently held connections."""

    def __init__(self, inner: Any, *, busy_first: float = 0.0) -> None:
        self.inner = inner
        self.busy_first = busy_first
        self.txs = 0
        self.active = 0
        self.peak = 0

    @asynccontextmanager
    async def tx(self) -> Any:
        if self.busy_first:
            delay, self.busy_first = self.busy_first, 0.0
            await asyncio.sleep(delay)  # waiting for a pool connection
        async with self.inner.tx() as conn:
            self.txs += 1
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                await asyncio.sleep(0.005)  # make overlapping transactions observable
                yield conn
            finally:
                self.active -= 1

    def read(self) -> Any:
        return self.inner.read()


async def test_storm_of_one_error_is_merged(errors_db: Any, sink: RecSink, clock: FrozenClock) -> None:
    db = CountingDb(errors_db)
    hub = ErrorHub(db, sink, clock, version="test")  # type: ignore[arg-type]
    fps = await asyncio.gather(*(hub.capture(boom(i), "screen:home", user_id=i % 7) for i in range(200)))
    await hub.drain()
    assert len(set(fps)) == 1 and hub._pending == 0
    assert db.txs <= 5  # merged writes instead of 200 FOR UPDATE transactions
    assert db.peak <= 3
    assert hub.dropped_to_memory == 0
    assert len(sink.new) == 1
    view = await hub.get(fps[0])  # type: ignore[arg-type]
    assert view is not None and view.count == 200 and view.episode_count == 200 and view.users_count == 7
    async with errors_db.read() as conn:
        events = await conn.scalar(sa.select(sa.func.count()).select_from(error_events))
    assert 1 < events <= 51  # the first one plus at most one capped batch
    clock.advance(60)
    assert await hub.flush() == 1  # the merged repeats show up as one throttled update
    await hub.drain()
    assert sink.updates[-1][0].count == 200


async def test_storm_of_many_errors_bounds_connections(
    errors_db: Any, sink: RecSink, clock: FrozenClock
) -> None:
    db = CountingDb(errors_db)
    hub = ErrorHub(db, sink, clock, version="test")  # type: ignore[arg-type]
    await asyncio.gather(*(hub.capture(boom(1), f"screen:s{i}") for i in range(30)))
    await hub.drain()
    assert db.peak <= 3
    assert hub.dropped_to_memory == 10  # max_pending=20 groups written at once, the rest in memory
    assert len(sink.new) == 30  # every distinct error is still reported once
    assert hub._db_down_until is None


async def test_busy_pool_delays_write_without_marking_down(
    errors_db: Any, sink: RecSink, clock: FrozenClock
) -> None:
    # The pool stays busy longer than db_timeout; the statements themselves get a timeout a loaded machine
    # meets (0.05 s marked a slow real INSERT as «database down» under a parallel suite).
    db = CountingDb(errors_db, busy_first=1.0)
    hub = ErrorHub(db, sink, clock, version="test", db_timeout=0.5, busy_retry=0.01)  # type: ignore[arg-type]
    fp = await hub.capture(boom(1), "screen:home")
    assert sink.new == []
    await hub.drain()
    assert hub._db_down_until is None and hub.dropped_to_memory == 0
    assert len(sink.new) == 1 and sink.new[0].extra == {}  # a normal report, not a degraded one
    view = await hub.get(fp)  # type: ignore[arg-type]
    assert view is not None and view.count == 1


async def test_slow_query_marks_database_down(sink: RecSink, clock: FrozenClock) -> None:
    class SlowConn:
        async def execute(self, stmt: Any) -> Any:
            await asyncio.sleep(10)

    class SlowQueryDb:
        @asynccontextmanager
        async def tx(self) -> Any:
            yield SlowConn()

    hub = ErrorHub(SlowQueryDb(), sink, clock, db_timeout=0.05)  # type: ignore[arg-type]
    assert await capture(hub, boom(1)) is not None
    assert hub._db_down_until is not None
    assert len(sink.new) == 1 and sink.new[0].extra == {"degraded": "database unavailable"}
