from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from svbg.billing.checkout import BillingError
from svbg.billing.kinds import FULFILL_JOB
from svbg.core.clock import now
from svbg.payments.core import CheckoutError, SpendDeniedError
from tests.billing.kit import BillingEnv


async def _freeze(env: BillingEnv, user_id: int) -> None:
    """A live subscription on hold (IP Guard), as the subscription service leaves it."""
    tg = await env.telegram_id(user_id)
    sid = await env.s.new_sub(None)
    await env.db.raw(
        "update subscriptions set user_id = $1, panel_telegram_id = $2, hold_kind = 'ip_guard', "
        "hold_since = now() where id = $3",
        user_id,
        tg,
        sid,
    )


# ------------------------------------------------------------------------------------------- drafts


async def test_draft_is_two_sql_and_freezes_the_price(env: BillingEnv) -> None:
    uid = await env.user()
    await env.fund(uid, 5_000)
    before = env.db.queries
    draft = await env.draft(uid, days=90, extra_devices=1)
    assert env.db.queries - before == 2, env.db.counter.since(before) if env.db.counter else None
    order = await env.order(draft.order_id)
    assert (order["kind"], order["status"], order["total_minor"]) == ("new", "draft", 49_900 + 5_700)
    assert order["snapshot"]["plan"]["plan_id"] == 1 and order["snapshot"]["days"] == 90
    items = await env.rows(
        "select type, amount_minor, payload from order_items where order_id = $1 order by position",
        draft.order_id,
    )
    assert [(i["type"], i["amount_minor"]) for i in items] == [("plan_period", 49_900), ("devices", 5_700)]
    assert items[1]["payload"] == {"count": 1, "days": 90}
    assert draft.balance_minor == 5_000 and draft.missing_minor == 55_600 - 5_000


async def test_draft_kind_follows_the_live_subscription(env: BillingEnv) -> None:
    uid = await env.user()
    await env.fund(uid, 17_900)
    first = await env.draft(uid)
    assert first.quote.kind == "new"
    await env.checkout.pay(first.order_id, uid)
    await env.drain()
    assert (await env.draft(uid)).quote.kind == "renew"
    assert (await env.draft(uid, plan_id=2)).quote.kind == "change"


async def test_draft_refusals(env: BillingEnv) -> None:
    uid = await env.user()
    with pytest.raises(BillingError) as e:
        await env.draft(uid, plan_id=99)
    assert e.value.code == "plan_unavailable" and "недоступен" in e.value.text
    with pytest.raises(BillingError) as e:
        await env.draft(uid, days=45)
    assert e.value.code == "pricing"
    with pytest.raises(BillingError):
        await env.checkout.draft_plan(10**9, 1, 30)
    with pytest.raises(BillingError):
        await env.checkout.draft_devices(uid, 1)  # no paid subscription


async def test_device_addon_draft_and_fulfill(env: BillingEnv) -> None:
    uid = await env.user()
    await env.fund(uid, 50_000)
    d = await env.draft(uid)
    await env.checkout.pay(d.order_id, uid)
    await env.drain()
    addon = await env.checkout.draft_devices(uid, 2)
    assert addon.quote.kind == "addon_devices" and addon.quote.days == 30
    assert addon.quote.total_minor == 3_800
    ref = env.messenger.show(await env.telegram_id(uid))
    res = await env.checkout.pay(addon.order_id, uid, ui_ref=ref)
    assert res.outcome == "paid"
    await env.drain()
    assert "Добавлено устройств: 2" in env.messenger.text(ref)
    sub = (
        await env.rows(
            "select extra_devices, desired_device_limit from subscriptions where user_id = $1", uid
        )
    )[0]
    assert (sub["extra_devices"], sub["desired_device_limit"]) == (2, 7)
    assert (await env.order(addon.order_id))["status"] == "fulfilled"
    with pytest.raises(BillingError, match="15"):
        await env.checkout.draft_devices(uid, 9)


# ------------------------------------------------------------------------------------------- pay


async def test_pay_with_enough_balance(env: BillingEnv) -> None:
    uid = await env.user()
    await env.fund(uid, 20_000)
    draft = await env.draft(uid)
    ref = env.messenger.show(await env.telegram_id(uid))
    before = env.db.queries
    res = await env.checkout.pay(draft.order_id, uid, ui_ref=ref)
    assert env.db.queries - before <= 6
    assert (res.outcome, res.balance_minor, res.price_minor) == ("paid", 2_100, 17_900)
    order = await env.order(draft.order_id)
    assert order["status"] == "paid" and order["ui_ref"]["message_id"] == ref.message_id
    jobs = await env.jobs(FULFILL_JOB)
    assert [(j["dedup_key"], j["lane"]) for j in jobs] == [(f"fulfill:order:{draft.order_id}", "interactive")]
    again = await env.checkout.pay(draft.order_id, uid)
    assert again.outcome == "already"
    assert len(await env.jobs(FULFILL_JOB)) == 1
    ledger = await env.ledger(uid)
    assert [(r["reason"], r["amount_minor"]) for r in ledger] == [("bonus", 20_000), ("purchase", -17_900)]
    await env.assert_wallet_invariants()


