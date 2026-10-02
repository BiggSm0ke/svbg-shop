"""Delivery of a broadcast: a persistent ``jobs`` task with a cursor, the global rate of the notifier.

* :meth:`BroadcastSender.run_job` (kind ``broadcast.run``, dedup key ``broadcast:<id>``) works in slices of
  ``slice_s`` seconds: batches of ``batch_size`` recipients ordered by ``users.id`` (one ``SELECT``), sent
  concurrently through the :class:`~svbg.tg.notifier.Notifier` (``LOW`` priority: ~25 msg/s globally, 1/s
  per chat), then **one** transaction advances ``cursor``/``skip`` and the counters, marks ``403`` chats in
  ``users.bot_blocked_at`` and remembers messages to delete later. At the end of a slice the job re-arms
  itself (same dedup key → the running job runs again), so a long broadcast never monopolises a worker slot.
* Restart: the job is released or its lease expires; the next run resumes from the cursor (at most one batch
  may be delivered twice when the process dies mid-batch).
* Pause / stop: the status changes in the database and :meth:`interrupt` wakes the running slice at once —
  in-flight sends are cancelled, the batch's finished part is saved (``skip`` keeps the ids sent above the
  cursor, so «Продолжить» never sends them twice). The batch-end ``UPDATE … RETURNING status`` covers another
  process: either way the run stops within a few seconds.
* Progress: one message edited at most every ``progress_every`` seconds (sent, errors, blocked, ETA).
* Delivery: ``copyMessage`` of the admin's original (every entity, incl. Premium emoji and spoilers, is kept
  by Telegram); when the original is gone, ``send*`` from the normalized copy with ``entities=``.
* «Удалить через N ч»: ``broadcast_msgs`` + the job ``broadcast.cleanup`` (``deleteMessage`` when due).
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import html
import logging
import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Final, Protocol, TypeVar

import sqlalchemy as sa
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.methods import DeleteMessage, EditMessageText, PinChatMessage, TelegramMethod
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.dialects.postgresql import insert as pg_insert

from svbg.broadcasts.message import build_markup, copy_method, send_method
from svbg.broadcasts.repo import Broadcast, BroadcastRepo, Recipient
from svbg.broadcasts.tables import broadcast_msgs, broadcasts
from svbg.core import clock
from svbg.core.tables import users
from svbg.jobs import PermanentJobError, RetryJob, enqueue
from svbg.tg.notifier import TRANSPORT_ERRORS, NotifierClosedError, NotifierError, Priority
from svbg.tg.ui.codec import encode

if TYPE_CHECKING:
    from svbg.db.engine import Database
    from svbg.jobs import Job, JobContext

__all__ = [
    "CONTROL_SCREEN",
    "JOB_CLEANUP",
    "JOB_RUN",
    "BroadcastSender",
    "Delivery",
    "Outcome",
    "controls",
    "progress_text",
    "run_dedup",
]

log = logging.getLogger("svbg.broadcasts")

T = TypeVar("T")

JOB_RUN: Final = "broadcast.run"
JOB_CLEANUP: Final = "broadcast.cleanup"
#: Callback screen of the control buttons (actions are registered by ``svbg.tg.admin.broadcasts``).
CONTROL_SCREEN: Final = "bca"
CARD_SCREEN: Final = "bc.c"
GLOBAL_RATE: Final = 25.0
_CLEANUP_BATCH: Final = 100
_SOURCE_GONE: Final = ("message to copy not found", "message_id_invalid", "message not found")
_NOT_MODIFIED: Final = "message is not modified"

STATUS_LABELS: Final[Mapping[str, str]] = {
    "draft": "📝 черновик",
    "running": "▶️ идёт",
    "paused": "⏸ на паузе",
    "done": "✅ завершена",
    "canceled": "⏹ остановлена",
}


class Outcome(enum.Enum):
    SENT = "sent"
    BLOCKED = "blocked"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Delivery:
    outcome: Outcome
    message_id: int | None = None
    error: str | None = None


class NotifierLike(Protocol):
    async def call(
        self,
        method: TelegramMethod[T],
        *,
        chat_id: int | str,
        priority: Priority = Priority.NORMAL,
        coalesce_key: str | None = None,
    ) -> T | None: ...


def run_dedup(bid: int) -> str:
    return f"broadcast:{bid}"


def cleanup_dedup(bid: int) -> str:
    return f"broadcast.cleanup:{bid}"


def _fmt(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def progress_text(bc: Broadcast, *, rate: float | None = None) -> str:
    """Progress / summary text (HTML)."""
    status = STATUS_LABELS.get(bc.status, bc.status)
    lines = [f"📣 <b>Рассылка #{bc.id}</b> · {html.escape(status)}"]
    total = max(bc.total, bc.processed)
    pct = f" ({bc.processed * 100 // total}%)" if total else ""
    lines.append(f"Отправлено: {_fmt(bc.sent)} из ~{_fmt(total)}{pct}")
    lines.append(f"Ошибок: {_fmt(bc.failed)} · Заблокировали бота: {_fmt(bc.blocked)}")
    if bc.status == "running":
        speed = rate if rate and rate > 0 else (GLOBAL_RATE / 2 if bc.pin else GLOBAL_RATE)
        left = max(total - bc.processed, 0)
        minutes = math.ceil(left / speed / 60) if left else 0
        lines.append(f"Осталось ≈ {minutes} мин" if minutes else "Почти готово")
    if bc.status == "paused":
        lines.append("Нажмите «Продолжить» — отправка пойдёт с того же места.")
    return "\n".join(lines)


def controls(bc: Broadcast) -> InlineKeyboardMarkup:
    """Buttons of the progress message for the current status."""
    arg = str(bc.id)
    rows: list[list[InlineKeyboardButton]] = []
    if bc.status == "running":
        rows.append(
            [
                InlineKeyboardButton(text="⏸ Пауза", callback_data=encode(CONTROL_SCREEN, "pause", arg)),
                InlineKeyboardButton(
                    text="⏹ Остановить", callback_data=encode(CONTROL_SCREEN, "stop", arg), style="danger"
                ),
            ]
        )
    elif bc.status == "paused":
        rows.append(
            [
                InlineKeyboardButton(
                    text="▶️ Продолжить", callback_data=encode(CONTROL_SCREEN, "resume", arg), style="success"
                ),
                InlineKeyboardButton(
                    text="⏹ Остановить", callback_data=encode(CONTROL_SCREEN, "stop", arg), style="danger"
                ),
            ]
        )
    rows.append([InlineKeyboardButton(text="📋 Карточка", callback_data=encode(CARD_SCREEN, arg=arg))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _bid(payload: Mapping[str, Any]) -> int:
    bid = payload.get("broadcast_id")
    if isinstance(bid, bool) or not isinstance(bid, int) or bid <= 0:
        raise PermanentJobError(f"bad broadcast_id in payload: {bid!r}")
    return bid


def _source_gone(exc: TelegramBadRequest) -> bool:
    message = (exc.message or "").lower()
    return any(s in message for s in _SOURCE_GONE)


class BroadcastSender:
    def __init__(
        self,
        db: Database,
        notifier: NotifierLike,
        *,
        repo: BroadcastRepo | None = None,
        bot_username: Callable[[], str | None] = lambda: None,
        batch_size: int = 50,
        slice_s: float = 30.0,
        progress_every: float = 5.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if batch_size < 1 or slice_s <= 0 or progress_every <= 0:
            raise ValueError("batch_size, slice_s and progress_every must be positive")
        self.db = db
        self.notifier = notifier
        self.repo = repo or BroadcastRepo(db)
        self.bot_username = bot_username
        self.batch_size = batch_size
        self.slice_s = slice_s
        self.progress_every = progress_every
        self._mono = monotonic
        self._interrupts: dict[int, asyncio.Event] = {}
        self._source_lost: set[int] = set()

    def handlers(self) -> dict[str, Callable[[Job, JobContext], Any]]:
        return {JOB_RUN: self.run_job, JOB_CLEANUP: self.cleanup_job}

    def interrupt(self, bid: int) -> bool:
        """Wake the running slice of ``bid`` (after its status changed). ``True`` if one was running here."""
        event = self._interrupts.get(bid)
        if event is None:
            return False
        event.set()
        return True

    def running(self, bid: int) -> bool:
        return bid in self._interrupts

    # ------------------------------------------------------------------ one message

    async def _call(self, method: TelegramMethod[T], chat_id: int, priority: Priority) -> T | None:
        return await self.notifier.call(method, chat_id=chat_id, priority=priority)

    async def deliver(
        self,
        bc: Broadcast,
        chat_id: int,
        lang: str,
        markups: dict[str, InlineKeyboardMarkup | None] | None = None,
        *,
        pin: bool | None = None,
        priority: Priority = Priority.LOW,
    ) -> Delivery:
        """Send ``bc`` to one chat. Never raises for Telegram/network errors (only on notifier shutdown)."""
        cache = markups if markups is not None else {}
        if lang not in cache:
            cache[lang] = build_markup(bc.buttons, lang, bot_username=self.bot_username())
        markup = cache[lang]
        try:
            result: Any = None
            sent = False
            if bc.can_copy and bc.id not in self._source_lost:
                assert bc.source_chat_id is not None and bc.source_msg_id is not None
                method = copy_method(bc.source_chat_id, bc.source_msg_id, chat_id, markup, silent=bc.silent)
                try:
                    result, sent = await self._call(method, chat_id, priority), True
                except TelegramBadRequest as e:
                    if not _source_gone(e):
                        raise
                    log.info("broadcast %s: the original message is gone, sending from the copy", bc.id)
                    self._source_lost.add(bc.id)
            if not sent:
                result = await self._call(
                    send_method(bc.content, chat_id, markup, silent=bc.silent), chat_id, priority
                )
        except NotifierClosedError:
            raise
        except (TelegramAPIError, NotifierError, TimeoutError, ValueError, *TRANSPORT_ERRORS) as e:
            reason = getattr(e, "message", None) or type(e).__name__
            log.debug("broadcast %s to %s failed: %s", bc.id, chat_id, reason)
            return Delivery(Outcome.FAILED, error=str(reason)[:200])
        if result is None:
            return Delivery(Outcome.BLOCKED)
        msg_id = getattr(result, "message_id", None)
        if (bc.pin if pin is None else pin) and isinstance(msg_id, int):
            with contextlib.suppress(TelegramAPIError, NotifierError, TimeoutError, *TRANSPORT_ERRORS):
                await self._call(
                    PinChatMessage(
                        chat_id=chat_id, message_id=msg_id, disable_notification=bc.silent or None
                    ),
                    chat_id,
                    priority,
                )
        return Delivery(Outcome.SENT, msg_id if isinstance(msg_id, int) else None)

    # ------------------------------------------------------------------ progress

    async def refresh(self, bc: Broadcast, *, rate: float | None = None) -> None:
        """Edit the progress message (errors such as «not modified» are ignored)."""
        ref = bc.progress_msg
        if not ref:
            return
        chat_id, message_id = ref.get("chat_id"), ref.get("message_id")
        if not isinstance(chat_id, int) or not isinstance(message_id, int):
            return
        method = EditMessageText(
            chat_id=chat_id,
            message_id=message_id,
            text=progress_text(bc, rate=rate),
            parse_mode="HTML",
            reply_markup=controls(bc),
        )
        try:
            await self._call(method, chat_id, Priority.NORMAL)
        except TelegramBadRequest as e:
            if _NOT_MODIFIED not in (e.message or "").lower():
                log.debug("broadcast %s: progress edit rejected: %s", bc.id, e.message)
        except (TelegramAPIError, NotifierError, TimeoutError, *TRANSPORT_ERRORS) as e:
            log.debug("broadcast %s: progress edit failed: %s", bc.id, type(e).__name__)

    # ------------------------------------------------------------------ the job

    async def run_job(self, job: Job, _ctx: JobContext) -> None:
        bid = _bid(job.payload)
        if await self.run(bid):
            async with self.db.tx() as conn:
                await enqueue(conn, JOB_RUN, {"broadcast_id": bid}, dedup_key=run_dedup(bid))

    async def run(self, bid: int) -> bool:
        """One slice. ``True`` = the broadcast is still running and needs another slice."""
        event = asyncio.Event()
        self._interrupts[bid] = event
        try:
            return await self._run(bid, event)
        finally:
            if self._interrupts.get(bid) is event:
                del self._interrupts[bid]

    async def _run(self, bid: int, event: asyncio.Event) -> bool:
        bc = await self.repo.get(bid)
        if bc is None or bc.status != "running":
            return False
        started = last_edit = self._mono()
        done_here = 0
        while True:
            if self._mono() - started >= self.slice_s:
                return True
            async with self.db.read() as conn:
                batch = await self.repo.audience.batch(
                    conn, bc.segment, after=bc.cursor, skip=bc.skip, limit=self.batch_size
                )
            if not batch:
                await self._finish(bc)
                return False
            results, interrupted, closed = await self._send_batch(bc, batch, event)
            saved = await self._save(bc, batch, results)
            done_here += len(results)
            if closed:
                raise RetryJob(5.0, "notifier is closed (shutdown)")
            if saved is None:  # the broadcast row is gone
                return False
            bc = saved
            if bc.status != "running":
                await self.refresh(bc)
                return False
            if interrupted:
                event.clear()  # woken, but the status is still «running» (e.g. resumed again)
            now = self._mono()
            if now - last_edit >= self.progress_every:
                elapsed = max(now - started, 1e-6)
                await self.refresh(bc, rate=done_here / elapsed if done_here else None)
                last_edit = now

    async def _send_batch(
        self, bc: Broadcast, batch: Sequence[Recipient], event: asyncio.Event
    ) -> tuple[dict[int, Delivery], bool, bool]:
        """Send to ``batch`` concurrently, stop early on ``event``. → (results, interrupted, closed)."""
        markups: dict[str, InlineKeyboardMarkup | None] = {}
        tasks = {
            asyncio.ensure_future(self.deliver(bc, r.telegram_id, r.lang, markups)): r.user_id for r in batch
        }
        waiter = asyncio.ensure_future(event.wait())
        results: dict[int, Delivery] = {}
        pending = set(tasks)
        interrupted = closed = False
        try:
            while pending and not closed:
                done, _ = await asyncio.wait(pending | {waiter}, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    if task is waiter:
                        continue
                    pending.discard(task)
                    exc = task.exception()
                    if isinstance(exc, NotifierClosedError):
                        closed = True
                    elif exc is not None:
                        log.warning("broadcast %s: delivery crashed: %s", bc.id, type(exc).__name__)
                        results[tasks[task]] = Delivery(Outcome.FAILED, error=type(exc).__name__)
                    else:
                        results[tasks[task]] = task.result()
                if waiter in done:
                    interrupted = True
                    break
        finally:
            for task in pending:
                task.cancel()
            waiter.cancel()
            await asyncio.gather(*pending, waiter, return_exceptions=True)
        return results, interrupted, closed

    async def _save(
        self, bc: Broadcast, batch: Sequence[Recipient], results: Mapping[int, Delivery]
    ) -> Broadcast | None:
        cursor = bc.cursor
        for r in batch:  # ordered by id: the cursor moves over the finished prefix only
            if r.user_id not in results:
                break
            cursor = r.user_id
        skip = [uid for uid in results if uid > cursor] + [uid for uid in bc.skip if uid > cursor]
        counts = dict.fromkeys(Outcome, 0)
        for d in results.values():
            counts[d.outcome] += 1
        by_user = {r.user_id: r for r in batch}
        blocked = [by_user[uid].telegram_id for uid, d in results.items() if d.outcome is Outcome.BLOCKED]
        delete_h = bc.delete_after_h
        to_delete = (
            [
                {
                    "broadcast_id": bc.id,
                    "user_id": uid,
                    "chat_id": by_user[uid].telegram_id,
                    "msg_id": d.message_id,
                    "delete_at": clock.now() + timedelta(hours=delete_h),
                }
                for uid, d in results.items()
                if d.outcome is Outcome.SENT and d.message_id is not None
            ]
            if delete_h
            else []
        )
        async with self.db.tx() as conn:
            saved = await self.repo.save_progress(
                conn,
                bc.id,
                cursor=cursor,
                skip=skip,
                sent=counts[Outcome.SENT],
                failed=counts[Outcome.FAILED],
                blocked=counts[Outcome.BLOCKED],
            )
            if blocked:
                await conn.execute(
                    sa.update(users)
                    .where(users.c.telegram_id.in_(blocked), users.c.bot_blocked_at.is_(None))
                    .values(bot_blocked_at=sa.func.now())
                )
            if to_delete and saved is not None:
                ins = pg_insert(broadcast_msgs).values(to_delete).on_conflict_do_nothing()
                await conn.execute(ins)
                await enqueue(
                    conn,
                    JOB_CLEANUP,
                    {"broadcast_id": bc.id},
                    dedup_key=cleanup_dedup(bc.id),
                    run_at=min(r["delete_at"] for r in to_delete),
                )
            if bc.id in self._source_lost and bc.source_msg_id is not None and saved is not None:
                await conn.execute(
                    sa.update(broadcasts).where(broadcasts.c.id == bc.id).values(source_msg_id=None)
                )
                saved = replace(saved, source_msg_id=None)
        return saved

    async def _finish(self, bc: Broadcast) -> None:
        async with self.db.tx() as conn:
            done = await self.repo.transition(conn, bc.id, ("running",), "done", finished_at=sa.func.now())
        final = done or await self.repo.get(bc.id)
        if final is not None:
            log.info(
                "broadcast %s finished: sent %s, failed %s, blocked %s",
                final.id,
                final.sent,
                final.failed,
                final.blocked,
            )
            await self.refresh(final)
        self._source_lost.discard(bc.id)

    # ------------------------------------------------------------------ delete after N hours

    async def cleanup_job(self, job: Job, _ctx: JobContext) -> None:
        bid = _bid(job.payload)
        deadline = self._mono() + self.slice_s
        while True:
            due = (
                sa.select(broadcast_msgs.c.user_id, broadcast_msgs.c.chat_id, broadcast_msgs.c.msg_id)
                .where(broadcast_msgs.c.broadcast_id == bid, broadcast_msgs.c.delete_at <= sa.func.now())
                .order_by(broadcast_msgs.c.delete_at)
                .limit(_CLEANUP_BATCH)
            )
            async with self.db.read() as conn:
                rows = (await conn.execute(due)).all()
            if not rows:
                break
            await asyncio.gather(*(self._delete(int(r.chat_id), int(r.msg_id)) for r in rows))
            async with self.db.tx() as conn:
                await conn.execute(
                    sa.delete(broadcast_msgs).where(
                        broadcast_msgs.c.broadcast_id == bid,
                        broadcast_msgs.c.user_id.in_([int(r.user_id) for r in rows]),
                    )
                )
            if self._mono() >= deadline:
                break
        async with self.db.tx() as conn:
            nxt = await conn.scalar(
                sa.select(sa.func.min(broadcast_msgs.c.delete_at)).where(broadcast_msgs.c.broadcast_id == bid)
            )
            if nxt is not None:
                run_at = max(nxt, clock.now())
                await enqueue(
                    conn, JOB_CLEANUP, {"broadcast_id": bid}, dedup_key=cleanup_dedup(bid), run_at=run_at
                )

    async def _delete(self, chat_id: int, msg_id: int) -> None:
        with contextlib.suppress(TelegramAPIError, NotifierError, TimeoutError, *TRANSPORT_ERRORS):
            await self._call(DeleteMessage(chat_id=chat_id, message_id=msg_id), chat_id, Priority.LOW)
