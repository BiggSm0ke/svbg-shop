"""Catalog writes: validation, compare-and-set, audit, database constraints, the owner's preset."""

from __future__ import annotations

import asyncpg
import pytest

from svbg.catalog import repo
from svbg.catalog.model import CatalogError, DeviceAddon
from svbg.catalog.preset import seed_owner_preset
from svbg.catalog.service import CatalogService
from svbg.tg.ui.context import UserCtx
from tests.catalog.kit import SQ_DE, SQ_NL, add_location, add_plan
from tests.dbkit import CountingDatabase

ACTOR = repo.Actor(42, "admin")


async def audit_rows(db: CountingDatabase) -> list[dict[str, object]]:
    return [dict(r) for r in await db.raw("select * from admin_audit order by id")]


async def test_create_plan_code_sort_audit(db: CountingDatabase, catalog: CatalogService) -> None:
    async with db.tx() as conn:
        a = await repo.create_plan(conn, name="Стандарт", actor=ACTOR)
        b = await repo.create_plan(conn, name="Стандарт", actor=ACTOR)
        c = await repo.create_plan(conn, name="!!!", code=None)
    snap = await catalog.reload()
    assert [snap.plan(i).code for i in (a, b, c)] == ["standart", "standart_2", "plan"]  # type: ignore[union-attr]
    plan_a = snap.plan(a)
    assert plan_a is not None and not plan_a.enabled and plan_a.squads == () and plan_a.version == 1
    assert snap.plan(b).sort > plan_a.sort  # type: ignore[union-attr]
    rows = await audit_rows(db)
    assert [(r["action"], r["target"], r["actor_id"]) for r in rows] == [
        ("plan.create", f"plan:{a}", 42),
        ("plan.create", f"plan:{b}", 42),
    ]


async def test_create_rejects_bad_input(db: CountingDatabase) -> None:
    async with db.tx() as conn:
        with pytest.raises(CatalogError):
            await repo.create_plan(conn, name="  ")
        with pytest.raises(CatalogError):
            await repo.create_plan(conn, name="X", code="Bad Code")
        with pytest.raises(CatalogError, match="без сквадов"):
            await repo.create_plan(conn, name="X", enabled=True)
        with pytest.raises(ValueError, match="not editable"):
            await repo.create_plan(conn, name="X", code_extra=1)


async def test_update_plan_cas_and_merge(db: CountingDatabase, catalog: CatalogService) -> None:
    pid = await add_plan(db, "std")
    await db.raw(
        """update plans set name = '{"ru": "Стандарт", "en": "Standard"}'::jsonb where id = $1""", pid
    )
    async with db.tx() as conn:
        v2 = await repo.update_plan(conn, pid, expected_version=1, name={"ru": "Базовый"}, actor=ACTOR)
        assert v2 == 2
        with pytest.raises(repo.StalePlanError):
            await repo.update_plan(conn, pid, expected_version=1, traffic_bytes=1)
        v3 = await repo.update_plan(conn, pid, device_limit=None, panel_tag="paid")
        assert v3 == 3
    plan = (await catalog.reload()).plan(pid)
    assert plan is not None and dict(plan.name) == {"ru": "Базовый", "en": "Standard"}
    assert plan.panel_tag == "PAID" and plan.device_limit is None and plan.version == 3
    rows = await audit_rows(db)
    assert rows[-1]["details"] == {"name": {"ru": "Базовый"}}
    with pytest.raises(repo.StalePlanError):
        async with db.tx() as conn:
            await repo.update_plan(conn, 999_999, traffic_bytes=0)


