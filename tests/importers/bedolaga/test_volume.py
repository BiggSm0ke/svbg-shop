"""Acceptance (stage 4b): prod-like volumes import in one pass, a re-run creates no rows, Σ wallet = Σ
balances."""

from __future__ import annotations

import time

import pytest

from svbg.core.crypto import Crypto, generate_key
from svbg.importers.bedolaga import BedolagaImporter, ImportConfig, StaticPanelReader
from tests.dbkit import CountingDatabase
from tests.importers.bedolaga.synth import ENV, T0, Src, seed_volume
from tests.importers.bedolaga.test_import import counts

pytestmark = pytest.mark.timeout(300)


async def test_volume_apply_then_rerun(target: CountingDatabase, src_dsn: str) -> None:
    src = await Src.connect(src_dsn)
    try:
        panel = await seed_volume(src)
        total_balance = int(await src.conn.fetchval("select sum(balance_kopeks) from users"))
    finally:
        await src.close()
    cfg = ImportConfig(env=ENV, t0=T0, crypto=Crypto([generate_key()]))
    imp = BedolagaImporter(target, src_dsn, panel=StaticPanelReader(panel), config=cfg)

    started = time.monotonic()
    report = await imp.run("apply")
    elapsed = time.monotonic() - started
    assert elapsed < 120, f"import took {elapsed:.1f}s"
    assert report.green, (report.blocking, {k: v for k, v in report.checks.items() if v.get("ok") is False})
    c = await counts(target)
    assert c["users"] == 2522 and c["subscriptions"] == 2109 + 22 and c["legacy_transactions"] == 1352
    assert c["payments"] == 548 and c["jobs"] == 0
    wallet = (await target.raw("select sum(wallet_minor) s from users"))[0]["s"]
    ledger = (await target.raw("select sum(amount_minor) s from wallet_ledger"))[0]["s"]
    assert wallet == ledger == total_balance
    assert report.counts["subscriptions"]["linked"] == 2109

    again = await imp.run("apply")
    assert await counts(target) == c, "re-run creates 0 new rows"
    assert again.green
