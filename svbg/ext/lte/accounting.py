"""LTE quotas: traffic accounting — counters, deltas, intervals, allocation and buckets (05 §2.1.8).

The only source is the panel history read by ``POST /api/bandwidth-stats/nodes/usage`` **one UTC date per
call**: a cumulative ``totalBytes`` per (node, UTC date, panel user), written by the panel in batches roughly
every 2 minutes, the date taken from ``NOW()`` in UTC at INSERT. Hence:

- every cycle reads D−1 and D for the nodes of LTE groups (after downtime — every date since the last good
  read, at most ``max_catchup_days``) — :func:`node_read_plan`, :func:`batch_requests`;
- the delta of a key follows the rules Δ1–Δ8/Δ7′ with ``carry`` (:func:`classify_counter`), key invariant
  ``baseline + accounted = total + carry``; mass detectors spot a truncated or rolled back history;
- the delta interval ``(s, e]`` is cut by the UTC day of the date and pinned after a connection gap
  (:func:`delta_interval`);
- the interval is split by node membership, then by subscription periods; a boundary closer than ``snap`` to
  an edge takes everything to one side, otherwise the cut moves by the write lag ``B + 120 s`` and bytes split
  proportionally to time without loss (:func:`allocate`);
- the result goes to ``lte_period_usage`` (decisions), ``lte_usage_hourly`` (72 h, exact series starts) and
  ``lte_usage_daily`` (MSK days, re-summing periods) — :class:`UsageBatch`.

Everything here is pure; :func:`account_cycle` runs one whole cycle over prepared inputs and returns what to
write. Reading the panel and writing the rows is the runtime's job (``collector``/``service``).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from enum import IntFlag
from typing import Any, Final

from svbg.ext.lte.model import int_param, msk_date, msk_midnight, utc_date

__all__ = [
    "ANOMALY_COUNTER_REGRESS",
    "ANOMALY_HISTORY_ROLLBACK",
    "ANOMALY_INVARIANT",
    "ANOMALY_PANEL_SETTINGS",
    "ANOMALY_ROWS_VANISHED",
    "CYCLE_INTERVAL_SECONDS",
    "INCOMPLETE_CATCHUP_LIMIT",
    "INCOMPLETE_NODE_DISCONNECTED",
    "INCOMPLETE_READ_ERROR",
    "AccountingParams",
    "Allocation",
    "AllocationResult",
    "CounterKey",
    "CounterOutcome",
    "CounterState",
    "CounterWrite",
    "CoverageEntry",
    "CycleResult",
    "DateRequest",
    "DeltaFlag",
    "GroupState",
    "Interval",
    "MembershipSegment",
    "NodeCycleInput",
    "NodeMark",
    "NodeMembership",
    "NodeReadContext",
    "NodeReadPlan",
    "PairOutcome",
    "PeriodSpan",
    "PeriodUsageDelta",
    "Retention",
    "SubjectSpans",
    "UsageBatch",
    "UsageRow",
    "account_cycle",
    "allocate",
    "batch_requests",
    "build_group_states",
    "classify_counter",
    "counters_retention_cutoff",
    "cycle_slot",
    "day_bounds_utc",
    "delta_interval",
    "detect_history_rollback",
    "detect_rows_vanished",
    "diff_pair",
    "gap_tail_active",
    "group_at",
    "history_gap_alert",
    "history_gap_ratio",
    "hour_floor",
    "is_implausible",
    "node_read_plan",
    "parse_usage_response",
    "rebaseline_counter",
    "retention_cutoffs",
    "sanity_limit_bytes",
    "split_by_membership",
    "spread_over_hours",
    "usage_between",
]

#: Cycle of ``lte.cycle`` (code constant, 05 §2.1.7): 600 s, started 5 s after the slot.
CYCLE_INTERVAL_SECONDS: Final = 600
CYCLE_OFFSET_SECONDS: Final = 5
#: Retention (05 §2.1.8): counters 3 days (the last read state of a node is kept), hourly 72 h, daily 70 days,
#: closed periods 13 months.
COUNTERS_KEEP_DAYS: Final = 3
HOURLY_KEEP_HOURS: Final = 72
DAILY_KEEP_DAYS: Final = 70
CLOSED_PERIODS_KEEP_DAYS: Final = 396
#: Daily reconciliation "node as a whole vs the sum of users": alert above 5 % loss.
HISTORY_GAP_ALERT_PERCENT: Final = 5


class DeltaFlag(IntFlag):
    """Properties of one delta (kept in memory and in counters of the usage rows, not stored per delta)."""

    NONE = 0
    NEW_ROW = 1
    AFTER_GAP = 2
    NODE_RECONNECTED = 4
    CLAMPED_SANITY = 8
    GROUP_SPLIT = 16
    REAPPEARED = 32


RULE_D1: Final = "d1_growth"
RULE_D2: Final = "d2_same"
RULE_D3: Final = "d3_new_row"
RULE_D4: Final = "d4_baseline"
RULE_D5: Final = "d5_new_node"
RULE_D6: Final = "d6_regress"
RULE_D7: Final = "d7_vanished"
RULE_D7_PRIME: Final = "d7_reappeared"
RULE_D8: Final = "d8_user_deleted"
RULE_IDLE: Final = "idle"
RULE_REBASELINE: Final = "rebaseline"

# Incompleteness does not forbid new blocks.
INCOMPLETE_READ_ERROR: Final = "read_error"
INCOMPLETE_NODE_DISCONNECTED: Final = "node_disconnected"
INCOMPLETE_NODE_ORPHANED: Final = "node_orphaned"
INCOMPLETE_CATCHUP_LIMIT: Final = "catchup_limit"
INCOMPLETE_CYCLE_PARTIAL: Final = "cycle_partial"

# Anomalies forbid new blocks of the group; releases go on.
ANOMALY_COUNTER_REGRESS: Final = "counter_regress"
ANOMALY_ROWS_VANISHED: Final = "rows_vanished"
ANOMALY_HISTORY_ROLLBACK: Final = "history_rollback"
ANOMALY_PANEL_SETTINGS: Final = "panel_settings"
ANOMALY_QUARANTINE: Final = "quarantine"
ANOMALY_INVARIANT: Final = "invariant"
ANOMALY_RECONCILE_OVERCOUNT: Final = "reconcile_overcount"
#: Anomalies that are incidents: sticky until an admin clears them (05 §2.1.8 "до ручного снятия").
INCIDENT_ANOMALIES: Final = frozenset({ANOMALY_ROWS_VANISHED, ANOMALY_HISTORY_ROLLBACK})


# ---------------------------------------------------------------- parameters and clock helpers


@dataclass(frozen=True, slots=True)
class AccountingParams:
    """Accounting knobs (05 §2.1.7 "Расширенные"); clamped like the registry ranges."""

    snap_seconds: int = 60  # LTE_BOUNDARY_SNAP_S (0–300)
    write_lag_seconds: int = 120  # LTE_WRITE_LAG_S (0–300)
    max_catchup_days: int = 35  # LTE_MAX_CATCHUP_DAYS (7–70)
    sanity_max_mbps: int = 2000  # LTE_SANITY_MAX_MBPS (100–100000)
    rollback_min_keys: int = 5
    rollback_min_percent: int = 10
    gap_tail_cycles: int = 2

    @classmethod
    def from_values(cls, values: Mapping[str, Any]) -> AccountingParams:
        return cls(
            snap_seconds=int_param(values, "boundary_snap_s", 60, 0, 300),
            write_lag_seconds=int_param(values, "write_lag_s", 120, 0, 300),
            max_catchup_days=int_param(values, "max_catchup_days", 35, 7, 70),
            sanity_max_mbps=int_param(values, "sanity_max_mbps", 2000, 100, 100_000),
        )


def day_bounds_utc(day: date) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return start, start + timedelta(days=1)


def hour_floor(moment: datetime) -> datetime:
    """Start of the UTC hour of ``moment`` — the key of ``lte_usage_hourly``."""
    moment = moment.astimezone(UTC)
    return moment.replace(minute=0, second=0, microsecond=0)


def cycle_slot(moment: datetime, *, interval_seconds: int = CYCLE_INTERVAL_SECONDS) -> datetime:
    """``floor(now / 600 s) × 600 s``: the slot a cycle belongs to (00:00 MSK = 21:00 UTC is a slot)."""
    moment = moment.astimezone(UTC)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    elapsed = int((moment - epoch).total_seconds())
    return epoch + timedelta(seconds=elapsed - elapsed % interval_seconds)


def clock_skew_seconds(panel_date: datetime | None, bot_now: datetime) -> float | None:
    """``panel_date − bot_now`` by the ``Date`` header of the panel's answer."""
    if panel_date is None:
        return None
    return (panel_date.astimezone(UTC) - bot_now.astimezone(UTC)).total_seconds()


