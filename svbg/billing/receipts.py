"""Manual payments by bank details (04 §8, §9.1): the user's receipt → a card in «💳 Оплаты» → an admin with
``payments.confirm`` confirms (the amount from the receipt is mandatory; a different amount → ``mismatch``) or
rejects (a reason is mandatory).

* The payment itself goes through the payment core (:meth:`PaymentCore.confirm_manual` / ``reject_manual``):
  the role is re-checked there on **every** press (a member of the admin group is not an admin of the bot),
  ``admin_audit`` is written in the same transaction, a second confirmation is a no-op (payment CAS).
* **One decision per receipt.** The receipt row is locked (``FOR UPDATE SKIP LOCKED``) for the whole decision:
  the core's change and ``submitted → confirmed | rejected`` happen under that lock, so «✅» and «❌» pressed
  at the same moment by two admins cannot both act — the second press gets «Уже решено» and changes nothing.
  A payment already ``canceled`` by a rejection is never confirmed through its receipt (the «поздняя оплата
  побеждает» rule of the core is for providers, not for a receipt an admin has rejected). The lock is held by
  a database transaction only — no Telegram call happens inside it (the card is updated after the commit).
* A resubmitted receipt replaces the file while it is not decided, at most :data:`RESUBMIT_MAX` times and not
  more often than every :data:`RESUBMIT_COOLDOWN` (each resubmission posts the file to the admin topic again);
  the same file again changes nothing.
* The card is posted / updated through :class:`ReceiptCards` (the tg layer: admin chat + buttons).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from svbg.billing.tables import manual_receipts
from svbg.core.clock import now
from svbg.core.tables import users
from svbg.payments.tables import payments
from svbg.services import roles

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from svbg.db.engine import Database
    from svbg.payments.core import ApplyResult

__all__ = [
    "RESUBMIT_COOLDOWN",
    "RESUBMIT_MAX",
    "ReceiptCards",
    "ReceiptDecision",
    "ReceiptError",
    "ReceiptView",
    "Receipts",
]

#: Re-sent receipts per payment (each one posts the file to «💳 Оплаты» again) and the pause between them.
RESUBMIT_MAX: Final = 3
RESUBMIT_COOLDOWN: Final = timedelta(minutes=1)
#: The admin right of a decision (the core re-checks it; billing checks it only where the core is not asked).
CONFIRM_PERM: Final = "payments.confirm"

TEXTS: Final[Mapping[str, str]] = {
    "not_found": "Платёж не найден.",
    "not_pending": "Этот платёж уже обработан.",
    "decided": "Уже решено другим администратором.",
    "too_often": "Чек уже получен. Новый файл можно прислать через минуту.",
    "too_many": "Чек уже получен — дождитесь проверки, администратор ответит.",
}


class ReceiptError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        self.text = TEXTS.get(code, code)
        super().__init__(self.text)


@dataclass(frozen=True, slots=True)
class ReceiptView:
    id: int
    payment_id: str
    user_id: int
    amount_minor: int
    currency: str
    file_id: str | None
    comment: str | None
    status: str
    card_ref: Mapping[str, Any] | None


@dataclass(frozen=True, slots=True)
class ReceiptDecision:
    outcome: Literal["confirmed", "rejected", "decided"]
    receipt_id: int
    payment_outcome: str | None = None  # applied | mismatch | ignored …


class ReceiptCards(Protocol):
    """The admin card in «💳 Оплаты» (tg layer)."""

    async def post(self, receipt: ReceiptView) -> Mapping[str, Any] | None:
        """Post the card with «Подтвердить» / «Отклонить»; returns its reference (stored as ``card_ref``)."""
        ...

    async def decided(self, receipt: ReceiptView, decision: ReceiptDecision) -> None:
        """Update the card: who decided and how (buttons removed)."""
        ...


class ManualPaymentsPort(Protocol):
    async def confirm_manual(
        self,
        payment_id: str,
        *,
        actor_telegram_id: int,
        owner_ids: frozenset[int] | set[int],
        paid_amount_minor: int,
        currency: str | None = None,
        reason: str | None = None,
    ) -> ApplyResult: ...

    async def reject_manual(
        self, payment_id: str, *, actor_telegram_id: int, owner_ids: frozenset[int] | set[int], reason: str
    ) -> bool: ...


def _view(row: Mapping[str, Any]) -> ReceiptView:
    return ReceiptView(
        id=int(row["id"]),
        payment_id=str(row["payment_id"]),
        user_id=int(row["user_id"]),
        amount_minor=int(row["amount_minor"]),
        currency=str(row["currency"]),
        file_id=row["file_id"],
        comment=row["comment"],
        status=str(row["status"]),
        card_ref=row["card_ref"],
    )


class Receipts:
    def __init__(
        self,
        db: Database,
        payments_core: ManualPaymentsPort,
        cards: ReceiptCards | None = None,
        *,
        clock: Callable[[], datetime] = now,
    ) -> None:
        self._db = db
        self._clock = clock
        self._core = payments_core
        self._cards = cards
        # receipt id → (resubmissions so far, when the card was last posted); per process — a restart only
        # gives a user a few more resubmissions, never a decision.
        self._posted: dict[int, tuple[int, datetime]] = {}

    async def submit(
        self, payment_id: str, user_id: int, *, file_id: str | None, comment: str | None = None
    ) -> ReceiptView:
        """The user sent a receipt for their pending manual payment. Re-sending replaces the file while the
        receipt is not decided (limited: ``too_often`` / ``too_many``); the same file again is a no-op.
        Posts (or re-posts) the admin card."""
        text = (comment or "").strip()[:500] or None
        at = self._clock()
        async with self._db.tx() as conn:
            pay = (
                await conn.execute(
                    sa.select(payments.c.amount_minor, payments.c.currency, payments.c.status).where(
                        payments.c.id == payment_id, payments.c.user_id == user_id
                    )
                )
            ).first()
            if pay is None:
                raise ReceiptError("not_found")
            if pay.status != "pending":
                raise ReceiptError("not_pending")
            current = (
                (
                    await conn.execute(
                        sa.select(manual_receipts)
                        .where(manual_receipts.c.payment_id == payment_id)
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if current is not None:
                if current["status"] != "submitted":
                    raise ReceiptError("decided")
                if current["file_id"] == file_id and current["comment"] == text:
                    return _view(current)  # the same receipt again: nothing to post
                self._check_resubmit(int(current["id"]), at)
            stmt = pg_insert(manual_receipts).values(
                payment_id=payment_id,
                user_id=user_id,
                amount_minor=int(pay.amount_minor),
                currency=str(pay.currency),
                file_id=file_id,
                comment=text,
            )
            row = (
                (
                    await conn.execute(
                        stmt.on_conflict_do_update(
                            index_elements=[manual_receipts.c.payment_id],
                            set_={"file_id": stmt.excluded.file_id, "comment": stmt.excluded.comment},
                            where=manual_receipts.c.status == "submitted",
                        ).returning(*manual_receipts.c)
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise ReceiptError("decided")
        view = _view(row)
        count = self._posted.get(view.id, (-1, at))[0] + 1
        self._posted[view.id] = (count, at)
        if self._cards is not None:
            card = await self._cards.post(view)
            if card is not None:
                async with self._db.tx() as conn:
                    await conn.execute(
                        sa.update(manual_receipts)
                        .where(manual_receipts.c.id == view.id)
                        .values(card_ref=dict(card))
                    )
                view = replace(view, card_ref=dict(card))
        return view

    def _check_resubmit(self, receipt_id: int, at: datetime) -> None:
        count, last = self._posted.get(receipt_id, (0, at - RESUBMIT_COOLDOWN))
        if count >= RESUBMIT_MAX:
            raise ReceiptError("too_many")
        if at - last < RESUBMIT_COOLDOWN:
            raise ReceiptError("too_often")

    async def get(self, receipt_id: int) -> ReceiptView | None:
        async with self._db.read() as conn:
            row = (
                (await conn.execute(sa.select(manual_receipts).where(manual_receipts.c.id == receipt_id)))
                .mappings()
                .first()
            )
        return None if row is None else _view(row)

    async def confirm(
        self,
        receipt_id: int,
        *,
        actor_telegram_id: int,
        owner_ids: frozenset[int] | set[int],
        paid_amount_minor: int,
        reason: str | None = None,
    ) -> ReceiptDecision:
        """Confirm with the amount from the receipt. ``PermissionDeniedError`` («Нет прав») comes from the
        core before anything changes."""
        from svbg.payments.core import PermissionDeniedError

        async with self._db.tx() as conn:
            receipt = await self._lock(conn, receipt_id)
            if receipt is None or receipt.status != "submitted":
                return ReceiptDecision("decided", receipt_id)
            status = await self._payment_status(conn, receipt.payment_id)
            if status == "canceled":
                # Rejected before (its receipt was not closed): never revived through the receipt.
                actor = await roles.load_actor(conn, telegram_id=actor_telegram_id, owner_ids=owner_ids)
                if actor is None or not actor.has_perm(CONFIRM_PERM):
                    raise PermissionDeniedError
                decision = await self._close(
                    conn, receipt, "rejected", actor_telegram_id, None, "отклонён ранее"
                )
                outcome = ReceiptDecision("decided", receipt_id, "canceled")
            else:
                result = await self._core.confirm_manual(
                    receipt.payment_id,
                    actor_telegram_id=actor_telegram_id,
                    owner_ids=owner_ids,
                    paid_amount_minor=paid_amount_minor,
                    currency=receipt.currency,
                    reason=reason,
                )
                decision = await self._close(
                    conn,
                    receipt,
                    "confirmed",
                    actor_telegram_id,
                    paid_amount_minor,
                    reason,
                    payment_outcome=result.outcome.value,
                )
                outcome = decision
        await self._announce(receipt, decision)
        return outcome

    async def reject(
        self, receipt_id: int, *, actor_telegram_id: int, owner_ids: frozenset[int] | set[int], reason: str
    ) -> ReceiptDecision:
        if not reason or not reason.strip():
            raise ValueError("reason is required")
        async with self._db.tx() as conn:
            receipt = await self._lock(conn, receipt_id)
            if receipt is None or receipt.status != "submitted":
                return ReceiptDecision("decided", receipt_id)
            changed = await self._core.reject_manual(
                receipt.payment_id, actor_telegram_id=actor_telegram_id, owner_ids=owner_ids, reason=reason
            )
            status = "canceled" if changed else await self._payment_status(conn, receipt.payment_id)
            if status in ("paid", "refunded"):
                # The money was confirmed before (its receipt was not closed): the card must say so.
                decision = await self._close(
                    conn, receipt, "confirmed", actor_telegram_id, None, None, payment_outcome="already_paid"
                )
                outcome = ReceiptDecision("decided", receipt_id, "already_paid")
            else:
                decision = await self._close(conn, receipt, "rejected", actor_telegram_id, None, reason)
                outcome = decision
        await self._announce(receipt, decision)
        return outcome

    async def _lock(self, conn: AsyncConnection, receipt_id: int) -> ReceiptView | None:
        """The receipt, locked for this decision; ``None`` when another admin is deciding it right now.
        Raises ``not_found`` for an unknown receipt."""
        row = (
            (
                await conn.execute(
                    sa.select(manual_receipts)
                    .where(manual_receipts.c.id == receipt_id)
                    .with_for_update(skip_locked=True)
                )
            )
            .mappings()
            .first()
        )
        if row is not None:
            return _view(row)
        exists = await conn.scalar(sa.select(manual_receipts.c.id).where(manual_receipts.c.id == receipt_id))
        if exists is None:
            raise ReceiptError("not_found")
        return None

    @staticmethod
    async def _payment_status(conn: AsyncConnection, payment_id: str) -> str | None:
        value = await conn.scalar(sa.select(payments.c.status).where(payments.c.id == payment_id))
        return None if value is None else str(value)

    @staticmethod
    async def _close(  # noqa: PLR0917 - internal helper, every argument is required
        conn: AsyncConnection,
        receipt: ReceiptView,
        status: Literal["confirmed", "rejected"],
        actor_telegram_id: int,
        amount: int | None,
        reason: str | None,
        *,
        payment_outcome: str | None = None,
    ) -> ReceiptDecision:
        actor = (
            await conn.execute(sa.select(users.c.id).where(users.c.telegram_id == actor_telegram_id))
        ).scalar()
        await conn.execute(
            sa.update(manual_receipts)
            .where(manual_receipts.c.id == receipt.id, manual_receipts.c.status == "submitted")
            .values(
                status=status,
                decided_by=actor,
                decided_amount_minor=amount,
                decision_reason=(reason or "").strip()[:500] or None,
                decided_at=now(),
            )
        )
        return ReceiptDecision(status, receipt.id, payment_outcome)

    async def _announce(self, receipt: ReceiptView, decision: ReceiptDecision) -> None:
        """Update the admin card after the commit (Telegram is never called under the receipt lock)."""
        self._posted.pop(receipt.id, None)
        if self._cards is not None:
            await self._cards.decided(receipt, decision)
