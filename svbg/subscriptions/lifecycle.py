"""Paid and granted changes of a subscription: purchase / renew / plan change, device addon, extend (X2, X5).

Called by billing's ``fulfill`` handler (and admin grants) **inside their transaction**: the order status, the
subscription change, the audit row in ``subscription_events``, the panel job (through
:mod:`svbg.remnawave.writer`) and the domain event (:mod:`svbg.subscriptions.hooks`) commit together or not at
all. Nothing here talks to the panel.

Rules:

* **one subscription per user** (06 M1): a purchase renews / converts the user's live subscription
  (``pending``/``linked``) or creates one when there is none (closed and ``panel_missing`` ones are history);
* **absolute targets**: the new ``paid_until`` is computed once, from the bot's own ``paid_until`` (the panel
  is not the source of money), and pushed as an absolute ``expireAt`` PATCH — a retry repeats the same date.
  An admin's manual extension in the panel (``overrides.expire``) is kept as the base, then the override is
  cleared so the paid term reaches the panel;
* **trial remainder is kept** (06 M2): converting a trial adds the purchased days to what is left of it;
* **plan change** keeps the remaining term by default; billing passes ``replace_term=True`` with
  ``extra_seconds`` (the converted remainder) when it priced the conversion itself;
* **frozen** subscriptions (X5) never move ``paid_until``: granted seconds are added to
  ``hold_frozen_seconds`` and arrive on unfreeze;
* **exactly once**: every operation carries a reference (``order``/<id>); a second run with the same reference
  returns the first result (``duplicate=True``) and changes nothing. Concurrent operations of one user are
  serialized by a row lock on ``users``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Literal

import sqlalchemy as sa

from svbg.core.clock import now
from svbg.core.tables import users
from svbg.remnawave.models import FOREVER
from svbg.remnawave.writer import UPDATE_FIELDS, enqueue_update
from svbg.subscriptions import hooks, journal
from svbg.subscriptions.service import Desired, SubscriptionService
from svbg.subscriptions.tables import subscriptions
from svbg.subscriptions.terms import PlanTerms

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

__all__ = [
    "LIVE_STATES",
    "PURCHASE_KINDS",
    "Applied",
    "SubscriptionError",
    "SubscriptionLifecycle",
    "term_base",
]

LIVE_STATES: Final = ("pending", "linked")
DAY: Final = 86_400
MAX_DAYS: Final = 36_500
#: Event kinds of a purchase (one of them per order reference).
PURCHASE_KINDS: Final = ("purchase_new", "purchase_renew", "trial_converted", "plan_changed")
_ALL_FIELDS: Final = tuple(UPDATE_FIELDS)

Action = Literal["created", "renewed", "converted", "changed", "extended", "frozen_credit", "devices_added"]


class SubscriptionError(Exception):
    """A refused operation; ``text`` is ready for the user / admin (Russian)."""

    def __init__(self, code: str, text: str) -> None:
        super().__init__(f"{code}: {text}")
        self.code = code
        self.text = text


@dataclass(frozen=True, slots=True)
class Applied:
    """Outcome of one operation (``duplicate``: the same reference was applied earlier, nothing changed)."""

    subscription_id: int
    action: Action
    old_paid_until: datetime | None
    new_paid_until: datetime | None
    is_trial_before: bool = False
    is_trial_after: bool = False
    frozen: bool = False
    duplicate: bool = False


def _cap(value: datetime) -> datetime:
    return min(value, FOREVER)


def term_base(row: Mapping[str, Any], at: datetime) -> datetime:
    """Where added time starts: ``max(paid_until, now)``, or the panel's date if an admin extended it."""
    base = max(row["paid_until"] or at, at)
    overrides = row.get("overrides") or {}
    panel_expire = row.get("panel_expire_at")
    if "expire" in overrides and panel_expire is not None and panel_expire > base:
        base = panel_expire  # the admin's manual extension in the panel is not lost (02 §6.3)
    return base


def _check_days(days: int) -> None:
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= MAX_DAYS:
        raise ValueError(f"days must be an int in 1..{MAX_DAYS}")


def _plan_terms_of(row: Mapping[str, Any]) -> PlanTerms | None:
    try:
        return PlanTerms.from_snapshot(row["plan_snapshot"] or {})
    except (TypeError, ValueError):
        return None  # legacy / imported subscription without a usable snapshot