def sanity_limit_bytes(*, interval_seconds: float, max_mbps: int) -> int:
    """Physical limit of a link over the interval: ``Mbit/s × 125 000 × seconds``."""
    return int(max(0.0, interval_seconds) * max(0, max_mbps) * 125_000)


def is_implausible(delta_bytes: int, *, interval_seconds: float, max_mbps: int) -> bool:
    """A delta above the physical limit is not cut, it is flagged and holds new blocks for one cycle."""
    limit = sanity_limit_bytes(interval_seconds=interval_seconds, max_mbps=max_mbps)
    return limit > 0 and delta_bytes > limit


# ---------------------------------------------------------------- read plan


@dataclass(frozen=True, slots=True)
class NodeReadPlan:
    """UTC dates to read for a node in this cycle."""

    node_uuid: str
    dates: tuple[date, ...]
    first_read: bool = False
    catchup_dates: frozenset[date] = frozenset()
    truncated_from: date | None = None
    truncated_to: date | None = None

    @property
    def truncated(self) -> bool:
        return self.truncated_from is not None


def node_read_plan(
    *, node_uuid: str, panel_now: datetime, last_ok_read_at: datetime | None, max_catchup_days: int
) -> NodeReadPlan:
    """Always D−1 and D; after downtime every date from the last good read, at most ``max_catchup_days``."""
    today = utc_date(panel_now)
    dates = {today - timedelta(days=1), today}
    if last_ok_read_at is None:
        return NodeReadPlan(node_uuid=node_uuid, dates=tuple(sorted(dates)), first_read=True)
    last_date = utc_date(last_ok_read_at)
    floor_date = today - timedelta(days=max(0, max_catchup_days))
    truncated_from: date | None = None
    truncated_to: date | None = None
    if last_date < floor_date:
        truncated_from, truncated_to = last_date, floor_date - timedelta(days=1)
        start = floor_date
    else:
        start = last_date
    day = start
    while day <= today:
        dates.add(day)
        day += timedelta(days=1)
    catchup = frozenset(day for day in dates if last_date < day < today)
    return NodeReadPlan(
        node_uuid=node_uuid,
        dates=tuple(sorted(dates)),
        catchup_dates=catchup,
        truncated_from=truncated_from,
        truncated_to=truncated_to,
    )


@dataclass(frozen=True, slots=True)
class DateRequest:
    """One ``nodes/usage`` call: a date and the nodes that need it (a date range would glue sums)."""

    usage_date: date
    node_uuids: tuple[str, ...]


def batch_requests(plans: Iterable[NodeReadPlan]) -> tuple[DateRequest, ...]:
    by_date: dict[date, set[str]] = {}
    for plan in plans:
        for usage_date in plan.dates:
            by_date.setdefault(usage_date, set()).add(plan.node_uuid)
    return tuple(
        DateRequest(usage_date=d, node_uuids=tuple(sorted(nodes))) for d, nodes in sorted(by_date.items())
    )


@dataclass(frozen=True, slots=True)
class UsageRow:
    node_uuid: str
    panel_user_id: int
    total_bytes: int


@dataclass(frozen=True, slots=True)
class CoverageEntry:
    """A (node, date) read: ``ok=False`` — HTTP error / timeout; ``rows=0`` is a success."""

    node_uuid: str
    usage_date: date
    ok: bool
    rows: int


def parse_usage_response(payload: Any) -> dict[str, tuple[UsageRow, ...]]:
    """``{nodes:[{uuid, users:[{id,totalBytes}]}]}`` → rows per node; garbage entries are skipped."""
    nodes = payload.get("nodes") if isinstance(payload, Mapping) else payload
    result: dict[str, list[UsageRow]] = {}
    for node in nodes or ():
        if not isinstance(node, Mapping):
            continue
        node_uuid = str(node.get("uuid") or "").lower()
        if not node_uuid:
            continue
        rows = result.setdefault(node_uuid, [])
        for user in node.get("users") or ():
            if not isinstance(user, Mapping):
                continue
            try:
                panel_user_id = int(user.get("id"))  # type: ignore[arg-type]
                total = int(user.get("totalBytes") or 0)
            except (TypeError, ValueError):
                continue
            if panel_user_id <= 0:
                continue
            rows.append(UsageRow(node_uuid=node_uuid, panel_user_id=panel_user_id, total_bytes=max(0, total)))
    return {node_uuid: tuple(rows) for node_uuid, rows in result.items()}


