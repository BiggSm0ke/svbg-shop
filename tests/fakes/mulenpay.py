"""Fake MulenPay API for tests — built from our own specification (``docs/providers/mulenpay.md``).

* as the responder of :class:`~svbg.payments.testkit.CountingHttp` (``CountingHttp(fake)``): no sockets;
* as a real aiohttp server (``async with FakeMulenPay() as desk: desk.base_url``).

Realistic where the plugin depends on it: ``Authorization: Bearer`` (401 otherwise), ``POST /v2/payments``
validates ``sign = sha1(currency + amount + shopId + secret)`` (independent implementation) and the receipt
items, answers ``201 {success, paymentUrl, id}``; ``GET /v2/payments/{id}`` → ``{success, payment}`` with the
numeric status (404 for unknown ids); ``GET /v2/shops/{id}/balances`` (403 for a foreign shop); callbacks
``{id, amount, currency, uuid, payment_status}`` — unsigned, or with an optional ``sign``. Fault injection:
HTTP status of the next answers.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlsplit

from aiohttp import ClientSession, ClientTimeout, web

from svbg.payments.testkit import HttpCall
from svbg.sdk import HttpResponse, WebhookRequest

API_KEY = "mulen-api-key-0123456789abcdef"
SECRET_KEY = "mulen-secret-0123456789abcdef"
SHOP_ID = 42
BASE_PATH = "/api"
FAKE_BASE_URL = "https://mulenpay.fake/api"
STATUS = {"created": 0, "processing": 1, "canceled": 2, "paid": 3, "error": 4, "hold": 5}


def sign(currency: str, amount: str, shop_id: int | str, secret: str) -> str:
    """Independent implementation of the documented MulenPay signature (do not import the plugin's)."""
    return hashlib.sha1((currency + amount + str(shop_id) + secret).encode()).hexdigest()


@dataclass
class Payment:
    id: int
    uuid: str
    amount: str
    currency: str
    description: str = ""
    status: int = 0
    body: dict[str, Any] = field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "uuid": self.uuid,
            "amount": self.amount,
            "currency": self.currency,
            "description": self.description,
            "status": self.status,
        }


