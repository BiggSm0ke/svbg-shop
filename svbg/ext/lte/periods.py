"""LTE quotas: anchor, series and periods — pure arithmetic of E0–E10 (05 §2.1.9).

No I/O. The series state (``lte_anchors`` + the live ``lte_periods`` row) comes in as a value and goes out as
a new value; what to write is a list of :class:`Action`. The machine is incremental (05 §2.1.9 "Механика"):

    out = process(state, event, now=now)    # one subscription_events row (X2); None — late, replay
    out = advance(state, until=now)         # timers: boundary reset / deferral / end of series (E8)
    out = simulate(state, inputs, until=now)  # replay from the start of a series or an imported point

``simulate`` is ``advance`` + ``apply`` for every input in ``t_eff`` order, so "processing on time" and
"replay from the start" are the same code path (``tests/ext/lte/test_properties.py`` checks it).

Rule order (05 §2.1.9 table): E0 → E1 → E2 → E4 (trial/provisional) → E5 → E3 → E6 → E9, then the extra
checks E7 (deferred period resolved), E6a (trial converted without re-anchoring) and E9 (trial flag dropped
without a payment). E8 and the boundary timer run in :func:`advance`. E10 is :func:`set_anchor`.

Conventions fixed by tests (not spelled out in the design):

- when an input and a timer fall on the same moment, the input goes first (an unfreeze at the very moment of
  E8 extends the series instead of closing it);
- the first period starts at the anchor moment, later ones at 00:00 MSK of the anchor day; the day is clamped
  to the month end and always counted from the anchor (31 → 28 → 31, no drift).
"""

from __future__ import annotations

import calendar
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any, Final, Literal

from svbg.ext.lte.model import MSK, AnchorKind, EventKind, PeriodStateName, int_param

__all__ = [
    "ANCHORING_KINDS",
    "CONVERTIBLE_KINDS",
    "MANUAL_ANCHOR_MODES",
    "REANCHORABLE_KINDS",
    "Action",
    "AnchorState",
    "EffectiveTime",
    "Event",
    "EventResult",
    "HoldInterval",
    "Outcome",
    "PeriodDiff",
    "PeriodParams",
    "PeriodState",
    "StateInput",
    "add_months",
    "advance",
    "anchor_day",
    "apply",
    "boundary",
    "effective_old_end",
    "effective_time",
    "grace_for",
    "index_at",
    "input_moment",
    "is_late",
    "manual_anchor_at",
    "match_rule",
    "parse_manual_anchor",
    "period_bounds",
    "plan_recompute",
    "process",
    "rebuild_period",
    "series_alive",
    "series_end_at",
    "set_anchor",
    "simulate",
]

ActionKind = Literal[
    "close_period",
    "open_period",
    "defer_period",
    "release_blocks",
    "expire_credits",
    "revoke_exemption",
    "needs_review",
    "step",
]

#: Anchoring kinds: only they move the anchor of a live series.
ANCHORING_KINDS: Final = frozenset({"paid", "admin"})
#: Anchor kinds that E4 re-anchors on a payment / admin grant.
REANCHORABLE_KINDS: Final = frozenset({"trial", "provisional"})
#: Anchor kinds where a payment on a live trial period converts it without re-anchoring (E6a). ``import`` is
#: an anchor carried over from a real (Bedolaga) series, so it behaves like ``paid``.
CONVERTIBLE_KINDS: Final = frozenset({"paid", "admin", "manual", "import"})
#: Kinds whose new series is self-explaining (no review).
SELF_EXPLAINING_KINDS: Final = ANCHORING_KINDS | {"trial"}
#: Guard against corrupted data (a wrong coverage would otherwise spin timers forever).
MAX_TIMERS_PER_RUN: Final = 5000
MANUAL_ANCHOR_MODES: Final = frozenset({"exact", "day_start"})


# ---------------------------------------------------------------- parameters


@dataclass(frozen=True, slots=True)
class PeriodParams:
    """Parameters of the period arithmetic (05 §2.1.7 "Расширенные")."""

    grace: timedelta = timedelta(hours=24)  # LTE_RENEWAL_GRACE_HOURS
    daily_tariff_grace: timedelta = timedelta(days=7)  # no daily plans in SvBG; kept for imported series
    rollover_min_remaining: timedelta = timedelta(hours=24)  # R, LTE_ROLLOVER_MIN_REMAINING_HOURS
    paid_at_max_lag: timedelta = timedelta(hours=168)  # window of trust for paid_at (code constant)
    paid_at_tolerance: timedelta = timedelta(seconds=60)  # paid_at ≤ t + 60 s
    close_tolerance: timedelta = timedelta(seconds=60)  # new_end ≤ t_eff + 60 s ⇒ E2
    tz: tzinfo = MSK

    @classmethod
    def from_values(cls, values: Mapping[str, Any]) -> PeriodParams:
        """From registry values (``renewal_grace_hours``, ``rollover_min_remaining_hours``), clamped."""
        return cls(
            grace=timedelta(hours=int_param(values, "renewal_grace_hours", 24, 0, 168)),
            rollover_min_remaining=timedelta(
                hours=int_param(values, "rollover_min_remaining_hours", 24, 0, 72)
            ),
        )


