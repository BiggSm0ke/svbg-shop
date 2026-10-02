"""CloudPayments plugin: the shared TestKit plus protocol vectors from ``docs/providers/cloudpayments.md``."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import unquote_plus, urlencode

import pytest

from svbg.payments.providers.cloudpayments import DEFAULT_BASE_URL, CloudPayments, sign
from svbg.payments.testkit import (
    CountingHttp,
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
    WebhookIgnored,
    WebhookRejected,
    WebhookRequest,
)
from tests.dbkit import CountingDatabase
from tests.fakes.cloudpayments import (
    API_SECRET,
    FAKE_BASE_URL,
    PUBLIC_ID,
    FakeCloudPayments,
    form,
    signed_request,
)
from tests.fakes.cloudpayments import sign as fake_sign
from tests.payments.providers.conftest import HarnessFactory, outcomes

CONFIG = {"public_id": PUBLIC_ID, "api_secret": API_SECRET, "base_url": FAKE_BASE_URL}
PID = "0190f5d2-7b1e-7c3a-9d4e-5f6a7b8c9d0e"

# Specification §11 (computed with OpenSSL and Python; the documentation publishes no vectors).
SPEC_SECRET = "test_api_secret"
SPEC_BODY = (
    b"TransactionId=1504&Amount=10.00&Currency=RUB&PaymentAmount=10.00&PaymentCurrency=RUB"
    b"&DateTime=2026-10-02%2010%3A00%3A00&CardFirstSix=424242&CardLastFour=4242&CardType=Visa"
    b"&CardExpDate=12%2F27&TestMode=1&Status=Completed&OperationType=Payment&GatewayName=Test"
    b"&InvoiceId=42&AccountId=tg_100500"
)
SPEC_CONTENT_HMAC = "6RpezKCVfdyrmULWqd1oBK6vxASIZBAZs4Sf9qMC4ms="
SPEC_X_CONTENT_HMAC = "1KuJs1vzTQpS1vnnaMYwrovBAPk4wtIe+5oVm/ucmao="


def pay_fields(**extra: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "TransactionId": 1504,
        "Amount": "179.00",
        "Currency": "RUB",
        "PaymentAmount": "179.00",
        "PaymentCurrency": "RUB",
        "DateTime": "2026-10-02 10:00:00",
        "CardFirstSix": "424242",
        "CardLastFour": "4242",
        "CardType": "Visa",
        "CardExpDate": "12/27",
        "TestMode": 0,
        "Status": "Completed",
        "OperationType": "Payment",
        "GatewayName": "Tinkoff",
        "InvoiceId": PID,
    }
    fields.update(extra)
    return {k: v for k, v in fields.items() if v is not None}


class CloudPaymentsVectors:
    """:class:`~svbg.payments.testkit.WebhookVectors` of CloudPayments: URL-encoded notifications with both
    HMAC headers. CloudPayments has no «expired» notification (an abandoned invoice is never reported) — the
    vector uses Cancel; a chargeback is not notified at all — the vector uses Refund."""

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
        common = {
            "TransactionId": abs(hash(external_id)) % 10**9,
            "Amount": amount,
            "DateTime": signed_at.strftime("%Y-%m-%d %H:%M:%S"),
            "TestMode": 1 if test else 0,
            "InvoiceId": payment_id,
        }
        if state in (PaymentState.PAID, PaymentState.PROCESSING):
            status = "Completed" if state is PaymentState.PAID else "Authorized"
            fields = {**common, "Currency": currency, "Status": status, "OperationType": "Payment"}
            fields["GatewayName"] = "Test"
        elif state in (PaymentState.CHARGEBACK, PaymentState.REFUNDED):
            fields = {**common, "PaymentTransactionId": 1, "OperationType": "Refund"}
        elif state in (PaymentState.EXPIRED, PaymentState.CANCELED):
            fields = common
        else:
            fields = {**common, "Currency": currency, "Reason": "InsufficientFunds", "ReasonCode": 5051}
        return signed_request(fields)

    def tamper(self, req: WebhookRequest) -> WebhookRequest:
        body = req.body.replace(b"Amount=", b"Amount=1", 1)
        return WebhookRequest(body=body, headers=dict(req.headers))

    def strip_auth(self, req: WebhookRequest) -> WebhookRequest:
        return WebhookRequest(body=req.body, headers={"Content-Type": "application/x-www-form-urlencoded"})


def provider(http: CountingHttp | None = None, **config: Any) -> CloudPayments:
    p = make_provider(CloudPayments, {**CONFIG, **config}, http=http)
    assert isinstance(p, CloudPayments)
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


# ------------------------------------------------------------------------------------------- TestKit


def test_static_contract() -> None:
    check_static(CloudPayments)
    caps = CloudPayments.capabilities
    assert caps.webhook_auth is WebhookAuth.SIGNATURE and caps.replay_window_s is None
    assert caps.fetch_status and not caps.batch_status and caps.refund and not caps.recurring
    assert set(CloudPayments.manifest.method_kinds) == {MethodKind.CARD, MethodKind.SBP}
    assert CloudPayments.manifest.min_minor is None and CloudPayments.manifest.max_minor is None
    for name, fld in CloudPayments.manifest.config.fields().items():
        assert fld.title and fld.description and fld.where, name
    fields = CloudPayments.manifest.config.fields()
    assert fields["api_secret"].is_secret and not fields["public_id"].is_secret
    assert fields["base_url"].default == DEFAULT_BASE_URL


async def test_testkit_plugin_level() -> None:
    await check_plugin(CloudPayments, CONFIG, CloudPaymentsVectors())


async def test_testkit_core_level(make_harness: HarnessFactory, db: CountingDatabase) -> None:
    """Everything passes except the expiry vector: CloudPayments never notifies about an abandoned invoice
    (the closest notification, Cancel, cancels the payment; a late payment still wins)."""
    harness = await make_harness(CloudPayments, CONFIG, http=CountingHttp(FakeCloudPayments()))
    with pytest.raises(KitFailure) as err:
        await check_core(harness, CloudPaymentsVectors())
    assert err.value.failures == ["expired webhook did not expire the payment"]
    assert len(harness.credited) == 3 and len(harness.refunded) == 1
    assert {"bad_signature", "test_rejected", "mismatch"} <= set(await outcomes(db))


@pytest.mark.parametrize("domain", [True, False], ids=["domain", "no-domain"])
async def test_testkit_poll_budget(make_harness: HarnessFactory, domain: bool) -> None:
    harness = await make_harness(
        CloudPayments, CONFIG, http=CountingHttp(FakeCloudPayments()), has_domain=domain
    )
    used = await check_poll_budget(harness, domain=domain, invoices=3)
    assert used <= (3 if domain else 24) * 3


# ------------------------------------------------------------------------------- signature vectors


def test_spec_vectors() -> None:
    assert len(SPEC_BODY) == 291
    assert sign(SPEC_SECRET, SPEC_BODY) == SPEC_CONTENT_HMAC
    decoded = unquote_plus(SPEC_BODY.decode()).encode()
    assert b"DateTime=2026-10-02 10:00:00" in decoded and b"CardExpDate=12/27" in decoded
    assert sign(SPEC_SECRET, decoded) == SPEC_X_CONTENT_HMAC
    assert fake_sign(SPEC_SECRET, SPEC_BODY) == SPEC_CONTENT_HMAC
    # RFC 4231 test case 2 as base64 (tool check of the specification)
    assert sign("Jefe", b"what do ya want for nothing?") == "W9zBRr9gdU5qBCQmCJV1x1oAPwidJzmDnexYuWTsOEM="


@pytest.mark.parametrize(
    "headers",
    [
        {"Content-HMAC": SPEC_CONTENT_HMAC, "X-Content-HMAC": SPEC_X_CONTENT_HMAC},
        {"Content-HMAC": SPEC_CONTENT_HMAC},
        {"X-Content-HMAC": SPEC_X_CONTENT_HMAC},
        {"content-hmac": "wrong", "x-content-hmac": SPEC_X_CONTENT_HMAC},
    ],
)
async def test_spec_vector_authenticates(headers: dict[str, str]) -> None:
    """The specification's body is authentic; its ``InvoiceId=42`` is not one of our ids, so it is
    acknowledged with ``{"code":0}`` instead of being applied."""
    with pytest.raises(WebhookIgnored) as err:
        await provider(api_secret=SPEC_SECRET).parse_webhook(WebhookRequest(body=SPEC_BODY, headers=headers))
    assert err.value.reason == "no invoice of ours" and json.loads(err.value.response.body) == {"code": 0}


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Content-HMAC": ""},
        {"Content-HMAC": SPEC_X_CONTENT_HMAC},  # the decoded-body MAC in the raw-body header
        {"X-Content-HMAC": SPEC_CONTENT_HMAC},
        {"Content-HMAC": SPEC_CONTENT_HMAC.lower()},
        {"Content-HMAC": hmac.new(SPEC_SECRET.encode(), SPEC_BODY, hashlib.sha256).hexdigest()},  # hex
        {"Content-HMAC": fake_sign("another_secret", SPEC_BODY)},
    ],
)
async def test_wrong_or_missing_signature_is_401(headers: dict[str, str]) -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider(api_secret=SPEC_SECRET).parse_webhook(WebhookRequest(body=SPEC_BODY, headers=headers))
    assert err.value.status == 401


async def test_changed_amount_is_rejected() -> None:
    body = SPEC_BODY.replace(b"Amount=10.00", b"Amount=99.00")
    with pytest.raises(WebhookRejected) as err:
        await provider(api_secret=SPEC_SECRET).parse_webhook(
            WebhookRequest(
                body=body, headers={"Content-HMAC": SPEC_CONTENT_HMAC, "X-Content-HMAC": SPEC_X_CONTENT_HMAC}
            )
        )
    assert err.value.status == 401


async def test_x_content_hmac_with_plus_as_space() -> None:
    """A body encoded with ``+`` for spaces: ``X-Content-HMAC`` over the decoded form is accepted."""
    body = urlencode({k: str(v) for k, v in pay_fields().items()}).encode()
    assert b"+" in body
    decoded = unquote_plus(body.decode()).encode()
    event = await provider().parse_webhook(
        WebhookRequest(body=body, headers={"X-Content-HMAC": fake_sign(API_SECRET, decoded)})
    )
    assert event.state is PaymentState.PAID


# ----------------------------------------------------------------------------------- notifications


async def test_pay_completed_is_paid() -> None:
    req = signed_request(pay_fields())
    event = await provider().parse_webhook(
        WebhookRequest(
            body=req.body, headers=dict(req.headers), received_at=datetime(2026, 10, 2, 10, 1, tzinfo=UTC)
        )
    )
    assert event.state is PaymentState.PAID and event.payment_id == PID and event.external_id is None
    assert event.amount == Decimal("179") and event.currency == "RUB" and not event.is_test
    assert event.paid_at == datetime(2026, 10, 2, 10, 0, tzinfo=UTC) and event.signed_at is None
    assert event.summary["transaction_id"] == "1504" and event.summary["kind"] == "pay"
    assert "4242" not in json.dumps(dict(event.summary))  # no card data in the event log


async def test_implausible_payment_time_is_dropped() -> None:
    event = await provider().parse_webhook(signed_request(pay_fields(DateTime="2031-01-01 00:00:00")))
    assert event.paid_at is None


@pytest.mark.parametrize(
    ("fields", "state", "kind"),
    [
        (pay_fields(Status="Authorized"), PaymentState.PROCESSING, "pay"),
        (pay_fields(Status="Completed", TestMode=1), PaymentState.PAID, "pay"),
        (
            pay_fields(Status=None, GatewayName=None, Reason="InsufficientFunds", ReasonCode=5051),
            PaymentState.CREATED,
            "fail",
        ),
        (
            {"TransactionId": 1505, "Amount": "179.00", "DateTime": "2026-10-02 10:05:00", "InvoiceId": PID},
            PaymentState.CANCELED,
            "cancel",
        ),
        (
            {
                "TransactionId": 1506,
                "PaymentTransactionId": 1504,
                "Amount": "50.00",
                "DateTime": "2026-10-02 11:00:00",
                "OperationType": "Refund",
                "InvoiceId": PID,
            },
            PaymentState.REFUNDED,
            "refund",
        ),
    ],
)
async def test_notification_kinds(fields: dict[str, Any], state: PaymentState, kind: str) -> None:
    event = await provider().parse_webhook(signed_request(fields))
    assert event.state is state and event.payment_id == PID and event.summary["kind"] == kind


async def test_fail_keeps_the_reason_code() -> None:
    fields = pay_fields(Status=None, GatewayName=None, Reason="InsufficientFunds", ReasonCode=5051)
    event = await provider().parse_webhook(signed_request(fields))
    assert event.summary["reason_code"] == "5051"


@pytest.mark.parametrize(
    ("fields", "query"),
    [
        (pay_fields(GatewayName=None), {}),  # Check: no GatewayName, the payment is not authorized yet
        (pay_fields(), {"type": "check"}),  # Check sent to the URL marked ?type=check
        (
            {"Id": "sc_8cf8a9338fb8ebf7202b08d09c938", "Status": "Active", "Interval": "Month"},
            {},
        ),  # Recurrent
        (pay_fields(OperationType="CardPayout"), {}),
        (pay_fields(InvoiceId="order-42"), {}),  # not an invoice of this bot
        (pay_fields(InvoiceId=None), {}),
        (pay_fields(Status="Unknown"), {}),
    ],
)
async def test_acknowledged_without_a_state(fields: dict[str, Any], query: dict[str, str]) -> None:
    with pytest.raises(WebhookIgnored) as err:
        await provider().parse_webhook(signed_request(fields, query=query))
    assert err.value.response.status == 200 and json.loads(err.value.response.body) == {"code": 0}


@pytest.mark.parametrize(
    "body",
    [
        form(pay_fields(Amount="abc")),
        form(pay_fields(Amount="-1")),
        form(pay_fields(Currency="R")),
        form(pay_fields(Currency=None)),
        b"TransactionId=1&Amount=1\xff",
        b"not a form",
    ],
)
async def test_malformed_authentic_bodies_are_400(body: bytes) -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(signed_request(body, headers=("Content-HMAC",)))
    assert err.value.status == 400


async def test_get_notifications_are_refused() -> None:
    req = signed_request(pay_fields())
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(WebhookRequest(body=req.body, headers=dict(req.headers), method="GET"))
    assert err.value.status == 400


def test_ack_is_code_zero() -> None:
    resp = provider().ack(None)
    assert resp.status == 200 and json.loads(resp.body) == {"code": 0}


# ------------------------------------------------------------------------------------------ create


async def test_create_order_shape_and_privacy() -> None:
    desk = FakeCloudPayments()
    http = CountingHttp(desk)
    checkout = await provider(http).create(intent(return_url="https://t.me/demo_bot"))
    call = http.calls[0]
    assert call.method == "POST" and call.url == f"{FAKE_BASE_URL}/orders/create"
    assert (
        call.headers["Authorization"].startswith("Basic ")
        and call.headers["X-Request-ID"] == f"svbg-order-{PID}"
    )
    assert call.json == {
        "Amount": 179,
        "Currency": "RUB",
        "Description": "Пополнение баланса на 179 ₽",
        "InvoiceId": PID,
        "RequireConfirmation": False,
        "SendEmail": False,
        "CultureName": "ru-RU",
        "SuccessRedirectUrl": "https://t.me/demo_bot",
        "FailRedirectUrl": "https://t.me/demo_bot",
    }
    assert "c0ffee" not in json.dumps(call.json)  # no AccountId: only the opaque payment id leaves the bot
    assert checkout.kind == "url" and checkout.external_id == PID
    assert checkout.pay_url is not None and checkout.pay_url.startswith(
        "https://orders.cloudpayments.fake/d/"
    )


async def test_create_is_idempotent_by_request_id() -> None:
    desk = FakeCloudPayments()
    plugin = provider(CountingHttp(desk))
    first = await plugin.create(intent(amount_minor=17_950))
    second = await plugin.create(intent(amount_minor=17_950))
    assert first.pay_url == second.pay_url and len(desk.created) == 1
    assert desk.created[0][1]["Amount"] == 179.5


async def test_sbp_button_goes_to_the_sbp_link() -> None:
    desk = FakeCloudPayments()
    http = CountingHttp(desk)
    checkout = await provider(http).create(intent(method_hint=MethodKind.SBP))
    call = http.calls[0]
    assert call.url == f"{FAKE_BASE_URL}/payments/qr/sbp/link"
    assert call.json["PublicId"] == PUBLIC_ID and call.json["Scheme"] == "charge"
    assert call.json["InvoiceId"] == PID and call.json["Currency"] == "RUB"
    assert checkout.external_id == PID and checkout.pay_url is not None
    assert checkout.pay_url.startswith("https://qr.nspk.fake/")


@pytest.mark.parametrize(
    ("config", "kw"),
    [
        ({"sbp_link": "false"}, {"method_hint": MethodKind.SBP}),
        ({}, {"method_hint": MethodKind.SBP, "currency": "EUR"}),
    ],
)
async def test_sbp_falls_back_to_the_payment_page(config: dict[str, Any], kw: dict[str, Any]) -> None:
    http = CountingHttp(FakeCloudPayments())
    await provider(http, **config).create(intent(**kw))
    assert http.calls[0].url == f"{FAKE_BASE_URL}/orders/create"


@pytest.mark.parametrize(
    ("answer", "retryable", "words"),
    [
        (HttpResponse(401, b""), False, "Public ID или API Secret"),
        (HttpResponse(429, b""), True, "временно недоступен"),
        (HttpResponse(503, b""), True, "временно недоступен"),
        (HttpResponse(400, b""), False, "HTTP 400"),
        (
            HttpResponse(200, b'{"Success": false, "Message": "Amount is required"}'),
            False,
            "Amount is required",
        ),
        (HttpResponse(200, b"<html>"), True, "непонятный"),
        (HttpResponse(200, b'{"Success": true, "Model": {"Url": "ftp://x"}}'), False, "непонятный"),
    ],
)
async def test_create_errors(answer: HttpResponse, retryable: bool, words: str) -> None:
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(lambda _call: answer)).create(intent())
    assert err.value.retryable is retryable and words in err.value.human
    assert API_SECRET not in err.value.human


async def test_unsupported_currency_is_refused_before_the_api() -> None:
    http = CountingHttp(FakeCloudPayments())
    with pytest.raises(ProviderError, match="RUB, USD, EUR и GBP"):
        await provider(http).create(intent(currency="JPY"))
    assert http.requests == 0


# ----------------------------------------------------------------------------- status, refund, probe


async def test_fetch_status() -> None:
    desk = FakeCloudPayments()
    ids = [f"0190f5d2-7b1e-7c3a-9d4e-5f6a7b8c9d0{n}" for n in range(6)]
    desk.pay(ids[0], test=False)
    desk.pay(ids[1], status="Declined")
    desk.pay(ids[2], status="Authorized")
    desk.pay(ids[3], status="Cancelled")
    desk.pay(ids[4]).refunded = True
    statuses = await provider(CountingHttp(desk)).fetch_status([*ids, ids[0]])
    by_id = {s.external_id: s for s in statuses}
    assert set(by_id) == set(ids[:5])  # ids[5] is «Not found»: absent
    paid = by_id[ids[0]]
    assert paid.state is PaymentState.PAID and paid.amount == Decimal("179") and paid.currency == "RUB"
    assert paid.payment_id == ids[0] and not paid.is_test and paid.paid_at is not None
    assert by_id[ids[1]].state is PaymentState.CREATED and by_id[ids[1]].is_test
    assert by_id[ids[2]].state is PaymentState.PROCESSING
    assert by_id[ids[3]].state is PaymentState.CANCELED
    assert by_id[ids[4]].state is PaymentState.REFUNDED


async def test_fetch_status_errors() -> None:
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(lambda _call: HttpResponse(502, b""))).fetch_status([PID])
    assert err.value.retryable


async def test_refund() -> None:
    desk = FakeCloudPayments()
    op = desk.pay(PID)
    plugin = provider(CountingHttp(desk))
    partial = await plugin.refund(PID, 10_000, "RUB")
    assert partial.ok and desk.refund_calls[-1] == {"TransactionId": op.transaction_id, "Amount": 100}
    rest = await plugin.refund(PID, 7_900, "RUB")
    assert rest.ok and op.refunds == [Decimal(100), Decimal(79)]
    too_much = await plugin.refund(PID, 100, "RUB")
    assert not too_much.ok and "exceeds" in too_much.message


async def test_refund_of_unknown_or_unpaid_invoice() -> None:
    desk = FakeCloudPayments()
    assert not (await provider(CountingHttp(desk)).refund(PID, 17_900, "RUB")).ok
    desk.pay(PID, status="Declined")
    result = await provider(CountingHttp(desk)).refund(PID, 17_900, "RUB")
    assert not result.ok and desk.refund_calls == []


async def test_test_credentials() -> None:
    desk = FakeCloudPayments()
    http = CountingHttp(desk)
    assert (await provider(http).test_credentials()).ok
    assert http.calls[0].url == f"{FAKE_BASE_URL}/test" and desk.created == []
    bad = await provider(CountingHttp(desk), api_secret="wrong").test_credentials()
    assert not bad.ok and "API Secret" in bad.message


# ------------------------------------------------------------------------------------ through the core


async def test_end_to_end_through_the_core(make_harness: HarnessFactory) -> None:
    desk = FakeCloudPayments()
    harness = await make_harness(CloudPayments, CONFIG, http=CountingHttp(desk))
    result = await harness.core.create_payment(
        user_id=harness.user_id,
        instance_id=harness.instance.id,
        amount_minor=17_900,
        currency="RUB",
        description="Пополнение баланса",
    )
    assert result.checkout.external_id == result.payment_id
    # a declined attempt, then a successful one on the same page
    declined = desk.pay(result.payment_id, status="Declined", test=False)
    fail = desk.pay_notification(declined, Status=None, GatewayName=None, Reason="Declined", ReasonCode=5005)
    assert await harness.send(fail) == 200
    assert await harness.status(result.payment_id) == "pending"
    op = desk.pay(result.payment_id, test=False)
    resp = await harness.core.handle_webhook(
        harness.instance.id, harness.instance.webhook_token, desk.pay_notification(op)
    )
    assert resp.status == 200 and json.loads(resp.body) == {"code": 0}
    assert await harness.status(result.payment_id) == "paid" and len(harness.credited) == 1
    await harness.core.verify(harness.instance.id, [(result.payment_id, result.payment_id)])
    assert len(harness.credited) == 1
    assert await harness.send(desk.refund_notification(op, "179.00")) == 200
    assert await harness.status(result.payment_id) == "refunded" and len(harness.refunded) == 1


async def test_test_site_payments_on_a_test_instance(make_harness: HarnessFactory) -> None:
    """``TestMode=1`` is accepted by a test instance (a live one rejects it: TestKit above)."""
    desk = FakeCloudPayments()
    test = await make_harness(CloudPayments, CONFIG, http=CountingHttp(desk), is_test=True)
    pid2 = await test.pending()
    assert await test.send(desk.pay_notification(desk.pay(pid2, test=True))) == 200
    assert await test.status(pid2) == "paid"


async def test_lost_notification_is_found_by_the_status_check(make_harness: HarnessFactory) -> None:
    desk = FakeCloudPayments()
    harness = await make_harness(CloudPayments, CONFIG, http=CountingHttp(desk))
    result = await harness.core.create_payment(
        user_id=harness.user_id,
        instance_id=harness.instance.id,
        amount_minor=17_900,
        currency="RUB",
        description="x",
    )
    desk.pay(result.payment_id, test=False)
    await harness.core.verify(harness.instance.id, [(result.payment_id, result.payment_id)])
    assert await harness.status(result.payment_id) == "paid"
