"""Pure referral rules (05 §2.3.1, §2.3.3): what to do with one «inviter ↔ invited» pair. No I/O.

:func:`decide` gets everything as plain data — the settings (:class:`Rules`), the state of the pair
(:class:`PairState`: existing reward rows, who has a subscription, whether the invited user meets the trigger)
and the inviter's counters — and returns a :class:`Decision` with one :class:`SideAction` per side.

Owner rules (prod: days mode, 14 / 7, ``trial_or_paid``, cap 20 per rolling 30 days):

* the pair is closed only when **both** sides are settled (granted / expired / denied / legacy, or switched
  off with ``0`` days);
* a side that cannot be granted (the recipient has no subscription, the inviter hit a cap) becomes
  ``deferred`` for :data:`RETRY_WINDOW` (168 h) and is re-checked when the recipient gets a subscription or a
  slot under the cap frees; after the window it is ``expired`` silently;
* the invited side is granted regardless of the inviter's caps;
* self-referral is denied; nothing happens until the invited user meets the trigger (``register``: at once);
* the admin hears about a deferred side once (on the transition), never on every re-check (lesson 1 of 05
  §2.3.5): ``Decision.newly_deferred`` lists only fresh deferrals.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Final

__all__ = [
    "DEFAULTS",
    "RETRY_WINDOW",
    "SETTLED",
    "Action",
    "Decision",
    "InviterStats",
    "Mode",
    "PairState",
    "Rules",
    "SideAction",
    "SideState",
    "Trigger",
    "decide",
    "percent_reward",
]

#: How long a deferred side waits for a subscription or a free slot (05 §2.3.4: a constant).
RETRY_WINDOW: Final = timedelta(hours=168)
#: Reward statuses after which a side never changes again.
SETTLED: Final = frozenset({"granted", "expired", "denied", "legacy"})
MAX_DAYS: Final = 365
MAX_PERCENT: Final = 100


class Mode(StrEnum):
    DAYS = "days"
    PERCENT = "percent"


class Trigger(StrEnum):
    PAID = "paid"
    TRIAL_OR_PAID = "trial_or_paid"
    REGISTER = "register"


class Action(StrEnum):
    NONE = "none"  # nothing to do now (settled, switched off, trigger not met yet)
    GRANT = "grant"
    DEFER = "defer"  # new deferral or still deferred (``fresh`` tells which)
    EXPIRE = "expire"
    DENY = "deny"


#: Defaults of the settings (registry keys ``REFERRAL_*``); prod values of the owner.
DEFAULTS: Final[Mapping[str, Any]] = {
    "REFERRAL_ENABLED": False,
    "REFERRAL_MODE": Mode.DAYS.value,
    "REFERRAL_INVITER_DAYS": 14,
    "REFERRAL_INVITEE_DAYS": 7,
    "REFERRAL_TRIGGER": Trigger.TRIAL_OR_PAID.value,
    "REFERRAL_INVITER_CAP_30D": 20,
    "REFERRAL_INVITER_CAP_TOTAL": 0,
    "REFERRAL_PERCENT": 10,
}


def _int(value: Any, default: int, lo: int, hi: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, number))


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Rules:
    """The seven settings of 05 §2.3.4 plus the percent of the wallet mode. ``0`` days switch a side off;
    ``0`` caps mean «no cap»."""

    enabled: bool = False
    mode: Mode = Mode.DAYS
    inviter_days: int = 14
    invitee_days: int = 7
    trigger: Trigger = Trigger.TRIAL_OR_PAID
    cap_30d: int = 20
    cap_total: int = 0
    percent: int = 10

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any]) -> Rules:
        """Read the ``REFERRAL_*`` keys; a broken value falls back to its default (never raises)."""

        def get(key: str) -> Any:
            value = cfg.get(key)
            return DEFAULTS[key] if value is None else value

        try:
            mode = Mode(str(get("REFERRAL_MODE")).strip().lower())
        except ValueError:
            mode = Mode.DAYS
        try:
            trigger = Trigger(str(get("REFERRAL_TRIGGER")).strip().lower())
        except ValueError:
            trigger = Trigger.TRIAL_OR_PAID
        return cls(
            enabled=_bool(get("REFERRAL_ENABLED")),
            mode=mode,
            inviter_days=_int(get("REFERRAL_INVITER_DAYS"), 14, 0, MAX_DAYS),
            invitee_days=_int(get("REFERRAL_INVITEE_DAYS"), 7, 0, MAX_DAYS),
            trigger=trigger,
            cap_30d=_int(get("REFERRAL_INVITER_CAP_30D"), 20, 0, 1_000_000),
            cap_total=_int(get("REFERRAL_INVITER_CAP_TOTAL"), 0, 0, 1_000_000),
            percent=_int(get("REFERRAL_PERCENT"), 10, 0, MAX_PERCENT),
        )

    @property
    def days_active(self) -> bool:
        return self.enabled and self.mode is Mode.DAYS and (self.inviter_days > 0 or self.invitee_days > 0)

    @property
    def percent_active(self) -> bool:
        return self.enabled and self.mode is Mode.PERCENT and self.percent > 0

    def days_for(self, side: str) -> int:
        return self.inviter_days if side == "inviter" else self.invitee_days


@dataclass(frozen=True, slots=True)
class SideState:
    """An existing ``referral_rewards`` row of the side (``kind='days'``)."""

    status: str
    retry_until: datetime | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class InviterStats:
    """The inviter's granted inviter-side rewards (days mode)."""

    granted_30d: int = 0
    granted_total: int = 0


