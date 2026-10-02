"""Broadcast operations for the admin UI: drafts, test send, start / pause / resume / stop.

Start, resume and stop are one transaction each: the conditional status change, the ``broadcast.run`` job
(start/resume) and the ``admin_audit`` row. Telegram calls (progress message, test send) happen after the
commit, never inside a transaction.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from aiogram.exceptions import TelegramAPIError
from aiogram.methods import SendMessage
from aiogram.types import LinkPreviewOptions

from svbg.broadcasts.message import ComposeError
from svbg.broadcasts.repo import DELETE_AFTER_CHOICES, Broadcast, BroadcastRepo
from svbg.broadcasts.segments import SegmentError
from svbg.broadcasts.sender import JOB_RUN, BroadcastSender, Delivery, controls, progress_text, run_dedup
from svbg.broadcasts.tables import broadcasts
from svbg.core.tables import admin_audit
from svbg.jobs import enqueue
from svbg.tg.notifier import TRANSPORT_ERRORS, NotifierError, Priority

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

__all__ = ["Actor", "BroadcastError", "BroadcastService"]

log = logging.getLogger("svbg.broadcasts")


class BroadcastError(Exception):
    """A user-facing refusal (``str`` is a short Russian text)."""


@dataclass(frozen=True, slots=True)
class Actor:
    user_id: int
    role: str


class BroadcastService:
    def __init__(self, repo: BroadcastRepo, sender: BroadcastSender) -> None:
        self.repo = repo
        self.sender = sender
        self.db = repo.db

    # ------------------------------------------------------------ drafts

    async def create(self, actor: Actor, *, chat_id: int, message_id: int, content: Mapping[str, Any]) -> int:
        return await self.repo.create(
            actor_id=actor.user_id, source_chat_id=chat_id, source_msg_id=message_id, content=content
        )

    async def replace_message(
        self, bid: int, *, chat_id: int, message_id: int, content: Mapping[str, Any]
    ) -> Broadcast | None:
        return await self.repo.update_draft(
            bid, source_chat_id=chat_id, source_msg_id=message_id, content=dict(content)
        )

    async def set_buttons(self, bid: int, buttons: Sequence[Mapping[str, Any]]) -> Broadcast | None:
        return await self.repo.update_draft(bid, buttons=[dict(b) for b in buttons])

    async def set_segment(self, bid: int, segment: Mapping[str, Any]) -> Broadcast:
        try:
            bc = await self.repo.set_segment(bid, segment)
        except SegmentError as e:
            raise BroadcastError(str(e)) from None
        if bc is None:
            raise BroadcastError("Черновик уже запущен или удалён")
        return bc

    async def toggle(self, bc: Broadcast, option: str) -> Broadcast | None:
        options = dict(bc.options)
        if option in ("pin", "silent"):
            options[option] = not bool(options.get(option))
        elif option == "delete":
            choices = DELETE_AFTER_CHOICES
            current = bc.delete_after_h
            nxt = choices[(choices.index(current) + 1) % len(choices)]
            if nxt is None:
                options.pop("delete_after_h", None)
            else:
                options["delete_after_h"] = nxt
        else:
            raise BroadcastError("Неизвестная опция")
        return await self.repo.update_draft(bc.id, options=options)

    async def test_send(self, bc: Broadcast, chat_id: int, lang: str) -> Delivery:
        """The exact copy to the admin's private chat (no pin, normal priority)."""
        try:
            return await self.sender.deliver(bc, chat_id, lang, pin=False, priority=Priority.NORMAL)
        except ComposeError as e:
            raise BroadcastError(str(e)) from None

    # ------------------------------------------------------------ run state

    @staticmethod
    async def _audit(conn: AsyncConnection, actor: Actor, action: str, bid: int, **details: Any) -> None:
        await conn.execute(
            sa.insert(admin_audit).values(
                actor_id=actor.user_id,
                role=actor.role,
                action=action,
                target=f"broadcast:{bid}",
                details=details,
            )
        )

    async def start(self, bid: int, actor: Actor, chat_id: int) -> Broadcast:
        """Draft → running (recipients recounted in the same transaction), job + audit; then the progress
        message is sent to ``chat_id``."""
        async with self.db.tx() as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(broadcasts.c.segment, broadcasts.c.status)
                        .where(broadcasts.c.id == bid)
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if row is None or row["status"] != "draft":
                raise BroadcastError("Рассылка уже запущена или удалена")
            try:
                total = await self.repo.audience.count(conn, row["segment"])
            except SegmentError as e:
                raise BroadcastError(str(e)) from None
            if total == 0:
                raise BroadcastError("Нет получателей: измените сегмент")
            bc = await self.repo.transition(
                conn, bid, ("draft",), "running", total=total, started_at=sa.func.now(), cursor=0, skip=[]
            )
            assert bc is not None  # the row is locked
            await enqueue(conn, JOB_RUN, {"broadcast_id": bid}, dedup_key=run_dedup(bid))
            await self._audit(conn, actor, "broadcast.start", bid, total=total)
        log.info("broadcast %s started by user %s for %s recipients", bid, actor.user_id, total)
        return await self._post_progress(bc, chat_id)

    async def _post_progress(self, bc: Broadcast, chat_id: int) -> Broadcast:
        try:
            msg = await self.sender.notifier.call(
                _progress_message(bc, chat_id), chat_id=chat_id, priority=Priority.NORMAL
            )
        except (TelegramAPIError, NotifierError, TimeoutError, *TRANSPORT_ERRORS) as e:
            log.warning("broadcast %s: progress message not sent: %s", bc.id, type(e).__name__)
            return bc
        if msg is None:
            return bc
        ref = {"chat_id": chat_id, "message_id": int(msg.message_id)}
        async with self.db.tx() as conn:
            await conn.execute(sa.update(broadcasts).where(broadcasts.c.id == bc.id).values(progress_msg=ref))
        return replace(bc, progress_msg=ref)

    async def pause(self, bid: int, actor: Actor) -> Broadcast:
        return await self._change(bid, actor, ("running",), "paused", "broadcast.pause")

    async def stop(self, bid: int, actor: Actor) -> Broadcast:
        return await self._change(
            bid, actor, ("running", "paused"), "canceled", "broadcast.stop", finished_at=sa.func.now()
        )

    async def resume(self, bid: int, actor: Actor) -> Broadcast:
        return await self._change(bid, actor, ("paused",), "running", "broadcast.resume", job=True)

    async def _change(
        self,
        bid: int,
        actor: Actor,
        from_: Sequence[str],
        to: str,
        action: str,
        *,
        job: bool = False,
        **values: Any,
    ) -> Broadcast:
        async with self.db.tx() as conn:
            bc = await self.repo.transition(conn, bid, from_, to, **values)
            if bc is None:
                raise BroadcastError("Статус рассылки уже изменился")
            if job:
                await enqueue(conn, JOB_RUN, {"broadcast_id": bid}, dedup_key=run_dedup(bid))
            await self._audit(conn, actor, action, bid)
        log.info("broadcast %s: %s by user %s", bid, to, actor.user_id)
        if not self.sender.interrupt(bid):
            await self.sender.refresh(bc)  # no live run here: update the progress message ourselves
        return bc


def _progress_message(bc: Broadcast, chat_id: int) -> SendMessage:
    return SendMessage(
        chat_id=chat_id,
        text=progress_text(bc),
        parse_mode="HTML",
        reply_markup=controls(bc),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )
