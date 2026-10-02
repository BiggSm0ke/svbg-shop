from __future__ import annotations

import asyncio
import random
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa

from svbg.jobs import Job, JobContext, JobQueue, JobWorker, PermanentJobError, RetryJob, enqueue
from svbg.jobs.tables import jobs
from tests.jobs.conftest import FakeHub, wait_until


async def _statuses(db: Any) -> Counter[str]:
    async with db.read() as conn:
        rows = (await conn.execute(sa.select(jobs.c.status))).all()
    return Counter(r[0] for r in rows)


async def _row(db: Any, job_id: int) -> dict[str, Any]:
    async with db.read() as conn:
        row = (await conn.execute(sa.select(jobs).where(jobs.c.id == job_id))).mappings().first()
    assert row is not None
    return dict(row)


async def _all_done(db: Any, n: int) -> bool:
    return (await _statuses(db)).get("done", 0) == n


def make_worker(db: Any, handlers: dict[str, Any], **kw: Any) -> JobWorker:
    kw.setdefault("poll_interval", 0.2)
    return JobWorker(db, JobQueue(db), handlers, **kw)


async def test_two_workers_execute_each_job_exactly_once(db: Any, hub: FakeHub) -> None:
    runs: Counter[int] = Counter()

    async def handler(job: Job, ctx: JobContext) -> None:
        runs[job.payload["i"]] += 1
        await asyncio.sleep(random.random() * 0.005)

    async with db.tx() as conn:
        for i in range(200):
            await enqueue(conn, "work", {"i": i})
    workers = [
        make_worker(db, {"work": handler}, concurrency={"background": 4}, hub=hub, worker_id=f"w{n}")
        for n in range(2)
    ]
    for w in workers:
        await w.start()
    try:
        await wait_until(lambda: _all_done(db, 200), timeout=30)
    finally:
        for w in workers:
            await w.stop()
    assert len(runs) == 200
    assert set(runs.values()) == {1}
    assert hub.captured == []


async def test_ordering_key_fifo_with_concurrency(db: Any) -> None:
    order: dict[str, list[int]] = {"sub:1": [], "sub:2": []}
    active: Counter[str] = Counter()
    overlap: list[str] = []

    async def handler(job: Job, ctx: JobContext) -> None:
        key = job.ordering_key or ""
        active[key] += 1
        if active[key] > 1:
            overlap.append(key)
        await asyncio.sleep(random.random() * 0.01)
        order[key].append(job.payload["n"])
        active[key] -= 1

    async with db.tx() as conn:
        for n in range(25):
            await enqueue(conn, "panel", {"n": n}, ordering_key="sub:1")
            await enqueue(conn, "panel", {"n": n}, ordering_key="sub:2")
    w = make_worker(db, {"panel": handler}, concurrency={"background": 6})
    await w.start()
    try:
        await wait_until(lambda: _all_done(db, 50), timeout=30)
    finally:
        await w.stop()
    assert order["sub:1"] == list(range(25))
    assert order["sub:2"] == list(range(25))
    assert overlap == []


async def test_notify_wakes_worker_without_poll_tick(db: Any) -> None:
    done = asyncio.Event()

    async def handler(job: Job, ctx: JobContext) -> None:
        done.set()

    w = make_worker(db, {"fast": handler}, poll_interval=30, concurrency={"interactive": 2})
    await w.start()
    try:
        await asyncio.sleep(0.3)  # let the first (empty) claim pass so only NOTIFY can wake the loop
        t0 = time.monotonic()
        async with db.tx() as conn:
            await enqueue(conn, "fast", {}, lane="interactive")
        await asyncio.wait_for(done.wait(), timeout=1.0)
        assert time.monotonic() - t0 < 1.0
    finally:
        await w.stop()


