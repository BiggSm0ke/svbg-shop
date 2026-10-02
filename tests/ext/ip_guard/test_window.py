"""IP Guard collector: normalization and the window (owner's collector tests ported, 05 §2.2.3)."""

from __future__ import annotations

import ipaddress
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from svbg.ext.ip_guard import window as w
from svbg.ext.ip_guard.config import CollectorParams

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
NODE_A = "11111111-1111-1111-1111-111111111111"
NODE_B = "22222222-2222-2222-2222-222222222222"
NODE_CDN = "33333333-3333-3333-3333-333333333333"


def node(uuid: str, *, connected: bool = True, disabled: bool = False) -> w.NodeInfo:
    return w.NodeInfo(
        uuid, name=f"node-{uuid[:1]}", address="198.51.100.1", is_connected=connected, is_disabled=disabled
    )


def ok_poll(uuid: str, users: dict[int, list[str]], last_seen: datetime | None = None) -> w.NodePoll:
    return w.NodePoll(uuid, "ok", users={uid: [(ip, last_seen) for ip in ips] for uid, ips in users.items()})


def params(**overrides: Any) -> CollectorParams:
    base: dict[str, Any] = {"interval_s": 60, "window_minutes": 10}
    base.update(overrides)
    return CollectorParams(**base)


def public_ips(count: int, *, first_octet: int = 5) -> list[str]:
    return [f"{first_octet}.{i + 1}.0.1" for i in range(count)]


# ------------------------------------------------------------------------------------------ normalization


def test_normalize_ipv6_brackets_and_mapped() -> None:
    assert w.normalize_ip("[2a00:1450:4001::1]") == ipaddress.ip_address("2a00:1450:4001::1")
    assert w.normalize_ip(" ::ffff:8.8.8.8 ") == ipaddress.ip_address("8.8.8.8")
    assert w.strip_ip("[2a00::1]") == "2a00::1"


@pytest.mark.parametrize(
    "raw",
    [
        "127.0.0.1",
        "10.1.2.3",
        "192.168.1.1",
        "172.16.0.1",
        "169.254.1.1",
        "100.64.0.1",
        "224.0.0.1",
        "0.0.0.0",
        "::",
        "::1",
        "fe80::1",
        "fc00::1",
        "ff02::1",
        "2001:db8::1",
        "garbage",
        "",
        "1.2.3",
        None,
        123,
        "1" * 100,
    ],
)
def test_normalize_rejects_non_public_and_garbage(raw: Any) -> None:
    assert w.normalize_ip(raw) is None


def test_normalize_infra_and_ignore_cidrs() -> None:
    infra = {ipaddress.ip_address("8.8.4.4")}
    cidrs = (ipaddress.ip_network("9.9.9.0/24"),)
    assert w.normalize_ip("8.8.4.4", infra_ips=infra) is None
    assert w.normalize_ip("9.9.9.9", ignore_cidrs=cidrs) is None
    assert w.normalize_ip("9.9.10.9", infra_ips=infra, ignore_cidrs=cidrs) is not None


def test_keys_and_subnets() -> None:
    assert w.ip_key(ipaddress.ip_address("5.6.7.8")) == "5.6.7.8"
    assert w.subnet_of_key("5.6.7.8") == "5.6.7.0/24"
    a = w.ip_key(ipaddress.ip_address("2a00:1450:4001:81a::1"))
    b = w.ip_key(ipaddress.ip_address("2a00:1450:4001:81a:ffff::2"))
    assert a == b == "2a00:1450:4001:81a::/64"
    assert w.subnet_of_key(a) == a
    assert w.ip_key(ipaddress.ip_address("2a00:1450:4001:81a::1"), 48) == "2a00:1450:4001::/48"


# ---------------------------------------------------------------------------------------- job status


