"""Durable job queue (``jobs`` table, SKIP LOCKED + LISTEN/NOTIFY), job worker and periodic scheduler.

Producers call :func:`enqueue` inside their business transaction; :class:`JobWorker` executes jobs by kind;
:class:`Scheduler` runs in-process periodic tasks.
"""

from __future__ import annotations

from svbg.jobs.queue import NOTIFY_CHANNEL, Job, JobQueue, JobStateError, enqueue
from svbg.jobs.scheduler import Scheduler, next_daily_run
from svbg.jobs.worker import Handler, JobContext, JobWorker, PermanentJobError, RetryJob

__all__ = [
    "NOTIFY_CHANNEL",
    "Handler",
    "Job",
    "JobContext",
    "JobQueue",
    "JobStateError",
    "JobWorker",
    "PermanentJobError",
    "RetryJob",
    "Scheduler",
    "enqueue",
    "next_daily_run",
]
