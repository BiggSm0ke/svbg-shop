"""LTE quotas: limits, per-subscription decisions and the squad contribution (05 §2.1.10).

Pure. :func:`decide_subject` is the per-subscription part of the cycle's ``decide()``; the cycle-wide fuses
(quarantine, throttling) live in :mod:`svbg.ext.lte.planner`. The same functions compute the cycle, the
``lte.term`` hook (one subscription right after a payment) and the admin preview, so they never disagree.

Order per subscription (05 §2.1.10):

1. exempt (for every group) or an unconfirmed panel identity → release every live block;
2. blocks of groups that are gone, or the subscription has no rights to → release;
3. no live period → nothing else;
4. per group the base squad has rights to: ``needed = used ≥ limit_eff`` (unless ``no_block``) or limit 0;
   the estimated share after a gap alone never blocks; a ``releasing`` block is restored instead of a new
   one; a block of a past period is re-bound without a panel write; a new block needs no anomaly, quarantine
   or implausible delta; below the limit → release (``topup`` / ``limit_change``); a manual block is never
   released automatically; notifications (warn, exhausted, reset) only where the quota is applied.

Enforcement (``lte.enforce``): ``on`` — applied (for the pilot list only, if set, and only for groups with
``enforce``); ``shadow`` — decisions are journaled with ``mode='shadow'``, live enforce blocks are kept as
they are (they came from an import or from ``on``) and nothing is sent to users; ``off`` — no decisions,
live blocks released (or held when ``release_when_off`` is false — the "снять / оставить" choice).

Squads (X1): the module never writes the panel. It contributes ``panel_squad_substitutions`` rows "base →
twin" for blocked groups (:func:`substitutions_for`); the core writer applies them and reverses them before
comparing (:func:`project_squads` / :func:`reverse_squads` are the same mapping, for previews and the CLI).
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final

from svbg.ext.lte.model import LIVE_BLOCK_STATUSES, BlockMode, EnforceMode

__all__ = [
    "MODE_ENFORCE",
    "MODE_SHADOW",
    "NOTIFY_EXHAUSTED",
    "NOTIFY_RESET",
    "NOTIFY_WARN",
    "BlockCandidate",
    "EffectiveLimit",
    "EnforceSettings",
    "GroupInput",
    "LiveBlock",
    "NotifyRequest",
    "Override",
    "Rebind",
    "Release",
    "Restore",
    "SubjectDecision",
    "SubjectInput",
    "Twin",
    "TwinProblem",
    "after_block_resend_due",
    "base_limit",
    "check_foreign_nodes",
    "check_twins",
    "decide_subject",
    "effective_limit",
    "margin_of",
    "project_squads",
    "reverse_squads",
    "rights_of",
    "substitutions_for",
]

MODE_ENFORCE: Final = "enforce"
MODE_SHADOW: Final = "shadow"

REASON_QUOTA: Final = "quota"
REASON_UNAVAILABLE: Final = "unavailable"
REASON_MANUAL: Final = "manual"

RELEASE_EXEMPT: Final = "exempt"
RELEASE_IDENTITY: Final = "identity"
RELEASE_GROUP_DELETED: Final = "group_deleted"
RELEASE_NO_RIGHTS: Final = "no_rights"
RELEASE_FEATURE_OFF: Final = "feature_off"
RELEASE_MODE_CHANGE: Final = "mode_change"
RELEASE_TOPUP: Final = "topup"
RELEASE_LIMIT_CHANGE: Final = "limit_change"

HOLD_NOT_ACTIVE: Final = "group_not_active"
HOLD_SUSPENDED: Final = "suspended"
HOLD_ANOMALY: Final = "anomaly"
HOLD_QUARANTINE: Final = "quarantine"
HOLD_CLAMPED_SANITY: Final = "clamped_sanity"
HOLD_GAP_ESTIMATED: Final = "gap_estimated"

NOTIFY_WARN: Final = "warn"
NOTIFY_EXHAUSTED: Final = "exhausted"
NOTIFY_RESET: Final = "reset"

#: "Traffic after a block" (05 §2.1.10): new deltas 20+ min after the block, alert + resend at most hourly.
AFTER_BLOCK_GRACE: Final = timedelta(minutes=20)
AFTER_BLOCK_EVERY: Final = timedelta(hours=1)


# ---------------------------------------------------------------- inputs


@dataclass(frozen=True, slots=True)
class EnforceSettings:
    """``lte.enforce`` / ``lte.enforce_list`` / ``lte.warn_percent`` and the "снять / оставить" choice."""

    mode: EnforceMode = "shadow"
    pilot: frozenset[int] = frozenset()  # panel_user_id; empty — everybody
    release_when_off: bool = True
    warn_percent: int = 80


@dataclass(frozen=True, slots=True)
class GroupInput:
    """An ``lte_groups`` row plus the cycle flags of the group (``accounting.build_group_states``)."""

    id: int
    state: str = "active"
    enforce: bool = True
    margin_bytes: int = 0
    margin_pct: int = 0
    limit_rows: Mapping[str, int | None] = field(default_factory=dict)
    incomplete: tuple[str, ...] = ()
    anomaly: tuple[str, ...] = ()
    suspended: bool = False
    quarantined: bool = False  # a quarantine is open (an attention item waits for the admin)
    quarantine_cleared: bool = False  # the admin confirmed the wave: thresholds are not checked this cycle

    @property
    def is_active(self) -> bool:
        return self.state == "active"

    @property
    def blocks_new_blocks(self) -> tuple[str, ...]:
        """Why there are no new blocks for the group this cycle (releases still go on)."""
        reasons: list[str] = []
        if not self.is_active:
            reasons.append(HOLD_NOT_ACTIVE)
        if self.suspended:
            reasons.append(HOLD_SUSPENDED)
        if self.anomaly:
            reasons.append(HOLD_ANOMALY)
        if self.quarantined:
            reasons.append(HOLD_QUARANTINE)
        return tuple(reasons)


@dataclass(frozen=True, slots=True)
class Override:
    """A live ``lte_overrides`` row (``revoked_at IS NULL``). ``group_id=None`` — every group."""

    kind: str  # limit | exempt | no_block
    group_id: int | None = None
    limit_bytes: int | None = None
    applies_to: str = "all"  # all | paid | trial
    period_id: int | None = None
    valid_until: datetime | None = None
    exempt_kind: str | None = None

    def alive(self, now: datetime, period_id: int | None) -> bool:
        if self.valid_until is not None and self.valid_until <= now:
            return False
        return self.period_id is None or self.period_id == period_id

    def covers(self, group_id: int) -> bool:
        return self.group_id is None or self.group_id == group_id


@dataclass(frozen=True, slots=True)
class LiveBlock:
    """An ``lte_blocks`` row in a live status (``active`` or ``releasing``)."""

    id: int
    group_id: int
    mode: BlockMode = "enforce"
    status: str = "active"
    reason: str = REASON_QUOTA
    period_id: int | None = None
    release_reason: str | None = None

    @property
    def is_live(self) -> bool:
        return self.status in LIVE_BLOCK_STATUSES

    @property
    def is_applied(self) -> bool:
        return self.status == "active"


@dataclass(frozen=True, slots=True)
class SubjectInput:
    """One subscription for the cycle: its live period, rights, usage, credits, overrides and live blocks."""

    subscription_id: int
    panel_user_id: int
    period_id: int | None = None
    period_state: str = "open"
    period_is_trial: bool = False
    rights: frozenset[int] = frozenset()
    used: Mapping[int, int] = field(default_factory=dict)
    credits: Mapping[int, int] = field(default_factory=dict)  # Σ active lte_credits of the period
    gap_estimated: Mapping[int, int] = field(default_factory=dict)  # this cycle's estimated share after a gap
    clamped_sanity: frozenset[int] = frozenset()
    overrides: tuple[Override, ...] = ()
    live_blocks: tuple[LiveBlock, ...] = ()
    notified: frozenset[tuple[int, str]] = frozenset()  # (group, kind) already sent in this period
    reset_notice_groups: frozenset[int] = frozenset()  # past period had a block or a warning
    identity_ok: bool = True
    frozen: bool = False  # IP Guard / admin hold: LTE is not shown and not decided while frozen

    @property
    def has_live_period(self) -> bool:
        return self.period_id is not None and self.period_state in ("open", "deferred")

    def block_of(self, group_id: int) -> LiveBlock | None:
        for block in self.live_blocks:
            if block.group_id == group_id and block.is_live:
                return block
        return None

    def exempt_groups(self, now: datetime) -> tuple[bool, frozenset[int]]:
        """``(exempt from every group, exempt group ids)``."""
        alive = [o for o in self.overrides if o.kind == "exempt" and o.alive(now, self.period_id)]
        return any(o.group_id is None for o in alive), frozenset(
            o.group_id for o in alive if o.group_id is not None
        )

    def no_block_groups(self, now: datetime, groups: Iterable[int]) -> frozenset[int]:
        alive = [o for o in self.overrides if o.kind == "no_block" and o.alive(now, self.period_id)]
        return frozenset(g for g in groups if any(o.covers(g) for o in alive))

    def limit_rows(self, group_id: int, now: datetime) -> dict[str, int | None]:
        """Individual limit rows for the group (``applies_to`` → value); a group-specific row wins."""
        rows: dict[str, int | None] = {}
        alive = [
            o
            for o in self.overrides
            if o.kind == "limit" and o.alive(now, self.period_id) and o.covers(group_id)
        ]
        for override in sorted(alive, key=lambda o: o.group_id is not None):
            rows[override.applies_to] = override.limit_bytes
        return rows


# ---------------------------------------------------------------- limits


@dataclass(frozen=True, slots=True)
class EffectiveLimit:
    """``base + Σ credits − margin`` of the period; three states of the base row."""

    found: bool
    base: int | None
    margin: int = 0
    credits: int = 0
    exempt: bool = False

    @property
    def unlimited(self) -> bool:
        return self.exempt or not self.found or self.base is None

    @property
    def zero(self) -> bool:
        """The group is unavailable (``base = 0``; credits and margin do not matter)."""
        return not self.unlimited and self.base == 0

    @property
    def limit(self) -> int | None:
        if self.unlimited:
            return None
        return max(0, int(self.base or 0) + self.credits - self.margin)

    @property
    def shown(self) -> int | None:
        """What the user sees: ``base + credits`` (without the margin)."""
        if self.unlimited:
            return None
        return int(self.base or 0) + self.credits

    def percent_of(self, used: int) -> int | None:
        limit = self.limit
        if limit is None or limit <= 0:
            return None
        return used * 100 // limit


def margin_of(group: GroupInput, base: int | None) -> int:
    """``max(0, margin_bytes + base × margin_pct / 100)``."""
    if base is None:
        return 0
    return max(0, int(group.margin_bytes) + int(base) * int(group.margin_pct) // 100)


def base_limit(
    is_trial: bool, user_rows: Mapping[str, int | None] | None, group_rows: Mapping[str, int | None] | None
) -> tuple[bool, int | None]:
    """Choose the base **by presence of a row**: individual ``trial|paid`` → ``all`` → group ``trial`` →
    group ``default``. ``(False, None)`` — no row at all (the group must not be active)."""
    user_rows = user_rows or {}
    group_rows = group_rows or {}
    for key in ("trial" if is_trial else "paid", "all"):
        if key in user_rows:
            return True, user_rows[key]
    if is_trial and "trial" in group_rows:
        return True, group_rows["trial"]
    if "default" in group_rows:
        return True, group_rows["default"]
    return False, None


def effective_limit(
    group: GroupInput,
    *,
    is_trial: bool,
    user_rows: Mapping[str, int | None] | None = None,
    credit_bytes: int = 0,
    exempt: bool = False,
) -> EffectiveLimit:
    """Effective limit of a subscription in a group; an exempt subscription has no limit at all."""
    if exempt:
        return EffectiveLimit(found=False, base=None, exempt=True)
    found, base = base_limit(is_trial, user_rows, group.limit_rows)
    return EffectiveLimit(
        found=found, base=base, margin=margin_of(group, base), credits=max(0, int(credit_bytes))
    )


# ---------------------------------------------------------------- outputs


@dataclass(frozen=True, slots=True)
class BlockCandidate:
    subscription_id: int
    panel_user_id: int
    group_id: int
    period_id: int
    mode: BlockMode
    reason: str
    used_bytes: int
    limit_bytes: int
    decided_incomplete: tuple[str, ...] = ()
    ratio: float = 0.0  # used/limit — order of throttling


@dataclass(frozen=True, slots=True)
class Restore:
    """A ``releasing`` block goes back to ``active`` (usage crossed the limit again before the release)."""

    block_id: int
    subscription_id: int
    panel_user_id: int
    group_id: int
    period_id: int
    reason: str
    used_bytes: int
    limit_bytes: int
    mode: BlockMode = "enforce"


@dataclass(frozen=True, slots=True)
class Rebind:
    """A live block moves to the new period without a release and without a panel write."""

    block_id: int
    subscription_id: int
    panel_user_id: int
    group_id: int
    period_id: int
    reason: str


@dataclass(frozen=True, slots=True)
class Release:
    """``active → releasing`` (then the substitution row is deleted and the writer re-targets the user).

    ``immediate`` — a shadow block: nothing in the panel, straight to ``released``.
    """

    block_id: int
    subscription_id: int
    panel_user_id: int
    group_id: int
    release_reason: str
    immediate: bool = False


@dataclass(frozen=True, slots=True)
class NotifyRequest:
    """A notification request; quiet hours, dedup (``notification_log``) and sending are the runtime's."""

    subscription_id: int
    panel_user_id: int
    group_id: int
    period_id: int
    kind: str
    threshold: int | None = None
    used_bytes: int = 0
    limit_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class SubjectDecision:
    candidates: tuple[BlockCandidate, ...] = ()
    restores: tuple[Restore, ...] = ()
    rebinds: tuple[Rebind, ...] = ()
    releases: tuple[Release, ...] = ()
    notifications: tuple[NotifyRequest, ...] = ()
    warnings: tuple[str, ...] = ()
    #: groups where the subscription counts in the blocked-share fuse: (eligible, blocked now)
    eligible_groups: frozenset[int] = frozenset()
    blocked_groups: frozenset[int] = frozenset()


