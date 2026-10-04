"""Read side of the user card: one SQL per screen (card, payments, orders, wallet ledger, events).

Keyset pagination everywhere except the merged event feed (small pages, ``OFFSET`` over two short streams).
Nothing here writes; the operations live in :mod:`svbg.tg.admin.users.ops`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

import sqlalchemy as sa

from svbg.billing.tables import orders, wallet_ledger
from svbg.core.tables import admin_audit, users
from svbg.payments.tables import payment_instances, payments
from svbg.services.roles import stored_perms
from svbg.subscriptions.tables import subscription_events, subscriptions

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

__all__ = [
    "DIR_FILTERS",
    "DIR_PAGE",
    "LIVE_STATES",
    "PAGE",
    "Card",
    "DirRow",
    "EventRow",
    "LedgerRow",
    "ListRow",
    "OrderRow",
    "PaymentRow",
    "banned_users",
    "count_directory",
    "list_directory",
    "load_card",
    "load_events",
    "load_ledger",
    "load_orders",
    "load_payments",
    "recent_payers",
    "recent_users",
]

PAGE: Final = 10
LIVE_STATES: Final = ("pending", "linked")


@dataclass(frozen=True, slots=True)
class Card:
    user_id: int
    telegram_id: int | None
    username: str | None
    first_name: str | None
    role: str
    perms: tuple[str, ...]
    created_at: datetime
    last_seen_at: datetime | None
    banned_at: datetime | None
    bot_blocked_at: datetime | None
    wallet_minor: int
    # the latest live subscription (or the latest one at all)
    sub_id: int | None = None
    link_state: str | None = None
    is_trial: bool = False
    paid_until: datetime | None = None
    hold_kind: str | None = None
    disabled_reason: str | None = None
    plan_name: str | None = None
    device_limit: int | None = None
    extra_devices: int = 0
    traffic_limit: int | None = None
    traffic_used: int | None = None
    panel_username: str | None = None
    short_uuid: str | None = None
    subscription_url: str | None = None
    # money (shown to admins and owners only)
    paid_count: int = 0
    paid_total_minor: int = 0  # in the shop currency
    last_paid_at: datetime | None = None
    orders_count: int = 0
    captcha_passed: bool = True  # the entry captcha (``users.captcha_passed_at``)
    staff_role_id: int | None = None  # a custom staff role (``svbg.services.staff_roles``)

    @property
    def live(self) -> bool:
        return self.sub_id is not None and self.link_state in LIVE_STATES

    @property
    def linked(self) -> bool:
        return self.sub_id is not None and self.link_state == "linked"


def _plan_name(snapshot: Any, lang: str = "ru") -> str | None:
    if not isinstance(snapshot, Mapping):
        return None
    name = snapshot.get("name")
    if isinstance(name, Mapping):
        value = name.get(lang) or next((v for v in name.values() if isinstance(v, str) and v), None)
        if isinstance(value, str) and value:
            return value
    elif isinstance(name, str) and name:
        return name
    code = snapshot.get("code")
    return code if isinstance(code, str) and code else None


async def load_card(conn: AsyncConnection, user_id: int, *, currency: str) -> Card | None:
    """The card in one statement: user + latest subscription (live first) + payment and order counters."""
    s = subscriptions.alias("s")
    sub = (
        sa.select(
            s.c.id.label("sub_id"),
            s.c.link_state,
            s.c.is_trial,
            s.c.paid_until,
            s.c.hold_kind,
            s.c.disabled_reason,
            s.c.plan_snapshot,
            s.c.desired_device_limit,
            s.c.panel_device_limit,
            s.c.extra_devices,
            s.c.desired_traffic_bytes,
            s.c.panel_traffic_limit,
            s.c.panel_used_traffic,
            s.c.panel_username,
            s.c.panel_short_uuid,
            s.c.subscription_url,
        )
        .where(s.c.user_id == users.c.id)
        .order_by(s.c.link_state.in_(LIVE_STATES).desc(), s.c.id.desc())
        .limit(1)
        .lateral("sub")
    )
    paid = payments.c.status == "paid"
    real = sa.and_(paid, payments.c.is_test.is_(False))
    pay = (
        sa.select(
            sa.func.count().filter(real).label("paid_count"),
            sa.func.coalesce(
                sa.func.sum(sa.func.coalesce(payments.c.paid_amount_minor, payments.c.amount_minor)).filter(
                    real, payments.c.currency == currency
                ),
                0,
            ).label("paid_total"),
            sa.func.max(payments.c.paid_at).filter(real).label("last_paid_at"),
        )
        .where(payments.c.user_id == users.c.id)
        .lateral("pay")
    )
    n_orders = (
        sa.select(sa.func.count())
        .where(orders.c.user_id == users.c.id, orders.c.kind != "topup")
        .scalar_subquery()
        .label("orders_count")
    )
    stmt = (
        sa.select(
            users.c.id,
            users.c.telegram_id,
            users.c.username,
            users.c.first_name,
            users.c.role,
            users.c.perms,
            users.c.created_at,
            users.c.last_seen_at,
            users.c.banned_at,
            users.c.bot_blocked_at,
            users.c.wallet_minor,
            users.c.captcha_passed_at,
            users.c.staff_role_id,
            sub,
            pay.c.paid_count,
            pay.c.paid_total,
            pay.c.last_paid_at,
            n_orders,
        )
        .select_from(users.outerjoin(sub, sa.true()).outerjoin(pay, sa.true()))
        .where(users.c.id == user_id)
    )
    row = (await conn.execute(stmt)).mappings().first()
    if row is None:
        return None
    return Card(
        user_id=int(row["id"]),
        telegram_id=row["telegram_id"],
        username=row["username"],
        first_name=row["first_name"],
        role=str(row["role"]),
        perms=stored_perms(row["perms"]),
        created_at=row["created_at"],
        last_seen_at=row["last_seen_at"],
        banned_at=row["banned_at"],
        bot_blocked_at=row["bot_blocked_at"],
        wallet_minor=int(row["wallet_minor"] or 0),
        sub_id=row["sub_id"],
        link_state=row["link_state"],
        is_trial=bool(row["is_trial"]),
        paid_until=row["paid_until"],
        hold_kind=row["hold_kind"],
        disabled_reason=row["disabled_reason"],
        plan_name=_plan_name(row["plan_snapshot"]),
        device_limit=(
            row["panel_device_limit"]
            if row["panel_device_limit"] is not None
            else row["desired_device_limit"]
        ),
        extra_devices=int(row["extra_devices"] or 0),
        traffic_limit=(
            row["panel_traffic_limit"]
            if row["panel_traffic_limit"] is not None
            else row["desired_traffic_bytes"]
        ),
        traffic_used=row["panel_used_traffic"],
        panel_username=row["panel_username"],
        short_uuid=row["panel_short_uuid"],
        subscription_url=row["subscription_url"],
        paid_count=int(row["paid_count"] or 0),
        paid_total_minor=int(row["paid_total"] or 0),
        last_paid_at=row["last_paid_at"],
        orders_count=int(row["orders_count"] or 0),
        captcha_passed=row["captcha_passed_at"] is not None,
        staff_role_id=int(row["staff_role_id"]) if row["staff_role_id"] is not None else None,
    )


# ------------------------------------------------------------------------------------------------ history


@dataclass(frozen=True, slots=True)
class PaymentRow:
    id: str
    status: str
    amount_minor: int
    currency: str
    paid_amount_minor: int | None
    method: str
    is_test: bool
    created_at: datetime
    paid_at: datetime | None


async def load_payments(
    conn: AsyncConnection, user_id: int, *, before: str | None = None, limit: int = PAGE
) -> list[PaymentRow]:
    """Newest first; ``before`` = the last id of the previous page (UUIDv7 ids sort by time)."""
    stmt = (
        sa.select(
            payments.c.id,
            payments.c.status,
            payments.c.amount_minor,
            payments.c.currency,
            payments.c.paid_amount_minor,
            sa.func.coalesce(payment_instances.c.title, payments.c.method_kind, "—").label("method"),
            payments.c.is_test,
            payments.c.created_at,
            payments.c.paid_at,
        )
        .select_from(payments.outerjoin(payment_instances, payment_instances.c.id == payments.c.instance_id))
        .where(payments.c.user_id == user_id)
        .order_by(payments.c.id.desc())
        .limit(limit + 1)
    )
    if before is not None:
        stmt = stmt.where(payments.c.id < before)
    rows = (await conn.execute(stmt)).all()
    return [
        PaymentRow(
            str(r.id),
            str(r.status),
            int(r.amount_minor),
            str(r.currency),
            r.paid_amount_minor,
            str(r.method),
            bool(r.is_test),
            r.created_at,
            r.paid_at,
        )
        for r in rows
    ]


@dataclass(frozen=True, slots=True)
class OrderRow:
    id: int
    kind: str
    status: str
    total_minor: int
    currency: str
    plan_name: str | None
    days: int | None
    created_at: datetime


async def load_orders(
    conn: AsyncConnection, user_id: int, *, before: int | None = None, limit: int = PAGE
) -> list[OrderRow]:
    stmt = (
        sa.select(
            orders.c.id,
            orders.c.kind,
            orders.c.status,
            orders.c.total_minor,
            orders.c.currency,
            orders.c.snapshot,
            orders.c.created_at,
        )
        .where(orders.c.user_id == user_id)
        .order_by(orders.c.id.desc())
        .limit(limit + 1)
    )
    if before is not None:
        stmt = stmt.where(orders.c.id < before)
    out: list[OrderRow] = []
    for r in (await conn.execute(stmt)).all():
        snap = r.snapshot if isinstance(r.snapshot, Mapping) else {}
        title = snap.get("title")
        plan = snap.get("plan") if isinstance(snap.get("plan"), Mapping) else snap
        days = snap.get("days")
        out.append(
            OrderRow(
                int(r.id),
                str(r.kind),
                str(r.status),
                int(r.total_minor),
                str(r.currency),
                title if isinstance(title, str) and title else _plan_name(plan),
                days if isinstance(days, int) and not isinstance(days, bool) else None,
                r.created_at,
            )
        )
    return out


@dataclass(frozen=True, slots=True)
class LedgerRow:
    id: int
    amount_minor: int
    currency: str
    balance_after: int
    reason: str
    note: str | None
    created_at: datetime


async def load_ledger(
    conn: AsyncConnection, user_id: int, *, before: int | None = None, limit: int = PAGE
) -> list[LedgerRow]:
    stmt = (
        sa.select(wallet_ledger)
        .where(wallet_ledger.c.user_id == user_id)
        .order_by(wallet_ledger.c.id.desc())
        .limit(limit + 1)
    )
    if before is not None:
        stmt = stmt.where(wallet_ledger.c.id < before)
    return [
        LedgerRow(
            int(r.id),
            int(r.amount_minor),
            str(r.currency),
            int(r.balance_after),
            str(r.reason),
            r.note,
            r.created_at,
        )
        for r in (await conn.execute(stmt)).all()
    ]


@dataclass(frozen=True, slots=True)
class EventRow:
    ts: datetime
    source: str  # "sub" | "admin"
    kind: str
    delta_seconds: int | None
    new_expire: datetime | None
    actor_role: str | None
    reason: str | None
    amount_minor: int | None


async def load_events(
    conn: AsyncConnection, user_id: int, *, page: int = 0, limit: int = PAGE
) -> list[EventRow]:
    """Subscription events of the user's subscriptions and admin actions on the user, newest first."""
    sub_ids = sa.select(subscriptions.c.id).where(subscriptions.c.user_id == user_id)
    null_text = sa.cast(sa.null(), sa.Text)
    ev = sa.select(
        subscription_events.c.ts.label("ts"),
        sa.literal("sub", sa.Text).label("source"),
        subscription_events.c.kind.label("kind"),
        subscription_events.c.delta_seconds.label("delta"),
        subscription_events.c.new_expire.label("new_expire"),
        null_text.label("actor_role"),
        null_text.label("reason"),
        sa.cast(sa.null(), sa.BigInteger).label("amount"),
    ).where(subscription_events.c.subscription_id.in_(sub_ids))
    adm = sa.select(
        admin_audit.c.ts.label("ts"),
        sa.literal("admin", sa.Text).label("source"),
        admin_audit.c.action.label("kind"),
        sa.cast(sa.null(), sa.BigInteger).label("delta"),
        sa.cast(sa.null(), admin_audit.c.ts.type).label("new_expire"),
        admin_audit.c.role.label("actor_role"),
        admin_audit.c.reason.label("reason"),
        admin_audit.c.amount_minor.label("amount"),
    ).where(admin_audit.c.target == f"user:{user_id}", admin_audit.c.action != "access_denied")
    merged = sa.union_all(ev, adm).subquery("feed")
    stmt = sa.select(merged).order_by(merged.c.ts.desc()).offset(max(0, page) * limit).limit(limit + 1)
    return [
        EventRow(
            r.ts,
            str(r.source),
            str(r.kind),
            r.delta,
            r.new_expire,
            r.actor_role,
            r.reason,
            r.amount,
        )
        for r in (await conn.execute(stmt)).all()
    ]


