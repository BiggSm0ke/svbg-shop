"""Importer review follow-ups (06 §2.2–2.8, §4.9) on the synthetic source: who polls a live invoice and
when, late webhooks of imported invoices through the real payment core (exactly one credit), a source
``paid`` winning over a local status, unclaimed panel accounts following the panel, skipped subscriptions
keeping their panel account, orphan wallets, the IP Guard hold, the required-channel membership and the
blocking problems that keep the cut-over gate red."""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from svbg.billing.crediting import Crediting
from svbg.core.crypto import Crypto, generate_key
from svbg.importers.bedolaga import BedolagaImporter, ImportConfig, StaticPanelReader
from svbg.payments.core import Outcome, PaymentCore
from svbg.payments.providers.cryptobot import CryptoBot
from svbg.payments.providers.rollypay import RollyPay
from svbg.payments.registry import InstanceRegistry, InstanceSpec, LiveInstance, ProviderCatalog
from svbg.payments.testkit import CountingHttp
from svbg.remnawave.models import PanelUser
from svbg.sdk import PaymentState, ProviderStatus, WebhookRequest
from tests.dbkit import CountingDatabase
from tests.fakes.cryptobot import API_TOKEN, iso
from tests.fakes.cryptobot import sign as cb_sign
from tests.fakes.rollypay import API_KEY, FAKE_BASE_URL, SIGNING_SECRET
from tests.importers.bedolaga.synth import SQ_DE, SQ_NL, T0, Scenario, Src, seed_edge_cases

pytestmark = pytest.mark.timeout(180)

CRYPTO = Crypto([generate_key()])


def importer(db: CountingDatabase, src_dsn: str, sc: Scenario, **overrides: Any) -> BedolagaImporter:
    rules: dict[str, Any] = {"skip_panel_user_ids": [601], **overrides}
    config = ImportConfig(env=sc.env, t0=T0, crypto=CRYPTO, overrides=rules)
    return BedolagaImporter(db, src_dsn, panel=StaticPanelReader(sc.panel), config=config)


@pytest.fixture
async def scenario(src_dsn: str) -> Scenario:
    src = await Src.connect(src_dsn)
    try:
        return await seed_edge_cases(src)
    finally:
        await src.close()


async def src_exec(src_dsn: str, sql: str, *args: Any) -> None:
    src = await Src.connect(src_dsn)
    try:
        await src.conn.execute(sql, *args)
    finally:
        await src.close()


async def one(db: CountingDatabase, sql: str, *args: Any) -> dict[str, Any]:
    rows = await db.raw(sql, *args)
    assert len(rows) == 1, (sql, rows)
    return dict(rows[0])


def changed(user: PanelUser, **values: Any) -> PanelUser:
    fields = {f: getattr(user, f) for f in user.__struct_fields__}
    return user.__class__(**{**fields, **values})


def panel_index(sc: Scenario, pid: int) -> int:
    return next(i for i, u in enumerate(sc.panel) if u.id == pid)


async def payment(db: CountingDatabase, external_id: str) -> dict[str, Any]:
    return await one(db, "select * from payments where external_id = $1", external_id)


# ------------------------------------------------------------------------------- the payment core stack


@dataclass
class Stack:
    core: PaymentCore
    registry: InstanceRegistry
    cryptobot: LiveInstance
    rollypay: LiveInstance


@pytest.fixture
async def stack(target: CountingDatabase) -> AsyncIterator[Stack]:
    """The production instances, already configured (06 §4.1 п.6) — the importer reuses them by slug."""
    registry = InstanceRegistry(
        target,
        CRYPTO,
        ProviderCatalog([CryptoBot, RollyPay]),
        public_url=lambda: "https://shop.example",
        http_factory=lambda _proxy: CountingHttp(),
    )
    cb = await registry.save(
        InstanceSpec(slug="cryptobot", provider="cryptobot", enabled=True, config={"api_token": API_TOKEN})
    )
    rp = await registry.save(
        InstanceSpec(
            slug="rollypay",
            provider="rollypay",
            enabled=True,
            config={"api_key": API_KEY, "signing_secret": SIGNING_SECRET, "base_url": FAKE_BASE_URL},
        )
    )
    core = PaymentCore(target, registry, has_domain=lambda: True)
    core.on_paid(Crediting(config=lambda: {}).on_paid)
    try:
        yield Stack(core, registry, cb, rp)
    finally:
        await core.drain()
        await registry.close()


