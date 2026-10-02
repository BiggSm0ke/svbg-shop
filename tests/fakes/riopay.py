"""Fake RioPay merchant API for tests — built from our specification (``docs/providers/riopay.md``).

* as the responder of :class:`~svbg.payments.testkit.CountingHttp` (``CountingHttp(fake)``): no sockets;
* as a real aiohttp server (``async with FakeRioPay() as desk: desk.base_url``).

Realistic where the plugin depends on it: ``X-Api-Token`` (401 otherwise; ``Authorization`` is ignored),
``PUT /v1/orders`` idempotent by ``externalId`` (the same order again, 200), ``POST /v1/orders`` (409 on
a duplicate ``externalId``), ``GET /v1/orders/{id}`` and ``/v1/orders/external/{externalId}`` (404 for
unknown), ``GET /v1/orders/services``, ``POST /v1/refunds`` (only ``COMPLETED`` orders, one refund per
order, full amount only), amounts as normalized strings (``"1000.50"`` → ``"1000.5"``), signed webhooks
``X-Signature = hex(HMAC_SHA512(token, raw body))`` with ``X-Type``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import unquote, urlsplit

from aiohttp import web

from svbg.payments.testkit import HttpCall
from svbg.sdk import HttpResponse, WebhookRequest

API_TOKEN = "riopay-test-token-not-real"
FAKE_BASE_URL = "https://riopay.fake"
SERVICES: list[dict[str, Any]] = [
    {
        "serviceId": 3,
        "displayName": "SBP RUB",
        "isDefault": True,
        "currencies": ["RUB"],
        "minOrderAmount": 100,
        "maxOrderAmount": 300000,
        "currencyLimits": {"RUB": {"minOrderAmount": 150, "maxOrderAmount": 100000}},
    },
    {"serviceId": 7, "displayName": "Cards", "isDefault": False, "currencies": ["RUB"],
     "minOrderAmount": "500", "maxOrderAmount": None},
]  # fmt: skip


def sign(token: str, body: bytes) -> str:
    """Independent implementation of the RioPay webhook signature (do not import the plugin's)."""
    return hmac.new(token.encode(), body, hashlib.sha512).hexdigest()


def norm(amount: Any) -> str:
    return format(Decimal(str(amount)).normalize(), "f")


@dataclass
class Order:
    id: str
    external_id: str | None
    amount: str
    currency: str = "RUB"
    status: str = "CREATED"
    is_test: bool = False
    body: dict[str, Any] = field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "amount": self.amount,
            "currency": self.currency,
            "paymentLink": f"https://pay.riopay.fake/{self.id}",
            "externalId": self.external_id,
            "isTest": self.is_test,
            "paymentType": "SBP",
            "createdAt": "2026-10-02T10:00:00.000Z",
            "updatedAt": "2026-10-02T10:01:00.000Z",
        }


