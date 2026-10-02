from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteMessage,
    EditMessageCaption,
    EditMessageMedia,
    EditMessageText,
    SendMessage,
    SendPhoto,
)
from aiogram.types import FSInputFile, Update
from ui_harness import Env, callback, make_env, make_router, text_message

from svbg.content import defaults
from svbg.content.tables import media, screen_buttons, screens
from svbg.core.log import mask
from svbg.tg.ui import router as router_mod
from svbg.tg.ui.codec import decode, encode
from svbg.tg.ui.forms import Field, Form
from svbg.tg.ui.renderer import MessageShape
from svbg.tg.ui.router import MAX_WAITING_PER_USER, BotTransport, ScreenCtx, ScreenRouter, UiStateStore
from svbg.tg.ui.view import Redirect, Toast, View
from tests.dbkit import CountingDatabase, open_db


@pytest.fixture
async def db(pg_dsn: str) -> AsyncIterator[CountingDatabase]:
    async with open_db(pg_dsn) as database:
        yield database


@pytest.fixture
async def env(db: CountingDatabase) -> AsyncIterator[Env]:
    async with make_env(db) as e:
        yield e


STALE = "Меню обновилось"
DENIED = "Нет прав"
ERROR_TOAST = "Что-то пошло не так, мы уже знаем"
FLOOD = "Слишком часто, подождите пару секунд и повторите"


async def _media_screen(
    db: CountingDatabase,
    code: str,
    *,
    file_ids: dict[str, str] | None = None,
    path: str | None = None,
    mode: str = "attach",
    sha: str = "b",
) -> int:
    async with db.tx() as conn:
        m = (
            await conn.execute(
                sa.insert(media)
                .values(kind="photo", sha256=sha * 64, path=path, file_ids=file_ids or {})
                .returning(media.c.id)
            )
        ).first()
        assert m is not None
        await conn.execute(
            sa.insert(screens).values(
                code=code,
                kind="custom",
                body={"ru": {"text": f"Экран {code}"}},
                media_id=m.id,
                media_mode=mode,
            )
        )
    return int(m.id)


# ---------------------------------------------------------------- basics


async def test_open_content_screen_answers_first_then_edits(env: Env) -> None:
    user = await env.add(111)
    await env.router.dispatch_callback(callback(111, "v1:home:o"))
    assert env.transport.events[0] == "answer"
    assert env.transport.toasts == [None]
    (edit,) = env.transport.of(EditMessageText)
    assert edit.message_id == 10 and edit.chat_id == 111
    assert edit.text.startswith("👋")
    assert edit.parse_mode is None and edit.entities and edit.entities[0].type == "bold"
    markup = edit.reply_markup.inline_keyboard if edit.reply_markup else []
    data = [b.callback_data for row in markup for b in row]
    assert "v1:settings_root:o" not in data  # settings button hidden for a plain user
    state = await env.ui_state.get(user.user_id)
    assert state.main_msg_id == 10 and state.main_shape == MessageShape("text")


async def test_admin_sees_one_admin_button_and_settings_still_open(env: Env) -> None:
    await env.add(222, role="admin")
    await env.router.dispatch_callback(callback(222, "v1:home:o"))
    edit = env.transport.of(EditMessageText)[-1]
    assert edit.reply_markup is not None
    data = [b.callback_data for row in edit.reply_markup.inline_keyboard for b in row]
    assert "v1:admin:o" in data  # the one staff entry; settings and plans live inside the admin
    assert "v1:settings_root:o" not in data and "v1:plans:o" not in data
    await env.router.dispatch_callback(callback(222, "v1:settings_root:o"))
    assert env.transport.of(EditMessageText)[-1].text.startswith("⚙️")


@pytest.mark.parametrize(
    "data",
    ["garbage", "v0:home:o", "v1:nosuch:o", "v1:home:zzz", "v1:sys:unknown", None, "v1:s:a:~AAAAAAAAAAAA"],
)
async def test_unknown_or_old_callback_goes_home_with_toast(env: Env, data: str | None) -> None:
    await env.add(111)
    await env.router.dispatch_callback(callback(111, data))
    assert env.transport.toasts == [STALE]
    (edit,) = env.transport.of(EditMessageText)
    assert edit.text.startswith("👋")
    assert env.hub.captured == []


async def test_disabled_content_screen_is_stale(env: Env, db: CountingDatabase) -> None:
    await env.add(111)
    await db.raw("insert into screens (code, kind, body, enabled) values ('off', 'custom', '{}', false)")
    await env.content.reload()
    await env.router.dispatch_callback(callback(111, "v1:off:o"))
    assert env.transport.toasts == [STALE]


# ---------------------------------------------------------------- permissions