def cryptobot_paid(invoice_id: str, *, asset: str, amount: str, update_id: int) -> WebhookRequest:
    """A signed Crypto Pay ``invoice_paid`` of a legacy invoice priced in its asset."""
    at = datetime.now(UTC)
    body = json.dumps(
        {
            "update_id": update_id,
            "update_type": "invoice_paid",
            "request_date": iso(at),
            "payload": {
                "invoice_id": invoice_id,
                "status": "paid",
                "currency_type": "crypto",
                "asset": asset,
                "amount": amount,
                "paid_asset": asset,
                "paid_at": iso(at),
                "payload": "balance_2_30000",
            },
        }
    ).encode()
    return WebhookRequest(body=body, headers={"crypto-pay-api-signature": cb_sign(API_TOKEN, body)})


def rub_paid(external_id: str, rubles: str) -> ProviderStatus:
    return ProviderStatus(
        state=PaymentState.PAID, external_id=external_id, amount=Decimal(rubles), currency="RUB"
    )


async def wallet(db: CountingDatabase, user_id: int) -> tuple[int, list[tuple[str, int]]]:
    w = (await one(db, "select wallet_minor from users where id = $1", user_id))["wallet_minor"]
    rows = await db.raw(
        "select reason, amount_minor from wallet_ledger where user_id = $1 order by id", user_id
    )
    return int(w), [(r["reason"], int(r["amount_minor"])) for r in rows]


# --------------------------------------------------------------------------------------------- tests


async def test_shadow_never_schedules_polls_apply_arms_the_live_invoices(
    target: CountingDatabase, src_dsn: str, scenario: Scenario
) -> None:
    """Finding 1: before T0 the invoice is Bedolaga's — the stand's poller (it reads disabled instances too)
    must not ask the cash desk; ``apply`` arms exactly the live ones."""
    report = await importer(target, src_dsn, scenario).run("shadow")
    pending = await target.raw(
        "select external_id, poll_plan, next_check_at from payments where status = 'pending'"
    )
    assert {r["external_id"] for r in pending} == {"RP-2", "RP-3", "CB-2"}
    assert all(r["poll_plan"] is None and r["next_check_at"] is None for r in pending)

    # A schedule left by an older build is removed by the next shadow run.
    await target.raw(
        "update payments set poll_plan = 'domain', next_check_at = $1 where external_id = 'RP-2'", T0
    )
    report = await importer(target, src_dsn, scenario).run("shadow")
    assert report.get("payments", "reconciler_disarmed") == 1
    assert (await payment(target, "RP-2"))["next_check_at"] is None

    report = await importer(target, src_dsn, scenario).run("apply")
    assert report.get("payments", "reconciler_armed") == 3
    armed = {
        r["external_id"]: (r["poll_plan"], r["next_check_at"])
        for r in await target.raw("select external_id, poll_plan, next_check_at from payments")
        if r["poll_plan"] is not None or r["next_check_at"] is not None
    }
    assert armed == {k: ("domain", T0) for k in ("RP-2", "RP-3", "CB-2")}
    assert (await target.raw("select count(*) n from jobs"))[0]["n"] == 0


