"""The importer only reads the panel: through the real Remnawave client against the fake panel every request
is a GET (``users/stream``) and nothing is queued for the writer (06 §4.2, §0 п.3)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from svbg.core.crypto import Crypto, generate_key
from svbg.importers.bedolaga import ApiPanelReader, BedolagaImporter, ImportConfig
from tests.importers.bedolaga.conftest import widen_import_modes
from tests.importers.bedolaga.synth import ENV, T0, Src
from tests.subscriptions.kit import sync_env

pytestmark = pytest.mark.timeout(120)


async def test_shadow_reads_the_panel_and_never_writes(pg_dsn: str, src_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        await widen_import_modes(env.db)
        a = env.panel.add_user(
            username="user_2001",
            telegramId=2001,
            expireAt=T0 + timedelta(days=9),
            activeInternalSquads=[env.squad],
        )
        env.panel.add_user(username="panel_only", telegramId=None, expireAt=T0 + timedelta(days=9))
        src = await Src.connect(src_dsn)
        try:
            await src.add(
                "users",
                id=1,
                telegram_id=2001,
                auth_type="telegram",
                status="active",
                balance_kopeks=100,
                remnawave_id=a["id"],
                created_at=T0 - timedelta(days=9),
            )
            await src.add(
                "subscriptions",
                id=1,
                user_id=1,
                status="active",
                is_trial=False,
                end_date=T0 + timedelta(days=9),
                connected_squads=[env.squad],
                is_daily_paused=False,
                remnawave_short_id="s1",
            )
        finally:
            await src.close()
        imp = BedolagaImporter(
            env.db,
            src_dsn,
            panel=ApiPanelReader(env.api, page_size=1),
            config=ImportConfig(env=ENV, t0=T0, crypto=Crypto([generate_key()])),
        )
        report = await imp.run("shadow")
        assert report.counts["subscriptions"]["linked"] == 1
        assert report.counts["subscriptions"]["unowned_created"] == 1
        assert env.panel.requests and all(r.method == "GET" for r in env.panel.requests)
        assert not await env.jobs()
        await imp.run("shadow")
        assert all(r.method == "GET" for r in env.panel.requests)
        assert not await env.jobs()
