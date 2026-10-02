"""Money arrives: top-up credited, the waiting purchase completed automatically in the same message (07
§4.5)."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from svbg.billing.kinds import ATTENTION_JOB, FULFILL_JOB, NOTICE_JOB
from svbg.billing.ports import Notice, UiRef
from svbg.billing.texts import SCREEN_CREDITED, SCREEN_PAID
from svbg.core.clock import now
from tests.billing.kit import BillingEnv


async def _waiting(env: BillingEnv, *, funded: int = 5_000, days: int = 30) -> tuple[int, int, UiRef]:
    """A user with ``funded`` on the balance whose purchase waits for a top-up; returns (user, order,
    message)."""
    uid = await env.user()
    if funded:
        await env.fund(uid, funded)
    draft = await env.draft(uid, days=days)
    ref = env.messenger.show(await env.telegram_id(uid))
    res = await env.checkout.pay(draft.order_id, uid, ui_ref=ref)
    assert res.outcome == "awaiting_funds"
    return uid, draft.order_id, ref


async def test_shortfall_topup_completes_the_purchase_in_the_same_message(env: BillingEnv) -> None:
    uid, order_id, ref = await _waiting(env)
    top = await env.topup(uid, 17_900, parent=order_id, ui_ref=ref)
    assert await env.paid_webhook(top.payment_id) == 200
    parent, topup = await env.order(order_id), await env.order(top.order_id)
    assert (parent["status"], topup["status"]) == ("paid", "credited")
    ledger = [(r["reason"], r["amount_minor"], r["balance_after"]) for r in await env.ledger(uid)]
    assert ledger == [("bonus", 5_000, 5_000), ("topup", 17_900, 22_900), ("purchase", -17_900, 5_000)]
    assert [j["dedup_key"] for j in await env.jobs(FULFILL_JOB)] == [f"fulfill:order:{order_id}"]
    await env.drain()
    parent = await env.order(order_id)
    assert (parent["status"], parent["ui_stage"]) == ("fulfilled", "done")
    sub = (await env.rows("select * from subscriptions where user_id = $1", uid))[0]
    assert sub["link_state"] == "linked" and sub["subscription_url"]
    shown = env.messenger.notice(ref)
    assert isinstance(shown, Notice) and shown.screen == SCREEN_PAID, shown
    assert "Оплачено" in shown.text and "действует до" in shown.text and "осталось 50" in shown.text
    connect = shown.buttons[0][0]
    assert connect.text == "🔗 Подключиться" and connect.web_app == sub["subscription_url"]
    assert env.messenger.sends == []  # the same message, no new one
    assert await env.jobs(NOTICE_JOB) == []  # no «Зачислено» when the purchase completed
    await env.assert_wallet_invariants()


async def test_late_payment_stays_on_the_balance_with_a_notice(env: BillingEnv) -> None:
    uid, order_id, ref = await _waiting(env, funded=0)
    top = await env.topup(uid, 17_900, parent=order_id, ui_ref=ref)
    await env.db.raw(
        "update orders set autocomplete_until = now() - interval '1 minute' where id = $1", order_id
    )
    assert await env.paid_webhook(top.payment_id) == 200
    assert (await env.order(order_id))["status"] == "expired"
    assert (await env.order(top.order_id))["status"] == "credited"
    assert await env.balance(uid) == 17_900
    assert await env.jobs(FULFILL_JOB) == []
    await env.drain()
    shown = env.messenger.notice(ref)
    assert isinstance(shown, Notice) and shown.screen == SCREEN_CREDITED
    assert "Зачислено 179" in shown.text and "сейчас 179" in shown.text and "уже не пройдёт" in shown.text
    buy = shown.buttons[0][0]
    assert buy.action == "reorder" and buy.params == {"order_id": order_id} and "179" in buy.text
    # «Купить … за Z» gives a fresh draft at today's price, payable at once from the balance
    again = await env.checkout.reorder(order_id, uid)
    assert again.order_id != order_id and again.quote.total_minor == 17_900
    assert (await env.checkout.pay(again.order_id, uid)).outcome == "paid"
    await env.assert_wallet_invariants()


async def test_sweeper_expired_parent_then_payment_is_a_late_payment(env: BillingEnv) -> None:
    uid, order_id, ref = await _waiting(env, funded=0)
    top = await env.topup(uid, 17_900, parent=order_id, ui_ref=ref)
    await env.db.raw(
        "update orders set autocomplete_until = now() - interval '1 minute' where id = $1", order_id
    )
    await env.billing.sweep()
    assert await env.paid_webhook(top.payment_id) == 200
    assert (await env.order(order_id))["status"] == "expired"
    assert await env.balance(uid) == 17_900


async def test_concurrent_webhooks_one_credit_one_fulfill(env: BillingEnv) -> None:
    uid, order_id, ref = await _waiting(env)
    top = await env.topup(uid, 17_900, parent=order_id, ui_ref=ref)
    at = now()
    statuses = await asyncio.gather(
        *(env.paid_webhook(top.payment_id, at=at - timedelta(seconds=i)) for i in range(6)),
        env.paid_webhook(top.payment_id, at=at),  # the very same body: deduplicated by sha256
    )
    assert set(statuses) <= {200, 503}, statuses
    if 503 in statuses:  # a lost serialization race answers 503 so the provider retries; retry like it would
        assert await env.paid_webhook(top.payment_id, at=at + timedelta(seconds=1)) == 200
    ledger = await env.ledger(uid)
    assert [r["reason"] for r in ledger] == ["bonus", "topup", "purchase"]
    assert len(await env.jobs(FULFILL_JOB)) == 1
    # two fulfill runs racing (a duplicate job after a crash) still give one purchase
    from svbg.billing.fulfill import Fulfiller

    f: Fulfiller = env.billing.fulfiller
    results = await asyncio.gather(f.fulfill(order_id), f.fulfill(order_id))
    assert sorted(results) == ["fulfilled", "fulfilled"]
    await env.drain()
    events = await env.rows(
        "select kind from subscription_events where ref_type = 'order' and ref_id = $1", str(order_id)
    )
    assert [e["kind"] for e in events] == ["purchase_new"]
    await env.assert_wallet_invariants()


async def test_frozen_user_purchase_is_held_and_money_stays(env: BillingEnv) -> None:
    uid, order_id, ref = await _waiting(env)
    top = await env.topup(uid, 17_900, parent=order_id, ui_ref=ref)
    sid = await env.s.new_sub(None)
    await env.db.raw(
        "update subscriptions set user_id = $1, hold_kind = 'ip_guard', hold_since = now() where id = $2",
        uid,
        sid,
    )
    assert await env.paid_webhook(top.payment_id) == 200
    parent = await env.order(order_id)
    assert (parent["status"], parent["note"]) == ("held", "frozen")
    assert await env.balance(uid) == 22_900
    assert [j["kind"] for j in await env.jobs(ATTENTION_JOB)] == [ATTENTION_JOB]
    await env.drain()
    item = await env.rows(
        "select title, body from attention_items where dedup_key = $1", f"billing:held:{order_id}"
    )
    assert item and "заморожена" in item[0]["title"]
    shown = env.messenger.notice(ref)
    assert isinstance(shown, Notice) and "приостановлена" in shown.text
    # the owner decides: buy it anyway into the frozen balance
    owner_tg = await env.telegram_id(await env.user(role="owner"))
    res = await env.billing.fulfiller.resolve_held(order_id, "credit_hold", actor_telegram_id=owner_tg)
    assert res.action == "credit_hold"
    await env.drain()
    assert (await env.order(order_id))["status"] == "fulfilled"
    assert await env.balance(uid) == 5_000
    await env.assert_wallet_invariants()


async def test_insufficient_topup_keeps_waiting_and_asks_for_the_rest(env: BillingEnv) -> None:
    uid, order_id, ref = await _waiting(env, funded=0)
    top = await env.topup(uid, 10_000, parent=order_id, ui_ref=ref)
    assert await env.paid_webhook(top.payment_id) == 200
    assert (await env.order(order_id))["status"] == "awaiting_funds"
    await env.drain()
    shown = env.messenger.notice(ref)
    assert isinstance(shown, Notice) and "Пополните ещё на 79" in shown.text
    assert shown.buttons[0][0].action == "topup"
    second = await env.topup(uid, 10_000, parent=order_id, ui_ref=ref)
    assert await env.paid_webhook(second.payment_id) == 200
    assert (await env.order(order_id))["status"] == "paid"
    assert await env.balance(uid) == 20_000 - 17_900
    await env.assert_wallet_invariants()


async def test_replaced_purchase_is_not_completed_by_its_topup(env: BillingEnv) -> None:
    uid, first, ref = await _waiting(env, funded=0)
    top = await env.topup(uid, 17_900, parent=first, ui_ref=ref)
    second = await env.draft(uid, days=90)
    assert (await env.checkout.pay(second.order_id, uid)).outcome == "awaiting_funds"
    assert await env.paid_webhook(top.payment_id) == 200
    assert (await env.order(first))["status"] == "canceled"
    assert (await env.order(second.order_id))["status"] == "awaiting_funds"
    assert await env.balance(uid) == 17_900
    await env.drain()
    shown = env.messenger.notice(ref)
    assert isinstance(shown, Notice) and shown.buttons[0][0].params == {"order_id": first}


async def test_plain_topup_and_message_too_old_sends_a_new_one(env: BillingEnv) -> None:
    uid = await env.user()
    tg = await env.telegram_id(uid)
    old = env.messenger.show(tg, "счёт", at=now() - timedelta(hours=49))
    top = await env.topup(uid, 50_000, ui_ref=old)
    assert await env.paid_webhook(top.payment_id) == 200
    await env.drain()
    assert env.messenger.text(old) == "счёт"  # not edited: older than 48 h
    [(chat, notice)] = env.messenger.sends
    assert chat == tg and "Зачислено 500" in notice.text and len(notice.buttons) == 1  # «Меню» only


async def test_amount_mismatch_credits_nothing(env: BillingEnv) -> None:
    uid, order_id, ref = await _waiting(env)
    top = await env.topup(uid, 17_900, parent=order_id, ui_ref=ref)
    assert await env.paid_webhook(top.payment_id, amount="100.00") == 200
    assert (await env.pay.payment(top.payment_id))["status"] == "mismatch"
    assert (await env.order(top.order_id))["status"] == "awaiting_payment"
    assert (await env.order(order_id))["status"] == "awaiting_funds"
    assert await env.balance(uid) == 5_000
    # «179» equals «179.00»
    other = await env.topup(uid, 17_900)
    assert await env.paid_webhook(other.payment_id, amount="179") == 200
    assert await env.balance(uid) == 5_000 + 17_900


async def test_stars_invoice_paid_through_successful_payment(env: BillingEnv) -> None:
    uid, order_id, ref = await _waiting(env)
    top = await env.topup(uid, 12_900, parent=order_id, ui_ref=ref, instance_id=env.stars_id())
    assert (top.pay_currency, top.pay_amount_minor) == ("XTR", 129)
    result = await env.pay.core.credit_external(
        env.stars_id(),
        user_id=uid,
        external_id="charge-1",
        amount_minor=129,
        currency="XTR",
        payment_id=top.payment_id,
    )
    assert result.credited
    assert (await env.order(order_id))["status"] == "paid"
    assert await env.balance(uid) == 0
    await env.assert_wallet_invariants()


async def test_legacy_stars_payment_without_order_goes_to_the_payer(env: BillingEnv) -> None:
    uid = await env.user()
    result = await env.pay.core.credit_external(
        env.stars_id(),
        user_id=uid,
        external_id="legacy-charge",
        amount_minor=250,
        currency="XTR",
        metadata={"legacy_payload": "balance_7_25000"},
    )
    assert result.credited
    [entry] = await env.ledger(uid)
    assert (entry["reason"], entry["amount_minor"], entry["ref_id"]) == (
        "stars_legacy",
        25_000,
        result.payment.id,
    )
    again = await env.pay.core.credit_external(
        env.stars_id(), user_id=uid, external_id="legacy-charge", amount_minor=250, currency="XTR"
    )
    assert not again.credited and len(await env.ledger(uid)) == 1
    await env.drain()
    assert "Зачислено 250" in env.messenger.sends[0][1].text


async def test_payment_in_a_foreign_currency_without_order_raises_attention(env: BillingEnv) -> None:
    env.config["PAY_STARS_RATE"] = "bad"
    uid = await env.user()
    result = await env.pay.core.credit_external(
        env.stars_id(), user_id=uid, external_id="x-1", amount_minor=10, currency="XTR"
    )
    assert result.credited  # the payment is recorded …
    assert await env.ledger(uid) == []  # … but nothing is guessed onto the balance
    await env.drain()
    assert await env.rows("select id from attention_items where dedup_key like 'billing:unpriced:%'")


async def test_chargeback_takes_back_what_is_there(env: BillingEnv) -> None:
    uid, order_id, ref = await _waiting(env)
    top = await env.topup(uid, 17_900, parent=order_id, ui_ref=ref)
    assert await env.paid_webhook(top.payment_id) == 200
    assert await env.balance(uid) == 5_000
    assert await env.paid_webhook(top.payment_id, status="chargeback", at=now() + timedelta(seconds=1)) == 200
    assert (await env.pay.payment(top.payment_id))["status"] == "refunded"
    ledger = [(r["reason"], r["amount_minor"]) for r in await env.ledger(uid)]
    assert ledger[-1] == ("chargeback", -5_000)
    assert await env.balance(uid) == 0
    await env.drain()
    item = await env.rows(
        "select body from attention_items where dedup_key = $1", f"billing:chargeback:{top.payment_id}"
    )
    assert item and "не хватило 129" in item[0]["body"].replace("\xa0", " ")
    await env.assert_wallet_invariants()


async def test_full_chargeback_of_an_unspent_topup(env: BillingEnv) -> None:
    uid = await env.user()
    top = await env.topup(uid, 20_000)
    assert await env.paid_webhook(top.payment_id) == 200
    assert await env.paid_webhook(top.payment_id, status="refunded", at=now() + timedelta(seconds=1)) == 200
    assert await env.balance(uid) == 0
    await env.drain()
    assert not await env.rows("select id from attention_items where dedup_key like 'billing:chargeback:%'")
    await env.assert_wallet_invariants()


async def test_webhook_with_auto_complete_is_fast_and_lean(env: BillingEnv) -> None:
    """The whole crediting runs inside the webhook transaction: a fixed handful of statements, no HTTP, well
    under the 100 ms budget (heavy work — fulfill, panel, Telegram — is in jobs)."""
    import time

    from tests.payments.conftest import stub_webhook

    inst = env.pay.inst("stubpay")
    timings: list[float] = []
    for _ in range(9):
        uid, order_id, ref = await _waiting(env)
        top = await env.topup(uid, 17_900, parent=order_id, ui_ref=ref)
        row = await env.pay.payment(top.payment_id)
        req = stub_webhook("paid", ext=row["external_id"], order=top.payment_id, amount="179.00", at=now())
        http_before, sql_before = len(env.pay.http.calls), env.db.queries
        started = time.perf_counter()
        resp = await env.pay.core.handle_webhook(inst.id, inst.webhook_token, req)
        timings.append(time.perf_counter() - started)
        assert resp.status == 200
        assert env.db.queries - sql_before <= 12
        assert len(env.pay.http.calls) == http_before
        assert (await env.order(order_id))["status"] == "paid"
    assert sorted(timings)[len(timings) // 2] < 0.1, timings