async def test_update_plan_rules(db: CountingDatabase) -> None:
    empty = await add_plan(db, "empty", enabled=False, squads=())
    trial = await add_plan(db, "trial", is_trial=True)
    other = await add_plan(db, "other")
    cases: list[tuple[int, dict[str, object], str]] = [
        (empty, {"enabled": True}, "сквады"),
        (other, {"squads": []}, "хотя бы один"),
        (other, {"is_trial": True}, "Пробный тариф уже есть"),
        (other, {"availability": "friends"}, "доступность"),
        (other, {"reset_strategy": "YEAR"}, "стратегия"),
        (other, {"device_limit": -1}, "Устройств"),
        (other, {"traffic_bytes": True}, "трафика"),
        (other, {"panel_tag": "no spaces"}, "Тег"),
        (other, {"device_addon": {"price_minor": 1}}, "доплата"),
        (other, {"enabled": "yes"}, "да или нет"),
    ]
    for pid, change, message in cases:
        with pytest.raises(CatalogError, match=message):
            async with db.tx() as conn:
                await repo.update_plan(conn, pid, **change)
    async with db.tx() as conn:  # moving the trial flag works once the old one is cleared
        await repo.update_plan(conn, trial, is_trial=False)
        await repo.update_plan(conn, other, is_trial=True)
        await repo.update_plan(conn, other, device_addon=DeviceAddon(1900, 30, 15))
        await repo.update_plan(conn, other, device_addon=None)


async def test_prices_upsert_delete_highlight(db: CountingDatabase, catalog: CatalogService) -> None:
    pid = await add_plan(db, "std", prices=())
    async with db.tx() as conn:
        await repo.set_price(conn, pid, days=30, amount_minor=17900, currency="RUB", actor=ACTOR)
        await repo.set_price(conn, pid, days=90, amount_minor=49900, currency="RUB")
        await repo.set_price(conn, pid, days=30, amount_minor=19900, currency="RUB")  # upsert
        assert await repo.toggle_highlight(conn, pid, days=30, currency="RUB") is True
        assert await repo.toggle_highlight(conn, pid, days=90, currency="RUB") is True  # moves the star
        assert await repo.toggle_highlight(conn, pid, days=7, currency="RUB") is None
        assert await repo.delete_price(conn, pid, days=7, currency="RUB") is False
    plan = (await catalog.reload()).plan(pid)
    assert plan is not None
    assert [(p.days, p.amount_minor, p.highlight) for p in plan.prices] == [
        (30, 19900, False),
        (90, 49900, True),
    ]
    assert plan.version == 1 + 5  # 3 prices + 2 highlight changes
    async with db.tx() as conn:
        assert await repo.delete_price(conn, pid, days=30, currency="RUB", actor=ACTOR) is True
        for days, amount in ((0, 100), (3651, 100), (30, 0), (30, -5)):
            with pytest.raises(CatalogError):
                await repo.set_price(conn, pid, days=days, amount_minor=amount, currency="RUB")
        with pytest.raises(repo.StalePlanError):
            await repo.set_price(conn, 999_999, days=30, amount_minor=100, currency="RUB")
    assert [r["action"] for r in await audit_rows(db)] == ["plan.price", "plan.price_delete"]


async def test_location_edits(db: CountingDatabase, catalog: CatalogService) -> None:
    await add_location(db, SQ_NL, "NL-1")
    async with db.tx() as conn:
        assert await repo.set_location(conn, SQ_NL, title="Нидерланды", flag="🇳🇱", actor=ACTOR)
        assert await repo.set_location(conn, SQ_NL, title="Netherlands", lang="en")
        assert not await repo.set_location(conn, SQ_DE, title="X")
        with pytest.raises(CatalogError):
            await repo.set_location(conn, SQ_NL, flag="two words")
        with pytest.raises(ValueError, match="nothing"):
            await repo.set_location(conn, SQ_NL)
    loc = (await catalog.reload()).location(SQ_NL)
    assert loc is not None and loc.label("ru") == "🇳🇱 Нидерланды" and loc.label("en") == "🇳🇱 Нидерланды"
    async with db.tx() as conn:
        await repo.set_location(conn, SQ_NL, clear_flag=True, sort=5)
    loc = (await catalog.reload()).location(SQ_NL)
    assert loc is not None and loc.flag is None and loc.sort == 5