async def test_late_reports_of_imported_invoices_credit_exactly_once(
    target: CountingDatabase, src_dsn: str, scenario: Scenario, stack: Stack
) -> None:
    """Findings 5 and 10 (06 §4.9): after T0 a late «paid» of an old invoice goes through the real core —
    a live invoice is credited once with the rubles of its order (the CryptoBot one is paid in USDT), an
    imported ``paid`` is never credited again, a late payment of an expired one is credited once."""
    report = await importer(target, src_dsn, scenario).run("apply")
    assert report.finished
    assert await wallet(target, 2) == (0, [])

    # CryptoBot, priced in the asset: «3.1 USDT» matches the imported invoice exactly.
    status = await stack.core.handle_webhook(
        stack.cryptobot.id,
        stack.cryptobot.webhook_token,
        cryptobot_paid("CB-2", asset="USDT", amount="3.1", update_id=1),
    )
    assert status.status == 200
    cb2 = await payment(target, "CB-2")
    assert (cb2["status"], cb2["paid_amount_minor"], cb2["paid_currency"]) == ("paid", 310, "USDT")
    assert await wallet(target, 2) == (30000, [("topup", 30000)])
    order = await one(target, "select status from orders where id = $1", cb2["order_id"])
    assert order["status"] == "credited"
    # The provider repeats it (another update id → another body): no second credit.
    await stack.core.handle_webhook(
        stack.cryptobot.id,
        stack.cryptobot.webhook_token,
        cryptobot_paid("CB-2", asset="USDT", amount="3.1", update_id=2),
    )
    assert await wallet(target, 2) == (30000, [("topup", 30000)])

    # RollyPay: the live one is credited through its order, once; the reconciler's re-read changes nothing.
    for _ in range(2):
        await stack.core.apply_statuses(stack.rollypay, [rub_paid("RP-2", "200.00")], source="poll")
    assert await wallet(target, 2) == (50000, [("topup", 30000), ("topup", 20000)])

    # An invoice paid before T0 (its money is inside the opening balance): ignored.
    before = await wallet(target, 1)
    res = await stack.core.apply_statuses(stack.rollypay, [rub_paid("RP-1", "179.00")], source="webhook")
    assert res[0].outcome is Outcome.IGNORED
    assert await wallet(target, 1) == before

    # An expired invoice (older than 48 h) paid after T0: Bedolaga never credited it — credited here once.
    res = await stack.core.apply_statuses(stack.rollypay, [rub_paid("RP-4", "300.00")], source="webhook")
    assert res[0].outcome is Outcome.APPLIED and res[0].credited
    res = await stack.core.apply_statuses(stack.rollypay, [rub_paid("RP-4", "300.00")], source="webhook")
    assert res[0].outcome is Outcome.IGNORED
    assert await wallet(target, 3) == (30500, [("import_opening", 500), ("payment_credit", 30000)])

    # Σ wallet_ledger = wallet_minor for everybody.
    bad = await target.raw(
        "select u.id from users u where u.wallet_minor <> "
        "coalesce((select sum(amount_minor) from wallet_ledger l where l.user_id = u.id), 0)"
    )
    assert bad == []


async def test_source_paid_wins_over_a_local_status(
    target: CountingDatabase, src_dsn: str, scenario: Scenario, stack: Stack
) -> None:
    """Finding 6: the stand expired a live invoice, then Bedolaga got the late «paid» and credited its
    balance: the next run settles the payment (no credit), a later report finds it paid — nothing twice."""
    await importer(target, src_dsn, scenario).run("shadow")
    await target.raw("update payments set status = 'expired' where external_id = 'RP-2'")
    await target.raw("update payments set status = 'mismatch' where external_id = 'RP-3'")
    await src_exec(src_dsn, "update rollypay_payments set status = 'paid', is_paid = true where id in (2, 3)")
    await src_exec(src_dsn, "update users set balance_kopeks = 20000 where id = 2")
    report = await importer(target, src_dsn, scenario).run("shadow")

    rp2 = await payment(target, "RP-2")
    assert (rp2["status"], rp2["paid_amount_minor"]) == ("paid", 20000)
    order = await one(target, "select status from orders where id = $1", rp2["order_id"])
    assert order["status"] == "canceled", "the money came with the balance"
    assert report.issue_totals["payment_settled_from_source"] == 1
    # A local «mismatch» against a source «paid» needs the owner: blocking.
    assert (await payment(target, "RP-3"))["status"] == "mismatch"
    assert report.blocking.get("payment_paid_conflict") == 1 and not report.green

    res = await stack.core.apply_statuses(stack.rollypay, [rub_paid("RP-2", "200.00")], source="webhook")
    assert res[0].outcome is Outcome.IGNORED
    assert await wallet(target, 2) == (20000, [("import_opening", 20000)])


