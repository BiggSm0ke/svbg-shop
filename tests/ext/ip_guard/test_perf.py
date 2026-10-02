"""IP Guard pass cost on the event loop (review: ≈ 2 s of synchronous ``ipaddress`` work every minute).

The heavy part (normalization) runs once per distinct address in :func:`prepare_polls`, which the service
moves to a worker thread; what stays on the loop (:meth:`Window.apply_pass` with ``prepared``) is dictionary
work only and must stay well under 200 ms for 10 000 users × 3 IPs.
"""

from __future__ import annotations

import asyncio
import gc
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from svbg.ext.ip_guard import window as w
from svbg.ext.ip_guard.config import CollectorParams
from tests.ext.ip_guard.kit import guard_env
from tests.subscriptions.kit import frozen_clock

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
NODES = [f"{i:08d}-0000-0000-0000-000000000000" for i in range(10)]
USERS = 10_000
LOOP_BUDGET_S = 0.2


def _polls(pass_no: int = 0) -> dict[str, w.NodePoll]:
    """10 nodes, 10 000 users × 3 public IPs; every 10th user is also on a second node; on each next pass
    every 10th user has one new address (mobile churn)."""
    polls: dict[str, w.NodePoll] = {u: w.NodePoll(u, "ok", users={}) for u in NODES}
    for uid in range(1, USERS + 1):
        churn = pass_no if uid % 10 == 0 else 0
        addrs = [
            f"{20 + k + (churn if k == 2 else 0)}.{uid % 250 + 1}.{(uid // 250) % 250}.{k + 1}"
            for k in range(3)
        ]
        entries = [(a, None) for a in addrs]
        polls[NODES[uid % 10]].users[uid] = entries
        if uid % 10 == 3:
            polls[NODES[(uid + 1) % 10]].users[uid] = list(entries)
    return polls


def _nodes() -> list[w.NodeInfo]:
    return [w.NodeInfo(u, name=u[:8], address="198.51.100.1", is_connected=True) for u in NODES]


def _params(**overrides: Any) -> CollectorParams:
    return CollectorParams(
        **{"interval_s": 60, "window_minutes": 10, "max_tracked_keys": 200_000, **overrides}
    )


