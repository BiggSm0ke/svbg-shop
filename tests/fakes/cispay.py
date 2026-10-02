"""Fake cisPay Merchant API for tests — built from our own specification (``docs/providers/cispay.md``).

* as the responder of :class:`~svbg.payments.testkit.CountingHttp` (``CountingHttp(fake)``): no sockets;
* as a real aiohttp server (``async with FakeCisPay() as desk: desk.base_url``).

Realistic where the plugin depends on it: ``X-Shop-ID`` + ``X-Api-Key`` on every call (401 otherwise),
``POST /payments`` → ``201`` with ``payment_url`` (``order_id`` unique per shop → ``400``; CARD below 5000
kopecks and SBP without ``customer_id`` → ``422`` with ``detail[]``; an inactive method → ``403``),
``GET /payments/status?id=|order_id=`` (no ``payment_url``; ``404`` for an unknown id), full refund with
``confirm_amount`` = ``charged_amount``, ``GET /store/capabilities``. Webhooks are compact JSON signed
``hex(HMAC-SHA256(api key, raw body))`` by an independent implementation (``hmac``). Fault injection: HTTP
status of the next answers.
"""

from __future__ import annotations

import hashlib
import hmac
import itertools
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from aiohttp import ClientSession, ClientTimeout, web

from svbg.payments.testkit import HttpCall
from svbg.sdk import HttpResponse, WebhookRequest

SHOP_ID = "0192a0b0-0000-7000-8000-0000000000aa"
API_KEY = "cis_sec_TEST_ONLY_not_a_real_key"
FAKE_BASE_URL = "https://api.cispay.fake"


def sign(body: bytes, key: str = API_KEY) -> str:
    """Independent implementation of the cisPay webhook signature (do not import the plugin's)."""
    return hmac.new(key.encode(), body, hashlib.sha256).hexdigest()


def iso(at: datetime | None = None) -> str:
    return (at or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds")


@dataclass
class Transaction:
    id: str
    order_id: str
    amount: int
    method: str
    status: str = "PENDING"
    charged_amount: int | None = None
    customer_id: str | None = None
    description: str | None = None
    payload: str | None = None
    is_sandbox: bool = False
    block_reason: str | None = None
    paid_at: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def charged(self) -> int:
        return self.amount if self.charged_amount is None else self.charged_amount

    @property
    def payment_url(self) -> str:
        return f"https://cispay.fake/pay/{self.id}"

    def created_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "order_id": self.order_id,
            "status": self.status,
            "amount": self.amount,
            "charged_amount": self.charged,
            "payment_url": self.payment_url,
            "created_at": iso(self.created_at),
        }
        if self.payload is not None:
            data["payload"] = self.payload
        return data

    def status_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "order_id": self.order_id,
            "status": self.status,
            "amount": self.amount,
            "charged_amount": self.charged,
            "payment_method": self.method,
            "currency": "RUB",
            "store_name": "Магазин",
            "description": self.description,
            "available_methods": ["CARD", "SBP"],
            "is_sandbox": self.is_sandbox or self.block_reason is not None,
            "block_reason": self.block_reason,
            "is_subscription": False,
            "checkout_template": "classic",
            "payment_in_progress": False,
            "paid_at": iso(self.paid_at) if self.paid_at else None,
            "created_at": iso(self.created_at),
            "expires_in_seconds": 1800,
        }
        if self.payload is not None:
            data["payload"] = self.payload
        return data

    def webhook_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "store_id": SHOP_ID,
            "order_id": self.order_id,
            "payment_method": self.method,
            "status": self.status,
            "amount": self.amount,
            "currency": "RUB",
            "charged_amount": self.charged,
            "merchant_revenue": self.amount * 965 // 1000,
            "is_sandbox": self.is_sandbox,
            "description": self.description,
            "subscription_id": None,
        }
        if self.status == "PAID":
            data["paid_at"] = iso(self.paid_at)
        if self.payload is not None:
            data["payload"] = self.payload
        data["timestamp"] = iso()
        return data


