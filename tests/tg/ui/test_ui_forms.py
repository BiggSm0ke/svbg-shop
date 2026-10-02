from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
from aiogram.methods import DeleteMessage, EditMessageText, SendMessage
from ui_harness import Env, callback, make_env, text_message

from svbg.core.clock import FrozenClock, reset_clock, set_clock
from svbg.core.log import mask
from svbg.tg.ui import forms
from svbg.tg.ui.context import UserCtx
from svbg.tg.ui.forms import Field, Form, FormState, ValidationError, integer, text
from svbg.tg.ui.router import ScreenCtx
from svbg.tg.ui.view import Toast, View
from tests.dbkit import CountingDatabase, open_db


@pytest.fixture
async def db(pg_dsn: str) -> AsyncIterator[CountingDatabase]:
    async with open_db(pg_dsn) as database:
        yield database


@pytest.fixture
async def env(db: CountingDatabase) -> AsyncIterator[Env]:
    async with make_env(db) as e:
        yield e


DONE: list[dict[str, Any]] = []


async def _done(ctx: ScreenCtx, data: dict[str, Any]) -> View:
    DONE.append(data)
    return View(text=f"Промокод {data['code']} на {data['days']} дн. создан", toast="✅ Готово")


def _promo_form(**kw: Any) -> Form:
    return Form(
        name="promo.create",
        fields=(
            Field("code", {"ru": "Введите код", "en": "Enter the code"}, text(max_len=16)),
            Field("days", "Сколько дней?", integer(min_value=1, max_value=365)),
            Field("note", "Комментарий (необязательно)", optional=True),
        ),
        on_done=_done,
        **kw,
    )


def _register(env: Env, form: Form) -> None:
    env.router.form(form)

    @env.router.action("home", "newpromo")
    async def start(ctx: ScreenCtx, arg: Any) -> View:
        return await ctx.start_form(form.name, {"source": "test"})


@pytest.fixture(autouse=True)
def _reset_done() -> None:
    DONE.clear()


async def _start(env: Env, tg: int) -> None:
    await env.router.dispatch_callback(callback(tg, "v1:home:newpromo"))


def _last_text(env: Env) -> str:
    sent = [c for c in env.transport.calls if isinstance(c, SendMessage | EditMessageText)]
    return sent[-1].text


async def test_full_form_flow(env: Env) -> None:
    user = await env.add(111, role="admin")
    _register(env, _promo_form())
    await _start(env, 111)
    edit = env.transport.of(EditMessageText)[-1]
    assert edit.text == "(1/3) Введите код"
    assert edit.reply_markup is not None
    assert [b.callback_data for b in edit.reply_markup.inline_keyboard[0]] == ["v1:form:cancel"]
    assert (await env.ui_state.get(user.user_id)).awaiting is not None

    assert await env.router.dispatch_message(text_message(111, "  AUTUMN  "))
    # after a text message the bot answers with a new message and removes the old prompt
    assert _last_text(env) == "(2/3) Сколько дней?"
    assert isinstance(env.transport.calls[-2], SendMessage)
    assert isinstance(env.transport.calls[-1], DeleteMessage)

    assert await env.router.dispatch_message(text_message(111, "сорок", message_id=501))
    assert _last_text(env).startswith("⚠️ Нужно целое число")
    assert await env.router.dispatch_message(text_message(111, "999", message_id=502))
    assert _last_text(env).startswith("⚠️ Максимум 365")
    assert await env.router.dispatch_message(text_message(111, "30", message_id=503))
    last = env.transport.of(SendMessage)[-1]
    assert last.text == "(3/3) Комментарий (необязательно)"
    assert last.reply_markup is not None
    assert [b.callback_data for b in last.reply_markup.inline_keyboard[0]] == [
        "v1:form:skip",
        "v1:form:cancel",
    ]

    await env.router.dispatch_callback(callback(111, "v1:form:skip", message_id=last_id(env)))
    assert DONE == [{"source": "test", "code": "AUTUMN", "days": 30, "note": None}]
    assert (await env.ui_state.get(user.user_id)).awaiting is None
    assert _last_text(env) == "Промокод AUTUMN на 30 дн. создан"
    assert env.transport.toasts[-1] is None  # the skip click was answered before on_done ran
    # nothing is awaited any more: free text is not consumed
    assert not await env.router.dispatch_message(text_message(111, "ещё", message_id=504))


def last_id(env: Env) -> int:
    state = env.ui_state._cache
    return next(iter(state.values())).main_msg_id or 0


