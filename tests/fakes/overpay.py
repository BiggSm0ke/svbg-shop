"""Fake Overpay (Checkout + card gateway + APM API) for tests — built only from our specification
(``docs/providers/overpay.md``), which follows the official documentation
(https://docs.overpay.io/ru/, 2026-10-02).

Two ways to use it:

* as the responder of :class:`~svbg.payments.testkit.CountingHttp` (``CountingHttp(fake)``): no sockets;
  every host (``checkout.``, ``gateway.``, ``api.``) is answered by the same fake;
* as a real aiohttp server (``async with FakeOverpay() as op: op.base_url``) for all three base addresses.

Realistic where the plugin depends on it: HTTP Basic ``shop_id:secret`` (``401`` otherwise);
``X-API-Version: 2`` on Checkout; ``POST /ctp/api/checkouts`` (integer ``order.amount``, ``currency``,
``description``, ``transaction_type``) → ``token`` + ``redirect_url``; ``GET /ctp/api/checkouts/{token}``
(``finished``, ``expired``, ``status``, ``test``, ``order``, ``gateway_response.payment``); the documented
test cards (``4200000000000000`` → successful, ``4005550000000019`` → failed) and the APM ``bogus`` method
(``amount < 10000`` → successful, ``10000 < amount < 20000`` → failed); refunds on
``/transactions/refunds`` (cards) and ``/beyag/transactions/refunds`` (APM) with ``RequestID`` replay and
``{"message": "transaction can't be refunded", "errors": {"base": [...]}}``. Notifications are signed by an
independent implementation (``cryptography``): ``Content-Signature = base64(RSA-SHA256 PKCS#1 v1.5 (raw
body))`` plus Basic. Fault injection: a number of ``429`` answers, a status for every answer.
"""

from __future__ import annotations

import base64
import json
import secrets
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import cache
from typing import Any
from urllib.parse import unquote, urlsplit

from aiohttp import web
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from svbg.payments.testkit import HttpCall
from svbg.sdk import HttpResponse, WebhookRequest

SHOP_ID = "1"
SECRET_KEY = "test_secret_key"
FAKE_CHECKOUT_URL = "https://checkout.overpay.fake"
FAKE_GATEWAY_URL = "https://gateway.overpay.fake"
FAKE_APM_URL = "https://api.overpay.fake"
CARD_OK = "4200000000000000"
CARD_DECLINED = "4005550000000019"


@cache
def keypair(name: str = "shop", bits: int = 2048) -> rsa.RSAPrivateKey:
    """One RSA key per name and process (``shop``, ``other``)."""
    return rsa.generate_private_key(public_exponent=65537, key_size=bits)


def public_pem(name: str = "shop") -> str:
    return (
        keypair(name)
        .public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )


def public_bare(name: str = "shop") -> str:
    """The key as the Overpay cabinet shows it: base64 of the SubjectPublicKeyInfo DER, no PEM lines."""
    der = (
        keypair(name)
        .public_key()
        .public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    )
    return base64.b64encode(der).decode()


def rsa_sign(body: bytes, name: str = "shop") -> str:
    """Independent implementation of the notification signature (do not import the plugin's)."""
    return base64.b64encode(keypair(name).sign(body, padding.PKCS1v15(), hashes.SHA256())).decode()


def basic(shop_id: str = SHOP_ID, secret_key: str = SECRET_KEY) -> str:
    return "Basic " + base64.b64encode(f"{shop_id}:{secret_key}".encode()).decode()


