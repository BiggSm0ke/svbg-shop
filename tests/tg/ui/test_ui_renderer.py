from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from aiogram.methods import (
    EditMessageCaption,
    EditMessageMedia,
    EditMessageText,
    SendAnimation,
    SendMessage,
    SendPhoto,
)
from aiogram.types import (
    Animation,
    Chat,
    Document,
    InlineKeyboardButton,
    InputMediaPhoto,
    Message,
    MessageEntity,
    PhotoSize,
)

from svbg.content.model import (
    Button,
    CopyAction,
    DeeplinkAction,
    ModuleAction,
    ScreenAction,
    ShareAction,
    SystemAction,
    UrlAction,
    WebAppAction,
)
from svbg.content.store import build_snapshot, compile_keyboard
from svbg.tg.ui.context import UserCtx
from svbg.tg.ui.renderer import (
    CAPTION_LIMIT,
    TEXT_LIMIT,
    MessageShape,
    Op,
    TextTooLongError,
    as_markup,
    build_edit,
    build_keyboard,
    build_send,
    check_length,
    content_view,
    fit_text,
    format_label,
    format_text,
    plan_transition,
    utf16_len,
)
from svbg.tg.ui.view import MediaRef, View

USER = UserCtx(1, telegram_id=10, days_left=5, balance_minor=12_345, sub_state="active")


def _btn(label: str, action: Any = None, **kw: Any) -> Button:
    return Button(label={"ru": label}, action=action or SystemAction("buy"), **kw)


def _texts(markup: Any) -> list[list[str]]:
    return [[b.text for b in row] for row in markup.inline_keyboard]


# ---------------------------------------------------------------- keyboards


def test_visibility_and_order() -> None:
    buttons = [
        _btn("B", row=0, sort=2),
        _btn("A", row=0, sort=1),
        _btn("Owner only", row=1, visible_if={"role": "owner"}),
        _btn("Expiring", row=2, visible_if={"all": [{"sub": "active"}, {"days_left": {"lte": 7}}]}),
        _btn("Off", row=3, enabled=False),
    ]
    assert _texts(build_keyboard(buttons, USER, "ru")) == [["A", "B"], ["Expiring"]]
    owner = UserCtx(2, role="owner", sub_state="none")
    assert _texts(build_keyboard(buttons, owner, "ru")) == [["A", "B"], ["Owner only"]]


def test_row_width_is_split_to_eight_and_total_capped() -> None:
    buttons = [_btn(f"b{i}", row=0, sort=i) for i in range(19)]
    kb = build_keyboard(buttons, USER, "ru")
    assert [len(r) for r in kb.inline_keyboard] == [8, 8, 3]
    many = [_btn(f"b{i}", row=i % 90, sort=i) for i in range(150)]
    kb = build_keyboard(many, USER, "ru")
    assert sum(len(r) for r in kb.inline_keyboard) == 100
    assert all(len(r) <= 8 for r in kb.inline_keyboard)


def test_style_icon_and_all_action_kinds() -> None:
    buttons = [
        _btn("Экран", ScreenAction("tariffs"), style="primary", icon_custom_emoji_id="5368324170671202286"),
        _btn("Купить", SystemAction("buy"), style="success", row=1),
        _btn("LTE", ModuleAction("lte", "status"), style="danger", row=2),
        _btn("Сайт", UrlAction("https://example.com"), row=3),
        _btn("App", WebAppAction("https://app.example.com"), row=4),
        _btn("Ссылка", DeeplinkAction("p_month"), row=5),
        _btn("Копировать", CopyAction("PROMO"), row=6),
        _btn("Поделиться", ShareAction("Лучший VPN & быстрый"), row=7),
    ]
    kb = build_keyboard(buttons, USER, "ru", bot_username="svbg_bot")
    flat = [b for row in kb.inline_keyboard for b in row]
    assert flat[0].callback_data == "v1:tariffs:o"
    assert flat[0].style == "primary" and flat[0].icon_custom_emoji_id == "5368324170671202286"
    assert flat[1].callback_data == "v1:sys:buy" and flat[1].style == "success"
    assert flat[2].callback_data == "v1:mod:lte.status" and flat[2].style == "danger"
    assert flat[3].url == "https://example.com"
    assert flat[4].web_app is not None and flat[4].web_app.url == "https://app.example.com"
    assert flat[5].url == "https://t.me/svbg_bot?start=p_month"
    assert flat[6].copy_text is not None and flat[6].copy_text.text == "PROMO"
    assert flat[7].url is not None and flat[7].url.startswith("https://t.me/share/url?url=")
    assert "%26" in flat[7].url and " " not in flat[7].url
    dumped = kb.model_dump(exclude_none=True)
    assert dumped["inline_keyboard"][0][0]["style"] == "primary"
    # without a bot username the deep link button cannot be built and is skipped
    kb2 = build_keyboard(buttons, USER, "ru")
    assert "Ссылка" not in [b.text for row in kb2.inline_keyboard for b in row]


