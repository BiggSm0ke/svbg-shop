"""Ops screen and buttons through the real router: rights on every click, toasts, background work, wiring."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message

from svbg.core.settings.registry import Registry, core_registry
from svbg.core.settings.service import Change
from svbg.ops.daily_report import DailyReport
from svbg.ops.module import A_BACKUP, A_REPORT, A_UPDATES, ACTIONS, SCREEN, OpsModule, setup
from svbg.ops.settings import OPS_SETTINGS
from svbg.ops.state import K_BACKUP, MetaState
from svbg.ops.updates import UpdateChecker
from svbg.tg.ui.codec import encode
from tests.dbkit import CountingDatabase, open_db
from tests.tg.admin.settings_harness import SEnv, build_senv
from tests.tg.ui.ui_harness import DATE, tg_user

pytestmark = pytest.mark.pg

OWNER, STATS, NOSTATS, SUPPORT, USER = 1001, 2002, 3003, 4004, 5005
GROUP = -1009876543210


def ops_registry() -> Registry:
    reg = core_registry()
    for d in OPS_SETTINGS:
        reg.add(d)
    return reg


@dataclass
class FakeBackups:
    running: bool = False
    runs: list[str] = field(default_factory=list)
    gate: asyncio.Event = field(default_factory=asyncio.Event)

    async def run(self, reason: str = "manual") -> None:
        self.runs.append(reason)
        self.running = True
        try:
            await self.gate.wait()
        finally:
            self.running = False

    async def tick(self) -> bool:
        return False


@dataclass
class Calls:
    posts: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    sends: list[tuple[int, str, dict[str, Any]]] = field(default_factory=list)

    async def post(self, kind: str, text: str, **kw: Any) -> None:
        self.posts.append((kind, text if isinstance(text, str) else text.html(), kw))

    async def send(self, chat_id: int, text: str, **kw: Any) -> None:
        self.sends.append((chat_id, text if isinstance(text, str) else text.html(), kw))


@dataclass
class GitHub:
    payload: Any = field(default_factory=list)

    async def __call__(self, _url: str) -> Any:
        return self.payload


@dataclass
class Ops:
    env: SEnv
    module: OpsModule
    backups: FakeBackups
    calls: Calls
    github: GitHub

    async def click(self, tg_id: int, data: str) -> None:
        await self.env.click(tg_id, data)

    @property
    def toast(self) -> str | None:
        return self.env.toasts[-1]


@pytest.fixture
async def db(pg_dsn: str) -> AsyncIterator[CountingDatabase]:
    async with open_db(pg_dsn) as database:
        yield database


@pytest.fixture
async def ops(db: CountingDatabase, tmp_path: Path) -> AsyncIterator[Ops]:
    env = await build_senv(db, tmp_path / ".env", registry=ops_registry())
    await env.add(OWNER, "owner")
    await env.add(STATS, "admin", frozenset({"stats"}))
    await env.add(NOSTATS, "admin", frozenset({"settings.business"}))
    await env.add(SUPPORT, "support")
    await env.add(USER, "user")
    calls, github, backups = Calls(), GitHub(), FakeBackups()
    state = MetaState(db)

    def settings() -> Mapping[str, Any]:
        return env.service.current()

    module = OpsModule(
        settings=settings,
        state=state,
        backups=backups,  # type: ignore[arg-type]
        report=DailyReport(db, settings=settings, post=lambda t, b: calls.post("reports", t, buttons=b)),
        updates=UpdateChecker(
            db, settings=settings, post=lambda t: calls.post("system", t), fetcher=github, state=state
        ),
        post=calls.post,
        send=calls.send,
    )
    module.install(env.router)
    try:
        yield Ops(env, module, backups, calls, github)
    finally:
        backups.gate.set()
        await module.drain(2.0)
        await env.screens.drain()


async def test_owner_screen(ops: Ops, db: CountingDatabase) -> None:
    await ops.click(OWNER, encode(SCREEN))
    text = ops.env.text
    assert "Бэкапы и обновления" in text and "Бэкап: ещё не делался" in text
    assert "каждый день в 04:00 (Europe/Moscow), хранить 7" in text
    assert "Пароль бэкапов не задан" in text
    assert "Отчёт: каждый день в 09:00" in text
    assert "Обновления: ещё не проверялось" in text
    labels = ops.env.labels()
    assert labels == [
        "💾 Бэкап сейчас",
        "📊 Отчёт сейчас",
        "🔄 Проверить обновления",
        "⚙️ Настройки",
        "⬅️ Система",
        "🛠 Админка",
    ]
    assert ops.env.button("Настройки") == encode("set.v", arg="sys.backup")  # the backup slice

    await MetaState(db).merge(
        K_BACKUP,
        {
            "last": {
                "at": "2026-10-02T01:00:00+00:00",
                "size": 5 * 1024 * 1024,
                "encrypted": True,
                "sent": 1,
            },
            "last_error": {"at": "2026-10-02T02:00:00+00:00", "error": "pg_dump <упал>"},
        },
    )
    await ops.env.service.apply([Change("BACKUP_PASSWORD", "long enough pass")], source="bot", actor_id=None)
    mark = db.queries
    view = await ops.module.render()
    assert db.queries - mark == 1, "one statement per screen render"
    assert "Последний бэкап: 02.10 04:00 · 5,0 МБ · отправлен в Telegram" in view.text
    assert "🔴 Последняя попытка не удалась (02.10 05:00): pg_dump &lt;упал&gt;" in view.text
    assert "🔐 Пароль бэкапов задан" in view.text and "long enough pass" not in view.text


@pytest.mark.parametrize("tg_id", [STATS, NOSTATS, SUPPORT, USER])
async def test_only_the_owner_sees_the_screen_and_backs_up(ops: Ops, tg_id: int) -> None:
    await ops.click(tg_id, encode(SCREEN))
    assert ops.toast == "Нет прав"
    await ops.click(tg_id, encode(ACTIONS, A_BACKUP))
    assert ops.toast == "Нет прав"
    await ops.click(tg_id, encode(ACTIONS, A_UPDATES))
    assert ops.toast == "Нет прав"
    assert ops.backups.runs == []


@pytest.mark.parametrize("tg_id", [NOSTATS, SUPPORT, USER])
async def test_report_needs_stats(ops: Ops, tg_id: int) -> None:
    await ops.click(tg_id, encode(ACTIONS, A_REPORT))
    assert ops.toast == "Нет прав"
    await ops.module.drain()
    assert ops.calls.sends == [] and ops.calls.posts == []


async def test_report_now_in_private_chat(ops: Ops, db: CountingDatabase) -> None:
    mark = db.queries
    await ops.click(STATS, encode(ACTIONS, A_REPORT))
    assert db.queries - mark <= 2, "the click itself does no report queries"
    assert ops.toast == "Готовлю отчёт…"
    await ops.module.drain()
    ((chat_id, text, kw),) = ops.calls.sends
    assert chat_id == STATS and "Отчёт за сегодня" in text and kw["parse_mode"] == "HTML"
    markup = kw["reply_markup"]
    assert isinstance(markup, InlineKeyboardMarkup)
    assert markup.inline_keyboard[0][0].callback_data == encode(ACTIONS, A_REPORT)
    await ops.click(STATS, encode(ACTIONS, A_REPORT))
    assert ops.toast == "Отчёт уже готовится, подождите полминуты"
    await ops.module.drain()
    assert len(ops.calls.sends) == 1


def _group_callback(tg_id: int, data: str) -> CallbackQuery:
    msg = Message(message_id=900, date=DATE, chat=Chat(id=GROUP, type="supergroup"), text="📊 Отчёт")
    return CallbackQuery(id="cqg", from_user=tg_user(tg_id), chat_instance="g", data=data, message=msg)


async def test_report_button_in_the_admin_group(ops: Ops) -> None:
    await ops.env.router.dispatch_callback(_group_callback(OWNER, encode(ACTIONS, A_REPORT)))
    await ops.module.drain()
    ((kind, text, kw),) = ops.calls.posts
    assert kind == "reports" and "Отчёт за сегодня" in text and kw["buttons"]
    # A group member without a bot role: «Нет прав», nothing posted.
    await ops.env.router.dispatch_callback(_group_callback(USER, encode(ACTIONS, A_REPORT)))
    assert ops.env.toasts[-1] == "Нет прав"
    await ops.module.drain()
    assert len(ops.calls.posts) == 1


async def test_backup_now_runs_in_the_background_once(ops: Ops) -> None:
    await ops.click(OWNER, encode(ACTIONS, A_BACKUP))
    assert ops.toast == "Бэкап запущен — файл придёт в «💾 Бэкапы»"
    assert ops.backups.runs == ["manual"] and ops.backups.running
    await ops.click(OWNER, encode(ACTIONS, A_BACKUP))
    assert ops.toast == "Бэкап уже выполняется"
    ops.backups.gate.set()
    await ops.module.drain()
    assert ops.backups.runs == ["manual"]


async def test_check_updates_now(ops: Ops) -> None:
    await ops.click(OWNER, encode(ACTIONS, A_UPDATES))
    assert ops.toast == "Проверяю обновления…"
    await ops.module.drain()
    assert ops.calls.sends[-1][1] == "Не задан репозиторий релизов (UPDATE_REPO)"

    await ops.env.service.apply([Change("UPDATE_REPO", "owner/svbg-shop")], source="bot", actor_id=None)
    await ops.click(OWNER, encode(ACTIONS, A_UPDATES))
    await ops.module.drain()
    assert ops.calls.sends[-1][1].startswith("✅ У вас последняя версия")

    ops.github.payload = [{"tag_name": "v9.0.0", "body": "", "html_url": ""}]
    sends = len(ops.calls.sends)
    await ops.click(OWNER, encode(ACTIONS, A_UPDATES))
    await ops.module.drain()
    assert len(ops.calls.sends) == sends, "the release message goes to «⚙️ Система» only"
    assert ops.calls.posts[-1][0] == "system" and "v9.0.0" in ops.calls.posts[-1][1]


@dataclass
class FakeScheduler:
    tasks: dict[str, float] = field(default_factory=dict)

    def every(self, name: str, interval_s: float, _fn: Callable[[], Any], **_kw: Any) -> None:
        self.tasks[name] = interval_s


async def test_setup_wires_schedule_screen_and_stop(db: CountingDatabase, tmp_path: Path) -> None:
    env = await build_senv(db, tmp_path / ".env", registry=ops_registry())
    stops: list[str] = []
    scheduler = FakeScheduler()
    deps = SimpleNamespace(
        db=db,
        settings=env.service,
        notifier=SimpleNamespace(send=Calls().send),
        holder=SimpleNamespace(get=lambda: None),
        env_path=tmp_path / "data" / ".env",
        crypto=SimpleNamespace(primary_fingerprint="abcd1234"),
        attention=None,
        admin_chat=None,
        scheduler=scheduler,
        on_stop=lambda name, _fn: stops.append(name),
        owner_ids=_owners,
    )
    setup(env.router, deps)
    assert scheduler.tasks == {"ops.backup.tick": 60.0, "ops.report.tick": 60.0, "ops.updates": 43200.0}
    assert stops == ["ops"]
    await env.add(OWNER, "owner")
    await env.click(OWNER, encode(SCREEN))
    assert "Бэкапы и обновления" in env.text
    await env.screens.drain()


async def _owners() -> frozenset[int]:
    return frozenset({OWNER})