async def test_pay_short_waits_for_funds_and_replaces_the_previous_waiting(env: BillingEnv) -> None:
    uid = await env.user()
    await env.fund(uid, 5_000)
    a = await env.draft(uid)
    b = await env.draft(uid, days=90)
    ra = await env.checkout.pay(a.order_id, uid)
    assert (ra.outcome, ra.missing_minor, ra.balance_minor) == ("awaiting_funds", 12_900, 5_000)
    oa = await env.order(a.order_id)
    window = oa["autocomplete_until"] - now()
    assert timedelta(minutes=59) < window <= timedelta(minutes=60)
    rb = await env.checkout.pay(b.order_id, uid)
    assert rb.outcome == "awaiting_funds" and rb.canceled_order_id == a.order_id
    assert (await env.order(a.order_id))["status"] == "canceled"
    waiting = await env.rows("select id from orders where user_id = $1 and status = 'awaiting_funds'", uid)
    assert [w["id"] for w in waiting] == [b.order_id]
    # re-pressing «Оплатить» on the waiting order keeps it the only one
    rb2 = await env.checkout.pay(b.order_id, uid)
    assert rb2.outcome == "awaiting_funds" and rb2.canceled_order_id is None
    assert await env.balance(uid) == 5_000
    assert await env.jobs(FULFILL_JOB) == []


async def test_parallel_pay_of_two_orders_one_balance(env: BillingEnv) -> None:
    uid = await env.user()
    await env.fund(uid, 17_900)
    drafts = [await env.draft(uid) for _ in range(5)]
    results = await asyncio.gather(*(env.checkout.pay(d.order_id, uid) for d in drafts))
    outcomes = sorted(r.outcome for r in results)
    assert outcomes.count("paid") == 1
    waiting = await env.rows("select id from orders where user_id = $1 and status = 'awaiting_funds'", uid)
    assert len(waiting) == 1
    assert await env.balance(uid) == 0
    await env.assert_wallet_invariants()


async def test_frozen_or_banned_user_cannot_pay(env: BillingEnv) -> None:
    uid = await env.user()
    await env.fund(uid, 20_000)
    draft = await env.draft(uid)
    await _freeze(env, uid)
    res = await env.checkout.pay(draft.order_id, uid)
    assert res.outcome == "denied" and "приостановлена" in (res.text or "")
    assert await env.balance(uid) == 20_000
    banned = await env.user()
    d2 = await env.draft(banned)
    await env.db.raw("update users set banned_at = now() where id = $1", banned)
    assert (await env.checkout.pay(d2.order_id, banned)).outcome == "denied"
    # top-ups are refused by the payment core's guard (registered by ``Billing.attach``) as well
    with pytest.raises(SpendDeniedError):
        await env.topup(uid, 17_900)
    assert await env.rows("select id from orders where user_id = $1 and kind = 'topup'", uid) == []


async def test_pay_refusals(env: BillingEnv) -> None:
    uid = await env.user()
    other = await env.user()
    draft = await env.draft(uid)
    with pytest.raises(BillingError) as e:
        await env.checkout.pay(draft.order_id, other)
    assert e.value.code == "not_found"
    assert await env.checkout.cancel(draft.order_id, uid)
    with pytest.raises(BillingError) as e:
        await env.checkout.pay(draft.order_id, uid)
    assert e.value.code == "not_payable"
    assert not await env.checkout.cancel(draft.order_id, uid)


async def test_free_order_is_paid_without_a_ledger_entry(env: BillingEnv) -> None:
    from svbg.domain.pricing import PercentOff

    uid = await env.user()
    draft = await env.draft(uid, discounts=[PercentOff(100, "подарок")])
    res = await env.checkout.pay(draft.order_id, uid)
    assert res.outcome == "paid" and res.price_minor == 0
    assert await env.ledger(uid) == []
    await env.drain()
    assert (await env.order(draft.order_id))["status"] == "fulfilled"


# ------------------------------------------------------------------------------------------- top-up


async def test_topup_with_parent_restarts_the_window_and_returns_the_invoice(env: BillingEnv) -> None:
    uid = await env.user()
    draft = await env.draft(uid)
    await env.checkout.pay(draft.order_id, uid)
    await env.db.raw(
        "update orders set autocomplete_until = now() + interval '1 minute' where id = $1", draft.order_id
    )
    ref = env.messenger.show(await env.telegram_id(uid))
    res = await env.topup(uid, 17_900, parent=draft.order_id, ui_ref=ref)
    assert res.checkout.kind == "url" and res.checkout.pay_url.startswith("https://stub.example/pay/")
    assert (res.credit_minor, res.pay_amount_minor, res.pay_currency) == (17_900, 17_900, "RUB")
    topup = await env.order(res.order_id)
    assert (topup["kind"], topup["status"], topup["parent_order_id"]) == (
        "topup",
        "awaiting_payment",
        draft.order_id,
    )
    pay = await env.pay.payment(res.payment_id)
    assert (pay["order_id"], pay["amount_minor"], pay["status"]) == (res.order_id, 17_900, "pending")
    parent = await env.order(draft.order_id)
    assert parent["autocomplete_until"] - now() > timedelta(minutes=59)
    assert parent["ui_ref"]["message_id"] == ref.message_id
    # the provider only ever sees the opaque payment id
    assert env.pay.server.created[-1]["order"] == res.payment_id
    assert str(await env.telegram_id(uid)) not in str(env.pay.server.created[-1])


