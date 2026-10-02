"""IP Guard on a real PostgreSQL with the fake panel: pass → decision → block (freeze + disable + drop) →
cards → unblock by button; anomaly fuse; exempt; bypass; failure paths."""

from __future__ import annotations

import inspect
from datetime import timedelta
from typing import Any

from svbg.core.clock import now
from svbg.ext.ip_guard.service import TOPIC, IpGuardService
from svbg.ext.ip_guard.window import NodeInfo, NodePoll, PassResult
from svbg.subscriptions.lifecycle import SubscriptionLifecycle
from tests.ext.ip_guard.kit import GuardEnv, guard_env
from tests.subscriptions.kit import frozen_clock

DAY = 86_400


async def two_passes(g: GuardEnv, clock: object) -> None:
    await g.service.run_pass()
    clock.advance(seconds=60)  # type: ignore[attr-defined]
    await g.service.run_pass()


async def test_auto_block_freezes_disables_drops_and_reports(pg_dsn: str) -> None:
    async with guard_env(pg_dsn, IP_GUARD_AUTO_BLOCK=True) as g:
        with frozen_clock() as clock:
            sid, pid = await g.blocked_sub(501)
            first = await g.service.run_pass()
            assert first.nodes_ok == 1 and first.watched == 1 and first.blocks == []
            assert await g.db.raw("select 1 from ip_guard_alerts") == []  # confirmation pending: no card
            clock.advance(seconds=60)
            second = await g.service.run_pass()
            assert second.blocks == [sid]
            block = await g.one("select * from ip_guard_blocks where subscription_id = $1", sid)
            assert block["status"] == "active" and block["reason"] == "auto" and block["ip_count"] == 30
            sub = await g.env.sub(sid)
            assert sub["hold_kind"] == "ip_guard" and sub["hold_frozen_seconds"] > 29 * DAY
            # the module's own decision: reported by its card, not in the admins' audit trail
            assert await g.db.raw("select 1 from admin_audit where action like 'ip_guard.%'") == []
            await g.drain()
            assert g.panel.users[pid]["status"] == "DISABLED"
            drops = g.panel.dropped
            assert len(drops) == 1
            assert drops[0]["dropBy"]["by"] == "ipAddresses" and len(drops[0]["dropBy"]["ipAddresses"]) == 30
            assert drops[0]["targetNodes"] == {"target": "specificNodes", "nodeUuids": [g.node]}
            card = g.chat.last(f"block:{block['id']}")
            assert card["kind"] == TOPIC and "Подписка заблокирована" in card["text"]
            assert "разорваны на 1 нодах" in card["text"] and "✅ уведомлён" in card["text"]
            assert ("PinChatMessage", -1001, card["message_id"]) in g.notifier.calls
            assert g.notifier.sent[0]["chat_id"] == 501 and "30 разных IP" in g.notifier.sent[0]["text"]
            # a third pass sees the user still blocked: nothing new
            clock.advance(seconds=60)
            third = await g.service.run_pass()
            assert third.blocks == []


