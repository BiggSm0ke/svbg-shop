"""Smaller pieces of the composition root: options, key derivation, owner DM sink, user directory,
module wiring isolation, cancellation (Ctrl+C on Windows) and the offline ``.env`` renderer."""

from __future__ import annotations

import asyncio
import types
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from aiogram.types import User as TgUser

from svbg.app import App, AppError, AppOptions, OwnerDmSink, derive_key, render_env_offline, run
from svbg.boot.envfile import EnvDocument
from svbg.core.errors import ErrorGroupView
from svbg.tg.user.directory import UserDirectory
from tests.dbkit import CountingDatabase, open_db
from tests.e2e.conftest import OWNER_ID, AppEnv, StartApp

# ------------------------------------------------------------------ options & keys


def test_options_from_environ_defaults_and_overrides(tmp_path: Path) -> None:
    opts = AppOptions.from_environ({"DATA_DIR": str(tmp_path)})
    assert opts.env_path == tmp_path / ".env"
    assert (opts.web_host, opts.web_port, opts.log_json) == ("0.0.0.0", 8080, False)
    opts = AppOptions.from_environ(
        {
            "DATA_DIR": str(tmp_path),
            "SVBG_WEB_PORT": "9000",
            "SVBG_WEB_HOST": "127.0.0.1",
            "SVBG_LOG_JSON": "1",
        }
    )
    assert (opts.web_host, opts.web_port, opts.log_json) == ("127.0.0.1", 9000, True)
    for bad in ("abc", "70000", "-1"):
        with pytest.raises(AppError, match="SVBG_WEB_PORT"):
            AppOptions.from_environ({"SVBG_WEB_PORT": bad})


def test_derive_key_is_stable_and_purpose_bound() -> None:
    master = "k" * 44
    assert derive_key(master, "callback-codec") == derive_key(master, "callback-codec")
    assert derive_key(master, "callback-codec") != derive_key(master, "other")
    assert derive_key("x" * 44, "callback-codec") != derive_key(master, "callback-codec")
    assert len(derive_key(master, "callback-codec")) == 32
    assert master.encode() not in derive_key(master, "callback-codec")


# ------------------------------------------------------------------ owner DM sink


class FakeNotifier:
    def __init__(self, fail_for: set[int] | None = None) -> None:
        self.sent: list[tuple[int, str, dict[str, Any]]] = []
        self.calls: list[Any] = []
        self.fail_for = fail_for or set()
        self._ids = iter(range(100, 10_000))

    async def send(self, chat_id: int, text: str, **kw: Any) -> Any:
        if chat_id in self.fail_for:
            from svbg.tg.notifier import NotifierError

            raise NotifierError("down")
        self.sent.append((chat_id, text, kw))
        return types.SimpleNamespace(message_id=next(self._ids))

    async def call(self, method: Any, **kw: Any) -> Any:
        self.calls.append((method, kw))
        return True


def _view() -> ErrorGroupView:
    now = datetime(2026, 10, 1, tzinfo=UTC)
    return ErrorGroupView(
        fingerprint="f" * 40,
        place="screen:x",
        title="Ошибка",
        hint="Проверьте",
        first_seen=now,
        last_seen=now,
    )


async def test_owner_sink_sends_to_every_owner_and_edits_in_place() -> None:
    notifier = FakeNotifier(fail_for={3})

    async def owners() -> frozenset[int]:
        return frozenset({1, 2, 3})

    sink = OwnerDmSink(notifier, owners)  # type: ignore[arg-type]
    ref = await sink.send_new(_view())
    assert ref is not None and set(ref["dm"]) == {"1", "2"}  # owner 3 failed, the others still got it
    assert all(kw["parse_mode"] == "HTML" for _, _, kw in notifier.sent)
    await sink.update(_view(), ref)
    assert len(notifier.calls) == 2
    await sink.update(_view(), {"garbage": True})
    await sink.update(_view(), None)
    assert len(notifier.calls) == 2


async def test_owner_sink_without_owners_returns_none() -> None:
    async def owners() -> frozenset[int]:
        return frozenset()

    sink = OwnerDmSink(FakeNotifier(), owners)  # type: ignore[arg-type]
    assert await sink.send_new(_view()) is None


# ------------------------------------------------------------------ user directory


class _Settings:
    def __init__(self, **values: Any) -> None:
        self.values = {"OWNER_IDS": [], "DEFAULT_LANGUAGE": "ru", "CURRENCY": "RUB", **values}

    def current(self) -> dict[str, Any]:
        return self.values


