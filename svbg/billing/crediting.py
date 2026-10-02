"""Money arrives (07 §4.5 steps 4 and 6): the payment core's ``on_paid`` / ``on_refunded`` hooks.

Runs **inside the payment core's transaction**, right after its CAS ``pending|expired|canceled|failed → paid``
(exactly once per payment): either the payment, the credit and the auto-completed purchase commit together,
or nothing does (the provider retries, the reconciler re-checks).

``on_paid``:

1. lock the user (lock order ``payments`` → ``users`` → ``orders``);
2. a payment through a **test-mode** instance is test money: it is credited (``test_topup``) only to staff
   (:data:`~svbg.billing.checkout.TEST_ROLES` — the owner checking the cash desk end to end); anybody else
   gets nothing and the owner gets «Требует внимания» (a live cash desk left in test mode);
3. top-up ``awaiting_payment | canceled | expired → credited`` («поздняя оплата побеждает») and
   ``wallet_ledger(topup, +amount)`` keyed by the payment;
4. the parent purchase (if any) is decided by :func:`svbg.domain.wallet_rules.decide_autocomplete`:
   *complete* → ``wallet_ledger(purchase)``, parent ``paid``, ``jobs(fulfill)`` (NOTIFY on commit);
   *expired* / *not waiting* → the money stays on the balance, «💰 Зачислено…» with «Купить … за Z»;
   *held* (frozen) → parent ``held`` + «Требует внимания»; *insufficient* → «Пополните ещё на …»;
5. a payment without an order (legacy Stars payload, 06 §2.4.4) is credited to the payer as is (Stars at
   ``PAY_STARS_RATE``).

``on_refunded``: a chargeback / refund takes the refunded part of the credited amount back from the balance —
as much as there is (the balance never goes below zero); a remainder raises «Требует внимания». The refunded
amount comes from the provider's report (``refund_minor``, in the payment's currency); without it the whole
credited amount is taken and the owner is told to check.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, Final

import sqlalchemy as sa

from svbg.billing import wallet
from svbg.billing.checkout import TEST_ROLES
from svbg.billing.config import BillingConfig, ConfigSource
from svbg.billing.kinds import enqueue_attention, enqueue_fulfill, enqueue_notice
from svbg.billing.tables import orders, users_wallet
from svbg.billing.texts import ATTENTION, money
from svbg.core.clock import now
from svbg.domain.wallet_rules import AutocompleteFacts, Decision, decide_autocomplete
from svbg.subscriptions.hold import can_spend

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from svbg.payments.core import PaymentRecord

__all__ = [
    "CREDIT_REASONS_BY_PAYMENT",
    "HELD_SCREEN",
    "Crediting",
    "SpendCheck",
    "held_fix_action",
    "refund_share",
    "spend_refusal",
]

log = logging.getLogger("svbg.billing")

#: Ledger reasons that credit a payment (a refund of the payment takes the same amount back).
CREDIT_REASONS_BY_PAYMENT: Final = ("topup", "test_topup", "payment_credit", "stars_legacy")
_REOPENABLE: Final = ("awaiting_payment", "canceled", "expired")
#: Admin screen (tg layer) that shows a ``held`` order and the two decisions; ``arg`` = the order id.
HELD_SCREEN: Final = "bill.held"

#: ``(conn, user_id) -> True`` when the user may spend (freeze / ban, X5). Default: subscriptions' check.
SpendCheck = Callable[["AsyncConnection", int], Any]


async def _default_spend(conn: AsyncConnection, user_id: int) -> bool:
    return bool(await can_spend(conn, user_id))


async def spend_refusal(conn: AsyncConnection, user_id: int) -> str | None:
    """The payment core's ``SpendGuard`` (X5): ``None`` = may pay, else the text for the user."""
    check = await can_spend(conn, user_id)
    return None if check else check.text


