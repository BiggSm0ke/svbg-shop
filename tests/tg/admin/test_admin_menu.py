"""The admin home and its sections: the root by role and right, hubs and back rows, settings slices, the
search by a typed message, «➕ Дни» presets, the user lists, cash desks, staff command menus and the old ids
(real PostgreSQL, real settings service, recording transport)."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from aiogram.methods import DeleteMessage, DeleteMyCommands, SetMyCommands
from aiogram.types import Chat, Message, MessageOriginHiddenUser, MessageOriginUser

from svbg import app_modules
from svbg.core.component import ComponentRegistry, HealthReport, ProbeError
from svbg.core.crypto import Crypto, generate_key
from svbg.core.settings import labels, values
from svbg.core.settings.registry import core_registry
from svbg.core.settings.service import Change, SettingsService
from svbg.tg.admin import nav
from svbg.tg.admin.commands import StaffCommands, commands_for
from svbg.tg.admin.menu import HUBS, ROOT_SECTIONS
from svbg.tg.admin.payments import PaymentScreens
from svbg.tg.admin.settings import SettingsScreens
from svbg.tg.admin.slices import OTHER, SLICES, home_of, pay_slice, target_of
from svbg.tg.ui.codec import MAX_BYTES, encode
from svbg.tg.ui.view import View
from tests.dbkit import CountingDatabase
from tests.tg.admin.settings_harness import FakeComponent
from tests.tg.admin.users.kit import (
    ADMIN,
    ADMIN_STATS,
    OWNER,
    SUPPORT,
    USER,
    UEnv,
    add_instance,
    add_payment,
    add_sub,
    build_uenv,
)
from tests.tg.ui.ui_harness import DATE, text_message, tg_user

PROMO_ADMIN = 8008  # an admin with the right «promo» only
URL = "https://bot.example.com/webhooks/pay/3/secret-token"

#: Screens of modules this harness does not build: stubs with the real guards, so the hubs show them.
STUBS: dict[str, dict[str, Any]] = {
    "plans": {"required_role": "admin", "perm": "plans"},
    "prm": {"required_role": "admin", "perm": "promo"},
    "ads": {"required_role": "admin", "perm": "promo"},
    "dl": {"required_role": "admin", "perm": "deeplinks"},
    "bc": {"required_role": "admin", "perm": "broadcast"},
    "pgs": {"required_role": "admin", "perm": "settings.business"},
    "ce.home": {"required_role": "admin", "perm": "content.edit"},
    "status": {"required_role": "admin", "perm": "system.view"},
    "status.att": {"required_role": "admin", "perm": "system.view"},
    "status.panel": {"required_role": "admin", "perm": "system.view"},
    "ops": {"required_role": "owner"},
    "achat": {"required_role": "owner"},
    "setup.wiz": {"required_role": "owner"},
}


class FakeInstances:
    """``InstanceRegistry`` stand-in: a live instance once the desk is on, with a webhook address."""

    def __init__(self, service: SettingsService) -> None:
        self.service = service

    def by_slug(self, slug: str) -> Any:
        if not self.service.current().get(f"PAY_{slug.upper()}_ENABLED"):
            return None
        return type("Inst", (), {"caps": type("Caps", (), {"webhook": True})()})()

    def webhook_url(self, _inst: Any) -> str:
        return URL


@dataclass
class MEnv:
    env: UEnv
    service: SettingsService
    components: ComponentRegistry
    pay: PaymentScreens

    def __getattr__(self, name: str) -> Any:
        return getattr(self.env, name)


@pytest.fixture
async def menv(db: CountingDatabase, tmp_path: Path) -> MEnv:
    components = ComponentRegistry()
    for name in ("bot", "remnawave", "admin_chat", "payments.rollypay"):
        components.register(FakeComponent(name))
    service = SettingsService(
        db, core_registry(), Crypto([generate_key()]), components, environ={}, env_path=tmp_path / ".env"
    )
    await service.load()
    env = await build_uenv(db, settings_service=service, components=components)
    await env.add(PROMO_ADMIN, "admin", frozenset({"promo"}), first_name="Маркетолог")
    SettingsScreens(env.router, service).install()
    instances = FakeInstances(service)
    pay = PaymentScreens(env.router, service, db=db, components=components, instances=lambda: instances)
    pay.install()

    async def stub(_ctx: Any, _arg: Any) -> View:
        return View(text="stub")

    for code, guard in STUBS.items():
        env.router.screen(code, **guard)(stub)
    return MEnv(env, service, components, pay)


def data_of(env: Any) -> list[str]:
    return [b.callback_data or "" for b in env.buttons()]


async def root(env: Any, tg: int) -> list[str]:
    await env.click(tg, encode(nav.ROOT))
    return env.labels()


def sections(labels: list[str]) -> list[str]:
    names = [e.label for e in ROOT_SECTIONS]
    return [lb for lb in labels if any(lb.startswith(n) for n in names)]


# ------------------------------------------------------------------------------------------ slices (pure)


def test_every_setting_has_exactly_one_home() -> None:
    reg = app_modules.full_registry()
    owner_of: dict[str, str] = {}
    for sl in SLICES.values():
        for key in (*sl.keys, *sl.more):
            assert key not in owner_of, (key, owner_of.get(key), sl.id)
            owner_of[key] = sl.id
    unknown = [k for sl in SLICES.values() for k in (*sl.keys, *sl.more, *sl.mirrors) if reg.find(k) is None]
    assert unknown == []  # no typo in the map
    for defn in reg.all():
        home = home_of(defn)
        assert home != OTHER, f"{defn.key} has no place in the admin (add it to svbg.tg.admin.slices)"
        if defn.section.startswith("payments."):
            slug = defn.section.partition(".")[2]
            assert home == pay_slice(slug) and target_of(home) == ("apay.c", slug), defn.key
    assert home_of(reg.get("PAY_CLOCK_SKEW_ALERT_COUNT")) == "pay.list"
    assert home_of(reg.get("CAPTCHA_ENABLED")) == "u.access"  # the entry captcha lives in «🚪 Вход в бот»


def test_choice_labels_and_presets_fit_their_settings() -> None:
    reg = app_modules.full_registry()
    for key, table in labels.CHOICE_LABELS.items():
        defn = reg.get(key)
        assert defn.kind == "enum" and set(table) == set(defn.choices or ()), key
    for key, ready in labels.PRESETS.items():
        defn = reg.get(key)
        for value in ready:
            assert values.coerce(defn, value) == value, (key, value)
    support = reg.get("SUPPORT_MODE")
    assert values.display(support, "tickets", human=True) == "Чат прямо в боте"
    assert values.display(support, "tickets") == "tickets"  # .env and its notes keep the raw value
    for key in labels.UI_DESCRIPTIONS:
        text = labels.description(reg.get(key))
        assert not re.search(r"\b[A-Z]{3,}_[A-Z_]{3,}\b", text.replace("PUBLIC_URL", "")), key


def test_hub_codes_and_callbacks_fit() -> None:
    for hub in HUBS.values():
        for entry in hub.entries:
            assert len(encode(entry.target, entry.action, entry.arg).encode()) <= MAX_BYTES
    for sl in SLICES.values():
        assert len(encode("set.v", "o", f"{sl.id}:m").encode()) <= MAX_BYTES


# ------------------------------------------------------------------------------------------ root and hubs


async def test_owner_root_shows_every_section_and_live_numbers(menv: MEnv) -> None:
    labels_ = await root(menv, OWNER)
    assert sections(labels_) == [e.label for e in ROOT_SECTIONS if e.target != nav.HUB_MODULES]  # no module
    text = menv.text
    assert text.startswith("🛠 <b>Админка</b>") and "Выручка: сегодня" in text
    assert "пришлите сюда его ID" in text and len(text) < 600
    assert labels_[-2:] == ["🔄 Обновить", "🏠 Меню"]
    assert menv.button("🏠 Меню") == encode("home")  # only the root leads to the user home
    assert menv.button("🔄 Обновить") == encode("adma", "rf")


async def test_support_and_subset_admins_see_only_what_they_can_open(menv: MEnv) -> None:
    assert sections(await root(menv, SUPPORT)) == ["👥 Пользователи"]
    assert "Выручка" not in menv.text
    assert sections(await root(menv, PROMO_ADMIN)) == ["👥 Пользователи", "🎯 Маркетинг"]
    await menv.press(PROMO_ADMIN, "🎯 Маркетинг")
    assert [lb for lb in menv.labels() if lb != "🛠 Админка"] == ["🎟 Промокоды", "📢 Реклама"]
    assert sections(await root(menv, ADMIN_STATS)) == ["👥 Пользователи", "📊 Статистика"]


async def test_admins_never_see_owner_only_entries(menv: MEnv) -> None:
    await root(menv, ADMIN)
    for hub_label, hidden in (
        ("💳 Оплата", "🏦 Кассы"),
        ("📣 Связь", "🛎 Админ-группа"),
        ("⚙️ Система", "🔌 Панель Remnawave"),
        ("⚙️ Система", "💾 Бэкапы и обновления"),
        ("⚙️ Система", "👮 Команда"),
        ("⚙️ Система", "🧰 Сервер и .env"),
    ):
        await menv.click(ADMIN, encode(nav.ROOT))
        await menv.press(ADMIN, hub_label)
        assert hidden not in menv.labels(), (hub_label, hidden)
    await menv.click(OWNER, encode(nav.HUB_SYSTEM))
    for label in ("🔌 Панель Remnawave", "💾 Бэкапы и обновления", "👮 Команда", "🔎 Все настройки"):
        assert label in menv.labels()


@pytest.mark.parametrize("tg", [OWNER, ADMIN, PROMO_ADMIN, ADMIN_STATS, SUPPORT])
async def test_every_visible_button_opens(menv: MEnv, tg: int) -> None:
    """Walk the root, every section and every entry: no «Нет прав», no stale button, no user-home exit."""
    await root(menv, tg)
    targets = [d for d in data_of(menv) if not d.endswith(":home:o")]
    seen: set[str] = set()
    while targets:
        data = targets.pop(0)
        if data in seen or ":adma:" in data or ":ce.a:" in data:
            continue
        seen.add(data)
        assert len(data.encode()) <= MAX_BYTES
        before = len(menv.toasts)
        await menv.click(tg, data)
        new = menv.toasts[before:]
        assert "Нет прав" not in new and "Меню обновилось" not in new, (tg, data, new)
        if data.split(":")[1] in HUBS:  # a section: its entries are walked too, and it never exits home
            assert encode("home") not in data_of(menv), data
            targets += [d for d in data_of(menv) if d not in seen]
    assert len(seen) >= 2


async def test_hub_back_rows_and_breadcrumbs(menv: MEnv) -> None:
    await menv.click(OWNER, encode(nav.HUB_COMM))
    assert menv.text.startswith("🛠 Админка › <b>📣 Связь</b>")
    assert menv.labels()[-1:] == ["🛠 Админка"]  # the parent is the root itself: one button
    await menv.press(OWNER, "🔔 Уведомления клиентам")
    assert "🛠 Админка › 📣 Связь › <b>🔔 Уведомления клиентам</b>" in menv.text
    assert menv.labels()[-2:] == ["⬅️ Связь", "🛠 Админка"]
    await menv.press(OWNER, "⬅️ Связь")
    assert "<b>📣 Связь</b>" in menv.text


async def test_old_ids_keep_working(menv: MEnv) -> None:
    await menv.click(OWNER, encode(nav.ROOT_ALIAS))  # the old content hub's code
    assert menv.text.startswith("🛠 <b>Админка</b>")
    await menv.click(ADMIN, encode("adma", "rf"))  # «🔄 Обновить» of the former dashboard
    assert menv.text.startswith("🛠 <b>Админка</b>") and menv.toasts[-1] == "Обновлено"
    await menv.click(OWNER, encode("settings_root"))
    assert "Все настройки" in menv.text and menv.labels()[0] == "🔎 Поиск"
    assert menv.labels()[-2:] == ["⬅️ Система", "🛠 Админка"]
    await menv.click(OWNER, encode("set.sec", arg="remnawave"))
    assert any("Адрес панели" in lb for lb in menv.labels())
    await menv.click(OWNER, encode("set.key", arg="TRIAL_DAYS"))
    assert "Ключ в .env: <code>TRIAL_DAYS</code>" in menv.text
    await menv.click(OWNER, encode("aua", "days", str(menv.ids[USER])))  # the old «➕ Дни» action
    assert "Сколько дней добавить" in menv.text


# ------------------------------------------------------------------------------------------ slices


async def test_slice_switches_toggle_in_place_with_undo(menv: MEnv) -> None:
    await menv.click(ADMIN, encode("set.v", arg="c.notify"))  # business keys: an admin with the right
    assert "✅ Сообщать об окончании подписки" in menv.labels()
    assert any(lb.startswith("Когда напоминать о конце подписки: ") for lb in menv.labels())
    await menv.press(ADMIN, "Сообщать об окончании подписки")
    assert menv.service.current()["NOTIFY_USER_EXPIRED"] is False
    assert "Готово: «Сообщать об окончании подписки» выключено." in menv.text
    assert menv.labels()[0] == "↩️ Отменить" and "⬜ Сообщать об окончании подписки" in menv.labels()
    assert menv.toasts[-1] == "Выключено"
    await menv.press(ADMIN, "↩️ Отменить")
    assert menv.service.current()["NOTIFY_USER_EXPIRED"] is True
    # forged: a key of another slice, a slice without switches
    await menv.click(ADMIN, encode("set", "vtog", "c.notify:TRIAL_CARRY_OVER"))
    assert menv.toasts[-1] == "Эта настройка не переключается"
    await menv.click(ADMIN, encode("set", "vtog", "p.trial:TRIAL_CARRY_OVER"))
    assert menv.toasts[-1] == "Меню обновилось"
    await menv.click(ADMIN_STATS, encode("set.v", arg="c.notify"))
    assert menv.toasts[-1] == "Нет прав"


async def test_slice_shows_labels_more_and_links_back(menv: MEnv) -> None:
    await menv.click(OWNER, encode("set.v", arg="p.trial"))
    labels_ = menv.labels()
    assert "Дней пробного периода: 3" in labels_ and "Кому доступен триал: Всем" in labels_
    assert "🧰 Ещё (1)" in labels_ and not any("Переносить" in lb for lb in labels_)
    assert labels_[-2:] == ["⬅️ Тарифы", "🛠 Админка"]
    await menv.press(OWNER, "🧰 Ещё")
    assert any(lb.startswith("Переносить остаток триала") for lb in menv.labels())
    await menv.press(OWNER, "Дней пробного периода")
    assert menv.button("⬅️ Пробный период") == encode("set.v", arg="p.trial")
    await menv.press(OWNER, "✅ 3")  # a ready value is applied like any change
    await menv.click(OWNER, encode("set.key", arg="TRIAL_DAYS"))
    await menv.press(OWNER, "7")
    assert menv.service.current()["TRIAL_DAYS"] == 7
    assert menv.button("⬅️ Пробный период") == encode("set.v", arg="p.trial")
    # from the registry tree the card goes back to its section
    await menv.click(OWNER, encode("set.key", arg="TRIAL_DAYS@t"))
    assert menv.button("Назад") == encode("set.sec", arg="sales")


async def test_entry_slice_holds_the_captcha_and_the_channel(menv: MEnv) -> None:
    await menv.click(OWNER, encode(nav.HUB_USERS))
    await menv.press(OWNER, "🚪 Вход в бот")
    labels_ = menv.labels()
    assert "✅ Капча при входе" in labels_
    assert any(lb.startswith("Обязательный канал") for lb in labels_)
    assert any(lb.startswith("Согласие с правилами при старте: Не спрашивать") for lb in labels_)


async def test_all_settings_tree_hides_the_cash_desks(menv: MEnv) -> None:
    await menv.click(OWNER, encode("set.sec", arg="payments"))
    assert "🏦 Кассы" in menv.labels()
    assert not any("Платёжка «" in lb for lb in menv.labels())
    await menv.click(OWNER, encode("set.sec", arg="payments.rollypay"))  # old buttons still open it
    assert any("API-ключ" in lb for lb in menv.labels())


# ------------------------------------------------------------------------------------------ quick actions


async def test_maintenance_and_restart_quick_actions(menv: MEnv) -> None:
    await menv.service.apply([Change("MAINTENANCE_MODE", "on")], source="bot", actor_id=None)
    menv.service.restart_pending.add("DATABASE_URL")
    labels_ = await root(menv, OWNER)
    assert "🛠 Техработы включены · выключить" in labels_ and "♻️ Ждут перезапуска: 1" in labels_
    assert "🛠 Техработы включены · выключить" not in await root(menv, ADMIN)  # owner only
    await root(menv, OWNER)
    await menv.press(OWNER, "Техработы включены")
    assert menv.service.current()["MAINTENANCE_MODE"] == "auto"
    assert menv.toasts[-1] == "Техработы выключены"
    assert "🛠 Техработы включены · выключить" not in menv.labels()


async def test_badges_from_the_cached_numbers(menv: MEnv) -> None:
    inst = await add_instance(menv.db, "Перевод", "manual")
    pid = await add_payment(menv.db, inst, menv.ids[USER], 179_00, status="pending")
    await menv.db.raw(
        "insert into manual_receipts (payment_id, user_id, amount_minor, currency) "
        "values ($1, $2, 17900, 'RUB')",
        pid,
        menv.ids[USER],
    )
    await menv.db.raw(
        "insert into attention_items (dedup_key, severity, title) values ('x', 'warn', 'Касса')"
    )
    await menv.dashboard.get(refresh=True)
    labels_ = await root(menv, OWNER)
    assert "💳 Оплата · 🧾 1" in labels_ and "⚙️ Система ⚠️" in labels_
    assert "⚠️ Требует внимания · 1" in labels_
    await menv.press(OWNER, "💳 Оплата")
    await menv.press(OWNER, "🧾 Ждут подтверждения")
    assert "Ждут подтверждения" in menv.text
    assert any("Иван" in lb and "179" in lb for lb in menv.labels())
    await menv.click(PROMO_ADMIN, encode("apay.rc"))
    assert menv.toasts[-1] == "Нет прав"


async def test_root_click_costs_no_sql_when_cached(menv: MEnv) -> None:
    await root(menv, OWNER)
    before = menv.db.queries
    await root(menv, OWNER)
    assert menv.db.queries - before <= 1  # the numbers come from the dashboard cache


# ------------------------------------------------------------------------------------------ search


def forwarded(tg: int, origin: Any, message_id: int = 600) -> Message:
    return Message(
        message_id=message_id,
        date=DATE,
        chat=Chat(id=tg, type="private"),
        from_user=tg_user(tg),
        text="привет",
        forward_origin=origin,
    )


async def test_typed_id_username_or_forward_finds_a_person(menv: MEnv) -> None:
    menu = menv.menu
    msg = text_message(OWNER, "5005", message_id=501)
    assert not menu.wants(msg)  # the admin was not opened yet: an ordinary message
    await root(menv, OWNER)
    assert menu.wants(msg) and await menu.handle_search(msg)
    assert "Иван" in menv.text and "Telegram ID: <code>5005</code>" in menv.text
    assert [m.message_id for m in menv.transport.of(DeleteMessage)] == [501]  # the query is removed
    assert await menu.handle_search(text_message(OWNER, "@ivan_petrov", message_id=502))
    assert "Иван" in menv.text
    origin = MessageOriginUser(type="user", date=DATE, sender_user=tg_user(5005))
    assert await menu.handle_search(forwarded(OWNER, origin))
    assert "Telegram ID: <code>5005</code>" in menv.text
    hidden = MessageOriginHiddenUser(type="hidden_user", date=DATE, sender_user_name="Иван")
    assert menu.query_of(forwarded(OWNER, hidden)) == "Иван"
    assert await menu.handle_search(text_message(OWNER, "@nobody_like_this", message_id=503))
    assert "никого не нашлось" in menv.text and menv.text.startswith("🛠 <b>Админка</b>")


async def test_search_leaves_other_messages_alone(menv: MEnv) -> None:
    menu = menv.menu
    await root(menv, OWNER)
    assert not menu.wants(text_message(OWNER, "/start"))
    assert not menu.wants(text_message(OWNER, "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.c2lnbmF0dXJlXzEyMw"))
    menu.seen_callback(OWNER, encode(nav.ROOT))  # the root again: still searching
    assert menu.wants(text_message(OWNER, "5005"))
    menu.seen_callback(OWNER, encode("au.find"))  # any other admin button ends the search mode
    assert not menu.wants(text_message(OWNER, "5005"))
    await menv.click(OWNER, encode(nav.HUB_USERS))  # «👥 Пользователи» arms it again
    assert menu.wants(text_message(OWNER, "5005"))
    await menv.press(OWNER, "🔍 Найти")
    assert not await menu.handle_search(text_message(OWNER, "5005"))  # the search form waits for it
    # a plain user is never searched for, even with a stale entry
    menu._recent[USER] = menu._clock()
    assert not await menu.handle_search(text_message(USER, "5005"))
    # 15 minutes later the root no longer takes text
    await root(menv, SUPPORT)
    menu._recent[SUPPORT] -= 16 * 60
    assert not menu.wants(text_message(SUPPORT, "5005"))


# ------------------------------------------------------------------------------------------ users


async def test_days_presets_then_reason(menv: MEnv) -> None:
    uid = menv.ids[USER]
    sid = await add_sub(menv.db, uid, days=10, short_uuid="AbCdEf123")
    before = (await menv.db.raw("select paid_until from subscriptions where id = $1", sid))[0]["paid_until"]
    await menv.click(ADMIN, encode("au", arg=str(uid)))
    copy = next(b for b in menv.buttons() if b.text == "📋 Ссылка")
    assert copy.copy_text is not None and copy.copy_text.text == "https://sub.example/AbCdEf123"
    assert menv.labels()[-2:] == ["⬅️ Пользователи", "🛠 Админка"]
    await menv.press(ADMIN, "➕ Дни")
    assert menv.labels()[:8] == ["+1", "+3", "+7", "+30", "+90", "−1", "−7", "✏️ Своё"]
    await menv.press(ADMIN, "+30")
    assert menv.text.startswith("➕ +30 дн.") and "Причина" in menv.text
    await menv.type(ADMIN, "компенсация за сбой")
    assert "✅ Готово: +30 дн." in menv.text
    after = (await menv.db.raw("select paid_until from subscriptions where id = $1", sid))[0]["paid_until"]
    assert after - before == timedelta(days=30)
    await menv.click(ADMIN, encode("aua", "dp", f"{uid}:5"))  # not a ready value: forged
    assert menv.toasts[-1] == "Пользователь не найден"
    await menv.click(SUPPORT, encode("au.days", arg=str(uid)))
    assert menv.toasts[-1] == "Нет прав"


async def test_user_lists(menv: MEnv) -> None:
    uid = menv.ids[USER]
    inst = await add_instance(menv.db)
    await add_payment(menv.db, inst, uid, 49_900)
    await menv.db.raw("update users set banned_at = now() where id = $1", uid)
    await menv.click(SUPPORT, encode(nav.HUB_USERS))
    hub = [lb for lb in menv.labels() if lb != "🛠 Админка"]
    assert hub == ["🔍 Найти", "🆕 Новые", "📋 Все пользователи"]
    await menv.press(SUPPORT, "🆕 Новые")
    assert "<b>🆕 Новые</b>" in menv.text and any(lb.startswith("Иван @ivan_petrov") for lb in menv.labels())
    await menv.click(OWNER, encode("au.paid"))
    assert any("Иван" in lb and "499" in lb for lb in menv.labels())
    await menv.click(OWNER, encode("au.ban"))
    assert any(lb.startswith("Иван") for lb in menv.labels())
    await menv.press(OWNER, "Иван")
    assert "Telegram ID: <code>5005</code>" in menv.text
    await menv.click(SUPPORT, encode("au.paid"))
    assert menv.toasts[-1] == "Нет прав"


# ------------------------------------------------------------------------------------------ cash desks


async def test_cash_desk_enable_flow(menv: MEnv) -> None:
    await menv.click(OWNER, encode(nav.HUB_PAY))
    await menv.press(OWNER, "🏦 Кассы")
    assert "Пока ни одна касса не включена." in menv.text
    await menv.press(OWNER, "➕ Подключить кассу")
    await menv.press(OWNER, "RollyPay")
    text = menv.text
    assert "Сейчас: ⏸ выключена" in text
    assert "1. API-ключ: не задано" in text and "2. Секрет подписи вебхуков: не задано" in text
    assert "«RollyPay»:" not in " ".join(menv.labels())  # titles without the desk's prefix
    assert "▶️ Включить (сначала заполните поля)" in menv.labels()
    assert any(b.url and "docs/providers/rollypay.md" in b.url for b in menv.buttons())
    await menv.press(OWNER, "▶️ Включить")
    assert "Сначала заполните" in (menv.toasts[-1] or "")
    assert menv.service.current()["PAY_ROLLYPAY_ENABLED"] is False

    await menv.service.apply(
        [Change("PAY_ROLLYPAY_API_KEY", "key-" + "x" * 30), Change("PAY_ROLLYPAY_SIGNING_SECRET", "s" * 32)],
        source="bot",
        actor_id=None,
    )
    comp = menv.components.get("payments.rollypay")
    assert isinstance(comp, FakeComponent)
    comp.probe_error = ProbeError("ключ не подошёл")
    await menv.click(OWNER, encode("apay.c", arg="rollypay"))
    assert "1. API-ключ: ••••" in menv.text and "▶️ Включить" in menv.labels()
    await menv.press(OWNER, "▶️ Включить")
    assert "Не получилось" in menv.text and "ключ не подошёл" in menv.text
    assert menv.service.current()["PAY_ROLLYPAY_ENABLED"] is False

    comp.probe_error = None
    await menv.press(OWNER, "▶️ Включить")
    assert menv.service.current()["PAY_ROLLYPAY_ENABLED"] is True
    text = menv.text
    assert "Касса включена." in text and "Сейчас: ✅ работает" in text
    assert f"<code>{URL}</code>" in text
    copy = next(b for b in menv.buttons() if b.text == "📋 Скопировать адрес")
    assert copy.copy_text is not None and copy.copy_text.text == URL
    assert menv.toasts[-1] == "Касса включена."

    comp.health_report = HealthReport.degraded("касса не отвечает")
    await menv.press(OWNER, "⬅️ Кассы")
    assert any(lb.startswith("⚠️ RollyPay") for lb in menv.labels())
    await menv.press(OWNER, "RollyPay")
    await menv.press(OWNER, "🧪 Тестовый режим: выкл")
    assert menv.service.current()["PAY_ROLLYPAY_TEST_MODE"] is True
    await menv.press(OWNER, "⏸ Выключить")
    assert menv.service.current()["PAY_ROLLYPAY_ENABLED"] is False
    assert "📋 Скопировать адрес" not in menv.labels()
    # the desk's keys go back to its card
    await menv.click(OWNER, encode("set.key", arg="PAY_ROLLYPAY_API_KEY"))
    assert menv.button("⬅️ Касса") == encode("apay.c", arg="rollypay")
    await menv.click(ADMIN, encode("apay"))
    assert menv.toasts[-1] == "Нет прав"


# ------------------------------------------------------------------------------------------ command menus


def test_command_menus_by_role() -> None:
    def names(role: str, perms: frozenset[str] = frozenset()) -> list[str]:
        return [c.command for c in commands_for(role, perms)]

    assert names("owner") == ["admin", "user", "broadcast", "plans", "promos", "status", "settings"]
    assert names("support") == ["admin", "user"]
    assert names("admin", frozenset({"promo"})) == ["admin", "user", "promos"]
    assert names("user") == []


async def test_command_menus_on_start_and_after_a_role_change(menv: MEnv) -> None:
    sent: list[Any] = []

    async def call(method: Any, chat_id: int) -> bool:
        sent.append((chat_id, method))
        return True

    staff = StaffCommands(call, db=menv.db, configured_owners=lambda: frozenset({7007})).attach(menv.router)
    assert await staff.sync_all() == 6  # 5 stored staff + the owner from the settings
    by_chat = {chat: m for chat, m in sent}
    assert isinstance(by_chat[SUPPORT], SetMyCommands)
    assert [c.command for c in by_chat[SUPPORT].commands] == ["admin", "user"]
    assert by_chat[SUPPORT].scope.chat_id == SUPPORT
    assert USER not in by_chat  # users keep the default menu

    sent.clear()
    await menv.click(OWNER, encode("rla", "save", f"{menv.ids[SUPPORT]}:user:0"))  # support → plain user
    await asyncio.sleep(0.05)
    for _ in range(50):
        if sent:
            break
        await asyncio.sleep(0.02)
    assert [(chat, type(m)) for chat, m in sent] == [(SUPPORT, DeleteMyCommands)]


async def test_command_menus_survive_any_failure(menv: MEnv) -> None:
    """A refused or broken call (no bot yet, a closed notifier) skips one person, never the rest."""
    calls: list[int] = []

    async def call(_method: Any, chat_id: int) -> bool:
        calls.append(chat_id)
        if len(calls) == 1:
            raise RuntimeError("bot is not configured")
        return True

    staff = StaffCommands(call, db=menv.db, configured_owners=lambda: frozenset({7007}))
    assert await staff.sync_all() == 5 and len(calls) == 6

    async def broken() -> None:
        raise ValueError("boom")

    staff.spawn(broken())  # a failed background sync is logged, not left unretrieved
    for _ in range(3):
        await asyncio.sleep(0)
    assert not staff._tasks


async def test_a_command_ends_the_search_mode(menv: MEnv) -> None:
    """/start (home, a support question may follow) or /broadcast leave the admin: the next text is not a
    search query and stays for the support or the composer."""
    menu = menv.menu
    await root(menv, OWNER)
    assert menu.wants(text_message(OWNER, "5005"))
    menu.seen_message(text_message(OWNER, "/start"))
    assert not menu.wants(text_message(OWNER, "5005"))
    await root(menv, OWNER)  # /admin or the root again arms it
    menu.seen_message(text_message(OWNER, "5005"))  # plain text keeps it
    assert menu.wants(text_message(OWNER, "5005"))


async def test_slice_links_to_a_module_that_is_not_wired_are_hidden(menv: MEnv) -> None:
    await menv.click(OWNER, encode("set.v", arg="l.media"))
    assert not any("Заглушка" in lb for lb in menv.labels())  # the constructor is not built here

    async def stub(_ctx: Any, _arg: Any) -> None:
        return None

    menv.router.action("ce.a", "bnx", required_role="admin", perm="content.edit")(stub)
    await menv.click(OWNER, encode("set.v", arg="l.media"))
    assert any("Заглушка: убрать" in lb for lb in menv.labels())
    assert not any("Заглушка: вернуть" in lb for lb in menv.labels())


async def test_slice_back_skips_sections_closed_to_the_viewer(menv: MEnv) -> None:
    """An admin with «settings.business» only reaches «🎁 Пробный период» or «📰 Ежедневный отчёт» from «Все
    настройки»; «⬅️» must not lead into «📦 Тарифы» or «📊 Статистика», which answer «Нет прав» to them."""
    clerk = 8009
    await menv.env.add(clerk, "admin", frozenset({"settings.business"}), first_name="Настройщик")
    for sid in ("p.trial", "st.report"):
        await menv.click(clerk, encode("set.v", arg=sid))
        assert menv.labels()[-1:] == ["🛠 Админка"] and not menv.labels()[-2].startswith("⬅️"), sid
    await menv.click(OWNER, encode("set.v", arg="p.trial"))  # the owner keeps the way to the section
    assert menv.labels()[-2:] == ["⬅️ Тарифы", "🛠 Админка"]
