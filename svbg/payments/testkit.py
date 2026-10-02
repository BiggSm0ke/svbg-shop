"""TestKit — the shared acceptance suite every payment provider must pass (07 §4.1, 04 §8, D14).

A provider's tests implement :class:`WebhookVectors` (how to build an authentic webhook of that provider) and,
for ``fetch_status`` providers, a fake status endpoint behind :class:`CountingHttp`; then call:

* :func:`check_static` — contract checks without running code (a weak webhook scheme without ``fetch_status``
  fails here);
* :func:`check_plugin` — signature vectors (valid / tampered / missing), ``signed_at`` for providers with a
  replay window, the test flag, amounts as number and as string (``"179"`` == ``"179.00"``);
* :func:`check_core` — the same vectors through the real :class:`~svbg.payments.core.PaymentCore` on a real
  database: wrong signature → 401; stale timestamp → 401; replayed body → one credit; ``expired → paid`` →
  credited; chargeback → ``refunded``; test event on a live instance → rejected; ``"179"`` vs ``"179.00"`` →
  credited; a different amount → ``mismatch``;
* :func:`check_poll_budget` — the request counter over the life of an abandoned checkout: ≤ 3 status
  requests with a domain, ≤ 24 without (D14).

:class:`KitFailure` lists every failed check at once.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol

import sqlalchemy as sa

from svbg.payments.core import PaymentCore, PaymentRecord
from svbg.payments.poller import Poller
from svbg.payments.registry import InstanceRegistry, InstanceSpec, ProviderCatalog, static_problems
from svbg.payments.tables import payment_events, payments
from svbg.sdk.context import HttpResponse, PluginContext
from svbg.sdk.payments import (
    PaymentProvider,
    PaymentState,
    ProviderError,
    ProviderEvent,
    WebhookRejected,
    WebhookRequest,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from svbg.core.crypto import Crypto
    from svbg.db.engine import Database
    from svbg.payments.registry import LiveInstance

__all__ = [
    "CoreHarness",
    "CountingHttp",
    "HttpCall",
    "KitFailure",
    "MemoryKV",
    "WebhookVectors",
    "check_core",
    "check_plugin",
    "check_poll_budget",
    "check_static",
    "make_context",
    "make_provider",
]


class KitFailure(AssertionError):
    """One or more TestKit checks failed."""

    def __init__(self, provider: str, failures: Sequence[str]) -> None:
        self.failures = list(failures)
        super().__init__(f"{provider}: " + "; ".join(self.failures))


# --------------------------------------------------------------------------------------------- doubles


@dataclass(frozen=True, slots=True)
class HttpCall:
    method: str
    url: str
    headers: Mapping[str, str]
    params: Mapping[str, str]
    json: Any
    data: Any


Responder = Callable[[HttpCall], Awaitable[HttpResponse] | HttpResponse]


class CountingHttp:
    """``ctx.http`` double: every request is counted and answered by ``responder`` (a fake provider)."""

    def __init__(self, responder: Responder | None = None) -> None:
        self.responder = responder
        self.calls: list[HttpCall] = []
        self.requests = 0
        self.failures_in_row = 0
        self.last_error: str | None = None
        self.closed = False

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None,
        json: Any = None,
        data: bytes | str | Mapping[str, str] | None = None,
        timeout: float | None = None,  # noqa: ASYNC109 - mirrors the HttpClient protocol
    ) -> HttpResponse:
        call = HttpCall(method.upper(), url, dict(headers or {}), dict(params or {}), json, data)
        self.calls.append(call)
        self.requests += 1
        if self.responder is None:
            raise ProviderError("нет связи с кассой (тест)", retryable=True)
        result = self.responder(call)
        if asyncio.iscoroutine(result):
            result = await result
        assert isinstance(result, HttpResponse)
        return result

    async def close(self) -> None:
        self.closed = True


class MemoryKV:
    """``ctx.kv`` double."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    async def get(self, key: str) -> Any | None:
        return self.data.get(key)

    async def set(self, key: str, value: Any) -> None:
        self.data[key] = value

    async def delete(self, key: str) -> None:
        self.data.pop(key, None)


