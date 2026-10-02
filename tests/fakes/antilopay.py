"""Fake Antilopay H2H API for tests — built from our own specification (``docs/providers/antilopay.md``).

* as the responder of :class:`~svbg.payments.testkit.CountingHttp` (``CountingHttp(fake)``): no sockets;
* as a real aiohttp server (``async with FakeAntilopay() as desk: desk.base_url``).

Realistic where the plugin depends on it: every POST must carry ``X-Apay-Secret-Id`` (code 1/2 otherwise),
``X-Apay-Sign-Version: 1`` and ``X-Apay-Sign`` = RSA-SHA256 PKCS#1 v1.5 over the **raw request bytes**,
checked by an independent implementation (``cryptography``) with the merchant's public key (code 3); answers
are HTTP 200 with a ``code`` field; ``order_id`` is unique per project (code 5); ``payment/check`` finds
payments only by ``order_id`` (code 8); amounts must be numbers with ≤ 2 decimals (code 13); the customer
needs an email or a phone (code 10). Callbacks are signed with the project's callback key (512-bit by
default, as documented, or 2048-bit) and come from Antilopay's published address. Fault injection: HTTP
status or ``code`` of the next answers.
"""

from __future__ import annotations

import base64
import itertools
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from functools import cache
from typing import Any
from urllib.parse import urlsplit

from aiohttp import ClientSession, ClientTimeout, web
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from svbg.payments.testkit import HttpCall
from svbg.sdk import HttpResponse, WebhookRequest

SECRET_ID = "MRCH-0123456789abcdef"
PROJECT_ID = "PE8BED46C045139256"
CUSTOMER_EMAIL = "support@shop.example"
FAKE_BASE_URL = "https://lk.antilopay.fake/api/v1/"
BASE_PATH = "/api/v1/"
ANTILOPAY_IP = "81.177.221.226"
MSK = timezone(timedelta(hours=3))

#: The example merchant key of the document (§3.4, 512-bit PKCS#8) with its three Cyrillic «а» replaced by
#: the Latin ``a`` (spec §6), and the public key computed from it.
DOC_PRIVATE_KEY = (
    "MIIBVAIBADANBgkqhkiG9w0BAQEFAASCAT4wggE6AgEAAkEAiuIjIP1l2as57VabZaj2xfjevMJO3RKJlBqJGb9ZirHxOQkq+xCHEQ3a"
    "YlcujmhbvfcZP228yZIQZbkyJAoaaQIDAQABAkABm3egMX044/aICMMvDbcqaB84HxsPdlVq23+X8XsZWUH7M1/lvYuG2fQ/dSFB6tc4"
    "VvxplM3P0TLfQ/Da5bkBAiEA5TwxwIe/poNPc416JBkD3c3UpkRBOsC5DDhoz3QNfWkCIQCbGVc/xU/qQBdHQqg6zrEFnn78q1wxCyL0"
    "hbJXEbsVAQIhALv0QfrRkyNNUQy2uKn2VMQ9axk0p6Mrt848RjuqtRDZAiA6qu077BD8lN25UNd91y1S6M80GEW5L3M7d08sbEKOAQIg"
    "JHO32eDzMaJcO/8arwbI6jhBvPg4QvqcVofcdUG1rq0="
)
DOC_PUBLIC_KEY = (
    "MFwwDQYJKoZIhvcNAQEBBQADSwAwSAJBAIriIyD9ZdmrOe1Wm2Wo9sX43rzCTt0SiZQaiRm/WYqx8TkJKvsQhxEN2mJXLo5oW733GT9t"
    "vMmSEGW5MiQKGmkCAwEAAQ=="
)


