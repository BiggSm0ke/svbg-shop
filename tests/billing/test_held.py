"""A paid purchase of a frozen / banned user (``held``): the user is told the money is safe, the owner decides
with one button (rights re-checked, audited), and an undecided purchase goes back to the wallet."""

from __future__ import annotations

from datetime import timedelta

import pytest

from svbg.billing.crediting import held_fix_action
from svbg.billing.fulfill import HELD_TTL
from svbg.billing.ports import Notice, UiRef
from svbg.billing.texts import SCREEN_HELD, SCREEN_REFUNDED
from svbg.services.roles import RoleError
from tests.billing.kit import BillingEnv

OWNERS: frozenset[int] = frozenset()


async def _freeze(env: BillingEnv, user_id: int) -> None:
    sid = await env.s.new_sub(None)
    await env.db.raw(
        "update subscriptions set user_id = $1, hold_kind = 'ip_guard', hold_since = now() where id = $2",
        user_id,
        sid,
    )


async def _held_after_pay(env: BillingEnv) -> tuple[int, int, UiRef]:
    """Paid from the balance, then frozen by IP Guard before the fulfill job ran."""
    uid = await env.user()
    await env.fund(uid, 20_000)
    draft = await env.draft(uid)
    ref = env.messenger.show(await env.telegram_id(uid))
    assert (await env.checkout.pay(draft.order_id, uid, ui_ref=ref)).outcome == "paid"
    await _freeze(env, uid)
    await env.drain()
    assert (await env.order(draft.order_id))["status"] == "held"
    return uid, draft.order_id, ref


async def test_frozen_between_pay_and_fulfill_then_refund_to_the_wallet(env: BillingEnv) -> None:
    uid, order_id, ref = await _held_after_pay(env)
    assert await env.balance(uid) == 2_100  # taken, nothing given yet
    # the user is not left with «⏳ Оформляю…»: the same message says the money is safe
    shown = env.messenger.notice(ref)
    assert isinstance(shown, Notice) and shown.screen == SCREEN_HELD
    assert "Деньги не пропадут" in shown.text
    # the owner's item leads to the decision screen
    item = (
        await env.rows(
            "select fix_action from attention_items where dedup_key = $1", f"billing:held:{order_id}"
        )
    )[0]
    assert item["fix_action"] == held_fix_action(order_id) == f"screen:bill.held:{order_id}"
    resolve = env.billing.fulfiller.resolve_held
    # a member of the admin group who is no admin of the bot, and an admin without the wallet right
    stranger = await env.telegram_id(await env.user())
    support = await env.telegram_id(await env.user(role="admin", perms=["payments.confirm"]))
    for actor in (stranger, support):
        with pytest.raises(RoleError) as e:
            await resolve(order_id, "refund", actor_telegram_id=actor, owner_ids=OWNERS)
        assert e.value.text == "Нет прав"
    assert (await env.order(order_id))["status"] == "held"
    admin = await env.telegram_id(await env.user(role="admin", perms=["wallet.adjust"]))
    res = await resolve(order_id, "refund", actor_telegram_id=admin, owner_ids=OWNERS)
    assert (res.action, res.refunded_minor) == ("refund", 17_900)
    assert await env.balance(uid) == 20_000
    assert (await env.order(order_id))["status"] == "canceled"
    audit = await env.rows("select action, amount_minor, reason, target from admin_audit order by id")
    assert [a["action"] for a in audit] == [
        "billing.held.denied",
        "billing.held.denied",
        "billing.held.refund",
    ]
    assert audit[-1]["amount_minor"] == 17_900 and audit[-1]["target"] == f"order:{order_id}"
    assert audit[-1]["reason"]
    # the decision closes the owner's item
    assert f"billing:held:{order_id}" not in await env.s.attention_keys()
    await env.drain()
    shown = env.messenger.notice(ref)
    assert isinstance(shown, Notice) and shown.screen == SCREEN_REFUNDED
    assert (await resolve(order_id, "credit_hold", actor_telegram_id=admin)).action == "noop"
    await env.assert_wallet_invariants()


