"""LTE accounting: Δ1–Δ8, intervals, allocation, buckets, one whole cycle (05 §2.1.8).

The owner's table tests (``test_accounting.py`` of wl_quota) ported to the pure engine, plus the SvBG parts:
MSK daily buckets, the 72 h hourly buffer, re-summing periods and :func:`account_cycle`.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from svbg.ext.lte import accounting as a

NODE = "aaaaaaaa-0000-0000-0000-000000000001"
NODE_2 = "bbbbbbbb-0000-0000-0000-000000000002"
PID = 501
SNAP = 60
LAG = 120
D = date(2026, 9, 17)


def dt(hour: int, minute: int = 0, second: int = 0, *, day: int = 17) -> datetime:
    return datetime(2026, 9, day, hour, minute, second, tzinfo=UTC)


def ctx(**kwargs: object) -> a.NodeReadContext:
    kwargs.setdefault("node_uuid", NODE)
    return a.NodeReadContext(**kwargs)  # type: ignore[arg-type]


def interval(start: datetime, end: datetime) -> a.Interval:
    return a.Interval(start=start, end=end)


def period(period_id: int, starts_at: datetime, planned_end_at: datetime, **kwargs: object) -> a.PeriodSpan:
    return a.PeriodSpan(period_id=period_id, starts_at=starts_at, planned_end_at=planned_end_at, **kwargs)  # type: ignore[arg-type]


def step(state: a.CounterState, current: int | None, **kwargs: object) -> a.CounterOutcome:
    """One accounting step of a key with the mandatory key invariant check."""
    outcome = a.classify_counter(
        current_bytes=current,
        state=state,
        node=kwargs.pop("node", ctx(first_read_at=dt(11))),  # type: ignore[arg-type]
        read_at=kwargs.pop("read_at", dt(12)),  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )
    assert outcome.counter.invariant_ok, (outcome.rule, outcome.counter)
    return outcome


def existing(total: int, **kwargs: object) -> a.CounterState:
    return a.CounterState(total_bytes=total, accounted_bytes=total, exists=True, **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------- cycle schedule, dates


def test_cycle_slot_is_aligned_to_wall_clock() -> None:
    assert a.cycle_slot(dt(12, 3, 17)) == dt(12, 0)
    assert a.cycle_slot(dt(12, 10, 0)) == dt(12, 10)
    assert a.cycle_slot(dt(21, 0, 4)) == dt(21, 0)  # 00:00 MSK = 21:00 UTC is a slot
    assert a.CYCLE_INTERVAL_SECONDS == 600


def test_clock_skew() -> None:
    assert a.clock_skew_seconds(None, dt(12)) is None
    assert a.clock_skew_seconds(dt(12, 0, 30), dt(12)) == 30


def test_msk_and_utc_dates_differ_at_the_boundary() -> None:
    from svbg.ext.lte.model import msk_date, utc_date

    assert utc_date(dt(21, 5)) == D
    assert msk_date(dt(21, 5)) == date(2026, 9, 18)
    assert msk_date(dt(20, 55)) == D
    assert a.day_bounds_utc(D) == (dt(0, 0), dt(0, 0, day=18))


# ---------------------------------------------------------------- read plan


def test_read_plan_always_reads_two_dates() -> None:
    plan = a.node_read_plan(
        node_uuid=NODE, panel_now=dt(0, 2), last_ok_read_at=dt(23, 55, day=16), max_catchup_days=35
    )
    assert plan.dates == (date(2026, 9, 16), D)
    assert plan.catchup_dates == frozenset()
    assert plan.first_read is False


def test_read_plan_first_read_takes_baseline_dates() -> None:
    plan = a.node_read_plan(node_uuid=NODE, panel_now=dt(12), last_ok_read_at=None, max_catchup_days=35)
    assert plan.dates == (date(2026, 9, 16), D)
    assert plan.first_read is True
    assert plan.truncated is False


def test_read_plan_catches_up_after_long_outage() -> None:
    plan = a.node_read_plan(
        node_uuid=NODE, panel_now=dt(12), last_ok_read_at=dt(4, 0, day=14), max_catchup_days=35
    )
    assert plan.dates == (date(2026, 9, 14), date(2026, 9, 15), date(2026, 9, 16), D)
    assert plan.catchup_dates == {date(2026, 9, 15), date(2026, 9, 16)}
    assert plan.truncated is False


def test_read_plan_truncates_deeper_than_catchup_limit() -> None:
    plan = a.node_read_plan(
        node_uuid=NODE, panel_now=dt(12), last_ok_read_at=dt(12, day=1), max_catchup_days=7
    )
    assert plan.dates[0] == date(2026, 9, 10)
    assert plan.truncated is True
    assert (plan.truncated_from, plan.truncated_to) == (date(2026, 9, 1), date(2026, 9, 9))


def test_batch_requests_makes_one_call_per_date() -> None:
    plans = [
        a.node_read_plan(node_uuid=NODE, panel_now=dt(12), last_ok_read_at=dt(11), max_catchup_days=35),
        a.node_read_plan(
            node_uuid=NODE_2, panel_now=dt(12), last_ok_read_at=dt(4, day=15), max_catchup_days=35
        ),
    ]
    requests = a.batch_requests(plans)
    assert [item.usage_date for item in requests] == [date(2026, 9, 15), date(2026, 9, 16), D]
    assert requests[0].node_uuids == (NODE_2,)
    assert requests[-1].node_uuids == (NODE, NODE_2)


def test_parse_usage_response_skips_garbage() -> None:
    parsed = a.parse_usage_response(
        {
            "nodes": [
                {
                    "uuid": NODE.upper(),
                    "users": [
                        {"id": "7", "totalBytes": "15"},
                        {"id": None},
                        {"id": 0, "totalBytes": 5},
                        {"id": 8, "totalBytes": "x"},
                        {"id": 9, "totalBytes": -4},
                        "junk",
                    ],
                },
                {"users": []},
                "junk",
            ]
        }
    )
    assert parsed == {
        NODE: (
            a.UsageRow(node_uuid=NODE, panel_user_id=7, total_bytes=15),
            a.UsageRow(node_uuid=NODE, panel_user_id=9, total_bytes=0),
        )
    }
    assert a.parse_usage_response(None) == {}
    assert a.parse_usage_response({"nodes": None}) == {}


# ---------------------------------------------------------------- Δ1–Δ8


def test_delta1_growth_and_delta2_same_value() -> None:
    grew = step(existing(1000), 2500)
    assert (grew.rule, grew.delta_bytes) == (a.RULE_D1, 1500)
    assert grew.counter.total_bytes == 2500
    assert grew.counter.accounted_bytes == 2500
    same = step(existing(2500), 2500)
    assert (same.rule, same.delta_bytes) == (a.RULE_D2, 0)


def test_delta3_new_row_counts_everything_and_marks_catchup() -> None:
    fresh = step(a.CounterState(), 700)
    assert (fresh.rule, fresh.delta_bytes) == (a.RULE_D3, 700)
    assert fresh.flags == a.DeltaFlag.NEW_ROW
    assert fresh.counter.baseline_bytes == 0
    after_gap = step(a.CounterState(), 700, catchup=True)
    assert after_gap.flags == a.DeltaFlag.NEW_ROW | a.DeltaFlag.AFTER_GAP


def test_delta4_baseline_only_on_first_read_of_old_node() -> None:
    old_node = ctx(first_read_at=None, node_created_at=dt(12, day=1), prev_cycle_read_at=dt(11, 50))
    baseline = step(a.CounterState(), 5_000, node=old_node)
    assert (baseline.rule, baseline.delta_bytes) == (a.RULE_D4, 0)
    assert baseline.counter.baseline_bytes == 5_000
    first_cycle = ctx(
        first_read_at=None, node_created_at=dt(11, 55), prev_cycle_read_at=None, is_first_module_cycle=True
    )
    assert step(a.CounterState(), 5_000, node=first_cycle).rule == a.RULE_D4


def test_delta5_new_node_counts_all_its_bytes() -> None:
    new_node = ctx(first_read_at=None, node_created_at=dt(11, 55), prev_cycle_read_at=dt(11, 50))
    outcome = step(a.CounterState(), 900, node=new_node)
    assert (outcome.rule, outcome.delta_bytes) == (a.RULE_D5, 900)
    assert outcome.counter.baseline_bytes == 0


def test_delta6_regress_gives_zero_and_new_baseline() -> None:
    outcome = step(existing(9_000), 4_000)
    assert (outcome.rule, outcome.delta_bytes) == (a.RULE_D6, 0)
    assert outcome.anomaly == a.ANOMALY_COUNTER_REGRESS
    assert outcome.counter.total_bytes == 4_000
    assert outcome.counter.carry_bytes == 5_000
    assert outcome.counter.accounted_bytes == 9_000


def test_delta7_and_delta8_mark_key_vanished() -> None:
    vanished = step(existing(3_000), None, user_exists=True)
    assert (vanished.rule, vanished.delta_bytes) == (a.RULE_D7, 0)
    assert vanished.counter.vanished_at == dt(12)
    deleted = step(existing(3_000), None, user_exists=False)
    assert deleted.rule == a.RULE_D8
    idle = step(vanished.counter, None)
    assert (idle.rule, idle.delta_bytes) == (a.RULE_IDLE, 0)
    assert (
        a.classify_counter(current_bytes=None, state=a.CounterState(), node=ctx(), read_at=dt(12)).rule
        == a.RULE_IDLE
    )


def test_delta7_prime_after_truncate_counts_reappeared_value() -> None:
    vanished = step(existing(3_000), None).counter
    back = step(vanished, 400)
    assert (back.rule, back.delta_bytes) == (a.RULE_D7_PRIME, 400)
    assert back.flags == a.DeltaFlag.REAPPEARED
    assert back.counter.vanished_at is None
    assert back.counter.total_bytes == 400
    assert back.counter.carry_bytes == 3_000
    assert back.counter.accounted_bytes == 3_400


def test_key_invariant_catches_broken_bookkeeping() -> None:
    broken = a.CounterState(total_bytes=10, accounted_bytes=20, exists=True)
    assert broken.invariant_ok is False
    assert broken.invariant_error == 10


def test_anomaly_detectors() -> None:
    assert a.detect_rows_vanished(live_positive_keys=3, vanished_now=3) is True
    assert a.detect_rows_vanished(live_positive_keys=3, vanished_now=2) is False
    assert a.detect_rows_vanished(live_positive_keys=0, vanished_now=0) is False
    assert a.detect_history_rollback(regress_keys=0, live_keys=100) is False
    assert a.detect_history_rollback(regress_keys=4, live_keys=100) is False
    assert a.detect_history_rollback(regress_keys=5, live_keys=10) is True
    assert a.detect_history_rollback(regress_keys=9, live_keys=100) is False
    assert a.detect_history_rollback(regress_keys=10, live_keys=100) is True


def test_sanity_limit_flags_only_impossible_deltas() -> None:
    assert a.sanity_limit_bytes(interval_seconds=600, max_mbps=2000) == 150_000_000_000
    assert a.is_implausible(10_000_000_000, interval_seconds=600, max_mbps=2000) is False
    assert a.is_implausible(200_000_000_000, interval_seconds=600, max_mbps=2000) is True
    assert a.is_implausible(10, interval_seconds=0, max_mbps=2000) is False  # a zero interval proves nothing


def test_rebaseline_keeps_the_invariant_and_counts_nothing() -> None:
    grown = a.rebaseline_counter(current_bytes=5_000, state=existing(1_000))
    assert grown.delta_bytes == 0 and grown.counter.baseline_bytes == 4_000 and grown.counter.invariant_ok
    regressed = a.rebaseline_counter(current_bytes=300, state=existing(1_000))
    assert (
        regressed.delta_bytes == 0 and regressed.counter.carry_bytes == 700 and regressed.counter.invariant_ok
    )
    gone = a.rebaseline_counter(current_bytes=200, state=existing(1_000, vanished_at=dt(11)))
    assert gone.counter.vanished_at is None and gone.counter.invariant_ok
    after = step(grown.counter, 5_600)
    assert (after.rule, after.delta_bytes) == (a.RULE_D1, 600)


# ---------------------------------------------------------------- one (node, date) pair


def test_diff_pair_covers_keys_from_rows_and_counters() -> None:
    prior = {
        a.CounterKey(NODE, D, 1): existing(100),
        a.CounterKey(NODE, D, 2): existing(200),
        a.CounterKey(NODE_2, D, 3): existing(300),  # another node: ignored
    }
    rows = [a.UsageRow(NODE, 1, 150), a.UsageRow(NODE, 4, 40)]
    out = a.diff_pair(node=ctx(first_read_at=dt(1)), usage_date=D, rows=rows, counters=prior, read_at=dt(12))
    rules = {key.panel_user_id: o.rule for key, o in out.outcomes.items()}
    assert rules == {1: a.RULE_D1, 2: a.RULE_D7, 4: a.RULE_D3}
    assert out.anomalies == frozenset()


def test_diff_pair_detects_truncate_and_rollback() -> None:
    prior = {a.CounterKey(NODE, D, pid): existing(1000) for pid in range(1, 11)}
    truncated = a.diff_pair(
        node=ctx(first_read_at=dt(1)), usage_date=D, rows=[], counters=prior, read_at=dt(12)
    )
    assert a.ANOMALY_ROWS_VANISHED in truncated.anomalies
    rows = [a.UsageRow(NODE, pid, 10) for pid in range(1, 11)]
    rolled = a.diff_pair(
        node=ctx(first_read_at=dt(1)), usage_date=D, rows=rows, counters=prior, read_at=dt(12)
    )
    assert {a.ANOMALY_HISTORY_ROLLBACK, a.ANOMALY_COUNTER_REGRESS} <= rolled.anomalies
    one = [a.UsageRow(NODE, pid, 10 if pid == 1 else 1000) for pid in range(1, 11)]
    single = a.diff_pair(
        node=ctx(first_read_at=dt(1)), usage_date=D, rows=one, counters=prior, read_at=dt(12)
    )
    assert single.anomalies == frozenset({a.ANOMALY_COUNTER_REGRESS})


def test_diff_pair_first_read_rebaselines_existing_keys() -> None:
    """Module re-enabled / bot DB restored: the first read counts nothing, even for known keys."""
    prior = {a.CounterKey(NODE, D, 1): existing(100)}
    rows = [a.UsageRow(NODE, 1, 5_000), a.UsageRow(NODE, 2, 7_000)]
    out = a.diff_pair(node=ctx(first_read_at=None), usage_date=D, rows=rows, counters=prior, read_at=dt(12))
    assert {k.panel_user_id: (o.rule, o.delta_bytes) for k, o in out.outcomes.items()} == {
        1: (a.RULE_REBASELINE, 0),
        2: (a.RULE_D4, 0),
    }


def test_diff_pair_known_users_split_d7_d8() -> None:
    prior = {a.CounterKey(NODE, D, 1): existing(100), a.CounterKey(NODE, D, 2): existing(100)}
    out = a.diff_pair(
        node=ctx(first_read_at=dt(1)), usage_date=D, rows=[], counters=prior, read_at=dt(12), known_users={1}
    )
    assert {k.panel_user_id: o.rule for k, o in out.outcomes.items()} == {1: a.RULE_D7, 2: a.RULE_D8}


# ---------------------------------------------------------------- interval


def test_delta_interval_default_and_catchup_clamp() -> None:
    normal = a.delta_interval(read_at=dt(12, 10, 5), usage_date=D, last_ok_read_at=dt(12, 0, 5))
    assert (normal.start, normal.end, normal.flags) == (dt(12, 0, 5), dt(12, 10, 5), a.DeltaFlag.NONE)
    assert normal.seconds == 600
    catchup = a.delta_interval(
        read_at=dt(12), usage_date=date(2026, 9, 15), last_ok_read_at=dt(4, day=14), catchup=True
    )
    assert catchup.start == dt(0, 0, day=15)
    assert catchup.end == dt(0, 0, day=16)
    assert catchup.flags & a.DeltaFlag.AFTER_GAP


def test_delta_interval_pins_start_after_node_gap_and_panel_outage() -> None:
    pinned = a.delta_interval(
        read_at=dt(12),
        usage_date=D,
        last_ok_read_at=dt(11, 50),
        gap_anchor_read_at=dt(6),
        node_reconnected=True,
    )
    assert pinned.start == dt(6)
    assert pinned.flags & a.DeltaFlag.NODE_RECONNECTED
    panel = a.delta_interval(
        read_at=dt(12), usage_date=D, last_ok_read_at=dt(11, 50), gap_anchor_read_at=dt(10), panel_gap=True
    )
    assert panel.start == dt(10)
    assert panel.flags & a.DeltaFlag.AFTER_GAP
    unpinned = a.delta_interval(read_at=dt(12), usage_date=D, last_ok_read_at=dt(11, 50), panel_gap=True)
    assert unpinned.start == dt(11, 50)
    assert unpinned.flags & a.DeltaFlag.AFTER_GAP
    first = a.delta_interval(read_at=dt(12), usage_date=D, last_ok_read_at=None)
    assert first.start == dt(0, 0)


def test_delta_interval_after_downtime_is_cut_by_the_utc_day_of_the_date() -> None:
    """№35: the bot was down from the 15th 10:00 to the 17th 12:00 — dates are not smeared over it."""
    last_read = dt(10, day=15)
    read_at = dt(12, day=17)
    old = a.delta_interval(
        read_at=read_at, usage_date=date(2026, 9, 15), last_ok_read_at=last_read, panel_gap=True
    )
    assert (old.start, old.end) == (last_read, dt(0, day=16))
    new = a.delta_interval(read_at=read_at, usage_date=D, last_ok_read_at=last_read, write_lag_seconds=LAG)
    assert (new.start, new.end) == (dt(23, 58, day=16), read_at)
    boundary = dt(21, day=15)  # 00:00 MSK of the 16th
    periods = (
        period(1, boundary - timedelta(days=30), boundary),
        period(2, boundary, boundary + timedelta(days=30)),
    )
    result = alloc(new.start, new.end, 1_000, periods=periods)
    assert [(item.period_id, item.bytes_value) for item in result.allocations] == [(2, 1_000)]
    around = a.delta_interval(read_at=dt(0, 5, day=18), usage_date=D, last_ok_read_at=dt(23, 55))
    assert (around.start, around.end) == (dt(23, 55), dt(0, day=18))
    nxt = a.delta_interval(read_at=dt(0, 5, day=18), usage_date=date(2026, 9, 18), last_ok_read_at=dt(23, 55))
    assert (nxt.start, nxt.end) == (dt(23, 58), dt(0, 5, day=18))
    pinned = a.delta_interval(
        read_at=read_at,
        usage_date=D,
        last_ok_read_at=dt(11, 50, day=17),
        gap_anchor_read_at=last_read,
        node_reconnected=True,
    )
    assert pinned.start == last_read


def test_gap_tail_keeps_pinning_until_backlog_is_flushed() -> None:
    closed = dt(12)
    assert a.gap_tail_active(gap_closed_at=None, now=closed, tail_cycles=2) is False
    assert a.gap_tail_active(gap_closed_at=closed, now=closed, tail_cycles=2) is True
    assert a.gap_tail_active(gap_closed_at=closed, now=dt(12, 1), tail_cycles=0) is True
    assert a.gap_tail_active(gap_closed_at=closed, now=dt(12, 5), tail_cycles=0) is False


# ---------------------------------------------------------------- membership


def test_membership_split_keeps_bytes_outside_the_group() -> None:
    memberships = [a.NodeMembership(group_id=1, counted_from=dt(10, 20), counted_to=dt(10, 40))]
    segments = a.split_by_membership(
        interval=interval(dt(10), dt(11)),
        total_bytes=3600,
        memberships=memberships,
        snap_seconds=SNAP,
        write_lag_seconds=LAG,
    )
    assert [item.group_id for item in segments] == [None, 1, None]
    assert [item.bytes_value for item in segments] == [
        1320,
        1200,
        1080,
    ]  # cuts moved by the lag: 10:22, 10:42
    assert all(item.estimated for item in segments)


def test_membership_without_rows_gives_single_segment_outside_groups() -> None:
    segments = a.split_by_membership(
        interval=interval(dt(10), dt(11)), total_bytes=500, snap_seconds=SNAP, write_lag_seconds=LAG
    )
    assert [(s.group_id, s.bytes_value) for s in segments] == [(None, 500)]


def test_node_returned_to_group_does_not_get_outside_traffic() -> None:
    memberships = [
        a.NodeMembership(group_id=1, counted_from=dt(8), counted_to=dt(9)),
        a.NodeMembership(group_id=1, counted_from=dt(11)),
    ]
    assert a.group_at(memberships, dt(8, 30)) == 1
    assert a.group_at(memberships, dt(10)) is None
    assert a.group_at(memberships, dt(12)) == 1


def test_draft_membership_with_empty_span_counts_nothing() -> None:
    """A pending node has ``counted_to = counted_from`` (never "since the beginning of time")."""
    draft = [a.NodeMembership(group_id=1, counted_from=dt(10), counted_to=dt(10))]
    assert a.group_at(draft, dt(9)) is None
    assert a.group_at(draft, dt(10)) is None


# ---------------------------------------------------------------- periods


def alloc(
    start: datetime,
    end: datetime,
    total_bytes: int,
    periods: tuple[a.PeriodSpan, ...] = (),
    memberships: tuple[a.NodeMembership, ...] = (a.NodeMembership(group_id=1),),
    **kwargs: object,
) -> a.AllocationResult:
    return a.allocate(
        interval=interval(start, end),
        total_bytes=total_bytes,
        periods=periods,
        memberships=memberships,
        snap_seconds=SNAP,
        write_lag_seconds=LAG,
        **kwargs,  # type: ignore[arg-type]
    )


def two_periods() -> tuple[a.PeriodSpan, a.PeriodSpan]:
    boundary = dt(21, 0, 0)
    old = period(1, dt(21, 0, day=10), boundary, ended_at=boundary, state="closed", anchor_at=dt(21, day=10))
    new = period(2, boundary, boundary + timedelta(days=30), anchor_at=dt(21, day=10))
    return old, new


def test_snap_keeps_whole_interval_in_the_old_period() -> None:
    result = alloc(dt(20, 50, 5), dt(21, 0, 5), 300_000_000, periods=two_periods())
    assert [(item.period_id, item.bytes_value) for item in result.allocations] == [(1, 300_000_000)]
    assert result.allocations[0].estimated is False
    assert result.end_estimated_period_ids == frozenset()


def test_snap_at_the_start_puts_everything_into_the_new_period() -> None:
    result = alloc(dt(20, 59, 30), dt(21, 9, 30), 120_000_000, periods=two_periods())
    assert [(item.period_id, item.bytes_value) for item in result.allocations] == [(2, 120_000_000)]


def test_missed_cycle_splits_proportionally_with_write_lag() -> None:
    """3600 s through the 21:00 boundary → 1.315 / 2.285 GB (owner's example 3)."""
    result = alloc(dt(20, 40, 5), dt(21, 40, 5), 3_600_000_000, periods=two_periods())
    assert {item.period_id: item.bytes_value for item in result.allocations} == {
        1: 1_315_000_000,
        2: 2_285_000_000,
    }
    assert all(item.estimated for item in result.allocations)
    assert result.end_estimated_period_ids == {1}
    assert result.start_estimated_period_ids == {2}
    assert result.branches == (a.BRANCH_BOUNDARY,)


def test_reanchor_splits_the_current_interval() -> None:
    anchor = dt(12, 3)
    trial = period(
        1, dt(12, day=10), anchor, ended_at=anchor, state="closed", is_trial=True, anchor_at=dt(12, day=10)
    )
    paid = period(2, anchor, anchor + timedelta(days=30), series_first=True, anchor_at=anchor)
    result = alloc(dt(12, 0, 5), dt(12, 10, 5), 120_000_000, periods=(trial, paid))
    assert {item.period_id: item.bytes_value for item in result.allocations} == {1: 59_000_000, 2: 61_000_000}


def test_new_series_head_goes_entirely_into_the_first_period() -> None:
    anchor = dt(12, 3)
    paid = period(2, anchor, anchor + timedelta(days=30), series_first=True, anchor_at=anchor)
    result = alloc(dt(12, 0, 5), dt(12, 10, 5), 80_000_000, periods=(paid,))
    assert [(item.period_id, item.bytes_value) for item in result.allocations] == [(2, 80_000_000)]
    assert result.branches == (a.BRANCH_SERIES_FIRST,)


def test_series_tail_after_the_end_stays_in_the_last_period() -> None:
    ended = dt(12, 5)
    last = period(1, dt(12, day=10), ended, ended_at=ended, state="closed", anchor_at=dt(12, day=10))
    result = alloc(dt(12, 0, 5), dt(12, 10, 5), 10_000, periods=(last,))
    assert [(item.period_id, item.bytes_value) for item in result.allocations] == [(1, 10_000)]
    assert result.branches == (a.BRANCH_SERIES_TAIL,)


def test_interval_without_periods_is_kept_with_null_period() -> None:
    result = alloc(dt(12), dt(12, 10), 4_096, periods=())
    assert result.allocations == (a.Allocation(period_id=None, group_id=1, bytes_value=4_096),)
    assert result.branches == (a.BRANCH_NO_PERIOD,)


def test_deferred_period_never_ends() -> None:
    deferred = period(1, dt(12, day=10), dt(12, day=16), state="deferred", anchor_at=dt(12, day=10))
    assert deferred.end_at is None
    result = alloc(dt(12), dt(12, 10), 1_000, periods=(deferred,))
    assert result.allocations[0].period_id == 1
    assert result.branches == (a.BRANCH_SINGLE,)


def test_after_block_marks_bytes_consumed_after_the_patch() -> None:
    open_period = period(1, dt(10), dt(10) + timedelta(days=30), anchor_at=dt(10))
    after = alloc(dt(12), dt(12, 10), 5_000, periods=(open_period,), blocked_since={1: dt(11, 30)})
    assert after.allocations[0].after_block is True
    assert after.segments[0].after_block is True
    before = alloc(dt(12), dt(12, 10), 5_000, periods=(open_period,), blocked_since={1: dt(12, 30)})
    assert before.allocations[0].after_block is False


def test_group_and_period_boundaries_split_together_without_losing_bytes() -> None:
    old, new = two_periods()
    memberships = (a.NodeMembership(group_id=1, counted_from=dt(20, 45)),)
    result = alloc(dt(20, 40, 5), dt(21, 40, 5), 1_234_567, periods=(old, new), memberships=memberships)
    assert result.flags & a.DeltaFlag.GROUP_SPLIT
    assert result.total_bytes == 1_234_567
    assert {(item.period_id, item.group_id) for item in result.allocations} == {(1, None), (1, 1), (2, 1)}


def test_gap_estimated_bytes_counted_only_for_gap_deltas() -> None:
    result = alloc(dt(20, 40, 5), dt(21, 40, 5), 3_600_000_000, periods=two_periods())
    assert result.gap_estimated_by_period(a.DeltaFlag.NONE) == {}
    assert result.gap_estimated_by_period(a.DeltaFlag.NODE_RECONNECTED) == {
        1: 1_315_000_000,
        2: 2_285_000_000,
    }


@pytest.mark.parametrize("total", [1, 7, 999, 1_000_003, 2**40 + 1])
def test_allocation_never_loses_or_invents_bytes(total: int) -> None:
    result = alloc(
        dt(20, 40, 5),
        dt(21, 40, 5),
        total,
        periods=two_periods(),
        memberships=(a.NodeMembership(group_id=1, counted_from=dt(20, 50), counted_to=dt(21, 20)),),
    )
    assert result.total_bytes == total


# ---------------------------------------------------------------- buckets


def test_spread_over_hours_is_proportional_and_lossless() -> None:
    hours = a.spread_over_hours(interval(dt(10, 30), dt(12, 30)), 4_000)
    assert hours == {dt(10): 1_000, dt(11): 2_000, dt(12): 1_000}
    assert a.spread_over_hours(interval(dt(10, 30), dt(10, 30)), 7) == {dt(10): 7}
    assert a.spread_over_hours(interval(dt(11), dt(11)), 7) == {dt(10): 7}  # read at 11:00 → the hour before
    assert a.spread_over_hours(interval(dt(10), dt(11)), 0) == {}
    odd = a.spread_over_hours(interval(dt(10, 59, 59), dt(13, 0, 1)), 1_000_003)
    assert sum(odd.values()) == 1_000_003


def test_usage_batch_merges_rows_and_puts_msk_days() -> None:
    """A delta through 21:00 UTC (= 00:00 MSK) lands on two Moscow days and on two periods."""
    batch = a.UsageBatch()
    result = alloc(
        dt(20, 40, 5), dt(21, 40, 5), 3_600_000_000, periods=two_periods(), blocked_since={1: dt(20)}
    )
    batch.add(7, result, delta_flags=a.DeltaFlag.NODE_RECONNECTED)
    batch.add(7, alloc(dt(21, 40, 5), dt(21, 50, 5), 600, periods=two_periods()))
    assert batch.period_usage[(1, 1)].used_bytes == 1_315_000_000
    assert batch.period_usage[(2, 1)].used_bytes == 2_285_000_000 + 600
    assert batch.period_usage[(1, 1)].after_block_bytes == 1_315_000_000
    assert batch.period_usage[(2, 1)].estimated_bytes == 2_285_000_000
    assert batch.period_usage[(2, 1)].last_delta_at == dt(21, 50, 5)
    assert batch.gap_estimated == {(1, 1): 1_315_000_000, (2, 1): 2_285_000_000}
    assert sum(batch.hourly.values()) == 3_600_000_600
    days = {key[2]: value.bytes_value for key, value in batch.daily.items()}
    assert set(days) == {D, date(2026, 9, 18)}
    assert days[D] == batch.hourly[(7, 1, dt(20))]
    assert sum(days.values()) == 3_600_000_600
    assert not batch.empty


def test_usage_batch_ignores_bytes_outside_groups_and_counts_outside_periods() -> None:
    batch = a.UsageBatch()
    batch.add(7, alloc(dt(12), dt(12, 10), 500, memberships=()))
    assert batch.empty
    batch.add(7, alloc(dt(12), dt(12, 10), 400, periods=()))
    assert batch.outside_periods == 400
    assert batch.period_usage == {}
    assert sum(batch.hourly.values()) == 400  # the buckets still keep it for a later re-sum


def test_usage_between_resums_days_and_splits_partial_days() -> None:
    day1, day2, day3 = date(2026, 9, 16), date(2026, 9, 17), date(2026, 9, 18)
    daily = {day1: 2_400, day2: 4_800, day3: 9_600}
    hourly = {dt(9): 360, dt(10): 720}  # 12:00 and 13:00 MSK of the 17th
    since = dt(0, day=15)
    whole = a.usage_between(dt(21, day=15), dt(21, day=18), daily=daily, hourly=hourly, hourly_since=since)
    assert whole == 2_400 + 4_800 + 9_600
    exact = a.usage_between(dt(9, 30), dt(21), daily=daily, hourly=hourly, hourly_since=since)
    assert exact == 180 + 720  # half of 12:00 MSK and the whole 13:00 hour
    old = a.usage_between(dt(9), dt(21), daily=daily, hourly=hourly, hourly_since=dt(12))
    assert old == 4_800 // 2  # hours already purged: proportional share of the day
    assert a.usage_between(dt(10), dt(10), daily=daily, hourly=hourly, hourly_since=since) == 0


# ---------------------------------------------------------------- group states, checks, retention


def test_group_state_incomplete_does_not_block_new_blocks() -> None:
    states = a.build_group_states(
        group_nodes={1: [NODE, NODE_2]},
        coverage=[a.CoverageEntry(NODE, D, False, 0), a.CoverageEntry(NODE_2, D, True, 0)],
        disconnected_node_uuids=[NODE_2],
    )
    state = states[1]
    assert a.reason_for_node(a.INCOMPLETE_READ_ERROR, NODE) in state.incomplete
    assert a.reason_for_node(a.INCOMPLETE_NODE_DISCONNECTED, NODE_2) in state.incomplete
    assert a.reason_for_node(a.INCOMPLETE_READ_ERROR, NODE_2) not in state.incomplete
    assert state.blocks_new_blocks is False


def test_group_state_anomaly_blocks_new_blocks() -> None:
    states = a.build_group_states(
        group_nodes={1: [NODE], 2: [NODE_2]},
        node_anomalies={NODE: [a.ANOMALY_COUNTER_REGRESS]},
        suspended_group_ids=[2],
    )
    assert states[1].blocks_new_blocks is True
    assert a.reason_kind(states[1].anomaly[0]) == a.ANOMALY_COUNTER_REGRESS
    assert states[2].anomaly == (a.ANOMALY_INVARIANT,)
    assert states[1].as_dict() == {"incomplete": [], "anomaly": [f"counter_regress:{NODE}"]}


def test_group_state_orphaned_wins_over_disconnected_and_global_reasons() -> None:
    states = a.build_group_states(
        group_nodes={1: [NODE]},
        disconnected_node_uuids=[NODE],
        orphaned_node_uuids=[NODE],
        catchup_limited_node_uuids=[NODE],
        global_incomplete=[a.INCOMPLETE_CYCLE_PARTIAL],
        global_anomalies=[a.ANOMALY_PANEL_SETTINGS],
    )
    state = states[1]
    assert a.INCOMPLETE_CYCLE_PARTIAL in state.incomplete
    assert a.reason_for_node(a.INCOMPLETE_NODE_ORPHANED, NODE) in state.incomplete
    assert a.reason_for_node(a.INCOMPLETE_NODE_DISCONNECTED, NODE) not in state.incomplete
    assert a.reason_for_node(a.INCOMPLETE_CATCHUP_LIMIT, NODE) in state.incomplete
    assert state.anomaly == (a.ANOMALY_PANEL_SETTINGS,)


def test_history_gap_alert_threshold() -> None:
    assert a.history_gap_ratio(users_total_bytes=10, node_total_bytes=0) is None
    assert a.history_gap_alert(a.history_gap_ratio(users_total_bytes=94, node_total_bytes=100)) is True
    assert a.history_gap_alert(a.history_gap_ratio(users_total_bytes=99, node_total_bytes=100)) is False
    assert a.history_gap_alert(None) is False


def test_counters_retention_keeps_last_read_state_of_the_node() -> None:
    assert a.counters_retention_cutoff(today_utc=D, last_ok_read_date=None) == date(2026, 9, 14)
    assert a.counters_retention_cutoff(today_utc=D, last_ok_read_date=D) == date(2026, 9, 14)
    assert a.counters_retention_cutoff(today_utc=D, last_ok_read_date=date(2026, 8, 10)) == date(2026, 8, 9)


def test_retention_cutoffs() -> None:
    cut = a.retention_cutoffs(dt(0, 40))  # 03:40 MSK
    assert cut.hourly_before == dt(0) - timedelta(hours=72)
    assert cut.daily_before == D - timedelta(days=70)
    assert cut.closed_periods_before == dt(0, 40) - timedelta(days=396)


def test_params_from_values_are_clamped() -> None:
    params = a.AccountingParams.from_values(
        {"boundary_snap_s": 9999, "write_lag_s": -1, "max_catchup_days": "x"}
    )
    assert (params.snap_seconds, params.write_lag_seconds, params.max_catchup_days) == (300, 0, 35)
    assert a.AccountingParams.from_values({}) == a.AccountingParams()


# ---------------------------------------------------------------- a whole cycle


def _node(
    *, last: datetime | None = dt(12), first: datetime | None = dt(0, day=10), **kwargs: object
) -> a.NodeCycleInput:
    plan = a.node_read_plan(node_uuid=NODE, panel_now=dt(12, 10), last_ok_read_at=last, max_catchup_days=35)
    return a.NodeCycleInput(
        plan=plan,
        context=ctx(first_read_at=first),
        last_ok_read_at=last,
        memberships=(a.NodeMembership(group_id=1, counted_from=dt(0, day=1)),),
        **kwargs,  # type: ignore[arg-type]
    )


SUBJECT = a.SubjectSpans(subscription_id=70, periods=(period(5, dt(0, day=10), dt(0, day=30 - 0)),))


def test_account_cycle_counts_only_changed_keys_and_moves_marks() -> None:
    prev = date(2026, 9, 16)
    counters = {
        a.CounterKey(NODE, D, PID): existing(1_000),
        a.CounterKey(NODE, D, 502): existing(9_000),
        a.CounterKey(NODE, prev, PID): existing(50),
    }
    readings = {
        (NODE, prev): [a.UsageRow(NODE, PID, 50)],
        (NODE, D): [a.UsageRow(NODE, PID, 1_600), a.UsageRow(NODE, 502, 9_000), a.UsageRow(NODE, 999, 70)],
    }
    out = a.account_cycle(
        read_at=dt(12, 10), nodes=[_node()], readings=readings, counters=counters, subjects={PID: SUBJECT}
    )
    assert {(w.key.panel_user_id, w.key.usage_date, w.insert) for w in out.counters} == {
        (PID, D, False),
        (999, D, True),
    }
    assert out.usage.period_usage[(5, 1)].used_bytes == 600
    assert out.usage.period_usage[(5, 1)].last_delta_at == dt(12, 10)
    assert out.unmatched_users == {999}
    assert out.unmatched_bytes == 70
    assert [(m.node_uuid, m.last_ok_read_at, m.last_ok_read_date) for m in out.marks] == [
        (NODE, dt(12, 10), D)
    ]
    assert out.node_anomalies == {}
    assert out.rules[a.RULE_D1] == 1 and out.rules[a.RULE_D2] == 2 and out.rules[a.RULE_D3] == 1


def test_account_cycle_failed_date_keeps_marks_and_reports_coverage() -> None:
    readings = {(NODE, D): [a.UsageRow(NODE, PID, 1_600)]}  # D−1 missing → failed
    counters = {a.CounterKey(NODE, D, PID): existing(1_000)}
    out = a.account_cycle(
        read_at=dt(12, 10), nodes=[_node()], readings=readings, counters=counters, subjects={PID: SUBJECT}
    )
    assert out.marks == ()
    assert [(c.usage_date, c.ok) for c in out.coverage] == [(date(2026, 9, 16), False), (D, True)]
    assert out.usage.period_usage[(5, 1)].used_bytes == 600  # what was read is still counted


def test_account_cycle_first_read_counts_nothing() -> None:
    readings = {(NODE, date(2026, 9, 16)): [], (NODE, D): [a.UsageRow(NODE, PID, 10**9)]}
    out = a.account_cycle(
        read_at=dt(12, 10),
        nodes=[_node(last=None, first=None)],
        readings=readings,
        counters={},
        subjects={PID: SUBJECT},
    )
    assert out.usage.empty
    assert [w.state.baseline_bytes for w in out.counters] == [10**9]
    assert out.marks[0].first_read_at == dt(12, 10)


def test_account_cycle_flags_implausible_delta_and_incidents() -> None:
    counters = {a.CounterKey(NODE, D, pid): existing(10**6) for pid in range(1, 7)}
    counters[a.CounterKey(NODE, D, PID)] = existing(0)
    rows = [a.UsageRow(NODE, pid, 10) for pid in range(1, 7)] + [a.UsageRow(NODE, PID, 500 * 10**9)]
    readings = {(NODE, date(2026, 9, 16)): [], (NODE, D): rows}
    out = a.account_cycle(
        read_at=dt(12, 10), nodes=[_node()], readings=readings, counters=counters, subjects={PID: SUBJECT}
    )
    assert out.clamped == {(70, 1)}
    assert a.ANOMALY_HISTORY_ROLLBACK in out.node_anomalies[NODE]
    assert out.incidents == {NODE: frozenset({a.ANOMALY_HISTORY_ROLLBACK})}
    assert out.usage.period_usage[(5, 1)].used_bytes == 500 * 10**9  # flagged, not cut


def test_account_cycle_catchup_and_truncation() -> None:
    last = dt(4, day=14)
    plan = a.node_read_plan(node_uuid=NODE, panel_now=dt(12), last_ok_read_at=last, max_catchup_days=2)
    node = a.NodeCycleInput(
        plan=plan,
        context=ctx(first_read_at=dt(0, day=1)),
        last_ok_read_at=last,
        memberships=(a.NodeMembership(group_id=1),),
    )
    readings = {(NODE, d): [a.UsageRow(NODE, PID, 86_400)] for d in plan.dates}
    out = a.account_cycle(
        read_at=dt(12), nodes=[node], readings=readings, counters={}, subjects={PID: SUBJECT}
    )
    assert out.catchup_limited == {NODE}
    assert plan.catchup_dates == {date(2026, 9, 15), date(2026, 9, 16)}
    hourly_days = {hour.date() for (_, _, hour) in out.usage.hourly}
    assert {date(2026, 9, 15), date(2026, 9, 16)} <= hourly_days  # catch-up dates stay inside their UTC days
    assert sum(out.usage.hourly.values()) == 86_400 * len(plan.dates)