def _tg(user_id: int, *, is_bot: bool = False, username: str | None = "user") -> TgUser:
    return TgUser(id=user_id, is_bot=is_bot, first_name="Имя", username=username)


@pytest.fixture
async def db(e2e_dsn: str) -> AsyncIterator[CountingDatabase]:
    async with open_db(e2e_dsn, schema=False) as database:  # migrated by the ``e2e_dsn`` fixture
        yield database


@pytest.mark.pg
async def test_user_directory_registers_caches_and_respects_roles(db: CountingDatabase) -> None:
    settings = _Settings(OWNER_IDS=[OWNER_ID])
    users = UserDirectory(db, settings)
    first = await users.load(_tg(42))
    assert first is not None and first.is_new and first.role == "user" and first.lang == "ru"
    q = db.queries
    again = await users.load(_tg(42))
    assert again == first and db.queries == q, "a cache hit costs no SQL"
    users.invalidate(42)
    third = await users.load(_tg(42))
    assert third is not None and not third.is_new and third.user_id == first.user_id

    owner = await users.load(_tg(OWNER_ID))
    assert owner is not None and owner.role == "owner"
    assert await users.load(_tg(5, is_bot=True)) is None

    await db.raw("update users set banned_at = now() where telegram_id = 42")
    users.invalidate(42)
    assert await users.load(_tg(42)) is None

    await db.raw("insert into users (telegram_id, role) values (99, 'owner')")
    assert await users.owner_ids() == frozenset({OWNER_ID, 99})
    assert await users.has_owner()


@pytest.mark.pg
async def test_user_directory_blocked_flag_round_trip(db: CountingDatabase) -> None:
    users = UserDirectory(db, _Settings())
    await users.load(_tg(7))
    await users.mark_blocked(7)
    rows = await db.raw("select bot_blocked_at from users where telegram_id = 7")
    assert rows[0]["bot_blocked_at"] is not None
    await users.mark_blocked(-100123)  # groups are ignored
    await users.load(_tg(7))  # the user wrote again: not blocked any more
    rows = await db.raw("select bot_blocked_at from users where telegram_id = 7")
    assert rows[0]["bot_blocked_at"] is None


@pytest.mark.pg
async def test_user_directory_lru_is_bounded(db: CountingDatabase) -> None:
    clock = [0.0]
    users = UserDirectory(db, _Settings(), cache_size=3, ttl=10, clock=lambda: clock[0])
    for uid in (1, 2, 3, 4):
        await users.load(_tg(uid))
    q = db.queries
    await users.load(_tg(1))  # evicted (oldest)
    assert db.queries == q + 1
    clock[0] = 11  # expired
    q = db.queries
    await users.load(_tg(4))
    assert db.queries == q + 1


def test_user_directory_validates_arguments() -> None:
    with pytest.raises(ValueError):
        UserDirectory(object(), _Settings(), cache_size=0)  # type: ignore[arg-type]


# ------------------------------------------------------------------ wiring isolation & cancellation


