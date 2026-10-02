"""End to end with the owner modules (06 §4.3 С6–С9): the synthetic source of ``seed_edge_cases`` plus the
module state of ``seed_owner_modules`` goes through the real importer (``shadow``, twice) and then through
:class:`svbg.importers.shadow.Reconciliation` with the importer's own report — С6–С9 are green."""

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
from tests.importers.bedolaga.synth import T0, Scenario, Src, seed_edge_cases, seed_owner_modules

pytestmark = pytest.mark.timeout(240)

CRYPTO = Crypto([generate_key()])
RULES = {"skip_panel_user_ids": [601], "skip_subscription_ids": [110], "ack_restricted_user_ids": [12]}


@pytest.fixture
async def scenario(src_dsn: str) -> Scenario:
    src = await Src.connect(src_dsn)
    try:
        sc = await seed_edge_cases(src)
        await seed_owner_modules(src)
        return sc
    finally:
        await src.close()


async def module_tables(db: CountingDatabase) -> None:
    async with db.tx() as conn:  # registered by the integration step; idempotent here
        await conn.run_sync(lambda c: metadata.create_all(c, checkfirst=True))
        await conn.run_sync(lambda c: lte_metadata.create_all(c, checkfirst=True))


def importer(db: CountingDatabase, src_dsn: str, sc: Scenario) -> BedolagaImporter:
    config = ImportConfig(env=sc.env, t0=T0, crypto=CRYPTO, overrides=RULES)
    return BedolagaImporter(db, src_dsn, panel=StaticPanelReader(sc.panel), config=config)


async def reconcile(src_dsn: str, dst_dsn: str, sc: Scenario, result: dict[str, Any]) -> dict[str, Check]:
    cfg = _with_import_excludes(ShadowConfig(), result)
    panel = PanelIndex(users={int(u.id): u for u in sc.panel})
    async with open_readonly(src_dsn) as src, open_readonly(dst_dsn) as dst:
        checks = await Reconciliation(src, dst, panel, as_of=T0, config=cfg).run()
    return {c.code: c for c in [import_verdict(result), *checks]}


async def test_c6_to_c9_are_green_with_the_owner_modules(
    target: CountingDatabase, pg_dsn: str, src_dsn: str, scenario: Scenario
) -> None:
    await module_tables(target)
    first = await importer(target, src_dsn, scenario).run("shadow")
    assert first.counts["modules"] == {"lte": 1, "ip_guard": 1, "referral_days": 1}
    assert "module_missing" not in first.issue_totals, first.issues.get("module_missing")
    second = await importer(target, src_dsn, scenario).run("shadow")  # the daily re-run is idempotent
    checks = await reconcile(src_dsn, pg_dsn, scenario, second.as_json())
    problems = {code: (c.status, c.problems) for code, c in checks.items() if c.status != "ok"}

    for code in ("C6", "C7", "C8", "C9"):
        assert checks[code].status == "ok", problems
    # Only the paid invoice without a user still blocks (06 §2.4: settled by the owner).
    assert set(checks["C0"].facts["blocking"]) == {"payment_user_missing"}, problems
    assert checks["C6"].facts["operations"] == 0
    assert checks["C7"].facts["blocks_source"] == checks["C7"].facts["blocks_target"] == 2
    assert checks["C7"].facts["usage_drift"] == 0
    assert checks["C8"].facts["matched"] == 1 and checks["C8"].facts["whitelist_unresolved"] == [999]
    assert checks["C9"].facts["deferred_target"] == 1
    # The importer's own С7–С9 agree.
    assert {k: second.checks[k]["ok"] for k in ("C7", "C8", "C9")} == {"C7": True, "C8": True, "C9": True}
    # Nothing was duplicated by the second run.
    expected = {
        "lte_blocks": 2,
        "lte_periods": 3,
        "lte_overrides": 5,
        "lte_credits": 1,
        "ip_guard_blocks": 3,
        "ip_guard_alerts": 1,
    }
    for table, n in expected.items():
        rows = await target.raw(f"SELECT count(*) AS n FROM {table}")
        assert rows[0]["n"] == n, table