async def test_permissions_are_checked_on_every_callback(env: Env) -> None:
    denied: list[tuple[int, str]] = []
    env.router.on_denied = lambda u, place: denied.append((u.user_id, place))
    calls: list[str] = []

    @env.router.action("adm", "wipe", required_role="owner")
    async def wipe(ctx: ScreenCtx, arg: Any) -> Toast:
        calls.append("wipe")
        return Toast("Готово")

    @env.router.action("adm", "stats", required_role="admin", perm="stats")
    async def stats(ctx: ScreenCtx, arg: Any) -> Toast:
        calls.append("stats")
        return Toast("ok")

    user = await env.add(111)
    await env.router.dispatch_callback(callback(111, "v1:settings_root:o"))
    await env.router.dispatch_callback(callback(111, "v1:adm:wipe"))
    assert env.transport.toasts == [DENIED, DENIED]
    assert env.transport.of(EditMessageText) == []
    assert [p for _, p in denied] == ["screen:settings_root", "action:adm.wipe"]

    # promoted to admin without perms: stats still denied; with the perm → allowed
    env.users.by_tg[111] = type(user)(user.user_id, telegram_id=111, role="admin")
    await env.router.dispatch_callback(callback(111, "v1:adm:stats"))
    env.users.by_tg[111] = type(user)(user.user_id, telegram_id=111, role="admin", perms=frozenset({"stats"}))
    await env.router.dispatch_callback(callback(111, "v1:adm:stats"))
    # demoted again → the very next click is denied (no stale permission cache in the router)
    env.users.by_tg[111] = user
    await env.router.dispatch_callback(callback(111, "v1:adm:stats"))
    assert calls == ["stats"]
    assert env.transport.toasts[-3:] == [DENIED, "ok", DENIED]


async def test_denied_hook_failure_does_not_break_the_click(env: Env) -> None:
    async def broken(_u: Any, _p: str) -> None:
        raise RuntimeError("audit down")

    env.router.on_denied = broken
    await env.add(111)
    await env.router.dispatch_callback(callback(111, "v1:settings_root:o"))
    assert env.transport.toasts == [DENIED]


# ---------------------------------------------------------------- error isolation


async def test_exception_in_screen_shows_fallback_and_reports(env: Env) -> None:
    await env.add(111)

    @env.router.screen("boom")
    async def boom(ctx: ScreenCtx, arg: Any) -> View:
        raise ZeroDivisionError("broken screen")

    await env.router.dispatch_callback(callback(111, "v1:boom:o"))
    assert len(env.hub.captured) == 1
    exc, place, kw = env.hub.captured[0]
    assert isinstance(exc, ZeroDivisionError) and place == "ui:callback"
    assert kw["user_id"] is not None
    edit = env.transport.of(EditMessageText)[-1]
    assert edit.text.startswith("⚠️")
    assert edit.reply_markup is not None
    assert edit.reply_markup.inline_keyboard[0][0].callback_data == "v1:home:o"
    # the screen answered before failing, so the toast slot was already used; the bot keeps working
    await env.router.dispatch_callback(callback(111, "v1:home:o"))
    assert env.transport.of(EditMessageText)[-1].text.startswith("👋")


async def test_exception_in_action_gets_error_toast(env: Env) -> None:
    await env.add(111)

    @env.router.action("home", "buy")
    async def buy(ctx: ScreenCtx, arg: Any) -> View:
        raise KeyError("no plan")

    await env.router.dispatch_callback(callback(111, "v1:home:buy"))
    assert env.transport.toasts == [ERROR_TOAST]
    assert env.transport.of(EditMessageText)[-1].text.startswith("⚠️")
    assert len(env.hub.captured) == 1


async def test_builtin_fallback_without_content(db: CountingDatabase) -> None:
    async with make_env(db) as env:
        router = make_router(env.transport, env.users, env.ui_state, None, None, env.hub)
        await env.add(111)

        @router.screen("boom")
        async def boom(ctx: ScreenCtx, arg: Any) -> View:
            raise ValueError("x")

        await router.dispatch_callback(callback(111, "v1:boom:o"))
        edit = env.transport.of(EditMessageText)[-1]
        assert edit.text.startswith("⚠️ Что-то пошло не так")
        # home without content and without a code screen still renders a built-in menu
        await router.dispatch_callback(callback(111, "v1:home:o"))
        assert env.transport.of(EditMessageText)[-1].text == "Главное меню"


async def test_handler_timeout_is_isolated(db: CountingDatabase) -> None:
    async with make_env(db, handler_timeout=0.2, answer_deadline=0.05) as env:
        await env.add(111)

        @env.router.action("home", "slow")
        async def slow(ctx: ScreenCtx, arg: Any) -> View:
            await asyncio.sleep(5)
            return View(text="never")

        await env.router.dispatch_callback(callback(111, "v1:home:slow"))
        assert isinstance(env.hub.captured[0][0], TimeoutError)
        assert env.transport.of(EditMessageText)[-1].text.startswith("⚠️")


