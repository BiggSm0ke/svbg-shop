"""Manual payments in the admin chat (04 §8, §9.1): the receipt card in «💳 Оплаты» and its buttons.

* :class:`AdminReceiptCards` — :class:`svbg.billing.receipts.ReceiptCards` over the admin chat service:
  the card is posted with ``card_ref=receipt:<id>`` (edited in place later, never duplicated), the user's
  file is sent right under it (photo, or a document as a fallback), and :meth:`AdminReceiptCards.decided`
  turns the card into the decision (buttons removed).
* :class:`ReceiptActions` + :func:`receipts_router` — the buttons and the replies to the card.

Who may press (``callback_data`` can be forged by any client, so nothing in it is trusted):

1. **Place.** Only the connected admin group or an owner's private chat with the bot; anything else is
   «Нет прав» without touching the database.
2. **Role, read-only.** :func:`svbg.services.roles.load_actor` (one SELECT, no write). Non-staff get «Нет
   прав» with no audit row and no lookup of the receipt (no «найден / уже решено» oracle for foreign ids);
   staff without ``payments.confirm`` get «Нет прав» and an ``admin_audit`` row at most once a minute per
   person. Only an actor with the right reaches :class:`~svbg.billing.receipts.Receipts`, and the payment core
   re-checks the role inside its transaction anyway (a right revoked in between still wins).

The amount is never taken from the callback data. «✅ Пришло N» only asks again: the second step «Да, пришло
ровно N» confirms the invoice amount stored in the database and the audit says the amount was not typed in.
A different amount is entered by **replying to the card** with the sum from the receipt (``179`` /
``179,50``): the core then records ``mismatch`` and credits nothing — the owner decides by hand. Rejection
buttons carry fixed reasons (a reason is mandatory, 04 §9.1).

A second press (or a second admin) is answered «Уже решено» by the CAS of ``manual_receipts``.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from html import escape
from typing import TYPE_CHECKING, Any, Final

import sqlalchemy as sa
from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.methods import EditMessageReplyMarkup, SendDocument, SendMessage, SendPhoto
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, ReplyParameters

from svbg.billing.receipts import ReceiptDecision, ReceiptError, Receipts, ReceiptView
from svbg.billing.tables import manual_receipts
from svbg.core.errors import Capturer, guard
from svbg.core.money import format_money, parse_money
from svbg.core.tables import admin_audit, users
from svbg.payments.core import PERM_CONFIRM, CheckoutError, PermissionDeniedError
from svbg.services.roles import STAFF_ROLES, Actor, load_actor
from svbg.tg.notifier import TRANSPORT_ERRORS, NotifierError, Priority

if TYPE_CHECKING:
    from svbg.db.engine import Database
    from svbg.services.admin_chat import AdminChatService
    from svbg.tg.notifier import Notifier

__all__ = [
    "CALLBACK_PREFIX",
    "REJECT_REASONS",
    "TEXTS",
    "AdminReceiptCards",
    "Press",
    "ReceiptActions",
    "receipts_router",
]

log = logging.getLogger("svbg.tg.admin.receipts")

CALLBACK_PREFIX: Final = "rcpt"
K_PAYMENTS: Final = "payments"
_DATA_RE: Final = re.compile(r"^rcpt:(ok|sure|back|no):(\d{1,18})(?::([a-z_]{1,16}))?$")
DENIED_AUDIT_INTERVAL: Final = 60.0  # one admin_audit row per staff member per minute
_DENIED_AUDIT_MAX_KEYS: Final = 1024

#: Fixed rejection reasons (code → text written to the audit and shown on the card).
REJECT_REASONS: Final[Mapping[str, str]] = {
    "nofunds": "деньги не поступили",
    "amount": "сумма в чеке не совпадает",
    "fake": "чек недействителен",
}

TEXTS: Final[Mapping[str, str]] = {
    "card": (
        "🧾 <b>Чек ручной оплаты</b>\n"
        "👤 {who}\n"
        "Счёт: <b>{amount}</b>\n"
        "Платёж: <code>{pid}</code>{comment}\n\n"
        "Проверьте поступление. Если сумма по чеку другая — ответьте на это сообщение суммой из чека."
    ),
    "comment": "\nКомментарий: {text}",
    "confirmed": "\n\n✅ <b>Подтверждено</b> ({by}), сумма по чеку: {amount}",
    "mismatch": "\n\n⚠️ <b>Сумма не совпала</b> ({by}): по чеку {amount} — не зачислено, решите вручную",
    "rejected": "\n\n❌ <b>Отклонено</b> ({by}): {reason}",
    "btn_confirm": "✅ Пришло {amount}",
    "btn_sure": "✅ Да, по чеку ровно {amount}",
    "btn_back": "↩️ Назад",
    "btn_reject": "❌ {reason}",
    "ask_sure": (
        "Сверьте сумму в чеке: пришло ровно {amount}? Если сумма другая — нажмите «Назад» и ответьте "
        "на карточку суммой из чека."
    ),
    "reason_button": "сумма по чеку совпала со счётом (подтверждено кнопкой, сумма не вводилась)",
    "reason_reply": "сумма по чеку введена ответом на карточку",
    "no_rights": "Нет прав",
    "done_confirmed": "Подтверждено, деньги зачислены",
    "done_mismatch": "Сумма не совпала — не зачислено",
    "done_other": "Чек закрыт: платёж уже был обработан раньше, повторно не зачислено",
    "done_rejected": "Отклонено",
    "decided": "Уже решено другим администратором.",
    "bad_amount": "Не понял сумму. Ответьте на карточку числом, например 179 или 179,50.",
    "failed": "Не получилось, попробуйте ещё раз.",
}


def _keyboard(receipt: ReceiptView) -> list[list[InlineKeyboardButton]]:
    amount = format_money(receipt.amount_minor, receipt.currency)
    rows = [
        [
            InlineKeyboardButton(
                text=TEXTS["btn_confirm"].format(amount=amount),
                callback_data=f"{CALLBACK_PREFIX}:ok:{receipt.id}",
            )
        ]
    ]
    rows.extend(
        [
            InlineKeyboardButton(
                text=TEXTS["btn_reject"].format(reason=text[:1].upper() + text[1:]),
                callback_data=f"{CALLBACK_PREFIX}:no:{receipt.id}:{code}",
            )
        ]
        for code, text in REJECT_REASONS.items()
    )
    return rows


def _sure_keyboard(receipt: ReceiptView) -> list[list[InlineKeyboardButton]]:
    amount = format_money(receipt.amount_minor, receipt.currency)
    return [
        [
            InlineKeyboardButton(
                text=TEXTS["btn_sure"].format(amount=amount),
                callback_data=f"{CALLBACK_PREFIX}:sure:{receipt.id}",
            )
        ],
        [InlineKeyboardButton(text=TEXTS["btn_back"], callback_data=f"{CALLBACK_PREFIX}:back:{receipt.id}")],
    ]


class AdminReceiptCards:
    """:class:`~svbg.billing.receipts.ReceiptCards` over the admin chat (see module docstring)."""

    def __init__(self, db: Database, admin_chat: AdminChatService, notifier: Notifier) -> None:
        self._db = db
        self._chat = admin_chat
        self._notifier = notifier

    async def _who(self, user_id: int) -> str:
        async with self._db.read() as conn:
            row = (
                await conn.execute(
                    sa.select(users.c.first_name, users.c.username, users.c.telegram_id).where(
                        users.c.id == user_id
                    )
                )
            ).first()
        if row is None:
            return f"#{user_id}"
        name = escape((row.first_name or "").strip()[:64]) or "без имени"
        handle = f"@{escape(row.username[:64])}, " if row.username else ""
        return f"{name} ({handle}id <code>{row.telegram_id}</code>)"

    async def _text(self, receipt: ReceiptView) -> str:
        comment = TEXTS["comment"].format(text=escape(receipt.comment[:500])) if receipt.comment else ""
        return TEXTS["card"].format(
            who=await self._who(receipt.user_id),
            amount=escape(format_money(receipt.amount_minor, receipt.currency)),
            pid=escape(receipt.payment_id),
            comment=comment,
        )

    async def post(self, receipt: ReceiptView) -> Mapping[str, Any] | None:
        result = await self._chat.post(
            K_PAYMENTS,
            await self._text(receipt),
            html=True,
            buttons=_keyboard(receipt),
            card_ref=f"receipt:{receipt.id}",
            priority=Priority.HIGH,
            wait=True,
        )
        if result is None or not result.delivered:
            return None
        if result.chat_id is not None and result.message_id is not None:
            await self._send_file(receipt, result.chat_id, result.thread_id, result.message_id)
            return {"chat_id": result.chat_id, "message_id": result.message_id, "thread_id": result.thread_id}
        for chat_id, message_id in result.dm.items():  # no admin chat: the owners' private chats
            await self._send_file(receipt, chat_id, None, message_id)
        return {"dm": {str(k): v for k, v in result.dm.items()}}

    async def _send_file(
        self, receipt: ReceiptView, chat_id: int, thread_id: int | None, reply_to: int
    ) -> None:
        if not receipt.file_id:
            return
        reply = ReplyParameters(message_id=reply_to, allow_sending_without_reply=True)
        for method in (
            SendPhoto(
                chat_id=chat_id, photo=receipt.file_id, message_thread_id=thread_id, reply_parameters=reply
            ),
            SendDocument(
                chat_id=chat_id, document=receipt.file_id, message_thread_id=thread_id, reply_parameters=reply
            ),
        ):
            try:
                await self._notifier.call(method, chat_id=chat_id, priority=Priority.HIGH)
                return
            except TelegramBadRequest:
                continue  # a document's file_id cannot be sent as a photo: try as a document
            except (TelegramAPIError, NotifierError, *TRANSPORT_ERRORS) as exc:
                log.warning("receipt file was not sent to the admin chat: %s", type(exc).__name__)
                return

    async def decided(self, receipt: ReceiptView, decision: ReceiptDecision) -> None:
        async with self._db.read() as conn:
            row = (
                await conn.execute(
                    sa.select(
                        manual_receipts.c.decided_amount_minor,
                        manual_receipts.c.decision_reason,
                        users.c.first_name,
                        users.c.username,
                    )
                    .select_from(manual_receipts.outerjoin(users, users.c.id == manual_receipts.c.decided_by))
                    .where(manual_receipts.c.id == receipt.id)
                )
            ).first()
        by = "—"
        if row is not None and (row.username or row.first_name):
            by = escape(f"@{row.username}" if row.username else str(row.first_name))[:64]
        amount_minor = row.decided_amount_minor if row is not None else None
        amount = (
            escape(format_money(int(amount_minor), receipt.currency)) if amount_minor is not None else "—"
        )
        if decision.outcome == "rejected":
            tail = TEXTS["rejected"].format(by=by, reason=escape((row.decision_reason if row else "") or "—"))
        elif decision.payment_outcome == "mismatch":
            tail = TEXTS["mismatch"].format(by=by, amount=amount)
        else:
            tail = TEXTS["confirmed"].format(by=by, amount=amount)
        await self._chat.post(
            K_PAYMENTS,
            await self._text(receipt) + tail,
            html=True,
            card_ref=f"receipt:{receipt.id}",
            priority=Priority.HIGH,
        )


# ------------------------------------------------------------------------------------------ buttons


OwnerIds = Callable[[], Awaitable[frozenset[int]]]


@dataclass(frozen=True, slots=True)
class Press:
    """A toast (``alert`` = modal) and the card's new keyboard (``None`` = unchanged)."""

    toast: str
    alert: bool = False
    keyboard: list[list[InlineKeyboardButton]] | None = None


