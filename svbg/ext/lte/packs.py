"""LTE quotas: packs of gigabytes (O7, 05 §2.1.11) through the core order pipeline and the wallet model.

* :func:`availability` — **one** function for the button, the screen, the checkout step and the order (the
  owner's order of refusals is kept; ``insufficient_for_access`` is a warning, not a refusal). The coverage
  rule is a margin (``paid_until > now + LTE_TOPUP_MIN_COVERAGE_HOURS``), never "covers the boundary"
  (lesson ``72fae00f7``).
* :func:`load_facts` — everything ``availability`` and the screens need in **one** SQL (packs included).
* :func:`create_order` — an ``addon_lte`` order in ``draft`` with a frozen snapshot ``{group, period,
  planned_end_at, gb, amount}``; «Оплатить» is the core's: from the balance at once, or ``awaiting_funds`` and
  auto-complete in the transaction that credits the top-up. The module has no carts of its own.
* :class:`AddonLteKind` (order kind) — in the fulfill transaction: same period and boundary not passed, then
  ``availability`` again; a refusal raises ``SubscriptionError`` and the core refunds the whole order to the
  wallet with the reason shown to the user. Otherwise ``lte_credits`` (``order_id`` UNIQUE) and the block is
  released at once (decision for this subscription).
* :class:`LtePackItem` (order item ``lte_pack`` of a plan purchase or renewal, X4) — runs **after** the term
  was applied in the same fulfill: the term events are processed synchronously, so the pack lands in the
  right (possibly new) period; no period → only the item's money returns to the wallet.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import sqlalchemy as sa

from svbg.billing import wallet
from svbg.billing.tables import order_items, orders
from svbg.core.clock import now
from svbg.ext.lte.decide import EffectiveLimit, Override, SubjectInput, effective_limit
from svbg.ext.lte.model import group_limit_rows
from svbg.ext.lte.tables import (
    lte_anchors,
    lte_blocks,
    lte_credits,
    lte_groups,
    lte_overrides,
    lte_packs,
    lte_period_usage,
    lte_periods,
    lte_twins,
)
from svbg.subscriptions import journal
from svbg.subscriptions.lifecycle import SubscriptionError
from svbg.subscriptions.tables import subscriptions

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from svbg.ext.lte.service import LteConfig, LteService

__all__ = [
    "ITEM_TYPE",
    "ORDER_KIND",
    "REFUSALS",
    "REFUSALS_EN",
    "AddonLteKind",
    "Availability",
    "LtePackItem",
    "Pack",
    "PackFacts",
    "addon_kind",
    "attach_to_order",
    "availability",
    "create_order",
    "load_facts",
    "offer_for",
    "order_packs",
    "pack_item",
]

log = logging.getLogger("svbg.ext.lte.packs")

ORDER_KIND: Final = "addon_lte"
ITEM_TYPE: Final = "lte_pack"
REFUND_REASON: Final = "purchase_refund"  # integration: ``lte_refund`` once the wallet knows it
ANCHORS_PAID: Final = frozenset({"paid", "admin", "manual", "import"})
_PURCHASE_KINDS: Final = ("purchase_new", "purchase_renew", "plan_changed", "trial_converted")

#: Codes in the owner's order (05 §2.1.11); ``None`` text — no reason is shown, the button is just absent.
REFUSALS: Final[Mapping[str, str | None]] = {
    "exempt": "У вас нет ограничения трафика на серверах LTE.",
    "feature_off": None,
    "no_packages": None,
    "not_enforced": "Сейчас лимит не действует — докупать не нужно.",
    "no_subscription": "Докупка доступна для оплаченной подписки.",
    "trial": "На пробном периоде докупка недоступна.",
    "group_unavailable": "Серверы LTE недоступны на вашем тарифе.",
    "no_group_rights": "Серверы LTE недоступны на вашем тарифе.",
    "unlimited": "Серверы LTE недоступны на вашем тарифе.",
    "zero_limit": "Серверы LTE недоступны на вашем тарифе.",
    "no_period": None,
    "expiring": "Подписка закончится {date} — сначала продлите её.",
    "period_changed": "Предложение устарело — откройте докупку заново.",
    "manual_block": "Доступ ограничен администратором — напишите в поддержку.",
    "frozen": "Подписка заморожена — докупка недоступна.",
    "package_disabled": "Этот пакет больше не продаётся.",
}
#: English of :data:`REFUSALS` (same codes and placeholders).
REFUSALS_EN: Final[Mapping[str, str | None]] = {
    "exempt": "You have no traffic limit on LTE servers.",
    "feature_off": None,
    "no_packages": None,
    "not_enforced": "The limit is not in effect right now — no need to buy more.",
    "no_subscription": "Buying more traffic is available for a paid subscription.",
    "trial": "Buying more traffic is not available on the trial.",
    "group_unavailable": "LTE servers are not available on your plan.",
    "no_group_rights": "LTE servers are not available on your plan.",
    "unlimited": "LTE servers are not available on your plan.",
    "zero_limit": "LTE servers are not available on your plan.",
    "no_period": None,
    "expiring": "Your subscription ends on {date} — renew it first.",
    "period_changed": "The offer is outdated — open the top-up again.",
    "manual_block": "Access is restricted by an administrator — please contact support.",
    "frozen": "Your subscription is on hold — buying more traffic is not available.",
    "package_disabled": "This pack is no longer on sale.",
}


@dataclass(frozen=True, slots=True)
class Pack:
    id: int
    gb: int
    amount_minor: int
    currency: str


@dataclass(frozen=True, slots=True)
class PackFacts:
    """A subscription × group as availability sees it (one row of :func:`load_facts`)."""

    subscription_id: int
    user_id: int | None
    group_id: int
    group_name: Mapping[str, Any]
    group_state: str
    group_enforce: bool
    panel_user_id: int | None = None
    paid_until: datetime | None = None
    frozen: bool = False
    period_id: int | None = None
    period_state: str | None = None
    period_is_trial: bool = False
    planned_end_at: datetime | None = None
    anchor_kind: str | None = None
    rights: bool = False
    used_bytes: int = 0
    credit_bytes: int = 0
    limit: EffectiveLimit = field(default_factory=lambda: EffectiveLimit(found=False, base=None))
    exempt: bool = False
    block_reason: str | None = None  # live enforce block
    block_status: str | None = None
    packs: tuple[Pack, ...] = ()

    @property
    def blocked(self) -> bool:
        return self.block_reason is not None and self.block_status == "active"

    def overage(self) -> int:
        limit = self.limit.limit
        return 0 if limit is None else max(0, self.used_bytes - limit)


@dataclass(frozen=True, slots=True)
class Availability:
    ok: bool
    code: str = "ok"
    text: str | None = None
    insufficient: bool = False  # ok, but the pack does not return access (warning + «Всё равно купить»)
    text_en: str | None = field(default=None, compare=False)

    @classmethod
    def refuse(cls, code: str, **fmt: str) -> Availability:
        text = REFUSALS.get(code)
        text_en = REFUSALS_EN.get(code)
        return cls(
            False,
            code,
            text.format(**fmt) if text else None,
            text_en=text_en.format(**fmt) if text_en else None,
        )

    def localized(self, lang: str | None) -> str | None:
        """The refusal text in ``lang`` (Russian fallback)."""
        return (self.text_en or self.text) if lang == "en" else self.text


def _pilot_ok(cfg: LteConfig, facts: PackFacts) -> bool:
    return not cfg.pilot or (facts.panel_user_id or 0) in cfg.pilot


def availability(
    facts: PackFacts,
    cfg: LteConfig,
    *,
    at: datetime,
    pack: Pack | None = None,
    projected_coverage: datetime | None = None,
    gb_bytes: int | None = None,
) -> Availability:
    """``Ok | Refusal(code)`` in the owner's order of checks (05 §2.1.11)."""
    if facts.exempt:
        return Availability.refuse("exempt")
    if not cfg.topup_enabled:
        return Availability.refuse("feature_off")
    if not facts.packs:
        return Availability.refuse("no_packages")
    if cfg.mode != "on" or not facts.group_enforce or not _pilot_ok(cfg, facts):
        return Availability.refuse("not_enforced")
    if facts.period_id is None and facts.paid_until is None:
        return Availability.refuse("no_subscription")
    if facts.period_is_trial or (facts.anchor_kind is not None and facts.anchor_kind not in ANCHORS_PAID):
        return Availability.refuse("trial")
    if facts.group_state != "active":
        return Availability.refuse("group_unavailable")
    if not facts.rights:
        return Availability.refuse("no_group_rights")
    if facts.limit.unlimited:
        return Availability.refuse("unlimited")
    if facts.limit.zero:
        return Availability.refuse("zero_limit")
    if facts.period_id is None or facts.period_state not in ("open", "deferred"):
        return Availability.refuse("no_period")
    coverage = projected_coverage or facts.paid_until
    margin = timedelta(hours=cfg.min_coverage_hours)
    if facts.period_state == "deferred" or coverage is None or coverage <= at + margin:
        return Availability.refuse("expiring", date=_date(coverage))
    if facts.planned_end_at is not None and facts.planned_end_at <= at:
        return Availability.refuse("period_changed")
    if facts.block_reason == "manual":
        return Availability.refuse("manual_block")
    if facts.frozen:
        return Availability.refuse("frozen")
    if pack is not None and all(p.id != pack.id for p in facts.packs):
        return Availability.refuse("package_disabled")
    insufficient = False
    if pack is not None and facts.blocked:
        insufficient = pack.gb * int(gb_bytes or cfg.gb_bytes) <= facts.overage()
    return Availability(True, insufficient=insufficient)