def _mode_for(settings: EnforceSettings, group: GroupInput, panel_user_id: int) -> BlockMode | None:
    if settings.mode == "off":
        return None
    applies = (
        settings.mode == "on" and group.enforce and (not settings.pilot or panel_user_id in settings.pilot)
    )
    return MODE_ENFORCE if applies else MODE_SHADOW


def decide_subject(
    subject: SubjectInput,
    *,
    groups: Mapping[int, GroupInput],
    settings: EnforceSettings,
    now: datetime,
) -> SubjectDecision:
    """Decisions for one subscription (no cycle-wide fuses — see :func:`planner.decide`)."""
    out = _Collector(subject)
    exempt_all, exempt_groups = subject.exempt_groups(now)
    if exempt_all or not subject.identity_ok:
        reason = RELEASE_EXEMPT if exempt_all else RELEASE_IDENTITY
        for block in subject.live_blocks:
            if block.is_live and not (block.status == "releasing" and block.release_reason == reason):
                out.release(block, reason)
        return out.result()
    if settings.mode == "off":
        # No decisions at all; "снять" releases every block, "оставить" keeps enforce blocks as they are.
        for block in subject.live_blocks:
            if block.is_applied and (settings.release_when_off or block.mode == MODE_SHADOW):
                out.release(block, RELEASE_FEATURE_OFF)
        return out.result()
    for block in subject.live_blocks:
        if not block.is_live or (block.status == "releasing" and block.release_reason is not None):
            continue
        if block.group_id in exempt_groups:
            out.release(block, RELEASE_EXEMPT)
        elif block.group_id not in groups:
            out.release(block, RELEASE_GROUP_DELETED)
        elif block.group_id not in subject.rights:
            out.release(block, RELEASE_NO_RIGHTS)
    if subject.frozen or not subject.has_live_period:
        return out.result()
    period_id = int(subject.period_id or 0)
    no_block = subject.no_block_groups(now, subject.rights)
    for group_id in sorted(subject.rights):
        group = groups.get(group_id)
        if group is None or group_id in exempt_groups:
            continue
        _decide_group(out, subject, group, settings=settings, now=now, period_id=period_id, no_block=no_block)
    return out.result()


