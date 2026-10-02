"""Referral days markers (06 §2.5, 05 §2.3.6): granted with the estimated days, skipped → deferred /
expired by the retry window, a money pair with a days marker becomes granted, self markers dropped,
the cut-off makes legacy pairs, the rest is the live tail; a re-run leaves rows the bot changed alone."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from tests.dbkit import CountingDatabase
from tests.importers.bedolaga.synth import T0, Scenario, Src
from tests.importers.bedolaga.test_lte import importer, rows, scenario, src_exec
from tests.importers.bedolaga.test_lte_shadow_e2e import module_tables

pytestmark = pytest.mark.timeout(180)

__all__ = ["scenario"]  # the fixture is shared with test_lte


async def days(db: CountingDatabase) -> dict[tuple[int, str], dict[str, Any]]:
    return {
        (r["referred_user_id"], r["side"]): r
        for r in await rows(db, "SELECT * FROM referral_rewards WHERE kind = 'days' ORDER BY id")
    }


async def test_markers_become_rewards(target: CountingDatabase, src_dsn: str, scenario: Scenario) -> None:
    await module_tables(target)
    src = await Src.connect(src_dsn)
    try:  # a self marker is never carried over
        await src.add(
            "referral_earnings",
            id=15,
            user_id=1,
            referral_id=1,
            amount_kopeks=0,
            reason="referral_days_inviter",
            created_at=T0 - timedelta(days=3),
        )
    finally:
        await src.close()
    report = await importer(target, src_dsn, scenario).run("apply")
    got = await days(target)
    d = timedelta(days=1)
    status = {k: (r["status"], r["user_id"], r["days"]) for k, r in got.items()}
    assert status == {
        (2, "inviter"): ("granted", 1, 14),  # money pair + marker: the days were given
        (2, "invitee"): ("legacy", 2, None),  # money pair, no marker: never rewarded
        (3, "inviter"): ("granted", 1, 14),
        (3, "invitee"): ("granted", 3, 7),
        (5, "inviter"): ("granted", 3, 14),
        (7, "inviter"): ("deferred", 1, None),
        (8, "invitee"): ("expired", 8, None),
    }
    assert got[(2, "inviter")]["amount_minor"] == 5000 and got[(2, "inviter")]["granted_at"] == T0 - 59 * d
    assert got[(3, "inviter")]["reason"] == "estimated" and got[(3, "inviter")]["granted_at"] == T0 - 5 * d
    assert got[(7, "inviter")]["retry_until"] == T0 - 2 * d + timedelta(hours=168)
    assert got[(8, "invitee")]["retry_until"] == T0 - 10 * d + timedelta(hours=168)
    assert report.get("referral_days", "self_markers") == 1
    # 11 → 9 has no marker and there is no cut-off: the live tail, nothing written.
    assert report.get("referral_days", "live_tail") == 1
    assert report.issues["referral_days_live_tail"] == [{"pairs": 1, "min_user_id": None}]
    assert report.checks["C9"]["ok"] is True


async def test_cut_off_and_retry_window(target: CountingDatabase, src_dsn: str, scenario: Scenario) -> None:
    await module_tables(target)
    scenario.env["REFERRAL_DAYS_MIN_USER_ID"] = "10"  # invitee 9 is below the cut-off
    scenario.env["REFERRAL_DAYS_RETRY_SKIPPED_HOURS"] = "24"  # the skip of 7 two days ago has expired
    scenario.env["REFERRAL_DAYS_INVITER_DAYS"] = "30"
    report = await importer(target, src_dsn, scenario).run("apply")
    got = await days(target)
    assert {k: r["status"] for k, r in got.items() if k[0] == 9} == {
        (9, "inviter"): "legacy",
        (9, "invitee"): "legacy",
    }
    assert got[(9, "inviter")]["reason"] == "legacy_cutoff"
    assert got[(7, "inviter")]["status"] == "expired"
    assert got[(3, "inviter")]["days"] == 30
    assert report.get("referral_days", "live_tail") == 0


async def test_rerun_leaves_the_bots_rows_alone(
    target: CountingDatabase, src_dsn: str, scenario: Scenario
) -> None:
    await module_tables(target)
    await importer(target, src_dsn, scenario).run("shadow")
    # The stand's bot granted the deferred side of 7; Bedolaga meanwhile granted the expired side of 8.
    await target.raw(
        "UPDATE referral_rewards SET status = 'granted', granted_at = $1, days = 14, retry_until = NULL "
        "WHERE referred_user_id = 7 AND side = 'inviter'",
        T0,
    )
    await src_exec(
        src_dsn,
        "INSERT INTO referral_earnings (id, user_id, referral_id, amount_kopeks, reason, created_at) "
        "VALUES (20, 11, 8, 0, 'referral_days_invitee', $1)",
        T0 - timedelta(hours=1),
    )
    report = await importer(target, src_dsn, scenario).run("shadow")
    got = await days(target)
    assert (got[(8, "invitee")]["status"], got[(8, "invitee")]["days"]) == ("granted", 7)
    assert got[(7, "inviter")]["granted_at"] == T0
    assert [(e["invitee"], e["side"]) for e in report.issues["referral_days_changed_by_bot"]] == [
        (7, "inviter")
    ]
    assert len(got) == 7, "nothing duplicated"