def _date(moment: datetime | None) -> str:
    from svbg.ext.lte.notify import fmt_date

    return fmt_date(moment)


def order_packs(facts: PackFacts, gb_bytes: int) -> list[tuple[Pack, bool]]:
    """Packs for the screen: when access is blocked — the smallest sufficient first (✅), then the other
    sufficient ones; if none suffices, only the biggest. Otherwise by size."""
    packs = sorted(facts.packs, key=lambda p: (p.gb, p.amount_minor))
    if not facts.blocked:
        return [(p, False) for p in packs]
    need = facts.overage()
    enough = [p for p in packs if p.gb * gb_bytes > need]
    if not enough:
        return [(packs[-1], False)] if packs else []
    return [(p, i == 0) for i, p in enumerate(enough)]


# ----------------------------------------------------------------------------------------------- loads


def _facts_query(
    *, user_id: int | None = None, sid: int | None = None, group_id: int | None = None
) -> sa.Select[Any]:
    s, p, g = subscriptions.c, lte_periods.c, lte_groups.c
    live = sa.and_(p.subscription_id == s.id, p.state != "closed")
    used = (
        sa.select(lte_period_usage.c.used_bytes)
        .where(lte_period_usage.c.period_id == p.id, lte_period_usage.c.group_id == g.id)
        .scalar_subquery()
    )
    credits = (
        sa.select(sa.func.coalesce(sa.func.sum(lte_credits.c.bytes), 0))
        .where(
            lte_credits.c.period_id == p.id, lte_credits.c.group_id == g.id, lte_credits.c.status == "active"
        )
        .scalar_subquery()
    )
    o = lte_overrides.c
    overrides = (
        sa.select(
            sa.func.jsonb_agg(
                sa.func.jsonb_build_object(
                    "kind",
                    o.kind,
                    "group_id",
                    o.group_id,
                    "limit_bytes",
                    o.limit_bytes,
                    "applies_to",
                    o.applies_to,
                    "period_id",
                    o.period_id,
                    "valid_until",
                    o.valid_until,
                    "exempt_kind",
                    o.exempt_kind,
                )
            )
        )
        .where(o.subscription_id == s.id, o.revoked_at.is_(None))
        .scalar_subquery()
    )
    b = lte_blocks.c
    block = (
        sa.select(sa.func.concat(b.reason, ":", b.status))
        .where(
            b.subscription_id == s.id,
            b.group_id == g.id,
            b.status.in_(("active", "releasing")),
            b.mode == "enforce",
        )
        .limit(1)
        .scalar_subquery()
    )
    k = lte_packs.c
    packs = (
        sa.select(
            sa.func.jsonb_agg(
                sa.func.jsonb_build_object(
                    "id", k.id, "gb", k.gb, "amount", k.amount_minor, "currency", k.currency
                )
            )
        )
        .where(k.enabled, sa.or_(k.group_id.is_(None), k.group_id == g.id))
        .scalar_subquery()
    )
    rights = sa.exists().where(
        lte_twins.c.group_id == g.id, s.desired_squads.has_key(lte_twins.c.base_squad_uuid)
    )
    q = (
        sa.select(
            s.id.label("sid"),
            s.user_id,
            s.panel_user_id,
            s.paid_until,
            s.hold_kind,
            p.id.label("pid"),
            p.state.label("pstate"),
            p.is_trial,
            p.planned_end_at,
            lte_anchors.c.anchor_kind,
            g.id.label("gid"),
            g.name,
            g.state.label("gstate"),
            g.enforce,
            g.margin_bytes,
            g.margin_pct,
            g.has_default,
            g.limit_default_bytes,
            g.has_trial,
            g.limit_trial_bytes,
            used.label("used"),
            credits.label("credits"),
            overrides.label("overrides"),
            block.label("block"),
            packs.label("packs"),
            rights.label("rights"),
        )
        .select_from(
            subscriptions.outerjoin(lte_periods, live)
            .outerjoin(lte_anchors, lte_anchors.c.subscription_id == s.id)
            .join(lte_groups, g.state != "draft")
        )
        .where(s.link_state != "closed")
        .order_by(g.sort, g.id)
    )
    if user_id is not None:
        q = q.where(s.user_id == user_id)
    if sid is not None:
        q = q.where(s.id == sid)
    if group_id is not None:
        q = q.where(g.id == group_id)
    return q