class _Collector:
    __slots__ = (
        "blocked",
        "candidates",
        "eligible",
        "notifications",
        "rebinds",
        "released",
        "releases",
        "restores",
        "subject",
        "warnings",
    )

    def __init__(self, subject: SubjectInput) -> None:
        self.subject = subject
        self.candidates: list[BlockCandidate] = []
        self.restores: list[Restore] = []
        self.rebinds: list[Rebind] = []
        self.releases: list[Release] = []
        self.released: set[int] = set()
        self.notifications: list[NotifyRequest] = []
        self.warnings: list[str] = []
        self.eligible: set[int] = set()
        self.blocked: set[int] = set()

    def release(self, block: LiveBlock, reason: str) -> None:
        if block.id in self.released:
            return
        self.released.add(block.id)
        self.releases.append(
            Release(
                block_id=block.id,
                subscription_id=self.subject.subscription_id,
                panel_user_id=self.subject.panel_user_id,
                group_id=block.group_id,
                release_reason=reason,
                immediate=block.mode == MODE_SHADOW,
            )
        )

    def notify(
        self, group_id: int, period_id: int, kind: str, used: int, limit: EffectiveLimit, **kw: int
    ) -> None:
        self.notifications.append(
            NotifyRequest(
                subscription_id=self.subject.subscription_id,
                panel_user_id=self.subject.panel_user_id,
                group_id=group_id,
                period_id=period_id,
                kind=kind,
                used_bytes=used,
                limit_bytes=limit.shown,
                **kw,
            )
        )

    def result(self) -> SubjectDecision:
        return SubjectDecision(
            candidates=tuple(self.candidates),
            restores=tuple(self.restores),
            rebinds=tuple(self.rebinds),
            releases=tuple(self.releases),
            notifications=tuple(self.notifications),
            warnings=tuple(dict.fromkeys(self.warnings)),
            eligible_groups=frozenset(self.eligible),
            blocked_groups=frozenset(self.blocked),
        )


