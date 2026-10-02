"""The receipt card's buttons and replies (svbg.tg.admin.receipts): place → read-only role → action.

* a forged ``rcpt:*`` press by a non-staff user writes nothing (no ``admin_audit`` growth) and reveals nothing
  about the receipt (the same «Нет прав» for an existing and an unknown id);
* staff without ``payments.confirm`` are audited at most once a minute per person;
* «✅ Пришло N» only asks again; the second step confirms the invoice amount and the audit says the amount was
  not typed in; a reply to the card with a different amount is a ``mismatch``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from aiogram.dispatcher.event.bases import SkipHandler

from svbg.billing.receipts import ReceiptDecision, Receipts, ReceiptView
from svbg.tg.admin.receipts import TEXTS, ReceiptActions, actions_router
from tests.billing.kit import BillingEnv

ADMIN_CHAT = -100
OWNER_TG = 1


@dataclass
class FakeCards:
    posted: list[ReceiptView] = field(default_factory=list)
    decisions: list[tuple[int, str]] = field(default_factory=list)
    dm: bool = False

    async def post(self, receipt: ReceiptView) -> Mapping[str, Any] | None:
        self.posted.append(receipt)
        if self.dm:
            return {"dm": {str(OWNER_TG): 40 + len(self.posted)}}
        return {"chat_id": ADMIN_CHAT, "message_id": 10 + len(self.posted), "thread_id": 3}

    async def decided(self, receipt: ReceiptView, decision: ReceiptDecision) -> None:
        self.decisions.append((receipt.id, decision.outcome))


@dataclass
class Clock:
    t: float = 1000.0

    def __call__(self) -> float:
        return self.t


@dataclass
class Kit:
    env: BillingEnv
    receipts: Receipts
    actions: ReceiptActions
    cards: FakeCards
    clock: Clock
    uid: int
    order_id: int
    receipt: ReceiptView

    async def press(self, data: str, telegram_id: int, chat_id: int | None = ADMIN_CHAT) -> Any:
        chat_type = "private" if chat_id is not None and chat_id > 0 else "supergroup"
        return await self.actions.press(data, telegram_id, chat_id=chat_id, chat_type=chat_type)

    async def audit(self) -> list[dict[str, Any]]:
        return await self.env.rows("select action, target, reason, amount_minor from admin_audit order by id")

    async def status(self) -> str:
        return str((await self.receipts.get(self.receipt.id)).status)  # type: ignore[union-attr]


async def _kit(env: BillingEnv, *, dm: bool = False) -> Kit:
    cards = FakeCards(dm=dm)
    receipts = Receipts(env.db, env.pay.core, cards)
    uid = await env.user()
    draft = await env.draft(uid)
    ref = env.messenger.show(await env.telegram_id(uid))
    await env.checkout.pay(draft.order_id, uid, ui_ref=ref)
    top = await env.topup(uid, 17_900, parent=draft.order_id, ui_ref=ref, instance_id=env.manual_id())
    view = await receipts.submit(top.payment_id, uid, file_id="photo-1")
    clock = Clock()

    async def owners() -> frozenset[int]:
        return frozenset({OWNER_TG})

    actions = ReceiptActions(
        receipts,
        db=env.db,
        owner_ids=owners,
        admin_chat_id=lambda: None if dm else ADMIN_CHAT,
        clock=clock,
    )
    return Kit(env, receipts, actions, cards, clock, uid, draft.order_id, view)


async def test_forged_press_by_a_user_writes_nothing_and_reveals_nothing(env: BillingEnv) -> None:
    k = await _kit(env)
    user_tg = await env.telegram_id(k.uid)
    for data in (f"rcpt:sure:{k.receipt.id}", f"rcpt:ok:{k.receipt.id}", f"rcpt:no:{k.receipt.id}:fake"):
        for _ in range(5):
            res = await k.press(data, user_tg)
            assert (res.toast, res.alert, res.keyboard) == (TEXTS["no_rights"], True, None)
    unknown = await k.press("rcpt:ok:999999999", user_tg)
    assert unknown.toast == TEXTS["no_rights"]  # no «не найден / уже решено» oracle
    assert await k.audit() == []
    assert await k.status() == "submitted" and await env.balance(k.uid) == 0
    # one read-only SELECT (the role), no receipt lookup and no write
    mark = env.db.queries
    await k.press(f"rcpt:sure:{k.receipt.id}", user_tg)
    assert env.db.queries - mark == 1


async def test_press_from_a_foreign_chat_touches_no_database(env: BillingEnv) -> None:
    k = await _kit(env)
    admin_tg = await env.telegram_id(await env.user(role="admin", perms=["payments.confirm"]))
    mark = env.db.queries
    for chat in (-555, admin_tg, None):  # another group, a non-owner's DM, no chat at all
        res = await k.press(f"rcpt:sure:{k.receipt.id}", admin_tg, chat_id=chat)
        assert res.toast == TEXTS["no_rights"]
    assert env.db.queries == mark
    assert await k.status() == "submitted" and await k.audit() == []


async def test_staff_without_the_right_is_audited_once_a_minute(env: BillingEnv) -> None:
    k = await _kit(env)
    support_tg = await env.telegram_id(await env.user(role="support"))
    admin_tg = await env.telegram_id(await env.user(role="admin", perms=["users.ban"]))
    for _ in range(4):
        assert (await k.press(f"rcpt:sure:{k.receipt.id}", support_tg)).toast == TEXTS["no_rights"]
        assert (await k.press(f"rcpt:no:{k.receipt.id}:fake", admin_tg)).toast == TEXTS["no_rights"]
    assert [a["action"] for a in await k.audit()] == ["payments.confirm.denied"] * 2
    k.clock.t += 61
    await k.press(f"rcpt:sure:{k.receipt.id}", support_tg)
    rows = await k.audit()
    assert len(rows) == 3 and rows[-1]["target"] == f"receipt:{k.receipt.id}"
    assert await k.status() == "submitted"


async def test_confirm_button_asks_again_and_audits_that_no_amount_was_typed(env: BillingEnv) -> None:
    k = await _kit(env)
    admin_tg = await env.telegram_id(await env.user(role="admin", perms=["payments.confirm"]))
    ask = await k.press(f"rcpt:ok:{k.receipt.id}", admin_tg)
    assert ask.alert and "ровно 179 ₽" in ask.toast
    assert ask.keyboard is not None
    assert [row[0].callback_data for row in ask.keyboard] == [
        f"rcpt:sure:{k.receipt.id}",
        f"rcpt:back:{k.receipt.id}",
    ]
    assert ask.keyboard[0][0].text == "✅ Да, по чеку ровно 179 ₽"
    assert await k.status() == "submitted" and await k.audit() == []  # the first tap moves nothing
    back = await k.press(f"rcpt:back:{k.receipt.id}", admin_tg)
    assert back.keyboard is not None and back.keyboard[0][0].text == "✅ Пришло 179 ₽"
    done = await k.press(f"rcpt:sure:{k.receipt.id}", admin_tg)
    assert done.toast == TEXTS["done_confirmed"]
    assert (await env.order(k.order_id))["status"] == "paid"
    audit = await k.audit()
    assert [a["action"] for a in audit] == ["payments.confirm"]
    assert audit[0]["amount_minor"] == 17_900 and "сумма не вводилась" in audit[0]["reason"]
    again = await k.press(f"rcpt:sure:{k.receipt.id}", admin_tg)
    assert again.toast == TEXTS["decided"] and again.keyboard == []
    assert [r["reason"] for r in await env.ledger(k.uid)] == ["topup", "purchase"]


async def test_reply_with_a_different_amount_is_a_mismatch(env: BillingEnv) -> None:
    k = await _kit(env)
    admin_tg = await env.telegram_id(await env.user(role="admin", perms=["payments.confirm"]))
    user_tg = await env.telegram_id(k.uid)
    card = int(k.receipt.card_ref["message_id"])  # type: ignore[index]

    async def reply(text: str, tg: int, to: int = card) -> str | None:
        return await k.actions.reply(
            text, tg, chat_id=ADMIN_CHAT, chat_type="supergroup", reply_to_message_id=to
        )

    assert await reply("100", user_tg) is None  # not staff: the message goes on, nothing happens
    assert await reply("100", admin_tg, to=card + 500) is None  # not a receipt card
    assert await reply("спасибо", admin_tg) == TEXTS["bad_amount"]
    assert await k.status() == "submitted"
    assert await reply("100", admin_tg) == TEXTS["done_mismatch"]
    assert await env.balance(k.uid) == 0
    assert (await env.order(k.order_id))["status"] == "awaiting_funds"
    audit = await k.audit()
    assert audit[-1]["amount_minor"] == 10_000 and "введена" in audit[-1]["reason"]
    assert await reply("179", admin_tg) == TEXTS["decided"]


async def test_owner_in_dm_confirms_by_reply_and_rejects_by_button(env: BillingEnv) -> None:
    k = await _kit(env, dm=True)
    card = int(k.receipt.card_ref["dm"][str(OWNER_TG)])  # type: ignore[index]
    text = await k.actions.reply(
        "179", OWNER_TG, chat_id=OWNER_TG, chat_type="private", reply_to_message_id=card
    )
    assert text == TEXTS["done_confirmed"]
    assert (await env.order(k.order_id))["status"] == "paid"

    other = await _kit(env, dm=True)
    res = await other.press(f"rcpt:no:{other.receipt.id}:nofunds", OWNER_TG, chat_id=OWNER_TG)
    assert res.toast == TEXTS["done_rejected"]
    row = (await env.rows("select decision_reason from manual_receipts where id = $1", other.receipt.id))[0]
    assert row["decision_reason"] == "деньги не поступили"
    bad = await other.press(f"rcpt:no:{other.receipt.id}:zzz", OWNER_TG, chat_id=OWNER_TG)
    assert bad.toast == TEXTS["failed"]
    assert (await other.press("rcpt:boom", OWNER_TG, chat_id=OWNER_TG)).toast == TEXTS["failed"]


# ------------------------------------------------------------------------------------- aiogram adapter


class _Bot:
    id = 42

    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def __call__(self, method: Any) -> Any:
        self.calls.append(method)
        return True


class _Query(SimpleNamespace):
    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        self.answered = (text, show_alert)


class _Actions:
    def __init__(self, press: Any = None, reply: Any = None, boom: bool = False) -> None:
        self._press, self._reply, self._boom = press, reply, boom

    async def press(self, *_a: Any, **_k: Any) -> Any:
        if self._boom:
            raise RuntimeError("db down")
        return self._press

    async def reply(self, *_a: Any, **_k: Any) -> Any:
        if self._boom:
            raise RuntimeError("db down")
        return self._reply


async def test_adapter_answers_and_skips_foreign_replies() -> None:
    from svbg.tg.admin.receipts import Press

    bot = _Bot()
    chat = SimpleNamespace(id=ADMIN_CHAT, type="supergroup")
    message = SimpleNamespace(message_id=5, chat=chat)
    router = actions_router(_Actions(press=Press("ок", keyboard=[])))  # type: ignore[arg-type]
    q = _Query(data="rcpt:ok:1", from_user=SimpleNamespace(id=7), message=message, bot=bot)
    await router.callback_query.handlers[0].callback(q)
    assert type(bot.calls[0]).__name__ == "EditMessageReplyMarkup" and q.answered == ("ок", False)

    failing = actions_router(_Actions(boom=True))  # type: ignore[arg-type]
    q2 = _Query(data="rcpt:ok:1", from_user=SimpleNamespace(id=7), message=message, bot=bot)
    await failing.callback_query.handlers[0].callback(q2)
    assert q2.answered == (TEXTS["failed"], False)

    def msg(reply_from: int) -> Any:
        return SimpleNamespace(
            bot=bot,
            from_user=SimpleNamespace(id=7),
            chat=chat,
            text="179",
            message_id=9,
            message_thread_id=3,
            reply_to_message=SimpleNamespace(message_id=5, from_user=SimpleNamespace(id=reply_from)),
        )

    on_reply = actions_router(_Actions(reply=None)).message.handlers[0].callback  # type: ignore[arg-type]
    for from_id in (99, 42):  # a reply to someone else's message / not a card (actions say None)
        try:
            await on_reply(msg(from_id))
        except SkipHandler:
            pass
        else:
            raise AssertionError("expected SkipHandler")
    answering = actions_router(_Actions(reply="готово")).message.handlers[0].callback  # type: ignore[arg-type]
    bot.calls.clear()
    await answering(msg(42))
    assert type(bot.calls[0]).__name__ == "SendMessage" and bot.calls[0].text == "готово"
