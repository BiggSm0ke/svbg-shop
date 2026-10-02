"""LTE packs O7 (05 §2.1.11): one ``availability`` for every place, the ``addon_lte`` draft paid by the core
checkout, the order kind and the ``lte_pack`` item in the fulfill transaction, refunds to the wallet."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa

from svbg.billing import wallet
from svbg.billing.checkout import CheckoutService
from svbg.billing.tables import order_items, orders
from svbg.ext.lte import packs
from svbg.ext.lte.decide import EffectiveLimit
from svbg.ext.lte.packs import AddonLteKind, LtePackItem, Pack, PackFacts, availability, order_packs
from svbg.ext.lte.service import LteConfig
from svbg.subscriptions.lifecycle import SubscriptionError
from tests.dbkit import SqlCounter
from tests.ext.lte.rkit import GB, LteEnv, lte_env

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
CFG = LteConfig(enabled=True, mode="on", topup_enabled=True)
P10 = Pack(1, 10, 10_000, "RUB")
P50 = Pack(2, 50, 40_000, "RUB")


def facts(**kw: Any) -> PackFacts:
    base: dict[str, Any] = {
        "subscription_id": 1,
        "user_id": 1,
        "group_id": 1,
        "group_name": {"ru": "LTE"},
        "group_state": "active",
        "group_enforce": True,
        "panel_user_id": 7,
        "paid_until": NOW + timedelta(days=20),
        "period_id": 3,
        "period_state": "open",
        "planned_end_at": NOW + timedelta(days=5),
        "anchor_kind": "paid",
        "rights": True,
        "used_bytes": 5 * GB,
        "limit": EffectiveLimit(found=True, base=10 * GB),
        "packs": (P10, P50),
    }
    base.update(kw)
    return PackFacts(**base)


@pytest.mark.parametrize(
    ("kw", "cfg", "code"),
    [
        ({"exempt": True}, {"topup_enabled": False}, "exempt"),  # exemption is checked first
        ({}, {"topup_enabled": False}, "feature_off"),
        ({"packs": ()}, {}, "no_packages"),
        ({}, {"mode": "shadow"}, "not_enforced"),
        ({"group_enforce": False}, {}, "not_enforced"),
        ({}, {"pilot": frozenset({99})}, "not_enforced"),
        ({"period_id": None, "paid_until": None}, {}, "no_subscription"),
        ({"period_is_trial": True}, {}, "trial"),
        ({"anchor_kind": "provisional"}, {}, "trial"),
        ({"group_state": "suspended"}, {}, "group_unavailable"),
        ({"rights": False}, {}, "no_group_rights"),
        ({"limit": EffectiveLimit(found=True, base=None)}, {}, "unlimited"),
        ({"limit": EffectiveLimit(found=True, base=0)}, {}, "zero_limit"),
        ({"period_id": None}, {}, "no_period"),
        ({"period_state": "deferred"}, {}, "expiring"),
        ({"paid_until": NOW + timedelta(hours=23)}, {}, "expiring"),  # the margin rule, not "covers the end"
        ({"planned_end_at": NOW - timedelta(minutes=1)}, {}, "period_changed"),
        ({"block_reason": "manual", "block_status": "active"}, {}, "manual_block"),
        ({"frozen": True}, {}, "frozen"),
    ],
)
def test_availability_refusals_in_the_owners_order(
    kw: dict[str, Any], cfg: dict[str, Any], code: str
) -> None:
    result = availability(facts(**kw), replace(CFG, **cfg), at=NOW)
    assert not result.ok and result.code == code
    if code in ("feature_off", "no_packages", "no_period"):
        assert result.text is None  # never named: the button is simply absent
    else:
        assert result.text


def test_availability_ok_and_the_insufficient_warning() -> None:
    assert availability(facts(), CFG, at=NOW).ok
    # a subscription ending in 30 h still may buy (margin 24 h); the coverage boundary does not matter
    assert availability(facts(paid_until=NOW + timedelta(hours=30)), CFG, at=NOW).ok
    blocked = facts(used_bytes=25 * GB, block_reason="quota", block_status="active")
    weak = availability(blocked, CFG, at=NOW, pack=P10)
    assert weak.ok and weak.insufficient  # +10 GB does not return access: a warning, not a refusal
    strong = availability(blocked, CFG, at=NOW, pack=P50)
    assert strong.ok and not strong.insufficient
    gone = availability(facts(), CFG, at=NOW, pack=Pack(9, 5, 1, "RUB"))
    assert gone.code == "package_disabled"
    assert "02.10" in (availability(facts(paid_until=NOW), CFG, at=NOW).text or "")  # «закончится 02.10»


def test_order_packs_puts_the_smallest_sufficient_first() -> None:
    p25 = Pack(3, 25, 20_000, "RUB")
    f = facts(packs=(P50, P10, p25), used_bytes=22 * GB, block_reason="quota", block_status="active")
    assert order_packs(f, GB) == [(p25, True), (P50, False)]  # over by 12 GB: 10 is not enough
    over = facts(packs=(P10,), used_bytes=40 * GB, block_reason="quota", block_status="active")
    assert order_packs(over, GB) == [(P10, False)]  # none suffices: only the biggest
    assert order_packs(facts(packs=(P50, P10)), GB) == [(P10, False), (P50, False)]


# ---------------------------------------------------------------------------------------- database


async def _paid_user(env: LteEnv, tg: int, *, used: int, balance: int = 100_000) -> tuple[int, int, int]:
    sid = await env.linked_sub(tg)
    pid = await env.open_period(sid, used=used)
    uid = await env.user_of(sid)
    async with env.db.tx() as conn:
        await wallet.credit(conn, uid, balance, reason="topup", ref_type="test", ref_id=tg, currency="RUB")
    return sid, pid, uid


async def _order(env: LteEnv, order_id: int) -> dict[str, Any]:
    async with env.db.read() as conn:
        row = (await conn.execute(sa.select(orders).where(orders.c.id == order_id))).mappings().one()
    return dict(row)


async def test_addon_order_is_a_core_draft_paid_from_the_balance_and_releases_the_block(pg_dsn: str) -> None:
    async with lte_env(pg_dsn) as env:
        sid, pid, uid = await _paid_user(env, 201, used=12 * GB)
        await env.service.process_subscription(sid)
        await env.drain()
        assert await env.panel_squads(sid) == [env.twin]
        pack = await env.add_pack(10, 9_900)
        env.config["LTE_ADMIN_NOTIFY_TOPUPS"] = True
        with SqlCounter(env.db.engine) as counter:
            draft = await packs.create_order(env.service, uid, env.group_id, pack)
        assert counter.count <= 2, counter.recent
        assert draft.order_id is not None
        order = await _order(env, draft.order_id)
        assert order["kind"] == "addon_lte" and order["status"] == "draft" and order["total_minor"] == 9_900
        snap = order["snapshot"]
        assert snap["period_id"] == pid and snap["group_id"] == env.group_id and snap["bytes"] == 10 * GB

        checkout = CheckoutService(env.db, catalog=None, payments=None, config=lambda: {})  # type: ignore[arg-type]
        paid = await checkout.pay(draft.order_id, uid)
        assert paid.outcome == "paid"
        # what billing's fulfill does for a module order kind (integration: Fulfiller → host.order_kind)
        async with env.db.tx() as conn:
            row = (
                (await conn.execute(sa.select(orders).where(orders.c.id == draft.order_id).with_for_update()))
                .mappings()
                .one()
            )
            assert await AddonLteKind(env.service).apply(conn, row, []) == sid
        credits = await env.db.raw(
            "select bytes, order_id, source from lte_credits where subscription_id = $1", sid
        )
        assert [(c["bytes"], c["order_id"], c["source"]) for c in credits] == [
            (10 * GB, draft.order_id, "order")
        ]
        (block,) = await env.blocks(sid)
        assert block["status"] == "releasing" and block["release_reason"] == "topup"
        await env.drain()
        assert await env.panel_squads(sid) == [env.base]
        card = next(r for r in env.admin_chat.reports if r.title == "LTE: докупка")
        assert card.plain_text().splitlines()[1:] == [f"Подписка: №{sid}", "Добавлено: +10 ГБ"]
        # exactly once: a repeated apply does not add a second credit
        async with env.db.tx() as conn:
            await AddonLteKind(env.service).apply(conn, row, [])
        assert len(await env.db.raw("select 1 from lte_credits where subscription_id = $1", sid)) == 1


async def test_addon_order_refuses_when_the_period_changed(pg_dsn: str) -> None:
    async with lte_env(pg_dsn) as env:
        sid, pid, uid = await _paid_user(env, 202, used=12 * GB)
        pack = await env.add_pack(10, 9_900)
        draft = await packs.create_order(env.service, uid, env.group_id, pack)
        assert draft.order_id is not None
        # the reset happened meanwhile: a new period
        await env.db.raw("update lte_periods set state = 'closed', ended_at = now() where id = $1", pid)
        await env.db.raw(
            "insert into lte_periods (subscription_id, anchor_at, idx, starts_at, planned_end_at, state) "
            "values ($1, now(), 1, now(), now() + interval '30 days', 'open')",
            sid,
        )
        order = await _order(env, draft.order_id)
        with pytest.raises(SubscriptionError) as err:
            async with env.db.tx() as conn:
                await AddonLteKind(env.service).apply(conn, order, [])
        assert err.value.code == "lte_period_changed" and "баланс" in err.value.text
        assert await env.db.raw("select 1 from lte_credits") == []


async def test_draft_refusals_and_force(pg_dsn: str) -> None:
    async with lte_env(pg_dsn) as env:
        sid, _pid, uid = await _paid_user(env, 203, used=30 * GB)
        await env.service.process_subscription(sid)
        small = await env.add_pack(10, 9_900)
        hidden = await env.add_pack(100, 50_000, enabled=False)
        res = await packs.create_order(env.service, uid, env.group_id, hidden)
        assert res.order_id is None and res.refusal is not None and res.refusal.code == "package_disabled"
        res = await packs.create_order(env.service, uid, env.group_id, small)
        assert res.order_id is None and res.refusal is not None and res.refusal.insufficient  # ask first
        res = await packs.create_order(env.service, uid, env.group_id, small, force=True)
        assert res.order_id is not None  # «Всё равно купить»
        env.config["LTE_TOPUP_ENABLED"] = False
        res = await packs.create_order(env.service, uid, env.group_id, small)
        assert res.refusal is not None and res.refusal.code == "feature_off"
        other = await env.rw.add_user(9_999)
        res = await packs.create_order(env.service, other, env.group_id, small)
        assert res.refusal is not None and res.refusal.code == "no_subscription"


async def _purchase_with_pack(env: LteEnv, uid: int, sid: int | None, gb: int, amount: int) -> dict[str, Any]:
    async with env.db.tx() as conn:
        oid = (
            await conn.execute(
                sa.insert(orders)
                .values(
                    user_id=uid,
                    kind="renew",
                    status="paid",
                    currency="RUB",
                    total_minor=50_000 + amount,
                    subscription_id=sid,
                    snapshot={"title": "Тариф"},
                )
                .returning(orders.c.id)
            )
        ).scalar_one()
        await conn.execute(
            sa.insert(order_items).values(
                order_id=oid,
                position=2,
                type="lte_pack",
                payload={"group_id": env.group_id, "gb": gb, "bytes": gb * GB},
                amount_minor=amount,
            )
        )
        await wallet.debit(
            conn, uid, 50_000 + amount, reason="purchase", ref_type="order", ref_id=oid, currency="RUB"
        )
    async with env.db.read() as conn:
        order = (await conn.execute(sa.select(orders).where(orders.c.id == oid))).mappings().one()
        item = (
            (await conn.execute(sa.select(order_items).where(order_items.c.order_id == oid))).mappings().one()
        )
    return {"order": dict(order), "item": dict(item)}


async def test_pack_item_of_a_renewal_lands_in_the_live_period(pg_dsn: str) -> None:
    async with lte_env(pg_dsn) as env:
        sid, pid, uid = await _paid_user(env, 204, used=1 * GB)
        got = await _purchase_with_pack(env, uid, sid, 25, 20_000)
        async with env.db.tx() as conn:
            await LtePackItem(env.service).fulfill(conn, got["order"], got["item"])
        rows = await env.db.raw("select period_id, bytes, amount_minor from lte_credits")
        assert [(r["period_id"], r["bytes"], r["amount_minor"]) for r in rows] == [(pid, 25 * GB, 20_000)]


async def test_pack_item_without_a_period_returns_only_its_money(pg_dsn: str) -> None:
    async with lte_env(pg_dsn) as env:
        sid = await env.linked_sub(205)
        uid = await env.user_of(sid)
        async with env.db.tx() as conn:
            await wallet.credit(conn, uid, 100_000, reason="topup", ref_type="test", ref_id=1, currency="RUB")
        got = await _purchase_with_pack(env, uid, sid, 25, 20_000)
        # every term event already processed and no live period (e.g. the series is closed)
        await env.db.raw(
            "insert into lte_event_cursor (subscription_id, last_event_id) select $1, coalesce(max(id), 0) "
            "from subscription_events where subscription_id = $1",
            sid,
        )
        before = await _balance(env, uid)
        async with env.db.tx() as conn:
            await LtePackItem(env.service).fulfill(conn, got["order"], got["item"])
        assert await _balance(env, uid) == before + 20_000
        items = await env.db.raw("select status from order_items where id = $1", got["item"]["id"])
        assert items[0]["status"] == "refunded"
        async with env.db.tx() as conn:  # exactly once
            await LtePackItem(env.service).fulfill(conn, got["order"], got["item"])
        assert await _balance(env, uid) == before + 20_000


async def _balance(env: LteEnv, uid: int) -> int:
    async with env.db.read() as conn:
        return await wallet.balance(conn, uid)


async def test_offer_for_and_topup_check_follow_availability(pg_dsn: str) -> None:
    async with lte_env(pg_dsn) as env:
        sid, _pid, _uid = await _paid_user(env, 206, used=9 * GB)
        assert not await env.service.topup_available(sid, env.group_id)  # no packs yet
        await env.add_pack(10, 9_900)
        assert await env.service.topup_available(sid, env.group_id)
        offer = await packs.offer_for(env.service, sid, env.group_id)
        assert offer is not None and offer.ok and offer.facts.used_bytes == 9 * GB
        assert await packs.offer_for(env.service, sid, 999) is None


async def test_pack_item_of_a_first_purchase_lands_in_the_new_period(pg_dsn: str) -> None:
    async with lte_env(pg_dsn) as env:
        sid = await env.linked_sub(207)  # the purchase's term events are not processed yet
        uid = await env.user_of(sid)
        async with env.db.tx() as conn:
            await wallet.credit(conn, uid, 100_000, reason="topup", ref_type="test", ref_id=1, currency="RUB")
        got = await _purchase_with_pack(env, uid, sid, 10, 9_000)
        async with env.db.tx() as conn:
            await LtePackItem(env.service).fulfill(conn, got["order"], got["item"])
        periods = await env.db.raw(
            "select id from lte_periods where subscription_id = $1 and state <> 'closed'", sid
        )
        credits = await env.db.raw("select period_id from lte_credits where subscription_id = $1", sid)
        assert len(periods) == 1 and [c["period_id"] for c in credits] == [periods[0]["id"]]


async def test_attach_to_order_keeps_the_promo_discount_of_the_draft(pg_dsn: str) -> None:
    """A discount is not an order item: adding (or replacing) the pack must not re-sum the items."""
    async with lte_env(pg_dsn) as env:
        sid = await env.linked_sub(208)
        uid = await env.user_of(sid)
        async with env.db.tx() as conn:
            oid = (
                await conn.execute(
                    sa.insert(orders)
                    .values(
                        user_id=uid,
                        kind="renew",
                        status="draft",
                        currency="RUB",
                        total_minor=27_000,  # plan 30 000 − 10 % promo
                        snapshot={"promo": {"code": "TEN", "discount_minor": 3_000}},
                    )
                    .returning(orders.c.id)
                )
            ).scalar_one()
            await conn.execute(
                sa.insert(order_items).values(
                    order_id=oid, position=1, type="plan_period", payload={}, amount_minor=30_000
                )
            )
            assert await packs.attach_to_order(conn, oid, uid, P10, env.group_id, gb_bytes=GB)
        row = await _order(env, oid)
        assert row["total_minor"] == 27_000 + P10.amount_minor
        assert row["snapshot"]["promo"]["discount_minor"] == 3_000
        async with env.db.tx() as conn:  # another pack replaces the first one, the discount stays
            assert await packs.attach_to_order(conn, oid, uid, P50, env.group_id, gb_bytes=GB)
        assert (await _order(env, oid))["total_minor"] == 27_000 + P50.amount_minor
        async with env.db.tx() as conn:  # someone else's draft: refused, nothing changes
            assert not await packs.attach_to_order(conn, oid, uid + 1, P10, env.group_id, gb_bytes=GB)
        assert (await _order(env, oid))["total_minor"] == 27_000 + P50.amount_minor
