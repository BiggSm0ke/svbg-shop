"""Catalog (06 §2.3): ``server_squads`` → ``locations``, the classic tariff (``.env`` prices and limits, the
``tariffs`` row as a fallback) → one paid plan + ``plan_prices`` (30/90/180/360; 14 and 60 are not shown in
Bedolaga and not carried over), the paid extra-device option, and the twin squads of LTE (``wlq_squads``)
used to map a panel's squad set back to the plan's (a twin is never imported as a base squad, R4).

Trial locations (``server_squads.is_trial_eligible``, 06 §2.3): SvBG has no per-location trial flag — the
trial is granted from the one ``is_trial`` plan (``svbg.catalog.service.CatalogService.trial``), so the flag
becomes that plan's ``squads``. The trial plan itself (limits, tag) belongs to the settings import /
the catalog preset; here only its squads are aligned, and never over a change made on the stand (the set the
importer wrote last is remembered in ``legacy_id_map(entity='trial_plan')``; a different current set is
reported as ``trial_squads_changed``).

Existing rows of the owner are never overwritten otherwise: locations keep their titles, an existing price
that differs is reported.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Final

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from svbg.catalog.tables import RESET_STRATEGIES, locations, plan_prices, plans
from svbg.importers.bedolaga.plan import PlanInfo
from svbg.remnawave.contributors import SquadContributors

if TYPE_CHECKING:
    from svbg.importers.bedolaga.plan import Ctx

__all__ = ["DEFAULT_PERIODS", "GIB", "PLAN_CODE", "flag_emoji", "run"]

GIB: Final = 1024**3
PLAN_CODE: Final = "bedolaga_classic"
DEFAULT_PERIODS: Final = (30, 90, 180, 360)
_TAG_OK: Final = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


def flag_emoji(country: str | None) -> str | None:
    code = (country or "").strip().upper()
    if len(code) != 2 or not code.isalpha():
        return None
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in code)


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


async def run(ctx: Ctx) -> None:
    rep = ctx.report
    squads = await ctx.src.rows(
        "server_squads",
        [
            "id",
            "squad_uuid",
            "display_name",
            "original_name",
            "country_code",
            "is_available",
            "is_trial_eligible",
            "price_kopeks",
            "sort_order",
        ],
    )
    ctx.squad_by_server = {int(r["id"]): str(r["squad_uuid"]) for r in squads if r["squad_uuid"]}
    twins = await ctx.src.rows("wlq_squads", ["id", "kind", "base_squad_uuid", "panel_uuid"])
    ctx.twins = {
        str(r["panel_uuid"]): str(r["base_squad_uuid"])
        for r in twins
        if r["kind"] == "twin" and r["panel_uuid"] and r["base_squad_uuid"]
    }
    ctx.twins.update(await SquadContributors.twins(ctx.conn))

    loc_rows = []
    for r in squads:
        if not r["squad_uuid"]:
            continue
        title = str(r["display_name"] or r["original_name"] or r["squad_uuid"])
        loc_rows.append(
            {
                "squad_uuid": str(r["squad_uuid"]),
                "title": {"ru": title},
                "flag": flag_emoji(r["country_code"]),
                "sort": int(r["sort_order"] or 0),
                "panel_name": str(r["original_name"] or r["display_name"] or ""),
            }
        )
        if (r["price_kopeks"] or 0) > 0:
            rep.issue("location_paid", squad_uuid=r["squad_uuid"], price_kopeks=r["price_kopeks"])
    if loc_rows:
        stmt = pg_insert(locations).values(loc_rows)
        res = await ctx.conn.execute(
            stmt.on_conflict_do_update(
                index_elements=[locations.c.squad_uuid],
                set_={
                    "title": sa.case(
                        (locations.c.title == sa.text("'{}'::jsonb"), stmt.excluded.title),
                        else_=locations.c.title,
                    ),
                    "flag": sa.func.coalesce(locations.c.flag, stmt.excluded.flag),
                },
            ).returning(locations.c.squad_uuid)
        )
        rep.set("catalog", "locations", len(res.all()))
    await ctx.remember(
        "location",
        [
            (
                r["squad_uuid"],
                r["squad_uuid"],
                {"trial": bool(r["is_trial_eligible"]), "available": r["is_available"]},
            )
            for r in squads
            if r["squad_uuid"]
        ],
    )
    trial_squads = [str(r["squad_uuid"]) for r in squads if r["is_trial_eligible"] and r["squad_uuid"]]
    rep.set("catalog", "trial_locations", len(trial_squads))

    tariffs = await ctx.src.rows(
        "tariffs",
        [
            "id",
            "name",
            "is_active",
            "traffic_limit_gb",
            "device_limit",
            "device_price_kopeks",
            "max_device_limit",
            "allowed_squads",
            "period_prices",
            "traffic_reset_mode",
            "external_squad_uuid",
            "highlight_period_days",
            "panel_tag",
            "display_order",
        ],
        order="display_order",
    )
    await _plan(ctx, squads, tariffs[0] if tariffs else None)
    await _trial_locations(ctx, trial_squads)


async def _trial_locations(ctx: Ctx, trial_squads: list[str]) -> None:
    """``is_trial_eligible`` → the squads of the trial plan (see the module docstring)."""
    rep = ctx.report
    wanted = [ctx.twins.get(s, s) for s in dict.fromkeys(trial_squads)]
    if not wanted:
        return
    row = (
        await ctx.conn.execute(sa.select(plans.c.id, plans.c.squads).where(plans.c.is_trial.is_(True)))
    ).first()
    if row is None:
        rep.issue("trial_plan_missing", trial_locations=len(wanted))
        return
    current = [str(s) for s in (row.squads or [])]
    written = (await ctx.mapped("trial_plan")).get("trial")
    last = list(written[1].get("squads") or []) if written is not None else None
    if current == wanted:
        rep.inc("catalog", "trial_squads_kept")
    elif last is not None and current != last:
        rep.issue("trial_squads_changed", plan_id=int(row.id), stand=current, bedolaga=wanted)
        return
    else:
        from svbg.catalog.repo import update_plan

        await update_plan(ctx.conn, int(row.id), audit_action="plan.import", squads=wanted)
        rep.inc("catalog", "trial_squads_set")
    await ctx.remember("trial_plan", [("trial", int(row.id), {"squads": wanted})])


async def _plan(ctx: Ctx, squads: list[dict[str, Any]], tariff: dict[str, Any] | None) -> None:
    st, rep = ctx.settings, ctx.report
    allowed = [s for s in (_json(tariff["allowed_squads"]) or []) if isinstance(s, str)] if tariff else []
    plan_squads = allowed or [
        str(r["squad_uuid"])
        for r in sorted(squads, key=lambda r: (r["sort_order"] or 0, r["id"]))
        if r["squad_uuid"] and r["is_available"] is not False
    ]
    plan_squads = [ctx.twins.get(s, s) for s in dict.fromkeys(plan_squads)]
    strategy = str(
        st.get("DEFAULT_TRAFFIC_RESET_STRATEGY") or (tariff or {}).get("traffic_reset_mode") or "NO_RESET"
    )
    strategy = strategy.upper() if strategy.upper() in RESET_STRATEGIES else "NO_RESET"
    devices = st.int("DEFAULT_DEVICE_LIMIT", (tariff or {}).get("device_limit"))
    max_devices = st.int("MAX_DEVICES_LIMIT", (tariff or {}).get("max_device_limit"))
    per_device = st.int("PRICE_PER_DEVICE", (tariff or {}).get("device_price_kopeks")) or 0
    tag = st.get("PAID_SUBSCRIPTION_USER_TAG") or (tariff or {}).get("panel_tag")
    if tag and not (len(tag) <= 16 and set(tag) <= _TAG_OK):
        rep.issue("plan_tag_invalid", tag=tag)
        tag = None
    traffic_gb = st.int("FIXED_TRAFFIC_LIMIT_GB", (tariff or {}).get("traffic_limit_gb")) or 0
    addon: dict[str, Any] = {}
    if per_device > 0:
        addon = {"price_minor": per_device, "per_days": 30, "currency": ctx.cfg.currency}
        if max_devices:
            addon["max_devices"] = max_devices
    values: dict[str, Any] = {
        "code": PLAN_CODE,
        "name": {"ru": str((tariff or {}).get("name") or "Подписка")},
        "availability": "all",
        "is_trial": False,
        "enabled": bool(plan_squads),
        "traffic_bytes": traffic_gb * GIB,
        "reset_strategy": strategy,
        "device_limit": devices,
        "squads": plan_squads,
        "ext_squad": (tariff or {}).get("external_squad_uuid"),
        "panel_tag": tag,
        "traffic_on_renew": "keep",
        "devices_on_renew": "reset" if st.bool("RESET_DEVICES_ON_RENEWAL") else "keep",
        "device_addon": addon,
    }
    mapped = await ctx.mapped("plan")
    plan_id: int | None = int(mapped["classic"][0]) if "classic" in mapped else None
    if plan_id is not None and not await ctx.conn.scalar(sa.select(plans.c.id).where(plans.c.id == plan_id)):
        plan_id = None
    if plan_id is None:
        plan_id = await ctx.conn.scalar(sa.select(plans.c.id).where(plans.c.code == PLAN_CODE))
    if plan_id is None:
        plan_id = int(await ctx.conn.scalar(sa.insert(plans).values(**values).returning(plans.c.id)))
        rep.inc("catalog", "plans_created")
    else:
        rep.inc("catalog", "plans_kept")
    await ctx.remember("plan", [("classic", plan_id, {"code": PLAN_CODE})])
    ctx.plan = PlanInfo(int(plan_id), devices, plan_squads)

    prices = _prices(ctx, tariff)
    if not prices:
        rep.issue("plan_without_prices")
    existing = {
        int(r[0]): int(r[1])
        for r in (
            await ctx.conn.execute(
                sa.select(plan_prices.c.days, plan_prices.c.amount_minor).where(
                    plan_prices.c.plan_id == plan_id, plan_prices.c.currency == ctx.cfg.currency
                )
            )
        ).all()
    }
    highlight = (tariff or {}).get("highlight_period_days")
    new = []
    for days, amount in prices.items():
        if days in existing:
            if existing[days] != amount:
                rep.issue("price_differs", days=days, bedolaga=amount, current=existing[days])
            continue
        new.append(
            {
                "plan_id": plan_id,
                "days": days,
                "currency": ctx.cfg.currency,
                "amount_minor": amount,
                "highlight": highlight == days,
            }
        )
    if new:
        await ctx.conn.execute(sa.insert(plan_prices).values(new))
    rep.set("catalog", "prices", len(prices))


def _prices(ctx: Ctx, tariff: dict[str, Any] | None) -> dict[int, int]:
    st = ctx.settings
    periods = st.ints("AVAILABLE_SUBSCRIPTION_PERIODS") or list(DEFAULT_PERIODS)
    out: dict[int, int] = {}
    for days in periods:
        amount = st.int(f"PRICE_{days}_DAYS")
        if amount and amount > 0 and 1 <= days <= 3650:
            out[days] = amount
    if out:
        return dict(sorted(out.items()))
    table = _json((tariff or {}).get("period_prices")) or {}
    if isinstance(table, dict):
        for key, amount in table.items():
            try:
                days, value = int(key), int(amount)
            except (TypeError, ValueError):
                continue
            if value > 0 and 1 <= days <= 3650:
                out[days] = value
    return dict(sorted(out.items()))