async def test_auto_block_off_warns_and_admin_blocks_then_unblocks_with_new_link(pg_dsn: str) -> None:
    async with guard_env(pg_dsn) as g:  # auto block off by default
        with frozen_clock() as clock:
            sid, pid = await g.blocked_sub(502)
            await two_passes(g, clock)
            assert await g.db.raw("select 1 from ip_guard_blocks") == []
            alert = await g.one("select * from ip_guard_alerts where subscription_id = $1", sid)
            assert (alert["kind"], alert["reason"]) == ("not_blocked", "precondition")
            await g.drain()
            assert "автоблок выключен" in g.chat.last(f"alert:{alert['id']}")["text"]
            res = await g.service.block_from_alert(int(alert["id"]), actor_id=None)
            assert res.ok and res.code == "blocked"
            again = await g.service.block_from_alert(int(alert["id"]), actor_id=None)
            assert not again.ok and again.code == "already"
            await g.drain()
            block = await g.one("select * from ip_guard_blocks where subscription_id = $1", sid)
            assert block["reason"] == "manual"
            frozen = int((await g.env.sub(sid))["hold_frozen_seconds"])
            clock.advance(days=5)  # frozen days do not burn
            out = await g.service.unblock(int(block["id"]), actor_id=None, revoke=True)
            assert out.ok and out.code == "active"
            assert (await g.service.unblock(int(block["id"]), actor_id=None)).code == "already"
            await g.drain()
            sub = await g.env.sub(sid)
            assert sub["hold_kind"] is None and sub["paid_until"] == clock() + timedelta(seconds=frozen)
            user = g.panel.users[pid]
            assert user["status"] == "ACTIVE" and user["expireAt"] == clock() + timedelta(seconds=frozen)
            kinds = [r.path.rsplit("/", 1)[-1] for r in g.panel.calls(method="POST") if "/actions/" in r.path]
            assert kinds.index("revoke") < kinds.index("enable")  # new link while still disabled
            card = g.chat.last(f"block:{block['id']}")
            assert "Разблокирована" in card["text"] and "новая ссылка" in card["text"]
            assert ("UnpinChatMessage", -1001, card["message_id"]) in g.notifier.calls
            assert "Доступ восстановлен" in g.notifier.sent[-1]["text"]
            assert "Ссылка обновлена" in g.notifier.sent[-1]["text"]
            # grace: the window is reset and the user is not blocked again right away
            assert pid not in g.service.window.keys
            g.cfg["IP_GUARD_AUTO_BLOCK"] = True
            await two_passes(g, clock)
            assert len(await g.db.raw("select 1 from ip_guard_blocks where status = 'active'")) == 0
            grace = await g.db.raw(
                "select 1 from ip_guard_alerts where subscription_id = $1 and reason = 'grace'", sid
            )
            assert grace


async def test_extension_during_block_is_credited_exactly(pg_dsn: str) -> None:
    async with guard_env(pg_dsn) as g:
        with frozen_clock() as clock:
            sid, _ = await g.blocked_sub(503)
            assert (await g.service.manual_block(sid, actor_id=None)).ok
            frozen = int((await g.env.sub(sid))["hold_frozen_seconds"])
            async with g.db.tx() as conn:
                await SubscriptionLifecycle().extend(
                    conn, sid, 30 * DAY, source="admin", reason="компенсация"
                )
            clock.advance(days=3)
            block_id = int(
                (await g.one("select id from ip_guard_blocks where subscription_id = $1", sid))["id"]
            )
            await g.service.unblock(block_id, actor_id=None)
            sub = await g.env.sub(sid)
            assert sub["paid_until"] == clock() + timedelta(seconds=frozen + 30 * DAY)


async def test_close_zeroes_and_later_unblock_gives_nothing(pg_dsn: str) -> None:
    async with guard_env(pg_dsn) as g:
        with frozen_clock() as clock:
            sid, pid = await g.blocked_sub(504)
            await g.service.manual_block(sid, actor_id=None)
            block_id = int(
                (await g.one("select id from ip_guard_blocks where subscription_id = $1", sid))["id"]
            )
            frozen = int((await g.env.sub(sid))["hold_frozen_seconds"])
            assert (await g.service.close(block_id, actor_id=None, reason=" ")).code == "no_reason"
            assert (await g.env.sub(sid))["hold_frozen_seconds"] == frozen  # no reason: nothing zeroed
            res = await g.service.close(block_id, actor_id=None, reason="перепродажа", actor_role="admin")
            assert res.ok
            assert (await g.service.close(block_id, actor_id=None, reason="ещё раз")).code == "already"
            audit = await g.one("select * from admin_audit where action = 'ip_guard.close'")
            assert (audit["target"], audit["reason"], audit["role"]) == (f"sub:{sid}", "перепродажа", "admin")
            assert audit["details"] == {"block_id": block_id, "zeroed_seconds": frozen}
            sub = await g.env.sub(sid)
            assert sub["hold_kind"] == "ip_guard" and sub["hold_frozen_seconds"] == 0 and sub["hold_zeroed"]
            await g.drain()
            assert "Блок закрыт" in g.chat.last(f"block:{block_id}")["text"]
            assert g.panel.users[pid]["status"] == "DISABLED"
            sent_before = len(g.notifier.sent)
            out = await g.service.unblock(block_id, actor_id=None)
            assert out.code == "zeroed"
            await g.drain()
            assert len(g.notifier.sent) == sent_before  # «zeroed»: nothing to tell
            # the term is over: the panel gets "now" (clamped to +2 min by the writer) and expires the user
            assert g.panel.users[pid]["expireAt"] <= clock() + timedelta(minutes=2)
            assert (await g.env.sub(sid))["paid_until"] == clock()
            unblocked = await g.one("select * from admin_audit where action = 'ip_guard.unblock'")
            assert unblocked["target"] == f"sub:{sid}" and unblocked["details"]["outcome"] == "zeroed"
            assert unblocked["details"]["frozen_seconds"] == 0


