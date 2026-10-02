from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa

from svbg.jobs import JobQueue, JobStateError, enqueue
from svbg.jobs.queue import BACKOFF_CAP_S, NOTIFY_CHANNEL
from svbg.jobs.tables import jobs

BOT_TOKEN = "123456789:AAH" + "x" * 32


class Boom(Exception):
    pass


async def _row(db: Any, job_id: int) -> dict[str, Any]:
    async with db.read() as conn:
        row = (await conn.execute(sa.select(jobs).where(jobs.c.id == job_id))).mappings().first()
    assert row is not None
    return dict(row)


async def _db_now(db: Any) -> datetime:
    async with db.read() as conn:
        return (await conn.execute(sa.select(sa.func.now()))).scalar()


async def _count(db: Any) -> int:
    async with db.read() as conn:
        return (await conn.execute(sa.select(sa.func.count()).select_from(jobs))).scalar()


async def _set(db: Any, job_id: int, **values: Any) -> None:
    async with db.tx() as conn:
        await conn.execute(sa.update(jobs).where(jobs.c.id == job_id).values(**values))


# ---------------------------------------------------------------- enqueue


async def test_enqueue_commits_with_business_transaction(db: Any) -> None:
    async with db.tx() as conn:
        job_id = await enqueue(conn, "fulfill", {"order": 7}, queue="fulfill", lane="interactive")
    assert isinstance(job_id, int)
    row = await _row(db, job_id)
    assert row["status"] == "ready"
    assert row["payload"] == {"order": 7}
    assert row["queue"] == "fulfill"
    assert row["lane"] == "interactive"
    assert row["attempts"] == 0
    assert row["max_attempts"] == 10


async def test_enqueue_rolled_back_with_business_transaction(db: Any) -> None:
    with pytest.raises(Boom):
        async with db.tx() as conn:
            assert await enqueue(conn, "fulfill", {"order": 1}) is not None
            raise Boom
    assert await _count(db) == 0
    assert await JobQueue(db).claim("background", "w1", 10) == []


async def test_enqueue_validates_arguments(db: Any) -> None:
    async with db.tx() as conn:
        with pytest.raises(ValueError, match="lane"):
            await enqueue(conn, "k", {}, lane="urgent")
        with pytest.raises(ValueError, match="kind"):
            await enqueue(conn, "", {})
        with pytest.raises(ValueError, match="max_attempts"):
            await enqueue(conn, "k", {}, max_attempts=0)
        with pytest.raises(ValueError, match="timezone-aware"):
            await enqueue(conn, "k", {}, run_at=datetime(2030, 1, 1))
        with pytest.raises(TypeError):
            await enqueue(conn, "k", [1, 2])  # type: ignore[arg-type]
    assert await _count(db) == 0


async def test_dedup_key_blocks_only_live_jobs(db: Any) -> None:
    q = JobQueue(db)
    first = await q.enqueue("fulfill", {"n": 1}, dedup_key="fulfill:order:1")
    assert first is not None
    assert await q.enqueue("fulfill", {"n": 2}, dedup_key="fulfill:order:1") is None
    [job] = await q.claim("background", "w1", 5)
    assert job.payload == {"n": 1}
    assert await q.complete(job.id) is True
    assert (await _row(db, first))["status"] == "done"
    # After done the key is free again.
    second = await q.enqueue("fulfill", {"n": 4}, dedup_key="fulfill:order:1")
    assert second is not None and second != first
    # Different key and no key are independent.
    assert await q.enqueue("fulfill", {}, dedup_key="fulfill:order:2") is not None
    assert await q.enqueue("fulfill", {}) is not None
    assert await q.enqueue("fulfill", {}) is not None


