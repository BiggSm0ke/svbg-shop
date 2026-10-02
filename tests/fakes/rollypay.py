"""Fake RollyPay API for tests — built from our own specification (``docs/providers/rollypay.md``).

Two ways to use it:

* as the responder of :class:`~svbg.payments.testkit.CountingHttp` (``CountingHttp(fake)``): no sockets;
* as a real aiohttp server (``async with FakeRollyPay() as desk: desk.base_url``) for end-to-end tests through
  :class:`~svbg.payments.registry.InstanceHttp`.

Realistic where the plugin depends on it: ``X-API-Key`` (401 otherwise), ``X-Nonce`` must be a fresh UUID (a
reused nonce → 409), ``POST /payments`` → ``{payment_id, pay_url, expires_at}``, ``GET /payments/{id}`` (404
for unknown ids), statuses ``created|processing|paid|expired|canceled|chargeback|refunded`` with a late
``expired → paid``, signed webhooks ``X-Signature = hex(HMAC_SHA256(secret, X-Timestamp + "." + body))`` with
the timestamp in seconds, milliseconds or ISO 8601 and ``X-Test-Mode``. Fault injection: HTTP status of the
next answers, latency.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import itertools
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

from aiohttp import ClientSession, ClientTimeout, web

from svbg.payments.testkit import HttpCall
from svbg.sdk import HttpResponse, WebhookRequest

API_KEY = "rp-live-key-0123456789abcdef"
SIGNING_SECRET = "rp-whsec-0123456789abcdef0123"
BASE_PATH = "/api/v1"
FAKE_BASE_URL = "https://rollypay.fake/api/v1"
TimestampFormat = Literal["seconds", "ms", "iso", "iso_naive", "iso_z"]


def sign(secret: str, timestamp: str, body: bytes) -> str:
    """Independent implementation of the RollyPay webhook signature (do not import the plugin's)."""
    return hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def format_timestamp(at: datetime, fmt: TimestampFormat = "seconds") -> str:
    at = at.astimezone(UTC)
    if fmt == "seconds":
        return str(int(at.timestamp()))
    if fmt == "ms":
        return str(int(at.timestamp() * 1000))
    if fmt == "iso":
        return at.isoformat()
    if fmt == "iso_z":
        return at.strftime("%Y-%m-%dT%H:%M:%S.") + f"{at.microsecond // 1000:03d}Z"
    return at.replace(tzinfo=None).isoformat()


@dataclass
class Invoice:
    payment_id: str
    order_id: str
    amount: str
    currency: str
    status: str = "created"
    test: bool = False
    paid_at: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    body: dict[str, Any] = field(default_factory=dict)

    def as_json(self, *, paid_at_format: TimestampFormat | None = "iso_z") -> dict[str, Any]:
        data: dict[str, Any] = {
            "payment_id": self.payment_id,
            "order_id": self.order_id,
            "status": self.status,
            "amount": self.amount,
            "currency": self.currency,
            "test": self.test,
            "created_at": format_timestamp(self.created_at, "iso_z"),
            "updated_at": format_timestamp(datetime.now(UTC), "iso_z"),
        }
        if self.paid_at is not None and paid_at_format is not None:
            data["paid_at"] = format_timestamp(self.paid_at, paid_at_format)
        return data


class FakeRollyPay:
    """See module docstring."""

    def __init__(
        self,
        *,
        api_key: str = API_KEY,
        signing_secret: str = SIGNING_SECRET,
        base_url: str = FAKE_BASE_URL,
    ) -> None:
        self.api_key = api_key
        self.signing_secret = signing_secret
        self._base_url = base_url.rstrip("/")
        self.invoices: dict[str, Invoice] = {}
        self.created: list[dict[str, Any]] = []
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.nonces: set[str] = set()
        self.status_calls: list[str] = []
        self.fail_with: int | None = None  # HTTP status for every API answer while set
        self.fail_create: int | None = None
        self.fail_status: int | None = None
        self.delay: float = 0.0
        self.paid_at_format: TimestampFormat | None = "iso_z"
        self._seq = itertools.count(1)
        self._runner: web.AppRunner | None = None
        self._port: int | None = None

    # ------------------------------------------------------------------------------------- transport

    @property
    def base_url(self) -> str:
        """Base URL of the API (the server's when started)."""
        if self._port is not None:
            return f"http://127.0.0.1:{self._port}{BASE_PATH}"
        return self._base_url

    async def __call__(self, call: HttpCall) -> HttpResponse:
        """:class:`~svbg.payments.testkit.CountingHttp` responder."""
        if self.delay:
            await asyncio.sleep(self.delay)
        status, payload = self.handle(call.method, urlsplit(call.url).path, call.headers, call.json)
        return HttpResponse(status, payload, {"Content-Type": "application/json"})

    def handle(self, method: str, path: str, headers: Mapping[str, str], body: Any) -> tuple[int, bytes]:
        low = {k.lower(): v for k, v in headers.items()}
        self.requests.append((method.upper(), path, low))
        if self.fail_with is not None:
            return self.fail_with, b'{"error":"fault"}'
        if not path.startswith(BASE_PATH + "/"):
            return 404, b"<html>not found</html>"
        if not hmac.compare_digest(low.get("x-api-key", ""), self.api_key):
            return 401, _j({"error": "invalid api key"})
        nonce = low.get("x-nonce", "")
        try:
            uuid.UUID(nonce)
        except ValueError:
            return 400, _j({"error": "X-Nonce required"})
        if nonce in self.nonces:
            return 409, _j({"error": "nonce reused"})
        self.nonces.add(nonce)
        rest = path[len(BASE_PATH) :]
        if method.upper() == "POST" and rest == "/payments":
            return self._create(body)
        if method.upper() == "GET" and rest.startswith("/payments/"):
            return self._get(unquote(rest[len("/payments/") :]))
        return 404, _j({"error": "not found"})

    def _create(self, body: Any) -> tuple[int, bytes]:
        if self.fail_create is not None:
            return self.fail_create, _j({"error": "fault"})
        if not isinstance(body, dict):
            return 400, _j({"error": "json body required"})
        missing = [k for k in ("amount", "payment_currency", "order_id") if not body.get(k)]
        if missing:
            return 422, _j({"error": f"missing {missing}"})
        self.created.append(dict(body))
        ext = f"rp_{next(self._seq):06d}"
        inv = Invoice(
            payment_id=ext,
            order_id=str(body["order_id"]),
            amount=str(body["amount"]),
            currency=str(body["payment_currency"]),
            test=bool(body.get("test")),
            body=dict(body),
        )
        self.invoices[ext] = inv
        expires = (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
        return 200, _j(
            {"payment_id": ext, "pay_url": f"https://pay.rollypay.fake/{ext}", "expires_at": expires}
        )

    def _get(self, ext: str) -> tuple[int, bytes]:
        self.status_calls.append(ext)
        if self.fail_status is not None:
            return self.fail_status, _j({"error": "fault"})
        inv = self.invoices.get(ext)
        if inv is None:
            return 404, _j({"error": "payment not found"})
        return 200, _j(inv.as_json(paid_at_format=self.paid_at_format))

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

    async def __aenter__(self) -> FakeRollyPay:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # ------------------------------------------------------------------------------ desk-side actions

    def invoice_for(self, order_id: str) -> Invoice:
        for inv in self.invoices.values():
            if inv.order_id == order_id:
                return inv
        raise KeyError(order_id)

    def set_status(self, ext: str, status: str, *, amount: str | None = None) -> Invoice:
        inv = self.invoices[ext]
        inv.status = status
        if amount is not None:
            inv.amount = amount
        if status == "paid" and inv.paid_at is None:
            inv.paid_at = datetime.now(UTC)
        return inv

    def add_invoice(
        self, ext: str, order_id: str, amount: str = "179.00", currency: str = "RUB", status: str = "created"
    ) -> Invoice:
        inv = Invoice(payment_id=ext, order_id=order_id, amount=amount, currency=currency, status=status)
        self.invoices[ext] = inv
        return inv

    # ------------------------------------------------------------------------------------- webhooks

    def webhook_parts(
        self,
        ext: str,
        status: str | None = None,
        *,
        amount: str | None = None,
        at: datetime | None = None,
        ts_format: TimestampFormat = "seconds",
        test: bool | None = None,
        test_header: bool = False,
        secret: str | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> tuple[bytes, dict[str, str]]:
        """A webhook for invoice ``ext`` (its current state unless overridden): ``(body, headers)``."""
        inv = self.invoices[ext]
        payload: dict[str, Any] = {
            "event": "payment.status",
            "payment_id": inv.payment_id,
            "order_id": inv.order_id,
            "status": status or inv.status,
            "amount": amount if amount is not None else inv.amount,
            "currency": inv.currency,
            "test": inv.test if test is None else test,
            "metadata": {"payment_id": inv.order_id},
        }
        if payload["status"] == "paid":
            payload["paid_at"] = format_timestamp(inv.paid_at or at or datetime.now(UTC), "iso_z")
        payload.update(extra or {})
        body = json.dumps(payload, separators=(",", ":")).encode()
        stamp = format_timestamp(at or datetime.now(UTC), ts_format)
        headers = {
            "Content-Type": "application/json",
            "X-Timestamp": stamp,
            "X-Signature": sign(secret or self.signing_secret, stamp, body),
        }
        if test_header:
            headers["X-Test-Mode"] = "true"
        return body, headers

    def webhook(self, ext: str, status: str | None = None, **kw: Any) -> WebhookRequest:
        body, headers = self.webhook_parts(ext, status, **kw)
        return WebhookRequest(body=body, headers=headers)

    async def send_webhook(self, url: str, ext: str, status: str | None = None, **kw: Any) -> int:
        """POST a signed webhook to ``url`` (the bot's ``/webhooks/pay/{id}/{token}``); returns the status."""
        body, headers = self.webhook_parts(ext, status, **kw)
        async with (
            ClientSession(timeout=ClientTimeout(total=10)) as session,
            session.post(url, data=body, headers=headers) as resp,
        ):
            await resp.read()
            return resp.status


def _j(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False).encode()
