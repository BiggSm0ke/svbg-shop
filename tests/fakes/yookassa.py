"""Fake ЮKassa API v3 for tests — built from our specification (``docs/providers/yookassa.md``), which follows
the official documentation (https://yookassa.ru/developers, 2026-10-02).

Two ways to use it:

* as the responder of :class:`~svbg.payments.testkit.CountingHttp` (``CountingHttp(fake)``): no sockets;
* as a real aiohttp server (``async with FakeYooKassa() as kassa: kassa.base_url``) for end-to-end tests
  through :class:`~svbg.payments.registry.InstanceHttp`.

Realistic where the plugin depends on it: HTTP Basic ``shopId:secret`` (401 ``invalid_credentials``
otherwise); ``Idempotence-Key`` required on POST (≤ 64 chars) and replayed — the same key returns the stored
answer, so a retried create never makes a second payment; ``POST /v3/payments`` (amount as a string with the
currency's decimals, receipt total = payment amount, at least one contact), ``GET /v3/payments/{id}`` (404),
``POST /v3/payments/{id}/capture``, ``POST /v3/refunds``, ``GET /v3/me``; statuses ``pending``,
``waiting_for_capture``, ``succeeded``, ``canceled`` (+ ``cancellation_details``), ``refunded_amount``,
``test``. Notifications are unsigned ``{"type": "notification", "event", "object"}`` sent from a ЮKassa
address. Fault injection: a queue of HTTP answers for the next creates (``202``/``500``…), a status for every
answer.
"""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import unquote, urlsplit

from aiohttp import ClientSession, ClientTimeout, web

from svbg.payments.testkit import HttpCall
from svbg.sdk import HttpResponse, WebhookRequest

SHOP_ID = "123456"
SECRET_KEY = "live_Yk0123456789abcdefSECRET"
TEST_SECRET_KEY = "test_Yk0123456789abcdefSECRET"
BASE_PATH = "/v3"
FAKE_BASE_URL = "https://yookassa.fake/v3"
YOOKASSA_IP = "185.71.76.10"
YOOKASSA_IP6 = "2a02:5180:0:1509::17"