@pytest.mark.pg
async def test_broken_optional_module_does_not_stop_the_bot(
    start_app: StartApp, app_env: AppEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pkg = tmp_path / "plug"
    pkg.mkdir()
    (pkg / "broken_mod.py").write_text("def setup(router, deps):\n    raise RuntimeError('boom')\n")
    (pkg / "nosetup_mod.py").write_text("X = 1\n")
    (pkg / "good_mod.py").write_text(
        "from aiogram import Router\nCALLS = []\n"
        "def setup(router, deps):\n    CALLS.append((router, deps))\n    return Router(name='good')\n"
    )
    monkeypatch.syspath_prepend(str(pkg))
    app = await start_app(optional_modules=("broken_mod", "nosetup_mod", "good_mod", "not_installed_mod"))
    assert app.wired_modules == ["good_mod"]
    assert set(app.missing_modules) == {"broken_mod", "nosetup_mod", "not_installed_mod"}
    import good_mod  # type: ignore[import-not-found]

    router, deps = good_mod.CALLS[0]
    assert router is app.screens and deps is app.deps
    tg = app_env.tg
    tg.push_message(6006, "/start")
    await tg.wait_for("sendPhoto", lambda c: c.params.get("chat_id") == 6006, timeout=10)  # home + banner


@pytest.mark.pg
async def test_module_stop_hooks_run_first_and_the_bus_is_drained(
    app_env: AppEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pkg = tmp_path / "plug2"
    pkg.mkdir()
    (pkg / "stopping_mod.py").write_text(
        "EVENTS = []\n"
        "def setup(router, deps):\n"
        "    async def stop():\n"
        "        EVENTS.append(('module', deps.db is not None, deps.mirror()._started))\n"
        "    deps.on_stop('stopping mod', stop)\n"
    )
    monkeypatch.syspath_prepend(str(pkg))
    app = App(app_env.options(optional_modules=("svbg.tg.admin.settings", "stopping_mod")))
    await app.start()
    import stopping_mod  # type: ignore[import-not-found]

    assert [name for name, _ in app._module_stops] == ["settings ui", "stopping mod"]
    drained: list[float | None] = []

    async def bus_drain(grace_s: float | None = None) -> None:
        drained.append(grace_s)

    monkeypatch.setattr(app.bus, "drain", bus_drain)
    await app.stop()
    # module steps ran first: the mirror (and everything else) was still up
    assert stopping_mod.EVENTS == [("module", True, True)]
    assert drained == [5.0]


@pytest.mark.pg
async def test_cancelled_run_still_stops_the_app(app_env: AppEnv) -> None:
    """Ctrl+C on Windows cancels the main task: ``run`` must still stop every component."""
    task = asyncio.create_task(run(app_env.options()))
    await app_env.tg.wait_for("getMe", timeout=10)
    await asyncio.sleep(0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=20)
    count = len(app_env.tg.calls)
    await asyncio.sleep(0.5)
    assert len(app_env.tg.calls) == count  # nothing polls Telegram any more


@pytest.mark.pg
async def test_startup_report_reaches_the_owner(start_app: StartApp, app_env: AppEnv) -> None:
    await start_app(notify_owners_on_start=True)
    call = await app_env.tg.wait_for("sendMessage", lambda c: c.params.get("chat_id") == OWNER_ID, timeout=10)
    assert "запущен" in call.params["text"]
    assert app_env.token not in call.params["text"]


async def test_app_stop_before_start_is_noop(tmp_path: Path) -> None:
    app = App(AppOptions(env_path=tmp_path / ".env", environ={}))
    await app.stop()
    assert not app.running


# ------------------------------------------------------------------ offline .env


def test_render_env_offline_keeps_values_and_unknown_keys(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("TRIAL_DAYS=12\nCUSTOM_THING=1\n# заметка\n", encoding="utf-8")
    text = render_env_offline(path, {})
    doc = EnvDocument.parse(text)
    assert doc.get("TRIAL_DAYS") == "12"
    assert doc.get("CUSTOM_THING") == "1"
    assert doc.get("LOG_LEVEL") == "INFO"
    assert render_env_offline(path, {}) == render_env_offline(path, {})  # deterministic


def test_render_env_offline_marks_locked_keys(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("LOCKED_KEYS=TRIAL_DAYS\n", encoding="utf-8")
    text = render_env_offline(path, {"TRIAL_DAYS": "4"})
    assert "LOCKED_KEYS" in text
    block = text[: text.index("TRIAL_DAYS=")].rsplit("\n\n", 1)[-1]
    assert "🔒 Задано окружением контейнера" in block


# ------------------------------------------------------------------ access-denied audit


class _RecordingDb:
    """``tx()`` that records executed statements (only what ``App._on_denied`` needs)."""

    def __init__(self) -> None:
        self.statements: list[Any] = []

    def tx(self) -> Any:
        db = self

        class _Tx:
            async def __aenter__(self) -> Any:
                return self

            async def __aexit__(self, *_exc: object) -> None:
                return None

            async def execute(self, stmt: Any) -> None:
                db.statements.append(stmt)

        return _Tx()


async def test_access_denied_audit_only_for_staff_and_throttled(tmp_path: Path) -> None:
    from svbg.tg.ui.context import UserCtx

    app = App(AppOptions(env_path=tmp_path / ".env", environ={}))
    db = _RecordingDb()
    app.db = db  # type: ignore[assignment]
    # Anyone can forge callback_data: an ordinary user's denials never reach admin_audit.
    for i in range(50):
        await app._on_denied(UserCtx(user_id=1, role="user"), f"screen:settings_root:{i}")
    assert db.statements == []
    support = UserCtx(user_id=2, role="support")
    for _ in range(20):
        await app._on_denied(support, "screen:settings_root")
    assert len(db.statements) == 1  # one row per staff member and place per minute
    await app._on_denied(support, "screen:other")
    await app._on_denied(UserCtx(user_id=3, role="admin"), "screen:settings_root")
    assert len(db.statements) == 3
    params = db.statements[0].compile().params
    assert (params["actor_id"], params["role"], params["action"]) == (2, "support", "access_denied")


def test_throttle_is_bounded() -> None:
    from svbg.app import _Throttle

    throttle = _Throttle(60.0, 8)
    assert all(throttle.allow(i) for i in range(100))
    assert len(throttle._seen) <= 8
    assert not throttle.allow(99)  # recent keys are still remembered


# ------------------------------------------------------------------ database component (/ready)


class _BrokenDb:
    def __init__(self, error: BaseException | None = None, *, hang: bool = False) -> None:
        self.error = error
        self.hang = hang

    def read(self) -> Any:
        broken = self

        class _Conn:
            async def __aenter__(self) -> Any:
                if broken.hang:
                    await asyncio.sleep(3600)
                if broken.error is not None:
                    raise broken.error
                return self

            async def __aexit__(self, *_exc: object) -> None:
                return None

        return _Conn()


async def test_database_component_reports_outage_as_down() -> None:
    import sqlalchemy as sa

    from svbg.app import DatabaseComponent
    from svbg.core.component import Component, Health

    comp = DatabaseComponent(_BrokenDb(OSError("refused")))  # type: ignore[arg-type]
    assert isinstance(comp, Component) and comp.name == "database"
    report = await comp.health()
    assert report.status is Health.DOWN and "OSError" in report.summary
    db_error = sa.exc.OperationalError("select 1", {}, Exception("server closed the connection"))
    report = await DatabaseComponent(_BrokenDb(db_error)).health()  # type: ignore[arg-type]
    assert report.status is Health.DOWN
    hung = DatabaseComponent(_BrokenDb(hang=True), timeout=0.05)  # type: ignore[arg-type]
    report = await asyncio.wait_for(hung.health(), 2)
    assert report.status is Health.DOWN  # not UNKNOWN: /ready must turn red
    assert await comp.probe({}) is None and await comp.reconfigure({}) is None


@pytest.mark.pg
async def test_ready_turns_red_when_the_database_goes_away(start_app: StartApp) -> None:
    from aiohttp import ClientSession

    from svbg.core.component import Health

    app = await start_app()
    assert app.web is not None and app.db is not None
    report = await app.components.health("database")
    assert report.status is Health.OK
    real_read = app.db.read
    app.db.read = _BrokenDb(OSError("connection refused")).read  # type: ignore[method-assign]
    try:
        async with ClientSession() as session, session.get(f"{app.web.url}/ready") as resp:
            assert resp.status == 503
            body = await resp.json()
            assert body["components"]["database"] == "down"
    finally:
        app.db.read = real_read  # type: ignore[method-assign]


# ------------------------------------------------------------------ shutdown budgets


async def test_stop_budgets_fit_into_one_stop_step(start_app: StartApp) -> None:
    app = await start_app(stop_timeout=15.0)
    assert app.runner is not None
    # confirm offset (≤ 5 s) + drain + hooks/sessions must fit into the bot's 15 s step
    assert app.runner.drain_timeout + 5.0 < 15.0
    from svbg.app import _inner_budget

    assert _inner_budget(15.0) == 12.0  # the job worker's wait inside its 15 s step
    assert 0 < _inner_budget(0.5) < 0.5


async def test_stop_releases_running_jobs_before_the_database_closes(start_app: StartApp) -> None:
    import sqlalchemy as sa

    from svbg.jobs import Job, JobContext, enqueue
    from svbg.jobs.tables import jobs

    started = asyncio.Event()

    async def stuck(job: Job, ctx: JobContext) -> None:
        started.set()
        await asyncio.sleep(3600)

    app = await start_app(stop_timeout=1.0)
    assert app.worker is not None and app.db is not None and app.boot is not None
    assert app.boot.database_url is not None
    app.worker.register("stuck", stuck)
    async with app.db.tx() as conn:
        job_id = await enqueue(conn, "stuck", {}, lane="interactive")
    app.worker.wake()
    await asyncio.wait_for(started.wait(), 10)
    await app.stop()
    probe = CountingDatabase(app.boot.database_url)
    await probe.start()
    try:
        query = sa.select(jobs.c.status, jobs.c.attempts).where(jobs.c.id == job_id)
        async with probe.read() as conn:
            row = (await conn.execute(query)).one()
        assert (row[0], row[1]) == ("ready", 0)  # handed back, not left running with a burned attempt
    finally:
        await probe.close()
