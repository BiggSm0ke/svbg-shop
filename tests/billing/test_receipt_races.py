"""Receipt decisions are serialised (one decision per receipt) and resubmissions cannot flood «💳 Оплаты»."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import pytest

from svbg.billing.receipts import RESUBMIT_MAX, ReceiptDecision, ReceiptError, Receipts, ReceiptView
from svbg.core.clock import now
from svbg.payments.core import PermissionDeniedError
from tests.billing.kit import BillingEnv

OWNERS: frozenset[int] = frozenset()


@dataclass
class Cards:
    posted: list[ReceiptView] = field(default_factory=list)
    decisions: list[tuple[int, str, str | None]] = field(default_factory=list)

    async def post(self, receipt: ReceiptView) -> Mapping[str, Any] | None:
        self.posted.append(receipt)
        return {"chat_id": -100, "message_id": 1}

    async def decided(self, receipt: ReceiptView, decision: ReceiptDecision) -> None:
        self.decisions.append((receipt.id, decision.outcome, decision.payment_outcome))


class Clock:
    def __init__(self) -> None:
        self.at = now()

    def __call__(self) -> datetime:
        return self.at


class Interleave:
    """The payment core, with a hook that runs while one admin's decision is inside the core."""

    def __init__(self, core: Any) -> None:
        self.core = core
        self.during: list[Any] = []

    async def _hook(self) -> None:
        while self.during:
            await self.during.pop(0)()

    async def confirm_manual(self, payment_id: str, **kw: Any) -> Any:
        await self._hook()
        return await self.core.confirm_manual(payment_id, **kw)

    async def reject_manual(self, payment_id: str, **kw: Any) -> bool:
        await self._hook()
        return await self.core.reject_manual(payment_id, **kw)


async def _submitted(env: BillingEnv, receipts: Receipts) -> tuple[int, int, str, int]:
    uid = await env.user()
    draft = await env.draft(uid)
    ref = env.messenger.show(await env.telegram_id(uid))
    await env.checkout.pay(draft.order_id, uid, ui_ref=ref)
    top = await env.topup(uid, 17_900, parent=draft.order_id, ui_ref=ref, instance_id=env.manual_id())
    view = await receipts.submit(top.payment_id, uid, file_id="photo")
    return uid, draft.order_id, top.payment_id, view.id


async def _admins(env: BillingEnv) -> tuple[int, int]:
    a = await env.telegram_id(await env.user(role="admin", perms=["payments.confirm"]))
    b = await env.telegram_id(await env.user(role="admin", perms=["payments.confirm"]))
    return a, b


async def test_reject_pressed_during_a_confirmation_changes_nothing(env: BillingEnv) -> None:
    core = Interleave(env.pay.core)
    cards = Cards()
    receipts = Receipts(env.db, core, cards)
    uid, order_id, pid, rid = await _submitted(env, receipts)
    a, b = await _admins(env)
    seen: list[ReceiptDecision] = []

    async def other_admin_rejects() -> None:
        seen.append(await receipts.reject(rid, actor_telegram_id=b, owner_ids=OWNERS, reason="не пришло"))

    core.during.append(other_admin_rejects)
    decision = await receipts.confirm(rid, actor_telegram_id=a, owner_ids=OWNERS, paid_amount_minor=17_900)
    assert decision.outcome == "confirmed" and seen == [ReceiptDecision("decided", rid)]
    assert (await env.pay.payment(pid))["status"] == "paid"
    assert (await env.order(order_id))["status"] == "paid"
    row = (await env.rows("select status from manual_receipts where id = $1", rid))[0]
    assert row["status"] == "confirmed" and cards.decisions == [(rid, "confirmed", "applied")]
    audit = [r["action"] for r in await env.rows("select action from admin_audit order by id")]
    assert audit == ["payments.confirm"]  # the second admin never reached the core
    assert await env.balance(uid) == 0
    await env.assert_wallet_invariants()


async def test_confirm_pressed_during_a_rejection_credits_nothing(env: BillingEnv) -> None:
    core = Interleave(env.pay.core)
    cards = Cards()
    receipts = Receipts(env.db, core, cards)
    uid, order_id, pid, rid = await _submitted(env, receipts)
    a, b = await _admins(env)
    seen: list[ReceiptDecision] = []

    async def other_admin_confirms() -> None:
        seen.append(
            await receipts.confirm(rid, actor_telegram_id=a, owner_ids=OWNERS, paid_amount_minor=17_900)
        )

    core.during.append(other_admin_confirms)
    decision = await receipts.reject(rid, actor_telegram_id=b, owner_ids=OWNERS, reason="не пришло")
    assert decision.outcome == "rejected" and seen == [ReceiptDecision("decided", rid)]
    assert (await env.pay.payment(pid))["status"] == "canceled"
    assert (await env.order(order_id))["status"] == "awaiting_funds"
    assert await env.ledger(uid) == []
    # a later press of the stale «✅» on the card does not revive the rejected payment either
    again = await receipts.confirm(rid, actor_telegram_id=a, owner_ids=OWNERS, paid_amount_minor=17_900)
    assert again.outcome == "decided" and await env.ledger(uid) == []
    assert cards.decisions == [(rid, "rejected", None)]


