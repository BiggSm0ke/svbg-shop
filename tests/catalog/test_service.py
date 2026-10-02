"""Catalog snapshot: loading, hot path without SQL, sales rules on real rows, NOTIFY reload."""

from __future__ import annotations

import asyncio
import json

from svbg.catalog.service import CatalogService, count_plan_subscribers
from svbg.tg.ui.context import UserCtx
from tests.catalog.kit import SQ_DE, SQ_FI, SQ_NL, add_location, add_plan, add_sub
from tests.dbkit import CountingDatabase

NEWBIE = UserCtx(1, has_paid=False)
PAYER = UserCtx(2, has_paid=True)


async def test_empty_catalog(catalog: CatalogService) -> None:
    snap = catalog.snapshot
    assert snap.is_empty and snap.trial is None and snap.locations == ()
    assert snap.for_sale(NEWBIE, currency="RUB") == ()
    assert snap.plan(1) is None and snap.by_code("x") is None and snap.plan(None) is None


async def test_snapshot_reads_plans_prices_locations(db: CountingDatabase, catalog: CatalogService) -> None:
    await add_location(db, SQ_DE, "DE-1", sort=20, flag="🇩🇪")
    await add_location(db, SQ_NL, "NL-1", sort=10)
    await add_location(db, SQ_FI, "FI-1", sort=30, missing=True)
    std = await add_plan(db, "std", sort=20, prices=((90, 49900), (30, 17900)))
    pro = await add_plan(db, "pro", name="Про", sort=10, squads=(SQ_NL, SQ_DE), availability="existing")
    trial = await add_plan(db, "trial", is_trial=True, prices=())
    snap = await catalog.reload()
    assert [p.code for p in snap.plans] == ["trial", "pro", "std"]  # by (sort, id)
    assert snap.plan(std) is snap.by_code("std")
    assert [(p.days, p.amount_minor) for p in snap.plan(std).prices] == [(30, 17900), (90, 49900)]  # type: ignore[union-attr]
    assert snap.trial is not None and snap.trial.id == trial
    assert [loc.squad_uuid for loc in snap.locations] == [SQ_NL, SQ_DE, SQ_FI]
    assert [loc.squad_uuid for loc in snap.present_locations()] == [SQ_NL, SQ_DE]
    assert snap.squad_labels([SQ_DE, "unknown-squad"]) == ["🇩🇪 DE-1", "unknown-"]
    assert [p.id for p in snap.for_sale(NEWBIE, currency="RUB")] == [std]
    assert [p.id for p in snap.for_sale(PAYER, currency="RUB")] == [pro, std]
    assert snap.purchasable(pro, NEWBIE, currency="RUB") is None
    assert snap.purchasable(pro, PAYER, currency="RUB") is not None
    assert snap.purchasable(trial, PAYER, currency="RUB") is None
    assert snap.problems == ()


async def test_renewal_ignores_availability_and_hidden_flag(
    db: CountingDatabase, catalog: CatalogService
) -> None:
    hidden = await add_plan(db, "old", enabled=False, availability="new")
    broken = await add_plan(db, "brk", broken_reason="В панели нет сквадов: X")
    trial = await add_plan(db, "trial", is_trial=True)
    noprice = await add_plan(db, "free", prices=())
    snap = await catalog.reload()
    assert snap.renewable(hidden, currency="RUB") is not None  # withdrawn from new sales only
    assert snap.purchasable(hidden, NEWBIE, currency="RUB") is None
    assert snap.renewable(broken, currency="RUB") is None
    assert snap.renewable(trial, currency="RUB") is None
    assert snap.renewable(noprice, currency="RUB") is None
    assert snap.renewable(None, currency="RUB") is None


async def test_hot_path_costs_no_sql(db: CountingDatabase, catalog: CatalogService) -> None:
    await add_location(db, SQ_NL, "NL")
    for i in range(20):
        await add_plan(db, f"p{i}", availability=("all", "new", "existing", "link")[i % 4])
    await add_plan(db, "trial", is_trial=True)
    await catalog.reload()
    before = db.queries
    snap = catalog.snapshot
    for _ in range(200):
        plans = snap.for_sale(PAYER, currency="RUB", link_code="p3")
        assert plans
        assert snap.purchasable(plans[0].id, PAYER, currency="RUB") is not None
        assert snap.renewable(plans[-1].id, currency="RUB") is not None
        assert snap.trial is not None
        snap.squad_labels(plans[0].squads)
    assert db.queries == before


async def test_reload_costs_three_selects_and_swaps_atomically(
    db: CountingDatabase, catalog: CatalogService
) -> None:
    await add_plan(db, "std")
    old = catalog.snapshot
    before = db.queries
    new = await catalog.reload()
    assert db.queries - before == 3
    assert new.version > old.version and old.is_empty and not new.is_empty  # the old object never changes


async def test_bad_rows_are_reported_not_fatal(db: CountingDatabase, catalog: CatalogService) -> None:
    good = await add_plan(db, "good")
    bad = await add_plan(db, "bad", enabled=False, squads=())
    await db.raw("update plans set squads = $1::jsonb where id = $2", json.dumps(["ok", 5]), bad)
    snap = await catalog.reload()
    assert snap.plan(good) is not None and snap.plan(bad) is None
    assert len(snap.problems) == 1 and f"#{bad}" in snap.problems[0]


async def test_device_addon_currency_defaults_to_shop_currency(db: CountingDatabase) -> None:
    pid = await add_plan(db, "std", device_limit=5, device_addon=json.dumps({"price_minor": 300}))
    service = CatalogService(db, currency="USD")
    snap = await service.reload()
    addon = snap.plan(pid).addon  # type: ignore[union-attr]
    assert addon is not None and addon.currency == "USD" and addon.price_minor == 300


async def test_count_plan_subscribers(db: CountingDatabase) -> None:
    pid = await add_plan(db, "std")
    other = await add_plan(db, "other")
    await add_sub(db, pid)
    await add_sub(db, pid, link_state="pending")
    await add_sub(db, pid, manual=True)
    await add_sub(db, pid, link_state="closed")
    await add_sub(db, other)
    async with db.read() as conn:
        assert await count_plan_subscribers(conn, pid) == (3, 1)
        assert await count_plan_subscribers(conn, 999) == (0, 0)


async def test_notify_reloads_other_processes(db: CountingDatabase) -> None:
    a, b = CatalogService(db), CatalogService(db)
    await a.load()
    await b.load()
    await b.listen()
    await add_plan(db, "std")
    await a.changed()
    for _ in range(100):
        if not b.snapshot.is_empty:
            break
        await asyncio.sleep(0.05)
    assert b.snapshot.by_code("std") is not None
