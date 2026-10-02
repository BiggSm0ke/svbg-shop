"""The whole application: start/stop, /start → home screen, owner gate, health endpoints, startup failures."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from aiohttp import ClientSession

from svbg.app import App, AppError, AppOptions, run
from svbg.core.component import Health
from tests.e2e.conftest import OWNER_ID, AppEnv, StartApp, counting_db, make_database

pytestmark = pytest.mark.pg


async def test_start_shows_home_screen_and_registers_user(start_app: StartApp, app_env: AppEnv) -> None:
    tg = app_env.tg
    app = await start_app()
    assert app.startup_seconds is not None and app.startup_seconds < 5.0  # 07 §5 stage 0: start ≤ 5 s

    tg.push_message(1001, "/start")
    # every screen shows the default banner: the home card is a photo with the text as its caption
    call = await tg.wait_for("sendPhoto", lambda c: c.params.get("chat_id") == 1001 and c.ok, timeout=10)
    assert "Привет" in call.params["caption"]  # stage 2: the user path home card
    assert call.params.get("caption_entities"), "home text keeps its entities (bold title)"
    assert (app_env.data_dir / "media").is_dir() and any((app_env.data_dir / "media").rglob("*.jpg"))

    db = counting_db(app.db)
    sql = (
        "select u.role, s.main_msg_id from users u left join ui_state s on s.user_id = u.id "
        "where u.telegram_id = 1001"
    )
    rows: list[Any] = []

    async def main_message_stored() -> bool:
        rows[:] = await db.raw(sql)
        return bool(rows) and rows[0]["main_msg_id"] == call.result["message_id"]

    for _ in range(100):
        if await main_message_stored():
            break
        await asyncio.sleep(0.05)
    assert len(rows) == 1
    assert rows[0]["role"] == "user"
    assert rows[0]["main_msg_id"] == call.result["message_id"]

    # The bot keeps answering: a second /start sends a fresh main message.
    start = len(tg.calls)
    tg.push_message(1001, "/start")
    again = await tg.wait_for("sendPhoto", lambda c: c.params.get("chat_id") == 1001, timeout=10, start=start)
    # the banner was uploaded once: the next screen sends Telegram's file_id, not the file
    assert str(call.params.get("photo")).startswith("attach://")
    assert again.params.get("photo") == call.result["photo"][-1]["file_id"]

    health = await app.components.health_all()
    assert health["bot"].status is Health.OK


async def test_owner_sees_the_admin_button_and_user_does_not(start_app: StartApp, app_env: AppEnv) -> None:
    tg = app_env.tg
    await start_app()
    tg.push_message(OWNER_ID, "/start")
    owner_call = await tg.wait_for("sendPhoto", lambda c: c.params.get("chat_id") == OWNER_ID, timeout=10)
    tg.push_message(2002, "/start")
    user_call = await tg.wait_for("sendPhoto", lambda c: c.params.get("chat_id") == 2002, timeout=10)

    def labels(call: object) -> list[str]:
        markup = call.params.get("reply_markup") or {}  # type: ignore[attr-defined]
        return [b["text"] for row in markup.get("inline_keyboard", []) for b in row]

    assert [label for label in labels(owner_call) if "Админка" in label] == ["🛠 Админка"]
    assert not any("Настройки" in label or "Тарифы" in label for label in labels(owner_call))  # one entry
    assert not any("Админка" in label for label in labels(user_call))


async def test_without_owner_users_see_setup_notice(start_app: StartApp, app_env: AppEnv) -> None:
    app_env.write(OWNER_IDS=None)
    tg = app_env.tg
    await start_app()
    tg.push_message(3003, "/start")
    call = await tg.wait_for("sendMessage|sendPhoto", lambda c: c.params.get("chat_id") == 3003, timeout=10)
    assert "настраивается" in call.text


async def test_health_and_ready_are_served_locally(start_app: StartApp) -> None:
    app = await start_app()
    assert app.web is not None
    async with ClientSession() as session:
        async with session.get(f"{app.web.url}/health") as resp:
            assert resp.status == 200
        async with session.get(f"{app.web.url}/ready") as resp:
            assert resp.status == 200
            body = await resp.json()
            assert body["ready"] is True
            assert body["components"]["bot"] == "ok"


async def test_optional_modules_are_wired_or_reported(start_app: StartApp) -> None:
    app = await start_app()
    for name in app.options.optional_modules:
        assert (name in app.wired_modules) != (name in app.missing_modules)


async def test_bad_token_keeps_process_alive_and_reports_down(start_app: StartApp, app_env: AppEnv) -> None:
    app_env.write(BOT_TOKEN="123456:" + "x" * 35)  # well-formed, but the fake Telegram does not know it
    app = await start_app()
    report = await app.components.health("bot")
    assert report.status is Health.DOWN
    assert app.web is not None and app.web.running
    async with ClientSession() as session, session.get(f"{app.web.url}/ready") as resp:
        assert resp.status == 503


async def test_waits_for_token_then_starts(app_env: AppEnv, tmp_path: object) -> None:
    app_env.write(BOT_TOKEN=None)
    app = App(app_env.options(token_poll_interval=0.1))
    task = asyncio.create_task(app.start())
    try:
        await asyncio.sleep(0.5)
        assert not task.done(), "start() waits for BOT_TOKEN"
        text = app_env.env_path.read_text(encoding="utf-8")
        assert "TRIAL_DAYS=" in text, "the full .env template is written while waiting"
        assert "BOT_TOKEN=" in text
        from svbg.boot.envfile import EnvDocument, write_atomic

        doc = EnvDocument.parse(text)
        doc.set("BOT_TOKEN", app_env.token)
        write_atomic(app_env.env_path, doc.render())
        await asyncio.wait_for(task, timeout=10)
        assert app.running
        assert app.holder.get() is not None
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await app.stop()


async def test_database_url_from_environ_survives_the_first_start_template(app_env: AppEnv) -> None:
    """Typical docker setup: DATABASE_URL in compose, the owner pastes BOT_TOKEN into the file."""
    from svbg.boot.envfile import EnvDocument, write_atomic

    app_env.write(BOT_TOKEN=None, DATABASE_URL=None)
    environ = {"DATA_DIR": str(app_env.data_dir), "DATABASE_URL": app_env.dsn}
    app = App(app_env.options(token_poll_interval=0.1, environ=environ))
    task = asyncio.create_task(app.start())
    try:
        await asyncio.sleep(0.5)
        assert not task.done()
        doc = EnvDocument.parse(app_env.env_path.read_text(encoding="utf-8"))
        assert not doc.get("DATABASE_URL")  # the template has an empty line for it
        doc.set("BOT_TOKEN", app_env.token)
        write_atomic(app_env.env_path, doc.render())
        await asyncio.wait_for(task, timeout=10)
        assert app.running and app.boot is not None
        assert app.boot.database_url == app_env.dsn
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await app.stop()


async def test_file_rewritten_while_waiting_for_token_gets_a_new_secret_key(app_env: AppEnv) -> None:
    from svbg.boot.envfile import EnvDocument, write_atomic

    app_env.write(BOT_TOKEN=None)
    app = App(app_env.options(token_poll_interval=0.1))
    task = asyncio.create_task(app.start())
    try:
        await asyncio.sleep(0.5)
        assert not task.done()
        doc = EnvDocument.parse("")  # `echo BOT_TOKEN=… > data/.env`: SECRET_KEY and the rest are gone
        for key in ("BOT_TOKEN", "TELEGRAM_API_URL", "DATABASE_URL", "OWNER_IDS"):
            doc.set(key, app_env.token if key == "BOT_TOKEN" else app_env.values[key])
        write_atomic(app_env.env_path, doc.render())
        await asyncio.wait_for(task, timeout=10)
        assert app.running and app.boot is not None and app.boot.secret_key
        saved = EnvDocument.parse(app_env.env_path.read_text(encoding="utf-8")).get("SECRET_KEY")
        assert saved == app.boot.secret_key
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await app.stop()


async def test_no_token_without_waiting_is_a_config_error(app_env: AppEnv) -> None:
    app_env.write(BOT_TOKEN=None)
    with pytest.raises(AppError, match="BOT_TOKEN"):
        await App(app_env.options(wait_for_token=False)).start()


async def test_missing_database_url_is_a_config_error(app_env: AppEnv) -> None:
    app_env.write(DATABASE_URL=None)
    with pytest.raises(AppError, match="DATABASE_URL"):
        await App(app_env.options()).start()


async def test_unreachable_database_fails_fast_without_leaking_password(app_env: AppEnv) -> None:
    app_env.write(DATABASE_URL="postgresql://svbg:TopSecretPw1@127.0.0.1:1/svbg")
    began = time.monotonic()
    with pytest.raises(AppError) as info:
        await App(app_env.options(db_connect_timeout=1.0)).start()
    assert time.monotonic() - began < 10
    assert "TopSecretPw1" not in str(info.value)
    assert "База данных недоступна" in str(info.value)


async def test_run_stops_gracefully_on_stop_event(app_env: AppEnv) -> None:
    stop = asyncio.Event()
    options: AppOptions = app_env.options()
    task = asyncio.create_task(run(options, stop=stop))
    tg = app_env.tg
    tg.push_message(4004, "/start")  # delivered as soon as polling starts
    await tg.wait_for("sendPhoto", lambda c: c.params.get("chat_id") == 4004, timeout=15)  # home + banner
    stop.set()
    await asyncio.wait_for(task, timeout=15)
    # After the stop nothing talks to Telegram any more (the long poll was cancelled, not awaited).
    count = len(tg.calls)
    await asyncio.sleep(0.5)
    assert [c.method for c in tg.calls[count:]] == []


async def test_stop_is_idempotent_and_start_after_failure_cleans_up(app_env: AppEnv) -> None:
    app = App(app_env.options())
    await app.start()
    await app.stop()
    await app.stop()
    assert not app.running
    # The database pool was closed: a fresh database object can still connect (no leaked locks/pools).
    db = make_database(app_env.dsn)
    await db.start()
    await db.close()