# ---------------------------------------------------------------- month boundary


def add_months(year: int, month: int, k: int) -> tuple[int, int]:
    """``(year, month)`` ``k`` months later (``k`` may be negative)."""
    index = year * 12 + (month - 1) + k
    return index // 12, index % 12 + 1


def boundary(anchor_at: datetime, k: int, *, tz: tzinfo = MSK) -> datetime:
    """Boundary ``k``: 00:00 local of the anchor day in month ``month(A)+k`` as a UTC moment.

    The day is clamped to the month end and always counted **from the anchor** (31 → 28 → 31, no drift).
    """
    if k < 0:
        raise ValueError("boundary: k не может быть отрицательным")
    local = anchor_at.astimezone(tz)
    year, month = add_months(local.year, local.month, k)
    day = min(local.day, calendar.monthrange(year, month)[1])
    return datetime(year, month, day, tzinfo=tz).astimezone(UTC)


def index_at(anchor_at: datetime, moment: datetime, *, tz: tzinfo = MSK) -> int:
    """Index of the period that contains ``moment`` (0 — from the anchor to the first boundary)."""
    local_anchor = anchor_at.astimezone(tz)
    local_moment = moment.astimezone(tz)
    k = max(0, (local_moment.year - local_anchor.year) * 12 + (local_moment.month - local_anchor.month))
    while k > 0 and boundary(anchor_at, k, tz=tz) > moment:
        k -= 1
    while boundary(anchor_at, k + 1, tz=tz) <= moment:
        k += 1
    return k


def period_bounds(anchor_at: datetime, k: int, *, tz: tzinfo = MSK) -> tuple[datetime, datetime]:
    """``(start, planned end)`` of period ``k``; period 0 starts at the anchor itself."""
    starts_at = anchor_at if k == 0 else boundary(anchor_at, k, tz=tz)
    return starts_at, boundary(anchor_at, k + 1, tz=tz)


def anchor_day(anchor_at: datetime, *, tz: tzinfo = MSK) -> int:
    """Day of month of the anchor in Moscow time (1..31) — "сброс 7-го"."""
    return anchor_at.astimezone(tz).day


# ---------------------------------------------------------------- state and inputs


@dataclass(frozen=True, slots=True)
class PeriodState:
    """A period — the value of an ``lte_periods`` row."""

    anchor_at: datetime
    idx: int
    starts_at: datetime
    planned_end_at: datetime
    state: PeriodStateName = "open"
    is_trial: bool = False
    series_first: bool = False
    ended_at: datetime | None = None
    end_cause: str | None = None
    period_id: int | None = None

    @property
    def key(self) -> tuple[datetime, int, datetime]:
        """Matching key when recomputing periods."""
        return (self.anchor_at, self.idx, self.starts_at)

    @property
    def live(self) -> bool:
        return self.state in ("open", "deferred")


@dataclass(frozen=True, slots=True)
class AnchorState:
    """Series state of one subscription — ``lte_anchors`` plus the live ``lte_periods`` row."""

    anchor_at: datetime | None = None
    anchor_kind: AnchorKind | None = None
    anchor_source: str = ""
    series_open: bool = False
    series_started_at: datetime | None = None
    series_closed_at: datetime | None = None
    coverage_end: datetime | None = None
    is_trial: bool = False
    is_daily_tariff: bool = False
    review_reason: str | None = None
    period: PeriodState | None = None

    @property
    def has_anchor(self) -> bool:
        return self.anchor_at is not None

    @property
    def needs_review(self) -> bool:
        return self.review_reason is not None


@dataclass(frozen=True, slots=True)
class Event:
    """A term event — engine view of a ``subscription_events`` row (kind mapped by ``engine_event_kind``)."""

    occurred_at: datetime  # commit time of the change (t)
    kind: EventKind = "unclassified"
    paid_at: datetime | None = None
    old_end: datetime | None = None
    new_end: datetime | None = None
    was_trial: bool | None = None
    is_trial: bool | None = None
    is_daily_tariff: bool | None = None
    is_new_row: bool = False
    source: str | None = None
    event_id: int | None = None

    @property
    def priority(self) -> int:
        return 0

    @property
    def order_id(self) -> int:
        return self.event_id or 0


@dataclass(frozen=True, slots=True)
class StateInput:
    """A state set at a moment: an imported point (``import``), a manual anchor, a successor row."""

    at: datetime
    state: AnchorState
    cause: str = "import"
    row_id: int | None = None

    @property
    def priority(self) -> int:
        return 1

    @property
    def order_id(self) -> int:
        return self.row_id or 0


Input = Event | StateInput