def test_parse_job_status_variants() -> None:
    users = [{"userId": 7, "ips": [{"ip": "5.5.5.5", "lastSeen": "2026-09-17T11:00:00Z"}, "6.6.6.6"]}]
    ok = w.parse_job_status(
        NODE_A, {"isCompleted": True, "result": {"success": True, "nodeUuid": NODE_A, "users": users}}
    )
    assert ok is not None and ok.status == "ok"
    assert ok.users[7][0][0] == "5.5.5.5" and ok.users[7][1] == ("6.6.6.6", None)
    assert w.parse_job_status(NODE_A, {"isCompleted": False, "isFailed": False}) is None
    assert w.parse_job_status(NODE_A, None).status == "failed"  # type: ignore[union-attr]
    assert w.parse_job_status(NODE_A, {"isFailed": True}).status == "failed"  # type: ignore[union-attr]
    assert w.parse_job_status(NODE_A, {"isCompleted": True, "result": None}).reason == "result_null"  # type: ignore[union-attr]
    failed = w.parse_job_status(
        NODE_A, {"isCompleted": True, "result": {"success": False, "nodeUuid": NODE_A}}
    )
    assert failed is not None and failed.reason == "success_false"
    other = w.parse_job_status(NODE_A, {"isCompleted": True, "result": {"success": True, "nodeUuid": NODE_B}})
    assert other is not None and other.status == "failed"
    junk = w.parse_job_status(
        NODE_A,
        {
            "isCompleted": True,
            "result": {
                "success": True,
                "nodeUuid": NODE_A,
                "users": [{"userId": True}, {"userId": "x"}, 5, {"userId": -1}],
            },
        },
    )
    assert junk is not None and junk.users == {}


# ---------------------------------------------------------------------------------------------- window


def test_offline_node_not_required_and_window_filled() -> None:
    c = w.Window()
    res = c.apply_pass(
        [node(NODE_A), node(NODE_B, connected=False)],
        {NODE_A: ok_poll(NODE_A, {1: public_ips(30)})},
        frozenset(),
        params(),
        now=NOW,
    )
    assert res.polls[NODE_B].status == "offline"
    s = res.stats[1]
    assert (s.ip_count, s.live_ip_count, s.subnet_count, s.complete) == (30, 30, 30, True)


def test_last_seen_does_not_extend_key_and_window_expiry() -> None:
    c = w.Window()
    p = params()
    future = NOW + timedelta(hours=1)
    c.apply_pass(
        [node(NODE_A)],
        {NODE_A: ok_poll(NODE_A, {1: ["5.5.5.5"], 2: ["6.6.6.6"]}, future)},
        frozenset(),
        p,
        now=NOW,
    )
    at_edge = NOW + timedelta(minutes=10)
    res = c.apply_pass(
        [node(NODE_A)], {NODE_A: ok_poll(NODE_A, {2: ["6.6.6.6"]})}, frozenset(), p, now=at_edge
    )
    assert res.stats[1].ip_count == 1
    assert res.stats[1].live_ip_count == 0
    res = c.apply_pass(
        [node(NODE_A)],
        {NODE_A: ok_poll(NODE_A, {2: ["6.6.6.6"]})},
        frozenset(),
        p,
        now=at_edge + timedelta(seconds=1),
    )
    assert 1 not in res.stats
    assert 1 not in c.keys
    assert c.keys[2]["6.6.6.6"].seen_at == at_edge + timedelta(seconds=1)


def test_failed_node_removes_nothing() -> None:
    c = w.Window()
    p = params()
    c.apply_pass([node(NODE_A)], {NODE_A: ok_poll(NODE_A, {1: public_ips(5)})}, frozenset(), p, now=NOW)
    res = c.apply_pass(
        [node(NODE_A)],
        {NODE_A: w.NodePoll(NODE_A, "failed", "timeout")},
        frozenset(),
        p,
        now=NOW + timedelta(seconds=60),
    )
    assert res.stats[1].ip_count == 5
    assert res.stats[1].complete is False
    assert res.stats[1].missing_nodes == (NODE_A,)
    assert not res.any_ok


def test_cdn_node_marked_on_the_fly_drops_keys() -> None:
    c = w.Window()
    nodes = [node(NODE_A), node(NODE_CDN)]
    polls = {
        NODE_A: ok_poll(NODE_A, {1: public_ips(3)}),
        NODE_CDN: ok_poll(NODE_CDN, {1: public_ips(30, first_octet=7)}),
    }
    assert c.apply_pass(nodes, polls, frozenset(), params(), now=NOW).stats[1].ip_count == 33
    res = c.apply_pass(
        nodes,
        {NODE_A: ok_poll(NODE_A, {1: public_ips(3)})},
        frozenset(),
        params(),
        now=NOW + timedelta(seconds=60),
        excluded=frozenset({NODE_CDN}),
    )
    assert res.stats[1].ip_count == 3
    assert NODE_CDN not in res.polls


