"""Background edits of a screen with a picture (the default banner is on every screen by default): billing's
«✅ Оплачено» keeps the picture and edits its caption in place; a text too long for a caption goes anew."""

from __future__ import annotations

from typing import Any

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageCaption, EditMessageText

from svbg.billing.ports import Button, Notice, UiRef
from svbg.core.clock import now
from svbg.tg.user.messenger import UserMessenger

NO_TEXT = "Bad Request: there is no text in the message to edit"


def _photo_chat(calls: list[Any]) -> Any:
    async def call(method: Any, chat_id: int) -> Any:
        calls.append(method)
        if isinstance(method, EditMessageText):  # the message is a photo: it has a caption, not a text
            raise TelegramBadRequest(method=method, message=NO_TEXT)
        return True

    return call


async def test_paid_notice_edits_the_caption_of_a_picture_screen() -> None:
    calls: list[Any] = []
    m = UserMessenger(_photo_chat(calls))
    notice = Notice("billing.paid", "✅ Оплачено!", ((Button("Меню", action="menu"),),))
    assert await m.edit(UiRef(5, 77, now()), notice)
    assert [type(c) for c in calls] == [EditMessageText, EditMessageCaption]
    caption = calls[-1]
    assert (caption.chat_id, caption.message_id, caption.caption) == (5, 77, "✅ Оплачено!")
    assert caption.reply_markup is not None and caption.reply_markup.inline_keyboard[0][0].text == "Меню"


async def test_text_over_the_caption_limit_is_sent_as_a_new_message() -> None:
    calls: list[Any] = []
    m = UserMessenger(_photo_chat(calls))
    assert not await m.edit(UiRef(5, 77, now()), Notice("billing.paid", "я" * 1025))
    assert [type(c) for c in calls] == [EditMessageText]  # billing then sends a new message