async def test_redirect_loop_is_cut(env: Env) -> None:
    await env.add(111)

    @env.router.screen("a")
    async def a(ctx: ScreenCtx, arg: Any) -> Redirect:
        return Redirect("b")

    @env.router.screen("b")
    async def b(ctx: ScreenCtx, arg: Any) -> Redirect:
        return Redirect("a")

    await env.router.dispatch_callback(callback(111, "v1:a:o"))
    assert isinstance(env.hub.captured[0][0], RuntimeError)


async def test_user_loader_failure(env: Env) -> None:
    env.users.fail = RuntimeError("db down")
    await env.router.dispatch_callback(callback(111, "v1:home:o"))
    assert env.transport.toasts == [ERROR_TOAST]
    assert env.hub.captured[0][1] == "ui:user_loader"
    assert env.transport.calls == []
    env.users.fail = None
    await env.router.dispatch_callback(callback(999, "v1:home:o"))  # unknown/banned user → silent answer
    assert env.transport.toasts[-1] is None and env.transport.calls == []


# ---------------------------------------------------------------- actions and answers


async def test_action_results(env: Env) -> None:
    await env.add(111)
    results: dict[str, Any] = {
        "view": View(text="Готово", toast="✅ Применено"),
        "toast": Toast("Только тост", alert=True),
        "none": None,
        "redirect": Redirect("home", toast="Переход"),
    }
    for name in results:

        @env.router.action("t", name)
        async def handler(ctx: ScreenCtx, arg: Any, _name: str = name) -> Any:
            return results[_name]

    await env.router.dispatch_callback(callback(111, "v1:t:view"))
    assert env.transport.answers[-1].text == "✅ Применено"
    assert env.transport.of(EditMessageText)[-1].text == "Готово"
    n_edits = len(env.transport.of(EditMessageText))
    await env.router.dispatch_callback(callback(111, "v1:t:toast"))
    assert env.transport.answers[-1].text == "Только тост" and env.transport.answers[-1].show_alert
    await env.router.dispatch_callback(callback(111, "v1:t:none"))
    assert env.transport.answers[-1].text is None
    assert len(env.transport.of(EditMessageText)) == n_edits  # toast/None keep the message
    await env.router.dispatch_callback(callback(111, "v1:t:redirect"))
    assert env.transport.answers[-1].text == "Переход"
    assert env.transport.of(EditMessageText)[-1].text.startswith("👋")
    assert len(env.transport.answers) == 4  # exactly one answer per callback


async def test_slow_action_answers_at_deadline(db: CountingDatabase) -> None:
    async with make_env(db, answer_deadline=0.05) as env:
        await env.add(111)
        answered_before_done: list[bool] = []

        @env.router.action("home", "pay")
        async def pay(ctx: ScreenCtx, arg: Any) -> View:
            await asyncio.sleep(0.3)
            answered_before_done.append(ctx.answered)
            return View(text="Счёт создан", toast="поздний тост")

        await env.router.dispatch_callback(callback(111, "v1:home:pay"))
        assert answered_before_done == [True]
        assert env.transport.toasts == [None]  # spinner stopped at the deadline; late toast is dropped
        assert env.transport.of(EditMessageText)[-1].text == "Счёт создан"


async def test_long_args_via_short_tokens(env: Env) -> None:
    await env.add(111)
    got: list[Any] = []

    @env.router.action("ord", "check")
    async def check(ctx: ScreenCtx, arg: Any) -> Toast:
        got.append(arg)
        return Toast("ok")

    data = await env.codec.encode_long(
        "ord", "check", {"order": "0192f1c2-aaaa-7bbb-8ccc-1234567890ab", "n": 3}
    )
    assert decode(data) is not None
    await env.router.dispatch_callback(callback(111, data))
    assert got == [{"order": "0192f1c2-aaaa-7bbb-8ccc-1234567890ab", "n": 3}]
    # a restarted bot (cold cache) still resolves the old button
    env2 = env.restarted()

    @env2.router.action("ord", "check")
    async def check2(ctx: ScreenCtx, arg: Any) -> Toast:
        got.append(arg)
        return Toast("ok")

    await env2.router.dispatch_callback(callback(111, data))
    assert len(got) == 2


async def test_callbacks_of_one_user_are_serialized(db: CountingDatabase) -> None:
    async with make_env(db, answer_deadline=0.01) as env:
        await env.add(111)
        await env.add(222)
        active: dict[int, int] = {111: 0, 222: 0}
        peak: dict[int, int] = {111: 0, 222: 0}

        @env.router.action("home", "work")
        async def work(ctx: ScreenCtx, arg: Any) -> Toast:
            tg = ctx.user.telegram_id or 0
            active[tg] += 1
            peak[tg] = max(peak[tg], active[tg])
            await asyncio.sleep(0.05)
            active[tg] -= 1
            return Toast("ok")

        await asyncio.gather(
            *(env.router.dispatch_callback(callback(111, "v1:home:work", cq_id=f"a{i}")) for i in range(3)),
            *(env.router.dispatch_callback(callback(222, "v1:home:work", cq_id=f"b{i}")) for i in range(2)),
        )
        assert peak == {111: 1, 222: 1}


