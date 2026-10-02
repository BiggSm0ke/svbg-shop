"""Freeze ("hold") of a subscription and the money gate ``can_spend`` (05 §2.2.4, §3.2 X5).

Accumulator model: on freeze ``hold_frozen_seconds := max(0, paid_until − now)`` and the panel user is
disabled; while frozen every grant goes into the accumulator (:meth:`SubscriptionLifecycle.extend`); on
unfreeze ``paid_until := now + hold_frozen_seconds`` and the panel user is enabled again. Outcomes of
unfreeze: ``active`` (≥ 5 min left), ``expired`` (less than that), ``zeroed`` (the term was zeroed and
nothing granted).

A frozen user cannot pay **anywhere**: payments core, checkout, top-up and fulfill call :func:`can_spend`
before money moves (one SQL; :func:`spend_check` when the caller already has the row).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Literal

import sqlalchemy as sa

from svbg.core.clock import now
from svbg.core.tables import users
from svbg.remnawave.writer import K_DISABLE, K_ENABLE, enqueue_action, enqueue_update
from svbg.subscriptions import hooks, journal
from svbg.subscriptions.lifecycle import LIVE_STATES, SubscriptionError
from svbg.subscriptions.tables import HOLD_KINDS, subscriptions

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sqlalchemy.ext.asyncio import AsyncConnection

__all__ = [
    "MIN_ACTIVE_LEFT",
    "SPEND_TEXTS",
    "FreezeResult",
    "SpendCheck",
    "UnfreezeResult",
    "can_spend",
    "disabled_reason_for",
    "freeze",
    "localize_spend",
    "spend_check",
    "unfreeze",
    "zero_hold",
]

#: Less than this left after unfreeze → outcome ``expired`` (05 §2.2.4).
MIN_ACTIVE_LEFT: Final = timedelta(minutes=5)

SPEND_TEXTS: Final = {
    "frozen": "Подписка приостановлена — оплата сейчас недоступна. Напишите в поддержку.",
    "banned": "Доступ ограничен — оплата недоступна. Напишите в поддержку.",
    "no_user": "Пользователь не найден.",
}
#: English of :data:`SPEND_TEXTS` (same keys).
SPEND_TEXTS_EN: Final = {
    "frozen": "Your subscription is on hold — payment is not available right now. Please contact support.",
    "banned": "Access is restricted — payment is not available. Please contact support.",
    "no_user": "User not found.",
}


def localize_spend(text: str | None, lang: str | None) -> str:
    """A refusal of :data:`SPEND_TEXTS` (as the guards return it) in ``lang``; any other text stays."""
    value = text or ""
    if lang == "en":
        for key, ru in SPEND_TEXTS.items():
            if value == ru:
                return SPEND_TEXTS_EN[key]
    return value


Outcome = Literal["active", "expired", "zeroed"]


@dataclass(frozen=True, slots=True)
class SpendCheck:
    ok: bool
    reason: str | None = None  # frozen | banned | no_user
    hold_kind: str | None = None

    @property
    def text(self) -> str:
        return SPEND_TEXTS.get(self.reason or "", "")

    def __bool__(self) -> bool:
        return self.ok


def spend_check(*, exists: bool = True, banned_at: datetime | None, hold_kind: str | None) -> SpendCheck:
    """Pure form of :func:`can_spend` for a caller that already loaded the user and its holds."""
    if not exists:
        return SpendCheck(False, "no_user")
    if banned_at is not None:
        return SpendCheck(False, "banned")
    if hold_kind is not None:
        return SpendCheck(False, "frozen", hold_kind)
    return SpendCheck(True)


async def can_spend(conn: AsyncConnection, user_id: int, subscription_id: int | None = None) -> SpendCheck:
    """May ``user_id`` create a payment / be charged? One SQL. A hold on **any** of the user's subscriptions
    blocks money for the user (owner decision: a frozen user cannot pay anywhere); ``subscription_id`` is
    accepted for the X5 signature and does not narrow the check."""
    del subscription_id
    held = (
        sa.select(subscriptions.c.hold_kind)
        .where(subscriptions.c.user_id == users.c.id, subscriptions.c.hold_kind.is_not(None))
        .limit(1)
        .correlate(users)
        .scalar_subquery()
    )
    row = (
        await conn.execute(sa.select(users.c.banned_at, held.label("hold_kind")).where(users.c.id == user_id))
    ).first()
    if row is None:
        return spend_check(exists=False, banned_at=None, hold_kind=None)
    return spend_check(banned_at=row.banned_at, hold_kind=row.hold_kind)


def disabled_reason_for(kind: str) -> str:
    """``disabled_reason`` the panel user gets while frozen with ``kind``."""
    return "ip_guard" if kind == "ip_guard" else "hold"


@dataclass(frozen=True, slots=True)
class FreezeResult:
    subscription_id: int
    frozen_seconds: int
    already: bool = False


@dataclass(frozen=True, slots=True)
class UnfreezeResult:
    subscription_id: int
    outcome: Outcome | None  # None: was not frozen
    paid_until: datetime | None


async def _lock(conn: AsyncConnection, sid: int) -> Mapping[str, Any]:
    row = (
        (await conn.execute(sa.select(subscriptions).where(subscriptions.c.id == sid).with_for_update()))
        .mappings()
        .first()
    )
    if row is None or row["link_state"] not in LIVE_STATES:
        raise SubscriptionError("no_subscription", "Подписка не найдена или закрыта.")
    return row


async def freeze(
    conn: AsyncConnection,
    subscription_id: int,
    kind: str,
    *,
    reason: str,
    actor_id: int | None = None,
    source: str = "bot",
    caused_by: str | None = None,
) -> FreezeResult:
    """Freeze: remaining time goes to the accumulator, the panel user is disabled. Idempotent."""
    if kind not in HOLD_KINDS:
        raise ValueError(f"hold kind must be one of {HOLD_KINDS}")
    if not reason or not reason.strip():
        raise ValueError("причина заморозки обязательна")
    row = await _lock(conn, subscription_id)
    if row["hold_kind"] is not None:
        return FreezeResult(subscription_id, int(row["hold_frozen_seconds"]), already=True)
    at = now()
    paid_until = row["paid_until"]
    frozen = max(0, int((paid_until - at).total_seconds())) if paid_until is not None else 0
    await conn.execute(
        sa.update(subscriptions)
        .where(subscriptions.c.id == subscription_id)
        .values(
            hold_kind=kind,
            hold_since=at,
            hold_frozen_seconds=frozen,
            hold_zeroed=False,
            # Intent first (the writer confirms it on execution): checks never see a frozen "active" row.
            desired_status="disabled",
            disabled_reason=disabled_reason_for(kind),
            updated_at=sa.func.now(),
        )
    )
    await enqueue_action(
        conn, subscription_id, K_DISABLE, {"reason": disabled_reason_for(kind)}, caused_by=caused_by
    )
    details = {"hold_kind": kind, "reason": reason.strip(), "frozen_seconds": frozen, "actor_id": actor_id}
    await journal.record(
        conn, subscription_id, "frozen", source=source, old_expire=paid_until, details=details
    )
    await hooks.emit(
        conn,
        "subscription.frozen",
        {"subscription_id": subscription_id, "user_id": row["user_id"], **details},
        caused_by=caused_by,
    )
    return FreezeResult(subscription_id, frozen)


async def zero_hold(
    conn: AsyncConnection,
    subscription_id: int,
    *,
    reason: str,
    actor_id: int | None = None,
    source: str = "admin",
) -> None:
    """Zero a frozen subscription ("обнулить"): the accumulator becomes 0, ``hold_zeroed`` is set."""
    row = await _lock(conn, subscription_id)
    if row["hold_kind"] is None:
        raise SubscriptionError("not_frozen", "Подписка не заморожена.")
    await conn.execute(
        sa.update(subscriptions)
        .where(subscriptions.c.id == subscription_id)
        .values(hold_frozen_seconds=0, hold_zeroed=True, updated_at=sa.func.now())
    )
    await journal.record(
        conn,
        subscription_id,
        "hold_zeroed",
        source=source,
        delta_seconds=-int(row["hold_frozen_seconds"]),
        details={"reason": reason, "actor_id": actor_id},
    )


async def unfreeze(
    conn: AsyncConnection,
    subscription_id: int,
    *,
    actor_id: int | None = None,
    source: str = "bot",
    keep_disabled: str | None = None,
    caused_by: str | None = None,
) -> UnfreezeResult:
    """``paid_until := now + accumulator``; the panel date is pushed, then the user is enabled again.

    ``keep_disabled``: another reason still applies (e.g. ``channel_left`` from the required channel
    service) — the panel user then stays disabled with that reason instead of being enabled.
    """
    row = await _lock(conn, subscription_id)
    kind = row["hold_kind"]
    if kind is None:
        return UnfreezeResult(subscription_id, None, row["paid_until"])
    at = now()
    seconds = int(row["hold_frozen_seconds"])
    new = at + timedelta(seconds=seconds)
    outcome: Outcome
    if row["hold_zeroed"] and seconds == 0:
        outcome = "zeroed"
    elif timedelta(seconds=seconds) < MIN_ACTIVE_LEFT:
        outcome = "expired"
    else:
        outcome = "active"
    await conn.execute(
        sa.update(subscriptions)
        .where(subscriptions.c.id == subscription_id)
        .values(
            paid_until=new,
            desired_expire_at=new,
            hold_kind=None,
            hold_since=None,
            hold_frozen_seconds=0,
            hold_zeroed=False,
            updated_at=sa.func.now(),
        )
    )
    # FIFO per subscription: the new date reaches the panel before the enable (which checks it, 02 §4.9).
    await enqueue_update(conn, subscription_id, ["expire"], clear_overrides=("expire",), caused_by=caused_by)
    if keep_disabled is not None:
        await conn.execute(
            sa.update(subscriptions)
            .where(subscriptions.c.id == subscription_id)
            .values(disabled_reason=keep_disabled)
        )
        await enqueue_action(conn, subscription_id, K_DISABLE, {"reason": keep_disabled}, caused_by=caused_by)
    else:
        await enqueue_action(
            conn,
            subscription_id,
            K_ENABLE,
            {"only_reason": disabled_reason_for(str(kind))},
            caused_by=caused_by,
        )
    details = {"hold_kind": kind, "outcome": outcome, "actor_id": actor_id, "frozen_seconds": seconds}
    await journal.record(
        conn,
        subscription_id,
        "unfrozen",
        source=source,
        old_expire=row["paid_until"],
        new_expire=new,
        details=details,
    )
    payload = {"subscription_id": subscription_id, "user_id": row["user_id"], **details}
    await hooks.emit(conn, "subscription.unfrozen", payload, caused_by=caused_by)
    await hooks.emit(
        conn,
        "subscription.term_changed",
        {**payload, "kind": "unfrozen", "old_paid_until": row["paid_until"], "new_paid_until": new},
        caused_by=caused_by,
    )
    return UnfreezeResult(subscription_id, outcome, new)
