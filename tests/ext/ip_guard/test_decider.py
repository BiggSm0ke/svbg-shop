"""IP Guard decisions: the owner's decider tests ported as the specification (05 §2.2.1, §3.8 п.5).

Deviation (05 §2.2.8): the anomaly «additions» chain is one record whose members grow — the owner's
``append_to`` / ``addition_parent`` cases become «new members go to the active anomaly».
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from svbg.ext.ip_guard import decide as d
from svbg.ext.ip_guard.config import DecisionParams, Params
from svbg.ext.ip_guard.window import UserStats

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
P = DecisionParams()


def st(
    pid: int,
    w: int,
    live: int = 12,
    subnets: int | None = None,
    *,
    complete: bool = True,
    missing: tuple[str, ...] = (),
) -> UserStats:
    return UserStats(
        panel_user_id=pid,
        ip_count=w,
        live_ip_count=live,
        subnet_count=w if subnets is None else subnets,
        complete=complete,
        missing_nodes=missing,
    )


def db(now: datetime = NOW, **kwargs: Any) -> d.DbState:
    return d.DbState(now=now, **kwargs)


def run(
    stats: list[UserStats],
    memory: d.DeciderMemory,
    state: d.DbState | None = None,
    params: DecisionParams = P,
    **kw: Any,
) -> d.Plan:
    return d.decide({s.panel_user_id: s for s in stats}, memory, state or db(), params, **kw)


def kinds(plan: d.Plan) -> dict[int, str]:
    return {pid: draft.kind for pid, draft in plan.drafts.items()}


# ----------------------------------------------------------------------------------------- confirmation


def test_burst_then_no_live_gives_unconfirmed_not_block() -> None:
    plan = run([st(1, 26)], d.DeciderMemory())
    assert plan.blocks == ()
    assert plan.drafts == {}  # pending: nothing is sent
    plan = run([st(1, 26, live=0)], plan.memory)
    assert plan.blocks == ()
    assert kinds(plan) == {1: "unconfirmed"}


def test_two_confirmed_passes_block_without_warning() -> None:
    plan = run([st(1, 26)], d.DeciderMemory())
    plan = run([st(1, 26)], plan.memory)
    assert plan.blocks == (1,)
    assert plan.drafts == {}
    assert plan.anomaly is None


def test_drop_below_block_gives_single_warn() -> None:
    plan = run([st(1, 26)], d.DeciderMemory())
    plan = run([st(1, 22)], plan.memory)
    assert plan.blocks == ()
    assert kinds(plan) == {1: "warn"}
    assert plan.memory.consecutive[1] == 0


def test_below_warn_forgets_counters() -> None:
    plan = run([st(1, 26)], d.DeciderMemory())
    plan = run([st(1, 10)], plan.memory)
    assert 1 not in plan.memory.consecutive
    assert plan.drafts == {}


def test_incomplete_pass_does_not_confirm() -> None:
    plan = run([st(1, 26)], d.DeciderMemory())
    plan = run([st(1, 26, complete=False, missing=("n",))], plan.memory)
    assert plan.blocks == ()
    assert plan.memory.consecutive[1] == 1
    assert plan.drafts == {}
    plan = run([st(1, 26, complete=False, missing=("n",))], plan.memory)
    assert plan.drafts == {}
    plan = run([st(1, 26, complete=False, missing=("n",))], plan.memory)
    assert kinds(plan) == {1: "incomplete"}
    assert plan.drafts[1].missing_nodes == ("n",)
    plan = run([st(1, 26)], plan.memory)
    assert plan.blocks == (1,)


def test_incomplete_below_block_is_plain_warn() -> None:
    plan = run([st(1, 22, complete=False)], d.DeciderMemory())
    assert kinds(plan) == {1: "warn"}


def test_pool_cases() -> None:
    mem = d.DeciderMemory()
    plan = run([], mem)
    for _ in range(3):
        plan = run([st(1, 30, subnets=1), st(2, 26, subnets=4), st(3, 25, subnets=25)], mem)
        mem = plan.memory
    assert kinds(plan) == {1: "pool", 2: "pool"}
    assert 3 not in plan.drafts
    no_subnets = DecisionParams(min_subnets=0)
    plan = run([st(1, 30, subnets=1)], d.DeciderMemory(), params=no_subnets)
    plan = run([st(1, 30, subnets=1)], plan.memory, params=no_subnets)
    assert plan.blocks == (1,)


def test_pool_users_do_not_trigger_fuse() -> None:
    stats = [st(1, 30, subnets=2), st(2, 30, subnets=2), st(3, 30, subnets=2), st(4, 30)]
    plan = run(stats, d.DeciderMemory())
    plan = run(stats, plan.memory)
    assert plan.anomaly is None
    assert plan.blocks == (4,)


def test_cgnat_hot_without_live_does_not_trigger_per_run() -> None:
    stats = [st(pid, 30, live=3) for pid in (1, 2, 3, 4)] + [st(5, 30)]
    plan = run(stats, d.DeciderMemory())
    plan = run(stats, plan.memory)
    assert plan.anomaly is None
    assert plan.blocks == (5,)
    assert kinds(plan) == {1: "unconfirmed", 2: "unconfirmed", 3: "unconfirmed", 4: "unconfirmed"}


# ----------------------------------------------------------------------------------------------- fuses


def test_per_run_trigger_blocks_nobody_and_resets_counters() -> None:
    plan = run([st(pid, 30) for pid in (1, 2, 3, 4)], d.DeciderMemory(consecutive={1: 1, 2: 1}))
    assert plan.blocks == ()
    assert plan.anomaly is not None
    assert plan.anomaly.trigger == "per_run"
    assert plan.anomaly.create
    assert plan.anomaly.quarantine_until == NOW + timedelta(minutes=P.window_minutes)
    assert set(plan.anomaly.new_members) == {1, 2, 3, 4}
    assert all(plan.memory.consecutive[pid] == 0 for pid in (1, 2, 3, 4))
    assert plan.drafts == {}


def test_window_trigger_counts_recent_blocks_from_db() -> None:
    plan = run([st(9, 30)], d.DeciderMemory(consecutive={9: 1}), db(confirmed_recent=3))
    assert plan.blocks == ()
    assert plan.anomaly is not None and plan.anomaly.trigger == "window"


def test_per_hour_trigger() -> None:
    mem = d.DeciderMemory(consecutive={1: 1, 2: 1, 3: 1})
    plan = run([st(pid, 30) for pid in (1, 2, 3)], mem, db(blocks_last_hour=9))
    assert plan.blocks == ()
    assert plan.anomaly is not None and plan.anomaly.trigger == "per_hour"


def active_quarantine(**kwargs: Any) -> d.AnomalyRecord:
    base: dict[str, Any] = {
        "id": 1,
        "created_at": NOW - timedelta(minutes=5),
        "quarantine_until": NOW + timedelta(minutes=5),
        "members": frozenset({1, 2, 3, 4}),
    }
    base.update(kwargs)
    return d.AnomalyRecord(**base)


def test_quarantine_blocks_nobody_without_extension() -> None:
    plan = run([st(7, 30)], d.DeciderMemory(consecutive={7: 1}), db(quarantine=(active_quarantine(),)))
    assert plan.blocks == ()
    assert plan.quarantine_active
    assert plan.anomaly is not None
    assert plan.anomaly.trigger is None
    assert plan.anomaly.quarantine_until is None  # no prolongation without a trigger
    assert plan.anomaly.anomaly_id == 1
    assert plan.anomaly.new_members == (7,)
    assert plan.memory.consecutive[7] == 0


def test_waves_never_block_during_quarantine_across_restart() -> None:
    record = active_quarantine()
    mem = d.DeciderMemory()
    for wave in ([5, 6, 7], [8, 9], [10]):
        for _ in range(3):
            plan = run([st(pid, 30) for pid in wave], mem, db(quarantine=(record,), confirmed_recent=0))
            assert plan.blocks == ()
            mem = plan.memory
        mem = d.DeciderMemory()  # «restart»: empty memory, the quarantine comes from the database


def test_trigger_during_quarantine_extends_up_to_ceiling() -> None:
    record = active_quarantine(created_at=NOW - timedelta(minutes=55))
    stats = [st(pid, 30) for pid in (1, 2, 3, 4)]
    plan = run(stats, d.DeciderMemory(), db(quarantine=(record,)))
    assert plan.anomaly is not None
    assert plan.anomaly.trigger == "per_run"
    assert not plan.anomaly.create
    assert plan.anomaly.quarantine_until == record.created_at + timedelta(minutes=60)
    assert plan.anomaly.anomaly_id == 1
    expired = active_quarantine(
        created_at=NOW - timedelta(minutes=61), quarantine_until=NOW - timedelta(seconds=1)
    )
    plan = run(stats, d.DeciderMemory(), db(quarantine=(expired,)))
    assert plan.anomaly is not None and plan.anomaly.create  # the ceiling passed and it ended → a new one


def test_new_members_join_the_active_anomaly() -> None:
    root = active_quarantine()
    plan = run([st(7, 30), st(8, 30)], d.DeciderMemory(consecutive={7: 1, 8: 1}), db(quarantine=(root,)))
    assert plan.anomaly is not None
    assert plan.anomaly.anomaly_id == 1
    assert plan.anomaly.new_members == (7, 8)
    member = active_quarantine(members=frozenset({1, 2, 3, 4, 7}))
    plan = run([st(7, 30), st(8, 30)], d.DeciderMemory(consecutive={7: 1, 8: 1}), db(quarantine=(member,)))
    assert plan.anomaly is not None and plan.anomaly.new_members == (8,)
    assert plan.drafts == {}  # members of the anomaly get no separate cards


def test_dismissed_not_counted_in_triggers() -> None:
    dismissed = {
        1: d.DismissInfo(until=NOW + timedelta(hours=1), by=42),
        2: d.DismissInfo(until=NOW + timedelta(hours=1)),
    }
    mem = d.DeciderMemory(consecutive=dict.fromkeys((1, 2, 3, 4), 1))
    plan = run([st(pid, 30) for pid in (1, 2, 3, 4)], mem, db(dismissed=dismissed))
    assert plan.anomaly is None
    assert set(plan.blocks) == {3, 4}
    assert kinds(plan) == {1: "dismissed", 2: "dismissed"}


def test_whitelist_grace_precondition_kinds() -> None:
    params = DecisionParams(whitelist=frozenset({1}))
    grace = {2: d.GraceInfo(unblocked_at=NOW - timedelta(minutes=5), until=NOW + timedelta(minutes=25))}
    mem = d.DeciderMemory(consecutive={1: 5, 2: 5, 3: 5})
    plan = run([st(1, 30), st(2, 30), st(3, 30)], mem, db(grace=grace), params=params)
    assert plan.blocks == (3,)
    assert kinds(plan) == {1: "whitelist", 2: "grace"}
    plan = run([st(3, 30)], mem, db(preconditions_ok=False, precondition_reason="автоблок выключен"))
    assert plan.blocks == ()
    assert kinds(plan) == {3: "precondition"}
    assert plan.drafts[3].precondition_reason == "автоблок выключен"


def test_already_blocked_gets_nothing() -> None:
    plan = run([st(1, 30)], d.DeciderMemory(consecutive={1: 5}), db(blocked=frozenset({1})))
    assert plan.blocks == ()
    assert plan.drafts == {}


def test_excluded_node_resets_consecutive() -> None:
    plan = run([st(1, 22)], d.DeciderMemory(consecutive={1: 1}, sustained={1: 3}), excluded_affected=[1])
    assert plan.memory.consecutive[1] == 0
    assert plan.memory.sustained[1] == 0


def test_no_ok_nodes_no_decisions() -> None:
    mem = d.DeciderMemory(consecutive={1: 1})
    plan = run([st(1, 30)], mem, any_ok=False)
    assert not plan.decided
    assert plan.memory.consecutive == {1: 1}


# ------------------------------------------------------------------------------------------- escalation


def test_sustained_escalation() -> None:
    mem = d.DeciderMemory()
    params = DecisionParams(sustained_block_checks=20)
    unconfirmed = 0
    plan = run([], mem)
    for i in range(1, 21):
        plan = run([st(1, 30, live=4, subnets=20)], mem, params=params)
        mem = plan.memory
        if i < 20:
            assert plan.blocks == ()
            unconfirmed += int(kinds(plan).get(1) == "unconfirmed")
    assert plan.blocks == (1,)
    assert unconfirmed == 19  # the cooldown cuts repeats at send time
    first = run([st(1, 30, live=4, subnets=20)], d.DeciderMemory(), params=params)
    assert first.drafts[1].sustained_left == 19

    off = DecisionParams(sustained_block_checks=0)
    mem = d.DeciderMemory()
    for _ in range(30):
        plan = run([st(1, 30, live=4, subnets=20)], mem, params=off)
        mem = plan.memory
        assert plan.blocks == ()


# --------------------------------------------------------------------------------- cooldowns and sending


def test_cooldown_rules() -> None:
    last = d.LastWarning(kind="warn", created_at=NOW - timedelta(minutes=30), ip_count=22)
    state = db(last_warnings={1: last})
    assert not d.cooldown_allows("warn", 1, state, P)
    assert d.cooldown_allows("pool", 1, state, P)  # escalation warn → block level
    escalated = db(last_warnings={1: d.LastWarning(kind="pool", created_at=NOW - timedelta(minutes=10))})
    assert not d.cooldown_allows("unconfirmed", 1, escalated, P)
    reset = db(last_warnings={1: last}, last_unblocked_at={1: NOW - timedelta(minutes=5)})
    assert d.cooldown_allows("warn", 1, reset, P)
    old = db(last_warnings={1: d.LastWarning(kind="warn", created_at=NOW - timedelta(minutes=361))})
    assert d.cooldown_allows("warn", 1, old, P)


def test_block_failed_ignores_cooldown_but_repeats_every_10_minutes() -> None:
    last = d.LastWarning(kind="pool", created_at=NOW - timedelta(minutes=1))
    assert d.cooldown_allows("block_failed", 1, db(last_warnings={1: last}), P)
    recent = db(last_block_failed_at={1: NOW - timedelta(minutes=5)})
    assert not d.cooldown_allows("block_failed", 1, recent, P)
    assert d.cooldown_allows("block_failed", 1, db(last_block_failed_at={1: NOW - timedelta(minutes=10)}), P)


def test_plan_warnings_block_failed_and_health_streak() -> None:
    mem = d.DeciderMemory(consecutive={1: 1})
    state = db()
    result = None
    for i in range(1, 6):
        plan = run([st(1, 30)], mem, state)
        assert plan.blocks == (1,)
        result = d.plan_warnings(plan, {1: d.BlockOutcome("failed", "панель: 500")}, state, P)
        mem = result.memory
        assert plan.memory.consecutive[1] >= 2  # consecutive is not reset
        assert [w.kind for w in result.individual] == ["block_failed"]
        assert result.individual[0].fail_reason == "панель: 500"
        if i < 5:
            assert result.failing_blocks == ()
    assert result is not None
    assert result.failing_blocks == ((1, "панель: 500", 5),)
    ok = d.plan_warnings(run([st(1, 30)], mem, state), {1: d.BlockOutcome("blocked")}, state, P)
    assert ok.individual == ()
    assert 1 not in ok.memory.block_failed_streak


def test_max_warnings_split_into_summary() -> None:
    stats = [st(pid, 20 + (pid % 4), 12) for pid in range(1, 9)]
    plan = run(stats, d.DeciderMemory())
    result = d.plan_warnings(plan, {}, db(), P)
    assert len(result.individual) == 5
    assert len(result.summary) == 3
    ips = [w.stats.ip_count for w in (*result.individual, *result.summary)]
    assert ips == sorted(ips, reverse=True)
    last = {pid: d.LastWarning(kind="warn", created_at=NOW) for pid in range(1, 9)}
    later = db(now=NOW + timedelta(minutes=1), last_warnings=last)
    again = d.plan_warnings(run(stats, plan.memory, later), {}, later, P)
    assert again.individual == ()
    assert again.summary == ()


def test_block_failed_sorted_first() -> None:
    plan = run([st(1, 26), *[st(pid, 24) for pid in range(2, 8)]], d.DeciderMemory(consecutive={1: 1}))
    result = d.plan_warnings(plan, {1: d.BlockOutcome("failed", "лок занят")}, db(), P)
    assert result.individual[0].kind == "block_failed"


def test_missing_outcome_counts_as_failed() -> None:
    plan = run([st(1, 30)], d.DeciderMemory(consecutive={1: 1}))
    result = d.plan_warnings(plan, {}, db(), P)
    assert [w.kind for w in result.individual] == ["block_failed"]
    assert result.individual[0].fail_reason == "блок не выполнен"


# ------------------------------------------------------------------------------------------- parameters


def test_params_from_config_clamps_cross_keys_and_defaults() -> None:
    cfg = {
        "IP_GUARD_BLOCK_IPS": 15,
        "IP_GUARD_WARN_IPS": 40,
        "IP_GUARD_MIN_SUBNETS": 99,
        "IP_GUARD_CONFIRM_LIVE_IPS": 77,
        "IP_GUARD_CONFIRM_CHECKS": 99,
        "IP_GUARD_IGNORE_CIDRS": ["9.9.9.0/24", "bad", "1.1.1.1"],
        "IP_GUARD_AUTO_BLOCK": "yes",  # wrong type → default (off)
    }
    p = Params.from_config(cfg, whitelist=frozenset({5}))
    assert p.decision.warn_ips == 15 and p.decision.block_ips == 15
    assert p.decision.min_subnets == 15 and p.decision.confirm_live_ips == 15
    assert p.decision.confirm_checks == 5
    assert p.decision.whitelist == frozenset({5})
    assert [str(n) for n in p.collector.ignore_cidrs] == ["9.9.9.0/24", "1.1.1.1/32"]
    assert p.auto_block is False
    defaults = Params.from_config({})
    assert (defaults.decision.warn_ips, defaults.decision.block_ips) == (20, 25)
    assert (defaults.decision.min_subnets, defaults.decision.confirm_live_ips) == (10, 10)
    assert defaults.decision.confirm_checks == 2 and defaults.auto_block is False