def test_each_distinct_address_is_normalized_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    real = w.normalize_ip

    def counting(raw: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return real(raw, **kwargs)

    monkeypatch.setattr(w, "normalize_ip", counting)
    polls = _polls()
    distinct = {raw for p in polls.values() for entries in p.users.values() for raw, _ in entries}
    window = w.Window()
    window.apply_pass(_nodes(), polls, frozenset(), _params(), now=NOW)
    seen = sum(len(e) for p in polls.values() for e in p.users.values())
    assert calls == len(distinct) < seen  # not 2 × (every IP of every user on every node)


def test_loop_part_of_a_pass_stays_under_budget() -> None:
    """CPU time of this thread for a steady-state pass; the best of 4 passes (other agents and the GC of the
    test process are noise, not the pass). The inline pass (no ``prepared``) is the "before" reference."""
    params = _params()
    window = w.Window()
    nodes = _nodes()
    spent: list[float] = []
    result = None
    for i in range(5):  # the later passes meet a full window
        polls = _polls(i)
        prepared = w.prepare_polls(polls.values(), frozenset(), params)
        gc.collect()
        started = time.thread_time()
        result = window.apply_pass(
            nodes, polls, frozenset(), params, now=NOW + timedelta(minutes=i), prepared=prepared
        )
        if i:
            spent.append(time.thread_time() - started)
    assert result is not None and len(result.stats) == USERS
    assert result.stats[10].ip_count == 7  # 3 + 4 churned addresses
    gc.collect()
    started = time.thread_time()
    w.Window().apply_pass(nodes, _polls(0), frozenset(), params, now=NOW)  # everything on the loop
    inline = time.thread_time() - started
    best = min(spent)
    assert best < LOOP_BUDGET_S, f"{best * 1000:.0f} ms of the event loop"
    assert best < inline / 2, (best, inline)


def test_prepared_and_inline_passes_agree() -> None:
    p = _params(shared_ip_users=2)
    polls = {
        NODES[0]: w.NodePoll(
            NODES[0],
            "ok",
            users={
                1: [("[2a00:1450::1]", None), ("10.0.0.1", None), ("9.9.9.9", None)],
                2: [("8.8.8.8", None)],
            },
        ),
        NODES[1]: w.NodePoll(NODES[1], "ok", users={3: [("9.9.9.9", None)]}),
        NODES[2]: w.NodePoll(NODES[2], "failed", "timeout"),
    }
    prepared = w.prepare_polls(polls.values(), frozenset(), p)
    assert prepared.nodes[NODES[0]] == {
        1: [("2a00:1450::1", "2a00:1450::/64", "2a00:1450::1"), ("9.9.9.9", "9.9.9.9", "9.9.9.9")],
        2: [("8.8.8.8", "8.8.8.8", "8.8.8.8")],
    }
    assert prepared.shared == {"9.9.9.9": 2} and NODES[2] not in prepared.nodes
    a, b = w.Window(), w.Window()
    ra = a.apply_pass(_nodes()[:3], polls, frozenset(), p, now=NOW)
    rb = b.apply_pass(_nodes()[:3], polls, frozenset(), p, now=NOW, prepared=prepared)
    assert ra.stats == rb.stats and ra.shared_ips == rb.shared_ips == {"9.9.9.9": 2}
    assert a.keys.keys() == b.keys.keys() and ra.live == rb.live


def test_stale_prepared_is_recounted() -> None:
    """A prepared node that is not ``ok`` in this pass (offline now) must not make an IP shared."""
    p = _params(shared_ip_users=2)
    polls = {
        NODES[0]: w.NodePoll(NODES[0], "ok", users={1: [("9.9.9.9", None)]}),
        NODES[1]: w.NodePoll(NODES[1], "ok", users={3: [("9.9.9.9", None)]}),
    }
    prepared = w.prepare_polls(polls.values(), frozenset(), p)
    nodes = [_nodes()[0], w.NodeInfo(NODES[1], is_connected=False)]
    result = w.Window().apply_pass(nodes, polls, frozenset(), p, now=NOW, prepared=prepared)
    assert result.shared_ips == {} and result.stats[1].ip_count == 1


def test_required_nodes_by_last_nodes_cover_every_fresh_key_node() -> None:
    """The invariant ``required_nodes`` relies on: fresh per-key node marks ⊆ fresh ``last_nodes``."""
    p = _params()
    window = w.Window()
    nodes = _nodes()[:3]
    at = NOW
    for i in range(12):
        users = {
            NODES[i % 3]: {uid: [f"{30 + (uid + i) % 7}.1.{uid}.1"] for uid in range(1, 8) if (uid + i) % 3},
            NODES[(i + 1) % 3]: {uid: [f"{40 + uid}.2.{uid}.1"] for uid in range(1, 8) if uid % 2 == i % 2},
        }
        polls = {
            u: w.NodePoll(u, "ok", users={k: [(ip, None) for ip in v] for k, v in m.items()})
            for u, m in users.items()
        }
        polls[NODES[(i + 2) % 3]] = w.NodePoll(NODES[(i + 2) % 3], "failed", "timeout")
        window.apply_pass(nodes, polls, frozenset(), p, now=at)
        fresh = at - (window._pass_fresh or timedelta(seconds=2 * p.interval_s))
        for pid, keys in window.keys.items():
            marks = {u for e in keys.values() for u, t in e.nodes.items() if t >= fresh}
            assert marks <= {u for u, t in window.last_nodes.get(pid, {}).items() if t >= fresh}, (i, pid)
        at += timedelta(seconds=60 if i % 4 else 200)


async def test_service_prepares_off_the_event_loop(pg_dsn: str, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[bool] = []
    real = w.prepare_polls

    def spy(*args: Any) -> Any:
        try:
            asyncio.get_running_loop()
            seen.append(True)
        except RuntimeError:
            seen.append(False)  # no running loop here: a worker thread
        return real(*args)

    monkeypatch.setattr("svbg.ext.ip_guard.service.prepare_polls", spy)
    async with guard_env(pg_dsn) as g:
        with frozen_clock():
            await g.blocked_sub(990, n_ips=3)
            summary = await g.service.run_pass()
    assert summary.nodes_ok == 1 and seen == [False]
