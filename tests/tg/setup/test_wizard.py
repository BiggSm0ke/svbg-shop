"""Setup wizard end to end: real PostgreSQL, real settings pipeline, real RemnawaveComponent + fake panel."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from aiogram.methods import DeleteMessage

from svbg.core.component import HealthReport
from svbg.core.settings.service import Change
from svbg.remnawave.component import RemnawaveComponent
from svbg.tg.setup.wizard import (
    A_FINISH,
    ACTIONS,
    FORM_TOKEN,
    SCREEN,
    SetupWizard,
    WebhookActivity,
    is_panel_secret,
)
from svbg.tg.ui.codec import encode
from tests.fakes.remnawave import FakeRemnawave
from tests.tg.admin.settings_harness import SEnv
from tests.tg.ui.ui_harness import text_message

OWNER = 1001
ADMIN = 2002


@dataclass
class WEnv:
    env: SEnv
    wizard: SetupWizard
    panel: FakeRemnawave
    comp: RemnawaveComponent
    activity: list[WebhookActivity | None]

    async def open(self, arg: str | None = None, tg_id: int = OWNER) -> None:
        await self.env.click(tg_id, encode(SCREEN, "o", arg))

    @property
    def text(self) -> str:
        return self.env.text

    async def state(self) -> dict[str, Any]:
        rows = await self.env.db.raw("select value from config_meta where key = 'wizard'")
        return rows[0]["value"] if rows else {}


WEnvFactory = Callable[..., Awaitable[WEnv]]


@pytest.fixture
async def make_wenv(senv: SEnv) -> AsyncIterator[WEnvFactory]:
    made: list[tuple[WEnv, FakeRemnawave]] = []

    async def factory(*, hostname: str = "svbg-shop", resolves: bool = True, **panel_kw: Any) -> WEnv:
        panel = FakeRemnawave(**panel_kw)
        await panel.start()
        senv.components.unregister("remnawave")
        comp = RemnawaveComponent(transport_overrides={"max_attempts": 1}, probe_timeout=4.0)
        senv.components.register(comp)
        await senv.add(OWNER, "owner")
        await senv.add(ADMIN, "admin", frozenset({"system.view", "settings.business"}))
        activity: list[WebhookActivity | None] = [WebhookActivity()]

        async def seen() -> WebhookActivity | None:
            return activity[0]

        async def resolver(_host: str) -> bool:
            return resolves

        wizard = SetupWizard(
            senv.router,
            db=senv.db,
            settings=senv.service,
            components=senv.components,
            webhook_seen=seen,
            hostname=lambda: hostname,
            resolver=resolver,
        )
        wizard.install()
        w = WEnv(senv, wizard, panel, comp, activity)
        made.append((w, panel))
        return w

    yield factory
    for w, panel in made:
        await w.wizard.drain()
        await w.comp.aclose(grace=0)
        await panel.stop()


@pytest.fixture
async def wenv(make_wenv: WEnvFactory) -> WEnv:
    return await make_wenv()


async def connect(w: WEnv, token: str | None = None, *, url: str | None = None) -> str:
    """Drive «🌐 Ввести адрес» → address → token; returns the token used."""
    token = token or w.panel.add_token()
    await w.open("rw")
    await w.env.press(OWNER, "Ввести адрес")
    assert await w.env.type(OWNER, url or w.panel.url, message_id=701)
    assert "API-токен" in w.text
    assert await w.env.type(OWNER, token, message_id=702)
    return token


# ------------------------------------------------------------------------------------------ panel step


async def test_first_screen_and_local_button(wenv: WEnv) -> None:
    await wenv.open()
    assert "шаг 2 из 5" in wenv.text
    assert "Панель не подключена" in wenv.text
    labels = wenv.env.labels()
    assert any("На этом сервере" in label for label in labels)
    assert any("Пропустить" in label for label in labels)
    await wenv.env.press(OWNER, "На этом сервере")
    assert "http://remnawave:3000" in wenv.text
    awaiting = (await wenv.env.ui_state.get(wenv.env.users.by_tg[OWNER].user_id)).awaiting
    assert awaiting is not None and awaiting["form"] == FORM_TOKEN
    assert awaiting["data"] == {"url": "http://remnawave:3000"}


async def test_connect_with_url_and_token(wenv: WEnv) -> None:
    token = await connect(wenv)
    assert wenv.comp.configured
    current = wenv.env.service.current()
    assert current["REMNAWAVE_URL"] == wenv.panel.url
    assert current["REMNAWAVE_TOKEN"] == token
    text = wenv.text
    assert "Последняя проверка" in text and "✅ Подключено" in text
    assert "Панель 3.4.4 · совместимо" in text
    assert "Подключена:" in text
    # both messages (address and token) were deleted from the chat
    deleted = {m.message_id for m in wenv.env.transport.of(DeleteMessage)}
    assert {701, 702} <= deleted
    # the token never appears in the chat, the UI state or the wizard state
    assert token not in wenv.env.all_text()
    state = await wenv.state()
    assert token not in json.dumps(state)
    rows = await wenv.env.db.raw("select awaiting::text as a from ui_state")
    assert all(token not in (r["a"] or "") for r in rows)
    assert state["panel_check"]["ok"] is True
    # the running connection really works
    meta = await wenv.comp.client.metadata()
    assert meta.version == "3.4.4"


async def test_wrong_token_keeps_previous_connection(wenv: WEnv) -> None:
    good = await connect(wenv)
    await connect(wenv, "eyJhbGciOiJIUzI1NiJ9.eyJ1dWlkIjoid3JvbmcifQ.c2lnbmF0dXJlc2lnbmF0dXJl")
    text = wenv.text
    assert "❌ Не подключено" in text
    assert "Прежнее подключение продолжает работать" in text
    assert wenv.env.service.current()["REMNAWAVE_TOKEN"] == good
    assert (await wenv.comp.client.metadata()).version == "3.4.4"
    assert "eyJ1dWlkIjoid3JvbmcifQ" not in wenv.env.all_text()


async def test_missing_scopes_are_reported(wenv: WEnv) -> None:
    token = wenv.panel.add_token(scopes=("system:metadata", "users:read"))
    await connect(wenv, token)
    assert "❌ Не подключено" in wenv.text
    assert "нет обязательных прав" in wenv.text
    assert not wenv.comp.configured


async def test_token_pasted_as_address_is_deleted_and_rejected(wenv: WEnv) -> None:
    await wenv.open("rw")
    await wenv.env.press(OWNER, "Ввести адрес")
    token = wenv.panel.add_token()
    assert await wenv.env.type(OWNER, token, message_id=777)
    assert 777 in {m.message_id for m in wenv.env.transport.of(DeleteMessage)}
    assert "Похоже на токен, а не на адрес" in wenv.text
    assert token not in wenv.env.all_text()
    state = await wenv.env.ui_state.get(wenv.env.users.by_tg[OWNER].user_id)
    assert token not in json.dumps(state.awaiting)


async def test_token_validation_messages(wenv: WEnv) -> None:
    await wenv.open("rw")
    await wenv.env.press(OWNER, "На этом сервере")
    assert await wenv.env.type(OWNER, "https://panel.example.com", message_id=780)
    assert "нужен API-токен" in wenv.text
    assert await wenv.env.type(OWNER, "short", message_id=781)
    assert "Слишком короткий токен" in wenv.text
    assert {780, 781} <= {m.message_id for m in wenv.env.transport.of(DeleteMessage)}


async def test_check_button_reruns_self_test(wenv: WEnv) -> None:
    await connect(wenv)
    calls = len(wenv.panel.calls("/api/system/metadata")) + len(wenv.panel.calls("/system/metadata"))
    await wenv.env.press(OWNER, "Проверить")
    after = len(wenv.panel.calls("/api/system/metadata")) + len(wenv.panel.calls("/system/metadata"))
    assert after > calls
    assert "✅ Подключено" in wenv.text


async def test_slow_panel_answers_later(make_wenv: WEnvFactory) -> None:
    w = await make_wenv()
    w.wizard.apply_wait = 0.05
    w.panel.inject("latency", delay=0.4, times=1)
    await connect(w)
    assert "Проверяю подключение" in w.text
    await w.wizard.drain()
    assert "✅ Подключено" in w.text  # delivered by router.show once the check finished


# ------------------------------------------------------------------------------------------ access, resume


async def test_only_owner(wenv: WEnv) -> None:
    await wenv.open(tg_id=ADMIN)
    assert wenv.env.toasts[-1] == "Нет прав"
    assert any(place.startswith("screen:setup.wiz") for _uid, place in wenv.env.denied.calls)
    await wenv.env.click(ADMIN, encode(ACTIONS, A_FINISH))
    assert wenv.env.toasts[-1] == "Нет прав"
    handled = await wenv.wizard.handle_setup(text_message(ADMIN, "/setup"))
    assert handled is False
    assert await wenv.wizard.handle_setup(text_message(OWNER, "/setup")) is True
    assert "Мастер настройки" in wenv.text


async def test_skip_and_resume_after_restart(wenv: WEnv) -> None:
    await wenv.open()
    await wenv.env.press(OWNER, "Пропустить")
    assert "шаг 3 из 5" in wenv.text
    state = await wenv.state()
    assert state["step"] == "chat" and state["skipped"] == ["rw"]
    restarted = SetupWizard(
        wenv.env.router, db=wenv.env.db, settings=wenv.env.service, components=wenv.env.components
    )
    assert (await restarted.store.load()).step == "chat"
    await wenv.wizard.handle_setup(text_message(OWNER, "/setup"))
    assert "Админ-группа" in wenv.text


async def test_forged_skip_argument(wenv: WEnv) -> None:
    await wenv.env.click(OWNER, encode(ACTIONS, "skip", "nope"))
    assert wenv.env.toasts[-1] == "Кнопка устарела"


# ------------------------------------------------------------------------------------------ admin chat step


async def test_admin_chat_step(wenv: WEnv) -> None:
    chat = wenv.env.component("admin_chat")
    chat.health_report = HealthReport.disabled("Не подключён")
    await wenv.open("chat")
    assert "Не подключена" in wenv.text
    assert any("Подключить группу" in label for label in wenv.env.labels())
    assert wenv.env.button("Подключить группу") == encode("achat")
    chat.health_report = HealthReport.ok("Подключён, тем: 9; в очереди: 0")
    await wenv.env.press(OWNER, "Проверить")
    assert "✅ Подключён, тем: 9" in wenv.text
    assert any("Дальше" in label for label in wenv.env.labels())


# ------------------------------------------------------------------------------------------ webhooks step


async def test_webhooks_need_panel_first(wenv: WEnv) -> None:
    await wenv.open("wh")
    assert "Сначала подключите панель" in wenv.text


async def test_webhooks_mode_a_generates_secret_and_snippet(make_wenv: WEnvFactory) -> None:
    w = await make_wenv(webhook_enabled=False)
    await connect(w)
    await w.open("wh")
    assert "выключены" in w.text
    await w.env.press(OWNER, "Показать настройки")
    secret = w.env.service.current()["REMNAWAVE_WEBHOOK_SECRET"]
    assert isinstance(secret, str) and is_panel_secret(secret) and len(secret) == 64
    text = w.text
    assert f"setkv WEBHOOK_SECRET_HEADER '{secret}'" in text
    assert "OUR_URL='http://svbg-shop:8080/webhooks/remnawave'" in text
    assert "без пробелов" in text and "комментариев" in text
    assert "в той же docker-сети" in text  # the panel is reached by IP here, not by a docker name
    state = await w.state()
    assert state["webhook_mode"] == "A" and secret not in json.dumps(state)
    # opening again keeps the same secret (no silent rotation)
    await w.open("wh")
    assert w.env.service.current()["REMNAWAVE_WEBHOOK_SECRET"] == secret


async def test_webhooks_public_url_and_container_id(make_wenv: WEnvFactory) -> None:
    w = await make_wenv(hostname="3f2a9c1b7d4e", webhook_enabled=False)
    await connect(w)
    await w.open("wh")
    await w.env.press(OWNER, "Показать настройки")
    assert "похоже на случайный id" in w.text
    assert any("PUBLIC_URL" in label for label in w.env.labels())
    result = await w.env.service.apply(
        [Change("PUBLIC_URL", "https://bot.example.com")], source="bot", actor_id=None
    )
    assert result.ok, result.rejected
    await w.open("wh")
    assert "OUR_URL='https://bot.example.com/webhooks/remnawave'" in w.text
    assert "через PUBLIC_URL" in w.text


async def test_webhooks_mode_b_uses_the_existing_secret(wenv: WEnv) -> None:
    await connect(wenv)
    await wenv.open("wh")
    assert "Панель уже отправляет вебхуки" in wenv.text
    await wenv.env.press(OWNER, "другому боту")
    assert "WEBHOOK_SECRET_HEADER" in wenv.text
    assert await wenv.env.type(OWNER, "too-short", message_id=790)
    assert "не короче 32 символов" in wenv.text
    existing = "A" * 20 + "b" * 20 + "1" * 8
    assert await wenv.env.type(OWNER, existing, message_id=791)
    assert {790, 791} <= {m.message_id for m in wenv.env.transport.of(DeleteMessage)}
    assert wenv.env.service.current()["REMNAWAVE_WEBHOOK_SECRET"] == existing
    text = wenv.text
    assert "WEBHOOK_SECRET_HEADER '" not in text  # mode B never rewrites the shared secret
    assert existing not in text
    assert "общий с другим ботом" in text
    assert (await wenv.state())["webhook_mode"] == "B"


async def test_webhook_checks(wenv: WEnv) -> None:
    await connect(wenv)
    await wenv.open("wh")
    await wenv.env.press(OWNER, "Нет — пусть бот создаст секрет")
    await wenv.env.press(OWNER, "🔄 Проверить")
    assert "ждём первое событие" in wenv.text
    wenv.activity[0] = WebhookActivity(last_ok_at=datetime(2026, 10, 1, 9, 30, tzinfo=UTC))
    await wenv.env.press(OWNER, "🔄 Проверить")
    assert "Подписанные вебхуки доходят: последнее событие 01.10 09:30 UTC" in wenv.text
    wenv.activity[0] = WebhookActivity(
        last_ok_at=datetime(2026, 10, 1, 9, 30, tzinfo=UTC),
        last_bad_signature_at=datetime(2026, 10, 1, 9, 40, tzinfo=UTC),
    )
    await wenv.env.press(OWNER, "🔄 Проверить")
    assert "неверной подписью" in wenv.text
    wenv.panel.configuration["notifications"]["webhook"] = False
    await wenv.env.press(OWNER, "🔄 Проверить")
    assert "Вебхуки в панели выключены" in wenv.text
    assert "&amp;&amp;" in wenv.text  # escaped for HTML


async def test_webhook_url_line_check(wenv: WEnv) -> None:
    await connect(wenv)
    await wenv.open("wh")
    await wenv.env.press(OWNER, "Нет — пусть бот создаст секрет")
    await wenv.env.press(OWNER, "Проверить строку")
    assert await wenv.env.type(OWNER, "WEBHOOK_URL=https://old.example.com/hook, http://other:1")
    assert "Уберите пробелы" in wenv.text
    assert "нет адреса бота" in wenv.text
    await wenv.env.press(OWNER, "Проверить строку")
    line = "WEBHOOK_URL=https://old.example.com/hook,http://svbg-shop:8080/webhooks/remnawave"
    assert await wenv.env.type(OWNER, line)
    assert "✅ Строка WEBHOOK_URL корректна" in wenv.text


# ------------------------------------------------------------------------------------------ readiness


async def test_readiness_and_finish(wenv: WEnv) -> None:
    wenv.env.component("admin_chat").health_report = HealthReport.disabled("Не подключён")
    await wenv.open("ready")
    assert "Готовность: 1/3" in wenv.text
    assert "⚠️ Админ-группа не подключена" in wenv.text
    assert "Remnawave не подключена" in wenv.text
    assert any("Исправить: Remnawave" in label for label in wenv.env.labels())
    await connect(wenv)
    wenv.env.component("admin_chat").health_report = HealthReport.ok("Подключён")
    wenv.activity[0] = WebhookActivity(last_ok_at=datetime(2026, 10, 1, tzinfo=UTC))
    await wenv.open("ready")
    text = wenv.text
    assert "✅ Владелец назначен" in text
    assert "✅ Remnawave: Панель 3.4.4" in text
    assert "✅ Права токена: все нужные" in text
    assert "✅ Вебхуки панели включены" in text
    assert "✅ Первое событие вебхука получено" in text
    assert "✅ Админ-группа: Подключён" in text
    assert "Готовность: 6/6" in text
    await wenv.env.press(OWNER, "Готово")
    assert "Настройка завершена" in wenv.text and "(6/6)" in wenv.text
    state = await wenv.state()
    assert state["step"] == "done" and state["finished_at"]
    await wenv.wizard.handle_setup(text_message(OWNER, "/setup"))
    assert "Готовность: 6/6" in wenv.text  # a finished wizard reopens at the checklist


async def test_module_setup_entry_point(senv: SEnv) -> None:
    from types import SimpleNamespace

    from aiogram import Router

    from svbg.tg.setup import wizard as wizard_mod

    stops: list[str] = []
    deps = SimpleNamespace(
        db=senv.db,
        settings=senv.service,
        components=senv.components,
        on_stop=lambda name, _fn: stops.append(name),
    )
    assert isinstance(wizard_mod.setup(senv.router, deps), Router)
    assert stops == ["setup wizard"]
    await senv.add(OWNER, "owner")
    await senv.click(OWNER, encode(SCREEN))
    assert "Мастер настройки" in senv.text


async def test_same_credentials_again_rechecks(wenv: WEnv) -> None:
    token = await connect(wenv)
    mark = len(wenv.panel.requests)
    await connect(wenv, token)
    assert "✅ Подключено" in wenv.text
    assert len(wenv.panel.requests) > mark  # the running connection was re-checked, not assumed


def test_rejected_inputs_are_not_registered_as_secrets() -> None:
    from svbg.core.log import mask
    from svbg.tg.setup import wizard as wizard_mod
    from svbg.tg.ui.forms import ValidationError

    url = "https://panel-unique-check.example.com"
    with pytest.raises(ValidationError):
        wizard_mod._token_validator(url)
    with pytest.raises(ValidationError):
        wizard_mod._token_validator("shortword")
    assert mask(f"see {url}") == f"see {url}"
    assert mask("shortword") == "shortword"
