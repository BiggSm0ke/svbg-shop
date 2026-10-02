"""IP Guard in Telegram: rights on every press (from the database), confirmations, card keyboards, the
aiogram adapter, the admin screen rows and the user-screen slots (≤ 1 SQL)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from svbg.ext.api import SlotCall
from svbg.ext.ip_guard import runtime, texts
from svbg.ext.ip_guard.tg import CardActions, Press, cards_router, screen_rows
from tests.ext.ip_guard.kit import GuardEnv, guard_env
from tests.subscriptions.kit import frozen_clock

OWNER_TG = 1


async def staff(g: GuardEnv, tg: int, role: str, perms: list[str] | None = None) -> int:
    rows = await g.db.raw(
        "insert into users (telegram_id, role, perms) values ($1, $2, $3::jsonb) returning id",
        tg,
        role,
        json.dumps(perms or []),
    )
    return int(rows[0]["id"])


def actions(g: GuardEnv) -> CardActions:
    async def owners() -> frozenset[int]:
        return frozenset({OWNER_TG})

    return CardActions(lambda: g.service, g.db, owners)


async def test_rights_are_checked_on_every_press(pg_dsn: str) -> None:
    async with guard_env(pg_dsn) as g:
        with frozen_clock():
            sid, _ = await g.blocked_sub(301)
            await g.service.run_pass()
            await g.service.manual_block(sid, actor_id=None)
            block_id = int((await g.one("select id from ip_guard_blocks"))["id"])
            await staff(g, 10, "user")
            await staff(g, 11, "support")
            admin_id = await staff(g, 12, "admin", ["ip_guard.unblock"])
            await staff(g, 13, "admin", ["ip_guard.block"])
            act = actions(g)
            # a member of the admin group who is not staff of the bot / not a user at all
            for tg in (10, 99):
                press = await act.press(f"ipg:uy:{block_id}", tg)
                assert press.alert and "Только для администраторов" in press.toast
            # support may look but not unblock; an admin without the right neither
            assert (await act.press(f"ipg:c:block:{block_id}", 11)).keyboard is not None
            assert (await act.press(f"ipg:uy:{block_id}", 11)).alert
            assert (await act.press(f"ipg:uy:{block_id}", 13)).alert
            assert (await g.one("select status from ip_guard_blocks"))["status"] == "active"
            # «🔓 Разблокировать» asks first, then acts
            ask = await act.press(f"ipg:u:{block_id}", 12)
            assert ask.keyboard is not None
            assert [b.callback_data for b in ask.keyboard[0]] == [f"ipg:uy:{block_id}", f"ipg:ur:{block_id}"]
            done = await act.press(f"ipg:uy:{block_id}", 12)
            assert done.toast == "Разблокировано" and done.keyboard == []
            row = await g.one("select status, unblocked_by from ip_guard_blocks")
            assert (row["status"], row["unblocked_by"]) == ("unblocked", admin_id)
            # the owner (OWNER_IDS) has every right even without a users row
            again = await act.press(f"ipg:uy:{block_id}", OWNER_TG)
            assert again.toast == "Уже сделано"


async def test_close_needs_a_picked_reason_and_is_audited(pg_dsn: str) -> None:
    async with guard_env(pg_dsn) as g:
        with frozen_clock():
            sid, _ = await g.blocked_sub(303)
            await g.service.manual_block(sid, actor_id=None)
            block_id = int((await g.one("select id from ip_guard_blocks"))["id"])
            admin_id = await staff(g, 14, "admin", ["ip_guard.unblock"])
            act = actions(g)
            ask = await act.press(f"ipg:x:{block_id}", 14)
            assert ask.keyboard is not None
            datas = [row[0].callback_data for row in ask.keyboard]
            assert datas == [f"ipg:xy:{block_id}:{i}" for i in range(len(texts.CLOSE_REASONS))] + [
                f"ipg:c:block:{block_id}"
            ]
            # an old/forged button without a reason (or with an unknown one) only asks again
            for data in (f"ipg:xy:{block_id}", f"ipg:xy:{block_id}:99"):
                again = await act.press(data, 14)
                assert again.keyboard is not None and again.toast == texts.T["close_pick"]
            assert (await g.one("select status from ip_guard_blocks"))["status"] == "active"
            done = await act.press(f"ipg:xy:{block_id}:1", 14)
            assert done.toast == "Блок закрыт"
            audit = await g.one("select * from admin_audit where action = 'ip_guard.close'")
            assert (audit["actor_id"], audit["role"], audit["reason"], audit["target"]) == (
                admin_id,
                "admin",
                texts.CLOSE_REASONS[1],
                f"sub:{sid}",
            )
            assert all(len(d.encode()) <= 64 for d in datas)


async def test_alert_buttons_block_and_ack(pg_dsn: str) -> None:
    async with guard_env(pg_dsn) as g:
        with frozen_clock() as clock:
            sid, _ = await g.blocked_sub(302)
            await g.service.run_pass()
            clock.advance(seconds=60)
            await g.service.run_pass()
            alert_id = int((await g.one("select id from ip_guard_alerts"))["id"])
            await staff(g, 21, "admin", ["ip_guard.block"])
            act = actions(g)
            ask = await act.press(f"ipg:b:{alert_id}", 21)
            assert ask.keyboard is not None and ask.keyboard[0][0].callback_data == f"ipg:by:{alert_id}"
            res = await act.press(f"ipg:by:{alert_id}", 21)
            assert res.toast == "Заблокировано"
            assert (await g.one("select reason from ip_guard_blocks where subscription_id = $1", sid))[
                "reason"
            ] == "manual"
            ack = await act.press(f"ipg:ok:alert:{alert_id}", 21)
            assert ack.toast == "Уже сделано"  # blocking from the card already acknowledged it
            ips = await act.press(f"ipg:ip:alert:{alert_id}", 21)
            assert ips.document is not None
            assert (await act.press("ipg:zz:1", 21)).toast == "Кнопка устарела"
            assert (await act.press("garbage", 21)).toast == "Кнопка устарела"
            assert (await act.press("ipg:close", 21)).delete


async def test_anomaly_buttons_confirm_by_number(pg_dsn: str) -> None:
    async with guard_env(pg_dsn, IP_GUARD_AUTO_BLOCK=True) as g:
        with frozen_clock():
            for i in range(4):
                await g.blocked_sub(310 + i)
            await g.service.run_pass()
            alert_id = int((await g.one("select id from ip_guard_alerts where kind = 'anomaly'"))["id"])
            act = actions(g)
            ask = await act.press(f"ipg:ab:{alert_id}:4", OWNER_TG)
            assert ask.keyboard is not None and ask.keyboard[0][0].callback_data == f"ipg:aby:{alert_id}:4"
            stale = await act.press(f"ipg:aby:{alert_id}:2", OWNER_TG)
            assert stale.alert and "Список изменился" in stale.toast
            done = await act.press(f"ipg:aby:{alert_id}:4", OWNER_TG)
            assert done.toast == "Заблокировано: 4" and done.keyboard == []


class _Bot:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def __call__(self, method: Any) -> Any:
        self.calls.append(method)
        return True


class _Query(SimpleNamespace):
    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        self.answered = (text, show_alert)


async def test_aiogram_adapter_edits_card_and_answers() -> None:
    class Fake:
        async def press(self, data: str, telegram_id: int) -> Press:
            if data == "ipg:boom":
                raise RuntimeError("db down")
            return Press("ok", keyboard=[], document=None)

    router = cards_router(Fake())  # type: ignore[arg-type]
    handler = router.callback_query.handlers[0].callback
    bot = _Bot()
    message = SimpleNamespace(message_id=5, chat=SimpleNamespace(id=-100), message_thread_id=7)
    q = _Query(data="ipg:u:1", from_user=SimpleNamespace(id=1), message=message, bot=bot)
    await handler(q)
    assert type(bot.calls[0]).__name__ == "EditMessageReplyMarkup" and q.answered == ("ok", False)
    q2 = _Query(data="ipg:boom", from_user=SimpleNamespace(id=1), message=message, bot=bot)
    await handler(q2)
    assert q2.answered[0] == "Не получилось, попробуйте ещё раз"


async def test_admin_screen_rows_paginate(pg_dsn: str) -> None:
    async with guard_env(pg_dsn) as g:
        with frozen_clock():
            for i in range(12):
                sid, _ = await g.blocked_sub(400 + i, n_ips=3)
                await g.service.manual_block(sid, actor_id=None)
            await g.service.run_pass()
            mark = g.db.queries
            lines, buttons, more = await screen_rows(g.db, "a", 0)
            assert g.db.queries - mark == 1
            assert len(lines) == 10 and more and buttons[0][1].startswith("block:")
            lines, _, more = await screen_rows(g.db, "a", 1)
            assert len(lines) == 2 and not more
            nodes, node_buttons, _ = await screen_rows(g.db, "n", 0)
            assert "проверяется" in nodes[0] and node_buttons[0][1] == f"node:{g.node}"
            for section in ("w", "p", "q", "e"):
                await screen_rows(g.db, section, 0)


@pytest.mark.parametrize("from_view", [True, False])
async def test_user_slots_one_query(pg_dsn: str, from_view: bool) -> None:
    async with guard_env(pg_dsn) as g:
        with frozen_clock():
            sid, _ = await g.blocked_sub(320)
            uid = int((await g.env.sub(sid))["user_id"])
            await g.service.manual_block(sid, actor_id=None)
            sub = await g.env.sub(sid)
            view: dict[str, Any] = {"subscription": sub} if from_view else {}
            ctx = SimpleNamespace(config=lambda: g.cfg)
            async with g.db.read() as conn:
                mark = g.db.queries
                model = await runtime.load_subscription(conn, SimpleNamespace(user_id=uid), view)
                assert g.db.queries - mark == (0 if from_view else 1)
            assert model == {"frozen_seconds": int(sub["hold_frozen_seconds"])}
            banner = runtime.render_banner(SlotCall(None, view, model, ctx))  # type: ignore[arg-type]
            assert banner is not None and banner.banner and "Дни заморожены" in banner.lines[0]
            assert banner.buttons[0].url == "https://t.me/support"
            assert runtime.render_status_line(SlotCall(None, view, model, ctx)) is not None  # type: ignore[arg-type]
            assert runtime.render_banner(SlotCall(None, view, None, ctx)) is None  # type: ignore[arg-type]
            async with g.db.read() as conn:
                card = await runtime.load_admin_card(conn, None, {"user_id": uid})
                assert await runtime.load_admin_card(conn, None, {}) is None
            section = runtime.render_admin_section(SlotCall(None, {}, card, ctx))  # type: ignore[arg-type]
            assert section is not None and section.buttons[0].action == "ip_guard.unblock"
            assert section.buttons[0].perm == "ip_guard.unblock"
