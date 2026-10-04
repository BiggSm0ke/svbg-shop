"""Alembic: the migrations build exactly the schema of ``svbg.db.schema.create_schema``.

Compared on a real PostgreSQL 17: tables, columns (type, nullability, default, identity), constraints
(definition text) and indexes (definition text), extensions. The reference schema is built by the real
``create_schema``; the migration is applied by the real ``alembic upgrade``.
"""

from __future__ import annotations

import asyncio
import io
import uuid
from collections.abc import AsyncIterator

import asyncpg
import pytest
from alembic import command

from svbg.db import migrations
from svbg.db.meta import metadata
from svbg.db.schema import TABLE_MODULES, create_schema, index_ddl, index_ddl_names, load_all
from tests.e2e.conftest import apply_sql, schema_sql
from tests.pgcluster import PgCluster

pytestmark = pytest.mark.pg

HEAD = "0007_staff_roles"

_COLUMNS = """
select table_name, column_name, ordinal_position, data_type, udt_name, is_nullable, column_default,
       is_identity, identity_generation, character_maximum_length, numeric_precision
from information_schema.columns
where table_schema = 'public' and table_name <> 'alembic_version'
order by table_name, ordinal_position
"""
_CONSTRAINTS = """
select conrelid::regclass::text as tbl, conname, contype, pg_get_constraintdef(oid) as def
from pg_constraint
where connamespace = 'public'::regnamespace and conrelid::regclass::text <> 'alembic_version'
order by 1, 2
"""
_INDEXES = """
select tablename, indexname, indexdef from pg_indexes
where schemaname = 'public' and tablename <> 'alembic_version'
order by 1, 2
"""
_TABLES = """
select table_name from information_schema.tables
where table_schema = 'public' and table_type = 'BASE TABLE' order by 1
"""


async def _snapshot(dsn: str) -> dict[str, list[tuple[object, ...]]]:
    conn = await asyncpg.connect(dsn)
    try:
        out: dict[str, list[tuple[object, ...]]] = {}
        for name, sql in (("columns", _COLUMNS), ("constraints", _CONSTRAINTS), ("indexes", _INDEXES)):
            out[name] = [tuple(r.values()) for r in await conn.fetch(sql)]
        out["extensions"] = [
            tuple(r.values()) for r in await conn.fetch("select extname from pg_extension order by 1")
        ]
        return out
    finally:
        await conn.close()


async def _tables(dsn: str) -> list[str]:
    conn = await asyncpg.connect(dsn)
    try:
        return [r[0] for r in await conn.fetch(_TABLES)]
    finally:
        await conn.close()


@pytest.fixture
async def other_dsn(pg_cluster: PgCluster) -> AsyncIterator[str]:
    name = f"t_ref_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(pg_cluster.dsn("postgres"))
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()
    try:
        yield pg_cluster.dsn(name)
    finally:
        admin = await asyncpg.connect(pg_cluster.dsn("postgres"))
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            await admin.close()


async def _create_reference(dsn: str) -> None:
    await create_schema(dsn)


async def _migrate(dsn: str) -> None:
    await migrations.upgrade(dsn)


async def test_migration_equals_create_schema(pg_dsn: str, other_dsn: str) -> None:
    await _migrate(pg_dsn)
    await _create_reference(other_dsn)
    migrated, reference = await _snapshot(pg_dsn), await _snapshot(other_dsn)
    for part in ("columns", "constraints", "indexes", "extensions"):
        assert migrated[part] == reference[part], f"{part} differ between the migration and create_schema"
    assert "pg_trgm" in {e[0] for e in migrated["extensions"]}
    assert (
        len(migrated["columns"]) > 100 and len(migrated["constraints"]) > 40 and len(migrated["indexes"]) > 30
    )
    load_all()
    assert set(await _tables(pg_dsn)) == {t.name for t in metadata.sorted_tables} | {"alembic_version"}