async def test_dedup_while_running_reruns_the_job(db: Any) -> None:
    """An event that arrives while the job runs is never lost: the job runs once more after completing."""
    q = JobQueue(db)
    job_id = await q.enqueue("sync_user", {"user": 7}, dedup_key="sync:7")
    [job] = await q.claim("background", "w1", 5)
    # The handler has read the state; two more changes happen meanwhile.
    assert await q.enqueue("sync_user", {"user": 7}, dedup_key="sync:7") is None
    assert await q.enqueue("sync_user", {"user": 7}, dedup_key="sync:7") is None
    assert await _count(db) == 1
    assert (await _row(db, job_id))["rerun"] is True
    assert await q.complete(job.id, worker_id="w1", attempt=1) is True
    row = await _row(db, job_id)
    assert (row["status"], row["attempts"], row["rerun"], row["done_at"]) == ("ready", 0, False, None)
    # Runs exactly once more (any number of events during a run fold into one rerun).
    [again] = await q.claim("background", "w2", 5)
    assert again.id == job_id and again.attempts == 1
    assert await q.complete(again.id, worker_id="w2", attempt=1) is True
    row = await _row(db, job_id)
    assert row["status"] == "done" and row["done_at"] is not None
    assert await q.claim("background", "w2", 5) == []


async def test_rerun_respects_run_at_of_the_new_event(db: Any) -> None:
    q = JobQueue(db)
    job_id = await q.enqueue("k", {}, dedup_key="d")
    [job] = await q.claim("background", "w", 1)
    later = await _db_now(db) + timedelta(hours=1)
    sooner = later - timedelta(minutes=30)
    assert await q.enqueue("k", {}, dedup_key="d", run_at=later) is None
    assert await q.enqueue("k", {}, dedup_key="d", run_at=sooner) is None  # the earliest one wins
    await q.complete(job.id)
    row = await _row(db, job_id)
    assert row["status"] == "ready"
    assert abs((row["next_run_at"] - sooner).total_seconds()) < 1e-3
    assert await q.claim("background", "w", 1) == []


async def test_rerun_flag_is_cleared_by_retry_and_dead(db: Any) -> None:
    q = JobQueue(db)
    job_id = await q.enqueue("k", {}, dedup_key="d", max_attempts=2)
    [job] = await q.claim("background", "w", 1)
    assert await q.enqueue("k", {}, dedup_key="d") is None
    # A failed run is retried anyway; the retry reads the fresh state.
    assert await q.fail(job.id, "boom", retry_in=0) == "ready"
    assert (await _row(db, job_id))["rerun"] is False
    [job] = await q.claim("background", "w", 1)
    assert await q.enqueue("k", {}, dedup_key="d") is None
    # Out of attempts: dead (manual retry re-reads the state); the key is free for new events.
    assert await q.fail(job.id, "boom") == "dead"
    assert (await _row(db, job_id))["rerun"] is False
    assert await q.enqueue("k", {}, dedup_key="d") is not None
    # release/reap also clear the flag (the job runs again anyway).
    other = await q.enqueue("k2", {}, dedup_key="d2")
    [job2] = [j for j in await q.claim("background", "w", 5) if j.id == other]
    assert await q.enqueue("k2", {}, dedup_key="d2") is None
    assert await q.release(job2.id) is True
    row = await _row(db, other)
    assert (row["status"], row["rerun"]) == ("ready", False)


async def test_dedup_into_ready_job_blocks_claim_until_commit(db: Any) -> None:
    """A deduplicated event locks the pending job: it cannot run on state the producer has not committed."""
    q = JobQueue(db)
    job_id = await q.enqueue("k", {}, dedup_key="d")
    async with db.tx() as conn:
        assert await enqueue(conn, "k", {}, dedup_key="d") is None
        assert await asyncio.wait_for(q.claim("background", "w", 5), 5) == []
    [job] = await q.claim("background", "w", 5)
    assert job.id == job_id


async def test_dedup_inside_one_transaction(db: Any) -> None:
    async with db.tx() as conn:
        a = await enqueue(conn, "k", {}, dedup_key="same")
        b = await enqueue(conn, "k", {}, dedup_key="same")
    assert a is not None and b is None
    assert await _count(db) == 1


async def test_dedup_concurrent_enqueue_single_winner(db: Any) -> None:
    q = JobQueue(db)
    results = await asyncio.gather(*(q.enqueue("k", {"i": i}, dedup_key="race") for i in range(8)))
    assert sum(r is not None for r in results) == 1
    assert await _count(db) == 1