class _Throttle:
    """``allow(key)`` is true at most once per ``interval`` seconds per key; memory stays bounded."""

    def __init__(self, interval: float, max_keys: int, clock: Callable[[], float] = time.monotonic) -> None:
        self._interval = interval
        self._max_keys = max_keys
        self._clock = clock
        self._seen: dict[Any, float] = {}

    def allow(self, key: Any) -> bool:
        now = self._clock()
        last = self._seen.get(key)
        if last is not None and now - last < self._interval:
            return False
        if last is None and len(self._seen) >= self._max_keys:
            self._seen = {k: t for k, t in self._seen.items() if now - t < self._interval}
            while len(self._seen) >= self._max_keys:  # still full: forget the oldest
                del self._seen[next(iter(self._seen))]
        self._seen.pop(key, None)
        self._seen[key] = now
        return True


def _decision_text(decision: ReceiptDecision) -> tuple[str, bool]:
    if decision.outcome == "decided":
        return TEXTS["decided"], True
    if decision.outcome == "rejected":
        return TEXTS["done_rejected"], False
    if decision.payment_outcome == "applied":
        return TEXTS["done_confirmed"], False
    if decision.payment_outcome == "mismatch":
        return TEXTS["done_mismatch"], True
    return TEXTS["done_other"], True


