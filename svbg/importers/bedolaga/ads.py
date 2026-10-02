"""Advertising campaigns (06 §2.7): ``advertising_campaigns`` → ``ad_links`` (``code = start_parameter`` as
is,
the old ``?start=<code>`` links keep working), the bonus is kept for reference only (it was already given);
``advertising_campaign_registrations`` → ``ad_link_users`` first touch (earliest registration wins, no bonus
again); a user without a registration but with ``pending_campaign_slug`` gets that link. Both writes go
through :class:`svbg.ads.service.AdService` (upsert by ``(source='import', legacy_id)``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa

from svbg.ads.service import AdError, AdService
from svbg.ads.tables import ad_link_users, ad_links

if TYPE_CHECKING:
    from svbg.importers.bedolaga.plan import Ctx

__all__ = ["check", "run"]


async def run(ctx: Ctx) -> None:
    rep = ctx.report
    rows = await ctx.src.rows(
        "advertising_campaigns",
        [
            "id",
            "name",
            "start_parameter",
            "bonus_type",
            "balance_bonus_kopeks",
            "subscription_duration_days",
            "subscription_traffic_gb",
            "subscription_device_limit",
            "subscription_squads",
            "tariff_id",
            "tariff_duration_days",
            "is_active",
            "partner_user_id",
            "created_at",
        ],
    )
    taken = {
        str(code): (src, legacy)
        for code, src, legacy in (
            await ctx.conn.execute(sa.select(ad_links.c.code, ad_links.c.source, ad_links.c.legacy_id))
        ).all()
    }
    by_code: dict[str, int] = {}
    for r in rows:
        rep.inc("ads", "source")
        row = dict(r)
        row["device_limit"] = r["subscription_device_limit"]
        row["squads"] = r["subscription_squads"]
        code = str(r["start_parameter"] or "").strip()
        other = taken.get(code)
        if other is not None and other != ("import", str(r["id"])):
            rep.issue("campaign_code_taken", campaign_id=r["id"], code=code)
            rep.skip("campaigns", int(r["id"]))
            continue
        try:
            new_id = await AdService.import_campaign(ctx.conn, row)
        except AdError as exc:
            rep.issue("campaign_not_imported", campaign_id=r["id"], code=code, reason=str(exc))
            rep.skip("campaigns", int(r["id"]))
            continue
        ctx.ad_links[int(r["id"])] = new_id
        by_code[code] = new_id
        rep.inc("ads", "imported")
    await ctx.remember("ad_link", [(old, new, {}) for old, new in ctx.ad_links.items()])

    regs = await ctx.src.rows(
        "advertising_campaign_registrations", ["id", "campaign_id", "user_id", "created_at"], order="id"
    )
    regs.sort(key=lambda r: (r["created_at"] or ctx.t0, r["id"]))
    registered: set[int] = set()
    for r in regs:
        rep.inc("ads", "registrations_source")
        link = ctx.ad_links.get(int(r["campaign_id"]))
        if link is None or int(r["user_id"]) not in ctx.users:
            rep.issue(
                "registration_skipped",
                registration_id=r["id"],
                campaign_id=r["campaign_id"],
                user_id=r["user_id"],
            )
            continue
        registered.add(int(r["user_id"]))
        if await AdService.import_registration(
            ctx.conn, user_id=int(r["user_id"]), ad_link_id=link, attached_at=r["created_at"]
        ):
            rep.inc("ads", "registrations_attached")
        else:
            rep.inc("ads", "registrations_repeat")
    for uid, u in ctx.users.items():
        slug = u.get("pending_campaign_slug")
        link = by_code.get(str(slug)) if slug else None
        if link is None or uid in registered:
            continue
        registered.add(uid)
        if await AdService.import_registration(ctx.conn, user_id=uid, ad_link_id=link):
            rep.inc("ads", "pending_slug_attached")
    rep.set("ads", "registered_users", len(registered))


async def check(ctx: Ctx) -> None:
    """С1 (campaign part): links, and registrations per campaign (first touch per user)."""
    rep = ctx.report
    expected = (
        rep.get("ads", "source")
        - rep.issue_totals.get("campaign_code_taken", 0)
        - rep.issue_totals.get("campaign_not_imported", 0)
    )
    present = 0
    if ctx.ad_links:
        present = int(
            await ctx.conn.scalar(
                sa.select(sa.func.count())
                .select_from(ad_links)
                .where(ad_links.c.id.in_(list(ctx.ad_links.values())))
            )
            or 0
        )
    rep.part("C1", "ad_links", expected=expected, present=present)
    users_with_reg = int(
        await ctx.conn.scalar(
            sa.select(sa.func.count())
            .select_from(ad_link_users)
            .where(
                ad_link_users.c.ad_link_id.in_(list(ctx.ad_links.values()) or [0]),
                ad_link_users.c.source == "import",
            )
        )
        or 0
    )
    rep.part("C1", "ad_registrations", expected=rep.get("ads", "registered_users"), present=users_with_reg)
