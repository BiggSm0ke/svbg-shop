"""Robokassa plugin: the shared TestKit plus protocol vectors from ``docs/providers/robokassa.md``."""

from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import pytest

from svbg.core.ids import uuid7
from svbg.payments.providers.robokassa import (
    DEFAULT_BASE_URL,
    HASH_ALGORITHMS,
    SHP_KEY,
    Robokassa,
    inv_id_for,
    parse_op_state,
    payment_signature,
    result_signature,
    state_signature,
)
from svbg.payments.registry import InstanceHttp
from svbg.payments.testkit import (
    CountingHttp,
    HttpCall,
    KitFailure,
    check_core,
    check_plugin,
    check_poll_budget,
    check_static,
    make_provider,
)
from svbg.sdk import (
    HttpResponse,
    MethodKind,
    PaymentIntent,
    PaymentState,
    ProviderError,
    WebhookAuth,
    WebhookRejected,
    WebhookRequest,
)
from tests.dbkit import CountingDatabase
from tests.fakes.robokassa import (
    FAKE_BASE_URL,
    LOGIN,
    PASSWORD1,
    PASSWORD2,
    TEST_PASSWORD1,
    TEST_PASSWORD2,
    FakeRobokassa,
    result_sign,
)
from tests.payments.providers.conftest import HarnessFactory, outcomes, payment_row

CONFIG = {"merchant_login": LOGIN, "password1": PASSWORD1, "password2": PASSWORD2, "base_url": FAKE_BASE_URL}
TEST_CONFIG = {**CONFIG, "password1": TEST_PASSWORD1, "password2": TEST_PASSWORD2}
PID = "0190f5d2-7b1e-7c3a-9d4e-5f6a7b8c9d0e"


class RoboVectors:
    """:class:`~svbg.payments.testkit.WebhookVectors` of Robokassa (independent signing code).

    The Result URL is called only for a successful payment: there is no «expired» or «chargeback»
    notification. For those states the vector sends an empty request (no signature → 401, nothing changes);
    returns are learned from ``OpStateExt`` (state 60) instead (``test_refund_is_learned_from_status``)."""

    def __init__(self, password2: str = PASSWORD2, algorithm: str = "md5") -> None:
        self.password2 = password2
        self.algorithm = algorithm

    def webhook(
        self,
        state: PaymentState,
        *,
        payment_id: str,
        external_id: str,
        amount: str,
        currency: str,
        signed_at: datetime,
        test: bool = False,
    ) -> WebhookRequest:
        assert currency == "RUB"
        if state is not PaymentState.PAID:
            return WebhookRequest(body=b"")
        params = {"OutSum": amount, "InvId": external_id, "Fee": "5.370000", "EMail": "b@example.com"}
        params[SHP_KEY] = payment_id
        if test:
            params["IsTest"] = "1"
        params["SignatureValue"] = result_sign(self.algorithm, self.password2, params)
        return FakeRobokassa.as_request(params)

    def tamper(self, req: WebhookRequest) -> WebhookRequest:
        return WebhookRequest(body=req.body.replace(b"OutSum=", b"OutSum=1"), headers=req.headers)

    def strip_auth(self, req: WebhookRequest) -> WebhookRequest:
        body = "&".join(p for p in req.body.decode().split("&") if not p.startswith("SignatureValue="))
        return WebhookRequest(body=body.encode(), headers=req.headers)


def provider(http: CountingHttp | None = None, *, is_test: bool = False, **config: Any) -> Robokassa:
    base = TEST_CONFIG if is_test else CONFIG
    p = make_provider(Robokassa, {**base, **config}, http=http, is_test=is_test)
    assert isinstance(p, Robokassa)
    return p


def intent(**kw: Any) -> PaymentIntent:
    values: dict[str, Any] = {
        "payment_id": PID,
        "amount_minor": 17_900,
        "currency": "RUB",
        "description": "Пополнение баланса на 179 ₽",
        "customer_ref": "c0ffee",
    }
    values.update(kw)
    return PaymentIntent(**values)


def result(params: dict[str, str], *, method: str = "POST") -> WebhookRequest:
    return FakeRobokassa.as_request(params, method=method)


async def paid_params(desk: FakeRobokassa, plugin: Robokassa, **kw: Any) -> dict[str, str]:
    checkout = await plugin.create(intent(**kw))
    assert checkout.pay_url is not None and checkout.external_id is not None
    desk.open_link(checkout.pay_url)
    return desk.pay(checkout.external_id)