async def test_flood_of_clicks_is_dropped(db: CountingDatabase) -> None:
    async with make_env(db, answer_deadline=0.01) as env:
        await env.add(111)
        runs = 0

        @env.router.action("home", "work")
        async def work(ctx: ScreenCtx, arg: Any) -> None:
            nonlocal runs
            runs += 1
            await asyncio.sleep(0.05)

        n = MAX_WAITING_PER_USER + 5
        await asyncio.gather(
            *(env.router.dispatch_callback(callback(111, "v1:home:work", cq_id=f"c{i}")) for i in range(n))
        )
        assert runs == MAX_WAITING_PER_USER
        assert len(env.transport.answers) == n  # every click is answered, the extra ones with "wait"
        assert sum(1 for t in env.transport.toasts if t and "Подождите" in t) == n - MAX_WAITING_PER_USER


# ---------------------------------------------------------------- rendering transitions


async def test_text_media_transitions(env: Env, db: CountingDatabase) -> None:
    user = await env.add(111)
    await _media_screen(db, "promo", file_ids={"42": "CACHED"})
    await env.content.reload()
    # text → media: one editMessageMedia, the message stays in place
    await env.router.dispatch_callback(callback(111, "v1:promo:o"))
    assert env.transport.of(SendPhoto) == [] and env.transport.of(DeleteMessage) == []
    (edit,) = env.transport.of(EditMessageMedia)
    assert (
        edit.media.media == "CACHED" and edit.media.caption == "Экран promo" and edit.media.parse_mode is None
    )
    state = await env.ui_state.get(user.user_id)
    new_id = state.main_msg_id
    assert new_id == 10 and state.main_shape == MessageShape("photo", f"m:{photo_media_id(state)}")
    # same media again → caption-only edit (shape comes from ui_state)
    await env.router.dispatch_callback(callback(111, "v1:promo:o", message_id=new_id, photo=True))
    assert len(env.transport.of(EditMessageCaption)) == 1
    # media → text: send new text message, delete the photo
    await env.router.dispatch_callback(callback(111, "v1:home:o", message_id=new_id, photo=True))
    assert env.transport.of(SendMessage)[-1].text.startswith("👋")
    assert env.transport.of(DeleteMessage)[-1].message_id == new_id


def photo_media_id(state: Any) -> str:
    return state.main_shape.media_key.split(":")[1]


async def test_edit_failures_fall_back_to_new_message(env: Env) -> None:
    await env.add(111)
    env.transport.fail_next(
        EditMessageText,
        TelegramBadRequest(method=None, message="Bad Request: message is not modified"),  # type: ignore[arg-type]
    )
    await env.router.dispatch_callback(callback(111, "v1:home:o"))
    assert env.transport.of(SendMessage) == []  # "not modified" is success
    env.transport.fail_next(
        EditMessageText,
        TelegramBadRequest(method=None, message="Bad Request: message to edit not found"),  # type: ignore[arg-type]
    )
    await env.router.dispatch_callback(callback(111, "v1:home:o"))
    assert len(env.transport.of(SendMessage)) == 1
    assert env.transport.of(DeleteMessage) == []
    env.transport.fail_next(
        EditMessageText,
        TelegramBadRequest(method=None, message="Bad Request: message can't be edited"),  # type: ignore[arg-type]
    )
    await env.router.dispatch_callback(callback(111, "v1:home:o"))
    assert len(env.transport.of(SendMessage)) == 2
    assert len(env.transport.of(DeleteMessage)) == 1
    # the main message turned out to be media (unknown shape): replace it
    env.transport.fail_next(
        EditMessageText,
        TelegramBadRequest(method=None, message="Bad Request: there is no text in the message to edit"),  # type: ignore[arg-type]
    )
    await env.router.dispatch_callback(callback(111, "v1:home:o"))
    assert len(env.transport.of(SendMessage)) == 3
    assert len(env.transport.of(DeleteMessage)) == 2
    # any other bad request is a real error → hub + fallback attempt
    env.transport.fail_next(
        EditMessageText,
        TelegramBadRequest(method=None, message="Bad Request: can't parse entities"),  # type: ignore[arg-type]
    )
    await env.router.dispatch_callback(callback(111, "v1:home:o"))
    assert len(env.hub.captured) == 1


async def test_inaccessible_message_gets_a_new_message(env: Env) -> None:
    await env.add(111)
    await env.router.dispatch_callback(callback(111, "v1:home:o", inaccessible=True))
    assert env.transport.of(EditMessageText) == []
    assert len(env.transport.of(SendMessage)) == 1
    assert env.transport.of(DeleteMessage) == []


