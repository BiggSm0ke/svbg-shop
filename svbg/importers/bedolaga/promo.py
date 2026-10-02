"""Promo codes and their uses (06 §2.6): the mapping is :func:`svbg.promo.legacy.from_bedolaga` (6 Bedolaga
types; ``promo_group`` is not carried over), codes are kept as typed, upserted by ``(source='import',
legacy_id)``. ``promocode_uses`` → ``promo_uses(source='import')`` (without them a code could be used again),
then ``uses = COUNT(promo_uses)`` for the imported codes; a difference with Bedolaga's ``current_uses`` is
reported. A code that collides (case-insensitively) with a code the owner created in the bot is reported and
skipped.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import sqlalchemy as sa

from svbg.importers.bedolaga.plan import chunks
from svbg.promo.legacy import from_bedolaga
from svbg.promo.rules import PromoError
from svbg.promo.service import PromoService
from svbg.promo.tables import promo_uses, promocodes

if TYPE_CHECKING:
    from svbg.importers.bedolaga.plan import Ctx

__all__ = ["check", "run"]


async def run(ctx: Ctx) -> None:
    rep = ctx.report
    rows = await ctx.src.rows(
        "promocodes",
        [
            "id",
            "code",
            "type",
            "balance_bonus_kopeks",
            "subscription_days",
            "max_uses",
            "current_uses",
            "valid_from",
            "valid_until",
            "is_active",
            "first_purchase_only",
            "tariff_id",
        ],
    )
    plan_id = ctx.plan.id if ctx.plan else None
    taken = {
        str(code).lower(): (src, legacy)
        for code, src, legacy in (
            await ctx.conn.execute(sa.select(promocodes.c.code, promocodes.c.source, promocodes.c.legacy_id))
        ).all()
    }
    source_uses: dict[int, int] = {}
    for r in rows:
        rep.inc("promo", "source")
        try:
            legacy = from_bedolaga(r, currency=ctx.cfg.currency, plan_of=lambda _t: plan_id)
        except PromoError as exc:
            rep.issue("promo_not_imported", promo_id=r["id"], code=r["code"], type=r["type"], reason=str(exc))
            rep.skip("promocodes", int(r["id"]))
            continue
        other = taken.get(legacy.code.lower())
        if other is not None and other != ("import", legacy.legacy_id):
            rep.issue("promo_code_taken", promo_id=r["id"], code=legacy.code)
            rep.skip("promocodes", int(r["id"]))
            continue
        new_id = await PromoService.import_legacy(ctx.conn, legacy)
        ctx.promos[int(r["id"])] = new_id
        taken[legacy.code.lower()] = ("import", legacy.legacy_id)
        source_uses[new_id] = int(r["current_uses"] or 0)
        rep.inc("promo", "imported")
    await ctx.remember("promo", [(old, new, {}) for old, new in ctx.promos.items()])

    uses = await ctx.src.rows("promocode_uses", ["id", "promocode_id", "user_id", "used_at"])
    mapped = await ctx.mapped("promo_use")
    new_rows: list[dict[str, Any]] = []
    for u in uses:
        rep.inc("promo", "uses_source")
        if str(u["id"]) in mapped:
            continue
        pid = ctx.promos.get(int(u["promocode_id"]))
        if pid is None or int(u["user_id"]) not in ctx.users:
            rep.issue(
                "promo_use_skipped", use_id=u["id"], promocode_id=u["promocode_id"], user_id=u["user_id"]
            )
            rep.skip("promocode_uses", int(u["id"]))
            continue
        new_rows.append(
            {
                "promo_id": pid,
                "user_id": int(u["user_id"]),
                "source": "import",
                "effect": {"legacy_id": int(u["id"])},
                "used_at": u["used_at"] or ctx.t0,
            }
        )
    remember: list[tuple[Any, Any, dict[str, Any]]] = []
    for chunk in chunks(new_rows, 1000):
        ids = (
            (await ctx.conn.execute(sa.insert(promo_uses).values(chunk).returning(promo_uses.c.id)))
            .scalars()
            .all()
        )
        remember.extend((r["effect"]["legacy_id"], i, {}) for r, i in zip(chunk, ids, strict=True))
    await ctx.remember("promo_use", remember)
    rep.inc("promo", "uses_imported", len(remember))

    if ctx.promos:
        counted = (
            sa.select(sa.func.count())
            .where(promo_uses.c.promo_id == promocodes.c.id)
            .correlate(promocodes)
            .scalar_subquery()
        )
        await ctx.conn.execute(
            sa.update(promocodes).where(promocodes.c.id.in_(list(ctx.promos.values()))).values(uses=counted)
        )
        now_uses = dict(
            (
                await ctx.conn.execute(
                    sa.select(promocodes.c.id, promocodes.c.uses).where(
                        promocodes.c.id.in_(list(ctx.promos.values()))
                    )
                )
            ).all()
        )
        for pid, was in source_uses.items():
            if int(now_uses.get(pid, 0)) != was:
                rep.issue("promo_uses_differ", promo_id=pid, bedolaga=was, counted=int(now_uses.get(pid, 0)))


async def check(ctx: Ctx) -> None:
    """С1 (promo part): imported codes and uses equal the source minus the reported exclusions."""
    rep = ctx.report
    expected = (
        rep.get("promo", "source")
        - rep.issue_totals.get("promo_not_imported", 0)
        - rep.issue_totals.get("promo_code_taken", 0)
    )
    uses = rep.get("promo", "uses_source") - rep.issue_totals.get("promo_use_skipped", 0)
    present = int(
        await ctx.conn.scalar(
            sa.select(sa.func.count()).select_from(promocodes).where(promocodes.c.source == "import")
        )
        or 0
    )
    present_uses = int(
        await ctx.conn.scalar(
            sa.select(sa.func.count()).select_from(promo_uses).where(promo_uses.c.source == "import")
        )
        or 0
    )
    rep.part("C1", "promocodes", expected=expected, present=present)
    rep.part("C1", "promo_uses", expected=uses, present=present_uses)