class FakeMulenPay:
    """See module docstring."""

    def __init__(
        self,
        *,
        api_key: str = API_KEY,
        secret_key: str = SECRET_KEY,
        shop_id: int = SHOP_ID,
        base_url: str = FAKE_BASE_URL,
    ) -> None:
        self.api_key = api_key
        self.secret_key = secret_key
        self.shop_id = shop_id
        self._base_url = base_url.rstrip("/")
        self.payments: dict[int, Payment] = {}
        self.created: list[dict[str, Any]] = []
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.status_calls: list[str] = []
        self.fail_with: int | None = None
        self._seq = itertools.count(1001)
        self._runner: web.AppRunner | None = None
        self._port: int | None = None

    @property
    def base_url(self) -> str:
        if self._port is not None:
            return f"http://127.0.0.1:{self._port}{BASE_PATH}"
        return self._base_url

    async def __call__(self, call: HttpCall) -> HttpResponse:
        status, payload = self.handle(call.method, urlsplit(call.url).path, call.headers, call.json)
        return HttpResponse(status, payload, {"Content-Type": "application/json"})

    def handle(self, method: str, path: str, headers: Mapping[str, str], body: Any) -> tuple[int, bytes]:
        low = {k.lower(): v for k, v in headers.items()}
        method = method.upper()
        self.requests.append((method, path, low))
        if self.fail_with is not None:
            return self.fail_with, b'{"error":"fault"}'
        if not path.startswith(BASE_PATH + "/"):
            return 404, b"<html>not found</html>"
        if low.get("authorization") != f"Bearer {self.api_key}":
            return 401, _j({"error": "unauthorized", "status": 401})
        rest = path[len(BASE_PATH) :]
        if method == "POST" and rest == "/v2/payments":
            return self._create(body)
        if method == "GET" and rest.startswith("/v2/payments/"):
            return self._get(unquote(rest[len("/v2/payments/") :]))
        if method == "GET" and rest.startswith("/v2/shops/") and rest.endswith("/balances"):
            shop = rest[len("/v2/shops/") : -len("/balances")]
            if shop != str(self.shop_id):
                return 403, _j({"success": False, "message": "Магазин не найден"})
            return 200, _j({"success": True, "data": {"balances": [{"currency": "rub", "amount": "0"}]}})
        return 404, _j({"error": "not found"})

    def _create(self, body: Any) -> tuple[int, bytes]:
        if not isinstance(body, dict):
            return 422, _j({"error": "param is missing or the value is empty", "status": 422})
        for key in ("currency", "amount", "uuid", "shopId", "description", "sign", "items"):
            if body.get(key) in (None, "", []):
                return 422, _j({"error": f"param is missing or the value is empty: {key}", "status": 422})
        if body["shopId"] != self.shop_id:
            return 403, _j({"success": False, "message": "Магазин не найден"})
        expected = sign(str(body["currency"]), str(body["amount"]), body["shopId"], self.secret_key)
        if body["sign"] != expected:
            return 400, _j({"success": False, "error": "invalid sign"})
        for item in body["items"]:
            for key in ("description", "price", "quantity", "vat_code", "payment_subject", "payment_mode"):
                if key not in item:
                    return 422, _j({"error": f"items.{key} is missing", "status": 422})
        self.created.append(dict(body))
        pid = next(self._seq)
        self.payments[pid] = Payment(
            id=pid,
            uuid=str(body["uuid"]),
            amount=str(body["amount"]),
            currency=str(body["currency"]),
            description=str(body["description"]),
            body=dict(body),
        )
        return 201, _j({"success": True, "paymentUrl": f"https://mulenpay.fake/payment/{pid}", "id": pid})

    def _get(self, raw_id: str) -> tuple[int, bytes]:
        self.status_calls.append(raw_id)
        if not raw_id.isdigit() or int(raw_id) not in self.payments:
            return 404, _j({"success": False, "error": "not found"})
        return 200, _j({"success": True, "payment": self.payments[int(raw_id)].as_json()})

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

    async def __aenter__(self) -> FakeMulenPay:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # ------------------------------------------------------------------------------ desk-side actions

    def payment_for(self, uuid: str) -> Payment:
        for payment in self.payments.values():
            if payment.uuid == uuid:
                return payment
        raise KeyError(uuid)

    def set_status(self, pid: int, status: str | int, *, amount: str | None = None) -> Payment:
        payment = self.payments[pid]
        payment.status = STATUS[status] if isinstance(status, str) else status
        if amount is not None:
            payment.amount = amount
        return payment

    def callback_body(
        self,
        pid: int,
        payment_status: str = "success",
        *,
        amount: Any = None,
        signed: bool = False,
        secret: str | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> bytes:
        payment = self.payments[pid]
        value = amount if amount is not None else float(payment.amount)
        data: dict[str, Any] = {
            "id": payment.id,
            "amount": value,
            "currency": "RUB",
            "uuid": payment.uuid,
            "payment_status": payment_status,
        }
        if signed:
            data["sign"] = sign("rub", f"{float(value):.2f}", self.shop_id, secret or self.secret_key)
        data.update(extra or {})
        return json.dumps(data).encode()

    def callback(self, pid: int, payment_status: str = "success", **kw: Any) -> WebhookRequest:
        return WebhookRequest(
            body=self.callback_body(pid, payment_status, **kw), headers={"Content-Type": "application/json"}
        )

    async def send_callback(self, url: str, pid: int, payment_status: str = "success", **kw: Any) -> int:
        body = self.callback_body(pid, payment_status, **kw)
        async with (
            ClientSession(timeout=ClientTimeout(total=10)) as session,
            session.post(url, data=body, headers={"Content-Type": "application/json"}) as resp,
        ):
            await resp.read()
            return resp.status


def _j(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False).encode()
