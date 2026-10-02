"""Property tests of the wallet model (07 §4.5 п.7): random scenarios with fixed seeds on a real database.

Each scenario is one fresh user doing a random sequence of: funding, «Оплатить» (enough / not enough money),
top-ups with and without a waiting purchase, **parallel** webhooks for the same payment (different signatures,
the same body), wrong amounts, ``expired`` before ``paid`` (late payment wins), windows running out, the
sweeper, cancellations, replaced purchases, freezes, chargebacks and racing fulfills. After every scenario:

* ``Σ wallet_ledger = users.wallet_minor``, running sums equal ``balance_after``, nothing below zero;
* at most one ``awaiting_funds`` purchase per user;
* every payment is credited at most once — exactly once if it is ``paid``/``refunded``, never otherwise;
* a top-up is ``credited`` iff its payment was paid, and credited the order's amount;
* at most one ``purchase`` debit per order; a paid / fulfilled order has it (unless free), an unpaid one not;
  a canceled paid order got its refund;
* a fulfilled order changed the subscription exactly once (one journal row with its reference).

4 × 55 = 220 scenarios; parameters are fixed seeds (deterministic under pytest-xdist).
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest

from svbg.billing.checkout import BillingError
from svbg.core.clock import now
from svbg.payments.core import CheckoutError
from tests.billing.kit import BillingEnv

SCENARIOS_PER_SEED = 55
ACTIONS = (
    ("fund", 3),
    ("buy", 5),
    ("topup_parent", 5),
    ("topup_plain", 2),
    ("webhook", 7),
    ("webhook_bad_amount", 1),
    ("expire_status_then_paid", 1),
    ("window_over", 2),
    ("sweep", 1),
    ("cancel", 1),
    ("fulfill", 3),
    ("freeze_toggle", 1),
    ("chargeback", 1),
)
_NAMES = [a for a, _ in ACTIONS]
_WEIGHTS = [w for _, w in ACTIONS]


@dataclass
class Scenario:
    rng: random.Random
    uid: int = 0
    tg: int = 0
    payments: list[str] = field(default_factory=list)
    hold_sub: int | None = None
    log: list[str] = field(default_factory=list)


async def _waiting_order(env: BillingEnv, uid: int) -> dict[str, Any] | None:
    rows = await env.rows("select * from orders where user_id = $1 and status = 'awaiting_funds'", uid)
    return rows[0] if rows else None


async def _act(env: BillingEnv, sc: Scenario, action: str) -> None:
    rng = sc.rng
    uid = sc.uid
    sc.log.append(action)
    if action == "fund":
        await env.fund(uid, rng.choice((1_000, 5_000, 17_900, 30_000)))
    elif action == "buy":
        draft = await env.draft(uid, days=rng.choice((30, 90)), extra_devices=rng.choice((0, 0, 1)))
        ref = env.messenger.show(sc.tg)
        await env.checkout.pay(draft.order_id, uid, ui_ref=ref)
    elif action in ("topup_parent", "topup_plain"):
        parent = await _waiting_order(env, uid) if action == "topup_parent" else None
        if parent is not None:
            missing = max(0, int(parent["total_minor"]) - await env.balance(uid))
            amount = rng.choice((max(10_000, missing), max(10_000, missing), 10_000, 20_000))
        else:
            amount = rng.choice((10_000, 17_900, 50_000))
        try:
            top = await env.topup(uid, amount, parent=parent["id"] if parent else None)
        except CheckoutError:
            return  # frozen: no invoice in any path
        except BillingError as e:
            assert e.code in ("too_many_invoices", "stale_price"), e
            return
        sc.payments.append(top.payment_id)
    elif action in ("webhook", "webhook_bad_amount") and sc.payments:
        pid = rng.choice(sc.payments)
        at = now()
        n = rng.randint(1, 4)
        amount = "1.00" if action == "webhook_bad_amount" else None
        sends = [env.paid_webhook(pid, at=at - timedelta(seconds=i % 3), amount=amount) for i in range(n)]
        statuses = await asyncio.gather(*sends)
        assert set(statuses) <= {200, 503}, statuses
    elif action == "expire_status_then_paid" and sc.payments:
        pid = rng.choice(sc.payments)
        await env.paid_webhook(pid, status="expired", at=now() - timedelta(seconds=5))
        if rng.random() < 0.7:
            await env.paid_webhook(pid, at=now())  # «поздняя оплата побеждает»
    elif action == "window_over":
        await env.db.raw(
            "update orders set autocomplete_until = now() - interval '1 second' "
            "where user_id = $1 and status = 'awaiting_funds'",
            uid,
        )
    elif action == "sweep":
        await env.billing.sweep()
    elif action == "cancel":
        rows = await env.rows(
            "select id from orders where user_id = $1 and status in ('draft', 'awaiting_funds')", uid
        )
        if rows:
            await env.checkout.cancel(rng.choice(rows)["id"], uid)
    elif action == "fulfill":
        rows = await env.rows("select id from orders where user_id = $1 and status = 'paid'", uid)
        if rows:
            oid = rng.choice(rows)["id"]
            f = env.billing.fulfiller
            await asyncio.gather(f.fulfill(oid), f.fulfill(oid))
    elif action == "freeze_toggle":
        if sc.hold_sub is None:
            sc.hold_sub = await env.s.new_sub(None)
            await env.db.raw(
                "update subscriptions set user_id = $1, hold_kind = 'ip_guard', hold_since = now() "
                "where id = $2",
                uid,
                sc.hold_sub,
            )
        else:
            await env.db.raw("update subscriptions set user_id = null where id = $1", sc.hold_sub)
            sc.hold_sub = None
    elif action == "chargeback" and sc.payments:
        pid = rng.choice(sc.payments)
        await env.paid_webhook(pid, status="chargeback", at=now() + timedelta(seconds=1))


async def _check(env: BillingEnv, sc: Scenario) -> None:
    uid = sc.uid
    ctx = f"user {uid}: {' → '.join(sc.log)}"
    waiting = await env.rows("select id from orders where user_id = $1 and status = 'awaiting_funds'", uid)
    assert len(waiting) <= 1, ctx
    pays = await env.rows("select id, status, order_id from payments where user_id = $1", uid)
    credits = await env.rows(
        "select ref_id, amount_minor from wallet_ledger where user_id = $1 and reason = 'topup'", uid
    )
    credited: dict[str, list[int]] = {}
    for c in credits:
        credited.setdefault(c["ref_id"], []).append(int(c["amount_minor"]))
    for p in pays:
        got = credited.get(p["id"], [])
        if p["status"] in ("paid", "refunded"):
            topup = await env.order(p["order_id"])
            assert got == [int(topup["total_minor"])], (ctx, p)
            assert topup["status"] == "credited", (ctx, p, topup)
        else:
            assert got == [], (ctx, p)
            topup = await env.order(p["order_id"])
            assert topup["status"] != "credited", (ctx, p, topup)
    orders = await env.rows("select * from orders where user_id = $1 and kind <> 'topup'", uid)
    for o in orders:
        debits = await env.rows(
            "select amount_minor from wallet_ledger where user_id = $1 and reason = 'purchase' "
            "and ref_id = $2",
            uid,
            str(o["id"]),
        )
        refunds = await env.rows(
            "select amount_minor from wallet_ledger where user_id = $1 and reason = 'purchase_refund' "
            "and ref_id = $2",
            uid,
            str(o["id"]),
        )
        assert len(debits) <= 1 and len(refunds) <= 1, (ctx, o)
        status = o["status"]
        if status in ("paid", "fulfilled") and o["total_minor"] > 0:
            assert [d["amount_minor"] for d in debits] == [-int(o["total_minor"])], (ctx, o)
        if status in ("draft", "awaiting_funds", "expired"):
            assert debits == [], (ctx, o)
        if status == "canceled":
            assert len(debits) == len(refunds), (ctx, o)
        journal = await env.rows(
            "select id from subscription_events where ref_type = 'order' and ref_id = $1", str(o["id"])
        )
        assert len(journal) == (1 if status == "fulfilled" else 0), (ctx, o)


async def _run_seed(env: BillingEnv, seed: int) -> None:
    for n in range(SCENARIOS_PER_SEED):
        sc = Scenario(random.Random(seed * 10_000 + n))
        sc.uid = await env.user()
        sc.tg = await env.telegram_id(sc.uid)
        for _ in range(sc.rng.randint(4, 14)):
            await _act(env, sc, sc.rng.choices(_NAMES, _WEIGHTS)[0])
        # whatever was paid gets fulfilled eventually; then everything must still hold
        for row in await env.rows("select id from orders where user_id = $1 and status = 'paid'", sc.uid):
            await env.billing.fulfiller.fulfill(row["id"])
        await _check(env, sc)
    await env.assert_wallet_invariants()


@pytest.mark.slow
@pytest.mark.parametrize("seed", [11, 22, 33, 44], ids=["seed11", "seed22", "seed33", "seed44"])
async def test_wallet_invariants_hold_in_random_scenarios(env: BillingEnv, seed: int) -> None:
    await _run_seed(env, seed)
    stats = await _stats(env)
    # the scenarios really went through the interesting paths (not only the happy one)
    for key in ("order:fulfilled", "order:canceled", "topup:credited", "ledger:purchase", "payment:mismatch"):
        assert stats.get(key, 0) > 0, (key, stats)
    assert stats["autocompleted"] > 0, stats


async def _stats(env: BillingEnv) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in await env.rows("select kind = 'topup' as t, status, count(*) as n from orders group by 1, 2"):
        out[("topup:" if r["t"] else "order:") + r["status"]] = int(r["n"])
    for r in await env.rows("select status, count(*) as n from payments group by 1"):
        out["payment:" + r["status"]] = int(r["n"])
    for r in await env.rows("select reason, count(*) as n from wallet_ledger group by 1"):
        out["ledger:" + r["reason"]] = int(r["n"])
    auto = await env.rows(
        "select count(*) as n from orders p where p.status = 'fulfilled' and exists ("
        " select 1 from orders t where t.parent_order_id = p.id and t.status = 'credited')"
    )
    out["autocompleted"] = int(auto[0]["n"])
    return out