def _dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def facts_from_row(r: Mapping[str, Any], *, at: datetime) -> PackFacts:
    from svbg.ext.lte.service import group_from_row

    overrides = tuple(
        Override(
            kind=str(x.get("kind")),
            group_id=x.get("group_id"),
            limit_bytes=x.get("limit_bytes"),
            applies_to=str(x.get("applies_to") or "all"),
            period_id=x.get("period_id"),
            valid_until=_dt(x.get("valid_until")),
            exempt_kind=x.get("exempt_kind"),
        )
        for x in (r["overrides"] or ())
        if isinstance(x, Mapping)
    )
    gid = int(r["gid"])
    subject = SubjectInput(
        subscription_id=int(r["sid"]),
        panel_user_id=int(r["panel_user_id"] or 0),
        period_id=r["pid"],
        overrides=overrides,
    )
    exempt_all, exempt_groups = subject.exempt_groups(at)
    group = group_from_row(
        {
            "id": gid,
            "slug": "",
            "name": r["name"],
            "state": r["gstate"],
            "enforce": r["enforce"],
            "margin_bytes": r["margin_bytes"],
            "margin_pct": r["margin_pct"],
            "has_default": r["has_default"],
            "limit_default_bytes": r["limit_default_bytes"],
            "has_trial": r["has_trial"],
            "limit_trial_bytes": r["limit_trial_bytes"],
            "squad_uuid": None,
            "version": 1,
        }
    ).input()
    credit = int(r["credits"] or 0)
    limit = effective_limit(
        group, is_trial=bool(r["is_trial"]), user_rows=subject.limit_rows(gid, at), credit_bytes=credit
    )
    block = str(r["block"] or "")
    reason, _, status = block.partition(":")
    packs = tuple(
        Pack(int(x["id"]), int(x["gb"]), int(x["amount"]), str(x["currency"]))
        for x in (r["packs"] or ())
        if isinstance(x, Mapping)
    )
    return PackFacts(
        subscription_id=int(r["sid"]),
        user_id=r["user_id"],
        group_id=gid,
        group_name=dict(r["name"] or {}),
        group_state=str(r["gstate"]),
        group_enforce=bool(r["enforce"]),
        panel_user_id=r["panel_user_id"],
        paid_until=r["paid_until"],
        frozen=r["hold_kind"] is not None,
        period_id=r["pid"],
        period_state=r["pstate"],
        period_is_trial=bool(r["is_trial"]),
        planned_end_at=r["planned_end_at"],
        anchor_kind=r["anchor_kind"],
        rights=bool(r["rights"]),
        used_bytes=int(r["used"] or 0),
        credit_bytes=credit,
        limit=limit,
        exempt=exempt_all or gid in exempt_groups,
        block_reason=reason or None,
        block_status=status or None,
        packs=tuple(sorted(packs, key=lambda p: (p.gb, p.amount_minor))),
    )


