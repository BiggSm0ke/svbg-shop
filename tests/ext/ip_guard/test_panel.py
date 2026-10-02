"""IP Guard reads from the panel through the real transport against the fake panel."""

from __future__ import annotations

from svbg.ext.ip_guard.panel import PanelReader
from tests.ext.ip_guard.kit import guard_env, ips


async def test_nodes_poll_and_by_user(pg_dsn: str) -> None:
    async with guard_env(pg_dsn) as g:
        reader = PanelReader(g.env.current_api, poll_interval=0.0)
        g.panel.nodes[0]["ips"] = [{"ip": "5.200.0.1/32"}, "5.200.0.2"]
        nodes = await reader.nodes()
        assert (
            nodes[0].uuid == g.node and nodes[0].ips == ("5.200.0.1", "5.200.0.2") and nodes[0].is_connected
        )
        g.panel.by_node[g.node] = {42: ips(3)}
        g.panel.pending_rounds = 3
        poll = await reader.poll_node(g.node, budget_s=5)
        assert poll.status == "ok" and [ip for ip, _ in poll.users[42]] == ips(3)
        assert await reader.by_user(42) == {g.node: sorted(ips(3))}

        g.panel.pending_rounds = 10_000
        slow = PanelReader(g.env.current_api, poll_interval=0.01)
        timed_out = await slow.poll_node(g.node, budget_s=0.2)
        assert (timed_out.status, timed_out.reason) == ("failed", "timeout")

        g.panel.pending_rounds = 0
        g.panel.node_errors[g.node] = 500
        failed = await reader.poll_node(g.node, budget_s=5)
        assert failed.status == "failed" and failed.http_status == 500
        bad = await reader.poll_node("a/b", budget_s=5)
        assert bad.reason == "bad_uuid"


async def test_vanished_job_is_a_node_failure(pg_dsn: str) -> None:
    async with guard_env(pg_dsn) as g:
        reader = PanelReader(g.env.current_api, poll_interval=0.0)
        g.panel.inject("404", path="/connections/by-node/", method="GET", times=None)  # A218: job gone
        poll = await reader.poll_node(g.node, budget_s=5)
        assert (poll.status, poll.reason) == ("failed", "job_missing")