# ---------------------------------------------------------------- counters and deltas Δ1–Δ8


@dataclass(frozen=True, slots=True)
class CounterKey:
    node_uuid: str
    usage_date: date
    panel_user_id: int


@dataclass(frozen=True, slots=True)
class CounterState:
    """An ``lte_counters`` row. Invariant: ``baseline + accounted = total + carry``."""

    total_bytes: int = 0
    baseline_bytes: int = 0
    accounted_bytes: int = 0
    carry_bytes: int = 0
    vanished_at: datetime | None = None
    exists: bool = False

    @property
    def invariant_error(self) -> int:
        return self.baseline_bytes + self.accounted_bytes - self.total_bytes - self.carry_bytes

    @property
    def invariant_ok(self) -> bool:
        return self.invariant_error == 0


@dataclass(frozen=True, slots=True)
class NodeReadContext:
    """What Δ3/Δ4/Δ5 depend on. ``first_read_at=None`` — first read of the node (or after a rebaseline)."""

    node_uuid: str
    first_read_at: datetime | None = None
    node_created_at: datetime | None = None
    prev_cycle_read_at: datetime | None = None
    is_first_module_cycle: bool = False


@dataclass(frozen=True, slots=True)
class CounterOutcome:
    rule: str
    delta_bytes: int
    counter: CounterState
    flags: DeltaFlag = DeltaFlag.NONE
    anomaly: str | None = None


def classify_counter(
    *,
    current_bytes: int | None,
    state: CounterState,
    node: NodeReadContext,
    read_at: datetime,
    user_exists: bool = True,
    catchup: bool = False,
) -> CounterOutcome:
    """Rules Δ1–Δ8 and Δ7′ for one key. ``current_bytes=None`` — the key is absent from the answer."""
    if current_bytes is None:
        if not state.exists or state.vanished_at is not None:
            return CounterOutcome(rule=RULE_IDLE, delta_bytes=0, counter=state)
        return CounterOutcome(
            rule=RULE_D7 if user_exists else RULE_D8,
            delta_bytes=0,
            counter=replace(state, vanished_at=read_at),
        )
    current = max(0, int(current_bytes))
    if state.exists and state.vanished_at is not None:
        # Δ7′: the history was wiped, everything in the row is new; the old value goes to carry.
        return CounterOutcome(
            rule=RULE_D7_PRIME,
            delta_bytes=current,
            counter=replace(
                state,
                carry_bytes=state.carry_bytes + state.total_bytes,
                accounted_bytes=state.accounted_bytes + current,
                total_bytes=current,
                vanished_at=None,
            ),
            flags=DeltaFlag.REAPPEARED,
        )
    if state.exists:
        if current > state.total_bytes:
            delta = current - state.total_bytes
            return CounterOutcome(
                rule=RULE_D1,
                delta_bytes=delta,
                counter=replace(state, accounted_bytes=state.accounted_bytes + delta, total_bytes=current),
            )
        if current == state.total_bytes:
            return CounterOutcome(rule=RULE_D2, delta_bytes=0, counter=state)
        # Δ6: a restored panel database must not give an overcount.
        return CounterOutcome(
            rule=RULE_D6,
            delta_bytes=0,
            counter=replace(
                state, carry_bytes=state.carry_bytes + (state.total_bytes - current), total_bytes=current
            ),
            anomaly=ANOMALY_COUNTER_REGRESS,
        )
    if node.first_read_at is not None:
        flags = DeltaFlag.NEW_ROW | (DeltaFlag.AFTER_GAP if catchup else DeltaFlag.NONE)
        return CounterOutcome(
            rule=RULE_D3,
            delta_bytes=current,
            counter=CounterState(total_bytes=current, accounted_bytes=current, exists=True),
            flags=flags,
        )
    node_is_new = (
        not node.is_first_module_cycle
        and node.node_created_at is not None
        and node.prev_cycle_read_at is not None
        and node.node_created_at >= node.prev_cycle_read_at
    )
    if node_is_new:
        return CounterOutcome(
            rule=RULE_D5,
            delta_bytes=current,
            counter=CounterState(total_bytes=current, accounted_bytes=current, exists=True),
            flags=DeltaFlag.NEW_ROW,
        )
    return CounterOutcome(
        rule=RULE_D4,
        delta_bytes=0,
        counter=CounterState(total_bytes=current, baseline_bytes=current, exists=True),
    )


def rebaseline_counter(*, current_bytes: int, state: CounterState) -> CounterOutcome:
    """New baseline of an existing key without counting (module re-enabled, bot DB restored)."""
    current = max(0, int(current_bytes))
    if state.vanished_at is not None:
        counter = replace(
            state,
            baseline_bytes=state.baseline_bytes + current,
            carry_bytes=state.carry_bytes + state.total_bytes,
            total_bytes=current,
            vanished_at=None,
        )
    elif current >= state.total_bytes:
        counter = replace(
            state, baseline_bytes=state.baseline_bytes + current - state.total_bytes, total_bytes=current
        )
    else:
        counter = replace(
            state, carry_bytes=state.carry_bytes + state.total_bytes - current, total_bytes=current
        )
    return CounterOutcome(rule=RULE_REBASELINE, delta_bytes=0, counter=counter)


def detect_rows_vanished(*, live_positive_keys: int, vanished_now: int) -> bool:
    """**All** live rows of a (node, date) with ``total > 0`` vanished — suspected ``TRUNCATE``."""
    return live_positive_keys > 0 and vanished_now >= live_positive_keys