async def test_cancel_by_button_and_command(env: Env) -> None:
    user = await env.add(111, role="admin")
    _register(env, _promo_form())
    await _start(env, 111)
    await env.router.dispatch_callback(callback(111, "v1:form:cancel"))
    assert env.transport.toasts[-1] == "Отменено"
    assert _last_text(env).startswith("👋")
    assert (await env.ui_state.get(user.user_id)).awaiting is None

    await _start(env, 111)
    assert await env.router.dispatch_message(text_message(111, "/cancel@svbg_bot"))
    assert (await env.ui_state.get(user.user_id)).awaiting is None
    assert _last_text(env).startswith("👋")
    assert DONE == []


async def test_custom_on_cancel_and_other_commands(env: Env) -> None:
    user = await env.add(111, role="admin")

    async def on_cancel(ctx: ScreenCtx) -> Toast:
        return Toast("Создание промокода отменено")

    _register(env, _promo_form(on_cancel=on_cancel))
    await _start(env, 111)
    await env.router.dispatch_callback(callback(111, "v1:form:cancel"))
    assert env.transport.toasts[-1] == "Создание промокода отменено"
    await _start(env, 111)
    # another command abandons the form and is left to other handlers
    assert not await env.router.dispatch_message(text_message(111, "/start"))
    assert (await env.ui_state.get(user.user_id)).awaiting is None


async def test_form_survives_restart(env: Env) -> None:
    await env.add(111, role="admin")
    form = _promo_form()
    _register(env, form)
    await _start(env, 111)
    assert await env.router.dispatch_message(text_message(111, "WINTER"))
    # process restart: new router and cold ui_state cache over the same database
    env2 = env.restarted()
    env2.router.form(form)
    assert await env2.router.dispatch_message(text_message(111, "7", message_id=600))
    assert _last_text(env2) == "(3/3) Комментарий (необязательно)"
    assert await env2.router.dispatch_message(text_message(111, "для блогера", message_id=601))
    assert DONE == [{"source": "test", "code": "WINTER", "days": 7, "note": "для блогера"}]


async def test_unknown_or_expired_form_is_discarded(env: Env) -> None:
    clock = FrozenClock()
    set_clock(clock)
    try:
        user = await env.add(111, role="admin")
        _register(env, _promo_form(ttl=timedelta(minutes=5)))
        await _start(env, 111)
        clock.advance(timedelta(minutes=6))
        assert not await env.router.dispatch_message(text_message(111, "LATE"))
        assert (await env.ui_state.get(user.user_id)).awaiting is None
        # a stored state for a form that no longer exists (after an update) is dropped too
        await env.ui_state.set_awaiting(user.user_id, FormState("gone", 0, {}, None).to_json())
        assert not await env.router.dispatch_message(text_message(111, "x"))
        assert (await env.ui_state.get(user.user_id)).awaiting is None
        # skip on a stale form → menu with "Меню обновилось"
        await env.ui_state.set_awaiting(user.user_id, {"kind": "garbage"})
        await env.router.dispatch_callback(callback(111, "v1:form:skip"))
        assert env.transport.toasts[-1] == "Меню обновилось"
    finally:
        reset_clock()


async def test_permissions_rechecked_on_each_input(env: Env) -> None:
    admin = await env.add(111, role="admin")
    _register(env, _promo_form(required_role="admin"))
    await _start(env, 111)
    env.users.by_tg[111] = UserCtx(admin.user_id, telegram_id=111, role="user")  # demoted mid-form
    assert await env.router.dispatch_message(text_message(111, "CODE"))
    assert _last_text(env) == "Нет прав"
    assert (await env.ui_state.get(admin.user_id)).awaiting is None
    assert DONE == []
    # a plain user cannot start it either
    await _start(env, 111)
    assert env.transport.toasts[-1] == "Нет прав"
    assert (await env.ui_state.get(admin.user_id)).awaiting is None


async def test_required_field_cannot_be_skipped_and_non_text_input(env: Env) -> None:
    await env.add(111, role="admin")
    _register(env, _promo_form())
    await _start(env, 111)
    await env.router.dispatch_callback(callback(111, "v1:form:skip"))
    assert env.transport.toasts[-1] == "Это поле обязательно"
    assert await env.router.dispatch_message(text_message(111, None))
    assert _last_text(env).startswith("⚠️ Пришлите ответ текстом.")


async def test_secret_field_is_never_persisted(env: Env, db: CountingDatabase) -> None:
    user = await env.add(111, role="owner")
    got: list[dict[str, Any]] = []

    async def done(ctx: ScreenCtx, data: dict[str, Any]) -> Toast:
        got.append(data)
        return Toast("Сохранено")

    form = Form(
        name="token.set",
        fields=(Field("label", "Название"), Field("token", "Токен", secret=True)),
        on_done=done,
        required_role="owner",
    )
    _register(env, form)
    await _start(env, 111)
    assert await env.router.dispatch_message(text_message(111, "main", message_id=700))
    rows = await db.raw("select awaiting::text as a from ui_state where user_id = $1", user.user_id)
    assert "main" in rows[0]["a"]
    assert await env.router.dispatch_message(text_message(111, "123456:SECRET-TOKEN-VALUE", message_id=701))
    assert got == [{"source": "test", "label": "main", "token": "123456:SECRET-TOKEN-VALUE"}]
    # the user's message with the secret was deleted, and the secret never reached the database
    assert any(isinstance(c, DeleteMessage) and c.message_id == 701 for c in env.transport.calls)
    rows = await db.raw(
        "select coalesce(awaiting::text, '') as a from ui_state where user_id = $1", user.user_id
    )
    assert "SECRET" not in rows[0]["a"]


