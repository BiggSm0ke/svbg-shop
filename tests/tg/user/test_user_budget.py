"""Hot path budget (07 §2.6): every user screen click costs ≤ 2 SQL statements and 0 HTTP requests to the
panel; forged callbacks and refusals never move money or break the menu."""

from __future__ import annotations

from typing import Any

import pytest

from tests.tg.user.kit import UserEnv, build_user_env

BUDGET = 2


async def _measure(env: UserEnv, tg: int, data: str) -> tuple[int, int, Any]:
    q0 = env.db.queries
    p0 = len(env.b.s.panel.calls())
    shown = await env.click(tg, data)
    return env.db.queries - q0, len(env.b.s.panel.calls()) - p0, shown


async def test_screen_clicks_stay_within_two_sql_and_no_panel_http(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn, plans=(1, 2)) as env:
        _uid, tg = await env.new_user(balance=20_000)
        await env.open(tg)
        await env.click(tg, "v1:home:o")  # warm the user directory and ui_state caches
        clicks = {
            "home": "v1:home:o",
            "buy": "v1:buy:o",
            "plan": "v1:buy_plan:o:1",
            "checkout": "v1:buy:pick:1:30:0",
            "balance": "v1:bal:o",
            "topup": "v1:topup:o:49900",
            "connect": "v1:connect:o",
            "lang": "v1:lang:o",
        }
        report: dict[str, tuple[int, int]] = {}
        for name, data in clicks.items():
            sql, http, _ = await _measure(env, tg, data)
            report[name] = (sql, http)
        assert all(sql <= BUDGET and http == 0 for sql, http in report.values()), report
        # the checkout screen opened back from the shortfall costs one read
        order_id = (await env.rows("select id from orders order by id desc limit 1"))[0]["id"]
        sql, http, shown = await _measure(env, tg, f"v1:co:o:{order_id}")
        assert sql <= BUDGET and http == 0 and "Оплатить" in shown.labels()[0]


async def test_devices_screen_reads_the_cache_not_the_panel(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn) as env:
        uid, tg = await env.new_user(balance=20_000)
        await env.open(tg)
        await env.press(tg, "Подписка")
        await env.press(tg, "Купить подписку")
        await env.press(tg, "1 мес.")
        await env.press(tg, "Оплатить")
        await env.drain()
        await env.click(tg, "v1:dev:o")  # stale cache → a refresh job (one more SQL), no panel call
        await env.drain()
        sql, http, shown = await _measure(env, tg, "v1:dev:o")
        assert sql <= BUDGET and http == 0
        assert "Устройства: 0 из 5" in shown.text and env.panel_fetch.calls == 1
        assert uid


@pytest.mark.parametrize(
    "data",
    [
        "v1:buy:pick:abc",
        "v1:buy:pick:1:30",
        "v1:buy:pick:999:30:0",
        "v1:buy:pick:1:7:0",
        "v1:buy_plan:o:999",
        "v1:pay:go:999999",
        "v1:pay:inv:x:1:100",
        "v1:pay:inv:-:999:17900",
        "v1:pay:inv:-:1:1",
        "v1:pay:paid:not-a-uuid",
        "v1:pay:amt:zz",
        "v1:pay_short:o:999999",
        "v1:co:o:999999",
        "v1:topup:o:1",
        "v1:topup:o:-5",
        "v1:dev:del:x",
        "v1:lang:set:de",
        "v1:chan:check",
        "v1:buy:reorder:999999",
    ],
    ids=lambda d: d.replace(":", "_"),
)
async def test_forged_or_stale_callbacks_never_move_money(pg_dsn: str, data: str) -> None:
    async with build_user_env(pg_dsn) as env:
        uid, tg = await env.new_user(balance=500)
        await env.open(tg)
        shown = await env.click(tg, data)
        assert shown.text.strip()  # the user always sees a screen
        assert env.hub.captured == []  # refusals are not errors
        assert await env.b.balance(uid) == 500
        assert await env.rows("select id from payments where user_id = $1", uid) == []
        assert await env.rows("select id from orders where user_id = $1 and status <> 'draft'", uid) == []
        await env.b.assert_wallet_invariants()


async def test_someone_elses_order_cannot_be_paid(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn) as env:
        _victim, tg_v = await env.new_user(balance=20_000)
        thief, tg_t = await env.new_user(balance=20_000)
        await env.open(tg_v)
        await env.press(tg_v, "Подписка")
        await env.press(tg_v, "Купить подписку")
        await env.press(tg_v, "1 мес.")
        order_id = (await env.rows("select id from orders order by id desc limit 1"))[0]["id"]
        await env.open(tg_t)
        await env.click(tg_t, f"v1:pay:go:{order_id}")
        assert "Заказ не найден" in (env.tg.toasts()[-1] or "")
        assert await env.b.balance(thief) == 20_000
        assert (await env.rows("select status from orders where id = $1", order_id))[0]["status"] == "draft"


async def test_double_tap_on_pay_debits_once(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn) as env:
        uid, tg = await env.new_user(balance=20_000)
        await env.open(tg)
        await env.press(tg, "Подписка")
        await env.press(tg, "Купить подписку")
        checkout = await env.press(tg, "1 мес.")
        data = checkout.data("Оплатить")
        await env.click(tg, data, message_id=checkout.message_id)
        again = await env.click(tg, data, message_id=checkout.message_id)
        assert "Оформляю" in again.text  # still being fulfilled: billing draws the result here
        await env.drain()
        assert env.tg.shown(tg, checkout.message_id).text.startswith("✅ Оплачено!")
        assert await env.b.balance(uid) == 2_100
        assert [e["reason"] for e in await env.b.ledger(uid)] == ["bonus", "purchase"]
        await env.b.assert_wallet_invariants()


async def test_frozen_user_gets_no_invoice_and_no_debit(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn) as env:
        uid, tg = await env.new_user(balance=500)
        await env.open(tg)
        await env.press(tg, "Попробовать")
        await env.drain()
        await env.db.raw(
            "update subscriptions set hold_kind = 'admin', hold_since = now() where user_id = $1", uid
        )
        home = await env.click(tg, "v1:home:o")
        assert "приостановлена" in home.text
        await env.click(tg, "v1:buy_plan:o:1")
        checkout = await env.press(tg, "1 мес.")
        denied = await env.press(tg, "СБП", message_id=checkout.message_id)
        assert "приостановлена" in denied.text and "Поддержка" in " ".join(denied.labels())
        await env.click(tg, "v1:topup:o:17900")
        refused = await env.press(tg, "СБП")
        assert "⚠️" in refused.text
        assert await env.rows("select id from payments where user_id = $1", uid) == []
        assert await env.b.balance(uid) == 500


async def test_panel_down_shows_connecting_then_the_same_message_gets_connect(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn) as env:
        uid, tg = await env.new_user(balance=20_000)
        await env.open(tg)
        await env.press(tg, "Подписка")
        await env.press(tg, "Купить подписку")
        checkout = await env.press(tg, "1 мес.")
        env.b.s.panel.inject("503", times=None)
        await env.press(tg, "Оплатить")
        await env.drain()
        await env.drain(make_due=True, kinds=["billing.ui_progress"])
        assert "подключаем" in env.tg.shown(tg, checkout.message_id).text
        assert await env.b.balance(uid) == 2_100  # the money part is done whatever the panel does
        env.b.s.panel.clear_faults()
        await env.drain(make_due=True)
        done = env.tg.shown(tg, checkout.message_id)
        assert done.text.startswith("✅ Оплачено!") and done.button("Подключиться").web_app is not None