async def test_hot_path_click_costs_no_sql(env: Env) -> None:
    await env.add(111)
    await env.router.dispatch_callback(callback(111, "v1:home:o"))  # warm-up: loads ui_state, stores main
    before = env.db.queries
    for i in range(5):
        await env.router.dispatch_callback(callback(111, "v1:home:o", cq_id=f"h{i}"))
    assert env.db.queries == before


async def test_cold_click_stays_within_sql_budget(env: Env) -> None:
    """07 §2: at most 2 SQL per click on the hot path, even with a cold ui_state cache (after a restart)."""
    ctx = await env.add(111)
    await env.router.dispatch_callback(callback(111, "v1:home:o"))  # stores the main message
    for i in range(5):
        env.ui_state.forget(ctx.user_id)
        before = env.db.queries
        await env.router.dispatch_callback(callback(111, "v1:home:o", cq_id=f"c{i}"))
        spent = env.db.counter.since(before) if env.db.counter else []
        assert env.db.queries - before <= 2, spent
    assert env.transport.of(SendMessage)[1:] == []  # the stored main message was edited, not resent


async def test_upload_from_disk_caches_file_id(db: CountingDatabase, tmp_path: Path) -> None:
    (tmp_path / "img").mkdir()
    (tmp_path / "img" / "banner.jpg").write_bytes(b"\xff\xd8\xff")
    (tmp_path.parent / "secret.jpg").write_bytes(b"x")
    async with make_env(db, media_root=tmp_path) as env:
        await env.add(111)
        mid = await _media_screen(db, "banner", path="img/banner.jpg")
        await _media_screen(db, "evil", path="../secret.jpg", sha="c")
        await env.content.reload()
        await env.router.dispatch_callback(callback(111, "v1:banner:o"))
        (edit,) = env.transport.of(EditMessageMedia)
        assert isinstance(edit.media.media, FSInputFile)
        assert env.content.file_id(mid, 42) == "TG-FILE-ID"
        rows = await db.raw("select file_ids from media where id = $1", mid)
        assert rows[0]["file_ids"] == {"42": "TG-FILE-ID"}
        # path traversal is refused: the screen renders without media
        await env.router.dispatch_callback(callback(111, "v1:evil:o", photo=True, message_id=900))
        assert len(env.transport.of(EditMessageMedia)) == 1 and env.transport.of(SendPhoto) == []
        assert env.transport.of(SendMessage)[-1].text == "Экран evil"


async def test_preview_mode_uses_link_preview(db: CountingDatabase) -> None:
    def media_url(base: str, item: Any) -> str:
        return f"{base.rstrip('/')}/m/tok{item.id}.jpg"

    async with make_env(db, public_url=lambda: "https://shop.example/", media_url=media_url) as env:
        await env.add(111)
        mid = await _media_screen(db, "prev", mode="preview", file_ids={"42": "F"})
        await env.content.reload()
        await env.router.dispatch_callback(callback(111, "v1:prev:o"))
        edit = env.transport.of(EditMessageText)[-1]  # still a cheap text edit
        assert edit.link_preview_options is not None
        assert edit.link_preview_options.url == f"https://shop.example/m/tok{mid}.jpg"


async def test_preview_mode_without_public_media_sends_the_media(db: CountingDatabase) -> None:
    async with make_env(db, public_url=lambda: "https://shop.example/") as env:
        await env.add(111)
        await _media_screen(db, "prev", mode="preview", file_ids={"42": "F"})
        await env.content.reload()
        await env.router.dispatch_callback(callback(111, "v1:prev:o"))
        assert not any(
            getattr(c, "link_preview_options", None) is not None and c.link_preview_options.url
            for c in env.transport.calls
        )  # no dead /m/<id> link: the photo is sent by its file id instead


# ---------------------------------------------------------------- programmatic show


async def test_show_sends_then_edits_main_message(env: Env) -> None:
    user = await env.add(111)
    await env.router.show(user, 111, "home")
    (sent,) = env.transport.of(SendMessage)
    await env.router.show(user, 111, "home")
    edit = env.transport.of(EditMessageText)[-1]
    state = await env.ui_state.get(user.user_id)
    assert edit.message_id == state.main_msg_id
    await env.router.show(user, 111, "home", new=True)
    assert len(env.transport.of(SendMessage)) == 2
    assert env.transport.of(DeleteMessage)[-1].message_id == edit.message_id
    assert sent.text.startswith("👋")


