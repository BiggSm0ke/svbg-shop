"""Payment core test kit: stub providers speaking a tiny made-up protocol, a fake provider server behind
:class:`~svbg.payments.testkit.CountingHttp`, fakes for attention / admin chat, and a database fixture.

Until integration registers ``svbg.payments.tables`` in ``svbg.db.schema.TABLE_MODULES`` (and a migration),
the payment tables are attached to the shared metadata only while a test of this package runs: otherwise
``tests/e2e/test_migrations.py`` (same xdist worker) would see tables that no migration creates.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

import svbg.payments.tables as pay_tables
from svbg.core.crypto import Crypto, generate_key
from svbg.db import schema
from svbg.db.meta import metadata
from svbg.payments.core import PaymentCore, PaymentRecord
from svbg.payments.registry import InstanceRegistry, InstanceSpec, LiveInstance, ProviderCatalog
from svbg.payments.testkit import CountingHttp, HttpCall
from svbg.sdk import (
    Capabilities,
    Checkout,
    ConfigModel,
    HttpResponse,
    Manifest,
    MethodKind,
    PaymentIntent,
    PaymentProvider,
    PaymentState,
    Probe,
    ProviderError,
    ProviderEvent,
    ProviderStatus,
    WebhookAuth,
    WebhookIgnored,
    WebhookRejected,
    WebhookRequest,
    constant_time_equal,
    hmac_sha256_hex,
    parse_amount,
    parse_timestamp,
    secret,
    url,
)
from tests.dbkit import CountingDatabase, open_db

PAYMENT_TABLES = (pay_tables.payment_instances, pay_tables.payments, pay_tables.payment_events)
_REGISTERED = "svbg.payments.tables" in schema.TABLE_MODULES
BASE = "https://stub.example/api"
SECRET = "whsec-0123456789abcdef"
API_KEY = "key-live-0123456789"


def _detach() -> None:
    if _REGISTERED:
        return
    for table in PAYMENT_TABLES:
        if table.name in metadata.tables:
            metadata.remove(table)


def _attach() -> None:
    for table in PAYMENT_TABLES:
        if table.name not in metadata.tables:
            metadata._add_table(table.name, table.schema, table)


_detach()


@pytest.fixture
def payment_schema() -> Iterator[None]:
    _attach()
    try:
        yield
    finally:
        _detach()


@pytest.fixture
async def db(pg_dsn: str, payment_schema: None) -> AsyncIterator[CountingDatabase]:
    async with open_db(pg_dsn, pool_size=12) as database:
        yield database


@pytest.fixture
def crypto() -> Crypto:
    return Crypto([generate_key()])


# ------------------------------------------------------------------------------------------ stub plugins


class StubConfig(ConfigModel):
    api_key = secret("API-ключ", where="кабинет Stub → API")
    signing_secret = secret("Секрет подписи", where="кабинет Stub → Вебхуки")
    base_url = url("Адрес API", default=BASE, required=False, advanced=True)


class StubPay(PaymentProvider):
    """Signature scheme: ``X-Sig = hex(HMAC_SHA256(secret, X-Ts + "." + body))``, ``X-Ts`` in Unix seconds."""

    manifest = Manifest(
        slug="stubpay",
        title="Stub Pay",
        method_kinds=(MethodKind.SBP, MethodKind.CARD),
        currencies=("RUB",),
        config=StubConfig,
        docs_url="https://stub.example/docs",
        min_minor=10_000,
    )
    capabilities = Capabilities(webhook_auth=WebhookAuth.SIGNATURE, replay_window_s=300, fetch_status=True)

    async def create(self, intent: PaymentIntent) -> Checkout:
        resp = await self.ctx.http.request(
            "POST",
            f"{self.config.base_url}/invoices",
            headers={"X-Key": self.config.api_key},
            json={
                "order": intent.payment_id,
                "amount": intent.amount_text(),
                "currency": intent.currency,
                "customer": intent.customer_ref,
                "test": intent.is_test,
            },
        )
        if resp.status != 200:
            raise ProviderError(f"касса ответила {resp.status}", retryable=resp.status >= 500)
        data = resp.json()
        return Checkout(kind="url", external_id=data["id"], pay_url=data["url"])

    async def parse_webhook(self, req: WebhookRequest) -> ProviderEvent:
        stamp = req.header("X-Ts") or ""
        expected = hmac_sha256_hex(self.config.signing_secret, stamp.encode() + b"." + req.body)
        if not constant_time_equal(expected, (req.header("X-Sig") or "").strip().lower()):
            raise WebhookRejected("bad_signature")
        data = req.json()
        if data.get("type") == "ping":
            raise WebhookIgnored("ping")
        try:
            signed_at = parse_timestamp(stamp)
            amount = parse_amount(data["amount"]) if data.get("amount") is not None else None
        except (ValueError, TypeError):
            raise WebhookRejected("malformed", status=400) from None
        return ProviderEvent(
            state=PaymentState(data["status"]),
            external_id=data.get("id"),
            payment_id=data.get("order"),
            amount=amount,
            currency=data.get("currency"),
            is_test=bool(data.get("test")),
            signed_at=signed_at,
            summary={"id": data.get("id"), "status": data.get("status"), "amount": data.get("amount")},
        )

    async def fetch_status(self, ids: Sequence[str]) -> list[ProviderStatus]:
        resp = await self.ctx.http.request(
            "GET", f"{self.config.base_url}/invoices", params={"ids": ",".join(ids)}
        )
        if resp.status != 200:
            raise ProviderError(f"касса ответила {resp.status}", retryable=True)
        return [
            ProviderStatus(
                state=PaymentState(item["status"]),
                external_id=item["id"],
                amount=parse_amount(item["amount"]),
                currency=item["currency"],
            )
            for item in resp.json()
        ]

    async def test_credentials(self) -> Probe:
        resp = await self.ctx.http.request(
            "GET", f"{self.config.base_url}/me", headers={"X-Key": self.config.api_key}
        )
        if resp.status == 200:
            return Probe(True, "ключ принят")
        return Probe(False, "касса отклонила ключ")


class BatchPay(StubPay):
    manifest = Manifest(
        slug="batchpay",
        title="Batch Pay",
        method_kinds=(MethodKind.CRYPTO,),
        currencies=("RUB", "USDT"),
        config=StubConfig,
        docs_url="https://stub.example/docs",
    )
    capabilities = Capabilities(
        webhook_auth=WebhookAuth.SIGNATURE, fetch_status=True, batch_status=True, batch_limit=50
    )


class WeakPay(StubPay):
    """Static secret in a header (weak): the core must re-read the status."""

    manifest = Manifest(
        slug="weakpay",
        title="Weak Pay",
        method_kinds=(MethodKind.CARD,),
        currencies=("RUB",),
        config=StubConfig,
        docs_url="https://stub.example/docs",
    )
    capabilities = Capabilities(webhook_auth=WebhookAuth.SECRET_HEADER, fetch_status=True)

    async def parse_webhook(self, req: WebhookRequest) -> ProviderEvent:
        if not constant_time_equal(req.header("X-Secret"), self.config.signing_secret):
            raise WebhookRejected("bad_secret")
        data = req.json()
        return ProviderEvent(
            state=PaymentState(data["status"]),
            external_id=data.get("id"),
            amount=parse_amount(data["amount"]) if data.get("amount") is not None else None,
            currency=data.get("currency"),
        )


class ManualConfig(ConfigModel):
    details = secret("Реквизиты", where="ваш банк")


class ManualPay(PaymentProvider):
    manifest = Manifest(
        slug="manualpay",
        title="Перевод",
        method_kinds=(MethodKind.MANUAL,),
        currencies=("RUB",),
        config=ManualConfig,
        docs_url="https://stub.example/manual",
    )
    capabilities = Capabilities(webhook_auth=WebhookAuth.NONE, webhook=False, redirect=False)

    async def create(self, intent: PaymentIntent) -> Checkout:
        return Checkout(kind="details", details=f"{self.config.details}\nСумма: {intent.amount_text()} ₽")

    async def test_credentials(self) -> Probe:
        return Probe(True)


# ------------------------------------------------------------------------------------------ fake server


@dataclass
class FakeStubServer:
    """The provider side of the stub protocol (answers through ``CountingHttp``)."""

    invoices: dict[str, dict[str, Any]] = field(default_factory=dict)
    fail_create: int | None = None  # HTTP status to answer create with
    fail_status: bool = False
    raise_network: bool = False
    delay: float = 0.0
    concurrent: int = 0
    max_concurrent: int = 0
    keys: set[str] = field(default_factory=lambda: {API_KEY})
    created: list[dict[str, Any]] = field(default_factory=list)
    status_calls: list[list[str]] = field(default_factory=list)
    _seq: int = 0

    async def __call__(self, call: HttpCall) -> HttpResponse:
        if self.raise_network:
            raise ProviderError("нет связи с кассой (тест)", retryable=True)
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            return self._answer(call)
        finally:
            self.concurrent -= 1

    def _answer(self, call: HttpCall) -> HttpResponse:
        if call.url.endswith("/me"):
            return HttpResponse(200 if call.headers.get("X-Key") in self.keys else 401, b"{}")
        if call.method == "POST" and call.url.endswith("/invoices"):
            if self.fail_create is not None:
                return HttpResponse(self.fail_create, b"{}")
            self._seq += 1
            ext = f"inv-{self._seq}"
            body = dict(call.json)
            self.created.append(body)
            self.invoices[ext] = {
                "id": ext,
                "status": "created",
                "amount": body["amount"],
                "currency": body["currency"],
            }
            return HttpResponse(
                200, json.dumps({"id": ext, "url": f"https://stub.example/pay/{ext}"}).encode()
            )
        if call.method == "GET" and call.url.endswith("/invoices"):
            ids = [i for i in call.params.get("ids", "").split(",") if i]
            self.status_calls.append(ids)
            if self.fail_status:
                return HttpResponse(503, b"{}")
            items = [self.invoices[i] for i in ids if i in self.invoices]
            return HttpResponse(200, json.dumps(items).encode())
        return HttpResponse(404, b"{}")

    def set(self, ext: str, status: str, amount: str | None = None, currency: str = "RUB") -> None:
        inv = self.invoices.setdefault(ext, {"id": ext, "amount": amount or "179.00", "currency": currency})
        inv["status"] = status
        if amount is not None:
            inv["amount"] = amount
        inv["currency"] = currency


def sign(body: bytes, ts: str, secret_value: str = SECRET) -> str:
    return hmac_sha256_hex(secret_value, ts.encode() + b"." + body)


def stub_webhook(
    status: str | PaymentState,
    *,
    ext: str | None = None,
    order: str | None = None,
    amount: str | None = "179",
    currency: str = "RUB",
    at: datetime | None = None,
    test: bool = False,
    secret_value: str = SECRET,
    extra: dict[str, Any] | None = None,
) -> WebhookRequest:
    payload: dict[str, Any] = {"status": str(status), "currency": currency, "test": test}
    if ext is not None:
        payload["id"] = ext
    if order is not None:
        payload["order"] = order
    if amount is not None:
        payload["amount"] = amount
    payload.update(extra or {})
    body = json.dumps(payload).encode()
    ts = str(int((at or datetime.now(UTC)).timestamp()))
    return WebhookRequest(body=body, headers={"X-Ts": ts, "X-Sig": sign(body, ts, secret_value)})


class StubVectors:
    """:class:`~svbg.payments.testkit.WebhookVectors` of the stub protocol."""

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
        return stub_webhook(
            state,
            ext=external_id,
            order=payment_id,
            amount=amount,
            currency=currency,
            at=signed_at,
            test=test,
        )

    def tamper(self, req: WebhookRequest) -> WebhookRequest:
        return WebhookRequest(body=req.body + b" ", headers=dict(req.headers))

    def strip_auth(self, req: WebhookRequest) -> WebhookRequest:
        headers = {k: v for k, v in req.headers.items() if k.lower() != "x-sig"}
        return WebhookRequest(body=req.body, headers=headers)


# ------------------------------------------------------------------------------------------ fakes


@dataclass
class FakeAttention:
    raised: list[tuple[str, str, str, str]] = field(default_factory=list)

    async def raise_item(
        self, dedup_key: str, severity: str, title: str, body: str = "", fix_action: str | None = None
    ) -> None:
        self.raised.append((dedup_key, severity, title, body))

    def dedup_keys(self) -> list[str]:
        return [k for k, *_ in self.raised]


@dataclass
class FakeAdminChat:
    posts: list[tuple[str, str]] = field(default_factory=list)

    async def post(self, kind: str, text: str, *, html: bool = False) -> None:
        self.posts.append((kind, text))


STUB_CONFIG = {"api_key": API_KEY, "signing_secret": SECRET}


@dataclass
class Env:
    db: CountingDatabase
    registry: InstanceRegistry
    core: PaymentCore
    server: FakeStubServer
    http: CountingHttp
    attention: FakeAttention
    admin: FakeAdminChat
    credited: list[PaymentRecord] = field(default_factory=list)
    refunded: list[PaymentRecord] = field(default_factory=list)
    user_id: int = 0

    def inst(self, slug: str = "stubpay") -> LiveInstance:
        inst = self.registry.by_slug(slug)
        assert inst is not None
        return inst

    async def add_user(self, telegram_id: int, role: str = "user", perms: Sequence[str] = ()) -> int:
        rows = await self.db.raw(
            "insert into users (telegram_id, role, perms) values ($1, $2, $3::jsonb) returning id",
            telegram_id,
            role,
            json.dumps(list(perms)),
        )
        return int(rows[0]["id"])

    async def rows(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        return [dict(r) for r in await self.db.raw(sql, *args)]

    async def count(self, table: str) -> int:
        return int((await self.db.raw(f"select count(*) as n from {table}"))[0]["n"])

    async def payment(self, payment_id: str) -> dict[str, Any]:
        rows = await self.db.raw("select * from payments where id = $1", payment_id)
        return dict(rows[0])

    async def pending(
        self,
        slug: str = "stubpay",
        amount_minor: int = 17_900,
        currency: str = "RUB",
        user_id: int | None = None,
    ) -> str:
        """A pending payment with a real invoice on the fake server; returns the payment id."""
        result = await self.core.create_payment(
            user_id=user_id or self.user_id,
            instance_id=self.inst(slug).id,
            amount_minor=amount_minor,
            currency=currency,
            description="Пополнение баланса",
        )
        return result.payment_id

    async def send(self, req: WebhookRequest, slug: str = "stubpay") -> int:
        inst = self.inst(slug)
        resp = await self.core.handle_webhook(inst.id, inst.webhook_token, req)
        return resp.status


async def build_env(
    db: CountingDatabase,
    crypto: Crypto,
    *,
    has_domain: bool = True,
    providers: Sequence[type[PaymentProvider]] = (StubPay, BatchPay, WeakPay, ManualPay),
    test_instances: Sequence[str] = (),
    register_on_paid: bool = True,
) -> Env:
    server = FakeStubServer()
    http = CountingHttp(server)
    registry = InstanceRegistry(
        db,
        crypto,
        ProviderCatalog(providers),
        public_url=lambda: "https://shop.example",
        http_factory=lambda _proxy: http,
    )
    for sort, cls in enumerate(providers):
        slug = cls.manifest.slug
        config = {"details": "Карта 2200 0000 0000 0000"} if cls is ManualPay else STUB_CONFIG
        await registry.save(
            InstanceSpec(
                slug=slug,
                provider=slug,
                enabled=True,
                is_test=slug in test_instances,
                config=config,
                sort=sort,
            )
        )
    attention = FakeAttention()
    admin = FakeAdminChat()
    core = PaymentCore(
        db,
        registry,
        attention=attention,  # type: ignore[arg-type]
        admin_chat=admin,
        has_domain=lambda: has_domain,
    )
    env = Env(db, registry, core, server, http, attention, admin)

    async def on_paid(_conn: Any, payment: PaymentRecord) -> None:
        env.credited.append(payment)

    async def on_refunded(_conn: Any, payment: PaymentRecord) -> None:
        env.refunded.append(payment)

    if register_on_paid:
        core.on_paid(on_paid)
    core.on_refunded(on_refunded)
    env.user_id = await env.add_user(555_000_001)
    return env


@pytest.fixture
async def env(db: CountingDatabase, crypto: Crypto) -> AsyncIterator[Env]:
    environment = await build_env(db, crypto)
    try:
        yield environment
    finally:
        await environment.core.drain()
