"""Payments that arrive through the chat itself: Telegram Stars and manual-transfer receipts.

* ``pre_checkout_query`` — the last moment to refuse a star invoice before money moves: the payload must be
  one of our payment ids, the payment still payable, the currency and the exact star count unchanged (the
  Stars plugin's ``check_pre_checkout``), and the user allowed to spend (freeze, X5). Legacy and unknown
  payloads are refused — nothing is charged that the bot could not credit.
* ``successful_payment`` → :meth:`~svbg.payments.core.PaymentCore.credit_external` (CAS «exactly once» by
  ``telegram_payment_charge_id``); billing then completes the waiting purchase and edits the payment message.
* a photo / PDF from a user with an open manual transfer (``pending``, created within
  :data:`TRANSFER_TTL` — an old forgotten «Перевод» does not turn every later photo into a receipt) →
  :meth:`~svbg.billing.receipts.Receipts.submit` (the receipt card goes to «💳 Оплаты»; an admin with
  ``payments.confirm`` decides). The file limits are the transfer instance's own (``receipt_rules()``:
  ``max_mb``). The user is answered within :data:`RECEIPT_REPLY_WAIT_S` even when the admin chat is slow:
  the card is then posted in the background. Other photos are left to other handlers.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Final

import sqlalchemy as sa
from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message, PreCheckoutQuery

from svbg.core.clock import now
from svbg.core.errors import Capturer, guard
from svbg.payments.providers.manual import receipt_problem
from svbg.payments.providers.stars import TEXTS as STARS_TEXTS
from svbg.payments.providers.stars import PayloadKind, classify_payload, localize_pre_checkout
from svbg.payments.tables import LATE_PAYABLE, payments
from svbg.subscriptions.hold import localize_spend
from svbg.tg.user.texts import t

if TYPE_CHECKING:
    from svbg.billing.receipts import Receipts
    from svbg.db.engine import Database
    from svbg.payments.core import ApplyResult, PaymentCore
    from svbg.tg.user.directory import UserDirectory

__all__ = ["RECEIPT_REPLY_WAIT_S", "TRANSFER_TTL", "ChatPayments"]

log = logging.getLogger("svbg.tg.user.chat_payments")

#: Pauses between attempts to credit a ``successful_payment`` when the database is briefly unavailable.
CREDIT_RETRY_DELAYS: Final = (0.5, 2.0, 5.0, 0.0)
#: A manual transfer accepts receipts this long after the details were shown (later: support).
TRANSFER_TTL: Final = timedelta(days=3)
#: The user hears «Чек получен» after at most this long; a slow admin chat gets the card in the background.
RECEIPT_REPLY_WAIT_S: Final = 1.5
DEFAULT_RECEIPT_MB: Final = 10


class ChatPayments:
    def __init__(
        self,
        db: Database,
        *,
        payments_core: PaymentCore | None,
        users: UserDirectory,
        receipts: Receipts | None = None,
        hub: Capturer | None = None,
    ) -> None:
        self._db = db
        self._core = payments_core
        self._users = users
        self._receipts = receipts
        self._hub = hub
        self._background: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------------------------------ queries

    async def _payment(self, payment_id: str) -> Mapping[str, Any] | None:
        async with self._db.read() as conn:
            return (
                (
                    await conn.execute(
                        sa.select(
                            payments.c.id,
                            payments.c.instance_id,
                            payments.c.user_id,
                            payments.c.status,
                            payments.c.amount_minor,
                            payments.c.currency,
                            payments.c.checkout,
                        ).where(payments.c.id == payment_id)
                    )
                )
                .mappings()
                .first()
            )

    # ------------------------------------------------------------------------------------------ Stars

    async def decide_pre_checkout(self, payload: str, currency: str, total_amount: int) -> str | None:
        """``None`` → answer ``ok=True``; otherwise the Russian reason for ``ok=False``."""
        if self._core is None or classify_payload(payload) is not PayloadKind.OURS:
            return STARS_TEXTS["outdated"]
        row = await self._payment(payload)
        if row is None:
            return STARS_TEXTS["not_found"]
        inst = self._core.instances.get(int(row["instance_id"]))
        if inst is None:
            return STARS_TEXTS["closed"]
        checkout = row["checkout"] if isinstance(row["checkout"], Mapping) else {}
        invoice = checkout.get("invoice") if isinstance(checkout.get("invoice"), Mapping) else None
        check = getattr(inst.provider, "check_pre_checkout", None)
        if callable(check):
            reason = check(
                payload=payload,
                currency=currency,
                total_amount=total_amount,
                payment_status=str(row["status"]),
                invoice=invoice,
            )
        elif row["status"] == "paid":
            reason = STARS_TEXTS["paid"]
        elif row["status"] not in LATE_PAYABLE:
            reason = STARS_TEXTS["closed"]
        elif currency != row["currency"] or total_amount != int(row["amount_minor"]):
            reason = STARS_TEXTS["changed"]
        else:
            reason = None
        if reason is None:
            async with self._db.read() as conn:
                reason = await self._core.can_spend(conn, int(row["user_id"]))
        return reason

    async def credit_stars(
        self, payload: str, *, charge_id: str, currency: str, total_amount: int
    ) -> ApplyResult | None:
        """Credit a ``successful_payment`` (idempotent by ``charge_id``). ``None``: not our invoice."""
        if self._core is None or classify_payload(payload) is not PayloadKind.OURS:
            log.warning("successful_payment with a foreign payload was not credited")
            return None
        row = await self._payment(payload)
        if row is None:
            log.warning("successful_payment for an unknown payment")
            return None
        return await self._core.credit_external(
            int(row["instance_id"]),
            user_id=int(row["user_id"]),
            external_id=charge_id,
            amount_minor=int(total_amount),
            currency=currency,
            payment_id=payload,
        )

    # ------------------------------------------------------------------------------------------ receipts

    async def pending_transfer(self, user_id: int) -> tuple[str, int] | None:
        """``(payment_id, instance_id)`` of the user's latest open manual transfer: a ``details`` checkout
        still ``pending``, not older than :data:`TRANSFER_TTL`."""
        async with self._db.read() as conn:
            row = (
                await conn.execute(
                    sa.select(payments.c.id, payments.c.instance_id)
                    .where(
                        payments.c.user_id == user_id,
                        payments.c.status == "pending",
                        payments.c.checkout["kind"].astext == "details",
                        payments.c.created_at > now() - TRANSFER_TTL,
                    )
                    .order_by(payments.c.created_at.desc())
                    .limit(1)
                )
            ).first()
        return None if row is None else (str(row.id), int(row.instance_id))

    def _max_mb(self, instance_id: int) -> int:
        """The transfer instance's receipt size limit (``ManualTransfer.receipt_rules()['max_mb']``)."""
        inst = None if self._core is None else self._core.instances.get(instance_id)
        rules = getattr(getattr(inst, "provider", None), "receipt_rules", None)
        if not callable(rules):
            return DEFAULT_RECEIPT_MB
        try:
            value = rules().get("max_mb")
        except (AttributeError, TypeError, ValueError):
            return DEFAULT_RECEIPT_MB
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            return DEFAULT_RECEIPT_MB
        return value

    async def submit_receipt(
        self,
        user_id: int,
        lang: str,
        *,
        kind: str,
        file_id: str,
        mime_type: str | None = None,
        size: int | None = None,
        caption: str | None = None,
    ) -> str | None:
        """The reply for a receipt, or ``None`` when the user has no open transfer (not a receipt)."""
        from svbg.billing.receipts import ReceiptError

        if self._receipts is None:
            return None
        transfer = await self.pending_transfer(user_id)
        if transfer is None:
            return None
        payment_id, instance_id = transfer
        problem = receipt_problem(
            kind, mime_type=mime_type, size=size, max_mb=self._max_mb(instance_id), lang=lang
        )
        if problem is not None:
            return problem
        task = asyncio.create_task(
            self._receipts.submit(payment_id, user_id, file_id=file_id, comment=caption)
        )
        done, _ = await asyncio.wait({task}, timeout=RECEIPT_REPLY_WAIT_S)
        if task not in done:
            # The receipt is stored (that part is one quick transaction); the admin card waits for the
            # group's rate limit or an unreachable chat — the user should not.
            follow = asyncio.create_task(self._finish(task, user_id))
            self._background.add(follow)
            follow.add_done_callback(self._background.discard)
            return t(lang, "receipt_saved")
        try:
            task.result()
        except ReceiptError:
            return t(lang, "receipt_already")
        return t(lang, "receipt_saved")

    async def _finish(self, task: asyncio.Task[Any], user_id: int) -> None:
        from svbg.billing.receipts import ReceiptError

        async with guard("tg:receipt_card", hub=self._hub, user_id=user_id):
            try:
                await task
            except ReceiptError as e:  # decided meanwhile: nothing to post, nothing to report
                log.info("late receipt of user %s not posted: %s", user_id, e.code)

    async def wait_background(self) -> None:
        """Wait for the receipt cards still being posted (shutdown, tests)."""
        while self._background:
            await asyncio.gather(*list(self._background), return_exceptions=True)

    # ------------------------------------------------------------------------------------------ aiogram

    def router(self, name: str = "svbg-user-pay") -> Router:
        router = Router(name=name)

        @router.pre_checkout_query()
        async def on_pre_checkout(query: PreCheckoutQuery) -> None:
            reason: str | None = STARS_TEXTS["closed"]
            lang = "ru"
            async with guard("tg:pre_checkout", hub=self._hub):
                user = await self._users.load(query.from_user)
                lang = user.lang if user is not None else "ru"
                reason = await self.decide_pre_checkout(
                    query.invoice_payload, query.currency, query.total_amount
                )
            if reason is not None:
                reason = localize_spend(localize_pre_checkout(reason, lang), lang)
            try:
                await query.answer(ok=reason is None, error_message=reason)
            except TelegramAPIError as e:
                log.warning("answerPreCheckoutQuery failed: %s", type(e).__name__)

        @router.message(F.successful_payment)
        async def on_paid(message: Message) -> None:
            sp = message.successful_payment
            if sp is None:
                return
            # Telegram never repeats successful_payment: retry a transient failure (database restart) a few
            # times, then report it with the charge id so the owner can credit by hand.
            context = {"charge_id": sp.telegram_payment_charge_id, "payment_id": sp.invoice_payload}
            async with guard("tg:successful_payment", hub=self._hub, context=context):
                for attempt, delay in enumerate(CREDIT_RETRY_DELAYS):
                    try:
                        await self.credit_stars(
                            sp.invoice_payload,
                            charge_id=sp.telegram_payment_charge_id,
                            currency=sp.currency,
                            total_amount=sp.total_amount,
                        )
                        break
                    except (sa.exc.SQLAlchemyError, OSError):
                        if attempt == len(CREDIT_RETRY_DELAYS) - 1:
                            raise
                        await asyncio.sleep(delay)

        @router.message(F.chat.type == "private", F.photo | F.document)
        async def on_receipt(message: Message) -> None:
            tg_user = message.from_user
            if tg_user is None:
                raise SkipHandler
            user = await self._users.load(tg_user)
            if user is None:
                raise SkipHandler
            if message.photo:
                photo = message.photo[-1]
                args: dict[str, Any] = {"kind": "photo", "file_id": photo.file_id, "size": photo.file_size}
            elif message.document is not None:
                doc = message.document
                args = {
                    "kind": "document",
                    "file_id": doc.file_id,
                    "mime_type": doc.mime_type,
                    "size": doc.file_size,
                }
            else:  # pragma: no cover - the filter guarantees one of them
                raise SkipHandler
            reply: str | None = None
            async with guard("tg:receipt", hub=self._hub):
                reply = await self.submit_receipt(user.user_id, user.lang, caption=message.caption, **args)
            if reply is None:
                raise SkipHandler
            try:
                await message.answer(reply)
            except TelegramAPIError as e:
                log.warning("could not answer a receipt: %s", type(e).__name__)

        return router
