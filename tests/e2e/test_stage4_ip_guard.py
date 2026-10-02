"""Stage 4 end to end — IP Guard (05 §2.2) in the assembled application.

Too many IPs on a node → (the automatic block is off) a warning card in «🛡 Антиабуз» → the owner blocks by the
card's button → the subscription is frozen (``hold_kind='ip_guard'``), disabled in the panel, the IPs are
dropped, the user is told → «🔓 Разблокировать» → active again with the frozen days back.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from svbg.core.settings import Change
from svbg.ext.api import ModuleState
from tests.e2e import test_stage2_kit
from tests.e2e.conftest import OWNER_ID, AppEnv, StartApp
from tests.e2e.test_stage2_kit import GROUP, Shop, open_shop, until, until_async
from tests.e2e.test_stage3_kit import chat, tg  # noqa: F401 - the ``tg`` fixture
from tests.ext.ip_guard.kit import ConnPanel, ips

pytestmark = pytest.mark.pg


def group_cards(shop: Shop, needle: str) -> list[dict[str, Any]]:
    return [m for m in shop.tg.bot_messages(GROUP) if needle in str(m.get("text"))]


def button_data(msg: dict[str, Any], label: str) -> str:
    for row in (msg.get("reply_markup") or {}).get("inline_keyboard", []):
        for b in row:
            if label in str(b.get("text")):
                return str(b["callback_data"])
    raise AssertionError(f"no {label!r} on the card: {msg.get('reply_markup')}")


async def press_in_group(shop: Shop, msg: dict[str, Any], label: str) -> str:
    """The owner presses a card button in the admin group; returns the toast."""
    data = button_data(shop.tg.message(GROUP, int(msg["message_id"])) or msg, label)
    cq = shop.tg.push_callback(OWNER_ID, data, int(msg["message_id"]), chat_id=GROUP)["callback_query"]["id"]
    answer = await shop.tg.wait_for(
        "answerCallbackQuery", lambda c: c.params.get("callback_query_id") == cq, 15
    )
    return str(answer.params.get("text") or "")


async def test_ip_guard_warns_owner_blocks_by_button_and_unblocks(
    start_app: StartApp, app_env: AppEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(test_stage2_kit, "FakeRemnawave", ConnPanel)
    async with open_shop(start_app, app_env) as shop:
        panel: Any = shop.panel
        node = panel.add_node("NL-1")
        buyer = chat(shop, 5_701)
        await buyer.start()
        await shop.fund(5_701, 17_900)
        await buyer.press("Купить подписку", expect="Выберите срок")
        await buyer.press("1 мес.", expect="Спишем с баланса")
        await buyer.press("Оплатить 179")
        await buyer.wait_text("✅ Оплачено! Подписка", timeout=20)
        (pid,) = panel.users.keys()
        panel.by_node[node] = {int(pid): ips(30, third=7)}

        app = shop.app
        assert app.settings is not None and app.ext is not None and app.scheduler is not None
        await app.settings.apply([Change("IP_GUARD_ENABLED", "true")], source="bot", actor_id=None)
        await app.ext.drain()
        assert app.ext.state("ip_guard") is ModuleState.ACTIVE
        assert app.settings.current()["IP_GUARD_AUTO_BLOCK"] is False  # the default until calibration

        async def warned() -> bool:
            app.scheduler.trigger("ip_guard.collect")  # type: ignore[union-attr]
            return bool(group_cards(shop, "автоблок выключен"))

        await until_async(warned, timeout=40, what="the warning card in «🛡 Антиабуз»")
        assert await shop.rows("select id from ip_guard_blocks") == []
        topic = await shop.rows("select thread_id from admin_topics where kind = 'antiabuse'")
        card = group_cards(shop, "автоблок выключен")[-1]
        assert card.get("message_thread_id") == topic[0]["thread_id"]

        # manual block from the card
        await press_in_group(shop, card, "Заблокировать")
        await press_in_group(shop, card, "Да, заблокировать")
        sub_id = (await shop.rows("select id from subscriptions"))[0]["id"]

        async def blocked() -> bool:
            rows = await shop.rows("select hold_kind, panel_status from subscriptions where id = $1", sub_id)
            return rows[0]["hold_kind"] == "ip_guard" and panel.users[pid]["status"] == "DISABLED"

        await until_async(blocked, timeout=30, what="frozen and disabled in the panel")
        sub = (
            await shop.rows("select hold_frozen_seconds, paid_until from subscriptions where id = $1", sub_id)
        )[0]
        frozen = int(sub["hold_frozen_seconds"])
        assert timedelta(seconds=frozen) > timedelta(days=29)  # the rest of the month is kept
        await until(
            lambda: panel.calls("/connections/drop", "POST"), timeout=20, what="IPs dropped on the node"
        )
        await until(
            lambda: any("заблок" in str(m.get("text")).lower() for m in shop.tg.bot_messages(5_701)),
            timeout=20,
            what="the user is told",
        )

        # 🔓 unblock by the button of the block card
        def block_cards() -> list[dict[str, Any]]:
            return [
                m
                for m in shop.tg.bot_messages(GROUP)
                if any(
                    "Разблокировать" in str(b.get("text"))
                    for row in (m.get("reply_markup") or {}).get("inline_keyboard", [])
                    for b in row
                )
            ]

        await until(block_cards, timeout=20, what="the block card with «🔓 Разблокировать»")
        block_card = block_cards()[-1]
        await press_in_group(shop, block_card, "🔓 Разблокировать")
        await press_in_group(shop, block_card, "Разблокировать")

        async def active() -> bool:
            rows = await shop.rows("select hold_kind from subscriptions where id = $1", sub_id)
            return rows[0]["hold_kind"] is None and panel.users[pid]["status"] == "ACTIVE"

        await until_async(active, timeout=30, what="active again")
        after = (await shop.rows("select paid_until from subscriptions where id = $1", sub_id))[0][
            "paid_until"
        ]
        expected = datetime.now(UTC) + timedelta(seconds=frozen)
        assert abs(after - expected) < timedelta(minutes=5)  # the frozen days came back
        blocks = await shop.rows("select status, reason from ip_guard_blocks")
        assert blocks == [{"status": "unblocked", "reason": "manual"}]
        audit = await shop.rows("select action from admin_audit where action like 'ip_guard.%' order by id")
        assert [a["action"] for a in audit] == ["ip_guard.block", "ip_guard.unblock"]
