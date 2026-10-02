"""LTE quotas: the cycle plan with safety fuses (05 §2.1.10 p.5) and the admin preview (05 §2.1.6).

``decide()`` runs :func:`svbg.ext.lte.decide.decide_subject` for every subscription, then:

- **quarantine** of a group: more than ``quarantine_new_blocks`` quota candidates in one cycle, or more than
  ``max_blocked_share_pct`` % of the eligible subscriptions blocked (only on a sample of at least
  ``share_min_eligible`` — a tiny pilot must not quarantine itself). The group's candidates are dropped and an
  attention item waits for the admin; releases continue;
- **throttling**: at most ``max_new_blocks_per_cycle`` new quota blocks per cycle, the biggest ``used/limit``
  first; ``unavailable`` (limit 0) candidates are outside both fuses (a setting, not a wave of overuse);
- the "исчерпан" notification stays only for blocks that are really placed this cycle.

:func:`preview` is the same computation for the admin: "сейчас заблокировано N → после: +A, −R" with the
distribution of ``used/limit`` by buckets 0/10/…/150 % and the first rows.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from svbg.ext.lte.decide import (
    HOLD_QUARANTINE,
    NOTIFY_EXHAUSTED,
    REASON_QUOTA,
    REASON_UNAVAILABLE,
    BlockCandidate,
    EffectiveLimit,
    EnforceSettings,
    GroupInput,
    NotifyRequest,
    Rebind,
    Release,
    Restore,
    SubjectInput,
    decide_subject,
    effective_limit,
)
from svbg.ext.lte.model import int_param

__all__ = [
    "PREVIEW_BUCKETS",
    "Plan",
    "PlannerParams",
    "Preview",
    "decide",
    "preview",
]

#: Buckets of the preview histogram: ``[0,10) … [140,150) and ≥150 %`` of the effective limit.
PREVIEW_BUCKETS: Final = (*range(0, 150, 10), 150)
PREVIEW_SAMPLE: Final = 10


@dataclass(frozen=True, slots=True)
class PlannerParams:
    """Fuses (05 §2.1.7 "Расширенные")."""

    max_new_blocks_per_cycle: int = 30  # LTE_MAX_NEW_BLOCKS_PER_CYCLE (1–1000)
    quarantine_new_blocks: int = 100  # LTE_QUARANTINE_NEW_BLOCKS (≥ max_new_blocks_per_cycle)
    max_blocked_share_pct: int = 25  # LTE_MAX_BLOCKED_SHARE_PCT (1–100)
    share_min_eligible: int = 20  # code constant: below this sample the share is not checked

    @classmethod
    def from_values(cls, values: Mapping[str, Any]) -> PlannerParams:
        max_new = int_param(values, "max_new_blocks_per_cycle", 30, 1, 1000)
        return cls(
            max_new_blocks_per_cycle=max_new,
            # the quarantine threshold can never be below throttling
            quarantine_new_blocks=max(max_new, int_param(values, "quarantine_new_blocks", 100, 1, 100_000)),
            max_blocked_share_pct=int_param(values, "max_blocked_share_pct", 25, 1, 100),
        )


@dataclass(frozen=True, slots=True)
class Plan:
    """The cycle's decisions; nothing is applied here."""

    blocks: tuple[BlockCandidate, ...] = ()
    restores: tuple[Restore, ...] = ()
    rebinds: tuple[Rebind, ...] = ()
    releases: tuple[Release, ...] = ()
    throttled: tuple[BlockCandidate, ...] = ()
    quarantine: frozenset[int] = frozenset()
    notifications: tuple[NotifyRequest, ...] = ()
    group_reasons: Mapping[int, tuple[str, ...]] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    @property
    def panel_writes(self) -> int:
        """Changes of the desired squads (re-binds need none; shadow decisions never reach the panel)."""
        return (
            sum(1 for b in self.blocks if b.mode == "enforce")
            + sum(1 for r in self.restores if r.mode == "enforce")
            + sum(1 for r in self.releases if not r.immediate)
        )

    @property
    def empty(self) -> bool:
        return not (self.blocks or self.restores or self.rebinds or self.releases or self.notifications)


def decide(
    *,
    groups: Iterable[GroupInput],
    subjects: Iterable[SubjectInput],
    settings: EnforceSettings,
    now: datetime,
    params: PlannerParams | None = None,
) -> Plan:
    """Decisions of a cycle (or of one subscription in the ``lte.term`` hook) with the safety fuses."""
    params = params or PlannerParams()
    group_map = {group.id: group for group in groups}
    candidates: list[BlockCandidate] = []
    restores: list[Restore] = []
    rebinds: list[Rebind] = []
    releases: list[Release] = []
    notifications: list[NotifyRequest] = []
    warnings: list[str] = []
    eligible: dict[int, int] = dict.fromkeys(group_map, 0)
    blocked: dict[int, int] = dict.fromkeys(group_map, 0)
    for subject in subjects:
        decision = decide_subject(subject, groups=group_map, settings=settings, now=now)
        candidates.extend(decision.candidates)
        restores.extend(decision.restores)
        rebinds.extend(decision.rebinds)
        releases.extend(decision.releases)
        notifications.extend(decision.notifications)
        warnings.extend(decision.warnings)
        for group_id in decision.eligible_groups:
            eligible[group_id] = eligible.get(group_id, 0) + 1
        for group_id in decision.blocked_groups:
            blocked[group_id] = blocked.get(group_id, 0) + 1

    quarantine, kept = _quarantine(
        candidates, groups=group_map, params=params, eligible=eligible, blocked=blocked
    )
    applied, throttled = _throttle(kept, params=params)
    placed = {(item.subscription_id, item.group_id) for item in applied}
    notifications = [
        item
        for item in notifications
        if item.kind != NOTIFY_EXHAUSTED or (item.subscription_id, item.group_id) in placed
    ]
    reasons = {
        group_id: group.blocks_new_blocks + ((HOLD_QUARANTINE,) if group_id in quarantine else ())
        for group_id, group in group_map.items()
        if group.blocks_new_blocks or group_id in quarantine
    }
    return Plan(
        blocks=tuple(applied),
        restores=tuple(restores),
        rebinds=tuple(rebinds),
        releases=tuple(releases),
        throttled=tuple(throttled),
        quarantine=frozenset(quarantine),
        notifications=tuple(notifications),
        group_reasons=reasons,
        warnings=tuple(dict.fromkeys(warnings)),
    )


