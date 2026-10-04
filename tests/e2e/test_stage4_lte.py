"""Stage 4 end to end — LTE quotas (05 §2.1, 07 §2.4.3) in the assembled application.

* switched on by settings without a restart → usage read from the fake panel's ``nodes/usage`` → the quota
  is exhausted → the block replaces the squad in the panel (base → twin without the LTE inbound) → the user
  is told → «⚡ Докупить трафик LTE» with a shortfall → RollyPay → the purchase completes by itself → the
  block is lifted (base squad back) → «Подключиться»;
* switching the module off with «оставить» keeps the block in the panel (fail-closed).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from svbg.core.settings import Change
from svbg.ext.api import ModuleState
from tests.e2e.conftest import AppEnv, StartApp
from tests.e2e.test_stage2_kit import Shop, open_shop, until, until_async
from tests.e2e.test_stage3_kit import chat, tg  # noqa: F401 - the ``tg`` fixture

pytestmark = pytest.mark.pg

GB = 10**9
ON = [
    Change("LTE_ENABLED", "true"),
    Change("LTE_ENFORCE", "on"),
    Change("LTE_TOPUP_ENABLED", "true"),
    Change("LTE_QUIET_HOURS", "off"),
]


async def lte_topology(shop: Shop) -> dict[str, Any]:
    """Base squad = {main, lte} inbounds, its twin = {main}; an LTE node; one 10 GB group; a 5 GB pack."""
    panel = shop.panel
    panel.internal_squads[shop.squad]["inbounds"] = [
        {"uuid": "ib-main", "tag": "MAIN"},
        {"uuid": "ib-lte", "tag": "LTE"},
    ]
    twin = panel.add_internal_squad("NL noLTE")
    panel.internal_squads[twin]["inbounds"] = [{"uuid": "ib-main", "tag": "MAIN"}]
    lte_node = panel.add_node("LTE-1")
    panel.add_node("NL-1")  # a plain node: never read for LTE
    for node in panel.nodes:
        tag = "LTE" if node["uuid"] == lte_node else "MAIN"
        node["configProfile"]["activeInbounds"] = [{"uuid": f"ib-{tag.lower()}", "tag": tag}]
    rows = await shop.rows(
        "insert into lte_groups (slug, name, state, enforce, has_default, limit_default_bytes, has_trial,"
        " limit_trial_bytes) values ('lte', '{\"ru\": \"LTE\"}'::jsonb, 'active', true, true, $1, true, 0)"
        " returning id",
        10 * GB,
    )
    group = int(rows[0]["id"])
    await shop.db.raw(
        "insert into lte_group_nodes (group_id, node_uuid, counted_from)"
        " values ($1, $2, now() - interval '2 days')",
        group,
        lte_node,
    )
    await shop.db.raw(
        "insert into lte_twins (base_squad_uuid, group_id, twin_squad_uuid) values ($1, $2, $3)",
        shop.squad,
        group,
        twin,
    )
    await shop.db.raw("insert into lte_packs (gb, amount_minor, enabled) values (5, 9900, true)")
    return {"twin": twin, "node": lte_node, "group": group}


async def switch(shop: Shop, changes: list[Change]) -> None:
    app = shop.app
    assert app.settings is not None and app.ext is not None
    await app.settings.apply(changes, source="bot", actor_id=None)
    await app.ext.drain()


async def buy_month(shop: Shop, telegram_id: int) -> Any:
    person = chat(shop, telegram_id)
    await person.start()
    await shop.fund(telegram_id, 17_900)
    await person.press("Профиль", expect="👤 Профиль")
    await person.press("Купить подписку", expect="Выберите срок")
    await person.press("1 мес.", expect="Спишем с баланса")
    await person.press("Оплатить 179")
    await person.wait_text("✅ Оплачено! Подписка", timeout=20)
    assert await shop.balance(telegram_id) == 0
    return person


def squads_of(shop: Shop) -> list[str]:
    (user,) = shop.panel.users.values()
    return sorted(s["uuid"] if isinstance(s, dict) else str(s) for s in user["activeInternalSquads"])


async def cycle(shop: Shop) -> dict[str, Any]:
    """Run one ``lte.cycle`` now (the scheduler's trigger) and wait for its result in ``lte_kv``."""

    async def last() -> str | None:
        rows = await shop.rows("select value from lte_kv where key = 'cycle'")
        return str(rows[0]["value"].get("at")) if rows else None

    before = await last()
    assert shop.app.scheduler is not None
    shop.app.scheduler.trigger("lte.cycle")

    async def done() -> bool:
        return await last() != before

    await until_async(done, timeout=20, what="an LTE cycle")
    return dict((await shop.rows("select value from lte_kv where key = 'cycle'"))[0]["value"])


async def exhaust(shop: Shop, topo: dict[str, Any]) -> None:
    """The LTE node reports usage of the only panel user: a baseline, then 13 GB more than the 10 GB quota;
    cycles run until the block is applied in the panel (the base squad is replaced by its twin)."""

    async def period_open() -> bool:
        rows = await shop.rows("select state from lte_periods")
        return [r["state"] for r in rows] == ["open"]

    await until_async(period_open, timeout=30, what="the LTE period opens after the payment")
    # The panel's clock (``Date``) has whole seconds: a read in the second the period opened belongs to the
    # time before it. Real cycles are 10 minutes apart; here the next second is waited for.
    await asyncio.sleep(1.2)
    (panel_id,) = shop.panel.users.keys()
    today = datetime.now(UTC).date().isoformat()
    shop.panel.node_usage[(topo["node"], today)] = {int(panel_id): 1 * GB}
    assert (await cycle(shop))["ok"]
    await asyncio.sleep(1.2)
    shop.panel.node_usage[(topo["node"], today)] = {int(panel_id): 14 * GB}
    for _ in range(10):  # an implausibly fast delta waits one cycle (LTE_SANITY_MAX_MBPS)
        last = await cycle(shop)
        if await shop.rows("select id from lte_blocks where status = 'active'"):
            break
    blocks = await shop.rows("select status, reason from lte_blocks")
    usage = await shop.rows("select used_bytes from lte_period_usage")
    if blocks != [{"status": "active", "reason": "quota"}]:
        dump = {
            t: await shop.rows(f"select * from {t}")
            for t in ("lte_counters", "lte_node_state", "lte_periods", "lte_usage_hourly", "lte_kv")
        }
        raise AssertionError(str((blocks, usage, last, shop.panel.node_usage, dump)))
    await until(lambda: squads_of(shop) == [topo["twin"]], timeout=40, what="the twin squad in the panel")


async def test_lte_quota_block_topup_with_a_shortfall_and_release(
    start_app: StartApp, app_env: AppEnv
) -> None:
    async with open_shop(start_app, app_env) as shop:
        topo = await lte_topology(shop)
        await switch(shop, ON)
        assert shop.app.ext is not None and shop.app.ext.state("lte") is ModuleState.ACTIVE  # no restart
        anna = await buy_month(shop, 5_601)
        assert squads_of(shop) == [shop.squad]

        await exhaust(shop, topo)
        used = await shop.rows("select used_bytes from lte_period_usage")
        assert used and used[0]["used_bytes"] >= 10 * GB

        # the user is told, with the pack button
        await until(
            lambda: any(
                "Трафик LTE исчерпан" in str(m.get("text") or m.get("caption"))
                for m in shop.tg.bot_messages(5_601)
            ),
            timeout=20,
            what="the exhausted notice",
        )
        notice = next(
            m
            for m in shop.tg.bot_messages(5_601)
            if "Трафик LTE исчерпан" in str(m.get("text") or m.get("caption"))
        )
        anna.main = int(notice["message_id"])
        await anna.tap("⚡ Докупить трафик LTE", expect="+5 ГБ")
        await anna.tap("+5 ГБ", expect="Подтвердить")
        await anna.tap("✅ Подтвердить", expect="Не хватает")
        await anna.tap("СБП", expect="готов")
        (payment,) = [p for p in await shop.payments_of(5_601) if p["status"] == "pending"]
        shop.desk.set_status(payment["external_id"], "paid")
        assert await shop.desk.send_webhook(shop.webhook_url("rollypay"), payment["external_id"]) == 200
        await anna.wait_text("✅", timeout=20)
        await until(lambda: squads_of(shop) == [shop.squad], timeout=30, what="the base squad is back")
        blocks = await shop.rows("select status, release_reason from lte_blocks")
        assert [b["status"] for b in blocks] == ["released"], blocks
        orders = await shop.rows("select kind, status from orders where kind = 'addon_lte'")
        assert orders == [{"kind": "addon_lte", "status": "fulfilled"}]
        assert any("Подключиться" in str(b.get("text")) for b in anna.buttons())
        await shop.assert_wallet_invariants()


async def test_switching_lte_off_with_keep_leaves_the_block(start_app: StartApp, app_env: AppEnv) -> None:
    async with open_shop(start_app, app_env) as shop:
        topo = await lte_topology(shop)
        await switch(shop, ON)
        await buy_month(shop, 5_602)
        await exhaust(shop, topo)

        await switch(shop, [Change("LTE_OFF_ACTION", "keep"), Change("LTE_ENABLED", "false")])
        assert shop.app.ext is not None and shop.app.ext.state("lte") is ModuleState.DISABLED
        await until_async(lambda: _jobs_settled(shop), timeout=20, what="panel jobs settled")
        assert squads_of(shop) == [topo["twin"]]  # fail-closed: the module is off, the block stays
        blocks = await shop.rows("select status from lte_blocks")
        assert [b["status"] for b in blocks] == ["active"]
        subs = await shop.rows("select count(*) as n from panel_squad_substitutions")
        assert subs[0]["n"] == 1


async def _jobs_settled(shop: Shop) -> bool:
    rows = await shop.rows(
        "select count(*) as n from jobs where status in ('ready', 'running') and queue = 'panel'"
    )
    return rows[0]["n"] == 0
