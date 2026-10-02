"""LTE periods: the owner's four generative properties (05 §2.1.9), deterministic seeds.

1. the order of arrival does not change the result (it depends on ``t_eff`` only);
2. incremental processing (``process`` per event, replay from the start when an event is late) ≡ one
   ``simulate`` of the whole chain;
3. periods of one series never overlap and cover it (the only gap is a reopening after the end of the series);
4. E3 never fires inside the grace of a live series.

Plus accounting properties: the allocation never loses or invents bytes, the counter key invariant holds on
random sequences of panel readings.
"""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta

import pytest

from svbg.ext.lte import accounting as a
from svbg.ext.lte import periods as p
from svbg.ext.lte.model import MSK

PARAMS = p.PeriodParams()
SEEDS = list(range(25))


def _chain(seed: int) -> list[p.Event]:
    """A random but consistent chronology: ``old_end`` is always the previous ``new_end``."""
    rnd = random.Random(seed)
    at = datetime(2026, 1, 5, 12, tzinfo=MSK)
    coverage: datetime | None = None
    events: list[p.Event] = []
    for index in range(rnd.randint(3, 8)):
        kind = rnd.choice(["paid", "paid", "bonus", "admin", "trial", "close", "unfreeze"])
        at = at + timedelta(hours=rnd.randint(1, 1200))
        old_end = coverage
        if kind == "close":
            new_end = at
        else:
            base = coverage if coverage is not None and coverage > at else at
            new_end = base + timedelta(days=rnd.choice([7, 14, 30, 90]))
        paid_at = at - timedelta(hours=rnd.randint(0, 6)) if kind == "paid" and rnd.random() < 0.3 else None
        events.append(
            p.Event(
                occurred_at=at,
                kind=kind,  # type: ignore[arg-type]
                paid_at=paid_at,
                old_end=old_end,
                new_end=new_end,
                is_trial=True if kind == "trial" else None,
                is_new_row=index == 0,
                event_id=index + 1,
            )
        )
        coverage = new_end
    return events


def _compare(state: p.AnchorState) -> tuple[object, ...]:
    return (
        state.anchor_at,
        state.anchor_kind,
        state.series_open,
        state.series_closed_at,
        state.coverage_end,
        state.is_trial,
        state.period,
    )


def _spans(out: p.Outcome) -> list[tuple[datetime, datetime, str]]:
    return [(per.starts_at, per.ended_at or per.planned_end_at, per.state) for per in out.periods]


@pytest.mark.parametrize("seed", SEEDS)
def test_order_of_arrival_does_not_change_result(seed: int) -> None:
    events = _chain(seed)
    until = max(e.occurred_at for e in events) + timedelta(days=200)
    timely = p.simulate(p.AnchorState(), events, until=until, params=PARAMS)
    shuffled = list(events)
    random.Random(seed + 1000).shuffle(shuffled)
    late = p.simulate(p.AnchorState(), shuffled, until=until, params=PARAMS)
    assert _compare(late.state) == _compare(timely.state)
    assert _spans(late) == _spans(timely)


@pytest.mark.parametrize("seed", SEEDS)
def test_incremental_processing_equals_simulation(seed: int) -> None:
    """Events arrive in a shuffled order; each is processed at its arrival time, a late one (``process`` →
    ``None``) triggers a replay of everything arrived so far from the start. The end state equals a timely
    simulation of the whole chain."""
    events = _chain(seed)
    until = max(e.occurred_at for e in events) + timedelta(days=200)
    timely = p.simulate(p.AnchorState(), events, until=until, params=PARAMS)

    arrival = list(events)
    random.Random(seed + 7).shuffle(arrival)
    state = p.AnchorState()
    arrived: list[p.Event] = []
    now = datetime(2000, 1, 1, tzinfo=UTC)
    replays = 0
    for event in arrival:
        now = max(now, event.occurred_at)
        arrived.append(event)
        out = p.process(state, event, now=now, params=PARAMS)
        if out is None:
            replays += 1
            out = p.simulate(p.AnchorState(), arrived, until=now, params=PARAMS)
        state = out.state
    state = p.advance(state, until=until, params=PARAMS).state
    assert _compare(state) == _compare(timely.state)
    in_order = sorted(events, key=lambda e: p.input_moment(e, params=PARAMS))
    if arrival == in_order:
        assert replays == 0