async def test_unclaimed_accounts_follow_the_panel_until_the_bot_changes_them(
    target: CountingDatabase, src_dsn: str, scenario: Scenario
) -> None:
    """Finding 4: panel-only clients renew through the site past the bot — every run follows the panel."""
    await importer(target, src_dsn, scenario).run("shadow")
    first = await one(target, "select * from subscriptions where panel_user_id = 600")
    i = panel_index(scenario, 600)
    later = T0 + timedelta(days=60)
    scenario.panel[i] = changed(scenario.panel[i], expire_at=later, hwid_device_limit=7, status="DISABLED")
    report = await importer(target, src_dsn, scenario).run("shadow")
    row = await one(target, "select * from subscriptions where panel_user_id = 600")
    assert row["id"] == first["id"]
    assert (row["paid_until"], row["desired_expire_at"], row["panel_expire_at"]) == (later, later, later)
    assert (row["desired_device_limit"], row["panel_device_limit"]) == (7, 7)
    assert row["overrides"] == {"status": "DISABLED"} and row["desired_status"] == "active"
    assert report.get("subscriptions", "unowned_updated") == 1
    assert report.get("subscriptions", "unowned_expire_moved") == 1
    again = await importer(target, src_dsn, scenario).run("shadow")
    assert again.get("subscriptions", "unowned_unchanged") == 1 and not again.get(
        "subscriptions", "unowned_updated"
    )

    # The bot took the row over (an admin edit): the import leaves it alone and says so.
    await target.raw("update subscriptions set desired_device_limit = 9 where panel_user_id = 600")
    scenario.panel[i] = changed(scenario.panel[i], expire_at=later + timedelta(days=30))
    report = await importer(target, src_dsn, scenario).run("shadow")
    row = await one(target, "select * from subscriptions where panel_user_id = 600")
    assert (row["desired_device_limit"], row["paid_until"]) == (9, later)
    assert report.issue_totals["unowned_changed_locally"] == 1


async def test_a_skipped_subscription_keeps_its_panel_account(
    target: CountingDatabase, src_dsn: str, scenario: Scenario
) -> None:
    """Finding 13: neither the owner's skip nor a user conflict turns the account into an unclaimed one."""
    await target.raw("insert into users (id, telegram_id) values (2, 424242)")  # sub 102 loses its user
    report = await importer(target, src_dsn, scenario, skip_subscription_ids=[101]).run("apply")
    owned = {r["panel_user_id"] for r in await target.raw("select panel_user_id from subscriptions")}
    assert 501 not in owned and 502 not in owned
    pairs = {
        (e["subscription_id"], e["panel_user_id"], e["why"])
        for e in report.issues["skipped_subscription_panel"]
    }
    assert pairs == {(101, 501, "skip_subscription_ids"), (102, 502, "subscription_user_missing")}
    assert 600 in owned, "a real panel-only account is still imported"


async def test_a_user_left_out_later_gets_his_import_entries_reversed(
    target: CountingDatabase, src_dsn: str, scenario: Scenario
) -> None:
    """Finding 12: user 5 (deleted, 3 ₽) was carried over; the money is gone in Bedolaga and so is the
    reason to import him — his opening must not stay on the wallet."""
    await importer(target, src_dsn, scenario).run("shadow")
    assert (await wallet(target, 5)) == (300, [("import_opening", 300)])
    await src_exec(src_dsn, "update users set balance_kopeks = 0 where id = 5")
    report = await importer(target, src_dsn, scenario).run("shadow")
    assert report.get("users", "skipped_deleted") == 2
    assert await wallet(target, 5) == (0, [("import_opening", 300), ("import_adjust", -300)])
    assert report.get("wallet", "orphans") == 1 and report.checks["C2"]["ok"] is True


