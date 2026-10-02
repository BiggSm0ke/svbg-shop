"""Fake SeverPay merchant API for tests — built from our specification (``docs/providers/severpay.md``).

* as the responder of :class:`~svbg.payments.testkit.CountingHttp` (``CountingHttp(fake)``): no sockets;
* as a real aiohttp server (``async with FakeSeverPay() as desk: desk.base_url``).

Realistic where the plugin depends on it: every request is a ``POST`` with ``mid``/``salt``/``sign`` in the
body and the server re-encodes the decoded body like PHP (``ksort`` + ``json_encode`` with
``JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES``) to check ``sign`` — a mismatch or another ``mid``
answers ``{"status": false, "msg": …}``; ``/payin/create`` (``id``, ``uid``, ``url``, ``expire_at``),
``/payin/get`` by ``id`` or ``uid``, ``/payin/settings``; statuses ``new|process|success|decline|fail``;
webhooks ``payin`` and ``test`` signed over PHP ``json_encode`` **without** flags (``/`` → ``\\/``,
non-ASCII → ``\\uXXXX``).
"""

from __future__ import annotations

import hashlib
import hmac
import itertools
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from aiohttp import web

from svbg.payments.testkit import HttpCall
from svbg.sdk import HttpResponse, WebhookRequest

MID = 1
#: The sample key printed in the official documentation (spec §6).
TOKEN = "041131a0906b08a5bebc1d4fdcc6d9"
FAKE_BASE_URL = "https://severpay.fake/api/merchant"
BASE_PATH = "/api/merchant"
WEBHOOK_IP = "45.76.81.14"


def hmac_hex(token: str, text: str) -> str:
    return hmac.new(token.encode(), text.encode("utf-8"), hashlib.sha256).hexdigest()


def request_canon(body: Mapping[str, Any]) -> str:
    """Independent re-implementation of the request signing form (do not import the plugin's)."""
    return json.dumps(dict(sorted(body.items())), ensure_ascii=False, separators=(",", ":"))


def webhook_canon(body: Mapping[str, Any]) -> str:
    """PHP ``json_encode`` without flags of a decoded body (keys in body order)."""
    return json.dumps(body, ensure_ascii=True, separators=(",", ":")).replace("/", "\\/")


@dataclass
class Payin:
    id: int
    uid: str
    order_id: str
    amount: Any  # a JSON number, as SeverPay reports it
    currency: str = "RUB"
    status: str = "new"
    body: dict[str, Any] = field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        return {"id": self.id, "uid": self.uid, "order_id": self.order_id, "amount": self.amount,
                "currency": self.currency, "status": self.status}  # fmt: skip


