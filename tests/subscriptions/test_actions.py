"""User actions: link reissue and HWID devices (02 §4.7–4.8) — guards, cooldowns, panel effect."""

from __future__ import annotations

from datetime import timedelta

import pytest

from svbg.subscriptions.devices import K_HWID_DELETE, K_HWID_RESET, SubscriptionActions
from svbg.subscriptions.hold import freeze
from tests.subscriptions.kit import SyncEnv, frozen_clock, sync_env

ACTIONS = SubscriptionActions()


async def owned(env: SyncEnv, tg: int) -> tuple[int, int]:
    sid = await env.linked_sub(tg)
    uid = (await env.sub(sid))["user_id"]
    return int(uid), sid


async def test_reissue_link_with_cooldown_and_guards(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        with frozen_clock() as clock:
            uid, sid = await owned(env, 1000)
            before = await env.sub(sid)
            async with env.db.tx() as conn:
                mark = env.db.queries
                res = await ACTIONS.reissue_link(conn, sid, user_id=uid)
                assert res.ok and res.job_id is not None
                guard_sql = env.db.queries - mark
            assert guard_sql <= 6  # guard CAS + job + notify + audit + event (no reads, no panel HTTP)
            async with env.db.tx() as conn:
                again = await ACTIONS.reissue_link(conn, sid, user_id=uid)
            assert not again.ok and again.reason == "cooldown" and again.retry_after_s == 600
            assert "10 мин" in again.text
            await env.drain()
            after = await env.sub(sid)
            assert after["panel_short_uuid"] != before["panel_short_uuid"]
            assert after["subscription_url"] != before["subscription_url"]
            clock.advance(timedelta(minutes=10))
            async with env.db.tx() as conn:
                assert (await ACTIONS.reissue_link(conn, sid, user_id=uid)).ok
            assert "subscription.reissue_requested" in {e.name for e in env.events}


async def test_guards_refuse_without_side_effects(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        uid, sid = await owned(env, 1001)
        stranger = await env.add_user(1002)
        pending = await env.new_sub(1003)
        jobs = len(await env.jobs())
        async with env.db.tx() as conn:
            assert (await ACTIONS.reissue_link(conn, sid, user_id=stranger)).reason == "not_found"
            assert (await ACTIONS.reset_devices(conn, 10**9)).reason == "not_found"
            assert (await ACTIONS.reissue_link(conn, pending)).reason == "pending"
        async with env.db.tx() as conn:
            await freeze(conn, sid, "admin", reason="тест")
        async with env.db.tx() as conn:
            res = await ACTIONS.reset_devices(conn, sid, user_id=uid)
        assert res.reason == "frozen" and "приостановлена" in res.text
        assert len(await env.jobs(K_HWID_RESET)) == 0
        assert len(await env.jobs()) == jobs + 1  # only the freeze's disable
        assert (await env.sub(sid))["cooldowns"] == {}
        with pytest.raises(ValueError):
            async with env.db.tx() as conn:
                await ACTIONS.delete_device(conn, sid, "")


async def test_reset_and_delete_devices_on_the_panel(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        uid, sid = await owned(env, 1004)
        pid = (await env.sub(sid))["panel_user_id"]
        for hwid in ("a1", "b2", "c3"):
            env.panel.add_device(pid, hwid)
        async with env.db.tx() as conn:
            assert (await ACTIONS.delete_device(conn, sid, "b2", user_id=uid)).ok
        await env.drain()
        assert [d["hwid"] for d in env.panel.devices[pid]] == ["a1", "c3"]
        assert env.panel.dropped  # connections dropped so the device really goes offline
        job = (await env.jobs(K_HWID_DELETE))[0]
        assert job["ordering_key"] == f"sub:{sid}" and job["status"] == "done"
        # Already gone (a retry, or the user removed it elsewhere): still a success.
        await env.db.raw("update subscriptions set cooldowns = '{}'::jsonb where id = $1", sid)
        async with env.db.tx() as conn:
            assert (await ACTIONS.delete_device(conn, sid, "b2", user_id=uid)).ok
        outcomes = await env.drain()
        assert [o for j, o in outcomes if j.queue == "panel"] == ["done"]
        async with env.db.tx() as conn:
            assert (await ACTIONS.reset_devices(conn, sid, user_id=uid)).ok
            assert (await ACTIONS.reset_devices(conn, sid, user_id=uid)).reason == "cooldown"
        await env.drain()
        assert env.panel.devices[pid] == []
        kinds = [r["kind"] for r in await env.db.raw("select kind from subscription_events order by id")]
        assert {"device_deleted", "devices_reset", "devices_reset_requested"} <= set(kinds)


async def test_device_job_after_close_is_a_noop(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        uid, sid = await owned(env, 1005)
        pid = (await env.sub(sid))["panel_user_id"]
        env.panel.add_device(pid, "z9")
        async with env.db.tx() as conn:
            assert (await ACTIONS.reset_devices(conn, sid, user_id=uid)).ok
        await env.db.raw("update subscriptions set link_state = 'closed' where id = $1", sid)
        outcomes = await env.drain()
        assert [o for j, o in outcomes if j.kind == K_HWID_RESET] == ["done"]
        assert [d["hwid"] for d in env.panel.devices[pid]] == ["z9"]


def test_cooldowns_follow_settings() -> None:
    cfg = {"REISSUE_COOLDOWN_MINUTES": 0, "DEVICES_RESET_COOLDOWN_MINUTES": 30}
    actions = SubscriptionActions(config=lambda: cfg)
    assert actions.cooldown("reissue") == timedelta(0)
    assert actions.cooldown("devices_reset") == timedelta(minutes=30)
    assert SubscriptionActions(config=dict).cooldown("reissue") == timedelta(minutes=10)