async def test_owner_credits_the_term_into_the_freeze(env: BillingEnv) -> None:
    uid, order_id, _ref = await _held_after_pay(env)
    owner_tg = 777_000_001  # OWNER_IDS: an owner even without a stored role
    await env.pay.add_user(owner_tg)
    res = await env.billing.fulfiller.resolve_held(
        order_id, "credit_hold", actor_telegram_id=owner_tg, owner_ids={owner_tg}, reason="подтверждено"
    )
    assert res.action == "credit_hold"
    await env.drain()
    assert (await env.order(order_id))["status"] == "fulfilled"
    assert await env.balance(uid) == 2_100
    audit = await env.rows("select action, role, reason from admin_audit")
    assert audit == [{"action": "billing.held.credit_hold", "role": "owner", "reason": "подтверждено"}]


async def test_undecided_held_purchase_returns_to_the_wallet(env: BillingEnv) -> None:
    uid, order_id, ref = await _held_after_pay(env)
    await env.billing.sweep()
    assert (await env.order(order_id))["status"] == "held"  # a week is given to decide
    await env.db.raw(
        "update orders set updated_at = now() - $2::interval where id = $1",
        order_id,
        HELD_TTL + timedelta(hours=1),
    )
    await env.billing.sweep()
    assert (await env.order(order_id))["status"] == "held"  # the sweeper looks at held orders hourly
    assert await env.billing.fulfiller.expire_held() == 1
    assert (await env.order(order_id))["status"] == "canceled"
    assert await env.balance(uid) == 20_000
    await env.drain()
    assert env.messenger.notice(ref).screen == SCREEN_REFUNDED  # type: ignore[union-attr]
    keys = await env.s.attention_keys()
    assert f"billing:held_expired:{order_id}" in keys and f"billing:held:{order_id}" not in keys
    assert not [k for k in keys if k.startswith("billing:fulfill_failed")]
    audit = await env.rows("select action, actor_id, amount_minor from admin_audit")
    assert audit == [{"action": "billing.held.expired", "actor_id": None, "amount_minor": 17_900}]
    assert await env.billing.fulfiller.expire_held() == 0  # nothing twice
    assert [r["reason"] for r in await env.ledger(uid)] == ["bonus", "purchase", "purchase_refund"]
    await env.assert_wallet_invariants()


async def test_held_by_crediting_is_canceled_without_money_moving(env: BillingEnv) -> None:
    """The top-up landed while the user was frozen: the purchase was never paid, so «refund» only cancels."""
    uid = await env.user()
    draft = await env.draft(uid)
    ref = env.messenger.show(await env.telegram_id(uid))
    await env.checkout.pay(draft.order_id, uid, ui_ref=ref)
    top = await env.topup(uid, 17_900, parent=draft.order_id, ui_ref=ref)
    await _freeze(env, uid)
    assert await env.paid_webhook(top.payment_id) == 200
    assert (await env.order(draft.order_id))["status"] == "held"
    await env.drain()
    item = (
        await env.rows(
            "select fix_action from attention_items where dedup_key = $1", f"billing:held:{draft.order_id}"
        )
    )[0]
    assert item["fix_action"] == held_fix_action(draft.order_id)
    owner = await env.telegram_id(await env.user(role="owner"))
    res = await env.billing.fulfiller.resolve_held(draft.order_id, "refund", actor_telegram_id=owner)
    assert (res.action, res.refunded_minor) == ("refund", 0)
    assert await env.balance(uid) == 17_900  # the top-up stays on the balance
    assert (await env.order(draft.order_id))["status"] == "canceled"


def test_the_fix_action_opens_an_admin_screen() -> None:
    from svbg.tg.admin.status import fix_button_target

    assert fix_button_target(held_fix_action(42)) == ("bill.held", "42")