def test_shared_ips_not_counted_and_not_live() -> None:
    c = w.Window()
    users = {uid: ["8.8.8.8", f"5.0.{uid}.1"] for uid in range(1, 6)}
    res = c.apply_pass([node(NODE_A)], {NODE_A: ok_poll(NODE_A, users)}, frozenset(), params(), now=NOW)
    assert res.shared_ips == {"8.8.8.8": 5}
    assert res.stats[1].ip_count == 1
    assert "8.8.8.8" not in res.live[1][NODE_A]
    assert all("8.8.8.8" not in ips for ips in c.drop_map(1).values())  # a shared IP is never dropped

    c2 = w.Window()
    res = c2.apply_pass(
        [node(NODE_A)],
        {NODE_A: ok_poll(NODE_A, {1: ["8.8.8.8"], 2: ["8.8.8.8"]})},
        frozenset(),
        params(),
        now=NOW,
    )
    assert res.shared_ips == {}
    assert res.stats[1].ip_count == 1
    assert res.live[2][NODE_A] == {"8.8.8.8"}


def test_shared_ip_is_removed_retroactively() -> None:
    c = w.Window()
    c.apply_pass(
        [node(NODE_A)], {NODE_A: ok_poll(NODE_A, {1: ["8.8.8.8", "5.0.0.1"]})}, frozenset(), params(), now=NOW
    )
    assert (
        c.apply_pass(
            [node(NODE_A)],
            {NODE_A: ok_poll(NODE_A, {uid: ["8.8.8.8"] for uid in range(2, 7)})},
            frozenset(),
            params(),
            now=NOW + timedelta(seconds=60),
        )
        .stats[1]
        .ip_count
        == 1
    )


def test_ipv6_brackets_live_without_brackets_and_one_key() -> None:
    c = w.Window()
    ips = ["[2a00:1450:4001:81a::1]", "[2a00:1450:4001:81a::2]"]
    res = c.apply_pass([node(NODE_A)], {NODE_A: ok_poll(NODE_A, {1: ips})}, frozenset(), params(), now=NOW)
    assert res.stats[1].ip_count == 1
    assert res.live[1][NODE_A] == {"2a00:1450:4001:81a::1", "2a00:1450:4001:81a::2"}


def test_raw_per_key_limit() -> None:
    c = w.Window()
    ips = [f"2a00:1450:4001:81a::{i + 1:x}" for i in range(70)]
    c.apply_pass([node(NODE_A)], {NODE_A: ok_poll(NODE_A, {1: ips})}, frozenset(), params(), now=NOW)
    entry = c.keys[1]["2a00:1450:4001:81a::/64"]
    assert len(entry.raw) == w.MAX_RAW_PER_KEY
    assert entry.raw_truncated


def test_max_tracked_keys_overflow() -> None:
    c = w.Window()
    res = c.apply_pass(
        [node(NODE_A)],
        {NODE_A: ok_poll(NODE_A, {1: public_ips(10), 2: public_ips(3, first_octet=6)})},
        frozenset(),
        params(max_tracked_keys=10),
        now=NOW,
    )
    assert res.window_overflow
    assert 2 not in c.keys
    assert res.stats[1].ip_count == 10


def test_infra_ips_filtered() -> None:
    c = w.Window()
    infra = frozenset({ipaddress.ip_address("5.1.0.1")})
    res = c.apply_pass(
        [node(NODE_A)], {NODE_A: ok_poll(NODE_A, {1: public_ips(3)})}, infra, params(), now=NOW
    )
    assert res.stats[1].ip_count == 2


