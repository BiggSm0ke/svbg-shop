"""Fake Freekassa (SCI form + API v1) for tests, built from our specification ``docs/providers/freekassa.md``.

* as the responder of :class:`~svbg.payments.testkit.CountingHttp` (``CountingHttp(fake)``): no sockets;
* as a real aiohttp server (``async with FakeFreekassa() as desk: desk.api_url``).

Realistic where the plugin depends on it: API requests are JSON with ``shopId``, a strictly increasing
``nonce`` (a repeated or smaller one → 400) and ``signature = HMAC_SHA256(api_key, "|".join(sorted values))``
(401 otherwise); ``/orders/create`` requires ``i``, ``email``, ``ip``, ``amount``, ``currency``; ``/orders``
filters by ``paymentId`` and answers ``amount`` as a JSON number; ``/orders/refund``; ``/balance``. The form
(``open_form``) checks ``s = md5(m:oa:secret:currency:o)`` like the real page. Notifications are form-data
(urlencoded or multipart) with ``SIGN = md5(MERCHANT_ID:AMOUNT:secret2:MERCHANT_ORDER_ID)`` from one of the
published IPs. Signature code here is independent of the plugin's.
"""

from __future__ import annotations

import hashlib
import hmac
import itertools
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from aiohttp import ClientSession, ClientTimeout, web

from svbg.payments.testkit import HttpCall
from svbg.sdk import HttpResponse, WebhookRequest

SHOP_ID = "7012"
SECRET_WORD = "fk-secret-1-0123456789"
SECRET_WORD_2 = "fk-secret-2-abcdef0123"
API_KEY = "fk-api-key-0123456789abcdef0123456789"
FAKE_API_URL = "https://api.freekassa.fake/v1"
FREEKASSA_IP = "168.119.157.136"
_API_PATH = "/v1"


def md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


def api_sign(api_key: str, data: Mapping[str, Any]) -> str:
    return hmac.new(
        api_key.encode(), "|".join(str(data[k]) for k in sorted(data)).encode(), hashlib.sha256
    ).hexdigest()


@dataclass
class Order:
    fk_order_id: int
    payment_id: str
    amount: Any  # a JSON number, as the API reports it
    currency: str = "RUB"
    status: int = 0
    i: int | None = None
    body: dict[str, Any] = field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        return {
            "merchant_order_id": self.payment_id,
            "fk_order_id": self.fk_order_id,
            "amount": self.amount,
            "currency": self.currency,
            "email": "user@test.ru",
            "account": "5555555555554444",
            "date": "2026-10-02 12:28:24",
            "status": self.status,
        }


