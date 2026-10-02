"""Fake TabPay API for tests — built from our own specification (``docs/providers/tabpay.md``) only.

Two ways to use it:

* as the responder of :class:`~svbg.payments.testkit.CountingHttp` (``CountingHttp(fake)``): no sockets;
* as a real aiohttp server (``async with FakeTabPay() as desk: desk.base_url``) for end-to-end tests through
  :class:`~svbg.payments.registry.InstanceHttp`.

Realistic where the plugin depends on it: ``X-Api-Key`` (a single ``401`` otherwise),
``POST /api/v1/payments`` (``orderId`` 1–64 chars and unique per shop → ``409`` on a repeat, ``amountKopecks``
integer 100 … 10¹⁰, ``method`` must be enabled for the shop → ``409``, ``400`` with ``message`` as a list),
``GET /api/v1/payments/{id}`` (not a UUID → ``400``, unknown → ``404``), ``GET /api/v1/payments?orderId=``
(an object or ``404``), the period list, ``POST …/{id}/cancel`` (only ``CREATED``; idempotent),
``GET /api/v1/balance``, statuses ``CREATED|PENDING|SUCCESS|FAILED|EXPIRED|REFUNDED|CANCELED`` with a late
``EXPIRED → SUCCESS``, and webhooks signed with both schemes (``X-Signature-V2`` over
``X-Timestamp + "." + body``, legacy ``X-Signature`` over the body). Fault injection: HTTP status of every
answer, of create / status, and a «lost» create (the invoice is stored, the answer is a ``502``).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from aiohttp import ClientSession, ClientTimeout, web

from svbg.payments.testkit import HttpCall
from svbg.sdk import HttpResponse, WebhookRequest

API_KEY = "tp_live_0123456789abcdef0123456789"
WEBHOOK_SECRET = "whsec_tabpay_fake_0123456789"
BASE_PATH = "/api/v1"
FAKE_BASE_URL = "https://tabpay.fake/api"
_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def sign_v2(secret: str, timestamp: str, body: bytes) -> str:
    """Independent implementation of the v2 signature (do not import the plugin's)."""
    return hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def sign_v1(secret: str, body: bytes) -> str:
    """Legacy v1 signature: HMAC over the body only."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def iso(at: datetime) -> str:
    at = at.astimezone(UTC)
    return at.strftime("%Y-%m-%dT%H:%M:%S.") + f"{at.microsecond // 1000:03d}Z"


@dataclass
class Payment:
    id: str
    order_id: str
    amount_kopecks: int
    status: str = "CREATED"
    description: str | None = None
    method: str | None = None
    is_test: bool = False
    success_url: str | None = None
    fail_url: str | None = None
    paid_at: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    body: dict[str, Any] = field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "orderId": self.order_id,
            "status": self.status,
            "amountKopecks": self.amount_kopecks,
            "commissionKopecks": self.amount_kopecks * 7 // 100,
            "description": self.description,
            "method": self.method,
            "telegramId": None,
            "metadata": None,
            "successUrl": self.success_url,
            "failUrl": self.fail_url,
            "payUrl": f"https://tabpay.fake/pay/{self.id}",
            "isTest": self.is_test,
            "paidAt": iso(self.paid_at) if self.paid_at else None,
            "createdAt": iso(self.created_at),
        }


class FakeTabPay:
    """See module docstring."""

    def __init__(
        self,
        *,
        api_key: str = API_KEY,
        webhook_secret: str = WEBHOOK_SECRET,
        base_url: str = FAKE_BASE_URL,
        methods: tuple[str, ...] = ("SBP", "CARD"),
        sandbox: bool = False,
    ) -> None:
        self.api_key = api_key
        self.webhook_secret = webhook_secret
        self._base_url = base_url.rstrip("/")
        self.methods = methods
        self.sandbox = sandbox
        self.payments: dict[str, Payment] = {}
        self.created: list[dict[str, Any]] = []
        self.requests: list[tuple[str, str, dict[str, str], dict[str, str]]] = []
        self.status_calls: list[str] = []
        self.fail_with: int | None = None
        self.fail_create: int | None = None
        self.lose_create: bool = False  # store the invoice, answer 502 (the answer is lost)
        self.fail_status: int | None = None
        self._runner: web.AppRunner | None = None
        self._port: int | None = None

    # ------------------------------------------------------------------------------------- transport

    @property
    def base_url(self) -> str:
        """Base URL of the API (the server's when started); the plugin appends ``/v1/…``."""
        if self._port is not None:
            return f"http://127.0.0.1:{self._port}/api"
        return self._base_url

    async def __call__(self, call: HttpCall) -> HttpResponse:
        """:class:`~svbg.payments.testkit.CountingHttp` responder."""
        parts = urlsplit(call.url)
        query = dict(parse_qsl(parts.query))
        query.update({k: str(v) for k, v in call.params.items()})
        status, payload = self.handle(call.method, parts.path, call.headers, call.json, query)
        ctype = "text/html" if payload.startswith(b"<") else "application/json"
        return HttpResponse(status, payload, {"Content-Type": ctype})

    def handle(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: Any,
        query: Mapping[str, str] | None = None,
    ) -> tuple[int, bytes]:
        low = {k.lower(): v for k, v in headers.items()}
        q = dict(query or {})
        method = method.upper()
        self.requests.append((method, path, low, q))
        if self.fail_with is not None:
            return self.fail_with, _err(self.fail_with, "fault")
        if not path.startswith(BASE_PATH + "/"):
            return 404, b"<html>not found</html>"
        if not hmac.compare_digest(low.get("x-api-key", "").encode(), self.api_key.encode()):
            return 401, _err(401, "Unauthorized")
        rest = path[len(BASE_PATH) :]
        if method == "GET" and rest == "/balance":
            return 200, _j({"availableKopecks": 1_250_000, "frozenKopecks": 89_000, "holdHour": 12})
        if method == "POST" and rest == "/payments":
            return self._create(body)
        if method == "GET" and rest == "/payments":
            return self._list(q)
        if method == "POST" and rest.startswith("/payments/") and rest.endswith("/cancel"):
            return self._cancel(unquote(rest[len("/payments/") : -len("/cancel")]))
        if method == "GET" and rest.startswith("/payments/"):
            return self._get(unquote(rest[len("/payments/") :]))
        return 404, _err(404, "Not Found")

    def _create(self, body: Any) -> tuple[int, bytes]:
        if self.fail_create is not None:
            return self.fail_create, _err(self.fail_create, "fault")
        if not isinstance(body, dict):
            return 400, _err(400, ["body must be an object"])
        problems: list[str] = []
        order_id, amount = body.get("orderId"), body.get("amountKopecks")
        if not isinstance(order_id, str) or not 1 <= len(order_id) <= 64:
            problems.append("orderId must be 1..64 characters")
        if type(amount) is not int or not 100 <= amount <= 10_000_000_000:
            problems.append("amountKopecks must be an integer 100..10000000000")
        method = body.get("method")
        if method is not None and method not in ("SBP", "CARD"):
            problems.append("method must be one of SBP, CARD")
        description = body.get("description")
        if description is not None and (not isinstance(description, str) or len(description) > 255):
            problems.append("description must be at most 255 characters")
        for key in ("successUrl", "failUrl"):
            value = body.get(key)
            if value is not None and (
                not isinstance(value, str) or len(value) > 300 or not value.startswith(("https://", "tg://"))
            ):
                problems.append(f"{key} must be an https/t.me/tg:// URL")
        if problems:
            return 400, _err(400, problems)
        if any(p.order_id == order_id for p in self.payments.values()):
            return 409, _err(409, "orderId already exists")
        if method is not None and method not in self.methods:
            return 409, _err(409, "method is not enabled for the shop")
        self.created.append(dict(body))
        pay = Payment(
            id=str(uuid.uuid4()),
            order_id=str(order_id),
            amount_kopecks=int(amount),
            description=description,
            method=method,
            is_test=self.sandbox,
            success_url=body.get("successUrl"),
            fail_url=body.get("failUrl"),
            body=dict(body),
        )
        self.payments[pay.id] = pay
        if self.lose_create:
            return 502, b"<html>bad gateway</html>"
        return 201, _j(pay.as_json())

    def _get(self, pid: str) -> tuple[int, bytes]:
        self.status_calls.append(pid)
        if self.fail_status is not None:
            return self.fail_status, _err(self.fail_status, "fault")
        if not _UUID_RE.fullmatch(pid):
            return 400, _err(400, ["id must be a UUID"])
        pay = self.payments.get(pid.lower())
        if pay is None:
            return 404, _err(404, "Payment not found")
        return 200, _j(pay.as_json())

    def _list(self, q: Mapping[str, str]) -> tuple[int, bytes]:
        if "orderId" in q:
            for pay in self.payments.values():
                if pay.order_id == q["orderId"]:
                    return 200, _j(pay.as_json())
            return 404, _err(404, "Payment not found")
        items = sorted(self.payments.values(), key=lambda p: p.created_at)
        if q.get("status"):
            wanted = set(q["status"].split(","))
            items = [p for p in items if p.status in wanted]
        page, limit = int(q.get("page", 1)), int(q.get("limit", 20))
        if not 1 <= limit <= 100 or page < 1:
            return 400, _err(400, ["bad paging"])
        chunk = items[(page - 1) * limit : page * limit]
        return 200, _j(
            {"items": [p.as_json() for p in chunk], "total": len(items), "page": page, "pageSize": limit}
        )

    def _cancel(self, pid: str) -> tuple[int, bytes]:
        pay = self.payments.get(pid)
        if pay is None:
            return 404, _err(404, "Payment not found")
        if pay.status == "CREATED":
            pay.status = "CANCELED"
        elif pay.status != "CANCELED":
            return 409, _err(409, "payment cannot be canceled")
        return 200, _j(pay.as_json())

    # ------------------------------------------------------------------------------------- server

    async def start(self) -> None:
        app = web.Application()

        async def any_route(request: web.Request) -> web.Response:
            raw = await request.read()
            body = json.loads(raw) if raw else None
            status, payload = self.handle(
                request.method, request.path, dict(request.headers), body, dict(request.query)
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

    async def __aenter__(self) -> FakeTabPay:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # ------------------------------------------------------------------------------ desk-side actions

    def by_order(self, order_id: str) -> Payment:
        for pay in self.payments.values():
            if pay.order_id == order_id:
                return pay
        raise KeyError(order_id)

    def add_payment(
        self, order_id: str, amount_kopecks: int = 17_900, status: str = "CREATED", pid: str | None = None
    ) -> Payment:
        pay = Payment(id=pid or str(uuid.uuid4()), order_id=order_id, amount_kopecks=amount_kopecks)
        pay.status = status
        self.payments[pay.id] = pay
        return pay

    def set_status(self, pid: str, status: str) -> Payment:
        pay = self.payments[pid]
        pay.status = status
        if status == "SUCCESS" and pay.paid_at is None:
            pay.paid_at = datetime.now(UTC)
        return pay

    # ------------------------------------------------------------------------------------- webhooks

    def webhook_parts(
        self,
        pid: str,
        status: str | None = None,
        *,
        amount_kopecks: int | None = None,
        at: datetime | None = None,
        test: bool | None = None,
        secret: str | None = None,
    ) -> tuple[bytes, dict[str, str]]:
        """A webhook for payment ``pid`` (its current state unless overridden): ``(body, headers)``."""
        pay = self.payments[pid]
        payload = {
            "id": pay.id,
            "orderId": pay.order_id,
            "status": status or pay.status,
            "amountKopecks": pay.amount_kopecks if amount_kopecks is None else amount_kopecks,
            "telegramId": None,
            "metadata": None,
            "test": pay.is_test if test is None else test,
        }
        return signed(payload, at=at, secret=secret or self.webhook_secret)

    def webhook(self, pid: str, status: str | None = None, **kw: Any) -> WebhookRequest:
        body, headers = self.webhook_parts(pid, status, **kw)
        return WebhookRequest(body=body, headers=headers)

    async def send_webhook(self, url: str, pid: str, status: str | None = None, **kw: Any) -> int:
        """POST a signed webhook to ``url``; returns the HTTP status."""
        body, headers = self.webhook_parts(pid, status, **kw)
        async with (
            ClientSession(timeout=ClientTimeout(total=10)) as session,
            session.post(url, data=body, headers=headers) as resp,
        ):
            await resp.read()
            return resp.status


def signed(
    payload: Mapping[str, Any] | bytes,
    *,
    at: datetime | None = None,
    stamp: str | None = None,
    secret: str = WEBHOOK_SECRET,
) -> tuple[bytes, dict[str, str]]:
    """Compact JSON body (or the given bytes) with both TabPay signature headers."""
    body = payload if isinstance(payload, bytes) else json.dumps(payload, separators=(",", ":")).encode()
    ts = stamp if stamp is not None else str(int((at or datetime.now(UTC)).timestamp()))
    return body, {
        "Content-Type": "application/json",
        "X-Timestamp": ts,
        "X-Signature-V2": sign_v2(secret, ts, body),
        "X-Signature": sign_v1(secret, body),
    }


def _j(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False).encode()


def _err(status: int, message: str | list[str]) -> bytes:
    names = {400: "Bad Request", 401: "Unauthorized", 404: "Not Found", 409: "Conflict"}
    data: dict[str, Any] = {"statusCode": status, "message": message}
    if status in names:  # 429 and others come without «error»
        data["error"] = names[status]
    return _j(data)