async def load_facts(
    conn: AsyncConnection,
    *,
    user_id: int | None = None,
    sid: int | None = None,
    group_id: int | None = None,
    at: datetime | None = None,
) -> list[PackFacts]:
    """One SQL: the user's (or subscription's) live subscription × non-draft groups."""
    at = at or now()
    rows = (await conn.execute(_facts_query(user_id=user_id, sid=sid, group_id=group_id))).mappings().all()
    return [facts_from_row(r, at=at) for r in rows]


@dataclass(frozen=True, slots=True)
class Offer:
    facts: PackFacts
    result: Availability

    @property
    def ok(self) -> bool:
        return self.result.ok


async def offer_for(service: LteService, sid: int, group_id: int) -> Offer | None:
    async with service.db.read() as conn:
        found = await load_facts(conn, sid=sid, group_id=group_id)
    if not found:
        return None
    facts = found[0]
    return Offer(facts, availability(facts, service.cfg(), at=now()))


# ---------------------------------------------------------------------------------------------- orders


@dataclass(frozen=True, slots=True)
class DraftResult:
    order_id: int | None
    refusal: Availability | None = None


async def create_order(
    service: LteService,
    user_id: int,
    group_id: int,
    pack_id: int,
    *,
    force: bool = False,
    at: datetime | None = None,
) -> DraftResult:
    """«✅ Подтвердить» → an ``addon_lte`` draft (2 SQL). ``force`` — «Всё равно купить» (not enough)."""
    at = at or now()
    cfg = service.cfg()
    async with service.db.tx() as conn:
        found = await load_facts(conn, user_id=user_id, group_id=group_id, at=at)
        facts = next((f for f in found if f.rights), found[0] if found else None)
        if facts is None:
            return DraftResult(None, Availability.refuse("no_subscription"))
        pack = next((p for p in facts.packs if p.id == pack_id), None)
        if pack is None:
            return DraftResult(None, Availability.refuse("package_disabled"))
        result = availability(facts, cfg, at=at, pack=pack)
        if not result.ok or (result.insufficient and not force):
            return DraftResult(None, result)
        gb_bytes = cfg.gb_bytes
        snapshot = {
            "v": 1,
            "title": f"Трафик LTE +{pack.gb} ГБ",
            "subscription_id": facts.subscription_id,
            "group_id": facts.group_id,
            "period_id": facts.period_id,
            "planned_end_at": facts.planned_end_at.isoformat() if facts.planned_end_at else None,
            "gb": pack.gb,
            "bytes": pack.gb * gb_bytes,
            "pack_id": pack.id,
            "amount_minor": pack.amount_minor,
        }
        order_id = (
            await conn.execute(
                sa.insert(orders)
                .values(
                    user_id=user_id,
                    kind=ORDER_KIND,
                    status="draft",
                    currency=pack.currency,
                    total_minor=pack.amount_minor,
                    subscription_id=facts.subscription_id,
                    snapshot=snapshot,
                )
                .returning(orders.c.id)
            )
        ).scalar_one()
    return DraftResult(int(order_id))


