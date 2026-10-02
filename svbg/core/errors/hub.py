"""Error hub: groups exceptions by fingerprint, persists them and delivers human reports.

``await hub.capture(exc, place, ...)`` never raises (except ``CancelledError`` of the caller) and never
blocks for long: DB work is bounded by ``db_timeout``, delivery to the sink runs in background tasks bounded
by ``sink_timeout``.

Delivery policy per group (07 §2.4.3):

* first occurrence → ``sink.send_new(view)``; the returned ``msg_ref`` (JSON-serializable) is stored;
* repeats → ``sink.update(view, msg_ref)`` at most once per ``update_interval`` (60 s); repeats inside the
  interval are remembered and delivered by :meth:`flush` (run periodically by :meth:`start`), so the final
  count is always shown;
* after ``close_after`` (1 h) of silence the group closes; the next occurrence starts a new episode and a new
  message marked "🔁 снова";
* muted groups (``muted_until`` in the future) are counted but not delivered.

A failed delivery (the sink raised, timed out, or ``send_new`` returned ``None``) is retried by :meth:`flush`
with exponential backoff (``update_interval`` doubling up to ``close_after``, at most ``delivery_attempts``
tries), so a report produced before the bot is ready is not lost.

Load control (an error storm must not starve the application's database pool):

* the hub never uses more than ``db_concurrency`` (3) connections at once;
* repeats of a fingerprint that arrive while that group is being written are merged in memory and written
  by the same writer in one transaction (one ``UPDATE`` + one multi-row event insert per batch), instead of
  one ``FOR UPDATE`` transaction per occurrence; at most ``batch_events`` events per batch are stored, the
  rest are only counted;
* at most ``max_pending`` groups are written concurrently; beyond that captures fall back to memory;
* ``db_timeout`` bounds waiting for a slot/connection and, separately, running the statements. Only a failed
  or slow *statement* (or a connection error) marks the database down; a busy pool just delays the write
  (the batch is retried in the background), unless it stays busy for ``db_retry_after``.

Sink failures are logged and never re-enter the hub (a context flag blocks recursive captures made from the
delivery path, e.g. a guarded admin-chat send). If the database is unavailable the hub degrades to an
in-memory dedup (first occurrence per hour is still delivered) and retries the database after a pause.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import re
import traceback
from collections import OrderedDict
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

import svbg
from svbg.core.errors.breaker import BreakerState, CircuitBreaker, StateHook
from svbg.core.errors.classify import ClassifierRegistry, Severity
from svbg.core.errors.classify import registry as default_classifier
from svbg.core.errors.fingerprint import exc_type_name, fingerprint, own_relpath
from svbg.core.errors.report import ErrorGroupView
from svbg.core.errors.sanitize import Clock, clean, default_clock, is_json_safe, sanitize_context, truncate
from svbg.core.errors.tables import error_events, error_groups

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from svbg.db.engine import Database

log = logging.getLogger("svbg.errors")

DEFAULT_HANDLED = "ошибка записана, бот продолжает работу"

_MAX_MESSAGE = 2000
_MAX_STACK = 8000
_MAX_PLACE = 200

# Database failures that make the hub fall back to memory (anything else is a hub bug and is logged).
_DB_ERRORS: tuple[type[BaseException], ...] = (TimeoutError, OSError, sa.exc.SQLAlchemyError)

# True while the hub delivers a report: captures made from inside the sink are not re-reported.
_delivering: contextvars.ContextVar[bool] = contextvars.ContextVar("svbg_errors_delivering", default=False)


class ErrorSink(Protocol):
    """Where reports go (owner DMs at stage 0, admin chat topic later)."""

    async def send_new(self, view: ErrorGroupView) -> Any | None:
        """Send a new report; return a JSON-serializable message reference.

        ``None`` means nothing was delivered: the hub retries later (with backoff).
        """
        ...

    async def update(self, view: ErrorGroupView, msg_ref: Any) -> None:
        """Edit a previously sent report in place (raise if it could not be delivered)."""
        ...


class Delivery(Enum):
    NONE = "none"
    SEND_NEW = "send_new"
    UPDATE = "update"
    DEFER = "defer"


class _DbBusy(Exception):
    """No hub slot / pool connection within ``db_timeout``: the database is busy, not necessarily down."""


@dataclass(slots=True)
class _Occurrence:
    fp: str
    now: datetime
    place: str
    module: str | None
    title: str
    severity: Severity
    sample: dict[str, Any]
    user_id: int | None
    ctx: dict[str, Any]


@dataclass(slots=True)
class _MemGroup:
    count: int
    first_seen: datetime
    last_seen: datetime
    episode_started_at: datetime
    episode_count: int


@dataclass(slots=True)
class _Batch:
    """Occurrences of one fingerprint waiting for its (single) writer."""

    cap: int
    items: list[_Occurrence] = field(default_factory=list)
    extra: int = 0  # occurrences counted but not stored as events (over ``cap``)
    backoff: bool = False  # the last write found the database busy: pause before the next one

    def add(self, occ: _Occurrence) -> None:
        if len(self.items) < self.cap:
            self.items.append(occ)
        else:
            self.items[-1] = occ  # keep the newest sample; the replaced one is still counted
            self.extra += 1

    def take(self) -> tuple[list[_Occurrence], int]:
        items, extra = self.items, self.extra
        self.items, self.extra = [], 0
        return items, extra

    def requeue(self, items: list[_Occurrence], extra: int) -> None:
        merged = items + self.items
        self.extra += extra
        if len(merged) > self.cap:
            keep = [*merged[: self.cap - 1], merged[-1]]
            self.extra += len(merged) - len(keep)
            merged = keep
        self.items = merged


@dataclass(slots=True)
class _Retry:
    attempts: int
    due: datetime
    view: ErrorGroupView | None  # set for in-memory reports (no DB row to rebuild the view from)


_FILE_RE = re.compile(r'File "([^"]+)"')


def _short_path(match: re.Match[str]) -> str:
    path = match.group(1)
    rel = own_relpath(path)
    if rel is None:
        norm = path.replace("\\", "/")
        idx = norm.rfind("site-packages/")
        if idx >= 0:
            rel = norm[idx + len("site-packages/") :]
        elif not path.startswith("<"):
            rel = norm.rsplit("/", 1)[-1]  # never expose local directories (user names in paths)
        else:
            rel = path
    return f'File "{rel}"'


def format_stack(exc: BaseException) -> str:
    """Traceback text with shortened paths, masked, capped (keeps the tail)."""
    try:
        text = "".join(traceback.format_exception(exc, limit=40))
    except Exception as e:  # noqa: BLE001 - formatting a hostile exception must not break reporting
        text = f"<traceback unavailable: {type(e).__name__}>"
    text = _FILE_RE.sub(_short_path, text)
    return truncate(clean(text), _MAX_STACK, keep="tail")


def _log_occurrence(occ: _Occurrence, *, first: bool, n: int = 1) -> None:
    """Full (masked) details once per episode; repeats only at debug level to keep logs readable."""
    s = occ.sample
    if first:
        log.warning(
            "error at %s [%s]: %s: %s\n%s", occ.place, occ.fp[:12], s["exc_type"], s["message"], s["stack"]
        )
    else:
        log.debug("repeated error at %s [%s]: %s (x%d)", occ.place, occ.fp[:12], s["exc_type"], n)


def safe_message(exc: BaseException) -> str:
    try:
        text = str(exc)
    except Exception as e:  # noqa: BLE001 - a broken __str__ must not break reporting
        text = f"<str() failed: {type(e).__name__}>"
    return truncate(clean(text), _MAX_MESSAGE)


class ErrorHub:
    def __init__(
        self,
        db: Database | None,
        sink: ErrorSink | None = None,
        clock: Clock | None = None,
        *,
        version: str | None = None,
        classifier: ClassifierRegistry | None = None,
        update_interval: float = 60.0,
        close_after: float = 3600.0,
        retention_days: int = 14,
        group_retention_days: int = 90,
        events_per_group: int = 500,
        breaker_threshold: int = 5,
        breaker_window: float = 300.0,
        breaker_quiet: float = 600.0,
        on_state_change: StateHook | None = None,
        db_timeout: float = 5.0,
        db_retry_after: float = 30.0,
        db_concurrency: int = 3,
        busy_retry: float = 1.0,
        sink_timeout: float = 30.0,
        max_pending: int = 20,
        batch_events: int = 50,
        delivery_attempts: int = 8,
        flush_interval: float = 15.0,
        memory_groups: int = 1000,
    ) -> None:
        self._db = db
        self._sink = sink
        self._clock = clock or default_clock
        self.version = version or svbg.__version__
        self._classifier = classifier or default_classifier
        self.update_interval = timedelta(seconds=update_interval)
        self.close_after = timedelta(seconds=close_after)
        self.retention = timedelta(days=retention_days)
        self.group_retention = timedelta(days=group_retention_days)
        self.events_per_group = events_per_group
        self._breaker_cfg = (breaker_threshold, breaker_window, breaker_quiet)
        self._on_state_change = on_state_change
        self._db_timeout = db_timeout
        self._db_retry_after = timedelta(seconds=db_retry_after)
        self._db_slots = asyncio.Semaphore(max(1, db_concurrency))
        self._busy_retry = busy_retry
        self._sink_timeout = sink_timeout
        self._max_pending = max_pending
        self._batch_events = max(1, batch_events)
        self._delivery_attempts = delivery_attempts
        self._flush_interval = flush_interval
        self._memory_groups = memory_groups

        self._breakers: dict[str, CircuitBreaker] = {}
        self._dirty: set[str] = set()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._batches: dict[str, _Batch] = {}
        self._retry: OrderedDict[str, _Retry] = OrderedDict()
        self._db_down_until: datetime | None = None
        self._busy_since: datetime | None = None
        self._mem: OrderedDict[str, _MemGroup] = OrderedDict()
        self._loop_task: asyncio.Task[None] | None = None
        self._last_purge: datetime | None = None
        self.dropped_to_memory = 0

    @property
    def _pending(self) -> int:
        """Groups currently being written (each by exactly one writer)."""
        return len(self._batches)

    # --- configuration -------------------------------------------------------------------------------------

    def set_sink(self, sink: ErrorSink | None) -> None:
        """Swap the delivery target (e.g. owner DMs → admin chat topic)."""
        self._sink = sink

    def breaker(self, module: str) -> CircuitBreaker:
        br = self._breakers.get(module)
        if br is None:
            threshold, window, quiet = self._breaker_cfg
            br = CircuitBreaker(
                module,
                threshold=threshold,
                window=window,
                quiet=quiet,
                clock=self._clock,
                on_change=self._on_state_change,
            )
            self._breakers[module] = br
        return br

    def breakers(self) -> dict[str, BreakerState]:
        return {name: br.state for name, br in self._breakers.items()}

    # --- capture -------------------------------------------------------------------------------------------

    async def capture(
        self,
        exc: BaseException,
        place: str,
        *,
        module: str | None = None,
        user_id: int | None = None,
        context: Mapping[str, Any] | None = None,
        handled: str = DEFAULT_HANDLED,
    ) -> str | None:
        """Record ``exc`` and schedule delivery. Returns the fingerprint (None if the capture was skipped)."""
        if _delivering.get():
            # Our own delivery path failed (e.g. admin chat send inside a guard): log only, never loop.
            log.warning(
                "error during error delivery at %s: %s: %s", place, type(exc).__name__, safe_message(exc)
            )
            return None
        try:
            return await self._capture(
                exc, place, module=module, user_id=user_id, context=context, handled=handled
            )
        except Exception:
            log.exception("error hub failed to capture an error at %s", place)
            return None

    async def _capture(
        self,
        exc: BaseException,
        place: str,
        *,
        module: str | None,
        user_id: int | None,
        context: Mapping[str, Any] | None,
        handled: str,
    ) -> str:
        now = self._clock()
        place = truncate(place, _MAX_PLACE)
        fp = fingerprint(exc, place)
        if module:
            self.breaker(module).record_error()
        cls = self._classifier.classify(exc)
        sample = {
            "exc_type": exc_type_name(exc),
            "message": safe_message(exc),
            "stack": format_stack(exc),
            "handled": truncate(clean(handled), 500),
            "hint": cls.hint_ru,
            "version": self.version,
            "last_user_id": user_id,
        }
        occ = _Occurrence(
            fp=fp,
            now=now,
            place=place,
            module=module,
            title=cls.title_ru,
            severity=cls.severity,
            sample=sample,
            user_id=user_id,
            ctx=sanitize_context(context),
        )

        if self._db is None or not self._db_available(now):
            if self._db is not None:
                self.dropped_to_memory += 1
            self._capture_memory(occ)
            return fp

        batch = self._batches.get(fp)
        if batch is not None:
            batch.add(occ)  # merged: the group's active writer persists it in its next transaction
            return fp
        if len(self._batches) >= self._max_pending:
            self.dropped_to_memory += 1
            self._capture_memory(occ)
            return fp

        batch = _Batch(self._batch_events, [occ])
        self._batches[fp] = batch
        try:
            await self._write_batch(batch)
        finally:
            # Repeats merged while we were writing (or a requeue after a busy pool) go to a background
            # writer, so this caller is not held for the whole storm.
            if batch.items:
                self._spawn(self._drain_batch(fp, batch), f"error-batch:{fp[:8]}")
            elif self._batches.get(fp) is batch:
                del self._batches[fp]
        return fp

    def _db_available(self, now: datetime) -> bool:
        return self._db_down_until is None or now >= self._db_down_until

    def _mark_down(self, now: datetime, reason: str, where: str = "") -> None:
        self._db_down_until = now + self._db_retry_after
        self._busy_since = None
        # No traceback on purpose: one short line per outage, the cause is in the DB component health.
        log.error("error hub%s: database unavailable (%s); using in-memory dedup", where, reason)

    @asynccontextmanager
    async def _conn(self) -> AsyncIterator[AsyncConnection]:
        """A hub transaction: at most ``db_concurrency`` at once.

        Waiting for a slot and a pool connection is bounded by ``db_timeout`` and raises :class:`_DbBusy`;
        the statements (and commit) then get their own ``db_timeout`` and raise ``TimeoutError``.
        """
        assert self._db is not None
        loop = asyncio.get_running_loop()
        acquired = False
        try:
            async with asyncio.timeout(self._db_timeout) as deadline:
                async with self._db_slots, self._db.tx() as conn:
                    acquired = True
                    self._busy_since = None
                    deadline.reschedule(loop.time() + self._db_timeout)
                    yield conn
        except TimeoutError as e:
            if not acquired:
                raise _DbBusy from e
            raise

    async def _write_batch(self, batch: _Batch) -> None:
        occs, extra = batch.take()
        if not occs:
            return
        now = self._clock()
        if not self._db_available(now):
            self._to_memory(occs, extra)
            return
        try:
            decision, view = await self._record(occs, extra)
        except _DbBusy:
            if self._busy_since is None:
                self._busy_since = now
                log.warning("error hub: database busy; error reports are queued")
            if now - self._busy_since < self._db_retry_after:
                batch.requeue(occs, extra)
                batch.backoff = True
                return
            self._mark_down(now, "busy too long")
            self._to_memory(occs, extra)
            return
        except _DB_ERRORS as e:
            self._mark_down(now, type(e).__name__)
            self._to_memory(occs, extra)
            return

        _log_occurrence(occs[0], first=decision is Delivery.SEND_NEW, n=len(occs) + extra)
        fp = view.fingerprint
        if decision is Delivery.DEFER:
            self._dirty.add(fp)
        elif decision in (Delivery.SEND_NEW, Delivery.UPDATE):
            self._dirty.discard(fp)
            self._spawn_delivery(decision, view)

    async def _drain_batch(self, fp: str, batch: _Batch) -> None:
        """Background writer for occurrences merged into ``batch`` (bounded per batch, one tx each)."""
        try:
            while batch.items:
                if batch.backoff:
                    batch.backoff = False
                    await asyncio.sleep(self._busy_retry)
                await self._write_batch(batch)
        except Exception:
            log.exception("error hub failed to persist merged errors for group %s", fp[:12])
        finally:
            if self._batches.get(fp) is batch:
                del self._batches[fp]

    def _to_memory(self, occs: list[_Occurrence], extra: int) -> None:
        last = len(occs) - 1
        for i, occ in enumerate(occs):
            self._capture_memory(occ, extra=extra if i == last else 0)

    async def _record(self, occs: list[_Occurrence], extra: int) -> tuple[Delivery, ErrorGroupView]:
        """Apply a batch of occurrences of one fingerprint in one transaction; decide delivery."""
        first, last = occs[0], occs[-1]
        fp, now = last.fp, last.now
        n = len(occs) + extra
        users = list(dict.fromkeys(o.user_id for o in occs if o.user_id is not None))
        last_user = next((o.user_id for o in reversed(occs) if o.user_id is not None), None)
        sample = {**last.sample, "last_user_id": last_user}
        g = error_groups.c
        locked = sa.select(error_groups).where(g.fingerprint == fp).with_for_update()
        async with self._conn() as conn:
            row = (await conn.execute(locked)).mappings().first()
            if row is None:
                ins = (
                    pg_insert(error_groups)
                    .values(
                        fingerprint=fp,
                        place=last.place,
                        module=last.module,
                        severity=last.severity.value,
                        title=last.title,
                        first_seen=first.now,
                        last_seen=now,
                        count=n,
                        users_count=len(users),
                        sample=sample,
                        status="open",
                        episode_started_at=first.now,
                        episode_count=n,
                        reopened=False,
                        notified_at=now,
                        notified_count=n,
                    )
                    .on_conflict_do_nothing(index_elements=[g.fingerprint])
                    .returning(*error_groups.c)
                )
                created = (await conn.execute(ins)).mappings().first()
                if created is not None:
                    event_id = await self._insert_events(conn, occs)
                    return Delivery.SEND_NEW, self._view(created, event_id)
                row = (await conn.execute(locked)).mappings().one()  # lost a race with another process

            if last_user is None and row["sample"]:
                sample["last_user_id"] = row["sample"].get("last_user_id")
            values: dict[str, Any] = {
                "last_seen": now,
                "count": row["count"] + n,
                "sample": sample,
                "title": last.title,
                "severity": last.severity.value,
            }
            muted = row["status"] == "muted" and row["muted_until"] is not None and row["muted_until"] > now
            if row["status"] == "muted" and not muted:
                values.update(status="open", muted_until=None)
            reopen = row["status"] == "resolved" or first.now - row["last_seen"] >= self.close_after
            chat_ref = row["chat_ref"]
            notified_at = row["notified_at"]
            if reopen:
                chat_ref, notified_at = None, None
                values.update(
                    episode_started_at=first.now,
                    episode_count=n,
                    reopened=True,
                    chat_ref=None,
                    notified_at=None,
                    notified_count=0,
                    status="muted" if muted else "open",
                )
            else:
                values["episode_count"] = row["episode_count"] + n
            if users:
                known = (
                    (
                        await conn.execute(
                            sa.select(error_events.c.user_id)
                            .distinct()
                            .where(error_events.c.fingerprint == fp, error_events.c.user_id.in_(users))
                        )
                    )
                    .mappings()
                    .all()
                )
                new_users = len(set(users) - {r["user_id"] for r in known})
                if new_users:
                    values["users_count"] = row["users_count"] + new_users

            due = notified_at is None or now - notified_at >= self.update_interval
            if muted:
                decision = Delivery.NONE
            elif not due:
                decision = Delivery.DEFER
            else:
                decision = Delivery.SEND_NEW if chat_ref is None else Delivery.UPDATE
            if decision in (Delivery.SEND_NEW, Delivery.UPDATE):
                values.update(notified_at=now, notified_count=values["count"])

            updated = (
                (
                    await conn.execute(
                        sa.update(error_groups)
                        .where(g.fingerprint == fp)
                        .values(**values)
                        .returning(*error_groups.c)
                    )
                )
                .mappings()
                .one()
            )
            event_id = await self._insert_events(conn, occs)
            return decision, self._view(updated, event_id)

    @staticmethod
    async def _insert_events(conn: AsyncConnection, occs: list[_Occurrence]) -> int:
        """One multi-row insert; returns the id of the newest event."""
        rows = [{"fingerprint": o.fp, "ts": o.now, "user_id": o.user_id, "context": o.ctx} for o in occs]
        result = await conn.execute(sa.insert(error_events).values(rows).returning(error_events.c.id))
        return max(int(r["id"]) for r in result.mappings().all())

    def _view(self, row: Mapping[str, Any], event_id: int | None = None) -> ErrorGroupView:
        sample: Mapping[str, Any] = row["sample"] or {}
        return ErrorGroupView(
            fingerprint=row["fingerprint"],
            place=row["place"],
            module=row["module"],
            severity=Severity(row["severity"]),
            title=row["title"],
            hint=str(sample.get("hint", "")),
            handled=str(sample.get("handled", "")),
            first_seen=row["first_seen"],
            last_seen=row["last_seen"],
            count=int(row["count"]),
            users_count=int(row["users_count"]),
            episode_count=int(row["episode_count"]),
            episode_started_at=row["episode_started_at"],
            reopened=bool(row["reopened"]),
            status=row["status"],
            muted_until=row["muted_until"],
            last_user_id=sample.get("last_user_id"),
            exc_type=str(sample.get("exc_type", "")),
            message=str(sample.get("message", "")),
            stack=str(sample.get("stack", "")),
            version=str(sample.get("version", self.version)),
            event_id=event_id,
            chat_ref=row["chat_ref"],
        )

    # --- in-memory fallback --------------------------------------------------------------------------------

    def _capture_memory(self, occ: _Occurrence, *, extra: int = 0) -> None:
        fp, now, sample = occ.fp, occ.now, occ.sample
        mg = self._mem.get(fp)
        send = False
        if mg is None or now - mg.last_seen >= self.close_after:
            reopened = mg is not None
            mg = _MemGroup(
                count=(mg.count if mg else 0) + 1 + extra,
                first_seen=mg.first_seen if mg else now,
                last_seen=now,
                episode_started_at=now,
                episode_count=1 + extra,
            )
            send = True
        else:
            reopened = False
            mg.count += 1 + extra
            mg.episode_count += 1 + extra
            mg.last_seen = now
        self._mem[fp] = mg
        self._mem.move_to_end(fp)
        while len(self._mem) > self._memory_groups:
            self._mem.popitem(last=False)
        _log_occurrence(occ, first=send, n=1 + extra)
        if not send:
            return
        view = ErrorGroupView(
            fingerprint=fp,
            place=occ.place,
            module=occ.module,
            severity=occ.severity,
            title=occ.title,
            hint=str(sample["hint"]),
            handled=str(sample["handled"]),
            first_seen=mg.first_seen,
            last_seen=now,
            count=mg.count,
            users_count=1 if occ.user_id is not None else 0,
            episode_count=mg.episode_count,
            episode_started_at=now,
            reopened=reopened,
            last_user_id=occ.user_id,
            exc_type=str(sample["exc_type"]),
            message=str(sample["message"]),
            stack=str(sample["stack"]),
            version=self.version,
            extra={"degraded": "database unavailable"},
        )
        self._spawn_delivery(Delivery.SEND_NEW, view, persist=False)

    # --- delivery ------------------------------------------------------------------------------------------

    def _spawn(self, coro: Any, name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _spawn_delivery(self, decision: Delivery, view: ErrorGroupView, *, persist: bool = True) -> None:
        if self._sink is None:
            return
        self._spawn(self._deliver(decision, view, persist), f"error-report:{view.fingerprint[:8]}")

    async def _deliver(self, decision: Delivery, view: ErrorGroupView, persist: bool) -> None:
        sink = self._sink
        if sink is None:
            return
        _delivering.set(True)  # task-local: this task has its own context copy
        ref: Any = None
        try:
            async with asyncio.timeout(self._sink_timeout):
                if decision is Delivery.SEND_NEW:
                    ref = await sink.send_new(view)
                else:
                    await sink.update(view, view.chat_ref)
        except Exception as e:  # noqa: BLE001 - any sink failure is isolated and must not loop back
            log.warning(
                "error report delivery failed (%s) for group %s: %s",
                decision.value,
                view.fingerprint[:12],
                clean(f"{type(e).__name__}: {e}"),
            )
            self._schedule_retry(view, persist)
            return
        if decision is Delivery.SEND_NEW and ref is None:
            log.warning("error report for group %s was not delivered; will retry", view.fingerprint[:12])
            self._schedule_retry(view, persist)
            return
        self._retry.pop(view.fingerprint, None)
        if decision is not Delivery.SEND_NEW or not persist or self._db is None:
            return
        if not is_json_safe(ref):
            log.warning("error sink returned a non-JSON message reference; updates will resend")
            return
        g = error_groups.c
        try:
            async with self._conn() as conn:
                await conn.execute(
                    sa.update(error_groups)
                    .where(g.fingerprint == view.fingerprint, g.episode_started_at == view.episode_started_at)
                    .values(chat_ref=ref)
                )
        except (_DbBusy, *_DB_ERRORS) as e:
            log.warning("error hub: could not store report reference (%s)", type(e).__name__)

    def _schedule_retry(self, view: ErrorGroupView, persist: bool) -> None:
        """Remember a failed delivery; :meth:`flush` retries it with exponential backoff."""
        fp = view.fingerprint
        prev = self._retry.pop(fp, None)
        attempts = (prev.attempts if prev else 0) + 1
        if attempts >= self._delivery_attempts:
            log.warning("error report for group %s dropped after %d failed deliveries", fp[:12], attempts)
            return
        delay = min(self.update_interval * 2 ** (attempts - 1), self.close_after)
        self._retry[fp] = _Retry(attempts, self._clock() + delay, None if persist else view)
        while len(self._retry) > self._memory_groups:
            self._retry.popitem(last=False)

    async def drain(self, grace: float | None = None) -> None:
        """Wait for in-flight writes and report deliveries (tests, graceful stop), including the ones they
        spawn; cancel leftovers after ``grace`` seconds."""
        loop = asyncio.get_running_loop()
        deadline = None if grace is None else loop.time() + grace
        while self._tasks:
            timeout = None if deadline is None else max(0.0, deadline - loop.time())
            _done, pending = await asyncio.wait(list(self._tasks), timeout=timeout)
            if pending:
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                return

    async def flush(self) -> int:
        """Deliver throttled updates and failed deliveries that are now due; evaluate breakers.

        Returns the number of deliveries scheduled."""
        for br in list(self._breakers.values()):
            _ = br.state  # fires half-open/closed transitions on time
        now = self._clock()
        scheduled = 0
        # Hold re-sent reports for a while: their outcome either clears or reschedules the retry.
        hold = max(self.update_interval, timedelta(seconds=self._sink_timeout))
        forced: set[str] = set()
        for fp, r in list(self._retry.items()):
            if r.due > now:
                continue
            if r.view is not None:
                r.due = now + hold
                self._spawn_delivery(Delivery.SEND_NEW, r.view, persist=False)
                scheduled += 1
            else:
                forced.add(fp)
        if self._db is None or not (self._dirty or forced) or not self._db_available(now):
            return scheduled
        for fp in list(self._dirty | forced):
            try:
                decision, view = await self._flush_one(fp, now, force=fp in forced)
            except _DbBusy:
                log.debug("error hub flush: database busy, retrying later")
                return scheduled
            except _DB_ERRORS as e:
                self._mark_down(now, type(e).__name__, " flush")
                return scheduled
            if decision is Delivery.DEFER:
                continue
            self._dirty.discard(fp)
            if view is None or decision not in (Delivery.SEND_NEW, Delivery.UPDATE):
                self._retry.pop(fp, None)  # resolved / muted / gone / already shown: nothing to retry
                continue
            if (r := self._retry.get(fp)) is not None:
                r.due = now + hold
            self._spawn_delivery(decision, view)
            scheduled += 1
        return scheduled

    async def _flush_one(
        self, fp: str, now: datetime, *, force: bool = False
    ) -> tuple[Delivery, ErrorGroupView | None]:
        g = error_groups.c
        async with self._conn() as conn:
            row = (
                (await conn.execute(sa.select(error_groups).where(g.fingerprint == fp).with_for_update()))
                .mappings()
                .first()
            )
            if row is None or row["status"] == "resolved":
                return Delivery.NONE, None
            if not force and row["notified_count"] >= row["count"]:
                return Delivery.NONE, None
            if row["status"] == "muted" and row["muted_until"] is not None and row["muted_until"] > now:
                return Delivery.NONE, None
            if row["notified_at"] is not None and now - row["notified_at"] < self.update_interval:
                return Delivery.DEFER, None  # also guards a forced retry against a concurrent delivery
            decision = Delivery.SEND_NEW if row["chat_ref"] is None else Delivery.UPDATE
            updated = (
                (
                    await conn.execute(
                        sa.update(error_groups)
                        .where(g.fingerprint == fp)
                        .values(notified_at=now, notified_count=row["count"])
                        .returning(*error_groups.c)
                    )
                )
                .mappings()
                .one()
            )
            return decision, self._view(updated)

    # --- owner actions & queries ---------------------------------------------------------------------------

    async def mute(self, fp: str, until: datetime) -> bool:
        """Silence a group until ``until`` (buttons "Заглушить 1 ч / 24 ч")."""
        return await self._set_status(fp, status="muted", muted_until=until)

    async def unmute(self, fp: str) -> bool:
        return await self._set_status(fp, status="open", muted_until=None)

    async def resolve(self, fp: str) -> bool:
        """Mark fixed; the next occurrence opens a new episode ("🔁 снова")."""
        self._dirty.discard(fp)
        self._retry.pop(fp, None)
        return await self._set_status(fp, status="resolved", muted_until=None)

    async def _set_status(self, fp: str, *, status: str, muted_until: datetime | None) -> bool:
        if self._db is None:
            return False
        async with self._db.tx() as conn:
            result = await conn.execute(
                sa.update(error_groups)
                .where(error_groups.c.fingerprint == fp)
                .values(status=status, muted_until=muted_until)
            )
            return result.rowcount > 0

    async def get(self, fp: str) -> ErrorGroupView | None:
        if self._db is None:
            return None
        async with self._db.read() as conn:
            row = (
                (await conn.execute(sa.select(error_groups).where(error_groups.c.fingerprint == fp)))
                .mappings()
                .first()
            )
        return self._view(row) if row is not None else None

    async def open_groups(self, limit: int = 10, *, since: timedelta | None = None) -> list[ErrorGroupView]:
        """Most recent open/muted groups (for the "Состояние" screen)."""
        if self._db is None:
            return []
        now = self._clock()
        g = error_groups.c
        q = (
            sa.select(error_groups)
            .where(g.status != "resolved", g.last_seen >= now - (since or self.close_after))
            .order_by(g.last_seen.desc())
            .limit(limit)
        )
        async with self._db.read() as conn:
            rows = (await conn.execute(q)).mappings().all()
        return [self._view(r) for r in rows]

    async def purge(self) -> int:
        """Drop events older than the retention, keep at most ``events_per_group`` per group, drop stale
        groups. Returns deleted event rows (excluding cascades)."""
        if self._db is None:
            return 0
        now = self._clock()
        e = error_events.c
        ranked = sa.select(
            e.id,
            sa.func.row_number().over(partition_by=e.fingerprint, order_by=e.id.desc()).label("rn"),
        ).subquery()
        excess = sa.select(ranked.c.id).where(ranked.c.rn > self.events_per_group)
        async with self._db.tx() as conn:
            old = await conn.execute(sa.delete(error_events).where(e.ts < now - self.retention))
            over = await conn.execute(sa.delete(error_events).where(e.id.in_(excess)))
            await conn.execute(
                sa.delete(error_groups).where(error_groups.c.last_seen < now - self.group_retention)
            )
        self._last_purge = now
        return int(old.rowcount or 0) + int(over.rowcount or 0)

    # --- lifecycle -----------------------------------------------------------------------------------------

    async def start(self) -> None:
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = asyncio.create_task(self._loop(), name="error-hub")

    async def stop(self, grace: float = 5.0) -> None:
        task, self._loop_task = self._loop_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self.drain(grace)

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._flush_interval)
            try:
                await self.flush()
                now = self._clock()
                if self._last_purge is None or now - self._last_purge >= timedelta(hours=1):
                    self._last_purge = now
                    await self.purge()
            except Exception:
                log.exception("error hub background loop iteration failed")