async def test_unknown_kind_goes_dead_with_clear_error(db: Any, hub: FakeHub) -> None:
    async def handler(job: Job, ctx: JobContext) -> None:
        return None

    q = JobQueue(db)
    job_id = await q.enqueue("mystery", {})
    w = make_worker(db, {"known": handler}, hub=hub)
    await w.start()
    try:
        await wait_until(lambda: _is(db, job_id, "dead"))
    finally:
        await w.stop()
    row = await _row(db, job_id)
    assert "unknown job kind 'mystery'" in row["last_error"]
    assert "known" in row["last_error"]
    assert row["attempts"] == 1
    await wait_until(lambda: len(hub.captured) == 1)
    assert hub.captured[0].place == "job:mystery"
    assert isinstance(hub.captured[0].exc, LookupError)


async def _is(db: Any, job_id: int, status: str) -> bool:
    return (await _row(db, job_id))["status"] == status


async def test_handler_exception_is_isolated_and_reported(db: Any, hub: FakeHub) -> None:
    ok: list[int] = []

    async def good(job: Job, ctx: JobContext) -> None:
        ok.append(job.id)

    async def bad(job: Job, ctx: JobContext) -> None:
        raise RuntimeError("panel said 500 for 123456789:AAH" + "x" * 32)

    q = JobQueue(db)
    bad_id = await q.enqueue("bad", {}, queue="panel", max_attempts=5)
    good_ids = [await q.enqueue("good", {}) for _ in range(5)]
    w = make_worker(db, {"good": good, "bad": bad}, hub=hub, concurrency={"background": 2})
    await w.start()
    try:
        await wait_until(lambda: len(ok) == 5)
        await wait_until(lambda: len(hub.captured) >= 1)
    finally:
        await w.stop()
    assert sorted(ok) == good_ids
    row = await _row(db, bad_id)
    assert row["status"] == "ready"  # scheduled for a retry with backoff
    assert row["attempts"] == 1
    assert row["last_error"].startswith("RuntimeError: panel said 500")
    assert "AAH" not in row["last_error"]
    cap = hub.captured[0]
    assert cap.place == "job:bad"
    assert "повторена" in cap.handled
    assert cap.context is not None and cap.context["job_id"] == bad_id
    assert cap.context["queue"] == "panel"


async def test_dead_after_max_attempts_and_retry_dead(db: Any, hub: FakeHub) -> None:
    calls: list[int] = []
    healthy = asyncio.Event()

    async def flaky(job: Job, ctx: JobContext) -> None:
        calls.append(job.attempts)
        if not healthy.is_set():
            raise RetryJob(0.05, "panel unavailable")

    q = JobQueue(db)
    job_id = await q.enqueue("flaky", {}, max_attempts=3)
    w = make_worker(db, {"flaky": flaky}, hub=hub, poll_interval=0.05)
    await w.start()
    try:
        await wait_until(lambda: _is(db, job_id, "dead"))
        assert calls == [1, 2, 3]
        assert (await _row(db, job_id))["last_error"] == "panel unavailable"
        healthy.set()
        await q.retry_dead(job_id)
        await wait_until(lambda: _is(db, job_id, "done"))
    finally:
        await w.stop()
    assert calls == [1, 2, 3, 1]
    assert hub.captured == []  # RetryJob is a controlled retry, not an error


async def test_generic_failure_on_last_attempt_reports_dead(db: Any, hub: FakeHub) -> None:
    async def boom(job: Job, ctx: JobContext) -> None:
        raise ValueError("bad state")

    q = JobQueue(db)
    job_id = await q.enqueue("boom", {}, max_attempts=1)
    w = make_worker(db, {"boom": boom}, hub=hub)
    await w.start()
    try:
        await wait_until(lambda: _is(db, job_id, "dead"))
        await wait_until(lambda: len(hub.captured) == 1)
    finally:
        await w.stop()
    assert "остановлена" in hub.captured[0].handled