async def test_ui_state_survives_restart(env: Env) -> None:
    user = await env.add(111)
    await env.router.show(user, 111, "home")
    await env.ui_state.set_pending_intent(user.user_id, {"screen": "promo", "code": "AUTUMN"})
    fresh = UiStateStore(env.db)
    state = await fresh.get(user.user_id)
    assert state.main_msg_id is not None and state.main_shape == MessageShape("text")
    assert await fresh.pop_pending_intent(user.user_id) == {"screen": "promo", "code": "AUTUMN"}
    assert (await UiStateStore(env.db).get(user.user_id)).pending_intent is None


# ---------------------------------------------------------------- registration and aiogram glue


async def test_registration_validation(env: Env) -> None:
    r: ScreenRouter = env.router
    with pytest.raises(ValueError, match="reserved"):
        r.screen("sys")
    with pytest.raises(ValueError, match="reserved"):
        r.action("home", "o")
    with pytest.raises(ValueError):
        r.screen("bad name")
    with pytest.raises(ValueError, match="role"):
        r.screen("x", required_role="root")

    @r.screen("dup")
    async def dup(ctx: ScreenCtx, arg: Any) -> View:
        return View(text="x")

    with pytest.raises(ValueError, match="already"):
        r.screen("dup")(dup)
    with pytest.raises(TypeError, match="async"):
        r.action("home", "sync")(lambda ctx, arg: None)  # type: ignore[arg-type,return-value]

    @r.action("sys", "buy")
    async def sys_buy(ctx: ScreenCtx, arg: Any) -> Toast:
        return Toast("buy")

    await env.add(111)
    await r.dispatch_callback(callback(111, encode("sys", "buy")))
    assert env.transport.toasts == ["buy"]


async def test_aiogram_dispatcher_integration(env: Env) -> None:
    await env.add(111)
    dp = Dispatcher()
    dp.include_router(env.router.aiogram_router())
    bot = Bot("123456:" + "A" * 35)
    try:
        cq = callback(111, "v1:home:o")
        await dp.feed_update(bot, Update(update_id=1, callback_query=cq))
        assert env.transport.toasts == [None]
        assert len(env.transport.of(EditMessageText)) == 1
        # a plain text message without an active form is not consumed by the UI router
        result = await dp.feed_update(bot, Update(update_id=2, message=text_message(111, "привет")))
        assert result is not None  # aiogram's UNHANDLED sentinel
        assert len(env.transport.calls) == 1
    finally:
        await bot.session.close()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhcGkifQ.c2lnbmF0dXJlLXZhbHVl", True),  # Remnawave API token (JWT)
        ("123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw", True),  # bot token
        ("Ab3dEf6hIj9kLm2nOp5qRs8tUv1wXy4zAb3dEf6h", True),  # a 40-char panel webhook secret
        ("  password=hunter2hunter2  ", True),
        ("привет", False),
        ("https://panel.example.com/api/users/1234567890abcdef", False),
        ("a" * 40, False),  # letters only: not an opaque token
        ("/setup eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhcGkifQ.c2lnbmF0dXJl", False),  # a command
        ("", False),
        (None, False),
    ],
)
def test_looks_like_secret(text: str | None, expected: bool) -> None:
    assert router_mod.looks_like_secret(text) is expected


async def test_stray_token_from_staff_is_deleted_and_explained(env: Env) -> None:
    """A token pasted with no form waiting (e.g. before pressing the wizard's button) is not left in chat."""
    await env.add(111, role="owner")
    await env.add(222, role="user")
    token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJzdHJheSJ9.c3RyYXktc2lnbmF0dXJl"
    dp = Dispatcher()
    dp.include_router(env.router.aiogram_router())
    bot = Bot("123456:" + "A" * 35)
    try:
        await dp.feed_update(bot, Update(update_id=1, message=text_message(111, token, message_id=901)))
        deleted = [c.message_id for c in env.transport.of(DeleteMessage)]
        assert deleted == [901]
        sent = env.transport.of(SendMessage)
        assert len(sent) == 1 and "Сообщение удалено" in sent[0].text and "/setup" in sent[0].text
        assert token not in mask(f"x {token}")  # registered as a secret
        # an ordinary user's message is none of the UI router's business, even if it looks like a token
        other = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.dXNlci1zaWduYXR1cmU"
        result = await dp.feed_update(
            bot, Update(update_id=2, message=text_message(222, other, message_id=902))
        )
        assert result is not None  # UNHANDLED
        # ordinary text from staff is not touched either; group chats are never touched
        await dp.feed_update(bot, Update(update_id=3, message=text_message(111, "привет", message_id=903)))
        await dp.feed_update(
            bot, Update(update_id=4, message=text_message(111, token, message_id=904, chat_type="group"))
        )
        assert [c.message_id for c in env.transport.of(DeleteMessage)] == [901]
    finally:
        await bot.session.close()