def _decide_group(
    out: _Collector,
    subject: SubjectInput,
    group: GroupInput,
    *,
    settings: EnforceSettings,
    now: datetime,
    period_id: int,
    no_block: frozenset[int],
) -> None:
    group_id = group.id
    mode = _mode_for(settings, group, subject.panel_user_id)
    block = subject.block_of(group_id)
    if block is not None and block.id in out.released:
        return
    if mode is None:  # pragma: no cover - "off" is handled in decide_subject
        return
    if block is not None and block.mode != mode:
        if block.mode == MODE_SHADOW:
            out.release(block, RELEASE_MODE_CHANGE)  # immediate: frees the unique slot for a real block
            block = None
        elif settings.mode == "shadow":
            out.eligible.add(group_id)
            if block.is_applied:
                out.blocked.add(group_id)
            return  # shadow keeps imported / earlier enforce blocks untouched
        else:
            if block.is_applied:
                out.release(block, RELEASE_FEATURE_OFF)  # outside the pilot or the group does not enforce
            return

    out.eligible.add(group_id)
    if block is not None and block.is_applied:
        out.blocked.add(group_id)
    used = int(subject.used.get(group_id, 0))
    limit = effective_limit(
        group,
        is_trial=subject.period_is_trial,
        user_rows=subject.limit_rows(group_id, now),
        credit_bytes=subject.credits.get(group_id, 0),
    )
    if not limit.found:
        out.warnings.append(f"group:{group_id}:no_limit_row")
        needed, reason = False, REASON_QUOTA
    elif limit.zero:
        needed, reason = True, REASON_UNAVAILABLE
    elif limit.unlimited:
        needed, reason = False, REASON_QUOTA
    else:
        needed, reason = used >= int(limit.limit or 0) and group_id not in no_block, REASON_QUOTA
    if needed and reason == REASON_QUOTA:
        gap = int(subject.gap_estimated.get(group_id, 0))
        if gap > 0 and used - gap < int(limit.limit or 0):
            needed = False
            out.warnings.append(
                f"subscription:{subject.subscription_id}:group:{group_id}:{HOLD_GAP_ESTIMATED}"
            )
    limit_value = int(limit.limit or 0)
    if needed and block is not None:
        keep_reason = REASON_MANUAL if block.reason == REASON_MANUAL else reason
        if block.status == "releasing":
            out.restores.append(
                Restore(
                    block.id,
                    subject.subscription_id,
                    subject.panel_user_id,
                    group_id,
                    period_id,
                    keep_reason,
                    used,
                    limit_value,
                    block.mode,
                )
            )
            out.blocked.add(group_id)
        elif block.period_id != period_id:
            out.rebinds.append(
                Rebind(
                    block.id, subject.subscription_id, subject.panel_user_id, group_id, period_id, keep_reason
                )
            )
        return
    if needed:
        holds = list(group.blocks_new_blocks)
        if group_id in subject.clamped_sanity:
            holds.append(HOLD_CLAMPED_SANITY)
        if holds:
            out.warnings.append(f"subscription:{subject.subscription_id}:group:{group_id}:{holds[0]}")
            return
        out.candidates.append(
            BlockCandidate(
                subscription_id=subject.subscription_id,
                panel_user_id=subject.panel_user_id,
                group_id=group_id,
                period_id=period_id,
                mode=mode,
                reason=reason,
                used_bytes=used,
                limit_bytes=limit_value,
                decided_incomplete=group.incomplete,
                ratio=(used / limit_value) if limit_value > 0 else float("inf"),
            )
        )
        if mode == MODE_ENFORCE and reason == REASON_QUOTA:
            # "unavailable" (limit 0) is a setting, not an exhaustion: no "исчерпан" message for it
            out.notify(group_id, period_id, NOTIFY_EXHAUSTED, used, limit)
        return
    if block is not None:
        if block.reason == REASON_MANUAL:
            if block.period_id != period_id and block.is_applied:
                out.rebinds.append(
                    Rebind(
                        block.id,
                        subject.subscription_id,
                        subject.panel_user_id,
                        group_id,
                        period_id,
                        REASON_MANUAL,
                    )
                )
            return
        if block.is_applied:
            out.release(block, RELEASE_TOPUP if limit.credits > 0 else RELEASE_LIMIT_CHANGE)
            out.blocked.discard(group_id)
        return
    if mode != MODE_ENFORCE:
        return  # no notifications in the shadow or outside the pilot
    if (
        group_id in subject.reset_notice_groups
        and limit.found
        and (group_id, NOTIFY_RESET) not in subject.notified
    ):
        out.notify(group_id, period_id, NOTIFY_RESET, used, limit)
    percent = limit.percent_of(used)
    if (
        percent is not None
        and percent >= settings.warn_percent
        and (group_id, NOTIFY_WARN) not in subject.notified
    ):
        out.notify(group_id, period_id, NOTIFY_WARN, used, limit, threshold=settings.warn_percent)


