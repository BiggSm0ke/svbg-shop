"""The admin end to end: one home, sections by role, old buttons, the search by a typed message, cash desks.

* the owner, an admin with the right «promo» only and a support member walk the root, every section and every
  entry of it: nothing visible answers «Нет прав» or «Меню обновилось», no list of a section leads to the user
  home;
* buttons of old messages (``admin``, ``adm``, ``adma:rf``, ``settings_root``, a settings section and card)
  still open what they opened;
* on the root a typed ID, @username or a forwarded message opens the person's card, even for a staff member
  who once had a support ticket (the query is not copied into the support topic);
* «🏦 Кассы»: a working desk shows the webhook address with a copy button, turns off and on again (the keys
  are probed against the provider);
* the staff «/» command menus are set after the start.
"""

from __future__ import annotations

from typing import Any

import pytest

from svbg.tg.admin.menu import HUBS
from svbg.tg.ui.codec import encode
from tests.e2e.conftest import OWNER_ID, AppEnv, StartApp
from tests.e2e.test_stage2_kit import open_shop, until
from tests.e2e.test_stage3_kit import Chat, chat, tg  # noqa: F401

pytestmark = pytest.mark.pg

FORBIDDEN = ("Нет прав", "Меню обновилось")
CARD = "Telegram ID: <code>5401</code>"  # the card of the client (the fake keeps HTML)
RENDERS = "editMessageText|editMessageCaption|editMessageMedia|sendMessage|sendPhoto"


async def open_(p: Chat, data: str) -> str:
    """Press a button by its data and wait until the screen is drawn; returns the toast."""
    start = len(p.tg.calls)
    p.follow()
    toast = await p.click(data)
    assert toast not in FORBIDDEN, (p.telegram_id, data, toast)
    await p.tg.wait_for(RENDERS, lambda c: c.params.get("chat_id") == p.telegram_id, timeout=15, start=start)
    p.follow()
    return toast


def datas(p: Chat) -> list[str]:
    return [str(b["callback_data"]) for b in p.buttons() if b.get("callback_data")]


async def walk(p: Chat) -> list[str]:
    """The root, every section of it and every entry of every section."""
    await open_(p, encode("adm"))
    opened: list[str] = []
    for data in datas(p):
        if data == encode("home") or ":adma:" in data or data == encode("adm"):
            continue
        await open_(p, encode("adm"))
        await open_(p, data)
        opened.append(data)
        if data.split(":")[1] not in HUBS:
            continue
        assert encode("home") not in datas(p), data
        for entry in [d for d in datas(p) if d != encode("adm") and ":ce.a:" not in d]:
            await open_(p, data)
            await open_(p, entry)
            opened.append(entry)
            assert encode("home") not in datas(p), (entry, p.text()[:120])  # back is to the admin
    return opened


async def test_every_role_walks_its_sections(start_app: StartApp, app_env: AppEnv) -> None:
    async with open_shop(start_app, app_env) as shop:
        owner = chat(shop, OWNER_ID)
        await owner.start()
        seen = await walk(owner)
        for code in (
            "adm.u",
            "plans",
            "adm.pay",
            "adm.mk",
            "adm.c",
            "adm.l",
            "adm.s",
            "adm.sys",
            "apay",
            "bc",
        ):
            assert any(d.split(":")[1] == code for d in seen), (code, seen)

        marketer, helper = chat(shop, 5_701), chat(shop, 5_702)
        await marketer.start()
        await helper.start()
        await shop.db.raw(
            "update users set role = 'admin', perms = '[\"promo\"]'::jsonb where telegram_id = 5701"
        )
        await shop.db.raw("update users set role = 'support' where telegram_id = 5702")
        assert shop.app.users is not None
        shop.app.users.invalidate(5_701)
        shop.app.users.invalidate(5_702)
        seen = await walk(marketer)
        assert {d.split(":")[1] for d in seen} >= {"adm.u", "adm.mk", "prm", "ads"}
        assert not {"adm.pay", "adm.sys", "plans", "apay", "bc"} & {d.split(":")[1] for d in seen}
        seen = await walk(helper)
        assert {d.split(":")[1] for d in seen} <= {"adm.u", "au.find", "au.new", "au.all", "adm.x", "ipguard"}
        assert "Выручка" not in helper.text()