# ------------------------------------------------------------------ lists of «👥 Пользователи»


@dataclass(frozen=True, slots=True)
class ListRow:
    user_id: int
    first_name: str | None
    username: str | None
    at: datetime | None
    amount_minor: int | None = None
    currency: str | None = None


async def recent_users(conn: AsyncConnection, *, limit: int = 20) -> list[ListRow]:
    """The newest users (index ``ix_users_created_at``)."""
    stmt = (
        sa.select(users.c.id, users.c.first_name, users.c.username, users.c.created_at)
        .order_by(users.c.created_at.desc(), users.c.id.desc())
        .limit(limit)
    )
    return [
        ListRow(int(r.id), r.first_name, r.username, r.created_at) for r in (await conn.execute(stmt)).all()
    ]


async def recent_payers(conn: AsyncConnection, *, limit: int = 20) -> list[ListRow]:
    """The latest real payments (no test, no imported ones; index ``ix_payments_paid_at``)."""
    stmt = (
        sa.select(
            payments.c.user_id,
            users.c.first_name,
            users.c.username,
            payments.c.paid_at,
            sa.func.coalesce(payments.c.paid_amount_minor, payments.c.amount_minor).label("amount"),
            sa.func.coalesce(payments.c.paid_currency, payments.c.currency).label("currency"),
        )
        .join(users, users.c.id == payments.c.user_id)
        .where(payments.c.status == "paid", sa.not_(payments.c.is_test), sa.not_(payments.c.is_imported))
        .order_by(payments.c.paid_at.desc())
        .limit(limit)
    )
    return [
        ListRow(int(r.user_id), r.first_name, r.username, r.paid_at, int(r.amount), str(r.currency))
        for r in (await conn.execute(stmt)).all()
    ]


