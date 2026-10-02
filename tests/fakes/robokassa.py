"""Fake Robokassa for tests — built from the official pages (``docs/providers/robokassa.md``).

* :meth:`FakeRobokassa.open_link` plays the payment page: it checks the link's ``SignatureValue`` with
  Password #1 exactly like Robokassa (``MerchantLogin:OutSum:InvId:Password#1[:Shp_…]``) and registers the
  invoice; :meth:`FakeRobokassa.pay` builds the Result URL notification (``OutSum`` with six decimals,
  upper-case ``SignatureValue = H(OutSum:InvId:Password#2[:Shp_…])``, ``Fee``, ``EMail``, ``IsTest`` for test
  payments).
* As a :class:`~svbg.payments.testkit.CountingHttp` responder or a real aiohttp server it answers
  ``GET /Merchant/WebService/Service.asmx/OpStateExt`` with the official XML (``Result/Code`` 0–4, 1000;
  ``State/Code`` 5, 10, 20, 50, 60, 80, 100; 7-digit fractions in dates). Test payments are unknown to it
  (code 3), as in production.
"""

from __future__ import annotations

import hashlib
import itertools
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import parse_qsl, urlencode, urlsplit
from xml.sax.saxutils import escape

from aiohttp import ClientSession, ClientTimeout, web

from svbg.payments.testkit import HttpCall
from svbg.sdk import HttpResponse, WebhookRequest

LOGIN = "svbg-demo"
PASSWORD1 = "rk-pass1-live-0123456789"
PASSWORD2 = "rk-pass2-live-9876543210"
TEST_PASSWORD1 = "rk-pass1-test-aaaaaaaaaa"
TEST_PASSWORD2 = "rk-pass2-test-bbbbbbbbbb"
FAKE_BASE_URL = "https://robokassa.fake/Merchant"
STATE_PATH = "/Merchant/WebService/Service.asmx/OpStateExt"
MSK = timezone(timedelta(hours=3))
NS = "http://merchant.roboxchange.com/WebService/"


def h(algorithm: str, raw: str) -> str:
    """Independent hash helper (do not import the plugin's)."""
    return hashlib.new(algorithm, raw.encode()).hexdigest()


def shp_tail(params: Mapping[str, str]) -> str:
    shp = sorted((k, v) for k, v in params.items() if k.startswith("Shp_"))
    return "".join(f":{k}={v}" for k, v in shp)


def result_sign(algorithm: str, password2: str, params: Mapping[str, str]) -> str:
    return h(algorithm, f"{params['OutSum']}:{params['InvId']}:{password2}{shp_tail(params)}").upper()


def robokassa_time(at: datetime) -> str:
    """``2026-10-02T15:00:00.1234567+03:00`` — 7-digit fraction, Moscow offset."""
    msk = at.astimezone(MSK)
    return msk.strftime("%Y-%m-%dT%H:%M:%S.") + f"{msk.microsecond:06d}1+03:00"


@dataclass
class Invoice:
    inv_id: str
    out_sum: str
    shp: dict[str, str]
    is_test: bool
    state: int = 5
    paid_at: datetime | None = None
    params: dict[str, str] = field(default_factory=dict)


