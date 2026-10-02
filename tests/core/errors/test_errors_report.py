from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser

import pytest

from svbg.core.errors.classify import Severity
from svbg.core.errors.hub import format_stack, safe_message
from svbg.core.errors.report import (
    MAX_LEN,
    ErrorGroupView,
    format_duration_ru,
    plural_ru,
    render_report,
    utf16_len,
)
from svbg.core.errors.sanitize import sanitize_context, scrub_pii
from svbg.core.log import SecretRegistry, register_secret

SECRET = "Sup3rS3cretPanelTok"
BOT_TOKEN = "123456789:AAH-abcdefghijklmnopqrstuvwxyz012345"
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
ALLOWED_TAGS = {"b", "code", "blockquote"}


@pytest.fixture
def secret() -> Iterator[str]:
    register_secret(SECRET)
    try:
        yield SECRET
    finally:
        SecretRegistry.unregister(SECRET)


class _Checker(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.tags: set[str] = set()
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.add(tag)
        if tag == "blockquote":
            assert attrs == [("expandable", None)]
        else:
            assert attrs == []
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        assert self.stack and self.stack[-1] == tag, f"unbalanced </{tag}>"
        self.stack.pop()

    def handle_data(self, data: str) -> None:
        self.text.append(data)


def check_html(text: str) -> str:
    """Assert Telegram-HTML validity; return the visible text."""
    p = _Checker()
    p.feed(text)
    p.close()
    assert not p.stack, f"unclosed tags {p.stack}"
    assert p.tags <= ALLOWED_TAGS, p.tags
    # every "&" in the source must start one of the three entities we emit
    assert text.count("&") == text.count("&amp;") + text.count("&lt;") + text.count("&gt;")
    return "".join(p.text)


def make_view(**kw: object) -> ErrorGroupView:
    base: dict[str, object] = {
        "fingerprint": "a" * 40,
        "place": "screen:subscription",
        "title": "Не открылся блок LTE",
        "hint": "Проверьте «Состояние → LTE»",
        "first_seen": T0,
        "last_seen": T0 + timedelta(minutes=10),
        "count": 7,
        "users_count": 3,
        "episode_count": 7,
        "episode_started_at": T0,
        "module": "lte",
        "handled": "экран показан без блока LTE",
        "last_user_id": 42,
        "exc_type": "ValueError",
        "message": "bad value",
        "stack": 'Traceback (most recent call last):\n  File "svbg/x.py", line 1, in f\n'
        "ValueError: bad value",
        "version": "0.1.0",
        "event_id": 99,
    }
    base.update(kw)
    return ErrorGroupView(**base)  # type: ignore[arg-type]


def test_report_layout() -> None:
    text = render_report(make_view())
    visible = check_html(text)
    lines = text.split("\n")
    assert lines[0].startswith("🚨 <b>Не открылся блок LTE</b>")
    assert "Где:" in visible and "screen:subscription (модуль lte)" in visible
    assert "У кого: 3 пользователя, последний — #42; ×7 за 10 мин" in visible
    assert "Что сделано: экран показан без блока LTE" in visible
    assert "Что проверить: Проверьте «Состояние → LTE»" in visible
    assert "<blockquote expandable>" in text
    assert text.endswith("</blockquote>")
    assert "событие #99" in visible
    assert "версия 0.1.0" in visible


def test_reopened_and_muted_and_no_users() -> None:
    text = render_report(
        make_view(
            reopened=True,
            users_count=0,
            episode_count=1,
            count=12,
            status="muted",
            muted_until=T0 + timedelta(hours=1),
            severity=Severity.WARN,
        )
    )
    visible = check_html(text)
    assert text.startswith("⚠️ 🔁 снова · ")
    assert "не связано с пользователем; один раз (всего ×12)" in visible
    assert "Заглушено до 01.10 13:00 UTC" in visible


def test_html_is_escaped() -> None:
    nasty = '<script>alert("x")</script> & <b>bold</b> </blockquote>'
    text = render_report(make_view(title=nasty, message=nasty, place=nasty, hint=nasty, stack=nasty))
    visible = check_html(text)
    assert "<script>" not in text
    assert nasty in visible  # survives as visible text


def test_secrets_and_pii_masked(secret: str) -> None:
    try:
        raise RuntimeError(
            f"panel said no: token={secret} bot={BOT_TOKEN} "
            f"dsn=postgresql://svbg:pgpass123@db/svbg mail ivan@example.com @ivan_petrov +7 (912) 345-67-89"
        )
    except RuntimeError as e:
        exc = e
    view = make_view(message=safe_message(exc), stack=format_stack(exc), title=f"x {secret}")
    text = render_report(view)
    for leaked in (
        secret,
        BOT_TOKEN,
        "AAH-abcdefghijklmnop",
        "pgpass123",
        "ivan@example.com",
        "ivan_petrov",
        "345-67-89",
    ):
        assert leaked not in text, leaked
    # also masked even if a caller hands raw values to the view
    raw = render_report(make_view(message=f"{secret} {BOT_TOKEN}", stack=f"x {secret}\n{BOT_TOKEN}"))
    assert secret not in raw
    assert BOT_TOKEN not in raw
    assert "***" in raw


def test_hard_cap_with_huge_inputs() -> None:
    huge_stack = "\n".join(f'  File "svbg/mod{i}.py", line {i}, in f{i} <&>' for i in range(5000))
    view = make_view(
        title="T" * 5000,
        place="&" * 5000,
        hint="<" * 5000,
        handled="h" * 5000,
        message="m&" * 5000,
        stack=huge_stack,
    )
    text = render_report(view)
    assert utf16_len(text) <= MAX_LEN
    check_html(text)
    # stack keeps the most recent (bottom) lines
    assert "mod4999.py" in text
    assert "mod0.py" not in text


def test_cap_counts_utf16_units() -> None:
    emoji_stack = "\n".join("😀" * 50 for _ in range(500))
    text = render_report(make_view(stack=emoji_stack, message="😀" * 2000))
    assert utf16_len(text) <= MAX_LEN
    check_html(text)


def test_stack_paths_are_shortened() -> None:
    try:
        int("x")
    except ValueError as e:
        stack = format_stack(e)
    assert "test_errors_report.py" in stack
    assert "\\Users\\" not in stack and "/Users/" not in stack


def test_hostile_exception_str() -> None:
    class Bad(Exception):
        def __str__(self) -> str:
            raise RuntimeError("no str")

    assert "str() failed" in safe_message(Bad())


def test_helpers() -> None:
    assert plural_ru(1, "a", "b", "c") == "a"
    assert plural_ru(3, "a", "b", "c") == "b"
    assert plural_ru(11, "a", "b", "c") == "c"
    assert plural_ru(21, "a", "b", "c") == "a"
    assert format_duration_ru(timedelta(seconds=5)) == "меньше минуты"
    assert format_duration_ru(timedelta(minutes=90)) == "1 ч 30 мин"
    assert format_duration_ru(timedelta(days=3)) == "3 дня"
    assert scrub_pii("call +79123456789 or a@b.co @someone") == "call *** or ***@*** @***"


def test_sanitize_context() -> None:
    ctx = sanitize_context(
        {
            "screen": "home",
            "api_token": "abc",
            "n": 5,
            "nested": {"password": "p", "ok": [1, 2, {"x": BOT_TOKEN}]},
            "obj": object(),
            "nan": float("nan"),
        }
    )
    assert ctx["screen"] == "home"
    assert ctx["api_token"] == "***"
    assert ctx["nested"]["password"] == "***"
    assert BOT_TOKEN not in repr(ctx)
    assert ctx["nan"] is None
    assert isinstance(ctx["obj"], str)
    big = sanitize_context({f"k{i}": i for i in range(100)})
    assert len(big) <= 31
