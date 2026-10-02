"""End to end over the fake Bot API: aiogram ``Bot`` + ``Dispatcher`` + real PostgreSQL.

Owner link → welcome → «⚙️ Настройки» → card → edit through a form; ``/set`` with a secret is deleted from
the chat; a non-owner's ``/set`` is refused.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.types import User

from svbg.tg.runner import BotHolder
from svbg.tg.setup.owner import OwnerSetup, create_owner_link
from svbg.tg.ui.context import UserCtx
from svbg.tg.ui.router import BotTransport
from tests.dbkit import CountingDatabase
from tests.fakes.telegram import Call, FakeTelegram
from tests.tg.admin.settings_harness import SEnv, build_senv

BOT_SECRET = "987654321:AAF" + "q" * 32


class DbUsers:
    """User loader over the ``users`` table (creates plain users on first contact)."""

    def __init__(self, db: CountingDatabase) -> None:
        self.db = db

    async def __call__(self, tg_user: User) -> UserCtx | None:
        rows = await self.db.raw(
            "insert into users (telegram_id, first_name) values ($1, $2) "
            "on conflict (telegram_id) do update set last_seen_at = now() "
            "returning id, role, perms::text as perms",
            tg_user.id,
            tg_user.first_name,
        )
        row = rows[0]
        return UserCtx(
            row["id"], telegram_id=tg_user.id, role=row["role"], perms=frozenset(json.loads(row["perms"]))
        )


@dataclass
class World:
    fake: FakeTelegram
    bot: Bot
    dp: Dispatcher
    env: SEnv
    owner_setup: OwnerSetup

    async def send(self, user_id: int, text: str) -> dict[str, Any]:
        update = self.fake.push_message(user_id, text)
        await self.dp.feed_raw_update(self.bot, update)
        return update["message"]

    async def press(self, user_id: int, data: str, message_id: int) -> None:
        update = self.fake.push_callback(user_id, data, message_id)
        await self.dp.feed_raw_update(self.bot, update)

    def last(self, method: str, user_id: int) -> Call:
        calls = [c for c in self.fake.calls_for(method) if c.params.get("chat_id") == user_id and c.ok]
        assert calls, f"no {method} to {user_id}"
        return calls[-1]

    def screen(self, user_id: int) -> tuple[str, int, dict[str, str]]:
        """Text, message id and buttons (label → data) of the user's latest bot message."""
        state_calls = [
            c
            for c in self.fake.calls
            if c.ok
            and c.params.get("chat_id") == user_id
            and c.method.lower() in ("sendmessage", "editmessagetext")
        ]
        call = state_calls[-1]
        msg = call.result
        markup = call.params.get("reply_markup") or {}
        buttons = {
            b["text"]: b.get("callback_data", "") for row in markup.get("inline_keyboard", []) for b in row
        }
        return call.params["text"], msg["message_id"], buttons


@pytest.fixture
async def world(db: CountingDatabase, tmp_path: Path) -> AsyncIterator[World]:
    async with FakeTelegram() as fake:
        token = fake.add_bot(username="svbg_bot")
        bot = Bot(token, session=AiohttpSession(api=TelegramAPIServer.from_base(fake.url)))
        try:
            holder = BotHolder(bot, await bot.me())
            env = await build_senv(
                db, tmp_path / ".env", transport=BotTransport(holder), user_loader=DbUsers(db)
            )
            owner_setup = OwnerSetup(db, env.router, settings=env.service)
            owner_setup.install()
            dp = Dispatcher()
            dp.include_router(owner_setup.aiogram_router())
            dp.include_router(env.screens.aiogram_router())
            dp.include_router(env.router.aiogram_router())
            yield World(fake, bot, dp, env, owner_setup)
            await env.screens.drain()
        finally:
            await bot.session.close()


def _find(buttons: dict[str, str], label: str) -> str:
    for text, data in buttons.items():
        if label in text:
            return data
    raise AssertionError(f"no {label!r} in {list(buttons)}")


async def test_owner_journey(world: World) -> None:
    owner = 5551
    link = await create_owner_link(world.env.db, bot_username="svbg_bot")
    await world.send(owner, f"/start {link.payload}")
    text, msg_id, buttons = world.screen(owner)
    assert "владелец" in text
    assert world.env.service.current()["OWNER_IDS"] == [owner]

    await world.press(owner, _find(buttons, "Настройки"), msg_id)
    text, msg_id, buttons = world.screen(owner)
    assert "Настройки" in text
    assert world.fake.calls_for("answerCallbackQuery")

    await world.press(owner, _find(buttons, "Продажи"), msg_id)
    text, msg_id, buttons = world.screen(owner)
    await world.press(owner, _find(buttons, "Дней пробного периода"), msg_id)
    text, msg_id, buttons = world.screen(owner)
    assert "<code>TRIAL_DAYS</code>" in text
    await world.press(owner, _find(buttons, "Изменить"), msg_id)
    await world.send(owner, "14")
    text, msg_id, buttons = world.screen(owner)
    assert "Применено" in text
    assert world.env.service.current()["TRIAL_DAYS"] == 14
    assert any("Отменить" in label for label in buttons)


async def test_set_secret_over_the_wire(world: World) -> None:
    owner = 5552
    link = await create_owner_link(world.env.db)
    await world.send(owner, f"/start {link.payload}")
    msg = await world.send(owner, f"/set BOT_TOKEN {BOT_SECRET}")
    deleted = [c.params["message_id"] for c in world.fake.calls_for("deleteMessage") if c.ok]
    assert msg["message_id"] in deleted
    assert world.fake.message(owner, msg["message_id"]) is None
    text, _, _ = world.screen(owner)
    assert "Применено" in text
    assert world.env.service.current()["BOT_TOKEN"] == BOT_SECRET
    for call in world.fake.calls:
        if call.method.lower() in ("sendmessage", "editmessagetext", "answercallbackquery"):
            assert BOT_SECRET not in json.dumps(call.params, ensure_ascii=False)


async def test_set_from_a_plain_user_is_not_handled(world: World) -> None:
    await world.send(7777, "/set TRIAL_DAYS 30")
    assert world.env.service.current()["TRIAL_DAYS"] == 3
    assert not world.fake.calls_for("sendMessage")
