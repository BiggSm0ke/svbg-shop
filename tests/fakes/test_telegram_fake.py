"""The fake Bot API must produce JSON that aiogram parses into real types."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramConflictError,
    TelegramForbiddenError,
    TelegramRetryAfter,
    TelegramServerError,
    TelegramUnauthorizedError,
)
from aiogram.types import (
    ChatMemberLeft,
    ChatMemberMember,
    ChatMemberOwner,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    MessageEntity,
)

from tests.fakes.telegram import FakeTelegram


@pytest.fixture
async def tg() -> AsyncIterator[FakeTelegram]:
    async with FakeTelegram() as fake:
        yield fake


@pytest.fixture
async def bot(tg: FakeTelegram) -> AsyncIterator[Bot]:
    b = Bot(tg.add_bot(username="fake_bot"), session=AiohttpSession(api=TelegramAPIServer.from_base(tg.url)))
    try:
        yield b
    finally:
        await b.session.close()


def kb(style: str = "primary") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="A", callback_data="v1:a", style=style, icon_custom_emoji_id="123")]
        ]
    )


async def test_get_me_and_unknown_token(tg: FakeTelegram, bot: Bot) -> None:
    me = await bot.get_me()
    assert me.username == "fake_bot" and me.is_bot
    other = Bot("1:" + "x" * 35, session=AiohttpSession(api=TelegramAPIServer.from_base(tg.url)))
    try:
        with pytest.raises(TelegramUnauthorizedError):
            await other.get_me()
    finally:
        await other.session.close()
    assert tg.calls_for("getMe")[-1].status == 401


async def test_message_lifecycle(tg: FakeTelegram, bot: Bot) -> None:
    ents = [MessageEntity(type="italic", offset=0, length=2)]
    msg = await bot.send_message(5, "hi there", entities=ents, reply_markup=kb())
    assert msg.reply_markup is not None
    button = msg.reply_markup.inline_keyboard[0][0]
    assert (button.style, button.icon_custom_emoji_id) == ("primary", "123")
    assert msg.entities is not None and msg.entities[0].type == "italic"

    edited = await bot.edit_message_text(
        text="changed", chat_id=5, message_id=msg.message_id, reply_markup=kb()
    )
    assert not isinstance(edited, bool) and edited.text == "changed"
    with pytest.raises(TelegramBadRequest, match="not modified"):
        await bot.edit_message_text(text="changed", chat_id=5, message_id=msg.message_id, reply_markup=kb())
    marked = await bot.edit_message_reply_markup(
        chat_id=5, message_id=msg.message_id, reply_markup=kb("danger")
    )
    assert not isinstance(marked, bool) and marked.reply_markup is not None
    assert marked.reply_markup.inline_keyboard[0][0].style == "danger"

    media = await bot.edit_message_media(
        media=InputMediaPhoto(media="AgACphoto", caption="cap"), chat_id=5, message_id=msg.message_id
    )
    assert not isinstance(media, bool) and media.photo and media.caption == "cap" and media.text is None

    copied = await bot.copy_message(chat_id=6, from_chat_id=5, message_id=msg.message_id)
    assert copied.message_id == 1
    assert await bot.delete_message(5, msg.message_id) is True
    with pytest.raises(TelegramBadRequest, match="not found"):
        await bot.delete_message(5, msg.message_id)
    with pytest.raises(TelegramBadRequest, match="not found"):
        await bot.edit_message_text(text="x", chat_id=5, message_id=999)


async def test_photo_chat_member_topic_callback(tg: FakeTelegram, bot: Bot) -> None:
    photo = await bot.send_photo(
        -1001, photo="https://example.invalid/p.jpg", caption="c", message_thread_id=3
    )
    assert photo.photo and photo.message_thread_id == 3
    chat = await bot.get_chat(-1001)
    assert chat.type == "supergroup"
    tg.chat_members[(-1001, 7)] = "left"
    tg.chat_members[(-1001, 8)] = "creator"
    assert isinstance(await bot.get_chat_member(-1001, 7), ChatMemberLeft)
    assert isinstance(await bot.get_chat_member(-1001, 8), ChatMemberOwner)
    assert isinstance(await bot.get_chat_member(-1001, 9), ChatMemberMember)
    topic = await bot.create_forum_topic(-1001, "Ошибки")
    assert topic.name == "Ошибки" and topic.message_thread_id >= 100
    with pytest.raises(TelegramBadRequest):
        await bot.create_forum_topic(5, "private")
    assert await bot.answer_callback_query("1", text="ok") is True


async def test_updates_and_offsets(tg: FakeTelegram, bot: Bot) -> None:
    tg.push_message(1, "/start")
    sent = await bot.send_message(1, "menu")
    tg.push_callback(1, "v1:home:buy", sent.message_id)
    updates = await bot.get_updates(timeout=0)
    assert [u.update_id for u in updates] == [1, 2]
    assert updates[0].message is not None and updates[0].message.text == "/start"
    assert updates[0].message.entities is not None
    cq = updates[1].callback_query
    assert cq is not None and cq.data == "v1:home:buy" and cq.message is not None
    again = await bot.get_updates(offset=updates[-1].update_id + 1, timeout=0)
    assert again == []
    assert tg.pending_updates() == []


async def test_long_poll_wakes_on_push_and_conflicts(tg: FakeTelegram, bot: Bot) -> None:
    poll = asyncio.ensure_future(bot.get_updates(timeout=5))
    await asyncio.sleep(0.1)
    tg.push_message(1, "late")
    updates = await asyncio.wait_for(poll, 2)
    assert updates[0].message is not None and updates[0].message.text == "late"

    first = asyncio.ensure_future(bot.get_updates(offset=updates[0].update_id + 1, timeout=5))
    await asyncio.sleep(0.1)
    second = asyncio.ensure_future(bot.get_updates(timeout=0))
    with pytest.raises(TelegramConflictError):
        await asyncio.wait_for(first, 2)
    assert await second == []
    assert tg.conflicts == 1


async def test_webhook_blocks_get_updates(tg: FakeTelegram, bot: Bot) -> None:
    assert await bot.set_webhook("http://127.0.0.1:1/hook", secret_token="abc_DEF-123")
    info = await bot.get_webhook_info()
    assert info.url == "http://127.0.0.1:1/hook"
    with pytest.raises(TelegramConflictError):
        await bot.get_updates(timeout=0)
    await bot.delete_webhook()
    assert (await bot.get_webhook_info()).url == ""


async def test_faults(tg: FakeTelegram, bot: Bot) -> None:
    tg.fail_next("429", method="sendMessage", retry_after=7)
    with pytest.raises(TelegramRetryAfter) as err:
        await bot.send_message(1, "x")
    assert err.value.retry_after == 7
    tg.fail_next("403", chat_id=2)
    await bot.send_message(1, "fine")  # other chat is not affected
    with pytest.raises(TelegramForbiddenError):
        await bot.send_message(2, "x")
    tg.fail_next("500")
    with pytest.raises(TelegramServerError):
        await bot.get_me()
    tg.blocked_chats.add(3)
    with pytest.raises(TelegramForbiddenError, match="blocked"):
        await bot.send_message(3, "x")


async def test_latency_and_wait_for(tg: FakeTelegram, bot: Bot) -> None:
    tg.method_latency["sendMessage"] = 0.2
    task = asyncio.ensure_future(bot.send_message(9, "slow"))
    call = await tg.wait_for("sendMessage", lambda c: c.params["chat_id"] == 9, timeout=2)
    assert call.params["text"] == "slow"
    await task
    with pytest.raises(TimeoutError):
        await tg.wait_for("sendMessage", lambda c: c.params["chat_id"] == 10, timeout=0.1)
