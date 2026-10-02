"""Fake Tribute Shop API for tests — built from our specification (``docs/providers/tribute.md``).

Used as the responder of :class:`~svbg.payments.testkit.CountingHttp` (``CountingHttp(fake)``), no sockets.

Realistic where the plugin depends on it: ``Api-Key`` (401 ``{"error": …}`` otherwise); ``POST /shop/orders``
validates ``amount`` (integer minor units, 100…), ``currency`` (``rub|eur|usd``), ``title`` ≤ 100 and
``description`` ≤ 300, ``https://`` URLs and ``customerId`` ≤ 256, answers a ``ShopOrder`` with ``uuid``,
``paymentUrl`` and ``webappPaymentUrl``; ``GET /shop/orders/{uuid}`` (404 unknown, 403 another shop);
``GET /shop/orders/{uuid}/transactions``; ``POST …/transactions/{txId}/refund``; ``GET /shops`` (a bare
array).
Webhooks are signed independently of the plugin: ``trbt-signature`` = HMAC-SHA256(API key, raw body) in hex
or base64.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import itertools
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import parse_qs, unquote, urlsplit

from svbg.payments.testkit import HttpCall
from svbg.sdk import HttpResponse, WebhookRequest

API_KEY = "trb-api-key-0123456789abcdef0123456789"
FAKE_BASE_URL = "https://tribute.fake/api/v1"
BASE_PATH = "/api/v1"
SHOP_ID = 7
Encoding = Literal["hex", "base64"]


def sign(key: str, body: bytes, encoding: Encoding = "hex") -> str:
    """Independent implementation of the webhook signature (do not import the plugin's)."""
    digest = hmac.new(key.encode(), body, hashlib.sha256).digest()
    return digest.hex() if encoding == "hex" else base64.b64encode(digest).decode()


@dataclass
class Order:
    uuid: str
    amount: int
    currency: str
    customer_id: str | None
    status: str = "pending"
    shop_id: int = SHOP_ID
    first_period_amount: int | None = None
    transactions: list[dict[str, Any]] = field(default_factory=list)
    body: dict[str, Any] = field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        return {
            "uuid": self.uuid,
            "shopId": self.shop_id,
            "amount": self.amount,
            "currency": self.currency,
            "title": self.body.get("title", "Order"),
            "description": self.body.get("description", "Order"),
            "status": self.status,
            "paymentUrl": f"https://web.tribute.fake/shop/pay/{self.uuid}",
            "webappPaymentUrl": f"https://t.me/tribute/app?startapp=s{self.uuid}",
            "createdAt": "2026-10-02T10:00:00Z",
            "period": "onetime",
            "starsAmount": 0,
            "onlyStars": False,
            "firstPeriodAmount": self.first_period_amount,
            "lastPaidTransactionAt": "2026-10-02T10:01:00Z" if self.status == "paid" else None,
            "sendEmail": False,
        }


class FakeTribute:
    """See module docstring."""

    def __init__(self, *, api_key: str = API_KEY, base_url: str = FAKE_BASE_URL) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.orders: dict[str, Order] = {}
        self.created: list[dict[str, Any]] = []
        self.refunds: list[tuple[str, int]] = []
        self.shops: list[dict[str, Any]] = [
            {
                "id": SHOP_ID,
                "userId": 1,
                "name": "Demo Shop",
                "link": "demo",
                "callbackUrl": "https://shop.example/webhooks/pay/1/token",
                "recurrent": False,
                "onlyStars": False,
                "tokenCharging": False,
                "status": 1,
            }
        ]
        self.fail_with: int | None = None
        self._tx_ids = itertools.count(1001)

    async def __call__(self, call: HttpCall) -> HttpResponse:
        parts = urlsplit(call.url)
        path = parts.path[len(BASE_PATH) :] if parts.path.startswith(BASE_PATH) else parts.path
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        status, payload = self.handle(call.method, path, call.headers, call.json, query)
        return HttpResponse(status, payload, {"Content-Type": "application/json"})

    def handle(
        self, method: str, path: str, headers: Mapping[str, str], body: Any, query: Mapping[str, str]
    ) -> tuple[int, bytes]:
        if self.fail_with is not None:
            return self.fail_with, _j({"error": "error_internal", "message": "fault"})
        low = {k.lower(): v for k, v in headers.items()}
        if not hmac.compare_digest(low.get("api-key", ""), self.api_key):
            return 401, _j({"error": "error_unauthorized", "message": "Invalid API key"})
        segments = [unquote(s) for s in path.strip("/").split("/")]
        if method == "GET" and segments == ["shops"]:
            return 200, _j(self.shops)
        if method == "POST" and segments == ["shop", "orders"]:
            return self._create(body)
        if len(segments) >= 3 and segments[:2] == ["shop", "orders"]:
            order = self.orders.get(segments[2])
            if order is None:
                return 404, _j({"error": "error_order_not_found", "message": "Order not found"})
            if order.shop_id != SHOP_ID:
                return 403, _j({"error": "error_forbidden", "message": "Forbidden"})
            rest = segments[3:]
            if method == "GET" and not rest:
                return 200, _j(order.as_json())
            if method == "GET" and rest == ["status"]:
                return 200, _j({"status": order.status})
            if method == "GET" and rest == ["transactions"]:
                return 200, _j({"transactions": order.transactions, "nextFrom": ""})
            if method == "POST" and len(rest) == 3 and rest[0] == "transactions" and rest[2] == "refund":
                return self._refund(order, rest[1])
        return 404, _j({"error": "error_not_found", "message": "not found"})

    def _create(self, body: Any) -> tuple[int, bytes]:
        if not isinstance(body, dict):
            return 400, _j({"error": "error_bad_request"})
        amount = body.get("amount")
        if isinstance(amount, bool) or not isinstance(amount, int):
            return 400, _j({"error": "error_bad_request", "message": "amount must be an integer"})
        if amount < 100:
            return 400, _j({"error": "error_amount_too_small", "message": "minimum amount is 100"})
        if body.get("currency") not in ("rub", "eur", "usd"):
            return 400, _j({"error": "error_invalid_currency"})
        for key, limit in (("title", 100), ("description", 300)):
            value = body.get(key)
            if not isinstance(value, str) or not value.strip():
                return 400, _j({"error": f"error_{key}_required"})
            if len(value.encode("utf-16-le")) // 2 > limit:
                return 400, _j({"error": f"error_{key}_too_long"})
        for key in ("successUrl", "failUrl"):
            if key in body and not str(body[key]).startswith("https://"):
                return 400, _j({"error": "error_invalid_url"})
        if len(str(body.get("customerId") or "")) > 256:
            return 400, _j({"error": "error_customer_id_too_long"})
        if body.get("shopId") not in (None, SHOP_ID):
            return 404, _j({"error": "error_shop_not_found"})
        self.created.append(dict(body))
        order = Order(
            uuid=str(uuid.uuid4()),
            amount=amount,
            currency=body["currency"],
            customer_id=body.get("customerId"),
            body=dict(body),
        )
        self.orders[order.uuid] = order
        return 200, _j(order.as_json())

    def _refund(self, order: Order, tx_id: str) -> tuple[int, bytes]:
        if order.status != "paid":
            return 400, _j({"error": "error_order_not_paid"})
        tx = next((t for t in order.transactions if str(t["id"]) == tx_id), None)
        if tx is None:
            return 400, _j({"error": "error_transaction_mismatch"})
        if tx["isRefunded"]:
            return 400, _j({"error": "error_already_refunded"})
        tx["isRefunded"], tx["isRefundable"] = True, False
        self.refunds.append((order.uuid, int(tx_id)))
        return 200, _j({"success": True, "message": "refund initiated", "status": "initiated"})

    # ------------------------------------------------------------------------------ desk-side actions

    def add(
        self, order_uuid: str, customer_id: str | None, amount: int = 17_900, status: str = "pending"
    ) -> Order:
        order = Order(uuid=order_uuid, amount=amount, currency="rub", customer_id=customer_id, status=status)
        self.orders[order_uuid] = order
        if status == "paid":
            self._sell(order)
        return order

    def pay(self, order_uuid: str) -> Order:
        order = self.orders[order_uuid]
        order.status = "paid"
        self._sell(order)
        return order

    def _sell(self, order: Order) -> None:
        major = f"{order.amount / 100:.2f}"
        order.transactions.append(
            {
                "id": next(self._tx_ids),
                "type": "shop_order_sell",
                "amount": major,
                "serviceFee": "0.00",
                "total": major,
                "currency": order.currency,
                "createdAt": 1_790_000_000,
                "paymentMethod": "bank_card",
                "isRefunded": False,
                "isRefundable": True,
                "isRecurring": False,
            }
        )

    def webhook(
        self,
        name: str,
        payload: Mapping[str, Any],
        *,
        key: str | None = None,
        encoding: Encoding = "hex",
        sent_at: str = "2026-10-02T10:01:00.123456789Z",
    ) -> WebhookRequest:
        body = json.dumps(
            {
                "name": name,
                "created_at": "2026-10-02T10:00:59.5Z",
                "sent_at": sent_at,
                "payload": dict(payload),
            },
            separators=(",", ":"),
        ).encode()
        return WebhookRequest(
            body=body,
            headers={
                "Content-Type": "application/json",
                "trbt-signature": sign(key or self.api_key, body, encoding),
            },
        )

    def order_webhook(self, order_uuid: str, name: str = "shop_order", **extra: Any) -> WebhookRequest:
        order = self.orders[order_uuid]
        payload: dict[str, Any] = {
            "uuid": order.uuid,
            "shopId": order.shop_id,
            "amount": order.amount,
            "currency": order.currency,
            "customerId": order.customer_id,
        }
        if name == "shop_order":
            payload |= {
                "fee": order.amount // 10,
                "status": "paid",
                "isRecurrent": False,
                "period": "onetime",
            }
        payload |= extra
        return self.webhook(name, payload)


def _j(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False).encode()