def test_placeholders_are_safe() -> None:
    buttons = [
        _btn("Осталось {days_left} дн.", row=0),
        _btn("Баланс: {balance}", row=1),
        _btn("{unknown} {0.__class__} {days_left!r} {{days_left}}", row=2),
    ]
    kb = build_keyboard(buttons, USER, "ru")
    texts = _texts(kb)
    assert texts[0] == ["Осталось 5 дн."]
    assert "123,45" in texts[1][0]
    # no str.format: unknown names, attribute access and conversions stay literal
    assert texts[2][0] == "{unknown} {0.__class__} {days_left!r} {5}"
    assert format_label("{balance}{balance}", {"balance": "1"}) == "11"


def test_static_buttons_are_memoized_per_snapshot() -> None:
    template = compile_keyboard([_btn("Static"), _btn("Dyn {days_left}", row=1)], "ru")
    kb1 = build_keyboard(template, USER, "ru")
    kb2 = build_keyboard(template, UserCtx(2, days_left=1), "ru")
    assert kb1.inline_keyboard[0][0] is kb2.inline_keyboard[0][0]
    assert kb1.inline_keyboard[1][0].text == "Dyn 5" and kb2.inline_keyboard[1][0].text == "Dyn 1"


def test_broken_condition_fails_closed() -> None:
    template = compile_keyboard([_btn("x")], "ru")
    t = template.rows[0][0]

    def boom(_ctx: UserCtx) -> bool:
        raise TypeError("bad")

    broken = type(t)(t.button, t.label, t.needs_format, boom)
    from svbg.content.store import KeyboardTemplate

    assert build_keyboard(KeyboardTemplate(((broken,),)), USER, "ru").inline_keyboard == []


def test_extra_rows_and_markup_normalization() -> None:
    extra = [[InlineKeyboardButton(text=str(i), callback_data="v1:x:o") for i in range(10)]]
    kb = build_keyboard([], USER, "ru", extra)
    assert [len(r) for r in kb.inline_keyboard] == [8, 2]
    assert as_markup(None) is None
    assert as_markup([]) is None
    assert as_markup(build_keyboard([], USER, "ru")) is None
    assert as_markup(extra) is not None


# ---------------------------------------------------------------- texts


def test_utf16_lengths() -> None:
    assert utf16_len("abc") == 3
    assert utf16_len("👋") == 2
    assert utf16_len("привет") == 6
    check_length("x" * TEXT_LIMIT, caption=False)
    with pytest.raises(TextTooLongError):
        check_length("x" * (CAPTION_LIMIT + 1), caption=True)
    with pytest.raises(TextTooLongError):
        check_length("👋" * 2049, caption=False)  # 4098 UTF-16 units


def test_fit_text_cuts_on_code_points_and_clips_entities() -> None:
    text = "👋" * 3000  # 6000 UTF-16 units
    ents = [
        MessageEntity(type="bold", offset=0, length=10),
        MessageEntity(type="italic", offset=4090, length=100),
        MessageEntity(type="code", offset=5000, length=4),
    ]
    out, out_ents = fit_text(text, ents, TEXT_LIMIT)
    assert utf16_len(out) <= TEXT_LIMIT
    assert out.endswith("…")
    assert out[:-1] == "👋" * (len(out) - 1)  # no broken surrogate pairs
    assert out_ents is not None
    assert [e.type for e in out_ents] == ["bold", "italic"]
    assert out_ents[1].offset + out_ents[1].length <= utf16_len(out) - 1
    same, same_ents = fit_text("short", ents[:1], TEXT_LIMIT)
    assert same == "short" and same_ents == ents[:1]


