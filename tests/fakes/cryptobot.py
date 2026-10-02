"""Fake Crypto Pay API (CryptoBot) for tests — built from our specification (``docs/providers/cryptobot.md``).

Use it as the responder of :class:`~svbg.payments.testkit.CountingHttp` or as a real aiohttp server
(``async with FakeCryptoBot() as bot: bot.base_url``). Realistic where the plugin depends on it: the
``Crypto-Pay-API-Token`` header (``401 UNAUTHORIZED`` otherwise), the ``{"ok": …, "result"|"error": …}``
envelope, ``createInvoice`` for fiat invoices, ``getInvoices`` with ``invoice_ids`` (``result.items``),
``getMe``, numeric invoice ids, statuses ``active|paid|expired`` and ``invoice_paid`` webhooks signed with
``HMAC_SHA256(key=sha256(token), body)``. A testnet desk is simply a second fake with another token.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import itertools
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from aiohttp import ClientSession, ClientTimeout, web

from svbg.payments.testkit import HttpCall
from svbg.sdk import HttpResponse, WebhookRequest

API_TOKEN = "12345:AAcryptoPayLiveToken0123456789"
TESTNET_TOKEN = "777:AAcryptoPayTestnetToken0123456"
BASE_PATH = "/api"
FAKE_BASE_URL = "https://pay.crypt.bot/api"


def sign(token: str, body: bytes) -> str:
    """Independent implementation of the Crypto Pay webhook signature."""
    return hmac.new(hashlib.sha256(token.encode()).digest(), body, hashlib.sha256).hexdigest()


def iso(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{at.microsecond // 1000:03d}Z"


@dataclass
class Invoice:
    invoice_id: int
    amount: str
    fiat: str
    payload: str
    description: str = ""
    status: str = "active"
    currency_type: str = "fiat"
    asset: str | None = None
    paid_asset: str | None = None
    paid_amount: str | None = None
    paid_at: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    expires_in: int = 3600
    body: dict[str, Any] = field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "invoice_id": self.invoice_id,
            "hash": f"IV{self.invoice_id:08d}",
            "currency_type": self.currency_type,
            "amount": self.amount,
            "status": self.status,
            "description": self.description,
            "payload": self.payload,
            "bot_invoice_url": f"https://t.me/CryptoBot?start=IV{self.invoice_id:08d}",
            "mini_app_invoice_url": f"https://t.me/CryptoBot/app?startapp=invoice-IV{self.invoice_id:08d}",
            "created_at": iso(self.created_at),
            "expiration_date": iso(self.created_at + timedelta(seconds=self.expires_in)),
            "allow_comments": False,
            "allow_anonymous": True,
        }
        if self.currency_type == "fiat":
            data["fiat"] = self.fiat
        else:
            data["asset"] = self.asset
        if self.status == "paid":
            data["paid_asset"] = self.paid_asset or "USDT"
            data["paid_amount"] = self.paid_amount or "1.93"
            data["paid_at"] = iso(self.paid_at or datetime.now(UTC))
        return data


class FakeCryptoBot:
    """See module docstring."""

    def __init__(
        self, *, token: str = API_TOKEN, base_url: str = FAKE_BASE_URL, name: str = "SvBG Test"
    ) -> None:
        self.token = token
        self.name = name
        self._base_url = base_url.rstrip("/")
        self.invoices: dict[int, Invoice] = {}
        self.created: list[dict[str, Any]] = []
        self.get_invoices_calls: list[list[str]] = []
        self.fail_with: int | None = None
        self.delay: float = 0.0
        self._seq = itertools.count(1001)
        self._update = itertools.count(1)
        self._runner: web.AppRunner | None = None
        self._port: int | None = None

    @property
    def base_url(self) -> str:
        if self._port is not None:
            return f"http://127.0.0.1:{self._port}{BASE_PATH}"
        return self._base_url

    async def __call__(self, call: HttpCall) -> HttpResponse:
        if self.delay:
            await asyncio.sleep(self.delay)
        params = dict(call.params)
        if isinstance(call.json, Mapping):
            params.update({k: v for k, v in call.json.items()})
        status, payload = self.handle(call.method, urlsplit(call.url).path, call.headers, params)
        return HttpResponse(status, payload, {"Content-Type": "application/json"})

    def handle(
        self, method: str, path: str, headers: Mapping[str, str], params: Mapping[str, Any]
    ) -> tuple[int, bytes]:
        if self.fail_with is not None:
            return self.fail_with, b"<html>bad gateway</html>"
        low = {k.lower(): v for k, v in headers.items()}
        if not path.startswith(BASE_PATH + "/"):
            return 404, _err(404, "METHOD_NOT_FOUND")
        if not hmac.compare_digest(low.get("crypto-pay-api-token", ""), self.token):
            return 401, _err(401, "UNAUTHORIZED")
        name = path[len(BASE_PATH) + 1 :]
        if name == "getMe":
            return 200, _ok({"app_id": 42, "name": self.name, "payment_processing_bot_username": "CryptoBot"})
        if name == "createInvoice":
            return self._create(params)
        if name == "getInvoices":
            return self._list(params)
        return 404, _err(404, "METHOD_NOT_FOUND")

    def _create(self, params: Mapping[str, Any]) -> tuple[int, bytes]:
        if params.get("currency_type") != "fiat" or not params.get("fiat") or not params.get("amount"):
            return 400, _err(400, "PARAMS_INVALID")
        self.created.append(dict(params))
        inv = Invoice(
            invoice_id=next(self._seq),
            amount=str(params["amount"]),
            fiat=str(params["fiat"]),
            payload=str(params.get("payload") or ""),
            description=str(params.get("description") or ""),
            expires_in=int(params.get("expires_in") or 3600),
            body=dict(params),
        )
        self.invoices[inv.invoice_id] = inv
        return 200, _ok(inv.as_json())

    def _list(self, params: Mapping[str, Any]) -> tuple[int, bytes]:
        raw = str(params.get("invoice_ids") or "")
        ids = [i for i in raw.split(",") if i]
        self.get_invoices_calls.append(ids)
        wanted = {int(i) for i in ids if i.isdigit()}
        items = [inv.as_json() for inv_id, inv in self.invoices.items() if not wanted or inv_id in wanted]
        return 200, _ok({"items": items})

    # ------------------------------------------------------------------------------------- server

    async def start(self) -> None:
        app = web.Application()

        async def any_route(request: web.Request) -> web.Response:
            params: dict[str, Any] = dict(request.query)
            raw = await request.read()
            if raw:
                params.update(json.loads(raw))
            status, payload = self.handle(request.method, request.path, dict(request.headers), params)
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

    async def __aenter__(self) -> FakeCryptoBot:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # ------------------------------------------------------------------------------ desk-side actions

    def invoice_for(self, payload: str) -> Invoice:
        for inv in self.invoices.values():
            if inv.payload == payload:
                return inv
        raise KeyError(payload)

    def add_invoice(
        self, invoice_id: int, payload: str, amount: str = "179", fiat: str = "RUB", status: str = "active"
    ) -> Invoice:
        inv = Invoice(invoice_id=invoice_id, amount=amount, fiat=fiat, payload=payload, status=status)
        self.invoices[invoice_id] = inv
        return inv

    def pay(self, invoice_id: int, *, asset: str = "USDT", paid_amount: str = "1.93") -> Invoice:
        inv = self.invoices[invoice_id]
        inv.status = "paid"
        inv.paid_asset = asset
        inv.paid_amount = paid_amount
        inv.paid_at = datetime.now(UTC)
        return inv

    def expire(self, invoice_id: int) -> Invoice:
        inv = self.invoices[invoice_id]
        inv.status = "expired"
        return inv

    # ------------------------------------------------------------------------------------- webhooks

    def webhook_parts(
        self,
        invoice_id: int,
        *,
        at: datetime | None = None,
        token: str | None = None,
        update_type: str = "invoice_paid",
        overrides: Mapping[str, Any] | None = None,
    ) -> tuple[bytes, dict[str, str]]:
        invoice = self.invoices[invoice_id].as_json()
        invoice.update(overrides or {})
        payload = {
            "update_id": next(self._update),
            "update_type": update_type,
            "request_date": iso(at or datetime.now(UTC)),
            "payload": invoice,
        }
        body = json.dumps(payload, separators=(",", ":")).encode()
        headers = {
            "Content-Type": "application/json",
            "crypto-pay-api-signature": sign(token or self.token, body),
        }
        return body, headers

    def webhook(self, invoice_id: int, **kw: Any) -> WebhookRequest:
        body, headers = self.webhook_parts(invoice_id, **kw)
        return WebhookRequest(body=body, headers=headers)

    async def send_webhook(self, url: str, invoice_id: int, **kw: Any) -> int:
        body, headers = self.webhook_parts(invoice_id, **kw)
        async with (
            ClientSession(timeout=ClientTimeout(total=10)) as session,
            session.post(url, data=body, headers=headers) as resp,
        ):
            await resp.read()
            return resp.status


def _ok(result: Any) -> bytes:
    return json.dumps({"ok": True, "result": result}, ensure_ascii=False).encode()


def _err(code: int, name: str) -> bytes:
    return json.dumps({"ok": False, "error": {"code": code, "name": name}}).encode()