async def test_admin_actions_are_audited_in_the_same_transaction(pg_dsn: str) -> None:
    """Review: block / unblock change paid time → ``admin_audit`` (actor, role, ``sub:<id>``, seconds)."""
    async with guard_env(pg_dsn) as g:
        with frozen_clock() as clock:
            sid, _ = await g.blocked_sub(505)
            await g.service.manual_block(sid, actor_id=None, actor_role="owner")
            block = await g.one(
                "select id, frozen_seconds from ip_guard_blocks where subscription_id = $1", sid
            )
            clock.advance(days=1)
            await g.service.unblock(int(block["id"]), actor_id=None, revoke=True, actor_role="owner")
            rows = await g.db.raw("select * from admin_audit where target = $1 order by id", f"sub:{sid}")
            assert [(r["action"], r["role"]) for r in rows] == [
                ("ip_guard.block", "owner"),
                ("ip_guard.unblock", "owner"),
            ]
            assert rows[0]["details"]["frozen_seconds"] == block["frozen_seconds"]
            details = rows[1]["details"]
            assert (details["mode"], details["outcome"]) == ("revoke", "active")
            assert details["frozen_seconds"] == block["frozen_seconds"]
            expected = clock() + timedelta(seconds=int(block["frozen_seconds"]))
            assert details["new_paid_until"] == expected.isoformat()
            assert "новой ссылкой" in rows[1]["reason"]


async def test_anomaly_fuse_quarantine_and_admin_decisions(pg_dsn: str) -> None:
    async with guard_env(pg_dsn, IP_GUARD_AUTO_BLOCK=True) as g:
        with frozen_clock() as clock:
            subs = [await g.blocked_sub(600 + i) for i in range(4)]
            await g.service.run_pass()
            assert await g.db.raw("select 1 from ip_guard_blocks") == []
            anomaly = await g.one("select * from ip_guard_alerts where kind = 'anomaly'")
            assert len(anomaly["members"]) == 4 and anomaly["metrics"]["trigger"] == "per_run"
            assert anomaly["quarantine_until"] == clock() + timedelta(minutes=10)
            await g.drain()
            card = g.chat.last(f"alert:{anomaly['id']}")
            assert "Автоблоки остановлены" in card["text"]
            assert card["buttons"][0][0].callback_data == f"ipg:ab:{anomaly['id']}:4"
            assert "ip_guard:anomaly" in await g.env.attention_keys()
            # during the quarantine nobody is blocked, even after more passes
            clock.advance(seconds=60)
            await g.service.run_pass()
            assert await g.db.raw("select 1 from ip_guard_blocks") == []
            # the admin saw 3, the list has 4: refused
            stale = await g.service.anomaly_block(int(anomaly["id"]), actor_id=None, expected=3)
            assert not stale.ok and stale.code == "changed"
            res = await g.service.anomaly_block(int(anomaly["id"]), actor_id=None, expected=4)
            assert res.ok and res.text == "Заблокировано: 4"
            rows = await g.db.raw("select reason from ip_guard_blocks")
            assert sorted(r["reason"] for r in rows) == ["anomaly"] * 4
            members = (await g.one("select members from ip_guard_alerts where id = $1", anomaly["id"]))[
                "members"
            ]
            assert {m["state"] for m in members.values()} == {"blocked"}
            for sid, _ in subs:
                assert (await g.env.sub(sid))["hold_kind"] == "ip_guard"