def test_format_text_moves_entity_offsets() -> None:
    text = "Осталось {days_left} дн. 👋 Баланс {balance}"
    entities = [
        {"type": "bold", "offset": 0, "length": 8},  # "Осталось"
        {"type": "italic", "offset": 9, "length": 11},  # exactly "{days_left}"
        {"type": "underline", "offset": 25, "length": 2},  # "👋" after the placeholder
        {"type": "code", "offset": 10, "length": 3},  # inside the placeholder
    ]
    out, ents = format_text(text, entities, {"days_left": "12", "balance": "100 ₽"})
    assert out == "Осталось 12 дн. 👋 Баланс 100 ₽"

    def cut(e: dict[str, Any]) -> str:
        raw = out.encode("utf-16-le")
        return raw[e["offset"] * 2 : (e["offset"] + e["length"]) * 2].decode("utf-16-le")

    by_type = {e["type"]: e for e in ents}
    assert cut(by_type["bold"]) == "Осталось"
    assert cut(by_type["italic"]) == "12"
    assert cut(by_type["underline"]) == "👋"
    assert cut(by_type["code"]) == "12"  # snapped to the substituted value
    # unknown placeholders and no values leave text and entities untouched
    assert format_text("a {x}", [], {"days_left": "1"}) == ("a {x}", [])
    assert format_text("plain", [{"type": "bold", "offset": 0, "length": 5}], {})[0] == "plain"


# ---------------------------------------------------------------- transitions


TEXT = MessageShape("text")
PHOTO_A = MessageShape("photo", "m:1")
PHOTO_B = MessageShape("photo", "m:2")
GIF_A = MessageShape("animation", "m:1")
VIDEO = MessageShape("video", "m:3")
UNKNOWN_PHOTO = MessageShape("photo")


@pytest.mark.parametrize(
    ("prev", "new", "force", "op"),
    [
        (None, TEXT, False, Op.SEND_NEW),
        (None, PHOTO_A, False, Op.SEND_NEW),
        (TEXT, TEXT, False, Op.EDIT_TEXT),
        (TEXT, PHOTO_A, False, Op.EDIT_MEDIA),
        (PHOTO_A, TEXT, False, Op.SEND_NEW_DELETE_OLD),
        (PHOTO_A, PHOTO_A, False, Op.EDIT_CAPTION),
        (PHOTO_A, PHOTO_B, False, Op.EDIT_MEDIA),
        (PHOTO_A, GIF_A, False, Op.EDIT_MEDIA),
        (GIF_A, VIDEO, False, Op.EDIT_MEDIA),
        (UNKNOWN_PHOTO, PHOTO_A, False, Op.EDIT_MEDIA),
        (UNKNOWN_PHOTO, UNKNOWN_PHOTO, False, Op.EDIT_MEDIA),
        (TEXT, TEXT, True, Op.SEND_NEW_DELETE_OLD),
        (PHOTO_A, PHOTO_A, True, Op.SEND_NEW_DELETE_OLD),
    ],
)
def test_plan_transition(prev: MessageShape | None, new: MessageShape, force: bool, op: Op) -> None:
    assert plan_transition(prev, new, force_new=force) is op


def _msg(**kw: Any) -> Message:
    return Message(message_id=1, date=datetime(2026, 1, 1, tzinfo=UTC), chat=Chat(id=1, type="private"), **kw)


def test_shape_of_message_and_json() -> None:
    photo = [PhotoSize(file_id="f", file_unique_id="u", width=1, height=1)]
    assert MessageShape.of_message(_msg(text="hi")) == TEXT
    assert MessageShape.of_message(_msg(photo=photo, caption="c")) == MessageShape("photo")
    gif = Animation(file_id="g", file_unique_id="u", width=1, height=1, duration=1)
    doc = Document(file_id="d", file_unique_id="u")
    assert MessageShape.of_message(_msg(animation=gif, document=doc)) == MessageShape("animation")
    assert MessageShape.of_message(_msg(document=doc)) == MessageShape("document")
    assert MessageShape.of_message(_msg()) is None
    assert MessageShape.from_json(PHOTO_A.to_json()) == PHOTO_A
    assert MessageShape.from_json({"k": "sticker"}) is None
    assert MessageShape.from_json("x") is None


