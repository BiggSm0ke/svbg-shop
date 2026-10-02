"""Fake Lava Business API for tests — built from our own specification (``docs/providers/lava.md``).

* as the responder of :class:`~svbg.payments.testkit.CountingHttp` (``CountingHttp(fake)``): no sockets;
* as a real aiohttp server (``async with FakeLava() as desk: desk.base_url``).

Realistic where the plugin depends on it: every ``POST /business/...`` must carry ``Signature =
hex(HMAC_SHA256(secret key, the exact body bytes))`` — checked over the bytes as received (an **independent**
implementation) — and a raw JSON body; answers ``{"data": …, "status": 200, "status_check": true}`` or
``{"error": …, "data": null, "status": <code>, "status_check": false}``. ``invoice/create`` validates ``sum``
(a JSON number, 1…2 000 000), ``expire`` (minutes ≤ 7200), a unique ``orderId`` (422 otherwise) and the
``shopId`` (404 «Проект не найден»); ``invoice/status`` → ``InvoiceApiStatusResource`` (404 for unknown
invoices); ``invoice/get-available-tariffs``. Webhooks are signed with the additional key either over the
PHP-canonical body (top-level keys sorted, ``\\/`` and ``\\uXXXX`` escapes — the official SDK) or over the raw
body (the current documentation), in the ``Authorization`` or the ``Signature`` header.
"""

from __future__ import annotations

import hashlib
import hmac
import itertools
import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

from aiohttp import ClientSession, ClientTimeout, web

from svbg.payments.testkit import HttpCall
from svbg.sdk import HttpResponse, WebhookRequest

SHOP_ID = "1b6e9f2d-8c4a-4b7e-9d3f-2a5c8e1b4f7d"
SECRET_KEY = "lava-secret-key-0123456789abcdef"
ADDITIONAL_KEY = "lava-additional-key-fedcba9876543210"
OTHER_KEY = "lava-key-of-another-project-000000"
API_PATH = "/business"
FAKE_BASE_URL = "https://lava.fake"


def hmac_hex(key: str, message: bytes) -> str:
    """Independent HMAC-SHA256 helper (do not import the plugin's)."""
    return hmac.new(key.encode(), message, hashlib.sha256).hexdigest()


def sdk_canonical(data: Mapping[str, Any]) -> bytes:
    """``json_encode(ksort($data))`` for flat bodies of strings, integers and ``null`` (the SDK's form)."""
    ordered = {k: data[k] for k in sorted(data)}
    return json.dumps(ordered, separators=(",", ":"), ensure_ascii=True).replace("/", "\\/").encode()


@dataclass
class Invoice:
    id: str
    order_id: str
    amount: Decimal
    expire: int
    body: dict[str, Any]
    status: str = "created"
    custom_fields: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "error_message": "",
            "id": self.id,
            "shop_id": self.body.get("shopId"),
            "amount": float(self.amount),
            "expire": "2026-10-02 17:00:00",
            "order_id": self.order_id,
            "fail_url": self.body.get("failUrl"),
            "success_url": self.body.get("successUrl"),
            "hook_url": self.body.get("hookUrl"),
            "custom_fields": self.custom_fields,
            "include_service": self.body.get("includeService") or [],
            "exclude_service": [],
        }