class FakeRioPay:
    """See module docstring."""

    def __init__(self, *, api_token: str = API_TOKEN, base_url: str = FAKE_BASE_URL) -> None:
        self.api_token = api_token
        self._base_url = base_url.rstrip("/")
        self.orders: dict[str, Order] = {}
        self.created: list[tuple[str, dict[str, Any]]] = []
        self.refunds: list[dict[str, Any]] = []
        self.status_calls: list[str] = []
        self.fail_with: int | None = None
        self.test_terminal = False
        self.refunds_enabled = True
        self._runner: web.AppRunner | None = None
        self._port: int | None = None

    @property
    def base_url(self) -> str:
        if self._port is not None:
            return f"http://127.0.0.1:{self._port}"
        return self._base_url

    async def __call__(self, call: HttpCall) -> HttpResponse:
        body = call.json
        if body is None and call.data:
            body = json.loads(call.data)
        status, payload = self.handle(call.method, urlsplit(call.url).path, call.headers, body)
        return HttpResponse(status, payload, {"Content-Type": "application/json"})

    def handle(self, method: str, path: str, headers: Mapping[str, str], body: Any) -> tuple[int, bytes]:
        low = {k.lower(): v for k, v in headers.items()}
        method = method.upper()
        if self.fail_with is not None:
            return self.fail_with, _err(self.fail_with, "fault")
        if not path.startswith("/v1/"):
            return 404, b"<html>not found</html>"
        if not hmac.compare_digest(low.get("x-api-token", ""), self.api_token):
            return 401, _err(401, "Unauthorized")
        if method == "GET" and path == "/v1/orders/services":
            return 200, _j({"data": SERVICES})
        if method in ("PUT", "POST") and path == "/v1/orders":
            return self._create(method, body)
        if method == "GET" and path.startswith("/v1/orders/external/"):
            ext = unquote(path[len("/v1/orders/external/") :])
            found = next((o for o in self.orders.values() if o.external_id == ext), None)
            return (200, _j(found.as_json())) if found else (404, _err(404, "Order not found"))
        if method == "GET" and path.startswith("/v1/orders/"):
            oid = unquote(path[len("/v1/orders/") :])
            self.status_calls.append(oid)
            order = self.orders.get(oid)
            return (200, _j(order.as_json())) if order else (404, _err(404, "Order not found"))
        if method == "POST" and path == "/v1/refunds":
            return self._refund(body)
        return 404, _err(404, "Not Found")

    def _create(self, method: str, body: Any) -> tuple[int, bytes]:
        if not isinstance(body, dict) or "amount" not in body:
            return 400, _err(400, "amount is required")
        try:
            amount = norm(body["amount"])
        except (InvalidOperation, ValueError):
            return 400, _err(400, "amount must be a decimal string")
        if body.get("serviceId") not in (None, 3, 7):
            return 404, _err(404, "Service not found")
        ext = body.get("externalId")
        if method == "PUT" and not ext:
            return 400, _err(400, "externalId is required")
        existing = next((o for o in self.orders.values() if ext and o.external_id == ext), None)
        if existing is not None:
            if method == "POST":
                return 409, _err(409, "Duplicate external ID")
            return 200, _j(existing.as_json())
        self.created.append((method, dict(body)))
        order = Order(
            id=str(uuid.uuid4()),
            external_id=ext,
            amount=amount,
            currency=str(body.get("currency") or "RUB"),
            is_test=self.test_terminal,
            body=dict(body),
        )
        self.orders[order.id] = order
        return (200 if method == "PUT" else 201), _j(order.as_json())

    def _refund(self, body: Any) -> tuple[int, bytes]:
        if not isinstance(body, dict) or not body.get("orderId"):
            return 400, _err(400, "orderId is required")
        if not self.refunds_enabled:
            return 403, _err(403, "Refunds are not enabled for this payment method")
        order = self.orders.get(str(body["orderId"]))
        if order is None:
            return 404, _err(404, "Order not found")
        if order.status != "COMPLETED":
            return 409, _err(409, "Order is not COMPLETED")
        if any(r["orderId"] == order.id or r["externalId"] == body.get("externalId") for r in self.refunds):
            return 409, _err(409, "Refund already exists")
        if body.get("amount") is not None and norm(body["amount"]) != order.amount:
            return 400, _err(400, "Only full refunds are supported")
        refund = {"id": str(uuid.uuid4()), "orderId": order.id, "externalId": body.get("externalId"),
                  "status": "PENDING", "amount": order.amount}  # fmt: skip
        self.refunds.append(refund)
        return 201, _j(refund)

    # ------------------------------------------------------------------------------------- server

    async def start(self) -> None:
        app = web.Application()

        async def any_route(request: web.Request) -> web.Response:
            raw = await request.read()
            body = json.loads(raw) if raw else None
            status, payload = self.handle(request.method, request.path, dict(request.headers), body)
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

    async def __aenter__(self) -> FakeRioPay:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # ------------------------------------------------------------------------------ desk-side actions

    def add(self, oid: str, external_id: str | None, amount: Any = "179", status: str = "CREATED") -> Order:
        order = Order(id=oid, external_id=external_id, amount=norm(amount), status=status)
        self.orders[oid] = order
        return order

    def set_status(self, oid: str, status: str, *, amount: Any = None) -> Order:
        order = self.orders[oid]
        order.status = status
        if amount is not None:
            order.amount = norm(amount)
        return order

    def webhook(
        self,
        oid: str,
        status: str | None = None,
        *,
        kind: str = "ORDER_UPDATE",
        token: str | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> WebhookRequest:
        """A signed ``ORDER_UPDATE`` for order ``oid`` (its current state unless overridden)."""
        order = self.orders[oid]
        payload = order.as_json()
        payload.pop("paymentLink", None)
        payload.update({"status": status or order.status, "commission": "1.5", "received": "177.5"})
        if payload["status"] == "COMPLETED":
            payload["payedAt"] = "2026-10-02T10:01:00Z"
        payload.update(extra or {})
        body = json.dumps(payload, separators=(",", ":")).encode()
        return WebhookRequest(
            body=body,
            headers={
                "Content-Type": "application/json",
                "X-Type": kind,
                "X-Signature": sign(token or self.api_token, body),
            },
        )


def _err(status: int, message: str) -> bytes:
    names = {400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found", 409: "Conflict"}
    return _j({"statusCode": status, "message": message, "error": names.get(status, "Error")})


def _j(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False).encode()