@dataclass(frozen=True, slots=True)
class HoldInterval:
    """A freeze of the subscription (IP Guard or an admin hold). ``unblocked_at=None`` — still frozen.

    While a hold covers the E8 moment the series does not end; after the unfreeze E8 fires at the unfreeze
    moment: ``ended_at = max(coverage_end + G_eff, unblocked_at)`` (05 §2.1.9 E8).
    """

    blocked_at: datetime
    unblocked_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Action:
    """What the runtime writes after a step. ``step`` is the journal line of a rule or a timer."""

    kind: ActionKind
    at: datetime
    cause: str = ""
    period: PeriodState | None = None
    event_id: int | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EventResult:
    event_id: int | None
    t_eff: datetime
    rules: tuple[str, ...]
    effect: str


@dataclass(frozen=True, slots=True)
class Outcome:
    state: AnchorState
    actions: tuple[Action, ...] = ()
    periods: tuple[PeriodState, ...] = ()
    rules: tuple[str, ...] = ()
    effect: str = "none"
    events: tuple[EventResult, ...] = ()
    truncated: bool = False  # MAX_TIMERS_PER_RUN reached — corrupted input, the runtime raises an alert

    def actions_of(self, kind: str) -> tuple[Action, ...]:
        return tuple(a for a in self.actions if a.kind == kind)


# ---------------------------------------------------------------- t_eff, G_eff, eff_old_end


@dataclass(frozen=True, slots=True)
class EffectiveTime:
    at: datetime
    from_paid_at: bool
    out_of_window: bool


def effective_time(event: Event, *, params: PeriodParams) -> EffectiveTime:
    """``t_eff``: ``paid_at`` when ``t − 168 h ≤ paid_at ≤ t + 60 s``, otherwise ``t`` (flagged)."""
    t = event.occurred_at
    if event.paid_at is None:
        return EffectiveTime(t, False, False)
    if event.paid_at <= t + params.paid_at_tolerance and t - event.paid_at <= params.paid_at_max_lag:
        return EffectiveTime(event.paid_at, True, False)
    return EffectiveTime(t, False, True)


def grace_for(state: AnchorState, event: Event | None, *, params: PeriodParams) -> timedelta:
    """``G_eff``: an extended grace for a daily plan (on the event or on the series)."""
    daily = state.is_daily_tariff or bool(event is not None and event.is_daily_tariff)
    return params.daily_tariff_grace if daily else params.grace


def effective_old_end(state: AnchorState, event: Event) -> tuple[datetime | None, str]:
    """``eff_old_end`` and its source: the event's old end, otherwise the coverage of a live series."""
    if event.old_end is not None:
        return event.old_end, "event"
    if state.has_anchor and state.series_open and state.coverage_end is not None:
        return state.coverage_end, "series_coverage"
    return None, "none"


def series_alive(
    state: AnchorState, eff_old_end: datetime | None, moment: datetime, grace: timedelta
) -> bool:
    """ "The series is alive at ``moment``": open, anchored and not later than ``eff_old_end + G_eff``."""
    return (
        state.series_open and state.has_anchor and eff_old_end is not None and moment <= eff_old_end + grace
    )


# ---------------------------------------------------------------- one run


class _Run:
    """Accumulator of actions and periods within one call."""

    __slots__ = ("actions", "event_id", "params", "periods", "state")

    def __init__(self, state: AnchorState, *, params: PeriodParams, event_id: int | None = None) -> None:
        self.state = state
        self.params = params
        self.event_id = event_id
        self.actions: list[Action] = []
        self.periods: list[PeriodState] = [state.period] if state.period is not None else []

    def close_period(self, at: datetime, cause: str, *, expire_credits: bool = True) -> None:
        period = self.state.period
        if period is None or not period.live:
            return
        closed = replace(period, state="closed", ended_at=at, end_cause=cause)
        self.put_period(period, closed)
        self.state = replace(self.state, period=None)
        self.actions.append(Action("close_period", at, cause=cause, period=closed, event_id=self.event_id))
        if expire_credits:
            self.actions.append(
                Action("expire_credits", at, cause=cause, period=closed, event_id=self.event_id)
            )

    def open_period(self, at: datetime, *, is_trial: bool | None = None, series_first: bool = False) -> None:
        anchor = self.state.anchor_at
        if anchor is None:  # pragma: no cover - every caller sets the anchor first
            raise ValueError("open_period: у серии нет якоря")
        tz = self.params.tz
        k = index_at(anchor, at, tz=tz)
        starts_at, planned_end_at = period_bounds(anchor, k, tz=tz)
        period = PeriodState(
            anchor_at=anchor,
            idx=k,
            starts_at=max(starts_at, at),
            planned_end_at=planned_end_at,
            is_trial=self.state.is_trial if is_trial is None else is_trial,
            series_first=series_first,
        )
        self.periods.append(period)
        self.state = replace(self.state, period=period)
        self.actions.append(Action("open_period", at, cause="open", period=period, event_id=self.event_id))

    def defer_period(self, at: datetime) -> None:
        period = self.state.period
        if period is None or period.state != "open":
            return
        deferred = replace(period, state="deferred")
        self.put_period(period, deferred)
        self.state = replace(self.state, period=deferred)
        self.actions.append(Action("defer_period", at, cause="deferred", period=deferred))

    def put_period(self, old: PeriodState, new: PeriodState) -> None:
        for index, item in enumerate(self.periods):
            if item is old or item.key == old.key:
                self.periods[index] = new
                return
        self.periods.append(new)

    def release_blocks(self, at: datetime, reason: str) -> None:
        self.actions.append(Action("release_blocks", at, cause=reason, event_id=self.event_id))

    def needs_review(self, at: datetime, reason: str) -> None:
        self.state = replace(self.state, review_reason=reason)
        self.actions.append(Action("needs_review", at, cause=reason, event_id=self.event_id))

    def revoke_exemption(self, at: datetime) -> None:
        self.actions.append(
            Action(
                "revoke_exemption",
                at,
                cause="converted_to_paid",
                event_id=self.event_id,
                detail={"exempt_kinds": ("launch_trial",)},
            )
        )

    def step(self, at: datetime, cause: str, **detail: Any) -> None:
        self.actions.append(Action("step", at, cause=cause, event_id=self.event_id, detail=detail))

    def finish(
        self, *, rules: tuple[str, ...], effect: str, events: tuple[EventResult, ...], truncated: bool = False
    ) -> Outcome:
        return Outcome(
            state=self.state,
            actions=tuple(self.actions),
            periods=tuple(self.periods),
            rules=rules,
            effect=effect,
            events=events,
            truncated=truncated,
        )


