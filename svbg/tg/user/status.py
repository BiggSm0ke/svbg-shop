"""The user's status in **one** SQL statement: balance, «paid before», trial facts, the current subscription.

Every user screen that depends on the subscription (home card, buy, connect, devices) starts with
:meth:`StatusReader.load` — a single ``SELECT`` with a ``LATERAL`` join — and then renders from memory
(catalog snapshot, content snapshot). Together with the cached ``ui_state`` this keeps a click within the
budget of ≤ 2 SQL and 0 HTTP to the panel.

:meth:`UserStatus.enrich` turns the facts into the :class:`~svbg.tg.ui.context.UserCtx` fields the visibility
DSL reads (``sub``, ``days_left``, ``balance_minor``, ``has_paid``, ``plan``, ``flag:trial``), so content
buttons such as «Продлить» or «🎁 Попробовать бесплатно» show up exactly when they make sense.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

import sqlalchemy as sa

from svbg.billing.tables import orders
from svbg.core.clock import now
from svbg.db.meta import UtcDateTime
from svbg.subscriptions.tables import subscriptions, trial_grants
from svbg.tg.user.tables import user_devices

if TYPE_CHECKING:
    from svbg.db.engine import Database
    from svbg.tg.ui.context import UserCtx

__all__ = ["DeviceCache", "StatusReader", "SubInfo", "UserStatus", "status_query", "wallet_users"]

#: ``users`` as the user path reads it (``wallet_minor`` is added by the stage 2 migration).
wallet_users: Final = sa.table(
    "users",
    sa.column("id", sa.BigInteger),
    sa.column("telegram_id", sa.BigInteger),
    sa.column("first_name", sa.Text),
    sa.column("username", sa.Text),
    sa.column("wallet_minor", sa.BigInteger),
    sa.column("banned_at", UtcDateTime),
    sa.column("created_at", UtcDateTime),
)

LIVE: Final = ("pending", "linked")


@dataclass(frozen=True, slots=True)
class DeviceCache:
    devices: tuple[Mapping[str, Any], ...]
    fetched_at: datetime | None

    def fresh(self, at: datetime, ttl_s: float) -> bool:
        return self.fetched_at is not None and (at - self.fetched_at).total_seconds() <= ttl_s


@dataclass(frozen=True, slots=True)
class SubInfo:
    id: int
    plan_id: int | None
    plan_snapshot: Mapping[str, Any]
    is_trial: bool
    paid_until: datetime | None
    link_state: str
    subscription_url: str | None
    hold_kind: str | None
    extra_devices: int
    device_limit: int | None
    traffic_bytes: int | None
    used_traffic: int | None
    panel_user_id: int | None

    def seconds_left(self, at: datetime) -> float:
        return 0.0 if self.paid_until is None else (self.paid_until - at).total_seconds()

    def plan_title(self, lang: str) -> str:
        name = self.plan_snapshot.get("name")
        if isinstance(name, Mapping):
            text = name.get(lang) or name.get("ru") or next((v for v in name.values() if v), "")
            if isinstance(text, str) and text:
                return text
        code = self.plan_snapshot.get("code")
        return str(code) if code else ""

    @property
    def connectable(self) -> bool:
        return self.link_state == "linked" and bool(self.subscription_url) and self.hold_kind is None


@dataclass(frozen=True, slots=True)
class UserStatus:
    user_id: int
    telegram_id: int | None
    first_name: str | None
    balance_minor: int
    has_paid: bool
    trial_used: bool
    had_subscription: bool
    banned: bool
    sub: SubInfo | None
    devices: DeviceCache | None = None
    username: str | None = None
    created_at: datetime | None = None

    def sub_state(self, at: datetime) -> str:
        sub = self.sub
        if sub is None:
            return "none"
        if sub.hold_kind is not None:
            return "frozen"
        if sub.seconds_left(at) <= 0:
            return "expired"
        return "trial" if sub.is_trial else "active"

    def days_left(self, at: datetime) -> int | None:
        if self.sub is None or self.sub.paid_until is None:
            return None
        return max(0, math.ceil(self.sub.seconds_left(at) / 86_400))

    def trial_offer(self, trial_days: int) -> bool:
        """The «🎁 Попробовать бесплатно» button: trial on, never taken, never subscribed (cheap facts only;
        the click re-checks everything, including the channel)."""
        return trial_days > 0 and not self.trial_used and not self.had_subscription and not self.banned

    def enrich(self, user: UserCtx, *, trial_days: int = 0, at: datetime | None = None) -> UserCtx:
        at = at or now()
        flags = set(user.flags)
        if self.trial_offer(trial_days):
            flags.add("trial")
        else:
            flags.discard("trial")
        plan_code = None
        if self.sub is not None:
            code = self.sub.plan_snapshot.get("code")
            plan_code = str(code) if code else None
        return replace(
            user,
            sub_state=self.sub_state(at),
            days_left=self.days_left(at),
            balance_minor=self.balance_minor,
            has_paid=self.has_paid,
            plan_code=plan_code,
            flags=frozenset(flags),
            _placeholders=None,
        )


def status_query(user_id: int, *, with_devices: bool = False) -> sa.Select[Any]:
    """One statement: the user + the latest not-closed subscription (live first) + optional device cache."""
    live_first = sa.case((subscriptions.c.link_state.in_(LIVE), 0), else_=1)
    sub = (
        sa.select(
            subscriptions.c.id,
            subscriptions.c.plan_id,
            subscriptions.c.plan_snapshot,
            subscriptions.c.is_trial,
            subscriptions.c.paid_until,
            subscriptions.c.link_state,
            subscriptions.c.subscription_url,
            subscriptions.c.hold_kind,
            subscriptions.c.extra_devices,
            subscriptions.c.desired_device_limit,
            subscriptions.c.desired_traffic_bytes,
            subscriptions.c.panel_used_traffic,
            subscriptions.c.panel_user_id,
        )
        .where(subscriptions.c.user_id == wallet_users.c.id, subscriptions.c.link_state != "closed")
        .order_by(live_first, subscriptions.c.id.desc())
        .limit(1)
        .correlate(wallet_users)
        .lateral("s")
    )
    has_paid = (
        sa.select(sa.literal(1))
        .where(orders.c.user_id == wallet_users.c.id, orders.c.status.in_(("paid", "fulfilled")))
        .correlate(wallet_users)
        .exists()
    )
    trial_used = (
        sa.select(sa.literal(1))
        .where(
            sa.or_(
                trial_grants.c.user_id == wallet_users.c.id,
                sa.and_(
                    wallet_users.c.telegram_id.is_not(None),
                    trial_grants.c.telegram_id == wallet_users.c.telegram_id,
                ),
            )
        )
        .correlate(wallet_users)
        .exists()
    )
    had = (
        sa.select(sa.literal(1))
        .where(subscriptions.c.user_id == wallet_users.c.id)
        .correlate(wallet_users)
        .exists()
    )
    cols: list[Any] = [
        wallet_users.c.id.label("user_id"),
        wallet_users.c.telegram_id,
        wallet_users.c.first_name,
        wallet_users.c.username,
        wallet_users.c.wallet_minor,
        wallet_users.c.banned_at,
        wallet_users.c.created_at,
        has_paid.label("has_paid"),
        trial_used.label("trial_used"),
        had.label("had_subscription"),
        sub.c.id.label("sub_id"),
        sub.c.plan_id,
        sub.c.plan_snapshot,
        sub.c.is_trial,
        sub.c.paid_until,
        sub.c.link_state,
        sub.c.subscription_url,
        sub.c.hold_kind,
        sub.c.extra_devices,
        sub.c.desired_device_limit,
        sub.c.desired_traffic_bytes,
        sub.c.panel_used_traffic,
        sub.c.panel_user_id,
    ]
    from_: Any = wallet_users.outerjoin(sub, sa.true())
    if with_devices:
        cols += [user_devices.c.devices.label("dev_list"), user_devices.c.fetched_at.label("dev_at")]
        from_ = from_.outerjoin(user_devices, user_devices.c.subscription_id == sub.c.id)
    return sa.select(*cols).select_from(from_).where(wallet_users.c.id == user_id)


def _status(row: Mapping[str, Any], with_devices: bool) -> UserStatus:
    sub: SubInfo | None = None
    if row["sub_id"] is not None:
        snap = row["plan_snapshot"]
        limit = row["desired_device_limit"]
        sub = SubInfo(
            id=int(row["sub_id"]),
            plan_id=row["plan_id"],
            plan_snapshot=snap if isinstance(snap, Mapping) else {},
            is_trial=bool(row["is_trial"]),
            paid_until=row["paid_until"],
            link_state=str(row["link_state"]),
            subscription_url=row["subscription_url"],
            hold_kind=row["hold_kind"],
            extra_devices=int(row["extra_devices"] or 0),
            device_limit=None if limit is None else int(limit),
            traffic_bytes=row["desired_traffic_bytes"],
            used_traffic=row["panel_used_traffic"],
            panel_user_id=row["panel_user_id"],
        )
    devices: DeviceCache | None = None
    if with_devices and row.get("dev_at") is not None:
        raw = row.get("dev_list")
        items = tuple(d for d in raw if isinstance(d, Mapping)) if isinstance(raw, list) else ()
        devices = DeviceCache(items, row["dev_at"])
    return UserStatus(
        user_id=int(row["user_id"]),
        telegram_id=row["telegram_id"],
        first_name=row["first_name"],
        balance_minor=int(row["wallet_minor"] or 0),
        has_paid=bool(row["has_paid"]),
        trial_used=bool(row["trial_used"]),
        had_subscription=bool(row["had_subscription"]),
        banned=row["banned_at"] is not None,
        sub=sub,
        devices=devices,
        username=row.get("username"),
        created_at=row.get("created_at"),
    )


class StatusReader:
    """Reads :class:`UserStatus` (1 SQL); ``trial_days`` is the current ``TRIAL_DAYS`` for :meth:`enrich`."""

    def __init__(self, db: Database, *, trial_days: Callable[[], int] = lambda: 0) -> None:
        self._db = db
        self.trial_days = trial_days

    async def load(self, user_id: int, *, with_devices: bool = False) -> UserStatus | None:
        async with self._db.read() as conn:
            row = (await conn.execute(status_query(user_id, with_devices=with_devices))).mappings().first()
        return None if row is None else _status(row, with_devices)

    async def enriched(
        self, user: UserCtx, *, with_devices: bool = False
    ) -> tuple[UserCtx, UserStatus | None]:
        """``(enriched UserCtx, status)``; the context is unchanged when the user row is gone."""
        status = await self.load(user.user_id, with_devices=with_devices)
        if status is None:
            return user, None
        return status.enrich(user, trial_days=self.trial_days()), status