async def test_false_alarm_protects_members_for_six_hours(pg_dsn: str) -> None:
    async with guard_env(pg_dsn, IP_GUARD_AUTO_BLOCK=True) as g:
        with frozen_clock() as clock:
            for i in range(4):
                await g.blocked_sub(700 + i)
            await g.service.run_pass()
            anomaly = await g.one("select id from ip_guard_alerts where kind = 'anomaly'")
            res = await g.service.anomaly_dismiss(int(anomaly["id"]), actor_id=None)
            assert res.ok
            rows = await g.db.raw("select until from ip_guard_exempt")
            assert len(rows) == 4 and all(r["until"] == clock() + timedelta(hours=6) for r in rows)
            clock.advance(minutes=11)  # the quarantine is over; «ложная тревога» still holds
            await two_passes(g, clock)
            assert await g.db.raw("select 1 from ip_guard_blocks") == []
            dismissed = await g.db.raw("select 1 from ip_guard_alerts where reason = 'dismissed'")
            assert len(dismissed) == 4


async def test_white_list_never_blocks(pg_dsn: str) -> None:
    async with guard_env(pg_dsn, IP_GUARD_AUTO_BLOCK=True) as g:
        with frozen_clock() as clock:
            sid, _ = await g.blocked_sub(801)
            await g.service.set_exempt(sid, on=True, actor_id=None, reason="офис с одним NAT")
            await two_passes(g, clock)
            assert await g.db.raw("select 1 from ip_guard_blocks") == []
            assert await g.db.raw("select 1 from ip_guard_alerts where reason = 'whitelist'")
            await g.service.set_exempt(sid, on=False, actor_id=None)
            await two_passes(g, clock)
            assert len(await g.db.raw("select 1 from ip_guard_blocks")) == 1


async def test_bypass_is_disabled_again_and_reported(pg_dsn: str) -> None:
    async with guard_env(pg_dsn) as g:
        with frozen_clock() as clock:
            sid, pid = await g.blocked_sub(901)
            await g.service.manual_block(sid, actor_id=None)
            await g.drain()
            # someone enables the user in the panel; the projection saw it
            g.panel.users[pid]["status"] = "ACTIVE"
            await g.db.raw("update subscriptions set panel_status = 'ACTIVE' where id = $1", sid)
            clock.advance(minutes=3)
            await g.service.run_pass()
            assert f"ip_guard:bypass:{sid}" in await g.env.attention_keys()
            await g.drain()
            assert g.panel.users[pid]["status"] == "DISABLED"
            block = await g.one("select id, events from ip_guard_blocks where subscription_id = $1", sid)
            assert any(e["kind"] == "bypass" for e in block["events"])
            assert "в обход кнопки" in g.chat.last(f"block:{block['id']}")["text"]
            # cooldown: the next pass does not repeat it
            await g.db.raw("update subscriptions set panel_status = 'ACTIVE' where id = $1", sid)
            clock.advance(minutes=1)
            await g.service.run_pass()
            assert len(await g.db.raw("select 1 from jobs where kind = 'panel.disable'")) == 2
            # the unblock closes the bypass item (and answers normally: no DNS lookup of "bypass:<id>")
            out = await g.service.unblock(int(block["id"]), actor_id=None)
            assert out.ok
            assert f"ip_guard:bypass:{sid}" not in await g.env.attention_keys()


class _Attention:
    def __init__(self) -> None:
        self.raised: list[str] = []
        self.resolved: list[str] = []

    async def raise_item(self, key: str, severity: str, title: str, body: str) -> None:
        del severity, title, body
        self.raised.append(key)

    async def resolve(self, key: str) -> None:
        self.resolved.append(key)