@pytest.mark.parametrize(
    "sql",
    [
        "insert into plans (code, enabled) values ('x', true)",  # on sale without squads
        "insert into plans (code) values ('Bad Code')",
        "insert into plans (code, availability) values ('x', 'friends')",
        "insert into plans (code, panel_tag) values ('x', 'lower')",
        "insert into plans (code, device_limit) values ('x', -1)",
        "insert into plans (code, is_trial) values ('a', true), ('b', true)",
    ],
    ids=["enabled-no-squads", "code", "availability", "tag", "devices", "two-trials"],
)
async def test_database_constraints(db: CountingDatabase, sql: str) -> None:
    with pytest.raises(asyncpg.IntegrityConstraintViolationError):
        await db.raw(sql)


async def test_price_constraints(db: CountingDatabase) -> None:
    pid = await add_plan(db, "std")
    for sql in (
        "insert into plan_prices (plan_id, days, currency, amount_minor) values ($1, 30, 'RUB', 1)",  # dup
        "insert into plan_prices (plan_id, days, currency, amount_minor) values ($1, 45, 'RUB', 0)",
        "insert into plan_prices (plan_id, days, currency, amount_minor) values ($1, 0, 'RUB', 10)",
        "insert into plan_prices (plan_id, days, currency, amount_minor) values ($1, 45, 'rub', 10)",
    ):
        with pytest.raises(asyncpg.IntegrityConstraintViolationError):
            await db.raw(sql, pid)
    await db.raw("delete from plans where id = $1", pid)
    assert await db.raw("select * from plan_prices") == []  # cascade


# ------------------------------------------------------------------------------------------- preset


async def test_owner_preset(db: CountingDatabase, catalog: CatalogService) -> None:
    await add_location(db, SQ_NL, "NL", sort=1)
    await add_location(db, SQ_DE, "DE", sort=2)
    await add_location(db, "gone-squad", "OLD", missing=True)
    async with db.tx() as conn:
        result = await seed_owner_preset(conn, actor=ACTOR)
    assert result.created and result.enabled and result.trial_id is not None
    snap = await catalog.reload()
    std, trial = snap.plan(result.plan_id), snap.trial
    assert std is not None and trial is not None and trial.id == result.trial_id
    assert [(p.days, p.amount_minor) for p in std.prices] == [
        (30, 17900),
        (90, 49900),
        (180, 89900),
        (360, 169900),
    ]
    assert std.device_limit == 5 and std.traffic_bytes == 0 and std.reset_strategy == "NO_RESET"
    assert std.addon == DeviceAddon(1900, 30, 15, "RUB")
    assert std.squads == (SQ_NL, SQ_DE) and std.panel_tag == "PAID" and std.availability == "all"
    assert trial.device_limit == 5 and trial.traffic_bytes == 0 and trial.squads == (SQ_NL, SQ_DE)
    assert [p.code for p in snap.for_sale(UserCtx(1), currency="RUB")] == ["standard"]
    assert std.addon.price_for(2, 90) == 11400
    async with db.tx() as conn:
        again = await seed_owner_preset(conn)
    assert not again.created and again.plan_id == result.plan_id and again.trial_id == result.trial_id
    assert len((await catalog.reload()).plans) == 2
    assert [r["action"] for r in await audit_rows(db)] == ["plan.preset"]


async def test_owner_preset_without_squads_is_hidden(db: CountingDatabase, catalog: CatalogService) -> None:
    async with db.tx() as conn:
        result = await seed_owner_preset(conn, with_trial=False)
    assert result.created and not result.enabled and result.trial_id is None
    snap = await catalog.reload()
    assert snap.for_sale(UserCtx(1), currency="RUB") == ()
    assert snap.plan(result.plan_id).squads == ()  # type: ignore[union-attr]


async def test_owner_preset_keeps_an_existing_trial(db: CountingDatabase, catalog: CatalogService) -> None:
    mine = await add_plan(db, "mytrial", is_trial=True)
    async with db.tx() as conn:
        result = await seed_owner_preset(conn, squads=[SQ_NL])
    assert result.trial_id is None
    assert (await catalog.reload()).trial.id == mine  # type: ignore[union-attr]