# ---------------------------------------------------------------- rules E0–E9


def apply(state: AnchorState, event: Event, *, params: PeriodParams | None = None) -> Outcome:
    """Apply one term event: E0→E1→E2→E4→E5→E3→E6→E9, then E7, E6a, E9 checks."""
    params = params or PeriodParams()
    run = _Run(state, params=params, event_id=event.event_id)
    eff = effective_time(event, params=params)
    t_eff = eff.at
    if eff.out_of_window:
        run.needs_review(t_eff, "время оплаты вне допустимого окна")

    eff_old_end, old_end_source = effective_old_end(run.state, event)
    grace = grace_for(run.state, event, params=params)
    rules: list[str] = []

    rule = match_rule(run.state, event, t_eff=t_eff, eff_old_end=eff_old_end, grace=grace, params=params)
    rules.append(rule)
    _apply_rule(run, rule, event, t_eff=t_eff, eff_old_end=eff_old_end, old_end_source=old_end_source)

    # E7 — extra check after rules that extended the coverage.
    if rule in ("E0", "E1", "E2", "E5", "E6") and _e7_ready(run.state, t_eff, params):
        _apply_e7(run, t_eff)
        rules.append("E7")

    # E6a — trial conversion without re-anchoring, on top of E1/E5/E6/E7.
    applied = rules[-1]
    converted = False
    if applied in ("E1", "E5", "E6", "E7") and _e6a_ready(run.state, event):
        _apply_e6a(run, t_eff, event, rolled=applied == "E7")
        rules.append("E6a")
        converted = True

    # E9 — trial flag dropped without a payment and without an admin grant.
    if not converted and rule not in ("E3", "E4") and _e9_ready(run.state, event):
        _apply_e9(run, t_eff)
        if rule != "E9":
            rules.append("E9")

    if event.kind == "paid":
        # A launch_trial exemption is revoked only by money (05 §2.1.9); idempotent on the runtime side.
        run.revoke_exemption(t_eff)

    effect = _effect_for(tuple(rules), run.state)
    result = EventResult(event.event_id, t_eff, tuple(rules), effect)
    return run.finish(rules=tuple(rules), effect=effect, events=(result,))


def match_rule(
    state: AnchorState,
    event: Event,
    *,
    t_eff: datetime,
    eff_old_end: datetime | None,
    grace: timedelta,
    params: PeriodParams | None = None,
) -> str:
    """The first matching rule (no effects applied)."""
    params = params or PeriodParams()
    new_end = event.new_end
    if event.kind == "freeze":
        return "E0"  # the hold itself changes nothing; E8 is held by the hold intervals
    if event.new_end == event.old_end and not event.is_new_row and event.kind not in ("close", "unfreeze"):
        return "E0"
    if event.kind == "unfreeze":
        return "E1"
    if new_end is None or event.kind == "close" or new_end <= t_eff + params.close_tolerance:
        return "E2"
    # E4 before E5/E3/E6: an anchoring event on a live trial/provisional series (grace included) re-anchors;
    # otherwise a payment right after the trial ended would stay on the trial anchor and limit.
    if (
        event.kind in ANCHORING_KINDS
        and state.anchor_kind in REANCHORABLE_KINDS
        and series_alive(state, eff_old_end, t_eff, grace)
    ):
        return "E4"
    # E5 before E3: being late by ≤ G_eff keeps the reset day (admin grants included).
    if (
        state.has_anchor
        and state.series_open
        and eff_old_end is not None
        and eff_old_end <= t_eff <= eff_old_end + grace
    ):
        return "E5"
    if eff_old_end is None or t_eff - eff_old_end > grace or not state.has_anchor or not state.series_open:
        return "E3"
    if eff_old_end > t_eff:
        return "E6"
    return "E9"