async def test_health_closes_items_and_never_sends_them_to_dns() -> None:
    """Review: the DNS resolver once shadowed ``_resolve(key)``: each good pass did getaddrinfo("scopes")."""
    looked_up: list[str] = []

    async def dns(host: str) -> frozenset[Any]:
        looked_up.append(host)
        raise OSError("no DNS in tests")

    attention = _Attention()
    service = IpGuardService(
        None,  # type: ignore[arg-type]
        lambda: None,  # type: ignore[arg-type, return-value]
        config=dict,
        attention=attention,  # type: ignore[arg-type]
        resolve=dns,
    )
    assert inspect.ismethod(service._resolve)
    service._down_alerted = True
    result = PassResult(at=now(), polls={"n": NodePoll("n", "ok")}, stats={}, live={})
    await service._health(result, ())
    assert attention.resolved == ["ip_guard:collect_down", "ip_guard:scopes"]
    assert looked_up == []
    # node host names still go to the resolver; its failure only means "no node IPs"
    assert await service._infra_ips([NodeInfo("n", address="node.example")]) == frozenset()
    assert looked_up == ["node.example"]


async def test_residual_connections_are_reported_once(pg_dsn: str) -> None:
    async with guard_env(pg_dsn) as g:
        with frozen_clock() as clock:
            sid, _ = await g.blocked_sub(902)
            await g.service.manual_block(sid, actor_id=None)
            await g.drain()
            for _ in range(4):  # the node keeps reporting the user (UDP/QUIC, no CAP_NET_ADMIN)
                clock.advance(seconds=60)
                await g.service.run_pass()
            events = (await g.one("select events from ip_guard_blocks where subscription_id = $1", sid))[
                "events"
            ]
            assert [e["kind"] for e in events].count("residual") == 1


async def test_failures_are_isolated(pg_dsn: str) -> None:
    async with guard_env(pg_dsn, IP_GUARD_AUTO_BLOCK=True) as g:
        with frozen_clock() as clock:
            await g.blocked_sub(950)
            g.panel.node_errors[g.node] = 403
            summary = await g.service.run_pass()
            assert summary.nodes_failed == 1 and summary.blocks == []
            assert "ip_guard:scopes" in await g.env.attention_keys()
            g.panel.node_errors.clear()
            g.panel.pending_rounds = 2  # the job is answered after two «not ready» polls
            clock.advance(seconds=60)
            assert (await g.service.run_pass()).nodes_ok == 1
            # a panel user without a subscription of the bot: never blocked, the card says why
            g.panel.add_user(id=9999)
            g.panel.by_node[g.node][9999] = [f"6.{i + 1}.1.1" for i in range(30)]
            clock.advance(seconds=60)
            await g.service.run_pass()
            clock.advance(seconds=60)
            await g.service.run_pass()
            failed = await g.one(
                "select * from ip_guard_alerts where panel_user_id = 9999 and kind = 'block_failed'"
            )
            assert failed["subscription_id"] is None
            assert "не привязан" in failed["metrics"]["fail_reason"]


async def test_nodes_list_error_is_not_a_crash(pg_dsn: str) -> None:
    async with guard_env(pg_dsn) as g:
        g.panel.inject("500", path="/nodes", method="GET", times=None)
        summary = await g.service.run_pass()
        assert summary.skipped == "nodes"
        report = await g.service.health()
        assert report.status.value in ("ok", "degraded", "down", "unknown")