# ------------------------------------------------------------------------------------------- TestKit


def test_static_contract() -> None:
    check_static(Robokassa)
    caps = Robokassa.capabilities
    assert caps.webhook_auth is WebhookAuth.SIGNATURE and caps.replay_window_s is None
    assert caps.fetch_status and not caps.batch_status and not caps.refund and not caps.receipt_54fz
    assert Robokassa.manifest.method_kinds == (MethodKind.CARD, MethodKind.SBP)
    assert all(f.where for f in Robokassa.manifest.config.fields().values() if not f.advanced)


async def test_testkit_plugin_level() -> None:
    await check_plugin(Robokassa, CONFIG, RoboVectors())


async def test_testkit_core_level(make_harness: HarnessFactory, db: CountingDatabase) -> None:
    """Everything passes except the two vectors the Result URL cannot express (documented)."""
    harness = await make_harness(Robokassa, CONFIG, http=CountingHttp(FakeRobokassa()))
    with pytest.raises(KitFailure) as err:
        await check_core(harness, RoboVectors())
    assert err.value.failures == [
        "expired webhook did not expire the payment",
        "chargeback did not mark the payment refunded",
    ]
    assert len(harness.credited) == 3 and harness.refunded == []
    assert {"bad_signature", "applied", "mismatch", "test_rejected"} <= set(await outcomes(db))


@pytest.mark.parametrize("domain", [True, False], ids=["domain", "no-domain"])
async def test_testkit_poll_budget(make_harness: HarnessFactory, domain: bool) -> None:
    desk = FakeRobokassa()
    http = CountingHttp(desk)
    harness = await make_harness(Robokassa, CONFIG, http=http, has_domain=domain)

    async def initiated(call: HttpCall) -> HttpResponse:  # every invoice stays «5 — initiated»
        invoice_id = call.params.get("InvoiceID", "")
        if invoice_id not in desk.invoices:
            desk.add_invoice(invoice_id)
        return await desk(call)

    http.responder = initiated
    used = await check_poll_budget(harness, domain=domain)
    assert 1 <= used <= (3 if domain else 24)
    assert all(
        c.method == "GET" and c.url.endswith("/WebService/Service.asmx/OpStateExt") for c in http.calls
    )


# ---------------------------------------------------------------------------------- signature vectors


def test_signatures_known_answer() -> None:
    """Computed with OpenSSL (``openssl dgst -md5`` / ``-sha256``) over the strings in the docs."""
    shp = {SHP_KEY: PID}
    assert payment_signature("md5", "svbg-demo", "179.00", "1", "rk-kat-pass1", shp) == (
        "46798b3c9fc36bef572758419c03589c"
    )
    assert payment_signature("sha256", "svbg-demo", "179.00", "1", "rk-kat-pass1", shp) == (
        "5018f1c370a88bcee169adc494b8ddeefb8253762458c1948d004eeda418d0e3"
    )
    assert (
        result_signature("md5", "179.000000", "1", "rk-kat-pass2", shp) == "dbafba773e449dedfe9dd09c7c943aaa"
    )
    assert state_signature("md5", "svbg-demo", "1", "rk-kat-pass2") == "a66ea5fb395be3398e0cbc4b798c8dc0"


def test_shp_parameters_are_sorted_by_name() -> None:
    a = result_signature("md5", "10", "7", "p", {"Shp_b": "2", "Shp_a": "1"})
    b = result_signature("md5", "10", "7", "p", {"Shp_a": "1", "Shp_b": "2"})
    assert (
        a == b == result_sign("md5", "p", {"OutSum": "10", "InvId": "7", "Shp_b": "2", "Shp_a": "1"}).lower()
    )


@pytest.mark.parametrize("algorithm", HASH_ALGORITHMS)
async def test_every_algorithm_round_trips(algorithm: str) -> None:
    desk = FakeRobokassa(algorithm=algorithm)
    plugin = provider(hash_algorithm=algorithm)
    params = await paid_params(desk, plugin)
    event = await plugin.parse_webhook(result(params))
    assert event.state is PaymentState.PAID and event.payment_id == PID
    with pytest.raises(WebhookRejected):  # the same notification with another algorithm configured
        other = "sha512" if algorithm != "sha512" else "md5"
        await provider(hash_algorithm=other).parse_webhook(result(params))