async def test_permanent_error_goes_dead_without_retries(db: Any, hub: FakeHub) -> None:
    calls = 0

    async def handler(job: Job, ctx: JobContext) -> None:
        nonlocal calls
        calls += 1
        raise PermanentJobError("order 5 does not exist")

    q = JobQueue(db)
    job_id = await q.enqueue("perm", {})
    w = make_worker(db, {"perm": handler}, hub=hub)
    await w.start()
    try:
        await wait_until(lambda: _is(db, job_id, "dead"))
    finally:
        await w.stop()
    assert calls == 1
    assert "order 5 does not exist" in (await _row(db, job_id))["last_error"]


async def test_job_timeout(db: Any, hub: FakeHub) -> None:
    async def slow(job: Job, ctx: JobContext) -> None:
        await asyncio.sleep(10)

    q = JobQueue(db)
    job_id = await q.enqueue("slow", {})
    w = make_worker(db, {"slow": slow}, hub=hub, timeouts={"slow": 0.2})
    await w.start()
    try:
        await wait_until(lambda: len(hub.captured) == 1)
    finally:
        await w.stop()
    row = await _row(db, job_id)
    assert row["status"] == "ready"
    assert "0.2s" in row["last_error"]
    assert "0.2" in hub.captured[0].handled


async def test_handler_raising_its_own_timeout_is_a_normal_failure(db: Any, hub: FakeHub) -> None:
    async def handler(job: Job, ctx: JobContext) -> None:
        raise TimeoutError("upstream timed out")

    q = JobQueue(db)
    job_id = await q.enqueue("t", {})
    w = make_worker(db, {"t": handler}, hub=hub)
    await w.start()
    try:
        await wait_until(lambda: len(hub.captured) == 1)
    finally:
        await w.stop()
    assert (await _row(db, job_id))["last_error"] == "TimeoutError: upstream timed out"


async def test_broken_hub_does_not_break_worker(db: Any) -> None:
    broken = FakeHub(fail=True)
    ok: list[int] = []

    async def bad(job: Job, ctx: JobContext) -> None:
        raise RuntimeError("x")

    async def good(job: Job, ctx: JobContext) -> None:
        ok.append(job.id)

    q = JobQueue(db)
    await q.enqueue("bad", {})
    await q.enqueue("good", {})
    w = make_worker(db, {"bad": bad, "good": good}, hub=broken, concurrency={"background": 1})
    await w.start()
    try:
        await wait_until(lambda: len(broken.captured) == 1 and len(ok) == 1)
        await q.enqueue("good", {})
        await wait_until(lambda: len(ok) == 2)
    finally:
        await w.stop()


async def test_graceful_stop_waits_for_running_jobs(db: Any) -> None:
    started = asyncio.Event()
    finished: list[int] = []

    async def handler(job: Job, ctx: JobContext) -> None:
        started.set()
        await asyncio.sleep(0.5)
        finished.append(job.id)

    q = JobQueue(db)
    job_id = await q.enqueue("long", {})
    w = make_worker(db, {"long": handler})
    await w.start()
    await asyncio.wait_for(started.wait(), 5)
    await w.stop(timeout=5)
    assert finished == [job_id]
    assert await _is(db, job_id, "done")
    assert w.running_jobs == 0


async def test_stop_timeout_releases_unfinished_job(db: Any) -> None:
    started = asyncio.Event()

    async def handler(job: Job, ctx: JobContext) -> None:
        started.set()
        await asyncio.sleep(30)

    q = JobQueue(db)
    job_id = await q.enqueue("stuck", {})
    w = make_worker(db, {"stuck": handler})
    await w.start()
    await asyncio.wait_for(started.wait(), 5)
    t0 = time.monotonic()
    await w.stop(timeout=0.2)
    assert time.monotonic() - t0 < 3
    row = await _row(db, job_id)
    assert (row["status"], row["attempts"]) == ("ready", 0)  # handed back, attempt not burned


