"""Normalized copy, button list parsing, keyboards and the Bot API calls of a broadcast."""

from __future__ import annotations

from typing import Any

import pytest
from aiogram.methods import CopyMessage, SendAnimation, SendDocument, SendMessage, SendPhoto, SendVoice
from aiogram.types import (
    Animation,
    Chat,
    Document,
    LinkPreviewOptions,
    Message,
    MessageEntity,
    PhotoSize,
    Sticker,
    Voice,
)

from svbg.broadcasts.message import (
    MAX_BUTTONS,
    ComposeError,
    build_markup,
    copy_method,
    describe,
    has_custom_emoji,
    normalize,
    parse_buttons,
    send_method,
)
from tests.broadcasts.kit import DATE

EMOJI_ID = "5368324170671202286"


def msg(**kw: Any) -> Message:
    return Message(message_id=1, date=DATE, chat=Chat(id=10, type="private"), **kw)


def u16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def test_text_keeps_every_entity_and_preview() -> None:
    text = "🔥 Скидка 50%! Секрет внутри\nцитата"
    entities = [
        MessageEntity(type="custom_emoji", offset=0, length=2, custom_emoji_id=EMOJI_ID),
        MessageEntity(type="bold", offset=3, length=6),
        MessageEntity(type="spoiler", offset=u16("🔥 Скидка 50%! "), length=6),
        MessageEntity(type="expandable_blockquote", offset=u16("🔥 Скидка 50%! Секрет внутри\n"), length=6),
        MessageEntity(type="text_link", offset=3, length=6, url="https://example.com/sale"),
    ]
    content = normalize(
        msg(
            text=text,
            entities=entities,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    )
    assert content["type"] == "text" and content["text"] == text
    assert [e["type"] for e in content["entities"]] == [
        "custom_emoji",
        "bold",
        "spoiler",
        "expandable_blockquote",
        "text_link",
    ]
    assert content["entities"][0]["custom_emoji_id"] == EMOJI_ID
    assert content["preview"] == {"is_disabled": True}
    assert has_custom_emoji(content)
    method = send_method(content, 77, None, silent=True)
    assert isinstance(method, SendMessage)
    assert method.parse_mode is None and method.disable_notification is True
    assert method.entities is not None
    assert [e.model_dump(exclude_none=True) for e in method.entities] == content["entities"]
    assert method.link_preview_options is not None and method.link_preview_options.is_disabled


def test_photo_with_caption_spoiler_and_above() -> None:
    content = normalize(
        msg(
            photo=[
                PhotoSize(file_id="small", file_unique_id="a", width=1, height=1),
                PhotoSize(file_id="BIG", file_unique_id="b", width=9, height=9),
            ],
            caption="Подпись",
            caption_entities=[MessageEntity(type="italic", offset=0, length=7)],
            has_media_spoiler=True,
            show_caption_above_media=True,
        )
    )
    assert content == {
        "type": "photo",
        "text": "Подпись",
        "entities": [{"type": "italic", "offset": 0, "length": 7}],
        "file_id": "BIG",
        "above": True,
        "spoiler": True,
    }
    method = send_method(content, 5, None)
    assert isinstance(method, SendPhoto)
    assert method.photo == "BIG" and method.caption == "Подпись" and method.parse_mode is None
    assert method.has_spoiler and method.show_caption_above_media and method.disable_notification is None
    assert describe(content) == "🖼 фото · 7 симв."


def test_animation_is_not_mistaken_for_a_document_and_others() -> None:
    gif = normalize(
        msg(
            animation=Animation(file_id="GIF", file_unique_id="g", width=1, height=1, duration=1),
            document=Document(file_id="DOC", file_unique_id="d"),
        )
    )
    assert gif["type"] == "animation" and gif["file_id"] == "GIF"
    assert isinstance(send_method(gif, 1, None), SendAnimation)
    assert describe(gif) == "🎞 GIF без подписи"
    doc = normalize(msg(document=Document(file_id="DOC", file_unique_id="d"), caption="файл"))
    assert isinstance(send_method(doc, 1, None), SendDocument)
    voice = normalize(msg(voice=Voice(file_id="V", file_unique_id="v", duration=3)))
    assert isinstance(send_method(voice, 1, None), SendVoice)


def test_unsupported_and_too_long() -> None:
    sticker = Sticker(
        file_id="S", file_unique_id="s", type="regular", width=1, height=1, is_animated=False, is_video=False
    )
    with pytest.raises(ComposeError, match="нельзя разослать"):
        normalize(msg(sticker=sticker))
    with pytest.raises(ComposeError, match="1024"):
        normalize(
            msg(photo=[PhotoSize(file_id="p", file_unique_id="p", width=1, height=1)], caption="я" * 1025)
        )
    with pytest.raises(ComposeError):
        send_method({"type": "photo", "text": ""}, 1, None)  # no file


def test_copy_method() -> None:
    method = copy_method(10, 99, 555, None, silent=True)
    assert isinstance(method, CopyMessage)
    assert (method.from_chat_id, method.message_id, method.chat_id) == (10, 99, 555)
    assert method.disable_notification is True and method.caption is None


def test_parse_buttons_rows_styles_actions() -> None:
    text = (
        "🛒 Купить | screen:buy | зелёная\n"
        "Канал | t.me/svbg ;; Сайт | https://example.com\n"
        "\n"
        "Скопировать | copy:PROMO2026 ;; Промо | deeplink:pr_SALE | red"
    )
    buttons = parse_buttons(text)
    assert [(b["label"]["ru"], b["row"], b["sort"]) for b in buttons] == [
        ("🛒 Купить", 0, 0),
        ("Канал", 1, 0),
        ("Сайт", 1, 1),
        ("Скопировать", 2, 0),
        ("Промо", 2, 1),
    ]
    assert buttons[0]["action"] == {"type": "screen", "target": "buy"} and buttons[0]["style"] == "success"
    assert buttons[1]["action"] == {"type": "url", "url": "https://t.me/svbg"}
    assert buttons[3]["action"] == {"type": "copy", "text": "PROMO2026"}
    assert buttons[4]["action"] == {"type": "deeplink", "code": "pr_SALE"} and buttons[4]["style"] == "danger"
    markup = build_markup(buttons, "ru", bot_username="svbg_bot")
    assert markup is not None
    rows = markup.inline_keyboard
    assert [len(r) for r in rows] == [1, 2, 2]
    assert rows[0][0].callback_data == "v1:buy:o" and rows[0][0].style == "success"
    assert rows[2][1].url == "https://t.me/svbg_bot?start=pr_SALE"
    assert rows[2][0].copy_text is not None and rows[2][0].copy_text.text == "PROMO2026"
    no_user = build_markup(buttons, "ru", bot_username=None)  # deep link cannot be built yet
    assert no_user is not None and sum(len(r) for r in no_user.inline_keyboard) == 4
    assert build_markup([], "ru") is None


def test_premium_emoji_at_the_start_becomes_the_icon() -> None:
    text = "Без значка | screen:home\n⭐ Тарифы | screen:buy"
    offset = u16("Без значка | screen:home\n")
    entities = [{"type": "custom_emoji", "offset": offset, "length": 1, "custom_emoji_id": EMOJI_ID}]
    buttons = parse_buttons(text, entities)
    assert "icon_custom_emoji_id" not in buttons[0]
    assert buttons[1]["icon_custom_emoji_id"] == EMOJI_ID and buttons[1]["label"]["ru"] == "Тарифы"
    markup = build_markup(buttons, "en")
    assert markup is not None and markup.inline_keyboard[1][0].icon_custom_emoji_id == EMOJI_ID


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("", "ни одной кнопки"),
        ("Просто текст", "Текст | действие"),
        ("A | b | c | d", "Текст | действие"),
        ("Кнопка | ftp://x", "Неизвестное действие"),
        ("Кнопка | nothing", "Не понял действие"),
        ("Кнопка | https://x.y | фиолетовая", "Цвет кнопки"),
        (" | https://x.y", "нет текста"),
        ("Кнопка | screen:bad name", "Кнопка «Кнопка»"),
        (";; ".join(f"B{i} | https://x.y" for i in range(9)), "не больше 8"),
        ("\n".join(f"B{i} | https://x.y ;; C{i} | https://x.y" for i in range(13)), "рядов"),
    ],
)
def test_parse_buttons_errors(text: str, error: str) -> None:
    with pytest.raises(ComposeError, match=error):
        parse_buttons(text)


def test_too_many_buttons() -> None:
    rows = "\n".join(";; ".join(f"B{r}{i} | https://x.y" for i in range(3)) for r in range(9))
    assert MAX_BUTTONS < 27
    with pytest.raises(ComposeError, match="кнопок"):
        parse_buttons(rows)
