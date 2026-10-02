"""Payment core (07 §4.1–4.2, 04 D12, D14): the only place where a payment changes state.

Invariants (property tests in ``tests/payments``):

* **Exactly-once crediting.** A payment becomes ``paid`` by one compare-and-set
  ``UPDATE … WHERE status IN ('pending','expired','canceled','failed') AND amount_minor = :paid AND currency =
  :cur`` («поздняя оплата побеждает»); ``UNIQUE(instance_id, external_id)`` binds one provider invoice to one
  payment. The ``on_paid(conn, payment)`` hooks (billing: wallet top-up, auto-complete of the purchase) run in
  **the same transaction**: either both the status and the money change, or nothing does.
* **Amount reconciliation.** The provider's amount (``Decimal``, major units) is converted to minor units
  exactly — ``"179"`` equals ``"179.00"``, ``"179.001"`` RUB is not an amount — and compared with the invoice;
  any difference in amount or currency turns the payment into ``mismatch`` and raises an alert. Nothing is
  credited.
* **Webhook gate** (:meth:`PaymentCore.handle_webhook`, ≤ 100 ms, no provider HTTP): the instance token is
  compared in constant time; the plugin authenticates the raw bytes; ``event.signed_at`` must lie within
  ``Capabilities.replay_window_s`` (otherwise ``401`` + event logged with reason; > N rejections an hour →
  «проверьте NTP»); a test event on a live instance is rejected; accepted bodies are deduplicated by
  ``sha256(body)``; a *weak* scheme (``secret_header``, ``ip_only``, ``none``) never changes state by itself —
  the status is re-read with ``fetch_status`` in a job (``payments.verify``).
* **Spending guard.** ``can_spend`` hooks (freeze, X5) run before an invoice is created, in every path.
* **Manual confirmation** re-checks the actor's role in the database (``roles.load_actor``:
  ``payments.confirm``, a banned admin has no rights, a member of the admin group is not an admin of the bot),
  never lets an admin decide their own payment and writes ``admin_audit`` in the same transaction.
* **Test mode.** A live instance never changes a payment created in test mode (``payments.is_test``).

Provider calls (create, fetch) have timeouts and happen outside database transactions.
"""

from __future__ import annotations

import asyncio
import enum
import hashlib
import json
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final, Protocol

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from svbg.core import clock as core_clock
from svbg.core.bus import Event
from svbg.core.ids import uuid7
from svbg.core.log import mask
from svbg.core.money import exponent, format_money, to_decimal
from svbg.core.tables import admin_audit
from svbg.db.meta import UtcDateTime
from svbg.jobs.queue import enqueue
from svbg.jobs.worker import PermanentJobError, RetryJob
from svbg.payments.tables import (
    EVENT_DEDUP_PREDICATE,
    LATE_PAYABLE,
    payment_events,
    payments,
)
from svbg.sdk.payments import (
    Checkout,
    MethodKind,
    PaymentIntent,
    PaymentState,
    ProviderError,
    ProviderEvent,
    ProviderStatus,
    WebhookIgnored,
    WebhookRejected,
    WebhookRequest,
    WebhookResponse,
)
from svbg.services.roles import Actor, load_actor

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from svbg.core.attention import AttentionService
    from svbg.core.bus import EventBus
    from svbg.db.engine import Database
    from svbg.jobs.queue import Job
    from svbg.jobs.worker import JobContext
    from svbg.payments.registry import InstanceRegistry, LiveInstance

__all__ = [
    "VERIFY_JOB",
    "AdminPoster",
    "ApplyResult",
    "CheckoutError",
    "CheckoutResult",
    "OnPaid",
    "Outcome",
    "PaymentCore",
    "PaymentRecord",
    "PendingPayment",
    "PermissionDeniedError",
    "PresenceHook",
    "SpendDeniedError",
    "SpendGuard",
    "to_minor",
]

log = logging.getLogger("svbg.payments")

VERIFY_JOB: Final = "payments.verify"
PERM_CONFIRM: Final = "payments.confirm"
CREATE_TIMEOUT_S: Final = 15.0
FETCH_TIMEOUT_S: Final = 10.0
#: First reconciler check of an invoice with a public webhook URL (D14: safety checks at 10 / 30 / 120 min).
DOMAIN_FIRST_CHECK_S: Final = 600
#: First decay check without a domain: after the 3-minute presence phase, plus 30 s (D14).
NODOMAIN_FIRST_CHECK_S: Final = 210
#: Without a domain, a payment the poller saw expire or cancel is re-read once more this much later: no
#: webhook would bring a late ``expired → paid`` (the extra request fits the D14 budget of 24).
LATE_RECHECK_S: Final = 6 * 3600
#: Status sources that come from the provider itself (their ``paid_at`` is the provider's time).
_PROVIDER_SOURCES: Final = frozenset({"webhook", "poll", "fetch"})
_POLLED: Final = frozenset({"poll", "fetch"})
SUMMARY_MAX_CHARS: Final = 2000
EVENTS_TTL_DAYS: Final = 90
CLOCK_SKEW_ALERT_COUNT: Final = 5
CLOCK_SKEW_WINDOW_S: Final = 3600.0
_PURGE_BATCH: Final = 5000

