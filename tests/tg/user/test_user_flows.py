"""User path end to end on the real stack: home card, trial, purchase from the balance, purchase with a
shortfall that completes by itself in the same message, late payment, connect, devices, reissue."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from svbg.core.clock import now
from tests.billing.kit import stub_webhook
from tests.tg.user.kit import build_user_env


async def test_home_card_for_a_new_user(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn) as env:
        _uid, tg = await env.new_user(balance=5_000)
        home = await env.open(tg)
        assert "Привет, Аня!" in home.text
        assert "Подписки пока нет." in home.text
        assert "Баланс: 50 ₽" in home.text
        labels = home.labels()
        # «Профиль» (no suffix, own colour without a subscription), balance, trial, support
        assert labels == ["👤 Профиль", "💰 Баланс: 50 ₽", "🎁 Попробовать бесплатно", "💬 Поддержка"]
        assert not any(x in labels for x in ("🛒 Купить подписку", "🔄 Продлить", "🔗 Подключиться"))
        assert "📱 Устройства" not in labels and len(labels) <= 7
        support = home.button("Поддержка")
        assert support.url == "https://t.me/svbg_support"
        assert home.button("Профиль").style is None and home.data("Профиль") == "v1:profile:o"
        section = await env.press(tg, "Профиль")
        assert "Подписки пока нет" in section.text and "попробовать бесплатно: 3 дн." in section.text
        assert section.labels()[:3] == ["🛒 Купить подписку", "💳 Пополнить", "🎁 Попробовать бесплатно"]
        assert section.button("Купить").style == "success"


async def test_trial_in_one_button_then_connect_in_the_same_message(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn) as env:
        uid, tg = await env.new_user()
        home = await env.open(tg)
        started = await env.press(tg, "Попробовать")
        assert started.message_id == home.message_id
        assert "Пробный период на 3 дн. активирован" in started.text
        await env.drain()
        ready = env.tg.shown(tg, home.message_id)
        assert "Готово! Пробный период до" in ready.text
        sub = (await env.rows("select subscription_url from subscriptions where user_id = $1", uid))[0]
        connect = ready.button("Подключиться")
        assert connect.web_app is not None and connect.web_app.url == sub["subscription_url"]
        # the home card now shows the trial and hides the trial button
        home2 = await env.click(tg, "v1:home:o", message_id=home.message_id)
        assert "🎁 Пробный период до" in home2.text
        assert "🎁 Попробовать бесплатно" not in home2.labels()
        assert "🔗 Подключиться" in home2.labels() and "🛒 Купить подписку" not in home2.labels()
        trial_btn = home2.button("Профиль")
        assert trial_btn.text == "👤 Профиль · 2 дн. 23 ч" and trial_btn.style == "danger"  # a trial is red
        section = await env.press(tg, "Профиль", message_id=home.message_id)
        assert "Статус: 🎁 пробный период" in section.text and "Осталось: 2 дн. 23 ч" in section.text
        assert "🛒 Купить подписку" in section.labels() and "🔄 Продлить" not in section.labels()
        home2 = await env.click(tg, "v1:home:o", message_id=home.message_id)
        # a second trial is refused with a toast, nothing new is created
        again = await env.click(tg, "v1:sys:trial", message_id=home.message_id)
        assert "уже был использован" in (env.tg.toasts()[-1] or "")
        assert again.text == home2.text
        assert len(await env.rows("select id from subscriptions where user_id = $1", uid)) == 1


async def test_purchase_with_enough_balance(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn) as env:
        uid, tg = await env.new_user(balance=20_000)
        home = await env.open(tg)
        await env.press(tg, "Профиль")
        periods = await env.press(tg, "Купить подписку")
        # a single plan: the plan list is skipped
        assert "Тариф 1" in periods.text and "Выберите срок" in periods.text
        labels = periods.labels()
        assert labels[0] == "1 мес. — 179 ₽"
        assert labels[1].startswith("3 мес. — 499 ₽ · 166 ₽/мес, −7%")
        checkout = await env.press(tg, "1 мес.")
        assert checkout.message_id == home.message_id
        assert "Тариф: Тариф 1" in checkout.text and "Цена: 179 ₽" in checkout.text
        assert "Спишем с баланса 179 ₽, останется 21 ₽." in checkout.text
        assert checkout.labels()[0] == "💳 Оплатить 179 ₽"
        waiting = await env.press(tg, "Оплатить")
        assert "Оформляю подписку" in waiting.text
        await env.drain()
        done = env.tg.shown(tg, home.message_id)
        assert done.text.startswith("✅ Оплачено! Подписка «Тариф 1» действует до")
        assert done.button("Подключиться").web_app is not None
        assert await env.b.balance(uid) == 2_100
        await env.b.assert_wallet_invariants()


async def test_shortfall_tops_up_and_completes_in_the_same_message(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn) as env:
        uid, tg = await env.new_user(balance=5_000)
        home = await env.open(tg)
        await env.press(tg, "Профиль")
        await env.press(tg, "Купить подписку")
        checkout = await env.press(tg, "1 мес.")
        # not enough money: the methods are on the checkout itself
        assert "Не хватает 129 ₽" in checkout.text
        assert "📱 СБП — 129 ₽" in checkout.labels() and "✏️ Другая сумма" in checkout.labels()
        # «Оплатить» from an older message: the shortfall screen with the same methods
        order_id = (await env.rows("select id from orders where user_id = $1", uid))[0]["id"]
        short = await env.click(tg, f"v1:pay:go:{order_id}")
        assert short.message_id == home.message_id
        assert "Не хватает 129 ₽" in short.text
        labels = short.labels()
        # one button per method kind; RollyPay-like minimum 100 ₽ < 129 ₽ → exactly the shortfall
        assert "📱 СБП — 129 ₽" in labels and "🏦 Перевод — 129 ₽" in labels
        assert any(label.startswith("⭐ Stars — 129") for label in labels)
        assert "✏️ Другая сумма" in labels
        assert labels[:3] == checkout.labels()[:3]
        invoice = await env.press(tg, "СБП")
        assert invoice.message_id == home.message_id
        assert "Счёт на 129 ₽ готов" in invoice.text and "Как только оплата пройдёт" in invoice.text
        pay = invoice.button("Оплатить")
        assert pay.url and pay.url.startswith("http")
        payment = (await env.rows("select id, external_id from payments where user_id = $1", uid))[0]
        status = await env.b.pay.send(
            stub_webhook("paid", ext=payment["external_id"], order=payment["id"], amount="129.00", at=now())
        )
        assert status == 200
        await env.drain()
        done = env.tg.shown(tg, home.message_id)
        assert done.text.startswith("✅ Оплачено!"), done.text
        assert done.button("Подключиться").web_app is not None
        assert await env.b.balance(uid) == 0
        await env.b.assert_wallet_invariants()
        orders = await env.rows("select kind, status from orders where user_id = $1 order by id", uid)
        assert [(o["kind"], o["status"]) for o in orders] == [("new", "fulfilled"), ("topup", "credited")]


async def test_late_payment_lands_on_balance_with_a_buy_button(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn) as env:
        uid, tg = await env.new_user()
        home = await env.open(tg)
        await env.press(tg, "Профиль")
        await env.press(tg, "Купить подписку")
        await env.press(tg, "1 мес.")
        await env.press(tg, "СБП")
        await env.db.raw(
            "update orders set autocomplete_until = now() - interval '1 minute' where kind = 'new'"
        )
        payment = (await env.rows("select id, external_id from payments where user_id = $1", uid))[0]
        await env.b.pay.send(
            stub_webhook("paid", ext=payment["external_id"], order=payment["id"], amount="179.00", at=now())
        )
        await env.drain()
        msg = env.tg.shown(tg, home.message_id)
        assert "Зачислено 179 ₽ на баланс" in msg.text
        assert await env.b.balance(uid) == 17_900
        buy = msg.data("Купить «Тариф 1»")
        checkout = await env.click(tg, buy, message_id=home.message_id)
        assert "Спишем с баланса 179 ₽" in checkout.text
        await env.b.assert_wallet_invariants()


async def test_other_amount_form_and_surplus_note(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn) as env:
        uid, tg = await env.new_user(balance=12_000)
        await env.open(tg)
        await env.press(tg, "Профиль")
        await env.press(tg, "Купить подписку")
        short = await env.press(tg, "1 мес.")  # not enough money: the methods are on the checkout
        assert "Не хватает 59 ₽" in short.text
        # the stub cash desk takes at least 100 ₽: the surplus stays on the balance and the screen says so
        assert "📱 СБП — 100 ₽" in short.labels()
        assert "остаток" in short.text or "останется на балансе" in short.text
        prompt = await env.press(tg, "Другая сумма")
        assert "Сколько пополнить" in prompt.text
        from tests.tg.ui.ui_harness import text_message

        await env.router.dispatch_message(text_message(tg, "30"))
        retry = env.tg.last(tg)
        assert "Минимум 59 ₽" in retry.text
        await env.router.dispatch_message(text_message(tg, "300"))
        methods = env.tg.last(tg)
        assert "Пополнение на 300 ₽" in methods.text
        assert "📱 СБП — 300 ₽" in methods.labels()
        assert uid


async def test_balance_presets_and_plain_topup(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn) as env:
        uid, tg = await env.new_user()
        await env.open(tg)
        bal = await env.press(tg, "Баланс")
        assert "Баланс: 0 ₽" in bal.text
        assert bal.labels()[:3] == ["179 ₽", "499 ₽", "899 ₽"]
        methods = await env.press(tg, "499")
        assert "Пополнение на 499 ₽" in methods.text
        invoice = await env.press(tg, "СБП")
        assert "Деньги зачислятся на баланс" in invoice.text
        payment = (await env.rows("select id, external_id, order_id from payments where user_id = $1", uid))[
            0
        ]
        await env.b.pay.send(
            stub_webhook("paid", ext=payment["external_id"], order=payment["id"], amount="499", at=now())
        )
        await env.drain()
        assert await env.b.balance(uid) == 49_900
        assert "Зачислено 499 ₽" in env.tg.shown(tg, invoice.message_id).text
        await env.b.assert_wallet_invariants()


async def test_stars_invoice_and_successful_payment(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn) as env:
        uid, tg = await env.new_user()
        home = await env.open(tg)
        await env.press(tg, "Профиль")
        await env.press(tg, "Купить подписку")
        await env.press(tg, "1 мес.")
        invoice = await env.press(tg, "Stars")
        link = invoice.button("Оплатить")
        assert link.url and link.url.startswith("https://t.me/$invoice")
        payment = (await env.rows("select id, amount_minor, currency from payments where user_id = $1", uid))[
            0
        ]
        assert (payment["amount_minor"], payment["currency"]) == (179, "XTR")
        cp = env.path.chat_payments
        assert await cp.decide_pre_checkout(payment["id"], "XTR", 179) is None
        assert await cp.decide_pre_checkout(payment["id"], "XTR", 100) is not None
        assert await cp.decide_pre_checkout("balance_1_100", "XTR", 179) is not None
        assert await cp.decide_pre_checkout("00000000-0000-7000-8000-000000000000", "XTR", 179) is not None
        first = await cp.credit_stars(payment["id"], charge_id="charge-1", currency="XTR", total_amount=179)
        second = await cp.credit_stars(payment["id"], charge_id="charge-1", currency="XTR", total_amount=179)
        assert first is not None and first.credited
        assert second is None or not second.credited
        await env.drain()
        assert env.tg.shown(tg, home.message_id).text.startswith("✅ Оплачено!")
        ledger = await env.b.ledger(uid)
        assert [e["reason"] for e in ledger] == ["topup", "purchase"]
        await env.b.assert_wallet_invariants()
        # paid already → refused at pre-checkout
        assert await cp.decide_pre_checkout(payment["id"], "XTR", 179) is not None


async def test_manual_transfer_details_and_receipt(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn) as env:
        from svbg.billing.receipts import Receipts

        receipts = Receipts(env.db, env.b.pay.core)
        env.path.chat_payments._receipts = receipts
        uid, tg = await env.new_user()
        assert await env.path.chat_payments.submit_receipt(uid, "ru", kind="photo", file_id="f0") is None
        await env.open(tg)
        await env.press(tg, "Баланс")
        await env.press(tg, "179")
        details = await env.press(tg, "Перевод")
        assert "Перевод на 179 ₽" in details.text and "пришлите сюда фото или PDF чека" in details.text
        reply = await env.path.chat_payments.submit_receipt(
            uid, "ru", kind="document", file_id="f1", mime_type="text/plain"
        )
        assert reply is not None and "Чек получен" not in reply
        reply = await env.path.chat_payments.submit_receipt(
            uid, "ru", kind="photo", file_id="f2", caption="вот"
        )
        assert reply is not None and "Чек получен" in reply
        rows = await env.rows("select file_id, comment, status from manual_receipts where user_id = $1", uid)
        assert rows == [{"file_id": "f2", "comment": "вот", "status": "submitted"}]


async def test_connect_screen_copy_and_qr(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn) as env:
        uid, tg = await env.new_user(balance=20_000)
        await env.open(tg)
        none = await env.click(tg, "v1:connect:o")
        assert "У вас пока нет подписки" in none.text
        await env.press(tg, "Купить подписку")
        await env.press(tg, "1 мес.")
        await env.press(tg, "Оплатить")
        await env.drain()
        url = (await env.rows("select subscription_url from subscriptions where user_id = $1", uid))[0][
            "subscription_url"
        ]
        connect = await env.click(tg, "v1:connect:o")
        assert "Подписка действует до" in connect.text
        assert connect.button("Открыть страницу").web_app.url == url
        assert connect.button("Скопировать").copy_text.text == url
        assert "📱 Устройства" in connect.labels() and "♻️ Перевыпустить ссылку" in connect.labels()
        qr = await env.press(tg, "QR-код")
        assert qr.photo and "QR-код" in qr.text
        back = await env.press(tg, "Назад", message_id=qr.message_id)
        assert not back.photo and "Подписка действует до" in back.text


async def test_devices_list_refresh_and_delete_through_the_writer(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn) as env:
        uid, tg = await env.new_user(balance=20_000)
        await env.open(tg)
        await env.press(tg, "Профиль")
        await env.press(tg, "Купить подписку")
        await env.press(tg, "1 мес.")
        await env.press(tg, "Оплатить")
        await env.drain()
        panel_id = (await env.rows("select panel_user_id from subscriptions where user_id = $1", uid))[0][
            "panel_user_id"
        ]
        env.b.s.panel.add_device(panel_id, "hw-iphone", platform="iOS")
        env.b.s.panel.devices[panel_id][-1]["deviceModel"] = "iPhone 15"
        env.b.s.panel.add_device(panel_id, "hw-win", platform="Windows")
        first = await env.click(tg, "v1:dev:o")
        assert "Загружаю список" in first.text
        assert env.panel_fetch.calls == 0  # nothing from the panel on the click
        await env.drain()
        assert env.panel_fetch.calls == 1
        listed = env.tg.last(tg)
        assert "Устройства: 2 из 5" in listed.text and "iPhone 15 · iOS" in listed.text
        deleted = await env.press(tg, "🗑 1")
        assert "Устройства: 1 из 5" in deleted.text and "iPhone" not in deleted.text
        await env.drain()
        assert [d["hwid"] for d in env.b.s.panel.devices.get(panel_id, [])] == ["hw-win"]
        forged = await env.click(tg, "v1:dev:del:1:deadbeef")
        assert "уже нет в списке" in (env.tg.toasts()[-1] or "")
        assert forged.text == deleted.text


async def test_reissue_link_turns_into_the_new_link(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn) as env:
        uid, tg = await env.new_user(balance=20_000)
        await env.open(tg)
        await env.press(tg, "Профиль")
        await env.press(tg, "Купить подписку")
        await env.press(tg, "1 мес.")
        await env.press(tg, "Оплатить")
        await env.drain()
        old = (await env.rows("select subscription_url from subscriptions where user_id = $1", uid))[0]
        await env.click(tg, "v1:connect:o")
        confirm = await env.press(tg, "Перевыпустить")
        assert "Старая ссылка перестанет работать" in confirm.text
        wait = await env.press(tg, "Да, перевыпустить")
        assert "Перевыпускаем ссылку" in wait.text
        await env.drain()
        new = (await env.rows("select subscription_url from subscriptions where user_id = $1", uid))[0]
        assert new["subscription_url"] != old["subscription_url"]
        done = env.tg.shown(tg, wait.message_id)
        assert "Новая ссылка готова" in done.text
        assert done.button("Подключиться").web_app.url == new["subscription_url"]
        # cooldown: a second reissue right away is refused
        await env.click(tg, "v1:reissue:ok", message_id=wait.message_id)
        assert "Слишком часто" in (env.tg.toasts()[-1] or "")


async def test_amount_mismatch_credits_nothing(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn) as env:
        uid, tg = await env.new_user()
        await env.open(tg)
        await env.press(tg, "Баланс")
        await env.press(tg, "179")
        await env.press(tg, "СБП")
        payment = (await env.rows("select id, external_id from payments where user_id = $1", uid))[0]
        await env.b.pay.send(
            stub_webhook("paid", ext=payment["external_id"], order=payment["id"], amount="100", at=now())
        )
        await env.drain()
        assert await env.b.balance(uid) == 0
        row = await env.b.pay.payment(payment["id"])
        assert row["status"] == "mismatch"
        assert Decimal(1) and timedelta(0) == timedelta(0)