class FakeLava:
    """See module docstring."""

    def __init__(
        self,
        *,
        shop_id: str = SHOP_ID,
        secret_key: str = SECRET_KEY,
        additional_key: str = ADDITIONAL_KEY,
        base_url: str = FAKE_BASE_URL,
    ) -> None:
        self.shop_id = shop_id
        self.secret_key = secret_key
        self.additional_key = additional_key
        self._base_url = base_url.rstrip("/")
        self.invoices: dict[str, Invoice] = {}
        self.created: list[dict[str, Any]] = []
        self.raw_bodies: list[bytes] = []
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.status_calls: list[str] = []
        self.fail_with: int | None = None
        self._seq = itertools.count(1)
        self._runner: web.AppRunner | None = None
        self._port: int | None = None

    @property
    def base_url(self) -> str:
        if self._port is not None:
            return f"http://127.0.0.1:{self._port}"
        return self._base_url

    async def __call__(self, call: HttpCall) -> HttpResponse:
        raw = call.data if isinstance(call.data, bytes) else b""
        if call.json is not None:  # a dict sent through json= has no fixed byte form: Lava cannot verify it
            raw = json.dumps(call.json).encode()
        status, payload = self.handle(call.method, urlsplit(call.url).path, call.headers, raw)
        return HttpResponse(status, payload, {"Content-Type": "application/json"})

    def handle(self, method: str, path: str, headers: Mapping[str, str], raw: bytes) -> tuple[int, bytes]:
        low = {k.lower(): v for k, v in headers.items()}
        self.requests.append((method.upper(), path, low))
        self.raw_bodies.append(raw)
        if self.fail_with is not None:
            return self.fail_with, _err("fault", self.fail_with)
        if method.upper() != "POST" or not path.startswith(API_PATH + "/"):
            return 404, b"<html>not found</html>"
        if low.get("content-type", "").split(";")[0].strip() != "application/json":
            return 422, _err("Content-Type должен быть application/json", 422)
        if not hmac.compare_digest(low.get("signature", ""), hmac_hex(self.secret_key, raw)):
            return 401, _err("Неверная подпись", 401)
        try:
            body = json.loads(raw, parse_float=Decimal)
        except ValueError:
            return 422, _err("Невалидный JSON", 422)
        if not isinstance(body, dict):
            return 422, _err("Невалидный JSON", 422)
        if body.get("shopId") != self.shop_id:
            return 404, _err("Проект не найден", 404)
        rest = path[len(API_PATH) :]
        if rest == "/invoice/create":
            return self._create(body)
        if rest == "/invoice/status":
            invoice_id = body.get("invoiceId")
            self.status_calls.append(str(invoice_id))
            invoice = self.invoices.get(str(invoice_id))
            if invoice is None and body.get("orderId"):
                invoice = next((i for i in self.invoices.values() if i.order_id == body["orderId"]), None)
            if invoice is None:
                return 404, _err("Счёт не найден", 404)
            return 200, _ok(invoice.as_json())
        if rest == "/invoice/get-available-tariffs":
            return 200, _ok(
                [
                    {
                        "service_id": "card",
                        "service_name": "Карта",
                        "percent": 4,
                        "currency": "RUB",
                        "min_amount": 10,
                        "max_amount": 300000,
                        "status": True,
                    },
                    {
                        "service_id": "sbp",
                        "service_name": "СБП",
                        "percent": 3,
                        "currency": "RUB",
                        "min_amount": 10,
                        "max_amount": 600000,
                        "status": True,
                    },
                ]
            )
        return 404, _err("Not Found", 404)

    def _create(self, body: dict[str, Any]) -> tuple[int, bytes]:
        amount = body.get("sum")
        if isinstance(amount, bool) or not isinstance(amount, int | Decimal):
            return 422, _err({"sum": ["The sum must be a number."]}, 422)
        if not Decimal(1) <= Decimal(amount) <= Decimal(2_000_000):
            return 422, _err({"sum": ["The sum must be between 1 and 2000000."]}, 422)
        if not isinstance(body.get("orderId"), str) or not body["orderId"]:
            return 422, _err({"orderId": ["The order id field is required."]}, 422)
        if any(i.order_id == body["orderId"] for i in self.invoices.values()):
            return 422, _err({"orderId": ["The order id has already been taken."]}, 422)
        expire = body.get("expire", 300)
        if not isinstance(expire, int) or not 1 <= expire <= 7200:
            return 422, _err({"expire": ["The expire must be between 1 and 7200."]}, 422)
        self.created.append(body)
        invoice_id = f"7e1a0c3b-0000-4000-8000-{next(self._seq):012d}"
        invoice = Invoice(
            id=invoice_id, order_id=body["orderId"], amount=Decimal(amount), expire=expire, body=body
        )
        self.invoices[invoice_id] = invoice
        return 200, _ok(
            {
                "id": invoice_id,
                "amount": float(amount),
                "expired": "2026-10-02 17:00:00",
                "status": "created",
                "shop_id": self.shop_id,
                "url": f"https://pay.lava.fake/invoice/{invoice_id}",
                "comment": body.get("comment"),
                "merchantName": "SvBG Test",
                "include_service": body.get("includeService") or [],
                "exclude_service": None,
            }
        )

    # ------------------------------------------------------------------------------------- server

    async def start(self) -> None:
        app = web.Application()

        async def any_route(request: web.Request) -> web.Response:
            raw = await request.read()
            status, payload = self.handle(request.method, request.path, dict(request.headers), raw)
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

    async def __aenter__(self) -> FakeLava:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # ------------------------------------------------------------------------------ desk-side actions

    def invoice_for(self, order_id: str) -> Invoice:
        for invoice in self.invoices.values():
            if invoice.order_id == order_id:
                return invoice
        raise KeyError(order_id)

    def webhook_data(self, invoice: Invoice, status: str = "success", **extra: Any) -> dict[str, Any]:
        """The webhook body in the order of the SDK's test (S2), numbers as strings."""
        data: dict[str, Any] = {
            "invoice_id": invoice.id,
            "status": status,
            "pay_time": "2026-10-02 12:00:00",
            "amount": f"{invoice.amount:.2f}",
            "order_id": invoice.order_id,
            "pay_service": "card",
            "payer_details": "553691******8079",
            "custom_fields": invoice.custom_fields,
            "type": 1,
            "credited": f"{invoice.amount * Decimal('0.96'):.2f}",
        }
        data.update(extra)
        return data

    def sign(
        self,
        data: Mapping[str, Any],
        *,
        mode: str = "sdk",
        header: str = "Authorization",
        key: str | None = None,
    ) -> WebhookRequest:
        """A webhook request: ``mode="sdk"`` signs the PHP-canonical form, ``"raw"`` the body bytes."""
        body = json.dumps(dict(data), ensure_ascii=False).encode()
        signed = sdk_canonical(data) if mode == "sdk" else body
        return WebhookRequest(
            body=body,
            headers={
                "Content-Type": "application/json",
                header: hmac_hex(key or self.additional_key, signed),
            },
        )

    def webhook(self, invoice: Invoice, status: str = "success", **kw: Any) -> WebhookRequest:
        return self.sign(self.webhook_data(invoice, status), **kw)

    async def send(self, url: str, req: WebhookRequest) -> int:
        headers = {k: v for k, v in req.headers.items()}  # type: ignore[union-attr]
        async with (
            ClientSession(timeout=ClientTimeout(total=10)) as session,
            session.post(url, data=req.body, headers=headers) as resp,
        ):
            await resp.read()
            return resp.status


def _ok(data: Any) -> bytes:
    return json.dumps({"data": data, "status": 200, "status_check": True}, ensure_ascii=False).encode()


def _err(error: Any, status: int) -> bytes:
    payload = {"data": None, "error": error, "status": status, "status_check": False}
    return json.dumps(payload, ensure_ascii=False).encode()