class FakeCisPay:
    """See module docstring."""

    def __init__(
        self,
        *,
        shop_id: str = SHOP_ID,
        api_key: str = API_KEY,
        methods: tuple[str, ...] = ("CARD", "SBP"),
        base_url: str = FAKE_BASE_URL,
    ) -> None:
        self.shop_id = shop_id
        self.api_key = api_key
        self.methods = methods
        self._base_url = base_url.rstrip("/")
        self.transactions: dict[str, Transaction] = {}
        self.created: list[dict[str, Any]] = []
        self.refunds: list[dict[str, Any]] = []
        self.requests: list[tuple[str, str, dict[str, str], dict[str, str]]] = []
        self.status_calls: list[dict[str, str]] = []
        self.fail_with: int | None = None
        self._seq = itertools.count(1)
        self._runner: web.AppRunner | None = None
        self._port: int | None = None

    # ------------------------------------------------------------------------------------- transport

    @property
    def base_url(self) -> str:
        if self._port is not None:
            return f"http://127.0.0.1:{self._port}"
        return self._base_url

    async def __call__(self, call: HttpCall) -> HttpResponse:
        parts = urlsplit(call.url)
        query = dict(parse_qsl(parts.query))
        query.update({k: str(v) for k, v in call.params.items()})
        status, payload = self.handle(call.method, parts.path, call.headers, query, call.json)
        return HttpResponse(status, payload, {"Content-Type": "application/json"})

    def handle(
        self, method: str, path: str, headers: Mapping[str, str], query: Mapping[str, str], body: Any
    ) -> tuple[int, bytes]:
        low = {k.lower(): v for k, v in headers.items()}
        method = method.upper()
        self.requests.append((method, path, low, dict(query)))
        if self.fail_with is not None:
            return self.fail_with, b'{"detail":"fault"}'
        if low.get("x-shop-id") != self.shop_id or low.get("x-api-key") != self.api_key:
            return 401, _j({"detail": "Invalid shop credentials"})
        if method == "POST" and path == "/payments":
            return self._create(body)
        if method == "GET" and path == "/payments/status":
            return self._status(query)
        if method == "POST" and path == "/payments/refund":
            return self._refund(query, body)
        if method == "GET" and path == "/store/capabilities":
            return 200, _j(
                {
                    "store_id": self.shop_id,
                    "store_name": "Магазин",
                    "is_active": True,
                    "payment_methods": [
                        {
                            "payment_method": m,
                            "is_active": m in self.methods,
                            "system_fee_percent": 350,
                            "customer_fee_share_percent": 0,
                        }
                        for m in ("CARD", "SBP", "CRYPTO")
                    ],
                    "direct_sbp_enabled": False,
                }
            )
        if method == "POST" and path == "/payouts":
            raise AssertionError("the plugin must never call /payouts")
        return 404, _j({"detail": "Not Found"})

    def _create(self, body: Any) -> tuple[int, bytes]:
        if not isinstance(body, dict):
            return 422, _validation(["body"], "Field required")
        amount = body.get("amount")
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            return 422, _validation(["body", "amount"], "Input should be a valid integer")
        method = body.get("payment_method")
        if method not in ("CARD", "SBP"):
            return 422, _validation(["body", "payment_method"], "Input should be 'CARD' or 'SBP'")
        if method == "CARD" and amount < 5000:
            return 422, _validation(["body", "amount"], "Minimum amount for CARD is 5000")
        if method == "SBP" and not body.get("customer_id"):
            return 422, _validation(["body", "customer_id"], "customer_id is required for SBP")
        if body.get("currency", "RUB") != "RUB":
            return 422, _validation(["body", "currency"], "Only RUB is supported")
        if method not in self.methods:
            return 403, _j({"detail": f"Payment method {method} is not enabled"})
        order = body.get("order_id")
        if not isinstance(order, str) or not 1 <= len(order) <= 255:
            return 422, _validation(["body", "order_id"], "String should have at least 1 character")
        if any(t.order_id == order for t in self.transactions.values()):
            return 400, _j({"detail": "order_id already exists"})
        self.created.append(dict(body))
        tx = Transaction(
            id=str(uuid.uuid4()),
            order_id=order,
            amount=amount,
            method=method,
            customer_id=body.get("customer_id"),
            description=body.get("description"),
            payload=body.get("payload"),
        )
        self.transactions[tx.id] = tx
        return 201, _j(tx.created_json())

    def _status(self, query: Mapping[str, str]) -> tuple[int, bytes]:
        self.status_calls.append(dict(query))
        tx: Transaction | None = None
        if "id" in query:
            tx = self.transactions.get(query["id"])
        elif "order_id" in query:
            tx = next((t for t in self.transactions.values() if t.order_id == query["order_id"]), None)
        if tx is None:
            return 404, _j({"detail": "Transaction not found"})
        return 200, _j(tx.status_json())

    def _refund(self, query: Mapping[str, str], body: Any) -> tuple[int, bytes]:
        tx = self.transactions.get(query.get("id", ""))
        if tx is None:
            return 404, _j({"detail": "Transaction not found"})
        if not isinstance(body, dict) or body.get("confirm_amount") != tx.charged:
            return 400, _j({"detail": "confirm_amount must equal charged_amount"})
        if tx.status != "PAID":
            return 400, _j({"detail": "Only paid transactions can be refunded"})
        self.refunds.append({"id": tx.id, **body})
        tx.status = "REFUNDED"
        return 200, _j({"transaction_id": tx.id, "status": "REFUNDED", "refund_status": "DONE"})

    # ------------------------------------------------------------------------------------- server

    async def start(self) -> None:
        app = web.Application()

        async def any_route(request: web.Request) -> web.Response:
            raw = await request.read()
            body = json.loads(raw) if raw else None
            status, payload = self.handle(
                request.method, request.path, dict(request.headers), dict(request.query), body
            )
            return web.Response(status=status, body=payload, content_type="application/json")

        app.router.add_route("*", "/{tail:.*}", any_route)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        server = site._server
        assert server is not None
        self._port = server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
        self._runner = None
        self._port = None

    async def __aenter__(self) -> FakeCisPay:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # ------------------------------------------------------------------------------ desk-side actions

    def by_order(self, order_id: str) -> Transaction:
        return next(t for t in self.transactions.values() if t.order_id == order_id)

    def pay(self, order_id: str, *, status: str = "PAID", sandbox: bool = False) -> Transaction:
        tx = self.by_order(order_id)
        tx.status = status
        tx.is_sandbox = sandbox
        if status == "PAID":
            tx.paid_at = datetime.now(UTC)
        return tx

    # ------------------------------------------------------------------------------------- webhooks

    def webhook_parts(
        self, tx: Transaction, *, key: str | None = None, extra: Mapping[str, Any] | None = None
    ) -> tuple[bytes, dict[str, str]]:
        data = tx.webhook_json()
        data.update(extra or {})
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
        return body, {"Content-Type": "application/json", "X-Signature": sign(body, key or self.api_key)}

    def webhook(self, tx: Transaction, **kw: Any) -> WebhookRequest:
        body, headers = self.webhook_parts(tx, **kw)
        return WebhookRequest(body=body, headers=headers)

    async def send_webhook(self, url: str, tx: Transaction, **kw: Any) -> int:
        body, headers = self.webhook_parts(tx, **kw)
        async with (
            ClientSession(timeout=ClientTimeout(total=10)) as session,
            session.post(url, data=body, headers=headers) as resp,
        ):
            await resp.read()
            return resp.status


def _validation(loc: list[str], msg: str) -> bytes:
    return _j({"detail": [{"loc": loc, "msg": msg, "type": "value_error"}]})


def _j(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False).encode()