def detect_history_rollback(
    *, regress_keys: int, live_keys: int, min_keys: int = 5, min_percent: int = 10
) -> bool:
    """Mass regress ≥ ``max(min_keys, min_percent % of keys)`` — the panel database was restored."""
    if regress_keys <= 0:
        return False
    threshold = max(min_keys, (live_keys * min_percent + 99) // 100)
    return regress_keys >= threshold


@dataclass(frozen=True, slots=True)
class PairOutcome:
    """All keys of one (node, date): outcomes, and the pair-level anomalies."""

    outcomes: Mapping[CounterKey, CounterOutcome]
    anomalies: frozenset[str] = frozenset()


def diff_pair(
    *,
    node: NodeReadContext,
    usage_date: date,
    rows: Sequence[UsageRow],
    counters: Mapping[CounterKey, CounterState],
    read_at: datetime,
    catchup: bool = False,
    known_users: Collection[int] | None = None,
    params: AccountingParams | None = None,
) -> PairOutcome:
    """Rules for every key of the pair: present in ``rows`` and/or in ``counters`` (prior state of the pair).

    On the first read of a node (``first_read_at=None``) existing keys get a new baseline, new keys Δ4/Δ5.
    """
    params = params or AccountingParams()
    current = {row.panel_user_id: row.total_bytes for row in rows}
    prior = {
        k.panel_user_id: v
        for k, v in counters.items()
        if k.node_uuid == node.node_uuid and k.usage_date == usage_date
    }
    outcomes: dict[CounterKey, CounterOutcome] = {}
    regress = 0
    vanished = 0
    live_positive = sum(1 for s in prior.values() if s.exists and s.vanished_at is None and s.total_bytes > 0)
    live = sum(1 for s in prior.values() if s.exists and s.vanished_at is None)
    for pid in sorted(set(current) | set(prior)):
        key = CounterKey(node.node_uuid, usage_date, pid)
        state = prior.get(pid, CounterState())
        value = current.get(pid)
        if node.first_read_at is None and state.exists and value is not None:
            outcome = rebaseline_counter(current_bytes=value, state=state)
        else:
            outcome = classify_counter(
                current_bytes=value,
                state=state,
                node=node,
                read_at=read_at,
                user_exists=known_users is None or pid in known_users,
                catchup=catchup,
            )
        if outcome.rule == RULE_D6:
            regress += 1
        if outcome.rule in (RULE_D7, RULE_D8) and state.total_bytes > 0:
            vanished += 1
        outcomes[key] = outcome
    anomalies: set[str] = set()
    if regress:
        anomalies.add(ANOMALY_COUNTER_REGRESS)
    if detect_history_rollback(
        regress_keys=regress,
        live_keys=live,
        min_keys=params.rollback_min_keys,
        min_percent=params.rollback_min_percent,
    ):
        anomalies.add(ANOMALY_HISTORY_ROLLBACK)
    if detect_rows_vanished(live_positive_keys=live_positive, vanished_now=vanished):
        anomalies.add(ANOMALY_ROWS_VANISHED)
    return PairOutcome(outcomes=outcomes, anomalies=frozenset(anomalies))


# ---------------------------------------------------------------- delta interval


@dataclass(frozen=True, slots=True)
class Interval:
    start: datetime
    end: datetime
    flags: DeltaFlag = DeltaFlag.NONE

    @property
    def seconds(self) -> float:
        return max(0.0, (self.end - self.start).total_seconds())


def delta_interval(
    *,
    read_at: datetime,
    usage_date: date,
    last_ok_read_at: datetime | None,
    gap_anchor_read_at: datetime | None = None,
    node_reconnected: bool = False,
    panel_gap: bool = False,
    catchup: bool = False,
    fallback_start: datetime | None = None,
    write_lag_seconds: int = 120,
) -> Interval:
    """Interval ``(s, e]`` of a delta: ``e`` — the read moment (panel clock), ``s`` — the last good read.

    (a) a date before the date of ``e`` ends at ``d+1 00:00 UTC``; (b) without pinning the start is not
    earlier than ``d 00:00 − write lag``; (c) after a gap the start is pinned at the last good read before the
    gap (xray flushes the backlog in one piece).
    """
    flags = DeltaFlag.NONE
    start = last_ok_read_at
    anchored = False
    if (node_reconnected or panel_gap) and gap_anchor_read_at is not None:
        start = gap_anchor_read_at
        anchored = True
        flags |= DeltaFlag.NODE_RECONNECTED if node_reconnected else DeltaFlag.AFTER_GAP
    elif panel_gap and start is not None:
        flags |= DeltaFlag.AFTER_GAP
    day_start, day_end = day_bounds_utc(usage_date)
    if start is None:
        start = fallback_start or day_start
    end = read_at
    if catchup:
        start = max(start, day_start)
        end = min(read_at, day_end)
        flags |= DeltaFlag.AFTER_GAP
    else:
        if usage_date < utc_date(end):
            end = min(end, day_end)
        if not anchored and usage_date > utc_date(start):
            start = max(start, day_start - timedelta(seconds=max(0, write_lag_seconds)))
    end = max(end, start)
    return Interval(start=start, end=end, flags=flags)


def gap_tail_active(
    *,
    gap_closed_at: datetime | None,
    now: datetime,
    tail_cycles: int,
    write_lag_seconds: int = 120,
    settle_seconds: int = 15,
) -> bool:
    """Is the start still pinned after a gap closed (``tail_cycles`` left or the flush not settled yet)?"""
    if gap_closed_at is None:
        return False
    if tail_cycles > 0:
        return True
    return now < gap_closed_at + timedelta(seconds=write_lag_seconds + settle_seconds)


# ---------------------------------------------------------------- splitting an interval


@dataclass(frozen=True, slots=True)
class _Cut:
    boundary: datetime
    at: datetime
    estimated: bool


def _cut_points(
    boundaries: Iterable[datetime], interval: Interval, *, snap_seconds: int, write_lag_seconds: int
) -> tuple[_Cut, ...]:
    """Cuts of the interval by boundaries, shifted by the write lag; snapped near the edges."""
    cuts: list[_Cut] = []
    for edge in sorted(set(boundaries)):
        if edge <= interval.start or edge >= interval.end:
            continue
        if (interval.end - edge).total_seconds() <= snap_seconds:
            cuts.append(_Cut(boundary=edge, at=interval.end, estimated=False))
        elif (edge - interval.start).total_seconds() <= snap_seconds:
            cuts.append(_Cut(boundary=edge, at=interval.start, estimated=False))
        else:
            shifted = edge + timedelta(seconds=write_lag_seconds)
            cuts.append(
                _Cut(boundary=edge, at=min(max(shifted, interval.start), interval.end), estimated=True)
            )
    cuts.sort(key=lambda cut: (cut.at, cut.boundary))
    return tuple(cuts)


def _split_bytes(total_bytes: int, spans: Sequence[float]) -> tuple[int, ...]:
    """Proportional split without loss: the parts sum to exactly ``total_bytes``."""
    if not spans:
        return ()
    if len(spans) == 1:
        return (total_bytes,)
    whole = sum(spans)
    if whole <= 0:
        return tuple(total_bytes if i == len(spans) - 1 else 0 for i in range(len(spans)))
    parts: list[int] = []
    assigned = 0
    running = 0.0
    for index, span in enumerate(spans):
        if index == len(spans) - 1:
            parts.append(total_bytes - assigned)
            break
        running += span
        upto = int(total_bytes * running / whole)
        parts.append(upto - assigned)
        assigned = upto
    return tuple(parts)


@dataclass(frozen=True, slots=True)
class _Piece:
    at: datetime
    start: datetime
    end: datetime
    bytes_value: int
    estimated: bool


def _pieces(
    interval: Interval, total_bytes: int, cuts: Sequence[_Cut], *, include_zero: bool = False
) -> tuple[_Piece, ...]:
    reps = [interval.start, *[cut.boundary for cut in cuts]]
    edges = [interval.start, *[cut.at for cut in cuts], interval.end]
    spans = [max(0.0, (edges[i + 1] - edges[i]).total_seconds()) for i in range(len(reps))]
    shares = _split_bytes(total_bytes, spans)
    pieces: list[_Piece] = []
    for index, rep in enumerate(reps):
        estimated = bool(
            (index > 0 and cuts[index - 1].estimated) or (index < len(cuts) and cuts[index].estimated)
        )
        if shares[index] == 0 and not include_zero:
            continue
        pieces.append(_Piece(rep, edges[index], edges[index + 1], shares[index], estimated))
    return tuple(pieces)


@dataclass(frozen=True, slots=True)
class NodeMembership:
    """A membership span of a node in a group: ``[counted_from, counted_to)`` of ``lte_group_nodes``."""

    group_id: int
    counted_from: datetime | None = None
    counted_to: datetime | None = None

    def covers(self, moment: datetime) -> bool:
        if self.counted_from is not None and moment < self.counted_from:
            return False
        return not (self.counted_to is not None and moment >= self.counted_to)


def group_at(memberships: Sequence[NodeMembership], moment: datetime) -> int | None:
    """Group of the node at ``moment`` (a node is in at most one group at a time)."""
    for membership in memberships:
        if membership.covers(moment):
            return membership.group_id
    return None


@dataclass(frozen=True, slots=True)
class MembershipSegment:
    group_id: int | None
    interval: Interval
    bytes_value: int
    estimated: bool = False
    after_block: bool = False


def split_by_membership(
    *,
    interval: Interval,
    total_bytes: int,
    memberships: Sequence[NodeMembership] = (),
    snap_seconds: int,
    write_lag_seconds: int,
) -> tuple[MembershipSegment, ...]:
    """Split a delta by membership changes; parts outside any group get ``group_id=None``."""
    boundaries = [
        moment for ms in memberships for moment in (ms.counted_from, ms.counted_to) if moment is not None
    ]
    cuts = _cut_points(boundaries, interval, snap_seconds=snap_seconds, write_lag_seconds=write_lag_seconds)
    pieces = _pieces(interval, total_bytes, cuts, include_zero=not cuts)
    return tuple(
        MembershipSegment(
            group_id=group_at(memberships, piece.at),
            interval=Interval(start=piece.start, end=piece.end),
            bytes_value=piece.bytes_value,
            estimated=piece.estimated,
        )
        for piece in pieces
    )


@dataclass(frozen=True, slots=True)
class PeriodSpan:
    """A period of the subscription (``lte_periods``) as seen by the allocation."""

    period_id: int
    starts_at: datetime
    planned_end_at: datetime
    ended_at: datetime | None = None
    state: str = "open"
    series_first: bool = False
    anchor_at: datetime | None = None
    is_trial: bool = False

    @property
    def end_at(self) -> datetime | None:
        """``ended_at``, otherwise ``+∞`` for a deferred period, otherwise the planned end."""
        if self.ended_at is not None:
            return self.ended_at
        if self.state == "deferred":
            return None
        return self.planned_end_at

    def intersects(self, interval: Interval) -> bool:
        if self.starts_at >= interval.end:
            return False
        end_at = self.end_at
        return end_at is None or end_at > interval.start


BRANCH_NO_PERIOD: Final = "no_period"
BRANCH_SERIES_FIRST: Final = "series_first"
BRANCH_SERIES_TAIL: Final = "series_tail"
BRANCH_SINGLE: Final = "single"
BRANCH_BOUNDARY: Final = "boundary"


@dataclass(frozen=True, slots=True)
class Allocation:
    period_id: int | None
    group_id: int | None
    bytes_value: int
    estimated: bool = False
    after_block: bool = False


@dataclass(frozen=True, slots=True)
class AllocationResult:
    allocations: tuple[Allocation, ...] = ()
    segments: tuple[MembershipSegment, ...] = ()
    flags: DeltaFlag = DeltaFlag.NONE
    branches: tuple[str, ...] = ()
    start_estimated_period_ids: frozenset[int] = frozenset()
    end_estimated_period_ids: frozenset[int] = frozenset()

    @property
    def total_bytes(self) -> int:
        return sum(item.bytes_value for item in self.allocations)

    def gap_estimated_by_period(self, delta_flags: DeltaFlag) -> dict[int, int]:
        """Estimated shares of deltas after a gap (flags 2/4) per period."""
        if not delta_flags & (DeltaFlag.AFTER_GAP | DeltaFlag.NODE_RECONNECTED):
            return {}
        result: dict[int, int] = {}
        for item in self.allocations:
            if item.estimated and item.period_id is not None:
                result[item.period_id] = result.get(item.period_id, 0) + item.bytes_value
        return result


def allocate(
    *,
    interval: Interval,
    total_bytes: int,
    periods: Sequence[PeriodSpan] = (),
    memberships: Sequence[NodeMembership] = (),
    blocked_since: Mapping[int, datetime] | None = None,
    snap_seconds: int,
    write_lag_seconds: int,
) -> AllocationResult:
    """Allocate a delta: by membership segments, then by the subscription's period boundaries."""
    blocked = dict(blocked_since or {})
    segments = tuple(
        replace(
            seg,
            after_block=seg.group_id is not None
            and seg.group_id in blocked
            and blocked[seg.group_id] < interval.end,
        )
        for seg in split_by_membership(
            interval=interval,
            total_bytes=total_bytes,
            memberships=memberships,
            snap_seconds=snap_seconds,
            write_lag_seconds=write_lag_seconds,
        )
    )
    ordered = sorted(periods, key=lambda span: span.starts_at)
    merged: dict[tuple[int | None, int | None], Allocation] = {}
    branches: list[str] = []
    flags = DeltaFlag.GROUP_SPLIT if len(segments) > 1 else DeltaFlag.NONE
    start_estimated: set[int] = set()
    end_estimated: set[int] = set()

    def add(period_id: int | None, group_id: int | None, value: int, *, estimated: bool) -> None:
        after_block = group_id is not None and group_id in blocked and blocked[group_id] < interval.end
        key = (period_id, group_id)
        previous = merged.get(key)
        if previous is None:
            merged[key] = Allocation(period_id, group_id, value, estimated, after_block)
        else:
            merged[key] = Allocation(
                period_id,
                group_id,
                previous.bytes_value + value,
                previous.estimated or estimated,
                previous.after_block or after_block,
            )

    for segment in segments:
        matched = [span for span in ordered if span.intersects(segment.interval)]
        if not matched:
            branches.append(BRANCH_NO_PERIOD)
            add(None, segment.group_id, segment.bytes_value, estimated=segment.estimated)
            continue
        if len(matched) == 1:
            span = matched[0]
            branches.append(_single_branch(span, segment.interval, ordered, snap_seconds=snap_seconds))
            add(span.period_id, segment.group_id, segment.bytes_value, estimated=segment.estimated)
            continue
        branches.append(BRANCH_BOUNDARY)
        cuts = _cut_points(
            [span.starts_at for span in matched[1:]],
            segment.interval,
            snap_seconds=snap_seconds,
            write_lag_seconds=write_lag_seconds,
        )
        pieces = _pieces(segment.interval, segment.bytes_value, cuts, include_zero=not cuts)
        for index, piece in enumerate(pieces):
            span = matched[min(index, len(matched) - 1)] if not cuts else _period_at(matched, piece.at)
            add(
                span.period_id,
                segment.group_id,
                piece.bytes_value,
                estimated=piece.estimated or segment.estimated,
            )
        for index, cut in enumerate(cuts):
            if cut.estimated:
                end_estimated.add(matched[index].period_id)
                start_estimated.add(matched[index + 1].period_id)

    allocations = tuple(merged[key] for key in sorted(merged, key=lambda item: (item[0] or 0, item[1] or 0)))
    return AllocationResult(
        allocations=allocations,
        segments=segments,
        flags=flags,
        branches=tuple(branches),
        start_estimated_period_ids=frozenset(start_estimated),
        end_estimated_period_ids=frozenset(end_estimated),
    )


def _period_at(spans: Sequence[PeriodSpan], moment: datetime) -> PeriodSpan:
    chosen = spans[0]
    for span in spans:
        if span.starts_at <= moment:
            chosen = span
    return chosen


def _single_branch(
    span: PeriodSpan, interval: Interval, ordered: Sequence[PeriodSpan], *, snap_seconds: int
) -> str:
    """Which branch handled a single intersecting period: series head, series tail or plain."""
    snap = timedelta(seconds=snap_seconds)
    earlier_same_series = any(o.anchor_at == span.anchor_at and o.starts_at < span.starts_at for o in ordered)
    if span.series_first and interval.start < span.starts_at - snap and not earlier_same_series:
        return BRANCH_SERIES_FIRST
    end_at = span.end_at
    has_next = any(o.starts_at > span.starts_at for o in ordered)
    if end_at is not None and interval.end > end_at + snap and not has_next:
        return BRANCH_SERIES_TAIL
    return BRANCH_SINGLE


# ---------------------------------------------------------------- buckets


def spread_over_hours(interval: Interval, total_bytes: int) -> dict[datetime, int]:
    """Bytes of an interval by UTC hours, proportionally to time and without loss.

    A degenerate interval (``start == end``) puts everything into the hour just before ``end``.
    """
    if total_bytes <= 0:
        return {}
    if interval.end <= interval.start:
        return {hour_floor(interval.end - timedelta(microseconds=1)): total_bytes}
    edges = [interval.start]
    hour = hour_floor(interval.start) + timedelta(hours=1)
    while hour < interval.end:
        edges.append(hour)
        hour += timedelta(hours=1)
    edges.append(interval.end)
    spans = [(edges[i + 1] - edges[i]).total_seconds() for i in range(len(edges) - 1)]
    shares = _split_bytes(total_bytes, spans)
    result: dict[datetime, int] = {}
    for index, share in enumerate(shares):
        if share:
            key = hour_floor(edges[index])
            result[key] = result.get(key, 0) + share
    return result


@dataclass(slots=True)
class PeriodUsageDelta:
    """Increment of an ``lte_period_usage`` row (``UPDATE … SET used = used + Δ``)."""

    used_bytes: int = 0
    after_block_bytes: int = 0
    estimated_bytes: int = 0
    last_delta_at: datetime | None = None


@dataclass(slots=True)
class DailyDelta:
    bytes_value: int = 0
    after_block_bytes: int = 0


@dataclass(slots=True)
class UsageBatch:
    """Everything a cycle adds to the usage tables; merged per row so one ``executemany`` per table."""

    period_usage: dict[tuple[int, int], PeriodUsageDelta] = field(default_factory=dict)
    hourly: dict[tuple[int, int, datetime], int] = field(default_factory=dict)
    daily: dict[tuple[int, int, date], DailyDelta] = field(default_factory=dict)
    #: Estimated share after a gap of **this** cycle, per (period, group) — decide must not block on it alone.
    gap_estimated: dict[tuple[int, int], int] = field(default_factory=dict)
    #: Group bytes of the subscription outside any period (no live period yet / a gap in the series).
    outside_periods: int = 0

    def add(
        self, subscription_id: int, result: AllocationResult, *, delta_flags: DeltaFlag = DeltaFlag.NONE
    ) -> None:
        last_at = max((seg.interval.end for seg in result.segments), default=None)
        for item in result.allocations:
            if item.group_id is None or item.bytes_value <= 0:
                continue
            if item.period_id is None:
                self.outside_periods += item.bytes_value
                continue
            row = self.period_usage.setdefault((item.period_id, item.group_id), PeriodUsageDelta())
            row.used_bytes += item.bytes_value
            if item.after_block:
                row.after_block_bytes += item.bytes_value
            if item.estimated:
                row.estimated_bytes += item.bytes_value
            if last_at is not None and (row.last_delta_at is None or last_at > row.last_delta_at):
                row.last_delta_at = last_at
        if delta_flags & (DeltaFlag.AFTER_GAP | DeltaFlag.NODE_RECONNECTED):
            for item in result.allocations:
                if item.estimated and item.period_id is not None and item.group_id is not None:
                    key = (item.period_id, item.group_id)
                    self.gap_estimated[key] = self.gap_estimated.get(key, 0) + item.bytes_value
        for seg in result.segments:
            if seg.group_id is None or seg.bytes_value <= 0:
                continue
            for hour, value in spread_over_hours(seg.interval, seg.bytes_value).items():
                hkey = (subscription_id, seg.group_id, hour)
                self.hourly[hkey] = self.hourly.get(hkey, 0) + value
                day = self.daily.setdefault((subscription_id, seg.group_id, msk_date(hour)), DailyDelta())
                day.bytes_value += value
                if seg.after_block:
                    day.after_block_bytes += value

    @property
    def empty(self) -> bool:
        return not (self.period_usage or self.hourly or self.daily)


def usage_between(
    start: datetime,
    end: datetime,
    *,
    daily: Mapping[date, int],
    hourly: Mapping[datetime, int],
    hourly_since: datetime | None,
) -> int:
    """Usage of one (subscription, group) in ``[start, end)`` re-summed from the buckets (05 §2.1.9).

    Whole Moscow days come from ``lte_usage_daily``. A partial day (a series started at an arbitrary moment)
    is summed exactly from ``lte_usage_hourly`` when the hours are still kept (``≥ hourly_since``), otherwise
    the day's bytes are split proportionally to time. Partial hours are proportional too.
    """
    if end <= start:
        return 0
    total = 0
    day = msk_date(start)
    last = msk_date(end - timedelta(microseconds=1))
    while day <= last:
        day_start, day_end = msk_midnight(day), msk_midnight(day + timedelta(days=1))
        lo, hi = max(start, day_start), min(end, day_end)
        if lo <= day_start and hi >= day_end:
            total += int(daily.get(day, 0))
        elif hourly_since is not None and lo >= hourly_since:
            hour = hour_floor(lo)
            while hour < hi:
                part_lo, part_hi = max(lo, hour), min(hi, hour + timedelta(hours=1))
                value = int(hourly.get(hour, 0))
                if part_lo == hour and part_hi == hour + timedelta(hours=1):
                    total += value
                else:
                    total += value * int((part_hi - part_lo).total_seconds()) // 3600
                hour += timedelta(hours=1)
        else:
            total += int(daily.get(day, 0)) * int((hi - lo).total_seconds()) // 86_400
        day += timedelta(days=1)
    return total


# ---------------------------------------------------------------- group states


@dataclass(frozen=True, slots=True)
class GroupState:
    """Per-cycle flags of a group: incompleteness never forbids new blocks, an anomaly does."""

    incomplete: tuple[str, ...] = ()
    anomaly: tuple[str, ...] = ()

    @property
    def blocks_new_blocks(self) -> bool:
        return bool(self.anomaly)

    def as_dict(self) -> dict[str, list[str]]:
        return {"incomplete": list(self.incomplete), "anomaly": list(self.anomaly)}


def reason_for_node(reason: str, node_uuid: str) -> str:
    return f"{reason}:{node_uuid}"


def reason_kind(reason: str) -> str:
    return reason.split(":", 1)[0]


def build_group_states(
    *,
    group_nodes: Mapping[int, Collection[str]],
    coverage: Iterable[CoverageEntry] = (),
    disconnected_node_uuids: Collection[str] = (),
    orphaned_node_uuids: Collection[str] = (),
    catchup_limited_node_uuids: Collection[str] = (),
    node_anomalies: Mapping[str, Collection[str]] | None = None,
    global_incomplete: Collection[str] = (),
    global_anomalies: Collection[str] = (),
    suspended_group_ids: Collection[int] = (),
) -> dict[int, GroupState]:
    """Flags of every group for the cycle; "no rows for the date" is not incompleteness."""
    failed: set[str] = {entry.node_uuid for entry in coverage if not entry.ok}
    anomalies_by_node = {str(k).lower(): set(v) for k, v in (node_anomalies or {}).items()}
    disconnected = {str(item).lower() for item in disconnected_node_uuids}
    orphaned = {str(item).lower() for item in orphaned_node_uuids}
    catchup_limited = {str(item).lower() for item in catchup_limited_node_uuids}
    suspended = set(suspended_group_ids)
    states: dict[int, GroupState] = {}
    for group_id, nodes in group_nodes.items():
        incomplete: list[str] = list(global_incomplete)
        anomaly: list[str] = list(global_anomalies)
        if group_id in suspended:
            anomaly.append(ANOMALY_INVARIANT)
        for node_uuid in sorted({str(item).lower() for item in nodes}):
            if node_uuid in failed:
                incomplete.append(reason_for_node(INCOMPLETE_READ_ERROR, node_uuid))
            if node_uuid in orphaned:
                incomplete.append(reason_for_node(INCOMPLETE_NODE_ORPHANED, node_uuid))
            elif node_uuid in disconnected:
                incomplete.append(reason_for_node(INCOMPLETE_NODE_DISCONNECTED, node_uuid))
            if node_uuid in catchup_limited:
                incomplete.append(reason_for_node(INCOMPLETE_CATCHUP_LIMIT, node_uuid))
            anomaly.extend(
                reason_for_node(reason, node_uuid) for reason in sorted(anomalies_by_node.get(node_uuid, ()))
            )
        states[group_id] = GroupState(
            incomplete=tuple(dict.fromkeys(incomplete)), anomaly=tuple(dict.fromkeys(anomaly))
        )
    return states


# ---------------------------------------------------------------- daily checks and retention


def history_gap_ratio(*, users_total_bytes: int, node_total_bytes: int) -> float | None:
    """``Σ(total + carry) of users / traffic of the node`` for a day (``None`` — the node had no traffic)."""
    if node_total_bytes <= 0:
        return None
    return users_total_bytes / node_total_bytes


def history_gap_alert(ratio: float | None, *, alert_percent: int = HISTORY_GAP_ALERT_PERCENT) -> bool:
    return ratio is not None and ratio < 1 - alert_percent / 100


def counters_retention_cutoff(
    *, today_utc: date, last_ok_read_date: date | None, keep_days: int = COUNTERS_KEEP_DAYS
) -> date:
    """Counter dates ``< cutoff`` are deleted: ``min(today − 3, last_ok_read_date − 1)``.

    The last read state of a node survives any downtime, otherwise the catch-up would count a date twice (Δ3).
    """
    cutoff = today_utc - timedelta(days=max(0, keep_days))
    if last_ok_read_date is not None:
        cutoff = min(cutoff, last_ok_read_date - timedelta(days=1))
    return cutoff


@dataclass(frozen=True, slots=True)
class Retention:
    hourly_before: datetime
    daily_before: date
    closed_periods_before: datetime


def retention_cutoffs(now: datetime) -> Retention:
    """Cut-offs of ``lte.retention`` (03:40 MSK): hourly 72 h, daily 70 days, closed periods 13 months."""
    return Retention(
        hourly_before=hour_floor(now) - timedelta(hours=HOURLY_KEEP_HOURS),
        daily_before=msk_date(now) - timedelta(days=DAILY_KEEP_DAYS),
        closed_periods_before=now - timedelta(days=CLOSED_PERIODS_KEEP_DAYS),
    )


# ---------------------------------------------------------------- one whole cycle


@dataclass(frozen=True, slots=True)
class NodeCycleInput:
    """A node of an LTE group in this cycle: its plan, read context, marks and membership."""

    plan: NodeReadPlan
    context: NodeReadContext
    last_ok_read_at: datetime | None = None
    gap_anchor_at: datetime | None = None
    node_reconnected: bool = False  # pin the start at the gap anchor (connection gap or its tail)
    panel_gap: bool = False  # the bot itself did not read (downtime): estimated share
    memberships: tuple[NodeMembership, ...] = ()

    @property
    def node_uuid(self) -> str:
        return self.plan.node_uuid


@dataclass(frozen=True, slots=True)
class SubjectSpans:
    """A subscription as seen by the allocation: its periods and when its live blocks were applied."""

    subscription_id: int
    periods: tuple[PeriodSpan, ...] = ()
    blocked_since: Mapping[int, datetime] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CounterWrite:
    key: CounterKey
    state: CounterState
    insert: bool


@dataclass(frozen=True, slots=True)
class NodeMark:
    """New read marks of ``lte_node_state`` — only when **every** date of the node was read."""

    node_uuid: str
    first_read_at: datetime
    last_ok_read_at: datetime
    last_ok_read_date: date


@dataclass(frozen=True, slots=True)
class CycleResult:
    counters: tuple[CounterWrite, ...]
    usage: UsageBatch
    marks: tuple[NodeMark, ...]
    coverage: tuple[CoverageEntry, ...]
    node_anomalies: Mapping[str, frozenset[str]]
    incidents: Mapping[str, frozenset[str]]
    catchup_limited: frozenset[str]
    clamped: frozenset[tuple[int, int]]
    unmatched_users: frozenset[int]
    unmatched_bytes: int
    rules: Mapping[str, int]


def account_cycle(
    *,
    read_at: datetime,
    nodes: Sequence[NodeCycleInput],
    readings: Mapping[tuple[str, date], Sequence[UsageRow] | None],
    counters: Mapping[CounterKey, CounterState],
    subjects: Mapping[int, SubjectSpans],
    params: AccountingParams | None = None,
    known_users: Collection[int] | None = None,
) -> CycleResult:
    """Account one cycle over prepared inputs; returns what to write in one transaction.

    ``readings[(node, date)]`` — rows of a successful call (``()`` = no rows), ``None`` or missing — the call
    failed. ``counters`` — prior state of every key of the read pairs; ``subjects`` — by ``panel_user_id``.
    Only changed counters are returned (UPSERT of changed keys only).
    """
    params = params or AccountingParams()
    batch = UsageBatch()
    writes: list[CounterWrite] = []
    marks: list[NodeMark] = []
    coverage: list[CoverageEntry] = []
    node_anomalies: dict[str, set[str]] = {}
    clamped: set[tuple[int, int]] = set()
    unmatched_users: set[int] = set()
    unmatched_bytes = 0
    rules: Counter[str] = Counter()
    for node in nodes:
        all_ok = True
        for usage_date in node.plan.dates:
            rows = readings.get((node.node_uuid, usage_date))
            coverage.append(CoverageEntry(node.node_uuid, usage_date, rows is not None, len(rows or ())))
            if rows is None:
                all_ok = False
                continue
            catchup = usage_date in node.plan.catchup_dates
            pair = diff_pair(
                node=node.context,
                usage_date=usage_date,
                rows=rows,
                counters=counters,
                read_at=read_at,
                catchup=catchup,
                known_users=known_users,
                params=params,
            )
            if pair.anomalies:
                node_anomalies.setdefault(node.node_uuid, set()).update(pair.anomalies)
            for key, outcome in pair.outcomes.items():
                rules[outcome.rule] += 1
                prior = counters.get(key, CounterState())
                if outcome.counter != prior:
                    writes.append(CounterWrite(key, outcome.counter, insert=not prior.exists))
                if outcome.delta_bytes <= 0:
                    continue
                interval = delta_interval(
                    read_at=read_at,
                    usage_date=usage_date,
                    last_ok_read_at=node.last_ok_read_at,
                    gap_anchor_read_at=node.gap_anchor_at,
                    node_reconnected=node.node_reconnected,
                    panel_gap=node.panel_gap,
                    catchup=catchup,
                    fallback_start=node.context.node_created_at,
                    write_lag_seconds=params.write_lag_seconds,
                )
                subject = subjects.get(key.panel_user_id)
                if subject is None:
                    unmatched_users.add(key.panel_user_id)
                    unmatched_bytes += outcome.delta_bytes
                    continue
                result = allocate(
                    interval=interval,
                    total_bytes=outcome.delta_bytes,
                    periods=subject.periods,
                    memberships=node.memberships,
                    blocked_since=subject.blocked_since,
                    snap_seconds=params.snap_seconds,
                    write_lag_seconds=params.write_lag_seconds,
                )
                if is_implausible(
                    outcome.delta_bytes, interval_seconds=interval.seconds, max_mbps=params.sanity_max_mbps
                ):
                    clamped.update(
                        (subject.subscription_id, seg.group_id)
                        for seg in result.segments
                        if seg.group_id is not None
                    )
                batch.add(subject.subscription_id, result, delta_flags=interval.flags | outcome.flags)
        if all_ok:
            marks.append(
                NodeMark(
                    node_uuid=node.node_uuid,
                    first_read_at=node.context.first_read_at or read_at,
                    last_ok_read_at=read_at,
                    last_ok_read_date=utc_date(read_at),
                )
            )
    incidents = {
        node: frozenset(reasons & INCIDENT_ANOMALIES)
        for node, reasons in node_anomalies.items()
        if reasons & INCIDENT_ANOMALIES
    }
    return CycleResult(
        counters=tuple(writes),
        usage=batch,
        marks=tuple(marks),
        coverage=tuple(coverage),
        node_anomalies={node: frozenset(reasons) for node, reasons in node_anomalies.items()},
        incidents=incidents,
        catchup_limited=frozenset(node.node_uuid for node in nodes if node.plan.truncated),
        clamped=frozenset(clamped),
        unmatched_users=frozenset(unmatched_users),
        unmatched_bytes=unmatched_bytes,
        rules=dict(rules),
    )