async def test_simultaneous_presses_agree_with_the_money(env: BillingEnv) -> None:
    receipts = Receipts(env.db, env.pay.core, Cards())
    a, b = await _admins(env)
    for _ in range(4):
        uid, _order_id, pid, rid = await _submitted(env, receipts)
        results = await asyncio.gather(
            receipts.confirm(rid, actor_telegram_id=a, owner_ids=OWNERS, paid_amount_minor=17_900),
            receipts.reject(rid, actor_telegram_id=b, owner_ids=OWNERS, reason="не пришло"),
        )
        outcomes = sorted(r.outcome for r in results)
        assert outcomes in (["confirmed", "decided"], ["decided", "rejected"]), outcomes
        receipt = (await env.rows("select status from manual_receipts where id = $1", rid))[0]["status"]
        payment = (await env.pay.payment(pid))["status"]
        credited = [r for r in await env.ledger(uid) if r["reason"] == "topup"]
        if receipt == "confirmed":
            assert payment == "paid" and len(credited) == 1
        else:
            assert receipt == "rejected" and payment == "canceled" and credited == []
    await env.assert_wallet_invariants()


async def test_receipt_left_open_by_a_crash_follows_the_payment(env: BillingEnv) -> None:
    receipts = Receipts(env.db, env.pay.core, Cards())
    a, _b = await _admins(env)
    # rejected in the core, the receipt not closed (the process died in between): «✅» must not credit it
    uid, _order_id, pid, rid = await _submitted(env, receipts)
    assert await env.pay.core.reject_manual(pid, actor_telegram_id=a, owner_ids=OWNERS, reason="нет денег")
    stranger = await env.telegram_id(await env.user())
    with pytest.raises(PermissionDeniedError):
        await receipts.confirm(rid, actor_telegram_id=stranger, owner_ids=OWNERS, paid_amount_minor=17_900)
    res = await receipts.confirm(rid, actor_telegram_id=a, owner_ids=OWNERS, paid_amount_minor=17_900)
    assert (res.outcome, res.payment_outcome) == ("decided", "canceled")
    assert (await env.pay.payment(pid))["status"] == "canceled" and await env.ledger(uid) == []
    assert (await env.rows("select status from manual_receipts where id = $1", rid))[0][
        "status"
    ] == "rejected"
    # confirmed in the core, the receipt not closed: «❌» must not claim it was rejected
    uid2, _order2, pid2, rid2 = await _submitted(env, receipts)
    await env.pay.core.confirm_manual(pid2, actor_telegram_id=a, owner_ids=OWNERS, paid_amount_minor=17_900)
    res = await receipts.reject(rid2, actor_telegram_id=a, owner_ids=OWNERS, reason="нет денег")
    assert (res.outcome, res.payment_outcome) == ("decided", "already_paid")
    assert (await env.rows("select status from manual_receipts where id = $1", rid2))[0][
        "status"
    ] == "confirmed"
    assert [r["reason"] for r in await env.ledger(uid2)] == ["topup", "purchase"]


async def test_resubmissions_are_limited(env: BillingEnv) -> None:
    clock = Clock()
    cards = Cards()
    receipts = Receipts(env.db, env.pay.core, cards, clock=clock)
    uid, _order_id, pid, _rid = await _submitted(env, receipts)
    assert len(cards.posted) == 1
    # the same file again (Telegram re-delivers, the user taps twice): nothing is posted
    await receipts.submit(pid, uid, file_id="photo")
    assert len(cards.posted) == 1
    with pytest.raises(ReceiptError) as e:
        await receipts.submit(pid, uid, file_id="photo-2")
    assert e.value.code == "too_often" and "через минуту" in e.value.text
    for n in range(RESUBMIT_MAX):
        clock.at += timedelta(minutes=2)
        await receipts.submit(pid, uid, file_id=f"photo-{n + 2}")
    assert len(cards.posted) == 1 + RESUBMIT_MAX
    clock.at += timedelta(minutes=2)
    with pytest.raises(ReceiptError) as e:
        await receipts.submit(pid, uid, file_id="photo-last")
    assert e.value.code == "too_many"
    row = (await env.rows("select file_id from manual_receipts where payment_id = $1", pid))[0]
    assert row["file_id"] == f"photo-{RESUBMIT_MAX + 1}"
    assert len(cards.posted) == 1 + RESUBMIT_MAX
