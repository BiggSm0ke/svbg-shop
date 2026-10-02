"""End to end (finding 10): the real importer's output is what the shadow reconciliation (06 §4.3) reads.

The synthetic source of ``synth.seed_edge_cases`` goes through ``BedolagaImporter.run('shadow')`` twice — the
second time after Bedolaga moved on (a balance spent to zero, a pending campaign slug of a user without a
registration) — and then through :class:`svbg.importers.shadow.Reconciliation` with the importer's own
report, exactly as :class:`svbg.importers.shadow.ShadowService` wires them."""

from __future__ import annotations

from typing import Any

import pytest

import svbg.ext.ip_guard.tables  # noqa: F401 - registers the tables on the shared metadata
from svbg.core.crypto import Crypto, generate_key
from svbg.db.meta import metadata
from svbg.ext.lte.tables import lte_metadata
from svbg.importers.bedolaga import BedolagaImporter, ImportConfig, StaticPanelReader
from svbg.importers.shadow import (
    Check,
    PanelIndex,
    Reconciliation,
    ShadowConfig,
    _with_import_excludes,
    import_verdict,
    open_readonly,
)
from tests.dbkit import CountingDatabase
from tests.importers.bedolaga.synth import T0, Scenario, Src, seed_edge_cases

pytestmark = pytest.mark.timeout(240)

CRYPTO = Crypto([generate_key()])


@pytest.fixture
async def scenario(src_dsn: str) -> Scenario:
    src = await Src.connect(src_dsn)
    try:
        return await seed_edge_cases(src)
    finally:
        await src.close()


def importer(db: CountingDatabase, src_dsn: str, sc: Scenario, **rules: Any) -> BedolagaImporter:
    overrides = {"skip_panel_user_ids": [601], **rules}
    config = ImportConfig(env=sc.env, t0=T0, crypto=CRYPTO, overrides=overrides)
    return BedolagaImporter(db, src_dsn, panel=StaticPanelReader(sc.panel), config=config)


async def reconcile(src_dsn: str, dst_dsn: str, sc: Scenario, result: dict[str, Any]) -> dict[str, Check]:
    cfg = _with_import_excludes(ShadowConfig(), result)
    panel = PanelIndex(users={int(u.id): u for u in sc.panel})
    async with open_readonly(src_dsn) as src, open_readonly(dst_dsn) as dst:
        checks = await Reconciliation(src, dst, panel, as_of=T0, config=cfg).run()
    return {c.code: c for c in [import_verdict(result), *checks]}


async def test_two_shadow_runs_of_the_real_importer_reconcile(
    target: CountingDatabase, pg_dsn: str, src_dsn: str, scenario: Scenario
) -> None:
    async with target.tx() as conn:  # the owner-module tables (registered by the integration step)
        await conn.run_sync(lambda c: metadata.create_all(c, checkfirst=True))
        await conn.run_sync(lambda c: lte_metadata.create_all(c, checkfirst=True))
    first = await importer(target, src_dsn, scenario).run("shadow")
    checks = await reconcile(src_dsn, pg_dsn, scenario, first.as_json())
    # The importer's blocking problems are what keeps the day red (С5 alone sees the conflict only).
    assert checks["C0"].status == "fail"
    assert {"subscription_conflict", "user_restricted"} <= set(checks["C0"].facts["blocking"])
    assert checks["C5"].status == "fail" and "110" in checks["C5"].problems[0]

    src = await Src.connect(src_dsn)
    try:
        # Bedolaga moved on: user 3 spent his 5.00 ₽, user 7 (no registration) followed a campaign link.
        await src.conn.execute("UPDATE users SET balance_kopeks = 0 WHERE id = 3")
        slug = await src.conn.fetchval(
            "SELECT start_parameter FROM advertising_campaigns ORDER BY id LIMIT 1"
        )
        await src.conn.execute("UPDATE users SET pending_campaign_slug = $1 WHERE id = 7", slug)
    finally:
        await src.close()
    # The owner resolved the conflict (skip) and applied the restriction by hand.
    rules = {"skip_subscription_ids": [110], "ack_restricted_user_ids": [12]}
    report = await importer(target, src_dsn, scenario, **rules).run("shadow")
    checks = await reconcile(src_dsn, pg_dsn, scenario, report.as_json())
    status = {code: c.status for code, c in checks.items()}
    problems = {code: c.problems for code, c in checks.items() if c.status != "ok"}

    # What still blocks is the importer's own verdict: a paid invoice without a user. The owner modules are
    # installed (С7–С9 green); this source has the LTE twin in the panel of 511 and 600 but no live block,
    # so the writer would put the base squad back — С6 shows it and the importer lists it. The full module
    # state (blocks included) is ``test_lte_shadow_e2e``.
    assert set(checks["C0"].facts["blocking"]) == {"payment_user_missing"}, problems
    assert {c for c, st in status.items() if st != "ok"} == {"C0", "C6"}, problems
    assert all("33333333-3333-4333-8333-333333333333" in p for p in checks["C6"].problems)  # the LTE twin
    assert {e["panel_user_id"] for e in report.issues["lte_twin_without_block"]} == {511, 600}

    # С2: one import_opening + the adjust that spent it — a zero balance with an opening is fine.
    ledger = await target.raw("SELECT reason, amount_minor FROM wallet_ledger WHERE user_id = 3 ORDER BY id")
    assert [(r["reason"], r["amount_minor"]) for r in ledger] == [
        ("import_opening", 500),
        ("import_adjust", -500),
    ]
    # С1: the pending slug is counted on both sides; nothing the importer skipped is counted.
    assert checks["C1"].facts["регистрации"]["pending_slug"] >= 1
    assert checks["C1"].facts["регистрации"]["source"] == checks["C1"].facts["регистрации"]["target"]