def refund_share(credited_minor: int, paid_minor: int, refund_minor: int | None) -> int:
    """How much of ``credited_minor`` (shop currency) a refund of ``refund_minor`` out of ``paid_minor`` (the
    payment's currency) takes back: proportional, rounded up (the cash desk already gave that money back),
    never more than was credited. ``None`` (amount unknown) → everything."""
    if credited_minor <= 0:
        return 0
    if refund_minor is None or paid_minor <= 0 or refund_minor >= paid_minor:
        return credited_minor
    if refund_minor <= 0:
        return 0
    return min(credited_minor, -(-credited_minor * refund_minor // paid_minor))


class Crediting:
    def __init__(self, *, config: ConfigSource, spend: SpendCheck | None = None) -> None:
        self._config = config
        self._spend = spend or _default_spend

    @property
    def config(self) -> BillingConfig:
        return BillingConfig.from_mapping(self._config())

    # --------------------------------------------------------------------------------------- on_paid

    async def on_paid(self, conn: AsyncConnection, payment: PaymentRecord) -> None:
        cfg = self.config
        locked = await wallet.lock_user(conn, payment.user_id)
        if locked is None:
            raise RuntimeError(f"payment {payment.id}: user {payment.user_id} does not exist")
        topup = None
        if payment.order_id is not None:
            topup = (
                (
                    await conn.execute(
                        sa.select(orders).where(orders.c.id == payment.order_id).with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if topup is not None and (topup["kind"] != "topup" or topup["user_id"] != payment.user_id):
                log.warning(
                    "payment %s points to order %s which is not the user's top-up", payment.id, topup["id"]
                )
                topup = None
        if payment.is_test and not await self._is_staff(conn, payment.user_id):
            await self._refuse_test(conn, payment, topup)
            return
        if topup is None:
            await self._credit_without_order(conn, payment, cfg)
            return
        at = now()
        await conn.execute(
            sa.update(orders)
            .where(orders.c.id == topup["id"], orders.c.status.in_(_REOPENABLE))
            .values(status="credited", paid_at=at, updated_at=at)
        )
        amount = int(topup["total_minor"])
        entry = await wallet.credit(
            conn,
            payment.user_id,
            amount,
            reason="test_topup" if payment.is_test else "topup",
            ref_type="payment",
            ref_id=payment.id,
            currency=cfg.currency,
            note="тестовый платёж" if payment.is_test else None,
        )
        if entry is None:  # cannot happen after the payment CAS; never credit twice anyway
            log.error("payment %s: top-up %s was already credited", payment.id, topup["id"])
            return
        notice: dict[str, Any] = {
            "type": "credited",
            "user_id": payment.user_id,
            "ui_ref": topup["ui_ref"],
            "amount_minor": amount,
            "balance_minor": entry.balance_after,
            "currency": cfg.currency,
        }
        parent_id = topup["parent_order_id"]
        if parent_id is not None:
            done = await self._autocomplete(
                conn,
                payment,
                parent_id=int(parent_id),
                balance=entry.balance_after,
                topup=topup,
                notice=notice,
            )
            if done:
                return
        await enqueue_notice(conn, notice, dedup_key=f"notice:payment:{payment.id}")

    @staticmethod
    async def _is_staff(conn: AsyncConnection, user_id: int) -> bool:
        role = await conn.scalar(sa.select(users_wallet.c.role).where(users_wallet.c.id == user_id))
        return role in TEST_ROLES

    @staticmethod
    async def _refuse_test(
        conn: AsyncConnection, payment: PaymentRecord, topup: Mapping[str, Any] | None
    ) -> None:
        """Test money of an ordinary user: nothing is credited, nothing is completed (the payment stays
        ``paid`` in the core — that is what the cash desk reported)."""
        if topup is not None:
            await conn.execute(
                sa.update(orders)
                .where(orders.c.id == topup["id"], orders.c.status.in_(_REOPENABLE))
                .values(status="canceled", note="test_payment", updated_at=now())
            )
        paid = payment.paid_amount_minor if payment.paid_amount_minor is not None else payment.amount_minor
        log.warning(
            "payment %s: test payment of a non-staff user %s not credited", payment.id, payment.user_id
        )
        await enqueue_attention(
            conn,
            f"billing:test_payment:{payment.instance_id}",
            "error",
            ATTENTION["test_payment_title"],
            ATTENTION["test_payment_body"].format(
                payment=payment.id, amount=money(paid, payment.currency.upper()), user=payment.user_id
            ),
        )

    async def _autocomplete(
        self,
        conn: AsyncConnection,
        payment: PaymentRecord,
        *,
        parent_id: int,
        balance: int,
        topup: Mapping[str, Any],
        notice: dict[str, Any],
    ) -> bool:
        """Step 4 of 07 §4.5 for the parent purchase. ``True`` when the purchase was completed (the fulfill
        flow will show the result in the same message, no «Зачислено» notice)."""
        parent = (
            (await conn.execute(sa.select(orders).where(orders.c.id == parent_id).with_for_update()))
            .mappings()
            .first()
        )
        if parent is None or parent["user_id"] != payment.user_id:
            return False
        at = now()
        price = int(parent["total_minor"])
        decision = decide_autocomplete(
            AutocompleteFacts(
                parent_status=str(parent["status"]),
                now=at,
                autocomplete_until=parent["autocomplete_until"],
                can_spend=bool(await self._spend(conn, payment.user_id)),
                balance_minor=balance,
                price_minor=price,
            )
        )
        title = str((parent["snapshot"] or {}).get("title") or "")
        offer = {"order_id": parent_id, "title": title, "price_minor": price}
        if decision is Decision.COMPLETE:
            entry = None
            if price > 0:
                entry = await wallet.debit(
                    conn,
                    payment.user_id,
                    price,
                    reason="purchase",
                    ref_type="order",
                    ref_id=parent_id,
                    currency=str(parent["currency"]),
                )
            if price == 0 or entry is not None:
                await conn.execute(
                    sa.update(orders)
                    .where(orders.c.id == parent_id)
                    .values(
                        status="paid",
                        paid_at=at,
                        autocomplete_until=None,
                        # The message that shows the purchase: the top-up's (it has the pay button) if newer.
                        ui_ref=topup["ui_ref"] or parent["ui_ref"],
                        updated_at=at,
                    )
                )
                await enqueue_fulfill(conn, parent_id)
                return True
            decision = Decision.INSUFFICIENT  # the CAS lost (cannot happen under the user lock)
        if decision is Decision.EXPIRED:
            if parent["status"] == "awaiting_funds":
                await conn.execute(
                    sa.update(orders)
                    .where(orders.c.id == parent_id, orders.c.status == "awaiting_funds")
                    .values(status="expired", updated_at=at)
                )
            notice.update(reason="late", **offer)
        elif decision is Decision.HELD:
            await conn.execute(
                sa.update(orders)
                .where(orders.c.id == parent_id, orders.c.status == "awaiting_funds")
                .values(status="held", note="frozen", autocomplete_until=None, updated_at=at)
            )
            notice.update(reason="held")
            await enqueue_attention(
                conn,
                f"billing:held:{parent_id}",
                "warn",
                ATTENTION["held_title"],
                ATTENTION["held_body"].format(
                    order=parent_id,
                    user=payment.user_id,
                    amount=money(int(notice["amount_minor"]), str(notice["currency"])),
                    title=title,
                ),
                fix_action=held_fix_action(parent_id),
            )
        elif decision is Decision.INSUFFICIENT:
            notice.update(reason="insufficient", **offer)
        elif parent["status"] in ("canceled", "expired"):
            notice.update(reason="late", **offer)  # replaced / given up: offer it again at today's price
        return False

    async def _credit_without_order(
        self, conn: AsyncConnection, payment: PaymentRecord, cfg: BillingConfig
    ) -> None:
        paid = payment.paid_amount_minor if payment.paid_amount_minor is not None else payment.amount_minor
        currency = payment.currency.upper()
        amount: int | None
        if currency == cfg.currency:
            amount = paid
        elif currency == "XTR" and cfg.stars_rate_minor is not None:
            amount = paid * cfg.stars_rate_minor
        else:
            amount = None
        if not amount:
            await enqueue_attention(
                conn,
                f"billing:unpriced:{payment.id}",
                "error",
                ATTENTION["unpriced_title"],
                ATTENTION["unpriced_body"].format(
                    payment=payment.id, amount=f"{paid} {currency}", user=payment.user_id
                ),
            )
            return
        if payment.is_test:
            reason = "test_topup"
        else:
            reason = "stars_legacy" if payment.metadata.get("legacy_payload") else "payment_credit"
        entry = await wallet.credit(
            conn,
            payment.user_id,
            amount,
            reason=reason,
            ref_type="payment",
            ref_id=payment.id,
            currency=cfg.currency,
        )
        if entry is None:
            return
        await enqueue_notice(
            conn,
            {
                "type": "credited",
                "user_id": payment.user_id,
                "ui_ref": None,
                "amount_minor": amount,
                "balance_minor": entry.balance_after,
                "currency": cfg.currency,
            },
            dedup_key=f"notice:payment:{payment.id}",
        )

    # ----------------------------------------------------------------------------------- on_refunded

    async def on_refunded(
        self, conn: AsyncConnection, payment: PaymentRecord, refund_minor: int | None = None
    ) -> None:
        """Take a chargeback / refund back from the balance. ``refund_minor``: the refunded amount in the
        payment's currency as the cash desk reported it (``None`` — not reported: everything credited is
        taken and the owner is asked to check)."""
        cfg = self.config
        if await wallet.lock_user(conn, payment.user_id) is None:
            return
        credited = 0
        for reason in CREDIT_REASONS_BY_PAYMENT:
            entry = await wallet.find(conn, payment.user_id, reason, "payment", payment.id)
            if entry is not None:
                credited += entry.amount_minor
        if credited <= 0:
            return
        if await wallet.find(conn, payment.user_id, "chargeback", "payment", payment.id) is not None:
            return
        paid = payment.paid_amount_minor if payment.paid_amount_minor is not None else payment.amount_minor
        due = refund_share(credited, paid, refund_minor)
        if due <= 0:
            return
        taken = await wallet.take(
            conn,
            payment.user_id,
            due,
            reason="chargeback",
            ref_type="payment",
            ref_id=payment.id,
            currency=cfg.currency,
            note="возврат / чарджбэк" if due == credited else "частичный возврат",
        )
        if refund_minor is None:
            await enqueue_attention(
                conn,
                f"billing:refund_amount:{payment.id}",
                "warn",
                ATTENTION["refund_amount_title"],
                ATTENTION["refund_amount_body"].format(
                    payment=payment.id, user=payment.user_id, amount=money(due, cfg.currency)
                ),
            )
        if taken < due:
            await enqueue_attention(
                conn,
                f"billing:chargeback:{payment.id}",
                "error",
                ATTENTION["chargeback_title"],
                ATTENTION["chargeback_body"].format(
                    payment=payment.id,
                    amount=money(due, cfg.currency),
                    user=payment.user_id,
                    taken=money(taken, cfg.currency),
                    missing=money(due - taken, cfg.currency),
                ),
            )


def held_fix_action(order_id: int) -> str:
    """``fix_action`` of a ``held`` purchase: the admin screen with «Зачесть в заморозку» / «Вернуть на
    кошелёк» (:meth:`svbg.billing.fulfill.Fulfiller.resolve_held`)."""
    return f"screen:{HELD_SCREEN}:{int(order_id)}"
