"""Fake Pear (PayPear, ``api.paypear.ru/v1``) for tests — built only from our specification
(``docs/providers/paypear.md``), which follows the official documentation
(https://paypear.ru/docs/, 2026-10-02).

Two ways to use it:

* as the responder of :class:`~svbg.payments.testkit.CountingHttp` (``CountingHttp(fake)``): no sockets;
* as a real aiohttp server (``async with FakePayPear() as pear: pear.base_url``).

Realistic where the plugin depends on it: HTTP Basic ``shop_id:secret`` (``401 UNAUTHORIZED`` otherwise); an
idempotency key on ``POST`` (either spelling, ≤ 64 chars; the same key returns the stored answer);
``POST /payment/`` (``order_id`` ≤ 36, ``amount.value`` a string with a dot, ``payment_method_data.type``
``sbp``/``card``, ``confirmation.return_url``, ``description`` ≤ 128); ``GET /payment/{id}/`` and
``GET /payment/order/{order_id}/`` (``404 NOT_FOUND``); the answer envelope key ``result`` or ``response``;
amounts as numbers or strings; statuses ``NEW``/``PROCESS``/``CONFIRMED``/``CANCELED``/``EXPIRED``/
``REFUNDED`` and ``refunded_amount``. Notifications ``{"type": "notification", "event", "object",
"signature"}`` come from ``158.160.85.101``; the signature is random hex (its algorithm is unpublished).
Fault injection: answers of the next creates (``500`` with or without the payment made), a status for every
answer, the rate limit as ``409 TOO_MANY_REQUESTS`` or ``429``.
"""

from __future__ import annotations

import base64
import json
import secrets
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import unquote, urlsplit

from aiohttp import ClientSession, ClientTimeout, web

from svbg.payments.testkit import HttpCall
from svbg.sdk import HttpResponse, WebhookRequest

SHOP_ID = "33661"
SECRET_KEY = "test_secret_key"
BASE_PATH = "/v1"
FAKE_BASE_URL = "https://paypear.fake/v1"
PEAR_IP = "158.160.85.101"


