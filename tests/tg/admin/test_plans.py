"""«📦 Тарифы»: list, card, create, prices, devices, traffic, availability, trial, sale, locations, access."""

from __future__ import annotations

import pytest

from svbg.catalog.model import GIB, DeviceAddon
from svbg.remnawave.errors import ErrorKind, RemnawaveError
from svbg.tg.admin.plans import ACTIONS, SCREEN_CARD, SCREEN_LIST, SCREEN_LOCATIONS
from svbg.tg.ui.codec import encode
from tests.catalog.kit import (
    ADMIN,
    ADMIN_NOPERM,
    OWNER,
    SQ_DE,
    SQ_NL,
    SUPPORT,
    USER,
    PEnv,
    add_location,
    add_plan,
    build_penv,
    squad,
)
from tests.dbkit import CountingDatabase
from tests.tg.ui.ui_harness import text_message


@pytest.fixture
async def env(db: CountingDatabase) -> PEnv:
    penv = await build_penv(db)
    await penv.staff()
    return penv


async def reload(env: PEnv) -> None:
    await env.catalog.reload()


async def audit_actions(env: PEnv) -> list[str]:
    return [r["action"] for r in await env.db.raw("select action from admin_audit order by id")]


# ------------------------------------------------------------------------------------------- access


@pytest.mark.parametrize("who", [ADMIN_NOPERM, SUPPORT, USER], ids=["admin-without-plans", "support", "user"])
async def test_denied_without_the_plans_permission(env: PEnv, who: int) -> None:
    pid = await add_plan(env.db, "std")
    await reload(env)
    for data in (
        encode(SCREEN_LIST),
        encode(SCREEN_CARD, arg=str(pid)),
        encode(ACTIONS, "en", str(pid)),
        encode(ACTIONS, "preset"),
        encode(ACTIONS, "sqy", f"{pid}:1:abcdef:1"),
    ):
        await env.click(who, data)
        assert env.toasts[-1] == "Нет прав"
    assert not env.rendered()
    assert (await env.db.raw("select enabled from plans where id = $1", pid))[0]["enabled"] is True
    assert len(env.denied) == 5


@pytest.mark.parametrize("who", [OWNER, ADMIN], ids=["owner", "admin-with-plans"])
async def test_allowed_roles(env: PEnv, who: int) -> None:
    await env.click(who, encode(SCREEN_LIST))
    assert "Тарифы" in env.text and env.toasts[-1] != "Нет прав"


async def test_plans_command(env: PEnv) -> None:
    assert await env.screens.handle_command(text_message(OWNER, "/plans"))
    assert "Тарифы" in env.text
    assert not await env.screens.handle_command(text_message(ADMIN_NOPERM, "/plans"))
    assert not await env.screens.handle_command(text_message(OWNER, "/plans", chat_type="group"))


# ------------------------------------------------------------------------------------------- list, preset


async def test_empty_list_offers_the_preset(env: PEnv) -> None:
    await add_location(env.db, SQ_NL, "NL")
    await reload(env)
    await env.click(OWNER, encode(SCREEN_LIST))
    assert "Тарифов пока нет" in env.text
    await env.press(OWNER, "Пресет")
    assert "Пресет добавлен." in env.text and "Стандарт" in env.text
    assert "30 дн. — 179 ₽ · 90 дн. — 499 ₽ · 180 дн. — 899 ₽ · 360 дн. — 1 699 ₽" in env.text
    assert "5 включено" in env.text and "19 ₽ за 30 дн., всего до 15" in env.text and "безлимит" in env.text
    assert "🟢 В продаже" in env.text
    await env.click(OWNER, encode(SCREEN_LIST))
    assert "Пресет" not in " ".join(env.labels())
    assert any("🎁 Пробный" in lb for lb in env.labels())
    assert any("🟢 Стандарт · 179 ₽–1 699 ₽" in lb for lb in env.labels())
    assert "plan.preset" in await audit_actions(env)
    await env.click(OWNER, encode(ACTIONS, "preset"))  # forged second press
    assert env.toasts[-1] == "Тариф «Стандарт» уже есть"