def iso(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{at.microsecond // 1000:03d}Z"


def _j(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False).encode()


@dataclass
class Transaction:
    uid: str
    amount: int
    currency: str
    tracking_id: str | None
    status: str = "pending"
    type: str = "payment"
    method: str = "credit_card"
    test: bool = False
    message: str = ""
    paid_at: datetime | None = None
    parent_uid: str | None = None
    refunded: int = 0

    def as_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "uid": self.uid,
            "status": self.status,
            "amount": self.amount,
            "currency": self.currency,
            "type": self.type,
            "tracking_id": self.tracking_id,
            "test": self.test,
            "message": self.message or ("Successfully processed" if self.status == "successful" else ""),
            "created_at": iso(datetime.now(UTC)),
            "updated_at": iso(datetime.now(UTC)),
            "paid_at": iso(self.paid_at) if self.paid_at else None,
        }
        if self.method == "credit_card":
            data["payment_method_type"] = "credit_card"
            data["payment"] = {"status": self.status, "ref_id": "777", "message": data["message"]}
        else:
            data["method_type"] = self.method
            data["payment"] = {"status": self.status, "ref_id": "A0000000000000000000000000000001"}
        if self.parent_uid:
            data["parent_uid"] = self.parent_uid
        return data


@dataclass
class Token:
    token: str
    shop_id: int
    amount: int
    currency: str
    description: str
    tracking_id: str | None
    test: bool
    expired_at: datetime
    body: dict[str, Any] = field(default_factory=dict)
    expired: bool = False
    payment: Transaction | None = None
    error: str | None = None

    @property
    def finished(self) -> bool:
        return self.payment is not None and self.payment.status in ("successful", "failed", "expired")

    def as_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "token": self.token,
            "shop_id": self.shop_id,
            "transaction_type": "payment",
            "test": self.test,
            "finished": self.finished,
            "expired": self.expired,
            "order": {
                "amount": self.amount,
                "currency": self.currency,
                "description": self.description,
                "tracking_id": self.tracking_id,
                "expired_at": self.expired_at.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            },
            "settings": dict(self.body.get("settings") or {}),
        }
        if self.expired and not self.finished:
            data["status"], data["message"] = "error", "Token is expired."
        elif self.error:
            data["status"], data["message"] = "error", self.error
        elif self.payment is not None:
            data["status"] = self.payment.status
            data["gateway_response"] = {
                "payment": {
                    "uid": self.payment.uid,
                    "status": self.payment.status,
                    "amount": self.payment.amount,
                    "currency": self.payment.currency,
                    "message": self.payment.message,
                    "paid_at": iso(self.payment.paid_at) if self.payment.paid_at else None,
                    **(
                        {"payment_method_type": "credit_card"}
                        if self.payment.method == "credit_card"
                        else {"method_type": self.payment.method}
                    ),
                }
            }
        return data


