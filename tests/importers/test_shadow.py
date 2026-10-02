"""Shadow mode (06 §4.2–4.3): token probe, reconciliation С1–С12, «what the writer would write», storage.

The source is the REAL Bedolaga schema of the owner's production (``.tools/bedolaga/db-schema.sql``, structure
only) loaded into a throwaway database, filled with synthetic rows that exercise every reconciled field. The
target is our schema (``create_schema`` + the owner-module tables) filled as a *perfect* import of that
source; each red test breaks exactly one thing and expects exactly the matching check to turn red.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from svbg.db.engine import normalize_dsn
from svbg.importers.shadow import (
    HISTORY_KEY,
    LAST_KEY,
    PROBE_OFFSET,
    PanelIndex,
    Reconciliation,
    ShadowConfig,
    ShadowService,
    _frozen_seconds,
    deeplink_resolver,
    green_streak,
    open_readonly,
    probe_token,
    ref_payload,
)
from svbg.remnawave.api import RemnawaveApi
from svbg.remnawave.transport import Transport, TransportConfig
from tests.fakes.remnawave import FakeRemnawave
from tests.pgcluster import PgCluster

pytestmark = pytest.mark.timeout(180)

REPO = Path(__file__).resolve().parents[2]
SCHEMA_SQL = REPO / ".tools" / "bedolaga" / "db-schema.sql"
BASE = "11111111-1111-4111-8111-111111111111"
TWIN = "22222222-2222-4222-8222-222222222222"
READ_SCOPES = ("users:read", "system:read", "internal-squads:read", "nodes:read")
GiB = 1024**3


# ------------------------------------------------------------------------------------------ source schema


def bedolaga_ddl() -> str:
    """The production schema dump without roles/owners/grants/psql meta-commands (no data in it at all)."""
    keep: list[str] = []
    for line in SCHEMA_SQL.read_text("utf-8").splitlines():
        s = line.strip()
        if s.startswith("\\") or "OWNER TO" in s:
            continue
        if s.startswith(
            ("GRANT ", "REVOKE ", "CREATE EXTENSION", "COMMENT ON EXTENSION", "ALTER DEFAULT PRIVILEGES")
        ):
            continue
        keep.append(line)
    return "\n".join(keep)


_TEMPLATES: dict[tuple[int, str], Any] = {}


async def _admin(cluster: PgCluster, sql: str) -> None:
    admin = await asyncpg.connect(cluster.dsn("postgres"))
    try:
        await admin.execute(sql)
    finally:
        await admin.close()


async def _new_db(cluster: PgCluster, prefix: str, template: str | None = None) -> str:
    name = f"{prefix}_{uuid.uuid4().hex[:10]}"
    tpl = f' TEMPLATE "{template}" STRATEGY FILE_COPY' if template else ""
    await _admin(cluster, f'CREATE DATABASE "{name}"{tpl}')
    return name


@contextlib.asynccontextmanager
async def _clone(cluster: PgCluster, template: str, prefix: str) -> AsyncIterator[str]:
    name = await _new_db(cluster, prefix, template)
    try:
        yield cluster.dsn(name)
    finally:
        await _admin(cluster, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


async def _template(cluster: PgCluster, kind: str, build: Any) -> Any:
    """Build a template database once per test process (cloning it costs ~0.1 s)."""
    key = (id(cluster), kind)
    if key not in _TEMPLATES:
        _TEMPLATES[key] = await build()
    return _TEMPLATES[key]


@pytest.fixture(scope="module")
async def bedolaga_template(pg_cluster: PgCluster) -> str:
    async def build() -> str:
        name = await _new_db(pg_cluster, "bedolaga_tpl")
        conn = await asyncpg.connect(pg_cluster.dsn(name))
        try:
            await conn.execute(bedolaga_ddl())
        finally:
            await conn.close()
        return name

    return await _template(pg_cluster, "bedolaga", build)


@pytest.fixture(scope="module")
async def target_template(pg_cluster: PgCluster) -> str:
    async def build() -> str:
        name = await _new_db(pg_cluster, "svbg_tpl")
        await create_target(pg_cluster.dsn(name))
        return name

    return await _template(pg_cluster, "target", build)


@pytest.fixture
async def source_dsn(pg_cluster: PgCluster, bedolaga_template: str) -> AsyncIterator[str]:
    async with _clone(pg_cluster, bedolaga_template, "src") as dsn:
        yield dsn


async def create_target(dsn: str) -> None:
    """Our schema + the owner-module tables (registered by the integration step later)."""
    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import create_async_engine

    import svbg.ads.tables
    import svbg.ext.ip_guard.tables
    import svbg.promo.tables
    import svbg.referral.tables  # noqa: F401 - registers the tables on the shared metadata
    from svbg.db.schema import create_schema
    from svbg.ext.lte import tables as lte

    await create_schema(dsn)
    engine = create_async_engine(normalize_dsn(dsn)[0])
    try:
        async with engine.begin() as conn:
            names = [t for t in lte.lte_metadata.sorted_tables if t.name.startswith("lte_")]
            await conn.run_sync(lambda c: lte.lte_metadata.create_all(c, tables=names))
            assert await conn.scalar(sa.text("SELECT to_regclass('public.lte_blocks') IS NOT NULL"))
    finally:
        await engine.dispose()


@pytest.fixture
async def target_dsn(pg_cluster: PgCluster, target_template: str) -> AsyncIterator[str]:
    async with _clone(pg_cluster, target_template, "dst") as dsn:
        yield dsn


# ------------------------------------------------------------------------------------------- row helpers

_FILL: dict[str, Any] = {
    "integer": 0,
    "bigint": 0,
    "smallint": 0,
    "boolean": False,
    "character varying": "",
    "text": "",
    "double precision": 0.0,
    "numeric": 0,
    "jsonb": "{}",
    "json": "{}",
}


class Rows:
    """INSERT helper: JSON values are encoded, NOT NULL columns without a default get a neutral value."""

    def __init__(self, conn: asyncpg.Connection, *, fill: bool) -> None:
        self.conn = conn
        self.fill = fill
        self._cols: dict[str, list[asyncpg.Record]] = {}

    async def columns(self, table: str) -> list[asyncpg.Record]:
        if table not in self._cols:
            self._cols[table] = await self.conn.fetch(
                "SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = $1",
                table,
            )
        return self._cols[table]

    async def add(self, table: str, **values: Any) -> None:
        types = {}
        for c in await self.columns(table):
            types[c["column_name"]] = c["data_type"]
            if (
                self.fill
                and c["is_nullable"] == "NO"
                and c["column_default"] is None
                and c["column_name"] not in values
            ):
                kind = c["data_type"]
                if kind == "timestamp with time zone":
                    values[c["column_name"]] = datetime.now(UTC)
                elif kind == "uuid":
                    values[c["column_name"]] = str(uuid.uuid4())
                else:
                    values[c["column_name"]] = _FILL.get(kind, "")
        for key, value in list(values.items()):
            if types.get(key) in ("json", "jsonb") and not isinstance(value, str):
                values[key] = json.dumps(value)
            if types.get(key) == "uuid" and value is not None:
                values[key] = str(value)
        cols = ", ".join(values)
        marks = ", ".join(f"${i}" for i in range(1, len(values) + 1))
        await self.conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", *values.values())


def _s(dt: datetime) -> datetime:
    return dt.replace(microsecond=0)


@dataclass
class World:
    now: datetime
    panel: FakeRemnawave
    expire: dict[int, datetime] = field(default_factory=dict)  # panel user id → expireAt
    short: dict[int, str] = field(default_factory=dict)
    blocked_at: datetime | None = None
    frozen: int = 0


# ------------------------------------------------------------------------------------------ the dataset


def seed_panel(panel: FakeRemnawave, now: datetime) -> World:
    world = World(now=now, panel=panel)
    panel.add_internal_squad("NL", squad_uuid=BASE)
    panel.add_internal_squad("NL-LTE-twin", squad_uuid=TWIN)
    spec = [
        # id: tg, days, squads, tag, status
        (1001, 20, [BASE], "PAID", "ACTIVE"),
        (1002, 2, [BASE], "TRIAL", "ACTIVE"),
        (1004, 40, [TWIN], "PAID", "ACTIVE"),
        (None, 10, [BASE], "PAID", "DISABLED"),
        (None, 365, [BASE], None, "ACTIVE"),  # unowned service account
    ]
    for tg, days, squads, tag, status in spec:
        expire = _s(now + timedelta(days=days))
        user = panel.add_user(
            shortUuid=f"short{len(panel.users) + 1:011d}",
            username=f"user_{tg}" if tg else f"svc_{days}",
            telegramId=tg,
            expireAt=expire,
            activeInternalSquads=squads,
            tag=tag,
            status=status,
            hwidDeviceLimit=5,
        )
        world.expire[user["id"]] = expire
        world.short[user["id"]] = user["shortUuid"]
    return world


async def seed_source(dsn: str, w: World) -> None:
    now = w.now
    conn = await asyncpg.connect(dsn)
    try:
        r = Rows(conn, fill=True)
        users = [
            # id, tg, balance, status, referred_by, code, created
            (1, 1001, 50000, "active", None, "AbC123xy", now - timedelta(days=100)),
            (2, 1002, 0, "active", 1, "ref2Code", now - timedelta(days=6)),
            (3, 1003, 0, "deleted", None, None, now - timedelta(days=50)),
            (4, 1004, 12345, "deleted", None, None, now - timedelta(days=40)),
            (5, None, 0, "active", 4, None, now - timedelta(days=2)),
        ]
        for uid, tg, bal, status, ref, code, created in users:
            await r.add(
                "users",
                id=uid,
                telegram_id=tg,
                auth_type="email" if tg is None else "telegram",
                username=f"u{uid}",
                status=status,
                language="ru",
                balance_kopeks=bal,
                referred_by_id=ref,
                referral_code=code,
                created_at=created,
                updated_at=created,
                partner_status="none",
                remnawave_id={1: 1, 2: 2, 4: 3, 5: 4}.get(uid),
            )
        subs = [(11, 1, 1, False), (12, 2, 2, True), (13, 4, 3, False), (14, 5, 4, False)]
        for sid, uid, pid, trial in subs:
            await r.add(
                "subscriptions",
                id=sid,
                user_id=uid,
                status="trial" if trial else "active",
                is_trial=trial,
                start_date=now - timedelta(days=30),
                end_date=w.expire[pid],
                traffic_limit_gb=0,
                device_limit=5,
                connected_squads=[BASE],
                subscription_url=f"https://sub.example.com/{w.short[pid]}",
                remnawave_short_uuid=w.short[pid],
                remnawave_id=pid,
                remnawave_short_id=f"s{sid}",
                created_at=now - timedelta(days=30),
            )
        await r.add("server_squads", id=1, squad_uuid=BASE, display_name="🇳🇱 NL", is_trial_eligible=True)
        await r.add(
            "transactions",
            id=501,
            user_id=1,
            type="deposit",
            amount_kopeks=50000,
            payment_method="cryptobot",
            is_completed=True,
            created_at=now - timedelta(days=20),
        )
        await r.add(
            "transactions",
            id=502,
            user_id=1,
            type="deposit",
            amount_kopeks=10000,
            payment_method="telegram_stars",
            external_id="tgcharge_1",
            is_completed=True,
            created_at=now - timedelta(days=10),
        )
        rp = [
            ("rp1001_aaaaaa", "RP-1", 17900, "paid", True, now - timedelta(days=30)),
            ("rp1002_bbbbbb", "RP-2", 49900, "pending", False, now - timedelta(hours=1)),
            ("rp1001_cccccc", "RP-3", 17900, "expired", False, now - timedelta(days=5)),
            ("rp1001_dddddd", "RP-4", 17900, "expired", False, now - timedelta(hours=10)),
        ]
        for i, (order, pid, amount, status, paid, created) in enumerate(rp, start=1):
            await r.add(
                "rollypay_payments",
                id=i,
                user_id=1 if order.startswith("rp1001") else 2,
                order_id=order,
                rollypay_payment_id=pid,
                amount_kopeks=amount,
                status=status,
                is_paid=paid,
                paid_at=created if paid else None,
                created_at=created,
            )
        cb = [
            ("CB-1", "paid", 501, now - timedelta(days=20)),
            ("CB-2", "active", None, now - timedelta(hours=2)),
            ("CB-3", "active", None, now - timedelta(days=3)),
        ]
        for i, (inv, status, tx, created) in enumerate(cb, start=1):
            await r.add(
                "cryptobot_payments",
                id=i,
                user_id=1,
                invoice_id=inv,
                amount="5.00",
                asset="USDT",
                status=status,
                transaction_id=tx,
                created_at=created,
            )
        await r.add(
            "platega_payments",
            id=1,
            user_id=1,
            correlation_id="PL-1",
            amount_kopeks=30000,
            currency="RUB",
            payment_method_code=2,
            status="CONFIRMED",
            is_paid=True,
            created_at=now - timedelta(days=60),
        )
        await r.add(
            "promocodes",
            id=21,
            code="SALE10",
            type="discount",
            balance_bonus_kopeks=10,
            subscription_days=48,
            is_active=True,
        )
        await r.add(
            "promocodes", id=22, code="Gift", type="balance", balance_bonus_kopeks=1000, is_active=True
        )
        await r.add("promocode_uses", id=1, promocode_id=21, user_id=1, used_at=now - timedelta(days=9))
        await r.add("promocode_uses", id=2, promocode_id=21, user_id=2, used_at=now - timedelta(days=3))
        await r.add(
            "advertising_campaigns",
            id=31,
            name="TikTok",
            start_parameter="tiktok",
            bonus_type="none",
            is_active=True,
        )
        await r.add(
            "advertising_campaigns",
            id=32,
            name="Insta",
            start_parameter="Insta_2",
            bonus_type="none",
            is_active=True,
        )
        regs = [
            (31, 2, now - timedelta(days=6)),
            (32, 2, now - timedelta(days=5)),
            (32, 1, now - timedelta(days=50)),
        ]
        for i, (cid, uid, at) in enumerate(regs, start=1):
            await r.add(
                "advertising_campaign_registrations",
                id=i,
                campaign_id=cid,
                user_id=uid,
                bonus_type="none",
                created_at=at,
            )
        refs = [
            (1, 2, "referral_days_inviter", 5),
            (1, 2, "referral_days_invitee", 5),  # user_id is always the inviter (the pair's key)
            (2, 2, "referral_days_invitee", 5),  # a self marker (admin test): never counted, never imported
            (4, 5, "referral_days_inviter_skipped", 1),
            (1, 2, "referral_days_inviter_skipped", 6),  # skipped, then granted: the granted marker wins
        ]
        for i, (uid, rid, reason, days_ago) in enumerate(refs, start=1):
            await r.add(
                "referral_earnings",
                id=i,
                user_id=uid,
                referral_id=rid,
                amount_kopeks=0,
                reason=reason,
                reward_type="days",
                created_at=now - timedelta(days=days_ago),
            )
        await r.add("wlq_groups", id=1)
        await r.add(
            "wlq_squads",
            id=1,
            kind="twin",
            base_squad_uuid=BASE,
            group_id=1,
            panel_uuid=TWIN,
            name="twin",
            desired_inbound_uuids=[],
        )
        for sid, (pid, sub) in enumerate([(1, 11), (2, 12), (3, 13), (4, 14)], start=101):
            await r.add("wlq_subjects", id=sid, panel_user_id=pid, subscription_id=sub, kind="bot")
        await r.add(
            "wlq_periods",
            id=1003,
            subject_id=103,
            anchor_at=now - timedelta(days=10),
            period_index=0,
            starts_at=now - timedelta(days=10),
            planned_end_at=now + timedelta(days=20),
            state="open",
        )
        await r.add("wlq_period_usage", period_id=1003, group_id=1, used_bytes=3_000_000_000)
        await r.add(
            "wlq_blocks",
            id=1,
            subject_id=103,
            panel_user_id=3,
            group_id=1,
            period_id=1003,
            mode="enforce",
            status="active",
            reason="quota",
            used_bytes_at_block=3 * 10**9,
            limit_bytes_at_block=3 * 10**9,
        )
        w.blocked_at = _s(now - timedelta(days=2))
        block = {
            "owner_kind": "bot_sub",
            "blocked_at": w.blocked_at,
            "end_date_at_block": w.expire[4],
            "panel_expire_at_block": w.expire[4],
            "credited_seconds": 3600,
            "zeroed_during_block": False,
        }
        w.frozen = _frozen_seconds(block)
        await r.add(
            "ip_guard_blocks",
            id=1,
            status="active",
            panel_user_id=4,
            user_id=5,
            subscription_id=14,
            ip_count=30,
            live_ip_count=12,
            subnet_count=11,
            ips=[],
            **block,
        )
        await r.add("system_settings", id=1, key="IP_GUARD_WHITELIST_PANEL_USER_IDS", value="1, 999999;abc")
        await r.add(
            "sent_notifications",
            id=1,
            user_id=2,
            subscription_id=12,
            notification_type="trial_expiring",
            days_before=1,
            created_at=now - timedelta(hours=1),
        )
    finally:
        await conn.close()


async def seed_target(dsn: str, w: World) -> None:
    """What a correct importer produces from :func:`seed_source` (06 §2) — the reconciliation must be
    green."""
    now = w.now
    conn = await asyncpg.connect(dsn)
    try:
        r = Rows(conn, fill=False)
        for slug in ("rollypay", "cryptobot", "platega_legacy", "stars"):
            await r.add(
                "payment_instances",
                provider=slug.split("_")[0],
                slug=slug,
                title=slug,
                config="enc:v1:x",
                webhook_token="enc:v1:y",
            )
        inst = {row["slug"]: row["id"] for row in await conn.fetch("SELECT slug, id FROM payment_instances")}
        for uid, tg, wallet, created in [
            (1, 1001, 50000, 100),
            (2, 1002, 0, 6),
            (4, 1004, 12345, 40),
            (5, None, 0, 2),
        ]:
            await r.add(
                "users",
                id=uid,
                telegram_id=tg,
                username=f"u{uid}",
                language="ru",
                wallet_minor=wallet,
                created_at=now - timedelta(days=created),
            )
            if wallet:
                await r.add(
                    "wallet_ledger",
                    user_id=uid,
                    amount_minor=wallet,
                    currency="RUB",
                    balance_after=wallet,
                    reason="import_opening",
                    ref_type="import_run",
                    ref_id="1",
                )
            await r.add("trial_grants", user_id=uid, telegram_id=tg, source="import")
        subs = [
            (11, 1, 1, False),
            (12, 2, 2, True),
            (13, 4, 3, False),
            (14, 5, 4, False),
            (15, None, 5, False),
        ]
        for sid, uid, pid, trial in subs:
            user = w.panel.users[pid]
            hold = sid == 14
            await r.add(
                "subscriptions",
                id=sid,
                user_id=uid,
                link_state="linked",
                panel_user_id=pid,
                panel_short_uuid=w.short[pid],
                panel_username=user["username"],
                subscription_url=f"https://sub.example.com/{w.short[pid]}",
                paid_until=w.expire[pid],
                desired_expire_at=w.expire[pid],
                desired_traffic_bytes=0,
                desired_reset_strategy="NO_RESET",
                desired_device_limit=5,
                desired_squads=[BASE],
                desired_tag=user["tag"],
                desired_status="disabled" if hold else "active",
                disabled_reason="ip_guard" if hold else None,
                hold_kind="ip_guard" if hold else None,
                hold_since=w.blocked_at if hold else None,
                hold_frozen_seconds=w.frozen if hold else 0,
                is_trial=trial,
                panel_status=user["status"],
                panel_expire_at=w.expire[pid],
                created_at=now - timedelta(days=30),
            )
        await r.add("panel_squad_twins", substitute_squad_uuid=TWIN, base_squad_uuid=BASE, owner_module="lte")
        await r.add(
            "panel_squad_substitutions",
            subscription_id=13,
            base_squad_uuid=BASE,
            substitute_squad_uuid=TWIN,
            owner_module="lte",
        )

        async def pay(
            slug: str,
            *,
            ext: str,
            ref: str | None,
            amount: int,
            status: str,
            user: int = 1,
            paid: int | None = None,
            order: bool = False,
            currency: str = "RUB",
        ) -> None:
            order_id = None
            if order:
                order_id = await conn.fetchval(
                    "INSERT INTO orders (user_id, kind, status, currency, total_minor) "
                    "VALUES ($1, 'topup', 'awaiting_payment', 'RUB', $2) RETURNING id",
                    user,
                    amount,
                )
            await r.add(
                "payments",
                instance_id=inst[slug],
                user_id=user,
                external_id=ext,
                merchant_ref=ref,
                amount_minor=amount,
                currency=currency,
                status=status,
                is_imported=True,
                order_id=order_id,
                paid_amount_minor=paid,
                paid_at=now - timedelta(days=5) if status == "paid" else None,
            )

        await pay("rollypay", ext="RP-1", ref="rp1001_aaaaaa", amount=17900, status="paid", paid=17900)
        await pay(
            "rollypay", ext="RP-2", ref="rp1002_bbbbbb", amount=49900, status="pending", user=2, order=True
        )
        await pay("rollypay", ext="RP-3", ref="rp1001_cccccc", amount=17900, status="expired")
        await pay("rollypay", ext="RP-4", ref="rp1001_dddddd", amount=17900, status="pending", order=True)
        await pay("cryptobot", ext="CB-1", ref=None, amount=50000, status="paid", paid=50000)
        await pay("cryptobot", ext="CB-2", ref=None, amount=50000, status="pending", order=True)
        await pay("cryptobot", ext="CB-3", ref=None, amount=50000, status="expired")
        await pay("platega_legacy", ext="PL-1", ref=None, amount=30000, status="paid", paid=30000)
        await pay("stars", ext="tgcharge_1", ref=None, amount=100, status="paid", paid=100, currency="XTR")

        await r.add(
            "promocodes", id=21, code="SALE10", kind="percent", percent=10, pending_hours=48, source="import"
        )
        await r.add(
            "promocodes",
            id=22,
            code="Gift",
            kind="wallet",
            amount_minor=1000,
            currency="RUB",
            source="import",
        )
        await r.add("promo_uses", promo_id=21, user_id=1, source="import")
        await r.add("promo_uses", promo_id=21, user_id=2, source="import")
        await r.add("ad_links", id=31, code="tiktok", title="TikTok", source="import")
        await r.add("ad_links", id=32, code="Insta_2", title="Insta", source="import")
        await r.add("ad_link_users", user_id=2, ad_link_id=31, source="import")
        await r.add("ad_link_users", user_id=1, ad_link_id=32, source="import")
        await r.add("referral_codes", user_id=1, code="AbC123xy", source="import")
        await r.add("referral_codes", user_id=2, code="ref2Code", source="import")
        await r.add("referrals", referred_user_id=2, referrer_id=1, source="import")
        await r.add("referrals", referred_user_id=5, referrer_id=4, source="import")
        await r.add(
            "referral_rewards",
            referred_user_id=2,
            user_id=1,
            side="inviter",
            kind="days",
            status="granted",
            days=14,
            granted_at=now - timedelta(days=5),
        )
        await r.add(
            "referral_rewards",
            referred_user_id=2,
            user_id=2,
            side="invitee",
            kind="days",
            status="granted",
            days=7,
            granted_at=now - timedelta(days=5),
        )
        await r.add(
            "referral_rewards",
            referred_user_id=5,
            user_id=4,
            side="inviter",
            kind="days",
            status="deferred",
            retry_until=now + timedelta(days=6),
        )
        await r.add("lte_groups", id=1, slug="lte", squad_uuid=None)
        await r.add("lte_blocks", subscription_id=13, group_id=1, reason="quota", status="active")
        await r.add(
            "lte_periods",
            id=1003,
            subscription_id=13,
            anchor_at=now - timedelta(days=10),
            idx=0,
            starts_at=now - timedelta(days=10),
            planned_end_at=now + timedelta(days=20),
            state="open",
        )
        await r.add("lte_period_usage", period_id=1003, group_id=1, used_bytes=3_010_000_000)
        await r.add(
            "ip_guard_blocks",
            subscription_id=14,
            panel_user_id=4,
            user_id=5,
            status="active",
            reason="auto",
            blocked_at=w.blocked_at,
            frozen_seconds=w.frozen,
        )
        await r.add("ip_guard_exempt", subscription_id=11, reason="import")
        await r.add(
            "notification_log",
            target="sub:12",
            kind="trial_ending",
            anchor=str(int(w.expire[2].timestamp())),
            subscription_id=12,
            user_id=2,
            status="sent",
        )
    finally:
        await conn.close()


# ------------------------------------------------------------------------------------------------- setup


@pytest.fixture
async def panel() -> AsyncIterator[FakeRemnawave]:
    async with FakeRemnawave() as fake:
        yield fake


@dataclass
class Stand:
    world: World
    source: str
    target: str
    api: RemnawaveApi
    panel: FakeRemnawave


#: One moment for the whole module: the seeded templates and every stand share it.
NOW = datetime.now(UTC).replace(microsecond=0)


@pytest.fixture(scope="module")
async def seeded(pg_cluster: PgCluster, bedolaga_template: str, target_template: str) -> tuple[str, str]:
    """The seeded source (shared: nothing ever writes to it) and the seeded target template (cloned per
    test)."""

    async def build() -> tuple[str, str]:
        world = seed_panel(FakeRemnawave(), NOW)  # deterministic: ids 1..5, fixed shortUuids
        src = await _new_db(pg_cluster, "src_tpl", bedolaga_template)
        await seed_source(pg_cluster.dsn(src), world)
        dst = await _new_db(pg_cluster, "dst_tpl", target_template)
        await seed_target(pg_cluster.dsn(dst), world)
        return src, dst

    return await _template(pg_cluster, "seeded", build)


@pytest.fixture
async def stand(pg_cluster: PgCluster, panel: FakeRemnawave, seeded: tuple[str, str]) -> AsyncIterator[Stand]:
    world = seed_panel(panel, NOW)
    src_db, dst_tpl = seeded
    source = pg_cluster.dsn(src_db)
    async with _clone(pg_cluster, dst_tpl, "dst") as target:
        world.blocked_at = _s(NOW - timedelta(days=2))
        transport = Transport(TransportConfig(base_url=panel.url, token=panel.add_token(READ_SCOPES)))
        try:
            yield Stand(world, source, target, RemnawaveApi(transport), panel)
        finally:
            await transport.aclose()


async def reconcile(
    stand: Stand, config: ShadowConfig | None = None
) -> tuple[list[Any], list[dict[str, Any]]]:
    index = await PanelIndex.load(stand.api)
    async with open_readonly(stand.source) as src, open_readonly(stand.target) as dst:
        rec = Reconciliation(src, dst, index, as_of=stand.world.now, config=config)
        checks = await rec.run()
    return checks, rec.ops


def by_code(checks: list[Any]) -> dict[str, Any]:
    return {c.code: c for c in checks}


async def target_exec(stand: Stand, sql: str, *args: Any) -> None:
    conn = await asyncpg.connect(stand.target)
    try:
        await conn.execute(sql, *args)
    finally:
        await conn.close()


# ------------------------------------------------------------------------------------------------- tests


async def test_real_bedolaga_schema_loads(source_dsn: str) -> None:
    conn = await asyncpg.connect(source_dsn)
    try:
        n = await conn.fetchval(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'"
        )
        cols = {
            r[0]
            for r in await conn.fetch(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'users'"
            )
        }
    finally:
        await conn.close()
    assert n >= 160
    assert {"balance_kopeks", "remnawave_id", "referred_by_id", "referral_code"} <= cols


async def test_perfect_import_is_green_and_writer_plan_empty(stand: Stand) -> None:
    checks, ops = await reconcile(stand)
    bad = {c.code: (c.error, c.problems) for c in checks if c.status != "ok"}
    assert bad == {}
    assert [c.code for c in checks] == [f"C{i}" for i in range(1, 13)]
    assert ops == []
    c = by_code(checks)
    assert c["C2"].facts["sum_source"] == c["C2"].facts["sum_target"] == 62345
    assert c["C3"].facts["rollypay"] == {
        "count_source": 1,
        "count_target": 1,
        "sum_source": 17900,
        "sum_target": 17900,
        "amount_unknown": 0,
    }
    assert c["C3"].facts["cryptobot"]["sum_source"] == 50000  # crediting from the linked transaction
    assert c["C4"].facts == {
        "live": 3,
        "imported_pending": 3,
        "polled_before_t0": 0,
    }  # RP-2, RP-4 (expired < 48 h), CB-2
    assert c["C5"].facts["states"] == {"linked": 5}
    assert c["C5"].facts["unowned_panel"] == 0
    assert c["C8"].facts["whitelist_unresolved"] == [999999]
    assert c["C8"].facts["whitelist_invalid"] == ["abc"]
    assert c["C9"].facts["deferred_source"] == 1
    assert c["C10"].facts == {"expected": 1, "seeded": 1}
    assert c["C12"].facts == {"users_with_subscription": 4, "marked": 4}
    assert c["C1"].facts["пользователи"] == {
        "source": 4,
        "target": 4,
        "missing": 0,
    }  # deleted w/o money skipped


BREAKS: list[tuple[str, str, tuple[Any, ...], set[str]]] = [
    ("C1", "DELETE FROM promo_uses WHERE user_id = 2", (), {"C1"}),
    ("C2", "UPDATE users SET wallet_minor = wallet_minor + 1 WHERE id = 1", (), {"C2"}),
    ("C2", "DELETE FROM wallet_ledger WHERE user_id = 4", (), {"C2"}),
    ("C3", "UPDATE payments SET status = 'expired', paid_at = NULL WHERE external_id = 'RP-1'", (), {"C3"}),
    ("C3", "UPDATE payments SET paid_amount_minor = 49999 WHERE external_id = 'CB-1'", (), {"C3"}),
    ("C4", "UPDATE payments SET order_id = NULL WHERE external_id = 'RP-4'", (), {"C4"}),
    ("C4", "DELETE FROM payments WHERE external_id = 'CB-2'", (), {"C1", "C4"}),
    ("C5", "UPDATE subscriptions SET link_state = 'pending' WHERE id = 12", (), {"C5"}),
    ("C6", "UPDATE subscriptions SET desired_device_limit = 7 WHERE id = 11", (), {"C6"}),
    ("C6", "UPDATE subscriptions SET desired_tag = NULL WHERE id = 11", (), {"C6"}),
    ("C7", "DELETE FROM panel_squad_substitutions WHERE subscription_id = 13", (), {"C6", "C7"}),
    ("C7", "UPDATE lte_period_usage SET used_bytes = 2000000000", (), {"C7"}),
    ("C8", "UPDATE ip_guard_blocks SET frozen_seconds = frozen_seconds + 1", (), {"C8"}),
    ("C8", "UPDATE subscriptions SET desired_status = 'active' WHERE id = 14", (), {"C6", "C8"}),
    ("C8", "DELETE FROM ip_guard_exempt", (), {"C8"}),
    ("C9", "DELETE FROM referral_rewards WHERE status = 'deferred'", (), {"C9"}),
    ("C10", "DELETE FROM notification_log", (), {"C10"}),
    ("C11", "UPDATE ad_links SET code = 'insta_2' WHERE id = 32", (), {"C1", "C11"}),
    ("C11", "UPDATE referral_codes SET code = 'abc123xy' WHERE user_id = 1", (), {"C11"}),
    ("C12", "DELETE FROM trial_grants WHERE user_id = 4", (), {"C12"}),
]


@pytest.mark.parametrize(
    ("code", "sql", "args", "red"), BREAKS, ids=[f"{b[0]}-{i}" for i, b in enumerate(BREAKS)]
)
async def test_each_break_turns_exactly_its_check_red(
    stand: Stand, code: str, sql: str, args: tuple[Any, ...], red: set[str]
) -> None:
    await target_exec(stand, sql, *args)
    checks, _ = await reconcile(stand)
    got = {c.code for c in checks if c.status != "ok"}
    assert got == red, {c.code: c.problems for c in checks if c.status != "ok"}
    assert all(c.error is None for c in checks)
    assert by_code(checks)[code].problems


async def test_payment_rows_follow_the_importer_rules(
    stand: Stand, pg_cluster: PgCluster, seeded: tuple[str, str]
) -> None:
    """Money first (06 §2.3): a ``deleted`` user with a desk row is carried over; a CryptoBot payment without
    the linked transaction has no ruble amount — counted, left out of the ruble sums, not a false red."""
    async with _clone(pg_cluster, seeded[0], "srcw") as source:
        conn = await asyncpg.connect(source)
        try:
            r = Rows(conn, fill=True)
            await r.add(
                "cryptobot_payments",
                id=9,
                user_id=1,
                invoice_id="CB-9",
                amount="3.00",
                asset="USDT",
                status="paid",
                transaction_id=None,
                created_at=NOW - timedelta(days=4),
            )
        finally:
            await conn.close()
        await target_exec(
            stand,
            "INSERT INTO payments (instance_id, user_id, external_id, status, amount_minor, currency, "
            "paid_amount_minor, paid_currency, paid_at, is_imported) "
            "SELECT id, 1, 'CB-9', 'paid', 300, 'USDT', 300, 'USDT', $1, true FROM payment_instances "
            "WHERE slug = 'cryptobot'",
            NOW - timedelta(days=4),
        )
        stand.source = source
        checks, _ = await reconcile(stand)
        bad = {c.code: c.problems for c in checks if c.status != "ok"}
        assert bad == {}
        assert by_code(checks)["C3"].facts["cryptobot"]["amount_unknown"] == 1

        conn = await asyncpg.connect(source)
        try:  # user 3 is «deleted», no subscription, no balance — but now he has a RollyPay row
            await Rows(conn, fill=True).add(
                "rollypay_payments",
                id=9,
                user_id=3,
                order_id="rp1003_eeeeee",
                rollypay_payment_id="RP-9",
                amount_kopeks=17900,
                status="expired",
                is_paid=False,
                created_at=NOW - timedelta(days=9),
            )
        finally:
            await conn.close()
        checks, _ = await reconcile(stand)
        c1 = by_code(checks)["C1"]
        assert c1.status == "fail" and any("пользователи: нет в боте 1: 3" in p for p in c1.problems)
        assert any("оплаты rollypay: нет в боте 1: rp1003_eeeeee" in p for p in c1.problems)


async def test_c11_with_the_router_rules(stand: Stand) -> None:
    """С11 through :func:`deeplink_resolver`: exact ``ad_links`` match first, then ``ref<code>`` (06 M7)."""
    seen: list[str] = []

    async def lookup(sql: str, code: str) -> Any:
        conn = await asyncpg.connect(stand.target)
        try:
            return await conn.fetchval(sql, code)
        finally:
            await conn.close()

    async def find_ad(code: str) -> Any:
        seen.append(code)
        return await lookup("SELECT id FROM ad_links WHERE code = $1 AND enabled", code)

    async def find_referrer(code: str) -> Any:
        return await lookup("SELECT user_id FROM referral_codes WHERE code = $1", code)

    resolve = deeplink_resolver(find_ad, find_referrer)
    cfg = ShadowConfig(resolve_link=resolve)
    checks, _ = await reconcile(stand, cfg)
    c11 = by_code(checks)["C11"]
    assert c11.status == "ok", c11.problems
    assert {"tiktok", "Insta_2", "refAbC123xy", "ref2Code"} <= set(seen)
    assert ref_payload("AbC123xy") == "refAbC123xy" and ref_payload("refXYZ1") == "refXYZ1"
    assert await resolve("refNoSuch1") is None and await resolve("pr_SALE10") == "promo"

    await target_exec(stand, "UPDATE ad_links SET enabled = false WHERE code = 'tiktok'")
    checks, _ = await reconcile(stand, cfg)
    c11 = by_code(checks)["C11"]
    assert c11.status == "fail" and any("?start=tiktok" in p for p in c11.problems)


async def test_hold_subscription_expire_patch_is_flagged(stand: Stand) -> None:
    """С8: the writer plan must contain no PATCH expireAt for a frozen (hold) subscription."""
    await target_exec(stand, "UPDATE subscriptions SET desired_expire_at = hold_since WHERE id = 14")
    checks, ops = await reconcile(stand)
    assert {c.code for c in checks if c.status != "ok"} == {"C6", "C8"}
    assert [(o["sub_id"], o["field"], o["hold"]) for o in ops] == [(14, "expire", "ip_guard")]
    assert by_code(checks)["C8"].facts["writer_expire_on_hold"] == 1


async def test_writer_plan_on_panel_side_drift_and_overrides(stand: Stand) -> None:
    stand.panel.users[1]["tag"] = "VIP"
    stand.panel.users[1]["hwidDeviceLimit"] = 9
    stand.panel.users[2]["expireAt"] = stand.world.expire[2] + timedelta(hours=3)
    _, ops = await reconcile(stand)
    assert sorted((o["sub_id"], o["field"]) for o in ops) == [
        (11, "device_limit"),
        (11, "tag"),
        (12, "expire"),
    ]
    # An owner decision kept in ``overrides`` is respected: the writer never fights a manual panel edit.
    await target_exec(
        stand,
        """UPDATE subscriptions SET overrides = '{"tag": "VIP", "device_limit": 9, "_squads_pending": true}'
           WHERE id = 11""",
    )
    _, ops = await reconcile(stand)
    assert [(o["sub_id"], o["field"]) for o in ops] == [(12, "expire")]


async def test_status_and_squad_plan() -> None:
    from svbg.remnawave.contributors import Substitution
    from svbg.remnawave.models import PanelUser, SquadRef

    now = datetime(2026, 10, 2, tzinfo=UTC)
    user = PanelUser(
        id=7,
        short_uuid="s",
        username="u",
        status="DISABLED",
        expire_at=now,
        active_internal_squads=[SquadRef(uuid=TWIN, name="t")],
        hwid_device_limit=None,
    )
    row = {
        "id": 1,
        "panel_user_id": 7,
        "overrides": {},
        "desired_expire_at": now,
        "paid_until": now,
        "desired_status": "active",
        "desired_squads": [BASE],
        "desired_device_limit": None,
        "desired_traffic_bytes": None,
        "desired_reset_strategy": None,
        "desired_tag": None,
        "desired_ext_squad": None,
        "hold_kind": None,
        "disabled_reason": "ip_guard",
    }
    ops = Reconciliation.plan_for(row, user, [Substitution(BASE, TWIN, "lte")])
    assert [(o["op"], o["field"]) for o in ops] == [("enable", "status")]
    ops = Reconciliation.plan_for(row | {"desired_squads": []}, user, [])
    assert ("invalid", "squads") in [(o["op"], o["field"]) for o in ops]  # an empty list is never sent


async def test_unowned_and_missing_panel_users(stand: Stand) -> None:
    stand.panel.add_user(username="extra", expireAt=stand.world.now + timedelta(days=3))
    del stand.panel.users[2]
    checks, _ = await reconcile(stand)
    c5 = by_code(checks)["C5"]
    assert c5.status == "fail" and c5.facts["unowned_panel"] == 1
    assert any("пользователя панели 2 нет" in p for p in c5.problems)


def test_frozen_seconds_formula() -> None:
    t = datetime(2026, 10, 1, tzinfo=UTC)
    row = {
        "owner_kind": "bot_sub",
        "blocked_at": t,
        "end_date_at_block": t + timedelta(days=3),
        "panel_expire_at_block": t + timedelta(days=9),
        "credited_seconds": 60,
        "zeroed_during_block": False,
    }
    assert _frozen_seconds(row) == 3 * 86400 + 60
    assert _frozen_seconds(row | {"owner_kind": "panel"}) == 9 * 86400 + 60
    assert _frozen_seconds(row | {"zeroed_during_block": True}) == 60
    assert _frozen_seconds(row | {"end_date_at_block": t - timedelta(days=1)}) == 60  # never negative


def test_green_streak_counts_whole_days() -> None:
    h = [
        {"date": "2026-09-28", "green": True},
        {"date": "2026-09-29", "green": False},
        {"date": "2026-09-30", "green": True},
        {"date": "2026-10-01", "green": True},
        {"date": "2026-10-01", "green": True},
        {"date": "2026-10-02", "green": True},
    ]
    assert green_streak(h) == 3
    assert green_streak([*h, {"date": "2026-10-02", "green": False}]) == 0  # one red run spoils the day
    assert green_streak([{"date": "2026-09-30", "green": True}, {"date": "2026-10-02", "green": True}]) == 1
    assert green_streak([]) == 0


async def test_probe_verdicts(panel: FakeRemnawave) -> None:
    panel.add_user(username="real")
    cases = {READ_SCOPES: "read_only", ("*",): "writable", ("users:*",): "writable"}
    for scopes, verdict in cases.items():
        transport = Transport(TransportConfig(base_url=panel.url, token=panel.add_token(scopes)))
        try:
            probe = await probe_token(RemnawaveApi(transport), max_panel_id=1)
        finally:
            await transport.aclose()
        assert probe.verdict == verdict, scopes
    gone = panel.add_token()
    del panel.tokens[gone]
    bad = Transport(TransportConfig(base_url=panel.url, token=gone))
    try:
        assert (await probe_token(RemnawaveApi(bad), max_panel_id=1)).verdict == "auth"
    finally:
        await bad.aclose()
    patches = panel.calls("/users", "PATCH")
    assert patches and all(p.body == {"id": 1 + PROBE_OFFSET} for p in patches)  # never a real user id
    assert panel.users[1]["username"] == "real"


class _Attention:
    def __init__(self) -> None:
        self.items: dict[str, tuple[str, str]] = {}

    async def raise_item(self, dedup_key: str, severity: str, title: str, body: str, **_: Any) -> None:
        self.items[dedup_key] = (severity, title)

    async def resolve(self, dedup_key: str) -> None:
        self.items.pop(dedup_key, None)


async def test_service_pass_reads_only_and_reports(stand: Stand) -> None:
    posts: list[tuple[str, str]] = []
    calls: list[dict[str, Any]] = []
    attention = _Attention()

    async def post(kind: str, text: str) -> None:
        posts.append((kind, text))

    async def importer(**kw: Any) -> Mapping[str, Any]:
        calls.append(kw)
        return {"run_id": 7, "mode": kw["mode"], "green": True, "finished": True, "blocking": {}}

    service = ShadowService(
        target_dsn=stand.target,
        source_dsn=stand.source,
        api=lambda: stand.api,
        importer=importer,
        post=post,
        attention=attention,
    )
    stand.panel.requests.clear()
    report = await service.run("daily", as_of=stand.world.now)
    assert report.green, report.summary_text()
    assert calls == [{"mode": "shadow", "source_dsn": normalize_dsn(stand.source)[1]}]
    assert report.import_result == {
        "run_id": 7,
        "mode": "shadow",
        "green": True,
        "finished": True,
        "blocking": {},
    }
    # Zero writes to the panel: only reads plus the one probe, and the probe was refused (403).
    mutating = [r for r in stand.panel.requests if r.method != "GET"]
    assert [(r.method, r.path, r.body) for r in mutating] == [("PATCH", "/users", {"id": 5 + PROBE_OFFSET})]
    assert report.probe is not None and report.probe.status == 403
    assert posts and posts[0][0] == "system" and "С1 ✅" in posts[0][1] and "Writer: 0" in posts[0][1]
    assert attention.items == {}
    conn = await asyncpg.connect(stand.target)
    try:
        jobs = await conn.fetchval("SELECT count(*) FROM jobs")
        last = json.loads(await conn.fetchval("SELECT value::text FROM config_meta WHERE key = $1", LAST_KEY))
        history = json.loads(
            await conn.fetchval("SELECT value::text FROM config_meta WHERE key = $1", HISTORY_KEY)
        )
    finally:
        await conn.close()
    assert jobs == 0  # nothing enqueued for the writer
    assert last["green"] is True and last["streak"] == 1 and last["probe"]["verdict"] == "read_only"
    assert len(history) == 1 and history[0]["green"] is True

    await target_exec(stand, "UPDATE users SET wallet_minor = 1 WHERE id = 4")
    report = await service.run("daily", as_of=stand.world.now)
    assert report.red_codes == ["C2"] and report.streak == 0
    assert attention.items["shadow:red"][0] == "warn"
    assert "❌ Красные: С2" in posts[-1][1]


async def test_full_token_stops_shadow_before_anything(stand: Stand) -> None:
    attention = _Attention()
    called: list[Any] = []

    stopped: list[str] = []

    async def importer(**kw: Any) -> None:
        called.append(kw)

    async def stop_writer(reason: str) -> None:
        stopped.append(reason)

    transport = Transport(TransportConfig(base_url=stand.panel.url, token=stand.panel.add_token(("*",))))
    try:
        service = ShadowService(
            target_dsn=stand.target,
            source_dsn=stand.source,
            api=lambda: RemnawaveApi(transport),
            importer=importer,
            attention=attention,
            stop_writer=stop_writer,
        )
        report = await service.run("daily")
    finally:
        await transport.aclose()
    assert report.blocked and "не только для чтения" in report.blocked
    assert report.probe is not None and report.probe.writable and report.probe.status == 404
    assert called == [] and report.checks == [] and not report.green
    assert attention.items["shadow:token_writable"][0] == "error"
    assert stopped == [report.blocked]  # the stand's writer path is switched off (06 §4.1 p.3)


async def test_source_snapshot_is_read_only(stand: Stand) -> None:
    async with open_readonly(stand.source) as src:
        with pytest.raises(asyncpg.ReadOnlySQLTransactionError):
            await src.execute("UPDATE users SET balance_kopeks = 0")
    conn = await asyncpg.connect(stand.source)
    try:
        assert await conn.fetchval("SELECT balance_kopeks FROM users WHERE id = 1") == 50000
    finally:
        await conn.close()


async def test_missing_source_or_panel_blocks(target_dsn: str) -> None:
    service = ShadowService(target_dsn=target_dsn, source_dsn=None, api=lambda: None)
    report = await service.run()
    assert report.blocked and "панель не подключена" in report.blocked
    assert "⛔" in report.summary_text()


async def test_cli_shadow_run(stand: Stand, tmp_path: Path) -> None:
    import argparse
    import io as _io

    from svbg.importers.cutover import add_commands

    parser = argparse.ArgumentParser()
    add_commands(parser.add_subparsers(dest="command"))
    token = tmp_path / "token"
    token.write_text(stand.panel.add_token(READ_SCOPES))
    src = tmp_path / "src"
    src.write_text(stand.source)
    args = parser.parse_args(
        [
            "cutover",
            "shadow-run",
            "--source-dsn-file",
            str(src),
            "--panel-url",
            stand.panel.url,
            "--token-file",
            str(token),
            "--dsn",
            stand.target,
        ]
    )

    class Io:
        def __init__(self) -> None:
            self.out = _io.StringIO()

        def say(self, text: str = "") -> None:
            self.out.write(text + "\n")

        def warn(self, text: str) -> None:
            self.out.write(text + "\n")

    io = Io()
    code = await asyncio.to_thread(args.handler, args, {"DATA_DIR": str(tmp_path)}, io)
    assert code == 0, io.out.getvalue()
    assert "Все проверки С1–С12 зелёные" in io.out.getvalue()


# ------------------------------------------------------------------------------------------------ scale


async def test_green_at_production_scale(panel: FakeRemnawave, source_dsn: str, target_dsn: str) -> None:
    """~2.5k users / 2.1k subscriptions / 550 RollyPay rows (06 §2.12): green, and quick."""
    now = datetime.now(UTC).replace(microsecond=0)
    panel.add_internal_squad("NL", squad_uuid=BASE)
    n_users, n_subs = 2500, 2100
    expire = _s(now + timedelta(days=15))
    for i in range(1, n_subs + 1):
        panel.add_user(
            username=f"user_{10000 + i}",
            telegramId=10000 + i,
            expireAt=expire,
            activeInternalSquads=[BASE],
            tag="PAID",
            hwidDeviceLimit=5,
        )
    shorts = {uid: u["shortUuid"] for uid, u in panel.users.items()}
    src = await asyncpg.connect(source_dsn)
    try:
        await src.execute(
            """
            INSERT INTO users (id, telegram_id, auth_type, status, balance_kopeks, has_had_paid_subscription,
                               email_verified, auto_promo_group_assigned, auto_promo_group_threshold_kopeks,
                               promo_offer_discount_percent, has_made_first_topup, restriction_topup,
                               restriction_subscription, partner_status, created_at, referral_code)
            SELECT g, 10000 + g, 'telegram', 'active', (g % 7) * 1000, false, false, false, 0, 0, false,
                   false,
                   false, 'none', $1, 'c' || g
            FROM generate_series(1, $2) g
            """,
            now - timedelta(days=60),
            n_users,
        )
        await src.executemany(
            "INSERT INTO subscriptions (id, user_id, status, end_date, is_daily_paused, connected_squads, "
            "remnawave_id, remnawave_short_uuid, remnawave_short_id) VALUES ($1, $1, 'active', $2, false, "
            "$3, $6, $4, $5)",
            [(i, expire, json.dumps([BASE]), shorts[i], f"s{i}", i) for i in range(1, n_subs + 1)],
        )
        await src.execute(
            """
            INSERT INTO rollypay_payments (id, user_id, order_id, rollypay_payment_id, amount_kopeks, status,
                                           is_paid,
                                           created_at)
            SELECT g, g, 'rp' || g, 'RP' || g, 17900, 'paid', true, $1 FROM generate_series(1, 550) g
            """,
            now - timedelta(days=10),
        )
    finally:
        await src.close()
    dst = await asyncpg.connect(target_dsn)
    try:
        await dst.execute(
            "INSERT INTO payment_instances (provider, slug, title, config, webhook_token) "
            "VALUES ('rollypay', 'rollypay', 'R', 'enc:v1:x', 'enc:v1:y')"
        )
        await dst.execute(
            "INSERT INTO users (id, telegram_id, wallet_minor) SELECT g, 10000 + g, (g % 7) * 1000 "
            "FROM generate_series(1, $1) g",
            n_users,
        )
        await dst.execute(
            "INSERT INTO wallet_ledger (user_id, amount_minor, currency, balance_after, reason, ref_type, "
            "ref_id) "
            "SELECT id, wallet_minor, 'RUB', wallet_minor, 'import_opening', 'import_run', '1' FROM users "
            "WHERE wallet_minor > 0"
        )
        await dst.execute(
            "INSERT INTO trial_grants (user_id, telegram_id, source) "
            "SELECT id, telegram_id, 'import' FROM users WHERE id <= $1",
            n_subs,
        )
        await dst.executemany(
            "INSERT INTO subscriptions (id, user_id, link_state, panel_user_id, panel_short_uuid, "
            "paid_until, "
            "desired_expire_at, desired_squads, desired_device_limit, desired_tag, desired_traffic_bytes, "
            "desired_reset_strategy) VALUES ($1, $1, 'linked', $1, $2, $3, $3, $4::jsonb, 5, 'PAID', 0, "
            "'NO_RESET')",
            [(i, shorts[i], expire, json.dumps([BASE])) for i in range(1, n_subs + 1)],
        )
        await dst.execute(
            "INSERT INTO payments (instance_id, user_id, external_id, merchant_ref, amount_minor, currency, "
            "status, "
            "is_imported, paid_amount_minor, paid_at) SELECT 1, g, 'RP' || g, 'rp' || g, 17900, 'RUB', "
            "'paid', true, "
            "17900, now() FROM generate_series(1, 550) g"
        )
        await dst.execute(
            "INSERT INTO referral_codes (user_id, code, source) SELECT id, 'c' || id, 'import' FROM users"
        )
    finally:
        await dst.close()
    transport = Transport(TransportConfig(base_url=panel.url, token=panel.add_token(READ_SCOPES)))
    try:
        api = RemnawaveApi(transport)
        loop = asyncio.get_running_loop()
        started = loop.time()
        index = await PanelIndex.load(api)
        async with open_readonly(source_dsn) as s, open_readonly(target_dsn) as d:
            rec = Reconciliation(s, d, index, as_of=now)
            checks = await rec.run()
        elapsed = loop.time() - started
    finally:
        await transport.aclose()
    assert {c.code: c.problems[:3] for c in checks if c.status != "ok"} == {}
    assert by_code(checks)["C2"].facts["users"] == n_users
    assert len(panel.calls("/users/stream", "GET")) == 5  # 2100 users = 5 pages of 500
    assert elapsed < 60