async def test_stop_cut_off_by_callers_deadline_still_releases_jobs(db: Any) -> None:
    """The app bounds every stop step: when its deadline cancels ``stop(timeout=30)`` midway, running jobs
    must still be handed back before the database is closed (not left ``running`` until the lease ends)."""
    started = asyncio.Event()

    async def handler(job: Job, ctx: JobContext) -> None:
        started.set()
        await asyncio.sleep(30)

    q = JobQueue(db)
    job_id = await q.enqueue("stuck", {})
    w = make_worker(db, {"stuck": handler})
    await w.start()
    await asyncio.wait_for(started.wait(), 5)
    t0 = time.monotonic()
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.3):
            await w.stop(timeout=30)
    assert time.monotonic() - t0 < 5
    row = await _row(db, job_id)
    assert (row["status"], row["attempts"]) == ("ready", 0)
    assert w.running_jobs == 0
    await w.stop()  # already stopped: a no-op


async def test_stop_does_not_hang_on_a_loop_stuck_in_the_database(
    db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import svbg.jobs.worker as worker_mod

    monkeypatch.setattr(worker_mod, "_LOOP_EXIT_TIMEOUT", 0.2)
    entered = asyncio.Event()

    async def hung_reap(self: JobQueue, *a: Any, **kw: Any) -> int:
        entered.set()
        await asyncio.sleep(3600)  # a statement on a dead TCP connection
        return 0

    monkeypatch.setattr(JobQueue, "reap_expired", hung_reap)
    w = make_worker(db, {})
    await w.start()
    await asyncio.wait_for(entered.wait(), 5)
    t0 = time.monotonic()
    await w.stop(timeout=1)
    assert time.monotonic() - t0 < 3


async def test_stopped_worker_claims_nothing(db: Any) -> None:
    ran: list[int] = []

    async def handler(job: Job, ctx: JobContext) -> None:
        ran.append(job.id)

    w = make_worker(db, {"k": handler})
    await w.start()
    await w.stop()
    await JobQueue(db).enqueue("k", {})
    await asyncio.sleep(0.5)
    assert ran == []


async def test_expired_lease_is_picked_up_by_another_worker(db: Any) -> None:
    q = JobQueue(db)
    job_id = await q.enqueue("k", {})
    # A worker claimed it and died (never completes, never renews).
    [ghost] = await q.claim("background", "ghost", 1, lease_s=1)
    ran: list[tuple[int, int]] = []

    async def handler(job: Job, ctx: JobContext) -> None:
        ran.append((job.id, job.attempts))

    w = make_worker(db, {"k": handler}, reap_interval=0.2, worker_id="alive")
    await w.start()
    try:
        elapsed = await wait_until(lambda: _is(db, job_id, "done"), timeout=5)
    finally:
        await w.stop()
    assert ran == [(job_id, 2)]
    assert elapsed < 3
    assert await q.complete(ghost.id, worker_id="ghost", attempt=1) is False


async def test_lease_renewal_keeps_long_job_owned(db: Any) -> None:
    runs: Counter[int] = Counter()

    async def handler(job: Job, ctx: JobContext) -> None:
        runs[job.id] += 1
        await asyncio.sleep(2.5)

    q = JobQueue(db)
    job_id = await q.enqueue("long", {})
    a = make_worker(db, {"long": handler}, lease_s=1, reap_interval=0.2, worker_id="a")
    b = make_worker(db, {"long": handler}, lease_s=1, reap_interval=0.2, worker_id="b")
    await a.start()
    await b.start()
    try:
        await wait_until(lambda: _is(db, job_id, "done"), timeout=10)
    finally:
        await a.stop()
        await b.stop()
    assert runs == Counter({job_id: 1})


async def test_delayed_job_runs_when_due(db: Any) -> None:
    ran = asyncio.Event()

    async def handler(job: Job, ctx: JobContext) -> None:
        ran.set()

    w = make_worker(db, {"later": handler}, poll_interval=0.1)
    await w.start()
    try:
        async with db.tx() as conn:
            await enqueue(conn, "later", {}, run_at=datetime.now(UTC) + timedelta(seconds=0.5))
        await asyncio.sleep(0.3)
        assert not ran.is_set()
        await asyncio.wait_for(ran.wait(), 3)
    finally:
        await w.stop()


async def test_context_and_deps_passed_to_handler(db: Any) -> None:
    seen: list[JobContext] = []

    async def handler(job: Job, ctx: JobContext) -> None:
        seen.append(ctx)

    q = JobQueue(db)
    await q.enqueue("k", {})
    w = make_worker(db, {"k": handler}, deps={"notifier": "N"}, worker_id="w-ctx")
    await w.start()
    try:
        await wait_until(lambda: len(seen) == 1)
    finally:
        await w.stop()
    assert seen[0].worker_id == "w-ctx"
    assert seen[0].deps["notifier"] == "N"
    assert seen[0].db is db


async def test_worker_survives_claim_errors(db: Any, hub: FakeHub) -> None:
    class FlakyQueue(JobQueue):
        fails = 2

        async def claim(self, lane: str, worker_id: str, limit: int, lease_s: int = 60) -> list[Job]:
            if self.fails > 0:
                self.fails -= 1
                raise ConnectionError("db down")
            return await super().claim(lane, worker_id, limit, lease_s)

    ran = asyncio.Event()

    async def handler(job: Job, ctx: JobContext) -> None:
        ran.set()

    q = FlakyQueue(db)
    await q.enqueue("k", {})
    w = JobWorker(db, q, {"k": handler}, hub=hub, poll_interval=0.1, concurrency={"background": 1})
    await w.start()
    try:
        await asyncio.wait_for(ran.wait(), 10)
    finally:
        await w.stop()
    assert any(c.place == "jobs:claim:background" for c in hub.captured)


async def test_constructor_validation(db: Any) -> None:
    with pytest.raises(ValueError, match="lanes"):
        JobWorker(db, JobQueue(db), {}, concurrency={"turbo": 1})
    with pytest.raises(ValueError):
        JobWorker(db, JobQueue(db), {}, concurrency={"background": -1})


async def test_event_during_run_triggers_one_more_run(db: Any) -> None:
    """At-least-once for deduplicated jobs: a change made after the handler read the state is processed."""
    state = {"value": 1}
    seen: list[int] = []
    read_done = asyncio.Event()
    proceed = asyncio.Event()

    async def sync(job: Job, ctx: JobContext) -> None:
        seen.append(state["value"])
        read_done.set()
        await proceed.wait()

    q = JobQueue(db)
    job_id = await q.enqueue("sync", {}, dedup_key="sync:1")
    w = make_worker(db, {"sync": sync})
    await w.start()
    try:
        await asyncio.wait_for(read_done.wait(), 5)
        state["value"] = 2  # changed after the handler read it
        assert await q.enqueue("sync", {}, dedup_key="sync:1") is None
        proceed.set()
        await wait_until(lambda: len(seen) == 2)
        await wait_until(lambda: _is(db, job_id, "done"))
    finally:
        await w.stop()
    assert seen == [1, 2]
    assert (await _statuses(db)) == Counter({"done": 1})


async def test_handlers_registered_after_construction_are_used(db: Any, hub: FakeHub) -> None:
    """The worker keeps the owner's dict: handlers added later (modules wired after the worker) are seen."""
    handlers: dict[str, Any] = {}
    ran: list[str] = []

    async def late(job: Job, ctx: JobContext) -> None:
        ran.append(job.kind)

    w = make_worker(db, handlers, hub=hub)
    handlers["late"] = late  # e.g. App.job_handlers filled by a module after _build_jobs
    await w.start()
    try:
        job_id = await JobQueue(db).enqueue("late", {})
        await wait_until(lambda: _is(db, job_id, "done"))
        w.register("later", late)
        assert handlers["later"] is late
        job2 = await JobQueue(db).enqueue("later", {})
        await wait_until(lambda: _is(db, job2, "done"))
    finally:
        await w.stop()
    assert ran == ["late", "later"]
    assert hub.captured == []
