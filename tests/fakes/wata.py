"""Fake WATA H2H API for tests — built from our own specification (``docs/providers/wata.md``).

* as the responder of :class:`~svbg.payments.testkit.CountingHttp` (``CountingHttp(fake)``): no sockets;
* as a real aiohttp server (``async with FakeWata() as desk: desk.base_url``).

Realistic where the plugin depends on it: ``Authorization: Bearer`` (401 otherwise; ``/public-key`` needs no
auth), ``POST /links`` → link object, ``GET /transactions/?orderId=`` → ``{items, totalCount}``, the GET rate
limit (one GET per 30 s per object → 429, switchable), webhooks signed with RSA-SHA512 PKCS#1 v1.5 over the
raw body by an independent implementation (``cryptography``), separate live and sandbox key pairs and key
rotation. Fault injection: HTTP status of the next answers.
"""

from __future__ import annotations

import base64
import itertools
import json
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import cache
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from aiohttp import ClientSession, ClientTimeout, web
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from svbg.payments.testkit import HttpCall
from svbg.sdk import HttpResponse, WebhookRequest

API_TOKEN = "wata-jwt-live-0123456789abcdef"
SANDBOX_TOKEN = "wata-jwt-sandbox-0123456789abcdef"
BASE_PATH = "/api/h2h"
FAKE_BASE_URL = "https://wata.fake/api/h2h"


@cache
def keypair(name: str = "live", bits: int = 2048) -> rsa.RSAPrivateKey:
    """A deterministic-per-process RSA key (``live``, ``sandbox``, ``rotated``, ``attacker``)."""
    return rsa.generate_private_key(public_exponent=65537, key_size=bits)


def public_pem(name: str = "live", fmt: str = "spki") -> str:
    pub = keypair(name).public_key()
    if fmt == "pkcs1":
        return pub.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.PKCS1).decode()
    return pub.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()


def rsa_sign(body: bytes, name: str = "live") -> str:
    """Independent implementation of the WATA webhook signature (do not import the plugin's)."""
    signature = keypair(name).sign(body, padding.PKCS1v15(), hashes.SHA512())
    return base64.b64encode(signature).decode()


def iso(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


@dataclass
class Link:
    id: str
    order_id: str
    amount: float
    currency: str
    description: str = ""
    status: str = "Opened"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime = field(default_factory=lambda: datetime.now(UTC) + timedelta(days=3))
    body: dict[str, Any] = field(default_factory=dict)

    def as_json(self, base: str) -> dict[str, Any]:
        return {
            "id": self.id,
            "url": f"https://payment.wata.fake/pay-form/{self.id}",
            "status": self.status,
            "amount": self.amount,
            "currency": self.currency,
            "type": "OneTime",
            "terminalName": "svbg",
            "terminalPublicId": "0b5f3f4e-0000-4000-8000-000000000001",
            "creationTime": iso(self.created_at),
            "expirationDateTime": iso(self.expires_at),
            "orderId": self.order_id,
            "description": self.description,
        }


@dataclass
class Transaction:
    id: str
    link_id: str
    order_id: str | None
    amount: float
    currency: str
    status: str = "Created"
    kind: str = "Payment"
    original_id: str | None = None
    payment_time: datetime | None = None
    tx_type: str = "SBP"
    error_code: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "terminalPublicId": "0b5f3f4e-0000-4000-8000-000000000001",
            "type": self.tx_type,
            "kind": self.kind,
            "status": self.status,
            "amount": self.amount,
            "currency": self.currency,
            "orderId": self.order_id,
            "errorCode": self.error_code,
            "errorDescription": None,
            "creationTime": iso(datetime.now(UTC)),
            "paymentTime": iso(self.payment_time) if self.payment_time else None,
            "totalCommission": 0,
            "originalTransactionId": self.original_id,
            "sbpLink": None,
            "paymentLinkId": self.link_id,
            "payerData": {"payerId": "*456789"},
        }

    def webhook_payload(self) -> dict[str, Any]:
        data = {
            "transactionType": self.tx_type,
            "kind": self.kind,
            "id": self.link_id,
            "transactionId": self.id,
            "originalTransactionId": self.original_id,
            "transactionStatus": self.status,
            "terminalPublicId": "0b5f3f4e-0000-4000-8000-000000000001",
            "terminalName": "svbg",
            "errorCode": self.error_code,
            "errorDescription": None,
            "amount": self.amount,
            "currency": self.currency,
            "orderId": self.order_id,
            "orderDescription": "Оплата",
            "commission": 0,
            "paymentTime": iso(self.payment_time) if self.payment_time else None,
            "email": None,
            "paymentLinkId": self.link_id,
            "payerData": {"payerId": "*456789"},
        }
        if self.order_id is None:
            data.pop("orderId")
        return data