def _quarantine(
    candidates: Sequence[BlockCandidate],
    *,
    groups: Mapping[int, GroupInput],
    params: PlannerParams,
    eligible: Mapping[int, int],
    blocked: Mapping[int, int],
) -> tuple[frozenset[int], list[BlockCandidate]]:
    counts: dict[int, int] = {}
    for item in candidates:
        if item.reason == REASON_QUOTA:
            counts[item.group_id] = counts.get(item.group_id, 0) + 1
    quarantined: set[int] = set()
    for group_id, count in counts.items():
        group = groups.get(group_id)
        if group is None or group.quarantine_cleared:
            continue
        if count > params.quarantine_new_blocks:
            quarantined.add(group_id)
            continue
        total = int(eligible.get(group_id, 0))
        if total < max(1, params.share_min_eligible):
            continue
        if (int(blocked.get(group_id, 0)) + count) * 100 // total > params.max_blocked_share_pct:
            quarantined.add(group_id)
    kept = [
        item for item in candidates if item.group_id not in quarantined or item.reason == REASON_UNAVAILABLE
    ]
    return frozenset(quarantined), kept


def _throttle(
    candidates: Sequence[BlockCandidate], *, params: PlannerParams
) -> tuple[list[BlockCandidate], list[BlockCandidate]]:
    unavailable = [item for item in candidates if item.reason == REASON_UNAVAILABLE]
    quota = sorted(
        (item for item in candidates if item.reason != REASON_UNAVAILABLE),
        key=lambda item: (-item.ratio, item.panel_user_id, item.group_id),
    )
    limit = max(1, params.max_new_blocks_per_cycle)
    return unavailable + quota[:limit], quota[limit:]


# ---------------------------------------------------------------- admin preview


@dataclass(frozen=True, slots=True)
class PreviewRow:
    subscription_id: int
    panel_user_id: int
    group_id: int
    used_bytes: int
    limit_bytes: int | None
    action: str  # block | release | keep


@dataclass(frozen=True, slots=True)
class Preview:
    """Effect of a change of limits / enforcement before it is confirmed."""

    blocked_now: int
    new_blocks: int
    releases: int
    blocked_after: int
    throttled: int
    quarantine: frozenset[int]
    histogram: Mapping[int, int]  # bucket start (0, 10, …, 150) → subscriptions
    sample: tuple[PreviewRow, ...]


def _bucket(limit: EffectiveLimit, used: int) -> int | None:
    value = limit.limit
    if value is None:
        return None
    percent = 10**9 if value <= 0 else used * 100 // value
    return min(150, percent // 10 * 10)


def preview(
    *,
    groups: Iterable[GroupInput],
    subjects: Iterable[SubjectInput],
    settings: EnforceSettings,
    now: datetime,
    params: PlannerParams | None = None,
) -> Preview:
    """The plan for a hypothetical configuration (same :func:`decide`), summarized for the admin card."""
    group_list = list(groups)
    subject_list = list(subjects)
    group_map = {group.id: group for group in group_list}
    plan = decide(groups=group_list, subjects=subject_list, settings=settings, now=now, params=params)
    blocked_now = sum(1 for s in subject_list for b in s.live_blocks if b.is_applied and b.mode == "enforce")
    released = sum(1 for r in plan.releases if not r.immediate)
    placed = sum(1 for b in plan.blocks if b.mode == "enforce")
    histogram: dict[int, int] = dict.fromkeys(PREVIEW_BUCKETS, 0)
    rows: list[PreviewRow] = []
    blocks = {(b.subscription_id, b.group_id) for b in plan.blocks}
    gone = {(r.subscription_id, r.group_id) for r in plan.releases}
    for subject in subject_list:
        if not subject.has_live_period:
            continue
        for group_id in sorted(subject.rights):
            group = group_map.get(group_id)
            if group is None:
                continue
            used = int(subject.used.get(group_id, 0))
            limit = effective_limit(
                group,
                is_trial=subject.period_is_trial,
                user_rows=subject.limit_rows(group_id, now),
                credit_bytes=subject.credits.get(group_id, 0),
            )
            bucket = _bucket(limit, used)
            if bucket is not None:
                histogram[bucket] += 1
            key = (subject.subscription_id, group_id)
            action = "block" if key in blocks else "release" if key in gone else "keep"
            if action != "keep":
                rows.append(
                    PreviewRow(
                        subject.subscription_id, subject.panel_user_id, group_id, used, limit.limit, action
                    )
                )
    rows.sort(key=lambda r: (r.action != "block", -(r.used_bytes / r.limit_bytes) if r.limit_bytes else 0.0))
    return Preview(
        blocked_now=blocked_now,
        new_blocks=placed,
        releases=released,
        blocked_after=blocked_now + placed - released,
        throttled=len(plan.throttled),
        quarantine=plan.quarantine,
        histogram=histogram,
        sample=tuple(rows[:PREVIEW_SAMPLE]),
    )
