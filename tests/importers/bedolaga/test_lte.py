"""LTE state import (06 §2.9, 05 §2.1.17) on the synthetic source: groups / twins / anchors / live periods
with usage / overrides (``exempt_kind``, the paid ``launch_trial``) / credits / blocks with their
substitutions / sent notices / counters; re-runs follow the source."""

from __future__ import annotations

from typing import Any

import pytest

from svbg.core.crypto import Crypto, generate_key
from svbg.importers.bedolaga import BedolagaImporter, ImportConfig, StaticPanelReader
from tests.dbkit import CountingDatabase
from tests.importers.bedolaga.synth import (
    GB,
    LTE_NODE,
    SQ_LTE_GROUP,
    SQ_NL,
    SQ_TWIN,
    T0,
    Scenario,
    Src,
    seed_edge_cases,
    seed_owner_modules,
)
from tests.importers.bedolaga.test_lte_shadow_e2e import RULES, module_tables

pytestmark = pytest.mark.timeout(180)

CRYPTO = Crypto([generate_key()])


@pytest.fixture
async def scenario(src_dsn: str) -> Scenario:
    src = await Src.connect(src_dsn)
    try:
        sc = await seed_edge_cases(src)
        await seed_owner_modules(src)
        return sc
    finally:
        await src.close()


def importer(db: CountingDatabase, src_dsn: str, sc: Scenario) -> BedolagaImporter:
    config = ImportConfig(env=sc.env, t0=T0, crypto=CRYPTO, overrides=RULES)
    return BedolagaImporter(db, src_dsn, panel=StaticPanelReader(sc.panel), config=config)


async def rows(db: CountingDatabase, sql: str, *args: Any) -> list[dict[str, Any]]:
    return [dict(r) for r in await db.raw(sql, *args)]


async def src_exec(src_dsn: str, sql: str, *args: Any) -> None:
    src = await Src.connect(src_dsn)
    try:
        await src.conn.execute(sql, *args)
    finally:
        await src.close()


async def sub_of_panel(db: CountingDatabase, pid: int) -> int:
    return int((await rows(db, "SELECT id FROM subscriptions WHERE panel_user_id = $1", pid))[0]["id"])


async def test_lte_state_is_carried_over(target: CountingDatabase, src_dsn: str, scenario: Scenario) -> None:
    await module_tables(target)
    report = await importer(target, src_dsn, scenario).run("apply")
    s111, s600 = await sub_of_panel(target, 511), await sub_of_panel(target, 600)

    (g,) = await rows(target, "SELECT * FROM lte_groups")
    assert (g["slug"], g["state"], g["enforce"], g["margin_pct"], g["squad_uuid"]) == (
        "lte",
        "active",
        True,
        5,
        SQ_LTE_GROUP,
    )
    assert (g["has_default"], g["limit_default_bytes"], g["has_trial"], g["limit_trial_bytes"]) == (
        True,
        10 * GB,
        True,
        GB,
    )
    assert [
        (n["node_uuid"], n["counted_to"]) for n in await rows(target, "SELECT * FROM lte_group_nodes")
    ] == [(LTE_NODE, None)]
    twins = await rows(target, "SELECT base_squad_uuid, twin_squad_uuid, group_id FROM lte_twins")
    assert twins == [{"base_squad_uuid": SQ_NL, "twin_squad_uuid": SQ_TWIN, "group_id": g["id"]}]
    core = await rows(target, "SELECT * FROM panel_squad_twins")
    assert [(c["substitute_squad_uuid"], c["base_squad_uuid"], c["owner_module"]) for c in core] == [
        (SQ_TWIN, SQ_NL, "lte")
    ]

    # Blocks: the enforce ones (active + pending_apply), never the shadow-mode one; the twin substitution.
    blocks = {b["subscription_id"]: b for b in await rows(target, "SELECT * FROM lte_blocks")}
    assert set(blocks) == {s111, s600}
    assert all(b["status"] == "active" and b["mode"] == "enforce" for b in blocks.values())
    assert blocks[s111]["used_at_block"] == 10_000_000_001 and blocks[s111]["applied_at"] is not None
    subst = await rows(target, "SELECT * FROM panel_squad_substitutions ORDER BY subscription_id")
    assert [
        (r["subscription_id"], r["base_squad_uuid"], r["substitute_squad_uuid"], r["source_ref"])
        for r in subst
    ] == [(s, SQ_NL, SQ_TWIN, f"lte:block:{blocks[s]['id']}") for s in sorted((s111, s600))]

    # Periods: only the live ones; the usage (gap estimate added to the estimate).
    periods = {p["subscription_id"]: p for p in await rows(target, "SELECT * FROM lte_periods")}
    s101 = await sub_of_panel(target, 501)
    assert {s: p["state"] for s, p in periods.items()} == {s111: "open", s600: "open", s101: "deferred"}
    assert periods[s600]["start_estimated"] is True and periods[s111]["idx"] == 1
    usage = await rows(target, "SELECT * FROM lte_period_usage WHERE period_id = $1", periods[s111]["id"])
    assert (usage[0]["used_bytes"], usage[0]["after_block_bytes"], usage[0]["estimated_bytes"]) == (
        10_200_000_000,
        100_000_000,
        6_000_000,
    )
    assert blocks[s111]["period_id"] == periods[s111]["id"]

    anchors = {a["subscription_id"]: a for a in await rows(target, "SELECT * FROM lte_anchors")}
    assert anchors[s111]["coverage_end"] is not None and anchors[s111]["series_open"] is True
    assert anchors[s600]["anchor_kind"] == "manual" and anchors[s600]["review_reason"] == "panel only"
    assert anchors[await sub_of_panel(target, 502)]["is_trial"] is True

    # Overrides: live limit, exemptions by kind (the paid launch_trial carried over revoked), no_block.
    ov = await rows(target, "SELECT * FROM lte_overrides ORDER BY id")
    by = {(o["kind"], o["exempt_kind"], o["subscription_id"]): o for o in ov}
    s102, s103 = await sub_of_panel(target, 502), await sub_of_panel(target, 503)
    assert by[("limit", None, s101)]["limit_bytes"] == 20 * GB
    assert by[("exempt", "owner", s101)]["revoked_at"] is None
    assert by[("exempt", "launch_trial", s102)]["revoked_at"] is None
    assert (
        by[("exempt", "launch_trial", s103)]["revoke_reason"],
        by[("exempt", "launch_trial", s103)]["revoked_at"],
    ) == (
        "converted_to_paid",
        T0,
    )
    assert by[("no_block", None, s101)]["period_id"] == periods[s101]["id"]
    assert len(ov) == 5, "the expired limit and the revoked manual exemption stay behind"
    assert report.get("lte", "launch_trial_converted") == 1

    (credit,) = await rows(target, "SELECT * FROM lte_credits")
    assert (credit["source"], credit["bytes"], credit["amount_minor"], credit["period_id"]) == (
        "import",
        5 * GB,
        9900,
        periods[s111]["id"],
    )

    # Sent notices of the live period only (warn + exhausted with the block in the anchor).
    notes = await rows(
        target, "SELECT kind, anchor, status FROM notification_log WHERE kind LIKE 'lte_%' ORDER BY kind"
    )
    pid, gid, bid = periods[s111]["id"], g["id"], blocks[s111]["id"]
    assert notes == [
        {"kind": "lte_exhausted", "anchor": f"{pid}:{gid}:{bid}", "status": "sent"},
        {"kind": "lte_warn", "anchor": f"{pid}:{gid}", "status": "sent"},
    ]

    # Counters of D−1/D with a valid invariant; the node read marks.
    counters = await rows(target, "SELECT usage_date, total FROM lte_counters")
    assert [(c["usage_date"].isoformat(), c["total"]) for c in counters] == [("2026-09-30", 500)]
    assert report.issue_totals["lte_counter_invalid"] == 1
    (node,) = await rows(target, "SELECT * FROM lte_node_state")
    assert (node["node_uuid"], node["gap_tail"], node["xray_uptime_s"]) == (LTE_NODE, 2, 3600)
    assert report.checks["C7"]["ok"] is True
    assert "lte_twin_without_block" not in report.issue_totals