class FakeWata:
    """See module docstring."""

    def __init__(
        self,
        *,
        token: str = API_TOKEN,
        key_name: str = "live",
        base_url: str = FAKE_BASE_URL,
        rate_limit: bool = False,
    ) -> None:
        self.token = token
        self.key_name = key_name
        self._base_url = base_url.rstrip("/")
        self.rate_limit = rate_limit
        self.links: dict[str, Link] = {}
        self.transactions: list[Transaction] = []
        self.created: list[dict[str, Any]] = []
        self.requests: list[tuple[str, str, dict[str, str], dict[str, str]]] = []
        self.public_key_calls = 0
        self.status_calls: list[str] = []
        self.fail_with: int | None = None
        self.fail_public_key: int | None = None
        self._last_get: dict[str, float] = {}
        self._seq = itertools.count(1)
        self._runner: web.AppRunner | None = None
        self._port: int | None = None

    # ------------------------------------------------------------------------------------- transport

    @property
    def base_url(self) -> str:
        if self._port is not None:
            return f"http://127.0.0.1:{self._port}{BASE_PATH}"
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
            return self.fail_with, b'{"error":"fault"}'
        if not path.startswith(BASE_PATH + "/"):
            return 404, b"<html>not found</html>"
        rest = path[len(BASE_PATH) :].rstrip("/")
        if method == "GET" and rest == "/public-key":
            self.public_key_calls += 1
            if self.fail_public_key is not None:
                return self.fail_public_key, _j({"error": "fault"})
            return 200, _j({"value": public_pem(self.key_name)})
        if low.get("authorization") != f"Bearer {self.token}":
            return 401, _j({"error": {"code": "Unauthorized", "message": "invalid token"}})
        if method == "GET" and self.rate_limit:
            obj = f"{rest}?{query.get('orderId', '')}"
            now = time.monotonic()
            if now - self._last_get.get(obj, -1e9) < 30:
                return 429, _j({"error": {"message": "Too many requests. Retry after 30 s"}})
            self._last_get[obj] = now
        if method == "POST" and rest == "/links":
            return self._create(body)
        if method == "GET" and rest == "/links":
            items = [link.as_json(self.base_url) for link in self.links.values()]
            limit = int(query.get("maxResultCount", "10"))
            return 200, _j({"items": items[:limit], "totalCount": len(items)})
        if method == "GET" and rest == "/transactions":
            return self._transactions(query)
        return 404, _j({"error": "not found"})

    def _create(self, body: Any) -> tuple[int, bytes]:
        if not isinstance(body, dict):
            return 400, _j({"error": "json body required"})
        amount = body.get("amount")
        if not isinstance(amount, int | float) or isinstance(amount, bool):
            return 400, _j({"error": {"code": "Payment:PL_1001", "message": "amount must be a number"}})
        if body.get("currency") not in ("RUB", "USD", "EUR"):
            return 400, _j({"error": {"message": "bad currency"}})
        self.created.append(dict(body))
        link_id = str(uuid.uuid4())
        link = Link(
            id=link_id,
            order_id=str(body.get("orderId") or ""),
            amount=float(amount),
            currency=str(body["currency"]),
            description=str(body.get("description") or ""),
            body=dict(body),
        )
        self.links[link_id] = link
        return 200, _j(link.as_json(self.base_url))

    def _transactions(self, query: Mapping[str, str]) -> tuple[int, bytes]:
        order = query.get("orderId")
        self.status_calls.append(order or "")
        items = [t.as_json() for t in self.transactions if order is None or t.order_id == order]
        return 200, _j({"items": items, "totalCount": len(items)})

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

    async def __aenter__(self) -> FakeWata:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # ------------------------------------------------------------------------------ desk-side actions

    def link_for(self, order_id: str) -> Link:
        for link in self.links.values():
            if link.order_id == order_id:
                return link
        raise KeyError(order_id)

    def pay(
        self, order_id: str, *, status: str = "Paid", amount: float | None = None, tx_type: str = "SBP"
    ) -> Transaction:
        """A payment attempt on the link of ``order_id``."""
        link = self.link_for(order_id)
        tx = Transaction(
            id=str(uuid.uuid4()),
            link_id=link.id,
            order_id=order_id,
            amount=link.amount if amount is None else amount,
            currency=link.currency,
            status=status,
            tx_type=tx_type,
            payment_time=datetime.now(UTC) if status == "Paid" else None,
            error_code="Payment:TRA_2002" if status == "Declined" else None,
        )
        self.transactions.append(tx)
        if status == "Paid":
            link.status = "Closed"
        return tx

    def refund(self, original: Transaction, *, with_order: bool = True) -> Transaction:
        tx = Transaction(
            id=str(uuid.uuid4()),
            link_id=original.link_id,
            order_id=original.order_id if with_order else None,
            amount=original.amount,
            currency=original.currency,
            status="Paid",
            kind="Refund",
            original_id=original.id,
        )
        self.transactions.append(tx)
        return tx

    # ------------------------------------------------------------------------------------- webhooks

    def webhook_parts(
        self, tx: Transaction, *, key_name: str | None = None, extra: Mapping[str, Any] | None = None
    ) -> tuple[bytes, dict[str, str]]:
        payload = tx.webhook_payload()
        payload.update(extra or {})
        body = json.dumps(payload, ensure_ascii=False).encode()
        return body, {
            "Content-Type": "application/json",
            "X-Signature": rsa_sign(body, key_name or self.key_name),
        }

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


def _j(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False).encode()