async def test_enqueue_notifies_only_on_commit(db: Any) -> None:
    got: list[str] = []
    listener = await db.listen(NOTIFY_CHANNEL, lambda _ch, payload: got.append(payload))
    try:
        with pytest.raises(Boom):
            async with db.tx() as conn:
                await enqueue(conn, "k", {}, lane="interactive")
                raise Boom
        await asyncio.sleep(0.2)
        assert got == []
        async with db.tx() as conn:
            await enqueue(conn, "k", {}, lane="interactive")
            await enqueue(conn, "k", {}, lane="interactive")
        for _ in range(100):
            if got:
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.1)
        assert got == ["interactive"]  # folded into one notification per transaction
        # A delayed job does not wake anybody now; the periodic poll picks it up when due.
        async with db.tx() as conn:
            await enqueue(conn, "k", {}, run_at=datetime.now(UTC) + timedelta(minutes=5))
        await asyncio.sleep(0.2)
        assert got == ["interactive"]
    finally:
        await listener.close()


# ---------------------------------------------------------------- claim


async def test_claim_marks_running_with_lease(db: Any) -> None:
    q = JobQueue(db)
    job_id = await q.enqueue("k", {"a": 1}, caused_by="order:5")
    before = await _db_now(db)
    [job] = await q.claim("background", "w1", 10, lease_s=30)
    assert job.id == job_id
    assert job.payload == {"a": 1}
    assert job.attempts == 1
    assert job.locked_by == "w1"
    assert job.caused_by == "order:5"
    assert job.locked_until is not None
    assert timedelta(seconds=25) < job.locked_until - before < timedelta(seconds=35)
    assert (await _row(db, job_id))["status"] == "running"
    assert await q.claim("background", "w2", 10) == []


async def test_claim_respects_lane_limit_and_run_at(db: Any) -> None:
    q = JobQueue(db)
    for i in range(5):
        await q.enqueue("k", {"i": i}, lane="background")
    await q.enqueue("k", {}, lane="interactive")
    future = await q.enqueue("k", {}, run_at=datetime.now(UTC) + timedelta(hours=1))
    got = await q.claim("background", "w", 3)
    assert [j.payload["i"] for j in got] == [0, 1, 2]
    got2 = await q.claim("background", "w", 10)
    assert [j.payload.get("i") for j in got2] == [3, 4]
    assert [j.lane for j in await q.claim("interactive", "w", 10)] == ["interactive"]
    assert await q.claim("background", "w", 10) == []
    await _set(db, future, next_run_at=datetime.now(UTC) - timedelta(seconds=1))
    [due] = await q.claim("background", "w", 10)
    assert due.id == future
    assert await q.claim("background", "w", 0) == []
    with pytest.raises(ValueError, match="lane"):
        await q.claim("nope", "w", 1)


async def test_ordering_key_is_strict_fifo(db: Any) -> None:
    q = JobQueue(db)
    a1 = await q.enqueue("panel", {"n": 1}, ordering_key="sub:1")
    a2 = await q.enqueue("panel", {"n": 2}, ordering_key="sub:1")
    b1 = await q.enqueue("panel", {"n": 1}, ordering_key="sub:2")
    free = await q.enqueue("panel", {"n": 0})
    got = await q.claim("background", "w", 10)
    assert {j.id for j in got} == {a1, b1, free}
    # a2 waits while a1 is running...
    assert await q.claim("background", "w", 10) == []
    # ...and while a1 is back in "ready" for a retry later (FIFO holds across retries).
    await q.fail(a1, "transient", retry_in=3600)
    assert await q.claim("background", "w", 10) == []
    await _set(db, a1, next_run_at=datetime.now(UTC) - timedelta(seconds=1))
    [again] = await q.claim("background", "w", 10)
    assert again.id == a1 and again.attempts == 2
    await q.complete(a1)
    [nxt] = await q.claim("background", "w", 10)
    assert nxt.id == a2


async def test_dead_job_does_not_block_ordering_key(db: Any) -> None:
    q = JobQueue(db)
    a1 = await q.enqueue("panel", {}, ordering_key="sub:9", max_attempts=1)
    a2 = await q.enqueue("panel", {}, ordering_key="sub:9")
    [j] = await q.claim("background", "w", 10)
    assert await q.fail(j.id, "boom") == "dead"
    [nxt] = await q.claim("background", "w", 10)
    assert (j.id, nxt.id) == (a1, a2)