def _apply_rule(
    run: _Run,
    rule: str,
    event: Event,
    *,
    t_eff: datetime,
    eff_old_end: datetime | None,
    old_end_source: str,
) -> None:
    if rule == "E1":
        _apply_e1(run, t_eff, event)
    elif rule == "E2":
        run.state = replace(run.state, coverage_end=event.new_end if event.new_end is not None else t_eff)
        run.step(t_eff, "close")
    elif rule in ("E5", "E6"):
        _apply_extend(run, t_eff, event, old_end_source=old_end_source, rule=rule)
    elif rule == "E4":
        _apply_e4(run, t_eff, event)
    elif rule == "E3":
        _apply_e3(run, t_eff, event, eff_old_end=eff_old_end, old_end_source=old_end_source)
    # E0 changes nothing; E9 alone is handled by the E9 check in apply()


def _row_trial(event: Event, fallback: bool) -> bool:
    return fallback if event.is_trial is None else bool(event.is_trial)


def _daily(event: Event, fallback: bool) -> bool:
    return fallback if event.is_daily_tariff is None else bool(event.is_daily_tariff)


def _apply_e1(run: _Run, t_eff: datetime, event: Event) -> None:
    """E1: unfreeze — the series goes on; a closed series is reopened (never a new series)."""
    run.state = replace(run.state, coverage_end=event.new_end or run.state.coverage_end)
    if not run.state.series_open and run.state.has_anchor:
        run.state = replace(run.state, series_open=True, series_closed_at=None)
        run.open_period(t_eff)
        run.step(t_eff, "unfreeze_reopen")
        return
    run.step(t_eff, "extend")


def _apply_extend(run: _Run, t_eff: datetime, event: Event, *, old_end_source: str, rule: str) -> None:
    """E5/E6: extension, the anchor stays."""
    run.state = replace(
        run.state, coverage_end=event.new_end, is_daily_tariff=_daily(event, run.state.is_daily_tariff)
    )
    if old_end_source == "series_coverage":
        run.step(t_eff, "extend", rule=rule, linked=True)
        return
    run.step(t_eff, "extend", rule=rule)


def _paid_trial(event: Event, fallback: bool) -> bool:
    """``is_trial`` after the event: a payment is a conversion even if the row still says trial."""
    if event.kind == "paid":
        return False
    return _row_trial(event, fallback)


def _anchor_kind_for(event: Event) -> AnchorKind:
    """Anchor kind of a new series: paid > trial flag > admin > provisional."""
    if event.kind == "paid":
        return "paid"
    if event.is_trial is True or event.kind == "trial":
        return "trial"
    if event.kind == "admin":
        return "admin"
    return "provisional"


def _apply_e3(
    run: _Run, t_eff: datetime, event: Event, *, eff_old_end: datetime | None, old_end_source: str
) -> None:
    """E3: a new series — anchor at ``t_eff``, period 0 marked ``series_first``, usage from zero."""
    kind = _anchor_kind_for(event)
    run.close_period(t_eff, "new_series")
    run.release_blocks(t_eff, "new_series")
    run.state = replace(
        run.state,
        anchor_at=t_eff,
        anchor_kind=kind,
        anchor_source=event.source or event.kind,
        series_started_at=t_eff,
        series_open=True,
        series_closed_at=None,
        coverage_end=event.new_end,
        is_trial=_paid_trial(event, kind == "trial"),
        is_daily_tariff=_daily(event, run.state.is_daily_tariff),
    )
    run.open_period(t_eff, is_trial=run.state.is_trial, series_first=True)
    if event.kind in ("import", "unclassified"):
        run.needs_review(t_eff, "новая серия по событию без вида")
    elif eff_old_end is None and old_end_source == "none" and event.kind not in SELF_EXPLAINING_KINDS:
        run.needs_review(t_eff, "новая серия без прежней даты")
    run.step(t_eff, "trial" if kind == "trial" else "new_series")


def _apply_e4(run: _Run, t_eff: datetime, event: Event) -> None:
    """E4: re-anchor a live trial/provisional series at the payment (or admin grant) moment."""
    kind: AnchorKind = "admin" if event.kind == "admin" else "paid"
    run.close_period(t_eff, "reanchor")
    run.release_blocks(t_eff, "reanchor")
    run.state = replace(
        run.state,
        anchor_at=t_eff,
        anchor_kind=kind,
        anchor_source=event.source or event.kind,
        coverage_end=event.new_end,
        is_trial=_paid_trial(event, run.state.is_trial),
        is_daily_tariff=_daily(event, run.state.is_daily_tariff),
    )
    run.open_period(t_eff, is_trial=run.state.is_trial, series_first=True)
    run.step(t_eff, "reanchor")


def _e7_ready(state: AnchorState, t_eff: datetime, params: PeriodParams) -> bool:
    period = state.period
    return (
        period is not None
        and period.state == "deferred"
        and state.coverage_end is not None
        and state.coverage_end >= t_eff + params.rollover_min_remaining
    )


