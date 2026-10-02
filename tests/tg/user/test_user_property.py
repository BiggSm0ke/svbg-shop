"""Property test through the UI (fixed seeds, deterministic under pytest-xdist): random click sequences —
buying with and without enough money, picking payment methods, double taps, cancellations, plain top-ups,
duplicated and concurrent webhooks, auto-complete windows running out — never break the money invariants:

* ``Σ wallet_ledger = users.wallet_minor``, running sums equal ``balance_after``, nothing below zero;
* at most one ``awaiting_funds`` purchase per user;
* every paid top-up is credited exactly once; nothing unpaid is credited;
* every order is debited at most once, a fulfilled order exactly once (or it was free);
* no screen ever fails (the error hub stays empty) and every shown message has text.
"""

from __future__ import annotations

import asyncio
import random
from decimal import Decimal

import pytest

from svbg.core.clock import now
from tests.payments.conftest import stub_webhook
from tests.tg.user.kit import UserEnv, build_user_env

SCENARIOS = 8


async def _try_press(env: UserEnv, tg: int, label: str) -> bool:
    try:
        await env.press(tg, label)
    except AssertionError:
        return False
    return True


async def _buy(env: UserEnv, tg: int, rng: random.Random) -> None:
    plan = rng.choice([1, 2])
    periods = await env.click(tg, f"v1:buy_plan:o:{plan}")
    choices = [label for label in periods.labels() if "—" in label]
    if not choices:
        return
    checkout = await env.press(tg, rng.choice(choices))
    method = rng.choice(["СБП", "Stars", "Перевод"])
    try:
        pay = checkout.data("Оплатить")
    except AssertionError:  # not enough money: the methods are on the checkout itself
        try:
            pay = checkout.data(method)
        except AssertionError:
            return
        await env.click(tg, pay, message_id=checkout.message_id)
        if rng.random() < 0.3:  # a double tap on the method
            await env.click(tg, pay, message_id=checkout.message_id)
        return
    await env.click(tg, pay, message_id=checkout.message_id)
    if rng.random() < 0.3:  # a double tap on «Оплатить»
        await env.click(tg, pay, message_id=checkout.message_id)
    if rng.random() < 0.7:
        await _try_press(env, tg, method)


async def _topup(env: UserEnv, tg: int, rng: random.Random) -> None:
    await env.click(tg, "v1:bal:o")
    await _try_press(env, tg, rng.choice(["179", "499", "899"]))
    await _try_press(env, tg, "СБП")


async def _webhooks(env: UserEnv, uid: int, rng: random.Random) -> None:
    rows = await env.rows(
        "select id, external_id, amount_minor from payments where user_id = $1 and status = 'pending' "
        "and checkout->>'kind' = 'url'",
        uid,
    )
    for row in rows:
        if rng.random() < 0.3:
            continue
        amount = str(Decimal(row["amount_minor"]).scaleb(-2))
        req = stub_webhook("paid", ext=row["external_id"], order=row["id"], amount=amount, at=now())
        if rng.random() < 0.4:  # the provider retries in parallel
            await asyncio.gather(env.b.pay.send(req), env.b.pay.send(req))
        else:
            await env.b.pay.send(req)


async def _stars(env: UserEnv, uid: int, rng: random.Random) -> None:
    rows = await env.rows(
        "select id, amount_minor from payments "
        "where user_id = $1 and status = 'pending' and currency = 'XTR'",
        uid,
    )
    for row in rows:
        charge = f"ch-{row['id']}"
        cp = env.path.chat_payments
        if await cp.decide_pre_checkout(row["id"], "XTR", int(row["amount_minor"])) is None:
            for _ in range(rng.choice([1, 2])):
                await cp.credit_stars(
                    row["id"], charge_id=charge, currency="XTR", total_amount=int(row["amount_minor"])
                )


async def _late(env: UserEnv, uid: int) -> None:
    await env.db.raw(
        "update orders set autocomplete_until = now() - interval '1 minute' "
        "where user_id = $1 and status = 'awaiting_funds'",
        uid,
    )


async def _check(env: UserEnv) -> None:
    await env.b.assert_wallet_invariants()
    waiting = await env.rows(
        "select user_id from orders where status = 'awaiting_funds' group by user_id having count(*) > 1"
    )
    assert waiting == []
    paid_topups = await env.rows(
        "select p.id from orders o join payments p on p.order_id = o.id "
        "where o.kind = 'topup' and p.status = 'paid'"
    )
    credited = await env.rows(
        "select ref_id from wallet_ledger where reason = 'topup' and ref_type = 'payment'"
    )
    assert sorted(str(r["id"]) for r in paid_topups) == sorted(str(r["ref_id"]) for r in credited)
    unpaid_credited = await env.rows(
        "select o.id from orders o where o.kind = 'topup' and o.status = 'credited' and not exists "
        "(select 1 from payments p where p.order_id = o.id and p.status = 'paid')"
    )
    assert unpaid_credited == []
    debits = await env.rows(
        "select ref_id, count(*) as n from wallet_ledger where reason = 'purchase' "
        "group by ref_id having count(*) > 1"
    )
    assert debits == []
    fulfilled_without_debit = await env.rows(
        "select o.id from orders o where o.status = 'fulfilled' and o.total_minor > 0 and not exists "
        "(select 1 from wallet_ledger w where w.reason = 'purchase' and w.ref_id = o.id::text)"
    )
    assert fulfilled_without_debit == []
    assert env.hub.captured == []
    assert all(m.text.strip() for m in env.tg.messages.values())


@pytest.mark.parametrize("seed", [101, 202, 303])
async def test_random_click_sequences_keep_the_money_invariants(pg_dsn: str, seed: int) -> None:
    rng = random.Random(seed)
    async with build_user_env(pg_dsn, plans=(1, 2)) as env:
        for _ in range(SCENARIOS):
            uid, tg = await env.new_user(balance=rng.choice([0, 5_000, 17_900, 60_000]))
            await env.open(tg)
            for _step in range(rng.randint(4, 9)):
                action = rng.choices(
                    ["buy", "topup", "webhooks", "stars", "late", "drain", "cancel", "home"],
                    weights=[5, 2, 4, 2, 1, 3, 1, 1],
                )[0]
                if action == "buy":
                    await _buy(env, tg, rng)
                elif action == "topup":
                    await _topup(env, tg, rng)
                elif action == "webhooks":
                    await _webhooks(env, uid, rng)
                elif action == "stars":
                    await _stars(env, uid, rng)
                elif action == "late":
                    await _late(env, uid)
                elif action == "drain":
                    await env.drain(make_due=rng.random() < 0.5)
                elif action == "cancel":
                    await _try_press(env, tg, "Отменить покупку")
                else:
                    await env.click(tg, "v1:home:o")
            await _webhooks(env, uid, rng)
            await env.drain(make_due=True)
            await _check(env)
