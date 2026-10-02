"""IP Guard state import (06 §2.8, 05 §2.2.10): the active block with its frozen seconds (``paid_until`` not
moved, the hold from the subscriptions stage), the archive, the unblocked-but-not-restored block, warnings as
one aggregate, the white list → ``ip_guard_exempt``; re-runs follow the source."""

from __future__ import annotations

from datetime import timedelta

import pytest

from svbg.importers.bedolaga.ip_guard import evidence, parse_whitelist
from tests.dbkit import CountingDatabase
from tests.importers.bedolaga.synth import T0, Scenario
from tests.importers.bedolaga.test_lte import importer, rows, scenario, src_exec, sub_of_panel
from tests.importers.bedolaga.test_lte_shadow_e2e import module_tables

pytestmark = pytest.mark.timeout(180)

__all__ = ["scenario"]  # the fixture is shared with test_lte


def test_whitelist_parsing_matches_bedolaga() -> None:
    assert parse_whitelist(" 501, 999;abc\n12 501 -3 0") == ([501, 999, 12], ["abc", "-3", "0"])
    assert parse_whitelist(None) == ([], [])


def test_evidence_keeps_the_top_50_in_the_module_format() -> None:
    ips = [
        {
            "key": f"k{i}",
            "raw": [f"10.0.{i}.{j}" for j in range(10)],
            "nodes": {"n2": "t", "n1": "t"},
            "seen_at": "t",
        }
        for i in range(60)
    ]
    ev = evidence(ips, 60)
    assert len(ev["top"]) == 50 and ev["total"] == 60
    assert ev["top"][0] == {
        "key": "k0",
        "ips": sorted(f"10.0.0.{j}" for j in range(10))[:8],
        "nodes": ["n1", "n2"],
        "seen": "t",
    }


async def test_ip_guard_state_is_carried_over(
    target: CountingDatabase, src_dsn: str, scenario: Scenario
) -> None:
    await module_tables(target)
    report = await importer(target, src_dsn, scenario).run("apply")
    s107, s101, s103 = [await sub_of_panel(target, p) for p in (507, 501, 503)]
    blocks = {b["subscription_id"]: b for b in await rows(target, "SELECT * FROM ip_guard_blocks")}
    assert set(blocks) == {s107, s101, s103}

    active = blocks[s107]
    blocked_at = T0 - timedelta(hours=5)
    assert (active["status"], active["reason"], active["blocked_at"], active["panel_user_id"]) == (
        "active",
        "auto",
        blocked_at,
        507,
    )
    assert active["frozen_seconds"] == int((T0 + timedelta(days=15) - blocked_at).total_seconds()) + 60
    assert active["user_id"] == 7 and active["confirmed_by"] == 1, "Telegram id 1001 → users.id 1"
    assert len(active["evidence"]["top"]) == 50 and active["evidence"]["total"] == 60
    assert active["events"][-1]["kind"] == "imported" and active["evidence_purge_at"] is not None
    sub = (await rows(target, "SELECT * FROM subscriptions WHERE id = $1", s107))[0]
    assert (sub["hold_kind"], sub["disabled_reason"], sub["paid_until"]) == (
        "ip_guard",
        "ip_guard",
        T0 + timedelta(days=15),
    ), "paid_until is not moved by the block"

    archive = blocks[s101]
    assert (archive["status"], archive["frozen_seconds"], archive["evidence"]) == (
        "closed",
        0,
        {"archived": True},
    )
    assert archive["closed_by"] == 1
    pending = blocks[s103]
    assert (pending["status"], pending["new_paid_until"], pending["unblock_mode"]) == (
        "unblocked",
        T0 + timedelta(days=12),
        "plain",
    )
    assert [e["block_id"] for e in report.issues["ip_guard_unblock_pending"]] == [3]

    (alert,) = await rows(target, "SELECT * FROM ip_guard_alerts")
    assert (alert["kind"], alert["subscription_id"], alert["metrics"]["legacy_warnings"]) == ("warn", s101, 2)
    assert alert["acked_at"] == T0

    exempt = await rows(target, "SELECT * FROM ip_guard_exempt")
    assert [(e["subscription_id"], e["reason"], e["until"]) for e in exempt] == [(s101, "import", None)]
    assert [e["panel_user_id"] for e in report.issues["ip_guard_whitelist_unresolved"]] == [999]
    assert [e["value"] for e in report.issues["ip_guard_whitelist_invalid"]] == ["abc"]
    assert report.checks["C8"]["ok"] is True


async def test_rerun_follows_the_source(target: CountingDatabase, src_dsn: str, scenario: Scenario) -> None:
    await module_tables(target)
    await importer(target, src_dsn, scenario).run("shadow")
    s101, s107 = await sub_of_panel(target, 501), await sub_of_panel(target, 507)
    # An admin's own «ложная тревога» exemption on 107 is never touched by the import.
    await target.raw(
        "INSERT INTO ip_guard_exempt (subscription_id, reason, until) VALUES ($1, 'false alarm', $2)",
        s107,
        T0 + timedelta(hours=6),
    )
    # Bedolaga: 507 was unblocked and restored, 501 left the white list.
    await src_exec(
        src_dsn,
        "UPDATE ip_guard_blocks SET status = 'unblocked', panel_restored = true, unblocked_at = $1 "
        "WHERE id = 1",
        T0,
    )
    await src_exec(
        src_dsn, "UPDATE system_settings SET value = '999' WHERE key = 'IP_GUARD_WHITELIST_PANEL_USER_IDS'"
    )
    report = await importer(target, src_dsn, scenario).run("shadow")
    blocks = await rows(
        target, "SELECT status, frozen_seconds FROM ip_guard_blocks WHERE subscription_id = $1", s107
    )
    assert blocks == [{"status": "closed", "frozen_seconds": 0}]
    exempt = {e["subscription_id"]: e["reason"] for e in await rows(target, "SELECT * FROM ip_guard_exempt")}
    assert exempt == {s107: "false alarm"}, s101
    assert report.get("ip_guard", "blocks_active") == 0
    assert (await rows(target, "SELECT count(*) AS n FROM ip_guard_alerts"))[0]["n"] == 1, "no duplicate"