async def test_preset_without_locations_is_hidden(env: PEnv) -> None:
    await env.click(OWNER, encode(ACTIONS, "preset"))
    assert "выберите сквады и включите продажу" in env.text and "⏸ Скрыт из продажи" in env.text


# ------------------------------------------------------------------------------------------- create, edit


async def test_create_plan_and_put_it_on_sale(env: PEnv) -> None:
    await add_location(env.db, SQ_NL, "NL")
    await reload(env)
    await env.click(ADMIN, encode(SCREEN_LIST))
    await env.press(ADMIN, "Новый тариф")
    assert "Название нового тарифа" in env.text
    await env.type(ADMIN, "Максимум")
    assert "Максимум" in env.text and "<code>maksimum</code>" in env.text and "⏸ Скрыт" in env.text
    pid = int((await env.db.raw("select id from plans where code = 'maksimum'"))[0]["id"])
    await env.press(ADMIN, "В продажу")
    assert env.toasts[-1] == "Сначала выберите сквады тарифа"
    # squads
    await env.press(ADMIN, "Сквады")
    await env.press(ADMIN, "NL")
    await env.press(ADMIN, "Сохранить")
    assert "Сквады сохранены" in env.text and "Сквады: NL" in env.text
    await env.press(ADMIN, "В продажу")
    assert env.toasts[-1] == "Сначала добавьте хотя бы одну цену"
    # prices
    await env.press(ADMIN, "Цены")
    await env.press(ADMIN, "Добавить")
    await env.type(ADMIN, "0")
    assert "Минимум 1" in env.text
    await env.type(ADMIN, "30")
    await env.type(ADMIN, "сто")
    assert "Нужна сумма" in env.text
    await env.type(ADMIN, "199,50")
    assert any("30 дн. — 199,50 ₽" in lb for lb in env.labels())
    await env.press(ADMIN, "30 дн.")  # highlight
    assert "⭐ 30 дн." in " ".join(env.labels())
    await env.press(ADMIN, "⬅️ Тариф")
    await env.press(ADMIN, "В продажу")
    assert "🟢 В продаже" in env.text
    await reload(env)
    plan = env.catalog.snapshot.plan(pid)
    assert (
        plan is not None
        and plan.enabled
        and plan.prices[0].amount_minor == 19950
        and plan.prices[0].highlight
    )
    assert {"plan.create", "plan.price", "plan.highlight", "plan.update", "plan.squads"} <= set(
        await audit_actions(env)
    )


async def test_rename_devices_addon_traffic_tag(env: PEnv) -> None:
    pid = await add_plan(env.db, "std", device_limit=3)
    await reload(env)
    await env.click(OWNER, encode(SCREEN_CARD, arg=str(pid)))
    await env.press(OWNER, "Название")
    await env.type(OWNER, "Семейный")
    assert "📦 <b>Семейный</b>" in env.text
    await env.press(OWNER, "Устройства")
    await env.press(OWNER, "Сколько устройств")
    await env.type(OWNER, "101")
    assert "Максимум 100" in env.text
    await env.type(OWNER, "5")
    assert "5 включено" in env.text
    await env.press(OWNER, "Доплата за доп. устройство")
    await env.type(OWNER, "19")
    await env.click(OWNER, encode("form", "skip"))
    assert "19 ₽ за 30 дн." in env.text and "всего до" not in env.text
    await env.press(OWNER, "Доплата за доп. устройство")
    await env.type(OWNER, "19")
    await env.type(OWNER, "15")
    assert "19 ₽ за 30 дн., всего до 15" in env.text
    await reload(env)
    assert env.catalog.snapshot.plan(pid).addon == DeviceAddon(1900, 30, 15, "RUB")  # type: ignore[union-attr]
    await env.press(OWNER, "Как в панели")
    assert "как в панели" in env.text and "Доплата за устройства: выключена" in env.text  # no base: no addon
    await env.press(OWNER, "Выключить доплату")
    await reload(env)
    assert env.catalog.snapshot.plan(pid).device_addon is None  # type: ignore[union-attr]
    await env.press(OWNER, "⬅️ Тариф")
    await env.press(OWNER, "Трафик")
    await env.type(OWNER, "100")
    assert "📶 Трафик: 100 ГБ" in env.text
    await reload(env)
    assert env.catalog.snapshot.plan(pid).traffic_bytes == 100 * GIB  # type: ignore[union-attr]
    await env.press(OWNER, "Сброс трафика")
    await env.press(OWNER, "каждый месяц, 1-го")
    assert "сброс: каждый месяц, 1-го числа" in env.text
    await env.press(OWNER, "Тег")
    await env.type(OWNER, "bad tag")
    assert "Тег:" in env.text
    await env.type(OWNER, "paid")
    assert "Тег в панели: PAID" in env.text
    await env.press(OWNER, "Тег")
    await env.type(OWNER, "-")
    assert "Тег в панели: нет" in env.text


