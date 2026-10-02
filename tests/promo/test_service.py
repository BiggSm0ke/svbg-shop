"""The promo engine on real PostgreSQL: every kind, limits, races, pending discounts through the real checkout
and fulfill, redemption, the owner's CRUD and the Bedolaga import."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from svbg.billing.checkout import CheckoutService
from svbg.billing.fulfill import Fulfiller
from svbg.core.bus import Event, EventBus
from svbg.promo.legacy import from_bedolaga
from svbg.promo.rules import PromoError
from svbg.promo.service import Actor, PromoService
from tests.dbkit import CountingDatabase
from tests.promo.kit import (
    FakeCatalog,
    add_paid_order,
    add_promo,
    add_sub,
    add_user,
    trial_service,
)

OWNER_TG = 1


@pytest.fixture
async def owner(db: CountingDatabase) -> Actor:
    return Actor(await add_user(db, OWNER_TG, role="owner"), "owner", telegram_id=OWNER_TG)


async def one(db: CountingDatabase, sql: str, *args: object) -> dict:
    rows = await db.raw(sql, *args)
    assert rows, sql
    return dict(rows[0])


async def wallet_of(db: CountingDatabase, user_id: int) -> int:
    return int((await one(db, "select wallet_minor from users where id = $1", user_id))["wallet_minor"])


async def uses_of(db: CountingDatabase, promo_id: int) -> int:
    return int((await one(db, "select uses from promocodes where id = $1", promo_id))["uses"])


# ------------------------------------------------------------------------------------------- immediate kinds


async def test_wallet_code_credits_once_and_is_case_insensitive(
    db: CountingDatabase, service: PromoService
) -> None:
    pid = await add_promo(db, "Gift100", "wallet", amount_minor=10_000, currency="RUB")
    uid = await add_user(db, 100)
    res = await service.activate(uid, "  gift100 ")
    assert res.outcome == "applied" and "+100 ₽ на баланс" in res.text and "Gift100" in res.text
    assert await wallet_of(db, uid) == 10_000
    ledger = await one(
        db, "select reason, ref_type, ref_id, amount_minor from wallet_ledger where user_id = $1", uid
    )
    assert ledger == {
        "reason": "bonus",
        "ref_type": "promo_use",
        "ref_id": str(res.use_id),
        "amount_minor": 10_000,
    }
    use = await one(db, "select source, effect, order_id from promo_uses where promo_id = $1", pid)
    assert use == {"source": "bot", "effect": {"wallet_minor": 10_000}, "order_id": None}
    again = await service.activate(uid, "GIFT100")
    assert again.outcome == "refused" and again.reason == "used"
    assert await wallet_of(db, uid) == 10_000 and await uses_of(db, pid) == 1


async def test_days_code_extends_the_live_subscription(db: CountingDatabase, service: PromoService) -> None:
    pid = await add_promo(db, "WEEK", "days", days=7)
    uid = await add_user(db, 101)
    no_sub = await service.activate(uid, "week")
    assert no_sub.outcome == "refused" and no_sub.reason == "no_sub" and "оформите" in no_sub.text
    assert await uses_of(db, pid) == 0
    sid = await add_sub(db, uid, days_left=10)
    before = (await one(db, "select paid_until from subscriptions where id = $1", sid))["paid_until"]
    res = await service.activate(uid, "WEEK", source="link")
    assert res.outcome == "applied" and "+7 дней к подписке" in res.text
    after = (await one(db, "select paid_until from subscriptions where id = $1", sid))["paid_until"]
    assert after - before == timedelta(days=7)
    event = await one(
        db, "select kind, source, ref_type, ref_id from subscription_events where subscription_id = $1", sid
    )
    assert event == {
        "kind": "extended",
        "source": "promo",
        "ref_type": "promo_use",
        "ref_id": str(res.use_id),
    }
    assert (await one(db, "select source from promo_uses where promo_id = $1", pid))["source"] == "link"
    assert await db.raw("select 1 from jobs where kind like 'panel.%'"), "the panel update is queued"


async def test_wallet_days_needs_a_subscription_and_gives_both(
    db: CountingDatabase, service: PromoService
) -> None:
    await add_promo(db, "COMBO", "wallet_days", amount_minor=5_000, currency="RUB", days=2)
    uid = await add_user(db, 102)
    assert (await service.activate(uid, "COMBO")).reason == "no_sub"
    assert await wallet_of(db, uid) == 0  # nothing half-applied
    await add_sub(db, uid)
    res = await service.activate(uid, "COMBO")
    assert res.outcome == "applied" and await wallet_of(db, uid) == 5_000
    effect = (await one(db, "select effect from promo_uses"))["effect"]
    assert effect["wallet_minor"] == 5_000 and effect["days"] == 2


async def test_trial_extend_variants(db: CountingDatabase, service: PromoService) -> None:
    await add_promo(db, "TRY", "trial_extend", days=5, once_per_user=False)
    fresh = await add_user(db, 103)
    res = await service.activate(fresh, "TRY")
    assert res.outcome == "applied", res.text
    sub = await one(db, "select is_trial, paid_until from subscriptions where user_id = $1", fresh)
    assert sub["is_trial"] is True
    assert timedelta(days=4, hours=23) < sub["paid_until"] - datetime.now(UTC) <= timedelta(days=5)
    assert (await one(db, "select source from trial_grants where user_id = $1", fresh))["source"] == "promo"
    # the same user again: now in a trial → +5 days
    res2 = await service.activate(fresh, "TRY")
    assert res2.outcome == "applied"
    later = await one(db, "select paid_until from subscriptions where user_id = $1", fresh)
    assert later["paid_until"] - sub["paid_until"] == timedelta(days=5)
    paid = await add_user(db, 104)
    await add_sub(db, paid, is_trial=False)
    assert (await service.activate(paid, "TRY")).reason == "has_paid_sub"
    old = await add_user(db, 105)
    await add_sub(db, old, link_state="closed")
    assert (await service.activate(old, "TRY")).reason == "trial_used"


async def test_trial_without_a_trial_service_is_refused(db: CountingDatabase, catalog: FakeCatalog) -> None:
    svc = PromoService(db, catalog=catalog)
    await add_promo(db, "TRY", "trial_extend", days=5)
    uid = await add_user(db, 106)
    res = await svc.activate(uid, "TRY")
    assert res.reason == "no_trial" and await uses_of(db, 1) == 0


async def test_plan_gift(db: CountingDatabase, service: PromoService) -> None:
    pid = await add_promo(db, "GIFT2", "plan_gift", days=30, plan_id=2, once_per_user=False)
    uid = await add_user(db, 107)
    res = await service.activate(uid, "GIFT2")
    assert res.outcome == "applied" and "тариф «Тариф 2» на 30 дней" in res.text
    sub = await one(db, "select plan_id, is_trial from subscriptions where user_id = $1", uid)
    assert sub == {"plan_id": 2, "is_trial": False}
    other = await add_user(db, 108)
    await add_sub(db, other, plan_id=1)
    assert (await service.activate(other, "GIFT2")).reason == "plan_conflict"
    missing = await add_promo(db, "GHOST", "plan_gift", days=30, plan_id=77)
    res3 = await service.activate(await add_user(db, 109), "GHOST")
    assert res3.reason == "plan_missing" and await uses_of(db, missing) == 0
    assert await uses_of(db, pid) == 1


# ------------------------------------------------------------------------------------------- limits


async def test_limits(db: CountingDatabase, service: PromoService) -> None:
    now = datetime.now(UTC)
    await add_promo(db, "OFF", "wallet", amount_minor=1, currency="RUB", enabled=False)
    await add_promo(
        db, "LATE", "wallet", amount_minor=1, currency="RUB", expires_at=now - timedelta(seconds=1)
    )
    await add_promo(db, "SOON", "wallet", amount_minor=1, currency="RUB", starts_at=now + timedelta(hours=1))
    await add_promo(db, "NEWBIE", "wallet", amount_minor=1, currency="RUB", new_users_only=True)
    await add_promo(db, "USD1", "wallet", amount_minor=1, currency="USD")
    uid = await add_user(db, 110)
    await add_paid_order(db, uid)
    got = {
        code: (await service.activate(uid, code)).reason for code in ("OFF", "LATE", "SOON", "NEWBIE", "USD1")
    }
    assert got == {
        "OFF": "inactive",
        "LATE": "expired",
        "SOON": "not_started",
        "NEWBIE": "not_new",
        "USD1": "currency",
    }
    banned = await add_user(db, 111)
    await db.raw("update users set banned_at = now() where id = $1", banned)
    await add_promo(db, "ANY", "wallet", amount_minor=1, currency="RUB")
    assert (await service.activate(banned, "ANY")).reason == "banned"
    assert not await db.raw("select 1 from promo_uses")


async def test_max_uses_is_exact_under_a_race(db: CountingDatabase, service: PromoService) -> None:
    pid = await add_promo(db, "RACE", "wallet", amount_minor=100, currency="RUB", max_uses=3)
    users = [await add_user(db, 200 + i) for i in range(8)]
    results = await asyncio.gather(*(service.activate(u, "RACE") for u in users))
    applied = [r for r in results if r.outcome == "applied"]
    assert len(applied) == 3
    assert {r.reason for r in results if r.outcome == "refused"} == {"exhausted"}
    assert await uses_of(db, pid) == 3
    assert len(await db.raw("select 1 from promo_uses where promo_id = $1", pid)) == 3


async def test_once_per_user_under_a_double_tap(db: CountingDatabase, service: PromoService) -> None:
    await add_promo(db, "TAP", "wallet", amount_minor=100, currency="RUB")
    uid = await add_user(db, 120)
    results = await asyncio.gather(*(service.activate(uid, "TAP") for _ in range(4)))
    assert [r.outcome for r in results].count("applied") == 1
    assert await wallet_of(db, uid) == 100


async def test_unknown_codes_are_rate_limited(db: CountingDatabase, catalog: FakeCatalog) -> None:
    svc = PromoService(db, catalog=catalog, max_attempts=3)
    await add_promo(db, "REAL", "wallet", amount_minor=100, currency="RUB")
    uid = await add_user(db, 121)
    for guess in ("AAA", "BBB", "not a code"):
        assert (await svc.activate(uid, guess)).reason == "not_found"
    blocked = await svc.activate(uid, "REAL")
    assert blocked.reason == "too_many" and "Слишком много попыток" in blocked.text
    other = await add_user(db, 122)
    assert (await svc.activate(other, "REAL")).outcome == "applied"  # per user


# ------------------------------------------------------------------------------------------- discounts


def checkout(db: CountingDatabase, catalog: FakeCatalog) -> CheckoutService:
    return CheckoutService(db, catalog=catalog, payments=None, config=lambda: {"CURRENCY": "RUB"})


async def test_percent_code_waits_for_checkout_and_is_redeemed_once(
    db: CountingDatabase, service: PromoService, catalog: FakeCatalog, bus: EventBus
) -> None:
    pid = await add_promo(db, "AUTUMN", "percent", percent=20, pending_hours=48)
    uid = await add_user(db, 130, wallet=100_000)
    res = await service.activate(uid, "autumn", source="link")
    assert res.outcome == "pending" and "−20 % на покупку" in res.text and "при оплате" in res.text
    entry = service.pending(uid)
    assert entry is not None and entry.code == "AUTUMN" and entry.label == "−20 % на покупку"
    assert await uses_of(db, pid) == 1  # the use is reserved when the code is typed
    reserved = await one(db, "select order_id, effect from promo_uses where promo_id = $1", pid)
    assert reserved == {"order_id": None, "effect": {"reserved": True}}
    row = await one(db, "select promo_id, source from promo_pending where user_id = $1", uid)
    assert row == {"promo_id": pid, "source": "link"}

    mark = db.queries
    discounts = await service.checkout_discounts(uid, 1)
    assert db.queries - mark == 1
    draft = await checkout(db, catalog).draft_plan(uid, 1, 30, discounts=discounts)
    assert draft.quote.subtotal_minor == 17_900 and draft.quote.total_minor == 14_320
    assert draft.quote.discounts[0].source == f"promo:{pid}"
    paid = await checkout(db, catalog).pay(draft.order_id, uid)
    assert paid.outcome == "paid" and paid.balance_minor == 100_000 - 14_320
    assert await Fulfiller(db, config=lambda: {}).fulfill(draft.order_id) == "fulfilled"

    unsubscribe = service.install(bus)
    try:
        assert await bus.publish(Event("order.fulfilled", {"order_id": draft.order_id, "user_id": uid})) == 0
    finally:
        unsubscribe()
    use = await one(db, "select order_id, source, effect from promo_uses where promo_id = $1", pid)
    assert use == {"order_id": draft.order_id, "source": "checkout", "effect": {"discount_minor": 3_580}}
    assert await uses_of(db, pid) == 1
    assert service.pending(uid) is None and not await db.raw("select 1 from promo_pending")
    assert await service.redeem_order(draft.order_id) == 0  # idempotent
    assert await uses_of(db, pid) == 1
    # once per user: the code cannot be activated again
    assert (await service.activate(uid, "AUTUMN")).reason == "used"
    stats = await service.stats(pid)
    assert (stats.uses, stats.users, stats.uses_7d, stats.discount_minor, stats.pending) == (
        1,
        1,
        1,
        3_580,
        0,
    )


async def test_checkout_without_a_pending_discount_costs_no_sql(
    db: CountingDatabase, service: PromoService
) -> None:
    uid = await add_user(db, 131)
    mark = db.queries
    assert await service.checkout_discounts(uid, 1) == []
    assert db.queries == mark


async def test_discount_plan_and_minimum_rules(
    db: CountingDatabase, service: PromoService, catalog: FakeCatalog
) -> None:
    await add_promo(db, "ONLY2", "percent", percent=10, plan_ids=[2])
    await add_promo(db, "BIG", "fixed", amount_minor=5_000, currency="RUB", min_amount_minor=20_000)
    uid = await add_user(db, 132)
    assert (await service.activate(uid, "ONLY2")).outcome == "pending"
    assert await service.checkout_discounts(uid, 1) == []
    assert service.pending(uid) is not None  # kept: another plan may still use it
    assert len(await service.checkout_discounts(uid, 2)) == 1
    # a newer discount replaces the waiting one
    assert (await service.activate(uid, "BIG")).outcome == "pending"
    assert service.pending(uid).code == "BIG"  # type: ignore[union-attr]
    co = checkout(db, catalog)
    small = await co.draft_plan(uid, 1, 30, discounts=await service.checkout_discounts(uid, 1))
    assert small.quote.total_minor == 17_900  # below the minimum
    large = await co.draft_plan(uid, 1, 90, discounts=await service.checkout_discounts(uid, 1))
    assert large.quote.total_minor == 49_900 - 5_000


async def test_a_dead_pending_discount_is_dropped(db: CountingDatabase, service: PromoService) -> None:
    pid = await add_promo(db, "GONE", "percent", percent=10)
    uid = await add_user(db, 133)
    assert (await service.activate(uid, "GONE")).outcome == "pending"
    await db.raw("update promocodes set enabled = false where id = $1", pid)
    assert await service.checkout_discounts(uid, 1) == []
    assert service.pending(uid) is None and not await db.raw("select 1 from promo_pending")


async def test_pending_index_survives_a_restart_and_the_sweep(
    db: CountingDatabase, service: PromoService, catalog: FakeCatalog
) -> None:
    await add_promo(db, "KEEP", "percent", percent=15)
    await add_promo(db, "SHORT", "percent", percent=15, pending_hours=1)
    a, b = await add_user(db, 140), await add_user(db, 141)
    await service.activate(a, "KEEP")
    await service.activate(b, "SHORT")
    fresh = PromoService(db, catalog=catalog)
    assert await fresh.load() == 2
    assert fresh.pending(a).code == "KEEP"  # type: ignore[union-attr]
    later = datetime.now(UTC) + timedelta(hours=2)
    assert await fresh.sweep(at=later) == 0
    assert [r["user_id"] for r in await db.raw("select user_id from promo_pending")] == [a]


async def test_sweep_redeems_a_missed_order(
    db: CountingDatabase, service: PromoService, catalog: FakeCatalog
) -> None:
    pid = await add_promo(db, "MISS", "percent", percent=50)
    uid = await add_user(db, 142, wallet=100_000)
    await service.activate(uid, "MISS")
    co = checkout(db, catalog)
    draft = await co.draft_plan(uid, 1, 30, discounts=await service.checkout_discounts(uid, 1))
    await co.pay(draft.order_id, uid)
    await Fulfiller(db, config=lambda: {}).fulfill(draft.order_id)
    assert await service.sweep() == 1
    assert await uses_of(db, pid) == 1
    assert await service.sweep() == 0


async def test_redeem_ignores_orders_without_promos_and_unfulfilled(
    db: CountingDatabase, service: PromoService
) -> None:
    uid = await add_user(db, 143)
    oid = await add_paid_order(db, uid)
    assert await service.redeem_order(oid) == 0
    assert await service.redeem_order(999_999) == 0
    rows = await db.raw(
        "insert into orders (user_id, kind, status, currency, total_minor, snapshot) values "
        "($1, 'new', 'paid', 'RUB', 1, '{\"discounts\": [{\"source\": \"promo:1\"}]}'::jsonb) returning id",
        uid,
    )
    assert await service.redeem_order(int(rows[0]["id"])) == 0


# ------------------------------------------------------------------------------------------- owner CRUD


async def test_create_update_delete_with_audit(
    db: CountingDatabase, service: PromoService, owner: Actor
) -> None:
    p = await service.create(owner, kind="percent", code="Spring", values={"percent": 25})
    assert p.code == "Spring" and p.percent == 25 and p.pending_hours == 72 and p.enabled
    with pytest.raises(PromoError, match="уже есть"):
        await service.create(owner, kind="wallet", code="SPRING", values={"amount_minor": 100}, reason="тест")
    gen = await service.create(owner, kind="wallet", code=None, values={"amount_minor": 100}, reason="тест")
    assert len(gen.code) == 8 and gen.currency == "RUB"
    with pytest.raises(PromoError):
        await service.create(owner, kind="days", code="BAD CODE", values={"days": 1}, reason="тест")
    with pytest.raises(PromoError, match="Неизвестные"):
        await service.create(
            owner, kind="days", code="X12", values={"days": 1}, limits={"uses": 5}, reason="тест"
        )

    upd = await service.update(p.id, owner, percent=30, max_uses=10, title="  осень  ")
    assert upd.percent == 30 and upd.max_uses == 10 and upd.title == "осень" and upd.version == 2
    with pytest.raises(PromoError, match="Скидка"):
        await service.update(p.id, owner, percent=0)
    with pytest.raises(PromoError, match="изменили"):
        await service.update(p.id, owner, expected_version=1, enabled=False)
    renamed = await service.update(p.id, owner, expected_version=2, code="Spring2")
    assert renamed.code == "Spring2"
    toggled = await service.toggle_plan(p.id, 2, owner)
    assert toggled.plan_ids == (2,)
    assert (await service.toggle_plan(p.id, 2, owner)).plan_ids == ()

    uid = await add_user(db, 150)
    await service.activate(uid, gen.code)
    with pytest.raises(PromoError, match="нельзя переименовать"):
        await service.update(gen.id, owner, code="NEWNAME")
    with pytest.raises(PromoError, match="только выключить"):
        await service.delete(gen.id, owner)
    await service.delete(p.id, owner)
    assert await service.get(p.id) is None
    actions = [r["action"] for r in await db.raw("select action from admin_audit order by id")]
    assert actions == [
        "promo.create",
        "promo.create",
        "promo.update",
        "promo.update",
        "promo.update",
        "promo.update",
        "promo.delete",
    ]
    page, total = await service.page(0, 10)
    assert total == 1 and [x.id for x in page] == [gen.id]
    assert (await service.find(gen.code.lower())) is not None and await service.find("a b") is None


async def test_import_legacy_and_recount(db: CountingDatabase, service: PromoService) -> None:
    legacy = from_bedolaga(
        {"id": 7, "code": "OldBedo", "type": "discount", "balance_bonus_kopeks": 15, "subscription_days": 24}
    )
    async with db.tx() as conn:
        pid = await service.import_legacy(conn, legacy)
        again = await service.import_legacy(conn, legacy)
    assert pid == again
    row = await one(
        db, "select kind, percent, pending_hours, source, legacy_id from promocodes where id = $1", pid
    )
    assert row == {
        "kind": "percent",
        "percent": 15,
        "pending_hours": 24,
        "source": "import",
        "legacy_id": "7",
    }
    uid = await add_user(db, 160)
    await db.raw("insert into promo_uses (promo_id, user_id, source) values ($1, $2, 'import')", pid, uid)
    async with db.tx() as conn:
        await service.recount_uses(conn)
    assert await uses_of(db, pid) == 1
    assert (await service.activate(uid, "oldbedo")).reason == "used"  # an imported use blocks a repeat


async def test_trial_service_kit_is_real(db: CountingDatabase, catalog: FakeCatalog) -> None:
    uid = await add_user(db, 170)
    assert (await trial_service(db, catalog).check(uid)).ok