# Owner- and user-facing texts (Russian).
_T: Final = {
    "no_instance": "Этот способ оплаты сейчас недоступен. Выберите другой.",
    "currency": "Этот способ оплаты не принимает {currency}.",
    "too_small": "Минимальная сумма для этого способа — {amount}.",
    "too_large": "Максимальная сумма для этого способа — {amount}.",
    "frozen": "Оплата сейчас недоступна.",
    "create_failed": "Не получилось создать счёт: {reason}. Попробуйте ещё раз или выберите другой способ.",
    "create_timeout": "касса не ответила вовремя",
    "no_rights": "Нет прав",
    "not_manual": "Этот платёж нельзя подтвердить вручную",
    "not_found": "Платёж не найден",
    "mismatch_title": "Сумма оплаты не совпала со счётом",
    "mismatch_body": (
        "Платёжка «{title}», платёж {pid}: счёт на {expected}, пришло {got}. "
        "Деньги не зачислены — проверьте оплату в кабинете кассы и решите вручную."
    ),
    "mismatch_admin": (
        "⚠️ <b>Сумма оплаты не совпала</b>\nСчёт: {expected}\nПришло: {got}\n"
        "Платёжка: {title}\nПлатёж: <code>{pid}</code>\nДеньги не зачислены."
    ),
    "refund_title": "Возврат или чарджбэк по оплате",
    "refund_body": (
        "Платёжка «{title}», платёж {pid} на {amount}: касса сообщила «{state}». Проверьте баланс."
    ),
    "refund_admin": (
        "↩️ <b>Возврат / чарджбэк</b>\nПлатёжка: {title}\nПлатёж: <code>{pid}</code>\nСумма: {amount}"
    ),
    "unknown_title": "Оплата по неизвестному счёту",
    "unknown_body": (
        "Платёжка «{title}» сообщила об оплате {amount} по счёту {ext}, которого нет в боте. "
        "Найдите плательщика в кабинете кассы."
    ),
    "skew_title": "Часы сервера расходятся — проверьте NTP",
    "skew_body": (
        "За последний час отклонено {n} вебхуков оплаты с устаревшей меткой времени. Скорее всего, часы "
        "сервера ушли: включите синхронизацию времени (NTP). Платежи не потеряются — касса повторит "
        "уведомления, а бот сам перепроверит счета."
    ),
    "amount_missing": "сумма не указана",
}
#: English of the texts the **user** sees (same keys and placeholders as in :data:`_T`).
_T_EN: Final = {
    "no_instance": "This payment method is not available right now. Please choose another one.",
    "currency": "This payment method does not accept {currency}.",
    "too_small": "The minimum amount for this method is {amount}.",
    "too_large": "The maximum amount for this method is {amount}.",
    "frozen": "Payment is not available right now.",
    "create_failed": "Could not create the invoice: {reason}. Please try again or choose another method.",
    "create_timeout": "the payment system did not respond in time",
    "create_error": "payment system error",
}


class Outcome(enum.StrEnum):
    """What happened to a status report (mirrors ``payment_events.outcome``)."""

    APPLIED = "applied"
    DUPLICATE = "duplicate"
    IGNORED = "ignored"
    VERIFY_QUEUED = "verify_queued"
    MISMATCH = "mismatch"
    UNKNOWN_PAYMENT = "unknown_payment"
    STALE = "stale"
    TEST_REJECTED = "test_rejected"
    BAD_SIGNATURE = "bad_signature"
    MALFORMED = "malformed"


@dataclass(frozen=True, slots=True)
class PaymentRecord:
    """A ``payments`` row as seen by hooks."""

    id: str
    instance_id: int
    user_id: int
    order_id: int | None
    status: str
    amount_minor: int
    currency: str
    paid_amount_minor: int | None
    external_id: str | None
    is_test: bool
    method_kind: str | None
    created_at: datetime
    paid_at: datetime | None
    confirmed_by: int | None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> PaymentRecord:
        return cls(
            id=row["id"],
            instance_id=row["instance_id"],
            user_id=row["user_id"],
            order_id=row["order_id"],
            status=row["status"],
            amount_minor=row["amount_minor"],
            currency=row["currency"],
            paid_amount_minor=row["paid_amount_minor"],
            external_id=row["external_id"],
            is_test=row["is_test"],
            method_kind=row["method_kind"],
            created_at=row["created_at"],
            paid_at=row["paid_at"],
            confirmed_by=row["confirmed_by"],
            metadata=dict(row["metadata"] or {}),
        )


@dataclass(frozen=True, slots=True)
class PendingPayment:
    """An inserted ``pending`` payment that has no invoice yet (see :meth:`PaymentCore.open_checkout`)."""

    id: str
    instance_id: int
    user_id: int
    order_id: int | None
    amount_minor: int
    currency: str
    description: str
    method_kind: str | None
    is_test: bool


@dataclass(frozen=True, slots=True)
class CheckoutResult:
    payment_id: str
    instance_id: int
    checkout: Checkout


@dataclass(frozen=True, slots=True)
class ApplyResult:
    outcome: Outcome
    payment: PaymentRecord | None = None
    credited: bool = False  # this call moved the payment to ``paid`` (on_paid ran)
    refunded: bool = False  # this call moved the payment to ``refunded``
    reason: str | None = None


class CheckoutError(Exception):
    """An invoice could not be created; ``human`` is shown to the user (Russian), ``human_en`` — in English
    (``None``: no translation, see :meth:`localized`)."""

    def __init__(self, human: str, *, retryable: bool = False, human_en: str | None = None) -> None:
        super().__init__(human)
        self.human = human
        self.human_en = human_en
        self.retryable = retryable

    def localized(self, lang: str | None) -> str:
        """``human`` in ``lang`` (Russian fallback)."""
        return (self.human_en or self.human) if lang == "en" else self.human


class SpendDeniedError(CheckoutError):
    """A spending guard (freeze, X5) refused the payment before any money moved."""


class PermissionDeniedError(Exception):
    """The actor may not perform this money action («Нет прав»)."""

    def __init__(self, human: str = _T["no_rights"]) -> None:
        super().__init__(human)
        self.human = human


#: Billing's hook: credit the payment (same transaction as the status change).
OnPaid = Callable[["AsyncConnection", PaymentRecord], Awaitable[None]]
#: Spending guard: ``None`` = allowed, otherwise the reason shown to the user.
SpendGuard = Callable[["AsyncConnection", int], Awaitable[str | None]]


class AdminPoster(Protocol):
    """``AdminChatService.post`` subset used for payment alerts (topic ``payments``)."""

    async def post(self, kind: str, text: str, *, html: bool = False) -> Any: ...


class PresenceHook(Protocol):
    """The poller's side of the core: start the frequent phase, forget settled payments."""

    def touch(
        self, payment_id: str, user_id: int, instance_id: int, external_id: str | None = None
    ) -> None: ...

    def forget(self, payment_id: str) -> None: ...


def to_minor(amount: Decimal, currency: str) -> int | None:
    """Exact minor units of a provider amount, or ``None`` when it is not representable (fractional minor
    units, unknown currency, negative)."""
    try:
        exp = exponent(currency)
    except ValueError:
        return None
    if not amount.is_finite() or amount < 0:
        return None
    scaled = amount.scaleb(exp)
    if scaled != scaled.to_integral_value():
        return None
    return int(scaled)


def _currency(code: str | None) -> str:
    """A provider currency code as the core stores it (``" rub"`` → ``"RUB"``)."""
    return (code or "").strip().upper()


def _money(amount_minor: int | None, currency: str | None, lang: str = "ru") -> str:
    if amount_minor is None or currency is None:
        return "—"
    try:
        return format_money(amount_minor, currency, lang)
    except ValueError:
        return f"{amount_minor} {currency}"


