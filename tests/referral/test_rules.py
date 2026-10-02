"""Table tests of the pure referral rules (05 §2.3, [MC] §1–3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from svbg.referral.rules import (
    RETRY_WINDOW,
    Action,
    InviterStats,
    Mode,
    PairState,
    Rules,
    SideState,
    Trigger,
    decide,
    percent_reward,
)

AT = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
RULES = Rules(enabled=True)  # 14 / 7, trial_or_paid, cap 20 per 30 days


def pair(**kw: Any) -> PairState:
    base: dict[str, Any] = {
        "referred_user_id": 2,
        "referrer_id": 1,
        "qualifies": True,
        "inviter_has_sub": True,
        "invitee_has_sub": True,
        "sides": {},
    }
    base.update(kw)
    return PairState(**base)


def deferred(hours_left: float, reason: str = "no_subscription") -> SideState:
    return SideState("deferred", AT + timedelta(hours=hours_left), reason)


CASES: list[
    tuple[str, Rules, PairState, InviterStats, tuple[Action, int, str | None], tuple[Action, int, str | None]]
] = [
    # name, rules, pair, stats, (inviter action, days, reason), (invitee action, days, reason)
    ("both granted", RULES, pair(), InviterStats(), (Action.GRANT, 14, None), (Action.GRANT, 7, None)),
    (
        "trigger not met",
        RULES,
        pair(qualifies=False),
        InviterStats(),
        (Action.NONE, 0, "trigger"),
        (Action.NONE, 0, "trigger"),
    ),
    (
        "inviter without subscription is deferred, invitee granted",
        RULES,
        pair(inviter_has_sub=False),
        InviterStats(),
        (Action.DEFER, 0, "no_subscription"),
        (Action.GRANT, 7, None),
    ),
    (
        "invitee without subscription (register) is deferred",
        RULES,
        pair(invitee_has_sub=False),
        InviterStats(),
        (Action.GRANT, 14, None),
        (Action.DEFER, 0, "no_subscription"),
    ),
    (
        "cap 30d reached: inviter waits, invitee still granted",
        RULES,
        pair(),
        InviterStats(granted_30d=20, granted_total=20),
        (Action.DEFER, 0, "cap_30d"),
        (Action.GRANT, 7, None),
    ),
    (
        "cap 30d below the limit",
        RULES,
        pair(),
        InviterStats(granted_30d=19, granted_total=100),
        (Action.GRANT, 14, None),
        (Action.GRANT, 7, None),
    ),
    (
        "cap total",
        Rules(enabled=True, cap_total=5),
        pair(),
        InviterStats(granted_30d=1, granted_total=5),
        (Action.DEFER, 0, "cap_total"),
        (Action.GRANT, 7, None),
    ),
    (
        "cap 0 means no cap",
        Rules(enabled=True, cap_30d=0),
        pair(),
        InviterStats(granted_30d=10_000, granted_total=10_000),
        (Action.GRANT, 14, None),
        (Action.GRANT, 7, None),
    ),
    (
        "cap is checked before the subscription",
        RULES,
        pair(inviter_has_sub=False),
        InviterStats(granted_30d=20),
        (Action.DEFER, 0, "cap_30d"),
        (Action.GRANT, 7, None),
    ),
    (
        "settled sides never change",
        RULES,
        pair(sides={"inviter": SideState("granted"), "invitee": SideState("legacy")}),
        InviterStats(),
        (Action.NONE, 0, "granted"),
        (Action.NONE, 0, "legacy"),
    ),
    (
        "expired and denied are settled too",
        RULES,
        pair(sides={"inviter": SideState("expired"), "invitee": SideState("denied")}),
        InviterStats(),
        (Action.NONE, 0, "expired"),
        (Action.NONE, 0, "denied"),
    ),
    (
        "a deferred side is granted once the subscription appears",
        RULES,
        pair(sides={"inviter": deferred(10), "invitee": SideState("granted")}),
        InviterStats(),
        (Action.GRANT, 14, None),
        (Action.NONE, 0, "granted"),
    ),
    (
        "a deferred side past the window expires",
        RULES,
        pair(sides={"inviter": deferred(-1), "invitee": SideState("granted")}),
        InviterStats(),
        (Action.EXPIRE, 0, "no_subscription"),
        (Action.NONE, 0, "granted"),
    ),
    (
        "zero days switch a side off",
        Rules(enabled=True, inviter_days=0),
        pair(),
        InviterStats(),
        (Action.NONE, 0, "off"),
        (Action.GRANT, 7, None),
    ),
    (
        "self-referral is denied",
        RULES,
        pair(referrer_id=2),
        InviterStats(),
        (Action.DENY, 0, "self"),
        (Action.DENY, 0, "self"),
    ),
]


@pytest.mark.parametrize(
    ("name", "rules", "state", "stats", "inviter", "invitee"), CASES, ids=[c[0] for c in CASES]
)
def test_decide_table(
    name: str,
    rules: Rules,
    state: PairState,
    stats: InviterStats,
    inviter: tuple[Action, int, str | None],
    invitee: tuple[Action, int, str | None],
) -> None:
    d = decide(rules, state, stats, AT)
    assert (d.inviter.action, d.inviter.days, d.inviter.reason) == inviter, name
    assert (d.invitee.action, d.invitee.days, d.invitee.reason) == invitee, name


def test_fresh_deferral_gets_the_window_and_is_reported_once() -> None:
    first = decide(RULES, pair(inviter_has_sub=False), InviterStats(), AT)
    assert first.inviter.fresh and first.inviter.retry_until == AT + RETRY_WINDOW
    assert first.newly_deferred == (first.inviter,)
    assert first.writes
    later = AT + timedelta(hours=5)
    state = pair(
        inviter_has_sub=False,
        sides={
            "inviter": SideState("deferred", AT + RETRY_WINDOW, "no_subscription"),
            "invitee": SideState("granted"),
        },
    )
    again = decide(RULES, state, InviterStats(), later)
    assert again.inviter.action is Action.DEFER and not again.inviter.fresh
    assert again.inviter.retry_until == AT + RETRY_WINDOW  # the window is not extended by re-checks
    assert again.newly_deferred == ()
    assert not again.writes  # an idle pass writes and says nothing (lesson 758ee693b)


def test_retry_window_boundary_is_inclusive() -> None:
    state = pair(inviter_has_sub=False, sides={"inviter": deferred(0)})
    assert decide(RULES, state, InviterStats(), AT).inviter.action is Action.EXPIRE
    state = pair(inviter_has_sub=False, sides={"inviter": deferred(0.01)})
    assert decide(RULES, state, InviterStats(), AT).inviter.action is Action.DEFER


@pytest.mark.parametrize(
    "rules",
    [
        Rules(enabled=False),
        Rules(enabled=True, mode=Mode.PERCENT),
        Rules(enabled=True, inviter_days=0, invitee_days=0),
    ],
)
def test_inactive_mode_only_expires(rules: Rules) -> None:
    state = pair(sides={"inviter": deferred(-1), "invitee": deferred(5)})
    d = decide(rules, state, InviterStats(), AT)
    assert d.inviter.action is Action.EXPIRE
    assert d.invitee.action is Action.NONE
    assert decide(rules, pair(), InviterStats(), AT).grants == ()


def test_rules_from_config_defaults_and_fallbacks() -> None:
    assert Rules.from_config({}) == Rules()
    broken = Rules.from_config(
        {
            "REFERRAL_ENABLED": "yes",
            "REFERRAL_MODE": "money",
            "REFERRAL_INVITER_DAYS": "abc",
            "REFERRAL_INVITEE_DAYS": 9999,
            "REFERRAL_TRIGGER": "ANY",
            "REFERRAL_INVITER_CAP_30D": -5,
            "REFERRAL_PERCENT": True,
        }
    )
    assert broken.enabled is True
    assert broken.mode is Mode.DAYS
    assert broken.inviter_days == 14
    assert broken.invitee_days == 365
    assert broken.trigger is Trigger.TRIAL_OR_PAID
    assert broken.cap_30d == 0
    assert broken.percent == 10
    assert Rules.from_config({"REFERRAL_TRIGGER": " Register "}).trigger is Trigger.REGISTER


def test_percent_reward() -> None:
    on = Rules(enabled=True, mode=Mode.PERCENT, percent=10)
    assert percent_reward(on, 17_999) == 1_799
    assert percent_reward(on, 9) == 0
    assert percent_reward(on, 0) == 0
    assert percent_reward(Rules(enabled=True, mode=Mode.DAYS), 10_000) == 0
    assert percent_reward(Rules(enabled=False, mode=Mode.PERCENT), 10_000) == 0