class FakeRobokassa:
    """See module docstring."""

    def __init__(
        self,
        *,
        login: str = LOGIN,
        password1: str = PASSWORD1,
        password2: str = PASSWORD2,
        test_password1: str = TEST_PASSWORD1,
        test_password2: str = TEST_PASSWORD2,
        algorithm: str = "md5",
    ) -> None:
        self.login = login
        self.password1, self.password2 = password1, password2
        self.test_password1, self.test_password2 = test_password1, test_password2
        self.algorithm = algorithm
        self.invoices: dict[str, Invoice] = {}
        self.state_calls: list[str] = []
        self.fail_with: int | None = None
        self.result_code: int | None = None  # forced Result/Code of OpStateExt
        self.answer: bytes | None = None  # forced raw body of OpStateExt
        self._fees = itertools.count(1)
        self._runner: web.AppRunner | None = None
        self._port: int | None = None

    @property
    def base_url(self) -> str:
        if self._port is not None:
            return f"http://127.0.0.1:{self._port}/Merchant"
        return FAKE_BASE_URL

    # ---------------------------------------------------------------------------------- payment page

    def open_link(self, link: str) -> Invoice:
        """Validate a payment link like Robokassa's page does; ``ValueError`` on a bad signature."""
        parts = urlsplit(link)
        assert parts.path.endswith("/Merchant/Index.aspx"), parts.path
        params = dict(parse_qsl(parts.query, keep_blank_values=True))
        if params.get("MerchantLogin") != self.login:
            raise ValueError("shop not found")
        is_test = params.get("IsTest") == "1"
        password1 = self.test_password1 if is_test else self.password1
        raw = f"{self.login}:{params['OutSum']}:{params.get('InvId', '')}:{password1}{shp_tail(params)}"
        if h(self.algorithm, raw).lower() != params.get("SignatureValue", "").lower():
            raise ValueError("bad signature (Password #1 or algorithm)")
        if len(params.get("Description", "")) > 100:
            raise ValueError("description too long")
        inv_id = params.get("InvId", "")
        if not inv_id.isdigit() or not 1 <= int(inv_id) <= 2**63 - 1:
            raise ValueError("bad InvId")
        shp = {k: v for k, v in params.items() if k.startswith("Shp_")}
        invoice = Invoice(inv_id, params["OutSum"], shp, is_test, params=params)
        self.invoices[inv_id] = invoice
        return invoice

    def add_invoice(
        self, inv_id: str, out_sum: str = "179.00", *, shp: Mapping[str, str] | None = None
    ) -> Invoice:
        invoice = Invoice(inv_id, out_sum, dict(shp or {}), False)
        self.invoices[inv_id] = invoice
        return invoice

    def set_state(self, inv_id: str, state: int) -> Invoice:
        invoice = self.invoices[inv_id]
        invoice.state = state
        if state == 100 and invoice.paid_at is None:
            invoice.paid_at = datetime.now(UTC)
        return invoice

    def pay(
        self, inv_id: str, *, out_sum: str | None = None, password2: str | None = None, **extra: str
    ) -> dict[str, str]:
        """The buyer pays: state 100 and the Result URL parameters Robokassa would send."""
        invoice = self.set_state(inv_id, 100)
        params = {
            "OutSum": out_sum or f"{Decimal(invoice.out_sum):.6f}",
            "InvId": inv_id,
            "Fee": f"{Decimal(next(self._fees)) / 10:.6f}",
            "EMail": "buyer@example.com",
            "PaymentMethod": "BankCard",
            "IncCurrLabel": "BankCardPSR",
            **invoice.shp,
        }
        if invoice.is_test:
            params["IsTest"] = "1"
        params.update(extra)
        secret = password2 or (self.test_password2 if invoice.is_test else self.password2)
        params["SignatureValue"] = result_sign(self.algorithm, secret, params)
        return params

    # ------------------------------------------------------------------------------------ OpStateExt

    async def __call__(self, call: HttpCall) -> HttpResponse:
        """:class:`~svbg.payments.testkit.CountingHttp` responder."""
        params = dict(call.params)
        query = urlsplit(call.url).query
        if query:
            params.update(parse_qsl(query))
        status, body = self.handle(call.method, urlsplit(call.url).path, params)
        return HttpResponse(status, body, {"Content-Type": "text/xml; charset=utf-8"})

    def handle(self, method: str, path: str, params: Mapping[str, str]) -> tuple[int, bytes]:
        if self.fail_with is not None:
            return self.fail_with, b"<html>fault</html>"
        if path != STATE_PATH or method.upper() not in ("GET", "POST"):
            return 404, b"<html>not found</html>"
        invoice_id = params.get("InvoiceID", "")
        self.state_calls.append(invoice_id)
        if self.answer is not None:
            return 200, self.answer
        if self.result_code is not None:
            return 200, self._xml(self.result_code)
        if params.get("MerchantLogin") != self.login:
            return 200, self._xml(2)
        expected = h(self.algorithm, f"{self.login}:{invoice_id}:{self.password2}")
        if expected.lower() != params.get("Signature", "").lower():
            return 200, self._xml(1)
        invoice = self.invoices.get(invoice_id)
        if invoice is None or invoice.is_test:
            return 200, self._xml(3)
        return 200, self._xml(0, invoice)

    def _xml(self, code: int, invoice: Invoice | None = None) -> bytes:
        parts = [
            '<?xml version="1.0" encoding="utf-8"?>',
            '<OperationStateResponse xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
            f'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns="{NS}">',
            f"<Result><Code>{code}</Code></Result>",
        ]
        if invoice is not None:
            now = robokassa_time(datetime.now(UTC))
            state_date = robokassa_time(invoice.paid_at) if invoice.paid_at else now
            parts += [
                f"<State><Code>{invoice.state}</Code><RequestDate>{now}</RequestDate>"
                f"<StateDate>{state_date}</StateDate></State>",
                "<Info><IncCurrLabel>BankCardPSR</IncCurrLabel>"
                f"<IncSum>{Decimal(invoice.out_sum):.6f}</IncSum><IncAccount>220220******1234</IncAccount>"
                "<PaymentMethod><Code>BankCard</Code>"
                "<Description>Банковская карта</Description></PaymentMethod>"
                f"<OutCurrLabel>RUR</OutCurrLabel><OutSum>{Decimal(invoice.out_sum):.6f}</OutSum>"
                "<OpKey>B3A2F7C1</OpKey><BankCardRRN>000000000001</BankCardRRN></Info>",
                "<UserFields>"
                + "".join(
                    f"<Field><Name>{escape(k)}</Name><Value>{escape(v)}</Value></Field>"
                    for k, v in invoice.shp.items()
                )
                + "</UserFields>",
            ]
        parts.append("</OperationStateResponse>")
        return "".join(parts).encode()

    # ------------------------------------------------------------------------------------- server

    async def start(self) -> None:
        app = web.Application()

        async def any_route(request: web.Request) -> web.Response:
            params = dict(request.query)
            raw = await request.read()
            if raw:
                params.update(parse_qsl(raw.decode()))
            status, body = self.handle(request.method, request.path, params)
            return web.Response(status=status, body=body, content_type="text/xml")

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

    async def __aenter__(self) -> FakeRobokassa:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # --------------------------------------------------------------------------------- Result URL

    @staticmethod
    def as_request(params: Mapping[str, str], *, method: str = "POST") -> WebhookRequest:
        if method == "GET":
            return WebhookRequest(body=b"", method="GET", query=dict(params))
        return WebhookRequest(
            body=urlencode(list(params.items())).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    async def send(self, url: str, params: Mapping[str, str]) -> tuple[int, str]:
        async with (
            ClientSession(timeout=ClientTimeout(total=10)) as session,
            session.post(
                url,
                data=urlencode(list(params.items())),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ) as resp,
        ):
            return resp.status, await resp.text()