async def test_availability_link_shows_the_deep_link(env: PEnv) -> None:
    pid = await add_plan(env.db, "vip")
    await reload(env)
    await env.click(ADMIN, encode(SCREEN_CARD, arg=str(pid)))
    await env.press(ADMIN, "Доступность")
    await env.press(ADMIN, "только по ссылке")
    assert "https://t.me/svbg_bot?start=plan_vip" in env.text
    await env.click(ADMIN, encode(ACTIONS, "av", f"{pid}:friends"))  # forged value
    assert env.toasts[-1] == "Тариф не найден"


async def test_trial_flag_is_unique(env: PEnv) -> None:
    first = await add_plan(env.db, "trial", is_trial=True, prices=())
    second = await add_plan(env.db, "std")
    await reload(env)
    await env.click(OWNER, encode(SCREEN_CARD, arg=str(first)))
    assert "🎁 Пробный тариф · включён" in env.text and "Цены" not in " ".join(env.labels())
    await env.click(OWNER, encode(SCREEN_CARD, arg=str(second)))
    await env.press(OWNER, "Сделать пробным")
    assert env.toasts[-1] == "Пробный тариф уже есть: сначала снимите отметку с него"
    await env.click(OWNER, encode(SCREEN_CARD, arg=str(first)))
    await env.press(OWNER, "Снять отметку")
    await env.click(OWNER, encode(SCREEN_CARD, arg=str(second)))
    await env.press(OWNER, "Сделать пробным")
    await reload(env)
    assert env.catalog.snapshot.trial.id == second  # type: ignore[union-attr]


async def test_hide_and_delete_price(env: PEnv) -> None:
    pid = await add_plan(env.db, "std", prices=((30, 17900), (90, 49900)))
    await reload(env)
    await env.click(OWNER, encode(SCREEN_CARD, arg=str(pid)))
    await env.press(OWNER, "Скрыть из продажи")
    assert "⏸ Скрыт из продажи" in env.text
    await env.press(OWNER, "Цены")
    await env.click(OWNER, env.buttons()[1].callback_data or "")  # 🗑 of the 30-day price
    assert not any("30 дн." in lb for lb in env.labels()) and any("90 дн." in lb for lb in env.labels())
    await env.click(OWNER, encode(ACTIONS, "pdel", f"{pid}:30"))  # repeated: nothing to delete, no error
    await env.click(OWNER, encode(ACTIONS, "pdel", "garbage"))
    assert env.toasts[-1] == "Тариф не найден"


async def test_unknown_and_forged_arguments(env: PEnv) -> None:
    for data in (
        encode(SCREEN_CARD, arg="999"),
        encode(SCREEN_CARD, arg="1; drop"),
        encode("pl.sq", arg="1:zz:abcdef"),
    ):
        await env.click(OWNER, data)
        assert "Тарифы" in env.text
    await env.click(OWNER, encode(ACTIONS, "en", "abc"))
    assert env.toasts[-1] == "Тариф не найден"