@dataclass(frozen=True, slots=True)
class PairState:
    referred_user_id: int
    referrer_id: int
    qualifies: bool  # the invited user meets the trigger
    inviter_has_sub: bool
    invitee_has_sub: bool
    sides: Mapping[str, SideState] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SideAction:
    side: str
    action: Action
    days: int = 0
    reason: str | None = None
    retry_until: datetime | None = None
    fresh: bool = False  # DEFER: the side was not deferred before (notify the admin once)


@dataclass(frozen=True, slots=True)
class Decision:
    inviter: SideAction
    invitee: SideAction

    @property
    def actions(self) -> tuple[SideAction, SideAction]:
        return (self.invitee, self.inviter)

    @property
    def grants(self) -> tuple[SideAction, ...]:
        return tuple(a for a in self.actions if a.action is Action.GRANT)

    @property
    def newly_deferred(self) -> tuple[SideAction, ...]:
        return tuple(a for a in self.actions if a.action is Action.DEFER and a.fresh)

    @property
    def writes(self) -> bool:
        """Anything to store: a grant, a fresh deferral, an expiry or a denial (re-checks write nothing)."""
        return any(
            a.action in (Action.GRANT, Action.EXPIRE, Action.DENY) or (a.action is Action.DEFER and a.fresh)
            for a in self.actions
        )


def _none(side: str, reason: str | None = None) -> SideAction:
    return SideAction(side, Action.NONE, reason=reason)


def _side(side: str, rules: Rules, pair: PairState, stats: InviterStats, at: datetime) -> SideAction:
    state = pair.sides.get(side)
    if state is not None and state.status in SETTLED:
        return _none(side, state.status)
    deferred = state is not None and state.status == "deferred"
    if deferred and state is not None and state.retry_until is not None and at >= state.retry_until:
        return SideAction(side, Action.EXPIRE, reason=state.reason)
    days = rules.days_for(side)
    if days <= 0:
        return _none(side, "off")
    if pair.referred_user_id == pair.referrer_id:
        return SideAction(side, Action.DENY, reason="self")
    if not pair.qualifies:
        return _none(side, "trigger")
    reason: str | None = None
    if side == "inviter":
        if rules.cap_total > 0 and stats.granted_total >= rules.cap_total:
            reason = "cap_total"
        elif rules.cap_30d > 0 and stats.granted_30d >= rules.cap_30d:
            reason = "cap_30d"
    has_sub = pair.inviter_has_sub if side == "inviter" else pair.invitee_has_sub
    if reason is None and not has_sub:
        reason = "no_subscription"
    if reason is not None:
        retry = (
            state.retry_until if deferred and state is not None and state.retry_until else at + RETRY_WINDOW
        )
        return SideAction(side, Action.DEFER, reason=reason, retry_until=retry, fresh=not deferred)
    return SideAction(side, Action.GRANT, days=days)


def decide(rules: Rules, pair: PairState, stats: InviterStats, at: datetime) -> Decision:
    """The decision for one pair at ``at`` (days mode). Disabled / percent mode: only expiries happen."""
    if not rules.days_active:
        idle = Rules(enabled=False)
        return Decision(
            inviter=_side("inviter", idle, pair, stats, at)
            if _is_due(pair, "inviter", at)
            else _none("inviter"),
            invitee=_side("invitee", idle, pair, stats, at)
            if _is_due(pair, "invitee", at)
            else _none("invitee"),
        )
    return Decision(
        inviter=_side("inviter", rules, pair, stats, at),
        invitee=_side("invitee", rules, pair, stats, at),
    )


def _is_due(pair: PairState, side: str, at: datetime) -> bool:
    state = pair.sides.get(side)
    return (
        state is not None
        and state.status == "deferred"
        and state.retry_until is not None
        and at >= state.retry_until
    )


def percent_reward(rules: Rules, amount_minor: int) -> int:
    """Wallet reward of the inviter for a purchase of ``amount_minor`` (floored; ``0`` = nothing)."""
    if not rules.percent_active or amount_minor <= 0:
        return 0
    return amount_minor * rules.percent // 100