async def attach_to_order(
    conn: AsyncConnection, order_id: int, user_id: int, pack: Pack, group_id: int, *, gb_bytes: int
) -> bool:
    """Checkout step «🛜 Трафик LTE» (X8): add an ``lte_pack`` item to a purchase draft at today's price
    (frozen in the snapshot — the user pays what the summary showed). ``False`` when the draft is gone.

    The total is ``previous total − the replaced pack + this pack``: discounts (a promo code) are not order
    items — they live only in ``total_minor`` — so re-summing the items would silently drop them."""
    row = (
        await conn.execute(
            sa.select(orders.c.id, orders.c.snapshot, orders.c.total_minor)
            .where(
                orders.c.id == order_id,
                orders.c.user_id == user_id,
                orders.c.status == "draft",
                orders.c.kind.in_(("new", "renew", "change")),
            )
            .with_for_update()
        )
    ).first()
    if row is None:
        return False
    replaced = (
        (
            await conn.execute(
                sa.delete(order_items)
                .where(order_items.c.order_id == order_id, order_items.c.type == ITEM_TYPE)
                .returning(order_items.c.amount_minor)
            )
        )
        .scalars()
        .all()
    )
    position = await conn.scalar(
        sa.select(sa.func.coalesce(sa.func.max(order_items.c.position), 0) + 1).where(
            order_items.c.order_id == order_id
        )
    )
    await conn.execute(
        sa.insert(order_items).values(
            order_id=order_id,
            position=int(position or 1),
            type=ITEM_TYPE,
            payload={"group_id": group_id, "pack_id": pack.id, "gb": pack.gb, "bytes": pack.gb * gb_bytes},
            amount_minor=pack.amount_minor,
        )
    )
    total = max(0, int(row.total_minor or 0) - sum(int(x or 0) for x in replaced)) + pack.amount_minor
    snap = dict(row.snapshot or {})
    snap["lte_pack"] = {"gb": pack.gb, "amount_minor": pack.amount_minor}
    await conn.execute(
        sa.update(orders)
        .where(orders.c.id == order_id)
        .values(total_minor=total, snapshot=snap, updated_at=now())
    )
    return True