async def test_screen_ctx_helpers(env: Env) -> None:
    user = await env.add(111)
    seen: dict[str, Any] = {}

    @env.router.screen("helper")
    async def helper(ctx: ScreenCtx, arg: Any) -> View:
        seen["short"] = await ctx.callback("helper", "go", "1")
        seen["long"] = await ctx.callback("helper", "go", "x" * 100)
        seen["content"] = ctx.content_view("home")
        seen["missing"] = ctx.content_view("nope")
        seen["snapshot"] = ctx.content
        return View(text="ok")

    await env.router.dispatch_callback(callback(111, "v1:helper:o"))
    assert seen["short"] == "v1:helper:go:1"
    assert seen["long"].startswith("v1:helper:go:~")
    assert seen["content"].text.startswith("👋") and seen["missing"] is None
    assert seen["snapshot"] is env.content.snapshot
    assert user.lang == "ru"


async def test_screen_buttons_table_has_seeded_system_keys(db: CountingDatabase) -> None:
    async with make_env(db):
        rows = await db.raw("select system_key from screen_buttons order by id")
    seeded = {b.system_key for screen in defaults.SYSTEM_SCREENS for b in screen.buttons}
    assert {r["system_key"] for r in rows} == seeded and {"admin", "home"} <= seeded
    assert not {"settings", "plans"} & seeded  # one staff entry on home: «🛠 Админка»
    assert screen_buttons.name == "screen_buttons"


# ---------------------------------------------------------------- per-user flood limits (review fixes)


async def test_start_spam_is_bounded(db: CountingDatabase) -> None:
    """/start spam must not queue unbounded show() calls behind the user's lock (shared handler slots)."""
    async with make_env(db) as env:
        user = await env.add(111)
        runs = 0

        @env.router.screen("slow")
        async def slow(ctx: ScreenCtx, arg: Any) -> View:
            nonlocal runs
            runs += 1
            await asyncio.sleep(0.05)
            return View(text="ok")

        n = MAX_WAITING_PER_USER + 7
        results = await asyncio.gather(*(env.router.show(user, 111, "slow", new=True) for _ in range(n)))
        assert runs == MAX_WAITING_PER_USER
        assert results.count(False) == n - MAX_WAITING_PER_USER
        assert env.router._locks.waiting(111) == 0  # nothing left behind


async def test_form_input_flood_is_bounded(db: CountingDatabase) -> None:
    async with make_env(db, answer_deadline=0.01) as env:
        await env.add(111)

        @env.router.action("home", "work")
        async def work(ctx: ScreenCtx, arg: Any) -> None:
            await asyncio.sleep(0.1)

        busy = asyncio.create_task(env.router.dispatch_callback(callback(111, "v1:home:work")))
        await asyncio.sleep(0.02)
        n = MAX_WAITING_PER_USER + 5
        results = await asyncio.gather(
            *(env.router.dispatch_message(text_message(111, "x", message_id=600 + i)) for i in range(n))
        )
        await busy
        # one slot is the running click; the queued messages found no form (False), the rest were dropped
        assert results.count(False) == MAX_WAITING_PER_USER - 1
        assert results.count(True) == n - (MAX_WAITING_PER_USER - 1)
        assert env.router._locks.waiting(111) == 0


async def test_waiting_for_the_user_lock_is_bounded(db: CountingDatabase) -> None:
    async with make_env(db, answer_deadline=0.01, lock_wait=0.05) as env:
        user = await env.add(111)

        @env.router.action("home", "work")
        async def work(ctx: ScreenCtx, arg: Any) -> None:
            await asyncio.sleep(0.5)

        busy = asyncio.create_task(env.router.dispatch_callback(callback(111, "v1:home:work", cq_id="a")))
        await asyncio.sleep(0.02)
        loop = asyncio.get_running_loop()
        started = loop.time()
        await env.router.dispatch_callback(callback(111, "v1:home:o", cq_id="b"))
        assert await env.router.show(user, 111, "home") is False
        assert loop.time() - started < 0.4
        assert any(
            a.callback_query_id == "b" and "Подождите" in (a.text or "") for a in env.transport.answers
        )
        await busy
        assert env.router._locks.waiting(111) == 0


# ---------------------------------------------------------------- Telegram flood control (429)


def _retry_after(method: Any, seconds: int) -> TelegramRetryAfter:
    return TelegramRetryAfter(method=method, message="Too Many Requests", retry_after=seconds)


async def test_short_429_is_retried_once_and_not_reported(env: Env) -> None:
    await env.add(111)
    env.transport.fail_next(EditMessageText, _retry_after(EditMessageText(text="x"), 0))
    await env.router.dispatch_callback(callback(111, "v1:home:o"))
    assert len(env.transport.of(EditMessageText)) == 2
    assert env.hub.captured == []


