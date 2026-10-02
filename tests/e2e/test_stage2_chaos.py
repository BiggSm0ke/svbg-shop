"""Stage 2 chaos and money-safety criteria (07 §5 «Этап 2», stage2-contracts invariants) end to end:

* the panel is down during a payment: the payment is credited, the user sees «✅ Оплачено, подключаем…», and
  the same message gets «🔗 Подключиться» once the panel is back;
* the process dies in the middle of ``billing.fulfill`` (after its commit, before the job is acknowledged): a
  new process re-runs the job and nothing is applied twice;
* a wrong amount → ``mismatch`` + alert, nothing credited; forged / stale / test-mode webhooks are refused;
* a frozen user cannot create a payment in any path;
* the 0002 → 0003 migration of a stage-1 database gives exactly the schema of ``create_schema``.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import pytest
from aiohttp import ClientSession, ClientTimeout

from svbg.db import migrations
from svbg.db.schema import create_schema
from tests.e2e.conftest import AppEnv, StartApp
from tests.e2e.test_migrations import _snapshot
from tests.e2e.test_stage2_kit import GROUP, Shop, open_shop, until, until_async
from tests.pgcluster import PgCluster

pytestmark = pytest.mark.pg


async def _waiting_purchase(shop: Shop, telegram_id: int) -> tuple[int, dict[str, Any]]:
    """A purchase with an empty balance up to the RollyPay invoice; returns (main message, payment row)."""
    person = shop.person(telegram_id)
    main = await person.start()
    await person.press("Купить подписку", expect="Выберите срок")
    await person.press("1 мес.", expect="Не хватает 179 ₽")
    await person.press("СБП", expect="Счёт на 179 ₽ готов")
    payment = (await shop.payments_of(telegram_id))[0]
    return main, payment


async def _post(url: str, body: bytes, headers: dict[str, str]) -> int:
    async with (
        ClientSession(timeout=ClientTimeout(total=10)) as session,
        session.post(url, data=body, headers=headers) as resp,
    ):
        await resp.read()
        return resp.status


# ------------------------------------------------------------------------------------------------ panel down


async def test_panel_down_during_payment_is_credited_and_connects_after_recovery(
    start_app: StartApp, app_env: AppEnv
) -> None:
    async with open_shop(start_app, app_env, admin_chat=False) as shop:
        main, payment = await _waiting_purchase(shop, 6_001)
        nina = shop.person(6_001)
        shop.panel.inject("503", times=None)  # the panel container is down behind its proxy
        shop.desk.set_status(payment["external_id"], "paid")
        assert await shop.desk.send_webhook(shop.webhook_url("rollypay"), payment["external_id"]) == 200
        await nina.wait_text("✅ Оплачено, подключаем…", message_id=main, timeout=30)
        uid = await shop.user_id(6_001)
        orders = await shop.rows("select kind, status from orders where user_id = $1 order by id", uid)
        assert [(o["kind"], o["status"]) for o in orders] == [("new", "fulfilled"), ("topup", "credited")]
        sub = (await shop.rows("select link_state from subscriptions where user_id = $1", uid))[0]
        assert sub["link_state"] == "pending" and not shop.panel.users
        shop.panel.clear_faults()  # the panel is back
        await nina.wait_text("✅ Оплачено! Подписка", message_id=main, timeout=60)
        assert nina.button("Подключиться", main).get("web_app")
        assert len(shop.panel.users) == 1
        await shop.assert_wallet_invariants()


# ------------------------------------------------------------------------------------------------ kill


async def test_process_killed_in_the_middle_of_fulfill_applies_nothing_twice(
    start_app: StartApp, app_env: AppEnv
) -> None:
    committed = asyncio.Event()

    def hang_after_commit(app: Any) -> None:
        real = app.job_handlers["billing.fulfill"]

        async def handler(job: Any, ctx: Any) -> None:
            await real(job, ctx)  # the fulfill transaction commits …
            committed.set()
            await asyncio.Event().wait()  # … and the process "dies" before the job is acknowledged

        app.job_handlers["billing.fulfill"] = handler

    async with open_shop(start_app, app_env, admin_chat=False, setup_hooks=[hang_after_commit]) as shop:
        oleg = shop.person(6_002)
        await oleg.start()
        await shop.fund(6_002, 20_000)
        await oleg.press("Купить подписку", expect="Выберите срок")
        await oleg.press("1 мес.", expect="Спишем с баланса")
        await oleg.press("Оплатить 179")
        await asyncio.wait_for(committed.wait(), 20)
        uid = await shop.user_id(6_002)
        before = await shop.rows("select paid_until from subscriptions where user_id = $1", uid)
        await shop.app.stop()  # the running job is released, not acknowledged

        second = await start_app(**{"jobs_poll_interval": 0.2})
        db: Any = second.db

        async def fulfill_done() -> bool:
            rows = await db.raw("select status from jobs where kind = 'billing.fulfill'")
            return [r["status"] for r in rows] == ["done"]

        await until_async(fulfill_done, timeout=30, what="the fulfill job re-ran in the new process")
        after = await db.raw("select paid_until from subscriptions where user_id = $1", uid)
        assert [r["paid_until"] for r in after] == [r["paid_until"] for r in before]
        ledger = await db.raw("select reason from wallet_ledger where user_id = $1 order by id", uid)
        assert [r["reason"] for r in ledger] == ["bonus", "purchase"]
        events = await db.raw(
            "select e.id from subscription_events e join subscriptions s on s.id = e.subscription_id "
            "where s.user_id = $1 and e.ref_type = 'order'",
            uid,
        )
        assert len(events) == 1
        fulfilled = await db.raw("select status from orders where user_id = $1", uid)
        assert [r["status"] for r in fulfilled] == ["fulfilled"]


# ------------------------------------------------------------------------------------------------ webhooks


async def test_wrong_amount_is_a_mismatch_with_an_alert_and_credits_nothing(
    start_app: StartApp, app_env: AppEnv
) -> None:
    async with open_shop(start_app, app_env) as shop:
        _main, payment = await _waiting_purchase(shop, 6_003)
        shop.desk.set_status(payment["external_id"], "paid", amount="100.00")
        assert await shop.desk.send_webhook(shop.webhook_url("rollypay"), payment["external_id"]) == 200
        row = await until_async(
            lambda: shop.rows(
                "select status from payments where id = $1 and status = 'mismatch'", payment["id"]
            ),
            what="payment marked mismatch",
        )
        assert row
        assert await shop.balance(6_003) == 0
        assert await shop.rows("select id from wallet_ledger") == []
        assert shop.app.attention is not None

        async def alerted() -> bool:
            item = await shop.app.attention.get(f"payments:mismatch:{payment['id']}")  # type: ignore[union-attr]
            return item is not None and item.is_open

        await until_async(alerted, what="«Требует внимания»")
        await until(
            lambda: any(
                c.ok
                and c.params.get("chat_id") == GROUP
                and "Сумма оплаты не совпала" in str(c.params.get("text"))
                for c in shop.tg.calls
            ),
            what="alert in «💳 Оплаты»",
        )


async def test_forged_stale_and_test_mode_webhooks_are_refused(start_app: StartApp, app_env: AppEnv) -> None:
    async with open_shop(start_app, app_env, admin_chat=False) as shop:
        _main, payment = await _waiting_purchase(shop, 6_004)
        ext = payment["external_id"]
        shop.desk.set_status(ext, "paid")
        url = shop.webhook_url("rollypay")
        forged = await shop.desk.send_webhook(url, ext, secret="not-the-secret-0123456789")
        stale = await shop.desk.send_webhook(url, ext, at=datetime.now(UTC) - timedelta(seconds=301))
        test_mode = await shop.desk.send_webhook(url, ext, test_header=True)
        wrong_token = await shop.desk.send_webhook(url.rsplit("/", 1)[0] + "/" + "x" * 43, ext)
        assert forged == 401 and stale == 401 and wrong_token in (401, 404)
        assert test_mode in (400, 401)
        assert (await shop.rows("select status from payments where id = $1", payment["id"]))[0]["status"] == (
            "pending"
        )
        assert await shop.balance(6_004) == 0
        outcomes = {r["outcome"] for r in await shop.rows("select outcome from payment_events")}
        assert {"bad_signature", "stale"} <= outcomes
        # the genuine webhook still goes through afterwards
        assert await shop.desk.send_webhook(url, ext) == 200
        await shop.person(6_004).wait_text("✅ Оплачено! Подписка", timeout=30)


async def test_frozen_user_cannot_create_a_payment_anywhere(start_app: StartApp, app_env: AppEnv) -> None:
    async with open_shop(start_app, app_env, admin_chat=False) as shop:
        petr = shop.person(6_005)
        await petr.start()
        await petr.press("Попробовать бесплатно", expect="Пробный период")
        await petr.wait_text("Готово! Пробный период до", timeout=20)
        uid = await shop.user_id(6_005)
        await shop.db.raw(
            "update subscriptions set hold_kind = 'admin', hold_since = now() where user_id = $1", uid
        )
        await petr.start()
        await petr.press("Баланс", expect="Выберите сумму пополнения")
        await petr.press("179", expect="Пополнение на 179 ₽")
        await petr.press("СБП", expect="⚠️ Подписка приостановлена — оплата сейчас недоступна")
        assert await shop.payments_of(6_005) == []
        assert shop.desk.created == []
        await petr.press("Другой способ", expect="Выберите способ оплаты")
        await petr.press("Stars", expect="⚠️ Подписка приостановлена")
        await petr.press("Другой способ", expect="Выберите способ оплаты")
        await petr.press("Перевод", expect="⚠️ Подписка приостановлена")
        assert await shop.payments_of(6_005) == []
        assert shop.app.payments is not None
        async with shop.app.db.read() as conn:  # type: ignore[union-attr]
            assert await shop.app.payments.can_spend(conn, uid)  # the refusal text of the X5 guard


# ------------------------------------------------------------------------------------------------ migration


@pytest.fixture
async def other_dsn(pg_cluster: PgCluster) -> AsyncIterator[str]:
    name = f"t_s2_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(pg_cluster.dsn("postgres"))
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()
    try:
        yield pg_cluster.dsn(name)
    finally:
        admin = await asyncpg.connect(pg_cluster.dsn("postgres"))
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            await admin.close()


async def test_stage1_database_upgrades_to_stage2_schema(pg_dsn: str, other_dsn: str) -> None:
    await migrations.upgrade(pg_dsn, "0002_stage1")
    conn = await asyncpg.connect(pg_dsn)
    try:  # a stage-1 user and subscription survive the upgrade with the new defaults
        await conn.execute("insert into users (telegram_id, first_name) values (42, 'Old')")
    finally:
        await conn.close()
    await migrations.upgrade(pg_dsn)
    assert await migrations.current_revision(pg_dsn) == migrations.head_revision()
    await create_schema(other_dsn)
    migrated, reference = await _snapshot(pg_dsn), await _snapshot(other_dsn)
    for part in ("columns", "constraints", "indexes", "extensions"):
        assert migrated[part] == reference[part], f"{part} differ after 0002 → 0003"
    conn = await asyncpg.connect(pg_dsn)
    try:
        assert await conn.fetchval("select wallet_minor from users where telegram_id = 42") == 0
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute("update users set wallet_minor = -1 where telegram_id = 42")
    finally:
        await conn.close()