async def test_every_table_module_is_covered() -> None:
    load_all()
    sql = schema_sql()
    for table in metadata.sorted_tables:
        assert f"CREATE TABLE {table.name} (" in sql, f"{table.name} is missing from the migrations"
    assert "svbg.content.tables" in TABLE_MODULES
    for ddl in index_ddl():
        assert ddl in sql, f"{ddl!r} is missing from the migrations"


async def test_revision_is_recorded_and_head_is_single(pg_dsn: str) -> None:
    assert await migrations.current_revision(pg_dsn) is None
    await _migrate(pg_dsn)
    assert await migrations.current_revision(pg_dsn) == migrations.head_revision() == HEAD


async def test_downgrade_removes_every_table(pg_dsn: str) -> None:
    await _migrate(pg_dsn)
    buf = io.StringIO()
    cfg = migrations.alembic_config()
    cfg.output_buffer = buf
    command.downgrade(cfg, f"{HEAD}:base", sql=True)
    await apply_sql(pg_dsn, buf.getvalue())
    assert set(await _tables(pg_dsn)) <= {"alembic_version"}


async def test_offline_sql_needs_no_database_and_holds_no_secrets() -> None:
    sql = migrations.offline_sql(dsn="postgresql://svbg:VerySecretPw9@db:5432/svbg")
    assert "VerySecretPw9" not in sql
    assert "CREATE EXTENSION IF NOT EXISTS pg_trgm" in sql
    assert sql.rstrip().endswith("COMMIT;")


async def test_online_upgrade_is_idempotent(pg_dsn: str) -> None:
    await migrations.upgrade(pg_dsn)
    await migrations.upgrade(pg_dsn)
    assert await migrations.current_revision(pg_dsn) == HEAD


async def test_stage1_upgrade_from_initial_equals_create_schema(pg_dsn: str, other_dsn: str) -> None:
    """A stage-0 database (``0001_initial``) upgraded in place gets exactly the stage-1 schema."""
    await migrations.upgrade(pg_dsn, "0001_initial")
    assert await migrations.current_revision(pg_dsn) == "0001_initial"
    assert "subscriptions" not in await _tables(pg_dsn)
    await migrations.upgrade(pg_dsn)
    await _create_reference(other_dsn)
    migrated, reference = await _snapshot(pg_dsn), await _snapshot(other_dsn)
    for part in ("columns", "constraints", "indexes", "extensions"):
        assert migrated[part] == reference[part], f"{part} differ after 0001 → head"
    stage1 = {
        "subscriptions", "subscription_events", "panel_squad_substitutions", "panel_squad_twins",
        "rw_inbox", "import_runs", "rw_sync_state", "admin_topics", "admin_cards",
    }  # fmt: skip
    assert stage1 <= set(await _tables(pg_dsn))


_STAGE34 = {
    "promocodes", "promo_uses", "promo_pending", "pages", "page_versions", "page_consents", "ad_links",
    "ad_link_users", "deeplinks", "deeplink_hits", "deeplink_daily", "broadcasts", "broadcast_msgs",
    "referral_codes", "referrals", "referral_rewards", "lte_groups", "lte_periods", "lte_kv",
    "lte_event_cursor",
    "ip_guard_blocks", "ip_guard_alerts", "ip_guard_exempt", "ip_guard_nodes", "legacy_id_map",
    "legacy_transactions",
}  # fmt: skip


async def test_stage34_table_modules_are_registered() -> None:
    from svbg.ext.lte import tables as lte_tables

    load_all()
    assert lte_tables.REGISTERED and lte_tables.lte_metadata is metadata
    assert set(metadata.tables) >= _STAGE34
    assert sum(name.startswith("lte_") for name in metadata.tables) == 16
    assert {"ix_users_username_trgm", "ix_payments_paid_at"} <= index_ddl_names()


