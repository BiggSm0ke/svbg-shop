"""Top-up guards: test-mode cash desks, the invoice limit and reuse, a purchase that no longer waits, the
frozen price's lifetime, the reorder budget."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta

import pytest

from svbg.billing.checkout import PURCHASE_TTL, TOPUP_RATE_MAX, BillingError
from tests.billing.kit import BillingEnv, build_billing_env


@pytest.fixture
async def test_env(pg_dsn: str, billing_schema: None) -> AsyncIterator[BillingEnv]:
    """``stubpay`` runs in test mode (``PAY_STUBPAY_TEST_MODE=true``) and is enabled."""
    async with build_billing_env(pg_dsn, test_instances=("stubpay",)) as environment:
        yield environment


# ------------------------------------------------------------------------------------------- test mode


async def test_test_mode_desk_is_not_offered_to_customers(test_env: BillingEnv) -> None:
    env = test_env
    instances = env.pay.registry.all()
    assert env.stub_id() not in {o.instance_id for o in env.billing.topup_options(5_000, instances)}
    assert env.stub_id() in {o.instance_id for o in env.billing.topup_options(5_000, instances, staff=True)}
    uid = await env.user()
    with pytest.raises(BillingError) as e:
        await env.topup(uid, 20_000)
    assert e.value.code == "topup_currency"
    assert await env.rows("select id from orders where user_id = $1", uid) == []
    admin = await env.user(role="admin")
    assert (await env.topup(admin, 20_000)).payment_id


async def test_test_money_of_a_customer_is_never_credited(test_env: BillingEnv) -> None:
    env = test_env
    uid = await env.user()
    draft = await env.draft(uid)
    ref = env.messenger.show(await env.telegram_id(uid))
    await env.checkout.pay(draft.order_id, uid, ui_ref=ref)
    # an invoice opened while the desk was live, or forced by a caller: the payment is a test one anyway
    top = await env.checkout.start_topup(
        uid, instance_id=env.stub_id(), amount_minor=17_900, parent_order_id=draft.order_id, allow_test=True
    )
    assert await env.paid_webhook(top.payment_id, test=True) == 200
    assert (await env.pay.payment(top.payment_id))["status"] == "paid"
    assert await env.balance(uid) == 0 and await env.ledger(uid) == []
    assert (await env.order(draft.order_id))["status"] == "awaiting_funds"  # nothing bought with test money
    topup = await env.order(top.order_id)
    assert (topup["status"], topup["note"]) == ("canceled", "test_payment")
    await env.drain()
    assert f"billing:test_payment:{env.stub_id()}" in await env.s.attention_keys()


async def test_staff_test_payment_runs_the_whole_flow_with_marked_money(test_env: BillingEnv) -> None:
    env = test_env
    owner = await env.user(role="owner")
    draft = await env.draft(owner)
    ref = env.messenger.show(await env.telegram_id(owner))
    await env.checkout.pay(draft.order_id, owner, ui_ref=ref)
    top = await env.topup(owner, 17_900, parent=draft.order_id, ui_ref=ref)
    assert await env.paid_webhook(top.payment_id, test=True) == 200
    assert (await env.order(draft.order_id))["status"] == "paid"
    assert [r["reason"] for r in await env.ledger(owner)] == ["test_topup", "purchase"]
    # a chargeback of a test payment takes the test money back as well
    assert await env.paid_webhook(top.payment_id, status="refunded", test=True) == 200
    await env.assert_wallet_invariants()


# ------------------------------------------------------------------------------------------- invoices


async def test_same_invoice_is_handed_out_again(env: BillingEnv) -> None:
    uid = await env.user()
    draft = await env.draft(uid)
    await env.checkout.pay(draft.order_id, uid)
    first = await env.topup(uid, 17_900, parent=draft.order_id)
    created = len(env.pay.server.created)
    ref = env.messenger.show(await env.telegram_id(uid))
    again = await env.topup(uid, 17_900, parent=draft.order_id, ui_ref=ref)
    assert again.reused and not first.reused
    assert (again.payment_id, again.order_id) == (first.payment_id, first.order_id)
    assert again.checkout.pay_url == first.checkout.pay_url and again.parent_order_id == draft.order_id
    assert len(env.pay.server.created) == created  # no provider call
    assert (await env.order(first.order_id))["ui_ref"]["message_id"] == ref.message_id
    # another amount, method or a plain top-up is another invoice
    other = await env.topup(uid, 20_000, parent=draft.order_id)
    plain = await env.topup(uid, 17_900)
    assert len({first.payment_id, other.payment_id, plain.payment_id}) == 3
    assert plain.parent_order_id is None
    # a paid invoice is never handed out again
    assert await env.paid_webhook(plain.payment_id) == 200
    fresh = await env.topup(uid, 17_900)
    assert not fresh.reused and fresh.payment_id != plain.payment_id


async def test_in_chat_invoices_are_never_reused(env: BillingEnv) -> None:
    uid = await env.user()
    a = await env.topup(uid, 17_900, instance_id=env.stars_id())
    b = await env.topup(uid, 17_900, instance_id=env.stars_id())
    assert a.payment_id != b.payment_id and not b.reused


async def test_invoice_rate_limit_per_user(env: BillingEnv) -> None:
    uid = await env.user()
    for n in range(TOPUP_RATE_MAX):
        await env.topup(uid, 20_000 + n * 100)
    created = len(env.pay.server.created)
    with pytest.raises(BillingError) as e:
        await env.topup(uid, 30_000)
    assert e.value.code == "too_many_invoices" and "10 минут" in e.value.text
    assert len(env.pay.server.created) == created
    assert len(await env.rows("select id from payments where user_id = $1", uid)) == TOPUP_RATE_MAX
    # the same invoice is still handed out; other users are not affected; the window slides
    assert (await env.topup(uid, 20_000)).reused
    assert await env.topup(await env.user(), 30_000)
    await env.db.raw("update orders set created_at = now() - interval '11 minutes' where user_id = $1", uid)
    assert not (await env.topup(uid, 30_000)).reused


# ------------------------------------------------------------------------------------------- the parent


async def test_parent_that_no_longer_waits_is_refused(env: BillingEnv) -> None:
    uid = await env.user()
    draft = await env.draft(uid)
    await env.checkout.pay(draft.order_id, uid)
    await env.db.raw(
        "update orders set autocomplete_until = now() - interval '1 second' where id = $1", draft.order_id
    )
    await env.billing.sweep()
    assert (await env.order(draft.order_id))["status"] == "expired"
    with pytest.raises(BillingError) as e:
        await env.topup(uid, 17_900, parent=draft.order_id)
    assert e.value.code == "order_gone" and "Откройте её заново" in e.value.text
    assert await env.rows("select id from orders where user_id = $1 and kind = 'topup'", uid) == []
    for code in ("draft", "canceled", "paid"):
        await env.db.raw(
            "update orders set status = $2, autocomplete_until = null where id = $1", draft.order_id, code
        )
        with pytest.raises(BillingError, match="order_gone"):
            await env.topup(uid, 17_900, parent=draft.order_id)
    stranger = await env.user()
    with pytest.raises(BillingError, match="order_gone"):
        await env.topup(stranger, 17_900, parent=draft.order_id)


async def test_frozen_price_does_not_live_forever(env: BillingEnv) -> None:
    uid = await env.user()
    old = await env.draft(uid)
    await env.db.raw(
        "update orders set created_at = now() - $2::interval where id = $1",
        old.order_id,
        PURCHASE_TTL + timedelta(minutes=1),
    )
    with pytest.raises(BillingError) as e:
        await env.checkout.pay(old.order_id, uid)
    assert e.value.code == "stale_price"
    # a waiting purchase near the end of its life: the window never reaches past the purchase's lifetime
    waiting = await env.draft(uid, days=90)
    await env.db.raw(
        "update orders set created_at = now() - $2::interval where id = $1",
        waiting.order_id,
        PURCHASE_TTL - timedelta(minutes=10),
    )
    await env.checkout.pay(waiting.order_id, uid)
    deadline = (await env.order(waiting.order_id))["created_at"] + PURCHASE_TTL
    assert (await env.order(waiting.order_id))["autocomplete_until"] == deadline
    top = await env.topup(uid, 49_900, parent=waiting.order_id)  # «Пополнить» restarts the window…
    assert (await env.order(waiting.order_id))["autocomplete_until"] == deadline  # …but not past the deadline
    await env.db.raw(
        "update orders set created_at = now() - $2::interval where id = $1",
        waiting.order_id,
        PURCHASE_TTL + timedelta(minutes=1),
    )
    with pytest.raises(BillingError) as e:
        await env.checkout.pay(waiting.order_id, uid)
    assert e.value.code == "stale_price"
    with pytest.raises(BillingError, match="stale_price"):
        await env.topup(uid, 50_000, parent=waiting.order_id)
    # money that arrives after the deadline stays on the balance (late payment)
    await env.db.raw(
        "update orders set autocomplete_until = now() - interval '1 second' where id = $1", waiting.order_id
    )
    assert await env.paid_webhook(top.payment_id) == 200
    assert (await env.order(waiting.order_id))["status"] == "expired"
    assert await env.balance(uid) == 49_900


async def test_reorder_is_two_sql(env: BillingEnv) -> None:
    uid = await env.user()
    first = await env.draft(uid, days=90, extra_devices=1)
    before = env.db.queries
    draft = await env.checkout.reorder(first.order_id, uid)
    assert env.db.queries - before == 2, env.db.counter.since(before) if env.db.counter else None
    assert (draft.quote.days, draft.quote.extra_devices) == (90, 1) and draft.order_id != first.order_id
    with pytest.raises(BillingError, match="not_found"):
        await env.checkout.reorder(first.order_id, await env.user())


def test_refusals_speak_the_users_language() -> None:
    assert "Open it again" in BillingError("order_gone").localized("en")
    assert BillingError("order_gone").localized("ru") == BillingError("order_gone").text
    custom = BillingError("topup_amount", "Сумма пополнения — от 10 ₽")
    assert custom.localized("en") == custom.text