def iso(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{at.microsecond // 1000:03d}Z"


@dataclass
class Payment:
    id: str
    amount: str
    currency: str = "RUB"
    status: str = "pending"
    metadata: dict[str, Any] = field(default_factory=dict)
    description: str = ""
    test: bool = False
    refunded: str | None = None
    cancel_reason: str | None = None
    captured_at: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    body: dict[str, Any] = field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "status": self.status,
            "paid": self.status in ("succeeded", "waiting_for_capture"),
            "amount": {"value": self.amount, "currency": self.currency},
            "created_at": iso(self.created_at),
            "description": self.description,
            "metadata": dict(self.metadata),
            "recipient": {"account_id": SHOP_ID, "gateway_id": "100500"},
            "refundable": self.status == "succeeded",
            "test": self.test,
        }
        if self.status == "pending":
            data["confirmation"] = {
                "type": "redirect",
                "confirmation_url": f"https://yoomoney.fake/api-pages/v2/payment-confirm/epl?orderId={self.id}",
            }
        if self.status == "waiting_for_capture":
            data["expires_at"] = iso(datetime.now(UTC) + timedelta(days=7))
        if self.status == "succeeded":
            data["captured_at"] = iso(self.captured_at or datetime.now(UTC))
            data["income_amount"] = {
                "value": f"{Decimal(self.amount) * Decimal('0.965'):.2f}",
                "currency": self.currency,
            }
        if self.refunded is not None:
            data["refunded_amount"] = {"value": self.refunded, "currency": self.currency}
        if self.status == "canceled":
            data["cancellation_details"] = {
                "party": "yoo_money",
                "reason": self.cancel_reason or "expired_on_confirmation",
            }
        return data


def _err(code: str, description: str, parameter: str | None = None) -> bytes:
    data: dict[str, Any] = {
        "type": "error",
        "id": str(uuid.uuid4()),
        "code": code,
        "description": description,
    }
    if parameter:
        data["parameter"] = parameter
    return json.dumps(data, ensure_ascii=False).encode()


class FakeYooKassa:
    """See module docstring."""

    def __init__(
        self,
        *,
        shop_id: str = SHOP_ID,
        secret_key: str = SECRET_KEY,
        base_url: str = FAKE_BASE_URL,
        test_shop: bool = False,
        fiscalization: bool = True,
    ) -> None:
        self.shop_id = shop_id
        self.secret_key = secret_key
        self.test_shop = test_shop
        self.fiscalization = fiscalization
        self._base_url = base_url.rstrip("/")
        self.payments: dict[str, Payment] = {}
        self.created: list[dict[str, Any]] = []
        self.refunds: list[dict[str, Any]] = []
        self.captures: list[str] = []
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.idempotence: dict[str, tuple[int, bytes]] = {}
        self.status_calls: list[str] = []
        self.fail_with: int | None = None  # HTTP status of every API answer while set
        self.create_faults: list[tuple[int, bytes]] = []  # answers of the next creates, then normal
        self.fail_status: int | None = None
        self.delay: float = 0.0
        self._runner: web.AppRunner | None = None
        self._port: int | None = None

    # ------------------------------------------------------------------------------------- transport

    @property
    def base_url(self) -> str:
        if self._port is not None:
            return f"http://127.0.0.1:{self._port}{BASE_PATH}"
        return self._base_url

    async def __call__(self, call: HttpCall) -> HttpResponse:
        """:class:`~svbg.payments.testkit.CountingHttp` responder."""
        if self.delay:
            await asyncio.sleep(self.delay)
        status, payload = self.handle(call.method, urlsplit(call.url).path, call.headers, call.json)
        return HttpResponse(status, payload, {"Content-Type": "application/json"})

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
            return self.fail_with, _err("internal_server_error", "fault")
        if not path.startswith(BASE_PATH + "/"):
            return 404, b"<html>not found</html>"
        if not self._authorized(low):
            return 401, _err("invalid_credentials", "Basic authentication failed")
        rest = path[len(BASE_PATH) :]
        if method == "GET" and rest == "/me":
            return 200, _j(
                {
                    "account_id": self.shop_id,
                    "test": self.test_shop,
                    "fiscalization": {"enabled": self.fiscalization, "provider": "yoo_receipt"},
                    "fiscalization_enabled": self.fiscalization,
                    "payment_methods": ["bank_card", "sbp", "yoo_money"],
                    "status": "enabled",
                }
            )
        if method == "GET" and rest.startswith("/payments/"):
            return self._get(unquote(rest[len("/payments/") :]))
        if method != "POST":
            return 405, _err("invalid_request", "method not allowed")
        key = low.get("idempotence-key", "")
        if not key or len(key) > 64:
            return 400, _err("invalid_request", "Idempotence-Key is required", "Idempotence-Key")
        if rest == "/payments" and self.create_faults:
            return self.create_faults.pop(0)  # nothing stored: the next attempt is processed
        stored = self.idempotence.get(key)
        if stored is not None:
            return stored
        if rest == "/payments":
            answer = self._create(body)
        elif rest.startswith("/payments/") and rest.endswith("/capture"):
            answer = self._capture(unquote(rest[len("/payments/") : -len("/capture")]))
        elif rest == "/refunds":
            answer = self._refund(body)
        else:
            answer = 404, _err("not_found", "unknown method")
        if answer[0] == 200:
            self.idempotence[key] = answer
        return answer

    def _check_receipt(self, body: Mapping[str, Any], total: Decimal) -> tuple[int, bytes] | None:
        receipt = body.get("receipt")
        if receipt is None:
            return None
        customer = receipt.get("customer") or {}
        if not (customer.get("email") or customer.get("phone")):
            return 400, _err("invalid_request", "Receipt customer email or phone is required", "receipt")
        items = receipt.get("items") or []
        if not items:
            return 400, _err("invalid_request", "Receipt items are required", "receipt.items")
        acc = Decimal(0)
        for item in items:
            if len(str(item.get("description", ""))) > 128:
                return 400, _err("invalid_request", "description too long", "receipt.items.description")
            if not 1 <= int(item.get("vat_code", 0)) <= 12:
                return 400, _err("invalid_request", "bad vat_code", "receipt.items.vat_code")
            acc += Decimal(str(item["quantity"])) * Decimal(item["amount"]["value"])
        if acc != total:
            return 400, _err("invalid_request", "Receipt total differs from the payment amount", "receipt")
        return None

    def _create(self, body: Any) -> tuple[int, bytes]:
        if not isinstance(body, dict):
            return 400, _err("invalid_request", "json body required")
        amount = body.get("amount") or {}
        value = amount.get("value")
        if not isinstance(value, str) or "." not in value or len(value.split(".")[1]) != 2:
            return 400, _err(
                "invalid_request", "amount.value must be a string with 2 decimals", "amount.value"
            )
        if Decimal(value) < 1:
            return 400, _err("invalid_request", "amount is below the minimum", "amount.value")
        if len(str(body.get("description", ""))) > 128:
            return 400, _err("invalid_request", "description too long", "description")
        confirmation = body.get("confirmation") or {}
        if confirmation.get("type") != "redirect" or not confirmation.get("return_url"):
            return 400, _err("invalid_request", "return_url is required", "confirmation.return_url")
        bad = self._check_receipt(body, Decimal(value))
        if bad is not None:
            return bad
        self.created.append(dict(body))
        pid = str(uuid.uuid4())
        payment = Payment(
            id=pid,
            amount=value,
            currency=str(amount.get("currency")),
            metadata=dict(body.get("metadata") or {}),
            description=str(body.get("description", "")),
            test=self.test_shop,
            body=dict(body),
        )
        self.payments[pid] = payment
        return 200, _j(payment.as_json())

    def _get(self, pid: str) -> tuple[int, bytes]:
        self.status_calls.append(pid)
        if self.fail_status is not None:
            return self.fail_status, _err("internal_server_error", "fault")
        payment = self.payments.get(pid)
        if payment is None:
            return 404, _err("not_found", "Payment not found", "payment_id")
        return 200, _j(payment.as_json())

    def _capture(self, pid: str) -> tuple[int, bytes]:
        payment = self.payments.get(pid)
        if payment is None:
            return 404, _err("not_found", "Payment not found")
        if payment.status != "waiting_for_capture":
            return 400, _err("invalid_request", f"Payment is {payment.status}")
        self.captures.append(pid)
        payment.status = "succeeded"
        payment.captured_at = datetime.now(UTC)
        return 200, _j(payment.as_json())

    def _refund(self, body: Any) -> tuple[int, bytes]:
        payment = self.payments.get(str((body or {}).get("payment_id")))
        if payment is None or payment.status != "succeeded":
            return 400, _err("invalid_request", "payment is not refundable", "payment_id")
        value = Decimal(body["amount"]["value"])
        done = Decimal(payment.refunded or "0") + value
        if done > Decimal(payment.amount):
            return 400, _err("invalid_request", "refund exceeds the payment", "amount")
        payment.refunded = f"{done:.2f}"
        self.refunds.append(dict(body))
        rid = str(uuid.uuid4())
        return 200, _j(
            {
                "id": rid,
                "payment_id": payment.id,
                "status": "succeeded",
                "amount": body["amount"],
                "created_at": iso(datetime.now(UTC)),
            }
        )

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

    async def __aenter__(self) -> FakeYooKassa:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # ------------------------------------------------------------------------------ kassa-side actions

    def add_payment(
        self,
        pid: str,
        our_id: str | None,
        amount: str = "179.00",
        status: str = "pending",
        *,
        test: bool = False,
    ) -> Payment:
        payment = Payment(
            id=pid,
            amount=amount,
            status=status,
            metadata={"payment_id": our_id} if our_id else {},
            test=test,
        )
        self.payments[pid] = payment
        return payment

    def set_status(
        self,
        pid: str,
        status: str,
        *,
        amount: str | None = None,
        reason: str | None = None,
        refunded: str | None = None,
    ) -> Payment:
        payment = self.payments[pid]
        payment.status = status
        if amount is not None:
            payment.amount = amount
        if reason is not None:
            payment.cancel_reason = reason
        if refunded is not None:
            payment.refunded = refunded
        if status == "succeeded" and payment.captured_at is None:
            payment.captured_at = datetime.now(UTC)
        return payment

    # ------------------------------------------------------------------------------------- webhooks

    def notification_body(self, pid: str, event: str | None = None) -> bytes:
        payment = self.payments[pid]
        if event is None:
            event = f"payment.{payment.status}"
        if event.startswith("refund."):
            obj: dict[str, Any] = {
                "id": str(uuid.uuid4()),
                "payment_id": pid,
                "status": "succeeded",
                "amount": {"value": payment.refunded or payment.amount, "currency": payment.currency},
                "created_at": iso(datetime.now(UTC)),
            }
        else:
            obj = payment.as_json()
        return json.dumps(
            {"type": "notification", "event": event, "object": obj}, ensure_ascii=False
        ).encode()

    def webhook(
        self,
        pid: str,
        event: str | None = None,
        *,
        remote: str | None = YOOKASSA_IP,
        headers: Mapping[str, str] | None = None,
    ) -> WebhookRequest:
        return WebhookRequest(
            body=self.notification_body(pid, event),
            headers={"Content-Type": "application/json", **dict(headers or {})},
            remote=remote,
        )

    async def send_webhook(
        self, url: str, pid: str, event: str | None = None, *, forwarded_for: str | None = YOOKASSA_IP
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


def _j(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False).encode()