class FakeSeverPay:
    """See module docstring."""

    def __init__(
        self, *, mid: int = MID, token: str = TOKEN, base_url: str = FAKE_BASE_URL, currency: str = "RUB"
    ) -> None:
        self.mid = mid
        self.token = token
        self.currency = currency
        self._base_url = base_url.rstrip("/")
        self.payins: dict[int, Payin] = {}
        self.created: list[dict[str, Any]] = []
        self.raw_requests: list[bytes] = []
        self.status_calls: list[dict[str, Any]] = []
        self.fail_with: int | None = None
        self._seq = itertools.count(1001)
        self._runner: web.AppRunner | None = None
        self._port: int | None = None

    @property
    def base_url(self) -> str:
        if self._port is not None:
            return f"http://127.0.0.1:{self._port}{BASE_PATH}"
        return self._base_url

    async def __call__(self, call: HttpCall) -> HttpResponse:
        raw = call.data if isinstance(call.data, bytes) else json.dumps(call.json).encode()
        status, payload = self.handle(call.method, urlsplit(call.url).path, raw)
        return HttpResponse(status, payload, {"Content-Type": "application/json"})

    def handle(self, method: str, path: str, raw: bytes) -> tuple[int, bytes]:
        self.raw_requests.append(raw)
        if self.fail_with is not None:
            return self.fail_with, b"<html>error</html>"
        if method.upper() != "POST" or not path.startswith(BASE_PATH + "/"):
            return 404, b"<html>not found</html>"
        try:
            body = json.loads(raw)
        except ValueError:
            return 200, _no("Invalid JSON")
        if not isinstance(body, dict):
            return 200, _no("Invalid JSON")
        given = body.pop("sign", None)
        if body.get("mid") != self.mid:
            return 200, _no("Merchant not found")
        if not isinstance(given, str) or not hmac.compare_digest(
            hmac_hex(self.token, request_canon(body)), given
        ):
            return 200, _no("Invalid sign")
        if not body.get("salt"):
            return 200, _no("salt is required")
        rest = path[len(BASE_PATH) :]
        if rest == "/payin/create":
            return self._create(body)
        if rest == "/payin/get":
            return self._get(body)
        if rest == "/payin/settings":
            return 200, _yes({"mid": self.mid, "name": "Demo shop", "currency": self.currency,
                              "amount": {"min": 100, "max": 150000}, "fees": {"percent": 5, "fixed": 0},
                              "rolling": {"percent": 0, "days": 0}, "fee": 5, "min_amount": 100,
                              "max_amount": 150000})  # fmt: skip
        return 404, _no("Not found")

    def _create(self, body: dict[str, Any]) -> tuple[int, bytes]:
        missing = [
            k for k in ("order_id", "amount", "currency", "client_email", "client_id") if not body.get(k)
        ]
        if missing:
            return 200, _no(f"Missing fields: {', '.join(missing)}")
        lifetime = body.get("lifetime", 1440)
        if not isinstance(lifetime, int) or not 30 <= lifetime <= 4320:
            return 200, _no("lifetime must be 30..4320")
        self.created.append(dict(body))
        pid = next(self._seq)
        payin = Payin(id=pid, uid=f"01KBCWM19JEQW53ZR1GKYQ{pid:04d}", order_id=str(body["order_id"]),
                      amount=body["amount"], currency=str(body["currency"]), body=dict(body))  # fmt: skip
        self.payins[pid] = payin
        return 200, _yes({"id": pid, "url": f"https://severpay.fake/pay/{payin.uid}",
                          "expire_at": int(time.time()) + lifetime * 60, "uid": payin.uid})  # fmt: skip

    def _get(self, body: dict[str, Any]) -> tuple[int, bytes]:
        self.status_calls.append({k: body[k] for k in ("id", "uid") if k in body})
        found: Payin | None = None
        if "id" in body:
            found = self.payins.get(body["id"]) if isinstance(body["id"], int) else None
        elif "uid" in body:
            found = next((p for p in self.payins.values() if p.uid == body["uid"]), None)
        else:
            return 200, _no("id or uid is required")
        if found is None:
            return 200, _no("Payment not found")
        return 200, _yes(found.as_json())

    # ------------------------------------------------------------------------------------- server

    async def start(self) -> None:
        app = web.Application()

        async def any_route(request: web.Request) -> web.Response:
            status, payload = self.handle(request.method, request.path, await request.read())
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

    async def __aenter__(self) -> FakeSeverPay:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # ------------------------------------------------------------------------------ desk-side actions

    def add(
        self, pid: int, order_id: str, amount: Any = 179, status: str = "new", currency: str = "RUB"
    ) -> Payin:
        payin = Payin(
            id=pid, uid=f"UID{pid}", order_id=order_id, amount=amount, currency=currency, status=status
        )
        self.payins[pid] = payin
        return payin

    def set_status(self, pid: int, status: str, *, amount: Any = None) -> Payin:
        payin = self.payins[pid]
        payin.status = status
        if amount is not None:
            payin.amount = amount
        return payin

    def webhook(
        self,
        pid: int,
        status: str | None = None,
        *,
        token: str | None = None,
        salt: str = "a9f3c2d8e4b1",
        remote: str | None = WEBHOOK_IP,
    ) -> WebhookRequest:
        """A signed ``payin`` webhook for payment ``pid`` (its current state unless overridden)."""
        payin = self.payins[pid]
        data = {**payin.as_json(), "status": status or payin.status}
        data.pop("uid")
        body = {"type": "payin", "data": data, "salt": salt}
        body["sign"] = hmac_hex(token or self.token, webhook_canon(body))
        return WebhookRequest(
            body=json.dumps(body, ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            remote=remote,
        )

    def test_webhook(self, *, timestamp: int = 1735555200, salt: str = "c4f7a1d9e2b3") -> WebhookRequest:
        body: dict[str, Any] = {"type": "test", "data": {"timestamp": timestamp}, "salt": salt}
        body["sign"] = hmac_hex(self.token, webhook_canon(body))
        return WebhookRequest(body=json.dumps(body).encode(), remote=WEBHOOK_IP)


def _yes(data: Any) -> bytes:
    return json.dumps({"status": True, "msg": "", "data": data}, ensure_ascii=False).encode()


def _no(msg: str) -> bytes:
    return json.dumps({"status": False, "msg": msg}).encode()
