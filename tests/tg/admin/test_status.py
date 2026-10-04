"""«⚙️ Состояние», «Требует внимания» (fix_action / snooze), access, isolation, SQL budget."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from svbg.core.attention import AttentionService
from svbg.core.component import HealthReport, fix_screen, fix_setting
from svbg.core.errors import ErrorHub
from svbg.core.log import register_secret
from svbg.jobs import JobQueue
from svbg.services.maintenance import MaintenanceService
from svbg.tg.admin.status import (
    A_SNOOZE,
    ACTIONS,
    SCREEN,
    SCREEN_ATTENTION,
    StatusScreens,
    fix_button_target,
    rss_bytes,
)
from svbg.tg.ui.codec import encode
from tests.tg.admin.settings_harness import SEnv
from tests.tg.ui.ui_harness import text_message

OWNER, VIEWER, ADMIN_NOPERM, SUPPORT, USER = 1001, 2002, 3003, 4004, 5005


@dataclass
class Mirror:
    error: str | None = None
    writable: bool = True
    readable: bool = True
    last_write_at: datetime | None = datetime(2026, 10, 1, 11, 0, tzinfo=UTC)
    invalid: dict[str, str] = field(default_factory=dict)
    conflicts: list[str] = field(default_factory=list)


@dataclass
class MirrorHolder:
    status: Mirror = field(default_factory=Mirror)


@dataclass
class Task:
    last_error: str | None = None
    last_ok_at: datetime | None = None
    runs: int = 0


@dataclass
class Scheduler:
    items: dict[str, Task] = field(default_factory=dict)

    def tasks(self) -> dict[str, Task]:
        return self.items


@dataclass
class Panel:
    configured: bool = True
    breaker_state: Any = None
    breaker_open_since: datetime | None = None


@dataclass
class StEnv:
    env: SEnv
    screens: StatusScreens
    attention: AttentionService
    hub: ErrorHub
    queue: JobQueue
    mirror: MirrorHolder
    scheduler: Scheduler

    async def open(self, tg_id: int = OWNER, screen: str = SCREEN) -> None:
        await self.env.click(tg_id, encode(screen))

    @property
    def text(self) -> str:
        return self.env.text


StFactory = Callable[..., Awaitable[StEnv]]


@pytest.fixture
async def make_stenv(make_senv: Callable[..., Awaitable[SEnv]]) -> AsyncIterator[StFactory]:
    hubs: list[ErrorHub] = []

    async def factory(**kw: Any) -> StEnv:
        env = await make_senv()
        await env.add(OWNER, "owner")
        await env.add(VIEWER, "admin", frozenset({"system.view"}))
        await env.add(ADMIN_NOPERM, "admin", frozenset({"settings.business"}))
        await env.add(SUPPORT, "support")
        await env.add(USER, "user")
        attention = AttentionService(env.db)
        hub = ErrorHub(env.db)
        hubs.append(hub)
        queue = JobQueue(env.db)
        mirror, scheduler = MirrorHolder(), Scheduler()
        maintenance = MaintenanceService(settings=env.service, panel=Panel)
        await maintenance.tick()
        screens = StatusScreens(
            env.router,
            components=env.components,
            attention=attention,
            hub=hub,
            queue=queue,
            scheduler=scheduler,
            settings=env.service,
            mirror=lambda: mirror,
            maintenance=maintenance,
            **kw,
        )
        screens.install()
        return StEnv(env, screens, attention, hub, queue, mirror, scheduler)

    yield factory
    for hub in hubs:
        await hub.stop(grace=1.0)


@pytest.fixture
async def st(make_stenv: StFactory) -> StEnv:
    return await make_stenv()


# ------------------------------------------------------------------------------------------ screen


async def test_owner_sees_everything(st: StEnv) -> None:
    st.env.component("remnawave").health_report = HealthReport.down(
        "Панель недоступна с 11:58 UTC", fix_action=fix_setting("REMNAWAVE_URL")
    )
    st.env.component("admin_chat").health_report = HealthReport.disabled("Не подключён")
    job_id = await st.queue.enqueue("panel.write", {"x": 1}, queue="panel")
    await st.queue.enqueue("panel.write", {"x": 2}, queue="panel")
    await st.env.db.raw("update jobs set status = 'dead' where id = $1", job_id)
    try:
        raise RuntimeError("сломалось")
    except RuntimeError as exc:
        await st.hub.capture(exc, "job:panel.write", module="remnawave")
    await st.hub.drain(5.0)
    await st.attention.raise_item(
        "component:remnawave", "error", "Панель не работает", fix_action="setting:X"
    )
    await st.attention.raise_item("token", "warn", "Токен истекает")
    st.scheduler.items["jobs.purge"] = Task(last_error="OSError", runs=3)
    st.scheduler.items["ok.task"] = Task(last_ok_at=datetime(2026, 10, 1, tzinfo=UTC), runs=5)
    st.mirror.status.invalid = {"TRIAL_DAYS": "не число"}

    await st.open()
    text = st.text
    assert "Состояние</b>" in text and "работает" in text and "память" in text
    assert "✅ Telegram-бот" in text
    assert "🔴 Remnawave — Панель недоступна с 11:58 UTC" in text
    assert "⚪ Админ-чат — Не подключён" in text
    # queues as a table: name, waiting, running, failed
    assert re.search(r"<pre>Очередь +Ждут +Идут +Ошибки\n[─ ]+\n(.+\n)*panel +1 +0 +1(\n|</pre>)", text)
    assert "jobs.purge: ошибка «OSError»" in text and "ok.task" not in text
    assert "🚨 <b>×1</b> Непредвиденная ошибка — job:panel.write" in text
    assert "✅ .env синхронизирован, запись 01.10 11:00 UTC" in text
    assert "В .env отклонены строки: TRIAL_DAYS" in text
    assert "Требует внимания: 2</b> (🔴 1 · 🟠 1)" in text
    assert "Техработы: выключены" in text
    labels = st.env.labels()
    assert "⚠️ Требует внимания (2)" in labels
    assert not any("Мастер настройки" in label for label in labels)  # it is in «🔌 Панель Remnawave»
    assert st.env.button("Техработы") == encode("set.v", "o", "sys.maint")  # admin → ⚙️ Система → 🛠


async def test_viewer_reads_without_owner_buttons(st: StEnv) -> None:
    await st.attention.raise_item("a1", "warn", "Что-то", fix_action=fix_setting("REMNAWAVE_TOKEN"))
    await st.open(VIEWER)
    assert "🛠 Админка › ⚙️ Система › <b>🩺 Состояние</b>" in st.text
    assert not any("Мастер" in label for label in st.env.labels())
    await st.open(VIEWER, SCREEN_ATTENTION)
    assert "Что-то" in st.text
    assert not any("Исправить" in label or "24 ч" in label for label in st.env.labels())


@pytest.mark.parametrize("tg_id", [ADMIN_NOPERM, SUPPORT, USER])
async def test_others_are_denied(st: StEnv, tg_id: int) -> None:
    await st.open(tg_id)
    assert st.env.toasts[-1] == "Нет прав"
    assert await st.screens.handle_status(text_message(tg_id, "/status")) is False


async def test_status_command(st: StEnv) -> None:
    assert await st.screens.handle_status(text_message(OWNER, "/status")) is True
    assert "🛠 Админка › ⚙️ Система › <b>🩺 Состояние</b>" in st.text
    assert await st.screens.handle_status(text_message(OWNER, "/status", chat_type="group")) is False


# ------------------------------------------------------------------------------------------ attention


async def test_attention_fix_and_snooze(st: StEnv) -> None:
    await st.attention.raise_item(
        "t1", "error", "Токен истёк", "Создайте новый", fix_setting("REMNAWAVE_TOKEN")
    )
    await st.attention.raise_item("t2", "warn", "Админ-чат", fix_action=fix_screen("achat"))
    await st.attention.raise_item("t3", "info", "Без кнопки")
    await st.open(OWNER, SCREEN_ATTENTION)
    text = st.text
    assert text.index("Токен истёк") < text.index("Админ-чат") < text.index("Без кнопки")
    assert st.env.button("1. Исправить") == encode("set.key", "o", "REMNAWAVE_TOKEN")
    assert st.env.button("2. Исправить") == encode("achat")
    assert not any(label.startswith("🛠 3.") for label in st.env.labels())
    item = await st.attention.get("t2")
    assert item is not None
    await st.env.press(OWNER, "2. 24 ч")
    assert st.env.toasts[-1] == "Скрыто на 24 ч"
    assert "Админ-чат" not in st.text
    refreshed = await st.attention.get("t2")
    assert refreshed is not None and refreshed.is_snoozed()


async def test_snooze_is_owner_only_and_validated(st: StEnv) -> None:
    await st.attention.raise_item("t1", "warn", "Пункт")
    item = await st.attention.get("t1")
    assert item is not None
    await st.env.click(VIEWER, encode(ACTIONS, A_SNOOZE, str(item.id)))
    assert st.env.toasts[-1] == "Нет прав"
    still = await st.attention.get("t1")
    assert still is not None and not still.is_snoozed()
    await st.env.click(OWNER, encode(ACTIONS, A_SNOOZE, "abc"))
    assert st.env.toasts[-1] == "Этот пункт уже решён"
    await st.env.click(OWNER, encode(ACTIONS, A_SNOOZE, "999999"))
    assert st.env.toasts[-1] == "Этот пункт уже решён"


def test_fix_button_target() -> None:
    assert fix_button_target("setting:REMNAWAVE_TOKEN") == ("set.key", "REMNAWAVE_TOKEN")
    assert fix_button_target("screen:status") == ("status", None)
    assert fix_button_target("screen:jobs:dead") == ("jobs", "dead")
    assert fix_button_target("setting:lower") is None
    assert fix_button_target("screen:bad name!") is None
    assert fix_button_target("http://evil") is None
    assert fix_button_target(None) is None


# ------------------------------------------------------------------------------------------ isolation, safety


@dataclass
class Hanging:
    name: str = "slowpoke"

    async def probe(self, candidate: Any) -> None:
        return None

    async def reconfigure(self, cfg: Any) -> None:
        return None

    async def health(self) -> HealthReport:
        await asyncio.sleep(30)
        return HealthReport.ok()


class BrokenQueue:
    async def stats(self) -> dict[str, dict[str, int]]:
        raise OSError("pool exhausted")


async def test_broken_parts_do_not_break_the_screen(make_stenv: StFactory) -> None:
    st = await make_stenv(health_timeout=0.2)
    st.env.components.register(Hanging())
    st.screens.queue = BrokenQueue()
    await st.open()
    text = st.text
    assert "❔ slowpoke — Не ответил на проверку" in text
    assert "Не удалось получить: очередь задач" in text
    assert "🛠 Админка › ⚙️ Система › <b>🩺 Состояние</b>" in text


async def test_secrets_are_masked(st: StEnv) -> None:
    secret = "sk_live_" + "Z" * 30
    register_secret(secret)
    st.env.component("bot").health_report = HealthReport.degraded(f"ошибка с токеном {secret}")
    await st.attention.raise_item("leak", "warn", "Пункт", body="тело")
    await st.open()
    await st.open(OWNER, SCREEN_ATTENTION)
    assert secret not in st.env.all_text()


async def test_sql_budget(st: StEnv) -> None:
    await st.open()  # warm-up: ui_state, codec
    mark = st.env.db.queries
    data = await st.screens.collect()
    assert not data.failed
    assert st.env.db.queries - mark <= 4, "attention counts + error groups + jobs stats (+ db health)"


def test_rss_bytes() -> None:
    value = rss_bytes()
    assert value is None or value > 0


async def test_env_mirror_problems(st: StEnv, tmp_path: Path) -> None:
    st.mirror.status.error = "Permission denied"
    st.env.service.restart_pending.add("DATABASE_URL")
    await st.open()
    assert "⚠️ .env: Permission denied" in st.text
    assert "♻️ Ждут перезапуска: DATABASE_URL" in st.text
    st.env.service.restart_pending.discard("DATABASE_URL")


async def test_module_setup_entry_point(make_senv: Callable[..., Awaitable[SEnv]]) -> None:
    from types import SimpleNamespace

    from aiogram import Router

    from svbg.tg.admin import status

    env = await make_senv()
    await env.add(OWNER, "owner")
    deps = SimpleNamespace(
        components=env.components, attention=AttentionService(env.db), settings=env.service
    )
    router = status.setup(env.router, deps)
    assert isinstance(router, Router)
    await env.click(OWNER, encode(SCREEN))
    assert "🛠 Админка › ⚙️ Система › <b>🩺 Состояние</b>" in env.text
