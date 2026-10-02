"""Changing settings from the bot: forms, toggles, choices, reset, undo, secrets, honest errors."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from aiogram.methods import DeleteMessage
from sqlalchemy.exc import OperationalError

from svbg.core.clock import FrozenClock, reset_clock, set_clock
from svbg.core.component import ProbeError
from svbg.core.settings.service import Change
from svbg.tg.admin.settings import ACTIONS, SCREEN_HISTORY, SCREEN_KEY
from tests.tg.admin.settings_harness import (
    ADMIN,
    OWNER,
    EnvFactory,
    SEnv,
    add_staff,
    extended_registry,
)
from tests.tg.ui.ui_harness import text_message


def key_cb(key: str) -> str:
    return f"v1:{SCREEN_KEY}:o:{key}"


@pytest.fixture(autouse=True)
def _real_clock() -> Any:
    yield
    reset_clock()


async def rows(env: SEnv, key: str) -> list[Any]:
    return await env.db.raw(
        "select key, value::text as value, source, updated_by from settings where key=$1", key
    )


async def audit(env: SEnv) -> list[Any]:
    return await env.db.raw(
        "select batch_id, key, old::text as old, new::text as new, source, applied"
        " from settings_audit order by id"
    )


# ---------------------------------------------------------------- edit through a form


async def test_edit_int_through_form_applies_and_offers_undo(senv: SEnv) -> None:
    staff = await add_staff(senv)
    await senv.click(OWNER, key_cb("TRIAL_DAYS"))
    await senv.press(OWNER, "Изменить")
    prompt = senv.text
    assert "TRIAL_DAYS" in prompt and "Диапазон: 0–365" in prompt
    assert (await senv.ui_state.get(staff["owner"].user_id)).awaiting is not None

    assert await senv.type(OWNER, "7")
    assert "⚡ <b>Применено</b>" in senv.text
    assert "<b>Дней пробного периода</b>: 7" in senv.text
    assert "↩️ Отменить" in senv.labels()
    assert senv.service.current()["TRIAL_DAYS"] == 7
    stored = await rows(senv, "TRIAL_DAYS")
    assert stored[0]["value"] == "7"
    assert stored[0]["source"] == "bot"
    assert stored[0]["updated_by"] == staff["owner"].user_id
    # the card now says where the value comes from and offers a reset
    await senv.press(OWNER, "К настройке")
    assert "🤖 из бота" in senv.text
    assert "Изменено только что · 🤖 бот" in senv.text
    assert "↩️ По умолчанию" in senv.labels()


async def test_invalid_input_is_reasked_with_a_human_error(senv: SEnv) -> None:
    await add_staff(senv)
    await senv.click(OWNER, key_cb("TRIAL_DAYS"))
    await senv.press(OWNER, "Изменить")
    assert await senv.type(OWNER, "abc")
    assert "ожидалось целое число" in senv.text
    assert await senv.type(OWNER, "999", message_id=701)
    assert "больше максимума (365)" in senv.text
    assert senv.service.current()["TRIAL_DAYS"] == 3
    assert await senv.type(OWNER, "0", message_id=702)
    assert "Применено" in senv.text
    assert senv.service.current()["TRIAL_DAYS"] == 0


async def test_cancel_form_returns_to_the_card(senv: SEnv) -> None:
    await add_staff(senv)
    await senv.click(OWNER, key_cb("TRIAL_DAYS"))
    await senv.press(OWNER, "Изменить")
    await senv.press(OWNER, "Отмена")
    assert "<code>TRIAL_DAYS</code>" in senv.text
    assert senv.toasts[-1] == "Отменено"


async def test_admin_edits_business_key(senv: SEnv) -> None:
    staff = await add_staff(senv)
    await senv.click(ADMIN, key_cb("WALLET_AUTOCOMPLETE_MINUTES"))
    await senv.press(ADMIN, "Изменить")
    assert await senv.type(ADMIN, "90")
    assert "Применено" in senv.text
    assert (await rows(senv, "WALLET_AUTOCOMPLETE_MINUTES"))[0]["updated_by"] == staff["admin"].user_id


async def test_form_rechecks_rights_at_the_end(senv: SEnv) -> None:
    staff = await add_staff(senv)
    await senv.click(ADMIN, key_cb("TRIAL_DAYS"))
    await senv.press(ADMIN, "Изменить")
    from svbg.tg.ui.context import UserCtx

    senv.users.by_tg[ADMIN] = UserCtx(staff["admin"].user_id, telegram_id=ADMIN, role="admin")
    assert await senv.type(ADMIN, "9")
    assert "Нет прав" in senv.text
    assert senv.service.current()["TRIAL_DAYS"] == 3


# ---------------------------------------------------------------- toggle / pick / reset


async def test_toggle_bool(make_senv: EnvFactory) -> None:
    env = await make_senv(registry=extended_registry())
    await add_staff(env)
    await env.click(OWNER, key_cb("FEATURE_FLAG"))
    assert "Сейчас: <b>❌ выкл</b>" in env.text
    await env.press(OWNER, "Включить")
    assert "Применено" in env.text
    assert env.service.current()["FEATURE_FLAG"] is True
    await env.press(OWNER, "К настройке")
    await env.press(OWNER, "Выключить")
    assert env.service.current()["FEATURE_FLAG"] is False
    # edit on a bool toggles as well; toggle on a non-bool is refused
    await env.click(OWNER, f"v1:{ACTIONS}:edit:FEATURE_FLAG")
    assert env.service.current()["FEATURE_FLAG"] is True
    await env.click(OWNER, f"v1:{ACTIONS}:tog:TRIAL_DAYS")
    assert env.toasts[-1] == "Эта настройка не переключается"


async def test_pick_enum_and_forged_choice(senv: SEnv) -> None:
    await add_staff(senv)
    await senv.click(OWNER, key_cb("TRIAL_AUDIENCE"))
    await senv.press(OWNER, "channel_members")
    assert "Применено" in senv.text
    assert senv.service.current()["TRIAL_AUDIENCE"] == "channel_members"
    await senv.click(OWNER, f"v1:{ACTIONS}:pick:TRIAL_AUDIENCE:everyone")
    assert senv.toasts[-1] == "Такого варианта нет"
    await senv.click(OWNER, f"v1:{ACTIONS}:pick:TRIAL_DAYS:5")
    assert senv.toasts[-1] == "Такого варианта нет"
    await senv.click(OWNER, f"v1:{ACTIONS}:pick:garbage")
    assert senv.toasts[-1] == "Меню обновилось"
    assert senv.service.current()["TRIAL_AUDIENCE"] == "channel_members"


async def test_reset_to_default_deletes_the_row(senv: SEnv) -> None:
    await add_staff(senv)
    await senv.service.apply([Change("TRIAL_DAYS", "10")], source="bot", actor_id=None)
    await senv.click(OWNER, key_cb("TRIAL_DAYS"))
    await senv.press(OWNER, "По умолчанию")
    assert "Применено" in senv.text
    assert senv.service.current()["TRIAL_DAYS"] == 3
    assert senv.service.current().source("TRIAL_DAYS") == "default"
    assert await rows(senv, "TRIAL_DAYS") == []


async def test_admin_cannot_change_owner_keys_with_forged_actions(senv: SEnv) -> None:
    staff = await add_staff(senv)
    for data in (
        f"v1:{ACTIONS}:pick:BOT_MODE:webhook",
        f"v1:{ACTIONS}:def:OWNER_IDS",
        f"v1:{ACTIONS}:edit:REMNAWAVE_TOKEN",
        f"v1:{ACTIONS}:tog:ENV_SECRETS",
    ):
        await senv.click(ADMIN, data)
        assert senv.toasts[-1] == "Нет прав", data
    assert len(senv.denied.calls) == 4
    assert all(uid == staff["admin"].user_id for uid, _ in senv.denied.calls)
    assert senv.service.current()["BOT_MODE"] == "polling"
    assert (await senv.ui_state.get(staff["admin"].user_id)).awaiting is None
    assert await audit(senv) == []


# ---------------------------------------------------------------- undo


async def test_undo_restores_previous_value(senv: SEnv) -> None:
    await add_staff(senv)
    await senv.click(OWNER, key_cb("TRIAL_DAYS"))
    await senv.press(OWNER, "Изменить")
    await senv.type(OWNER, "12")
    undo = senv.button("Отменить")
    await senv.click(OWNER, undo)
    assert "↩️ <b>Отменено</b>" in senv.text
    assert "Отменить" not in " ".join(senv.labels())
    assert senv.service.current()["TRIAL_DAYS"] == 3
    assert await rows(senv, "TRIAL_DAYS") == []


async def test_undo_expires_after_ten_minutes(senv: SEnv) -> None:
    await add_staff(senv)
    clock = FrozenClock()
    set_clock(clock)
    await senv.click(OWNER, key_cb("TRIAL_DAYS"))
    await senv.press(OWNER, "Изменить")
    await senv.type(OWNER, "12")
    undo = senv.button("Отменить")
    clock.advance(timedelta(minutes=10, seconds=1))
    await senv.click(OWNER, undo)
    assert "больше 10 минут" in (senv.toasts[-1] or "")
    assert senv.service.current()["TRIAL_DAYS"] == 12


async def test_undo_of_someone_elses_change_and_forged_ids(senv: SEnv) -> None:
    await add_staff(senv)
    await senv.click(ADMIN, key_cb("TRIAL_DAYS"))
    await senv.press(ADMIN, "Изменить")
    await senv.type(ADMIN, "12")
    undo = senv.button("Отменить")
    # another admin may not undo it
    other = await senv.add(6006, "admin", frozenset({"settings.business"}))
    await senv.click(6006, undo)
    assert senv.toasts[-1] == "Это изменение сделал другой администратор"
    assert senv.denied.calls[-1][0] == other.user_id
    assert senv.service.current()["TRIAL_DAYS"] == 12
    # the owner may
    await senv.click(OWNER, undo)
    assert senv.service.current()["TRIAL_DAYS"] == 3
    # garbage and unknown batch ids
    await senv.click(OWNER, f"v1:{ACTIONS}:undo:not-a-uuid")
    assert senv.toasts[-1] == "Меню обновилось"
    await senv.click(OWNER, f"v1:{ACTIONS}:undo:01900000-0000-7000-8000-000000000000")
    assert senv.toasts[-1] == "Меню обновилось"


async def test_admin_cannot_undo_a_batch_with_owner_keys(senv: SEnv) -> None:
    await add_staff(senv)
    staff_admin = senv.users.by_tg[ADMIN]
    result = await senv.service.apply(
        [Change("BOT_MODE", "polling"), Change("TRIAL_DAYS", "4"), Change("ENV_LAYOUT", "compact")],
        source="bot",
        actor_id=staff_admin.user_id,
    )
    assert "ENV_LAYOUT" in result.applied
    await senv.click(ADMIN, f"v1:{ACTIONS}:undo:{result.batch_id}")
    assert senv.toasts[-1] == "Нет прав"
    assert senv.service.current()["ENV_LAYOUT"] == "compact"


async def test_undo_only_for_bot_batches(senv: SEnv) -> None:
    await add_staff(senv)
    result = await senv.service.apply([Change("TRIAL_DAYS", "4")], source="env_file", actor_id=None)
    await senv.click(OWNER, f"v1:{ACTIONS}:undo:{result.batch_id}")
    assert senv.toasts[-1] == "Меню обновилось"
    assert senv.service.current()["TRIAL_DAYS"] == 4


# ---------------------------------------------------------------- secrets and honest errors


async def test_secret_input_is_deleted_and_never_echoed(senv: SEnv) -> None:
    staff = await add_staff(senv)
    token = "rwToken_" + "Q" * 40 + "Zx9k"
    await senv.click(OWNER, key_cb("REMNAWAVE_TOKEN"))
    await senv.press(OWNER, "Изменить")
    assert "будет удалено сразу после чтения" in senv.text
    assert await senv.type(OWNER, token, message_id=777)
    deletes = [c.message_id for c in senv.transport.of(DeleteMessage)]
    assert 777 in deletes
    assert "Применено" in senv.text
    assert "••••••••Zx9k" in senv.text
    assert token not in senv.all_text()
    assert senv.service.current()["REMNAWAVE_TOKEN"] == token
    stored = (await rows(senv, "REMNAWAVE_TOKEN"))[0]["value"]
    assert token not in stored and stored.startswith('"enc:v1:')
    for row in await audit(senv):
        assert token not in (row["new"] or "") and token not in (row["old"] or "")
    state = await senv.ui_state.get(staff["owner"].user_id)
    assert state.awaiting is None
    ui_rows = await senv.db.raw("select awaiting::text as a, pending_intent::text as p from ui_state")
    assert all(token not in (r["a"] or "") + (r["p"] or "") for r in ui_rows)
    assert senv.component("remnawave").probes[-1]["REMNAWAVE_TOKEN"] == token


async def test_probe_error_is_shown_honestly(senv: SEnv) -> None:
    await add_staff(senv)
    senv.component("remnawave").probe_error = ProbeError("401: токен неверный", "проверьте права токена")
    await senv.click(OWNER, key_cb("REMNAWAVE_URL"))
    await senv.press(OWNER, "Изменить")
    assert await senv.type(OWNER, "https://panel.example.com")
    assert "❌ <b>Не применено</b>" in senv.text
    assert "Причина: 401: токен неверный (проверьте права токена)" in senv.text
    assert "Отменить" not in " ".join(senv.labels())
    assert senv.service.current()["REMNAWAVE_URL"] is None


async def test_reload_component_result(make_senv: EnvFactory) -> None:
    env = await make_senv(registry=extended_registry())
    await add_staff(env)
    await env.click(ADMIN, key_cb("PANEL_LIMIT"))
    await env.press(ADMIN, "Изменить")
    assert await env.type(ADMIN, "8")
    assert "«remnawave» переподключён" in env.text
    assert env.component("remnawave").reconfigs[-1]["PANEL_LIMIT"] == 8


async def test_restart_key_result(senv: SEnv) -> None:
    await add_staff(senv)
    await senv.click(OWNER, key_cb("DATABASE_URL"))
    await senv.press(OWNER, "Изменить")
    assert await senv.type(OWNER, "postgresql://svbg:pw-123456@db:5432/svbg")
    assert "заработает после перезапуска" in senv.text
    assert "pw-123456" not in senv.all_text()
    assert "DATABASE_URL" in senv.service.restart_pending


async def test_database_outage_is_reported(senv: SEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    await add_staff(senv)

    async def down(**kw: Any) -> None:
        raise OperationalError("insert", None, ConnectionError("db is down"))

    monkeypatch.setattr(senv.service.store, "write", down)
    await senv.click(OWNER, key_cb("TRIAL_AUDIENCE"))
    await senv.press(OWNER, "channel_members")
    assert "Не применено" in senv.text
    assert "база данных недоступна" in senv.text
    assert senv.service.current()["TRIAL_AUDIENCE"] == "all"


async def test_slow_probe_is_not_cancelled_by_the_handler_timeout(make_senv: EnvFactory) -> None:
    env = await make_senv(router_kw={"handler_timeout": 0.3})
    await add_staff(env)
    env.component("remnawave").probe_delay = 0.6
    await env.click(OWNER, key_cb("REMNAWAVE_URL"))
    await env.press(OWNER, "Изменить")
    await env.type(OWNER, "https://panel.example.com")
    # The change outlives the handler budget: an honest «Применяется…», not a false «ошибка».
    assert not env.hub.captured
    assert "Применяется" in env.text
    await env.screens.drain()
    assert env.service.current()["REMNAWAVE_URL"] == "https://panel.example.com"
    assert env.component("remnawave").reconfigs  # the pipeline completed: persisted and reconfigured
    # ...and the real result is delivered to the same chat once it is known.
    assert "Применено" in env.text
    assert any("Отменить" in label for label in env.labels())


async def test_drain_timeout_does_not_cancel_a_running_change(make_senv: EnvFactory) -> None:
    env = await make_senv(router_kw={"handler_timeout": 0.3})
    await add_staff(env)
    env.component("remnawave").probe_delay = 0.6
    await env.click(OWNER, key_cb("REMNAWAVE_URL"))
    await env.press(OWNER, "Изменить")
    await env.type(OWNER, "https://panel3.example.com")
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):  # the app's stop step gave up waiting
            await env.screens.drain()
    await env.screens.drain()
    assert env.service.current()["REMNAWAVE_URL"] == "https://panel3.example.com"
    assert env.component("remnawave").reconfigs


async def test_slow_rejection_is_delivered_after_the_handler_budget(make_senv: EnvFactory) -> None:
    env = await make_senv(router_kw={"handler_timeout": 0.3})
    await add_staff(env)
    comp = env.component("remnawave")
    comp.probe_delay = 0.6
    comp.probe_error = ProbeError("панель не отвечает")
    await env.click(OWNER, key_cb("REMNAWAVE_URL"))
    await env.press(OWNER, "Изменить")
    await env.type(OWNER, "https://panel.example.com")
    assert "Применяется" in env.text
    await env.screens.drain()
    assert not env.hub.captured
    assert "Не применено" in env.text and "панель не отвечает" in env.text
    assert env.service.current()["REMNAWAVE_URL"] != "https://panel.example.com"


async def test_slow_set_command_reports_pending_then_result(make_senv: EnvFactory) -> None:
    env = await make_senv(router_kw={"handler_timeout": 0.2}, screens_kw={"command_timeout": 0.4})
    await add_staff(env)
    env.component("remnawave").probe_delay = 0.5
    assert await env.screens.handle_set(text_message(OWNER, "/set REMNAWAVE_URL https://p2.example.com"))
    assert "Применяется" in env.text
    await env.screens.drain()
    assert not env.hub.captured
    assert "Применено" in env.text
    assert env.service.current()["REMNAWAVE_URL"] == "https://p2.example.com"


async def test_concurrent_clicks_do_not_corrupt_state(make_senv: EnvFactory) -> None:
    env = await make_senv(registry=extended_registry())
    await add_staff(env)
    await env.click(OWNER, key_cb("FEATURE_FLAG"))
    data = env.button("Включить")
    await asyncio.gather(*(env.click(OWNER, data) for _ in range(3)))
    # every click toggled through the same pipeline; the value is a bool and audited each time
    assert isinstance(env.service.current()["FEATURE_FLAG"], bool)
    assert len([r for r in await audit(env) if r["key"] == "FEATURE_FLAG"]) == 3


# ---------------------------------------------------------------- history


async def test_history_shows_changes_and_masks_secrets(senv: SEnv) -> None:
    await add_staff(senv)
    token = "historyToken_" + "W" * 30
    await senv.service.apply([Change("REMNAWAVE_TOKEN", token)], source="bot", actor_id=None)
    await senv.service.apply([Change("TRIAL_DAYS", "4")], source="env_file", actor_id=None)
    await senv.service.apply([Change("TRIAL_DAYS", "x")], source="bot", actor_id=None)
    await senv.click(OWNER, f"v1:{SCREEN_HISTORY}:o:TRIAL_DAYS")
    text = senv.text
    assert "История" in text
    assert "✅" in text and "✏️ .env: по умолчанию → 4" in text
    assert "❌" in text and "ожидалось целое число" in text
    await senv.click(OWNER, f"v1:{SCREEN_HISTORY}:o:REMNAWAVE_TOKEN")
    fp = senv.service.crypto.value_fingerprint(token)
    assert fp in senv.text
    assert token not in senv.all_text()
    await senv.click(ADMIN, f"v1:{SCREEN_HISTORY}:o:REMNAWAVE_TOKEN")
    assert "Нет прав" in senv.text


async def test_history_without_database(senv: SEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    await add_staff(senv)

    async def down(*a: Any, **kw: Any) -> None:
        raise OperationalError("select", None, ConnectionError("db is down"))

    monkeypatch.setattr(senv.service.store, "history", down)
    await senv.click(OWNER, f"v1:{SCREEN_HISTORY}:o:TRIAL_DAYS")
    assert "недоступна" in senv.text
    await senv.click(OWNER, key_cb("TRIAL_DAYS"))  # the card still renders without the last change line
    assert "<code>TRIAL_DAYS</code>" in senv.text
