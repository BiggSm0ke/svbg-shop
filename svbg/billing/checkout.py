"""Checkout (07 §4.5 steps 1–3): drafts with a frozen price, «Оплатить» from the balance, top-ups.

* :meth:`CheckoutService.draft_plan` / :meth:`draft_devices` / :meth:`reorder` — price from the in-memory
  catalog (no SQL), kind decided from the user's live subscription; **2 SQL** (one read, one CTE insert of the
  order and its items), 0 HTTP.
* :meth:`CheckoutService.pay` — «Оплатить». One transaction: lock the user (+ freeze/ban check), lock the
  order, then either the CAS debit → ``paid`` + ``jobs(fulfill)`` (NOTIFY on commit), or ``awaiting_funds``
  with ``autocomplete_until`` and ``ui_ref``; the user's previous waiting purchase is canceled first (one
  waiting purchase per user — also a partial unique index). 0 HTTP. A purchase keeps its frozen price for at
  most :data:`PURCHASE_TTL` after the draft: later «Оплатить» is refused (``stale_price``) and the
  auto-complete window never reaches past it, however often it is restarted.
* :meth:`CheckoutService.start_topup` — ``orders(topup, parent_order_id)`` + ``payments(pending)`` in one
  transaction through the payment core (spending guards, limits), then the provider invoice outside any
  transaction (timeout inside the core). The parent's auto-complete window restarts from the invoice; a parent
  that no longer waits for money is refused (``order_gone``) instead of silently becoming a plain top-up. An
  open invoice for the same method, amount and purchase is handed out again instead of a new one, and a user
  gets at most :data:`TOPUP_RATE_MAX` invoices per :data:`TOPUP_RATE_WINDOW` (the provider's rate limits and
  the status poller are shared by all users). A test-mode instance takes only staff
  (:data:`TEST_ROLES`).
* :meth:`CheckoutService.sweep` — periodic: waiting purchases past their window → ``expired``, stale drafts
  are deleted (a draft never held money), week-old unpaid top-ups → ``expired`` (a late payment is still
  credited).

Lock order (deadlock-free with crediting and fulfill): ``users`` → ``orders``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

import sqlalchemy as sa

from svbg.billing import wallet
from svbg.billing.config import BillingConfig, ConfigSource
from svbg.billing.kinds import enqueue_fulfill
from svbg.billing.ports import UiRef
from svbg.billing.tables import order_items, orders, users_wallet
from svbg.billing.texts import ERRORS, money
from svbg.core.clock import now
from svbg.domain.pricing import Discount, PricingError, Quote, purchase_kind, quote_devices, quote_plan
from svbg.domain.wallet_rules import shortfall, stars_quote
from svbg.payments.tables import payments as payments_t
from svbg.subscriptions.hold import spend_check
from svbg.subscriptions.lifecycle import LIVE_STATES
from svbg.subscriptions.tables import subscriptions

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from svbg.db.engine import Database
    from svbg.payments.core import CheckoutResult, PendingPayment

__all__ = [
    "DRAFT_TTL",
    "INVOICE_REUSE_AGE",
    "PURCHASE_TTL",
    "TEST_ROLES",
    "TOPUP_RATE_MAX",
    "TOPUP_RATE_WINDOW",
    "TOPUP_TTL",
    "BillingError",
    "CatalogPort",
    "CheckoutService",
    "Draft",
    "PayResult",
    "PaymentsPort",
    "TopupResult",
]

log = logging.getLogger("svbg.billing")

DRAFT_TTL: Final = timedelta(days=1)
#: How long a purchase keeps its frozen price: «Оплатить» and the auto-complete window end there.
PURCHASE_TTL: Final = DRAFT_TTL
TOPUP_TTL: Final = timedelta(days=7)
#: Invoices per user (any method) in a sliding window: the providers and the status poller are shared.
TOPUP_RATE_MAX: Final = 5
TOPUP_RATE_WINDOW: Final = timedelta(minutes=10)
#: An open invoice for the same method, amount and purchase is shown again instead of creating a new one.
INVOICE_REUSE_AGE: Final = timedelta(minutes=30)
#: In-chat invoices are never handed out twice (a second invoice message could be paid a second time).
_REUSABLE_KINDS: Final = ("url", "details")
_REUSE_MARGIN: Final = timedelta(minutes=2)  # an invoice about to expire is not handed out again
#: Roles whose payments through a test-mode instance count (test money is not real money).
TEST_ROLES: Final = ("owner", "admin")
_SWEEP_BATCH: Final = 1000


class BillingError(Exception):
    """A refused checkout step; ``text`` is shown to the user (Russian). A third argument (an old English
    translation) is accepted and ignored."""

    def __init__(self, code: str, text: str | None = None, _unused: str | None = None) -> None:
        self.code = code
        self.text = text or ERRORS.get(code, "Не получилось. Попробуйте ещё раз.")
        super().__init__(f"{code}: {self.text}")

    def localized(self, _lang: str | None = None) -> str:
        """The refusal text (kept for old callers that pass a language; always Russian)."""
        return self.text


class CatalogPort(Protocol):
    """``svbg.catalog.CatalogService`` (only the snapshot is read: no SQL on the hot path)."""

    @property
    def snapshot(self) -> Any: ...


class PaymentsPort(Protocol):
    """The part of :class:`svbg.payments.core.PaymentCore` the checkout uses."""

    instances: Any

    async def insert_pending(
        self,
        conn: AsyncConnection,
        *,
        user_id: int,
        instance_id: int,
        amount_minor: int,
        currency: str,
        description: str,
        order_id: int | None = None,
        method_kind: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> PendingPayment: ...

    async def open_checkout(self, pending: PendingPayment) -> CheckoutResult: ...


@dataclass(frozen=True, slots=True)
class _Buyer:
    has_paid: bool


@dataclass(frozen=True, slots=True)
class Draft:
    order_id: int
    public_id: str
    quote: Quote
    balance_minor: int

    @property
    def missing_minor(self) -> int:
        return shortfall(self.balance_minor, self.quote.total_minor)


@dataclass(frozen=True, slots=True)
class PayResult:
    """``paid``: debited, fulfill queued. ``awaiting_funds``: show «Не хватает X» (``missing_minor``).
    ``denied``: frozen / banned (``text``). ``already``: the order was paid before (a double tap)."""

    outcome: Literal["paid", "awaiting_funds", "denied", "already"]
    order_id: int
    price_minor: int
    balance_minor: int
    currency: str
    missing_minor: int = 0
    text: str | None = None
    canceled_order_id: int | None = None


@dataclass(frozen=True, slots=True)
class TopupResult:
    """``parent_order_id``: the purchase this top-up completes (``None`` — a plain top-up: the screen says
    «покупка завершится сама» only when it is set). ``reused``: an open invoice handed out again."""

    order_id: int
    payment_id: str
    credit_minor: int  # what lands on the balance (shop currency)
    pay_amount_minor: int  # what the user pays (in pay_currency: RUB, XTR…)
    pay_currency: str
    checkout: Any  # svbg.sdk.Checkout: pay_url | invoice | details
    parent_order_id: int | None = None
    reused: bool = False


def _facts_query(user_id: int, source_order_id: int | None = None) -> sa.Select[Any]:
    """The buyer in one row: balance, «paid before», the live subscription (if any) and, for a reorder, the
    kind and snapshot of the user's earlier purchase ``source_order_id`` (``NULL`` when it is not theirs)."""
    live = (
        sa.select(
            subscriptions.c.id,
            subscriptions.c.plan_id,
            subscriptions.c.is_trial,
            subscriptions.c.paid_until,
            subscriptions.c.extra_devices,
            subscriptions.c.plan_snapshot,
        )
        .where(subscriptions.c.user_id == user_id, subscriptions.c.link_state.in_(LIVE_STATES))
        .order_by(subscriptions.c.id.desc())
        .limit(1)
        .lateral("live")
    )
    paid_before = (
        sa.select(sa.literal(1))
        .where(orders.c.user_id == user_id, orders.c.status.in_(("paid", "fulfilled")))
        .exists()
    )
    columns: list[Any] = [
        users_wallet.c.wallet_minor,
        paid_before.label("has_paid"),
        live.c.id.label("sub_id"),
        live.c.plan_id,
        live.c.is_trial,
        live.c.paid_until,
        live.c.extra_devices,
        live.c.plan_snapshot,
    ]
    if source_order_id is not None:
        src = orders.alias("src")
        mine = (src.c.id == source_order_id, src.c.user_id == user_id, src.c.kind != "topup")
        columns += [
            sa.select(src.c.kind).where(*mine).scalar_subquery().label("src_kind"),
            sa.select(src.c.snapshot).where(*mine).scalar_subquery().label("src_snapshot"),
        ]
    return (
        sa.select(*columns)
        .select_from(users_wallet.outerjoin(live, sa.true()))
        .where(users_wallet.c.id == user_id)
    )


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), ensure_ascii=False, separators=(",", ":"), default=str)


