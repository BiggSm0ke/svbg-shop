"""Fake Platega API for tests — built from our specification (``docs/providers/platega.md``).

* as the responder of :class:`~svbg.payments.testkit.CountingHttp` (``CountingHttp(fake)``): no sockets;
* as a real aiohttp server (``async with FakePlatega() as desk: desk.base_url``).

Realistic where the plugin depends on it: ``X-MerchantId`` + ``X-Secret`` (401 otherwise),
``POST /transaction/process`` (needs ``paymentMethod``; answers ``redirect``) and
``POST /v2/transaction/process``
(answers ``url``), both with ``transactionId``, ``status: PENDING``, ``expiresIn: "00:15:00"``;
``GET /transaction/{id}`` (404 for unknown ids) with ``paymentDetails.amount`` as a JSON number; statuses
``PENDING|CONFIRMED|CANCELED|CHARGEBACKED``; callbacks with the static header pair.
"""

from __future__ import annotations

import hmac
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlsplit

from aiohttp import web

from svbg.payments.testkit import HttpCall
from svbg.sdk import HttpResponse, WebhookRequest

MERCHANT_ID = "1a021d91-9b26-4762-b303-5d4aac74e921"
API_SECRET = "pl-secret-0123456789abcdef0123456789"
FAKE_BASE_URL = "https://platega.fake"
_CODES = {2: "SBPQR", 3: "ERIP", 11: "CARD", 12: "INTERNATIONAL", 13: "CRYPTO", 14: "SBERPAY"}


@dataclass
class Transaction:
    id: str
    amount: Any  # as Platega reports it: a JSON number
    currency: str = "RUB"
    status: str = "PENDING"
    payload: str | None = None
    method: int | None = None
    body: dict[str, Any] = field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "paymentDetails": {"amount": self.amount, "currency": self.currency},
            "merchantName": "Demo Merchant",
            "mechantId": MERCHANT_ID,
            "comission": 0,
            "paymentMethod": _CODES.get(self.method or 2, "SBPQR"),
            "expiresIn": "00:15:00",
            "payload": self.payload,
            "description": self.body.get("description"),
        }


class FakePlatega:
    """See module docstring."""

    def __init__(
        self, *, merchant_id: str = MERCHANT_ID, api_secret: str = API_SECRET, base_url: str = FAKE_BASE_URL
    ) -> None:
        self.merchant_id = merchant_id
        self.api_secret = api_secret
        self._base_url = base_url.rstrip("/")
        self.transactions: dict[str, Transaction] = {}
        self.created: list[tuple[str, dict[str, Any]]] = []
        self.status_calls: list[str] = []
        self.fail_with: int | None = None
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
            return self.fail_with, b'{"message":"fault"}'
        if not (
            hmac.compare_digest(low.get("x-merchantid", ""), self.merchant_id)
            and hmac.compare_digest(low.get("x-secret", ""), self.api_secret)
        ):
            return 401, _j({"message": "Unauthorized"})
        method = method.upper()
        if method == "POST" and path in ("/transaction/process", "/v2/transaction/process"):
            return self._create(path, body)
        if method == "GET" and path.startswith("/transaction/"):
            return self._get(unquote(path[len("/transaction/") :]))
        return 404, _j({"message": "not found"})

    def _create(self, path: str, body: Any) -> tuple[int, bytes]:
        if not isinstance(body, dict):
            return 400, _j({"message": "json body required"})
        details = body.get("paymentDetails")
        if not isinstance(details, dict) or not isinstance(details.get("amount"), int | float):
            return 400, _j({"message": "paymentDetails.amount must be a number"})
        for key in ("description", "return", "failedUrl"):
            if not body.get(key):
                return 400, _j({"message": f"{key} is required"})
        v1 = path == "/transaction/process"
        if v1 and body.get("paymentMethod") not in _CODES:
            return 400, _j({"message": "paymentMethod is required"})
        self.created.append((path, dict(body)))
        tx = Transaction(
            id=str(uuid.uuid4()),
            amount=details["amount"],
            currency=str(details.get("currency") or "RUB"),
            payload=body.get("payload"),
            method=body.get("paymentMethod"),
            body=dict(body),
        )
        self.transactions[tx.id] = tx
        answer: dict[str, Any] = {"transactionId": tx.id, "status": "PENDING", "expiresIn": "00:15:00"}
        if v1:
            answer |= {
                "redirect": f"https://pay.platega.fake/?id={tx.id}",
                "paymentMethod": _CODES[tx.method or 2],
            }
        else:
            answer |= {"url": f"https://pay.platega.fake/?id={tx.id}&mh=x", "rate": 91.2}
        return 200, _j(answer)

    def _get(self, tx_id: str) -> tuple[int, bytes]:
        self.status_calls.append(tx_id)
        tx = self.transactions.get(tx_id)
        if tx is None:
            return 404, _j({"message": "Транзакция не найдена"})
        return 200, _j(tx.as_json())

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

    async def __aenter__(self) -> FakePlatega:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # ------------------------------------------------------------------------------ desk-side actions

    def add(self, tx_id: str, payload: str | None, amount: Any = 179, status: str = "PENDING") -> Transaction:
        tx = Transaction(id=tx_id, amount=amount, payload=payload, status=status)
        self.transactions[tx_id] = tx
        return tx

    def set_status(self, tx_id: str, status: str, *, amount: Any = None) -> Transaction:
        tx = self.transactions[tx_id]
        tx.status = status
        if amount is not None:
            tx.amount = amount
        return tx

    def callback(
        self,
        tx_id: str,
        status: str | None = None,
        *,
        amount: Any = None,
        secret: str | None = None,
        merchant_id: str | None = None,
    ) -> WebhookRequest:
        tx = self.transactions[tx_id]
        payload = {
            "id": tx.id,
            "amount": tx.amount if amount is None else amount,
            "currency": tx.currency,
            "status": status or tx.status,
            "paymentMethod": tx.method or 2,
            "payload": tx.payload,
        }
        headers = {
            "Content-Type": "application/json",
            "X-MerchantId": merchant_id or self.merchant_id,
            "X-Secret": secret or self.api_secret,
        }
        return WebhookRequest(body=json.dumps(payload).encode(), headers=headers)


def _j(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False).encode()
