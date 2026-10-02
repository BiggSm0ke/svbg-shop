"""Fake Cryptomus merchant API for tests — built from our specification (``docs/providers/cryptomus.md``).

Heleket speaks the same protocol: :mod:`tests.fakes.heleket` is this desk with Heleket's host and limits.

Use it as the responder of :class:`~svbg.payments.testkit.CountingHttp` or as a real aiohttp server
(``async with FakeCryptomus() as desk: desk.base_url``). Realistic where the plugin depends on it: the
``merchant`` + ``sign = md5(base64(raw_body) + key)`` headers (``401`` otherwise, an empty body signs the
empty string), the ``{"state": 0, "result": …}`` / ``{"state": 1, "message", "errors"}`` envelope, ``POST
/v1/payment`` with the documented validation (``order_id`` charset/length, ``lifetime`` 300–43200, URL
lengths), ``/v1/payment/info`` (``404`` for an unknown invoice), ``/v1/payment/services`` and webhooks encoded
the way PHP does (``json_encode(…, JSON_UNESCAPED_UNICODE)``: ``/`` → ``\\/``) with ``sign`` appended last.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
import uuid as uuidlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, ClassVar
from urllib.parse import urlsplit

from aiohttp import web

from svbg.payments.testkit import HttpCall
from svbg.sdk import HttpResponse, WebhookRequest

MERCHANT = "8b03432e-385b-4670-8d06-064591096795"
API_KEY = "uK3yP4ymentKeyCryptomusLive0123456789abcdefABCDEF"
FAKE_BASE_URL = "https://api.cryptomus.com"


def sign(key: str, body: bytes) -> str:
    """Independent implementation of the request/webhook signature."""
    return hashlib.md5(base64.b64encode(body) + key.encode()).hexdigest()


def php_encode(data: Any) -> str:
    """What PHP's ``json_encode($data, JSON_UNESCAPED_UNICODE)`` prints (the documentation's JS recipe:
    ``JSON.stringify(data).replace(/\\//g, "\\\\/")``, plus PHP's escaping of U+2028/U+2029)."""
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return text.replace("/", "\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def signed_body(key: str, data: Mapping[str, Any]) -> bytes:
    """A webhook body as the provider sends it: the payload with ``sign`` of its PHP encoding appended."""
    payload = dict(data)
    payload.pop("sign", None)
    payload["sign"] = sign(key, php_encode(payload).encode())
    return php_encode(payload).encode()


@dataclass
class Invoice:
    uuid: str
    order_id: str
    amount: str
    currency: str
    status: str = "check"
    lifetime: int = 3600
    payer_currency: str | None = None
    payment_amount: str | None = None
    txid: str | None = None
    body: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def is_final(self) -> bool:
        return self.status in {"paid", "paid_over", "wrong_amount", "fail", "cancel", "system_fail",
                               "refund_fail", "refund_paid"}  # fmt: skip

    def as_json(self, pay_host: str) -> dict[str, Any]:
        return {
            "uuid": self.uuid,
            "order_id": self.order_id,
            "amount": self.amount,
            "payment_amount": self.payment_amount,
            "payer_amount": None,
            "discount_percent": None,
            "discount": "0.00000000",
            "payer_currency": self.payer_currency,
            "currency": self.currency,
            "merchant_amount": None,
            "network": "tron" if self.payer_currency else None,
            "address": None,
            "from": None,
            "txid": self.txid,
            "payment_status": self.status,
            "url": f"https://{pay_host}/pay/{self.uuid}",
            "expired_at": int((self.created_at + timedelta(seconds=self.lifetime)).timestamp()),
            "status": self.status,
            "is_final": self.is_final,
            "additional_data": None,
            "created_at": self.created_at.isoformat(timespec="seconds"),
            "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }


class FakeCryptomus:
    """See module docstring."""

    order_id_max: ClassVar[int] = 100
    pay_host: ClassVar[str] = "pay.cryptomus.com"
    default_base_url: ClassVar[str] = FAKE_BASE_URL
    default_key: ClassVar[str] = API_KEY
    webhook_ip: ClassVar[str] = "91.227.144.54"

    def __init__(
        self, *, key: str | None = None, merchant: str = MERCHANT, base_url: str | None = None
    ) -> None:
        self.key = key or self.default_key
        self.merchant = merchant
        self._base_url = (base_url or self.default_base_url).rstrip("/")
        self.invoices: dict[str, Invoice] = {}
        self.created: list[dict[str, Any]] = []
        self.info_calls: list[dict[str, Any]] = []
        self.fail_with: int | None = None
        self.delay: float = 0.0
        self._runner: web.AppRunner | None = None
        self._port: int | None = None

    @property
    def base_url(self) -> str:
        if self._port is not None:
            return f"http://127.0.0.1:{self._port}"
        return self._base_url

    async def __call__(self, call: HttpCall) -> HttpResponse:
        if self.delay:
            await asyncio.sleep(self.delay)
        raw = call.data if isinstance(call.data, bytes) else (call.data or "").encode()
        status, payload = self.handle(call.method, urlsplit(call.url).path, call.headers, raw)
        return HttpResponse(status, payload, {"Content-Type": "application/json"})

    def handle(self, method: str, path: str, headers: Mapping[str, str], raw: bytes) -> tuple[int, bytes]:
        if self.fail_with is not None:
            return self.fail_with, b"<html>bad gateway</html>"
        low = {k.lower(): v for k, v in headers.items()}
        if method != "POST":
            return 405, _err("Method not allowed")
        if low.get("merchant") != self.merchant or not hmac.compare_digest(
            low.get("sign", ""), sign(self.key, raw)
        ):
            return 401, json.dumps({"state": 1, "message": "Unauthorized"}).encode()
        try:
            params = json.loads(raw) if raw else {}
        except ValueError:
            return 422, _err("Invalid JSON")
        if path == "/v1/payment":
            return self._create(params)
        if path == "/v1/payment/info":
            return self._info(params)
        if path == "/v1/payment/services":
            return 200, _ok([
                {"network": "tron", "currency": "USDT", "is_available": True,
                 "limit": {"min_amount": "1.00", "max_amount": "1000000.00"},
                 "commission": {"fee_amount": "0.00", "percent": "2.00"}},
                {"network": "ton", "currency": "TON", "is_available": True,
                 "limit": {"min_amount": "0.10", "max_amount": "100000.00"},
                 "commission": {"fee_amount": "0.00", "percent": "2.00"}},
                {"network": "btc", "currency": "BTC", "is_available": False,
                 "limit": {"min_amount": "0.0001", "max_amount": "10.00"},
                 "commission": {"fee_amount": "0.00", "percent": "2.00"}},
            ])  # fmt: skip
        return 404, _err("Not found")

    def _create(self, params: Mapping[str, Any]) -> tuple[int, bytes]:
        errors: dict[str, list[str]] = {}
        amount = params.get("amount")
        if not isinstance(amount, str) or not re.fullmatch(r"\d+(\.\d+)?", amount):
            errors["amount"] = ["validation.numeric"]
        if not isinstance(params.get("currency"), str) or not params["currency"]:
            errors["currency"] = ["validation.required"]
        order_id = params.get("order_id")
        if not isinstance(order_id, str) or not re.fullmatch(
            rf"[A-Za-z0-9_-]{{1,{self.order_id_max}}}", order_id
        ):
            errors["order_id"] = ["validation.regex"]
        lifetime = params.get("lifetime", 3600)
        if not isinstance(lifetime, int) or not 300 <= lifetime <= 43200:
            errors["lifetime"] = ["validation.between"]
        for name in ("url_callback", "url_return", "url_success"):
            value = params.get(name)
            if value is not None and (not isinstance(value, str) or not 6 <= len(value) <= 255):
                errors[name] = ["validation.url"]
        if errors:
            return 422, json.dumps({"state": 1, "errors": errors}).encode()
        assert isinstance(order_id, str) and isinstance(amount, str)
        self.created.append(dict(params))
        for inv in self.invoices.values():  # an order id is unique: the same invoice comes back
            if inv.order_id == order_id:
                return 200, _ok(inv.as_json(self.pay_host))
        inv = Invoice(
            uuid=str(uuidlib.uuid4()),
            order_id=order_id,
            amount=f"{Decimal(amount):.2f}",
            currency=str(params["currency"]),
            lifetime=int(lifetime),
            body=dict(params),
        )
        self.invoices[inv.uuid] = inv
        return 200, _ok(inv.as_json(self.pay_host))

    def _info(self, params: Mapping[str, Any]) -> tuple[int, bytes]:
        self.info_calls.append(dict(params))
        ref_uuid, order_id = params.get("uuid"), params.get("order_id")
        if not ref_uuid and not order_id:
            return 422, json.dumps({"state": 1, "errors": {"uuid": ["validation.required_without"]}}).encode()
        for inv in self.invoices.values():
            if (order_id and inv.order_id == order_id) or (not order_id and inv.uuid == ref_uuid):
                return 200, _ok(inv.as_json(self.pay_host))
        return 404, _err("Payment not found")

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

    async def __aenter__(self) -> FakeCryptomus:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # ------------------------------------------------------------------------------ desk-side actions

    def add_invoice(
        self, invoice_uuid: str, order_id: str, amount: str = "179.00", currency: str = "RUB"
    ) -> Invoice:
        inv = Invoice(uuid=invoice_uuid, order_id=order_id, amount=amount, currency=currency)
        self.invoices[invoice_uuid] = inv
        return inv

    def pay(
        self,
        invoice_uuid: str,
        *,
        status: str = "paid",
        payer_currency: str = "USDT",
        payment_amount: str = "1.93000000",
    ) -> Invoice:
        inv = self.invoices[invoice_uuid]
        inv.status = status
        inv.payer_currency = payer_currency
        inv.payment_amount = payment_amount
        inv.txid = hashlib.sha256(invoice_uuid.encode()).hexdigest()
        return inv

    def set_status(self, invoice_uuid: str, status: str) -> Invoice:
        inv = self.invoices[invoice_uuid]
        inv.status = status
        return inv

    # ------------------------------------------------------------------------------------- webhooks

    def webhook_data(self, invoice_uuid: str, **overrides: Any) -> dict[str, Any]:
        inv = self.invoices[invoice_uuid]
        data: dict[str, Any] = {
            "type": "payment",
            "uuid": inv.uuid,
            "order_id": inv.order_id,
            "amount": f"{Decimal(inv.amount):.8f}",
            "payment_amount": inv.payment_amount,
            "payment_amount_usd": "1.93",
            "merchant_amount": "1.89140000",
            "commission": "0.03860000",
            "is_final": inv.is_final,
            "status": inv.status,
            "from": "THgEWubVc8tPKXLJ4VZ5zbiiAK7AgqSeGH",
            "wallet_address_uuid": None,
            "network": "tron",
            "currency": inv.currency,
            "payer_currency": inv.payer_currency,
            "additional_data": None,
            "txid": inv.txid,
        }
        data.update(overrides)
        return data

    def webhook(self, invoice_uuid: str, *, key: str | None = None, **overrides: Any) -> WebhookRequest:
        body = signed_body(key or self.key, self.webhook_data(invoice_uuid, **overrides))
        return WebhookRequest(body=body, headers={"Content-Type": "application/json"}, remote=self.webhook_ip)


def _ok(result: Any) -> bytes:
    return json.dumps({"state": 0, "result": result}, ensure_ascii=False).encode()


def _err(message: str) -> bytes:
    return json.dumps({"state": 1, "message": message}).encode()