def _apply_e7(run: _Run, t_eff: datetime) -> None:
    """E7: a deferred reset is resolved — the period closes at the event, a new one opens right away."""
    run.close_period(t_eff, "deferred_rollover")
    run.release_blocks(t_eff, "deferred_reset")
    run.open_period(t_eff)
    run.step(t_eff, "deferred_rollover")


def _e6a_ready(state: AnchorState, event: Event) -> bool:
    period = state.period
    return (
        event.kind in ANCHORING_KINDS
        and state.anchor_kind in CONVERTIBLE_KINDS
        and period is not None
        and period.live
        and (period.is_trial or state.is_trial)
    )


def _apply_e6a(run: _Run, t_eff: datetime, event: Event, *, rolled: bool) -> None:
    """E6a: trial conversion without re-anchoring — a paid period from the payment moment."""
    if rolled:
        period = run.state.period
        if period is not None:
            converted = replace(period, is_trial=False)
            run.put_period(period, converted)
            run.state = replace(run.state, period=converted, is_trial=False)
        run.step(t_eff, "trial_conversion")
        return
    run.close_period(t_eff, "trial_conversion")
    run.release_blocks(t_eff, "trial_conversion")
    run.state = replace(run.state, is_trial=False, is_daily_tariff=_daily(event, run.state.is_daily_tariff))
    run.open_period(t_eff, is_trial=False)
    run.step(t_eff, "trial_conversion")


def _e9_ready(state: AnchorState, event: Event) -> bool:
    return (
        event.was_trial is True
        and event.is_trial is False
        and event.kind not in ANCHORING_KINDS
        and (
            state.is_trial
            or state.anchor_kind == "trial"
            or (state.period is not None and state.period.is_trial)
        )
    )


def _apply_e9(run: _Run, t_eff: datetime) -> None:
    """E9: the trial flag dropped without a payment — the default limit right away, usage kept."""
    period = run.state.period
    if period is not None and period.is_trial:
        flipped = replace(period, is_trial=False)
        run.put_period(period, flipped)
        run.state = replace(run.state, period=flipped)
    kind = "provisional" if run.state.anchor_kind == "trial" else run.state.anchor_kind
    run.state = replace(run.state, anchor_kind=kind, is_trial=False)
    run.step(t_eff, "trial_flip")


def _effect_for(rules: tuple[str, ...], state: AnchorState) -> str:
    if "E3" in rules:
        return "trial_start" if state.anchor_kind == "trial" else "new_series"
    if "E4" in rules:
        return "reanchor"
    if "E7" in rules:
        return "deferred_reset"
    if "E2" in rules:
        return "close"
    if rules == ("E0",):
        return "none"
    return "continue"


# ---------------------------------------------------------------- timers


def series_end_at(
    state: AnchorState, *, params: PeriodParams, holds: Sequence[HoldInterval] = ()
) -> datetime | None:
    """Moment of E8: ``coverage_end + G_eff`` moved by holds; ``None`` — closed, unknown or still frozen."""
    if not state.series_open or state.coverage_end is None or not state.has_anchor:
        return None
    due = state.coverage_end + grace_for(state, None, params=params)
    intervals = sorted(holds, key=lambda item: item.blocked_at)
    moved = True
    while moved:
        moved = False
        for interval in intervals:
            if interval.blocked_at > due:
                continue
            if interval.unblocked_at is None:
                return None
            if interval.unblocked_at > due:
                due = interval.unblocked_at
                moved = True
    return due


def _next_timer(
    state: AnchorState, *, params: PeriodParams, holds: Sequence[HoldInterval]
) -> tuple[datetime, str] | None:
    candidates: list[tuple[datetime, str]] = []
    period = state.period
    if period is not None and period.state == "open":
        candidates.append((period.planned_end_at, "boundary"))
    end_at = series_end_at(state, params=params, holds=holds)
    if end_at is not None:
        candidates.append((end_at, "series_end"))
    if not candidates:
        return None
    return min(candidates)


def _run_timers(run: _Run, *, horizon: datetime, inclusive: bool, holds: Sequence[HoldInterval]) -> bool:
    """Boundaries and the end of the series at their theoretical moments up to ``horizon``.

    Returns ``False`` when :data:`MAX_TIMERS_PER_RUN` was hit (corrupted input).
    """
    for _ in range(MAX_TIMERS_PER_RUN):
        timer = _next_timer(run.state, params=run.params, holds=holds)
        if timer is None:
            return True
        at, kind = timer
        if at > horizon or (at == horizon and not inclusive):
            return True
        if kind == "boundary":
            _timer_boundary(run, at)
        else:
            _timer_series_end(run, at)
    return False