class SubscriptionLifecycle:
    """Stateless; every method works in the caller's transaction."""

    def __init__(self, service: SubscriptionService | None = None) -> None:
        self.service = service or SubscriptionService()

    # ------------------------------------------------------------------------------------------ purchase

    async def purchase(
        self,
        conn: AsyncConnection,
        *,
        user_id: int,
        terms: PlanTerms | Mapping[str, Any],
        days: int,
        ref_id: str,
        ref_type: str = "order",
        extra_devices: int | None = None,
        replace_term: bool = False,
        extra_seconds: int = 0,
        source: str = "bot",
        caused_by: str | None = None,
        lane: str = "interactive",
    ) -> Applied:
        """Apply a paid plan period (order kinds ``new``/``renew``/``change``; the case is decided here).

        ``extra_devices``: paid devices for the new period (``None`` keeps the current number).
        """
        _check_days(days)
        plan = PlanTerms.from_snapshot(terms)
        if extra_seconds < 0:
            raise ValueError("extra_seconds must be >= 0")
        await self._lock_user(conn, user_id)
        done = await journal.find_by_ref(conn, ref_type, ref_id, PURCHASE_KINDS)
        if done is not None:
            return self._duplicate(done)
        at = now()
        row = await self._live_sub(conn, user_id)
        if row is None:
            return await self._create_paid(
                conn, user_id, plan, days, extra_devices or 0, ref_type, ref_id, source, caused_by, lane, at
            )
        sid = int(row["id"])
        is_trial = bool(row["is_trial"])
        same_plan = not is_trial and plan.plan_id is not None and row["plan_id"] == plan.plan_id
        extra = int(row["extra_devices"]) if extra_devices is None else extra_devices
        seconds = days * DAY + extra_seconds
        values: dict[str, Any] = {}
        fields: set[str] = {"expire"}
        if same_plan:
            own = _plan_terms_of(row) or plan
            self._check_extra(own, extra)
            if extra != row["extra_devices"]:
                values["extra_devices"] = extra
                values["desired_device_limit"] = own.device_limit_with(extra)
                fields.add("device_limit")
            kind, action = "purchase_renew", "renewed"
        else:
            self._check_extra(plan, extra)
            values.update(
                plan_id=plan.plan_id,
                plan_snapshot=plan.to_snapshot(),
                is_trial=False,
                extra_devices=extra,
                desired_squads=list(plan.squads),
                desired_traffic_bytes=plan.traffic_bytes,
                desired_reset_strategy=plan.reset_strategy,
                desired_device_limit=plan.device_limit_with(extra),
                desired_ext_squad=plan.ext_squad,
                desired_tag=plan.panel_tag,
            )
            fields.update(_ALL_FIELDS)
            kind, action = ("trial_converted", "converted") if is_trial else ("plan_changed", "changed")
        old = row["paid_until"]
        frozen = row["hold_kind"] is not None
        if frozen:
            # X5: the term waits in the frozen balance; paid_until and the panel date stay as they are.
            values["hold_frozen_seconds"] = subscriptions.c.hold_frozen_seconds + seconds
            fields.discard("expire")
            new = old
        else:
            start = at if (replace_term and not same_plan) else term_base(row, at)
            new = _cap(start + timedelta(seconds=seconds))
            values.update(paid_until=new, desired_expire_at=new)
        await conn.execute(
            sa.update(subscriptions)
            .where(subscriptions.c.id == sid)
            .values(**values, updated_at=sa.func.now())
        )
        if fields:
            await enqueue_update(
                conn,
                sid,
                fields,
                clear_overrides=("expire",) if "expire" in fields else (),
                lane=lane,
                caused_by=caused_by,
            )
        details = {
            "is_trial_before": is_trial,
            "is_trial_after": False,
            "plan_id": plan.plan_id,
            "days": days,
            "extra_devices": extra,
            "frozen": frozen,
        }
        await journal.record(
            conn,
            sid,
            kind,
            source=source,
            old_expire=old,
            new_expire=new,
            delta_seconds=seconds,
            ref_type=ref_type,
            ref_id=ref_id,
            details=details,
        )
        await self._announce(conn, sid, user_id, kind, old, new, details, caused_by)
        return Applied(sid, action, old, new, is_trial, False, frozen)

    async def _create_paid(  # noqa: PLR0917 - internal step of purchase()
        self,
        conn: AsyncConnection,
        user_id: int,
        plan: PlanTerms,
        days: int,
        extra: int,
        ref_type: str,
        ref_id: str,
        source: str,
        caused_by: str | None,
        lane: str,
        at: datetime,
    ) -> Applied:
        self._check_extra(plan, extra)
        telegram_id = await conn.scalar(sa.select(users.c.telegram_id).where(users.c.id == user_id))
        new = _cap(at + timedelta(days=days))
        desired = Desired(
            expire_at=new,
            squads=plan.squads,
            traffic_bytes=plan.traffic_bytes,
            reset_strategy=plan.reset_strategy,
            device_limit=plan.device_limit_with(extra),
            ext_squad=plan.ext_squad,
            tag=plan.panel_tag,
        )
        sid = await self.service.create(
            conn,
            user_id=user_id,
            telegram_id=telegram_id,
            desired=desired,
            plan_id=plan.plan_id,
            plan_snapshot=plan.to_snapshot(),
            caused_by=caused_by,
            lane=lane,
            extra_devices=extra,
        )
        details = {
            "is_trial_before": False,
            "is_trial_after": False,
            "plan_id": plan.plan_id,
            "days": days,
            "extra_devices": extra,
            "frozen": False,
        }
        await journal.record(
            conn,
            sid,
            "purchase_new",
            source=source,
            new_expire=new,
            delta_seconds=days * DAY,
            ref_type=ref_type,
            ref_id=ref_id,
            details=details,
        )
        await self._announce(conn, sid, user_id, "purchase_new", None, new, details, caused_by)
        return Applied(sid, "created", None, new)

    # ------------------------------------------------------------------------------------- device addon

    async def add_devices(
        self,
        conn: AsyncConnection,
        subscription_id: int,
        count: int,
        *,
        ref_id: str,
        ref_type: str = "order",
        source: str = "bot",
        caused_by: str | None = None,
        lane: str = "interactive",
    ) -> Applied:
        """Paid extra devices for the current period (order kind ``addon_devices``, 02 §4.6)."""
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError("count must be a positive int")
        row = await self._lock_sub(conn, subscription_id)
        done = await journal.find_by_ref(conn, ref_type, ref_id, ("devices_added",))
        if done is not None:
            return self._duplicate(done)
        plan = _plan_terms_of(row)
        if plan is None or not plan.addon_available:
            raise SubscriptionError("addon_unavailable", "Для этого тарифа докупка устройств недоступна.")
        extra = int(row["extra_devices"]) + count
        self._check_extra(plan, extra)
        limit = plan.device_limit_with(extra)
        await conn.execute(
            sa.update(subscriptions)
            .where(subscriptions.c.id == subscription_id)
            .values(extra_devices=extra, desired_device_limit=limit, updated_at=sa.func.now())
        )
        await enqueue_update(
            conn,
            subscription_id,
            ["device_limit"],
            clear_overrides=("device_limit",),
            lane=lane,
            caused_by=caused_by,
        )
        details = {"added": count, "extra_devices": extra, "device_limit": limit}
        await journal.record(
            conn,
            subscription_id,
            "devices_added",
            source=source,
            ref_type=ref_type,
            ref_id=ref_id,
            details=details,
        )
        await hooks.emit(
            conn,
            "subscription.devices_changed",
            {"subscription_id": subscription_id, "user_id": row["user_id"], **details},
            caused_by=caused_by,
        )
        return Applied(
            subscription_id,
            "devices_added",
            row["paid_until"],
            row["paid_until"],
            bool(row["is_trial"]),
            bool(row["is_trial"]),
            row["hold_kind"] is not None,
        )

    @staticmethod
    def _check_extra(plan: PlanTerms, extra: int) -> None:
        if extra < 0:
            raise ValueError("extra_devices must be >= 0")
        if extra == 0:
            return
        if not plan.addon_available:
            raise SubscriptionError("addon_unavailable", "Для этого тарифа докупка устройств недоступна.")
        total = (plan.device_limit or 0) + extra
        if plan.max_devices is not None and total > plan.max_devices:
            raise SubscriptionError(
                "addon_limit", f"Можно не больше {plan.max_devices} устройств на подписку."
            )

    # ------------------------------------------------------------------------------------------- extend

    async def extend(
        self,
        conn: AsyncConnection,
        subscription_id: int,
        seconds: int,
        *,
        source: str,
        kind: str = "extended",
        ref_type: str | None = None,
        ref_id: str | None = None,
        reason: str | None = None,
        caused_by: str | None = None,
        lane: str = "interactive",
    ) -> Applied:
        """Granted time (admin ±N days, referral days, compensation). Negative ``seconds`` shortens the term
        (never below now — the panel then expires the user itself). Frozen: credited to the frozen balance."""
        if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds == 0:
            raise ValueError("seconds must be a non-zero int")
        if (ref_type is None) != (ref_id is None):
            raise ValueError("ref_type and ref_id go together")
        row = await self._lock_sub(conn, subscription_id)
        if ref_type is not None and ref_id is not None:
            done = await journal.find_by_ref(conn, ref_type, ref_id, (kind,))
            if done is not None:
                return self._duplicate(done)
        at = now()
        old = row["paid_until"]
        frozen = row["hold_kind"] is not None
        details: dict[str, Any] = {"seconds": seconds, "frozen": frozen, "is_trial": bool(row["is_trial"])}
        if reason:
            details["reason"] = reason
        if frozen:
            credited = max(0, int(row["hold_frozen_seconds"]) + seconds)
            await conn.execute(
                sa.update(subscriptions)
                .where(subscriptions.c.id == subscription_id)
                .values(hold_frozen_seconds=credited, updated_at=sa.func.now())
            )
            new = old
        else:
            base = term_base(row, at) if seconds > 0 else (old or at)
            new = _cap(max(base + timedelta(seconds=seconds), at))
            await conn.execute(
                sa.update(subscriptions)
                .where(subscriptions.c.id == subscription_id)
                .values(paid_until=new, desired_expire_at=new, updated_at=sa.func.now())
            )
            await enqueue_update(
                conn, subscription_id, ["expire"], clear_overrides=("expire",), lane=lane, caused_by=caused_by
            )
        await journal.record(
            conn,
            subscription_id,
            kind,
            source=source,
            old_expire=old,
            new_expire=new,
            delta_seconds=seconds,
            ref_type=ref_type,
            ref_id=ref_id,
            details=details,
        )
        await self._announce(conn, subscription_id, row["user_id"], kind, old, new, details, caused_by)
        return Applied(
            subscription_id,
            "frozen_credit" if frozen else "extended",
            old,
            new,
            bool(row["is_trial"]),
            bool(row["is_trial"]),
            frozen,
        )

    # ------------------------------------------------------------------------------------------ helpers

    @staticmethod
    async def _lock_user(conn: AsyncConnection, user_id: int) -> None:
        """Serializes every subscription change of one user (no two "first" subscriptions in a race)."""
        found = await conn.scalar(sa.select(users.c.id).where(users.c.id == user_id).with_for_update())
        if found is None:
            raise SubscriptionError("no_user", "Пользователь не найден.")

    @staticmethod
    async def _live_sub(conn: AsyncConnection, user_id: int) -> Mapping[str, Any] | None:
        return (
            (
                await conn.execute(
                    sa.select(subscriptions)
                    .where(subscriptions.c.user_id == user_id, subscriptions.c.link_state.in_(LIVE_STATES))
                    .order_by(subscriptions.c.id.desc())
                    .limit(1)
                    .with_for_update()
                )
            )
            .mappings()
            .first()
        )

    @staticmethod
    async def _lock_sub(conn: AsyncConnection, subscription_id: int) -> Mapping[str, Any]:
        row = (
            (
                await conn.execute(
                    sa.select(subscriptions).where(subscriptions.c.id == subscription_id).with_for_update()
                )
            )
            .mappings()
            .first()
        )
        if row is None or row["link_state"] not in LIVE_STATES:
            raise SubscriptionError("no_subscription", "Подписка не найдена или закрыта.")
        return row

    @staticmethod
    def _duplicate(done: journal.Recorded) -> Applied:
        action_of: dict[str, Action] = {
            "purchase_new": "created",
            "purchase_renew": "renewed",
            "trial_converted": "converted",
            "plan_changed": "changed",
            "devices_added": "devices_added",
        }
        details = done.details
        return Applied(
            done.subscription_id,
            action_of.get(done.kind, "extended"),
            done.old_expire,
            done.new_expire,
            bool(details.get("is_trial_before", False)),
            bool(details.get("is_trial_after", False)),
            bool(details.get("frozen", False)),
            duplicate=True,
        )

    @staticmethod
    async def _announce(  # noqa: PLR0917 - flat event payload
        conn: AsyncConnection,
        sid: int,
        user_id: int | None,
        kind: str,
        old: datetime | None,
        new: datetime | None,
        details: Mapping[str, Any],
        caused_by: str | None,
    ) -> None:
        await hooks.emit(
            conn,
            "subscription.term_changed",
            {
                "subscription_id": sid,
                "user_id": user_id,
                "kind": kind,
                "old_paid_until": old,
                "new_paid_until": new,
                **details,
            },
            caused_by=caused_by,
        )