def test_inv_id_is_stable_bounded_and_unique() -> None:
    assert inv_id_for(PID) == inv_id_for(PID.upper()) == ((0x0190F5D27B1E << 15) | 0x1D0E & 0x7FFF)
    ids = [uuid7() for _ in range(20_000)]  # many in the same millisecond
    invs = {inv_id_for(i) for i in ids}
    assert len(invs) == len(ids)
    assert all(1 <= v <= 2**63 - 1 for v in invs)
    v4 = str(uuid.uuid4())
    assert inv_id_for(v4) == inv_id_for(v4) and 1 <= inv_id_for(v4) <= 2**63 - 1
    assert 1 <= inv_id_for("legacy-order-1") <= 2**63 - 1


# -------------------------------------------------------------------------------------------- link


async def test_payment_link_is_accepted_by_the_page() -> None:
    desk = FakeRobokassa()
    checkout = await provider().create(intent(description="Пополнение баланса на 179 ₽ " + "x" * 200))
    assert checkout.kind == "url" and checkout.pay_url is not None
    assert checkout.pay_url.startswith(f"{FAKE_BASE_URL}/Index.aspx?")
    invoice = desk.open_link(checkout.pay_url)
    assert invoice.inv_id == checkout.external_id == str(inv_id_for(PID))
    params = invoice.params
    assert params["OutSum"] == "179.00" and params[SHP_KEY] == PID and "IsTest" not in params
    assert len(params["Description"]) == 100 and params["Description"].startswith("Пополнение")
    assert params["Culture"] == "ru" and params["Encoding"] == "utf-8"
    assert "c0ffee" not in checkout.pay_url and " " not in checkout.pay_url


async def test_test_instance_link_uses_is_test_and_test_passwords() -> None:
    desk = FakeRobokassa()
    checkout = await provider(is_test=True).create(intent())
    assert checkout.pay_url is not None
    invoice = desk.open_link(checkout.pay_url)
    assert invoice.is_test and invoice.params["IsTest"] == "1"
    with pytest.raises(ValueError, match="Password #1"):  # live passwords on a test link are refused
        desk.open_link((await provider(is_test=True, password1=PASSWORD1).create(intent())).pay_url or "")


async def test_create_makes_no_request_and_refuses_other_currencies() -> None:
    http = CountingHttp(FakeRobokassa())
    await provider(http).create(intent())
    with pytest.raises(ProviderError):
        await provider(http).create(intent(currency="USD"))
    assert http.requests == 0


# -------------------------------------------------------------------------------------- Result URL


async def test_result_url_paid_event_and_ack() -> None:
    desk = FakeRobokassa()
    plugin = provider()
    params = await paid_params(desk, plugin)
    assert params["OutSum"] == "179.000000" and params["SignatureValue"].isupper()
    event = await plugin.parse_webhook(result(params))
    assert event.state is PaymentState.PAID and event.amount == Decimal("179") and event.currency == "RUB"
    assert event.external_id == params["InvId"] and event.payment_id == PID and not event.is_test
    assert "EMail" not in event.summary and event.summary["Fee"] == params["Fee"]
    resp = plugin.ack(event)
    assert resp.status == 200 and resp.body == f"OK{params['InvId']}".encode()
    assert plugin.ack(None).body == b"OK"


async def test_result_url_in_get_mode_and_lower_case_signature() -> None:
    desk = FakeRobokassa()
    plugin = provider()
    params = await paid_params(desk, plugin)
    event = await plugin.parse_webhook(result(params, method="GET"))
    assert event.payment_id == PID
    lower = {**params, "SignatureValue": params["SignatureValue"].lower()}
    assert (await plugin.parse_webhook(result(lower))).state is PaymentState.PAID


@pytest.mark.parametrize(
    "change",
    [
        {"OutSum": "1.000000"},
        {"InvId": "1"},
        {SHP_KEY: "0190f5d2-7b1e-7c3a-9d4e-000000000000"},
        {"Shp_extra": "1"},
        {"SignatureValue": ""},
        {"SignatureValue": "0" * 32},
    ],
    ids=["outsum", "invid", "shp", "added-shp", "empty-sig", "zero-sig"],
)
async def test_any_change_is_401(change: dict[str, str]) -> None:
    desk = FakeRobokassa()
    plugin = provider()
    params = {**(await paid_params(desk, plugin)), **change}
    with pytest.raises(WebhookRejected) as err:
        await plugin.parse_webhook(result(params))
    assert err.value.status == 401


