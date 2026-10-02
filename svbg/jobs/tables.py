"""Tables of the durable job queue and the periodic scheduler.

``jobs`` is the single queue table (04 D9): one row per unit of work, acknowledged only after the handler
finished. Workers claim rows with ``FOR UPDATE SKIP LOCKED``; producers insert inside their own business
transaction, so a job exists if and only if the business change committed.
"""

from __future__ import annotations

import sqlalchemy as sa

from svbg.db.meta import JSONB, UtcDateTime, metadata, now_default

__all__ = [
    "ACTIVE_STATUSES",
    "DEDUP_INDEX_PREDICATE",
    "JOB_LANES",
    "JOB_STATUSES",
    "jobs",
    "scheduler_state",
]

JOB_LANES: tuple[str, ...] = ("interactive", "background")
JOB_STATUSES: tuple[str, ...] = ("ready", "running", "done", "dead")
#: Statuses that still "own" a dedup key and block later jobs with the same ordering key.
ACTIVE_STATUSES: tuple[str, ...] = ("ready", "running")


def _in(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


#: Predicate of the partial unique index on ``dedup_key``. ``ON CONFLICT`` must repeat it *literally*
#: (bound parameters would prevent PostgreSQL from inferring the arbiter index).
DEDUP_INDEX_PREDICATE = _in("status", ACTIVE_STATUSES) + " AND dedup_key IS NOT NULL"


jobs = sa.Table(
    "jobs",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column("queue", sa.Text, nullable=False, server_default="default"),
    sa.Column("lane", sa.Text, nullable=False, server_default="background"),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("payload", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column("status", sa.Text, nullable=False, server_default="ready"),
    sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
    sa.Column("max_attempts", sa.Integer, nullable=False, server_default="10"),
    sa.Column("next_run_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.Column("locked_until", UtcDateTime, nullable=True),
    sa.Column("locked_by", sa.Text, nullable=True),
    sa.Column("ordering_key", sa.Text, nullable=True),
    sa.Column("dedup_key", sa.Text, nullable=True),
    sa.Column("last_error", sa.Text, nullable=True),
    sa.Column("created_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.Column("updated_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.Column("done_at", UtcDateTime, nullable=True),
    sa.Column("caused_by", sa.Text, nullable=True),
    # Set by ``enqueue`` when a job with the same dedup key arrives while this one is *running*: the handler
    # may already have read the state the new event changed, so ``complete`` puts the job back to ``ready``
    # instead of ``done`` (one extra run covers any number of such events).
    sa.Column("rerun", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.CheckConstraint(_in("lane", JOB_LANES), name="lane"),
    sa.CheckConstraint(_in("status", JOB_STATUSES), name="status"),
    sa.CheckConstraint("attempts >= 0", name="attempts"),
    sa.CheckConstraint("max_attempts >= 1", name="max_attempts"),
    sa.CheckConstraint("kind <> ''", name="kind"),
)

# A dedup key is unique only among live jobs: once done/dead the same key can be enqueued again. A duplicate
# of a *running* job is not dropped: it sets ``rerun`` on it (see ``svbg.jobs.queue.enqueue``).
sa.Index(
    "uq_jobs_dedup_key_active",
    jobs.c.dedup_key,
    unique=True,
    postgresql_where=sa.text(DEDUP_INDEX_PREDICATE),
)
# Claim path: ready jobs of a lane ordered by due time.
sa.Index(
    "ix_jobs_ready_lane_next_run_at",
    jobs.c.lane,
    jobs.c.next_run_at,
    postgresql_where=sa.text("status = 'ready'"),
)
# Ordering check: "is there an earlier live job with the same ordering key?"
sa.Index(
    "ix_jobs_ordering_key_active",
    jobs.c.ordering_key,
    jobs.c.id,
    postgresql_where=sa.text(_in("status", ACTIVE_STATUSES) + " AND ordering_key IS NOT NULL"),
)
# Lease reaper: running jobs whose lease expired.
sa.Index(
    "ix_jobs_running_locked_until",
    jobs.c.locked_until,
    postgresql_where=sa.text("status = 'running'"),
)
# Purge and dead-letter listing.
sa.Index(
    "ix_jobs_finished_updated_at",
    jobs.c.status,
    jobs.c.updated_at,
    postgresql_where=sa.text("status IN ('done', 'dead')"),
)


scheduler_state = sa.Table(
    "scheduler_state",
    metadata,
    sa.Column("task", sa.Text, primary_key=True),
    sa.Column("last_run_at", UtcDateTime, nullable=True),
    sa.Column("last_ok_at", UtcDateTime, nullable=True),
    sa.Column("last_error", sa.Text, nullable=True),
    sa.Column("next_run_at", UtcDateTime, nullable=True),
)
