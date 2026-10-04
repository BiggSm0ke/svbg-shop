from __future__ import annotations

import pytest

from svbg.content.model import (
    Button,
    ContentError,
    CopyAction,
    DeeplinkAction,
    ModuleAction,
    Screen,
    ScreenAction,
    ShareAction,
    SystemAction,
    UrlAction,
    WebAppAction,
    parse_action,
    parse_label,
    parse_text_blocks,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"type": "screen", "target": "home"}, ScreenAction("home")),
        ({"type": "screen", "target": 12}, ScreenAction("12")),
        ("screen:buy", ScreenAction("buy")),
        ({"type": "system", "name": "buy"}, SystemAction("buy")),
        ("system:topup", SystemAction("topup")),
        ({"type": "url", "url": "https://example.com/x?a=1"}, UrlAction("https://example.com/x?a=1")),
        ("url:tg://resolve?domain=x", UrlAction("tg://resolve?domain=x")),
        ({"type": "webapp", "url": "https://app.example.com"}, WebAppAction("https://app.example.com")),
        ({"type": "deeplink", "code": "p_month"}, DeeplinkAction("p_month")),
        ({"type": "copy", "text": "PROMO2026"}, CopyAction("PROMO2026")),
        ({"type": "share", "text": "Попробуй VPN"}, ShareAction("Попробуй VPN")),
        ("module:lte.status", ModuleAction("lte", "status")),
        ({"type": "module", "ext": "ip_guard", "action": "unblock"}, ModuleAction("ip_guard", "unblock")),
    ],
)
def test_parse_action_all_kinds(raw: object, expected: object) -> None:
    action = parse_action(raw)
    assert action == expected
    assert parse_action(action.to_json()) == action  # round trip


@pytest.mark.parametrize(
    ("raw", "path"),
    [
        ({"type": "nope"}, "action.type"),
        ("weird", "action"),
        ("bogus:x", "action"),
        ({"type": "screen", "target": "a:b"}, "action.target"),
        ({"type": "screen", "target": True}, "action.target"),
        ({"type": "url", "url": "javascript:alert(1)"}, "action.url"),
        ({"type": "url", "url": "https://exa mple.com"}, "action.url"),
        ({"type": "url", "url": "ftp://x.y"}, "action.url"),
        ({"type": "webapp", "url": "http://insecure.example"}, "action.url"),
        ({"type": "deeplink", "code": "bad code"}, "action.code"),
        ({"type": "copy", "text": "x" * 257}, "action.text"),
        ({"type": "copy", "text": "   "}, "action.text"),
        ("module:noaction", "action"),
        ({"type": "system", "name": "a" * 40}, "action.name"),
        (42, "action"),
    ],
)
def test_parse_action_rejects(raw: object, path: str) -> None:
    with pytest.raises(ContentError) as ei:
        parse_action(raw)
    assert ei.value.path == path


def test_text_blocks_validate_entities_in_utf16() -> None:
    # "👋" is two UTF-16 units: the bold entity over "👋 Hi" has length 5.
    blocks = parse_text_blocks(
        {"ru": {"text": "👋 Hi", "entities": [{"type": "bold", "offset": 0, "length": 5}]}}
    )
    assert blocks["ru"].entities[0]["length"] == 5
    with pytest.raises(ContentError, match="за пределы текста"):
        parse_text_blocks({"ru": {"text": "👋 Hi", "entities": [{"type": "bold", "offset": 0, "length": 6}]}})


def test_text_blocks_reject_bad_values() -> None:
    with pytest.raises(ContentError, match="код языка"):
        parse_text_blocks({"Russian!": "x"})
    with pytest.raises(ContentError, match="тип форматирования"):
        parse_text_blocks({"ru": {"text": "abc", "entities": [{"type": "evil", "offset": 0, "length": 1}]}})
    with pytest.raises(ContentError, match="премиум-эмодзи"):
        parse_text_blocks(
            {"ru": {"text": "abc", "entities": [{"type": "custom_emoji", "offset": 0, "length": 1}]}}
        )
    with pytest.raises(ContentError, match="ссылк"):
        parse_text_blocks(
            {"ru": {"text": "abc", "entities": [{"type": "text_link", "offset": 0, "length": 1, "url": "x"}]}}
        )
    with pytest.raises(ContentError):
        parse_text_blocks({"ru": {"text": "x" * 4097}})


def test_text_blocks_drop_unknown_entity_keys_and_accept_plain_strings() -> None:
    blocks = parse_text_blocks(
        {
            "ru": {"text": "abc", "entities": [{"type": "bold", "offset": 0, "length": 1, "evil": 1}]},
            "en": "hi",
        }
    )
    assert "evil" not in blocks["ru"].entities[0]
    assert blocks["en"].text == "hi"


def test_label_needs_some_text() -> None:
    assert parse_label("Купить")["ru"] == "Купить"
    with pytest.raises(ContentError):
        parse_label({"ru": "  "})
    with pytest.raises(ContentError):
        parse_label({"ru": "x" * 200})


def test_button_validates_style_and_icon() -> None:
    with pytest.raises(ContentError):
        Button(label={"ru": "x"}, action=SystemAction("buy"), style="purple")  # type: ignore[arg-type]
    with pytest.raises(ContentError):
        Button(label={"ru": "x"}, action=SystemAction("buy"), icon_custom_emoji_id="abc")
    b = Button.from_row(
        {
            "id": 1,
            "label": {"ru": "Купить"},
            "action": {"type": "system", "name": "buy"},
            "style": "success",
            "icon_custom_emoji_id": "5368324170671202286",
            "row": 2,
            "sort": 1,
            "enabled": True,
        }
    )
    assert b.style == "success" and b.row == 2


def test_screen_text_is_russian_with_a_fallback() -> None:
    s = Screen.from_row({"id": 1, "code": "x", "kind": "custom", "body": {"ru": "привет", "en": "hello"}})
    assert s.text("en").text == "привет"  # an old English text stays in the row unused
    assert s.text("de").text == "привет"
    only_en = Screen.from_row({"id": 2, "code": "y", "body": {"en": "hello"}})
    assert only_en.text("de").text == "hello"
    assert Screen.from_row({"id": 3, "code": None}).text("ru").text == ""
    with pytest.raises(ContentError):
        Screen.from_row({"id": 4, "code": "z", "media_mode": "inline"})
