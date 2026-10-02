"""LTE tables on a real PostgreSQL: the 14 ``lte_*`` tables and the constraints the engines rely on."""

from __future__ import annotations

from collections.abc import AsyncIterator

import asyncpg
import pytest

import svbg.ext.lte.tables as lte_tables
from tests.dbkit import CountingDatabase, open_db

pytestmark = pytest.mark.pg

TABLES = {t.name for t in lte_tables.LTE_TABLES}


@pytest.fixture
async def db(pg_dsn: str) -> AsyncIterator[CountingDatabase]:
    async with open_db(pg_dsn) as database:
        if not lte_tables.REGISTERED:
            async with database.engine.begin() as conn:
                await conn.run_sync(lte_tables.create_tables)
        yield database


def test_shared_metadata_is_untouched_until_registration() -> None:
    from svbg.db.meta import metadata

    registered = {name for name in metadata.tables if name.startswith("lte_")}
    runtime = {"lte_kv", "lte_event_cursor"}  # ``svbg.ext.lte.service``: on the same metadata once imported
    assert registered - runtime == (TABLES if lte_tables.REGISTERED else set())


async def _sub(db: CountingDatabase) -> int:
    rows = await db.raw("insert into subscriptions default values returning id")
    return int(rows[0]["id"])


async def _group(db: CountingDatabase, slug: str = "lte") -> int:
    rows = await db.raw(
        "insert into lte_groups (slug, name, state, has_default, limit_default_bytes) "
        "values ($1, $2, 'active', true, 50000000000) returning id",
        slug,
        {"ru": "LTE", "en": "LTE"},
    )
    return int(rows[0]["id"])


async def _period(db: CountingDatabase, sub: int, *, state: str = "open") -> int:
    ended = "now()" if state == "closed" else "null"
    rows = await db.raw(
        "insert into lte_periods (subscription_id, anchor_at, idx, starts_at, planned_end_at, state, "
        f"ended_at) values ($1, now(), 0, now() - interval '1 day', now() + interval '1 day', $2, {ended}) "
        "returning id",
        sub,
        state,
    )
    return int(rows[0]["id"])


async def test_all_fourteen_tables_exist(db: CountingDatabase) -> None:
    rows = await db.raw(
        "select tablename from pg_tables where schemaname = 'public' and tablename like 'lte_%'"
    )
    assert {r["tablename"] for r in rows} - {"lte_kv", "lte_event_cursor"} == TABLES  # minus runtime tables
    assert len(TABLES) == 14


async def test_group_limit_states(db: CountingDatabase) -> None:
    await _group(db)
    with pytest.raises(asyncpg.CheckViolationError):  # activation without default
        await db.raw("insert into lte_groups (slug, state) values ('g2', 'active')")
    with pytest.raises(asyncpg.CheckViolationError):  # a value without its "set" flag
        await db.raw("insert into lte_groups (slug, limit_trial_bytes) values ('g3', 0)")
    with pytest.raises(asyncpg.CheckViolationError):
        await db.raw("insert into lte_groups (slug) values ('Bad Slug')")
    await db.raw("insert into lte_groups (slug, has_trial, limit_trial_bytes) values ('g4', true, null)")


async def test_one_live_period_per_subscription(db: CountingDatabase) -> None:
    sub = await _sub(db)
    await _period(db, sub, state="closed")
    await _period(db, sub)
    with pytest.raises(asyncpg.UniqueViolationError):
        await _period(db, sub, state="deferred")
    with pytest.raises(asyncpg.CheckViolationError):  # closed ⇔ ended_at
        await db.raw(
            "insert into lte_periods (subscription_id, anchor_at, idx, starts_at, planned_end_at, state) "
            "values ($1, now(), 0, now(), now() + interval '1 day', 'closed')",
            sub,
        )


async def test_one_live_block_per_subscription_and_group(db: CountingDatabase) -> None:
    sub, group = await _sub(db), await _group(db)
    period = await _period(db, sub)
    insert = (
        "insert into lte_blocks (subscription_id, group_id, period_id, reason) "
        "values ($1, $2, $3, 'quota') returning id"
    )
    first = int((await db.raw(insert, sub, group, period))[0]["id"])
    with pytest.raises(asyncpg.UniqueViolationError):
        await db.raw(insert, sub, group, period)
    await db.raw("update lte_blocks set status = 'releasing', release_reason = 'topup' where id = $1", first)
    with pytest.raises(asyncpg.UniqueViolationError):  # releasing is still live
        await db.raw(insert, sub, group, period)
    with pytest.raises(asyncpg.CheckViolationError):  # released needs released_at
        await db.raw("update lte_blocks set status = 'released' where id = $1", first)
    await db.raw("update lte_blocks set status = 'released', released_at = now() where id = $1", first)
    await db.raw(insert, sub, group, period)