async def test_concurrent_claims_never_share_a_job(db: Any) -> None:
    q = JobQueue(db)
    async with db.tx() as conn:
        for i in range(300):
            await enqueue(conn, "k", {"i": i})
    seen: list[int] = []

    async def grab(worker: str) -> None:
        while True:
            batch = await q.claim("background", worker, 7)
            if not batch:
                return
            seen.extend(j.id for j in batch)

    await asyncio.gather(*(grab(f"w{i}") for i in range(6)))
    assert len(seen) == 300
    assert len(set(seen)) == 300


# ---------------------------------------------------------------- complete / fail / dead


async def test_complete_requires_current_lease_holder(db: Any) -> None:
    q = JobQueue(db)
    await q.enqueue("k", {})
    [job] = await q.claim("background", "w1", 1)
    assert await q.complete(job.id, worker_id="w2", attempt=job.attempts) is False
    assert await q.complete(job.id, worker_id="w1", attempt=job.attempts + 1) is False
    assert await q.complete(job.id, worker_id="w1", attempt=job.attempts) is True
    row = await _row(db, job.id)
    assert row["status"] == "done"
    assert row["done_at"] is not None
    assert row["locked_by"] is None and row["locked_until"] is None
    assert await q.complete(job.id) is False  # already done


async def test_fail_backoff_with_jitter(db: Any) -> None:
    q = JobQueue(db)
    job_id = await q.enqueue("k", {})
    for attempt in range(1, 4):
        await _set(db, job_id, next_run_at=datetime.now(UTC) - timedelta(seconds=1))
        [job] = await q.claim("background", "w", 1)
        assert job.attempts == attempt
        now = await _db_now(db)
        assert await q.fail(job.id, f"err {attempt}") == "ready"
        row = await _row(db, job_id)
        delay = (row["next_run_at"] - now).total_seconds()
        expected = 2**attempt
        assert expected * 0.75 <= delay <= expected * 1.25 + 1, (attempt, delay)
        assert row["last_error"] == f"err {attempt}"
        assert row["locked_by"] is None


async def test_backoff_is_capped(db: Any) -> None:
    q = JobQueue(db)
    job_id = await q.enqueue("k", {}, max_attempts=100)
    await _set(db, job_id, attempts=40)
    [job] = await q.claim("background", "w", 1)
    now = await _db_now(db)
    await q.fail(job.id, "x")
    delay = ((await _row(db, job_id))["next_run_at"] - now).total_seconds()
    assert BACKOFF_CAP_S * 0.75 <= delay <= BACKOFF_CAP_S * 1.25 + 1


async def test_fail_retry_in_and_dead_after_max_attempts(db: Any) -> None:
    q = JobQueue(db)
    job_id = await q.enqueue("k", {}, max_attempts=3)
    statuses = []
    for _ in range(3):
        [job] = await q.claim("background", "w", 1)
        statuses.append(await q.fail(job.id, "nope", retry_in=0))
    assert statuses == ["ready", "ready", "dead"]
    row = await _row(db, job_id)
    assert row["status"] == "dead"
    assert row["attempts"] == 3
    assert await q.claim("background", "w", 1) == []
    with pytest.raises(ValueError, match="retry_in"):
        await q.fail(job_id, "x", retry_in=-1)


async def test_fail_permanent_goes_dead_immediately(db: Any) -> None:
    q = JobQueue(db)
    await q.enqueue("k", {})
    [job] = await q.claim("background", "w", 1)
    assert await q.fail(job.id, "bad payload", permanent=True) == "dead"
    assert await q.fail(job.id, "again") is None  # not running any more


async def test_fail_masks_secrets_and_clips(db: Any) -> None:
    q = JobQueue(db)
    await q.enqueue("k", {})
    [job] = await q.claim("background", "w", 1)
    await q.fail(job.id, f"token {BOT_TOKEN} " + "x" * 10_000, retry_in=0)
    err = (await _row(db, job.id))["last_error"]
    assert BOT_TOKEN not in err
    assert len(err) <= 2000