class FakeFreekassa:
    """See module docstring."""

    def __init__(
        self,
        *,
        shop_id: str = SHOP_ID,
        secret_word: str = SECRET_WORD,
        secret_word_2: str = SECRET_WORD_2,
        api_key: str = API_KEY,
    ) -> None:
        self.shop_id = shop_id
        self.secret_word = secret_word
        self.secret_word_2 = secret_word_2
        self.api_key = api_key
        self.orders: list[Order] = []
        self.created: list[dict[str, Any]] = []
        self.refunds: list[dict[str, Any]] = []
        self.status_calls: list[str] = []
        self.last_nonce = 0
        self.fail_with: int | None = None
        self._seq = itertools.count(1001)
        self._runner: web.AppRunner | None = None
        self._port: int | None = None

    @property
    def api_url(self) -> str:
        if self._port is not None:
            return f"http://127.0.0.1:{self._port}{_API_PATH}"
        return FAKE_API_URL

    async def __call__(self, call: HttpCall) -> HttpResponse:
        status, payload = self.handle(call.method, urlsplit(call.url).path, call.json)
        return HttpResponse(status, payload, {"Content-Type": "application/json"})

    def handle(self, method: str, path: str, body: Any) -> tuple[int, bytes]:
        if self.fail_with is not None:
            return self.fail_with, b'{"type":"error","message":"fault"}'
        if method.upper() != "POST" or not path.startswith(_API_PATH + "/"):
            return 404, b"<html>not found</html>"
        if not isinstance(body, dict):
            return 400, _j({"type": "error", "message": "json required"})
        given = str(body.get("signature", ""))
        unsigned = {k: v for k, v in body.items() if k != "signature"}
        if str(body.get("shopId")) != self.shop_id or not hmac.compare_digest(
            given, api_sign(self.api_key, unsigned)
        ):
            return 401, _j({"type": "error", "message": "Wrong signature"})
        nonce = body.get("nonce")
        if not isinstance(nonce, int) or nonce <= self.last_nonce:
            return 400, _j({"type": "error", "message": "Wrong nonce"})
        self.last_nonce = nonce
        rest = path[len(_API_PATH) :]
        if rest == "/orders/create":
            return self._create(body)
        if rest == "/orders":
            return self._list(body)
        if rest == "/orders/refund":
            return self._refund(body)
        if rest == "/balance":
            return 200, _j({"type": "success", "balance": [{"currency": "RUB", "value": "100.00"}]})
        return 404, _j({"type": "error", "message": "not found"})

    def _create(self, body: dict[str, Any]) -> tuple[int, bytes]:
        missing = [k for k in ("i", "email", "ip", "amount", "currency") if body.get(k) in (None, "")]
        if missing:
            return 400, _j({"type": "error", "message": f"missing {missing}"})
        self.created.append(dict(body))
        order = self.add(str(body.get("paymentId") or ""), float(body["amount"]), str(body["currency"]))
        order.i = int(body["i"])
        order.body = dict(body)
        h = uuid.uuid4().hex
        return 200, _j(
            {
                "type": "success",
                "orderId": order.fk_order_id,
                "orderHash": h,
                "location": f"https://pay.freekassa.fake/form/{order.fk_order_id}/{h}",
            }
        )

    def _list(self, body: dict[str, Any]) -> tuple[int, bytes]:
        pid = str(body.get("paymentId") or "")
        self.status_calls.append(pid)
        items = [o.as_json() for o in self.orders if not pid or o.payment_id == pid]
        return 200, _j({"type": "success", "pages": 1, "orders": items})

    def _refund(self, body: dict[str, Any]) -> tuple[int, bytes]:
        pid = str(body.get("paymentId") or "")
        paid = [o for o in self.orders if o.payment_id == pid and o.status == 1]
        if not paid:
            return 400, _j({"type": "error", "message": "Order not found"})
        paid[0].status = 6
        self.refunds.append(dict(body))
        return 200, _j({"type": "success", "id": 77})

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

    async def __aenter__(self) -> FakeFreekassa:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # ------------------------------------------------------------------------------ desk-side actions

    def add(self, payment_id: str, amount: Any = 179, currency: str = "RUB", status: int = 0) -> Order:
        order = Order(next(self._seq), payment_id, amount, currency, status)
        self.orders.append(order)
        return order

    def open_form(self, pay_url: str) -> Order:
        """What the SCI page does with our link: checks the signature and opens an order."""
        query = dict(parse_qsl(urlsplit(pay_url).query, keep_blank_values=True))
        for key in ("m", "oa", "currency", "o", "s"):
            if not query.get(key):
                raise ValueError(f"form parameter {key} is missing")
        expected = md5(f"{query['m']}:{query['oa']}:{self.secret_word}:{query['currency']}:{query['o']}")
        if query["m"] != self.shop_id or query["s"] != expected:
            raise ValueError("Неправильная подпись")
        order = self.add(query["o"], float(query["oa"]), query["currency"])
        order.body = query
        return order

    def order(self, payment_id: str) -> Order:
        return next(o for o in self.orders if o.payment_id == payment_id)

    def notification_fields(
        self,
        payment_id: str,
        *,
        amount: str | None = None,
        us_cur: str | None = "RUB",
        secret: str | None = None,
        shop_id: str | None = None,
    ) -> dict[str, str]:
        order = next((o for o in self.orders if o.payment_id == payment_id), None)
        amount_text = amount if amount is not None else (str(order.amount) if order else "179")
        merchant = shop_id or self.shop_id
        fields = {
            "MERCHANT_ID": merchant,
            "AMOUNT": amount_text,
            "intid": str(order.fk_order_id if order else 123456),
            "MERCHANT_ORDER_ID": payment_id,
            "P_EMAIL": "test_user@test_site.ru",
            "CUR_ID": "42",
            "payer_account": "123456xxxxxx1234",
            "commission": "0",
        }
        if us_cur:
            fields["us_cur"] = us_cur
        fields["SIGN"] = md5(f"{merchant}:{amount_text}:{secret or self.secret_word_2}:{payment_id}")
        return fields

    def notification(
        self,
        payment_id: str,
        *,
        multipart: bool = False,
        remote: str = FREEKASSA_IP,
        headers: Mapping[str, str] | None = None,
        **kw: Any,
    ) -> WebhookRequest:
        fields = self.notification_fields(payment_id, **kw)
        body, ctype = encode_form(fields, multipart=multipart)
        return WebhookRequest(
            body=body, headers={"Content-Type": ctype, **dict(headers or {})}, remote=remote
        )

    async def send_notification(self, url: str, payment_id: str, **kw: Any) -> tuple[int, str]:
        body, ctype = encode_form(self.notification_fields(payment_id, **kw))
        async with (
            ClientSession(timeout=ClientTimeout(total=10)) as session,
            session.post(url, data=body, headers={"Content-Type": ctype}) as resp,
        ):
            return resp.status, await resp.text()


def encode_form(fields: Mapping[str, str], *, multipart: bool = False) -> tuple[bytes, str]:
    if not multipart:
        return urlencode(fields).encode(), "application/x-www-form-urlencoded"
    boundary = "----fkboundary" + uuid.uuid4().hex
    chunks = []
    for key, value in fields.items():
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
        )
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _j(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False).encode()