def test_empty_suspicious_limited_to_three_passes() -> None:
    c = w.Window()
    p = params()
    base = {uid: [f"5.0.{uid}.1"] for uid in range(1, 6)}
    c.apply_pass([node(NODE_A)], {NODE_A: ok_poll(NODE_A, base)}, frozenset(), p, now=NOW)
    for i in range(1, 4):
        res = c.apply_pass(
            [node(NODE_A)], {NODE_A: ok_poll(NODE_A, {})}, frozenset(), p, now=NOW + timedelta(seconds=60 * i)
        )
        assert res.polls[NODE_A].reason == "empty_suspicious"
    res = c.apply_pass(
        [node(NODE_A)], {NODE_A: ok_poll(NODE_A, {})}, frozenset(), p, now=NOW + timedelta(seconds=240)
    )
    assert res.polls[NODE_A].status == "ok"


def test_complete_requires_fresh_nodes_until_fail_streak() -> None:
    c = w.Window()
    p = params()
    nodes = [node(NODE_A), node(NODE_B)]
    c.apply_pass(
        nodes,
        {
            NODE_A: ok_poll(NODE_A, {1: public_ips(20)}),
            NODE_B: ok_poll(NODE_B, {1: public_ips(10, first_octet=6)}),
        },
        frozenset(),
        p,
        now=NOW,
    )
    res = None
    for i in range(1, 4):
        res = c.apply_pass(
            nodes,
            {NODE_A: ok_poll(NODE_A, {1: public_ips(20)}), NODE_B: w.NodePoll(NODE_B, "failed", "timeout")},
            frozenset(),
            p,
            now=NOW + timedelta(seconds=60 * i),
        )
        if i < 3:
            assert res.stats[1].complete is False
            assert res.stats[1].missing_nodes == (NODE_B,)
    assert res is not None
    assert res.stats[1].complete is True  # 3 failed passes in a row: the node is no longer required
    assert res.stats[1].ip_count == 30


def test_offline_node_does_not_block_completeness() -> None:
    c = w.Window()
    p = params()
    polls = {
        NODE_A: ok_poll(NODE_A, {1: public_ips(20)}),
        NODE_B: ok_poll(NODE_B, {1: public_ips(10, first_octet=6)}),
    }
    c.apply_pass([node(NODE_A), node(NODE_B)], polls, frozenset(), p, now=NOW)
    res = c.apply_pass(
        [node(NODE_A), node(NODE_B, connected=False)],
        {NODE_A: ok_poll(NODE_A, {1: public_ips(20)})},
        frozenset(),
        p,
        now=NOW + timedelta(seconds=60),
    )
    assert res.stats[1].complete is True


def test_required_nodes_fresh_by_actual_pass_period() -> None:
    """A pass longer than the interval: the failing main node stays required (review 1, lesson 3)."""
    c = w.Window()
    p = params()
    nodes = [node(NODE_A), node(NODE_B)]
    c.apply_pass(
        nodes,
        {NODE_A: ok_poll(NODE_A, {1: public_ips(20)}), NODE_B: ok_poll(NODE_B, {})},
        frozenset(),
        p,
        now=NOW,
    )
    later = NOW + timedelta(minutes=3)  # a slow pass: 3 min > 2 intervals
    res = c.apply_pass(
        nodes,
        {NODE_A: w.NodePoll(NODE_A, "failed", "timeout"), NODE_B: ok_poll(NODE_B, {})},
        frozenset(),
        p,
        now=later,
    )
    assert res.stats[1].complete is False
    assert res.stats[1].missing_nodes == (NODE_A,)


def test_evidence_and_drop_map_and_forget() -> None:
    c = w.Window()
    c.apply_pass(
        [node(NODE_A), node(NODE_B)],
        {NODE_A: ok_poll(NODE_A, {1: public_ips(60)}), NODE_B: ok_poll(NODE_B, {1: ["9.9.9.9"]})},
        frozenset(),
        params(),
        now=NOW,
    )
    ev = c.evidence(1)
    assert ev["total"] == 61 and len(ev["top"]) == 50
    drops = c.drop_map(1)
    assert drops[NODE_B] == ["9.9.9.9"] and len(drops[NODE_A]) == 60
    assert len(c.all_keys(1)) == 61
    c.forget_user(1)
    assert c.evidence(1) == {"top": [], "total": 0} and c.drop_map(1) == {}