def iso(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{at.microsecond // 1000:03d}Z"


@dataclass
class Payment:
    id: str
    order_id: str
    amount: Any  # "179.00" or 179.0 — Pear answers both ways
    currency: str = "RUB"
    status: str = "NEW"
    shop_id: int = int(SHOP_ID)
    description: str = ""
    method: str = "sbp"
    metadata: dict[str, Any] = field(default_factory=dict)
    refunded: Any = None
    return_url: str = "https://t.me/svbg_bot"
    webhook_url: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime = field(default_factory=lambda: datetime.now(UTC) + timedelta(hours=1))

    def as_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "shop_id": self.shop_id,
            "order_id": self.order_id,
            "status": self.status,
            "description": self.description,
            "amount": {"value": self.amount, "currency": self.currency},
            "credited_amount": {"value": self.amount, "currency": self.currency},
            "created_at": iso(self.created_at),
            "expires_at": iso(self.expires_at),
            "paid": self.status in ("CONFIRMED", "REFUNDED"),
            "metadata": dict(self.metadata),
        }
        if self.webhook_url:
            data["webhook_url"] = self.webhook_url
        if self.status in ("NEW", "PROCESS"):
            data["confirmation"] = {
                "type": "redirect",
                "confirmation_url": f"https://pay.paypear.fake/{self.method}/{self.id}",
                "return_url": self.return_url,
            }
        if self.refunded is not None:
            data["refunded_amount"] = {"value": self.refunded, "currency": self.currency}
        return data


def _err(status: int, code: str, message: str) -> tuple[int, bytes]:
    body = {"success": False, "error": {"status_code": status, "code": code, "message": message}}
    return status, json.dumps(body).encode()


class FakePayPear:
    """See module docstring."""

    def __init__(
        self,
        *,
        shop_id: str = SHOP_ID,
        secret_key: str = SECRET_KEY,
        base_url: str = FAKE_BASE_URL,
        envelope: str = "result",
    ) -> None:
        self.shop_id = shop_id
        self.secret_key = secret_key
        self.envelope = envelope
        self._base_url = base_url.rstrip("/")
        self.payments: dict[str, Payment] = {}
        self.created: list[dict[str, Any]] = []
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.idempotence: dict[str, tuple[int, bytes]] = {}
        self.status_calls: list[str] = []
        self.fail_with: int | None = None  # every answer while set
        self.fail_code: str = "INTERNAL_SERVER_ERROR"
        #: answers of the next creates: (status, made) — ``made`` creates the payment but answers ``status``
        self.create_faults: list[tuple[int, bool]] = []
        self.fail_status: int | None = None
        self._runner: web.AppRunner | None = None
        self._port: int | None = None

    # ------------------------------------------------------------------------------------- transport

    @property
    def base_url(self) -> str:
        if self._port is not None:
            return f"http://127.0.0.1:{self._port}{BASE_PATH}"
        return self._base_url

    async def __call__(self, call: HttpCall) -> HttpResponse:
        status, payload = self.handle(call.method, urlsplit(call.url).path, call.headers, call.json)
        return HttpResponse(status, payload, {"Content-Type": "application/json"})

    def _ok(self, obj: dict[str, Any]) -> tuple[int, bytes]:
        return 200, json.dumps({"success": True, self.envelope: obj}).encode()

    def _authorized(self, headers: Mapping[str, str]) -> bool:
        auth = headers.get("authorization", "")
        if not auth.startswith("Basic "):
            return False
        try:
            user, _, password = base64.b64decode(auth[6:]).decode().partition(":")
        except ValueError:
            return False
        return user == self.shop_id and password == self.secret_key

    def handle(self, method: str, path: str, headers: Mapping[str, str], body: Any) -> tuple[int, bytes]:
        low = {k.lower(): v for k, v in headers.items()}
        method = method.upper()
        self.requests.append((method, path, low))
        if self.fail_with is not None:
            return _err(self.fail_with, self.fail_code, "fault")
        if not path.startswith(BASE_PATH + "/"):
            return 404, b"<html>not found</html>"
        if not self._authorized(low):
            return _err(401, "UNAUTHORIZED", "invalid shop id or secret key")
        rest = path[len(BASE_PATH) :]
        if method == "GET" and rest.startswith("/payment/order/"):
            return self._by_order(unquote(rest[len("/payment/order/") :].rstrip("/")))
        if method == "GET" and rest.startswith("/payment/"):
            return self._get(unquote(rest[len("/payment/") :].rstrip("/")))
        if method != "POST" or rest != "/payment/":
            return _err(404, "NOT_FOUND", "unknown method")
        key = low.get("idempotence-key") or low.get("idempotency-key") or ""
        if not key or len(key) > 64:
            return _err(400, "BAD_REQUEST", "Idempotence-Key is required")
        if self.create_faults:
            status, made = self.create_faults.pop(0)
            if made:
                self._create(body)
            return _err(status, "INTERNAL_SERVER_ERROR" if status >= 500 else "TOO_MANY_REQUESTS", "fault")
        stored = self.idempotence.get(key)
        if stored is not None:
            return stored
        answer = self._create(body)
        if answer[0] == 200:
            self.idempotence[key] = answer
        return answer

    def _create(self, body: Any) -> tuple[int, bytes]:
        if not isinstance(body, dict):
            return _err(400, "BAD_REQUEST", "json body required")
        order_id = body.get("order_id")
        if not isinstance(order_id, str) or not 0 < len(order_id) <= 36:
            return _err(400, "BAD_REQUEST", "order_id must be a string up to 36 characters")
        amount = body.get("amount") or {}
        value = amount.get("value")
        if not isinstance(value, str) or "," in value or "." not in value:
            return _err(400, "BAD_REQUEST", "amount.value must be a string")
        if amount.get("currency") != "RUB":
            return _err(400, "BAD_REQUEST", "currency must match the account currency")
        method = (body.get("payment_method_data") or {}).get("type")
        if method not in ("sbp", "card"):
            return _err(400, "BAD_REQUEST", "payment_method_data.type is required")
        confirmation = body.get("confirmation") or {}
        if confirmation.get("type") != "redirect" or not confirmation.get("return_url"):
            return _err(400, "BAD_REQUEST", "confirmation.return_url is required")
        if len(str(body.get("description", ""))) > 128:
            return _err(400, "BAD_REQUEST", "description is too long")
        self.created.append(dict(body))
        payment = Payment(
            id=str(uuid.uuid4()),
            order_id=order_id,
            amount=value,
            description=str(body.get("description", "")),
            method=method,
            metadata=dict(body.get("metadata") or {}),
            return_url=str(confirmation["return_url"]),
            webhook_url=body.get("webhook_url"),
        )
        if body.get("expires_at"):
            payment.expires_at = datetime.fromisoformat(str(body["expires_at"]).replace("Z", "+00:00"))
        self.payments[payment.id] = payment
        return self._ok(payment.as_json())

    def _get(self, pid: str) -> tuple[int, bytes]:
        self.status_calls.append(pid)
        if self.fail_status is not None:
            code = "TOO_MANY_REQUESTS" if self.fail_status in (409, 429) else "INTERNAL_SERVER_ERROR"
            return _err(self.fail_status, code, "fault")
        payment = self.payments.get(pid)
        if payment is None:
            return _err(404, "NOT_FOUND", "payment not found")
        return self._ok(payment.as_json())

    def _by_order(self, order_id: str) -> tuple[int, bytes]:
        for payment in self.payments.values():
            if payment.order_id == order_id:
                return self._ok(payment.as_json())
        return _err(404, "NOT_FOUND", "payment not found")

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

    async def __aenter__(self) -> FakePayPear:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # ------------------------------------------------------------------------------ kassa-side actions

    def add_payment(
        self, pid: str, order_id: str, amount: Any = "179.00", status: str = "NEW", **kw: Any
    ) -> Payment:
        payment = Payment(id=pid, order_id=order_id, amount=amount, status=status, **kw)
        self.payments[pid] = payment
        return payment

    def set_status(self, pid: str, status: str, *, amount: Any = None, refunded: Any = None) -> Payment:
        payment = self.payments[pid]
        payment.status = status
        if amount is not None:
            payment.amount = amount
        if refunded is not None:
            payment.refunded = refunded
        return payment

    # ------------------------------------------------------------------------------------- webhooks

    def notification_body(self, pid: str, event: str | None = None) -> bytes:
        payment = self.payments[pid]
        if event is None:
            event = f"payment.{payment.status.lower()}"
        if event.startswith("refund."):
            obj: dict[str, Any] = {
                "id": str(uuid.uuid4()),
                "payment_id": pid,
                "status": event.removeprefix("refund.").upper(),
                "amount": {"value": payment.refunded or payment.amount, "currency": payment.currency},
                "created_at": iso(datetime.now(UTC)),
                "metadata": {},
                "description": "",
            }
        else:
            obj = payment.as_json()
        return json.dumps(
            {"type": "notification", "event": event, "object": obj, "signature": secrets.token_hex(32)},
            ensure_ascii=False,
        ).encode()

    def webhook(
        self,
        pid: str,
        event: str | None = None,
        *,
        remote: str | None = PEAR_IP,
        headers: Mapping[str, str] | None = None,
    ) -> WebhookRequest:
        return WebhookRequest(
            body=self.notification_body(pid, event),
            headers={"Content-Type": "application/json", **dict(headers or {})},
            remote=remote,
        )

    async def send_webhook(
        self, url: str, pid: str, event: str | None = None, *, forwarded_for: str | None = PEAR_IP
    ) -> int:
        """POST a notification to ``url`` as a reverse proxy in front of the bot would forward it."""
        headers = {"Content-Type": "application/json"}
        if forwarded_for is not None:
            headers["X-Forwarded-For"] = forwarded_for
        async with (
            ClientSession(timeout=ClientTimeout(total=10)) as session,
            session.post(url, data=self.notification_body(pid, event), headers=headers) as resp,
        ):
            await resp.read()
            return resp.status
