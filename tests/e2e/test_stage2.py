"""Stage 2 end to end (stage2-contracts «E2E acceptance», 07 §5 «Этап 2»): the whole application on a real
PostgreSQL, the fake Telegram Bot API, the fake Remnawave panel and the fake RollyPay / CryptoBot desks.

* ``/start`` → trial → «Подключиться»;
* a purchase with enough balance; a purchase with a shortfall → «Пополнить на X» → a signed RollyPay webhook
  → the same message becomes «✅ … + 🔗 Подключиться» with no user action;
* a webhook after the auto-complete window → money on the balance + a notification;
* two concurrent webhooks → one credit, one fulfill; a double tap on «Оплатить» → one debit;
* a CryptoBot top-up; a Stars invoice → ``pre_checkout_query`` → ``successful_payment`` → credited;
* a manual transfer → the receipt card in «💳 Оплаты» → a group member without rights gets «Нет прав», the
  owner confirms → credited;
* devices list / delete through the writer; link reissue;
* sales events in the admin topics (💳 / 🎁 / 📦), the settings registry and the wiring.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from svbg.app import OPTIONAL_UI_MODULES
from tests.e2e.conftest import AppEnv, StartApp
from tests.e2e.test_stage2_kit import GROUP, OWNER_ID, Shop, open_shop, until, until_async

pytestmark = pytest.mark.pg


def group_texts(shop: Shop, thread: int | None = None) -> list[str]:
    return [
        str(c.params.get("text"))
        for c in shop.tg.calls
        if c.ok
        and c.method in ("sendMessage", "editMessageText")
        and c.params.get("chat_id") == GROUP
        and (thread is None or c.params.get("message_thread_id") == thread)
    ]


async def thread_of(shop: Shop, kind: str) -> int:
    rows = await shop.rows("select thread_id from admin_topics where kind = $1", kind)
    assert rows and rows[0]["thread_id"], f"topic {kind} has no thread"
    return int(rows[0]["thread_id"])


async def jobs_settled(shop: Shop) -> bool:
    rows = await shop.rows("select count(*) as n from jobs where status in ('ready', 'running')")
    return rows[0]["n"] == 0


# ------------------------------------------------------------------------------------------------ wiring


async def test_stage2_parts_are_wired(start_app: StartApp, app_env: AppEnv) -> None:
    async with open_shop(start_app, app_env) as shop:
        app = shop.app
        assert "svbg.tg.admin.plans" in OPTIONAL_UI_MODULES and "svbg.tg.admin.plans" in app.wired_modules
        assert app.deps is not None and app.deps.catalog is app.catalog and app.deps.billing is app.billing
        names = set(app.components.names())
        assert {"payments.rollypay", "payments.stars", "payments.cryptobot", "payments.manual"} <= names
        assert app.pay_instances is not None
        for slug in ("rollypay", "stars", "cryptobot", "manual"):
            inst = app.pay_instances.by_slug(slug)
            assert inst is not None and inst.enabled, slug
        for kind in (
            "billing.fulfill",
            "billing.ui",
            "payments.verify",
            "subscriptions.event",
            "notify.user",
        ):
            assert kind in app.job_handlers, kind
        assert {"panel.hwid_delete", "panel.hwid_reset"} <= set(app.job_handlers)
        # Telegram must deliver what the user path and the required channel listen to
        # (the fake filters getUpdates by allowed_updates; the channel test relies on it as well)
        assert app.dispatcher is not None
        allowed = set(app.dispatcher.resolve_used_update_types())
        assert {"message", "callback_query", "pre_checkout_query", "chat_member"} <= allowed
        # secrets of the cash desks are stored encrypted, never in clear text
        rows = await shop.rows("select config from payment_instances where slug = 'rollypay'")
        assert rows and "rp-whsec" not in rows[0]["config"] and rows[0]["config"].startswith("enc:")
        snap = app.settings.current()  # type: ignore[union-attr]
        assert snap["WALLET_TOPUP_PRESETS"] == [] and snap["CHANNEL_LEAVE_ACTION"] == "trial"
        assert snap["PAY_STARS_RATE"] == "1"


# ------------------------------------------------------------------------------------------------ trial


async def test_start_trial_then_connect_in_the_same_message(start_app: StartApp, app_env: AppEnv) -> None:
    async with open_shop(start_app, app_env) as shop:
        anna = shop.person(5_001)
        main = await anna.start()
        assert "Привет" in anna.text() and "Подписки пока нет" in anna.text()
        await anna.press("Попробовать бесплатно", expect="Пробный период на 3 дн. активирован")
        await anna.wait_text("Готово! Пробный период до", timeout=20)
        assert anna.main == main
        sub = (await shop.rows("select * from subscriptions where user_id = $1", await shop.user_id(5_001)))[
            0
        ]
        assert sub["is_trial"] and sub["link_state"] == "linked"
        connect = anna.button("Подключиться")
        assert connect.get("web_app", {}).get("url") == sub["subscription_url"]
        assert len(shop.panel.users) == 1
        # 🎁 in the admin group
        trials = await thread_of(shop, "trials")
        await until(lambda: any("Триал на 3 дн." in t for t in group_texts(shop, trials)), what="🎁 topic")
        # 👤 the first /start of a new user (low priority: folded into a digest in a burst)
        newcomers = await thread_of(shop, "new_users")
        await until(
            lambda: any(
                "Новый пользовател" in t or "новых пользовател" in t for t in group_texts(shop, newcomers)
            ),
            timeout=20,
            what="👤 topic",
        )


# ------------------------------------------------------------------------------------------------ purchase


async def test_purchase_with_enough_balance(start_app: StartApp, app_env: AppEnv) -> None:
    async with open_shop(start_app, app_env) as shop:
        boris = shop.person(5_002)
        await boris.start()
        await shop.fund(5_002, 20_000)
        await boris.press("Купить подписку", expect="Выберите срок")
        await boris.press("1 мес.", expect="Спишем с баланса 179 ₽, останется 21 ₽.")
        await boris.press("Оплатить 179")
        await boris.wait_text("✅ Оплачено! Подписка", timeout=20)
        assert boris.button("Подключиться").get("web_app")
        assert await shop.balance(5_002) == 2_100
        uid = await shop.user_id(5_002)
        orders = await shop.rows("select kind, status from orders where user_id = $1", uid)
        assert orders == [{"kind": "new", "status": "fulfilled"}]
        await shop.assert_wallet_invariants()
        payments_thread = await thread_of(shop, "payments")
        await until(
            lambda: any("Оплата с баланса" in t and "179" in t for t in group_texts(shop, payments_thread)),
            what="💳 purchase from the balance",
        )
        subs_thread = await thread_of(shop, "subscriptions")
        await until(
            lambda: any("Новая подписка" in t for t in group_texts(shop, subs_thread)),
            what="📦 new subscription",
        )


async def test_shortfall_tops_up_with_rollypay_and_completes_by_itself(
    start_app: StartApp, app_env: AppEnv
) -> None:
    async with open_shop(start_app, app_env) as shop:
        vera = shop.person(5_003)
        main = await vera.start()
        await shop.fund(5_003, 5_000)
        await vera.press("Купить подписку", expect="Выберите срок")
        await vera.press("1 мес.", expect="Не хватает 129 ₽")
        # RollyPay takes at least 179 ₽: the button offers 179 ₽, the surplus stays on the balance
        await vera.press("СБП", expect="Счёт на 179 ₽ готов")
        pay = vera.button("Оплатить")
        assert str(pay.get("url", "")).startswith("https://pay.rollypay.fake/")
        payment = (await shop.payments_of(5_003))[0]
        assert payment["status"] == "pending" and payment["amount_minor"] == 17_900
        assert shop.desk.created[0]["order_id"] == payment["id"]  # the opaque payment id only
        shop.desk.set_status(payment["external_id"], "paid")
        status = await shop.desk.send_webhook(shop.webhook_url("rollypay"), payment["external_id"])
        assert status == 200
        await vera.wait_text("✅ Оплачено! Подписка", message_id=main, timeout=20)
        assert vera.button("Подключиться", main).get("web_app")
        assert await shop.balance(5_003) == 5_000 + 17_900 - 17_900
        uid = await shop.user_id(5_003)
        orders = await shop.rows("select kind, status from orders where user_id = $1 order by id", uid)
        assert [(o["kind"], o["status"]) for o in orders] == [("new", "fulfilled"), ("topup", "credited")]
        await shop.assert_wallet_invariants()
        payments_thread = await thread_of(shop, "payments")
        await until(
            lambda: any("Пополнение 179" in t for t in group_texts(shop, payments_thread)), what="💳 top-up"
        )


async def test_payment_after_the_window_lands_on_the_balance_with_a_notice(
    start_app: StartApp, app_env: AppEnv
) -> None:
    async with open_shop(start_app, app_env) as shop:
        gleb = shop.person(5_004)
        main = await gleb.start()
        await gleb.press("Купить подписку", expect="Выберите срок")
        await gleb.press("1 мес.", expect="Не хватает 179 ₽")
        await gleb.press("СБП", expect="Счёт на 179 ₽ готов")
        uid = await shop.user_id(5_004)
        await shop.db.raw(
            "update orders set autocomplete_until = now() - interval '1 minute' "
            "where user_id = $1 and kind = 'new'",
            uid,
        )
        payment = (await shop.payments_of(5_004))[0]
        shop.desk.set_status(payment["external_id"], "paid")
        assert await shop.desk.send_webhook(shop.webhook_url("rollypay"), payment["external_id"]) == 200
        await gleb.wait_text("Зачислено 179 ₽ на баланс", message_id=main, timeout=20)
        assert gleb.button("Купить «Стандарт»", main)
        assert await shop.balance(5_004) == 17_900
        orders = await shop.rows("select kind, status from orders where user_id = $1 order by id", uid)
        assert [(o["kind"], o["status"]) for o in orders] == [("new", "expired"), ("topup", "credited")]
        await shop.assert_wallet_invariants()


async def test_two_concurrent_webhooks_credit_once_and_fulfill_once(
    start_app: StartApp, app_env: AppEnv
) -> None:
    async with open_shop(start_app, app_env) as shop:
        dana = shop.person(5_005)
        main = await dana.start()
        await dana.press("Купить подписку", expect="Выберите срок")
        await dana.press("1 мес.", expect="Не хватает 179 ₽")
        await dana.press("СБП", expect="Счёт на 179 ₽ готов")
        payment = (await shop.payments_of(5_005))[0]
        shop.desk.set_status(payment["external_id"], "paid")
        url = shop.webhook_url("rollypay")
        ext = payment["external_id"]
        # three different bodies (another timestamp format) for the same payment, at the same moment
        statuses = await asyncio.gather(
            shop.desk.send_webhook(url, ext, ts_format="seconds"),
            shop.desk.send_webhook(url, ext, ts_format="ms"),
            shop.desk.send_webhook(url, ext, ts_format="iso"),
        )
        assert set(statuses) == {200}
        await dana.wait_text("✅ Оплачено! Подписка", message_id=main, timeout=20)
        await until_async(lambda: jobs_settled(shop), what="jobs settled")
        uid = await shop.user_id(5_005)
        ledger = await shop.rows("select reason from wallet_ledger where user_id = $1 order by id", uid)
        assert [r["reason"] for r in ledger] == ["topup", "purchase"]
        fulfills = await shop.rows("select id from jobs where kind = 'billing.fulfill'")
        assert len(fulfills) == 1
        events = await shop.rows(
            "select e.kind from subscription_events e join subscriptions s on s.id = e.subscription_id "
            "where s.user_id = $1 and e.ref_type = 'order'",
            uid,
        )
        assert len(events) == 1
        assert len(shop.panel.users) == 1
        await shop.assert_wallet_invariants()


async def test_double_tap_on_pay_debits_once(start_app: StartApp, app_env: AppEnv) -> None:
    async with open_shop(start_app, app_env) as shop:
        egor = shop.person(5_006)
        main = await egor.start()
        await shop.fund(5_006, 40_000)
        await egor.press("Купить подписку", expect="Выберите срок")
        await egor.press("1 мес.", expect="Спишем с баланса 179 ₽")
        data = str(egor.button("Оплатить 179").get("callback_data"))
        await asyncio.gather(*(egor.click(data, message_id=main) for _ in range(3)))
        await egor.wait_text("✅ Оплачено! Подписка", message_id=main, timeout=20)
        await until_async(lambda: jobs_settled(shop), what="jobs settled")
        uid = await shop.user_id(5_006)
        purchases = await shop.rows(
            "select amount_minor from wallet_ledger where user_id = $1 and reason = 'purchase'", uid
        )
        assert purchases == [{"amount_minor": -17_900}]
        assert await shop.balance(5_006) == 40_000 - 17_900
        await shop.assert_wallet_invariants()


# ------------------------------------------------------------------------------------------------ top-ups


async def test_balance_topup_with_cryptobot_webhook(start_app: StartApp, app_env: AppEnv) -> None:
    async with open_shop(start_app, app_env) as shop:
        zhenya = shop.person(5_007)
        await zhenya.start()
        await zhenya.press("Баланс", expect="Выберите сумму пополнения")
        await zhenya.press("499", expect="Пополнение на 499 ₽")
        await zhenya.press("Крипта", expect="Счёт на 499 ₽ готов")
        payment = (await shop.payments_of(5_007))[0]
        invoice = shop.crypto.invoice_for(payment["id"])
        shop.crypto.pay(invoice.invoice_id)
        assert await shop.crypto.send_webhook(shop.webhook_url("cryptobot"), invoice.invoice_id) == 200
        await zhenya.wait_text("Зачислено 499 ₽", timeout=20)
        assert await shop.balance(5_007) == 49_900
        await shop.assert_wallet_invariants()


async def test_stars_invoice_pre_checkout_and_successful_payment(
    start_app: StartApp, app_env: AppEnv
) -> None:
    async with open_shop(start_app, app_env) as shop:
        zoya = shop.person(5_008)
        main = await zoya.start()
        await zoya.press("Купить подписку", expect="Выберите срок")
        await zoya.press("1 мес.", expect="Не хватает 179 ₽")
        await zoya.press("Stars", expect="Счёт на")
        link = str(zoya.button("Оплатить").get("url"))
        assert link in shop.tg.invoices
        invoice = shop.tg.invoices[link]
        payment = (await shop.payments_of(5_008))[0]
        assert invoice["payload"] == payment["id"] and invoice["currency"] == "XTR"
        stars = invoice["prices"][0]["amount"]
        assert stars == payment["amount_minor"] == 179  # PAY_STARS_RATE = 1 ₽ per ⭐
        # a changed amount is refused before money moves; the right one is accepted
        bad = shop.tg.push_pre_checkout(5_008, payment["id"], currency="XTR", total_amount=stars - 1)
        good = shop.tg.push_pre_checkout(5_008, payment["id"], currency="XTR", total_amount=stars)
        await until(lambda: {bad, good} <= set(shop.tg.pre_checkout_answers), what="pre-checkout answers")
        assert shop.tg.pre_checkout_answers[good] == (True, None)
        assert shop.tg.pre_checkout_answers[bad][0] is False
        for _ in range(2):  # Telegram never repeats it, but a duplicate must not credit twice
            shop.tg.push_successful_payment(
                5_008, payment["id"], currency="XTR", total_amount=stars, charge_id="tg-charge-1"
            )
        await zoya.wait_text("✅ Оплачено! Подписка", message_id=main, timeout=20)
        await until_async(lambda: jobs_settled(shop), what="jobs settled")
        uid = await shop.user_id(5_008)
        ledger = await shop.rows("select reason from wallet_ledger where user_id = $1 order by id", uid)
        assert [r["reason"] for r in ledger] == ["topup", "purchase"]
        await shop.assert_wallet_invariants()


async def test_manual_transfer_receipt_card_rights_and_confirmation(
    start_app: StartApp, app_env: AppEnv
) -> None:
    async with open_shop(start_app, app_env) as shop:
        ilya = shop.person(5_009)
        await ilya.start()
        await ilya.press("Баланс", expect="Выберите сумму пополнения")
        await ilya.press("179", expect="Пополнение на 179 ₽")
        await ilya.press("Перевод", expect="Перевод на 179 ₽")
        assert "Карта 2200" in ilya.text()
        begin = len(shop.tg.calls)
        shop.tg.push_photo(5_009, "receipt-photo-1", caption="перевёл")
        await shop.tg.wait_for(
            "sendMessage",
            lambda c: c.params.get("chat_id") == 5_009 and "Чек получен" in str(c.params.get("text")),
            15,
            start=begin,
        )
        payments_thread = await thread_of(shop, "payments")
        card = await shop.tg.wait_for(
            "sendMessage",
            lambda c: (
                c.ok and c.params.get("chat_id") == GROUP and "Чек ручной оплаты" in str(c.params["text"])
            ),
            15,
            start=begin,
        )
        assert card.params.get("message_thread_id") == payments_thread
        photo = await shop.tg.wait_for(
            "sendPhoto", lambda c: c.ok and c.params.get("chat_id") == GROUP, 15, start=begin
        )
        assert photo.params.get("photo") == "receipt-photo-1"
        buttons = [b for row in card.params["reply_markup"]["inline_keyboard"] for b in row]
        # two explicit steps: «✅ Пришло 179 ₽» → «✅ Да, по чеку ровно 179 ₽»
        confirm = next(b["callback_data"] for b in buttons if "Пришло" in b["text"])
        card_id = int(card.result["message_id"])

        # a member of the admin group who is not an admin of the bot
        intruder = 5_999
        begin = len(shop.tg.calls)
        cq = shop.tg.push_callback(intruder, confirm, card_id, chat_id=GROUP)["callback_query"]["id"]
        denied = await shop.tg.wait_for(
            "answerCallbackQuery", lambda c: c.params.get("callback_query_id") == cq, 15, start=begin
        )
        assert denied.params.get("text") == "Нет прав" and denied.params.get("show_alert") is True
        assert await shop.balance(5_009) == 0
        # a non-staff press writes nothing (no audit spam from forged callbacks); staff without
        # payments.confirm is audited — tests/billing/test_receipts_router.py
        audit = await shop.rows("select action from admin_audit where action like 'payments.confirm%'")
        assert audit == []

        # the owner confirms in two steps; a second press is answered «Уже решено»
        cq = shop.tg.push_callback(OWNER_ID, confirm, card_id, chat_id=GROUP)["callback_query"]["id"]
        ask = await shop.tg.wait_for(
            "answerCallbackQuery", lambda c: c.params.get("callback_query_id") == cq, 15, start=begin
        )
        assert "Сверьте сумму" in str(ask.params.get("text"))
        assert await shop.balance(5_009) == 0
        await until(
            lambda: any(
                "по чеку ровно" in str(b.get("text")) for b in _card_buttons(shop.tg.message(GROUP, card_id))
            ),
            what="the «are you sure» step on the card",
        )
        sure = next(
            b["callback_data"]
            for b in _card_buttons(shop.tg.message(GROUP, card_id))
            if "по чеку ровно" in str(b.get("text"))
        )
        cq = shop.tg.push_callback(OWNER_ID, sure, card_id, chat_id=GROUP)["callback_query"]["id"]
        ok = await shop.tg.wait_for(
            "answerCallbackQuery", lambda c: c.params.get("callback_query_id") == cq, 15, start=begin
        )
        assert "зачислены" in str(ok.params.get("text"))
        await ilya.wait_text("Зачислено 179 ₽", timeout=20)
        assert await shop.balance(5_009) == 17_900
        cq = shop.tg.push_callback(OWNER_ID, sure, card_id, chat_id=GROUP)["callback_query"]["id"]
        again = await shop.tg.wait_for(
            "answerCallbackQuery", lambda c: c.params.get("callback_query_id") == cq, 15, start=begin
        )
        assert "Уже решено" in str(again.params.get("text"))
        await until(
            lambda: "Подтверждено" in str((shop.tg.message(GROUP, card_id) or {}).get("text")),
            what="the card shows the decision",
        )
        assert _card_buttons(shop.tg.message(GROUP, card_id)) == []  # no buttons left on a decided card
        rows = await shop.rows("select status, decided_amount_minor from manual_receipts")
        assert rows == [{"status": "confirmed", "decided_amount_minor": 17_900}]
        await shop.assert_wallet_invariants()


def _card_buttons(message: dict[str, Any] | None) -> list[dict[str, Any]]:
    markup = (message or {}).get("reply_markup") or {}
    return [b for row in markup.get("inline_keyboard") or [] for b in row]


# ------------------------------------------------------------------------------------------------ devices


async def _bought(shop: Shop, telegram_id: int) -> dict[str, Any]:
    person = shop.person(telegram_id)
    await person.start()
    await shop.fund(telegram_id, 20_000)
    await person.press("Купить подписку", expect="Выберите срок")
    await person.press("1 мес.", expect="Спишем с баланса")
    await person.press("Оплатить 179")
    await person.wait_text("✅ Оплачено! Подписка", timeout=20)
    uid = await shop.user_id(telegram_id)
    return (await shop.rows("select * from subscriptions where user_id = $1", uid))[0]


async def test_devices_list_and_delete_through_the_writer(start_app: StartApp, app_env: AppEnv) -> None:
    async with open_shop(start_app, app_env) as shop:
        sub = await _bought(shop, 5_010)
        panel_id = int(sub["panel_user_id"])
        shop.panel.add_device(panel_id, "hw-iphone", platform="iOS")
        shop.panel.add_device(panel_id, "hw-win", platform="Windows")
        kira = shop.person(5_010)
        await kira.start()
        await kira.press("Устройства")
        await kira.wait_text("Устройства: 2 из 5", timeout=20)
        await kira.press("🗑 1", expect="Устройства: 1 из 5")
        await until(
            lambda: [d["hwid"] for d in shop.panel.devices.get(panel_id, [])] == ["hw-win"],
            timeout=20,
            what="device deleted in the panel",
        )


async def test_reissue_link(start_app: StartApp, app_env: AppEnv) -> None:
    async with open_shop(start_app, app_env) as shop:
        old = await _bought(shop, 5_011)
        lev = shop.person(5_011)
        await lev.start()
        await lev.press("Подключиться", expect="Подписка действует до")
        await lev.press("Перевыпустить ссылку", expect="Старая ссылка перестанет работать")
        await lev.press("Да, перевыпустить")
        await lev.wait_text("Новая ссылка готова", timeout=20)
        new = (await shop.rows("select subscription_url from subscriptions where id = $1", old["id"]))[0]
        assert new["subscription_url"] != old["subscription_url"]
        assert lev.button("Подключиться").get("web_app", {}).get("url") == new["subscription_url"]


# ------------------------------------------------------------------------------------------------ channel


async def test_required_channel_trial_is_disabled_on_leave_and_restored_on_return(
    start_app: StartApp, app_env: AppEnv
) -> None:
    channel = -1_009_000_000_001
    extra: dict[str, str | None] = {
        "REQUIRED_CHANNEL_ID": str(channel),
        "REQUIRED_CHANNEL_URL": "https://t.me/svbg_channel",
        "TRIAL_AUDIENCE": "channel_members",
    }
    async with open_shop(start_app, app_env, extra_env=extra) as shop:
        shop.tg.chat_members[(channel, 5_012)] = "left"
        mira = shop.person(5_012)
        await mira.start()
        await mira.press("Попробовать бесплатно")
        await mira.wait_text("Подпишитесь", timeout=15)
        shop.tg.chat_members[(channel, 5_012)] = "member"
        await mira.press("Я подписался")
        await mira.wait_text("Готово! Пробный период до", timeout=20)
        uid = await shop.user_id(5_012)
        panel_user = next(iter(shop.panel.users.values()))
        assert panel_user["status"] == "ACTIVE"

        shop.tg.push_chat_member(channel, 5_012, old="member", new="left")
        await until(
            lambda: panel_user["status"] == "DISABLED", timeout=20, what="trial disabled in the panel"
        )
        sub = (await shop.rows("select disabled_reason from subscriptions where user_id = $1", uid))[0]
        assert sub["disabled_reason"] == "channel_left"
        subs_thread = await thread_of(shop, "subscriptions")
        await until(
            lambda: any("Отписка от канала" in t for t in group_texts(shop, subs_thread)),
            what="📦 channel left",
        )

        shop.tg.push_chat_member(channel, 5_012, old="left", new="member")
        await until(lambda: panel_user["status"] == "ACTIVE", timeout=20, what="trial enabled again")


# ------------------------------------------------------------------------------------------------ catalog


async def test_locations_are_synced_as_soon_as_the_panel_answers(
    start_app: StartApp, app_env: AppEnv
) -> None:
    """The first start applies ``REMNAWAVE_*`` from ``.env`` after the scheduler's run-at-start pass: the
    squads must still reach ``locations`` right away (the owner's preset needs them), not 10 min later."""
    async with open_shop(start_app, app_env) as shop:

        async def synced() -> bool:
            rows = await shop.rows("select squad_uuid from locations where missing_since is null")
            return [r["squad_uuid"] for r in rows] == [shop.squad]

        await until_async(synced, timeout=20, what="panel squads in locations")
