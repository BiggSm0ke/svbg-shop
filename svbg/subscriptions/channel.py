"""Required channel: membership cache, ``chat_member`` updates, disable on leave / restore on return (06 M2).

* Membership is cached in ``channel_members``: kept current by ``ChatMemberUpdated`` (the bot must be an admin
  of the channel and ``chat_member`` must be in ``allowed_updates``) and refreshed by ``getChatMember`` when a
  cached answer is missing or stale. A cache hit costs one SQL and no HTTP.
* An older fact never overwrites a newer one (``seen_at``): updates may arrive out of order.
* On a real transition member → not member the leave policy ``CHANNEL_LEAVE_ACTION`` applies:
  ``off`` — nothing, ``trial`` (default) — the user's trial subscription is disabled with
  ``disabled_reason='channel_left'``, ``all`` — every live subscription is. On return the subscriptions
  disabled for this reason are enabled again (the writer enables only those still disabled *for this
  reason*, so a ban, an admin's disable or a freeze are never lifted by joining the channel).
* All panel changes go through the writer's jobs, in the transaction that records the fact.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from svbg.core.clock import now
from svbg.core.tables import users
from svbg.remnawave.writer import K_DISABLE, K_ENABLE, enqueue_action
from svbg.subscriptions import hooks, journal
from svbg.subscriptions.lifecycle import LIVE_STATES
from svbg.subscriptions.tables import channel_members, subscriptions

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.types import ChatMemberUpdated
    from sqlalchemy.ext.asyncio import AsyncConnection

    from svbg.db.engine import Database

__all__ = [
    "LEAVE_ACTIONS",
    "ChannelService",
    "MemberLookup",
    "Membership",
    "bot_lookup",
    "membership_of",
]

log = logging.getLogger("svbg.subscriptions.channel")

LEAVE_ACTIONS: Final = ("off", "trial", "all")
DEFAULT_LEAVE_ACTION: Final = "trial"
REASON: Final = "channel_left"
_MEMBER_STATUSES: Final = frozenset({"creator", "administrator", "member"})
#: Disable reasons that joining the channel must never lift or overwrite.
_FOREIGN_REASONS: Final = ("BOT_BAN", "admin", "ip_guard", "hold", "closed")
#: ``ChatMemberUpdated.date`` has a resolution of one second, a ``getChatMember`` answer is stamped with the
#: exact moment: within that second the update (a real transition) is the newer fact.
_UPDATE_RESOLUTION: Final = timedelta(seconds=1)


@dataclass(frozen=True, slots=True)
class Membership:
    status: str
    is_member: bool


def membership_of(status: Any, is_member: bool | None = None) -> Membership:
    """Bot API ``ChatMember`` → membership (``restricted`` counts only with ``is_member=true``)."""
    text = str(getattr(status, "value", status) or "").lower()
    if text in _MEMBER_STATUSES:
        return Membership(text, True)
    if text == "restricted":
        return Membership(text, bool(is_member))
    return Membership(text or "left", False)


MemberLookup = Callable[[int, int], Awaitable[Membership]]


def bot_lookup(bot: Bot) -> MemberLookup:
    """``getChatMember`` through an aiogram bot."""

    async def lookup(chat_id: int, telegram_id: int) -> Membership:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=telegram_id)
        return membership_of(member.status, getattr(member, "is_member", None))

    return lookup


class ChannelService:
    """See module docstring. ``config`` returns the current settings snapshot (a mapping)."""

    def __init__(
        self,
        db: Database,
        *,
        config: Callable[[], Mapping[str, Any]],
        lookup: MemberLookup | None = None,
        member_ttl: timedelta = timedelta(hours=6),
        non_member_ttl: timedelta = timedelta(minutes=1),
        lookup_timeout: float = 3.0,
    ) -> None:
        self._db = db
        self._config = config
        self._lookup = lookup
        self._member_ttl = member_ttl
        self._non_member_ttl = non_member_ttl
        self._timeout = lookup_timeout

    def set_lookup(self, lookup: MemberLookup | None) -> None:
        """Swap the Telegram lookup (bot token hot-swap)."""
        self._lookup = lookup

    def required_chat(self) -> int | None:
        value = self._config().get("REQUIRED_CHANNEL_ID")
        return int(value) if value else None

    def leave_action(self) -> str:
        value = str(self._config().get("CHANNEL_LEAVE_ACTION") or DEFAULT_LEAVE_ACTION)
        return value if value in LEAVE_ACTIONS else DEFAULT_LEAVE_ACTION

    # ------------------------------------------------------------------------------------------ reading

    async def is_member(self, telegram_id: int, *, fresh: bool = False) -> bool | None:
        """``True``/``False``, or ``None`` when it cannot be known now (Telegram unreachable, no cache).

        No required channel → ``True``. ``fresh`` (the «Я подписался — проверить» button) asks Telegram even
        when the cached answer is fresh; the cache is still the fallback when Telegram cannot be reached.
        """
        chat = self.required_chat()
        if chat is None:
            return True
        async with self._db.read() as conn:
            cached = (
                (
                    await conn.execute(
                        sa.select(channel_members.c.is_member, channel_members.c.seen_at).where(
                            channel_members.c.chat_id == chat,
                            channel_members.c.telegram_id == telegram_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
        if cached is not None and not fresh:
            ttl = self._member_ttl if cached["is_member"] else self._non_member_ttl
            if now() - cached["seen_at"] <= ttl:
                return bool(cached["is_member"])
        if self._lookup is None:
            return bool(cached["is_member"]) if cached is not None else None
        try:
            async with asyncio.timeout(self._timeout):
                member = await self._lookup(chat, telegram_id)
        except Exception as err:  # noqa: BLE001 - Telegram down / bot not in the channel: use what we know
            log.warning("getChatMember(%s) failed: %s", chat, type(err).__name__)
            return bool(cached["is_member"]) if cached is not None else None
        await self.record(chat, telegram_id, member, seen_at=now())
        return member.is_member

    # ------------------------------------------------------------------------------------------ updates

    async def on_update(self, update: ChatMemberUpdated) -> bool:
        """aiogram ``chat_member`` handler body. Returns whether a subscription change was queued."""
        new = update.new_chat_member
        old = update.old_chat_member
        return await self.record(
            update.chat.id,
            new.user.id,
            membership_of(new.status, getattr(new, "is_member", None)),
            seen_at=update.date,
            previous=membership_of(old.status, getattr(old, "is_member", None)),
            resolution=_UPDATE_RESOLUTION,
        )

    async def record(
        self,
        chat_id: int,
        telegram_id: int,
        member: Membership,
        *,
        seen_at: datetime,
        previous: Membership | None = None,
        resolution: timedelta = timedelta(0),
    ) -> bool:
        """Store a membership fact; apply the leave/return policy on a real transition.

        ``previous`` (from ``ChatMemberUpdated.old_chat_member``) is used when nothing is cached yet.
        ``resolution`` is the precision of ``seen_at``: a stored fact is newer only when it is later than
        the whole interval (an update dated 12:00:00 happened somewhen in [12:00:00, 12:00:01)).
        """
        if chat_id != self.required_chat():
            return False  # other chats (the admin group…) are not cached
        async with self._db.tx() as conn:
            prior = (
                (
                    await conn.execute(
                        sa.select(channel_members.c.is_member, channel_members.c.seen_at)
                        .where(
                            channel_members.c.chat_id == chat_id, channel_members.c.telegram_id == telegram_id
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if prior is not None:
                known: datetime = prior["seen_at"]
                if (known >= seen_at + resolution) if resolution else (known > seen_at):
                    return False  # a newer fact is already known
                seen_at = max(seen_at, known)  # the stamp never goes back (cache TTL, ordering)
            stmt = pg_insert(channel_members).values(
                chat_id=chat_id,
                telegram_id=telegram_id,
                status=member.status,
                is_member=member.is_member,
                seen_at=seen_at,
            )
            await conn.execute(
                stmt.on_conflict_do_update(
                    index_elements=["chat_id", "telegram_id"],
                    set_={
                        "status": stmt.excluded.status,
                        "is_member": stmt.excluded.is_member,
                        "seen_at": stmt.excluded.seen_at,
                    },
                )
            )
            was = (
                bool(prior["is_member"]) if prior is not None else (previous.is_member if previous else None)
            )
            if was is True and not member.is_member:
                return await self._on_leave(conn, telegram_id)
            if was is False and member.is_member:
                return await self._on_return(conn, telegram_id)
        return False

    async def _subs_of(self, conn: AsyncConnection, telegram_id: int) -> list[Mapping[str, Any]]:
        return list(
            (
                await conn.execute(
                    sa.select(
                        subscriptions.c.id,
                        subscriptions.c.user_id,
                        subscriptions.c.is_trial,
                        subscriptions.c.hold_kind,
                        subscriptions.c.disabled_reason,
                    )
                    .select_from(subscriptions.join(users, users.c.id == subscriptions.c.user_id))
                    .where(users.c.telegram_id == telegram_id, subscriptions.c.link_state.in_(LIVE_STATES))
                    .order_by(subscriptions.c.id)
                    .with_for_update(of=subscriptions)
                )
            )
            .mappings()
            .all()
        )

    def _in_scope(self, row: Mapping[str, Any], action: str) -> bool:
        return action == "all" or (action == "trial" and bool(row["is_trial"]))

    async def _on_leave(self, conn: AsyncConnection, telegram_id: int) -> bool:
        action = self.leave_action()
        if action == "off":
            return False
        changed = False
        for row in await self._subs_of(conn, telegram_id):
            if not self._in_scope(row, action) or row["hold_kind"] is not None:
                continue  # a frozen one is disabled already; its unfreeze checks the channel
            if row["disabled_reason"] in _FOREIGN_REASONS or row["disabled_reason"] == REASON:
                continue
            sid = int(row["id"])
            # The intent is recorded now (the writer confirms it): a quick return finds it even while the
            # disable is still queued, and the projection does not take DISABLED for a manual panel edit.
            await conn.execute(
                sa.update(subscriptions)
                .where(subscriptions.c.id == sid)
                .values(desired_status="disabled", disabled_reason=REASON, updated_at=sa.func.now())
            )
            await enqueue_action(conn, sid, K_DISABLE, {"reason": REASON}, caused_by="channel")
            await self._log(conn, row, "channel_left", "subscription.channel_left", action)
            changed = True
        return changed

    async def _on_return(self, conn: AsyncConnection, telegram_id: int) -> bool:
        action = self.leave_action()
        changed = False
        for row in await self._subs_of(conn, telegram_id):
            if row["hold_kind"] is not None or row["disabled_reason"] != REASON:
                continue  # never lifts a ban, an admin's disable or a freeze
            # The writer enables only while ``disabled_reason`` is still ``channel_left`` (FIFO after the
            # leave's disable), so the row is left as is here.
            await enqueue_action(conn, int(row["id"]), K_ENABLE, {"only_reason": REASON}, caused_by="channel")
            await self._log(conn, row, "channel_returned", "subscription.channel_returned", action)
            changed = True
        return changed

    @staticmethod
    async def _log(conn: AsyncConnection, row: Mapping[str, Any], kind: str, event: str, action: str) -> None:
        sid = int(row["id"])
        details = {"is_trial": bool(row["is_trial"]), "policy": action}
        await journal.record(conn, sid, kind, source="bot", details=details)
        await hooks.emit(conn, event, {"subscription_id": sid, "user_id": row["user_id"], **details})

    # ---------------------------------------------------------------------------------- freeze helper

    async def keeps_disabled(self, conn: AsyncConnection, subscription_id: int) -> str | None:
        """``'channel_left'`` when the leave policy still applies to this subscription (for ``unfreeze``)."""
        chat = self.required_chat()
        action = self.leave_action()
        if chat is None or action == "off":
            return None
        row = (
            (
                await conn.execute(
                    sa.select(subscriptions.c.is_trial, channel_members.c.is_member)
                    .select_from(
                        subscriptions.join(users, users.c.id == subscriptions.c.user_id).join(
                            channel_members,
                            sa.and_(
                                channel_members.c.chat_id == chat,
                                channel_members.c.telegram_id == users.c.telegram_id,
                            ),
                        )
                    )
                    .where(subscriptions.c.id == subscription_id)
                )
            )
            .mappings()
            .first()
        )
        if row is None or row["is_member"] or not self._in_scope(row, action):
            return None
        return REASON
