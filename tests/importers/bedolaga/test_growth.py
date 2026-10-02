"""Daily shadow against a growing source (06 §4.2 п.3): Bedolaga keeps numbering users and subscriptions,
and a panel account read before its subscription reached the dump is claimed by it on the next run."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from svbg.core.crypto import Crypto, generate_key
from svbg.importers.bedolaga import BedolagaImporter, ImportConfig, StaticPanelReader, config_from_env_file
from svbg.importers.bedolaga.plan import ID_GAP
from tests.dbkit import CountingDatabase
from tests.importers.bedolaga.synth import SQ_NL, T0, Scenario, Src, panel_user, seed_edge_cases

pytestmark = pytest.mark.timeout(180)


def _importer(db: CountingDatabase, src_dsn: str, sc: Scenario) -> BedolagaImporter:
    cfg = ImportConfig(
        env=sc.env, t0=T0, crypto=Crypto([generate_key()]), overrides={"skip_panel_user_ids": [601]}
    )
    return BedolagaImporter(db, src_dsn, panel=StaticPanelReader(sc.panel), config=cfg)


@pytest.fixture
async def scenario(src_dsn: str) -> Scenario:
    src = await Src.connect(src_dsn)
    try:
        return await seed_edge_cases(src)
    finally:
        await src.close()


async def test_stand_rows_never_take_the_next_bedolaga_ids(
    target: CountingDatabase, src_dsn: str, scenario: Scenario
) -> None:
    await _importer(target, src_dsn, scenario).run("shadow")
    # A test user on the stand registers between two shadow runs.
    stand = await target.raw("insert into users (telegram_id) values (555) returning id")
    assert stand[0]["id"] > 12 + ID_GAP - 1
    unowned = await target.raw("select id from subscriptions where panel_user_id = 600")
    assert unowned[0]["id"] > 112 + ID_GAP - 1, "placeholders sit above the shadow gap too"

    src = await Src.connect(src_dsn)
    try:  # Bedolaga registers the next user with the next id
        await src.add(
            "users", id=13, telegram_id=1013, auth_type="telegram", status="active", balance_kopeks=700
        )
        await src.add(
            "subscriptions",
            id=113,
            user_id=13,
            status="active",
            is_trial=False,
            end_date=T0 + timedelta(days=30),
            connected_squads=[SQ_NL],
            is_daily_paused=False,
            remnawave_short_id="s113",
        )
    finally:
        await src.close()
    scenario.panel.append(panel_user(513, tg=1013, expire=T0 + timedelta(days=30)))
    report = await _importer(target, src_dsn, scenario).run("shadow")
    assert not report.issue_totals.get("user_id_taken") and not report.issue_totals.get(
        "subscription_id_taken"
    )
    row = await target.raw(
        "select s.id, s.panel_user_id, u.wallet_minor from subscriptions s "
        "join users u on u.id = s.user_id where s.id = 113"
    )
    assert [(r["id"], r["panel_user_id"], r["wallet_minor"]) for r in row] == [(113, 513, 700)]


async def test_unowned_placeholder_is_claimed_by_its_late_subscription(
    target: CountingDatabase, src_dsn: str, scenario: Scenario
) -> None:
    first = await _importer(target, src_dsn, scenario).run("shadow")
    placeholder = await target.raw("select id, user_id from subscriptions where panel_user_id = 600")
    assert placeholder[0]["user_id"] is None

    src = await Src.connect(src_dsn)
    try:  # the subscription of panel account 600 reaches the next dump
        await src.add(
            "users", id=14, telegram_id=1014, auth_type="telegram", status="active", remnawave_id=600
        )
        await src.add(
            "subscriptions",
            id=114,
            user_id=14,
            status="active",
            is_trial=False,
            end_date=T0 + timedelta(days=30),
            connected_squads=[SQ_NL],
            is_daily_paused=False,
            remnawave_short_id="s114",
        )
    finally:
        await src.close()
    report = await _importer(target, src_dsn, scenario).run("shadow")
    assert report.counts["subscriptions"]["unowned_claimed"] == 1
    assert report.blocking == first.blocking, "nothing new blocks the cut-over"
    rows = await target.raw("select id, user_id from subscriptions where panel_user_id = 600")
    assert [(r["id"], r["user_id"]) for r in rows] == [(114, 14)]
    assert not await target.raw("select 1 from subscriptions where id = $1", placeholder[0]["id"])
    assert not await target.raw(
        "select 1 from legacy_id_map where entity = 'panel_user' and old_id = '600'"
    ), "the placeholder's mapping is gone, a later run does not re-create it"
    again = await _importer(target, src_dsn, scenario).run("shadow")
    assert (
        again.get("subscriptions", "unowned_created") == 0
        and again.get("subscriptions", "unowned_claimed") == 0
    )


async def test_placeholder_the_bot_adopted_is_never_dropped(
    target: CountingDatabase, src_dsn: str, scenario: Scenario
) -> None:
    await _importer(target, src_dsn, scenario).run("shadow")
    await target.raw(
        "update subscriptions set user_id = 12 where panel_user_id = 600"
    )  # adopted on the stand
    src = await Src.connect(src_dsn)
    try:
        await src.add(
            "users", id=14, telegram_id=1014, auth_type="telegram", status="active", remnawave_id=600
        )
        await src.add(
            "subscriptions",
            id=114,
            user_id=14,
            status="active",
            is_trial=False,
            end_date=T0 + timedelta(days=30),
            is_daily_paused=False,
            remnawave_short_id="s114",
        )
    finally:
        await src.close()
    report = await _importer(target, src_dsn, scenario).run("shadow")
    assert report.issue_totals["panel_user_taken"] == 1 and "subscription_conflict" in report.blocking
    rows = await target.raw("select user_id from subscriptions where panel_user_id = 600")
    assert [r["user_id"] for r in rows] == [12]


async def test_history_follows_the_source_and_env_file_is_parsed(
    target: CountingDatabase, src_dsn: str, scenario: Scenario, tmp_path: Path
) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "\n".join(f"{k}={v}" for k, v in scenario.env.items())
        + "\nPRICE_30_DAYS=17900  # inline comment\nPRICE_90_DAYS=#commented\n",
        encoding="utf-8",
    )
    cfg = config_from_env_file(env, t0=T0, crypto=Crypto([generate_key()]))
    assert cfg.env["PRICE_30_DAYS"] == "17900" and cfg.env["PRICE_90_DAYS"] == ""
    imp = BedolagaImporter(target, src_dsn, panel=StaticPanelReader(scenario.panel), config=cfg)
    await imp.run("shadow")
    prices = {
        r["days"]: r["amount_minor"] for r in await target.raw("select days, amount_minor from plan_prices")
    }
    assert prices == {30: 17900, 180: 89900, 360: 169900}, "an empty value is not a price"

    src = await Src.connect(src_dsn)
    try:
        await src.conn.execute("update transactions set is_completed = false where id = 5")
    finally:
        await src.close()
    first = await imp.run("shadow")
    assert (
        first.get("misc", "transactions_refreshed") == 1 and first.get("misc", "transactions_imported") == 0
    )
    row = await target.raw("select is_completed from legacy_transactions where legacy_id = '5'")
    assert row[0]["is_completed"] is False
    again = await imp.run("shadow")
    assert again.get("misc", "transactions_refreshed") == 0, "no churn when nothing changed"
