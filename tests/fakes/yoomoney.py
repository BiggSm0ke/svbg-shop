"""Fake ЮMoney quickpay for tests — built from the official pages (``docs/providers/yoomoney.md``).

Two ways to use it:

* as the responder of :class:`~svbg.payments.testkit.CountingHttp` (``CountingHttp(fake)``): no sockets;
* as a real aiohttp server (``async with FakeYooMoney() as ym: ym.base_url``) for end-to-end tests through
  :class:`~svbg.payments.registry.InstanceHttp` (which does not follow redirects).

Realistic where the plugin depends on it: ``POST /quickpay/confirm`` only (GET → 405), a form-encoded body
with ``receiver``, ``quickpay-form=button``, ``paymentType`` ``AC|PC``, ``sum``, ``label`` (≤ 64), optional
``successURL``; the answer is ``302`` with ``Location`` of the payment page. An unknown wallet gets the
``200`` HTML error page (no redirect). Notifications are ``application/x-www-form-urlencoded`` with ``sign`` =
``hex(HMAC_SHA256(secret, sorted "k=v" joined by "&", values RFC 3986-encoded))`` and optionally the retired
``sha1_hash``; the card fee makes ``amount`` < ``withdraw_amount``.
"""

from __future__ import annotations

import hashlib
import hmac
import itertools
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

from aiohttp import ClientSession, ClientTimeout, web

from svbg.payments.testkit import HttpCall
from svbg.sdk import HttpResponse, WebhookRequest

WALLET = "4100118888888888"
NOTIFICATION_SECRET = "ym-notify-secret-0123456789ab"
FAKE_BASE_URL = "https://yoomoney.fake"
PAY_HOST = "https://yoomoney.fake"
#: Card transfers lose ~3 % to the fee in this fake; wallet transfers ~1 %.
FEES = {"AC": Decimal("0.03"), "PC": Decimal("0.01")}


def sign(secret: str, params: Mapping[str, str]) -> str:
    """Independent implementation of the ``sign`` parameter (do not import the plugin's)."""
    items = sorted((k, v) for k, v in params.items() if k != "sign")
    message = urlencode(items, quote_via=quote, safe="")
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def sha1_hash(secret: str, params: Mapping[str, str]) -> str:
    keys = ("notification_type", "operation_id", "amount", "currency", "datetime", "sender", "codepro")
    raw = "&".join([*(params.get(k, "") for k in keys), secret, params.get("label", "")])
    return hashlib.sha1(raw.encode()).hexdigest()


def iso(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{at.microsecond // 1000:03d}Z"


def credited(withdraw: str, payment_type: str = "AC") -> str:
    """What reaches the wallet after the fee (2 decimals, rounded down)."""
    value = Decimal(withdraw) * (1 - FEES.get(payment_type, Decimal(0)))
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_DOWN))


