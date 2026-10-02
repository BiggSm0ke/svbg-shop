"""Settings import against real PostgreSQL: a synthetic Bedolaga source built from the owner's schema dump
(``.tools/bedolaga/db-schema.sql``, structure only) and a fresh SvBG install (``create_schema``)."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import asyncpg
import pytest
import sqlalchemy as sa

from svbg.catalog.repo import create_plan, set_price
from svbg.catalog.tables import locations, plan_prices, plans
from svbg.core.component import ComponentRegistry
from svbg.core.crypto import Crypto, generate_key
from svbg.core.settings.registry import Registry, core_registry
from svbg.core.settings.service import SettingsService
from svbg.ext.ip_guard.config import SETTINGS as IP_GUARD_SETTINGS
from svbg.ext.lte.service import SECTION as LTE_SECTION
from svbg.ext.lte.service import SETTINGS as LTE_SETTINGS
from svbg.importers.bedolaga.settings_map import (
    BedolagaSettingsSource,
    CatalogImport,
    apply_catalog,
    apply_cdn_nodes,
    apply_settings,
    apply_topics,
    build_plan,
    diff_against,
    read_source_tables,
)
from svbg.ops.settings import OPS_SETTINGS
from svbg.referral.config import SETTINGS as REFERRAL_SETTINGS
from tests.dbkit import CountingDatabase, open_db
from tests.importers.test_settings_map import OWNER_CHANNELS, OWNER_ENV, OWNER_SYSTEM, OWNER_WLQ
from tests.pgcluster import PgCluster

pytestmark = pytest.mark.pg

SCHEMA_DUMP = Path(__file__).resolve().parents[2] / ".tools" / "bedolaga" / "db-schema.sql"
SQUADS = ("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222")


def bedolaga_ddl() -> str:
    """The production schema dump without roles, ownership, grants and psql meta-commands."""
    out: list[str] = []
    for line in SCHEMA_DUMP.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("\\"):
            continue
        if re.match(r"^(GRANT|REVOKE|ALTER DEFAULT PRIVILEGES)\b", stripped):
            continue
        if stripped.startswith("ALTER ") and " OWNER TO " in stripped:
            continue
        if re.match(r"^(CREATE|COMMENT ON) EXTENSION\b", stripped):
            continue
        out.append(line)
    return "\n".join(out)


@pytest.fixture(scope="module")
async def source_dsn(pg_cluster: PgCluster) -> AsyncIterator[str]:
    name = f"bedolaga_{uuid.uuid4().hex[:8]}"
    admin = await asyncpg.connect(pg_cluster.dsn("postgres"))
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()
    dsn = pg_cluster.dsn(name)
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(bedolaga_ddl())
        await conn.execute("SET search_path = public")
        for key, value in OWNER_SYSTEM.items():
            await conn.execute(
                "INSERT INTO public.system_settings (key, value, description, created_at, updated_at)"
                " VALUES ($1, $2, 'synthetic', now(), now())",
                key,
                value,
            )
        for key, value in OWNER_WLQ.items():
            await conn.execute(
                "INSERT INTO public.wlq_settings (key, value, updated_by)"
                " VALUES ($1, $2::jsonb, 'wlq-admin')",
                key,
                json.dumps(value),
            )
        for row in OWNER_CHANNELS:
            await conn.execute(
                "INSERT INTO public.required_channels (id, channel_id, channel_link, title, is_active,"
                " sort_order, created_at, disable_trial_on_leave, disable_paid_on_leave)"
                " VALUES ($1, $2, $3, $4, $5, $6, now(), $7, $8)",
                row["id"],
                row["channel_id"],
                row["channel_link"],
                row["title"],
                row["is_active"],
                row["sort_order"],
                row["disable_trial_on_leave"],
                row["disable_paid_on_leave"],
            )
    finally:
        await conn.close()
    yield dsn
    admin = await asyncpg.connect(pg_cluster.dsn("postgres"))
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        await admin.close()


@pytest.fixture
async def db(pg_dsn: str) -> AsyncIterator[CountingDatabase]:
    async with open_db(pg_dsn) as database:
        async with database.tx() as conn:
            await conn.execute(
                sa.insert(locations),
                [{"squad_uuid": s, "title": {"ru": f"S{i}"}, "sort": i} for i, s in enumerate(SQUADS)],
            )
        yield database


def full_registry() -> Registry:
    """The core registry plus the module settings the integration registers (ops, referral, IP Guard, LTE)."""
    reg = core_registry()
    for defn in (*OPS_SETTINGS, *REFERRAL_SETTINGS):
        reg.add(defn)
    reg.add_section("ip_guard", "IP Guard")
    for defn in IP_GUARD_SETTINGS:
        reg.add(defn)
    reg.add_section(*LTE_SECTION)
    for defn in LTE_SETTINGS:
        reg.add(defn)
    return reg


async def make_service(db: CountingDatabase, tmp_path: Path, **kw: Any) -> SettingsService:
    service = SettingsService(
        db,
        kw.pop("registry", None) or full_registry(),
        Crypto([generate_key()]),
        ComponentRegistry(),
        environ=kw.pop("environ", {}),
        env_path=tmp_path / ".env",
    )
    await service.load()
    return service


async def read_source(dsn: str) -> BedolagaSettingsSource:
    conn = await asyncpg.connect(dsn)
    try:
        async with conn.transaction(readonly=True):  # the source is only read (06 §0 п.2)
            tables = await read_source_tables(conn)
    finally:
        await conn.close()
    return BedolagaSettingsSource.from_env_text(OWNER_ENV, **tables)


# ------------------------------------------------------------------------------------------ source


async def test_read_source_tables_from_real_schema(source_dsn: str) -> None:
    src = await read_source(source_dsn)
    assert src.system_settings == OWNER_SYSTEM
    assert src.wlq_settings == OWNER_WLQ
    assert [r["channel_id"] for r in src.required_channels] == ["-1001111111111", "-1002222222222"]
    plan = build_plan(src)
    assert plan.unaccounted() == set()
    assert plan.change("LTE_WARN_PERCENT").raw == "85"


async def test_missing_tables_give_empty_source(pg_dsn: str) -> None:
    conn = await asyncpg.connect(pg_dsn)
    try:
        assert await read_source_tables(conn) == {
            "system_settings": {},
            "wlq_settings": {},
            "required_channels": [],
        }
    finally:
        await conn.close()


# ------------------------------------------------------------------------------------------ settings


async def test_apply_one_batch_and_idempotent(source_dsn: str, db: CountingDatabase, tmp_path: Path) -> None:
    plan = build_plan(await read_source(source_dsn))
    service = await make_service(db, tmp_path)

    # Shadow/dry-run: the diff writes nothing.
    diff = diff_against(service, plan)
    changed = {d["key"] for d in diff if d["status"] == "change"}
    assert {"OWNER_IDS", "SUPPORT_URL", "PAY_ROLLYPAY_API_KEY", "PAY_ROLLYPAY_ENABLED"} <= changed
    assert "TRIAL_DAYS" not in changed  # 3 = our default
    # Keys added to the registry at integration (request 4b) are now transferred, not reported.
    assert {"PANEL_USERNAME_PREFIX", "TRIAL_CARRY_OVER"} <= changed
    assert "LTE_WARN_PERCENT" in changed
    assert not any("old" in d for d in diff if d["key"] == "REMNAWAVE_WEBHOOK_SECRET")
    assert (await db.raw("SELECT count(*) AS n FROM settings"))[0]["n"] == 0

    result = await apply_settings(service, plan, actor_id=111)
    assert result.batch_id is not None
    snap = service.current()
    assert snap["OWNER_IDS"] == [111, 222]
    assert snap["SUPPORT_URL"] == "https://t.me/myvpn_support"
    assert snap["ADMIN_CHAT_ID"] == -1001234567890
    assert snap["REQUIRED_CHANNEL_ID"] == -1001111111111
    assert snap["CHANNEL_REQUIRED_FOR"] == "all"
    assert snap["CHANNEL_LEAVE_ACTION"] == "trial"
    assert snap["REMNAWAVE_URL"] == "http://remnawave:3000"
    assert snap["REMNAWAVE_WEBHOOK_SECRET"] == "Whsec0abcdefghijklmnopqrstuvwxyz012345"
    assert snap["PAY_ROLLYPAY_API_KEY"] == "rp_live_api_key"
    assert snap["PAY_CRYPTOBOT_API_TOKEN"] == "12345:AAcryptoBotToken"
    assert snap["REPORT_DAILY_AT"] == "10:00"
    assert snap["NOTIFY_USER_DEVICES"] is False
    assert snap["REFERRAL_TRIGGER"] == "trial_or_paid"
    assert snap["IP_GUARD_MIN_SUBNETS"] == 10
    assert snap["IP_GUARD_IGNORE_CIDRS"] == ["10.0.0.0/8", "192.168.0.0/16"]
    assert snap["BACKUP_PASSWORD"] == "p@ss # not a comment"
    assert snap["MAINTENANCE_MODE"] == "auto"
    assert (snap["LTE_ENFORCE"], snap["LTE_OFF_ACTION"], snap["LTE_ENFORCE_LIST"]) == (
        "shadow",
        "keep",
        [101, 102],
    )
    assert (snap["LTE_WARN_PERCENT"], snap["LTE_QUIET_HOURS"], snap["LTE_TOPUP_ENABLED"]) == (
        85,
        "23:00-08:00",
        False,
    )
    # Deferred values are NOT applied: no cash desk, IP Guard, webhook mode switched on by the import.
    assert snap["PAY_ROLLYPAY_ENABLED"] is False
    assert snap["PAY_CRYPTOBOT_ENABLED"] is False
    assert snap["PAY_STARS_ENABLED"] is False
    assert snap["IP_GUARD_ENABLED"] is False
    assert snap["IP_GUARD_AUTO_BLOCK"] is False
    assert snap["LTE_ENABLED"] is False  # step 12 of the runbook, after the LTE state import
    assert snap["BOT_MODE"] == "polling"
    assert (snap["PANEL_USERNAME_PREFIX"], snap["TRIAL_CARRY_OVER"]) == ("user_", True)
    # Keys our registry does not have are reported, not lost silently.
    missing = {n.origin for n in result.not_transferred if "нет настройки" in n.reason}
    assert not {"PANEL_USERNAME_PREFIX", "TRIAL_CARRY_OVER"} & missing
    assert not any(k.startswith("LTE_") for k in missing)

    rows = await db.raw("SELECT key, source FROM settings ORDER BY key")
    assert rows and {r["source"] for r in rows} == {"import"}
    batches = await db.raw("SELECT DISTINCT batch_id FROM settings_audit WHERE source = 'import'")
    assert [str(b["batch_id"]) for b in batches] == [result.batch_id]  # ONE batch → one-button undo
    secret_rows = await db.raw("SELECT value FROM settings WHERE key = 'PAY_ROLLYPAY_API_KEY'")
    assert "rp_live_api_key" not in json.dumps(secret_rows[0]["value"])  # encrypted at rest

    audit_before = (await db.raw("SELECT count(*) AS n FROM settings_audit"))[0]["n"]
    again = await apply_settings(service, build_plan(await read_source(source_dsn)), actor_id=111)
    assert again.batch_id is None
    assert again.applied == []
    assert (await db.raw("SELECT count(*) AS n FROM settings_audit"))[0]["n"] == audit_before

    # One-button undo of the import batch.
    await service.undo(result.batch_id, actor_id=111)
    assert service.current()["OWNER_IDS"] == []


async def test_deferred_applied_on_request(source_dsn: str, db: CountingDatabase, tmp_path: Path) -> None:
    plan = build_plan(await read_source(source_dsn))
    service = await make_service(db, tmp_path)
    await apply_settings(service, plan)
    res = await apply_settings(service, plan, include_deferred=["PAY_ROLLYPAY_ENABLED", "IP_GUARD_ENABLED"])
    assert sorted(res.applied) == ["IP_GUARD_ENABLED", "PAY_ROLLYPAY_ENABLED"]
    snap = service.current()
    assert snap["PAY_ROLLYPAY_ENABLED"] is True
    assert snap["PAY_CRYPTOBOT_ENABLED"] is False
    res = await apply_settings(service, plan, include_deferred=True)
    assert "BOT_MODE" in res.applied and "PUBLIC_URL" in res.applied
    snap = service.current()
    assert snap["BOT_MODE"] == "webhook"
    assert (snap["LTE_ENABLED"], snap["LTE_ENFORCE"], snap["LTE_OFF_ACTION"]) == (True, "on", "keep")


async def test_locked_and_invalid_keys_are_reported(db: CountingDatabase, tmp_path: Path) -> None:
    service = await make_service(db, tmp_path, environ={"LOCKED_KEYS": "TRIAL_DAYS", "TRIAL_DAYS": "4"})
    plan = build_plan(
        BedolagaSettingsSource(
            env={"TRIAL_DURATION_DAYS": "3", "BACKUP_MAX_KEEP": "500", "TZ": "Europe/Moscow"}
        )
    )
    res = await apply_settings(service, plan)
    reasons = {n.origin: n.reason for n in res.not_transferred}
    assert "LOCKED_KEYS" in reasons["TRIAL_DAYS"]
    assert "BACKUP_KEEP" in reasons
    assert service.current()["TRIAL_DAYS"] == 4
    assert service.current()["BACKUP_KEEP"] == 7


# ------------------------------------------------------------------------------------------ data


async def _prices(db: CountingDatabase, plan_id: int) -> dict[int, int]:
    rows = await db.raw(
        "SELECT days, amount_minor FROM plan_prices WHERE plan_id = $1 AND currency = 'RUB'", plan_id
    )
    return {int(r["days"]): int(r["amount_minor"]) for r in rows}


async def test_catalog_created_and_idempotent(source_dsn: str, db: CountingDatabase) -> None:
    cat = build_plan(await read_source(source_dsn)).catalog
    async with db.tx() as conn:
        res = await apply_catalog(conn, cat, trial_squads=[SQUADS[0]])
    assert sorted(res.created) == ["bedolaga_classic", "trial"]
    plan_row = (await db.raw("SELECT * FROM plans WHERE id = $1", res.plan_id))[0]
    assert plan_row["code"] == "bedolaga_classic"
    assert plan_row["enabled"] is True and list(plan_row["squads"]) == list(SQUADS)
    assert plan_row["device_limit"] == 5
    assert plan_row["traffic_bytes"] == 0
    assert plan_row["reset_strategy"] == "MONTH"
    assert plan_row["traffic_on_renew"] == "keep"
    assert plan_row["devices_on_renew"] == "keep"
    assert plan_row["panel_tag"] == "PAID"
    assert plan_row["device_addon"] == {
        "price_minor": 1900,
        "per_days": 30,
        "currency": "RUB",
        "max_devices": 15,
    }
    assert await _prices(db, res.plan_id) == {30: 17900, 90: 49900, 180: 89900, 360: 169900}
    trial = (await db.raw("SELECT * FROM plans WHERE is_trial"))[0]
    assert (trial["device_limit"], trial["traffic_bytes"], trial["panel_tag"]) == (5, 0, "TRIAL")
    assert trial["enabled"] is True and list(trial["squads"]) == [SQUADS[0]]

    versions = {r["id"]: r["version"] for r in await db.raw("SELECT id, version FROM plans")}
    async with db.tx() as conn:
        again = await apply_catalog(conn, cat)
    assert not again.changed
    assert {r["id"]: r["version"] for r in await db.raw("SELECT id, version FROM plans")} == versions
    assert (await db.raw("SELECT count(*) AS n FROM plans"))[0]["n"] == 2


async def test_catalog_aligns_existing_plan(db: CountingDatabase) -> None:
    async with db.tx() as conn:
        plan_id = await create_plan(conn, name="Стандарт", code="standard", squads=list(SQUADS), enabled=True)
        await set_price(conn, plan_id, days=30, amount_minor=15000, currency="RUB")
        await set_price(conn, plan_id, days=14, amount_minor=9900, currency="RUB")
        await set_price(conn, plan_id, days=30, amount_minor=300, currency="USD")
    cat = CatalogImport(prices={30: 17900, 90: 49900}, device_limit=5, sources={"DEFAULT_DEVICE_LIMIT"})
    async with db.tx() as conn:
        res = await apply_catalog(conn, cat)
    assert res.plan_id == plan_id and res.created == []
    assert res.prices_set == [30, 90] and res.prices_removed == [14]
    assert await _prices(db, plan_id) == {30: 17900, 90: 49900}  # RUB aligned; other currency untouched
    usd = await db.raw(
        "SELECT amount_minor FROM plan_prices WHERE plan_id = $1 AND currency = 'USD'", plan_id
    )
    assert usd[0]["amount_minor"] == 300
    assert res.updated == ["device_limit"]
    assert (await db.raw("SELECT count(*) AS n FROM plans"))[0]["n"] == 1  # no trial data → no trial plan


async def _plan_map(db: CountingDatabase) -> list[str]:
    rows = await db.raw(
        "SELECT new_id FROM legacy_id_map"
        " WHERE source = 'bedolaga' AND entity = 'plan' AND old_id = 'classic'"
    )
    return [r["new_id"] for r in rows]


async def test_catalog_shares_the_paid_plan_with_the_data_importer(db: CountingDatabase) -> None:
    """The data importer (``bedolaga/catalog.py``) finds the paid plan through ``legacy_id_map`` first: the
    settings import writes that row for the plan it aligned, and follows a row the data importer wrote."""
    cat = CatalogImport(prices={30: 17900})
    async with db.tx() as conn:
        standard = await create_plan(
            conn, name="Стандарт", code="standard", squads=list(SQUADS), enabled=True
        )
        res = await apply_catalog(conn, cat)
    assert res.plan_id == standard
    assert await _plan_map(db) == [str(standard)]
    stamp_sql = "SELECT updated_at FROM legacy_id_map WHERE entity = 'plan'"
    stamp = (await db.raw(stamp_sql))[0]["updated_at"]
    async with db.tx() as conn:
        again = await apply_catalog(conn, cat)
    assert again.plan_id == standard and not again.changed
    assert (await db.raw(stamp_sql))[0]["updated_at"] == stamp  # an equal row is not rewritten

    # The data importer picked another plan earlier: the settings import aligns THAT one, not the preset.
    async with db.tx() as conn:
        other = await create_plan(conn, name="Импорт", code="imported", squads=list(SQUADS), enabled=True)
        await conn.execute(
            sa.text("UPDATE legacy_id_map SET new_id = :n WHERE entity = 'plan' AND old_id = 'classic'"),
            {"n": str(other)},
        )
        res = await apply_catalog(conn, cat)
    assert res.plan_id == other and res.prices_set == [30]
    assert await _prices(db, standard) == {30: 17900}  # the preset is left as it was
    assert (await db.raw("SELECT count(*) AS n FROM plans WHERE NOT is_trial"))[0]["n"] == 2


async def test_catalog_currency_mismatch_is_skipped(db: CountingDatabase) -> None:
    async with db.tx() as conn:
        res = await apply_catalog(conn, CatalogImport(prices={30: 17900}), currency="USD")
        count = await conn.scalar(sa.select(sa.func.count()).select_from(plans))
        prices = await conn.scalar(sa.select(sa.func.count()).select_from(plan_prices))
    assert res.skipped and count == 0 and prices == 0


async def test_topics_and_cdn_nodes(source_dsn: str, db: CountingDatabase) -> None:
    plan = build_plan(await read_source(source_dsn))
    async with db.tx() as conn:
        assert sorted(await apply_topics(conn, plan.topics)) == ["backups", "payments", "reports"]
        assert await apply_topics(conn, plan.topics) == []
    rows = await db.raw("SELECT kind, chat_id, thread_id FROM admin_topics ORDER BY kind")
    assert [(r["kind"], r["chat_id"], r["thread_id"]) for r in rows] == [
        ("backups", -1001234567890, 11),
        ("payments", -1001234567890, 5),
        ("reports", -1001234567890, 8),
    ]
    async with db.tx() as conn:
        if not await conn.scalar(sa.text("SELECT to_regclass('public.ip_guard_nodes') IS NOT NULL")):
            from svbg.ext.ip_guard.tables import ip_guard_nodes

            await conn.run_sync(ip_guard_nodes.create)
        # The table is hidden for a moment: without it the import is a no-op (``None``), nothing raised.
        hidden = await conn.begin_nested()
        await conn.execute(sa.text("ALTER TABLE ip_guard_nodes RENAME TO ip_guard_nodes_hidden"))
        assert await apply_cdn_nodes(conn, plan.cdn_nodes) is None
        await hidden.rollback()
        assert await apply_cdn_nodes(conn, plan.cdn_nodes) == 2
        assert await apply_cdn_nodes(conn, plan.cdn_nodes) == 0  # idempotent
    nodes = await db.raw("SELECT node_uuid, cdn FROM ip_guard_nodes ORDER BY node_uuid")
    assert [(r["node_uuid"], r["cdn"]) for r in nodes] == [("node-cdn-1", True), ("node-cdn-2", True)]