@pytest.mark.parametrize("seed", SEEDS)
def test_periods_never_overlap_and_cover_the_series(seed: int) -> None:
    events = _chain(seed)
    until = max(e.occurred_at for e in events) + timedelta(days=200)
    out = p.simulate(p.AnchorState(), events, until=until, params=PARAMS)
    if out.state.anchor_at is None:
        return
    chain = [per for per in out.periods if per.anchor_at == out.state.anchor_at]
    assert chain, out.periods
    assert chain[0].starts_at == out.state.anchor_at
    previous_end: datetime | None = None
    previous_cause: str | None = None
    for per in chain:
        end = per.ended_at or per.planned_end_at
        assert per.starts_at < end, per
        if previous_end is not None:
            assert per.starts_at >= previous_end, (per, previous_end)
            if per.starts_at > previous_end:
                assert previous_cause == "series_end", (per, previous_cause)
        previous_end, previous_cause = end, per.end_cause
    if out.state.period is not None:
        assert out.state.period.key == chain[-1].key
        assert out.state.period.planned_end_at > until or out.state.period.state == "deferred"


@pytest.mark.parametrize("seed", SEEDS)
def test_e3_never_fires_inside_grace(seed: int) -> None:
    state = p.AnchorState()
    for event in sorted(_chain(seed), key=lambda e: p.input_moment(e, params=PARAMS)):
        t_eff = p.input_moment(event, params=PARAMS)
        state = p.simulate(state, [], until=t_eff, params=PARAMS).state
        eff_old_end, _ = p.effective_old_end(state, event)
        grace = p.grace_for(state, event, params=PARAMS)
        alive = state.series_open
        out = p.apply(state, event, params=PARAMS)
        if "E3" in out.rules and eff_old_end is not None and alive:
            assert t_eff - eff_old_end > grace, (event, eff_old_end, grace)
        state = out.state


# ---------------------------------------------------------------- accounting properties


@pytest.mark.parametrize("seed", SEEDS)
def test_counter_invariant_on_random_readings(seed: int) -> None:
    """``baseline + accounted = total + carry`` after every step; accounted bytes never decrease."""
    rnd = random.Random(seed)
    node = a.NodeReadContext(node_uuid="n1", first_read_at=datetime(2026, 9, 17, tzinfo=UTC))
    state = a.CounterState()
    accounted = 0
    total_delta = 0
    for step in range(60):
        roll = rnd.random()
        if roll < 0.1:
            current: int | None = None
        elif roll < 0.2 and state.exists:
            current = rnd.randint(0, max(0, state.total_bytes))
        else:
            current = state.total_bytes + rnd.randint(0, 10**6)
        out = a.classify_counter(
            current_bytes=current,
            state=state,
            node=node,
            read_at=datetime(2026, 9, 17, 12, step % 60, tzinfo=UTC),
        )
        assert out.counter.invariant_ok, (step, out)
        assert out.counter.accounted_bytes >= accounted
        assert out.delta_bytes == out.counter.accounted_bytes - state.accounted_bytes
        total_delta += out.delta_bytes
        accounted = out.counter.accounted_bytes
        state = out.counter
    assert total_delta == state.accounted_bytes


@pytest.mark.parametrize("seed", SEEDS)
def test_allocation_and_buckets_are_lossless(seed: int) -> None:
    rnd = random.Random(seed)
    start = datetime(2026, 9, 17, 20, rnd.randint(0, 59), tzinfo=UTC)
    end = start + timedelta(seconds=rnd.randint(1, 7200))
    boundary = datetime(2026, 9, 17, 21, tzinfo=UTC)
    periods = (
        a.PeriodSpan(1, boundary - timedelta(days=30), boundary, ended_at=boundary, state="closed"),
        a.PeriodSpan(2, boundary, boundary + timedelta(days=30)),
    )
    total = rnd.randint(1, 10**12)
    membership = a.NodeMembership(group_id=1, counted_from=start + timedelta(seconds=rnd.randint(0, 900)))
    result = a.allocate(
        interval=a.Interval(start, end),
        total_bytes=total,
        periods=periods,
        memberships=(membership,),
        snap_seconds=60,
        write_lag_seconds=120,
    )
    assert result.total_bytes == total
    batch = a.UsageBatch()
    batch.add(7, result)
    group_bytes = sum(item.bytes_value for item in result.allocations if item.group_id is not None)
    assert sum(batch.hourly.values()) == group_bytes
    assert sum(v.bytes_value for v in batch.daily.values()) == group_bytes
    assert all(isinstance(key[2], date) for key in batch.daily)