def make_context(
    slug: str,
    *,
    http: CountingHttp | None = None,
    kv: MemoryKV | None = None,
    is_test: bool = False,
    instance_id: int = 1,
    webhook_url: str | None = "https://shop.example/webhooks/pay/1/token",
) -> PluginContext:
    return PluginContext(
        http=http or CountingHttp(),
        log=logging.getLogger(f"svbg.payments.testkit.{slug}"),
        kv=kv or MemoryKV(),
        instance_id=instance_id,
        slug=slug,
        is_test=is_test,
        webhook_url=webhook_url,
    )


def make_provider(
    cls: type[PaymentProvider],
    config: Mapping[str, Any],
    *,
    http: CountingHttp | None = None,
    is_test: bool = False,
) -> PaymentProvider:
    """A provider object outside the core (plugin-level checks)."""
    return cls(cls.manifest.config.parse(config), make_context(cls.manifest.slug, http=http, is_test=is_test))


class WebhookVectors(Protocol):
    """How to build authentic webhooks of one provider (implemented by the provider's tests)."""

    def webhook(
        self,
        state: PaymentState,
        *,
        payment_id: str,
        external_id: str,
        amount: str,
        currency: str,
        signed_at: datetime,
        test: bool = False,
    ) -> WebhookRequest:
        """A correctly authenticated webhook. ``amount`` is the provider's textual form (``"179"``)."""
        ...

    def tamper(self, req: WebhookRequest) -> WebhookRequest:
        """The same request with a wrong signature (body or signature changed)."""
        ...

    def strip_auth(self, req: WebhookRequest) -> WebhookRequest:
        """The same request without any authentication header (empty signature)."""
        ...


# ---------------------------------------------------------------------------------------------- checks


def check_static(cls: type[PaymentProvider]) -> None:
    problems = static_problems(cls)
    if problems:
        raise KitFailure(getattr(getattr(cls, "manifest", None), "slug", cls.__name__), problems)


_PID = "0190f5d2-7b1e-7c3a-9d4e-5f6a7b8c9d0e"


async def check_plugin(
    cls: type[PaymentProvider],
    config: Mapping[str, Any],
    vectors: WebhookVectors,
    *,
    currency: str = "RUB",
    now: datetime | None = None,
) -> None:
    """Plugin-level vectors (no database). Providers without webhooks only get the static checks."""
    check_static(cls)
    caps = cls.capabilities
    if not caps.webhook:
        return
    provider = make_provider(cls, config)
    at = now or datetime.now(UTC)
    failures: list[str] = []

    def req(state: PaymentState = PaymentState.PAID, amount: str = "179", **kw: Any) -> WebhookRequest:
        return vectors.webhook(
            state, payment_id=_PID, external_id="ext-1", amount=amount, currency=currency, signed_at=at, **kw
        )

    good = req()
    try:
        event = await provider.parse_webhook(good)
    except WebhookRejected as exc:
        raise KitFailure(cls.manifest.slug, [f"a valid webhook was rejected ({exc.reason})"]) from None
    if not isinstance(event, ProviderEvent):
        failures.append("parse_webhook must return a ProviderEvent")
    else:
        if event.state is not PaymentState.PAID:
            failures.append(f"paid webhook parsed as {event.state}")
        if event.external_id != "ext-1" and event.payment_id != _PID:
            failures.append("the event identifies neither the external id nor our payment id")
        if event.amount is None or event.amount != Decimal("179"):
            failures.append(f"amount parsed as {event.amount!r}, expected 179")
        if caps.replay_window_s is not None:
            if event.signed_at is None:
                failures.append("replay_window_s is declared but signed_at is not returned")
            elif abs((event.signed_at - at).total_seconds()) > 1:
                failures.append("signed_at does not match the signed timestamp")
        if event.is_test:
            failures.append("a live webhook is reported as test")
    for name, bad in (("tampered", vectors.tamper(good)), ("unsigned", vectors.strip_auth(good))):
        try:
            await provider.parse_webhook(bad)
            failures.append(f"a {name} webhook was accepted")
        except WebhookRejected as exc:
            if exc.status not in (400, 401, 403):
                failures.append(f"a {name} webhook was rejected with {exc.status} instead of 401/403")
    for text_amount in ("179.00", "179.0"):
        try:
            ev = await provider.parse_webhook(req(amount=text_amount))
            if ev.amount != Decimal("179"):
                failures.append(f"amount {text_amount!r} parsed as {ev.amount!r}")
        except WebhookRejected as exc:
            failures.append(f"amount {text_amount!r} rejected ({exc.reason})")
    try:
        ev = await provider.parse_webhook(req(test=True))
        if not ev.is_test:
            failures.append("a test-mode webhook is not flagged is_test")
    except WebhookRejected:
        pass  # a provider may reject test events itself
    if failures:
        raise KitFailure(cls.manifest.slug, failures)


