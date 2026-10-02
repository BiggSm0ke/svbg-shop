"""Fake AuraPay API for tests — built from our specification (``docs/providers/aurapay.md``).

* as the responder of :class:`~svbg.payments.testkit.CountingHttp` (``CountingHttp(fake)``): no sockets;
* as a real aiohttp server (``async with FakeAuraPay() as desk: desk.base_url``).

Realistic where the plugin depends on it: ``X-ApiKey`` + ``X-ShopId`` (the real answer to wrong keys is not
documented — the fake answers ``401``), ``POST /invoice/create`` (``amount`` a JSON number ≥ 0.01,
``order_id`` unique per shop → ``400 {"error": "Order id is not unique", "data": []}``, ``lifetime``
1…43200, ``service`` ``card|sbp``), ``POST /invoice/status`` by exactly one of ``id`` / ``order_id``
(``404 Invoice not found``),
``GET /shop/balance``; webhooks signed with ``hex(HMAC-SHA256(key #2, sorted top-level values glued))`` and
the ``amount`` as a string with two decimals.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

from aiohttp import web

from svbg.payments.testkit import HttpCall
from svbg.sdk import HttpResponse, WebhookRequest

API_KEY = "ap-key-0123456789abcdef0123456789abcdef"
SHOP_ID = "17f9db92-1c87-4ef1-9281-45e1ab74ae96"
WEBHOOK_SECRET = "test_secret_key_2"  # the key of the spec's vectors (§11)
FAKE_BASE_URL = "https://aurapay.fake"


def sign(payload: Mapping[str, Any], key: str = WEBHOOK_SECRET) -> str:
    """Spec §5.2 for flat bodies of strings, integers and ``null`` (what AuraPay sends)."""
    parts = []
    for name in sorted(payload):
        value = payload[name]
        parts.append("" if value is None else str(value))
    return hmac.new(key.encode(), "".join(parts).encode(), hashlib.sha256).hexdigest()


@dataclass
class Invoice:
    id: str
    order_id: str
    amount: Decimal
    status: str = "PENDING"
    service: str | None = None
    comment: str | None = None
    body: dict[str, Any] = field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        amount: Any = (
            int(self.amount) if self.amount == self.amount.to_integral_value() else float(self.amount)
        )
        return {
            "id": self.id,
            "order_id": self.order_id,
            "shop_id": SHOP_ID,
            "amount": amount,
            "comment": self.comment,
            "service": self.service,
            "expires_at": "2026-10-02 13:00:00",
            "created_at": "2026-10-02 12:00:00",
            "status": self.status,
            "payment_data": {"url": f"https://pay.aurapay.fake/{self.id}"},
        }


class FakeAuraPay:
    """See module docstring."""

    def __init__(
        self,
        *,
        api_key: str = API_KEY,
        shop_id: str = SHOP_ID,
        webhook_secret: str = WEBHOOK_SECRET,
        base_url: str = FAKE_BASE_URL,
    ) -> None:
        self.api_key = api_key
        self.shop_id = shop_id
        self.webhook_secret = webhook_secret
        self._base_url = base_url.rstrip("/")
        self.invoices: dict[str, Invoice] = {}
        self.created: list[dict[str, Any]] = []
        self.status_calls: list[dict[str, Any]] = []
        self.fail_with: int | None = None
        self.drop_create_answer = False  # the invoice is created but the answer is «lost» (502)
        self._runner: web.AppRunner | None = None
        self._port: int | None = None

    @property
    def base_url(self) -> str:
        if self._port is not None:
            return f"http://127.0.0.1:{self._port}"
        return self._base_url

    async def __call__(self, call: HttpCall) -> HttpResponse:
        status, payload = self.handle(call.method, urlsplit(call.url).path, call.headers, call.json)
        return HttpResponse(status, payload, {"Content-Type": "application/json"})

    def handle(self, method: str, path: str, headers: Mapping[str, str], body: Any) -> tuple[int, bytes]:
        low = {k.lower(): v for k, v in headers.items()}
        if self.fail_with is not None:
            return self.fail_with, _err("fault")
        if not (
            hmac.compare_digest(low.get("x-apikey", ""), self.api_key)
            and hmac.compare_digest(low.get("x-shopid", ""), self.shop_id)
        ):
            return 401, _err("Unauthorized")
        method = method.upper()
        if method == "POST" and path == "/invoice/create":
            return self._create(body)
        if method == "POST" and path == "/invoice/status":
            return self._status(body)
        if method == "GET" and path == "/shop/balance":
            return 200, _j({"balance": 1250.5, "balance_hold": 0})
        return 404, _err("Not found")

    def _create(self, body: Any) -> tuple[int, bytes]:
        if not isinstance(body, dict):
            return 400, _err("json body required")
        amount = body.get("amount")
        if isinstance(amount, bool) or not isinstance(amount, int | float) or amount < 0.01:
            return 400, _err("amount must be a number >= 0.01")
        order_id = body.get("order_id")
        if not isinstance(order_id, str) or not order_id:
            return 400, _err("order_id is required")
        lifetime = body.get("lifetime", 60)
        if not isinstance(lifetime, int) or not 1 <= lifetime <= 43_200:
            return 400, _err("lifetime must be 1..43200")
        if body.get("service") not in (None, "card", "sbp"):
            return 400, _err("service must be card or sbp")
        if any(inv.order_id == order_id for inv in self.invoices.values()):
            return 400, _err("Order id is not unique")
        self.created.append(dict(body))
        inv = Invoice(
            id=str(uuid.uuid4()),
            order_id=order_id,
            amount=Decimal(str(amount)),
            service=body.get("service"),
            comment=body.get("comment"),
            body=dict(body),
        )
        self.invoices[inv.id] = inv
        if self.drop_create_answer:
            self.drop_create_answer = False
            return 502, b"<html>bad gateway</html>"
        return 200, _j(inv.as_json())

    def _status(self, body: Any) -> tuple[int, bytes]:
        if not isinstance(body, dict) or len(body) != 1 or not ({"id", "order_id"} & set(body)):
            return 400, _err("exactly one of id, order_id is required")
        self.status_calls.append(dict(body))
        if "id" in body:
            inv = self.invoices.get(str(body["id"]))
        else:
            inv = next((i for i in self.invoices.values() if i.order_id == body["order_id"]), None)
        if inv is None:
            return 404, _err("Invoice not found")
        return 200, _j(inv.as_json())

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

    async def __aenter__(self) -> FakeAuraPay:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # ------------------------------------------------------------------------------ desk-side actions

    def add(self, invoice_id: str, order_id: str, amount: str = "179.00", status: str = "PENDING") -> Invoice:
        inv = Invoice(id=invoice_id, order_id=order_id, amount=Decimal(amount), status=status)
        self.invoices[invoice_id] = inv
        return inv

    def set_status(self, invoice_id: str, status: str, *, amount: str | None = None) -> Invoice:
        inv = self.invoices[invoice_id]
        inv.status = status
        if amount is not None:
            inv.amount = Decimal(amount)
        return inv

    def webhook_payload(
        self, invoice_id: str, status: str | None = None, *, amount: str | None = None
    ) -> dict:
        inv = self.invoices[invoice_id]
        return {
            "id": inv.id,
            "amount": amount if amount is not None else f"{inv.amount:.2f}",
            "status": status or inv.status,
            "comment": inv.comment or "",
            "created_at": "2026-10-02 12:00:00",
            "expires_at": "2026-10-02 13:00:00",
            "service": inv.service or "sbp",
            "payer_details": "794*****254",
            "payer_ip": "203.0.113.7",
            "shop_id": self.shop_id,
            "order_id": inv.order_id,
            "custom_fields": None,
        }

    def webhook(
        self, invoice_id: str, status: str | None = None, *, amount: str | None = None, key: str | None = None
    ) -> WebhookRequest:
        payload = self.webhook_payload(invoice_id, status, amount=amount)
        return signed(payload, key or self.webhook_secret)


def signed(payload: Mapping[str, Any], key: str = WEBHOOK_SECRET) -> WebhookRequest:
    return WebhookRequest(
        body=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json", "X-SIGNATURE": sign(payload, key)},
    )


def _err(message: str) -> bytes:
    return _j({"error": message, "data": []})


def _j(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False).encode()
