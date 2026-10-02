"""Fake CloudPayments API for tests — built from our specification (``docs/providers/cloudpayments.md``).

Used as the responder of :class:`~svbg.payments.testkit.CountingHttp` (``CountingHttp(fake)``), no sockets.

Realistic where the plugin depends on it: HTTP Basic ``Public ID:API Secret`` (401 otherwise), every method is
``POST`` with the ``{"Success", "Message", "Model"}`` envelope; ``X-Request-ID`` replays the first answer for
the same id; ``orders/create`` (``Amount`` a JSON number with ≤ 2 decimals, ``Description`` required) →
``Model.Url``; ``payments/qr/sbp/link`` (``PublicId``, ``Scheme=charge``, RUB) → ``Model.QrUrl``;
``payments/find`` by ``InvoiceId`` → the last payment operation or ``Success=false, Message="Not found"``;
``payments/refund``; ``test``. Notifications are URL-encoded forms signed independently of the plugin:
``Content-HMAC`` = base64(HMAC-SHA256(secret, raw body)), ``X-Content-HMAC`` over the URL-decoded body.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import itertools
import json
import random
import string
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import quote, unquote, urlencode, urlsplit

from svbg.payments.testkit import HttpCall
from svbg.sdk import HttpResponse, WebhookRequest

PUBLIC_ID = "pk_0123456789abcdef0123456789abc"
API_SECRET = "cp-api-secret-0123456789abcdef"
FAKE_BASE_URL = "https://cloudpayments.fake"


def sign(secret: str, message: bytes) -> str:
    """Independent implementation of the notification HMAC (do not import the plugin's)."""
    return base64.b64encode(hmac.new(secret.encode(), message, hashlib.sha256).digest()).decode()


def form(fields: Mapping[str, Any]) -> bytes:
    """A notification body the way CloudPayments sends it (spaces as ``%20``)."""
    return urlencode({k: str(v) for k, v in fields.items()}, quote_via=quote).encode()


def signed_request(
    fields: Mapping[str, Any] | bytes,
    *,
    secret: str = API_SECRET,
    headers: tuple[str, ...] = ("Content-HMAC", "X-Content-HMAC"),
    query: Mapping[str, str] | None = None,
) -> WebhookRequest:
    body = fields if isinstance(fields, bytes) else form(fields)
    values = {
        "Content-HMAC": sign(secret, body),
        "X-Content-HMAC": sign(secret, unquote(body.decode(errors="replace")).encode()),
    }
    hdrs = {"Content-Type": "application/x-www-form-urlencoded", **{h: values[h] for h in headers}}
    return WebhookRequest(body=body, headers=hdrs, query=dict(query or {}))


@dataclass
class Operation:
    transaction_id: int
    invoice_id: str
    amount: Decimal
    currency: str = "RUB"
    status: str = "Completed"
    test: bool = True
    refunded: bool = False
    refunds: list[Decimal] = field(default_factory=list)

    def as_json(self) -> dict[str, Any]:
        at = (datetime.now(UTC) - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S")  # ISO without a zone
        return {
            "TransactionId": self.transaction_id,
            "Amount": float(self.amount),
            "Currency": self.currency,
            "PaymentAmount": float(self.amount),
            "PaymentCurrency": self.currency,
            "InvoiceId": self.invoice_id,
            "AccountId": None,
            "Description": "Оплата",
            "CreatedDateIso": at,
            "AuthDateIso": at if self.status != "Declined" else None,
            "ConfirmDateIso": at if self.status == "Completed" else None,
            "TestMode": self.test,
            "Status": self.status,
            "StatusCode": {"Authorized": 2, "Completed": 3, "Cancelled": 4, "Declined": 5}.get(
                self.status, 0
            ),
            "Refunded": self.refunded,
            "GatewayName": "Test",
            "Type": 0,
        }


class FakeCloudPayments:
    """See module docstring."""

    def __init__(
        self, *, public_id: str = PUBLIC_ID, api_secret: str = API_SECRET, base_url: str = FAKE_BASE_URL
    ) -> None:
        self.public_id = public_id
        self.api_secret = api_secret
        self.base_url = base_url.rstrip("/")
        self.orders: dict[str, dict[str, Any]] = {}
        self.operations: dict[str, Operation] = {}  # last payment operation by InvoiceId
        self.created: list[tuple[str, dict[str, Any]]] = []
        self.refund_calls: list[dict[str, Any]] = []
        self.request_ids: dict[str, tuple[int, bytes]] = {}
        self.fail_with: int | None = None
        self._tx = itertools.count(1504)

    async def __call__(self, call: HttpCall) -> HttpResponse:
        path = urlsplit(call.url).path
        status, payload = self.handle(call.method, path, call.headers, call.json)
        return HttpResponse(status, payload, {"Content-Type": "application/json"})

    def handle(self, method: str, path: str, headers: Mapping[str, str], body: Any) -> tuple[int, bytes]:
        if self.fail_with is not None:
            return self.fail_with, b""
        low = {k.lower(): v for k, v in headers.items()}
        expected = "Basic " + base64.b64encode(f"{self.public_id}:{self.api_secret}".encode()).decode()
        if not hmac.compare_digest(low.get("authorization", ""), expected):
            return 401, b""
        if method != "POST":
            return 405, b""
        request_id = low.get("x-request-id")
        if request_id is not None and request_id in self.request_ids:
            return self.request_ids[request_id]
        answer = self._route(path.strip("/"), body if isinstance(body, dict) else {})
        if request_id is not None:
            self.request_ids[request_id] = answer
        return answer

    def _route(self, method: str, body: dict[str, Any]) -> tuple[int, bytes]:
        if method == "test":
            return _ok("0a1b2c3d-0000-4000-8000-000000000000", model=None)
        if method == "orders/create":
            return self._order(body)
        if method == "payments/qr/sbp/link":
            return self._sbp(body)
        if method == "payments/find":
            op = self.operations.get(str(body.get("InvoiceId")))
            return _fail("Not found") if op is None else _ok(None, op.as_json())
        if method == "payments/refund":
            return self._refund(body)
        return 404, b""

    def _check_amount(self, body: Mapping[str, Any]) -> str | None:
        amount = body.get("Amount")
        if isinstance(amount, bool) or not isinstance(amount, int | float) or amount <= 0:
            return "Amount is required"
        if Decimal(str(amount)).as_tuple().exponent < -2:  # type: ignore[operator]
            return "Amount has more than two decimals"
        return None

    def _order(self, body: dict[str, Any]) -> tuple[int, bytes]:
        error = self._check_amount(body) or (None if body.get("Description") else "Description is required")
        if error:
            return _fail(error)
        self.created.append(("orders/create", dict(body)))
        order_id = "".join(random.choices(string.ascii_letters + string.digits, k=16))
        self.orders[order_id] = dict(body)
        model = {
            "Id": order_id,
            "Number": len(self.orders),
            "Amount": body["Amount"],
            "Currency": body.get("Currency", "RUB"),
            "Description": body["Description"],
            "RequireConfirmation": bool(body.get("RequireConfirmation")),
            "Url": f"https://orders.cloudpayments.fake/d/{order_id}",
            "CultureName": body.get("CultureName", "ru-RU"),
            "Status": "Created",
            "StatusCode": 0,
        }
        return _ok(None, model)

    def _sbp(self, body: dict[str, Any]) -> tuple[int, bytes]:
        error = self._check_amount(body)
        if body.get("PublicId") != self.public_id:
            error = "PublicId is required"
        if body.get("Currency") != "RUB" or body.get("Scheme") != "charge":
            error = "Currency must be RUB and Scheme charge"
        if error:
            return _fail(error)
        self.created.append(("payments/qr/sbp/link", dict(body)))
        tx = next(self._tx)
        model = {
            "QrUrl": f"https://qr.nspk.fake/{tx}",
            "TransactionId": tx,
            "MerchantOrderId": body.get("InvoiceId"),
            "Amount": body["Amount"],
            "Status": "AwaitingAuthentication",
        }
        return _ok(None, model)

    def _refund(self, body: dict[str, Any]) -> tuple[int, bytes]:
        self.refund_calls.append(dict(body))
        op = next(
            (o for o in self.operations.values() if o.transaction_id == body.get("TransactionId")), None
        )
        if op is None or op.status != "Completed":
            return _fail("Transaction not found")
        amount = Decimal(str(body.get("Amount")))
        if sum(op.refunds, Decimal(0)) + amount > op.amount:
            return _fail("Refund amount exceeds payment amount")
        op.refunds.append(amount)
        op.refunded = True
        return _ok(None, {"TransactionId": next(self._tx)})

    # ------------------------------------------------------------------------------ desk-side actions

    def pay(
        self, invoice_id: str, amount: str = "179.00", status: str = "Completed", test: bool = True
    ) -> Operation:
        op = Operation(next(self._tx), invoice_id, Decimal(amount), status=status, test=test)
        self.operations[invoice_id] = op
        return op

    def pay_notification(self, op: Operation, **extra: Any) -> WebhookRequest:
        fields: dict[str, Any] = {
            "TransactionId": op.transaction_id,
            "Amount": f"{op.amount:.2f}",
            "Currency": op.currency,
            "PaymentAmount": f"{op.amount:.2f}",
            "PaymentCurrency": op.currency,
            "DateTime": "2026-10-02 10:00:05",
            "CardFirstSix": "424242",
            "CardLastFour": "4242",
            "CardType": "Visa",
            "CardExpDate": "12/27",
            "TestMode": 1 if op.test else 0,
            "Status": op.status,
            "OperationType": "Payment",
            "GatewayName": "Test",
            "InvoiceId": op.invoice_id,
            "PaymentMethod": "Card",
            "TotalFee": "0.39",
        }
        fields.update(extra)
        return signed_request(fields, secret=self.api_secret)

    def refund_notification(self, op: Operation, amount: str) -> WebhookRequest:
        fields = {
            "TransactionId": next(self._tx),
            "PaymentTransactionId": op.transaction_id,
            "Amount": amount,
            "DateTime": "2026-10-02 11:00:00",
            "OperationType": "Refund",
            "InvoiceId": op.invoice_id,
        }
        return signed_request(fields, secret=self.api_secret)


def _ok(message: str | None, model: Any) -> tuple[int, bytes]:
    return 200, json.dumps({"Success": True, "Message": message, "Model": model}).encode()


def _fail(message: str) -> tuple[int, bytes]:
    return 200, json.dumps({"Success": False, "Message": message}).encode()
