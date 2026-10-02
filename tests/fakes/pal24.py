"""Fake Pal24 / PayPalych API for tests — built from our own specification (``docs/providers/pal24.md``).

* as the responder of :class:`~svbg.payments.testkit.CountingHttp` (``CountingHttp(fake)``): no sockets;
* as a real aiohttp server (``async with FakePal24() as desk: desk.base_url``).

Realistic where the plugin depends on it: ``Authorization: Bearer`` (``401 {"message": "Unauthenticated."}``
otherwise), form bodies; ``POST /api/v1/bill/create`` validates ``shop_id`` and the amount format and answers
``{"success": "true", link_url, link_page_url, bill_id}`` (``success`` as a string, as in the documentation);
``GET /api/v1/bill/status`` → Bill Resource + ``success`` (``403 api:error.bill_not_found`` for unknown ids);
``GET /api/v1/payment/status`` → Payment Resource with ``refunds[]`` / ``chargeback``; ``GET
/api/v1/bill/search`` → ``{data, links, meta}``. Postbacks are form bodies signed with an **independent**
implementation of ``strtoupper(md5(...))``. Fault injection: HTTP status + error key of the next answers,
an IP whitelist.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from aiohttp import ClientSession, ClientTimeout, web

from svbg.payments.testkit import HttpCall
from svbg.sdk import HttpResponse, WebhookRequest

API_TOKEN = "72|oBCB7Z3svbgTestToken0123456789"
OTHER_TOKEN = "73|anotherMerchantToken9876543210"
SHOP_ID = "LXZv3R7Q8B"
API_PATH = "/api/v1"
FAKE_BASE_URL = "https://pal24.fake"
FORM = {"Content-Type": "application/x-www-form-urlencoded"}


def md5_upper(*parts: str) -> str:
    """Independent implementation of the documented signatures (do not import the plugin's)."""
    return hashlib.md5(":".join(parts).encode("utf-8")).hexdigest().upper()


def postback(fields: Mapping[str, str]) -> WebhookRequest:
    return WebhookRequest(body=urlencode(dict(fields)).encode(), headers=FORM)


@dataclass
class Payment:
    id: str
    bill_id: str
    order_id: str
    status: str
    amount: str  # with the commission put on the payer
    bill_amount: str
    commission: str
    currency: str
    refunds: list[dict[str, Any]] = field(default_factory=list)
    chargeback: dict[str, Any] | None = None

    def as_json(self, *, refunds: bool, chargeback: bool) -> dict[str, Any]:
        refunded = sum((Decimal(r["amount"]) for r in self.refunds if r["status"] == "SUCCESS"), Decimal(0))
        data: dict[str, Any] = {
            "id": self.id,
            "bill_id": self.bill_id,
            "bill_order_id": self.order_id,
            "status": self.status,
            "amount": float(self.amount),
            "bill_amount": float(self.bill_amount),
            "commission": float(self.commission),
            "refunded_amount": float(refunded),
            "currency_in": self.currency,
            "account_amount": float(self.bill_amount),
            "account_currency_code": "RUB",
            "from_card": "553691******8079",
            "created_at": "2026-10-02 12:00:00",
            "success": True,
        }
        if refunds:
            data["refunds"] = list(self.refunds)
        if chargeback:
            data["chargeback"] = self.chargeback
        return data


@dataclass
class Bill:
    id: str
    order_id: str
    amount: str
    currency: str
    form: dict[str, str]
    status: str = "NEW"
    type: str = "NORMAL"
    payments: list[str] = field(default_factory=list)

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "order_id": self.order_id,
            "status": self.status,
            "type": self.type,
            "amount": float(self.amount),
            "currency_in": self.currency,
            "created_at": "2026-10-02 12:00:00",
            "ttl": int(self.form["ttl"]) if "ttl" in self.form else None,
        }