async def test_counter_key_invariant_is_enforced(db: CountingDatabase) -> None:
    await db.raw(
        "insert into lte_counters (node_uuid, usage_date, panel_user_id, total, accounted, baseline, carry) "
        "values ('n', current_date, 1, 100, 60, 50, 10)"
    )
    with pytest.raises(asyncpg.CheckViolationError):
        await db.raw(
            "insert into lte_counters (node_uuid, usage_date, panel_user_id, total, accounted) "
            "values ('n', current_date, 2, 10, 20)"
        )


async def test_overrides_constraints(db: CountingDatabase) -> None:
    sub, group = await _sub(db), await _group(db)
    exempt = (
        "insert into lte_overrides (subscription_id, group_id, kind, exempt_kind, reason) "
        "values ($1, $2, 'exempt', 'launch_trial', 'snapshot')"
    )
    await db.raw(exempt, sub, None)
    with pytest.raises(asyncpg.UniqueViolationError):
        await db.raw(exempt, sub, None)
    await db.raw(exempt, sub, group)  # a group-specific exemption is a separate row
    await db.raw(
        "update lte_overrides set revoked_at = now(), revoke_reason = 'converted_to_paid' "
        "where subscription_id = $1 and group_id is null",
        sub,
    )
    await db.raw(exempt, sub, None)  # revoked rows do not count
    with pytest.raises(asyncpg.CheckViolationError):
        await db.raw("insert into lte_overrides (subscription_id, kind) values ($1, 'exempt')", sub)
    with pytest.raises(asyncpg.CheckViolationError):
        await db.raw(
            "insert into lte_overrides (subscription_id, kind, limit_bytes) values ($1, 'no_block', 5)", sub
        )
    await db.raw(
        "insert into lte_overrides (subscription_id, kind, limit_bytes, applies_to) "
        "values ($1, 'limit', null, 'paid')",
        sub,
    )


async def test_usage_rows_and_credits(db: CountingDatabase) -> None:
    sub, group = await _sub(db), await _group(db)
    period = await _period(db, sub)
    await db.raw(
        "insert into lte_period_usage (period_id, group_id, used_bytes) values ($1, $2, 10) "
        "on conflict (period_id, group_id) "
        "do update set used_bytes = lte_period_usage.used_bytes + excluded.used_bytes",
        period,
        group,
    )
    with pytest.raises(asyncpg.CheckViolationError):
        await db.raw(
            "insert into lte_credits (subscription_id, group_id, period_id, bytes, source) "
            "values ($1, $2, $3, 0, 'admin')",
            sub,
            group,
            period,
        )
    await db.raw(
        "insert into lte_credits (subscription_id, group_id, period_id, bytes, source) "
        "values ($1, $2, $3, 5, 'admin')",
        sub,
        group,
        period,
    )
    await db.raw("delete from subscriptions where id = $1", sub)
    left = await db.raw(
        "select (select count(*) from lte_periods) p, (select count(*) from lte_credits) c, "
        "(select count(*) from lte_period_usage) u"
    )
    assert tuple(left[0].values()) == (0, 0, 0)  # the subscription's LTE rows go with it


async def test_twins_and_nodes(db: CountingDatabase) -> None:
    group = await _group(db)
    await db.raw(
        "insert into lte_twins (base_squad_uuid, group_id, twin_squad_uuid) values ('b', $1, 't')", group
    )
    with pytest.raises(asyncpg.CheckViolationError):
        await db.raw(
            "insert into lte_twins (base_squad_uuid, group_id, twin_squad_uuid) values ('x', $1, 'x')", group
        )
    await db.raw(
        "insert into lte_group_nodes (group_id, node_uuid, counted_from, counted_to) "
        "values ($1, 'n1', now(), now())",
        group,
    )
    with pytest.raises(asyncpg.CheckViolationError):
        await db.raw(
            "insert into lte_group_nodes (group_id, node_uuid, counted_from) values ($1, 'N2', now())", group
        )