def _timer_boundary(run: _Run, at: datetime) -> None:
    """Reset at the boundary, or defer the period when the coverage does not reach ``boundary + R``."""
    coverage = run.state.coverage_end
    if coverage is None or coverage >= at + run.params.rollover_min_remaining:
        if coverage is None and not run.state.needs_review:
            # Fail-safe: unknown coverage would keep a deferred period forever — reset towards access.
            run.needs_review(at, "покрытие неизвестно: сброс на границе без проверки")
        run.close_period(at, "boundary")
        run.release_blocks(at, "reset")
        run.open_period(at)
        run.step(at, "boundary")
        return
    run.defer_period(at)
    run.step(at, "deferred")


def _timer_series_end(run: _Run, at: datetime) -> None:
    """E8: end of the series."""
    run.close_period(at, "series_end")
    run.release_blocks(at, "series_end")
    run.state = replace(run.state, series_open=False, series_closed_at=at)
    run.step(at, "series_end")


# ---------------------------------------------------------------- simulate / advance


def input_moment(item: Input, *, params: PeriodParams) -> datetime:
    """``t_eff`` of an event or the moment of a state input."""
    if isinstance(item, StateInput):
        return item.at
    return effective_time(item, params=params).at


def advance(
    state: AnchorState,
    *,
    until: datetime,
    params: PeriodParams | None = None,
    holds: Sequence[HoldInterval] = (),
) -> Outcome:
    """Run the timers of the series up to ``until`` inclusive (the cycle's "таймеры периодов")."""
    return simulate(state, (), until=until, params=params, holds=holds)


def is_late(state: AnchorState, t_eff: datetime) -> bool:
    """Timers effective **after** ``t_eff`` were already applied to ``state``.

    Then applying the event incrementally would see a future state (e.g. a payment with ``paid_at`` before
    E8 arriving after E8 closed the series); the runtime re-simulates the series from its start instead.
    """
    period = state.period
    if period is not None:
        if period.starts_at > t_eff:
            return True
        if period.state == "deferred" and period.planned_end_at > t_eff:
            return True
    return not state.series_open and state.series_closed_at is not None and state.series_closed_at > t_eff


def process(
    state: AnchorState,
    event: Event,
    *,
    now: datetime,
    params: PeriodParams | None = None,
    holds: Sequence[HoldInterval] = (),
) -> Outcome | None:
    """Incremental step of the ``lte.term`` hook: timers up to ``t_eff``, the event, timers up to ``now``.

    ``None`` — the event is late (:func:`is_late`): re-simulate from the start of the series (or from the
    imported point) with every event of the subscription.
    """
    params = params or PeriodParams()
    t_eff = effective_time(event, params=params).at
    if is_late(state, t_eff):
        return None
    return simulate(state, (event,), until=max(now, t_eff), params=params, holds=holds)


def simulate(
    state: AnchorState,
    inputs: Iterable[Input] = (),
    *,
    until: datetime,
    params: PeriodParams | None = None,
    holds: Sequence[HoldInterval] = (),
) -> Outcome:
    """Apply inputs in ``(t_eff, priority, id)`` order with the timers between them, up to ``until``."""
    params = params or PeriodParams()
    run = _Run(state, params=params)
    rules: list[str] = []
    events: list[EventResult] = []
    complete = True
    ordered = sorted(
        inputs, key=lambda item: (input_moment(item, params=params), item.priority, item.order_id)
    )
    for item in ordered:
        moment = input_moment(item, params=params)
        complete = _run_timers(run, horizon=min(moment, until), inclusive=False, holds=holds) and complete
        if moment > until:
            break
        if isinstance(item, StateInput):
            run.state = item.state
            if item.state.period is not None:
                run.put_period(item.state.period, item.state.period)
            run.step(item.at, item.cause)
            rules.append("E10" if item.cause == "manual" else item.cause)
            continue
        outcome = apply(run.state, item, params=params)
        run.state = outcome.state
        run.actions.extend(outcome.actions)
        _merge_periods(run, outcome.periods)
        rules.extend(outcome.rules)
        events.extend(outcome.events)
    complete = _run_timers(run, horizon=until, inclusive=True, holds=holds) and complete
    effect = events[-1].effect if events else "none"
    return run.finish(rules=tuple(rules), effect=effect, events=tuple(events), truncated=not complete)


def _merge_periods(run: _Run, periods: Sequence[PeriodState]) -> None:
    for period in periods:
        existing = next((item for item in run.periods if item.key == period.key), None)
        if existing is None:
            run.periods.append(period)
        elif existing != period:
            run.put_period(existing, period)


# ---------------------------------------------------------------- E10: manual anchor


def rebuild_period(
    state: AnchorState, at: datetime, *, params: PeriodParams | None = None, series_first: bool | None = None
) -> PeriodState:
    """The period that contains ``at`` under the current anchor (manual anchor, import)."""
    params = params or PeriodParams()
    if state.anchor_at is None:
        raise ValueError("rebuild_period: у серии нет якоря")
    k = index_at(state.anchor_at, at, tz=params.tz)
    starts_at, planned_end_at = period_bounds(state.anchor_at, k, tz=params.tz)
    return PeriodState(
        anchor_at=state.anchor_at,
        idx=k,
        starts_at=starts_at,
        planned_end_at=planned_end_at,
        is_trial=state.is_trial,
        series_first=k == 0 if series_first is None else series_first,
    )