async def _insert_credit(
    conn: AsyncConnection, *, sid: int, group_id: int, period_id: int, value: int, order_id: int, amount: int
) -> bool:
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    stmt = (
        pg_insert(lte_credits)
        .values(
            subscription_id=sid,
            group_id=group_id,
            period_id=period_id,
            bytes=value,
            order_id=order_id,
            source="order",
            status="active",
            amount_minor=amount,
        )
        .on_conflict_do_nothing(index_elements=["order_id"])
        .returning(lte_credits.c.id)
    )
    return (await conn.execute(stmt)).first() is not None


async def _topup_card(
    conn: AsyncConnection, service: LteService, *, sid: int, value: int, order_id: int
) -> None:
    """«⚡ LTE: докупка» in the topic (``LTE_ADMIN_NOTIFY_TOPUPS``): a durable job posted after the commit."""
    from svbg.ext.lte.notify import CARD_T, fmt_gb
    from svbg.ext.lte.service import CARD_KIND
    from svbg.jobs.queue import enqueue

    cfg = service.cfg()
    if not cfg.card_topups:
        return
    gb = fmt_gb(value, cfg.gb_bytes)
    await enqueue(
        conn,
        CARD_KIND,
        {"card": "topup", "sid": sid, "gb": gb, "text": CARD_T["topup"].format(sid=sid, gb=gb)},
        queue="notify",
        lane="background",
        dedup_key=f"lte.card:order:{order_id}",
        max_attempts=3,
        caused_by=f"order:{order_id}",
    )


def _service() -> LteService:
    from svbg.ext.lte.service import RUNTIME

    return RUNTIME.service()