async def test_drop_job_failure_paths(pg_dsn: str) -> None:
    async with guard_env(pg_dsn) as g:
        with frozen_clock():
            sid, _ = await g.blocked_sub(960)
            sid2, _ = await g.blocked_sub(961)
            await g.service.run_pass()
            g.panel.drop_status = 404  # the node is not connected now (A219): skipped, not retried
            await g.service.manual_block(sid, actor_id=None)
            outcomes = await g.drain()
            assert all(o == "done" for _, o in outcomes)
            events = (await g.one("select events from ip_guard_blocks where subscription_id = $1", sid))[
                "events"
            ]
            dropped = next((e for e in events if e["kind"] == "dropped"), None)
            assert dropped is not None, (events, outcomes)
            assert (dropped["nodes"], dropped["offline"]) == (0, 1)

            g.panel.drop_status = 403  # no scope: dead job + owner alert
            await g.service.manual_block(sid2, actor_id=None)
            outcomes = await g.drain()
            assert ("ip_guard.drop_ips", "dead") in [(j.kind, o) for j, o in outcomes]
            assert "ip_guard:scopes" in await g.env.attention_keys()


async def test_new_nodes_and_cdn_flag(pg_dsn: str) -> None:
    async with guard_env(pg_dsn, IP_GUARD_AUTO_BLOCK=True) as g:
        with frozen_clock() as clock:
            await g.service.run_pass()  # first run: the nodes become known silently
            assert "ip_guard:new_nodes" not in await g.env.attention_keys()
            cdn = g.panel.add_node("CDN-1")
            _, pid = await g.blocked_sub(970)
            g.panel.by_node[cdn] = {pid: g.panel.by_node[g.node].pop(pid)}
            await g.service.run_pass()
            assert "ip_guard:new_nodes" in await g.env.attention_keys()
            assert (await g.service.set_cdn(cdn, cdn=True)).ok
            clock.advance(seconds=60)
            await g.service.run_pass()
            clock.advance(seconds=60)
            await g.service.run_pass()
            assert await g.db.raw("select 1 from ip_guard_blocks") == []  # CDN IPs never count
            assert not any(r.path == f"/connections/by-node/{cdn}" for r in g.panel.requests[-6:])
            assert not (await g.service.set_cdn("nope", cdn=True)).ok


async def test_purge_erases_old_evidence(pg_dsn: str) -> None:
    async with guard_env(pg_dsn) as g:
        with frozen_clock() as clock:
            sid, _ = await g.blocked_sub(980)
            await g.service.run_pass()
            await g.service.manual_block(sid, actor_id=None)
            clock.advance(days=181)
            assert await g.service.purge() == 1
            row = await g.one(
                "select evidence, ip_count from ip_guard_blocks where subscription_id = $1", sid
            )
            assert row["evidence"] == {"purged": True} and row["ip_count"] == 30


async def test_user_notification_never_retried_and_card_says_so(pg_dsn: str) -> None:
    async with guard_env(pg_dsn) as g:
        with frozen_clock():
            sid, _ = await g.blocked_sub(990)
            g.notifier.fail = TimeoutError()
            await g.service.manual_block(sid, actor_id=None)
            outcomes = await g.drain()
            assert [o for j, o in outcomes if j.kind == "ip_guard.notify"] == ["done"]
            block = await g.one("select id from ip_guard_blocks where subscription_id = $1", sid)
            assert "не удалось отправить" in g.chat.last(f"block:{block['id']}")["text"]


async def test_status_report_and_ips_document(pg_dsn: str) -> None:
    async with guard_env(pg_dsn) as g:
        with frozen_clock():
            sid, _ = await g.blocked_sub(991)
            await g.service.run_pass()
            await g.service.manual_block(sid, actor_id=None)
            lines = await g.service.status_lines()
            assert lines[0].startswith("Автоблок: выключен") and "Активных блоков: 1" in lines[1]
            assert (await g.service.report_lines())[0] == "Блоков за сутки: 1"
            block = await g.one("select id from ip_guard_blocks where subscription_id = $1", sid)
            doc = await g.service.ips_document(f"block:{block['id']}")
            assert doc is not None and b"5.1.241.1" in doc.data and b"NL-1" in doc.data
            g.service.reset()  # after a restart only the stored top-50 is left
            doc = await g.service.ips_document(f"block:{block['id']}")
            assert doc is not None and "Полный список недоступен".encode() in doc.data
            assert await g.service.ips_document("block:999999") is None
            assert await g.service.ips_document("junk") is None
