"""Fake ParityPay API v2 for tests, built from our specification ``docs/providers/paritypay.md``.

* as the responder of :class:`~svbg.payments.testkit.CountingHttp` (``CountingHttp(fake)``): no sockets;
* as a real aiohttp server (``async with FakeParityPay() as desk: desk.base_url``).

Realistic where the plugin depends on it: ``X-ShopId`` + ``X-SecretKey`` (key No. 1; the HTTP code of auth
errors is not documented — the fake answers ``401 {"error": "SecretKey is incorrect"}``); errors are
``{"error": …}`` with ``400`` / ``404`` / ``422``; ``POST /v2/invoice/create`` (``order_id`` unique →
``422 "Order id is not unique"``, ``expire`` 1–43200, ``service`` sbp|card), ``GET /v2/invoice/status``
(``id`` or ``order_id``; ``404 "Invoice not found"``), ``GET /v2/invoice/list`` (newest first, ``per_page``
≤ 100, ``meta.last_page``), ``GET /v2/shop/balance``. Amounts in API answers are JSON numbers; in
notifications ``amount`` is a string and ``credited`` a number. The notification signature here is written
independently of the plugin: values sorted by key, joined, ``null`` → ``""``, HMAC-SHA256 with key No. 2.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from aiohttp import web

from svbg.payments.testkit import HttpCall
from svbg.sdk import HttpResponse, WebhookRequest

SHOP_ID = "874dfb1e-dbdb-4747-a1c0-005969725b74"
API_KEY = "pp-secret-1-0123456789abcdef"
WEBHOOK_KEY = "test_secret_key_2"  # the key of the specification's vectors (§10)
FAKE_BASE_URL = "https://api.paritypay.fake"
_STATUSES = ("NEW", "PAID", "EXPIRED", "ERROR", "REFUNDED")


def sign(key: str, body: Mapping[str, str | None]) -> str:
    """Reference signature over already-textual values (numbers given as their JSON literals)."""
    message = "".join("" if body[k] is None else str(body[k]) for k in sorted(body))
    return hmac.new(key.encode(), message.encode(), hashlib.sha256).hexdigest()


@dataclass
class Invoice:
    id: str
    order_id: str
    amount: Any  # a JSON number, as the API reports it
    status: str = "NEW"
    comment: str | None = None
    service: str | None = None
    custom_fields: str | None = None
    created: str = "2026-10-02 12:40:00"
    expires: str = "2026-10-02 13:40:00"
    seq: int = 0
    body: dict[str, Any] = field(default_factory=dict)

    def as_json(self, shop_id: str) -> dict[str, Any]:
        return {
            "id": self.id,
            "order_id": self.order_id,
            "shop_id": shop_id,
            "amount": self.amount,
            "comment": self.comment,
            "service": self.service,
            "custom_fields": self.custom_fields,
            "expires": self.expires,
            "created": self.created,
            "status": self.status,
            "link": f"https://pay.paritypay.fake/{self.id}",
        }

    def as_list_item(self, shop_id: str) -> dict[str, Any]:
        return {
            "id": self.id,
            "order_id": self.order_id,
            "shop_id": shop_id,
            "amount": self.amount,
            "comment": self.comment,
            "service": self.service,
            "custom_fields": self.custom_fields,
            "status": self.status,
            "created_at": self.created.replace(" ", "T") + "+03:00",
            "expires_at": self.expires.replace(" ", "T") + "+03:00",
            "paid_at": "2026-10-02T12:45:00+03:00" if self.status in ("PAID", "REFUNDED") else None,
            "link": f"https://pay.paritypay.fake/{self.id}",
        }


class FakeParityPay:
    """See module docstring."""

    def __init__(
        self,
        *,
        shop_id: str = SHOP_ID,
        api_key: str = API_KEY,
        webhook_key: str = WEBHOOK_KEY,
        base_url: str = FAKE_BASE_URL,
        currency: str = "RUB",
    ) -> None:
        self.shop_id = shop_id
        self.api_key = api_key
        self.webhook_key = webhook_key
        self.currency = currency
        self._base_url = base_url.rstrip("/")
        self.invoices: dict[str, Invoice] = {}
        self.created: list[dict[str, Any]] = []
        self.status_calls: list[dict[str, str]] = []
        self.list_calls: list[dict[str, str]] = []
        self.fail_with: int | None = None
        self.lose_next_create_answer = False
        self._seq = 0
        self._runner: web.AppRunner | None = None
        self._port: int | None = None

    @property
    def base_url(self) -> str:
        if self._port is not None:
            return f"http://127.0.0.1:{self._port}"
        return self._base_url

    async def __call__(self, call: HttpCall) -> HttpResponse:
        status, payload = self.handle(
            call.method, urlsplit(call.url).path, call.headers, call.params, call.json
        )
        return HttpResponse(status, payload, {"Content-Type": "application/json"})

    def handle(
        self, method: str, path: str, headers: Mapping[str, str], params: Mapping[str, str], body: Any
    ) -> tuple[int, bytes]:
        low = {k.lower(): v for k, v in headers.items()}
        if self.fail_with is not None:
            return self.fail_with, _j({"error": "fault"})
        if not hmac.compare_digest(low.get("x-shopid", ""), self.shop_id):
            return 401, _j({"error": "ShopId is incorrect"})
        if not hmac.compare_digest(low.get("x-secretkey", ""), self.api_key):
            return 401, _j({"error": "SecretKey is incorrect"})
        method = method.upper()
        if method == "POST" and path == "/v2/invoice/create":
            return self._create(body)
        if method == "GET" and path == "/v2/invoice/status":
            return self._status(dict(params))
        if method == "GET" and path == "/v2/invoice/list":
            return self._list(dict(params))
        if method == "GET" and path == "/v2/shop/balance":
            return 200, _j({"balance": 1250.5, "balance_hold": 0, "currency": self.currency})
        return 404, b"<html>not found</html>"

    def _create(self, body: Any) -> tuple[int, bytes]:
        if not isinstance(body, dict):
            return 400, _j({"error": "json required"})
        order_id = body.get("order_id")
        if not isinstance(order_id, str) or not 1 <= len(order_id) <= 255:
            return 400, _j({"error": "Параметр order_id обязателен"})
        amount = body.get("amount")
        if isinstance(amount, bool) or not isinstance(amount, int | float):
            return 400, _j({"error": "Параметр amount обязателен"})
        expire = body.get("expire", 60)
        if expire is not None and (not isinstance(expire, int) or not 1 <= expire <= 43_200):
            return 400, _j({"error": "Параметр expire должен быть от 1 до 43200"})
        service = body.get("service")
        if service is not None and service not in ("sbp", "card"):
            return 422, _j({"error": f"Service '{service}' is not a valid."})
        for key, limit in (("comment", 255), ("custom_fields", 255), ("success_url", 500), ("fail_url", 500),
                           ("callback_url", 500)):  # fmt: skip
            value = body.get(key)
            if value is not None and (not isinstance(value, str) or len(value) > limit):
                return 400, _j({"error": f"Параметр {key} слишком длинный"})
        if any(inv.order_id == order_id for inv in self.invoices.values()):
            return 422, _j({"error": "Order id is not unique"})
        self.created.append(dict(body))
        inv = self.add(str(uuid.uuid4()), order_id, amount)
        inv.comment = body.get("comment")
        inv.service = service
        inv.body = dict(body)
        if self.lose_next_create_answer:
            self.lose_next_create_answer = False
            return 504, b"<html>gateway timeout</html>"
        return 200, _j(inv.as_json(self.shop_id))

    def _status(self, params: dict[str, str]) -> tuple[int, bytes]:
        self.status_calls.append(params)
        if ("id" in params) == ("order_id" in params):
            return 400, _j({"error": "Нужен один из параметров id или order_id"})
        if "id" in params:
            inv = self.invoices.get(params["id"])
        else:
            inv = next((i for i in self.invoices.values() if i.order_id == params["order_id"]), None)
        if inv is None:
            return 404, _j({"error": "Invoice not found"})
        return 200, _j(inv.as_json(self.shop_id))

    def _list(self, params: dict[str, str]) -> tuple[int, bytes]:
        self.list_calls.append(params)
        try:
            page = int(params.get("page", "1"))
            per_page = int(params.get("per_page", "25"))
        except ValueError:
            return 400, _j({"error": "bad page"})
        if per_page > 100:
            return 400, _j({"error": "Максимальное значение per_page 100"})
        if page < 1 or per_page < 1:
            return 400, _j({"error": "bad page"})
        rows = sorted(self.invoices.values(), key=lambda i: i.seq, reverse=True)
        if "status" in params:
            rows = [r for r in rows if r.status == params["status"].upper()]
        total = len(rows)
        last = max(1, -(-total // per_page))
        chunk = rows[(page - 1) * per_page : page * per_page]
        return 200, _j({
            "invoices": [r.as_list_item(self.shop_id) for r in chunk],
            "meta": {"current_page": page, "last_page": last, "per_page": per_page,
                     "to": (page - 1) * per_page + len(chunk), "total": total},
        })  # fmt: skip

    # ------------------------------------------------------------------------------------- server

    async def start(self) -> None:
        app = web.Application()

        async def any_route(request: web.Request) -> web.Response:
            raw = await request.read()
            body = json.loads(raw) if raw else None
            status, payload = self.handle(
                request.method, request.path, dict(request.headers), dict(request.query), body
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

    async def __aenter__(self) -> FakeParityPay:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # ------------------------------------------------------------------------------ desk-side actions

    def add(self, invoice_id: str, order_id: str, amount: Any = 179, status: str = "NEW") -> Invoice:
        assert status in _STATUSES
        self._seq += 1
        inv = Invoice(id=invoice_id, order_id=order_id, amount=amount, status=status, seq=self._seq)
        self.invoices[invoice_id] = inv
        return inv

    def set_status(self, invoice_id: str, status: str) -> Invoice:
        assert status in _STATUSES
        inv = self.invoices[invoice_id]
        inv.status = status
        return inv

    def notification(
        self,
        invoice_id: str,
        status: str | None = None,
        *,
        amount: str | None = None,
        credited: str = "173.63",
        key: str | None = None,
    ) -> WebhookRequest:
        """An invoice notification as ParityPay sends it: ``amount`` a string with two decimals, ``credited``
        a JSON number (``credited`` is given as its literal)."""
        inv = self.invoices[invoice_id]
        textual: dict[str, str | None] = {
            "id": inv.id,
            "order_id": inv.order_id,
            "shop_id": self.shop_id,
            "amount": amount if amount is not None else f"{float(inv.amount):.2f}",
            "credited": credited,
            "comment": inv.comment,
            "service": inv.service or "sbp",
            "custom_fields": inv.custom_fields,
            "expires": inv.expires,
            "created": inv.created,
            "status": status or inv.status,
        }
        signature = sign(key or self.webhook_key, textual)
        parts = []
        for name, value in textual.items():
            if name == "credited":
                parts.append(f'"credited":{value}')
            else:
                parts.append(f"{json.dumps(name)}:{json.dumps(value, ensure_ascii=False)}")
        body = ("{" + ",".join(parts) + "}").encode()
        return WebhookRequest(
            body=body, headers={"Content-Type": "application/json", "X-SIGNATURE": signature}
        )


def _j(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False).encode()