async def test_unsigned_fields_do_not_matter_but_shp_does() -> None:
    desk = FakeRobokassa()
    plugin = provider()
    params = await paid_params(desk, plugin)
    params["EMail"] = "other@example.com"  # Fee, EMail, PaymentMethod are not signed by Robokassa
    assert (await plugin.parse_webhook(result(params))).state is PaymentState.PAID
    without = {k: v for k, v in params.items() if k != "OutSum"}
    with pytest.raises(WebhookRejected):
        await plugin.parse_webhook(result(without))


async def test_wrong_password2_or_live_password_on_test_notification() -> None:
    desk = FakeRobokassa()
    plugin = provider()
    params = await paid_params(desk, plugin)
    with pytest.raises(WebhookRejected):
        await provider(password2="other").parse_webhook(result(params))
    test_desk = FakeRobokassa()
    test_plugin = provider(is_test=True)
    test_params = await paid_params(test_desk, test_plugin)
    assert test_params["IsTest"] == "1"
    event = await test_plugin.parse_webhook(result(test_params))
    assert event.is_test
    with pytest.raises(WebhookRejected):  # a live instance cannot even authenticate a test notification
        await plugin.parse_webhook(result(test_params))


@pytest.mark.parametrize(
    ("body", "status"),
    [
        (b"OutSum=1&OutSum=2&InvId=1&SignatureValue=AA", 400),
        (b"\xff\xfe", 400),
        (b"a&&b", 400),
        (b"InvId=1", 401),
    ],
    ids=["duplicate", "not-utf8", "not-a-form", "no-signature"],
)
async def test_malformed_requests(body: bytes, status: int) -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(WebhookRequest(body=body))
    assert err.value.status == status


async def test_signed_garbage_amount_is_400_and_foreign_shp_has_no_payment_id() -> None:
    plugin = provider()
    bad = {"OutSum": "abc", "InvId": "77"}
    bad["SignatureValue"] = result_sign("md5", PASSWORD2, bad)
    with pytest.raises(WebhookRejected) as err:
        await plugin.parse_webhook(result(bad))
    assert err.value.status == 400
    foreign = {"OutSum": "179.000000", "InvId": "78", "Shp_svbg": "order-42"}
    foreign["SignatureValue"] = result_sign("md5", PASSWORD2, foreign)
    event = await plugin.parse_webhook(result(foreign))
    assert event.payment_id is None and event.external_id == "78"


# ------------------------------------------------------------------------------------------ status


@pytest.mark.parametrize(
    ("code", "state"),
    [
        (5, PaymentState.CREATED),
        (10, PaymentState.CANCELED),
        (20, PaymentState.PROCESSING),
        (50, PaymentState.PROCESSING),
        (60, PaymentState.REFUNDED),
        (80, PaymentState.PROCESSING),
        (100, PaymentState.PAID),
    ],
)
async def test_fetch_status_states(code: int, state: PaymentState) -> None:
    desk = FakeRobokassa()
    desk.add_invoice("42", "179.00", shp={SHP_KEY: PID})
    desk.set_state("42", code)
    [status] = await provider(CountingHttp(desk)).fetch_status(["42"])
    assert status.state is state and status.external_id == "42" and status.payment_id == PID
    assert status.amount == Decimal("179") and status.currency == "RUB"
    assert (status.paid_at is not None) is (state is PaymentState.PAID)
    if status.paid_at is not None:
        assert abs((status.paid_at - datetime.now(UTC)).total_seconds()) < 5


async def test_fetch_status_request_shape_and_missing_ids() -> None:
    desk = FakeRobokassa()
    desk.add_invoice("42")
    http = CountingHttp(desk)
    statuses = await provider(http).fetch_status(["42", "43", "42", ""])
    assert [s.external_id for s in statuses] == ["42"]
    assert desk.state_calls == ["42", "43"]
    call = http.calls[0]
    assert call.method == "GET" and call.url == f"{FAKE_BASE_URL}/WebService/Service.asmx/OpStateExt"
    assert call.params == {
        "MerchantLogin": LOGIN,
        "InvoiceID": "42",
        "Signature": state_signature("md5", LOGIN, "42", PASSWORD2),
    }
    assert PASSWORD2 not in str(call.params)


