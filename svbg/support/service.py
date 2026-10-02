"""Support tickets over forum topics (07 §2.4.6, mode ``tickets`` / ``both``).

* A user presses «💬 Поддержка» → «Написать» (:meth:`TicketService.arm`) and simply writes: text, photo,
  document… Every message is copied (``copyMessage``) into **the user's own topic**
  «🎫 Имя · тариф · до 12.10» in the admin group (or ``SUPPORT_CHAT_ID``); the first message of each episode
  in the topic is the user card with «Закрыть», «Карточка», «Продлить», «Блок».
* Any message of staff in that topic (a reply or not) is copied to the user. Who may answer or close: Support
  and above (:data:`~svbg.services.roles.Act.TICKETS`, re-read from the database on every message / press); a
  group member without a role is ignored (messages) or gets «Нет прав» (buttons).
* «Закрыть» renames the topic «✅ …» and closes it; the user's next message reopens it as a new episode
  (a new ``tickets`` row in the same topic). A topic deleted by a human is recreated.
* ``ticket_messages`` maps message ids of both sides: a reply on one side quotes the matching message on the
  other. ``first_reply_at`` (time to first answer) goes to the daily report.

Group requests go through :meth:`AdminChatService.call` (the group budget of the admin chat), private ones
through the notifier. Nothing here runs inside a database transaction while talking to Telegram.
"""

from __future__ import annotations

import asyncio
import html
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, tzinfo
from typing import TYPE_CHECKING, Any, Final, TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import sqlalchemy as sa
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.methods import (
    CloseForumTopic,
    CopyMessage,
    CreateForumTopic,
    EditForumTopic,
    ReopenForumTopic,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
    ReplyParameters,
)

from svbg.core.tables import users
from svbg.services import roles
from svbg.services.roles import Act, Actor
from svbg.support.tables import ticket_messages, tickets
from svbg.tg.admin.users.queries import Card, load_card
from svbg.tg.admin.users.screens import card_button
from svbg.tg.notifier import Priority

if TYPE_CHECKING:
    from svbg.db.engine import Database
    from svbg.services.admin_chat import AdminChatService
    from svbg.tg.notifier import Notifier

__all__ = ["CALLBACK", "MODES", "TEXTS", "TicketService", "t"]

log = logging.getLogger("svbg.support")

T = TypeVar("T")

MODES: Final = ("link", "tickets", "both")
CALLBACK: Final = "tk"  # group buttons ``tk:<c|e|b>:<users.id>`` (close / extend / block)
ARM_TTL: Final = 3600.0  # «Написать в поддержку» keeps the next messages for the support this long
_TITLE_MAX: Final = 128
_COPYABLE: Final = (
    "text", "photo", "document", "video", "voice", "audio", "animation", "sticker", "video_note", "location",
    "contact",
)  # fmt: skip
_THREAD_GONE: Final = ("thread not found", "topic_deleted", "topic not found", "topic_id_invalid")

TEXTS: Final[Mapping[str, Mapping[str, str]]] = {
    "ru": {
        "prompt": "💬 Напишите вопрос прямо сюда — одним или несколькими сообщениями, "
        "можно с фото или файлом. Ответ придёт в этот чат.",
        "accepted": "✅ Передали в поддержку. Ответ придёт сюда.",
        "closed": "✅ Обращение закрыто. Если вопрос остался — просто напишите сюда.",
        "unavailable": "Поддержка сейчас недоступна. Попробуйте позже.",
        "link": "💬 Открыть чат поддержки",
        "write": "✍️ Написать в поддержку",
        "title": "💬 Поддержка",
    },
    "en": {
        "prompt": "💬 Write your question right here — one or several messages, photos and files are fine. "
        "The answer will come to this chat.",
        "accepted": "✅ Sent to support. The answer will come here.",
        "closed": "✅ The request is closed. If you still have a question, just write here.",
        "unavailable": "Support is unavailable right now. Please try again later.",
        "link": "💬 Open the support chat",
        "write": "✍️ Write to support",
        "title": "💬 Support",
    },
}
_STAFF: Final[Mapping[str, str]] = {
    "close": "✅ Закрыть",
    "extend": "➕ Продлить",
    "block": "⛔ Блок",
    "closed_ok": "Обращение закрыто",
    "already": "Уже закрыто",
    "not_found": "Обращение не найдено",
    "undelivered": "⚠️ Не доставлено: пользователь заблокировал бота.",
    "failed": "⚠️ Не доставлено: {error}",
    "reopened": "🔓 Пользователь снова написал",
}


