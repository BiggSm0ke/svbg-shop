"""Durable job queue on PostgreSQL (07 §2.2, 04 D9).

* :func:`enqueue` inserts a job **inside the caller's transaction** and issues ``pg_notify('svbg_jobs')`` in
  the same transaction, so workers wake up right after the commit and a rolled back business change never
  leaves a job behind.
* :class:`JobQueue` is the consumer side: ``claim`` (``FOR UPDATE SKIP LOCKED`` with per-``ordering_key``
  FIFO), ``complete``, ``fail`` (exponential backoff with jitter, dead-letter after ``max_attempts``),
  ``retry_dead``, ``reap_expired`` (leases of crashed workers), ``stats`` and ``purge``.

All timestamps used for scheduling are taken from the database clock (``now()``), so several processes
never disagree about whether a job is due.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from svbg.core import clock
from svbg.jobs._support import clip, mask
from svbg.jobs.tables import ACTIVE_STATUSES, DEDUP_INDEX_PREDICATE, JOB_LANES, jobs

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from svbg.db.engine import Database

__all__ = [
    "BACKOFF_CAP_S",
    "NOTIFY_CHANNEL",
    "Job",
    "JobQueue",
    "JobStateError",
    "enqueue",
]

log = logging.getLogger("svbg.jobs")

NOTIFY_CHANNEL = "svbg_jobs"
#: Upper bound of the automatic retry delay, seconds.
BACKOFF_CAP_S = 600
#: Relative jitter applied to the automatic retry delay (±20 %).
BACKOFF_JITTER = 0.2

_MAX_NAME_LEN = 200
_MAX_KEY_LEN = 500
_ONE_SECOND = sa.literal_column("interval '1 second'")
_NOTIFY_HORIZON = timedelta(seconds=1)
#: ``NOTIFY`` payload that is not a lane name: workers wake every lane loop.
WAKE_ALL = "*"


class JobStateError(Exception):
    """The requested state transition is not possible (job missing, wrong status, dedup conflict)."""


@dataclass(frozen=True, slots=True)
class Job:
    """A claimed job as seen by a handler."""

    id: int
    queue: str
    lane: str
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    ordering_key: str | None
    dedup_key: str | None
    caused_by: str | None
    locked_by: str | None
    locked_until: datetime | None
    next_run_at: datetime
    created_at: datetime
    last_error: str | None = field(default=None, repr=False)

    @property
    def is_last_attempt(self) -> bool:
        return self.attempts >= self.max_attempts

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Job:
        return cls(
            id=row["id"],
            queue=row["queue"],
            lane=row["lane"],
            kind=row["kind"],
            payload=row["payload"] if isinstance(row["payload"], dict) else {},
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
            ordering_key=row["ordering_key"],
            dedup_key=row["dedup_key"],
            caused_by=row["caused_by"],
            locked_by=row["locked_by"],
            locked_until=row["locked_until"],
            next_run_at=row["next_run_at"],
            created_at=row["created_at"],
            last_error=row["last_error"],
        )


def _check_name(what: str, value: str, limit: int = _MAX_NAME_LEN) -> None:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ValueError(f"{what} must be a non-empty string of at most {limit} characters")


async def enqueue(
    conn: AsyncConnection,
    kind: str,
    payload: Mapping[str, Any] | None = None,
    *,
    queue: str = "default",
    lane: str = "background",
    run_at: datetime | None = None,
    ordering_key: str | None = None,
    dedup_key: str | None = None,
    max_attempts: int = 10,
    caused_by: str | None = None,
) -> int | None:
    """Insert a job inside the caller's transaction.

    Returns the new job id, or ``None`` when a live job with the same ``dedup_key`` already exists:

    * the live job is ``ready`` → it has not started yet and will see the new state; the new job is dropped
      (its payload is ignored);
    * the live job is ``running`` → its handler may already have read the old state, so the event must not be
      lost: the running job is flagged ``rerun`` and goes back to ``ready`` (fresh attempt budget, not before
      ``run_at``) when it completes, instead of becoming ``done``. Any number of events during one run cause
      one extra run. If the run fails, the regular retry/dead-letter path covers the event.

    In both cases the existing row stays locked until the caller's transaction ends, so a worker cannot claim
    it before the business change that triggered the event is committed. Workers are woken through
    ``NOTIFY svbg_jobs`` which PostgreSQL delivers on commit.

    Priority inheritance: an ``interactive`` job with an ``ordering_key`` moves the earlier ``ready`` jobs
    with the same key to the ``interactive`` lane. Otherwise the per-key FIFO of :meth:`JobQueue.claim` would
    park a user's click behind a background job of a mass operation (thousands of jobs drained slowly)
    although the interactive lane is idle. The promotion rides on the ``NOTIFY`` statement, so it costs no
    extra round trip.
    """
    _check_name("kind", kind)
    _check_name("queue", queue)
    if lane not in JOB_LANES:
        raise ValueError(f"lane must be one of {JOB_LANES}, got {lane!r}")
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
        raise ValueError("max_attempts must be an integer >= 1")
    if ordering_key is not None:
        _check_name("ordering_key", ordering_key, _MAX_KEY_LEN)
    if dedup_key is not None:
        _check_name("dedup_key", dedup_key, _MAX_KEY_LEN)
    if run_at is not None and (run_at.tzinfo is None or run_at.utcoffset() is None):
        raise ValueError("run_at must be a timezone-aware datetime")
    if payload is not None and not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")

    values: dict[str, Any] = {
        "kind": kind,
        "payload": dict(payload or {}),
        "queue": queue,
        "lane": lane,
        "max_attempts": max_attempts,
        "ordering_key": ordering_key,
        "dedup_key": dedup_key,
        "caused_by": caused_by,
    }
    if run_at is not None:
        values["next_run_at"] = run_at
    insert = pg_insert(jobs).values(**values)
    stmt = insert.returning(jobs.c.id, jobs.c.rerun)
    if dedup_key is not None:
        # The arbiter locks the conflicting row even when the WHERE below is false (ready job): it cannot be
        # claimed until this transaction commits. For a running job the run is re-armed (see docstring).
        stmt = insert.on_conflict_do_update(
            index_elements=[jobs.c.dedup_key],
            index_where=sa.text(DEDUP_INDEX_PREDICATE),
            set_={
                "rerun": True,
                "next_run_at": sa.case(
                    (jobs.c.rerun, sa.func.least(jobs.c.next_run_at, insert.excluded.next_run_at)),
                    else_=insert.excluded.next_run_at,
                ),
                "updated_at": sa.func.now(),
            },
            where=jobs.c.status == "running",
        ).returning(jobs.c.id, jobs.c.rerun)
    promote: sa.Update | None = None
    if lane == "interactive" and ordering_key is not None:
        promote = (
            sa.update(jobs)
            .where(
                jobs.c.ordering_key == ordering_key,
                jobs.c.status == "ready",
                jobs.c.lane != "interactive",
            )
            .values(lane="interactive", updated_at=sa.func.now())
        )
    row = (await conn.execute(stmt)).first()
    if row is None or row.rerun:
        if promote is not None:
            await conn.execute(promote)  # the live duplicate (or its predecessors) must not lag behind either
        # A fresh insert always has rerun = false; true means "merged into the running job".
        log.debug(
            "job %s deduplicated by key %s%s", kind, dedup_key, " (running job will run again)" if row else ""
        )
        return None
    job_id = row.id
    if run_at is None or run_at <= clock.now() + _NOTIFY_HORIZON:
        # Identical notifications in one transaction are folded by PostgreSQL: a batch costs one wakeup.
        # Delayed jobs are picked up by the workers' periodic poll instead.
        wakeup = sa.select(sa.func.pg_notify(NOTIFY_CHANNEL, lane))
        if promote is not None:
            # a data-modifying CTE always runs to completion even though the SELECT does not read it
            wakeup = wakeup.add_cte(promote.returning(jobs.c.id).cte("promoted"))
        await conn.execute(wakeup)
    elif promote is not None:
        await conn.execute(promote)
    return int(job_id)


def _fence(job_id: int, worker_id: str | None, attempt: int | None) -> list[sa.ColumnElement[bool]]:
    """WHERE clauses that make sure only the current lease holder finishes a running job."""
    clauses: list[sa.ColumnElement[bool]] = [jobs.c.id == job_id, jobs.c.status == "running"]
    if worker_id is not None:
        clauses.append(jobs.c.locked_by == worker_id)
    if attempt is not None:
        clauses.append(jobs.c.attempts == attempt)
    return clauses


class JobQueue:
    """Consumer-side operations on the ``jobs`` table."""

    def __init__(self, db: Database) -> None:
        self._db = db

    @property
    def db(self) -> Database:
        return self._db

    async def enqueue(self, kind: str, payload: Mapping[str, Any] | None = None, **kwargs: Any) -> int | None:
        """Convenience: :func:`enqueue` in its own transaction (when there is no business change to bind)."""
        async with self._db.tx() as conn:
            return await enqueue(conn, kind, payload, **kwargs)

    async def claim(self, lane: str, worker_id: str, limit: int, lease_s: int = 60) -> list[Job]:
        """Lock up to ``limit`` due jobs of ``lane`` for ``worker_id`` for ``lease_s`` seconds.

        A job with an ``ordering_key`` is skipped while an earlier (by id) ready/running job with the same
        key exists, which gives strict FIFO per key regardless of the number of workers.
        """
        if lane not in JOB_LANES:
            raise ValueError(f"unknown lane {lane!r}")
        if limit <= 0:
            return []
        if lease_s <= 0:
            raise ValueError("lease_s must be positive")
        earlier = jobs.alias("earlier")
        blocked = (
            sa.select(sa.literal(1))
            .where(
                earlier.c.ordering_key == jobs.c.ordering_key,
                earlier.c.id < jobs.c.id,
                earlier.c.status.in_(ACTIVE_STATUSES),
            )
            .correlate(jobs)
            .exists()
        )
        picked = (
            sa.select(jobs.c.id)
            .where(
                jobs.c.status == "ready",
                jobs.c.lane == lane,
                jobs.c.next_run_at <= sa.func.now(),
                sa.or_(jobs.c.ordering_key.is_(None), ~blocked),
            )
            .order_by(jobs.c.next_run_at, jobs.c.id)
            .limit(limit)
            .with_for_update(skip_locked=True, of=jobs)
            .cte("picked")
        )
        stmt = (
            sa.update(jobs)
            .where(jobs.c.id.in_(sa.select(picked.c.id)))
            .values(
                status="running",
                attempts=jobs.c.attempts + 1,
                locked_by=worker_id,
                locked_until=sa.func.now() + sa.literal(timedelta(seconds=lease_s), sa.Interval),
                updated_at=sa.func.now(),
            )
            .returning(*jobs.c)
        )
        async with self._db.tx() as conn:
            rows = (await conn.execute(stmt)).mappings().all()
        return sorted((Job.from_row(r) for r in rows), key=lambda j: j.id)

    async def extend_lease(self, job_id: int, worker_id: str, attempt: int, lease_s: int = 60) -> bool:
        """Push ``locked_until`` forward; ``False`` if the lease was lost (expired and taken by another)."""
        stmt = (
            sa.update(jobs)
            .where(*_fence(job_id, worker_id, attempt))
            .values(locked_until=sa.func.now() + sa.literal(timedelta(seconds=lease_s), sa.Interval))
            .returning(jobs.c.id)
        )
        async with self._db.tx() as conn:
            return (await conn.execute(stmt)).first() is not None

    async def complete(
        self, job_id: int, *, worker_id: str | None = None, attempt: int | None = None
    ) -> bool:
        """Mark a running job done. With ``worker_id``/``attempt`` only the current lease holder may do it.

        A job flagged ``rerun`` (a duplicate was enqueued while it ran) goes back to ``ready`` with a fresh
        attempt budget instead. Returns ``False`` when nothing was updated (lease lost or job not running).
        """
        rerun = jobs.c.rerun
        stmt = (
            sa.update(jobs)
            .where(*_fence(job_id, worker_id, attempt))
            .values(
                status=sa.case((rerun, "ready"), else_="done"),
                done_at=sa.case((rerun, sa.null()), else_=sa.func.now()),
                attempts=sa.case((rerun, 0), else_=jobs.c.attempts),
                next_run_at=sa.case(
                    (rerun, sa.func.greatest(jobs.c.next_run_at, sa.func.now())), else_=jobs.c.next_run_at
                ),
                rerun=False,
                updated_at=sa.func.now(),
                locked_by=None,
                locked_until=None,
            )
            .returning(jobs.c.lane, jobs.c.ordering_key, jobs.c.status)
        )
        async with self._db.tx() as conn:
            row = (await conn.execute(stmt)).first()
            if row is not None and (row.ordering_key is not None or row.status == "ready"):
                # The job itself (rerun) or the next job with its ordering key just became claimable; the
                # latter may live in another lane (an interactive click queued behind a background job).
                wake = row.lane if row.ordering_key is None else WAKE_ALL
                await conn.execute(sa.select(sa.func.pg_notify(NOTIFY_CHANNEL, wake)))
        if row is None:
            log.warning("job %s: complete ignored, lease is no longer held", job_id)
            return False
        return True

    async def fail(
        self,
        job_id: int,
        error: str,
        *,
        retry_in: float | None = None,
        permanent: bool = False,
        worker_id: str | None = None,
        attempt: int | None = None,
    ) -> str | None:
        """Record a failed attempt.

        The job goes back to ``ready`` after ``retry_in`` seconds or, by default, after
        ``min(2**attempts, 600)`` seconds ±20 % jitter. When ``attempts >= max_attempts`` or ``permanent``
        is set the job becomes ``dead``. Returns the new status, or ``None`` if the lease was lost.
        """
        if retry_in is not None and retry_in < 0:
            raise ValueError("retry_in must be >= 0")
        if retry_in is not None:
            delay: sa.ColumnElement[Any] = sa.literal(float(retry_in), sa.Float) * _ONE_SECOND
        else:
            base = sa.func.least(sa.func.power(2.0, sa.func.least(jobs.c.attempts, 10)), BACKOFF_CAP_S)
            jitter = 1.0 - BACKOFF_JITTER + sa.func.random() * (2 * BACKOFF_JITTER)
            delay = base * jitter * _ONE_SECOND
        goes_dead: sa.ColumnElement[bool] = sa.true() if permanent else jobs.c.attempts >= jobs.c.max_attempts
        stmt = (
            sa.update(jobs)
            .where(*_fence(job_id, worker_id, attempt))
            .values(
                status=sa.case((goes_dead, "dead"), else_="ready"),
                next_run_at=sa.case((goes_dead, jobs.c.next_run_at), else_=sa.func.now() + delay),
                last_error=clip(mask(error)),
                rerun=False,  # the retry (or the manual retry of a dead job) re-reads the current state
                locked_by=None,
                locked_until=None,
                updated_at=sa.func.now(),
            )
            .returning(jobs.c.status, jobs.c.lane, jobs.c.ordering_key)
        )
        async with self._db.tx() as conn:
            row = (await conn.execute(stmt)).first()
            if row is not None and row.status == "dead" and row.ordering_key is not None:
                # the next job with this key (any lane) is unblocked
                await conn.execute(sa.select(sa.func.pg_notify(NOTIFY_CHANNEL, WAKE_ALL)))
        if row is None:
            log.warning("job %s: fail ignored, lease is no longer held", job_id)
            return None
        return str(row.status)

    async def release(self, job_id: int, *, worker_id: str | None = None, attempt: int | None = None) -> bool:
        """Give a running job back without counting the attempt (e.g. interrupted by shutdown)."""
        stmt = (
            sa.update(jobs)
            .where(*_fence(job_id, worker_id, attempt), jobs.c.attempts > 0)
            .values(
                status="ready",
                attempts=jobs.c.attempts - 1,
                next_run_at=sa.func.now(),
                rerun=False,
                locked_by=None,
                locked_until=None,
                updated_at=sa.func.now(),
            )
            .returning(jobs.c.lane)
        )
        async with self._db.tx() as conn:
            row = (await conn.execute(stmt)).first()
            if row is not None:
                await conn.execute(sa.select(sa.func.pg_notify(NOTIFY_CHANNEL, row.lane)))
        return row is not None

    async def reap_expired(self) -> int:
        """Return jobs whose lease expired (worker crashed or hung) to ``ready``; dead if out of attempts."""
        expired = (
            sa.select(jobs.c.id)
            .where(jobs.c.status == "running", jobs.c.locked_until < sa.func.now())
            .order_by(jobs.c.locked_until)
            .limit(1000)
            .with_for_update(skip_locked=True)
            .cte("expired")
        )
        out_of_attempts = jobs.c.attempts >= jobs.c.max_attempts
        stmt = (
            sa.update(jobs)
            .where(jobs.c.id.in_(sa.select(expired.c.id)))
            .values(
                status=sa.case((out_of_attempts, "dead"), else_="ready"),
                next_run_at=sa.func.now(),
                last_error=sa.func.concat("lease expired (worker ", jobs.c.locked_by, " stopped responding)"),
                rerun=False,
                locked_by=None,
                locked_until=None,
                updated_at=sa.func.now(),
            )
            .returning(jobs.c.id, jobs.c.lane, jobs.c.status)
        )
        async with self._db.tx() as conn:
            rows = (await conn.execute(stmt)).all()
            for lane in {r.lane for r in rows if r.status == "ready"}:
                await conn.execute(sa.select(sa.func.pg_notify(NOTIFY_CHANNEL, lane)))
        if rows:
            log.warning("returned %d job(s) with expired leases: %s", len(rows), [r.id for r in rows][:20])
        return len(rows)

    async def retry_dead(self, job_id: int) -> None:
        """Put a dead job back to ``ready`` with a fresh attempt budget ("Повторить" in the bot)."""
        stmt = (
            sa.update(jobs)
            .where(jobs.c.id == job_id, jobs.c.status == "dead")
            .values(
                status="ready",
                attempts=0,
                next_run_at=sa.func.now(),
                done_at=None,
                rerun=False,
                locked_by=None,
                locked_until=None,
                updated_at=sa.func.now(),
            )
            .returning(jobs.c.lane)
        )
        try:
            async with self._db.tx() as conn:
                row = (await conn.execute(stmt)).first()
                if row is None:
                    raise JobStateError(f"job {job_id} does not exist or is not dead")
                await conn.execute(sa.select(sa.func.pg_notify(NOTIFY_CHANNEL, row.lane)))
        except IntegrityError as e:
            raise JobStateError(f"job {job_id}: another live job with the same dedup_key exists") from e

    async def get(self, job_id: int) -> Job | None:
        async with self._db.read() as conn:
            row = (await conn.execute(sa.select(jobs).where(jobs.c.id == job_id))).mappings().first()
        return Job.from_row(row) if row is not None else None

    async def status_of(self, job_id: int) -> str | None:
        async with self._db.read() as conn:
            return (await conn.execute(sa.select(jobs.c.status).where(jobs.c.id == job_id))).scalar()

    async def stats(self) -> dict[str, dict[str, int]]:
        """Per queue counts: ``{"panel": {"ready": 3, "running": 1, "dead": 0}, ...}``."""
        stmt = (
            sa.select(jobs.c.queue, jobs.c.status, sa.func.count())
            .where(jobs.c.status.in_(("ready", "running", "dead")))
            .group_by(jobs.c.queue, jobs.c.status)
        )
        async with self._db.read() as conn:
            rows = (await conn.execute(stmt)).all()
        out: dict[str, dict[str, int]] = {}
        for queue, status, count in rows:
            out.setdefault(queue, {"ready": 0, "running": 0, "dead": 0})[status] = int(count)
        return out

    async def dead(self, limit: int = 50, *, queues: Sequence[str] | None = None) -> list[Job]:
        """Most recent dead jobs (for the "Состояние → очередь" screen)."""
        stmt = sa.select(jobs).where(jobs.c.status == "dead")
        if queues:
            stmt = stmt.where(jobs.c.queue.in_(list(queues)))
        stmt = stmt.order_by(jobs.c.updated_at.desc()).limit(limit)
        async with self._db.read() as conn:
            rows = (await conn.execute(stmt)).mappings().all()
        return [Job.from_row(r) for r in rows]

    async def purge(
        self, done_older_than_days: float = 7, dead_older_than_days: float = 90, *, batch: int = 5000
    ) -> int:
        """Delete finished jobs in batches (short transactions, no long locks). Returns rows deleted."""
        total = 0
        for status, days in (("done", done_older_than_days), ("dead", dead_older_than_days)):
            cutoff = sa.func.now() - sa.literal(timedelta(days=days), sa.Interval)
            victims = (
                sa.select(jobs.c.id)
                .where(jobs.c.status == status, jobs.c.updated_at < cutoff)
                .limit(batch)
                .with_for_update(skip_locked=True)
            )
            stmt = sa.delete(jobs).where(jobs.c.id.in_(victims))
            while True:
                async with self._db.tx() as conn:
                    deleted = (await conn.execute(stmt)).rowcount or 0
                total += deleted
                if deleted < batch:
                    break
        return total