async def test_topup_in_stars_is_converted_at_the_rate(env: BillingEnv) -> None:
    env.config["PAY_STARS_RATE"] = "1.5"
    uid = await env.user()
    res = await env.topup(uid, 17_900, instance_id=env.stars_id())
    assert (res.pay_currency, res.pay_amount_minor, res.credit_minor) == ("XTR", 120, 18_000)
    assert res.checkout.kind == "invoice"
    env.config["PAY_STARS_RATE"] = "oops"
    with pytest.raises(BillingError) as e:
        await env.topup(uid, 17_900, instance_id=env.stars_id())
    assert e.value.code == "no_stars_rate"


async def test_topup_limits_and_failed_invoice(env: BillingEnv) -> None:
    uid = await env.user()
    for amount in (999, 10_000_001):
        with pytest.raises(BillingError) as e:
            await env.topup(uid, amount)
        assert e.value.code == "topup_amount" and "Сумма пополнения" in e.value.text
    with pytest.raises(CheckoutError):  # StubPay's minimum is 100 ₽
        await env.topup(uid, 5_000)
    env.pay.server.fail_create = 503
    with pytest.raises(CheckoutError):
        await env.topup(uid, 20_000)
    rows = await env.rows("select status, note from orders where user_id = $1 and kind = 'topup'", uid)
    assert rows == [{"status": "canceled", "note": "invoice_failed"}]
    with pytest.raises(BillingError):
        await env.topup(uid, 20_000, instance_id=999_999)


async def test_topup_options_round_up_to_the_method_minimum(env: BillingEnv) -> None:
    options = env.billing.topup_options(5_000, env.pay.registry.all())
    by_id = {o.instance_id: o for o in options}
    stub = by_id[env.stub_id()]
    assert (stub.credit_minor, stub.surplus_minor, stub.pay_currency) == (10_000, 5_000, "RUB")
    stars = by_id[env.stars_id()]
    assert (stars.pay_amount_minor, stars.pay_currency, stars.credit_minor) == (50, "XTR", 5_000)
    assert env.billing.topup_options(0, [env.pay.inst("stubpay")]) == []


# ------------------------------------------------------------------------------------------- sweeper


async def test_sweep_expires_windows_drafts_and_old_topups(env: BillingEnv) -> None:
    uid = await env.user()
    waiting = await env.draft(uid)
    await env.checkout.pay(waiting.order_id, uid)
    stale = await env.draft(uid)
    fresh = await env.draft(uid)
    old_topup = await env.topup(uid, 20_000)
    await env.db.raw(
        "update orders set autocomplete_until = now() - interval '1 second' where id = $1", waiting.order_id
    )
    await env.db.raw("update orders set created_at = now() - interval '2 days' where id = $1", stale.order_id)
    await env.db.raw(
        "update orders set created_at = now() - interval '8 days' where id = $1", old_topup.order_id
    )
    await env.billing.sweep()
    statuses = {
        r["id"]: r["status"] for r in await env.rows("select id, status from orders where user_id = $1", uid)
    }
    assert statuses[waiting.order_id] == "expired"
    assert stale.order_id not in statuses  # a stale draft never held money: deleted with its items
    assert await env.rows("select id from order_items where order_id = $1", stale.order_id) == []
    assert statuses[fresh.order_id] == "draft"
    assert statuses[old_topup.order_id] == "expired"
    # a late payment of the expired top-up still lands on the balance
    assert await env.paid_webhook(old_topup.payment_id) == 200
    assert (await env.order(old_topup.order_id))["status"] == "credited"
    assert await env.balance(uid) == 20_000


async def test_awaiting_funds_needs_a_window_and_is_unique_in_the_database(env: BillingEnv) -> None:
    uid = await env.user()
    a, b = await env.draft(uid), await env.draft(uid)
    await env.checkout.pay(a.order_id, uid)
    with pytest.raises(Exception, match="uq_orders_user_awaiting_funds"):
        await env.db.raw(
            "update orders set status = 'awaiting_funds', autocomplete_until = now() where id = $1",
            b.order_id,
        )
    with pytest.raises(Exception, match="waiting_has_window"):
        await env.db.raw("update orders set autocomplete_until = null where id = $1", a.order_id)
