"""User card: access by role, what Support may see, the SQL budget of a click, histories."""

from __future__ import annotations

import pytest

from svbg.tg.admin.users.screens import (
    ACTIONS,
    SCREEN_CARD,
    SCREEN_EVENTS,
    SCREEN_LEDGER,
    SCREEN_ORDERS,
    SCREEN_PAYMENTS,
)
from svbg.tg.ui.codec import encode
from tests.tg.admin.users.kit import (
    ADMIN,
    ADMIN_STATS,
    OTHER,
    OWNER,
    SUPPORT,
    USER,
    UEnv,
    add_instance,
    add_payment,
    add_sub,
)


async def test_plain_user_cannot_open_a_card(env: UEnv) -> None:
    uid = env.ids[USER]
    await env.add(OTHER)
    await env.click(OTHER, encode(SCREEN_CARD, arg=str(uid)))
    assert env.toasts[-1] == "Нет прав"
    assert not env.rendered()
    await env.click(OTHER, encode(ACTIONS, "rd", str(uid)))
    assert env.toasts[-1] == "Нет прав"
    assert await env.jobs() == []


async def test_card_for_admin_shows_money_and_actions(env: UEnv) -> None:
    uid = env.ids[USER]
    await env.db.raw("update users set wallet_minor = 15000 where id = $1", uid)
    await env.db.raw(
        "insert into wallet_ledger "
        "(user_id, amount_minor, currency, balance_after, reason, ref_type, ref_id) "
        "values ($1, 15000, 'RUB', 15000, 'bonus', 'test', '1')",
        uid,
    )
    inst = await add_instance(env.db)
    await add_payment(env.db, inst, uid, 17900)
    await add_sub(env.db, uid, days=10, username="sv_5005", short_uuid="AbCdEf12")
    await env.click(ADMIN, encode(SCREEN_CARD, arg=str(uid)))
    text = env.text
    assert "Иван" in text and "@ivan_petrov" in text and "<code>5005</code>" in text
    assert "Баланс: <b>150 ₽</b>" in text
    assert "Оплат: 1 на 179 ₽" in text
    assert "🟢 Подписка до" in text and "Стандарт" in text and "sv_5005" in text
    labels = env.labels()
    for label in (
        "➕ Дни",
        "🎁 Выдать тариф",
        "💰 Баланс",
        "📱 Сбросить устройства",
        "🔗 Новая ссылка",
        "✉️ Написать",
    ):
        assert label in labels
    assert "🚫 Заблокировать" in labels
    assert "🎖 Роль" not in labels  # owner only


async def test_card_for_support_hides_money_and_admin_actions(env: UEnv) -> None:
    uid = env.ids[USER]
    inst = await add_instance(env.db)
    await add_payment(env.db, inst, uid, 17900)
    await add_sub(env.db, uid)
    await env.click(SUPPORT, encode(SCREEN_CARD, arg=str(uid)))
    text = env.text
    assert "Баланс" not in text and "179" not in text and "Оплат: 1" in text
    labels = env.labels()
    assert "📱 Сбросить устройства" in labels and "✉️ Написать" in labels
    for hidden in ("➕ Дни", "💰 Баланс", "🚫 Заблокировать", "👛 Движения", "🎁 Выдать тариф"):
        assert hidden not in labels
    # forged buttons of admin actions are refused by the router and reported
    for data in (
        encode(ACTIONS, "days", str(uid)),
        encode(ACTIONS, "wal", str(uid)),
        encode(SCREEN_LEDGER, arg=str(uid)),
    ):
        await env.click(SUPPORT, data)
        assert env.toasts[-1] == "Нет прав"
    assert len(env.denied) == 3


async def test_owner_sees_the_role_button(env: UEnv) -> None:
    await env.click(OWNER, encode(SCREEN_CARD, arg=str(env.ids[USER])))
    assert "🎖 Роль" in env.labels()


async def test_admin_with_stats_only_sees_card_without_operations(env: UEnv) -> None:
    uid = env.ids[USER]
    await add_sub(env.db, uid)
    await env.click(ADMIN_STATS, encode(SCREEN_CARD, arg=str(uid)))
    labels = env.labels()
    assert "Баланс" in env.text  # admins see sums
    assert "➕ Дни" not in labels and "💰 Баланс" not in labels and "🚫 Заблокировать" not in labels


@pytest.mark.parametrize("arg", ["abc", "", "1" * 30, "-1"])
async def test_bad_card_arguments_go_back_to_admin_home(env: UEnv, arg: str) -> None:
    await env.click(ADMIN, encode(SCREEN_CARD, arg=arg or None))
    assert "Админка" in env.text