def _order_values(user_id: int, quote: Quote) -> dict[str, Any]:
    return {
        "user_id": user_id,
        "kind": quote.kind,
        "status": "draft",
        "currency": quote.currency,
        "total_minor": quote.total_minor,
        "subscription_id": quote.subscription_id,
        "plan_id": quote.plan_id,
        "snapshot": quote.snapshot(),
    }


def _stored_checkout(raw: Any, external_id: str | None) -> Any:
    """``payments.checkout`` back into a :class:`svbg.sdk.Checkout` (``None``: damaged — not reused)."""
    from svbg.sdk import Checkout

    if not isinstance(raw, Mapping):
        return None
    try:
        expires = raw.get("expires_at")
        at = datetime.fromisoformat(str(expires)) if expires else None
        return Checkout(
            kind=raw["kind"],
            external_id=external_id,
            pay_url=raw.get("pay_url"),
            invoice=raw.get("invoice"),
            details=raw.get("details"),
            expires_at=at,
        )
    except (KeyError, TypeError, ValueError):
        return None


class CheckoutService:
    def __init__(
        self,
        db: Database,
        *,
        catalog: CatalogPort,
        payments: PaymentsPort | None,
        config: ConfigSource,
    ) -> None:
        self._db = db
        self._catalog = catalog
        self._payments = payments
        self._config = config
        self.topup_rate_max = TOPUP_RATE_MAX
        self.topup_rate_window = TOPUP_RATE_WINDOW

    @property
    def config(self) -> BillingConfig:
        return BillingConfig.from_mapping(self._config())

    # ------------------------------------------------------------------------------------------ drafts

    async def _insert_draft(self, conn: AsyncConnection, user_id: int, quote: Quote) -> tuple[int, str]:
        """One statement: the order and its items (a data-modifying CTE)."""
        new = (
            sa.insert(orders)
            .values(**_order_values(user_id, quote))
            .returning(orders.c.id, orders.c.public_id)
        )
        o = new.cte("o")
        rows = quote.item_rows()
        values = sa.values(
            sa.column("position", sa.SmallInteger),
            sa.column("type", sa.Text),
            sa.column("payload", sa.Text),
            sa.column("amount_minor", sa.BigInteger),
            name="v",
        ).data([(r["position"], r["type"], _json(r["payload"]), r["amount_minor"]) for r in rows])
        items = (
            sa.insert(order_items)
            .from_select(
                ["order_id", "position", "type", "payload", "amount_minor"],
                sa.select(
                    o.c.id,
                    values.c.position,
                    values.c.type,
                    sa.cast(values.c.payload, order_items.c.payload.type),
                    values.c.amount_minor,
                ).select_from(o.join(values, sa.true())),
            )
            .cte("i")
        )
        row = (await conn.execute(sa.select(o.c.id, o.c.public_id).add_cte(items))).one()
        return int(row.id), str(row.public_id)

    async def draft_plan(
        self,
        user_id: int,
        plan_id: int,
        days: int,
        *,
        extra_devices: int = 0,
        link_code: str | None = None,
        discounts: Sequence[Discount] = (),
        lang: str | None = None,
    ) -> Draft:
        """Price a plan period for the user and store a ``draft`` (2 SQL). Raises :class:`BillingError`."""
        async with self._db.tx() as conn:
            facts = (await conn.execute(_facts_query(user_id))).mappings().first()
            return await self._draft_plan_in(
                conn,
                facts,
                user_id,
                plan_id,
                days,
                extra_devices=extra_devices,
                link_code=link_code,
                discounts=discounts,
            )

    async def _draft_plan_in(
        self,
        conn: AsyncConnection,
        facts: Mapping[str, Any] | None,
        user_id: int,
        plan_id: int,
        days: int,
        *,
        extra_devices: int = 0,
        link_code: str | None = None,
        discounts: Sequence[Discount] = (),
    ) -> Draft:
        cfg = self.config
        snap = self._catalog.snapshot
        if facts is None:
            raise BillingError("not_found")
        has_live = facts["sub_id"] is not None
        kind = purchase_kind(
            live_plan_id=facts["plan_id"],
            live_is_trial=bool(facts["is_trial"]),
            has_live=has_live,
            plan_id=plan_id,
        )
        if kind == "renew":
            plan = snap.renewable(plan_id, currency=cfg.currency)
        else:
            plan = snap.purchasable(
                plan_id, _Buyer(bool(facts["has_paid"])), currency=cfg.currency, link_code=link_code
            )
        if plan is None:
            raise BillingError("plan_unavailable")
        try:
            quote = quote_plan(
                plan,
                days=days,
                currency=cfg.currency,
                kind=kind,
                extra_devices=extra_devices,
                discounts=discounts,
                subscription_id=facts["sub_id"],
            )
        except PricingError as e:
            raise BillingError("pricing", str(e)) from None
        order_id, public_id = await self._insert_draft(conn, user_id, quote)
        return Draft(order_id, public_id, quote, int(facts["wallet_minor"] or 0))

    async def draft_devices(self, user_id: int, count: int, *, lang: str | None = None) -> Draft:
        """Extra devices until the end of the current period (``addon_devices``, 2 SQL). ``lang`` is accepted
        for old callers and ignored."""
        async with self._db.tx() as conn:
            facts = (await conn.execute(_facts_query(user_id))).mappings().first()
            return await self._draft_devices_in(conn, facts, user_id, count)

    async def _draft_devices_in(
        self,
        conn: AsyncConnection,
        facts: Mapping[str, Any] | None,
        user_id: int,
        count: int,
    ) -> Draft:
        cfg = self.config
        if facts is None or facts["sub_id"] is None or facts["is_trial"]:
            raise BillingError("plan_unavailable", "Докупка устройств доступна для оплаченной подписки.")
        plan = self._catalog.snapshot.plan(facts["plan_id"])
        addon = plan.addon if plan is not None else None
        device_limit = plan.device_limit if plan is not None else None
        left = (facts["paid_until"] - now()).total_seconds() if facts["paid_until"] else 0.0
        try:
            quote = quote_devices(
                addon,
                device_limit=device_limit,
                current_extra=int(facts["extra_devices"] or 0),
                count=count,
                seconds_left=left,
                currency=cfg.currency,
                plan_snapshot=dict(facts["plan_snapshot"] or {}),
                subscription_id=int(facts["sub_id"]),
                title=plan.title() if plan is not None else "",
            )
        except PricingError as e:
            raise BillingError("pricing", str(e)) from None
        order_id, public_id = await self._insert_draft(conn, user_id, quote)
        return Draft(order_id, public_id, quote, int(facts["wallet_minor"] or 0))

    async def reorder(self, order_id: int, user_id: int, *, lang: str | None = None) -> Draft:
        """A fresh draft with the same plan / period / devices at today's prices («Купить … за Z ₽» after a
        late top-up). **2 SQL**: the earlier order's snapshot rides on the buyer's facts row."""
        async with self._db.tx() as conn:
            facts = (await conn.execute(_facts_query(user_id, order_id))).mappings().first()
            if facts is None or facts["src_kind"] is None:
                raise BillingError("not_found")
            snap = facts["src_snapshot"] or {}
            if facts["src_kind"] == "addon_devices":
                count = int(snap.get("extra_devices") or 1)
                return await self._draft_devices_in(conn, facts, user_id, count)
            plan_id, days = snap.get("plan_id"), snap.get("days")
            if not isinstance(plan_id, int) or not isinstance(days, int):
                raise BillingError("plan_unavailable")
            return await self._draft_plan_in(
                conn,
                facts,
                user_id,
                plan_id,
                days,
                extra_devices=int(snap.get("extra_devices") or 0),
            )

    # --------------------------------------------------------------------------------------------- pay

    async def pay(self, order_id: int, user_id: int, *, ui_ref: UiRef | None = None) -> PayResult:
        """«Оплатить» (07 §4.5 step 1): one transaction, 0 HTTP."""
        cfg = self.config
        at = now()
        async with self._db.tx() as conn:
            user = await self._lock_buyer(conn, user_id)
            if user is None:
                raise BillingError("not_found")
            order = (
                (
                    await conn.execute(
                        sa.select(orders)
                        .where(orders.c.id == order_id, orders.c.user_id == user_id, orders.c.kind != "topup")
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if order is None:
                raise BillingError("not_found")
            price, currency, balance = (
                int(order["total_minor"]),
                str(order["currency"]),
                int(user["wallet_minor"]),
            )
            if order["status"] in ("paid", "fulfilled"):
                return PayResult("already", order_id, price, balance, currency)
            if order["status"] not in ("draft", "awaiting_funds"):
                raise BillingError("not_payable")
            deadline = order["created_at"] + PURCHASE_TTL
            if deadline <= at:
                raise BillingError("stale_price")  # the frozen price is not kept forever
            gate = spend_check(banned_at=user["banned_at"], hold_kind=user["hold_kind"])
            if not gate:
                return PayResult("denied", order_id, price, balance, currency, text=gate.text)
            ref = {"ui_ref": ui_ref.as_json()} if ui_ref is not None else {}
            if price == 0 or balance >= price:
                entry = None
                if price > 0:
                    entry = await wallet.debit(
                        conn,
                        user_id,
                        price,
                        reason="purchase",
                        ref_type="order",
                        ref_id=order_id,
                        currency=currency,
                    )
                if price == 0 or entry is not None:
                    await conn.execute(
                        sa.update(orders)
                        .where(orders.c.id == order_id)
                        .values(status="paid", paid_at=at, autocomplete_until=None, updated_at=at, **ref)
                    )
                    await enqueue_fulfill(conn, order_id)
                    left = entry.balance_after if entry is not None else balance
                    return PayResult("paid", order_id, price, left, currency)
            # Not enough money: this purchase waits for a top-up; the previous waiting one is replaced.
            canceled = (
                await conn.execute(
                    sa.update(orders)
                    .where(
                        orders.c.user_id == user_id,
                        orders.c.status == "awaiting_funds",
                        orders.c.id != order_id,
                    )
                    .values(status="canceled", note="replaced", updated_at=at)
                    .returning(orders.c.id)
                )
            ).scalar()
            until = min(at + timedelta(minutes=cfg.autocomplete_minutes), deadline)
            await conn.execute(
                sa.update(orders)
                .where(orders.c.id == order_id)
                .values(status="awaiting_funds", autocomplete_until=until, updated_at=at, **ref)
            )
        return PayResult(
            "awaiting_funds",
            order_id,
            price,
            balance,
            currency,
            missing_minor=shortfall(balance, price),
            canceled_order_id=canceled,
        )

    @staticmethod
    async def _lock_buyer(conn: AsyncConnection, user_id: int) -> Mapping[str, Any] | None:
        held = (
            sa.select(subscriptions.c.hold_kind)
            .where(subscriptions.c.user_id == users_wallet.c.id, subscriptions.c.hold_kind.is_not(None))
            .limit(1)
            .correlate(users_wallet)
            .scalar_subquery()
        )
        return (
            (
                await conn.execute(
                    sa.select(users_wallet.c.wallet_minor, users_wallet.c.banned_at, held.label("hold_kind"))
                    .where(users_wallet.c.id == user_id)
                    .with_for_update(of=users_wallet)
                )
            )
            .mappings()
            .first()
        )

    async def cancel(self, order_id: int, user_id: int) -> bool:
        """The user backs out of a draft or a waiting purchase (money on the balance stays there)."""
        async with self._db.tx() as conn:
            done = (
                await conn.execute(
                    sa.update(orders)
                    .where(
                        orders.c.id == order_id,
                        orders.c.user_id == user_id,
                        orders.c.status.in_(("draft", "awaiting_funds")),
                    )
                    .values(status="canceled", note="by_user", updated_at=now())
                    .returning(orders.c.id)
                )
            ).first()
        return done is not None

    # ------------------------------------------------------------------------------------------ top-up

    def topup_quote(self, instance: Any, credit_minor: int) -> tuple[int, int, str]:
        """``(credit_minor, pay_amount_minor, pay_currency)`` for ``instance``: the shop currency when the
        method takes it, else Stars at ``PAY_STARS_RATE`` (rounded up to a whole ⭐)."""
        cfg = self.config
        if instance.accepts(cfg.currency):
            return credit_minor, credit_minor, cfg.currency
        if instance.accepts("XTR"):
            if cfg.stars_rate_minor is None:
                raise BillingError("no_stars_rate")
            q = stars_quote(credit_minor, cfg.stars_rate_minor)
            return q.credit_minor, q.stars, "XTR"
        raise BillingError("topup_currency")

    async def start_topup(
        self,
        user_id: int,
        *,
        instance_id: int,
        amount_minor: int,
        parent_order_id: int | None = None,
        ui_ref: UiRef | None = None,
        method_kind: str | None = None,
        allow_test: bool = False,
        lang: str | None = None,
    ) -> TopupResult:
        """Create a top-up and its invoice (07 §4.5 step 3) — or hand out the same open invoice again.
        ``lang`` is accepted for old callers and ignored (the invoice description is Russian).

        Raises :class:`BillingError` (``order_gone``: the purchase no longer waits for money — the user must
        open it again; ``too_many_invoices``; ``stale_price``…) or the core's ``CheckoutError`` /
        ``SpendDeniedError`` (``.human`` for the user). ``allow_test``: the caller knows the user is staff (a
        test-mode instance is otherwise offered only to :data:`TEST_ROLES`)."""
        if self._payments is None:
            raise BillingError("topup_currency")
        cfg = self.config
        if (
            isinstance(amount_minor, bool)
            or not isinstance(amount_minor, int)
            or not cfg.topup_min_minor <= amount_minor <= cfg.topup_max_minor
        ):
            raise BillingError(
                "topup_amount",
                f"Сумма пополнения — от {money(cfg.topup_min_minor, cfg.currency)} "
                f"до {money(cfg.topup_max_minor, cfg.currency)}.",
            )
        inst = self._payments.instances.get(instance_id)
        if inst is None or not inst.enabled:
            raise BillingError("topup_currency")
        credit_minor, pay_minor, pay_currency = self.topup_quote(inst, amount_minor)
        at = now()
        reused: TopupResult | None = None
        async with self._db.tx() as conn:
            role = (
                await conn.execute(
                    sa.select(users_wallet.c.role).where(users_wallet.c.id == user_id).with_for_update()
                )
            ).first()
            if role is None:
                raise BillingError("not_found")
            if getattr(inst, "is_test", False) and not (allow_test or role.role in TEST_ROLES):
                raise BillingError("topup_currency")  # test money must not reach a customer's balance
            parent_id = None
            if parent_order_id is not None:
                parent_id = await self._restart_window(
                    conn, user_id, parent_order_id, ui_ref=ui_ref, at=at, cfg=cfg
                )
            reused = await self._reuse_invoice(
                conn,
                user_id,
                instance_id=instance_id,
                pay_minor=pay_minor,
                pay_currency=pay_currency,
                parent_id=parent_id,
                ui_ref=ui_ref,
                at=at,
            )
            if reused is None:
                recent = await conn.scalar(
                    sa.select(sa.func.count()).where(
                        orders.c.user_id == user_id,
                        orders.c.kind == "topup",
                        orders.c.created_at > at - self.topup_rate_window,
                    )
                )
                if int(recent or 0) >= self.topup_rate_max:
                    raise BillingError("too_many_invoices")
                snapshot = {
                    "v": 1,
                    "instance_id": instance_id,
                    "pay_amount_minor": pay_minor,
                    "pay_currency": pay_currency,
                    "method_kind": method_kind,
                }
                topup_id = (
                    await conn.execute(
                        sa.insert(orders)
                        .values(
                            user_id=user_id,
                            kind="topup",
                            status="awaiting_payment",
                            currency=cfg.currency,
                            total_minor=credit_minor,
                            parent_order_id=parent_id,
                            snapshot=snapshot,
                            ui_ref=ui_ref.as_json() if ui_ref is not None else None,
                        )
                        .returning(orders.c.id)
                    )
                ).scalar_one()
                pending = await self._payments.insert_pending(
                    conn,
                    user_id=user_id,
                    instance_id=instance_id,
                    amount_minor=pay_minor,
                    currency=pay_currency,
                    description=f"Пополнение баланса на {money(credit_minor, cfg.currency)}",
                    order_id=int(topup_id),
                    method_kind=method_kind,
                )
        if reused is not None:
            return reused
        try:
            result = await self._payments.open_checkout(pending)
        except Exception:
            # The invoice was not created; the payment is ``failed`` (a late payment would still be credited).
            async with self._db.tx() as conn:
                await conn.execute(
                    sa.update(orders)
                    .where(orders.c.id == topup_id, orders.c.status == "awaiting_payment")
                    .values(status="canceled", note="invoice_failed", updated_at=now())
                )
            raise
        return TopupResult(
            int(topup_id),
            result.payment_id,
            credit_minor,
            pay_minor,
            pay_currency,
            result.checkout,
            parent_order_id=parent_id,
        )

    @staticmethod
    async def _restart_window(
        conn: AsyncConnection,
        user_id: int,
        parent_order_id: int,
        *,
        ui_ref: UiRef | None,
        at: datetime,
        cfg: BillingConfig,
    ) -> int:
        """«Сколько минут после создания счёта пополнения»: the parent's window restarts from this invoice
        (never past :data:`PURCHASE_TTL` of the purchase). A parent that does not wait for money any more —
        expired, replaced, canceled, already paid — is refused: the «покупка завершится сама» promise of the
        invoice screen would be false."""
        parent = (
            await conn.execute(
                sa.select(orders.c.id, orders.c.status, orders.c.autocomplete_until, orders.c.created_at)
                .where(orders.c.id == parent_order_id, orders.c.user_id == user_id, orders.c.kind != "topup")
                .with_for_update()
            )
        ).first()
        if parent is None or parent.status != "awaiting_funds":
            raise BillingError("order_gone")
        deadline = parent.created_at + PURCHASE_TTL
        if deadline <= at:
            raise BillingError("stale_price")
        until = min(
            max(parent.autocomplete_until, at + timedelta(minutes=cfg.autocomplete_minutes)), deadline
        )
        values: dict[str, Any] = {"autocomplete_until": until, "updated_at": at}
        if ui_ref is not None:
            values["ui_ref"] = ui_ref.as_json()
        await conn.execute(sa.update(orders).where(orders.c.id == parent.id).values(**values))
        return int(parent.id)

    @staticmethod
    async def _reuse_invoice(
        conn: AsyncConnection,
        user_id: int,
        *,
        instance_id: int,
        pay_minor: int,
        pay_currency: str,
        parent_id: int | None,
        ui_ref: UiRef | None,
        at: datetime,
    ) -> TopupResult | None:
        """The user's open invoice for the same method, amount and purchase (a repeated press, «назад» and the
        same button again) — no new order, payment or provider call."""
        p = payments_t
        row = (
            (
                await conn.execute(
                    sa.select(
                        orders.c.id,
                        orders.c.total_minor,
                        p.c.id.label("payment_id"),
                        p.c.checkout,
                        p.c.external_id,
                    )
                    .select_from(orders.join(p, p.c.order_id == orders.c.id))
                    .where(
                        orders.c.user_id == user_id,
                        orders.c.kind == "topup",
                        orders.c.status == "awaiting_payment",
                        orders.c.created_at > at - INVOICE_REUSE_AGE,
                        orders.c.parent_order_id.is_not_distinct_from(parent_id),
                        p.c.status == "pending",
                        p.c.instance_id == instance_id,
                        p.c.amount_minor == pay_minor,
                        p.c.currency == pay_currency,
                        p.c.checkout["kind"].astext.in_(_REUSABLE_KINDS),
                        sa.or_(p.c.expires_at.is_(None), p.c.expires_at > at + _REUSE_MARGIN),
                    )
                    .order_by(orders.c.id.desc())
                    .limit(1)
                    .with_for_update(of=orders)
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        checkout = _stored_checkout(row["checkout"], row["external_id"])
        if checkout is None:
            return None
        if ui_ref is not None:  # «💰 Зачислено…» goes to the message the user looks at now
            await conn.execute(
                sa.update(orders)
                .where(orders.c.id == row["id"])
                .values(ui_ref=ui_ref.as_json(), updated_at=at)
            )
        return TopupResult(
            int(row["id"]),
            str(row["payment_id"]),
            int(row["total_minor"]),
            pay_minor,
            pay_currency,
            checkout,
            parent_order_id=parent_id,
            reused=True,
        )

    # ----------------------------------------------------------------------------------------- sweeper

    async def sweep(self, at: datetime | None = None) -> int:
        """Expire waiting purchases past their window, delete stale drafts, expire week-old unpaid top-ups.

        A draft never held money (no ledger row, no payment, no child top-up — a top-up only links to a
        waiting purchase), so a stale one is deleted with its items instead of piling up as ``canceled``."""
        at = at or now()
        total = 0
        steps: tuple[tuple[sa.ColumnElement[bool], dict[str, Any] | None], ...] = (
            (
                sa.and_(orders.c.status == "awaiting_funds", orders.c.autocomplete_until < at),
                {"status": "expired"},
            ),
            (sa.and_(orders.c.status == "draft", orders.c.created_at < at - DRAFT_TTL), None),
            (
                sa.and_(
                    orders.c.kind == "topup",
                    orders.c.status == "awaiting_payment",
                    orders.c.created_at < at - TOPUP_TTL,
                ),
                {"status": "expired"},
            ),
        )
        for cond, values in steps:
            ids = sa.select(orders.c.id).where(cond).limit(_SWEEP_BATCH).with_for_update(skip_locked=True)
            async with self._db.tx() as conn:
                if values is None:
                    stmt: Any = sa.delete(orders).where(orders.c.id.in_(ids), cond)
                else:
                    stmt = sa.update(orders).where(orders.c.id.in_(ids)).values(**values, updated_at=at)
                result = await conn.execute(stmt)
            total += result.rowcount or 0
        return total