class AddonLteKind:
    """Order kind ``addon_lte``: applied in the fulfill transaction instead of a plan term."""

    def __init__(self, service: LteService | None = None) -> None:
        self._service = service

    @property
    def service(self) -> LteService:
        return self._service or _service()

    async def apply(
        self, conn: AsyncConnection, order: Mapping[str, Any], items: Sequence[Mapping[str, Any]]
    ) -> int:
        del items
        snap = order["snapshot"] or {}
        sid, gid, pid = snap.get("subscription_id"), snap.get("group_id"), snap.get("period_id")
        value = snap.get("bytes")
        if not all(isinstance(x, int) and not isinstance(x, bool) for x in (sid, gid, pid, value)):
            raise SubscriptionError("bad_order", "Заказ повреждён.")
        at = now()
        await conn.execute(sa.select(subscriptions.c.id).where(subscriptions.c.id == sid).with_for_update())
        service = self.service
        await service.apply_events(conn, int(sid), at=at)
        found = await load_facts(conn, sid=int(sid), group_id=int(gid), at=at)
        if not found:
            raise SubscriptionError("lte_period_changed", REFUSALS["period_changed"])
        facts = found[0]
        if facts.period_id != pid or (facts.planned_end_at is not None and facts.planned_end_at <= at):
            raise SubscriptionError(
                "lte_period_changed", "Лимит LTE уже обновился — пакет не нужен, деньги вернулись на баланс."
            )
        result = availability(facts, service.cfg(), at=at)
        if not result.ok:
            raise SubscriptionError(f"lte_{result.code}", result.text or "Пакет сейчас недоступен.")
        if await _insert_credit(
            conn,
            sid=int(sid),
            group_id=int(gid),
            period_id=int(pid),
            value=int(value),
            order_id=int(order["id"]),
            amount=int(order["total_minor"]),
        ):
            await _topup_card(conn, service, sid=int(sid), value=int(value), order_id=int(order["id"]))
        await service.decide_for(conn, [int(sid)], at=at)
        return int(sid)


class LtePackItem:
    """Order item ``lte_pack`` in a plan purchase / renewal (after the term, same transaction)."""

    def __init__(self, service: LteService | None = None) -> None:
        self._service = service

    @property
    def service(self) -> LteService:
        return self._service or _service()

    async def fulfill(self, conn: AsyncConnection, order: Mapping[str, Any], item: Mapping[str, Any]) -> None:
        if order["kind"] == ORDER_KIND:
            return  # the kind handler credited it
        payload = item["payload"] or {}
        gid, value = payload.get("group_id"), payload.get("bytes")
        sid = order.get("subscription_id")
        if not isinstance(sid, int):
            done = await journal.find_by_ref(conn, "order", str(order["id"]), _PURCHASE_KINDS)
            sid = done.subscription_id if done is not None else None
        at = now()
        service = self.service
        facts: PackFacts | None = None
        if isinstance(sid, int) and isinstance(gid, int) and isinstance(value, int) and value > 0:
            await service.apply_events(conn, sid, at=at)
            found = await load_facts(conn, sid=sid, group_id=gid, at=at)
            facts = found[0] if found else None
        if (
            facts is None
            or facts.period_id is None
            or facts.exempt
            or facts.limit.unlimited
            or not facts.rights
        ):
            await self._refund(conn, order, item)
            return
        if await _insert_credit(
            conn,
            sid=facts.subscription_id,
            group_id=facts.group_id,
            period_id=int(facts.period_id),
            value=int(value or 0),
            order_id=int(order["id"]),
            amount=int(item["amount_minor"]),
        ):
            await _topup_card(
                conn, service, sid=facts.subscription_id, value=int(value or 0), order_id=int(order["id"])
            )
        await service.decide_for(conn, [facts.subscription_id], at=at)

    @staticmethod
    async def _refund(conn: AsyncConnection, order: Mapping[str, Any], item: Mapping[str, Any]) -> None:
        amount = int(item["amount_minor"] or 0)
        if (
            amount > 0
            and await wallet.find(conn, int(order["user_id"]), "purchase", "order", order["id"]) is not None
        ):
            await wallet.credit(
                conn,
                int(order["user_id"]),
                amount,
                reason=REFUND_REASON,
                ref_type="order_item",
                ref_id=item["id"],
                currency=str(order["currency"]),
                note="пакет LTE: нет периода",
            )
        await conn.execute(
            sa.update(order_items).where(order_items.c.id == item["id"]).values(status="refunded")
        )


def addon_kind() -> AddonLteKind:
    return AddonLteKind()


def pack_item() -> LtePackItem:
    return LtePackItem()


def group_rows(facts: PackFacts) -> dict[str, int | None]:  # pragma: no cover - used by admin previews
    return group_limit_rows(
        has_default=True, limit_default=facts.limit.base, has_trial=False, limit_trial=None
    )
