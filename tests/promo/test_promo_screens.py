"""«🎟 Промокоды» (owner's editor) and the user's «🎟 Промокод» on the real router and PostgreSQL."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from svbg.promo.service import PromoService
from svbg.promo.user import SCREEN as USER_SCREEN
from svbg.promo.user import PromoUserScreens
from svbg.tg.admin.promo import ACTIONS, SCREEN_CARD, SCREEN_KINDS, SCREEN_LIST, PromoAdminScreens
from svbg.tg.ui.codec import encode
from tests.dbkit import CountingDatabase
from tests.promo.kit import FakeCatalog, add_promo, add_sub
from tests.promo.ui_kit import ADMIN, ADMIN_NOPERM, OWNER, SUPPORT, USER, UiEnv, build_ui, text_message


@pytest.fixture
async def env(db: CountingDatabase, service: PromoService, catalog: FakeCatalog) -> UiEnv:
    ui = await build_ui(db)
    PromoAdminScreens(ui.router, service, catalog=catalog).install()
    PromoUserScreens(service).register(ui.router)
    await ui.staff("promo")
    return ui


async def audit(env: UiEnv) -> list[str]:
    return [r["action"] for r in await env.db.raw("select action from admin_audit order by id")]


# ------------------------------------------------------------------------------------------- access


@pytest.mark.parametrize("who", [ADMIN_NOPERM, SUPPORT, USER], ids=["admin-without-promo", "support", "user"])
async def test_denied_without_the_promo_permission(env: UiEnv, who: int) -> None:
    pid = await add_promo(env.db, "SECRET", "wallet", amount_minor=100, currency="RUB")
    for data in (
        encode(SCREEN_LIST),
        encode(SCREEN_CARD, arg=str(pid)),
        encode(SCREEN_KINDS),
        encode(ACTIONS, "en", str(pid)),
        encode(ACTIONS, "del", f"{pid}:1"),
        encode(ACTIONS, "kind", "wallet"),
    ):
        await env.click(who, data)
        assert env.toasts[-1] == "Нет прав"
    assert not env.rendered()
    assert len(env.denied) == 6
    row = (await env.db.raw("select enabled from promocodes where id = $1", pid))[0]
    assert row["enabled"] is True


async def test_promos_command(env: UiEnv, service: PromoService, catalog: FakeCatalog) -> None:
    screens = PromoAdminScreens(env.router, service, catalog=catalog)
    assert await screens.handle_command(text_message(OWNER, "/promos"))
    assert "Промокоды" in env.text and "Промокодов пока нет" in env.text
    assert not await screens.handle_command(text_message(ADMIN_NOPERM, "/promos"))
    assert not await screens.handle_command(text_message(OWNER, "/promos", chat_type="group"))


# ------------------------------------------------------------------------------------------- create


async def test_create_a_wallet_code(env: UiEnv) -> None:
    await env.click(ADMIN, encode(SCREEN_LIST))
    await env.press(ADMIN, "Новый промокод")
    assert "Что он даёт" in env.text and len(env.labels()) == 8
    await env.press(ADMIN, "На баланс")
    assert "Код промокода" in env.text
    await env.type(ADMIN, "bad code")
    assert "от 3 до 48 символов" in env.text
    await env.type(ADMIN, "Gift100")
    assert "Сумма в RUB" in env.text
    await env.type(ADMIN, "сто")
    assert "Нужна сумма" in env.text
    await env.type(ADMIN, "100")
    assert "Причина" in env.text
    await env.type(ADMIN, " ок ")
    assert "Укажите причину" in env.text
    await env.type(ADMIN, "розыгрыш в канале")
    assert "Промокод создан" in env.text and "<b>Gift100</b>" in env.text
    assert "+100 ₽ на баланс" in env.text and "Использовано: 0 (без лимита)" in env.text
    assert "https://t.me/svbg_bot?start=pr_Gift100" in env.text
    assert "Удалить" in " ".join(env.labels()) and "Мин. сумма" not in " ".join(env.labels())
    assert await audit(env) == ["promo.create"]
    row = (await env.db.raw("select actor_id, amount_minor, reason from admin_audit"))[0]
    assert dict(row) == {"actor_id": env.uid(ADMIN), "amount_minor": 10_000, "reason": "розыгрыш в канале"}
    # the same code again → a readable error
    await env.click(ADMIN, encode(ACTIONS, "kind", "days"))
    await env.type(ADMIN, "GIFT100")
    await env.type(ADMIN, "7")
    await env.type(ADMIN, "для блогера")
    assert "Такой код уже есть" in env.text


async def test_create_with_a_generated_code_and_skip(env: UiEnv) -> None:
    await env.click(OWNER, encode(ACTIONS, "kind", "percent"))
    await env.press(OWNER, "Пропустить")
    await env.type(OWNER, "20")
    assert "Промокод создан" in env.text and "−20 % на покупку" in env.text
    labels = " ".join(env.labels())
    assert "Мин. сумма" in labels and "Тарифы" in labels and "Сколько ждёт" in labels
    code = (await env.db.raw("select code from promocodes"))[0]["code"]
    assert len(code) == 8


async def test_plan_gift_wizard(env: UiEnv) -> None:
    await env.click(OWNER, encode(SCREEN_KINDS))
    await env.press(OWNER, "Тариф в подарок")
    assert "Какой тариф подарить" in env.text
    assert [lb for lb in env.labels() if lb.startswith("Тариф")] == ["Тариф 1", "Тариф 2"]  # no trial plan
    await env.press(OWNER, "Тариф 2")
    await env.type(OWNER, "VIP30")
    await env.type(OWNER, "30")
    await env.type(OWNER, "подарки партнёрам")
    assert "тариф «Тариф 2» на 30 дней" in env.text
    pid = int((await env.db.raw("select id from promocodes"))[0]["id"])
    await env.press(OWNER, "🎀 Тариф")
    await env.press(OWNER, "Тариф 1")
    assert "тариф «Тариф 1» на 30 дней" in env.text and "Сохранено" in env.text
    await env.click(OWNER, encode(ACTIONS, "gpl", f"{pid}:9"))  # the trial plan cannot be gifted
    assert env.toasts[-1] == "Промокод не найден"
    await env.click(OWNER, encode(ACTIONS, "gpl", "x:1"))
    assert env.toasts[-1] == "Промокод не найден"


async def test_admin_limits_and_reasons_on_value_codes(env: UiEnv) -> None:
    await env.click(ADMIN, encode(ACTIONS, "kind", "wallet"))
    await env.type(ADMIN, "BIGMONEY")
    await env.type(ADMIN, "1000000")
    await env.type(ADMIN, "себе на счёт")
    assert "может провести только владелец" in env.text
    await env.click(ADMIN, encode(ACTIONS, "kind", "days"))
    await env.type(ADMIN, "YEAR")
    await env.type(ADMIN, "3650")
    await env.type(ADMIN, "долгий подарок")
    assert "Больше 31 дн." in env.text
    assert not await env.db.raw("select 1 from promocodes")
    # the owner is not limited
    await env.click(OWNER, encode(ACTIONS, "kind", "days"))
    await env.type(OWNER, "YEAR")
    await env.type(OWNER, "365")
    await env.type(OWNER, "годовой подарок")
    assert "Промокод создан" in env.text
    pid = int((await env.db.raw("select id from promocodes"))[0]["id"])
    # an admin edit of the days / the limit of a value code asks for a reason and keeps the limit
    await env.click(ADMIN, encode(ACTIONS, "edit", f"{pid}:max"))
    await env.type(ADMIN, "10")
    assert "Причина" in env.text
    await env.type(ADMIN, "для канала")
    assert "Больше 31 дн." in env.text  # the code gives 365 days: only the owner may change it
    await env.click(OWNER, encode(ACTIONS, "edit", f"{pid}:days"))
    await env.type(OWNER, "30")
    await env.type(OWNER, "короче")
    assert "+30 дней к подписке" in env.text
    await env.click(ADMIN, encode(ACTIONS, "edit", f"{pid}:max"))
    await env.type(ADMIN, "10")
    await env.type(ADMIN, "для канала")
    assert "Использовано: 0 из 10" in env.text
    # a discount needs no reason
    await env.click(ADMIN, encode(ACTIONS, "kind", "percent"))
    await env.type(ADMIN, "SALE10")
    await env.type(ADMIN, "10")
    assert "Промокод создан" in env.text


async def test_a_revoked_admin_is_refused_by_the_service(env: UiEnv) -> None:
    await env.db.raw("update users set role = 'user', perms = '[]'::jsonb where telegram_id = $1", ADMIN)
    await env.click(ADMIN, encode(ACTIONS, "kind", "percent"))  # the cached context still says admin
    await env.type(ADMIN, "LATE10")
    await env.type(ADMIN, "10")
    assert "Нет прав" in env.text
    assert not await env.db.raw("select 1 from promocodes")


# ------------------------------------------------------------------------------------------- card edits


async def test_card_edits(env: UiEnv) -> None:
    pid = await add_promo(env.db, "AUTUMN", "percent", percent=20, pending_hours=72)
    arg = str(pid)
    await env.click(OWNER, encode(SCREEN_CARD, arg=arg))
    assert "🟢 действует" in env.text and "Тарифы: все" in env.text and "ждёт оплаты 72 ч." in env.text
    await env.press(OWNER, "Выключить")
    assert "⏸ выключен" in env.text
    await env.press(OWNER, "Включить")
    await env.press(OWNER, "Один раз на человека: да")
    assert "Один раз на человека: нет" in env.text
    await env.press(OWNER, "Только новым: нет")
    assert "Только новым: да" in env.text
    await env.press(OWNER, "Значение")
    await env.type(OWNER, "101")
    assert "Максимум 100" in env.text
    await env.type(OWNER, "35")
    assert "−35 % на покупку" in env.text
    await env.press(OWNER, "Лимит")
    await env.type(OWNER, "100")
    assert "Использовано: 0 из 100" in env.text
    await env.press(OWNER, "Срок")
    await env.type(OWNER, "31.02.2030")
    assert "Такой даты нет" in env.text
    await env.type(OWNER, "01.01.2020")
    assert "Дата уже прошла" in env.text
    await env.type(OWNER, "31.12.2030")
    assert "Срок: до 31.12.2030" in env.text
    expires = (await env.db.raw("select expires_at from promocodes"))[0]["expires_at"]
    assert expires == datetime(2030, 12, 31, 20, 59, 59, tzinfo=UTC)  # 23:59:59 Moscow
    await env.press(OWNER, "Срок")
    await env.type(OWNER, "0")
    assert "Срок: бессрочно" in env.text
    await env.press(OWNER, "Мин. сумма")
    await env.type(OWNER, "500")
    assert "Мин. сумма: 500 ₽" in env.text
    await env.press(OWNER, "Сколько ждёт")
    await env.type(OWNER, "24")
    assert "ждёт оплаты 24 ч." in env.text
    await env.press(OWNER, "Заметка")
    await env.type(OWNER, "для канала <b>")
    assert "Заметка: для канала &lt;b&gt;" in env.text
    await env.press(OWNER, "Тарифы")
    await env.press(OWNER, "Тариф 2")
    assert "✅ Тариф 2" in env.labels()
    await env.press(OWNER, "К промокоду")
    assert "Тарифы: Тариф 2" in env.text
    await env.press(OWNER, "✏️ Код")
    await env.type(OWNER, "WINTER")
    assert "<b>WINTER</b>" in env.text
    assert (await audit(env)).count("promo.update") == 13


async def test_forged_arguments(env: UiEnv) -> None:
    pid = await add_promo(env.db, "X1X", "wallet", amount_minor=100, currency="RUB")
    await env.click(OWNER, encode(SCREEN_CARD, arg="abc"))
    assert "<b>Промокоды</b>" in env.text  # back to the list
    for data in (
        encode(ACTIONS, "edit", f"{pid}:nope"),
        encode(ACTIONS, "edit", "9999:max"),
        encode(ACTIONS, "pl", f"{pid}:1"),  # not a discount
        encode(ACTIONS, "del", "garbage"),
        encode(ACTIONS, "en", "999999"),
    ):
        await env.click(OWNER, data)
        assert env.toasts[-1] == "Промокод не найден", data
    await env.click(OWNER, encode(ACTIONS, "kind", "unknown"))
    assert "Что он даёт" in env.text


async def test_delete_only_unused(env: UiEnv, service: PromoService) -> None:
    pid = await add_promo(env.db, "TMP", "wallet", amount_minor=100, currency="RUB")
    await env.click(OWNER, encode(SCREEN_CARD, arg=str(pid)))
    await env.press(OWNER, "Удалить")
    assert "Удалить промокод <b>TMP</b>" in env.text
    await env.press(OWNER, "Отмена")
    assert "<b>TMP</b>" in env.text
    await env.press(OWNER, "Удалить")
    stale = env.button("Да, удалить")
    await env.db.raw("update promocodes set version = version + 1")
    await env.click(OWNER, stale)
    assert "уже изменили" in str(env.toasts[-1])
    await env.press(OWNER, "Удалить")
    await env.press(OWNER, "Да, удалить")
    assert env.toasts[-1] == "🗑 Удалено" and not await env.db.raw("select 1 from promocodes")
    used = await add_promo(env.db, "USED", "wallet", amount_minor=100, currency="RUB")
    await service.activate(env.uid(USER), "USED")
    await env.click(OWNER, encode(SCREEN_CARD, arg=str(used)))
    assert "Удалить" not in " ".join(env.labels()) and "Код" not in " ".join(env.labels())
    await env.click(OWNER, encode(ACTIONS, "del", f"{used}:1"))
    assert "только выключить" in str(env.toasts[-1])


async def test_list_pages_and_find(env: UiEnv) -> None:
    for i in range(10):
        await add_promo(env.db, f"CODE{i:02d}", "days", days=i + 1, max_uses=5)
    await env.click(OWNER, encode(SCREEN_LIST))
    codes = [lb for lb in env.labels() if "CODE" in lb]
    assert len(codes) == 8 and codes[0].startswith("🟢 CODE09 · +10 дней к подписке · 0/5")
    await env.press(OWNER, "▶️")
    assert [lb for lb in env.labels() if "CODE" in lb][-1].startswith("🟢 CODE00")
    assert "▶️" not in env.labels() and "◀️" in env.labels()
    await env.press(OWNER, "Найти")
    await env.type(OWNER, "code05")
    assert "<b>CODE05</b>" in env.text
    await env.click(OWNER, encode(ACTIONS, "find"))
    await env.type(OWNER, "nothing")
    assert "Промокод не найден" in env.text


# ------------------------------------------------------------------------------------------- user


async def test_user_enters_codes(env: UiEnv, service: PromoService) -> None:
    await add_promo(env.db, "WEEK", "days", days=7)
    await add_promo(env.db, "SALE", "percent", percent=10, expires_at=datetime.now(UTC) + timedelta(days=30))
    uid = env.uid(USER)
    await env.click(USER, encode("sys", "promo"))
    assert "Отправьте промокод" in env.text
    await env.type(USER, "week")
    assert "сначала оформите" in env.text and "Другой код" in " ".join(env.labels())
    await add_sub(env.db, uid)
    await env.press(USER, "Другой код")
    await env.type(USER, "WEEK")
    assert env.text == "🎁 Промокод WEEK применён: +7 дней к подписке."
    await env.click(USER, encode(USER_SCREEN))
    await env.type(USER, "sale")
    assert "Скидка применится при оплате" in env.text and "Купить" in env.labels()[0]
    await env.click(USER, encode(USER_SCREEN))
    assert env.text.startswith("🏷 Уже ждёт оплаты: промокод SALE — −10 % на покупку.")
    assert service.pending(uid) is not None