def _summary(event: ProviderEvent | None, body: bytes) -> dict[str, Any]:
    """A short, masked copy for ``payment_events.summary``."""
    if event is not None and event.summary:
        data: dict[str, Any] = {k: v for k, v in event.summary.items() if isinstance(k, str)}
        text = mask(json.dumps(data, ensure_ascii=False, default=str))
        if len(text) <= SUMMARY_MAX_CHARS:
            return json.loads(text)
        return {"body": text[:SUMMARY_MAX_CHARS]}
    return {"body": mask(body[: SUMMARY_MAX_CHARS * 2].decode("utf-8", errors="replace"))[:SUMMARY_MAX_CHARS]}


class PaymentCore:
    """See module docstring. One instance per process; safe for concurrent use."""

    def __init__(
        self,
        db: Database,
        instances: InstanceRegistry,
        *,
        attention: AttentionService | None = None,
        admin_chat: AdminPoster | None = None,
        bus: EventBus | None = None,
        has_domain: Callable[[], bool] = lambda: False,
        return_url: Callable[[], str | None] = lambda: None,
        create_timeout: float = CREATE_TIMEOUT_S,
        fetch_timeout: float = FETCH_TIMEOUT_S,
        clock_skew_alert_count: Callable[[], int] | int = CLOCK_SKEW_ALERT_COUNT,
        clock_skew_window_s: float = CLOCK_SKEW_WINDOW_S,
        clock: Callable[[], datetime] = core_clock.now,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._db = db
        self.instances = instances
        self._attention = attention
        self._admin_chat = admin_chat
        self._bus = bus
        self._has_domain = has_domain
        self._return_url = return_url
        self._create_timeout = create_timeout
        self._fetch_timeout = fetch_timeout
        self._skew_count = clock_skew_alert_count
        self._skew_window = clock_skew_window_s
        self._clock = clock
        self._monotonic = monotonic
        self._on_paid: list[OnPaid] = []
        self._on_refunded: list[OnPaid] = []
        self._guards: list[SpendGuard] = []
        self._stale: deque[float] = deque()
        self._skew_alerted_at: float | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self.presence: PresenceHook | None = None

    @property
    def fetch_timeout(self) -> float:
        return self._fetch_timeout

    # ------------------------------------------------------------------------------------------ hooks

    def on_paid(self, fn: OnPaid) -> OnPaid:
        """Register a crediting hook (billing). Runs inside the payment's transaction; an exception rolls
        the whole change back (the provider retries, the reconciler re-checks)."""
        self._on_paid.append(fn)
        return fn

    def on_refunded(self, fn: OnPaid) -> OnPaid:
        """Register a hook for ``paid → refunded`` (chargebacks, refunds); same transaction."""
        self._on_refunded.append(fn)
        return fn

    def spend_guard(self, fn: SpendGuard) -> SpendGuard:
        """Register a ``can_spend`` check (X5 freeze): runs before an invoice is created."""
        self._guards.append(fn)
        return fn

    async def can_spend(self, conn: AsyncConnection, user_id: int) -> str | None:
        """``None`` if every guard allows the user to pay, else the first refusal text."""
        for guard in self._guards:
            reason = await guard(conn, user_id)
            if reason:
                return reason
        return None

    # ------------------------------------------------------------------------------------- creation

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
    ) -> PendingPayment:
        """Insert a ``pending`` payment inside the caller's transaction (billing creates the top-up order in
        the same one). Checks the instance, the amount limits and the spending guards; raises
        :class:`CheckoutError` / :class:`SpendDeniedError`. One SQL statement plus the guards' own."""
        inst = self.instances.get(instance_id)
        if inst is None or not inst.enabled:
            raise CheckoutError(_T["no_instance"], human_en=_T_EN["no_instance"])
        cur = currency.upper()
        if isinstance(amount_minor, bool) or not isinstance(amount_minor, int) or amount_minor <= 0:
            raise ValueError("amount_minor must be a positive int")
        if not inst.accepts(cur):
            raise CheckoutError(
                _T["currency"].format(currency=cur), human_en=_T_EN["currency"].format(currency=cur)
            )
        if inst.min_minor is not None and amount_minor < inst.min_minor:
            raise CheckoutError(
                _T["too_small"].format(amount=_money(inst.min_minor, cur)),
                human_en=_T_EN["too_small"].format(amount=_money(inst.min_minor, cur, "en")),
            )
        if inst.max_minor is not None and amount_minor > inst.max_minor:
            raise CheckoutError(
                _T["too_large"].format(amount=_money(inst.max_minor, cur)),
                human_en=_T_EN["too_large"].format(amount=_money(inst.max_minor, cur, "en")),
            )
        refusal = await self.can_spend(conn, user_id)
        if refusal:
            raise SpendDeniedError(refusal)
        payment_id = uuid7()
        kind = method_kind or (inst.method_kinds[0] if inst.method_kinds else None)
        await conn.execute(
            sa.insert(payments).values(
                id=payment_id,
                instance_id=inst.id,
                user_id=user_id,
                order_id=order_id,
                amount_minor=amount_minor,
                currency=cur,
                method_kind=kind,
                description=description[:500],
                is_test=inst.is_test,
                metadata=dict(metadata or {}),
            )
        )
        return PendingPayment(
            id=payment_id,
            instance_id=inst.id,
            user_id=user_id,
            order_id=order_id,
            amount_minor=amount_minor,
            currency=cur,
            description=description[:500],
            method_kind=kind,
            is_test=inst.is_test,
        )

    async def open_checkout(self, pending: PendingPayment) -> CheckoutResult:
        """Create the provider invoice for a committed pending payment (HTTP with a timeout, outside any
        transaction), store it (1 SQL) and schedule its checks. On failure the payment becomes ``failed``
        and :class:`CheckoutError` is raised (a late payment of such an invoice still wins)."""
        inst = self.instances.get(pending.instance_id)
        if inst is None:
            await self._fail(pending.id, _T["no_instance"])
            raise CheckoutError(_T["no_instance"], human_en=_T_EN["no_instance"])
        hint = pending.method_kind if pending.method_kind in MethodKind._value2member_map_ else None
        intent = PaymentIntent(
            payment_id=pending.id,
            amount_minor=pending.amount_minor,
            currency=pending.currency,
            description=pending.description,
            customer_ref=inst.customer_ref(pending.user_id),
            return_url=self._return_url(),
            method_hint=MethodKind(hint) if hint else None,
            is_test=inst.is_test,
        )
        try:
            async with asyncio.timeout(self._create_timeout):
                checkout = await inst.provider.create(intent)
            if not isinstance(checkout, Checkout):
                raise ProviderError("плагин вернул неверный ответ")  # noqa: TRY301
        except TimeoutError:
            await self._fail(pending.id, _T["create_timeout"])
            raise CheckoutError(
                _T["create_failed"].format(reason=_T["create_timeout"]),
                retryable=True,
                human_en=_T_EN["create_failed"].format(reason=_T_EN["create_timeout"]),
            ) from None
        except ProviderError as exc:
            await self._fail(pending.id, exc.human)
            raise CheckoutError(
                _T["create_failed"].format(reason=exc.human),
                retryable=exc.retryable,
                human_en=_T_EN["create_failed"].format(reason=_T_EN["create_error"]),
            ) from None
        except Exception as exc:
            log.exception("payments: %s create() failed", inst.slug)
            await self._fail(pending.id, type(exc).__name__)
            raise CheckoutError(
                _T["create_failed"].format(reason="ошибка платёжки"),
                human_en=_T_EN["create_failed"].format(reason=_T_EN["create_error"]),
            ) from None
        plan, first = self.poll_plan(inst, checkout.kind)
        now = self._clock()
        try:
            async with self._db.tx() as conn:
                await conn.execute(
                    sa.update(payments)
                    .where(payments.c.id == pending.id)
                    .values(
                        external_id=sa.func.coalesce(payments.c.external_id, checkout.external_id),
                        checkout=checkout.as_json(),
                        expires_at=checkout.expires_at,
                        poll_plan=plan,
                        next_check_at=(
                            None
                            if first is None
                            else sa.case(
                                (
                                    payments.c.status == "pending",
                                    sa.literal(now + timedelta(seconds=first), UtcDateTime),
                                ),
                                else_=sa.null(),
                            )
                        ),
                        updated_at=sa.func.now(),
                    )
                )
        except IntegrityError:
            log.exception("payments: %s returned an external id that belongs to another payment", inst.slug)
            await self._fail(pending.id, "duplicate external id")
            raise CheckoutError(
                _T["create_failed"].format(reason="ошибка платёжки"),
                human_en=_T_EN["create_failed"].format(reason=_T_EN["create_error"]),
            ) from None
        if plan == "nodomain" and self.presence is not None:
            self.presence.touch(pending.id, pending.user_id, inst.id, checkout.external_id)
        return CheckoutResult(payment_id=pending.id, instance_id=inst.id, checkout=checkout)

    async def create_payment(
        self,
        *,
        user_id: int,
        instance_id: int,
        amount_minor: int,
        currency: str,
        description: str,
        order_id: int | None = None,
        method_kind: str | None = None,
    ) -> CheckoutResult:
        """Convenience: :meth:`insert_pending` in its own transaction, then :meth:`open_checkout`."""
        async with self._db.tx() as conn:
            pending = await self.insert_pending(
                conn,
                user_id=user_id,
                instance_id=instance_id,
                amount_minor=amount_minor,
                currency=currency,
                description=description,
                order_id=order_id,
                method_kind=method_kind,
            )
        return await self.open_checkout(pending)

    def poll_plan(self, inst: LiveInstance, checkout_kind: str) -> tuple[str | None, int | None]:
        """Reconciler plan of a new invoice and the delay of its first check, seconds (D14)."""
        if not inst.caps.fetch_status or checkout_kind == "details":
            return None, None
        if inst.caps.webhook and self._has_domain():
            return "domain", DOMAIN_FIRST_CHECK_S
        return "nodomain", NODOMAIN_FIRST_CHECK_S

    async def _fail(self, payment_id: str, reason: str) -> None:
        try:
            async with self._db.tx() as conn:
                await conn.execute(
                    sa.update(payments)
                    .where(payments.c.id == payment_id, payments.c.status == "pending")
                    .values(
                        status="failed",
                        error=mask(reason)[:300],
                        next_check_at=None,
                        updated_at=sa.func.now(),
                    )
                )
        except Exception:
            log.exception("payments: could not mark %s failed", payment_id)

    # ------------------------------------------------------------------------------------- webhooks

    async def handle_webhook(self, instance_id: int, token: str, req: WebhookRequest) -> WebhookResponse:
        """The whole webhook pipeline (see module docstring). Never raises; database errors → ``503`` so
        the provider retries."""
        inst = self.instances.get(instance_id)
        if inst is None or not inst.token_matches(token) or not inst.caps.webhook:
            return WebhookResponse(404, b"not found")
        digest = hashlib.sha256(req.body).hexdigest()
        try:
            event = await inst.provider.parse_webhook(req)
            if not isinstance(event, ProviderEvent):
                raise WebhookRejected("plugin returned no event", status=500)  # noqa: TRY301
        except WebhookIgnored as ignored:
            return ignored.response
        except WebhookRejected as rejected:
            outcome = Outcome.MALFORMED if rejected.status == 400 else Outcome.BAD_SIGNATURE
            await self._record_rejected(inst, digest, outcome, rejected.reason[:200], None, req.body)
            return WebhookResponse(rejected.status, b"rejected")
        except Exception:
            log.exception("payments: %s parse_webhook crashed", inst.slug)
            return WebhookResponse(500, b"error")
        now = self._clock()
        window = inst.caps.replay_window_s
        if window is not None and (
            event.signed_at is None or abs((now - event.signed_at).total_seconds()) > window
        ):
            await self._record_rejected(inst, digest, Outcome.STALE, "outside replay window", event, req.body)
            self._note_stale()
            return WebhookResponse(401, b"stale")
        if event.is_test and not inst.is_test:
            await self._record_rejected(inst, digest, Outcome.TEST_REJECTED, "test event", event, req.body)
            log.warning("payments: %s test-mode webhook on a live instance rejected", inst.slug)
            return WebhookResponse(400, b"test mode")
        try:
            result = await self._accept(inst, digest, event, req.body)
        except Exception:
            log.exception("payments: %s webhook could not be processed", inst.slug)
            return WebhookResponse(503, b"unavailable")
        self._after(inst, result, event)
        try:
            return inst.provider.ack(event)
        except Exception:
            log.exception("payments: %s ack() crashed", inst.slug)
            return WebhookResponse.ok()

    async def _accept(
        self, inst: LiveInstance, digest: str, event: ProviderEvent, body: bytes
    ) -> ApplyResult:
        async with self._db.tx() as conn:
            event_id = (
                await conn.execute(
                    pg_insert(payment_events)
                    .values(
                        instance_id=inst.id,
                        payment_id=event.payment_id,
                        external_id=event.external_id,
                        body_sha256=digest,
                        status=event.state.value,
                        outcome=Outcome.APPLIED.value,
                        accepted=True,
                        signed_at=event.signed_at,
                        summary=_summary(event, body),
                    )
                    .on_conflict_do_nothing(
                        index_elements=[payment_events.c.instance_id, payment_events.c.body_sha256],
                        index_where=sa.text(EVENT_DEDUP_PREDICATE),
                    )
                    .returning(payment_events.c.id)
                )
            ).scalar()
            if event_id is None:
                return ApplyResult(Outcome.DUPLICATE)
            if inst.caps.webhook_auth.is_weak:
                await self._queue_verify(conn, inst, event)
                result = ApplyResult(Outcome.VERIFY_QUEUED)
            else:
                result = await self._apply(conn, inst, event, source="webhook")
            if result.outcome is not Outcome.APPLIED or (result.payment and not event.payment_id):
                await conn.execute(
                    sa.update(payment_events)
                    .where(payment_events.c.id == event_id)
                    .values(
                        outcome=result.outcome.value,
                        reason=result.reason,
                        payment_id=result.payment.id if result.payment else event.payment_id,
                    )
                )
        return result

    async def _queue_verify(self, conn: AsyncConnection, inst: LiveInstance, status: ProviderStatus) -> None:
        ref = status.external_id or status.payment_id
        await enqueue(
            conn,
            VERIFY_JOB,
            {"instance_id": inst.id, "external_id": status.external_id, "payment_id": status.payment_id},
            lane="interactive",
            dedup_key=f"{VERIFY_JOB}:{inst.id}:{ref}",
            max_attempts=8,
        )

    async def _record_rejected(  # noqa: PLR0917 - internal helper, all arguments are required
        self,
        inst: LiveInstance,
        digest: str,
        outcome: Outcome,
        reason: str,
        event: ProviderEvent | None,
        body: bytes,
    ) -> None:
        try:
            async with self._db.tx() as conn:
                await conn.execute(
                    sa.insert(payment_events).values(
                        instance_id=inst.id,
                        payment_id=event.payment_id if event else None,
                        external_id=event.external_id if event else None,
                        body_sha256=digest,
                        status=event.state.value if event else None,
                        outcome=outcome.value,
                        accepted=False,
                        reason=reason,
                        signed_at=event.signed_at if event else None,
                        # An unauthenticated body is not stored: it may be anything.
                        summary=_summary(event, body) if event is not None else {},
                    )
                )
        except Exception:
            log.exception("payments: rejected webhook of %s could not be logged", inst.slug)

    def _note_stale(self) -> None:
        now = self._monotonic()
        window = self._skew_window
        self._stale.append(now)
        while self._stale and now - self._stale[0] > window:
            self._stale.popleft()
        limit = self._skew_count() if callable(self._skew_count) else self._skew_count
        if len(self._stale) <= limit:
            return
        if self._skew_alerted_at is not None and now - self._skew_alerted_at < window:
            return
        self._skew_alerted_at = now
        if self._attention is not None:
            self._spawn(
                self._attention.raise_item(
                    "payments:clock_skew",
                    "warn",
                    _T["skew_title"],
                    _T["skew_body"].format(n=len(self._stale)),
                )
            )

    # ------------------------------------------------------------------------------ state machine

    def _key(self, inst: LiveInstance, status: ProviderStatus) -> sa.ColumnElement[bool]:
        if status.payment_id is not None:
            cond = sa.and_(payments.c.id == status.payment_id, payments.c.instance_id == inst.id)
            if status.external_id is not None:
                cond = sa.and_(
                    cond,
                    sa.or_(payments.c.external_id.is_(None), payments.c.external_id == status.external_id),
                )
            return cond
        return sa.and_(payments.c.instance_id == inst.id, payments.c.external_id == status.external_id)

    @staticmethod
    def _mode(inst: LiveInstance) -> sa.ColumnElement[bool]:
        """A live instance never changes a payment created in test mode (the owner switched test mode off:
        a test invoice must not credit a real balance)."""
        return sa.true() if inst.is_test else payments.c.is_test.is_(False)

    async def _queue_late_check(
        self, conn: AsyncConnection, inst: LiveInstance, row: Mapping[str, Any]
    ) -> None:
        """Without a domain no webhook brings a late ``expired → paid``: re-read the status once, later."""
        await enqueue(
            conn,
            VERIFY_JOB,
            {"instance_id": inst.id, "external_id": row["external_id"], "payment_id": row["id"]},
            lane="background",
            run_at=self._clock() + timedelta(seconds=LATE_RECHECK_S),
            dedup_key=f"{VERIFY_JOB}:late:{row['id']}",
            max_attempts=8,
        )

    async def _apply(
        self,
        conn: AsyncConnection,
        inst: LiveInstance,
        status: ProviderStatus,
        *,
        source: str,
        confirmed_by: int | None = None,
    ) -> ApplyResult:
        """Apply one authentic status report inside ``conn`` (see module docstring)."""
        if status.is_test and not inst.is_test:
            return ApplyResult(Outcome.TEST_REJECTED, reason="test status on a live instance")
        state = status.state
        if state in (PaymentState.CREATED, PaymentState.PROCESSING):
            return ApplyResult(Outcome.IGNORED, reason=state.value)
        if state is PaymentState.PAID:
            return await self._apply_paid(conn, inst, status, source=source, confirmed_by=confirmed_by)
        if state in (PaymentState.EXPIRED, PaymentState.CANCELED, PaymentState.FAILED):
            row = (
                (
                    await conn.execute(
                        sa.update(payments)
                        .where(self._key(inst, status), self._mode(inst), payments.c.status == "pending")
                        .values(status=state.value, next_check_at=None, updated_at=sa.func.now())
                        .returning(*payments.c)
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return ApplyResult(Outcome.IGNORED, reason=f"{state.value}: not pending")
            if source in _POLLED and row["poll_plan"] == "nodomain" and row["external_id"]:
                await self._queue_late_check(conn, inst, row)
            return ApplyResult(Outcome.APPLIED, PaymentRecord.from_row(row))
        # CHARGEBACK / REFUNDED: only a paid payment can be taken back.
        row = (
            (
                await conn.execute(
                    sa.update(payments)
                    .where(self._key(inst, status), self._mode(inst), payments.c.status == "paid")
                    .values(
                        status="refunded", next_check_at=None, error=state.value, updated_at=sa.func.now()
                    )
                    .returning(*payments.c)
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return ApplyResult(Outcome.IGNORED, reason=f"{state.value}: not paid")
        record = PaymentRecord.from_row(row)
        for hook in self._on_refunded:
            await hook(conn, record)
        return ApplyResult(Outcome.APPLIED, record, refunded=True, reason=state.value)

    async def _apply_paid(
        self,
        conn: AsyncConnection,
        inst: LiveInstance,
        status: ProviderStatus,
        *,
        source: str,
        confirmed_by: int | None,
    ) -> ApplyResult:
        cur = _currency(status.currency)  # the core reconciles by itself, whatever the plugin did
        if status.amount is None or not cur:
            if inst.caps.fetch_status and source == "webhook":
                await self._queue_verify(conn, inst, status)
                return ApplyResult(Outcome.VERIFY_QUEUED, reason=_T["amount_missing"])
            return await self._mismatch(conn, inst, status, None, _T["amount_missing"])
        paid_minor = to_minor(status.amount, cur)
        if paid_minor is None:
            return await self._mismatch(conn, inst, status, None, f"сумма {status.amount} {cur}")
        if not self._on_paid:
            raise RuntimeError("payments: no on_paid hook registered — refusing to mark payments paid")
        now = self._clock()
        paid_at: Any = now
        if status.paid_at is not None and source in _PROVIDER_SOURCES:
            # The provider's own time of payment (sanity-checked by the plugin): the auto-complete window is
            # judged by it, not by when a delayed check or a retried webhook got here. Never in the future,
            # never before the invoice.
            reported = min(now, status.paid_at)
            paid_at = sa.func.greatest(payments.c.created_at, sa.literal(reported, UtcDateTime))
        values: dict[str, Any] = {
            "status": "paid",
            "paid_amount_minor": paid_minor,
            "paid_currency": cur,
            "paid_at": paid_at,
            "next_check_at": None,
            "error": None,
            "updated_at": sa.func.now(),
        }
        if status.external_id is not None:
            values["external_id"] = sa.func.coalesce(payments.c.external_id, status.external_id)
        if confirmed_by is not None:
            values["confirmed_by"] = confirmed_by
        row = (
            (
                await conn.execute(
                    sa.update(payments)
                    .where(
                        self._key(inst, status),
                        self._mode(inst),
                        payments.c.status.in_(LATE_PAYABLE),
                        payments.c.amount_minor == paid_minor,
                        payments.c.currency == cur,
                    )
                    .values(**values)
                    .returning(*payments.c)
                )
            )
            .mappings()
            .first()
        )
        if row is not None:
            record = PaymentRecord.from_row(row)
            for hook in self._on_paid:
                await hook(conn, record)
            return ApplyResult(Outcome.APPLIED, record, credited=True)
        current = (
            (await conn.execute(sa.select(payments).where(self._key(inst, status)).with_for_update()))
            .mappings()
            .first()
        )
        if current is None:
            return ApplyResult(Outcome.UNKNOWN_PAYMENT, reason="no such payment")
        record = PaymentRecord.from_row(current)
        if current["is_test"] and not inst.is_test:
            log.warning("payments: %s reported a test-mode payment as paid on a live instance", inst.slug)
            return ApplyResult(Outcome.TEST_REJECTED, record, reason="test payment on a live instance")
        if current["status"] in LATE_PAYABLE:  # amount or currency differ
            got = _money(paid_minor, cur)
            return await self._mismatch(conn, inst, status, paid_minor, f"пришло {got}")
        return ApplyResult(Outcome.IGNORED, record, reason=f"already {current['status']}")

    async def _mismatch(
        self,
        conn: AsyncConnection,
        inst: LiveInstance,
        status: ProviderStatus,
        paid_minor: int | None,
        reason: str,
    ) -> ApplyResult:
        row = (
            (
                await conn.execute(
                    sa.update(payments)
                    .where(self._key(inst, status), self._mode(inst), payments.c.status.in_(LATE_PAYABLE))
                    .values(
                        status="mismatch",
                        paid_amount_minor=paid_minor,
                        paid_currency=_currency(status.currency) or None,
                        error=reason[:300],
                        next_check_at=None,
                        updated_at=sa.func.now(),
                    )
                    .returning(*payments.c)
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            exists = (await conn.execute(sa.select(payments.c.id).where(self._key(inst, status)))).first()
            if exists is None:
                return ApplyResult(Outcome.UNKNOWN_PAYMENT, reason="no such payment")
            return ApplyResult(Outcome.IGNORED, reason="settled before the mismatch")
        return ApplyResult(Outcome.MISMATCH, PaymentRecord.from_row(row), reason=reason)

    # ---------------------------------------------------------------------------- after commit

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("payments: background notification failed", exc_info=task.exception())

    def _after(self, inst: LiveInstance, result: ApplyResult, status: ProviderStatus | None = None) -> None:
        """Side effects after commit (never inside the transaction, never blocking the webhook)."""
        pay = result.payment
        if pay is not None and pay.status != "pending" and self.presence is not None:
            self.presence.forget(pay.id)
        if result.credited and pay is not None and self._bus is not None:
            self._bus.publish_nowait(
                Event(
                    "payment.paid",
                    {
                        "payment_id": pay.id,
                        "user_id": pay.user_id,
                        "order_id": pay.order_id,
                        "instance_id": pay.instance_id,
                        "amount_minor": pay.paid_amount_minor,
                        "currency": pay.currency,
                    },
                )
            )
        if result.outcome is Outcome.MISMATCH and pay is not None:
            got = _money(pay.paid_amount_minor, status.currency if status else pay.currency)
            fmt = {"title": inst.title, "pid": pay.id, "expected": _money(pay.amount_minor, pay.currency)}
            self._alert(
                f"payments:mismatch:{pay.id}",
                "error",
                _T["mismatch_title"],
                _T["mismatch_body"].format(**fmt, got=got),
                _T["mismatch_admin"].format(**fmt, got=got),
            )
        elif result.refunded and pay is not None:
            fmt = {"title": inst.title, "pid": pay.id, "amount": _money(pay.paid_amount_minor, pay.currency)}
            self._alert(
                f"payments:refund:{pay.id}",
                "error",
                _T["refund_title"],
                _T["refund_body"].format(**fmt, state=result.reason or "refunded"),
                _T["refund_admin"].format(**fmt),
            )
        elif (
            result.outcome is Outcome.UNKNOWN_PAYMENT
            and status is not None
            and status.state is PaymentState.PAID
        ):
            ref = status.external_id or status.payment_id or "?"
            amount = f"{status.amount} {status.currency}" if status.amount is not None else "—"
            self._alert(
                f"payments:unknown:{inst.id}:{ref}"[:200],
                "warn",
                _T["unknown_title"],
                _T["unknown_body"].format(title=inst.title, amount=amount, ext=ref),
                None,
            )

    def _alert(self, key: str, severity: str, title: str, body: str, admin_html: str | None) -> None:
        if self._attention is not None:
            self._spawn(self._attention.raise_item(key, severity, title, body))  # type: ignore[arg-type]
        if self._admin_chat is not None and admin_html is not None:
            self._spawn(self._admin_chat.post("payments", admin_html, html=True))

    async def drain(self, timeout: float = 5.0) -> None:  # noqa: ASYNC109 - graceful deadline
        """Wait for background notifications (shutdown, tests)."""
        pending = list(self._tasks)
        if pending:
            await asyncio.wait(pending, timeout=timeout)

    # ------------------------------------------------------------------------------- verification

    async def verify(
        self, instance_id: int, refs: Sequence[tuple[str | None, str | None]]
    ) -> list[ApplyResult]:
        """Re-read statuses from the provider (``fetch_status``) and apply them. ``refs`` are
        ``(external_id, payment_id)`` pairs; a pair without an external id is resolved from the database.
        Raises :class:`ProviderError` / ``TimeoutError`` when the provider cannot answer."""
        inst = self.instances.get(instance_id)
        if inst is None or not inst.caps.fetch_status:
            return []
        external: list[str] = [e for e, _ in refs if e]
        missing = [p for e, p in refs if not e and p]
        if missing:
            async with self._db.read() as conn:
                rows = await conn.execute(
                    sa.select(payments.c.external_id).where(
                        payments.c.id.in_(missing),
                        payments.c.instance_id == inst.id,
                        payments.c.external_id.is_not(None),
                    )
                )
                external.extend(r[0] for r in rows)
        external = list(dict.fromkeys(external))
        if not external:
            return []
        async with asyncio.timeout(self._fetch_timeout):
            statuses = await inst.provider.fetch_status(external)
        return await self.apply_statuses(inst, statuses, source="fetch")

    async def apply_statuses(
        self, inst: LiveInstance, statuses: Sequence[ProviderStatus], *, source: str
    ) -> list[ApplyResult]:
        """Apply trusted statuses, one transaction each (one failing hook does not block the others)."""
        results: list[ApplyResult] = []
        for status in statuses:
            if not isinstance(status, ProviderStatus):
                continue
            if status.state in (PaymentState.CREATED, PaymentState.PROCESSING):
                results.append(ApplyResult(Outcome.IGNORED, reason=status.state.value))
                continue
            async with self._db.tx() as conn:
                result = await self._apply(conn, inst, status, source=source)
            self._after(inst, result, status)
            results.append(result)
        return results

    async def verify_job(self, job: Job, ctx: JobContext) -> None:
        """Handler of ``payments.verify`` (weak webhooks, missing amounts)."""
        payload = job.payload
        try:
            instance_id = int(payload["instance_id"])
        except (KeyError, TypeError, ValueError):
            raise PermanentJobError("bad payload") from None
        inst = self.instances.get(instance_id)
        if inst is None:
            raise PermanentJobError(f"payment instance {instance_id} is not loaded")
        ref = (payload.get("external_id"), payload.get("payment_id"))
        try:
            await self.verify(instance_id, [ref])
        except ProviderError as exc:
            if not exc.retryable:
                raise PermanentJobError(exc.human) from None
            raise RetryJob(30.0, exc.human) from None
        except TimeoutError:
            raise RetryJob(30.0, "provider timeout") from None

    # ------------------------------------------------------------------------- trusted sources

    async def credit_external(
        self,
        instance_id: int,
        *,
        user_id: int,
        external_id: str,
        amount_minor: int,
        currency: str,
        payment_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ApplyResult:
        """A payment confirmed by a trusted channel (Telegram ``successful_payment``): with ``payment_id`` of
        our invoice it goes through the regular CAS; without one (legacy payload, 06 §2.4.4) a ``paid``
        payment is recorded directly, idempotent by ``UNIQUE(instance_id, external_id)``."""
        inst = self.instances.get(instance_id)
        if inst is None:
            raise CheckoutError(_T["no_instance"])
        cur = currency.upper()
        if payment_id is not None:
            status = ProviderStatus(
                state=PaymentState.PAID,
                payment_id=payment_id,
                external_id=external_id,
                amount=to_decimal(amount_minor, cur),
                currency=cur,
            )
            async with self._db.tx() as conn:
                result = await self._apply(conn, inst, status, source="trusted")
            self._after(inst, result, status)
            return result
        if not self._on_paid:
            raise RuntimeError("payments: no on_paid hook registered — refusing to mark payments paid")
        async with self._db.tx() as conn:
            row = (
                (
                    await conn.execute(
                        pg_insert(payments)
                        .values(
                            id=uuid7(),
                            instance_id=inst.id,
                            user_id=user_id,
                            external_id=external_id,
                            status="paid",
                            amount_minor=amount_minor,
                            currency=cur,
                            paid_amount_minor=amount_minor,
                            paid_currency=cur,
                            paid_at=self._clock(),
                            method_kind=inst.method_kinds[0] if inst.method_kinds else None,
                            is_test=inst.is_test,
                            metadata=dict(metadata or {}),
                        )
                        .on_conflict_do_nothing(
                            index_elements=[payments.c.instance_id, payments.c.external_id]
                        )
                        .returning(*payments.c)
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return ApplyResult(Outcome.DUPLICATE, reason="already recorded")
            record = PaymentRecord.from_row(row)
            for hook in self._on_paid:
                await hook(conn, record)
            result = ApplyResult(Outcome.APPLIED, record, credited=True)
        self._after(inst, result)
        return result

    @staticmethod
    async def _actor(
        conn: AsyncConnection, actor_telegram_id: int, owner_ids: frozenset[int] | set[int]
    ) -> tuple[Actor | None, str, bool]:
        """The presser as the database sees them now (:func:`svbg.services.roles.load_actor`, the row locked
        ``FOR SHARE``): a revoked role or a ban wins over a button pressed a moment earlier."""
        actor = await load_actor(conn, telegram_id=actor_telegram_id, owner_ids=owner_ids, lock=True)
        if actor is None:
            return None, "user", False
        return actor, actor.role, actor.has_perm(PERM_CONFIRM)

    @staticmethod
    def _own_payment(actor: Actor | None, payment_user_id: int) -> bool:
        """An admin deciding their own payment (only the owner may): no self-crediting without a transfer."""
        return actor is not None and not actor.is_owner and actor.user_id == payment_user_id

    @staticmethod
    async def _audit(
        conn: AsyncConnection,
        actor: Actor | None,
        role: str,
        action: str,
        target: str,
        *,
        amount_minor: int | None = None,
        reason: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        await conn.execute(
            sa.insert(admin_audit).values(
                actor_id=actor.user_id if actor else None,
                role=role,
                action=action,
                target=target,
                amount_minor=amount_minor,
                reason=reason,
                details=dict(details or {}),
            )
        )

    async def confirm_manual(
        self,
        payment_id: str,
        *,
        actor_telegram_id: int,
        owner_ids: frozenset[int] | set[int],
        paid_amount_minor: int,
        currency: str | None = None,
        reason: str | None = None,
    ) -> ApplyResult:
        """Admin confirmation of a manual payment (04 §8, §9.1): role re-checked here (a banned admin has
        no rights), an admin never confirms their own payment (only the owner may), the amount from the
        receipt is mandatory (a difference → ``mismatch``), ``admin_audit`` in the same transaction. A second
        confirmation is a no-op (CAS). Raises :class:`PermissionDeniedError` («Нет прав»)."""
        if (
            isinstance(paid_amount_minor, bool)
            or not isinstance(paid_amount_minor, int)
            or paid_amount_minor < 0
        ):
            raise ValueError("paid_amount_minor must be a non-negative int")
        denied = False
        result: ApplyResult | None = None
        inst: LiveInstance | None = None
        status: ProviderStatus | None = None
        target = f"payment:{payment_id}"
        async with self._db.tx() as conn:
            actor, role, allowed = await self._actor(conn, actor_telegram_id, owner_ids)
            row = None
            if allowed:
                row = (
                    (
                        await conn.execute(
                            sa.select(payments).where(payments.c.id == payment_id).with_for_update()
                        )
                    )
                    .mappings()
                    .first()
                )
                if row is None:
                    raise CheckoutError(_T["not_found"])
            if row is None or self._own_payment(actor, row["user_id"]):
                denied = True
                await self._audit(
                    conn,
                    actor,
                    role,
                    "payments.confirm.self" if row is not None else "payments.confirm.denied",
                    target,
                    details={"telegram_id": actor_telegram_id},
                )
            else:
                inst = self.instances.get(row["instance_id"])
                if inst is None or inst.caps.webhook or MethodKind.MANUAL not in inst.manifest.method_kinds:
                    raise CheckoutError(_T["not_manual"])
                cur = (currency or row["currency"]).strip().upper()
                status = ProviderStatus(
                    state=PaymentState.PAID,
                    payment_id=payment_id,
                    amount=to_decimal(paid_amount_minor, cur),
                    currency=cur,
                    is_test=row["is_test"],
                )
                result = await self._apply(
                    conn, inst, status, source="manual", confirmed_by=actor.user_id if actor else None
                )
                await self._audit(
                    conn,
                    actor,
                    role,
                    "payments.confirm",
                    target,
                    amount_minor=paid_amount_minor,
                    reason=(reason or "подтверждение ручной оплаты").strip()[:500] or "подтверждение",
                    details={"outcome": result.outcome.value, "currency": cur},
                )
        if denied:
            raise PermissionDeniedError
        assert result is not None and inst is not None
        self._after(inst, result, status)
        return result

    async def reject_manual(
        self,
        payment_id: str,
        *,
        actor_telegram_id: int,
        owner_ids: frozenset[int] | set[int],
        reason: str,
    ) -> bool:
        """Reject a manual payment (reason mandatory): ``pending → canceled``; a late confirmation wins. The
        same rights as :meth:`confirm_manual` (a banned admin or an admin's own payment → «Нет прав»)."""
        if not reason or not reason.strip():
            raise ValueError("reason is required")
        changed = False
        async with self._db.tx() as conn:
            actor, role, allowed = await self._actor(conn, actor_telegram_id, owner_ids)
            action = "payments.reject.denied"
            if allowed:
                owner_of = (
                    await conn.execute(
                        sa.select(payments.c.user_id).where(payments.c.id == payment_id).with_for_update()
                    )
                ).scalar()
                if owner_of is not None and self._own_payment(actor, owner_of):
                    allowed = False
                    action = "payments.reject.self"
            if allowed:
                action = "payments.reject"
                changed = (
                    await conn.execute(
                        sa.update(payments)
                        .where(payments.c.id == payment_id, payments.c.status == "pending")
                        .values(status="canceled", error=reason.strip()[:300], updated_at=sa.func.now())
                        .returning(payments.c.id)
                    )
                ).first() is not None
            await self._audit(conn, actor, role, action, f"payment:{payment_id}", reason=reason.strip()[:500])
        if not allowed:
            raise PermissionDeniedError
        return changed

    # ---------------------------------------------------------------------------------- reading

    async def get(self, payment_id: str) -> PaymentRecord | None:
        async with self._db.read() as conn:
            row = (
                (await conn.execute(sa.select(payments).where(payments.c.id == payment_id)))
                .mappings()
                .first()
            )
        return None if row is None else PaymentRecord.from_row(row)

    async def purge_events(self, older_than_days: int = EVENTS_TTL_DAYS) -> int:
        """Delete ``payment_events`` older than the TTL (in batches). Returns the number of rows."""
        cutoff = self._clock() - timedelta(days=older_than_days)
        total = 0
        while True:
            async with self._db.tx() as conn:
                ids = (
                    sa.select(payment_events.c.id)
                    .where(payment_events.c.received_at < cutoff)
                    .limit(_PURGE_BATCH)
                )
                deleted = (
                    await conn.execute(sa.delete(payment_events).where(payment_events.c.id.in_(ids)))
                ).rowcount
            total += deleted or 0
            if not deleted or deleted < _PURGE_BATCH:
                return total