def after_block_resend_due(
    *,
    applied_at: datetime | None,
    last_delta_at: datetime | None,
    resend_done_at: datetime | None,
    now: datetime,
) -> bool:
    """ "Traffic after a block": **new** deltas (``last_delta_at`` after the last resend) 20+ min after the
    block was applied → alert and resend, at most once an hour (lesson ``0324a19b4``: a cumulative total is
    not an event)."""
    if applied_at is None or last_delta_at is None:
        return False
    threshold = applied_at + AFTER_BLOCK_GRACE
    if resend_done_at is not None:
        threshold = max(threshold, resend_done_at)
        if now < resend_done_at + AFTER_BLOCK_EVERY:
            return False
    return last_delta_at > threshold


# ---------------------------------------------------------------- squads (X1)


@dataclass(frozen=True, slots=True)
class Twin:
    """An ``lte_twins`` row: the twin of a base squad for a group, plus the last invariant check."""

    base_squad_uuid: str
    group_id: int
    twin_squad_uuid: str
    problem: str | None = None


def rights_of(
    base_squads: Iterable[str],
    *,
    squad_inbounds: Mapping[str, Collection[str]],
    group_tags: Mapping[int, Collection[str]],
) -> frozenset[int]:
    """Groups the squads give rights to: ``tags(G) ⊆ inbounds(base)`` for at least one base squad."""
    rights: set[int] = set()
    bases = [frozenset(squad_inbounds.get(squad, ())) for squad in base_squads]
    for group_id, tags in group_tags.items():
        wanted = frozenset(tags)
        if wanted and any(wanted <= inbounds for inbounds in bases):
            rights.add(group_id)
    return frozenset(rights)