async def banned_users(conn: AsyncConnection, *, limit: int = 20) -> list[ListRow]:
    """Users banned in the bot, the latest first."""
    stmt = (
        sa.select(users.c.id, users.c.first_name, users.c.username, users.c.banned_at)
        .where(users.c.banned_at.is_not(None))
        .order_by(users.c.banned_at.desc())
        .limit(limit)
    )
    return [
        ListRow(int(r.id), r.first_name, r.username, r.banned_at) for r in (await conn.execute(stmt)).all()
    ]


# ------------------------------------------------------------------ «📋 Все пользователи»

DIR_PAGE: Final = 10
#: Filters of the full list in button order: all, active, trial, expired, no subscription, banned, no captcha.
DIR_FILTERS: Final[tuple[str, ...]] = ("all", "act", "trial", "exp", "nosub", "ban", "nocap")


@dataclass(frozen=True, slots=True)
class DirRow:
    """A row of the full list: the person and their current subscription (live first, then the latest)."""

    user_id: int
    first_name: str | None
    username: str | None
    banned: bool
    sub_id: int | None
    link_state: str | None
    is_trial: bool
    paid_until: datetime | None
    hold_kind: str | None


def _current_sub() -> Any:
    """The same subscription the card shows: the latest live one, else the latest at all."""
    s = subscriptions.alias("cur_s")
    return (
        sa.select(
            s.c.id.label("sub_id"),
            s.c.link_state,
            s.c.is_trial,
            s.c.paid_until,
            s.c.hold_kind,
        )
        .where(s.c.user_id == users.c.id)
        .order_by(s.c.link_state.in_(LIVE_STATES).desc(), s.c.id.desc())
        .limit(1)
        .lateral("cur")
    )


