"""Manual payment: receipt → card in «💳 Оплаты» → only an admin with ``payments.confirm`` confirms (04 §8,
§9.1)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import pytest

from svbg.billing.receipts import ReceiptDecision, ReceiptError, Receipts, ReceiptView
from svbg.core.clock import now
from svbg.payments.core import PermissionDeniedError
from tests.billing.kit import BillingEnv

OWNERS = frozenset({1})


@dataclass
class FakeCards:
    posted: list[ReceiptView] = field(default_factory=list)
    decisions: list[tuple[int, str]] = field(default_factory=list)

    async def post(self, receipt: ReceiptView) -> Mapping[str, Any] | None:
        self.posted.append(receipt)
        return {"chat_id": -100, "message_id": len(self.posted)}

    async def decided(self, receipt: ReceiptView, decision: ReceiptDecision) -> None:
        self.decisions.append((receipt.id, decision.outcome))


class _Clock:
    def __init__(self) -> None:
        self.at = now()

    def __call__(self) -> datetime:
        return self.at

    def shift(self, **delta: float) -> None:
        self.at += timedelta(**delta)


async def _manual_waiting(env: BillingEnv) -> tuple[int, int, str]:
    uid = await env.user()
    draft = await env.draft(uid)
    ref = env.messenger.show(await env.telegram_id(uid))
    await env.checkout.pay(draft.order_id, uid, ui_ref=ref)
    top = await env.topup(uid, 17_900, parent=draft.order_id, ui_ref=ref, instance_id=env.manual_id())
    assert top.checkout.kind == "details" and "2200" in top.checkout.details
    return uid, draft.order_id, top.payment_id


async def test_confirm_by_an_admin_completes_the_purchase(env: BillingEnv) -> None:
    cards = FakeCards()
    receipts = Receipts(env.db, env.pay.core, cards)
    uid, order_id, pid = await _manual_waiting(env)
    view = await receipts.submit(pid, uid, file_id="photo-1", comment="перевёл")
    assert (
        view.status == "submitted"
        and view.amount_minor == 17_900
        and view.card_ref == {"chat_id": -100, "message_id": 1}
    )
    # a member of the admin group who is not an admin of the bot
    member = await env.user()
    with pytest.raises(PermissionDeniedError) as e:
        await receipts.confirm(
            view.id,
            actor_telegram_id=await env.telegram_id(member),
            owner_ids=OWNERS,
            paid_amount_minor=17_900,
        )
    assert e.value.human == "Нет прав"
    assert (await receipts.get(view.id)).status == "submitted"  # type: ignore[union-attr]
    admin = await env.user(role="admin", perms=["payments.confirm"])
    decision = await receipts.confirm(
        view.id, actor_telegram_id=await env.telegram_id(admin), owner_ids=OWNERS, paid_amount_minor=17_900
    )
    assert (decision.outcome, decision.payment_outcome) == ("confirmed", "applied")
    assert (await env.order(order_id))["status"] == "paid"
    assert cards.decisions == [(view.id, "confirmed")]
    # a second admin pressing the same button
    other = await env.user(role="admin", perms=["payments.confirm"])
    again = await receipts.confirm(
        view.id, actor_telegram_id=await env.telegram_id(other), owner_ids=OWNERS, paid_amount_minor=17_900
    )
    assert again.outcome == "decided"
    assert [r["reason"] for r in await env.ledger(uid)] == ["topup", "purchase"]
    audit = await env.rows("select action from admin_audit order by id")
    assert [a["action"] for a in audit] == ["payments.confirm.denied", "payments.confirm"]
    with pytest.raises(ReceiptError):
        await receipts.submit(pid, uid, file_id="photo-2")


async def test_different_amount_is_a_mismatch(env: BillingEnv) -> None:
    receipts = Receipts(env.db, env.pay.core)
    uid, order_id, pid = await _manual_waiting(env)
    view = await receipts.submit(pid, uid, file_id="photo")
    admin = await env.user(role="admin", perms=["payments.confirm"])
    decision = await receipts.confirm(
        view.id, actor_telegram_id=await env.telegram_id(admin), owner_ids=OWNERS, paid_amount_minor=10_000
    )
    assert decision.payment_outcome == "mismatch"
    assert await env.balance(uid) == 0
    assert (await env.order(order_id))["status"] == "awaiting_funds"


async def test_reject_needs_a_reason_and_resubmit_replaces_the_file(env: BillingEnv) -> None:
    clock = _Clock()
    receipts = Receipts(env.db, env.pay.core, clock=clock)
    uid, _order_id, pid = await _manual_waiting(env)
    first = await receipts.submit(pid, uid, file_id="a")
    clock.shift(minutes=2)
    second = await receipts.submit(pid, uid, file_id="b")
    assert first.id == second.id and second.file_id == "b"
    owner_tg = await env.telegram_id(await env.user(role="owner"))
    with pytest.raises(ValueError):
        await receipts.reject(first.id, actor_telegram_id=owner_tg, owner_ids=OWNERS, reason=" ")
    decision = await receipts.reject(
        first.id, actor_telegram_id=owner_tg, owner_ids=OWNERS, reason="не пришло"
    )
    assert decision.outcome == "rejected"
    assert (await env.pay.payment(pid))["status"] == "canceled"
    row = (await env.rows("select status, decision_reason from manual_receipts where id = $1", first.id))[0]
    assert row == {"status": "rejected", "decision_reason": "не пришло"}


async def test_foreign_or_unknown_payment(env: BillingEnv) -> None:
    receipts = Receipts(env.db, env.pay.core)
    uid, _order_id, pid = await _manual_waiting(env)
    stranger = await env.user()
    with pytest.raises(ReceiptError) as e:
        await receipts.submit(pid, stranger, file_id="x")
    assert e.value.code == "not_found"
    with pytest.raises(ReceiptError):
        await receipts.confirm(10**9, actor_telegram_id=1, owner_ids=OWNERS, paid_amount_minor=1)
    assert uid