async def test_screens_cost_no_sql(env: PEnv) -> None:
    await add_location(env.db, SQ_NL, "NL")
    await add_location(env.db, SQ_DE, "DE")
    pid = await add_plan(env.db, "std", squads=(SQ_NL,))
    await reload(env)
    await env.click(OWNER, encode(SCREEN_LIST))  # warm-up: main message id stored once
    for data in (
        encode(SCREEN_CARD, arg=str(pid)),
        encode("pl.pr", arg=str(pid)),
        encode("pl.sq", arg=str(pid)),
        encode("pl.dv", arg=str(pid)),
        encode("pl.av", arg=str(pid)),
        encode(SCREEN_LOCATIONS),
        encode(SCREEN_LIST),
    ):
        before = env.db.queries
        await env.click(OWNER, data)
        assert env.db.queries == before, data
    await env.click(OWNER, encode("pl.sq", arg=str(pid)))
    before = env.db.queries
    await env.press(OWNER, "DE")  # a checkbox toggle is a pure render
    assert env.db.queries == before


# ------------------------------------------------------------------------------------------- locations


async def test_locations_sync_and_edit(env: PEnv) -> None:
    env.source.squads = [squad(SQ_NL, "NL-panel", 0, 7), squad(SQ_DE, "DE-panel", 1)]
    await env.click(OWNER, encode(SCREEN_LOCATIONS))
    assert "Локаций пока нет" in env.text
    await env.press(OWNER, "Обновить из панели")
    assert "локаций 2, новых 2" in env.text and env.source.calls == 1
    await env.press(OWNER, "NL-panel")
    assert "Пользователей: 7" in env.text
    await env.press(OWNER, "Название")
    await env.type(OWNER, "Нидерланды")
    await env.press(OWNER, "Флаг")
    await env.type(OWNER, "два слова")
    assert "эмодзи" in env.text
    await env.type(OWNER, "🇳🇱")
    assert "🇳🇱 Нидерланды" in env.text
    await env.press(OWNER, "Убрать флаг")
    assert "<b>Нидерланды</b>" in env.text
    assert (await audit_actions(env)).count("location.update") == 3
    await env.click(OWNER, encode("loc", arg="no-such-squad"))
    assert "Локации" in env.text


async def test_sync_failures_are_toasts(db: CountingDatabase) -> None:
    env = await build_penv(db, panel=False)
    await env.staff()
    await env.click(OWNER, encode(ACTIONS, "sync"))
    assert env.toasts[-1] == "Панель не подключена — подключите её в /setup"
    env2 = await build_penv(db)
    env2.users = env.users
    env2.router.user_loader = env.users
    env2.source.error = RemnawaveError(ErrorKind.AUTH, 401, None, "неверный токен")
    await env2.click(OWNER, encode(ACTIONS, "sync"))
    assert "Панель ответила ошибкой: неверный токен" in (env2.toasts[-1] or "")


async def test_setup_entry_point(db: CountingDatabase) -> None:
    from types import SimpleNamespace

    from aiogram import Router

    from svbg.tg.admin.plans import setup
    from svbg.tg.ui.router import UiStateStore
    from tests.tg.ui.ui_harness import FakeHub, FakeTransport, Users, make_router

    await add_plan(db, "std", prices=((30, 300),), currency="USD")
    users = Users()
    router = make_router(FakeTransport(), users, UiStateStore(db), None, None, FakeHub())
    deps = SimpleNamespace(
        db=db,
        settings=SimpleNamespace(current={"CURRENCY": "USD"}),
        remnawave=SimpleNamespace(configured=False),
    )
    assert isinstance(await setup(router, deps), Router)
    from svbg.tg.ui.context import UserCtx
    from tests.dbkit import add_user

    users.by_tg[OWNER] = UserCtx(await add_user(db, OWNER, "owner"), telegram_id=OWNER, role="owner")
    await router.show(users.by_tg[OWNER], OWNER, SCREEN_LIST, new=True)
    sent = router.transport.calls[-1]  # type: ignore[attr-defined]
    labels = [b.text for row in sent.reply_markup.inline_keyboard for b in row]
    assert any("· 3 $" in lb for lb in labels), labels  # the shop currency comes from the settings


async def test_preset_only_for_a_rouble_shop(db: CountingDatabase) -> None:
    env = await build_penv(db, currency="USD")
    await env.staff()
    await env.click(OWNER, encode(SCREEN_LIST))
    assert not any("Пресет" in lb for lb in env.labels())
    await env.click(OWNER, encode(ACTIONS, "preset"))
    assert "валюта магазина другая" in (env.toasts[-1] or "")
    assert await env.db.raw("select id from plans") == []