def _conditions(cur: Any) -> dict[str, Any]:
    # A frozen subscription still has paid time: it counts as running.
    running = sa.and_(
        cur.c.link_state.in_(LIVE_STATES),
        sa.or_(sa.func.coalesce(cur.c.paid_until > sa.func.now(), False), cur.c.hold_kind.is_not(None)),
    )
    return {
        "all": sa.true(),
        "act": sa.and_(running, cur.c.is_trial.is_(False)),
        "trial": sa.and_(running, cur.c.is_trial.is_(True)),
        "exp": sa.and_(cur.c.sub_id.is_not(None), sa.not_(running)),
        "nosub": cur.c.sub_id.is_(None),
        "ban": users.c.banned_at.is_not(None),
        "nocap": sa.and_(users.c.captcha_passed_at.is_(None), users.c.role == "user"),
    }


async def list_directory(
    conn: AsyncConnection,
    flt: str,
    *,
    before: int | None = None,
    after: int | None = None,
    limit: int = DIR_PAGE,
) -> list[DirRow]:
    """One page of the full list, newest first (keyset by ``users.id``): ``before`` — the next page (ids below
    it), ``after`` — the previous page (ids above it). ``limit + 1`` rows are read when going forward, so the
    caller knows whether there is a next page. One SQL."""
    cur = _current_sub()
    cond = _conditions(cur).get(flt)
    if cond is None:
        raise ValueError(f"unknown filter {flt!r}")
    stmt = (
        sa.select(
            users.c.id,
            users.c.first_name,
            users.c.username,
            users.c.banned_at,
            cur.c.sub_id,
            cur.c.link_state,
            cur.c.is_trial,
            cur.c.paid_until,
            cur.c.hold_kind,
        )
        .select_from(users.outerjoin(cur, sa.true()))
        .where(cond)
    )
    if after is not None:
        stmt = stmt.where(users.c.id > after).order_by(users.c.id.asc()).limit(limit)
    else:
        if before is not None:
            stmt = stmt.where(users.c.id < before)
        stmt = stmt.order_by(users.c.id.desc()).limit(limit + 1)
    rows = [
        DirRow(
            int(r.id),
            r.first_name,
            r.username,
            r.banned_at is not None,
            r.sub_id,
            r.link_state,
            bool(r.is_trial),
            r.paid_until,
            r.hold_kind,
        )
        for r in (await conn.execute(stmt)).all()
    ]
    if after is not None:
        rows.reverse()
    return rows


async def count_directory(conn: AsyncConnection) -> dict[str, int]:
    """How many people each filter of the full list has (one SQL; the screen caches it for a minute)."""
    cur = _current_sub()
    conds = _conditions(cur)
    stmt = sa.select(
        *(
            (sa.func.count() if name == "all" else sa.func.count().filter(cond)).label(name)
            for name, cond in conds.items()
        )
    ).select_from(users.outerjoin(cur, sa.true()))
    row = (await conn.execute(stmt)).mappings().one()
    return {name: int(row[name] or 0) for name in DIR_FILTERS}