def notification(
    *,
    label: str,
    withdraw_amount: str,
    operation_id: str = "1234567890",
    amount: str | None = None,
    notification_type: str = "card-incoming",
    at: datetime | None = None,
    secret: str = NOTIFICATION_SECRET,
    test: bool = False,
    unaccepted: bool = False,
    with_sign: bool = True,
    with_sha1: bool = False,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Parameters of one notification (signed last)."""
    params = {
        "notification_type": notification_type,
        "operation_id": operation_id,
        "amount": amount if amount is not None else credited(withdraw_amount),
        "withdraw_amount": withdraw_amount,
        "currency": "643",
        "datetime": iso(at or datetime.now(UTC)),
        "sender": "41001000040" if notification_type == "p2p-incoming" else "",
        "codepro": "false",
        "label": label,
        "unaccepted": "true" if unaccepted else "false",
    }
    if test:
        params["test_notification"] = "true"
    params.update(extra or {})
    if with_sha1:
        params["sha1_hash"] = sha1_hash(secret, params)
    if with_sign:
        params["sign"] = sign(secret, params)
    return params


def as_request(params: Mapping[str, str]) -> WebhookRequest:
    """A form-encoded POST like ЮMoney sends (spaces as ``+``)."""
    return WebhookRequest(
        body=urlencode(list(params.items())).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )


@dataclass
class Transfer:
    request_id: str
    form: dict[str, str]
    operations: list[str] = field(default_factory=list)


class FakeYooMoney:
    """See module docstring."""

    def __init__(self, *, wallet: str = WALLET, secret: str = NOTIFICATION_SECRET) -> None:
        self.wallet = wallet
        self.secret = secret
        self.forms: list[dict[str, str]] = []
        self.transfers: dict[str, Transfer] = {}
        self.requests: list[tuple[str, str]] = []
        self.fail_with: int | None = None
        self._ops = itertools.count(700_000_000_001)
        self._runner: web.AppRunner | None = None
        self._port: int | None = None

    @property
    def base_url(self) -> str:
        if self._port is not None:
            return f"http://127.0.0.1:{self._port}"
        return FAKE_BASE_URL

    async def __call__(self, call: HttpCall) -> HttpResponse:
        """:class:`~svbg.payments.testkit.CountingHttp` responder."""
        data = call.data
        if isinstance(data, bytes):
            data = data.decode()
        form = dict(parse_qsl(data, keep_blank_values=True)) if isinstance(data, str) else dict(data or {})
        status, body, headers = self.handle(call.method, urlsplit(call.url).path, form)
        return HttpResponse(status, body, headers)

    def handle(self, method: str, path: str, form: Mapping[str, str]) -> tuple[int, bytes, dict[str, str]]:
        self.requests.append((method.upper(), path))
        if self.fail_with is not None:
            return self.fail_with, b"<html>fault</html>", {"Content-Type": "text/html"}
        if path != "/quickpay/confirm":
            return 404, b"<html>not found</html>", {"Content-Type": "text/html"}
        if method.upper() != "POST":
            return 405, b"<html>method not allowed</html>", {"Content-Type": "text/html"}
        page = {"Content-Type": "text/html; charset=utf-8"}
        try:
            total = Decimal(form.get("sum", ""))
        except ArithmeticError:
            total = Decimal(0)
        if (
            form.get("receiver") != self.wallet
            or form.get("quickpay-form") != "button"
            or form.get("paymentType") not in FEES
            or total <= 0
            or len(form.get("label", "")) > 64
        ):
            return 200, "<html>Ошибка: проверьте параметры перевода</html>".encode(), page
        self.forms.append(dict(form))
        request_id = secrets.token_hex(12)
        self.transfers[request_id] = Transfer(request_id, dict(form))
        location = f"{PAY_HOST}/transfer/quickpay?requestId={request_id}"
        return 302, b"", {"Location": location, **page}

    # ------------------------------------------------------------------------------------- server

    async def start(self) -> None:
        app = web.Application()

        async def any_route(request: web.Request) -> web.Response:
            form = dict(parse_qsl((await request.read()).decode(), keep_blank_values=True))
            status, body, headers = self.handle(request.method, request.path, form)
            return web.Response(status=status, body=body, headers=headers)

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

    async def __aenter__(self) -> FakeYooMoney:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # ------------------------------------------------------------------------------ payer actions

    def transfer_for(self, label: str) -> Transfer:
        for transfer in self.transfers.values():
            if transfer.form.get("label") == label:
                return transfer
        raise KeyError(label)

    def pay(self, request_id: str, **kw: Any) -> dict[str, str]:
        """The payer completes the transfer: the notification ЮMoney would send (parameters)."""
        transfer = self.transfers[request_id]
        op = str(next(self._ops))
        transfer.operations.append(op)
        form = transfer.form
        kw.setdefault("amount", credited(form["sum"], form["paymentType"]))
        kw.setdefault("notification_type", "p2p-incoming" if form["paymentType"] == "PC" else "card-incoming")
        return notification(
            label=form.get("label", ""),
            withdraw_amount=kw.pop("withdraw_amount", form["sum"]),
            operation_id=op,
            secret=kw.pop("secret", self.secret),
            **kw,
        )

    async def send(self, url: str, params: Mapping[str, str]) -> tuple[int, str]:
        """POST a notification to ``url``; returns ``(status, text)``."""
        async with (
            ClientSession(timeout=ClientTimeout(total=10)) as session,
            session.post(
                url,
                data=urlencode(list(params.items())),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ) as resp,
        ):
            return resp.status, await resp.text()
