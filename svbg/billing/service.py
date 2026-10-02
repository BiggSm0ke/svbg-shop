"""The billing facade: one object the app builds and wires (07 §4.5).

::

    billing = Billing(db, catalog=catalog, payments=payment_core, config=settings_snapshot,
                      messenger=user_messenger, attention=attention)
    billing.attach()                         # on_paid / on_refunded hooks of the payment core
    app.job_handlers.update(billing.handlers())
    scheduler.every("billing.sweep", 60, billing.sweep)

The user path calls :attr:`checkout` (``draft_plan`` → ``pay`` → on ``awaiting_funds`` the «Не хватает X»
screen with :meth:`topup_options` → ``start_topup``); everything after the money arrives is automatic.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from svbg.billing.checkout import BillingError, CatalogPort, CheckoutService
from svbg.billing.config import BillingConfig, ConfigSource
from svbg.billing.crediting import Crediting, SpendCheck, spend_refusal
from svbg.billing.fulfill import Fulfiller, OrderItemHandler
from svbg.billing.ports import AttentionPort, Messenger
from svbg.core.clock import now
from svbg.domain.wallet_rules import suggest_topup

if TYPE_CHECKING:
    from svbg.db.engine import Database
    from svbg.jobs.worker import Handler
    from svbg.payments.core import PaymentCore
    from svbg.subscriptions.lifecycle import SubscriptionLifecycle

__all__ = ["HELD_CHECK_EVERY", "Billing", "TopupOption"]

#: How often the sweeper looks for ``held`` purchases nobody decided (they wait a week anyway).
HELD_CHECK_EVERY: Final = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class TopupOption:
    """One «Пополнить на X» button of the shortfall screen (07 §4.5 step 2)."""

    instance_id: int
    method_kind: str | None
    credit_minor: int  # lands on the balance (≥ the shortfall: rounded up to the method's minimum)
    pay_amount_minor: int
    pay_currency: str
    surplus_minor: int  # what stays on the balance after the purchase (the screen says so in advance)


class Billing:
    def __init__(
        self,
        db: Database,
        *,
        catalog: CatalogPort,
        payments: PaymentCore | None,
        config: ConfigSource,
        lifecycle: SubscriptionLifecycle | None = None,
        messenger: Messenger | None = None,
        attention: AttentionPort | None = None,
        spend: SpendCheck | None = None,
        item_handlers: Mapping[str, OrderItemHandler] | None = None,
    ) -> None:
        self._db = db
        self._payments = payments
        self._config = config
        self.checkout = CheckoutService(db, catalog=catalog, payments=payments, config=config)
        self.crediting = Crediting(config=config, spend=spend)
        self.fulfiller = Fulfiller(
            db,
            config=config,
            lifecycle=lifecycle,
            messenger=messenger,
            attention=attention,
            item_handlers=item_handlers,
        )
        self._attached = False
        self._held_checked: datetime | None = None

    @property
    def config(self) -> BillingConfig:
        return BillingConfig.from_mapping(self._config())

    def attach(self, *, spend_guard: bool = True) -> None:
        """Register the crediting hooks with the payment core (once) and, with ``spend_guard``, the X5
        money gate: a frozen or banned user gets no invoice in any path (``insert_pending`` refuses first)."""
        if self._payments is None or self._attached:
            return
        self._payments.on_paid(self.crediting.on_paid)
        self._payments.on_refunded(self.crediting.on_refunded)
        if spend_guard:
            self._payments.spend_guard(spend_refusal)
        self._attached = True

    def handlers(self) -> dict[str, Handler]:
        return self.fulfiller.handlers()

    async def sweep(self) -> None:
        """Periodic task (every minute): expired windows, stale drafts, week-old unpaid top-ups, ``held``
        purchases nobody decided for a week (refunded to the wallet)."""
        await self.checkout.sweep()
        at = now()
        if self._held_checked is None or at - self._held_checked >= HELD_CHECK_EVERY:
            self._held_checked = at
            await self.fulfiller.expire_held(at)

    def topup_options(
        self, missing_minor: int, instances: Iterable[Any], *, staff: bool = False
    ) -> list[TopupOption]:
        """Default «Пополнить на X» per enabled payment instance for a shortfall: the shortfall rounded up to
        the method's minimum (Stars: whole ⭐ at ``PAY_STARS_RATE``); methods that cannot take it are left
        out. A test-mode instance is offered only to staff (``staff``: the viewer is the owner or an admin) —
        its money is not real."""
        cfg = self.config
        out: list[TopupOption] = []
        for inst in instances:
            if not getattr(inst, "enabled", False):
                continue
            if getattr(inst, "is_test", False) and not staff:
                continue
            min_credit = max(cfg.topup_min_minor, self._credit_of(inst, inst.min_minor, cfg))
            max_limit = self._credit_of(inst, inst.max_minor, cfg) if inst.max_minor is not None else None
            max_credit = cfg.topup_max_minor if max_limit is None else min(cfg.topup_max_minor, max_limit)
            amount = suggest_topup(missing_minor, min_minor=min_credit, max_minor=max_credit)
            if amount is None:
                continue
            try:
                credit, pay, currency = self.checkout.topup_quote(inst, amount)
            except BillingError:  # a method that cannot be quoted is simply not offered
                continue
            kinds = tuple(getattr(inst, "method_kinds", ()) or ())
            out.append(
                TopupOption(
                    instance_id=int(inst.id),
                    method_kind=kinds[0] if kinds else None,
                    credit_minor=credit,
                    pay_amount_minor=pay,
                    pay_currency=currency,
                    surplus_minor=max(0, credit - missing_minor),
                )
            )
        return out

    @staticmethod
    def _credit_of(inst: Any, limit_minor: int | None, cfg: BillingConfig) -> int:
        """A provider limit (in its own currency) expressed in the shop currency."""
        if limit_minor is None:
            return 0
        if inst.accepts(cfg.currency):
            return int(limit_minor)
        if inst.accepts("XTR") and cfg.stars_rate_minor is not None:
            return int(limit_minor) * cfg.stars_rate_minor
        return int(limit_minor)