def substitutions_for(
    desired_squads: Sequence[str],
    blocked_groups: Collection[int],
    twins: Mapping[str, Twin],
    *,
    squad_inbounds: Mapping[str, Collection[str]],
    group_tags: Mapping[int, Collection[str]],
) -> tuple[dict[str, str], frozenset[int]]:
    """``(base → twin for the blocked groups, groups that cannot be enforced)``.

    A blocked group is unenforceable when no desired base with rights to it has a valid twin (missing twin,
    a twin with a problem, or a twin with no inbounds) — the runtime raises "блок неприменим" instead of
    writing an empty or wrong set (05 §2.1.10, lesson 9).
    """
    blocked = set(blocked_groups)
    subs: dict[str, str] = {}
    covered: set[int] = set()
    for base in desired_squads:
        twin = twins.get(base)
        inbounds = frozenset(squad_inbounds.get(base, ()))
        for group_id in blocked:
            tags = frozenset(group_tags.get(group_id, ()))
            if not tags or not tags <= inbounds:
                continue
            if twin is None or twin.group_id != group_id or twin.problem is not None:
                continue
            if not squad_inbounds.get(twin.twin_squad_uuid):
                continue
            subs[base] = twin.twin_squad_uuid
            covered.add(group_id)
    entitled = {
        g
        for g in blocked
        if any(
            frozenset(group_tags.get(g, ())) <= frozenset(squad_inbounds.get(b, ())) for b in desired_squads
        )
    }
    return subs, frozenset(entitled - covered)