async def test_rerun_follows_the_source(target: CountingDatabase, src_dsn: str, scenario: Scenario) -> None:
    await module_tables(target)
    await importer(target, src_dsn, scenario).run("shadow")
    s111 = await sub_of_panel(target, 511)
    # Bedolaga released the block of 511 (new period), revoked the owner exemption, the top-up burned.
    await src_exec(src_dsn, "UPDATE wlq_blocks SET status = 'released', released_at = now() WHERE id = 1")
    await src_exec(src_dsn, "UPDATE wlq_periods SET state = 'closed', ended_at = $1 WHERE id = 11", T0)
    await src_exec(
        src_dsn,
        "INSERT INTO wlq_periods (id, subject_id, anchor_at, period_index, starts_at, planned_end_at, state) "
        "VALUES (14, 2, $1, 2, $1, $2, 'open')",
        T0,
        T0.replace(month=11),
    )
    await src_exec(src_dsn, "UPDATE wlq_exemptions SET revoked_at = $1 WHERE id = 1", T0)
    await src_exec(src_dsn, "UPDATE wlq_topups SET status = 'expired', expired_at = $1", T0)
    report = await importer(target, src_dsn, scenario).run("shadow")

    blocks = await rows(target, "SELECT * FROM lte_blocks WHERE subscription_id = $1", s111)
    assert [(b["status"], b["release_reason"]) for b in blocks] == [("released", "import")]
    assert not await rows(
        target, "SELECT 1 FROM panel_squad_substitutions WHERE subscription_id = $1", s111
    ), "the writer puts the base squad back"
    live = await rows(
        target, "SELECT idx, state FROM lte_periods WHERE subscription_id = $1 ORDER BY idx", s111
    )
    assert live == [{"idx": 1, "state": "closed"}, {"idx": 2, "state": "open"}]
    owner = await rows(target, "SELECT revoked_at FROM lte_overrides WHERE exempt_kind = 'owner'")
    assert owner[0]["revoked_at"] is not None
    assert [c["status"] for c in await rows(target, "SELECT status FROM lte_credits")] == ["expired"]
    assert report.get("lte", "blocks_released") == 1 and report.get("lte", "blocks") == 1


async def test_without_the_module_tables_the_gate_stays_red(
    target: CountingDatabase, src_dsn: str, scenario: Scenario
) -> None:
    """The module code is there but its migration did not run: the importer says so (blocking)."""
    await module_tables(target)
    await target.raw("DROP TABLE lte_credits")
    report = await importer(target, src_dsn, scenario).run("dry_run")
    missing = {e["module"]: e["state"] for e in report.issues["module_missing"]}
    assert set(missing) == {"lte"} and "lte_credits" in missing["lte"][0]
    assert not report.green
