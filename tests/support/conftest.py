"""Support tickets over the real stack: aiogram ``Bot`` → :class:`FakeTelegram` (HTTP), real
:class:`Notifier`, :class:`AdminChatService`, :class:`ScreenRouter`, :class:`UserDirectory` and PostgreSQL."""

from __future__ import annotations

import itertools
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.types import CallbackQuery, Message, User

from svbg.core.clock import now
from svbg.db.engine import Database
from svbg.db.schema import create_schema
from svbg.services.admin_chat import AdminChatService
from svbg.support.service import TicketService
from svbg.support.ui import SupportUi
from svbg.tg.notifier import Limits, Notifier
from svbg.tg.runner import BotHolder
from svbg.tg.ui.codec import CallbackCodec
from svbg.tg.ui.router import BotTransport, ScreenRouter, UiStateStore
from svbg.tg.user.directory import UserDirectory
from tests.fakes.telegram import Call, FakeTelegram

GROUP = -1005550001
OWNER = 1001
ADMIN = 2002  # admin without extra rights
SUPPORT = 2003
MEMBER = 3003  # a member of the group without any role in the bot
USER = 5005

_ids = itertools.count(1)


@dataclass
class Kit:
    tg: FakeTelegram
    bot: Bot
    db: Database
    notifier: Notifier
    admin_chat: AdminChatService
    router: ScreenRouter
    service: TicketService
    ui: SupportUi
    cfg: dict[str, Any] = field(default_factory=dict)
    ids: dict[int, int] = field(default_factory=dict)  # telegram id → users.id

    async def add(
        self, tg_id: int, role: str = "user", *, name: str | None = None, perms: tuple[str, ...] = ()
    ) -> int:
        async with self.db.tx() as conn:
            uid = await conn.scalar(
                sa.text(
                    "insert into users (telegram_id, role, perms, first_name) "
                    "values (:t, :r, cast(:p as jsonb), :n) returning id"
                ),
                {"t": tg_id, "r": role, "p": json.dumps(list(perms)), "n": name},
            )
        self.ids[tg_id] = int(uid)
        return int(uid)

    async def sub(self, tg_id: int, plan: str = "Стандарт", days: int = 10) -> None:
        async with self.db.tx() as conn:
            await conn.execute(
                sa.text(
                    "insert into subscriptions (user_id, link_state, paid_until, plan_snapshot) "
                    "values (:u, 'pending', :p, cast(:s as jsonb))"
                ),
                {
                    "u": self.ids[tg_id],
                    "p": now() + timedelta(days=days),
                    "s": json.dumps({"name": {"ru": plan}}),
                },
            )

    async def sql(self, query: str, **params: Any) -> list[Any]:
        async with self.db.read() as conn:
            return list((await conn.execute(sa.text(query), params)).mappings().all())

    def user_text(self, tg_id: int, text: str) -> Message:
        return Message.model_validate(self.tg.push_message(tg_id, text)["message"])

    def user_photo(self, tg_id: int, file_id: str) -> Message:
        return Message.model_validate(self.tg.push_photo(tg_id, file_id)["message"])

    def staff_text(self, tg_id: int, thread: int, text: str, *, reply_to: int | None = None) -> Message:
        raw = self.tg.push_message(tg_id, text, chat_id=GROUP)["message"]
        raw.update(message_thread_id=thread, is_topic_message=True)
        if reply_to is not None:
            raw["reply_to_message"] = dict(self.tg.message(GROUP, reply_to) or {})
        return Message.model_validate(raw)

    def query(self, tg_id: int, data: str) -> CallbackQuery:
        return CallbackQuery(
            id=str(next(_ids)),
            from_user=User(id=tg_id, is_bot=False, first_name=f"U{tg_id}", language_code="ru"),
            chat_instance=str(GROUP),
            data=data,
        )

    def calls(self, method: str, **match: Any) -> list[Call]:
        return [
            c
            for c in self.tg.calls_for(method)
            if c.ok and all(c.params.get(k) == v for k, v in match.items())
        ]


@pytest.fixture
async def kit(pg_dsn: str) -> AsyncIterator[Kit]:
    await create_schema(pg_dsn)
    db = Database(pg_dsn)
    await db.start()
    async with FakeTelegram() as tg:
        bot = Bot(tg.add_bot(), session=AiohttpSession(api=TelegramAPIServer.from_base(tg.url)))
        tg.make_admin(GROUP, bot.id, can_manage_topics=True)
        holder = BotHolder(bot)
        notifier = Notifier(holder, limits=Limits(private_window=0.01, group_window=0.5))
        cfg: dict[str, Any] = {
            "OWNER_IDS": [OWNER],
            "DEFAULT_LANGUAGE": "ru",
            "CURRENCY": "RUB",
            "TIMEZONE": "Europe/Moscow",
            "SUPPORT_MODE": "tickets",
            "SUPPORT_URL": "https://t.me/help",
            "SUPPORT_CHAT_ID": None,
        }
        directory = UserDirectory(db, SimpleNamespace(current=lambda: cfg))
        admin_chat = AdminChatService(db, notifier, holder, owners=directory.owner_ids, retry_base=0.01)
        admin_chat.set_chat(GROUP)
        router = ScreenRouter(
            transport=BotTransport(holder),
            user_loader=directory.load,
            ui_state=UiStateStore(db),
            codec=CallbackCodec(db, key=b"k" * 32),
            answer_deadline=5.0,
        )

        async def owner_ids() -> frozenset[int]:
            return frozenset(directory.configured_owner_ids())

        service = TicketService(db, notifier, config=lambda: cfg, owner_ids=owner_ids, admin_chat=admin_chat)
        ui = SupportUi(service, router)
        ui.install()
        kit = Kit(tg, bot, db, notifier, admin_chat, router, service, ui, cfg)
        try:
            yield kit
        finally:
            await admin_chat.stop(grace=0.5)
            await notifier.close()
            await bot.session.close()
            await db.close()
