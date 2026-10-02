"""LTE collector (05 §2.1.8): topology reads, ``POST /bandwidth-stats/nodes/usage`` for the LTE nodes only,
one
transaction per cycle, failed reads = incompleteness, read marks move only after a good read."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from svbg.ext.lte.accounting import AccountingParams
from svbg.ext.lte.collector import Collector, PanelReader, Topology, parse_nodes, parse_squads
from svbg.remnawave.errors import RemnawaveError
from tests.ext.lte.rkit import GB, LteEnv, lte_env
from tests.fakes.remnawave import Fault


def test_parse_nodes_and_squads_skip_garbage() -> None:
    nodes = parse_nodes(
        [
            {
                "uuid": "ABC",
                "name": "LTE",
                "isConnected": True,
                "xrayUptime": 42,
                "createdAt": "2026-01-01T00:00:00Z",
                "configProfile": {"activeInbounds": [{"uuid": "i1"}, {"tag": "T"}, "i3", 5]},
            },
            {"name": "no uuid"},
            "junk",
        ]
    )
    assert list(nodes) == ["abc"]
    facts = nodes["abc"]
    assert facts.inbounds == frozenset({"i1", "T", "i3"}) and facts.xray_uptime_s == 42
    assert facts.created_at == datetime(2026, 1, 1, tzinfo=UTC)
    inbounds, names = parse_squads(
        {"internalSquads": [{"uuid": "s", "name": "NL", "inbounds": [{"uuid": "i1"}]}, 1]}
    )
    assert inbounds == {"s": frozenset({"i1"})} and names == {"s": "NL"}
    assert parse_squads(None) == ({}, {}) and parse_nodes(None) == {}


def test_group_tags_are_the_inbounds_of_the_group_nodes() -> None:
    topo = Topology(
        nodes=parse_nodes([{"uuid": "n1", "configProfile": {"activeInbounds": ["lte"]}}]), squad_inbounds={}
    )
    assert topo.group_tags({1: ["N1", "missing"]}) == {1: frozenset({"lte"})}
    assert topo.node_inbounds() == {"n1": frozenset({"lte"})}


pg = pytest.mark.pg


async def _collect(env: LteEnv, at: datetime) -> object:
    reader = PanelReader(env.rw.current_api)
    collector = Collector(env.db, reader)
    topology = await reader.topology()
    return await collector.collect(
        group_nodes={env.group_id: [env.lte_node]}, params=AccountingParams(), topology=topology, at=at
    )


@pg
async def test_topology_from_the_panel(pg_dsn: str) -> None:
    async with lte_env(pg_dsn) as env:
        topo = await PanelReader(env.rw.current_api).topology()
        assert topo.nodes[env.lte_node].inbounds == frozenset({"ib-lte"})
        assert topo.squad_inbounds[env.twin] == frozenset({"ib-main"})
        assert topo.group_tags({1: [env.lte_node]}) == {1: frozenset({"ib-lte"})}


@pg
async def test_two_cycles_store_a_delta_once(pg_dsn: str) -> None:
    async with lte_env(pg_dsn) as env:
        sid = await env.linked_sub(901)
        pid = await env.open_period(sid)
        panel_id = await env.panel_id(sid)
        t0 = datetime.now(UTC).replace(microsecond=0)
        day = t0.date().isoformat()
        env.panel.node_usage[(env.lte_node, day)] = {panel_id: 2 * GB}
        env.panel.node_usage[(env.plain_node, day)] = {panel_id: 50 * GB}  # never read
        first = await _collect(env, t0)
        assert first.ok and first.requests >= 1  # type: ignore[attr-defined]
        bodies = [c.body["nodesUuids"] for c in env.panel.calls("/bandwidth-stats/nodes/usage", "POST")]
        assert bodies and all(b == [env.lte_node] for b in bodies)
        t1 = t0 + timedelta(minutes=10)
        env.panel.node_usage[(env.lte_node, t1.date().isoformat())] = {panel_id: 5 * GB}
        await _collect(env, t1)
        used = (await env.db.raw("select used_bytes from lte_period_usage where period_id = $1", pid))[0]
        hourly = await env.db.raw(
            "select sum(bytes) as s from lte_usage_hourly where subscription_id = $1", sid
        )
        daily = await env.db.raw(
            "select sum(bytes) as s from lte_usage_daily where subscription_id = $1", sid
        )
        assert used["used_bytes"] == hourly[0]["s"] == daily[0]["s"]
        assert used["used_bytes"] >= 3 * GB
        before = used["used_bytes"]
        # the same totals again: nothing new is charged
        await _collect(env, t1 + timedelta(minutes=10))
        again = (await env.db.raw("select used_bytes from lte_period_usage where period_id = $1", pid))[0]
        assert again["used_bytes"] == before
        state = await env.db.raw(
            "select last_ok_read_at from lte_node_state where node_uuid = $1", env.lte_node
        )
        assert state[0]["last_ok_read_at"] is not None
        counters = await env.db.raw("select node_uuid from lte_counters")
        assert {r["node_uuid"] for r in counters} == {env.lte_node}


@pg
async def test_failed_read_is_incomplete_and_keeps_the_marks(pg_dsn: str) -> None:
    async with lte_env(pg_dsn) as env:
        sid = await env.linked_sub(902)
        await env.open_period(sid)
        env.panel.faults.append(Fault("500", path="/bandwidth-stats/nodes/usage", times=100))
        outcome = await _collect(env, datetime.now(UTC))
        assert not outcome.ok and outcome.failed_requests >= 1  # type: ignore[attr-defined]
        assert outcome.group_states[env.group_id].incomplete  # type: ignore[attr-defined]
        state = await env.db.raw(
            "select last_ok_read_at from lte_node_state where node_uuid = $1", env.lte_node
        )
        assert not state or state[0]["last_ok_read_at"] is None


@pg
async def test_disconnected_node_is_remembered(pg_dsn: str) -> None:
    async with lte_env(pg_dsn) as env:
        env.panel.nodes[0]["isConnected"] = False
        outcome = await _collect(env, datetime.now(UTC))
        assert outcome.group_states[env.group_id].incomplete  # type: ignore[attr-defined]
        rows = await env.db.raw(
            "select disconnected_since from lte_node_state where node_uuid = $1", env.lte_node
        )
        assert rows and rows[0]["disconnected_since"] is not None


@pg
async def test_node_usage_api_validates_its_input(pg_dsn: str) -> None:
    async with lte_env(pg_dsn) as env:
        api = env.rw.api
        with pytest.raises(RemnawaveError):
            await api.node_usage(datetime.now(UTC), [env.lte_node])  # type: ignore[arg-type]
        with pytest.raises(RemnawaveError):
            await api.node_usage(date.today(), [])
        with pytest.raises(RemnawaveError):
            await api.node_usage(date.today(), ["../users"])
        answer = await api.node_usage(date.today(), [env.lte_node])
        assert answer.payload == {"nodes": [{"uuid": env.lte_node, "users": []}]}
