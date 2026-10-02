"""«Что включено в панели»: GET /system/configuration → missing panel .env lines + modify-in-place snippet."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

import pytest

from svbg.core.attention import AttentionService
from svbg.remnawave.component import RemnawaveComponent
from svbg.tg.admin.status import SCREEN_PANEL, StatusScreens
from svbg.tg.ui.codec import encode
from tests.fakes.remnawave import FakeRemnawave
from tests.tg.admin.settings_harness import SEnv

OWNER, VIEWER = 1001, 2002


@dataclass
class PEnv:
    env: SEnv
    screens: StatusScreens
    panel: FakeRemnawave
    comp: RemnawaveComponent

    async def open(self, tg_id: int = OWNER) -> str:
        await self.env.click(tg_id, encode(SCREEN_PANEL))
        return self.env.text


@pytest.fixture
async def penv(make_senv: Callable[..., Awaitable[SEnv]]) -> AsyncIterator[PEnv]:
    env = await make_senv()
    await env.add(OWNER, "owner")
    await env.add(VIEWER, "admin", frozenset({"system.view"}))
    env.components.unregister("remnawave")
    comp = RemnawaveComponent(transport_overrides={"max_attempts": 1})
    env.components.register(comp)
    screens = StatusScreens(
        env.router, components=env.components, attention=AttentionService(env.db), panel_timeout=3.0
    )
    screens.install()
    async with FakeRemnawave(webhook_enabled=False) as panel:
        yield PEnv(env, screens, panel, comp)
        await comp.aclose(grace=0)


async def test_not_connected(penv: PEnv) -> None:
    text = await penv.open()
    assert "Панель не подключена" in text


async def test_missing_lines_and_snippet(penv: PEnv) -> None:
    await penv.comp.reconfigure(penv.panel.settings(penv.panel.add_token()))
    penv.panel.configuration["notifications"]["notConnectedAfter"] = [2, 24]
    text = await penv.open()
    assert "⚪ Вебхуки (<code>WEBHOOK_ENABLED</code>): выключены" in text
    assert "✅ Не подключившиеся пользователи" in text and "[2,24]" in text
    assert "⚪ Напоминания об истечении" in text
    assert "Домен подписок: <code>sub.example.com</code>" in text
    assert "мастере настройки" in text  # webhooks are set up in the wizard (needs URL + secret)
    assert '<pre><code class="language-bash">' in text
    assert "setkv EXPIRATION_NOTIFICATIONS '[-72,-24,24]'" in text
    assert "BANDWIDTH_USAGE_NOTIFICATIONS_THRESHOLD" in text
    assert "NOT_CONNECTED_USERS_NOTIFICATIONS_ENABLED" not in text.split("<pre>", 1)[1]
    assert "без пробелов после запятых" in text
    assert "WEBHOOK_SECRET_HEADER" not in text
    assert any("Мастер" in label for label in penv.env.labels())
    assert penv.panel.calls("/api/system/configuration") or penv.panel.calls("/system/configuration")


async def test_everything_enabled(penv: PEnv) -> None:
    await penv.comp.reconfigure(penv.panel.settings(penv.panel.add_token()))
    penv.panel.configuration["notifications"].update(
        webhook=True, bandwidthUsage=[80, 95], notConnectedAfter=[2], expirationNotifications=[-24, 24]
    )
    text = await penv.open(VIEWER)
    assert "✅ Всё, что полезно боту, в панели включено" in text
    assert "<pre>" not in text
    assert not any("Мастер" in label for label in penv.env.labels())


async def test_panel_errors_are_human(penv: PEnv) -> None:
    await penv.comp.reconfigure(penv.panel.settings(penv.panel.add_token(scopes=("system:metadata",))))
    text = await penv.open()
    assert "Не удалось прочитать настройки панели" in text
    await penv.comp.reconfigure(penv.panel.settings("not-a-valid-token-at-all-xxxxxxxx"))
    text = await penv.open()
    assert "Не удалось прочитать настройки панели" in text
    assert "not-a-valid-token" not in penv.env.all_text()
