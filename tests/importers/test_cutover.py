"""Cutover / rollback helpers (06 §1.4, §4.4, §4.7, §4.9): pay freeze, export-rollback with the «pending»
section, readiness gates, getWebhookInfo / deleteWebhook without dropping the update queue, panel token probe.

Everything runs against the real schema (``create_schema`` + owner-module tables), the real payment core, the
fake Telegram Bot API and the fake Remnawave panel. No network, no clocks other than fixed datetimes.
"""

from __future__ import annotations

import argparse
import asyncio
import io as _io
import json
import re
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from svbg.core.crypto import Crypto, generate_key
from svbg.importers.cutover import (
    FREEZE_KEY,
    FREEZE_TEXT,
    add_commands,
    delete_webhook_keep_queue,
    export_rollback,
    gate_check,
    install_pay_freeze,
    pay_freeze_state,
    set_pay_freeze,
    webhook_info,
)
from svbg.importers.shadow import HISTORY_KEY, LAST_KEY, PROBE_OFFSET
from svbg.payments.core import SpendDeniedError
from svbg.payments.providers.manual import ManualTransfer
from svbg.payments.testkit import CoreHarness
from tests.dbkit import CountingDatabase
from tests.fakes.remnawave import FakeRemnawave
from tests.fakes.telegram import FakeTelegram
from tests.importers.test_shadow import READ_SCOPES, create_target
from tests.pgcluster import PgCluster

pytestmark = pytest.mark.timeout(180)

REPO = Path(__file__).resolve().parents[2]
T0 = datetime(2026, 10, 2, 1, 0, tzinfo=UTC)
BEFORE = T0 - timedelta(days=1)
AFTER = T0 + timedelta(minutes=30)
OLD_HOOK = "https://bedolaga.example/webhook"


# ---------------------------------------------------------------------------------------------- fixtures

_TEMPLATE: dict[int, str] = {}


async def _admin(cluster: PgCluster, sql: str) -> None:
    conn = await asyncpg.connect(cluster.dsn("postgres"))
    try:
        await conn.execute(sql)
    finally:
        await conn.close()


