"""Review fixes: admin money limits on promo codes, exact discount limits (reservations + claim), and the
short promo row lock of an activation."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from svbg.billing.checkout import CheckoutService
from svbg.billing.fulfill import Fulfiller
from svbg.promo.rules import REFUSALS, PromoError
from svbg.promo.service import Actor, PromoService
from tests.dbkit import CountingDatabase
from tests.promo.kit import FakeCatalog, add_promo, add_sub, add_user

OWNER_TG, ADMIN_TG = 1, 2


async def one(db: CountingDatabase, sql: str, *args: object) -> dict:
    rows = await db.raw(sql, *args)
    assert rows, sql
    return dict(rows[0])


async def uses_of(db: CountingDatabase, promo_id: int) -> int:
    return int((await one(db, "select uses from promocodes where id = $1", promo_id))["uses"])


async def staff(db: CountingDatabase, tg: int, role: str, perms: str = "[]") -> Actor:
    uid = await add_user(db, tg, role=role)
    await db.raw("update users set perms = $1::jsonb where id = $2", perms, uid)
    return Actor(uid, role, telegram_id=tg)


@pytest.fixture
async def owner(db: CountingDatabase) -> Actor:
    return await staff(db, OWNER_TG, "owner")


@pytest.fixture
async def admin(db: CountingDatabase) -> Actor:
    return await staff(db, ADMIN_TG, "admin", '["promo"]')


def checkout(db: CountingDatabase, catalog: FakeCatalog) -> CheckoutService:
    return CheckoutService(db, catalog=catalog, payments=None, config=lambda: {"CURRENCY": "RUB"})


# ------------------------------------------------------------------------------- 1. admin limits on codes


async def test_admin_value_codes_obey_the_limits_and_need_a_reason(
    db: CountingDatabase, service: PromoService, admin: Actor, owner: Actor
) -> None:
    with pytest.raises(PromoError, match="только владелец"):
        await service.create(admin, kind="wallet", code="MILLION", values={"amount_minor": 100_000_000})
    with pytest.raises(PromoError, match="только владелец"):
        wd = {"amount_minor": 50_001, "days": 1}
        await service.create(admin, kind="wallet_days", code="WDAYS", values=wd)
    for kind, values in (
        ("days", {"days": 3650}),
        ("trial_extend", {"days": 60}),
        ("plan_gift", {"days": 365, "plan_id": 1}),
    ):
        with pytest.raises(PromoError, match=r"Больше 31 дн\."):
            await service.create(admin, kind=kind, code=None, values=values, reason="подарок")
    with pytest.raises(PromoError, match="причину"):
        await service.create(admin, kind="wallet", code="NOREASON", values={"amount_minor": 10_000})
    with pytest.raises(PromoError, match="причину"):
        await service.create(admin, kind="days", code="NOREASON2", values={"days": 7}, reason=" a ")
    assert not await db.raw("select 1 from promocodes")

    ok = await service.create(
        admin,
        kind="wallet",
        code="CHAN100",
        values={"amount_minor": 10_000},
        limits={"max_uses": 50},
        reason="  розыгрыш   в канале ",
    )
    assert ok.created_by == admin.user_id
    row = await one(db, "select actor_id, role, action, amount_minor, reason from admin_audit")
    assert row == {
        "actor_id": admin.user_id,
        "role": "admin",
        "action": "promo.create",
        "amount_minor": 10_000 * 50,  # what the code can give away in total
        "reason": "розыгрыш в канале",
    }
    # edits that change what it gives are checked the same way
    with pytest.raises(PromoError, match="только владелец"):
        await service.update(ok.id, admin, amount_minor=1_000_000, reason="больше")
    with pytest.raises(PromoError, match="причину"):
        await service.update(ok.id, admin, max_uses=1000)
    upd = await service.update(ok.id, admin, max_uses=100, reason="ещё один пост")
    assert upd.max_uses == 100
    last = await one(db, "select amount_minor, reason from admin_audit order by id desc limit 1")
    assert last == {"amount_minor": 10_000 * 100, "reason": "ещё один пост"}
    # a note or a switch needs no reason
    assert (await service.update(ok.id, admin, title="канал", enabled=False)).enabled is False
    # a discount is not a money grant: no reason, no limit
    sale = await service.create(admin, kind="percent", code="SALE50", values={"percent": 50})
    assert sale.percent == 50

    # the owner is not limited (a reason is still required for the report)
    big = await service.create(
        owner, kind="days", code="YEAR", values={"days": 3650}, reason="владелец: партнёрам"
    )
    assert big.days == 3650
    with pytest.raises(PromoError, match="причину"):
        await service.create(owner, kind="wallet", code="OWN", values={"amount_minor": 100_000_000})


async def test_the_role_is_re_read_in_the_transaction(
    db: CountingDatabase, service: PromoService, admin: Actor
) -> None:
    p = await service.create(admin, kind="percent", code="P10", values={"percent": 10})
    # revoked after the screen was drawn: the cached Actor still says «admin»
    await db.raw("update users set perms = '[]'::jsonb where id = $1", admin.user_id)
    for call in (
        service.create(admin, kind="percent", code="P20", values={"percent": 20}),
        service.update(p.id, admin, enabled=False),
        service.toggle_plan(p.id, 1, admin),
        service.delete(p.id, admin),
    ):
        with pytest.raises(PromoError, match="Нет прав"):
            await call
    support = await staff(db, 3, "support")
    with pytest.raises(PromoError, match="Нет прав"):
        await service.create(support, kind="percent", code="P30", values={"percent": 30})
    with pytest.raises(PromoError, match="Нет прав"):
        await service.create(Actor(None, "owner"), kind="percent", code="P40", values={"percent": 40})
    ban = "update users set banned_at = now(), perms = '[\"promo\"]'::jsonb where id = $1"
    await db.raw(ban, admin.user_id)
    with pytest.raises(PromoError, match="Нет прав"):
        await service.update(p.id, admin, enabled=False)
    assert (await service.get(p.id)).enabled  # type: ignore[union-attr]


async def test_owner_ids_make_an_owner(db: CountingDatabase, catalog: FakeCatalog) -> None:
    async def owners() -> list[int]:
        return [77]

    svc = PromoService(db, catalog=catalog, owner_ids=owners)
    who = Actor(await add_user(db, 77), "user", telegram_id=77)
    big = await svc.create(who, kind="wallet", code="BIG", values={"amount_minor": 10**9}, reason="владелец")
    assert big.amount_minor == 10**9


async def test_the_creator_cannot_activate_the_code(
    db: CountingDatabase, service: PromoService, admin: Actor
) -> None:
    values = {"amount_minor": 10_000}
    p = await service.create(admin, kind="wallet", code="MINE", values=values, reason="тест")
    assert admin.user_id is not None
    res = await service.activate(admin.user_id, "mine")
    assert res.outcome == "refused" and res.reason == "own" and res.text == REFUSALS["own"]
    assert await uses_of(db, p.id) == 0 and not await db.raw("select 1 from wallet_ledger")
    other = await add_user(db, 500)
    assert (await service.activate(other, "MINE")).outcome == "applied"


# ------------------------------------------------------------------------------- 2. exact discount limits


async def test_a_single_use_discount_goes_to_one_user_only(
    db: CountingDatabase, service: PromoService, catalog: FakeCatalog
) -> None:
    pid = await add_promo(db, "ONE", "percent", percent=50, max_uses=1)
    users = [await add_user(db, 600 + i, wallet=100_000) for i in range(5)]
    results = await asyncio.gather(*(service.activate(u, "ONE") for u in users))
    assert [r.outcome for r in results].count("pending") == 1
    assert {r.reason for r in results if r.outcome == "refused"} == {"exhausted"}
    assert await uses_of(db, pid) == 1
    winner = users[[r.outcome for r in results].index("pending")]
    # the holder still gets the discount at checkout although the code is «exhausted» now
    assert len(await service.checkout_discounts(winner, 1)) == 1
    # typing it again keeps the reservation (no second use, no refusal)
    again = await service.activate(winner, "one")
    assert again.outcome == "pending" and await uses_of(db, pid) == 1
    # everybody else gets nothing at checkout
    for u in users:
        if u != winner:
            assert await service.checkout_discounts(u, 1) == []


async def test_two_orders_with_one_activation_claim(
    db: CountingDatabase, service: PromoService, catalog: FakeCatalog
) -> None:
    """One activation pays for one order: the second ``pay`` gets the refusal from :meth:`claim`."""
    pid = await add_promo(db, "TWICE", "percent", percent=50, max_uses=1)
    uid = await add_user(db, 610, wallet=100_000)
    assert (await service.activate(uid, "TWICE")).outcome == "pending"
    co = checkout(db, catalog)
    a = await co.draft_plan(uid, 1, 30, discounts=await service.checkout_discounts(uid, 1))
    b = await co.draft_plan(uid, 1, 90, discounts=await service.checkout_discounts(uid, 1))
    assert a.quote.discounts and b.quote.discounts
    snap_a = (await one(db, "select snapshot from orders where id = $1", a.order_id))["snapshot"]
    snap_b = (await one(db, "select snapshot from orders where id = $1", b.order_id))["snapshot"]

    async def claim(order_id: int, snapshot: dict) -> str | None:
        async with db.tx() as conn:
            return await service.claim(conn, order_id=order_id, user_id=uid, snapshot=snapshot)

    first, second = await asyncio.gather(claim(a.order_id, snap_a), claim(b.order_id, snap_b))
    assert sorted([first, second], key=str) == sorted([None, REFUSALS["claim_gone"]], key=str)
    won = a.order_id if first is None else b.order_id
    assert await claim(won, snap_a if won == a.order_id else snap_b) is None  # idempotent
    assert await claim(won, {}) is None  # no promo in the order
    assert await uses_of(db, pid) == 1
    use = await one(db, "select order_id, effect from promo_uses where promo_id = $1", pid)
    assert use == {"order_id": won, "effect": {"claimed": True}}
    # paid and fulfilled → the claimed use becomes the checkout use
    await co.pay(won, uid)
    assert await Fulfiller(db, config=lambda: {}).fulfill(won) == "fulfilled"
    assert await service.redeem_order(won) == 1
    final = await one(db, "select order_id, source, effect from promo_uses where promo_id = $1", pid)
    assert final["order_id"] == won and final["source"] == "checkout"
    assert final["effect"]["discount_minor"] > 0 and "claimed" not in final["effect"]
    assert await service.redeem_order(won) == 0 and await uses_of(db, pid) == 1


async def test_without_claim_a_second_order_is_marked_over_the_limit(
    db: CountingDatabase, service: PromoService, catalog: FakeCatalog
) -> None:
    """Until ``pay`` calls :meth:`claim`, the second discounted order is recorded truthfully and flagged."""
    pid = await add_promo(db, "DOUBLE", "percent", percent=50, max_uses=1)
    uid = await add_user(db, 620, wallet=200_000)
    await service.activate(uid, "DOUBLE")
    co = checkout(db, catalog)
    a = await co.draft_plan(uid, 1, 30, discounts=await service.checkout_discounts(uid, 1))
    b = await co.draft_plan(uid, 1, 90, discounts=await service.checkout_discounts(uid, 1))
    fulfiller = Fulfiller(db, config=lambda: {})
    for d in (a, b):
        await co.pay(d.order_id, uid)
        await fulfiller.fulfill(d.order_id)
    assert await service.redeem_order(a.order_id) == 1
    assert await service.redeem_order(b.order_id) == 1
    rows = await db.raw("select order_id, effect from promo_uses where promo_id = $1 order by id", pid)
    assert [r["order_id"] for r in rows] == [a.order_id, b.order_id]
    assert "over_limit" not in rows[0]["effect"] and rows[1]["effect"]["over_limit"] is True
    assert await uses_of(db, pid) == 2


async def test_reservations_are_freed(
    db: CountingDatabase, service: PromoService, catalog: FakeCatalog
) -> None:
    a = await add_promo(db, "AAA", "percent", percent=10, max_uses=1)
    b = await add_promo(db, "BBB", "fixed", amount_minor=1_000, currency="RUB", max_uses=1, pending_hours=1)
    uid = await add_user(db, 630, wallet=100_000)
    other = await add_user(db, 631)
    # a newer code frees the older reservation
    await service.activate(uid, "AAA")
    assert await uses_of(db, a) == 1
    await service.activate(uid, "BBB")
    assert (await uses_of(db, a), await uses_of(db, b)) == (0, 1)
    assert (await service.activate(other, "AAA")).outcome == "pending"
    # an expired pending discount frees its use
    assert await service.sweep(at=datetime.now(UTC) + timedelta(hours=2)) == 0
    assert await uses_of(db, b) == 0 and not await db.raw("select 1 from promo_uses where promo_id = $1", b)
    # a discount switched off is dropped at checkout and frees its use
    await db.raw("update promocodes set enabled = false where id = $1", a)
    assert await service.checkout_discounts(other, 1) == []
    assert await uses_of(db, a) == 0
    # a claim of a canceled order goes back to the reservation (the pending discount still waits)
    c = await add_promo(db, "CCC", "percent", percent=10, max_uses=1)
    await service.activate(uid, "CCC")
    co = checkout(db, catalog)
    draft = await co.draft_plan(uid, 1, 30, discounts=await service.checkout_discounts(uid, 1))
    snap = (await one(db, "select snapshot from orders where id = $1", draft.order_id))["snapshot"]
    async with db.tx() as conn:
        assert await service.claim(conn, order_id=draft.order_id, user_id=uid, snapshot=snap) is None
    assert await co.cancel(draft.order_id, uid)
    await service.sweep()
    use = await one(db, "select order_id, effect from promo_uses where promo_id = $1", c)
    assert use == {"order_id": None, "effect": {"reserved": True}} and await uses_of(db, c) == 1
    assert len(await service.checkout_discounts(uid, 1)) == 1


async def test_a_reservation_that_expired_before_the_payment_is_taken_again(
    db: CountingDatabase, service: PromoService, catalog: FakeCatalog
) -> None:
    pid = await add_promo(db, "SLOW", "percent", percent=10, max_uses=5, pending_hours=1)
    uid = await add_user(db, 640, wallet=100_000)
    await service.activate(uid, "SLOW")
    co = checkout(db, catalog)
    draft = await co.draft_plan(uid, 1, 30, discounts=await service.checkout_discounts(uid, 1))
    await db.raw("update promo_pending set until = now() - interval '1 second'")
    await service.sweep()
    assert await uses_of(db, pid) == 0
    await co.pay(draft.order_id, uid)
    await Fulfiller(db, config=lambda: {}).fulfill(draft.order_id)
    assert await service.redeem_order(draft.order_id) == 1
    use = await one(db, "select order_id, effect from promo_uses where promo_id = $1", pid)
    assert use["order_id"] == draft.order_id and "over_limit" not in use["effect"]
    assert await uses_of(db, pid) == 1


# ------------------------------------------------------------------------------- 3. short promo row lock


async def test_the_promo_row_is_written_last(db: CountingDatabase, service: PromoService) -> None:
    pid = await add_promo(db, "HOT", "days", days=1)
    uid = await add_user(db, 700)
    await add_sub(db, uid)
    assert db.counter is not None
    mark = db.counter.count
    assert (await service.activate(uid, "HOT")).outcome == "applied"
    statements = [s.lower() for s in db.counter.since(mark)]
    touching = [i for i, s in enumerate(statements) if "promocodes" in s and "for update" in s]
    assert not touching, "the promo row is never locked with SELECT … FOR UPDATE"
    updates = [i for i, s in enumerate(statements) if s.lstrip().startswith("update promocodes")]
    assert updates == [len(statements) - 1], statements
    assert await uses_of(db, pid) == 1


async def test_a_locked_promo_row_does_not_block_the_work(
    db: CountingDatabase, service: PromoService
) -> None:
    """Another activation holding the promo row (its ``UPDATE … uses + 1`` = ``FOR NO KEY UPDATE`` until its
    commit): this one reads, inserts its use (the foreign key check does not conflict) and applies the
    effect, then waits only on its own final ``UPDATE``; released → applied."""
    pid = await add_promo(db, "BUSY", "wallet", amount_minor=100, currency="RUB")
    uid = await add_user(db, 710)
    async with db.tx() as holder:
        await holder.execute(text("select 1 from promocodes where id = :id for no key update"), {"id": pid})
        task = asyncio.create_task(service.activate(uid, "BUSY"))
        for _ in range(100):
            waiting = await db.raw(
                "select query from pg_stat_activity "
                "where wait_event_type = 'Lock' and query ilike 'update promocodes%'"
            )
            if waiting:
                break
            await asyncio.sleep(0.02)
        assert waiting, "the activation reached its last statement while the promo row was held"
        assert not task.done()
    res = await asyncio.wait_for(task, 5)
    assert res.outcome == "applied" and await uses_of(db, pid) == 1
