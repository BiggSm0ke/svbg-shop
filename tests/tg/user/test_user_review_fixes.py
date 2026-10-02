"""Review fixes of the user path: background edits never land before the click's own screen (a slow Telegram
round trip), a repeated «Оплатить» shows the result, «Создаю счёт…» is visible and a double tap does not open
a second invoice, three taps to the payment screen, devices refresh throttling and re-show only while the user
is still looking, receipts: the instance's size limit, old transfers, a slow admin chat."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest
from aiogram.methods import EditMessageText

import svbg.tg.user.chat_payments as chat_payments_mod
from svbg.billing.receipts import ReceiptDecision, Receipts, ReceiptView
from tests.tg.user.kit import UserEnv, build_user_env


class SlowAnswers:
    """``answerCallbackQuery`` held until :attr:`gate` opens — a Telegram far away while the panel is near."""

    def __init__(self, env: UserEnv) -> None:
        self._orig = env.tg.answer
        self.entered = asyncio.Event()
        self.gate = asyncio.Event()
        env.tg.answer = self.answer  # type: ignore[method-assign]

    async def answer(self, method: Any) -> None:
        self.entered.set()
        await self.gate.wait()
        await self._orig(method)


async def _race(env: UserEnv, tg: int, label: str) -> None:
    """Press ``label``; while the click waits for Telegram, the worker runs everything it queued."""
    slow = SlowAnswers(env)
    click = asyncio.create_task(env.press(tg, label))
    await asyncio.wait_for(slow.entered.wait(), 10)  # the click committed and is answering now
    drain = asyncio.create_task(env.drain())
    await asyncio.sleep(0.5)  # without the screen lock the worker draws the final message here
    slow.gate.set()
    await asyncio.wait_for(asyncio.gather(click, drain), 20)


# ---------------------------------------------------------------------------------------- ordering


async def test_paid_message_is_not_overwritten_by_a_late_processing_screen(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn) as env:
        _uid, tg = await env.new_user(balance=20_000)
        home = await env.open(tg)
        await env.press(tg, "Купить подписку")
        await env.press(tg, "1 мес.")
        await _race(env, tg, "Оплатить")
        done = env.tg.shown(tg, home.message_id)
        assert done.text.startswith("✅ Оплачено!"), done.text
        assert done.button("Подключиться").web_app is not None


async def test_trial_ready_message_is_not_overwritten_by_a_late_started_screen(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn) as env:
        _uid, tg = await env.new_user()
        home = await env.open(tg)
        await _race(env, tg, "Попробовать")
        ready = env.tg.shown(tg, home.message_id)
        assert "Готово! Пробный период до" in ready.text, ready.text
        assert ready.button("Подключиться").web_app is not None


async def test_repeated_pay_on_a_done_order_shows_connect(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn) as env:
        _uid, tg = await env.new_user(balance=20_000)
        await env.open(tg)
        await env.press(tg, "Купить подписку")
        checkout = await env.press(tg, "1 мес.")
        data = checkout.data("Оплатить")
        await env.click(tg, data, message_id=checkout.message_id)
        await env.drain()
        again = await env.click(tg, data, message_id=checkout.message_id)
        assert "Подписка действует до" in again.text and "Оформляю" not in again.text
        assert again.button("Открыть страницу").web_app is not None
        assert "Оплата получена" in (env.tg.toasts()[-1] or "")


# ---------------------------------------------------------------------------------------- invoices


async def test_first_purchase_reaches_the_invoice_in_three_taps(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn) as env:
        uid, tg = await env.new_user()
        home = await env.open(tg)
        await env.press(tg, "Купить подписку")  # 1
        checkout = await env.press(tg, "1 мес.")  # 2
        assert "Не хватает 179 ₽. Выберите, чем доплатить" in checkout.text
        labels = checkout.labels()
        assert "📱 СБП — 179 ₽" in labels and "✏️ Другая сумма" in labels
        assert not any("Оплатить" in label for label in labels)
        invoice = await env.press(tg, "СБП")  # 3
        assert invoice.message_id == home.message_id
        assert "Счёт на 179 ₽ готов" in invoice.text and "Как только оплата пройдёт" in invoice.text
        orders = await env.rows("select kind, status from orders where user_id = $1 order by id", uid)
        assert [(o["kind"], o["status"]) for o in orders] == [
            ("new", "awaiting_funds"),
            ("topup", "awaiting_payment"),
        ]


async def test_creating_invoice_is_visible_and_a_double_tap_reuses_it(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn) as env:
        uid, tg = await env.new_user()
        await env.open(tg)
        await env.press(tg, "Купить подписку")
        checkout = await env.press(tg, "1 мес.")
        data = checkout.data("СБП")
        before = len(env.tg.calls)
        first = await env.click(tg, data, message_id=checkout.message_id)
        progress = [
            c
            for c in env.tg.calls[before:]
            if isinstance(c, EditMessageText) and c.message_id == checkout.message_id
        ]
        assert progress[0].text == "⏳ Создаю счёт…" and progress[0].reply_markup is None
        # the same button again (a tap that was queued behind the first one): the same invoice
        second = await env.click(tg, data, message_id=checkout.message_id)
        pays = await env.rows("select id from payments where user_id = $1", uid)
        assert len(pays) == 1
        assert second.button("Оплатить").url == first.button("Оплатить").url
        topups = await env.rows("select id from orders where user_id = $1 and kind = 'topup'", uid)
        assert len(topups) == 1
        await env.b.assert_wallet_invariants()


async def test_method_on_checkout_pays_from_the_balance_when_it_grew(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn) as env:
        uid, tg = await env.new_user()
        await env.open(tg)
        await env.press(tg, "Купить подписку")
        checkout = await env.press(tg, "1 мес.")
        await env.b.fund(uid, 17_900)  # money arrived some other way meanwhile
        waiting = await env.press(tg, "СБП", message_id=checkout.message_id)
        assert "Оформляю подписку" in waiting.text
        assert await env.rows("select id from payments where user_id = $1", uid) == []
        await env.drain()
        assert env.tg.shown(tg, checkout.message_id).text.startswith("✅ Оплачено!")
        await env.b.assert_wallet_invariants()


# ---------------------------------------------------------------------------------------- devices


async def test_refresh_does_not_hit_the_panel_while_the_list_is_fresh(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn) as env:
        uid, tg = await env.new_user(balance=20_000)
        await env.open(tg)
        await env.press(tg, "Купить подписку")
        await env.press(tg, "1 мес.")
        await env.press(tg, "Оплатить")
        await env.drain()
        await env.click(tg, "v1:dev:o")
        await env.drain()
        assert env.panel_fetch.calls == 1
        for _ in range(5):
            shown = await env.click(tg, "v1:dev:refresh")
            await env.drain()
        assert env.panel_fetch.calls == 1
        assert "Список только что обновлён" in (env.tg.toasts()[-1] or "")
        assert "Устройства: 0 из 5" in shown.text
        await env.db.raw(
            "update user_devices set fetched_at = now() - interval '1 minute' "
            "where subscription_id = (select id from subscriptions where user_id = $1)",
            uid,
        )
        await env.click(tg, "v1:dev:refresh")
        assert "Загружаю" in (env.tg.toasts()[-1] or "")
        await env.drain()
        assert env.panel_fetch.calls == 2


async def test_devices_job_does_not_pull_the_user_back_from_another_screen(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn) as env:
        _uid, tg = await env.new_user(balance=20_000)
        await _buy_with_balance(env, tg)
        loading = await env.click(tg, "v1:dev:o")
        assert "Загружаю список" in loading.text
        buy = await env.click(tg, "v1:buy:o", message_id=loading.message_id)  # moved on
        await env.drain()
        assert env.panel_fetch.calls == 1
        assert env.tg.shown(tg, loading.message_id).text == buy.text


async def test_devices_screen_says_the_panel_is_down_after_the_last_attempt(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn) as env:
        _uid, tg = await env.new_user(balance=20_000)
        await _buy_with_balance(env, tg)

        async def down(_panel_user_id: int) -> list[Any]:
            raise OSError("connection refused")

        env.path.jobs._fetch = down
        loading = await env.click(tg, "v1:dev:o")
        assert "Загружаю список" in loading.text
        for _ in range(2):
            await env.drain(make_due=True)
            assert "Загружаю список" in env.tg.shown(tg, loading.message_id).text
        await env.drain(make_due=True)  # the third and last attempt
        failed = env.tg.shown(tg, loading.message_id)
        assert "панель временно недоступна" in failed.text and "Загружаю" not in failed.text
        assert "🔄 Обновить" in failed.labels()


async def _buy_with_balance(env: UserEnv, tg: int) -> None:
    await env.open(tg)
    await env.press(tg, "Купить подписку")
    await env.press(tg, "1 мес.")
    await env.press(tg, "Оплатить")
    await env.drain()


# ---------------------------------------------------------------------------------------- receipts


class SlowCards:
    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.posted: list[int] = []

    async def post(self, receipt: ReceiptView) -> Mapping[str, Any] | None:
        await self.gate.wait()
        self.posted.append(receipt.id)
        return {"chat_id": -1, "message_id": 77}

    async def decided(self, receipt: ReceiptView, decision: ReceiptDecision) -> None:
        return None


async def _open_transfer(env: UserEnv, cards: Any = None) -> tuple[int, int]:
    env.path.chat_payments._receipts = Receipts(env.db, env.b.pay.core, cards)
    uid, tg = await env.new_user()
    await env.open(tg)
    await env.press(tg, "Баланс")
    await env.press(tg, "179")
    details = await env.press(tg, "Перевод")
    assert "Перевод на 179 ₽" in details.text
    return uid, tg


async def test_receipt_size_limit_comes_from_the_transfer_instance(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn) as env:
        uid, _tg = await _open_transfer(env)
        provider = env.b.pay.inst("manualpay").provider
        provider.receipt_rules = lambda: {"max_mb": 2, "mime_types": ["application/pdf"]}  # type: ignore[attr-defined]
        cp = env.path.chat_payments
        big = await cp.submit_receipt(uid, "ru", kind="photo", file_id="f1", size=3 * 1024 * 1024)
        assert big is not None and "до 2 МБ" in big
        assert await env.rows("select id from manual_receipts where user_id = $1", uid) == []
        ok = await cp.submit_receipt(uid, "ru", kind="photo", file_id="f2", size=1024 * 1024)
        assert ok is not None and "Чек получен" in ok


async def test_old_transfer_does_not_turn_photos_into_receipts(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn) as env:
        uid, _tg = await _open_transfer(env)
        await env.db.raw("update payments set created_at = now() - interval '4 days' where user_id = $1", uid)
        assert await env.path.chat_payments.submit_receipt(uid, "ru", kind="photo", file_id="f") is None
        assert await env.rows("select id from manual_receipts where user_id = $1", uid) == []


async def test_slow_admin_chat_does_not_delay_the_users_answer(
    pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(chat_payments_mod, "RECEIPT_REPLY_WAIT_S", 0.2)
    async with build_user_env(pg_dsn) as env:
        cards = SlowCards()
        uid, _tg = await _open_transfer(env, cards)
        cp = env.path.chat_payments
        reply = await asyncio.wait_for(cp.submit_receipt(uid, "ru", kind="photo", file_id="f1"), 5)
        assert reply is not None and "Чек получен" in reply
        rows = await env.rows("select status, card_ref from manual_receipts where user_id = $1", uid)
        assert rows == [{"status": "submitted", "card_ref": None}]  # stored; the card is still on its way
        cards.gate.set()
        await asyncio.wait_for(cp.wait_background(), 5)
        assert len(cards.posted) == 1
        rows = await env.rows("select card_ref from manual_receipts where user_id = $1", uid)
        assert rows[0]["card_ref"] == {"chat_id": -1, "message_id": 77}
        assert env.hub.captured == []