async def test_retry_dead(db: Any) -> None:
    q = JobQueue(db)
    job_id = await q.enqueue("k", {}, max_attempts=1, dedup_key="d1")
    with pytest.raises(JobStateError):
        await q.retry_dead(job_id)  # not dead
    [job] = await q.claim("background", "w", 1)
    await q.fail(job.id, "boom")
    await q.retry_dead(job_id)
    row = await _row(db, job_id)
    assert (row["status"], row["attempts"]) == ("ready", 0)
    assert row["last_error"] == "boom"  # history kept until the next attempt
    [again] = await q.claim("background", "w", 1)
    assert again.id == job_id
    await q.fail(job_id, "boom")
    # A new live job took the dedup key meanwhile -> retry_dead refuses with a clear error.
    assert await q.enqueue("k", {}, dedup_key="d1") is not None
    with pytest.raises(JobStateError, match="dedup_key"):
        await q.retry_dead(job_id)
    with pytest.raises(JobStateError):
        await q.retry_dead(999_999)


async def test_release_returns_job_without_counting_attempt(db: Any) -> None:
    q = JobQueue(db)
    job_id = await q.enqueue("k", {})
    [job] = await q.claim("background", "w", 1)
    assert await q.release(job.id, worker_id="w", attempt=1) is True
    row = await _row(db, job_id)
    assert (row["status"], row["attempts"]) == ("ready", 0)


# ---------------------------------------------------------------- leases


async def test_expired_lease_is_reclaimed_by_another_worker(db: Any) -> None:
    q = JobQueue(db)
    job_id = await q.enqueue("k", {})
    [job] = await q.claim("background", "dead-worker", 1, lease_s=1)
    assert await q.reap_expired() == 0  # lease still valid
    await asyncio.sleep(1.2)
    assert await q.reap_expired() == 1
    row = await _row(db, job_id)
    assert row["status"] == "ready"
    assert "dead-worker" in row["last_error"]
    [again] = await q.claim("background", "w2", 1)
    assert again.id == job_id and again.attempts == 2
    # The original holder can no longer complete it.
    assert await q.complete(job.id, worker_id="dead-worker", attempt=job.attempts) is False
    assert await q.complete(again.id, worker_id="w2", attempt=again.attempts) is True


async def test_expired_lease_on_last_attempt_goes_dead(db: Any) -> None:
    q = JobQueue(db)
    job_id = await q.enqueue("k", {}, max_attempts=1)
    await q.claim("background", "w", 1, lease_s=1)
    await _set(db, job_id, locked_until=datetime.now(UTC) - timedelta(seconds=5))
    assert await q.reap_expired() == 1
    assert (await _row(db, job_id))["status"] == "dead"


async def test_extend_lease(db: Any) -> None:
    q = JobQueue(db)
    await q.enqueue("k", {})
    [job] = await q.claim("background", "w", 1, lease_s=1)
    assert await q.extend_lease(job.id, "w", job.attempts, lease_s=120) is True
    await asyncio.sleep(1.1)
    assert await q.reap_expired() == 0
    assert await q.extend_lease(job.id, "other", job.attempts) is False


# ---------------------------------------------------------------- stats / purge / get


async def test_stats_purge_get(db: Any) -> None:
    q = JobQueue(db)
    for _ in range(3):
        await q.enqueue("k", {}, queue="panel")
    await q.enqueue("k", {}, queue="tg_send", max_attempts=1)
    await q.enqueue("k", {}, queue="tg_send")
    claimed = await q.claim("background", "w", 10)
    by_queue: dict[str, list[int]] = {}
    for j in claimed:
        by_queue.setdefault(j.queue, []).append(j.id)
    await q.complete(by_queue["panel"][0])
    await q.fail(by_queue["tg_send"][0], "x")  # max_attempts=1 -> dead
    await q.fail(by_queue["panel"][1], "x", retry_in=0)
    assert await q.stats() == {
        "panel": {"ready": 1, "running": 1, "dead": 0},
        "tg_send": {"ready": 0, "running": 1, "dead": 1},
    }
    dead = await q.dead()
    assert [j.id for j in dead] == [by_queue["tg_send"][0]]
    assert dead[0].last_error == "x"
    assert (await q.get(by_queue["panel"][0])) is not None
    assert await q.get(123456) is None
    assert await q.status_of(by_queue["panel"][0]) == "done"

    assert await q.purge() == 0  # nothing old enough
    old = datetime.now(UTC) - timedelta(days=100)
    async with db.tx() as conn:
        await conn.execute(sa.update(jobs).where(jobs.c.status.in_(("done", "dead"))).values(updated_at=old))
    assert await q.purge(done_older_than_days=7, dead_older_than_days=90, batch=1) == 2
    assert await _count(db) == 3  # live jobs untouched