@cache
def keypair(name: str) -> rsa.RSAPrivateKey:
    """``doc`` — the document's 512-bit key; ``merchant``, ``project``, ``other`` — fresh 2048-bit keys."""
    if name == "doc":
        key = serialization.load_der_private_key(base64.b64decode(DOC_PRIVATE_KEY), None)
        assert isinstance(key, rsa.RSAPrivateKey)
        return key
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def private_b64(name: str) -> str:
    """Base64 DER PKCS#8, as the Antilopay cabinet issues it."""
    der = keypair(name).private_bytes(
        serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    return base64.b64encode(der).decode()


def public_b64(name: str) -> str:
    """Base64 DER SubjectPublicKeyInfo."""
    der = (
        keypair(name)
        .public_key()
        .public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    )
    return base64.b64encode(der).decode()


def rsa_sign(body: bytes, name: str) -> str:
    """Independent implementation of the Antilopay signature (do not import the plugin's)."""
    return base64.b64encode(keypair(name).sign(body, padding.PKCS1v15(), hashes.SHA256())).decode()


def rsa_verify(body: bytes, signature_b64: str, name: str) -> bool:
    try:
        keypair(name).public_key().verify(
            base64.b64decode(signature_b64, validate=True), body, padding.PKCS1v15(), hashes.SHA256()
        )
    except (InvalidSignature, ValueError):
        return False
    return True


def msk_time(at: datetime | None = None) -> str:
    return (at or datetime.now(UTC)).astimezone(MSK).strftime("%Y-%m-%d %H:%M:%S.%f")


@dataclass
class Payment:
    payment_id: str
    order_id: str
    amount: Decimal
    status: str = "PENDING"
    fee: Decimal = Decimal("0")
    ctime: str = field(default_factory=msk_time)
    body: dict[str, Any] = field(default_factory=dict)
    refunds: list[dict[str, Any]] = field(default_factory=list)

    @property
    def payment_url(self) -> str:
        return f"https://gate.antilopay.fake/payment/{self.payment_id}"

    def as_json(self) -> dict[str, Any]:
        return {
            "code": 0,
            "payment_id": self.payment_id,
            "order_id": self.order_id,
            "payment_url": self.payment_url,
            "ctime": self.ctime,
            "amount": self.amount - self.fee,
            "original_amount": self.amount,
            "fee": self.fee,
            "status": self.status,
            "currency": "RUB",
            "product_name": self.body.get("product_name"),
            "merchant_extra": None,
            "description": self.body.get("description"),
            "pay_method": "SBP" if self.status != "PENDING" else None,
            "pay_data": "+7 9**-***-12-34" if self.status != "PENDING" else None,
            "recurrent_id": None,
            "customer_ip": None,
            "customer_useragent": None,
            "customer": self.body.get("customer"),
            "refunds": list(self.refunds),
        }

    def callback_payload(self) -> dict[str, Any]:
        data = self.as_json()
        data.pop("code")
        data.pop("payment_url")
        data.pop("refunds")
        return {"type": "payment", **data}


class FakeAntilopay:
    """See module docstring."""

    def __init__(
        self,
        *,
        secret_id: str = SECRET_ID,
        project_id: str = PROJECT_ID,
        merchant_key: str = "merchant",
        callback_key: str = "doc",
        base_url: str = FAKE_BASE_URL,
    ) -> None:
        self.secret_id = secret_id
        self.project_id = project_id
        self.merchant_key = merchant_key
        self.callback_key = callback_key
        self._base_url = base_url
        self.payments: dict[str, Payment] = {}
        self.created: list[dict[str, Any]] = []
        self.raw_bodies: list[bytes] = []
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.status_calls: list[str] = []
        self.fail_with: int | None = None
        self.fail_code: int | None = None
        self.fail_times = 1_000_000
        self.lose_create_answer = False
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
        raw = call.data if isinstance(call.data, bytes) else b""
        status, payload = self.handle(call.method, urlsplit(call.url).path, call.headers, raw)
        return HttpResponse(
            status,
            payload,
            {"Content-Type": "application/json", "X-Apay-Request-Id": f"req-{next(self._seq)}"},
        )

    def handle(self, method: str, path: str, headers: Mapping[str, str], raw: bytes) -> tuple[int, bytes]:
        low = {k.lower(): v for k, v in headers.items()}
        self.requests.append((method.upper(), path, low))
        if self.fail_with is not None and self.fail_times > 0:
            self.fail_times -= 1
            return self.fail_with, b"<html>Bad Gateway</html>"
        if not path.startswith(BASE_PATH):
            return 404, b"<html>not found</html>"
        name = path[len(BASE_PATH) :].strip("/")
        if method.upper() != "POST":
            return 405, b"<html>method not allowed</html>"
        self.raw_bodies.append(raw)
        if not raw:
            return 200, _j({"code": 100, "error": "Empty request"})
        if "x-apay-secret-id" not in low:
            return 200, _j({"code": 1, "error": "X-Apay-Secret-Id not found"})
        if low["x-apay-secret-id"] != self.secret_id:
            return 200, _j({"code": 2, "error": "Invalid X-Apay-Secret-Id"})
        if low.get("x-apay-sign-version") != "1" or not rsa_verify(
            raw, low.get("x-apay-sign", ""), self.merchant_key
        ):
            return 200, _j({"code": 3, "error": "Invalid signature"})
        try:
            body = json.loads(raw.decode("utf-8"), parse_float=Decimal)
        except (UnicodeDecodeError, ValueError):
            return 200, _j({"code": 16, "error": "Invalid JSON"})
        if self.fail_code is not None and name != "signature/check":
            code, self.fail_code = self.fail_code, None
            return 200, _j({"code": code, "error": "fault"})
        if name == "signature/check":
            return 200, _j({"status": "ok"})
        if body.get("project_identificator") != self.project_id:
            return 200, _j({"code": 4, "error": "Project not found"})
        if name == "payment/create":
            return self._create(body)
        if name == "payment/check":
            return self._check(body)
        if name == "refund/create":
            return self._refund(body)
        if name == "refund/check":
            return self._refund_check(body)
        return 404, b"<html>not found</html>"

    def _create(self, body: dict[str, Any]) -> tuple[int, bytes]:
        amount = body.get("amount")
        if isinstance(amount, bool) or not isinstance(amount, int | Decimal):
            return 200, _j({"code": 13, "error": "Invalid amount"})
        amount = Decimal(amount)
        if amount <= 0 or amount != amount.quantize(Decimal("0.01")):
            return 200, _j({"code": 13, "error": "Invalid amount"})
        customer = body.get("customer")
        if not isinstance(customer, dict) or not (customer.get("email") or customer.get("phone")):
            return 200, _j({"code": 10, "error": "Customer data must contain email or phone"})
        for key in ("order_id", "product_name", "product_type", "description", "currency"):
            if not body.get(key):
                return 200, _j({"code": 15, "error": f"Missing parameter {key}"})
        if str(body["currency"]).upper() != "RUB":
            return 200, _j({"code": 15, "error": "Invalid currency"})
        order = str(body["order_id"])
        if order in self.payments:
            return 200, _j({"code": 5, "error": "order_id is not unique"})
        self.created.append(dict(body))
        payment = Payment(payment_id=f"APAY{next(self._seq):021d}", order_id=order, amount=amount, body=body)
        self.payments[order] = payment
        if self.lose_create_answer:
            self.lose_create_answer = False
            return 502, b"<html>Bad Gateway</html>"
        return 200, _j({"code": 0, "payment_id": payment.payment_id, "payment_url": payment.payment_url})

    def _check(self, body: dict[str, Any]) -> tuple[int, bytes]:
        order = str(body.get("order_id") or "")
        self.status_calls.append(order)
        payment = self.payments.get(order)
        if payment is None:
            return 200, _j({"code": 8, "error": "Payment not found"})
        return 200, _j(payment.as_json())

    def _refund(self, body: dict[str, Any]) -> tuple[int, bytes]:
        payment = next(
            (p for p in self.payments.values() if p.payment_id == body.get("transaction_id")), None
        )
        if payment is None or payment.status != "SUCCESS":
            return 200, _j({"code": 9, "error": "This payment can not be refunded"})
        order = body.get("order_id")
        if order and any(r["order_id"] == order for p in self.payments.values() for r in p.refunds):
            return 200, _j({"code": 18, "error": "order_id is not unique"})
        refund = {
            "refund_id": f"RFND{next(self._seq):021d}",
            "order_id": order,
            "payment_id": payment.payment_id,
            "amount": Decimal(body["amount"]),
            "status": "COMPLETE",
        }
        payment.refunds.append(refund)
        return 200, _j({"code": 0, "refund_id": refund["refund_id"]})

    def _refund_check(self, body: dict[str, Any]) -> tuple[int, bytes]:
        for payment in self.payments.values():
            for refund in payment.refunds:
                if body.get("order_id") in (None, refund["order_id"]) and body.get("refund_id") in (
                    None,
                    refund["refund_id"],
                ):
                    return 200, _j({"code": 0, **refund})
        return 200, _j({"code": 8, "error": "Refund not found"})

    # ------------------------------------------------------------------------------------- server

    async def start(self) -> None:
        app = web.Application()

        async def any_route(request: web.Request) -> web.Response:
            raw = await request.read()
            status, payload = self.handle(request.method, request.path, dict(request.headers), raw)
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

    async def __aenter__(self) -> FakeAntilopay:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # ------------------------------------------------------------------------------ desk-side actions

    def add(self, order_id: str, amount: str | Decimal, *, status: str = "PENDING") -> Payment:
        """A payment created in the cabinet / by another client."""
        payment = Payment(
            payment_id=f"APAY{next(self._seq):021d}", order_id=order_id, amount=Decimal(amount), status=status
        )
        self.payments[order_id] = payment
        return payment

    def pay(self, order_id: str, status: str = "SUCCESS") -> Payment:
        payment = self.payments[order_id]
        payment.status = status
        if status == "SUCCESS":
            payment.fee = (payment.amount * Decimal("0.03")).quantize(Decimal("0.01"))
        return payment

    # ------------------------------------------------------------------------------------- callbacks

    def callback_parts(
        self, payment: Payment, *, key: str | None = None, extra: Mapping[str, Any] | None = None
    ) -> tuple[bytes, dict[str, str]]:
        payload = payment.callback_payload()
        payload.update(extra or {})
        body = json.dumps(payload, ensure_ascii=False, default=_number).encode()
        return body, {
            "Content-Type": "application/json",
            "X-Apay-Callback": rsa_sign(body, key or self.callback_key),
            "X-Apay-Callback-Version": "1",
        }

    def callback(self, payment: Payment, **kw: Any) -> WebhookRequest:
        body, headers = self.callback_parts(payment, **kw)
        return WebhookRequest(body=body, headers=headers, remote=ANTILOPAY_IP)

    async def send_callback(self, url: str, payment: Payment, **kw: Any) -> int:
        body, headers = self.callback_parts(payment, **kw)
        async with (
            ClientSession(timeout=ClientTimeout(total=10)) as session,
            session.post(url, data=body, headers=headers) as resp,
        ):
            await resp.read()
            return resp.status


def _number(value: Any) -> Any:
    """Decimal → JSON number (amounts have ≤ 2 decimals, so a float prints exactly)."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    raise TypeError(type(value).__name__)


def _j(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False, default=_number).encode()
