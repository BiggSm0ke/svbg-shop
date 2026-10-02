"""Promo test kit: the promo tables attached to the shared metadata only while a promo test runs (until
integration registers ``svbg.promo.tables`` in ``TABLE_MODULES``), seeding helpers and a catalog snapshot."""

from __future__ import annotations

import itertools
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa

from svbg.catalog.service import CatalogSnapshot, build_snapshot
from svbg.db import schema
from svbg.db.meta import metadata
from svbg.promo import tables as promo_tables
from svbg.subscriptions.trial import TrialService
from tests.dbkit import CountingDatabase

SQUAD = "11111111-1111-4111-8111-111111111111"
PRICES = {30: 17_900, 90: 49_900}
TABLES: tuple[sa.Table, ...] = (promo_tables.promocodes, promo_tables.promo_uses, promo_tables.promo_pending)
_REGISTERED = "svbg.promo.tables" in schema.TABLE_MODULES
_PANEL_IDS = itertools.count(50_000)


def detach_tables(tables: Sequence[sa.Table] = TABLES, *, registered: bool = _REGISTERED) -> None:
    if registered:
        return
    for table in reversed(tables):
        if table.name in metadata.tables:
            metadata.remove(table)


def attach_tables(tables: Sequence[sa.Table] = TABLES) -> None:
    for table in tables:
        if table.name not in metadata.tables:
            metadata._add_table(table.name, table.schema, table)


detach_tables()


def plan_rows(plan_ids: Sequence[int] = (1, 2), *, trial_id: int | None = 9) -> tuple[list[Any], list[Any]]:
    plans: list[dict[str, Any]] = []
    prices: list[dict[str, Any]] = []
    for pid in (*plan_ids, *((trial_id,) if trial_id else ())):
        plans.append(
            {
                "id": pid,
                "code": f"plan{pid}",
                "name": {"ru": f"Тариф {pid}"},
                "availability": "all",
                "is_trial": pid == trial_id,
                "enabled": True,
                "traffic_bytes": 0,
                "reset_strategy": "NO_RESET",
                "device_limit": 5,
                "squads": [SQUAD],
                "ext_squad": None,
                "panel_tag": None,
                "traffic_on_renew": "reset",
                "devices_on_renew": "keep",
                "device_addon": None,
                "broken_reason": None,
                "sort": pid,
                "version": 1,
            }
        )
        if pid != trial_id:
            prices.extend(
                {"plan_id": pid, "days": d, "currency": "RUB", "amount_minor": p, "highlight": d == 30}
                for d, p in PRICES.items()
            )
    return plans, prices


@dataclass
class FakeCatalog:
    snapshot: CatalogSnapshot

    @classmethod
    def build(cls) -> FakeCatalog:
        plans, prices = plan_rows()
        return cls(build_snapshot(plans, prices, [], version=1))

    async def trial_plan(self, conn: Any) -> Any:
        plan = self.snapshot.trial
        return plan.snapshot() if plan is not None else None


def trial_service(db: CountingDatabase, catalog: FakeCatalog, days: int = 3) -> TrialService:
    return TrialService(db, catalog, config=lambda: {"TRIAL_DAYS": days, "TRIAL_AUDIENCE": "all"})


async def add_user(db: CountingDatabase, tg_id: int, *, wallet: int = 0, role: str = "user") -> int:
    rows = await db.raw(
        "insert into users (telegram_id, role, wallet_minor) values ($1, $2, $3) returning id",
        tg_id,
        role,
        wallet,
    )
    return int(rows[0]["id"])


async def add_sub(
    db: CountingDatabase,
    user_id: int,
    *,
    plan_id: int | None = 1,
    is_trial: bool = False,
    days_left: float = 10,
    link_state: str = "linked",
) -> int:
    until = datetime.now(UTC) + timedelta(days=days_left)
    snapshot = {"plan_id": plan_id, "code": f"plan{plan_id}", "squads": [SQUAD], "device_limit": 5}
    rows = await db.raw(
        "insert into subscriptions (user_id, plan_id, plan_snapshot, link_state, paid_until, "
        "desired_expire_at, desired_squads, is_trial, panel_user_id) "
        "values ($1, $2, $3::jsonb, $4, $5, $5, $6::jsonb, $7, $8) returning id",
        user_id,
        plan_id,
        json.dumps(snapshot),
        link_state,
        until,
        json.dumps([SQUAD]),
        is_trial,
        next(_PANEL_IDS) if link_state == "linked" else None,
    )
    return int(rows[0]["id"])


async def add_paid_order(db: CountingDatabase, user_id: int, total: int = 17_900) -> int:
    rows = await db.raw(
        "insert into orders (user_id, kind, status, currency, total_minor) "
        "values ($1, 'new', 'fulfilled', 'RUB', $2) returning id",
        user_id,
        total,
    )
    return int(rows[0]["id"])


async def add_promo(db: CountingDatabase, code: str, kind: str, **cols: Any) -> int:
    values: dict[str, Any] = {"code": code, "kind": kind, **cols}
    if "plan_ids" in values:
        values["plan_ids"] = json.dumps(values["plan_ids"])
    names = ", ".join(values)
    params = ", ".join(f"${i}::jsonb" if k == "plan_ids" else f"${i}" for i, k in enumerate(values, 1))
    rows = await db.raw(f"insert into promocodes ({names}) values ({params}) returning id", *values.values())
    return int(rows[0]["id"])