@pytest.fixture
async def dsn(pg_cluster: PgCluster) -> AsyncIterator[str]:
    """Our schema + owner-module tables (LTE, IP Guard, referral), cloned from a per-process template."""
    if id(pg_cluster) not in _TEMPLATE:
        tpl = f"cut_tpl_{uuid.uuid4().hex[:8]}"
        await _admin(pg_cluster, f'CREATE DATABASE "{tpl}"')
        await create_target(pg_cluster.dsn(tpl))
        _TEMPLATE[id(pg_cluster)] = tpl
    name = f"cut_{uuid.uuid4().hex[:10]}"
    await _admin(
        pg_cluster, f'CREATE DATABASE "{name}" TEMPLATE "{_TEMPLATE[id(pg_cluster)]}" STRATEGY FILE_COPY'
    )
    try:
        yield pg_cluster.dsn(name)
    finally:
        await _admin(pg_cluster, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


async def _conn(dsn: str) -> asyncpg.Connection:
    conn = await asyncpg.connect(dsn)
    for typ in ("json", "jsonb"):
        await conn.set_type_codec(
            typ,
            encoder=lambda v: v if isinstance(v, str) else json.dumps(v),
            decoder=json.loads,
            schema="pg_catalog",
        )
    return conn


async def ins(conn: asyncpg.Connection, table: str, **values: Any) -> Any:
    cols = ", ".join(values)
    marks = ", ".join(f"${i}" for i in range(1, len(values) + 1))
    return await conn.fetchval(
        f"INSERT INTO {table} ({cols}) VALUES ({marks}) RETURNING id",
        *values.values(),
    )


class Io:
    def __init__(self) -> None:
        self.out = _io.StringIO()

    def say(self, text: str = "") -> None:
        self.out.write(text + "\n")

    def warn(self, text: str) -> None:
        self.out.write(text + "\n")

    @property
    def text(self) -> str:
        return self.out.getvalue()


def parse(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    add_commands(parser.add_subparsers(dest="command", required=True))
    return parser.parse_args(list(argv))


async def run_cli(tmp_path: Path, *argv: str, env: dict[str, str] | None = None) -> tuple[int, str]:
    """The handler calls ``asyncio.run`` itself (as under ``python -m svbg``): run it in a worker thread so
    the fakes keep serving on this loop."""
    args = parse(*argv)
    io = Io()
    environ = {"DATA_DIR": str(tmp_path), **(env or {})}
    code = await asyncio.to_thread(args.handler, args, environ, io)
    return int(code), io.text


# -------------------------------------------------------------------------------------------- pay freeze


async def test_pay_freeze_stops_new_invoices_but_money_in_flight_is_credited(dsn: str) -> None:
    db = CountingDatabase(dsn, pool_size=4)
    await db.start()
    harness = await CoreHarness.create(
        db, Crypto([generate_key()]), ManualTransfer, {"details": "Карта 2200"}
    )
    try:
        install_pay_freeze(harness.core)
        before = await harness.pending(17_900)  # issued before the freeze — must still be credited

        state = await set_pay_freeze(dsn, True, reason="rollback")
        assert state["on"] is True and state["reason"] == "rollback"
        again = await set_pay_freeze(dsn, True, reason="other")  # idempotent: the first record stays
        assert again["since"] == state["since"] and again["reason"] == "rollback"

        with pytest.raises(SpendDeniedError) as err:
            await harness.pending(17_900)
        assert str(err.value) == FREEZE_TEXT or FREEZE_TEXT in str(err.value.args)

        # successful_payment / webhook of an invoice issued earlier: the trusted path ignores spend guards.
        result = await harness.core.credit_external(
            harness.instance.id,
            user_id=harness.user_id,
            external_id="charge-1",
            amount_minor=17_900,
            currency="RUB",
            payment_id=before,
        )
        assert result.credited
        assert await harness.status(before) == "paid"
        assert [p.id for p in harness.credited] == [before]

        await set_pay_freeze(dsn, False)
        await harness.pending(17_900)  # unfrozen: invoices are issued again
        audit = await db.raw("SELECT action, target, reason FROM admin_audit ORDER BY id")
        assert [(r["action"], r["target"]) for r in audit] == [
            ("pay.freeze", "payments"),
            ("pay.unfreeze", "payments"),
        ]
        conn = await _conn(dsn)
        try:
            assert await pay_freeze_state(conn) is None
            assert (await conn.fetchval("SELECT value FROM config_meta WHERE key = $1", FREEZE_KEY))[
                "on"
            ] is False
        finally:
            await conn.close()
    finally:
        await harness.close()
        await db.close()


async def test_unfreeze_when_not_frozen_writes_no_audit(dsn: str) -> None:
    await set_pay_freeze(dsn, False)
    conn = await _conn(dsn)
    try:
        assert await conn.fetchval("SELECT count(*) FROM admin_audit") == 0
        assert await pay_freeze_state(conn) is None
    finally:
        await conn.close()


async def test_cli_pay_freeze_status_unfreeze(dsn: str, tmp_path: Path) -> None:
    code, out = await run_cli(tmp_path, "pay", "status", "--dsn", dsn)
    assert code == 0 and "оплата работает" in out
    code, out = await run_cli(tmp_path, "pay", "freeze", "--dsn", dsn)
    assert code == 0 and "Новые счета больше не выставляются" in out
    code, out = await run_cli(tmp_path, "pay", "status", "--dsn", dsn)
    assert code == 0 and "заморожены" in out and "pending-счетов: 0" in out
    code, out = await run_cli(tmp_path, "pay", "unfreeze", "--dsn", dsn)
    assert code == 0 and "снова разрешено" in out


# --------------------------------------------------------------------------------------- export-rollback


async def seed_rollback(dsn: str) -> dict[str, str]:
    """State of the new bot ~30 min after T0: imported Bedolaga data + what SvBG did after T0."""
    conn = await _conn(dsn)
    ids: dict[str, str] = {}
    try:
        inst = {}
        for slug in ("rollypay", "cryptobot"):
            inst[slug] = await ins(
                conn,
                "payment_instances",
                provider=slug,
                slug=slug,
                title=slug,
                config="enc:v1:x",
                webhook_token="enc:v1:y",
            )
        await ins(conn, "users", id=1, telegram_id=1001, wallet_minor=60_000, created_at=BEFORE)
        await ins(conn, "users", id=2, telegram_id=2002, wallet_minor=0, created_at=AFTER)
        await ins(
            conn,
            "subscriptions",
            id=11,
            user_id=1,
            link_state="linked",
            panel_user_id=1,
            panel_username="user_1001",
            paid_until=T0 + timedelta(days=30),
            created_at=BEFORE,
        )
        await ins(
            conn,
            "subscriptions",
            id=12,
            user_id=2,
            link_state="linked",
            panel_user_id=2,
            panel_username="user_2002",
            paid_until=T0 + timedelta(days=3),
            created_at=AFTER,
        )

        def pay(key: str, slug: str, status: str, created: datetime, **kw: Any) -> dict[str, Any]:
            ids[key] = str(uuid.uuid4())
            return {
                "id": ids[key],
                "instance_id": inst[slug],
                "user_id": 1,
                "amount_minor": 17_900,
                "currency": "RUB",
                "status": status,
                "created_at": created,
                **kw,
            }

        rows = [
            # imported Bedolaga invoice, still alive at T0 (06 §2.4.3) — must be in «pending»
            pay(
                "imported_pending",
                "rollypay",
                "pending",
                BEFORE,
                is_imported=True,
                external_id="RP-9",
                merchant_ref="rp1001_abcdef",
                expires_at=T0 + timedelta(hours=47),
            ),
            # issued by SvBG after T0
            pay(
                "new_pending",
                "cryptobot",
                "pending",
                AFTER,
                external_id="CB-77",
                expires_at=T0 + timedelta(hours=24),
            ),
            # paid after T0 (journal), and an imported historical payment (not in the journal)
            pay(
                "paid_after",
                "rollypay",
                "paid",
                AFTER,
                external_id="RP-10",
                merchant_ref="rp1001_aaaaaa",
                paid_amount_minor=17_900,
                paid_currency="RUB",
                paid_at=AFTER,
            ),
            pay(
                "paid_before",
                "rollypay",
                "paid",
                BEFORE - timedelta(days=9),
                external_id="RP-1",
                is_imported=True,
                paid_amount_minor=17_900,
                paid_currency="RUB",
                paid_at=BEFORE - timedelta(days=9),
            ),
            pay("expired", "cryptobot", "expired", AFTER, external_id="CB-78"),
        ]
        for row in rows:
            await ins(conn, "payments", **row)

        await ins(
            conn,
            "wallet_ledger",
            user_id=1,
            amount_minor=50_000,
            currency="RUB",
            balance_after=50_000,
            reason="import_opening",
            ref_type="import_run",
            ref_id="1",
            created_at=BEFORE,
        )
        await ins(
            conn,
            "wallet_ledger",
            user_id=1,
            amount_minor=10_000,
            currency="RUB",
            balance_after=60_000,
            reason="topup",
            ref_type="payment",
            ref_id=ids["paid_after"],
            created_at=AFTER,
        )

        await ins(
            conn,
            "subscription_events",
            subscription_id=11,
            kind="renew",
            source="payment",
            old_expire=T0,
            new_expire=T0 + timedelta(days=30),
            delta_seconds=30 * 86400,
            ts=AFTER,
        )
        await ins(
            conn,
            "subscription_events",
            subscription_id=11,
            kind="import",
            source="import",
            new_expire=T0,
            ts=AFTER,
        )
        await ins(
            conn,
            "subscription_events",
            subscription_id=11,
            kind="renew",
            source="payment",
            new_expire=T0,
            ts=BEFORE,
        )

        await conn.execute(
            "INSERT INTO referrals (referred_user_id, referrer_id, attached_at) VALUES (2, 1, $1)", AFTER
        )
        await ins(
            conn,
            "referral_rewards",
            user_id=1,
            referred_user_id=2,
            side="inviter",
            kind="days",
            status="granted",
            days=14,
            granted_at=AFTER,
        )
        await ins(
            conn,
            "referral_rewards",
            user_id=1,
            referred_user_id=2,
            side="invitee",
            kind="days",
            status="legacy",
            granted_at=None,
        )

        await ins(conn, "lte_groups", id=1, slug="lte")
        await ins(
            conn,
            "lte_blocks",
            subscription_id=11,
            group_id=1,
            reason="quota",
            mode="enforce",
            status="active",
            created_at=BEFORE,
        )  # imported block — stays, not in the journal
        await ins(
            conn,
            "lte_blocks",
            subscription_id=12,
            group_id=1,
            reason="quota",
            mode="enforce",
            status="active",
            created_at=AFTER,
        )  # new block of SvBG → `svbg lte release --since T0`

        await ins(
            conn, "ip_guard_blocks", subscription_id=11, reason="auto", status="active", blocked_at=BEFORE
        )
        await ins(
            conn,
            "ip_guard_blocks",
            subscription_id=12,
            reason="auto",
            status="active",
            blocked_at=AFTER,
            frozen_seconds=3600,
        )
    finally:
        await conn.close()
    return ids


async def test_export_rollback_has_pending_section_and_journal(dsn: str, tmp_path: Path) -> None:
    ids = await seed_rollback(dsn)
    out = tmp_path / "rb" / "rollback.json"
    result = await export_rollback(dsn, T0, out=out)

    pending = {p["id"]: p for p in result.pending}
    assert set(pending) == {ids["imported_pending"], ids["new_pending"]}
    assert result.pending_db_count == 2 and result.pending_matches
    imp = pending[ids["imported_pending"]]
    assert imp["is_imported"] is True and imp["instance"] == "rollypay"
    assert imp["external_id"] == "RP-9" and imp["merchant_ref"] == "rp1001_abcdef"
    assert imp["amount_minor"] == 17_900 and imp["telegram_id"] == 1001
    assert imp["expires_at"] == T0 + timedelta(hours=47)
    assert pending[ids["new_pending"]]["instance"] == "cryptobot"

    s = result.sections
    assert [p["id"] for p in s["payments_paid"]] == [ids["paid_after"]]
    assert [(w["reason"], w["amount_minor"]) for w in s["wallet"]] == [("topup", 10_000)]
    assert [(e["source"], e["delta_seconds"]) for e in s["paid_until"]] == [("payment", 30 * 86400)]
    assert [(r["side"], r["days"]) for r in s["referral_days"]] == [("inviter", 14)]
    assert [b["subscription_id"] for b in s["lte_blocks"]] == [12]
    assert [(b["subscription_id"], b["frozen_seconds"]) for b in s["ip_guard_blocks"]] == [(12, 3600)]
    assert [u["id"] for u in s["new_users"]] == [2]
    assert [x["id"] for x in s["new_subscriptions"]] == [12]
    assert result.missing_tables == [] and result.frozen is None

    doc = json.loads(out.read_text("utf-8"))
    assert doc["counts"]["pending"] == 2 and doc["pending_matches"] is True
    assert {p["external_id"] for p in doc["pending"]} == {"RP-9", "CB-77"}
    assert doc["since"] == T0.isoformat()
    lines = result.summary()
    assert any("pending): 2 (импортированных из Bedolaga: 1)" in line for line in lines)

    # The export never changes anything: a second run sees the same numbers.
    again = await export_rollback(dsn, T0)
    assert {p["id"] for p in again.pending} == set(pending) and again.path is None


async def test_export_rollback_reports_freeze_and_rejects_naive_since(dsn: str, tmp_path: Path) -> None:
    await seed_rollback(dsn)
    await set_pay_freeze(dsn, True, reason="rollback")
    result = await export_rollback(dsn, T0)
    assert result.frozen is not None and result.frozen["reason"] == "rollback"
    with pytest.raises(ValueError, match="часовым поясом"):
        await export_rollback(dsn, T0.replace(tzinfo=None))

    code, out = await run_cli(
        tmp_path, "cutover", "export-rollback", "--since", "2026-10-02T01:00:00", "--dsn", dsn
    )
    assert code == 2 and "часовой пояс" in out
    target = tmp_path / "out.json"
    code, out = await run_cli(
        tmp_path, "cutover", "export-rollback", "--since", T0.isoformat(), "--dsn", dsn, "--out", str(target)
    )
    assert code == 0, out
    assert "незакрытые счета (pending): 2" in out and "Оплата не заморожена" not in out
    assert json.loads(target.read_text("utf-8"))["pay_freeze"]["on"] is True


async def test_cli_export_warns_when_not_frozen(dsn: str, tmp_path: Path) -> None:
    await seed_rollback(dsn)
    code, out = await run_cli(
        tmp_path,
        "cutover",
        "export-rollback",
        "--since",
        T0.isoformat(),
        "--dsn",
        dsn,
        "--out",
        str(tmp_path / "x.json"),
    )
    assert code == 0 and "сначала svbg pay freeze" in out


# ------------------------------------------------------------------------------------------------- gates


async def test_gate_check_red_on_fresh_stand(dsn: str, tmp_path: Path) -> None:
    items = await gate_check(dsn, at=T0)
    auto = {i.name: i for i in items if not i.manual}
    assert not auto["shadow: зелёных дней подряд"].ok
    assert not auto["shadow: последний отчёт"].ok
    assert not auto["кассы заведены (можно enabled=false до T0)"].ok
    assert not auto["настройки Bedolaga импортированы"].ok
    assert auto["оплата не заморожена"].ok and auto["очередь панели пуста"].ok
    assert all(not i.ok for i in items if i.manual) and len([i for i in items if i.manual]) == 7
    code, out = await run_cli(tmp_path, "cutover", "check", "--dsn", dsn)
    assert code == 1 and "дату не назначать" in out and "☐" in out


async def green_stand(dsn: str, *, at: datetime) -> None:
    conn = await _conn(dsn)
    try:
        days = [(at - timedelta(days=d)).date().isoformat() for d in (2, 1, 0)]
        history = [{"date": d, "green": True} for d in days]
        last = {
            "as_of": (at - timedelta(hours=1)).isoformat(),
            "green": True,
            "blocked": None,
            "ops_total": 0,
            "probe": {"verdict": "read_only", "status": 403},
            "checks": [],
            "import": {"run_id": 7, "green": True, "finished": True, "blocking": {}},
        }
        for key, value in ((HISTORY_KEY, history), (LAST_KEY, last)):
            await conn.execute("INSERT INTO config_meta (key, value) VALUES ($1, $2::jsonb)", key, value)
        for slug in ("rollypay", "cryptobot", "stars"):
            await ins(
                conn,
                "payment_instances",
                provider=slug,
                slug=slug,
                title=slug,
                config="enc:v1:x",
                webhook_token="enc:v1:y",
                enabled=False,
            )
        await ins(
            conn,
            "settings_audit",
            batch_id=str(uuid.uuid4()),
            key="TRIAL_DAYS",
            new="3",
            source="import",
            applied=True,
        )
    finally:
        await conn.close()


async def test_gate_check_green_then_each_gate_turns_red(dsn: str) -> None:
    at = datetime(2026, 10, 2, 7, 0, tzinfo=UTC)
    await green_stand(dsn, at=at)
    items = await gate_check(dsn, at=at)
    assert all(i.ok for i in items if not i.manual), [i for i in items if not i.ok and not i.manual]

    stale = await gate_check(dsn, at=at + timedelta(hours=30))
    assert not next(i for i in stale if i.name == "shadow: последний отчёт").ok

    conn = await _conn(dsn)
    try:
        last = await conn.fetchval("SELECT value FROM config_meta WHERE key = $1", LAST_KEY)
        last.update(
            {
                "ops_total": 3,
                "probe": {"verdict": "writable", "status": 404},
                "import": {"run_id": 8, "green": False, "blocking": {"subscription_conflict": 1}},
            }
        )
        await conn.execute("UPDATE config_meta SET value = $2::jsonb WHERE key = $1", LAST_KEY, last)
        await conn.execute("INSERT INTO jobs (queue, kind, status) VALUES ('panel', 'panel.sync', 'ready')")
    finally:
        await conn.close()
    await set_pay_freeze(dsn, True)
    red = {i.name for i in await gate_check(dsn, at=at) if not i.ok and not i.manual}
    assert red == {
        "панель: shadow работал на read-only токене",
        "writer: план shadow пуст",
        "очередь панели пуста",
        "оплата не заморожена",
        "импорт: без блокирующих проблем",
    }


# --------------------------------------------------------------------------------- Telegram webhook (R2)


@pytest.fixture
async def tg() -> AsyncIterator[FakeTelegram]:
    async with FakeTelegram() as fake:
        yield fake


def old_bot_left_webhook(tg: FakeTelegram, token: str, updates: int = 2) -> None:
    """Bedolaga is stopped; its webhook is still set and Telegram keeps the queue (Stars payments in it)."""
    for i in range(updates):
        tg.push_message(user_id=500 + i, text=f"/start {i}")
    tg._bot(tg.bot_id(token)).webhook_url = OLD_HOOK  # set after the pushes: no delivery attempts


async def test_webhook_info_and_delete_keep_the_queue(tg: FakeTelegram) -> None:
    token = tg.add_bot()
    old_bot_left_webhook(tg, token)
    info = await webhook_info(token, api_url=tg.url)
    assert info["url"] == OLD_HOOK and info["pending_update_count"] == 2

    with pytest.raises(Exception, match="ничего не удалено"):
        await delete_webhook_keep_queue(token, api_url=tg.url, expect_url="https://other.example/hook")
    assert tg.webhook_url() == OLD_HOOK and not tg.calls_for("deleteWebhook")

    res = await delete_webhook_keep_queue(token, api_url=tg.url, expect_url=OLD_HOOK)
    assert res["deleted"] is True
    assert res["before"]["pending_update_count"] == 2 and res["after"]["pending_update_count"] == 2
    assert res["after"]["url"] == "" and len(tg.pending_updates()) == 2  # nothing dropped
    calls = tg.calls_for("deleteWebhook")
    assert len(calls) == 1 and calls[0].params.get("drop_pending_updates") is False

    again = await delete_webhook_keep_queue(token, api_url=tg.url)
    assert again["deleted"] is False and len(tg.calls_for("deleteWebhook")) == 1


async def test_cli_webhook_info_and_delete(tg: FakeTelegram, tmp_path: Path) -> None:
    token = tg.add_bot()
    old_bot_left_webhook(tg, token, updates=3)
    token_file = tmp_path / "bot_token"
    token_file.write_text(token + "\n", "utf-8")

    code, out = await run_cli(
        tmp_path, "cutover", "webhook-info", "--token-file", str(token_file), "--api-url", tg.url
    )
    assert code == 0 and OLD_HOOK in out and "pending_update_count: 3" in out
    assert token not in out  # the secret is never echoed

    code, out = await run_cli(
        tmp_path, "cutover", "delete-webhook", "--token-file", str(token_file), "--api-url", tg.url
    )
    assert code == 2 and "--yes" in out and tg.webhook_url() == OLD_HOOK

    code, out = await run_cli(
        tmp_path,
        "cutover",
        "delete-webhook",
        "--yes",
        "--expect-url",
        OLD_HOOK,
        "--api-url",
        tg.url,
        env={"SVBG_CUTOVER_BOT_TOKEN": token},
    )
    assert code == 0 and "drop_pending_updates=false" in out and "было 3, сейчас 3" in out
    assert tg.webhook_url() == "" and len(tg.pending_updates()) == 3

    code, out = await run_cli(tmp_path, "cutover", "webhook-info", "--api-url", tg.url)
    assert code == 2 and "нужен токен бота" in out


def test_no_code_path_drops_pending_updates() -> None:
    """06 R2: nowhere in the application is the Telegram update queue dropped."""
    pattern = re.compile(
        r"drop_pending_updates\s*=\s*(True|1)\b|[\"']drop_pending_updates[\"']\s*:\s*(True|true|1)\b"
    )
    hits = [
        f"{path.relative_to(REPO)}:{n}"
        for path in (REPO / "svbg").rglob("*.py")
        for n, line in enumerate(path.read_text("utf-8").splitlines(), start=1)
        if pattern.search(line)
    ]
    assert hits == []


# -------------------------------------------------------------------------------------- panel token probe


async def test_cli_probe_panel_read_only_and_full_token(tmp_path: Path) -> None:
    async with FakeRemnawave() as panel:
        panel.add_user(username="user_1001", telegramId=1001)
        panel.add_user(username="user_1002", telegramId=1002)
        ro = tmp_path / "ro"
        ro.write_text(panel.add_token(READ_SCOPES), "utf-8")
        full = tmp_path / "full"
        full.write_text(panel.add_token(("*",)), "utf-8")
        base = ("cutover", "probe-panel", "--panel-url", panel.url)

        code, out = await run_cli(tmp_path, *base, "--token-file", str(ro))
        assert code == 0 and "годится для shadow" in out and "Пользователей в панели: 2" in out
        code, out = await run_cli(tmp_path, *base, "--token-file", str(full))
        assert code == 1 and "нужен токен только для чтения" in out
        code, out = await run_cli(tmp_path, *base, "--token-file", str(full), "--expect", "write")
        assert code == 0 and "404 — норма" in out
        code, out = await run_cli(tmp_path, *base, "--token-file", str(ro), "--expect", "write")
        assert code == 1 and "нет users:update" in out

        mutating = [(r.method, r.path, r.body) for r in panel.requests if r.method != "GET"]
        assert mutating and all(m == ("PATCH", "/users", {"id": 2 + PROBE_OFFSET}) for m in mutating)
        assert {u["username"] for u in panel.users.values()} == {"user_1001", "user_1002"}


async def test_cli_probe_without_panel_settings_is_a_config_error(tmp_path: Path) -> None:
    code, out = await run_cli(tmp_path, "cutover", "probe-panel")
    assert code == 78 and "панель не настроена" in out
