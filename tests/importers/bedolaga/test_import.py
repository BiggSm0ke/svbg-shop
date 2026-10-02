"""Bedolaga importer on a synthetic source built from the owner's real schema (06 §2): every mapped field,
the edge cases, the modes and idempotency."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from svbg.core.crypto import Crypto, generate_key
from svbg.importers.bedolaga import BedolagaImporter, ImportConfig, StaticPanelReader, render_text
from svbg.importers.bedolaga.source import FORBIDDEN_COLUMNS, open_source
from tests.dbkit import CountingDatabase
from tests.importers.bedolaga.synth import SQ_DE, SQ_NL, SQ_TWIN, T0, Scenario, Src, seed_edge_cases

pytestmark = pytest.mark.timeout(180)

COUNTED = (
    "users",
    "subscriptions",
    "subscription_events",
    "trial_grants",
    "wallet_ledger",
    "payments",
    "payment_instances",
    "orders",
    "plans",
    "plan_prices",
    "locations",
    "promocodes",
    "promo_uses",
    "ad_links",
    "ad_link_users",
    "referral_codes",
    "referrals",
    "referral_rewards",
    "legacy_transactions",
    "notification_log",
    "legacy_id_map",
    "jobs",
)


async def counts(db: CountingDatabase) -> dict[str, int]:
    return {t: int((await db.raw(f"select count(*) n from {t}"))[0]["n"]) for t in COUNTED}


def importer(db: CountingDatabase, src_dsn: str, sc: Scenario, **cfg: Any) -> BedolagaImporter:
    config = ImportConfig(
        env=sc.env,
        t0=T0,
        crypto=Crypto([generate_key()]),
        overrides={"skip_panel_user_ids": [601]},
        **cfg,
    )
    return BedolagaImporter(db, src_dsn, panel=StaticPanelReader(sc.panel), config=config)


@pytest.fixture
async def scenario(src_dsn: str) -> Scenario:
    src = await Src.connect(src_dsn)
    try:
        return await seed_edge_cases(src)
    finally:
        await src.close()


async def one(db: CountingDatabase, sql: str, *args: Any) -> dict[str, Any]:
    rows = await db.raw(sql, *args)
    assert len(rows) == 1, (sql, rows)
    return dict(rows[0])


async def test_dry_run_reports_everything_and_writes_nothing(
    target: CountingDatabase, src_dsn: str, scenario: Scenario
) -> None:
    before = await counts(target)
    report = await importer(target, src_dsn, scenario).run("dry_run")
    after = await counts(target)
    assert after == before, "dry run leaves no rows behind"
    run = await one(target, "select * from import_runs")
    assert run["mode"] == "dry_run" and run["status"] == "done" and run["source"] == "bedolaga"
    assert run["report"]["counts"]["users"]["created"] == 11
    assert report.counts["subscriptions"]["linked"] == 8
    assert report.issue_totals["subscription_conflict"] == 1
    assert "Импорт Bedolaga" in render_text(report)


async def test_apply_maps_every_field(target: CountingDatabase, src_dsn: str, scenario: Scenario) -> None:
    report = await importer(target, src_dsn, scenario).run("apply")
    j = report.as_json()

    # --- users: ids kept, deleted without money skipped, blocked, language, name, extras
    ids = [r["id"] for r in await target.raw("select id from users order by id")]
    assert ids == [1, 2, 3, 5, 6, 7, 8, 9, 10, 11, 12]
    u1 = await one(target, "select * from users where id = 1")
    assert (u1["telegram_id"], u1["username"], u1["first_name"], u1["language"]) == (
        1001,
        "alice",
        "Alice A",
        "ru",
    )
    assert u1["last_seen_at"] == T0 - timedelta(hours=1) and u1["bot_blocked_at"] is None
    assert (await one(target, "select language from users where id = 2"))["language"] == "en"
    u3 = await one(target, "select * from users where id = 3")
    assert u3["language"] == "ru" and u3["bot_blocked_at"] is not None
    assert (await one(target, "select bot_blocked_at from users where id = 5"))["bot_blocked_at"] is not None
    assert (await one(target, "select telegram_id from users where id = 6"))["telegram_id"] is None
    extras = {
        int(r["old_id"]): r["data"]
        for r in await target.raw("select old_id, data from legacy_id_map where entity = 'user'")
    }
    assert extras[1]["first_paid_at"] == (T0 - timedelta(days=30)).isoformat()
    assert extras[1]["first_paid_estimated"] is False
    assert extras[11]["first_paid_estimated"] is True, "no transaction → created_at, marked estimated"
    assert extras[1]["first_topup_at"] == (T0 - timedelta(days=20)).isoformat()
    assert extras[12]["personal_discount"]["pct"] == 15
    assert extras[12]["restrictions"] == {"topup": True, "subscription": False, "reason": "abuse"}
    assert report.issue_totals["user_restricted"] == 1
    # the sequence is above MAX(id): the next bot user never takes a Bedolaga id
    new = await target.raw("insert into users (telegram_id) values (77) returning id")
    assert new[0]["id"] > 12

    # --- trial_used_at for everybody with a subscription (С12)
    grants = {
        r["user_id"] for r in await target.raw("select user_id from trial_grants where source = 'import'")
    }
    assert grants == {1, 2, 3, 6, 7, 8, 9, 10, 11, 12}
    assert j["checks"]["C12"]["ok"] is True

    # --- subscriptions
    subs = {r["id"]: dict(r) for r in await target.raw("select * from subscriptions")}
    s1 = subs[101]
    assert s1["user_id"] == 1 and s1["link_state"] == "linked" and s1["panel_user_id"] == 501
    assert s1["paid_until"] == T0 + timedelta(days=20) and s1["desired_expire_at"] == s1["paid_until"]
    assert sorted(s1["desired_squads"]) == sorted([SQ_NL, SQ_DE])
    assert s1["desired_device_limit"] == 7 and s1["extra_devices"] == 2
    assert (
        s1["panel_short_uuid"] == "short501" and s1["subscription_url"] == "https://sub.example.com/short501"
    )
    assert (
        s1["desired_tag"] == "PAID"
        and s1["desired_reset_strategy"] == "MONTH"
        and s1["desired_traffic_bytes"] == 0
    )
    assert s1["cooldowns"] == {"reissue": (T0 - timedelta(days=3)).isoformat()}
    assert s1["plan_id"] is not None and s1["is_trial"] is False
    assert s1["plan_snapshot"]["bedolaga"]["autopay"] is True
    s2 = subs[102]
    assert s2["panel_user_id"] == 502 and s2["is_trial"] and s2["plan_id"] is None
    assert s2["plan_snapshot"]["bedolaga"]["via"] == "short_uuid" and s2["plan_snapshot"]["days"] == 3
    s3 = subs[103]
    assert s3["panel_user_id"] == 503 and s3["plan_snapshot"]["bedolaga"]["via"] == "telegram_id"
    assert s3["paid_until"] == T0 + timedelta(days=12) and s3["overrides"] == {
        "_import_expire": "panel_later"
    }
    assert subs[106]["user_id"] is None and subs[106]["link_state"] == "panel_missing"
    assert subs[106]["paid_until"] == T0 + timedelta(days=9)
    assert (subs[107]["disabled_reason"], subs[107]["desired_status"]) == ("ip_guard", "disabled")
    assert subs[108]["disabled_reason"] == "channel_left"
    assert subs[109]["disabled_reason"] == "admin" and subs[109]["desired_squads"] == [SQ_DE]
    assert report.issue_totals["disabled_admin"] == 1
    s10 = subs[110]
    assert s10["link_state"] == "panel_missing" and s10["panel_user_id"] is None, "conflict is never linked"
    assert subs[111]["desired_squads"] == [SQ_NL] and subs[111]["panel_squads"] == [SQ_TWIN], "twin → base"
    assert subs[112]["paid_until"] < T0
    unowned = [s for s in subs.values() if s["panel_user_id"] == 600]
    assert len(unowned) == 1 and unowned[0]["user_id"] is None and unowned[0]["desired_squads"] == [SQ_NL]
    assert unowned[0]["plan_snapshot"]["origin"] == "panel" and unowned[0]["id"] > 112
    assert not [s for s in subs.values() if s["panel_user_id"] == 601], "owner skipped the service account"
    assert all(SQ_TWIN not in s["desired_squads"] for s in subs.values())
    assert j["checks"]["C5"]["ok"] is True

    # --- catalog
    plan = await one(target, "select * from plans")
    assert plan["code"] == "bedolaga_classic" and plan["enabled"] and plan["device_limit"] == 5
    assert plan["device_addon"] == {"price_minor": 1900, "per_days": 30, "currency": "RUB", "max_devices": 15}
    assert (
        plan["panel_tag"] == "PAID" and plan["reset_strategy"] == "MONTH" and plan["squads"] == [SQ_NL, SQ_DE]
    )
    prices = {r["days"]: r["amount_minor"] for r in await target.raw("select * from plan_prices")}
    assert prices == {30: 17900, 90: 49900, 180: 89900, 360: 169900}, ".env wins; 14 days not carried over"
    locs = {r["squad_uuid"]: dict(r) for r in await target.raw("select * from locations")}
    assert (
        locs[SQ_NL]["flag"] == "🇳🇱"
        and locs[SQ_DE]["title"] == {"ru": "Германия"}
        and locs[SQ_DE]["sort"] == 2
    )
    assert report.issue_totals["location_paid"] == 1

    # --- wallet: one opening per user with money, Σ = Σ
    ledger = {
        r["user_id"]: dict(r)
        for r in await target.raw("select * from wallet_ledger where reason = 'import_opening'")
    }
    assert {k: v["amount_minor"] for k, v in ledger.items()} == {1: 15000, 3: 500, 5: 300}
    assert all(v["ref_type"] == "import_run" and v["ref_id"] == str(report.run_id) for v in ledger.values())
    total = await one(target, "select sum(wallet_minor) s from users")
    assert total["s"] == 15800
    assert j["checks"]["C2"]["ok"] is True

    # --- payments
    pays = {
        (r["metadata"]["legacy"]["table"], r["metadata"]["legacy"]["id"]): dict(r)
        for r in await target.raw(
            "select p.*, i.slug from payments p join payment_instances i on i.id = p.instance_id"
        )
    }
    assert all(p["is_imported"] for p in pays.values())
    rp1 = pays[("rollypay_payments", 1)]
    assert (rp1["slug"], rp1["status"], rp1["external_id"], rp1["merchant_ref"]) == (
        "rollypay",
        "paid",
        "RP-1",
        "rp1001_aaaaaa",
    )
    assert rp1["paid_amount_minor"] == 17900 and rp1["order_id"] is None
    rp2 = pays[("rollypay_payments", 2)]
    assert rp2["status"] == "pending" and rp2["order_id"] is not None and rp2["next_check_at"] == T0
    order = await one(target, "select * from orders where id = $1", rp2["order_id"])
    assert (order["kind"], order["status"], order["total_minor"], order["user_id"]) == (
        "topup",
        "awaiting_payment",
        20000,
        2,
    )
    assert pays[("rollypay_payments", 3)]["status"] == "pending", "expired ≤ 48 h is still alive"
    assert pays[("rollypay_payments", 4)]["status"] == "expired"
    assert pays[("rollypay_payments", 5)]["status"] == "canceled"
    assert ("rollypay_payments", 6) not in pays, "a paid invoice without a user → reported"
    assert report.issue_totals["payment_user_missing"] == 1
    cb1 = pays[("cryptobot_payments", 1)]
    assert (cb1["status"], cb1["amount_minor"], cb1["currency"], cb1["external_id"]) == (
        "paid",
        50000,
        "RUB",
        "CB-1",
    )
    assert cb1["metadata"]["legacy"]["asset"] == "USDT" and cb1["metadata"]["legacy"]["amount"] == "5.2"
    cb2 = pays[("cryptobot_payments", 2)]
    assert cb2["status"] == "pending" and cb2["order_id"] is not None
    assert (cb2["amount_minor"], cb2["currency"]) == (310, "USDT"), "the invoice lives in its asset"
    cb2_order = await one(target, "select * from orders where id = $1", cb2["order_id"])
    assert (cb2_order["total_minor"], cb2_order["currency"]) == (30000, "RUB"), "rubles of the payload"
    assert (cb2["poll_plan"], cb2["next_check_at"]) == ("domain", T0), "apply arms the reconciler"
    assert pays[("cryptobot_payments", 3)]["status"] == "expired"
    pl = pays[("platega_payments", 1)]
    assert (pl["slug"], pl["status"], pl["external_id"]) == ("platega_legacy", "paid", "PL-1")
    st = pays[("transactions", 2)]
    assert (st["slug"], st["currency"], st["amount_minor"], st["external_id"]) == (
        "stars",
        "XTR",
        100,
        "stars-charge-1",
    )
    assert st["metadata"]["credited_minor"] == 10000
    insts = {r["slug"]: r for r in await target.raw("select * from payment_instances")}
    assert set(insts) == {"rollypay", "cryptobot", "platega_legacy", "stars"}
    assert not any(i["enabled"] for i in insts.values())
    assert j["checks"]["C3"]["ok"] is True and j["checks"]["C4"]["ok"] is True
    assert j["checks"]["C4"]["live"] == 3

    # --- promo
    promos = {r["code"]: dict(r) for r in await target.raw("select * from promocodes")}
    assert set(promos) == {"Bonus100", "DAYS7", "Sale20", "COMBO"}
    assert promos["Bonus100"]["kind"] == "wallet" and promos["Bonus100"]["amount_minor"] == 10000
    assert promos["DAYS7"]["kind"] == "days" and promos["DAYS7"]["max_uses"] is None
    assert promos["Sale20"]["kind"] == "percent" and promos["Sale20"]["percent"] == 20
    assert promos["Sale20"]["pending_hours"] == 48 and promos["Sale20"]["expires_at"] == T0 + timedelta(
        days=30
    )
    assert promos["COMBO"]["kind"] == "wallet_days" and promos["COMBO"]["new_users_only"] is True
    assert promos["DAYS7"]["uses"] == 2 and promos["Bonus100"]["uses"] == 1
    assert report.issue_totals["promo_not_imported"] == 1
    assert (await one(target, "select count(*) n from promo_uses where source = 'import'"))["n"] == 3

    # --- campaigns: code as is, first touch
    links = {r["code"]: dict(r) for r in await target.raw("select * from ad_links")}
    assert set(links) == {"tgads_oct", "promoX"} and links["tgads_oct"]["owner_user_id"] == 1
    assert links["tgads_oct"]["bonus"]["type"] == "balance" and links["promoX"]["enabled"] is False
    regs = {r["user_id"]: r["ad_link_id"] for r in await target.raw("select * from ad_link_users")}
    assert regs == {3: links["tgads_oct"]["id"], 2: links["tgads_oct"]["id"], 12: links["promoX"]["id"]}

    # --- referral
    assert {r["code"] for r in await target.raw("select code from referral_codes")} == {
        "refAAAA1111",
        "refBBBB2222",
    }
    pair = await one(target, "select * from referrals")
    assert (pair["referred_user_id"], pair["referrer_id"], pair["source"]) == (2, 1, "import")
    rewards = {r["side"]: dict(r) for r in await target.raw("select * from referral_rewards")}
    # The money pair is legacy; its inviter side also has a days marker, so the referral-days stage makes
    # it granted (the days were given; the money stays in amount_minor).
    assert set(rewards) == {"inviter", "invitee"}
    assert (rewards["inviter"]["status"], rewards["invitee"]["status"]) == ("granted", "legacy")
    assert rewards["inviter"]["user_id"] == 1 and rewards["inviter"]["amount_minor"] == 5000
    assert rewards["invitee"]["user_id"] == 2
    assert not await target.raw("select 1 from wallet_ledger where reason not in ('import_opening')"), (
        "referral money is never credited again (it is in the balance)"
    )

    # --- history and reminders
    txs = {int(r["legacy_id"]): dict(r) for r in await target.raw("select * from legacy_transactions")}
    assert len(txs) == 6 and txs[3]["pair_key"] == "tc_ABC123" and txs[4]["pair_key"] == "tc_ABC123"
    assert txs[3]["counts_as_revenue"] is True and txs[4]["counts_as_revenue"] is False
    assert txs[5]["counts_as_revenue"] is True and txs[4]["amount_minor"] == -17900
    log = {(r["target"], r["kind"]): dict(r) for r in await target.raw("select * from notification_log")}
    assert set(log) == {("sub:102", "expiring_72h"), ("sub:108", "trial_ending")}, (
        "old reminders are not seeded"
    )
    assert log[("sub:102", "expiring_72h")]["anchor"] == str(int((T0 + timedelta(days=2)).timestamp()))
    assert all(r["status"] == "sent" for r in log.values())
    assert j["checks"]["C10"]["ok"] is True and j["checks"]["C1"]["ok"] is True, j["checks"]["C1"]

    # --- the panel is never touched, nothing is queued
    assert (await counts(target))["jobs"] == 0
    applied = await one(target, "select value from config_meta where key = 'import.bedolaga.applied'")
    assert applied["value"]["run_id"] == report.run_id


async def test_rerun_is_idempotent_and_follows_the_source(
    target: CountingDatabase, src_dsn: str, scenario: Scenario
) -> None:
    await importer(target, src_dsn, scenario).run("shadow")
    first = await counts(target)
    again = await importer(target, src_dsn, scenario).run("shadow")
    second = await counts(target)
    assert second == first, "a re-run creates nothing"
    assert again.counts["subscriptions"].get("created", 0) == 0 and again.get("wallet", "openings") == 0

    # The source moves on (shadow is repeated daily on a fresh dump).
    src = await Src.connect(src_dsn)
    try:
        await src.conn.execute("update users set balance_kopeks = 9000 where id = 1")
        await src.conn.execute("update users set balance_kopeks = 700 where id = 2")
        await src.conn.execute(
            "update rollypay_payments set status = 'paid', is_paid = true, paid_at = $1 where id = 2", T0
        )
        await src.conn.execute(
            "update subscriptions set end_date = $1 where id = 103", T0 + timedelta(days=40)
        )
    finally:
        await src.close()
    scenario.panel[2] = scenario.panel[2].__class__(
        **{
            **{f: getattr(scenario.panel[2], f) for f in scenario.panel[2].__struct_fields__},
            "expire_at": T0 + timedelta(days=40),
        }
    )
    # The bot changed one subscription by itself: the import must not fight it.
    await target.raw("update subscriptions set paid_until = paid_until + interval '1 day' where id = 101")
    third = await importer(target, src_dsn, scenario).run("shadow")
    w = {
        r["id"]: r["wallet_minor"]
        for r in await target.raw("select id, wallet_minor from users where id in (1, 2)")
    }
    assert w == {1: 9000, 2: 700}
    adj = await target.raw(
        "select user_id, amount_minor from wallet_ledger where reason = 'import_adjust' order by user_id"
    )
    assert [(r["user_id"], r["amount_minor"]) for r in adj] == [(1, -6000)]
    assert (
        await one(
            target, "select count(*) n from wallet_ledger where reason = 'import_opening' and user_id = 2"
        )
    )["n"] == 1
    paid = await one(
        target,
        "select p.status, o.status ostatus from payments p join orders o on o.id = p.order_id "
        "where p.external_id = 'RP-2'",
    )
    assert paid == {"status": "paid", "ostatus": "canceled"}, (
        "money came with the balance; the order is closed"
    )
    assert (await one(target, "select paid_until from subscriptions where id = 103"))[
        "paid_until"
    ] == T0 + timedelta(days=40)
    assert third.issue_totals["subscription_changed_locally"] == 1
    assert third.checks["C2"]["ok"] is True


async def test_shadow_and_apply_need_the_panel(target: CountingDatabase, src_dsn: str) -> None:
    with pytest.raises(ValueError, match="panel"):
        await BedolagaImporter(target, src_dsn).run("shadow")
    report = await BedolagaImporter(target, src_dsn, config=ImportConfig(t0=T0)).run("dry_run")
    assert report.finished and report.checks["C5"]["ok"] is None


async def test_conflicting_target_rows_are_reported_not_overwritten(
    target: CountingDatabase, src_dsn: str, scenario: Scenario
) -> None:
    await target.raw("insert into users (id, telegram_id) values (2, 424242)")  # a stand user took id 2
    await target.raw("insert into users (id, telegram_id) values (500, 1003)")  # and somebody else is tg 1003
    src = await Src.connect(src_dsn)
    try:
        await src.conn.execute("update users set balance_kopeks = -100 where id = 5")
    finally:
        await src.close()
    report = await importer(target, src_dsn, scenario).run("apply")
    assert report.issue_totals["user_id_taken"] == 1 and report.issue_totals["user_telegram_taken"] == 1
    assert report.issue_totals["negative_balance"] == 1
    assert not report.green and report.blocking
    assert (await one(target, "select telegram_id from users where id = 2"))["telegram_id"] == 424242
    assert not await target.raw("select 1 from wallet_ledger where user_id = 5")


async def test_secrets_are_never_read(src_dsn: str) -> None:
    async with open_source(src_dsn) as src:
        with pytest.raises(ValueError, match="secret"):
            await src.rows("users", ["id", "vless_uuid"])
        await src.rows("users", ["id", "telegram_id"])
        assert not any(col in sql for sql in src.statements for col in FORBIDDEN_COLUMNS)
        with pytest.raises(Exception, match="read-only"):
            await src.fetch("insert into system_settings (id, key) values (1, 'x')")


async def test_importer_reads_no_secret_column(
    target: CountingDatabase, src_dsn: str, scenario: Scenario
) -> None:
    seen: list[str] = []
    real = open_source

    def spy(dsn: str) -> Any:
        cm = real(dsn)

        class Wrap:
            async def __aenter__(self) -> Any:
                self.src = await cm.__aenter__()
                return self.src

            async def __aexit__(self, *exc: Any) -> Any:
                seen.extend(self.src.statements)
                return await cm.__aexit__(*exc)

        return Wrap()

    imp = BedolagaImporter(
        target,
        lambda: spy(src_dsn),
        panel=StaticPanelReader(scenario.panel),
        config=ImportConfig(env=scenario.env, t0=T0, crypto=Crypto([generate_key()])),
    )
    await imp.run("dry_run")
    assert seen
    assert not [sql for sql in seen for col in FORBIDDEN_COLUMNS if col in sql]
