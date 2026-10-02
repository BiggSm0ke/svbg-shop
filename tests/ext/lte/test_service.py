"""LTE runtime service (05 §2.1.8–2.1.10, 07 §2.4.3): config, the cycle end to end on the fake panel, term
events,
fail-closed «выключение», re-send after traffic past a block, daily / retention / invariants, health."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest

from svbg.ext.api import ExtensionHost, ModuleSpec
from svbg.ext.lte.service import (
    ADVANCED,
    K_ENABLED,
    K_ENFORCE,
    K_OFF_ACTION,
    SETTINGS,
    SPEC,
    LteConfig,
    engine_event,
    kv_get,
)
from tests.ext.lte.rkit import GB, LteEnv, lte_env

# ------------------------------------------------------------------------------------------- pure parts


def test_config_falls_back_to_defaults_on_bad_values() -> None:
    cfg = LteConfig.from_snapshot(
        {
            K_ENABLED: "yes",  # only a real True enables
            K_ENFORCE: "maybe",
            K_OFF_ACTION: "drop",
            "LTE_ENFORCE_LIST": [1, "2", "x"],
            "LTE_WARN_PERCENT": 5,
            "LTE_QUIET_HOURS": "garbage",
            "LTE_GB_BYTES": True,
            "SUPPORT_URL": "javascript:alert(1)",
        }
    )
    assert not cfg.enabled and cfg.mode == "shadow" and cfg.off_action == "keep"
    assert cfg.pilot == frozenset({1, 2}) and cfg.warn_percent == 50
    assert cfg.quiet == (time(0), time(9)) and cfg.gb_bytes == 10**9 and cfg.support_url is None
    assert LteConfig.from_snapshot({"SUPPORT_URL": "https://t.me/x"}).support_url == "https://t.me/x"
    assert LteConfig.from_snapshot({K_OFF_ACTION: "release", K_ENFORCE: "off"}).enforce.release_when_off


def test_manifest_is_valid_and_settings_are_hot_and_owner_only() -> None:
    assert isinstance(SPEC, ModuleSpec) and SPEC.owns_substitutions
    keys = {d.key for d in SETTINGS}
    assert all(k.startswith("LTE_") for k in keys) and set(ADVANCED) <= keys
    assert all(d.apply.name == "HOT" and d.owner_only for d in SETTINGS)
    assert {"lte_pack"} == set(SPEC.order_items) and {"addon_lte"} == set(SPEC.order_kinds)
    host = ExtensionHost([SPEC])
    assert [p.code for _, p in host.permissions()] == ["lte.view", "lte.users", "lte.config"]
    assert {j.kind: j.when_disabled for j in SPEC.jobs}["lte.confirm"] == "run"  # releases finish when off
    assert {j.kind: j.when_disabled for j in SPEC.jobs}["lte.resend"] == "run"


def test_engine_event_ignores_non_term_events() -> None:
    row = {
        "id": 1,
        "kind": "devices_changed",
        "source": "bot",
        "details": {},
        "ts": datetime(2026, 10, 2, tzinfo=UTC),
        "old_expire": None,
        "new_expire": None,
    }
    assert engine_event(row) is None
    paid = engine_event(
        {
            **row,
            "kind": "purchase_renew",
            "old_expire": datetime(2026, 10, 5, tzinfo=UTC),
            "new_expire": datetime(2026, 11, 5, tzinfo=UTC),
            "details": {"paid_at": "2026-10-02T10:00:00+00:00"},
        }
    )
    assert paid is not None and paid.kind == "paid" and paid.paid_at == datetime(2026, 10, 2, 10, tzinfo=UTC)


# -------------------------------------------------------------------------------------------- database

pg = pytest.mark.pg


async def _usage(env: LteEnv, sid: int, total: int, at: datetime) -> None:
    env.panel.node_usage[(env.lte_node, at.date().isoformat())] = {await env.panel_id(sid): total}


@pg
async def test_cycle_reads_only_lte_nodes_counts_and_blocks(pg_dsn: str) -> None:
    async with lte_env(pg_dsn) as env:
        sid = await env.linked_sub(401)
        pid = await env.open_period(sid, used=0)
        t0 = datetime.now(UTC).replace(microsecond=0)
        await _usage(env, sid, 1 * GB, t0)
        first = await env.service.run_cycle(at=t0)
        assert first.ok and first.requests >= 1
        calls = env.panel.calls("/bandwidth-stats/nodes/usage", "POST")
        assert calls and all(c.body["nodesUuids"] == [env.lte_node] for c in calls), "только ноды LTE"
        t1 = t0 + timedelta(minutes=10)
        await _usage(env, sid, 13 * GB, t1)
        second = await env.service.run_cycle(at=t1)
        assert second.ok
        used = await env.db.raw("select used_bytes from lte_period_usage where period_id = $1", pid)
        assert used[0]["used_bytes"] >= 10 * GB
        assert second.blocks == 1, env.service.last_plan
        (block,) = await env.blocks(sid)
        assert block["mode"] == "enforce" and block["reason"] == "quota"
        state = await _kv(env, "cycle")
        assert state["ok"] and state["blocks"] == 1
        assert (await env.service.health()).status.value == "ok"


async def _kv(env: LteEnv, key: str) -> dict[str, object]:
    async with env.db.read() as conn:
        return await kv_get(conn, key)


@pg
async def test_failed_panel_read_is_an_incomplete_cycle_not_a_crash(pg_dsn: str) -> None:
    async with lte_env(pg_dsn) as env:
        sid = await env.linked_sub(402)
        await env.open_period(sid, used=11 * GB)
        from tests.fakes.remnawave import Fault

        env.panel.faults.append(Fault("500", path="/bandwidth-stats/nodes/usage", times=100))
        report = await env.service.run_cycle()
        assert not report.ok and report.failed >= 1
        # decisions still run on what is known: incompleteness alone does not stop a block
        assert report.blocks == 1
        health = await env.service.health()
        assert health.status.value == "degraded" and "неполный" in health.summary


@pg
async def test_term_events_open_a_period_right_after_payment(pg_dsn: str) -> None:
    async with lte_env(pg_dsn) as env:
        sid = await env.linked_sub(403)
        await env.service.process_subscription(sid)
        rows = await env.db.raw("select state, is_trial from lte_periods where subscription_id = $1", sid)
        assert [r["state"] for r in rows] == ["open"]
        cursor = await env.db.raw(
            "select last_event_id from lte_event_cursor where subscription_id = $1", sid
        )
        assert cursor[0]["last_event_id"] > 0
        again = await env.service.process_subscription(sid)  # exactly once: nothing new
        assert again is not None and again.placed == []
        assert len(await env.db.raw("select 1 from lte_periods where subscription_id = $1", sid)) == 1


@pg
@pytest.mark.parametrize(("off_action", "status"), [("keep", "active"), ("release", "releasing")])
async def test_enforce_off_releases_only_with_the_explicit_choice(
    pg_dsn: str, off_action: str, status: str
) -> None:
    async with lte_env(pg_dsn) as env:
        sid = await env.linked_sub(404)
        await env.open_period(sid, used=11 * GB)
        await env.service.process_subscription(sid)
        await env.drain()
        env.config[K_ENFORCE] = "off"
        env.config[K_OFF_ACTION] = off_action
        await env.service.process_subscription(sid)
        (block,) = await env.blocks(sid)
        assert block["status"] == status
        await env.drain()
        assert await env.panel_squads(sid) == ([env.twin] if off_action == "keep" else [env.base])


@pg
async def test_traffic_after_a_block_resends_at_most_hourly(pg_dsn: str) -> None:
    async with lte_env(pg_dsn) as env:
        sid = await env.linked_sub(405)
        pid = await env.open_period(sid, used=11 * GB)
        await env.service.process_subscription(sid)
        await env.drain()
        await env.db.raw(
            "update lte_blocks set applied_at = now() - interval '1 hour' where subscription_id = $1", sid
        )
        await env.db.raw("update lte_period_usage set last_delta_at = now() where period_id = $1", pid)
        at = datetime.now(UTC) + timedelta(seconds=5)
        assert await env.service._after_block_check(at) == 1
        assert await env.service._after_block_check(at + timedelta(minutes=10)) == 0  # not more than hourly
        assert "lte:after_block" in await env.rw.attention_keys()
        await env.drain(make_due=True)
        assert env.panel.squad_resends == [(env.twin, [await env.panel_id(sid)])]


@pg
async def test_daily_expires_overrides_and_revokes_a_stuck_launch_trial(pg_dsn: str) -> None:
    async with lte_env(pg_dsn) as env:
        sid = await env.linked_sub(406)
        await env.open_period(sid)
        await env.db.raw(
            "insert into lte_overrides (subscription_id, kind, limit_bytes, valid_until) "
            "values ($1, 'limit', 1, now() - interval '1 minute')",
            sid,
        )
        await env.db.raw(
            "insert into lte_overrides (subscription_id, kind, exempt_kind) "
            "values ($1, 'exempt', 'launch_trial')",
            sid,
        )
        await env.db.raw(
            "insert into subscription_events (subscription_id, kind, source) "
            "values ($1, 'purchase_renew', 'bot')",
            sid,
        )
        await env.db.raw(
            "update lte_event_cursor set last_event_id = (select max(id) from subscription_events) "
            "where subscription_id = $1",
            sid,
        )
        assert await env.service.daily() == 2
        rows = await env.db.raw("select kind, revoke_reason from lte_overrides order by id")
        assert [(r["kind"], r["revoke_reason"]) for r in rows] == [
            ("limit", "expired"),
            ("exempt", "converted_to_paid"),
        ]


@pg
async def test_retention_keeps_recent_rows(pg_dsn: str) -> None:
    async with lte_env(pg_dsn) as env:
        sid = await env.linked_sub(407)
        await env.db.raw(
            "insert into lte_usage_hourly (subscription_id, group_id, hour_utc, bytes) values "
            "($1, $2, now() - interval '5 days', 1), ($1, $2, date_trunc('hour', now()), 1)",
            sid,
            env.group_id,
        )
        assert await env.service.retention() >= 1
        rows = await env.db.raw("select count(*) as n from lte_usage_hourly")
        assert rows[0]["n"] == 1


@pg
async def test_invariants_flag_a_twin_that_is_not_base_minus_lte(pg_dsn: str) -> None:
    async with lte_env(pg_dsn) as env:
        assert await env.service.invariants() == []
        env.panel.internal_squads[env.twin]["inbounds"].append({"uuid": "ib-lte", "tag": "LTE"})
        keys = await env.service.invariants()
        assert keys and any("twin" in k for k in keys)
        rows = await env.db.raw("select problem from lte_twins")
        assert rows[0]["problem"]
        # a broken twin is never used for a block
        sid = await env.linked_sub(408)
        await env.open_period(sid, used=11 * GB)
        applied = await env.service.process_subscription(sid)
        assert applied is not None and applied.placed == [] and applied.unenforceable


@pg
async def test_quarantine_holds_a_wave_until_the_admin_confirms(pg_dsn: str) -> None:
    async with lte_env(pg_dsn, LTE_QUARANTINE_NEW_BLOCKS=1, LTE_MAX_NEW_BLOCKS_PER_CYCLE=1) as env:
        sids = [await env.linked_sub(410 + i) for i in range(3)]
        for sid in sids:
            await env.open_period(sid, used=11 * GB)
        report = await env.service.run_cycle()
        assert report.blocks == 0 and report.quarantine == (env.group_id,)
        assert "lte:quarantine" in await env.rw.attention_keys()
        from svbg.ext.lte.admin import LteAdmin
        from svbg.tg.ui.context import UserCtx

        res = await LteAdmin(env.service).confirm_quarantine(actor=UserCtx(user_id=1, role="owner"))
        assert res.ok
        report = await env.service.run_cycle()
        assert report.blocks == 1  # the wave goes, throttled to 1 per cycle


@pg
async def test_status_lines_and_teardown_baseline(pg_dsn: str) -> None:
    async with lte_env(pg_dsn) as env:
        lines = await env.service.status_lines()
        assert lines[0] == "Применение: on" and lines[1] == "Заблокировано: 0"
        assert (await env.service.health()).summary == "Ждёт первого цикла учёта"
        await env.db.raw(
            "insert into lte_node_state (node_uuid, last_ok_read_at) values ($1, now())", env.lte_node
        )
        env.config[K_ENABLED] = False
        await env.service.on_teardown()
        env.config[K_ENABLED] = True
        await env.service.on_setup()  # re-enabled after a switch-off: a fresh baseline
        assert await env.db.raw("select 1 from lte_node_state") == []