# ------------------------------------------------------------------------------------- core harness


@dataclass
class CoreHarness:
    """The real core on a real database with one instance of the provider under test."""

    db: Database
    core: PaymentCore
    registry: InstanceRegistry
    instance: LiveInstance
    http: CountingHttp
    credited: list[PaymentRecord] = field(default_factory=list)
    refunded: list[PaymentRecord] = field(default_factory=list)
    user_id: int = 0

    @classmethod
    async def create(
        cls,
        db: Database,
        crypto: Crypto,
        provider: type[PaymentProvider],
        config: Mapping[str, Any],
        *,
        http: CountingHttp | None = None,
        is_test: bool = False,
        has_domain: bool = True,
        slug: str | None = None,
        telegram_id: int = 777_000_001,
        core_kwargs: Mapping[str, Any] | None = None,
    ) -> CoreHarness:
        counting = http or CountingHttp()
        registry = InstanceRegistry(
            db,
            crypto,
            ProviderCatalog([provider]),
            public_url=lambda: "https://shop.example",
            http_factory=lambda _proxy: counting,
        )
        inst = await registry.save(
            InstanceSpec(
                slug=slug or provider.manifest.slug,
                provider=provider.manifest.slug,
                enabled=True,
                is_test=is_test,
                config=config,
            )
        )
        core = PaymentCore(db, registry, has_domain=lambda: has_domain, **dict(core_kwargs or {}))
        harness = cls(db=db, core=core, registry=registry, instance=inst, http=counting)

        async def on_paid(_conn: AsyncConnection, payment: PaymentRecord) -> None:
            harness.credited.append(payment)

        async def on_refunded(_conn: AsyncConnection, payment: PaymentRecord) -> None:
            harness.refunded.append(payment)

        core.on_paid(on_paid)
        core.on_refunded(on_refunded)
        async with db.tx() as conn:
            existing = (
                await conn.execute(sa.text("SELECT id FROM users WHERE telegram_id = :t"), {"t": telegram_id})
            ).scalar()
            if existing is None:
                existing = (
                    await conn.execute(
                        sa.text("INSERT INTO users (telegram_id) VALUES (:t) RETURNING id"),
                        {"t": telegram_id},
                    )
                ).scalar_one()
        harness.user_id = int(existing)
        return harness

    async def pending(
        self, amount_minor: int = 17_900, currency: str = "RUB", external_id: str | None = None
    ) -> str:
        """Insert a pending payment directly (no provider call); returns its id."""
        async with self.db.tx() as conn:
            pend = await self.core.insert_pending(
                conn,
                user_id=self.user_id,
                instance_id=self.instance.id,
                amount_minor=amount_minor,
                currency=currency,
                description="TestKit",
            )
            if external_id is not None:
                await conn.execute(
                    sa.update(payments).where(payments.c.id == pend.id).values(external_id=external_id)
                )
        return pend.id

    async def status(self, payment_id: str) -> str:
        async with self.db.read() as conn:
            return str(
                (
                    await conn.execute(sa.select(payments.c.status).where(payments.c.id == payment_id))
                ).scalar_one()
            )

    async def outcomes(self) -> list[str]:
        async with self.db.read() as conn:
            rows = await conn.execute(sa.select(payment_events.c.outcome).order_by(payment_events.c.id))
            return [r[0] for r in rows]

    async def send(self, req: WebhookRequest) -> int:
        resp = await self.core.handle_webhook(self.instance.id, self.instance.webhook_token, req)
        return resp.status

    async def close(self) -> None:
        await self.core.drain()
        await self.registry.close()