@pytest.mark.parametrize(
    ("setup", "retryable", "words"),
    [
        ({"result_code": 1}, False, "Пароль #2"),
        ({"result_code": 2}, False, "идентификатор"),
        ({"result_code": 1000}, True, "1000"),
        ({"fail_with": 503}, True, "503"),
        ({"fail_with": 404}, False, "HTTP 404"),
        ({"answer": b"<html>maintenance</html>"}, True, "непонятный"),
        (
            {"answer": b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><OperationStateResponse/>'},
            True,
            "",
        ),
    ],
    ids=["bad-signature", "no-shop", "internal", "503", "404", "html", "doctype"],
)
async def test_fetch_status_errors(setup: dict[str, Any], retryable: bool, words: str) -> None:
    desk = FakeRobokassa()
    desk.add_invoice("42")
    for key, value in setup.items():
        setattr(desk, key, value)
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(desk)).fetch_status(["42"])
    assert err.value.retryable is retryable and words in err.value.human
    assert PASSWORD2 not in err.value.human


async def test_two_operations_with_one_inv_id_are_not_guessed() -> None:
    desk = FakeRobokassa()
    desk.result_code = 4
    assert await provider(CountingHttp(desk)).fetch_status(["42"]) == []


async def test_test_instance_never_asks_op_state() -> None:
    http = CountingHttp(FakeRobokassa())
    assert await provider(http, is_test=True).fetch_status(["42"]) == []
    probe = await provider(http, is_test=True).test_credentials()
    assert probe.ok and "Тестовый режим" in probe.message and http.requests == 0


def test_op_state_parser_rejects_foreign_xml() -> None:
    with pytest.raises(ValueError, match="unexpected"):
        parse_op_state(b"<Other/>")
    root = parse_op_state(
        b'<OperationStateResponse xmlns="x"><Result><Code>3</Code></Result></OperationStateResponse>'
    )
    assert root is not None


# ------------------------------------------------------------------------------------------- probe


async def test_test_credentials() -> None:
    desk = FakeRobokassa()
    ok = await provider(CountingHttp(desk)).test_credentials()
    assert ok.ok and "Пароль #1" in ok.message and desk.invoices == {}
    assert len(desk.state_calls) == 1 and desk.state_calls[0].isdigit()
    bad = await provider(CountingHttp(FakeRobokassa()), password2="wrong").test_credentials()
    assert not bad.ok and "Пароль #2" in bad.message
    algo = await provider(CountingHttp(FakeRobokassa()), hash_algorithm="sha256").test_credentials()
    assert not algo.ok and "алгоритм" in algo.message
    shop = await provider(CountingHttp(FakeRobokassa()), merchant_login="nobody").test_credentials()
    assert not shop.ok and "идентификатор" in shop.message
    html = FakeRobokassa()
    html.answer = b"<html>hello</html>"
    other = await provider(CountingHttp(html)).test_credentials()
    assert not other.ok and "Адрес Robokassa" in other.message
    down = FakeRobokassa()
    down.fail_with = 502
    assert not (await provider(CountingHttp(down)).test_credentials()).ok


def test_config_defaults_and_secrets() -> None:
    plugin = provider(base_url=None)
    assert plugin.config.base_url == DEFAULT_BASE_URL and plugin.config.hash_algorithm == "md5"
    assert PASSWORD1 not in repr(plugin.config) and PASSWORD2 not in repr(plugin.config)
    assert set(plugin.config.secret_values()) == {PASSWORD1, PASSWORD2}


# ------------------------------------------------------------------------------ through the real core


async def test_refund_is_learned_from_status(make_harness: HarnessFactory) -> None:
    desk = FakeRobokassa()
    harness = await make_harness(Robokassa, CONFIG, http=CountingHttp(desk))
    result_ = await harness.core.create_payment(
        user_id=harness.user_id,
        instance_id=harness.instance.id,
        amount_minor=17_900,
        currency="RUB",
        description="x",
    )
    assert result_.checkout.pay_url is not None and result_.checkout.external_id is not None
    ext = result_.checkout.external_id
    desk.open_link(result_.checkout.pay_url)
    assert await harness.send(result(desk.pay(ext))) == 200
    assert await harness.status(result_.payment_id) == "paid"
    desk.set_state(ext, 60)
    await harness.core.verify(harness.instance.id, [(ext, result_.payment_id)])
    assert await harness.status(result_.payment_id) == "refunded" and len(harness.refunded) == 1