async def test_old_buttons_and_the_command_menu(start_app: StartApp, app_env: AppEnv) -> None:
    async with open_shop(start_app, app_env) as shop:
        owner = chat(shop, OWNER_ID)
        await owner.start()
        await open_(owner, encode("admin"))
        assert "Админка" in owner.text() and "Выручка" in owner.text()
        await open_(owner, encode("adma", "rf"))
        assert "Админка" in owner.text()
        await open_(owner, encode("settings_root"))
        assert "Все настройки" in owner.text()
        await open_(owner, encode("set.sec", arg="remnawave"))
        await open_(owner, encode("set.key", arg="TRIAL_DAYS"))
        assert "Ключ в .env: <code>TRIAL_DAYS</code>" in owner.text()
        # the home of the owner has one staff button
        await owner.start()
        assert [str(b["text"]) for b in owner.buttons() if "Админка" in str(b["text"])] == ["🛠 Админка"]
        assert not any("Настройки" in str(b["text"]) for b in owner.buttons())
        # «/» menu of the owner, set in the background after the start
        await until(
            lambda: ("chat", OWNER_ID) in shop.tg.commands, timeout=20, what="the owner's command menu"
        )
        names = [c["command"] for c in shop.tg.commands[("chat", OWNER_ID)]]
        assert names[:2] == ["admin", "user"] and "settings" in names


async def test_search_by_a_typed_message(start_app: StartApp, app_env: AppEnv) -> None:
    async with open_shop(start_app, app_env, extra_env={"SUPPORT_MODE": "tickets"}) as shop:
        client = shop.person(5_401)
        await client.start()
        await shop.db.raw("update users set username = 'vasya_vpn' where telegram_id = 5401")
        owner = chat(shop, OWNER_ID)
        await owner.start()
        owner_id = await shop.user_id(OWNER_ID)
        await shop.db.raw("insert into tickets (user_id, status) values ($1, 'closed')", owner_id)  # had one

        await open_(owner, encode("adm"))
        start = len(shop.tg.calls)
        sent = shop.tg.push_message(OWNER_ID, "5401")
        await until(lambda: CARD in owner.text(), what="the card after a typed ID")
        deleted = [c.params.get("message_id") for c in shop.tg.calls[start:] if c.method == "deleteMessage"]
        assert sent["message"]["message_id"] in deleted  # the query does not stay in the chat
        assert not [c for c in shop.tg.calls[start:] if c.method in ("copyMessage", "createForumTopic")]

        await open_(owner, encode("adm"))
        shop.tg.push_message(OWNER_ID, "@vasya_vpn")
        await until(lambda: CARD in owner.text(), what="the card after @username")
        await open_(owner, encode("adm"))
        user: dict[str, Any] = {"id": 5401, "is_bot": False, "first_name": "Вася"}
        shop.tg.push_raw(
            OWNER_ID, text="помогите", forward_origin={"type": "user", "date": 0, "sender_user": user}
        )
        await until(lambda: CARD in owner.text(), what="the card after a forwarded message")
        await open_(owner, encode("adm"))
        shop.tg.push_message(OWNER_ID, "@nobody_like_this_one")
        await until(lambda: "никого не нашлось" in owner.text(), what="nothing found on the root")


async def test_cash_desk_card_copy_off_and_on(start_app: StartApp, app_env: AppEnv) -> None:
    async with open_shop(start_app, app_env, extra_env={"PUBLIC_URL": "https://bot.example.com"}) as shop:
        owner = chat(shop, OWNER_ID)
        await owner.start()
        await open_(owner, encode("adm"))
        await owner.press("💳 Оплата", expect="Кассы")
        await owner.press("🏦 Кассы", expect="Включены")
        assert any("RollyPay" in str(b["text"]) for b in owner.buttons())
        await owner.press("RollyPay", expect="Сейчас")
        instances = shop.app.pay_instances
        inst = instances.by_slug("rollypay") if instances is not None else None
        assert instances is not None and inst is not None
        url = instances.webhook_url(inst)
        assert url is not None and url.startswith("https://bot.example.com/webhooks/pay/")
        assert url in owner.text()
        copy = [b for b in owner.buttons() if b.get("copy_text")]
        assert copy and copy[0]["copy_text"]["text"] == url
        await owner.press("⏸ Выключить", expect="Касса выключена")
        assert shop.app.settings is not None
        assert shop.app.settings.current()["PAY_ROLLYPAY_ENABLED"] is False
        assert not [b for b in owner.buttons() if b.get("copy_text")]
        await owner.press("▶️ Включить", expect="Касса включена", timeout=40)
        assert shop.app.settings.current()["PAY_ROLLYPAY_ENABLED"] is True
        assert url in owner.text()