class FakePal24:
    """See module docstring."""

    def __init__(
        self, *, api_token: str = API_TOKEN, shop_id: str = SHOP_ID, base_url: str = FAKE_BASE_URL
    ) -> None:
        self.api_token = api_token
        self.shop_id = shop_id
        self._base_url = base_url.rstrip("/")
        self.bills: dict[str, Bill] = {}
        self.payments: dict[str, Payment] = {}
        self.created: list[dict[str, str]] = []
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.status_calls: list[tuple[str, str]] = []
        self.fail_with: tuple[int, str | None] | None = None
        self._bill_seq = itertools.count(1)
        self._pay_seq = itertools.count(1)
        self._runner: web.AppRunner | None = None
        self._port: int | None = None

    @property
    def base_url(self) -> str:
        if self._port is not None:
            return f"http://127.0.0.1:{self._port}"
        return self._base_url

    async def __call__(self, call: HttpCall) -> HttpResponse:
        form = call.data if isinstance(call.data, Mapping) else {}
        if isinstance(call.data, bytes | str):
            raw = call.data.decode() if isinstance(call.data, bytes) else call.data
            form = dict(parse_qsl(raw))
        status, payload = self.handle(
            call.method, urlsplit(call.url).path, call.headers, dict(call.params), dict(form)
        )
        return HttpResponse(status, payload, {"Content-Type": "application/json"})

    def handle(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str],
        query: Mapping[str, str],
        form: Mapping[str, str],
    ) -> tuple[int, bytes]:
        low = {k.lower(): v for k, v in headers.items()}
        method = method.upper()
        self.requests.append((method, path, low))
        if not path.startswith(API_PATH + "/"):
            return 404, b"<html>not found</html>"
        if low.get("authorization") != f"Bearer {self.api_token}":
            return 401, _j({"message": "Unauthenticated."})
        if self.fail_with is not None:
            code, key = self.fail_with
            return code, _j({"success": False, "message": key} if key else {"message": "Server Error"})
        rest = path[len(API_PATH) :]
        if method == "POST" and rest == "/bill/create":
            return self._create(form)
        if method == "GET" and rest == "/bill/status":
            self.status_calls.append(("bill", query.get("id", "")))
            bill = self.bills.get(query.get("id", ""))
            if bill is None:
                return 403, _j({"success": False, "message": "api:error.bill_not_found"})
            return 200, _j({**bill.as_json(), "success": True})
        if method == "GET" and rest == "/payment/status":
            self.status_calls.append(("payment", query.get("id", "")))
            payment = self.payments.get(query.get("id", ""))
            if payment is None:
                return 403, _j({"success": False, "message": "api:error.payment_not_found"})
            return 200, _j(
                payment.as_json(
                    refunds=query.get("refunds") == "1", chargeback=query.get("chargeback") == "1"
                )
            )
        if method == "GET" and rest == "/bill/search":
            if query.get("shop_id") not in (None, self.shop_id):
                return 403, _j({"success": False, "message": "api:error.access_denied"})
            items = [b.as_json() for b in self.bills.values()][: int(query.get("per_page", "15"))]
            return 200, _j(
                {
                    "data": items,
                    "links": {"prev": None, "next": None},
                    "meta": {"path": f"{self.base_url}{path}", "per_page": 1, "next_cursor": None},
                }
            )
        return 404, _j({"message": "Not Found"})

    def _create(self, form: Mapping[str, str]) -> tuple[int, bytes]:
        if not form.get("amount") or not form.get("shop_id"):
            return 422, _j({"message": "The amount field is required.", "errors": {}})
        if form["shop_id"] != self.shop_id:
            return 403, _j({"success": False, "message": "api:error.access_denied"})
        amount = form["amount"]
        parts = amount.split(".")
        if not (parts[0].isdigit() and (len(parts) == 1 or (len(parts) == 2 and 1 <= len(parts[1]) <= 2))):
            return 403, _j({"success": False, "message": "api:error.invalid_amount"})
        if Decimal(amount) <= 0:
            return 403, _j({"success": False, "message": "api:error.invalid_amount"})
        self.created.append(dict(form))
        bill_id = f"Bill{next(self._bill_seq):04d}"
        self.bills[bill_id] = Bill(
            id=bill_id,
            order_id=form.get("order_id", ""),
            amount=amount,
            currency=form.get("currency_in", "RUB"),
            form=dict(form),
            type=form.get("type", "normal").upper(),
        )
        return 200, _j(
            {
                "success": "true",
                "link_url": f"https://pally.fake/link/{bill_id}",
                "link_page_url": f"https://pally.fake/transfer/{bill_id}",
                "bill_id": bill_id,
            }
        )

    # ------------------------------------------------------------------------------------- server

    async def start(self) -> None:
        app = web.Application()

        async def any_route(request: web.Request) -> web.Response:
            form = dict(await request.post()) if request.method == "POST" else {}
            status, payload = self.handle(
                request.method,
                request.path,
                dict(request.headers),
                dict(request.query),
                {k: str(v) for k, v in form.items()},
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

    async def __aenter__(self) -> FakePal24:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # ------------------------------------------------------------------------------ desk-side actions

    def bill_for(self, order_id: str) -> Bill:
        for bill in self.bills.values():
            if bill.order_id == order_id:
                return bill
        raise KeyError(order_id)

    def pay(
        self, bill_id: str, status: str = "SUCCESS", *, paid: str | None = None, commission: str = "0.00"
    ) -> Payment:
        """The buyer pays ``paid`` (the bill amount by default) for the bill; the bill takes ``status``."""
        bill = self.bills[bill_id]
        bill.status = status
        pay_id = f"Pay{next(self._pay_seq):04d}"
        total = Decimal(paid or bill.amount) + Decimal(commission)
        payment = Payment(
            id=pay_id,
            bill_id=bill_id,
            order_id=bill.order_id,
            status=status,
            amount=f"{total:.2f}",
            bill_amount=bill.amount,
            commission=commission,
            currency=bill.currency,
        )
        self.payments[pay_id] = payment
        bill.payments.append(pay_id)
        return payment

    def payment_postback(
        self, payment: Payment, *, token: str | None = None, out_sum: str | None = None, **extra: str
    ) -> WebhookRequest:
        out = out_sum if out_sum is not None else payment.amount
        fields = {
            "InvId": payment.order_id,
            "OutSum": out,
            "Commission": payment.commission,
            "TrsId": payment.bill_id,
            "Status": payment.status,
            "CurrencyIn": payment.currency,
            "custom": "",
            "AccountType": "BANK_CARD",
            "AccountNumber": "553691******8079",
            "BalanceAmount": payment.bill_amount,
            "BalanceCurrency": "RUB",
            "PayerEmail": "buyer@example.com",
            "SignatureValue": md5_upper(out, payment.order_id, token or self.api_token),
        }
        fields.update(extra)
        return postback(fields)

    def refund(self, payment: Payment, amount: str | None = None, status: str = "SUCCESS") -> dict[str, Any]:
        refund = {
            "id": f"Ref{len(payment.refunds) + 1:04d}",
            "status": status,
            "amount": amount or payment.bill_amount,
            "currency": payment.currency,
            "entity_type": "payment",
            "entity_id": payment.id,
            "created_at": "2026-10-02 13:00:00",
        }
        payment.refunds.append(refund)
        return refund

    def refund_postback(
        self, payment: Payment, refund: Mapping[str, Any], *, token: str | None = None
    ) -> WebhookRequest:
        amount, currency = str(refund["amount"]), str(refund["currency"])
        return postback(
            {
                "Id": refund["id"],
                "Amount": amount,
                "Currency": currency,
                "Status": refund["status"],
                "InvId": payment.order_id,
                "BillId": payment.bill_id,
                "PaymentId": payment.id,
                "SignatureValue": md5_upper(
                    amount, currency, payment.bill_id, payment.id, refund["id"], token or self.api_token
                ),
            }
        )

    def chargeback(self, payment: Payment, status: str = "SUCCESS") -> dict[str, Any]:
        payment.chargeback = {
            "id": "Chb0001",
            "payment_id": payment.id,
            "status": status,
            "created_at": "2026-10-02 14:00:00",
        }
        return payment.chargeback

    def chargeback_postback(self, payment: Payment, *, token: str | None = None) -> WebhookRequest:
        cb = payment.chargeback or self.chargeback(payment)
        return postback(
            {
                "Id": cb["id"],
                "Status": cb["status"],
                "InvId": payment.order_id,
                "BillId": payment.bill_id,
                "PaymentId": payment.id,
                "SignatureValue": md5_upper(payment.bill_id, payment.id, cb["id"], token or self.api_token),
            }
        )

    async def send(self, url: str, req: WebhookRequest) -> int:
        async with (
            ClientSession(timeout=ClientTimeout(total=10)) as session,
            session.post(url, data=req.body, headers=FORM) as resp,
        ):
            await resp.read()
            return resp.status


def _j(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False).encode()
