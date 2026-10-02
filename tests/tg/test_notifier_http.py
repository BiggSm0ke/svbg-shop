"""Notifier against the fake Bot API over real HTTP (aiogram session, real clock)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText, SendPhoto
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, MessageEntity

from svbg.tg.notifier import Limits, Notifier, Priority
from tests.fakes.telegram import FakeTelegram


@pytest.fixture
async def tg() -> AsyncIterator[FakeTelegram]:
    async with FakeTelegram() as fake:
        yield fake


@pytest.fixture
async def bot(tg: FakeTelegram) -> AsyncIterator[Bot]:
    token = tg.add_bot()
    b = Bot(
        token,
        session=AiohttpSession(api=TelegramAPIServer.from_base(tg.url)),
        default=DefaultBotProperties(parse_mode="HTML"),
    )
    try:
        yield b
    finally:
        await b.session.close()


async def test_send_echoes_entities_and_button_style(tg: FakeTelegram, bot: Bot) -> None:
    n = Notifier(SimpleNamespace(current=bot))
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Купить",
                    callback_data="v1:home:buy",
                    style="success",
                    icon_custom_emoji_id="5368324170671202286",
                ),
                InlineKeyboardButton(text="Отмена", callback_data="v1:home:cancel", style="danger"),
            ]
        ]
    )
    entities = [MessageEntity(type="bold", offset=0, length=6)]
    msg = await n.send(1001, "Привет <мир>", entities=entities, reply_markup=markup, priority=Priority.HIGH)
    assert isinstance(msg, Message)
    assert msg.text == "Привет <мир>"
    assert msg.entities is not None
    assert [e.model_dump(exclude_none=True) for e in msg.entities] == [
        {"type": "bold", "offset": 0, "length": 6}
    ]
    assert msg.reply_markup is not None
    first, second = msg.reply_markup.inline_keyboard[0]
    assert first.style == "success"
    assert first.icon_custom_emoji_id == "5368324170671202286"
    assert second.style == "danger"
    call = tg.calls_for("sendMessage")[0]
    assert "parse_mode" not in call.params  # entities never mix with the bot's default parse_mode
    assert call.params["link_preview_options"] == {"is_disabled": True}


async def test_plain_text_overrides_default_parse_mode(tg: FakeTelegram, bot: Bot) -> None:
    n = Notifier(SimpleNamespace(current=bot))
    await n.send(1001, "a <b> c")
    assert "parse_mode" not in tg.calls_for("sendMessage")[0].params


async def test_thread_id_and_generic_call(tg: FakeTelegram, bot: Bot) -> None:
    n = Notifier(SimpleNamespace(current=bot))
    msg = await n.send(-100500, "в тему", thread_id=77)
    assert msg is not None and msg.message_thread_id == 77
    edited = await n.call(
        EditMessageText(chat_id=-100500, message_id=msg.message_id, text="изменено"), chat_id=-100500
    )
    assert isinstance(edited, Message) and edited.text == "изменено"
    photo = await n.call(SendPhoto(chat_id=-100500, photo="AgACAgIAAxk", caption="cap"), chat_id=-100500)
    assert isinstance(photo, Message) and photo.photo and photo.caption == "cap"


async def test_429_retry_after_over_http(tg: FakeTelegram, bot: Bot) -> None:
    n = Notifier(SimpleNamespace(current=bot))
    tg.fail_next("429", method="sendMessage", retry_after=1)
    started = time.monotonic()
    msg = await n.send(1001, "x")
    assert msg is not None
    assert time.monotonic() - started >= 0.95
    calls = tg.calls_for("sendMessage")
    assert [c.status for c in calls] == [429, 200]


async def test_403_over_http(tg: FakeTelegram, bot: Bot) -> None:
    blocked: list[int | str] = []
    n = Notifier(SimpleNamespace(current=bot), on_blocked=blocked.append)
    tg.blocked_chats.add(555)
    assert await n.send(555, "x") is None
    assert blocked == [555]


async def test_5xx_retried(tg: FakeTelegram, bot: Bot) -> None:
    n = Notifier(SimpleNamespace(current=bot), limits=Limits(transient_backoff=0.05))
    tg.fail_next("502", method="sendMessage", count=2)
    assert await n.send(1001, "x") is not None
    assert [c.status for c in tg.calls_for("sendMessage")] == [502, 502, 200]


async def test_400_raises(tg: FakeTelegram, bot: Bot) -> None:
    n = Notifier(SimpleNamespace(current=bot))
    tg.fail_next("400", method="sendMessage", description="Bad Request: chat not found")
    with pytest.raises(TelegramBadRequest):
        await n.send(1001, "x")


async def test_group_burst_never_hits_telegram_flood_limit(tg: FakeTelegram, bot: Bot) -> None:
    # Telegram-side limit is checked by the fake; our window is slightly stricter, as in production.
    tg.enforce_limits = True
    tg.group_limit, tg.group_window = 5, 0.9
    n = Notifier(SimpleNamespace(current=bot), limits=Limits(group_limit=5, group_window=1.0))
    results = await asyncio.gather(*(n.send(-42, f"m{i}") for i in range(12)))
    assert all(isinstance(r, Message) for r in results)
    assert tg.flood_errors == 0
    assert [c.params["text"] for c in tg.calls_for("sendMessage")] == [f"m{i}" for i in range(12)]