async def test_long_429_shows_a_toast_without_report_or_fallback(env: Env) -> None:
    await env.add(111)

    @env.router.action("home", "go2")
    async def go2(ctx: ScreenCtx, arg: Any) -> View:
        return View(text="result")

    env.transport.fail_next(EditMessageText, _retry_after(EditMessageText(text="x"), 30))
    loop = asyncio.get_running_loop()
    started = loop.time()
    await env.router.dispatch_callback(callback(111, "v1:home:go2"))
    assert loop.time() - started < 1  # no 30 s sleep under the user's lock
    assert env.hub.captured == []
    assert len(env.transport.of(EditMessageText)) == 1  # no fallback screen hammering a throttled chat
    # the click was answered before rendering (answer-first), so no error toast replaces it
    assert env.transport.toasts == [None]


async def test_429_before_the_answer_gets_a_flood_toast(env: Env) -> None:
    await env.add(111)

    @env.router.action("home", "direct")
    async def direct(ctx: ScreenCtx, arg: Any) -> None:
        raise _retry_after(SendMessage(chat_id=111, text="x"), 30)  # the handler's own Telegram call

    await env.router.dispatch_callback(callback(111, "v1:home:direct"))
    assert env.transport.toasts == [FLOOD]
    assert env.hub.captured == [] and env.transport.calls == []


async def test_429_in_show_and_form_input_is_not_reported(env: Env) -> None:
    user = await env.add(111)
    env.transport.fail_next(SendMessage, _retry_after(SendMessage(chat_id=111, text="x"), 30))
    assert await env.router.show(user, 111, "home", new=True) is True
    assert env.hub.captured == []
    assert env.transport.of(SendMessage) and env.transport.answers == []

    # form input: the reply to the text message hits flood control
    async def done(ctx: ScreenCtx, data: dict[str, Any]) -> View:
        return View(text="done")

    form = env.router.form(Form(name="t.name", fields=(Field("name", "Имя?"),), on_done=done))

    @env.router.action("home", "ask")
    async def ask(ctx: ScreenCtx, arg: Any) -> View:
        return await ctx.start_form(form.name)

    await env.router.dispatch_callback(callback(111, "v1:home:ask"))
    sends = len(env.transport.of(SendMessage))
    env.transport.fail_next(SendMessage, _retry_after(SendMessage(chat_id=111, text="x"), 30))
    assert await env.router.dispatch_message(text_message(111, "Вася")) is True
    assert len(env.transport.of(SendMessage)) == sends + 1  # one attempt, no fallback
    assert env.hub.captured == []


async def test_fallback_after_error_is_time_bounded(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    await env.add(111)
    monkeypatch.setattr(router_mod, "FALLBACK_TIMEOUT", 0.1)

    @env.router.screen("boom")
    async def boom(ctx: ScreenCtx, arg: Any) -> View:
        raise ZeroDivisionError("broken")

    env.transport.delay[EditMessageText] = 30  # the fallback edit hangs (a stuck Telegram request)
    loop = asyncio.get_running_loop()
    started = loop.time()
    await env.router.dispatch_callback(callback(111, "v1:boom:o"))
    assert loop.time() - started < 1
    assert len(env.hub.captured) == 1
    assert env.router._locks.waiting(111) == 0


async def test_fallback_does_not_wait_out_429(env: Env) -> None:
    await env.add(111)

    @env.router.screen("boom")
    async def boom(ctx: ScreenCtx, arg: Any) -> View:
        raise ZeroDivisionError("broken")

    env.transport.fail_next(EditMessageText, _retry_after(EditMessageText(text="x"), 1))
    await env.router.dispatch_callback(callback(111, "v1:boom:o"))
    assert len(env.transport.of(EditMessageText)) == 1  # the fallback gave up instead of sleeping
    assert len(env.hub.captured) == 1  # only the original error


# ---------------------------------------------------------------- BotTransport


class _FakeBot:
    id = 42

    def __init__(self, *, hang: bool = False) -> None:
        self.hang = hang
        self.timeouts: list[int | None] = []

    async def __call__(self, method: Any, request_timeout: int | None = None) -> Any:
        self.timeouts.append(request_timeout)
        if self.hang:
            await asyncio.sleep(30)
        return True


class _Holder:
    me = None

    def __init__(self, bot: _FakeBot) -> None:
        self.bot = bot

    def get(self) -> Any:
        return self.bot


async def test_bot_transport_bounds_requests() -> None:
    bot = _FakeBot()
    transport = BotTransport(_Holder(bot), request_timeout=10, answer_timeout=3)
    await transport.call(DeleteMessage(chat_id=1, message_id=2), chat_id=1)
    await transport.answer(AnswerCallbackQuery(callback_query_id="q"))
    assert bot.timeouts == [10, 3]  # never aiogram's 60 s default
    hanging = BotTransport(_Holder(_FakeBot(hang=True)), request_timeout=0.05)
    with pytest.raises(TimeoutError):
        await hanging.call(DeleteMessage(chat_id=1, message_id=2), chat_id=1)
    with pytest.raises(ValueError, match="positive"):
        BotTransport(_Holder(bot), request_timeout=0)