class ReceiptActions:
    """The card's buttons and replies without aiogram (see the module docstring for the order of checks).

    ``admin_chat_id`` returns the connected admin group (``None`` = cards go to the owners' DMs).
    """

    def __init__(
        self,
        receipts: Receipts,
        *,
        db: Database,
        owner_ids: OwnerIds,
        admin_chat_id: Callable[[], int | None] | None = None,
        audit_interval: float = DENIED_AUDIT_INTERVAL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._receipts = receipts
        self._db = db
        self._owner_ids = owner_ids
        self._admin_chat_id = admin_chat_id or (lambda: None)
        self._denied = _Throttle(audit_interval, _DENIED_AUDIT_MAX_KEYS, clock)

    async def _place(
        self, telegram_id: int, chat_id: int | None, chat_type: str | None
    ) -> frozenset[int] | None:
        """Owner ids when the press / reply comes from an allowed chat, otherwise ``None`` (no SQL)."""
        if chat_id is None:
            return None
        admin_chat = self._admin_chat_id()
        if admin_chat is not None and chat_id == admin_chat:
            return await self._owner_ids()
        if chat_type == "private" and chat_id == telegram_id:
            owners = await self._owner_ids()
            return owners if telegram_id in owners else None
        return None

    async def _actor(self, telegram_id: int, owners: frozenset[int]) -> Actor | None:
        async with self._db.read() as conn:
            return await load_actor(conn, telegram_id=telegram_id, owner_ids=owners)

    @staticmethod
    def _is_staff(actor: Actor | None) -> bool:
        return actor is not None and not actor.banned and actor.role in STAFF_ROLES

    async def _audit_denied(self, actor: Actor, telegram_id: int, target: str) -> None:
        """A staff member without ``payments.confirm``: one audit row per person per minute."""
        if not self._denied.allow(actor.user_id or telegram_id):
            return
        stmt = sa.insert(admin_audit).values(
            actor_id=actor.user_id,
            role=actor.role,
            action="payments.confirm.denied",
            target=target,
            details={"telegram_id": telegram_id, "via": "tg"},
        )
        try:
            async with self._db.tx() as conn:
                await conn.execute(stmt)
        except (sa.exc.SQLAlchemyError, OSError) as exc:
            log.warning("cannot audit a denied receipt action: %s", type(exc).__name__)

    async def press(
        self, data: str, telegram_id: int, *, chat_id: int | None, chat_type: str | None
    ) -> Press:
        match = _DATA_RE.fullmatch(data)
        if match is None:
            return Press(TEXTS["failed"])
        act, receipt_id, code = match.group(1), int(match.group(2)), match.group(3)
        owners = await self._place(telegram_id, chat_id, chat_type)
        if owners is None:
            return Press(TEXTS["no_rights"], alert=True)
        actor = await self._actor(telegram_id, owners)
        if actor is None or not self._is_staff(actor):
            log.info("receipt button pressed by a non-staff user")
            return Press(TEXTS["no_rights"], alert=True)
        if not actor.has_perm(PERM_CONFIRM):
            await self._audit_denied(actor, telegram_id, f"receipt:{receipt_id}")
            return Press(TEXTS["no_rights"], alert=True)
        try:
            return await self._act(act, receipt_id, code, telegram_id, owners)
        except PermissionDeniedError as exc:  # the right was revoked between the two checks
            return Press(exc.human, alert=True)
        except ReceiptError as exc:
            return Press(exc.text, alert=True)
        except CheckoutError as exc:
            return Press(exc.human, alert=True)

    async def _act(
        self, act: str, receipt_id: int, code: str | None, telegram_id: int, owners: frozenset[int]
    ) -> Press:
        if act == "no":
            reason = REJECT_REASONS.get(code or "")
            if reason is None:
                return Press(TEXTS["failed"])
            decision = await self._receipts.reject(
                receipt_id, actor_telegram_id=telegram_id, owner_ids=owners, reason=reason
            )
        else:
            receipt = await self._receipts.get(receipt_id)
            if receipt is None:
                raise ReceiptError("not_found")
            if receipt.status != "submitted":
                return Press(TEXTS["decided"], alert=True, keyboard=[])
            if act == "back":
                return Press("", keyboard=_keyboard(receipt))
            if act == "ok":
                amount = format_money(receipt.amount_minor, receipt.currency)
                return Press(
                    TEXTS["ask_sure"].format(amount=amount), alert=True, keyboard=_sure_keyboard(receipt)
                )
            decision = await self._receipts.confirm(  # "sure": the second, explicit step
                receipt_id,
                actor_telegram_id=telegram_id,
                owner_ids=owners,
                paid_amount_minor=receipt.amount_minor,  # the invoice amount, re-read from the database
                reason=TEXTS["reason_button"],
            )
        toast, alert = _decision_text(decision)
        return Press(toast, alert=alert, keyboard=[] if decision.outcome == "decided" else None)

    async def _receipt_by_card(self, chat_id: int, message_id: int) -> int | None:
        ref = manual_receipts.c.card_ref
        cond = sa.or_(
            sa.and_(ref["chat_id"].astext == str(chat_id), ref["message_id"].astext == str(message_id)),
            ref["dm"][str(chat_id)].astext == str(message_id),
        )
        stmt = sa.select(manual_receipts.c.id).where(cond).order_by(manual_receipts.c.id.desc()).limit(1)
        async with self._db.read() as conn:
            found = (await conn.execute(stmt)).scalar()
        return None if found is None else int(found)

    async def reply(
        self, text: str, telegram_id: int, *, chat_id: int, chat_type: str | None, reply_to_message_id: int
    ) -> str | None:
        """A reply to a card with the amount from the receipt; ``None`` = not a reply to a receipt card by
        staff (the message goes on to the other handlers)."""
        owners = await self._place(telegram_id, chat_id, chat_type)
        if owners is None:
            return None
        actor = await self._actor(telegram_id, owners)
        if actor is None or not self._is_staff(actor):
            return None
        receipt_id = await self._receipt_by_card(chat_id, reply_to_message_id)
        if receipt_id is None:
            return None
        if not actor.has_perm(PERM_CONFIRM):
            await self._audit_denied(actor, telegram_id, f"receipt:{receipt_id}")
            return TEXTS["no_rights"]
        receipt = await self._receipts.get(receipt_id)
        if receipt is None:
            return ReceiptError("not_found").text
        if receipt.status != "submitted":
            return TEXTS["decided"]
        try:
            amount = parse_money(text, receipt.currency)
        except ValueError:
            return TEXTS["bad_amount"]
        try:
            decision = await self._receipts.confirm(
                receipt_id,
                actor_telegram_id=telegram_id,
                owner_ids=owners,
                paid_amount_minor=amount,
                reason=TEXTS["reason_reply"],
            )
        except PermissionDeniedError as exc:
            return exc.human
        except ReceiptError as exc:
            return exc.text
        except CheckoutError as exc:
            return exc.human
        return _decision_text(decision)[0]


def receipts_router(
    receipts: Receipts,
    *,
    owner_ids: OwnerIds,
    db: Database,
    hub: Capturer | None = None,
    admin_chat_id: Callable[[], int | None] | None = None,
    name: str = "svbg-receipts",
) -> Router:
    """Callback buttons ``rcpt:*`` of the receipt cards and replies to the cards with the amount."""
    actions = ReceiptActions(receipts, db=db, owner_ids=owner_ids, admin_chat_id=admin_chat_id)
    return actions_router(actions, hub=hub, name=name)


def actions_router(
    actions: ReceiptActions, *, hub: Capturer | None = None, name: str = "svbg-receipts"
) -> Router:
    """The aiogram adapter over :class:`ReceiptActions`."""
    router = Router(name=name)

    @router.callback_query(F.data.startswith(f"{CALLBACK_PREFIX}:"))
    async def on_press(query: CallbackQuery) -> None:
        message = query.message
        chat = getattr(message, "chat", None)
        result = Press(TEXTS["failed"])
        async with guard("tg:receipts:press", hub=hub, module="payments"):
            result = await actions.press(
                query.data or "",
                query.from_user.id,
                chat_id=chat.id if chat is not None else None,
                chat_type=chat.type if chat is not None else None,
            )
        bot = query.bot
        if result.keyboard is not None and bot is not None and chat is not None and message is not None:
            try:
                await bot(
                    EditMessageReplyMarkup(
                        chat_id=chat.id,
                        message_id=message.message_id,
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=result.keyboard),
                    )
                )
            except TelegramAPIError as exc:
                log.warning("receipt card keyboard was not updated: %s", type(exc).__name__)
        try:
            await query.answer(result.toast[:190] or None, show_alert=result.alert)
        except TelegramAPIError as exc:
            log.warning("answerCallbackQuery failed: %s", type(exc).__name__)

    @router.message(F.reply_to_message, F.text)
    async def on_reply(message: Message) -> None:
        replied = message.reply_to_message
        bot = message.bot
        sender = message.from_user
        if (
            bot is None
            or sender is None
            or replied is None
            or replied.from_user is None
            or replied.from_user.id != bot.id
        ):
            raise SkipHandler
        answer: str | None = TEXTS["failed"]
        async with guard("tg:receipts:reply", hub=hub, module="payments"):
            answer = await actions.reply(
                message.text or "",
                sender.id,
                chat_id=message.chat.id,
                chat_type=message.chat.type,
                reply_to_message_id=replied.message_id,
            )
        if answer is None:
            raise SkipHandler
        try:
            await bot(
                SendMessage(
                    chat_id=message.chat.id,
                    text=answer,
                    message_thread_id=message.message_thread_id,
                    reply_parameters=ReplyParameters(
                        message_id=message.message_id, allow_sending_without_reply=True
                    ),
                )
            )
        except TelegramAPIError as exc:
            log.warning("receipt reply was not answered: %s", type(exc).__name__)

    return router