async def test_cancelled_then_paid_wins(make_harness: HarnessFactory) -> None:
    desk = FakeRobokassa()
    harness = await make_harness(Robokassa, CONFIG, http=CountingHttp(desk))
    created = await harness.core.create_payment(
        user_id=harness.user_id,
        instance_id=harness.instance.id,
        amount_minor=17_900,
        currency="RUB",
        description="x",
    )
    ext = created.checkout.external_id
    assert ext is not None and created.checkout.pay_url is not None
    desk.open_link(created.checkout.pay_url)
    desk.set_state(ext, 10)
    await harness.core.verify(harness.instance.id, [(ext, created.payment_id)])
    assert await harness.status(created.payment_id) == "canceled"
    assert await harness.send(result(desk.pay(ext))) == 200
    assert await harness.status(created.payment_id) == "paid" and len(harness.credited) == 1


async def test_different_out_sum_is_mismatch(make_harness: HarnessFactory, db: CountingDatabase) -> None:
    desk = FakeRobokassa()
    harness = await make_harness(Robokassa, CONFIG, http=CountingHttp(desk))
    pid = await harness.pending(external_id="9001")
    desk.add_invoice("9001", "100.00", shp={SHP_KEY: pid})
    assert await harness.send(result(desk.pay("9001"))) == 200
    assert await harness.status(pid) == "mismatch" and harness.credited == []
    row = await payment_row(db, pid)
    assert row["paid_amount_minor"] == 10_000


async def test_end_to_end_over_real_http_and_route(
    make_harness: HarnessFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """InstanceHttp against the fake Robokassa server (OpStateExt) and the Result URL POSTed as a form to
    ``/webhooks/pay/{id}/{token}`` over a socket; the answer must be exactly ``OK<InvId>``."""
    from aiohttp import web

    from svbg.web.routes.payments import payment_routes

    async with FakeRobokassa() as desk:
        http = InstanceHttp()
        try:
            harness = await make_harness(Robokassa, {**CONFIG, "base_url": desk.base_url}, http=http)
            assert (await harness.instance.provider.test_credentials()).ok
            created = await harness.core.create_payment(
                user_id=harness.user_id,
                instance_id=harness.instance.id,
                amount_minor=25_050,
                currency="RUB",
                description="Пополнение баланса",
            )
            ext = created.checkout.external_id
            assert ext is not None and created.checkout.pay_url is not None
            pay_url = created.checkout.pay_url
            desk.open_link(pay_url)
            assert dict(parse_qsl(urlsplit(pay_url).query))["OutSum"] == "250.50"
            app = web.Application()
            app.add_routes(payment_routes(harness.core))
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            try:
                port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
                inst = harness.instance
                url = f"http://127.0.0.1:{port}/webhooks/pay/{inst.id}/{inst.webhook_token}"
                with caplog.at_level(logging.DEBUG):
                    params = desk.pay(ext)
                    assert await desk.send(url, params) == (200, f"OK{ext}")
                    assert await desk.send(url, params) == (200, f"OK{ext}")  # a retry is answered the same
                    assert await desk.send(url, {**params, "SignatureValue": "0" * 32}) == (401, "rejected")
                assert await harness.status(created.payment_id) == "paid" and len(harness.credited) == 1
                statuses = await harness.instance.provider.fetch_status([ext])
                assert statuses[0].state is PaymentState.PAID and statuses[0].amount == Decimal("250.5")
                assert PASSWORD2 not in caplog.text and PASSWORD1 not in caplog.text
            finally:
                await runner.cleanup()
        finally:
            await http.close()


async def test_parse_is_fast() -> None:
    plugin = provider()
    reqs = []
    for i in range(300):
        params = {"OutSum": "179.000000", "InvId": str(i + 1), SHP_KEY: PID}
        params["SignatureValue"] = result_sign("md5", PASSWORD2, params)
        reqs.append(result(params))
    started = time.perf_counter()
    for req in reqs:
        await plugin.parse_webhook(req)
    assert (time.perf_counter() - started) * 1000 / len(reqs) < 5
