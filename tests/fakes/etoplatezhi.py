"""Fake «Это платежи» (Payment Page + Gate status) for tests — built from ``docs/providers/etoplatezhi.md``.

* as the responder of :class:`~svbg.payments.testkit.CountingHttp` (``CountingHttp(fake)``): no sockets;
* as a real aiohttp server (``async with FakeEtoplatezhi() as desk: desk.base_url``).

Realistic where the plugin depends on it: every request and answer is signed by the platform's algorithm
(implemented here independently of the plugin, after the official SDK's description); ``POST
/v2/payment/status`` checks ``general.signature`` (a bad one → ``400``, the real code is not documented),
answers a signed ``200`` with ``errors[0].code = "3061"`` for a payment nobody confirmed and the signed
payment with ``sum`` in kopecks otherwise; ``ip_blocked`` → ``403``. A Payment Page link is «opened» with
:meth:`FakeEtoplatezhi.open_form`, which checks the link's signature and registers the payment.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from aiohttp import web

from svbg.payments.testkit import HttpCall
from svbg.sdk import HttpResponse, WebhookRequest

PROJECT_ID = 12345
SECRET_KEY = "secret"  # the key of the spec's own vectors (§12, O1–O3)
FAKE_API_URL = "https://api.etoplatezhi.fake"
FAKE_PAGE_URL = "https://paymentpage.etoplatezhi.fake"


def _lines(value: Any, path: list[str], out: list[tuple[str, str]], in_array: bool) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            if (k == "signature" and not in_array) or (not path and k == "frame_mode"):
                continue
            _lines(v, [*path, k.replace(":", "::")], out, False)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _lines(v, [*path, str(i)], out, True)
    else:
        text = "" if value is None else ("1" if value is True else "0" if value is False else str(value))
        out.append((":".join(path), text))


def signature(data: Mapping[str, Any], key: str = SECRET_KEY) -> str:
    out: list[tuple[str, str]] = []
    _lines(dict(data), [], out, False)
    message = ";".join(f"{p}:{v}" for p, v in sorted(out))
    return base64.b64encode(hmac.new(key.encode(), message.encode(), hashlib.sha512).digest()).decode()


def signed_body(data: Mapping[str, Any], key: str = SECRET_KEY) -> dict[str, Any]:
    return {**data, "signature": signature(data, key)}


@dataclass
class Payment:
    id: str
    amount: int  # kopecks
    currency: str = "RUB"
    status: str = "success"
    method: str = "sbp-qr"
    description: str = ""
    customer_id: str = "c_9f2a"
    params: dict[str, str] = field(default_factory=dict)
    operation_id: int = 5028800010128999

    def as_payment(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "purchase",
            "status": self.status,
            "date": "2026-10-02T12:10:05+0000",
            "method": self.method,
            "sum": {"amount": self.amount, "currency": self.currency},
            "description": self.description,
        }

    def operation(self) -> dict[str, Any]:
        op_type = "refund" if "refund" in self.status else "reversal" if "reversed" in self.status else "sale"
        op_status = "success" if self.status != "decline" else "decline"
        return {
            "id": self.operation_id,
            "type": op_type,
            "status": op_status,
            "date": "2026-10-02T12:10:05+0000",
            "created_date": "2026-10-02T12:09:40+0000",
            "request_id": "abc123-def456",
            "sum_initial": {"amount": self.amount, "currency": self.currency},
            "sum_converted": {"amount": self.amount, "currency": self.currency},
            "provider": {"id": 6, "payment_id": "A1B2C3", "auth_code": ""},
            "code": "0" if op_status == "success" else "100",
            "message": "Success" if op_status == "success" else "General decline",
        }


class FakeEtoplatezhi:
    """See module docstring."""

    def __init__(
        self,
        *,
        project_id: int = PROJECT_ID,
        secret_key: str = SECRET_KEY,
        api_url: str = FAKE_API_URL,
    ) -> None:
        self.project_id = project_id
        self.secret_key = secret_key
        self._api_url = api_url.rstrip("/")
        self.payments: dict[str, Payment] = {}
        self.status_calls: list[str] = []
        self.ip_blocked = False
        self.fail_with: int | None = None
        self.unsigned_answers = False
        self._runner: web.AppRunner | None = None
        self._port: int | None = None

    @property
    def api_url(self) -> str:
        if self._port is not None:
            return f"http://127.0.0.1:{self._port}"
        return self._api_url

    async def __call__(self, call: HttpCall) -> HttpResponse:
        status, payload = self.handle(call.method, urlsplit(call.url).path, call.json)
        return HttpResponse(status, payload, {"Content-Type": "application/json"})

    def handle(self, method: str, path: str, body: Any) -> tuple[int, bytes]:
        if self.fail_with is not None:
            return self.fail_with, b"<html>fault</html>"
        if self.ip_blocked:
            return 403, _j({"status": "error", "code": "403", "message": "Forbidden"})
        if method.upper() != "POST" or path != "/v2/payment/status":
            return 404, _j({"status": "error", "message": "Not found"})
        general = body.get("general") if isinstance(body, dict) else None
        if not isinstance(general, dict) or not general.get("payment_id") or not general.get("project_id"):
            return 400, _j({"status": "error", "code": "2004", "message": "Required field not provided"})
        given = str(general.get("signature") or "")
        if not hmac.compare_digest(given, signature(body, self.secret_key)):
            return 400, _j({"status": "error", "code": "2010", "message": "Signature is invalid"})
        if general["project_id"] != self.project_id:
            return 400, _j({"status": "error", "code": "2012", "message": "Project not found"})
        payment_id = str(general["payment_id"])
        self.status_calls.append(payment_id)
        found = self.payments.get(payment_id.lower())
        if found is None:
            answer: dict[str, Any] = {
                "payment": {"status": "error"},
                "errors": [{"code": "3061", "message": "Transaction not found"}],
            }
        else:
            answer = {
                "project_id": self.project_id,
                "payment": found.as_payment(),
                "operations": [found.operation()],
                "customer": {"id": found.customer_id},
                "account": {"number": "79*******01"},
                "errors": [],
            }
        if self.unsigned_answers:
            return 200, _j(answer)
        return 200, _j(signed_body(answer, self.secret_key))

    # ------------------------------------------------------------------------------------- server

    async def start(self) -> None:
        app = web.Application()

        async def any_route(request: web.Request) -> web.Response:
            raw = await request.read()
            body = json.loads(raw) if raw else None
            status, payload = self.handle(request.method, request.path, body)
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

    async def __aenter__(self) -> FakeEtoplatezhi:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # ------------------------------------------------------------------------------ desk-side actions

    @staticmethod
    def form_params(pay_url: str) -> tuple[dict[str, str], str]:
        """Parameters of a Payment Page link and its signature."""
        pairs = parse_qsl(urlsplit(pay_url).query, keep_blank_values=True)
        params = {k: v for k, v in pairs if k != "signature"}
        sig = next(v for k, v in pairs if k == "signature")
        return params, sig

    def open_form(self, pay_url: str, *, status: str = "processing") -> Payment:
        """The user opens the link and confirms: the platform checks the signature, registers the payment."""
        params, sig = self.form_params(pay_url)
        if not hmac.compare_digest(sig, signature(params, self.secret_key)):
            raise AssertionError("the Payment Page link is not signed by the project key")
        if int(params["project_id"]) != self.project_id:
            raise AssertionError("foreign project")
        payment = Payment(
            id=params["payment_id"],
            amount=int(params["payment_amount"]),
            currency=params["payment_currency"],
            status=status,
            method=params.get("force_payment_method", "card"),
            description=params.get("payment_description", ""),
            customer_id=params["customer_id"],
            params=params,
        )
        self.payments[payment.id.lower()] = payment
        return payment

    def add(
        self, payment_id: str, amount: int = 17_900, status: str = "success", currency: str = "RUB"
    ) -> Payment:
        payment = Payment(id=payment_id, amount=amount, status=status, currency=currency)
        self.payments[payment_id.lower()] = payment
        return payment

    def set_status(self, payment_id: str, status: str, *, amount: int | None = None) -> Payment:
        payment = self.payments[payment_id.lower()]
        payment.status = status
        if amount is not None:
            payment.amount = amount
        return payment

    def callback_body(self, payment_id: str, status: str | None = None, *, amount: int | None = None) -> dict:
        payment = self.payments[payment_id.lower()]
        body = {
            "project_id": self.project_id,
            "payment": {**payment.as_payment(), **({"status": status} if status else {})},
            "customer": {"id": payment.customer_id},
            "account": {"number": "79*******01"},
            "operation": payment.operation(),
            "errors": [],
        }
        if amount is not None:
            body["payment"]["sum"]["amount"] = amount
        return body

    def callback(
        self, payment_id: str, status: str | None = None, *, amount: int | None = None, key: str | None = None
    ) -> WebhookRequest:
        body = signed_body(self.callback_body(payment_id, status, amount=amount), key or self.secret_key)
        return WebhookRequest(body=_j(body), headers={"Content-Type": "application/json"})


def _j(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False).encode()
