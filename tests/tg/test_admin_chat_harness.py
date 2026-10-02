"""Shared builders for the admin chat UI tests: the real stack over the fake Bot API and a real PostgreSQL.

aiogram ``Bot`` → :class:`FakeTelegram` (HTTP); :class:`Notifier`, :class:`AdminChatService`,
:class:`ScreenRouter` with :class:`BotTransport`, :class:`UserDirectory` (roles from ``users`` and
``OWNER_IDS``), :class:`ErrorHub` — nothing is mocked except Telegram itself.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import sqlalchemy as sa
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.types import CallbackQuery, Chat, Message, User

import svbg.services.tables  # noqa: F401 - registers admin_* tables before create_schema
from svbg.core.errors import ErrorHub, ErrorSink
from svbg.core.tables import users
from svbg.db.engine import Database
from svbg.db.schema import create_schema
from svbg.services.admin_chat import AdminChatService
from svbg.tg.notifier import Notifier
from svbg.tg.runner import BotHolder
from svbg.tg.ui.codec import CallbackCodec
from svbg.tg.ui.router import BotTransport, ScreenRouter, UiStateStore
from svbg.tg.user.directory import UserDirectory
from tests.fakes.telegram import Call, FakeTelegram

GROUP = -1005550001
OWNER = 1001
ADMIN = 2002
MEMBER = 3003  # a member of the admin group without any role in the bot
START = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)

_query_ids = itertools.count(1)


class MutableClock:
    def __init__(self, start: datetime = START) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


@dataclass
class ChatEnv:
    tg: FakeTelegram
    bot: Bot
    db: Database
    holder: BotHolder
    notifier: Notifier
    service: AdminChatService
    router: ScreenRouter
    directory: UserDirectory
    hub: ErrorHub
    clock: MutableClock
    settings_values: dict[str, Any] = field(default_factory=dict)

    async def close(self) -> None:
        await self.hub.stop(grace=2.0)
        await self.service.stop(grace=1.0)
        await self.notifier.close()
        await self.bot.session.close()
        await self.db.close()


async def build_env(
    pg_dsn: str,
    tg: FakeTelegram,
    *,
    chat: int | None = GROUP,
    sink: Callable[[AdminChatService], ErrorSink | None] | None = None,
    **service_kw: Any,
) -> ChatEnv:
    await create_schema(pg_dsn)
    db = Database(pg_dsn)
    await db.start()
    token = tg.add_bot()
    bot = Bot(token, session=AiohttpSession(api=TelegramAPIServer.from_base(tg.url)))
    tg.make_admin(GROUP, bot.id, can_manage_topics=True, can_pin_messages=True, can_delete_messages=True)
    holder = BotHolder(bot)
    notifier = Notifier(holder)
    values: dict[str, Any] = {"OWNER_IDS": [OWNER], "DEFAULT_LANGUAGE": "ru", "CURRENCY": "RUB"}
    directory = UserDirectory(db, SimpleNamespace(current=lambda: values))
    service_kw.setdefault("retry_base", 0.01)
    service = AdminChatService(db, notifier, holder, owners=directory.owner_ids, **service_kw)
    service.set_chat(chat)
    await service.start()
    clock = MutableClock()
    hub = ErrorHub(db, sink(service) if sink is not None else None, clock=clock)
    router = ScreenRouter(
        transport=BotTransport(holder),
        user_loader=directory.load,
        ui_state=UiStateStore(db),
        codec=CallbackCodec(db, key=b"k" * 32),
        hub=hub,
        answer_deadline=5.0,
    )
    async with db.tx() as conn:
        await conn.execute(sa.insert(users).values(telegram_id=ADMIN, role="admin", perms=[]))
    return ChatEnv(tg, bot, db, holder, notifier, service, router, directory, hub, clock, values)


def group_message(env: ChatEnv, message_id: int, chat_id: int = GROUP) -> Message:
    raw = env.tg.message(chat_id, message_id)
    if raw is not None:
        return Message.model_validate(raw)
    chat = Chat(id=chat_id, type="supergroup" if chat_id < 0 else "private")
    return Message(message_id=message_id, date=START, chat=chat, text="…")


async def click(
    env: ChatEnv, user_id: int, data: str, *, message_id: int, chat_id: int = GROUP
) -> str | None:
    """Press an inline button as ``user_id``; returns the toast (``None`` = answered without text)."""
    query = CallbackQuery(
        id=str(next(_query_ids)),
        from_user=User(id=user_id, is_bot=False, first_name=f"U{user_id}", language_code="ru"),
        chat_instance=str(chat_id),
        data=data,
        message=group_message(env, message_id, chat_id),
    )
    start = len(env.tg.calls)
    await env.router.dispatch_callback(query)
    answers = [c for c in env.tg.calls[start:] if c.method.lower() == "answercallbackquery"]
    assert answers, "the click was not answered"
    text = answers[-1].params.get("text")
    return str(text) if text else None


def sends(env: ChatEnv, chat_id: int) -> list[Call]:
    return [c for c in env.tg.calls_for("sendMessage") if c.params.get("chat_id") == chat_id and c.ok]


def buttons(call: Call) -> list[tuple[str, str]]:
    markup = call.params.get("reply_markup") or {}
    return [(b["text"], b.get("callback_data", "")) for row in markup.get("inline_keyboard", []) for b in row]


def button(call: Call, prefix: str) -> str:
    """``callback_data`` of the first button whose text starts with ``prefix``."""
    for text, data in buttons(call):
        if text.startswith(prefix):
            return data
    raise AssertionError(f"no button {prefix!r} in {buttons(call)}")