async def test_ip_guard_hold_and_missing_modules_block(
    target: CountingDatabase, src_dsn: str, scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 9: an active block freezes the term (06 §2.8) even without the module; a source whose state
    only a module carries over keeps the gate red while the module is missing."""
    for name in ("lte", "ip_guard", "referral_days"):  # «not installed»
        monkeypatch.setitem(sys.modules, f"svbg.importers.bedolaga.{name}", None)
    # ``create_schema`` registers the LTE / IP Guard / referral tables now: drop them to model an install
    # without those modules.
    await target.raw(
        "DO $$ DECLARE t text; BEGIN FOR t IN SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
        "AND (tablename LIKE 'lte\\_%' OR tablename LIKE 'ip\\_guard\\_%') "
        "LOOP EXECUTE format('DROP TABLE IF EXISTS %I CASCADE', t); END LOOP; END $$"
    )
    report = await importer(target, src_dsn, scenario).run("apply")
    s = await one(target, "select * from subscriptions where id = 107")
    blocked_at = T0 - timedelta(hours=5)
    assert (s["hold_kind"], s["hold_since"], s["disabled_reason"]) == ("ip_guard", blocked_at, "ip_guard")
    assert s["hold_frozen_seconds"] == int((T0 + timedelta(days=15) - blocked_at).total_seconds()) + 60
    assert s["paid_until"] == T0 + timedelta(days=15), "paid_until is not moved"
    others = await target.raw(
        "select count(*) n from subscriptions where id <> 107 and hold_kind is not null"
    )
    assert others[0]["n"] == 0
    missing = {e["module"]: e["state"] for e in report.issues["module_missing"]}
    assert set(missing) == {"ip_guard", "lte", "referral_days"}
    assert missing["ip_guard"] == ["активные блоки IP Guard: 1"]
    assert missing["lte"] == ["двойники сквадов LTE: 1"]
    assert report.blocking["module_missing"] == 3 and not report.green

    # The block is lifted in Bedolaga: the next run clears the hold.
    await src_exec(src_dsn, "update ip_guard_blocks set status = 'closed'")
    await importer(target, src_dsn, scenario).run("apply")
    s = await one(target, "select * from subscriptions where id = 107")
    assert (s["hold_kind"], s["hold_frozen_seconds"], s["disabled_reason"]) == (None, 0, "admin")


async def test_channel_membership_reads_the_required_channel_only(
    target: CountingDatabase, src_dsn: str, scenario: Scenario
) -> None:
    """Finding 17: the cache has a row per channel — only the required channel decides «channel_left»; an
    active trial disabled in the panel is listed for a getChatMember re-check."""
    src = await Src.connect(src_dsn)
    try:
        await src.add("required_channels", id=1, channel_id="-100", is_active=True)
        await src.add("user_channel_subscriptions", id=2, telegram_id=1008, channel_id="-200", is_member=True)
    finally:
        await src.close()
    report = await importer(target, src_dsn, scenario).run("dry_run")
    assert {e["subscription_id"] for e in report.issues["disabled_admin"]} == {109}
    recheck = report.issues["trial_membership_unverified"]
    assert [(e["subscription_id"], e["reason"]) for e in recheck] == [(108, "channel_left")]

    await src_exec(src_dsn, "update required_channels set channel_id = '-200'")
    report = await importer(target, src_dsn, scenario).run("dry_run")
    assert {e["subscription_id"] for e in report.issues["disabled_admin"]} == {108, 109}


async def test_restricted_users_block_until_the_owner_acknowledges(
    target: CountingDatabase, src_dsn: str, scenario: Scenario
) -> None:
    """Finding 14: a Bedolaga restriction has no column in the bot yet — the gate stays red until the
    owner applied it by hand (``ack_restricted_user_ids``); a live personal discount is reported."""
    report = await importer(target, src_dsn, scenario).run("dry_run")
    assert report.blocking["user_restricted"] == 1
    assert [e["user_id"] for e in report.issues["personal_discount_not_applied"]] == [12]
    report = await importer(target, src_dsn, scenario, ack_restricted_user_ids=[12]).run("dry_run")
    assert "user_restricted" not in report.blocking
    assert report.issue_totals["user_restricted_acknowledged"] == 1


async def test_trial_locations_become_the_trial_plan_squads(
    target: CountingDatabase, src_dsn: str, scenario: Scenario
) -> None:
    """Finding 15: the trial is the one ``is_trial`` plan of the bot (``CatalogService.trial``) — no second
    source of truth is created; the location flag ``is_trial_eligible`` becomes that plan's squads, never
    over a change made on the stand."""
    report = await importer(target, src_dsn, scenario).run("shadow")
    assert report.issue_totals.get("trial_plan_missing") == 1
    assert (await one(target, "select count(*) n from plans where is_trial"))["n"] == 0

    from svbg.catalog.repo import create_plan

    async with target.tx() as conn:
        trial = await create_plan(
            conn, name="Пробный", code="trial", is_trial=True, enabled=True, squads=[SQ_DE]
        )
    report = await importer(target, src_dsn, scenario).run("shadow")
    assert report.get("catalog", "trial_squads_set") == 1
    row = await one(target, "select squads, version from plans where id = $1", trial)
    assert (json.loads(row["squads"]) if isinstance(row["squads"], str) else row["squads"]) == [SQ_NL]
    report = await importer(target, src_dsn, scenario).run("shadow")
    assert report.get("catalog", "trial_squads_kept") == 1
    assert (await one(target, "select version from plans where id = $1", trial))["version"] == row["version"]

    await target.raw("update plans set squads = $2::jsonb where id = $1", trial, json.dumps([SQ_NL, SQ_DE]))
    report = await importer(target, src_dsn, scenario).run("shadow")
    assert [e["plan_id"] for e in report.issues["trial_squads_changed"]] == [trial]
    assert "trial_squads_changed" not in report.blocking
    squads = (await one(target, "select squads from plans where id = $1", trial))["squads"]
    assert (json.loads(squads) if isinstance(squads, str) else squads) == [SQ_NL, SQ_DE]