def parse_manual_anchor(value: Any, mode: str, *, tz: tzinfo = MSK) -> datetime | date:
    """Anchor value typed by an admin → ``datetime`` (``exact``) or ``date`` (``day_start``).

    ISO 8601 without an offset is Moscow time; a bare ``ГГГГ-ММ-ДД`` is allowed only for ``day_start``.
    Anything else raises ``ValueError`` with a short Russian message.
    """
    if mode not in MANUAL_ANCHOR_MODES:
        raise ValueError(f"set_anchor: неизвестный режим {mode!r}")
    if isinstance(value, datetime | date):
        return value
    if not isinstance(value, str) or not value.strip():
        raise ValueError("set_anchor: нужна дата якоря")
    raw = value.strip()
    if len(raw) <= 10:
        if mode != "day_start":
            raise ValueError("set_anchor: для режима exact нужны дата и время")
        try:
            return date.fromisoformat(raw)
        except ValueError:
            raise ValueError("set_anchor: дата ГГГГ-ММ-ДД") from None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        raise ValueError("set_anchor: дата и время ISO 8601") from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)


def manual_anchor_at(
    value: datetime | date | str,
    *,
    mode: str = "exact",
    params: PeriodParams | None = None,
    now: datetime | None = None,
) -> datetime:
    """Anchor moment for E10: ``day_start`` — 00:00 MSK of the day, ``exact`` — the moment. No future."""
    params = params or PeriodParams()
    tz = params.tz
    if not isinstance(value, datetime | date):
        value = parse_manual_anchor(value, mode, tz=tz)
    if mode == "day_start":
        if isinstance(value, datetime):
            day = value.astimezone(tz).date() if value.tzinfo else value.date()
        else:
            day = value
        moment = datetime(day.year, day.month, day.day, tzinfo=tz).astimezone(UTC)
    elif isinstance(value, datetime):
        moment = value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=tz).astimezone(UTC)
    else:
        raise ValueError("set_anchor: для режима exact нужен момент времени")
    if now is not None and moment > now:
        raise ValueError("set_anchor: якорь в будущем")
    return moment


def set_anchor(
    state: AnchorState,
    anchor_at: datetime,
    *,
    at: datetime,
    params: PeriodParams | None = None,
    kind: AnchorKind = "manual",
    source: str = "manual",
) -> Outcome:
    """E10: manual anchor. Paid credits are not burnt (no ``expire_credits``)."""
    params = params or PeriodParams()
    run = _Run(state, params=params)
    run.close_period(at, "manual", expire_credits=False)
    run.state = replace(
        run.state,
        anchor_at=anchor_at,
        anchor_kind=kind,
        anchor_source=source,
        series_open=True,
        series_closed_at=None,
        series_started_at=state.series_started_at or anchor_at,
    )
    period = rebuild_period(run.state, at, params=params)
    run.periods.append(period)
    run.state = replace(run.state, period=period)
    run.actions.append(Action("open_period", at, cause="manual", period=period))
    run.step(at, "manual")
    return run.finish(rules=("E10",), effect="continue", events=())


# ---------------------------------------------------------------- recompute


@dataclass(frozen=True, slots=True)
class PeriodDiff:
    """Stored periods vs. a fresh simulation: ``keep`` pairs (row, new value), stale rows, new rows."""

    keep: tuple[tuple[PeriodState, PeriodState], ...] = ()
    stale: tuple[PeriodState, ...] = ()
    insert: tuple[PeriodState, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.stale or self.insert) or any(old != new for old, new in self.keep)


def plan_recompute(
    old_periods: Sequence[PeriodState], new_periods: Sequence[PeriodState], *, since: datetime | None = None
) -> PeriodDiff:
    """Match stored periods with a simulation by ``(anchor_at, idx, starts_at)``.

    Only live periods and those started (or ended) after ``since`` are in scope (on both sides); older rows
    stay untouched.
    """
    scoped = [period for period in old_periods if _in_scope(period, since)]
    new_by_key = {period.key: period for period in new_periods}
    keep: list[tuple[PeriodState, PeriodState]] = []
    stale: list[PeriodState] = []
    for period in scoped:
        fresh = new_by_key.get(period.key)
        if fresh is None:
            stale.append(period)
            continue
        keep.append((period, replace(fresh, period_id=period.period_id)))
    known = {period.key for period in scoped}
    stored = {period.key for period in old_periods}
    # A simulation from the very first event also yields the old periods: out of scope, already stored.
    insert = [
        period
        for period in new_periods
        if period.key not in known and (period.key not in stored and _in_scope(period, since))
    ]
    return PeriodDiff(tuple(keep), tuple(stale), tuple(insert))


def _in_scope(period: PeriodState, since: datetime | None) -> bool:
    if since is None or period.live:
        return True
    if period.starts_at >= since:
        return True
    return period.ended_at is not None and period.ended_at > since