async def test_token_sent_after_the_secret_form_expired_is_still_deleted(env: Env) -> None:
    clock = FrozenClock()
    set_clock(clock)
    try:
        user = await env.add(111, role="owner")
        got: list[dict[str, Any]] = []

        async def done(ctx: ScreenCtx, data: dict[str, Any]) -> Toast:
            got.append(data)
            return Toast("Сохранено")

        form = Form(
            "token.late", (Field("token", "Токен", secret=True),), on_done=done, required_role="owner"
        )
        _register(env, form)
        await _start(env, 111)
        clock.advance(timedelta(hours=2))  # the owner came back after the form's ttl
        token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhcGkifQ.c2lnbmF0dXJlLXZhbHVl"
        assert await env.router.dispatch_message(text_message(111, token, message_id=801))
        assert any(isinstance(c, DeleteMessage) and c.message_id == 801 for c in env.transport.calls)
        assert "Время ввода истекло" in _last_text(env) and "удалено" in _last_text(env)
        assert got == [] and (await env.ui_state.get(user.user_id)).awaiting is None
        assert mask(f"log line {token}") == "log line ***"  # registered: never reaches a log in clear
    finally:
        reset_clock()


async def test_is_awaiting_filter(env: Env) -> None:
    await env.add(111, role="admin")
    _register(env, _promo_form())
    assert not await env.router.is_awaiting(text_message(111, "x"))
    await _start(env, 111)
    assert await env.router.is_awaiting(text_message(111, "x"))
    assert not await env.router.is_awaiting(text_message(111, "x", chat_type="group"))
    assert not await env.router.is_awaiting(text_message(999, "x"))  # unknown user


# ---------------------------------------------------------------- pure helpers


def test_form_definition_validation() -> None:
    f = Field("a", "A")
    with pytest.raises(ValueError, match="name"):
        Form("Bad Name", (f,), on_done=_done)
    with pytest.raises(ValueError, match="at least one"):
        Form("x", (), on_done=_done)
    with pytest.raises(ValueError, match="unique"):
        Form("x", (f, f), on_done=_done)
    with pytest.raises(ValueError, match="secret"):
        Form("x", (Field("s", "S", secret=True), f), on_done=_done)
    with pytest.raises(ValueError, match="role"):
        Form("x", (f,), on_done=_done, required_role="root")
    with pytest.raises(ValueError, match="ttl"):
        Form("x", (f,), on_done=_done, ttl=timedelta(0))


def test_state_json_roundtrip_and_corruption() -> None:
    form = _promo_form()
    state = forms.start_state(form, {"a": 1})
    assert FormState.from_json(state.to_json()) == state
    for bad in (
        None,
        "x",
        {},
        {"kind": "form", "v": 99},
        {"kind": "form", "v": 1, "form": 1, "step": 0, "data": {}},
        {"kind": "form", "v": 1, "form": "f", "step": -1, "data": {}},
        {"kind": "form", "v": 1, "form": "f", "step": 0, "data": []},
        {"kind": "form", "v": 1, "form": "f", "step": 0, "data": {}, "exp": "not a date"},
        {"kind": "form", "v": 1, "form": "f", "step": 0, "data": {}, "exp": "2026-01-01T00:00:00"},
    ):
        assert FormState.from_json(bad) is None


def test_validators() -> None:
    assert text()("  hi ") == "hi"
    with pytest.raises(ValidationError):
        text()("   ")
    with pytest.raises(ValidationError):
        text(max_len=3)("abcd")
    assert integer()("1 000") == 1000
    assert integer(min_value=-5)("-5") == -5
    with pytest.raises(ValidationError):
        integer()("1e9")
    with pytest.raises(ValidationError):
        integer(min_value=1)("0")

    def raw_value_error(v: str) -> str:
        raise ValueError("internal detail")

    form = Form("x", (Field("a", "A", raw_value_error),), on_done=_done)
    step = forms.advance(form, forms.start_state(form), "v")
    assert step.error == ""  # generic message, internal details are not shown
    view = forms.prompt_view(form, step.state, "ru", error=step.error)
    assert view.text.startswith("⚠️ Не понял ответ")
    long = forms.advance(form, forms.start_state(form), "x" * 5000)
    assert long.error is not None and "максимум" in long.error