def test_build_send_and_edit_use_entities_never_default_parse_mode() -> None:
    ents = [MessageEntity(type="bold", offset=0, length=2)]
    view = View(text="Hi there", entities=ents)
    send = build_send(view, 5, None)
    assert isinstance(send, SendMessage)
    assert send.parse_mode is None and send.entities == ents
    assert send.link_preview_options is not None and send.link_preview_options.is_disabled
    edit = build_edit(Op.EDIT_TEXT, view, 5, 9, None)
    assert isinstance(edit, EditMessageText) and edit.parse_mode is None and edit.message_id == 9
    html = View(text="<b>x</b>", parse_mode="HTML")
    assert build_send(html, 5, None).parse_mode == "HTML"  # type: ignore[union-attr]
    with pytest.raises(ValueError, match="either"):
        View(text="x", entities=ents, parse_mode="HTML")


def test_media_methods_and_caption_limit() -> None:
    long_caption = "я" * 2000
    view = View(text=long_caption, media=MediaRef("photo", "FILEID", "m:1", 1))
    send = build_send(view, 5, None)
    assert isinstance(send, SendPhoto)
    assert send.caption is not None and utf16_len(send.caption) <= CAPTION_LIMIT
    assert send.parse_mode is None
    gif = build_send(View(text="x", media=MediaRef("animation", "G")), 5, None)
    assert isinstance(gif, SendAnimation)
    cap = build_edit(Op.EDIT_CAPTION, view, 5, 9, None)
    assert isinstance(cap, EditMessageCaption) and utf16_len(cap.caption or "") <= CAPTION_LIMIT
    med = build_edit(Op.EDIT_MEDIA, view, 5, 9, None)
    assert isinstance(med, EditMessageMedia)
    assert isinstance(med.media, InputMediaPhoto) and med.media.parse_mode is None
    with pytest.raises(ValueError, match="media"):
        build_edit(Op.EDIT_MEDIA, View(text="x"), 5, 9, None)
    with pytest.raises(ValueError, match="not an edit"):
        build_edit(Op.SEND_NEW, view, 5, 9, None)
    with pytest.raises(ValueError, match="non-empty"):
        build_send(View(text="  "), 5, None)
    too_long_html = View(text="x" * 5000, parse_mode="HTML")
    with pytest.raises(TextTooLongError):
        build_send(too_long_html, 5, None)


def test_content_view_modes() -> None:
    snap = build_snapshot(
        [
            {
                "id": 1,
                "code": "promo",
                "kind": "custom",
                "body": {
                    "ru": {
                        "text": "До конца {days_left} дн.",
                        "entities": [{"type": "bold", "offset": 0, "length": 2}],
                    }
                },
                "media_id": 7,
                "media_mode": "attach",
            },
            {
                "id": 2,
                "code": "prev",
                "kind": "custom",
                "body": {"ru": "Текст"},
                "media_id": 7,
                "media_mode": "preview",
            },
            {"id": 3, "code": "empty", "kind": "custom", "body": {}},
        ],
        [{"id": 1, "screen_id": 1, "label": {"ru": "Купить"}, "action": {"type": "system", "name": "buy"}}],
        [{"id": 7, "kind": "photo", "sha256": "a" * 64}],
        version=1,
    )
    ref = MediaRef("photo", "FID", "m:7", 7)
    promo = snap.get_screen("promo")
    assert promo is not None
    v = content_view(promo, USER, media=ref)
    assert v.text == "До конца 5 дн." and v.media is ref
    assert v.entities is not None and v.entities[0].type == "bold"
    assert _texts(v.keyboard) == [["Купить"]]
    prev = snap.get_screen("prev")
    assert prev is not None
    pv = content_view(prev, USER, media=ref, preview_url="https://shop.example/m/7")
    assert pv.media is None and pv.preview is not None and pv.preview.url == "https://shop.example/m/7"
    assert pv.preview.show_above_text is True
    # without a public URL the preview mode falls back to an attachment
    assert content_view(prev, USER, media=ref).media is ref
    empty = snap.get_screen("empty")
    assert empty is not None and content_view(empty, USER).text == "…"