async def check_core(
    harness: CoreHarness,
    vectors: WebhookVectors,
    *,
    currency: str = "RUB",
    amount_text: str = "179",
    amount_minor: int = 17_900,
    now: datetime | None = None,
) -> None:
    """Core-level TestKit through the real webhook pipeline (see module docstring). Strong-signature
    providers only: a weak provider's webhook never changes state by itself (it is verified by a job)."""
    caps = harness.instance.caps
    slug = harness.instance.provider_slug
    if not caps.webhook:
        return
    at = now or datetime.now(UTC)
    failures: list[str] = []
    weak = caps.webhook_auth.is_weak

    def req(state: PaymentState, pid: str, ext: str, amount: str = amount_text, **kw: Any) -> WebhookRequest:
        return vectors.webhook(
            state,
            payment_id=pid,
            external_id=ext,
            amount=amount,
            currency=currency,
            signed_at=kw.pop("signed_at", at),
            **kw,
        )

    # 1. wrong / missing signature → 401, nothing credited
    pid = await harness.pending(amount_minor, currency, external_id="kit-sig")
    good = req(PaymentState.PAID, pid, "kit-sig")
    for name, bad in (("tampered", vectors.tamper(good)), ("unsigned", vectors.strip_auth(good))):
        code = await harness.send(bad)
        if code not in (401, 403):
            failures.append(f"{name} webhook answered {code}, expected 401")
    if await harness.status(pid) != "pending":
        failures.append("a badly signed webhook changed the payment")
    # 2. stale timestamp → 401 (only providers that sign the time)
    if caps.replay_window_s is not None:
        stale = req(
            PaymentState.PAID, pid, "kit-sig", signed_at=at - timedelta(seconds=caps.replay_window_s + 60)
        )
        if (code := await harness.send(stale)) != 401:
            failures.append(f"a stale webhook answered {code}, expected 401")
        if await harness.status(pid) != "pending":
            failures.append("a stale webhook changed the payment")
    if not weak:
        # 3. replay of the same body → one credit
        before = len(harness.credited)
        for _ in range(3):
            await harness.send(good)
        if len(harness.credited) - before != 1 or await harness.status(pid) != "paid":
            failures.append(f"replayed body credited {len(harness.credited) - before} times")
        # 4. "179" vs "179.00"
        pid2 = await harness.pending(amount_minor, currency, external_id="kit-dec")
        await harness.send(req(PaymentState.PAID, pid2, "kit-dec", amount=f"{amount_text}.00"))
        if await harness.status(pid2) != "paid":
            failures.append(f"{amount_text}.00 did not match an invoice of {amount_text}")
        # 5. expired → paid (late payment wins)
        pid3 = await harness.pending(amount_minor, currency, external_id="kit-late")
        await harness.send(req(PaymentState.EXPIRED, pid3, "kit-late"))
        if await harness.status(pid3) != "expired":
            failures.append("expired webhook did not expire the payment")
        await harness.send(req(PaymentState.PAID, pid3, "kit-late", signed_at=at + timedelta(seconds=1)))
        if await harness.status(pid3) != "paid":
            failures.append("late payment after expiry was not credited")
        # 6. chargeback → refunded
        await harness.send(
            req(PaymentState.CHARGEBACK, pid3, "kit-late", signed_at=at + timedelta(seconds=2))
        )
        if await harness.status(pid3) != "refunded":
            failures.append("chargeback did not mark the payment refunded")
        # 7. wrong amount → mismatch, nothing credited
        pid4 = await harness.pending(amount_minor, currency, external_id="kit-mm")
        before = len(harness.credited)
        await harness.send(req(PaymentState.PAID, pid4, "kit-mm", amount="1"))
        if await harness.status(pid4) != "mismatch" or len(harness.credited) != before:
            failures.append("a different amount was not turned into mismatch")
    # 8. test flag on a live instance → rejected
    if not harness.instance.is_test:
        pid5 = await harness.pending(amount_minor, currency, external_id="kit-test")
        code = await harness.send(req(PaymentState.PAID, pid5, "kit-test", test=True))
        if code < 400 or await harness.status(pid5) != "pending":
            failures.append("a test-mode webhook was accepted by a live instance")
    if failures:
        raise KitFailure(slug, failures)