def project_squads(desired_squads: Sequence[str], substitutions: Mapping[str, str]) -> list[str]:
    """Panel squads for the desired ones: every substituted base becomes its twin (order kept, no dups)."""
    out: list[str] = []
    for squad in desired_squads:
        target = substitutions.get(squad, squad)
        if target not in out:
            out.append(target)
    return out


def reverse_squads(panel_squads: Sequence[str], twin_to_base: Mapping[str, str]) -> list[str]:
    """Logical squads for a panel set: a twin becomes its base again (before comparing with desired)."""
    out: list[str] = []
    for squad in panel_squads:
        target = twin_to_base.get(squad, squad)
        if target not in out:
            out.append(target)
    return out


@dataclass(frozen=True, slots=True)
class TwinProblem:
    """An invariant violation for "Требует внимания" (I2/I3/I4/I7)."""

    code: str  # twin_missing | twin_empty | twin_mismatch | twin_is_base | base_without_twin | foreign_node
    subject: str  # squad or node uuid
    group_id: int | None = None
    detail: str = ""


def check_twins(
    twins: Iterable[Twin],
    *,
    squad_inbounds: Mapping[str, Collection[str]],
    group_tags: Mapping[int, Collection[str]],
) -> tuple[TwinProblem, ...]:
    """I3 (twin = base − tags(G)), I7/I11 (twin not empty), I4 (every base with group inbounds has a twin).

    An empty twin found in the panel is a fact to report, never something to "fix" (lesson 9).
    """
    problems: list[TwinProblem] = []
    twin_list = list(twins)
    all_tags: set[str] = set().union(*(set(t) for t in group_tags.values())) if group_tags else set()
    for twin in twin_list:
        base = frozenset(squad_inbounds.get(twin.base_squad_uuid, ()))
        tags = frozenset(group_tags.get(twin.group_id, ()))
        if twin.base_squad_uuid == twin.twin_squad_uuid:
            problems.append(TwinProblem("twin_is_base", twin.base_squad_uuid, twin.group_id))
            continue
        if twin.twin_squad_uuid not in squad_inbounds:
            problems.append(TwinProblem("twin_missing", twin.twin_squad_uuid, twin.group_id))
            continue
        actual = frozenset(squad_inbounds[twin.twin_squad_uuid])
        if not actual:
            problems.append(TwinProblem("twin_empty", twin.twin_squad_uuid, twin.group_id))
            continue
        expected = base - tags
        if actual != expected:
            extra = sorted(actual - expected)
            missing = sorted(expected - actual)
            problems.append(
                TwinProblem(
                    "twin_mismatch",
                    twin.twin_squad_uuid,
                    twin.group_id,
                    f"лишние: {', '.join(extra) or '—'}; не хватает: {', '.join(missing) or '—'}",
                )
            )
    with_twin = {twin.base_squad_uuid for twin in twin_list} | {twin.twin_squad_uuid for twin in twin_list}
    for squad, inbounds in sorted(squad_inbounds.items()):
        if squad in with_twin or not (frozenset(inbounds) & all_tags):
            continue
        for group_id, tags in sorted(group_tags.items()):
            if tags and frozenset(tags) <= frozenset(inbounds):
                problems.append(TwinProblem("base_without_twin", squad, group_id))
    return tuple(problems)


def check_foreign_nodes(
    *,
    node_inbounds: Mapping[str, Collection[str]],
    group_nodes: Mapping[int, Collection[str]],
    group_tags: Mapping[int, Collection[str]],
) -> tuple[TwinProblem, ...]:
    """I2: inbounds of a group active on a node outside the group (a block would cut a foreign node and its
    traffic would not be counted); also how a new node with LTE inbounds shows up."""
    problems: list[TwinProblem] = []
    for group_id, tags in sorted(group_tags.items()):
        members = set(group_nodes.get(group_id, ()))
        wanted = frozenset(tags)
        for node, inbounds in sorted(node_inbounds.items()):
            if node not in members and wanted & frozenset(inbounds):
                problems.append(
                    TwinProblem("foreign_node", node, group_id, ", ".join(sorted(wanted & set(inbounds))))
                )
    return tuple(problems)