async def test_unknown_user(env: UEnv) -> None:
    await env.click(ADMIN, encode(SCREEN_CARD, arg="999999"))
    assert "Пользователь не найден" in env.text


async def test_card_click_costs_at_most_two_sql(env: UEnv) -> None:
    uid = env.ids[USER]
    await add_sub(env.db, uid)
    await env.click(ADMIN, encode(SCREEN_CARD, arg=str(uid)))  # warm caches (ui_state, main message)
    before = env.db.queries
    await env.click(ADMIN, encode(SCREEN_CARD, arg=str(uid)))
    assert env.db.queries - before <= 2


async def test_card_states(env: UEnv) -> None:
    uid = env.ids[USER]
    await env.click(ADMIN, encode(SCREEN_CARD, arg=str(uid)))
    assert "Подписки нет" in env.text and "➕ Дни" not in env.labels()
    sid = await add_sub(env.db, uid, days=-2)
    await env.click(ADMIN, encode(SCREEN_CARD, arg=str(uid)))
    assert "⛔️ Подписка истекла" in env.text
    await env.db.raw("update subscriptions set hold_kind = 'admin', hold_since = now() where id = $1", sid)
    await env.click(ADMIN, encode(SCREEN_CARD, arg=str(uid)))
    assert "приостановлена" in env.text
    await env.db.raw(
        "update subscriptions set hold_kind = null, hold_since = null, is_trial = true, "
        "paid_until = now() + interval '3 days' where id = $1",
        sid,
    )
    await env.click(ADMIN, encode(SCREEN_CARD, arg=str(uid)))
    assert "🎁 Пробная до" in env.text
    await env.db.raw("update users set banned_at = now(), bot_blocked_at = now() where id = $1", uid)
    await env.click(ADMIN, encode(SCREEN_CARD, arg=str(uid)))
    assert "⛔️ Заблокирован" in env.text and "Заблокировал(а) бота" in env.text
    assert "✅ Разблокировать" in env.labels()
    assert "Капчу ещё не прошёл(а)" in env.text  # a new user stuck at the entry captcha
    await env.db.raw("update users set captcha_passed_at = now() where id = $1", uid)
    await env.click(ADMIN, encode(SCREEN_CARD, arg=str(uid)))
    assert "Капчу" not in env.text


async def test_payments_history_paginates_and_masks_sums_for_support(env: UEnv) -> None:
    uid = env.ids[USER]
    inst = await add_instance(env.db)
    for i in range(12):
        await add_payment(env.db, inst, uid, 10000 + i * 100, status="paid" if i % 2 else "expired")
    await env.click(ADMIN, encode(SCREEN_PAYMENTS, arg=str(uid)))
    assert "Оплаты" in env.text and "RollyPay" in env.text and "₽" in env.text
    assert env.text.count("<code>") == 10
    await env.press(ADMIN, "Дальше")
    assert env.text.count("<code>") == 2
    assert "Дальше" not in " ".join(env.labels())
    await env.click(SUPPORT, encode(SCREEN_PAYMENTS, arg=str(uid)))
    assert "₽" not in env.text and "•••" in env.text


async def test_forged_history_cursor(env: UEnv) -> None:
    await env.click(ADMIN, encode(SCREEN_PAYMENTS, arg=f"{env.ids[USER]}:'; drop table users;--"))
    assert "Админка" in env.text
    await env.click(ADMIN, encode(SCREEN_EVENTS, arg=f"{env.ids[USER]}:99999"))
    assert "Админка" in env.text


async def test_orders_ledger_and_events(env: UEnv) -> None:
    uid = env.ids[USER]
    await env.db.raw(
        "insert into orders (user_id, kind, status, currency, total_minor, snapshot) "
        "values ($1, 'new', 'fulfilled', 'RUB', 17900, $2::jsonb)",
        uid,
        '{"title": "Стандарт", "days": 30}',
    )
    await env.click(ADMIN, encode(SCREEN_ORDERS, arg=str(uid)))
    assert "покупка «Стандарт» 30 дн." in env.text and "179 ₽" in env.text and "выполнен" in env.text
    await env.click(SUPPORT, encode(SCREEN_ORDERS, arg=str(uid)))
    assert "179" not in env.text
    await env.click(ADMIN, encode(SCREEN_LEDGER, arg=str(uid)))
    assert "Пока ничего нет" in env.text
    await env.click(ADMIN, encode(SCREEN_EVENTS, arg=str(uid)))
    assert "Пока ничего нет" in env.text
