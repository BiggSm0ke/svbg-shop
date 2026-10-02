"""Stage 4 end to end — the Bedolaga migration stand (06 §4.2–4.3) in the assembled application.

A synthetic Bedolaga database (the production schema, every mapped edge case and the owner modules' state:
LTE, IP Guard, referral days) and a fake panel with its accounts; a clean install of the bot with a
**read-only** panel token and ``IMPORT_SOURCE_DSN`` → the app's own shadow pass (importer in ``shadow`` +
С0–С12) is green, and the only non-GET request the panel ever saw is the token probe.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from svbg.core.crypto import Crypto
from svbg.importers.bedolaga import ImportConfig
from svbg.importers.shadow import PROBE_OFFSET
from tests.e2e.conftest import AppEnv, StartApp
from tests.e2e.test_stage2_kit import FAST
from tests.e2e.test_stage3_kit import tg  # noqa: F401 - the ``tg`` fixture
from tests.fakes.remnawave import FakeRemnawave
from tests.importers.bedolaga.conftest import bedolaga_template, src_dsn  # noqa: F401 - fixtures
from tests.importers.bedolaga.synth import SQ_DE, SQ_NL, SQ_TWIN, T0, Src, seed_edge_cases, seed_owner_modules

pytestmark = [pytest.mark.pg, pytest.mark.timeout(300)]

READ_SCOPES = ("users:read", "system:read", "internal-squads:read", "nodes:read")
#: Lookups the panel API makes with POST (they change nothing).
READ_POSTS = frozenset({"/users/resolve"})
#: The owner's decisions of 06 §4.2 п.2 (stored in ``config_meta``, applied on every run).
OVERRIDES = {"skip_panel_user_ids": [601], "skip_subscription_ids": [110], "ack_restricted_user_ids": [12]}


def load_panel(panel: FakeRemnawave, accounts: list[Any]) -> None:
    for name, uuid in (("NL", SQ_NL), ("DE", SQ_DE), ("NL noLTE", SQ_TWIN)):
        panel.add_internal_squad(name, squad_uuid=uuid)
    for u in accounts:
        user = panel.add_user(
            shortUuid=u.short_uuid,
            username=u.username,
            status=u.status,
            trafficLimitBytes=u.traffic_limit_bytes,
            trafficLimitStrategy=u.traffic_limit_strategy,
            expireAt=u.expire_at,
            telegramId=u.telegram_id,
            tag=u.tag,
            hwidDeviceLimit=u.hwid_device_limit,
            activeInternalSquads=[s.uuid for s in u.active_internal_squads],
        )
        del panel.users[user["id"]]
        user["id"] = int(u.id)
        panel.users[int(u.id)] = user


async def test_synthetic_bedolaga_shadow_is_green_and_never_writes_to_the_panel(
    start_app: StartApp,
    app_env: AppEnv,
    src_dsn: str,  # noqa: F811 - the fixture imported above
) -> None:
    src = await Src.connect(src_dsn)
    try:
        scenario = await seed_edge_cases(src)
        await seed_owner_modules(src)
        # the owner settled the paid invoice without a user in Bedolaga before the cut-over (06 §2.4)
        await src.conn.execute("DELETE FROM rollypay_payments WHERE user_id IS NULL")
    finally:
        await src.close()

    async with FakeRemnawave(webhook_enabled=False) as panel:
        load_panel(panel, scenario.panel)
        app_env.write(
            REMNAWAVE_URL=panel.url,
            REMNAWAVE_TOKEN=panel.add_token(READ_SCOPES),
            IMPORT_SOURCE_DSN=src_dsn,
        )
        app = await start_app(**FAST)
        assert app.shadow is not None and app.db is not None, app.missing_modules
        db: Any = app.db
        await db.raw(
            "insert into config_meta (key, value) values ('import.bedolaga.overrides', $1::jsonb)"
            " on conflict (key) do update set value = excluded.value",
            json.dumps(OVERRIDES),
        )
        # Bedolaga's .env and T0 of the snapshot (the stand's import config; no setting for the .env yet)
        crypto = Crypto([app_env.values["SECRET_KEY"]])
        app.import_config = lambda: ImportConfig(env=scenario.env, t0=T0, crypto=crypto)

        report = await app.shadow.run("manual", as_of=T0)
        problems = {c.code: c.problems for c in report.checks if c.status != "ok"}
        assert report.blocked is None, report.blocked
        assert report.probe is not None and report.probe.read_only
        assert report.green, problems
        assert report.ops_total == 0  # what the writer WOULD write: nothing
        counts = report.import_result.get("counts", {})
        assert counts.get("modules") == {"lte": 1, "ip_guard": 1, "referral_days": 1}, counts

        writes = [
            (r.method, r.path) for r in panel.requests if r.method != "GET" and r.path not in READ_POSTS
        ]
        assert writes == [("PATCH", "/users")], writes
        probe = [r for r in panel.requests if r.method == "PATCH"]
        assert probe[0].body == {"id": max(panel.users) + PROBE_OFFSET}
        jobs = await db.raw("select kind from jobs where queue = 'panel' and status in ('ready', 'running')")
        assert jobs == [], jobs