class TopicError(Exception):
    """The group refused a topic (the bot was removed: Telegram answered 403)."""


def t(lang: str | None, key: str) -> str:
    return (TEXTS.get(lang or "ru") or TEXTS["ru"])[key]


@dataclass(frozen=True, slots=True)
class Ticket:
    id: int
    user_id: int
    chat_id: int | None
    thread_id: int | None
    status: str


@dataclass(frozen=True, slots=True)
class _Sender:
    user_id: int
    lang: str


def _ticket(row: Any) -> Ticket:
    return Ticket(int(row.id), int(row.user_id), row.chat_id, row.thread_id, str(row.status))


def _thread_gone(exc: TelegramBadRequest) -> bool:
    desc = (exc.message or "").lower()
    return any(s in desc for s in _THREAD_GONE)


def copyable(message: Message) -> bool:
    """A user / staff message that ``copyMessage`` can carry (not a service message)."""
    return any(getattr(message, attr, None) for attr in _COPYABLE)


class TicketService:
    """See the module docstring. ``config()`` = the settings snapshot (``SUPPORT_MODE``, ``SUPPORT_CHAT_ID``,
    ``ADMIN_CHAT_ID``, ``SUPPORT_URL``, ``TIMEZONE``, ``CURRENCY``)."""

    def __init__(
        self,
        db: Database,
        notifier: Notifier,
        *,
        config: Callable[[], Mapping[str, Any]],
        owner_ids: Callable[[], Awaitable[frozenset[int]]],
        admin_chat: AdminChatService | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._db = db
        self._notifier = notifier
        self._config = config
        self._owner_ids = owner_ids
        self._admin_chat = admin_chat
        self._clock = clock
        self._armed: dict[int, float] = {}  # telegram id → monotonic deadline
        self._locks: dict[int, asyncio.Lock] = {}

    # ------------------------------------------------------------------ settings

    def _get(self, key: str, default: Any = None) -> Any:
        try:
            value = self._config().get(key)
        except (KeyError, RuntimeError):
            return default
        return default if value is None else value

    def mode(self) -> str:
        mode = str(self._get("SUPPORT_MODE", "link"))
        return mode if mode in MODES else "link"

    def tickets_on(self) -> bool:
        return self.mode() != "link"

    def link(self) -> str | None:
        url = self._get("SUPPORT_URL")
        return url if isinstance(url, str) and url.startswith(("https://", "http://", "tg://")) else None

    def chat_id(self) -> int | None:
        """``SUPPORT_CHAT_ID``, else the admin chat; ``None`` = nowhere to open topics."""
        value = self._get("SUPPORT_CHAT_ID")
        if value is None:
            value = self._admin_chat.chat_id if self._admin_chat is not None else self._get("ADMIN_CHAT_ID")
        return int(value) if value is not None else None

    def _tz(self) -> tzinfo:
        try:
            return ZoneInfo(str(self._get("TIMEZONE", "Europe/Moscow")))
        except (ZoneInfoNotFoundError, ValueError):
            return UTC

    # ------------------------------------------------------------------ arming

    def arm(self, telegram_id: int) -> None:
        """«Написать в поддержку»: the next free messages of this user go to the support."""
        at = self._clock()
        if len(self._armed) > 10_000:
            self._armed = {k: v for k, v in self._armed.items() if v > at}
        self._armed[telegram_id] = at + ARM_TTL

    def armed(self, telegram_id: int) -> bool:
        deadline = self._armed.get(telegram_id)
        return deadline is not None and deadline > self._clock()

    def _lock(self, user_id: int) -> asyncio.Lock:
        lock = self._locks.get(user_id)
        if lock is None:
            if len(self._locks) > 10_000:
                self._locks = {k: v for k, v in self._locks.items() if v.locked()}
            lock = self._locks[user_id] = asyncio.Lock()
        return lock

    # ------------------------------------------------------------------ Telegram

    async def _group(self, method: TelegramMethod[T], chat_id: int) -> T | None:
        if self._admin_chat is not None:
            return await self._admin_chat.call(method, chat_id, priority=Priority.HIGH)
        return await self._notifier.call(method, chat_id=chat_id, priority=Priority.HIGH)

    async def _dm(self, method: TelegramMethod[T], chat_id: int) -> T | None:
        return await self._notifier.call(method, chat_id=chat_id, priority=Priority.HIGH)

    async def _say(self, chat_id: int, text: str) -> None:
        try:
            await self._dm(SendMessage(chat_id=chat_id, text=text), chat_id)
        except TelegramAPIError as exc:
            log.warning("support: message to the user failed: %s", type(exc).__name__)

    async def _note(self, ticket: Ticket, text: str) -> None:
        if ticket.chat_id is None or ticket.thread_id is None:
            return
        try:
            await self._group(
                SendMessage(chat_id=ticket.chat_id, message_thread_id=ticket.thread_id, text=text),
                ticket.chat_id,
            )
        except TelegramAPIError as exc:
            log.warning("support: note into the topic failed: %s", type(exc).__name__)

    # ------------------------------------------------------------------ card and title

    async def _card(self, user_id: int) -> Card | None:
        async with self._db.read() as conn:
            return await load_card(conn, user_id, currency=str(self._get("CURRENCY", "RUB")))

    @staticmethod
    def _name(card: Card | None) -> str:
        if card is None:
            return "?"
        name = (card.first_name or "").strip()
        if name:
            return name[:48]
        return f"@{card.username}" if card.username else f"id {card.telegram_id}"

    def title(self, card: Card | None, *, closed: bool = False) -> str:
        """«🎫 Имя · тариф · до 12.10» (closed: «✅ …»)."""
        parts = [self._name(card)]
        if card is not None and card.sub_id is not None:
            parts.append((card.plan_name or ("триал" if card.is_trial else "подписка"))[:32])
            if card.paid_until is not None:
                parts.append(f"до {card.paid_until.astimezone(self._tz()):%d.%m}")
        else:
            parts.append("без подписки")
        return (("✅ " if closed else "🎫 ") + " · ".join(parts))[:_TITLE_MAX]

    def _card_text(self, ticket_id: int, card: Card | None, *, reopened: bool) -> str:
        e = html.escape
        head = _STAFF["reopened"] if reopened else f"🎫 <b>Обращение #{ticket_id}</b>"
        lines = [head]
        if card is not None:
            handle = f" @{e(card.username)}" if card.username else ""
            lines.append(f"👤 {e(self._name(card))}{handle} · <code>{card.telegram_id}</code>")
            if card.sub_id is not None:
                until = (
                    f" · до {card.paid_until.astimezone(self._tz()):%d.%m.%Y}"
                    if card.paid_until is not None
                    else ""
                )
                trial = " (триал)" if card.is_trial else ""
                lines.append(f"📦 {e(card.plan_name or '—')}{trial}{until}")
            else:
                lines.append("📦 Подписки нет")
            if card.banned_at is not None:
                lines.append("⛔ Заблокирован")
        return "\n".join(lines)

    @staticmethod
    def keyboard(user_id: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text=_STAFF["close"], callback_data=f"{CALLBACK}:c:{user_id}"),
                    card_button(user_id),
                ],
                [
                    InlineKeyboardButton(text=_STAFF["extend"], callback_data=f"{CALLBACK}:e:{user_id}"),
                    InlineKeyboardButton(text=_STAFF["block"], callback_data=f"{CALLBACK}:b:{user_id}"),
                ],
            ]
        )

    async def _post_card(self, ticket: Ticket, card: Card | None, *, reopened: bool) -> None:
        assert ticket.chat_id is not None and ticket.thread_id is not None
        await self._group(
            SendMessage(
                chat_id=ticket.chat_id,
                message_thread_id=ticket.thread_id,
                text=self._card_text(ticket.id, card, reopened=reopened),
                parse_mode="HTML",
                reply_markup=self.keyboard(ticket.user_id),
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            ),
            ticket.chat_id,
        )

    async def _create_topic(self, chat_id: int, card: Card | None) -> int:
        topic = await self._group(CreateForumTopic(chat_id=chat_id, name=self.title(card)), chat_id)
        if topic is None:
            raise TopicError("the bot was removed from the group")
        return int(topic.message_thread_id)

    # ------------------------------------------------------------------ user → topic

    async def sender(self, telegram_id: int) -> _Sender | None:
        """The user if their free messages go to the support now: armed, or has had a ticket (1 SQL)."""
        has = sa.exists().where(tickets.c.user_id == users.c.id)
        stmt = sa.select(users.c.id, users.c.language, has.label("has")).where(
            users.c.telegram_id == telegram_id, users.c.banned_at.is_(None)
        )
        async with self._db.read() as conn:
            row = (await conn.execute(stmt)).first()
        if row is None or not (row.has or self.armed(telegram_id)):
            return None
        return _Sender(int(row.id), str(row.language or "ru"))

    async def user_message(self, message: Message) -> bool:
        """Copy a private message of the user into their topic; ``False`` = not for the support."""
        tg_user = message.from_user
        if tg_user is None or not self.tickets_on() or not copyable(message):
            return False
        who = await self.sender(tg_user.id)
        if who is None:
            return False
        async with self._lock(who.user_id):
            ticket, started = await self._ensure(who.user_id)
            if ticket is None:
                await self._say(message.chat.id, t(who.lang, "unavailable"))
                return True
            reply_to = await self._mapped(ticket.user_id, message.reply_to_message, side="user")
            group_msg = await self._copy_in(ticket, message, reply_to)
        self._armed.pop(tg_user.id, None)
        if group_msg is not None:
            async with self._db.tx() as conn:
                await conn.execute(
                    sa.insert(ticket_messages).values(
                        ticket_id=ticket.id, dir="in", user_msg_id=message.message_id, group_msg_id=group_msg
                    )
                )
        if started:
            await self._say(message.chat.id, t(who.lang, "accepted"))
        return True

    async def _latest(self, user_id: int) -> Ticket | None:
        async with self._db.read() as conn:
            row = (
                await conn.execute(
                    sa.select(tickets)
                    .where(tickets.c.user_id == user_id)
                    .order_by(tickets.c.id.desc())
                    .limit(1)
                )
            ).first()
        return None if row is None else _ticket(row)

    async def _ensure(self, user_id: int) -> tuple[Ticket | None, bool]:
        """The open ticket with a live topic (created / reopened as needed) and whether an episode started."""
        chat = self.chat_id()
        if chat is None:
            return None, False
        latest = await self._latest(user_id)
        if latest is not None and latest.status == "open" and latest.chat_id == chat and latest.thread_id:
            return latest, False
        card = await self._card(user_id)
        reopened = False
        try:
            if latest is not None and latest.chat_id == chat and latest.thread_id is not None:
                thread = latest.thread_id
                reopened = await self._reopen(chat, thread, card)
                if not reopened:
                    thread = await self._create_topic(chat, card)
            else:
                thread = await self._create_topic(chat, card)
        except (TelegramAPIError, TopicError) as exc:
            log.warning("support: cannot open a topic in %s: %s", chat, exc)
            return None, False
        async with self._db.tx() as conn:
            if latest is not None and latest.status == "open":
                row = (
                    await conn.execute(
                        sa.update(tickets)
                        .where(tickets.c.id == latest.id)
                        .values(chat_id=chat, thread_id=thread, opened_at=sa.func.now())
                        .returning(tickets)
                    )
                ).one()
            else:
                row = (
                    await conn.execute(
                        sa.insert(tickets)
                        .values(user_id=user_id, chat_id=chat, thread_id=thread, status="open")
                        .returning(tickets)
                    )
                ).one()
        ticket = _ticket(row)
        try:
            await self._post_card(ticket, card, reopened=reopened)
        except TelegramAPIError as exc:
            log.warning("support: ticket card not posted: %s", type(exc).__name__)
        return ticket, True

    async def _reopen(self, chat: int, thread: int, card: Card | None) -> bool:
        """Reopen + rename a closed topic; ``False`` when it was deleted (a new one is needed)."""
        for method in (
            ReopenForumTopic(chat_id=chat, message_thread_id=thread),
            EditForumTopic(chat_id=chat, message_thread_id=thread, name=self.title(card)),
        ):
            try:
                await self._group(method, chat)
            except TelegramBadRequest as exc:
                if _thread_gone(exc):
                    return False
                if "not_modified" not in (exc.message or "").lower():
                    log.info("support: %s failed: %s", type(method).__name__, exc.message)
        return True

    async def _copy_in(self, ticket: Ticket, message: Message, reply_to: int | None) -> int | None:
        for attempt in (1, 2):
            assert ticket.chat_id is not None and ticket.thread_id is not None
            method = CopyMessage(
                chat_id=ticket.chat_id,
                message_thread_id=ticket.thread_id,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
                reply_parameters=(
                    ReplyParameters(message_id=reply_to, allow_sending_without_reply=True)
                    if reply_to
                    else None
                ),
            )
            try:
                result = await self._group(method, ticket.chat_id)
            except TelegramBadRequest as exc:
                if attempt == 1 and _thread_gone(exc):
                    ticket = await self._recreate(ticket)
                    reply_to = None
                    continue
                log.warning("support: copy into the topic failed: %s", exc.message)
                return None
            return None if result is None else int(result.message_id)
        return None

    async def _recreate(self, ticket: Ticket) -> Ticket:
        """The topic was deleted by a human: a new one, the ticket moved there, a fresh card."""
        assert ticket.chat_id is not None
        card = await self._card(ticket.user_id)
        thread = await self._create_topic(ticket.chat_id, card)
        async with self._db.tx() as conn:
            await conn.execute(
                sa.update(tickets)
                .where(tickets.c.user_id == ticket.user_id, tickets.c.thread_id == ticket.thread_id)
                .values(thread_id=thread)
            )
        moved = Ticket(ticket.id, ticket.user_id, ticket.chat_id, thread, ticket.status)
        try:
            await self._post_card(moved, card, reopened=False)
        except TelegramAPIError as exc:
            log.warning("support: ticket card not posted: %s", type(exc).__name__)
        return moved

    async def _mapped(self, user_id: int, replied: Message | None, *, side: str) -> int | None:
        """The message on the other side that ``replied`` (a message on ``side``) corresponds to."""
        if replied is None:
            return None
        mine, other = (
            (ticket_messages.c.user_msg_id, ticket_messages.c.group_msg_id)
            if side == "user"
            else (ticket_messages.c.group_msg_id, ticket_messages.c.user_msg_id)
        )
        stmt = (
            sa.select(other)
            .select_from(ticket_messages.join(tickets, tickets.c.id == ticket_messages.c.ticket_id))
            .where(tickets.c.user_id == user_id, mine == replied.message_id)
            .order_by(ticket_messages.c.id.desc())
            .limit(1)
        )
        async with self._db.read() as conn:
            value = await conn.scalar(stmt)
        return None if value is None else int(value)

    # ------------------------------------------------------------------ topic → user

    async def actor(self, telegram_id: int) -> Actor | None:
        owners = await self._owner_ids()
        async with self._db.read() as conn:
            return await roles.load_actor(conn, telegram_id=telegram_id, owner_ids=owners)

    async def staff_message(self, message: Message) -> bool:
        """Copy a staff message from a ticket topic to the user; ``False`` = not a ticket topic."""
        thread = message.message_thread_id
        sender = message.from_user
        if thread is None or sender is None or sender.is_bot or message.chat.id != self.chat_id():
            return False
        if not copyable(message) or (message.text or "").startswith("/"):
            return False
        stmt = (
            sa.select(tickets, users.c.telegram_id.label("tg"))
            .select_from(tickets.join(users, users.c.id == tickets.c.user_id))
            .where(tickets.c.chat_id == message.chat.id, tickets.c.thread_id == thread)
            .order_by(tickets.c.id.desc())
            .limit(1)
        )
        async with self._db.read() as conn:
            row = (await conn.execute(stmt)).first()
        if row is None:
            return False
        ticket = _ticket(row)
        if not roles.authorize(await self.actor(sender.id), Act.TICKETS):
            log.info("support: %s is not staff; message in ticket %s ignored", sender.id, ticket.id)
            return True
        if row.tg is None:
            return True
        reply_to = await self._mapped(ticket.user_id, message.reply_to_message, side="group")
        method = CopyMessage(
            chat_id=int(row.tg),
            from_chat_id=message.chat.id,
            message_id=message.message_id,
            reply_parameters=ReplyParameters(message_id=reply_to, allow_sending_without_reply=True)
            if reply_to
            else None,
        )
        try:
            result = await self._dm(method, int(row.tg))
        except TelegramAPIError as exc:
            await self._note(ticket, _STAFF["failed"].format(error=type(exc).__name__))
            return True
        if result is None:
            await self._note(ticket, _STAFF["undelivered"])
            return True
        async with self._db.tx() as conn:
            await conn.execute(
                sa.insert(ticket_messages).values(
                    ticket_id=ticket.id,
                    dir="out",
                    user_msg_id=int(result.message_id),
                    group_msg_id=message.message_id,
                )
            )
            await conn.execute(
                sa.update(tickets)
                .where(
                    tickets.c.id == ticket.id, tickets.c.first_reply_at.is_(None), tickets.c.status == "open"
                )
                .values(first_reply_at=sa.func.now())
            )
        return True

    # ------------------------------------------------------------------ close

    async def close(self, user_id: int, telegram_id: int) -> str:
        """«Закрыть» (fresh role check): the toast for the presser."""
        owners = await self._owner_ids()
        async with self._db.tx() as conn:
            actor = await roles.load_actor(conn, telegram_id=telegram_id, owner_ids=owners)
            if not roles.authorize(actor, Act.TICKETS):
                return roles.DENIED
            row = (
                await conn.execute(
                    sa.update(tickets)
                    .where(tickets.c.user_id == user_id, tickets.c.status == "open")
                    .values(
                        status="closed", closed_at=sa.func.now(), closed_by=actor.user_id if actor else None
                    )
                    .returning(
                        tickets,
                        sa.select(users.c.telegram_id)
                        .where(users.c.id == user_id)
                        .scalar_subquery()
                        .label("tg"),
                    )
                )
            ).first()
            if row is None:
                exists = await conn.scalar(sa.select(sa.exists().where(tickets.c.user_id == user_id)))
                return _STAFF["already"] if exists else _STAFF["not_found"]
            await roles.audit(conn, actor, "tickets.close", target=f"ticket:{row.id}")
        ticket = _ticket(row)
        tg = row.tg
        if ticket.chat_id is not None and ticket.thread_id is not None:
            card = await self._card(user_id)
            for method in (
                EditForumTopic(
                    chat_id=ticket.chat_id,
                    message_thread_id=ticket.thread_id,
                    name=self.title(card, closed=True),
                ),
                CloseForumTopic(chat_id=ticket.chat_id, message_thread_id=ticket.thread_id),
            ):
                try:
                    await self._group(method, ticket.chat_id)
                except TelegramAPIError as exc:
                    log.info("support: %s failed: %s", type(method).__name__, exc)
        if tg is not None:
            lang = await self._lang(user_id)
            await self._say(int(tg), t(lang, "closed"))
        return _STAFF["closed_ok"]

    async def _lang(self, user_id: int) -> str:
        async with self._db.read() as conn:
            value = await conn.scalar(sa.select(users.c.language).where(users.c.id == user_id))
        return str(value or "ru")