class FakeOverpay:
    """See module docstring."""

    def __init__(self, *, shop_id: str = SHOP_ID, secret_key: str = SECRET_KEY, key: str = "shop") -> None:
        self.shop_id = shop_id
        self.secret_key = secret_key
        self.key = key
        self.tokens: dict[str, Token] = {}
        self.transactions: dict[str, Transaction] = {}
        self.created: list[dict[str, Any]] = []
        self.refunds: list[tuple[str, dict[str, Any]]] = []
        self.requests: list[tuple[str, str, str, dict[str, str]]] = []  # method, host, path, headers
        self.request_ids: dict[str, tuple[int, bytes]] = {}
        self.fail_with: int | None = None
        self.too_many: int = 0  # the next N answers are 429
        self._runner: web.AppRunner | None = None
        self._port: int | None = None

    # ------------------------------------------------------------------------------------- transport

    @property
    def base_url(self) -> str:
        assert self._port is not None, "start() the server first"
        return f"http://127.0.0.1:{self._port}"

    async def __call__(self, call: HttpCall) -> HttpResponse:
        parts = urlsplit(call.url)
        status, payload = self.handle(call.method, parts.hostname or "", parts.path, call.headers, call.json)
        return HttpResponse(status, payload, {"Content-Type": "application/json"})

    def _authorized(self, headers: Mapping[str, str]) -> bool:
        return headers.get("authorization", "") == basic(self.shop_id, self.secret_key)

    def handle(
        self, method: str, host: str, path: str, headers: Mapping[str, str], body: Any
    ) -> tuple[int, bytes]:
        low = {k.lower(): v for k, v in headers.items()}
        method = method.upper()
        self.requests.append((method, host, path, low))
        if self.too_many:
            self.too_many -= 1
            return 429, _j({"message": "Too Many Requests"})
        if self.fail_with is not None:
            return self.fail_with, _j({"message": "fault", "errors": {"base": ["fault"]}})
        if not self._authorized(low):
            return 401, _j({"message": "Unauthorized"})
        if path == "/ctp/api/checkouts" or path.startswith("/ctp/api/checkouts/"):
            if low.get("x-api-version") != "2":
                return 400, _j({"message": "X-API-Version: 2 is required"})
            if method == "POST" and path == "/ctp/api/checkouts":
                return self._create(body)
            if method == "GET":
                return self._get(unquote(path.rsplit("/", 1)[-1]))
        if method == "POST" and path in ("/transactions/refunds", "/beyag/transactions/refunds"):
            key = low.get("requestid")
            if key and key in self.request_ids:
                return self.request_ids[key]
            answer = self._refund(path, body)
            if key and answer[0] == 200:
                self.request_ids[key] = answer
            return answer
        return 404, _j({"message": "Not found"})

    def _create(self, body: Any) -> tuple[int, bytes]:
        checkout = (body or {}).get("checkout") if isinstance(body, dict) else None
        if not isinstance(checkout, dict):
            return 422, _j({"message": "checkout is missing", "errors": {"checkout": ["is missing"]}})
        order = checkout.get("order") or {}
        errors: dict[str, list[str]] = {}
        if checkout.get("transaction_type") != "payment":
            errors["transaction_type"] = ["is invalid"]
        amount = order.get("amount")
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            errors["order.amount"] = ["must be a positive integer"]
        if not isinstance(order.get("currency"), str) or len(order["currency"]) != 3:
            errors["order.currency"] = ["is invalid"]
        if not order.get("description"):
            errors["order.description"] = ["can't be blank"]
        attempts = checkout.get("attempts", 1)
        if not isinstance(attempts, int) or not 1 <= attempts <= 3:
            errors["attempts"] = ["must be 1..3"]
        if errors:
            return 422, _j({"message": "Validation failed", "errors": {"checkout": errors}})
        self.created.append(dict(checkout))
        token = secrets.token_hex(32)
        expired_at = datetime.now(UTC) + timedelta(hours=24)
        if order.get("expired_at"):
            expired_at = datetime.fromisoformat(str(order["expired_at"]))
        self.tokens[token] = Token(
            token=token,
            shop_id=int(self.shop_id),
            amount=amount,
            currency=order["currency"],
            description=str(order["description"]),
            tracking_id=order.get("tracking_id"),
            test=bool(checkout.get("test", False)),
            expired_at=expired_at,
            body=dict(checkout),
        )
        return 200, _j(
            {
                "checkout": {
                    "token": token,
                    "redirect_url": f"https://checkout.overpay.fake/v2/checkout?token={token}",
                }
            }
        )

    def _get(self, token: str) -> tuple[int, bytes]:
        item = self.tokens.get(token)
        if item is None:
            return 404, _j({"message": "Record not found"})
        return 200, _j({"checkout": item.as_json()})

    def _refund(self, path: str, body: Any) -> tuple[int, bytes]:
        request = (body or {}).get("request") if isinstance(body, dict) else None
        if not isinstance(request, dict) or not request.get("reason"):
            return 422, _j({"message": "Validation failed", "errors": {"reason": ["can't be blank"]}})
        parent = self.transactions.get(str(request.get("parent_uid")))
        apm = path.startswith("/beyag/")
        amount = request.get("amount")
        if not apm and not isinstance(amount, int):
            return 422, _j({"message": "Validation failed", "errors": {"amount": ["is required"]}})
        if (
            parent is None
            or parent.status != "successful"
            or (parent.method != "credit_card") != apm
            or parent.refunded + int(amount or parent.amount) > parent.amount
        ):
            return 422, _j(
                {
                    "message": "transaction can't be refunded",
                    "errors": {"base": ["Transaction can't be refunded"]},
                }
            )
        value = int(amount or parent.amount)
        parent.refunded += value
        self.refunds.append((path, dict(request)))
        refund = Transaction(
            uid=str(uuid.uuid4()),
            amount=value,
            currency=parent.currency,
            tracking_id=request.get("tracking_id"),
            status="successful",
            type="refund",
            method=parent.method,
            test=parent.test,
            parent_uid=parent.uid,
        )
        self.transactions[refund.uid] = refund
        return 200, _j({"transaction": refund.as_json()})

    # ------------------------------------------------------------------------------------- server

    async def start(self) -> None:
        app = web.Application()

        async def any_route(request: web.Request) -> web.Response:
            raw = await request.read()
            body = json.loads(raw) if raw else None
            status, payload = self.handle(
                request.method, "127.0.0.1", request.path, dict(request.headers), body
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

    async def __aenter__(self) -> FakeOverpay:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # ------------------------------------------------------------------------------ payer-side actions

    def add_token(self, tracking_id: str | None, amount: int = 17_900, *, test: bool = False) -> Token:
        item = Token(
            token=secrets.token_hex(32),
            shop_id=int(self.shop_id),
            amount=amount,
            currency="RUB",
            description="Пополнение баланса",
            tracking_id=tracking_id,
            test=test,
            expired_at=datetime.now(UTC) + timedelta(hours=24),
        )
        self.tokens[item.token] = item
        return item

    def _attempt(self, token: str, status: str, method: str, amount: int | None = None) -> Transaction:
        item = self.tokens[token]
        tx = Transaction(
            uid=str(uuid.uuid4()),
            amount=item.amount if amount is None else amount,
            currency=item.currency,
            tracking_id=item.tracking_id,
            status=status,
            method=method,
            test=item.test,
            paid_at=datetime.now(UTC) if status == "successful" else None,
        )
        item.payment = tx
        self.transactions[tx.uid] = tx
        return tx

    def pay_card(self, token: str, card: str = CARD_OK, *, amount: int | None = None) -> Transaction:
        """Documented test cards: ``4200000000000000`` → successful, ``4005550000000019`` → failed."""
        status = {CARD_OK: "successful", CARD_DECLINED: "failed"}[card]
        return self._attempt(token, status, "credit_card", amount)

    def pay_bogus(self, token: str) -> Transaction:
        """APM ``bogus``: ``0 < amount < 10000`` → successful, ``10000 < amount < 20000`` → failed."""
        amount = self.tokens[token].amount
        if 0 < amount < 10_000:
            status = "successful"
        elif 10_000 < amount < 20_000:
            status = "failed"
        else:
            raise ValueError("bogus amount outside the documented ranges")
        return self._attempt(token, status, "sbp")

    def pending_sbp(self, token: str) -> Transaction:
        return self._attempt(token, "pending", "sbp")

    def expire(self, token: str) -> Token:
        item = self.tokens[token]
        item.expired = True
        return item

    # ------------------------------------------------------------------------------------- webhooks

    def transaction_body(self, token: str) -> bytes:
        item = self.tokens[token]
        assert item.payment is not None
        return _j({"transaction": item.payment.as_json()})

    def checkout_body(self, token: str) -> bytes:
        return _j(self.tokens[token].as_json())

    def chargeback_body(self, token: str) -> bytes:
        item = self.tokens[token]
        assert item.payment is not None
        parent = item.payment.as_json()
        return _j(
            {
                "transaction": {
                    "uid": str(uuid.uuid4()),
                    "type": "chargeback",
                    "status": "successful",
                    "reason": "return",
                    "amount": item.amount,
                    "currency": item.currency,
                    "parent_uid": item.payment.uid,
                    "parent_transaction": parent,
                    "chargeback": {"reason_code": "4837", "comment": "No cardholder authorization"},
                    "test": item.test,
                }
            }
        )

    def signed(self, body: bytes, *, key: str | None = None, auth: str | None = None) -> WebhookRequest:
        return WebhookRequest(
            body=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": auth if auth is not None else basic(self.shop_id, self.secret_key),
                "Content-Signature": rsa_sign(body, key or self.key),
            },
        )
