"""IP Guard decisions (05 §2.2.1, port of the owner's decider as specification). Pure functions only.

The freeze itself (05 §2.2.4) is the core accumulator (:mod:`svbg.subscriptions.hold`); the owner's 14
freeze scenarios are checked on that real path in ``tests/ext/ip_guard/test_freeze.py``.

Everything that must survive a restart (blocks, grace after an unblock, cooldowns, quarantine, blocks per
hour,
the white list and «ложная тревога») comes in :class:`DbState` from the service's queries; only the counters
of :class:`DeciderMemory` live in memory between passes. Users are keyed by the **panel user id** (one bot
subscription = one panel user).

One pass::

    plan = decide(result.stats, memory, db, params, any_ok=result.any_ok, excluded_affected=...)
    outcomes = {pid: await service.block(...) for pid in plan.blocks}
    warnings = plan_warnings(plan, outcomes, db, params)
    memory = warnings.memory

Rules (owner thresholds 20/25 IP, ≥10 subnets, ≥10 live, 2 full passes):

* ``sustained``: W ≥ block ∧ S ≥ min_subnets; ``block_grade``: sustained ∧ L ≥ confirm_live;
* a candidate is ``block_grade`` in ``confirm_checks`` **complete** passes in a row, or ``sustained`` in
  ``sustained_block_checks`` passes in a row;
* eligible: not white-listed, not blocked, not in grace after an unblock, not «ложная тревога», preconditions
  met (auto block switched on, admin chat reachable);
* the fuse: more than ``max_blocks_per_run`` at block grade in one pass, or recent blocks + candidates above
  it
  within the window, or above ``max_blocks_per_hour`` → nobody is blocked, a quarantine (window, prolonged
  while the fuse keeps firing, never beyond ``quarantine_max_minutes`` from its start) collects the
  participants on **one** editable card.

Deviation from the owner's code (05 §2.2.8): the chain of «дополнений» of an anomaly is one record whose
``members`` grow; members already acted on keep their state.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final, Literal

from svbg.ext.ip_guard.config import DecisionParams
from svbg.ext.ip_guard.window import UserStats

__all__ = [
    "BLOCK_FAILED_HEALTH_STREAK",
    "BLOCK_FAILED_REPEAT",
    "BLOCK_LEVEL_KINDS",
    "AnomalyPlan",
    "AnomalyRecord",
    "BlockOutcome",
    "DbState",
    "DeciderMemory",
    "DismissInfo",
    "GraceInfo",
    "LastWarning",
    "Plan",
    "WarningDraft",
    "WarningKind",
    "WarningPlan",
    "block_grade",
    "cooldown_allows",
    "decide",
    "plan_warnings",
    "subnets_ok",
    "sustained_grade",
]

WarningKind = Literal[
    "warn",
    "pool",
    "unconfirmed",
    "incomplete",
    "grace",
    "whitelist",
    "precondition",
    "block_failed",
    "dismissed",
]
Trigger = Literal["per_run", "window", "per_hour"]

#: Kinds "of block level": one extra card after a plain ``warn`` is allowed within the cooldown.
BLOCK_LEVEL_KINDS: Final = frozenset(
    {"pool", "unconfirmed", "incomplete", "grace", "whitelist", "precondition", "dismissed"}
)
BLOCK_FAILED_REPEAT: Final = timedelta(minutes=10)
BLOCK_FAILED_HEALTH_STREAK: Final = 5
INCOMPLETE_PENDING_STREAK: Final = 2


# ------------------------------------------------------------------------------------------------- state


@dataclass(slots=True)
class DeciderMemory:
    consecutive: dict[int, int] = field(default_factory=dict)
    sustained: dict[int, int] = field(default_factory=dict)
    incomplete_streak: dict[int, int] = field(default_factory=dict)
    block_failed_streak: dict[int, int] = field(default_factory=dict)

    def copy(self) -> DeciderMemory:
        return DeciderMemory(
            dict(self.consecutive),
            dict(self.sustained),
            dict(self.incomplete_streak),
            dict(self.block_failed_streak),
        )

    def forget(self, pid: int) -> None:
        for counter in (self.consecutive, self.sustained, self.incomplete_streak, self.block_failed_streak):
            counter.pop(pid, None)

    def tracked(self) -> set[int]:
        return (
            set(self.consecutive)
            | set(self.sustained)
            | set(self.incomplete_streak)
            | set(self.block_failed_streak)
        )


@dataclass(frozen=True, slots=True)
class LastWarning:
    kind: str
    created_at: datetime
    ip_count: int = 0


@dataclass(frozen=True, slots=True)
class GraceInfo:
    unblocked_at: datetime
    until: datetime


@dataclass(frozen=True, slots=True)
class DismissInfo:
    until: datetime
    by: int | None = None


@dataclass(frozen=True, slots=True)
class AnomalyRecord:
    id: int
    created_at: datetime
    quarantine_until: datetime
    members: frozenset[int] = frozenset()


@dataclass(frozen=True, slots=True)
class DbState:
    """What the database says now (queried only for users with W ≥ warn)."""

    now: datetime
    blocked: frozenset[int] = frozenset()
    grace: Mapping[int, GraceInfo] = field(default_factory=dict)
    last_unblocked_at: Mapping[int, datetime] = field(default_factory=dict)
    last_warnings: Mapping[int, LastWarning] = field(default_factory=dict)
    last_block_failed_at: Mapping[int, datetime] = field(default_factory=dict)
    blocks_last_hour: int = 0
    confirmed_recent: int = 0
    quarantine: tuple[AnomalyRecord, ...] = ()
    dismissed: Mapping[int, DismissInfo] = field(default_factory=dict)
    preconditions_ok: bool = True
    precondition_reason: str | None = None


@dataclass(frozen=True, slots=True)
class WarningDraft:
    panel_user_id: int
    kind: WarningKind
    stats: UserStats
    fail_reason: str | None = None
    grace: GraceInfo | None = None
    dismissed: DismissInfo | None = None
    precondition_reason: str | None = None
    missing_nodes: tuple[str, ...] = ()
    sustained_left: int | None = None


@dataclass(frozen=True, slots=True)
class AnomalyPlan:
    trigger: Trigger | None
    create: bool = False
    quarantine_until: datetime | None = None  # new end of the quarantine (create or prolong); None = keep
    anomaly_id: int | None = None  # the active anomaly the members are appended to
    new_members: tuple[int, ...] = ()
    trigger_count: int = 0
    confirmed_recent: int = 0
    blocks_last_hour: int = 0


@dataclass(frozen=True, slots=True)
class Plan:
    now: datetime
    memory: DeciderMemory
    stats: Mapping[int, UserStats] = field(default_factory=dict)
    blocks: tuple[int, ...] = ()
    candidates: frozenset[int] = frozenset()
    hot: frozenset[int] = frozenset()
    grade_now: frozenset[int] = frozenset()
    anomaly: AnomalyPlan | None = None
    quarantine_active: bool = False
    drafts: Mapping[int, WarningDraft] = field(default_factory=dict)
    decided: bool = False


@dataclass(frozen=True, slots=True)
class BlockOutcome:
    status: Literal["blocked", "already_blocked", "failed"]
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class WarningPlan:
    individual: tuple[WarningDraft, ...]
    summary: tuple[WarningDraft, ...]
    memory: DeciderMemory
    failing_blocks: tuple[tuple[int, str, int], ...] = ()


# ------------------------------------------------------------------------------------------------ grades


def subnets_ok(st: UserStats, params: DecisionParams) -> bool:
    return params.min_subnets == 0 or st.subnet_count >= params.min_subnets


def sustained_grade(st: UserStats, params: DecisionParams) -> bool:
    return st.ip_count >= params.block_ips and subnets_ok(st, params)


def block_grade(st: UserStats, params: DecisionParams) -> bool:
    live_ok = params.confirm_live_ips == 0 or st.live_ip_count >= params.confirm_live_ips
    return sustained_grade(st, params) and live_ok


# ------------------------------------------------------------------------------------------------ decide


def decide(
    stats: Mapping[int, UserStats],
    memory: DeciderMemory,
    db: DbState,
    params: DecisionParams,
    *,
    any_ok: bool = True,
    excluded_affected: Iterable[int] = (),
) -> Plan:
    now = db.now
    mem = memory.copy()
    if not any_ok:
        return Plan(now=now, memory=mem)  # no node answered: no decisions, counters untouched

    watched = {pid: st for pid, st in stats.items() if st.ip_count >= params.warn_ips}
    ordered = sorted(watched, key=lambda pid: (-watched[pid].ip_count, pid))
    for pid in mem.tracked() - watched.keys():
        mem.forget(pid)
    for pid in excluded_affected:  # a node marked CDN on the fly and W fell below block
        st = watched.get(pid)
        if st is None or st.ip_count < params.block_ips:
            mem.consecutive.pop(pid, None)
            mem.sustained.pop(pid, None)

    for pid, st in watched.items():
        if not st.complete:
            mem.incomplete_streak[pid] = mem.incomplete_streak.get(pid, 0) + 1
            continue
        mem.incomplete_streak[pid] = 0
        mem.consecutive[pid] = mem.consecutive.get(pid, 0) + 1 if block_grade(st, params) else 0
        mem.sustained[pid] = mem.sustained.get(pid, 0) + 1 if sustained_grade(st, params) else 0

    dismissed = {pid: info for pid, info in db.dismissed.items() if info.until > now}

    def eligible(pid: int) -> bool:
        return (
            pid not in params.whitelist
            and pid not in db.blocked
            and pid not in db.grace
            and pid not in dismissed
            and db.preconditions_ok
        )

    hot = {pid for pid in ordered if eligible(pid) and sustained_grade(watched[pid], params)}
    candidates: set[int] = set()
    grade_now: set[int] = set()
    for pid in hot:
        st = watched[pid]
        if not st.complete:
            continue
        graded = block_grade(st, params)
        if graded:
            grade_now.add(pid)
        confirmed = graded and mem.consecutive.get(pid, 0) >= params.confirm_checks
        escalated = (
            params.sustained_block_checks > 0 and mem.sustained.get(pid, 0) >= params.sustained_block_checks
        )
        if confirmed or escalated:
            candidates.add(pid)

    trigger: Trigger | None = None
    trigger_count = 0
    if len(grade_now) > params.max_blocks_per_run:
        trigger, trigger_count = "per_run", len(grade_now)
    elif candidates and db.confirmed_recent + len(candidates) > params.max_blocks_per_run:
        trigger, trigger_count = "window", len(candidates)
    elif candidates and db.blocks_last_hour + len(candidates) > params.max_blocks_per_hour:
        trigger, trigger_count = "per_hour", len(candidates)

    active = sorted((r for r in db.quarantine if r.quarantine_until > now), key=lambda r: r.id)
    members_before: frozenset[int] = (
        frozenset().union(*(r.members for r in active)) if active else frozenset()
    )

    blocks: tuple[int, ...] = ()
    anomaly: AnomalyPlan | None = None
    counters = {"confirmed_recent": db.confirmed_recent, "blocks_last_hour": db.blocks_last_hour}
    if trigger is not None:
        for pid in hot:
            mem.consecutive[pid] = 0
            mem.sustained[pid] = 0
        new = tuple(pid for pid in ordered if pid in hot and pid not in members_before)
        if not active:
            anomaly = AnomalyPlan(
                trigger=trigger,
                create=True,
                quarantine_until=now + timedelta(minutes=params.window_minutes),
                new_members=new,
                trigger_count=trigger_count,
                **counters,
            )
        else:
            root = active[0]
            ceiling = root.created_at + timedelta(minutes=params.quarantine_max_minutes)
            until = min(now + timedelta(minutes=params.window_minutes), ceiling)
            until = max(until, *(r.quarantine_until for r in active))
            anomaly = AnomalyPlan(
                trigger=trigger,
                quarantine_until=until,
                anomaly_id=root.id,
                new_members=new,
                trigger_count=trigger_count,
                **counters,
            )
    elif active:
        for pid in candidates:  # quarantine: nobody is blocked, candidates join the anomaly card
            mem.consecutive[pid] = 0
            mem.sustained[pid] = 0
        new = tuple(pid for pid in ordered if pid in candidates and pid not in members_before)
        anomaly = AnomalyPlan(trigger=None, anomaly_id=active[0].id, new_members=new, **counters)
    else:
        blocks = tuple(pid for pid in ordered if pid in candidates)

    in_anomaly = members_before | frozenset(anomaly.new_members if anomaly else ())
    drafts: dict[int, WarningDraft] = {}
    for pid in ordered:
        if pid in db.blocked or pid in blocks or pid in in_anomaly:
            continue
        draft = _classify(pid, watched[pid], mem, db, params, dismissed)
        if draft is not None:
            drafts[pid] = draft

    return Plan(
        now=now,
        memory=mem,
        stats=watched,
        blocks=blocks,
        candidates=frozenset(candidates),
        hot=frozenset(hot),
        grade_now=frozenset(grade_now),
        anomaly=anomaly,
        quarantine_active=bool(active) or (anomaly is not None and anomaly.create),
        drafts=drafts,
        decided=True,
    )


def _classify(  # noqa: PLR0917 - port of the specification
    pid: int,
    st: UserStats,
    mem: DeciderMemory,
    db: DbState,
    params: DecisionParams,
    dismissed: Mapping[int, DismissInfo],
) -> WarningDraft | None:
    """Card for a user outside the candidates and the anomaly; ``None`` = pending (nothing is sent)."""
    if st.ip_count < params.block_ips:
        return WarningDraft(pid, "warn", st)
    if pid in dismissed:
        return WarningDraft(pid, "dismissed", st, dismissed=dismissed[pid])
    if pid in params.whitelist:
        return WarningDraft(pid, "whitelist", st)
    if pid in db.grace:
        return WarningDraft(pid, "grace", st, grace=db.grace[pid])
    if not db.preconditions_ok:
        return WarningDraft(pid, "precondition", st, precondition_reason=db.precondition_reason)
    if not subnets_ok(st, params):
        return WarningDraft(pid, "pool", st)
    if not st.complete:
        if mem.incomplete_streak.get(pid, 0) <= INCOMPLETE_PENDING_STREAK:
            return None
        return WarningDraft(pid, "incomplete", st, missing_nodes=st.missing_nodes)
    if block_grade(st, params):
        return None  # confirmation in progress: most likely a block on the next pass
    left = None
    if params.sustained_block_checks > 0:
        left = max(1, params.sustained_block_checks - mem.sustained.get(pid, 0))
    return WarningDraft(pid, "unconfirmed", st, sustained_left=left)


def cooldown_allows(kind: str, pid: int, db: DbState, params: DecisionParams) -> bool:
    """Cooldown from the database, reset by an unblock, one escalation ``warn`` → block level."""
    now = db.now
    if kind == "block_failed":
        last_failed = db.last_block_failed_at.get(pid)
        return last_failed is None or now - last_failed >= BLOCK_FAILED_REPEAT
    last = db.last_warnings.get(pid)
    if last is None:
        return True
    horizon = now - timedelta(minutes=params.warn_cooldown_minutes)
    unblocked_at = db.last_unblocked_at.get(pid)
    if unblocked_at is not None:
        horizon = max(horizon, unblocked_at)
    if last.created_at <= horizon:
        return True
    return last.kind == "warn" and kind in BLOCK_LEVEL_KINDS


def plan_warnings(
    plan: Plan,
    outcomes: Mapping[int, BlockOutcome],
    db: DbState,
    params: DecisionParams,
) -> WarningPlan:
    """After the blocks ran: ``block_failed`` cards, cooldowns, the per-pass card limit and the summary."""
    mem = plan.memory.copy()
    drafts = list(plan.drafts.values())
    failing: list[tuple[int, str, int]] = []
    for pid in plan.blocks:
        outcome = outcomes.get(pid) or BlockOutcome("failed", "блок не выполнен")
        if outcome.status != "failed":
            mem.block_failed_streak.pop(pid, None)
            continue
        reason = outcome.reason or "неизвестная ошибка"
        streak = mem.block_failed_streak.get(pid, 0) + 1
        mem.block_failed_streak[pid] = streak
        if streak >= BLOCK_FAILED_HEALTH_STREAK:
            failing.append((pid, reason, streak))
        drafts.append(WarningDraft(pid, "block_failed", plan.stats[pid], fail_reason=reason))
    for pid in [p for p in mem.block_failed_streak if p not in plan.blocks]:
        del mem.block_failed_streak[pid]

    sendable = [d for d in drafts if cooldown_allows(d.kind, d.panel_user_id, db, params)]
    sendable.sort(key=lambda d: (d.kind != "block_failed", -d.stats.ip_count, d.panel_user_id))
    limit = max(1, params.max_warnings_per_run)
    return WarningPlan(
        individual=tuple(sendable[:limit]),
        summary=tuple(sendable[limit:]),
        memory=mem,
        failing_blocks=tuple(failing),
    )