async def test_stage34_upgrade_from_stage2_with_data(pg_dsn: str, other_dsn: str) -> None:
    """A stage-2 database with rows upgrades in place to exactly the ``create_schema`` schema."""
    await migrations.upgrade(pg_dsn, "0003_stage2")
    conn = await asyncpg.connect(pg_dsn)
    try:
        uid = await conn.fetchval("insert into users (telegram_id) values (42) returning id")
        await conn.execute(
            "insert into import_runs (source, mode, status) values ('bedolaga', 'apply', 'done')"
        )
    finally:
        await conn.close()
    await migrations.upgrade(pg_dsn)
    await _create_reference(other_dsn)
    migrated, reference = await _snapshot(pg_dsn), await _snapshot(other_dsn)
    for part in ("columns", "constraints", "indexes", "extensions"):
        assert migrated[part] == reference[part], f"{part} differ after 0003 → head"
    assert set(await _tables(pg_dsn)) >= _STAGE34
    conn = await asyncpg.connect(pg_dsn)
    try:
        assert await conn.fetchval("select notify_marketing from users where id = $1", uid) is True
        await conn.execute(
            "insert into import_runs (source, mode, status) values ('bedolaga', 'shadow', 'running')"
        )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute("insert into import_runs (source, mode, status) values ('x', 'bogus', 'done')")
    finally:
        await conn.close()


async def test_captcha_upgrade_marks_everybody_already_there(pg_dsn: str) -> None:
    """``0006_captcha``: users who were there before the captcha count as passed, newcomers do not."""
    await migrations.upgrade(pg_dsn, "0005_tickets")
    conn = await asyncpg.connect(pg_dsn)
    try:
        old = await conn.fetchval("insert into users (telegram_id) values (42) returning id")
    finally:
        await conn.close()
    await migrations.upgrade(pg_dsn)
    conn = await asyncpg.connect(pg_dsn)
    try:
        assert await conn.fetchval("select captcha_passed_at is not null from users where id = $1", old)
        new = await conn.fetchval("insert into users (telegram_id) values (43) returning id")
        assert await conn.fetchval("select captcha_passed_at from users where id = $1", new) is None
    finally:
        await conn.close()
    await asyncio.to_thread(command.downgrade, migrations.alembic_config(pg_dsn), "0005_tickets")
    columns = [c[1] for c in (await _snapshot(pg_dsn))["columns"] if c[0] == "users"]
    assert "captcha_passed_at" not in columns and "notify_marketing" in columns


async def test_stage34_downgrade_restores_stage2(pg_dsn: str, other_dsn: str) -> None:
    """``downgrade 0003_stage2`` returns exactly the stage-2 schema (shadow runs survive as ``dry_run``)."""
    await migrations.upgrade(pg_dsn)
    conn = await asyncpg.connect(pg_dsn)
    try:
        await conn.execute(
            "insert into import_runs (source, mode, status) values ('bedolaga', 'shadow', 'done')"
        )
    finally:
        await conn.close()
    await asyncio.to_thread(command.downgrade, migrations.alembic_config(pg_dsn), "0003_stage2")
    assert await migrations.current_revision(pg_dsn) == "0003_stage2"
    await migrations.upgrade(other_dsn, "0003_stage2")
    migrated, reference = await _snapshot(pg_dsn), await _snapshot(other_dsn)
    for part in ("columns", "indexes", "extensions"):
        assert migrated[part] == reference[part], f"{part} differ after head → 0003"
    # ``ck_orders_kind`` comes back NOT VALID (existing addon_lte orders would block a validated one).
    norm = [(t, n, c, d.removesuffix(" NOT VALID")) for t, n, c, d in migrated["constraints"]]
    assert norm == reference["constraints"]
    conn = await asyncpg.connect(pg_dsn)
    try:
        assert await conn.fetchval("select mode from import_runs") == "dry_run"
    finally:
        await conn.close()
    assert not _STAGE34 & set(await _tables(pg_dsn))
