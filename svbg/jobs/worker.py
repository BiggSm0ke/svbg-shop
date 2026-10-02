"""Job worker: executes jobs from the ``jobs`` table per lane with bounded concurrency.

* wakes up on ``LISTEN svbg_jobs`` right after a producer commits, and polls every ``poll_interval``
  seconds as a safety net (missed notifications, delayed ``run_at``, backoff retries);
* every job runs in its own task: a handler exception is recorded with ``fail`` (backoff / dead-letter)
  and reported to the error hub with place ``job:<kind>``; it never affects other jobs or the loop;
* long jobs keep their lease alive (renewed every ``lease_s / 3``); leases of crashed workers are reaped;
* ``stop(timeout)`` stops claiming, waits for running jobs, and gives unfinished ones back to the queue.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from svbg.jobs._support import ErrorCapture, clip, describe_error, report
from svbg.jobs.queue import NOTIFY_CHANNEL, Job, JobQueue
from svbg.jobs.tables import JOB_LANES

if TYPE_CHECKING:
    from svbg.db.engine import Database, Listener

__all__ = [
    "DEFAULT_CONCURRENCY",
    "Handler",
    "JobContext",
    "JobWorker",
    "PermanentJobError",
    "RetryJob",
]

log = logging.getLogger("svbg.jobs")

DEFAULT_CONCURRENCY: Mapping[str, int] = {"interactive": 4, "background": 2}

# Owner-facing texts for the error report ("Что сделано:").
TEXTS: Mapping[str, str] = {
    "retry": "Задача «{kind}» будет повторена автоматически (попытка {attempt} из {max}).",
    "dead": "Задача «{kind}» остановлена после {attempt} попыток и ждёт ручного повтора в «Состоянии».",
    "permanent": "Задача «{kind}» остановлена без повторов и ждёт ручного повтора в «Состоянии».",
    "claim": "Очередь повторит выборку задач через несколько секунд.",
    "bookkeeping": "Задача вернётся в очередь после истечения аренды и будет выполнена снова.",
    "polling": "Очередь работает опросом раз в несколько секунд.",
    "timeout": "Задача «{kind}» не уложилась в {seconds:g} с и будет повторена (попытка {attempt} из {max}).",
}


class PermanentJobError(Exception):
    """Raise from a handler when retrying cannot help: the job goes straight to ``dead``."""


class RetryJob(Exception):
    """Raise from a handler to retry after ``delay`` seconds instead of the default backoff."""

    def __init__(self, delay: float, reason: str = "") -> None:
        super().__init__(reason or f"retry requested in {delay:g}s")
        self.delay = max(0.0, float(delay))
        self.reason = reason


@dataclass(frozen=True, slots=True)
class JobContext:
    """Passed to every handler next to the job."""

    db: Database
    queue: JobQueue
    worker_id: str
    deps: Mapping[str, Any] = field(default_factory=dict)


Handler = Callable[[Job, JobContext], Awaitable[None]]


def _default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{os.urandom(3).hex()}"


class JobWorker:
    def __init__(
        self,
        db: Database,
        queue: JobQueue,
        handlers: Mapping[str, Handler],
        *,
        concurrency: Mapping[str, int] | None = None,
        hub: ErrorCapture | None = None,
        poll_interval: float = 5.0,
        lease_s: int = 60,
        timeouts: Mapping[str, float] | None = None,
        default_timeout: float | None = None,
        reap_interval: float | None = None,
        deps: Mapping[str, Any] | None = None,
        worker_id: str | None = None,
    ) -> None:
        conc = dict(DEFAULT_CONCURRENCY if concurrency is None else concurrency)
        unknown = set(conc) - set(JOB_LANES)
        if unknown:
            raise ValueError(f"unknown lanes in concurrency: {sorted(unknown)}")
        if any(n < 0 for n in conc.values()):
            raise ValueError("concurrency must be >= 0")
        if poll_interval <= 0 or lease_s <= 0:
            raise ValueError("poll_interval and lease_s must be positive")
        self._db = db
        self._queue = queue
        # A plain dict is kept by reference, not copied: the owner (``App.job_handlers``) may register
        # handlers after the worker is built (modules are wired later), and the worker must see them.
        self._handlers: dict[str, Handler] = handlers if isinstance(handlers, dict) else dict(handlers)
        self._concurrency = {lane: n for lane, n in conc.items() if n > 0}
        self._hub = hub
        self._poll_interval = poll_interval
        self._lease_s = lease_s
        self._timeouts = dict(timeouts or {})
        self._default_timeout = default_timeout
        self._reap_interval = reap_interval if reap_interval is not None else max(1.0, lease_s / 2)
        self.worker_id = worker_id or _default_worker_id()
        self._ctx = JobContext(db=db, queue=queue, worker_id=self.worker_id, deps=dict(deps or {}))
        self._wake: dict[str, asyncio.Event] = {lane: asyncio.Event() for lane in self._concurrency}
        self._running: dict[str, set[asyncio.Task[None]]] = {lane: set() for lane in self._concurrency}
        self._loops: list[asyncio.Task[None]] = []
        self._listener: Listener | None = None
        self._stopping = asyncio.Event()
        self._started = False

    # ------------------------------------------------------------------ lifecycle

    @property
    def running_jobs(self) -> int:
        return sum(len(s) for s in self._running.values())

    def register(self, kind: str, handler: Handler) -> None:
        """Add a handler (before or after start); also visible in the ``handlers`` dict passed in."""
        self._handlers[kind] = handler

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._stopping.clear()
        try:
            self._listener = await self._db.listen(NOTIFY_CHANNEL, self._on_notify)
        except Exception as e:  # noqa: BLE001 - any LISTEN failure degrades to polling, never blocks start
            # Still works by polling; the hub learns about it.
            log.warning("LISTEN %s unavailable, falling back to polling: %s", NOTIFY_CHANNEL, e)
            await report(self._hub, e, "jobs:listen", handled=TEXTS["polling"])
        for lane in self._concurrency:
            self._loops.append(asyncio.create_task(self._lane_loop(lane), name=f"jobs:{lane}"))
        self._loops.append(asyncio.create_task(self._reaper_loop(), name="jobs:reaper"))
        log.info("job worker %s started: %s", self.worker_id, self._concurrency)

    async def stop(self, timeout: float = 30.0) -> None:  # noqa: ASYNC109 - graceful deadline, not a call timeout
        """Stop claiming, wait up to ``timeout`` s for running jobs, then cancel and release the rest."""
        if not self._started:
            return
        self._stopping.set()
        for ev in self._wake.values():
            ev.set()
        if self._listener is not None:
            with contextlib.suppress(OSError, ConnectionError):
                await self._listener.close()
            self._listener = None
        # Loops exit at their next check: they only block on the wake/stop events or a short DB call.
        # A DB call hung on a dead connection must not hang the shutdown: they are cancelled after
        # ``_LOOP_EXIT_TIMEOUT``.
        loops, self._loops = self._loops, []
        try:
            await _finish(loops, _LOOP_EXIT_TIMEOUT)
            pending = self._running_tasks()
            if pending:
                log.info("waiting up to %.1fs for %d running job(s)", timeout, len(pending))
                _done, still = await asyncio.wait(pending, timeout=timeout)
                if still:
                    await _cancel_and_wait(still)
                    log.warning("%d job(s) interrupted by shutdown and returned to the queue", len(still))
        except asyncio.CancelledError:
            # The caller's own deadline hit first: interrupted jobs must still be released (their ``_run``
            # does it on cancellation) before the caller goes on to close the database.
            await _cancel_and_wait(self._running_tasks())
            raise
        finally:
            self._started = False
        log.info("job worker %s stopped", self.worker_id)

    def _running_tasks(self) -> set[asyncio.Task[None]]:
        return {t for tasks in self._running.values() for t in tasks}

    def wake(self, lane: str | None = None) -> None:
        """Wake lane loops (all lanes when ``lane`` is None)."""
        if lane is not None and lane in self._wake:
            self._wake[lane].set()
            return
        for ev in self._wake.values():
            ev.set()

    def _on_notify(self, _channel: str, payload: str) -> None:
        self.wake(payload if payload in self._wake else None)

    # ------------------------------------------------------------------ loops

    async def _lane_loop(self, lane: str) -> None:
        limit = self._concurrency[lane]
        wake = self._wake[lane]
        running = self._running[lane]
        error_delay = 0.5
        while not self._stopping.is_set():
            wake.clear()
            free = limit - len(running)
            if free > 0:
                try:
                    claimed = await self._queue.claim(lane, self.worker_id, free, self._lease_s)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001 - DB outage must not kill the loop
                    log.warning("jobs: claim on lane %s failed: %s", lane, describe_error(e))
                    await report(self._hub, e, f"jobs:claim:{lane}", handled=TEXTS["claim"])
                    await self._sleep(error_delay)
                    error_delay = min(error_delay * 2, 30.0)
                    continue
                error_delay = 0.5
                for job in claimed:
                    task = asyncio.create_task(self._run(job), name=f"job:{job.kind}:{job.id}")
                    running.add(task)
                    task.add_done_callback(lambda t, r=running, w=wake: (r.discard(t), w.set()))
                if claimed and len(claimed) == free:
                    continue  # there may be more ready jobs: claim as soon as a slot frees up
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(wake.wait(), timeout=self._poll_interval)

    async def _reaper_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                if await self._queue.reap_expired():
                    self.wake()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - keep reaping on the next tick
                log.warning("jobs: lease reaper failed: %s", describe_error(e))
            await self._sleep(self._reap_interval)

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)

    # ------------------------------------------------------------------ one job

    async def _run(self, job: Job) -> None:
        handler = self._handlers.get(job.kind)
        fence = {"worker_id": self.worker_id, "attempt": job.attempts}
        if handler is None:
            known = ", ".join(sorted(self._handlers)) or "—"
            msg = f"unknown job kind {job.kind!r}: no handler registered (known: {known})"
            log.error("job %s: %s", job.id, msg)
            await self._safe(self._queue.fail(job.id, clip(msg), permanent=True, **fence), job)
            await self._report(LookupError(msg), job, TEXTS["permanent"].format(kind=job.kind))
            return
        timeout = self._timeouts.get(job.kind, self._default_timeout)
        deadline = asyncio.timeout(timeout)
        renew = asyncio.create_task(self._renew_lease(job), name=f"lease:{job.id}")
        try:
            async with deadline:
                await handler(job, self._ctx)
        except asyncio.CancelledError:
            # Shutdown deadline reached: hand the job back untouched (it will run again later).
            await self._safe(self._queue.release(job.id, **fence), job)
            raise
        except RetryJob as r:
            await self._safe(self._queue.fail(job.id, clip(str(r)), retry_in=r.delay, **fence), job)
        except PermanentJobError as e:
            await self._safe(self._queue.fail(job.id, describe_error(e), permanent=True, **fence), job)
            await self._report(e, job, TEXTS["permanent"].format(kind=job.kind))
        except Exception as e:  # noqa: BLE001 - isolation boundary: one job never breaks the worker
            timed_out = isinstance(e, TimeoutError) and deadline.expired() and timeout is not None
            error = f"TimeoutError: job exceeded {timeout:g}s" if timed_out else describe_error(e)
            status = await self._safe(self._queue.fail(job.id, error, **fence), job)
            if status == "dead":
                handled = TEXTS["dead"].format(kind=job.kind, attempt=job.attempts)
            elif timed_out:
                handled = TEXTS["timeout"].format(
                    kind=job.kind, seconds=timeout, attempt=job.attempts, max=job.max_attempts
                )
            else:
                handled = TEXTS["retry"].format(kind=job.kind, attempt=job.attempts, max=job.max_attempts)
            log.warning("job %s (%s) failed: %s", job.id, job.kind, error)
            await self._report(e, job, handled)
        else:
            await self._safe(self._queue.complete(job.id, **fence), job)
        finally:
            renew.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await renew

    async def _renew_lease(self, job: Job) -> None:
        period = max(0.2, self._lease_s / 3)
        while True:
            await asyncio.sleep(period)
            try:
                held = await self._queue.extend_lease(job.id, self.worker_id, job.attempts, self._lease_s)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - retry on next period, the lease is still valid for a while
                log.warning("job %s: lease renewal failed: %s", job.id, describe_error(e))
                continue
            if not held:
                log.warning("job %s (%s): lease lost, result will be discarded", job.id, job.kind)
                return

    async def _safe[T](self, op: Awaitable[T], job: Job) -> T | None:
        """Run a queue bookkeeping call; a DB error here is logged (the lease reaper will recover the job)."""
        try:
            return await op
        except asyncio.CancelledError:
            raise
        except Exception as e:  # bookkeeping failure must not crash the worker
            log.exception("job %s (%s): could not record result", job.id, job.kind)
            await report(
                self._hub,
                e,
                "jobs:bookkeeping",
                handled=TEXTS["bookkeeping"],
                context={"job_id": job.id, "kind": job.kind},
            )
            return None

    async def _report(self, exc: BaseException, job: Job, handled: str) -> None:
        await report(
            self._hub,
            exc,
            f"job:{job.kind}",
            handled=handled,
            context={
                "job_id": job.id,
                "queue": job.queue,
                "lane": job.lane,
                "attempt": job.attempts,
                "max_attempts": job.max_attempts,
                "caused_by": job.caused_by,
            },
        )


_LOOP_EXIT_TIMEOUT = 5.0  # lane/reaper loops leaving on stop (they may sit in a DB call)
_RELEASE_TIMEOUT = 5.0  # interrupted jobs giving their rows back to the queue


async def _finish(tasks: list[asyncio.Task[None]], timeout: float) -> None:  # noqa: ASYNC109 - a deadline
    """Wait up to ``timeout`` s for ``tasks`` to end on their own, then cancel the rest (bounded)."""
    if not tasks:
        return
    try:
        _done, pending = await asyncio.wait(tasks, timeout=timeout)
    except asyncio.CancelledError:
        await _cancel_and_wait(set(tasks))
        raise
    if pending:
        log.warning("jobs: %d loop(s) did not stop in %.0fs, cancelled", len(pending), timeout)
        await _cancel_and_wait(pending)


async def _cancel_and_wait(tasks: set[asyncio.Task[None]]) -> None:
    """Cancel ``tasks`` and give them up to ``_RELEASE_TIMEOUT`` s to run their cleanup (job release)."""
    live = {t for t in tasks if not t.done()}
    for t in live:
        t.cancel()
    if not live:
        return
    _done, stuck = await asyncio.wait(live, timeout=_RELEASE_TIMEOUT)
    if stuck:
        log.warning("jobs: %d task(s) still busy %.0fs after cancellation", len(stuck), _RELEASE_TIMEOUT)