class _FakeTime:
    def __init__(self, start: datetime) -> None:
        self.wall = start
        self.mono = 1_000.0

    def now(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        self.wall += timedelta(seconds=seconds)
        self.mono += seconds

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)


async def check_poll_budget(
    harness: CoreHarness,
    *,
    domain: bool,
    invoices: int = 1,
    lifetime_s: float = 6 * 3600,
    step_s: float = 2.0,
    presence: bool = True,
) -> int:
    """Simulate abandoned checkouts (the user saw the payment screen once and left) and return the number of
    provider status requests made. Raises :class:`KitFailure` above the D14 budget (per invoice: 3 with a
    domain, 24 without; a batch provider: at most one request per tick for all invoices of the instance).

    ``harness.http.responder`` must answer status requests with «still pending»."""
    inst = harness.instance
    if not inst.caps.fetch_status:
        return 0
    clock = _FakeTime(datetime.now(UTC))
    core = harness.core
    old_clock, old_domain = core._clock, core._has_domain
    core._clock, core._has_domain = clock.now, (lambda: domain)
    poller = Poller(core, harness.db, clock=clock.now, monotonic=clock.monotonic, sleep=clock.sleep)
    try:
        ids: list[str] = []
        for n in range(invoices):
            pid = await harness.pending()
            async with harness.db.tx() as conn:
                ext = f"budget-{pid[-12:]}-{n}"
                plan, first = core.poll_plan(inst, "url")
                await conn.execute(
                    sa.update(payments)
                    .where(payments.c.id == pid)
                    .values(
                        external_id=ext,
                        poll_plan=plan,
                        next_check_at=clock.now() + timedelta(seconds=first or 0) if first else None,
                    )
                )
            if presence and plan == "nodomain":
                poller.touch(pid, harness.user_id, inst.id, ext)
            ids.append(pid)
        before = harness.http.requests
        ticks = 0
        max_per_tick = 0
        elapsed = 0.0
        while elapsed < lifetime_s:
            stats = await poller.tick()
            ticks += 1
            max_per_tick = max(max_per_tick, stats.by_instance.get(inst.id, 0))
            clock.advance(step_s)
            elapsed += step_s
        used = harness.http.requests - before
    finally:
        core._clock, core._has_domain = old_clock, old_domain
        core.presence = None
    budget = 3 if domain else 24
    failures: list[str] = []
    if inst.caps.batch_status:
        if max_per_tick > 1:
            failures.append(f"batch provider made {max_per_tick} requests in one tick")
        if used > budget:
            failures.append(f"{used} status requests for {invoices} invoices (batch budget {budget})")
    elif used > budget * invoices:
        failures.append(f"{used} status requests for {invoices} invoices (budget {budget} each)")
    async with harness.db.read() as conn:
        statuses = {
            r[0] for r in await conn.execute(sa.select(payments.c.status).where(payments.c.id.in_(ids)))
        }
    if statuses - {"pending"}:
        failures.append(f"an abandoned checkout changed status: {sorted(statuses)}")
    if failures:
        raise KitFailure(inst.provider_slug, failures)
    return used
