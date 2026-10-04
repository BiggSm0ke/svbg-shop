"""Over real HTTP (aiogram → fake Bot API): Premium emoji, spoilers and quotes arrive intact."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer

from svbg.broadcasts.repo import Broadcast
from svbg.broadcasts.sender import BroadcastSender, Outcome
from svbg.tg.notifier import Limits, Notifier
from tests.broadcasts.kit import DATE
from tests.fakes.telegram import FakeTelegram

ADMIN = 42
USER = 777
ENTITIES = [
    {"type": "custom_emoji", "offset": 0, "length": 2, "custom_emoji_id": "5368324170671202286"},
    {"type": "bold", "offset": 3, "length": 6},
    {"type": "spoiler", "offset": 10, "length": 6},
    {"type": "expandable_blockquote", "offset": 17, "length": 4},
]
TEXT = "🔥 Скидка секрет цит"


@pytest.fixture
async def tg() -> AsyncIterator[FakeTelegram]:
    async with FakeTelegram() as fake:
        yield fake


@pytest.fixture
async def sender(tg: FakeTelegram) -> AsyncIterator[BroadcastSender]:
    token = tg.add_bot(username="svbg_bot")
    bot = Bot(
        token,
        session=AiohttpSession(api=TelegramAPIServer.from_base(tg.url)),
        default=DefaultBotProperties(parse_mode="HTML"),  # must never apply to broadcast content
    )
    notifier = Notifier(SimpleNamespace(current=bot), limits=Limits(private_limit=100))
    try:
        yield BroadcastSender(cast("Any", None), notifier, bot_username=lambda: "svbg_bot")
    finally:
        await notifier.close()
        await bot.session.close()


def broadcast(**over: Any) -> Broadcast:
    row: dict[str, Any] = {
        "id": 1,
        "status": "running",
        "created_by": None,
        "source_chat_id": ADMIN,
        "source_msg_id": None,
        "content": {"type": "text", "text": TEXT, "entities": ENTITIES},
        "buttons": [
            {"label": {"ru": "Купить"}, "action": {"type": "deeplink", "code": "pr_SALE"}, "style": "success"}
        ],
        "segment": {"preset": "all"},
        "options": {},
        "cursor": 0,
        "skip": [],
        "total": 1,
        "sent": 0,
        "failed": 0,
        "blocked": 0,
        "progress_msg": None,
        "last_error": None,
        "created_at": DATE,
        "started_at": None,
        "finished_at": None,
    }
    row.update(over)
    return Broadcast.from_row(row)


async def test_copy_keeps_premium_emoji_and_spoiler(tg: FakeTelegram, sender: BroadcastSender) -> None:
    src = tg.push_message(ADMIN, TEXT, entities=ENTITIES)["message"]
    bc = broadcast(source_msg_id=src["message_id"])
    result = await sender.deliver(bc, USER, "ru")
    assert result.outcome is Outcome.SENT and result.message_id is not None
    got = tg.message(USER, result.message_id)
    assert got is not None and got["text"] == TEXT and got["entities"] == ENTITIES
    button = got["reply_markup"]["inline_keyboard"][0][0]
    assert button["url"] == "https://t.me/svbg_bot?start=pr_SALE" and button["style"] == "success"


async def test_send_from_the_copy_when_the_original_is_gone(
    tg: FakeTelegram, sender: BroadcastSender
) -> None:
    bc = broadcast(source_msg_id=99_999)  # deleted by the admin
    result = await sender.deliver(bc, USER, "ru")
    assert result.outcome is Outcome.SENT
    got = tg.message(USER, result.message_id or 0)
    assert got is not None and got["text"] == TEXT and got["entities"] == ENTITIES
    call = tg.calls_for("sendMessage")[-1]
    assert "parse_mode" not in call.params  # entities only, the bot's default HTML is not applied


async def test_photo_caption_entities_survive(tg: FakeTelegram, sender: BroadcastSender) -> None:
    content = {"type": "photo", "text": TEXT, "entities": ENTITIES, "file_id": "PHOTO", "spoiler": True}
    result = await sender.deliver(broadcast(content=content, source_chat_id=None), USER, "ru")
    assert result.outcome is Outcome.SENT
    got = tg.message(USER, result.message_id or 0)
    assert got is not None and got["caption"] == TEXT and got["caption_entities"] == ENTITIES
    assert tg.calls_for("sendPhoto")[-1].params["has_spoiler"] is True


async def test_blocked_user_is_reported_as_blocked(tg: FakeTelegram, sender: BroadcastSender) -> None:
    tg.blocked_chats.add(USER)
    src = tg.push_message(ADMIN, TEXT, entities=ENTITIES)["message"]
    result = await sender.deliver(broadcast(source_msg_id=src["message_id"]), USER, "ru")
    assert result.outcome is Outcome.BLOCKED
