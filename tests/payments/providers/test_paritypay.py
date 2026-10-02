"""ParityPay plugin: the shared TestKit plus the protocol vectors of ``docs/providers/paritypay.md`` (§10)."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest

from svbg.payments.providers.paritypay import (
    DEFAULT_BASE_URL,
    ParityPay,
    signature_string,
    webhook_signature,
)
from svbg.payments.registry import InstanceHttp
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
from tests.fakes.paritypay import API_KEY, FAKE_BASE_URL, SHOP_ID, WEBHOOK_KEY, FakeParityPay, sign
from tests.payments.providers.conftest import HarnessFactory, outcomes

CONFIG = {"shop_id": SHOP_ID, "api_key": API_KEY, "webhook_key": WEBHOOK_KEY, "base_url": FAKE_BASE_URL}
DIRECT = {**CONFIG, "confirm_via_api": "false"}
PID = "0190f5d2-7b1e-7c3a-9d4e-5f6a7b8c9d0e"
INV = "9beea835-0937-4b5c-8f5a-c3a0d0e60346"
OTHER_KEY = "key-2-of-another-desk"
_STATUS = {
    PaymentState.CREATED: "NEW",
    PaymentState.PROCESSING: "NEW",
    PaymentState.PAID: "PAID",
    PaymentState.EXPIRED: "EXPIRED",
    PaymentState.CANCELED: "EXPIRED",
    PaymentState.FAILED: "ERROR",
    PaymentState.CHARGEBACK: "REFUNDED",  # ParityPay has no chargeback status
    PaymentState.REFUNDED: "REFUNDED",
}

# ----------------------------------------------------------------------- specification vectors (§10)

V1_BODY = (
    '{"id":"9beea835-0937-4b5c-8f5a-c3a0d0e60346","order_id":"order-1001",'
    '"shop_id":"874dfb1e-dbdb-4747-a1c0-005969725b74","amount":"1250.00","credited":1209.01,'
    '"comment":"Оплата заказа №1001","service":"sbp","custom_fields":null,'
    '"expires":"2026-08-24 13:40:00","created":"2026-08-24 12:40:00","status":"PAID"}'
)
V2_BODY = (
    '{"id":"7157f9e5-b49f-482b-9288-df49e6f5342e","order_id":"aeca1ac8-72e3-408b-81e3-0a6028751e3f_2",'
    '"shop_id":"874dfb1e-dbdb-4747-a1c0-005969725b74","amount":"990.00","credited":957.33,"comment":null,'
    '"service":"sbp","custom_fields":null,"expires":"2026-09-24 13:41:00","created":"2026-09-24 12:41:00",'
    '"status":"PAID","subscription_id":"aeca1ac8-72e3-408b-81e3-0a6028751e3f"}'
)
V3_BODY = V1_BODY.replace('"PAID"', '"REFUNDED"')
V4_BODY = (
    '{"id":"aeca1ac8-72e3-408b-81e3-0a6028751e3f","shop_subscription_id":"order-1002",'
    '"shop_id":"874dfb1e-dbdb-4747-a1c0-005969725b74","amount":"990.00","client_email":"client@example.com",'
    '"interval":"1m","start_at":"2026-08-24 12:41:00","next_debited_at":"2026-09-24 12:41:00",'
    '"last_debited_at":"2026-08-24 12:41:00","status":"active","description":"Премиум-доступ, ежемесячно"}'
)
V5_BODY = (
    '{"id":"d04e951d-08cf-498b-9f9f-1b4183d662f8","shop_subscription_id":"order-0998",'
    '"shop_id":"874dfb1e-dbdb-4747-a1c0-005969725b74","amount":"490.00","client_email":null,"interval":"1w",'
    '"start_at":null,"next_debited_at":null,"last_debited_at":null,"status":"failed",'
    '"description":"Еженедельный доступ"}'
)
V6_BODY = (
    '{"id":"3569a18b-86ad-46c4-ad5a-aef3372dbc40","order_id":"payoff-501",'
    '"shop_id":"874dfb1e-dbdb-4747-a1c0-005969725b74","amount":"1000.00","debit":"1000.00",'
    '"commission":"50.00","amount_to_payoff":"950.00","payment_details":"4111111111111111","service":"card",'
    '"status":"SUCCESS"}'
)
VECTORS = {
    "V1": (V1_BODY, "587deb7ab32d66779cc5463cd1a39b29ed95e5a138dfedd677c81c5d928a6d6c"),
    "V2": (V2_BODY, "3d63aa6c5edc2af3d513f25cc19d838e9eeca0d66eebe6a518d985f658ce99df"),
    "V3": (V3_BODY, "010b09ae5573a222d0752236a96f51d636a84fafafc40fe52c1e929626f26c64"),
    "V4": (V4_BODY, "ff2e139aff6c740a07652fc97f59762c56c9dbddc5848e07d7565b901a863841"),
    "V5": (V5_BODY, "e87ee18db2d88febdec02a45286087ecc8df05f6567c6af05d833585408c25ad"),
    "V6": (V6_BODY, "f1895b2d3ce187af4fe425d94a541aa2721923254664386d3a3881dc05468ce2"),
}
V1_STRING = (
    "1250.00Оплата заказа №10012026-08-24 12:40:001209.012026-08-24 13:40:00"
    "9beea835-0937-4b5c-8f5a-c3a0d0e60346order-1001sbp874dfb1e-dbdb-4747-a1c0-005969725b74PAID"
)
N1_VALID = "8d80ea73f495255336af9f9e0452ff97ef7383b4199c1380b85660d4ed0c9d12"


def note(body: str | bytes, signature: str | None) -> WebhookRequest:
    raw = body.encode() if isinstance(body, str) else body
    headers = {"Content-Type": "application/json"}
    if signature is not None:
        headers["X-SIGNATURE"] = signature
    return WebhookRequest(body=raw, headers=headers)


def literal_fields(body: str) -> dict[str, Any]:
    return json.loads(body, parse_float=str, parse_int=str)


class Raw(str):
    """A JSON number literal inside :func:`signed_invoice`."""


def signed_invoice(*, key: str = WEBHOOK_KEY, **fields: Any) -> WebhookRequest:
    """An invoice notification; numbers in ``fields`` must be given as ``Raw`` literals."""
    data: dict[str, Any] = {
        "id": INV,
        "order_id": PID,
        "shop_id": SHOP_ID,
        "amount": "179.00",
        "credited": Raw("173.63"),
        "comment": "Пополнение",
        "service": "sbp",
        "custom_fields": None,
        "expires": "2026-10-02 13:40:00",
        "created": "2026-10-02 12:40:00",
        "status": "PAID",
    }
    data.update(fields)
    textual = {k: (str(v) if isinstance(v, Raw) else v) for k, v in data.items()}
    parts = [
        f"{json.dumps(k)}:{v}"
        if isinstance(v, Raw)
        else f"{json.dumps(k)}:{json.dumps(v, ensure_ascii=False)}"
        for k, v in data.items()
    ]
    return note("{" + ",".join(parts) + "}", sign(key, textual))


class ParityPayVectors:
    """:class:`~svbg.payments.testkit.WebhookVectors` of ParityPay. ``amount`` is a string in ParityPay
    notifications (kept verbatim: ``"179"``, ``"179.00"``); a chargeback is reported as ``REFUNDED``. There is
    no test mode: a «test» notification is signed with another desk's key No. 2 and cannot authenticate."""

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
        return signed_invoice(
            key=OTHER_KEY if test else WEBHOOK_KEY,
            id=external_id,
            order_id=payment_id,
            amount=amount,
            status=_STATUS[state],
        )

    def tamper(self, req: WebhookRequest) -> WebhookRequest:
        data = json.loads(req.body)
        data["amount"] = "1" + data["amount"]
        return note(json.dumps(data, ensure_ascii=False), req.header("X-SIGNATURE"))

    def strip_auth(self, req: WebhookRequest) -> WebhookRequest:
        return note(req.body, None)


def provider(http: CountingHttp | None = None, **config: Any) -> ParityPay:
    p = make_provider(ParityPay, {**CONFIG, **config}, http=http)
    assert isinstance(p, ParityPay)
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
    check_static(ParityPay)
    caps = ParityPay.capabilities
    assert caps.webhook_auth is WebhookAuth.SIGNATURE and caps.replay_window_s is None
    assert caps.fetch_status and caps.batch_status and caps.batch_limit == 100
    assert not caps.refund and not caps.recurring and caps.redirect and caps.webhook
    assert ParityPay.manifest.currencies == ("RUB",)
    assert ParityPay.manifest.method_kinds == (MethodKind.SBP, MethodKind.CARD)
    for name, fld in ParityPay.manifest.config.fields().items():
        assert fld.title and fld.description, name
        if fld.required and fld.default is None:
            assert fld.where, f"{name}: «где взять» is missing"
    fields = ParityPay.manifest.config.fields()
    assert fields["api_key"].is_secret and fields["webhook_key"].is_secret and not fields["shop_id"].is_secret


@pytest.mark.parametrize("config", [CONFIG, DIRECT], ids=["confirm-via-api", "direct"])
async def test_testkit_plugin_level(config: dict[str, Any]) -> None:
    await check_plugin(ParityPay, config, ParityPayVectors())


async def test_testkit_core_level_direct(make_harness: HarnessFactory) -> None:
    harness = await make_harness(ParityPay, DIRECT, http=CountingHttp(FakeParityPay()))
    await check_core(harness, ParityPayVectors())
    assert len(harness.credited) == 3 and len(harness.refunded) == 1


async def test_testkit_core_level_confirm_via_api(make_harness: HarnessFactory, db: CountingDatabase) -> None:
    """Default mode: a notification never credits by itself — the invoice is read again through the API."""
    harness = await make_harness(ParityPay, CONFIG, http=CountingHttp(FakeParityPay()))
    with pytest.raises(KitFailure) as err:
        await check_core(harness, ParityPayVectors())
    assert "replayed body credited 0 times" in err.value.failures
    assert harness.credited == [] and "verify_queued" in await outcomes(db)


@pytest.mark.parametrize("domain", [True, False], ids=["domain", "no-domain"])
async def test_testkit_poll_budget(make_harness: HarnessFactory, domain: bool) -> None:
    harness = await make_harness(ParityPay, CONFIG, http=CountingHttp(FakeParityPay()), has_domain=domain)
    used = await check_poll_budget(harness, domain=domain, invoices=3)
    assert used <= (3 if domain else 24)


# ------------------------------------------------------------------------------------ signature vectors


@pytest.mark.parametrize("name", sorted(VECTORS))
def test_specification_vectors(name: str) -> None:
    body, expected = VECTORS[name]
    fields = literal_fields(body)
    assert webhook_signature(WEBHOOK_KEY, fields) == expected
    # cross-check with an independent HMAC over the same string
    message = signature_string(fields).encode()
    assert hmac.new(WEBHOOK_KEY.encode(), message, hashlib.sha256).hexdigest() == expected


def test_v1_signature_string() -> None:
    assert signature_string(literal_fields(V1_BODY)) == V1_STRING


def test_n1_tampered_amount_has_another_signature() -> None:
    fields = literal_fields(V1_BODY.replace('"1250.00"', '"1.00"'))
    assert webhook_signature(WEBHOOK_KEY, fields) == N1_VALID != VECTORS["V1"][1]


def test_fake_signs_like_the_plugin() -> None:
    fields = literal_fields(V2_BODY)
    assert sign(WEBHOOK_KEY, fields) == VECTORS["V2"][1]


def test_numbers_are_signed_as_their_literals() -> None:
    """PHP prints ``1209.0`` as ``1209``, Python as ``1209.0``: the plugin signs what is in the body."""
    assert signature_string(literal_fields('{"a":1209.0,"b":1209,"c":1.10}')) == "1209.012091.10"


@pytest.mark.parametrize("value", ["true", "false", "[1]", '{"x":1}'])
def test_values_without_a_defined_signature_form(value: str) -> None:
    with pytest.raises(ValueError, match="signature form"):
        signature_string(json.loads(f'{{"a":"1","b":{value}}}', parse_float=str, parse_int=str))


# ---------------------------------------------------------------------------------------- webhooks


async def test_v1_paid_notification_direct() -> None:
    body, sig = VECTORS["V1"]
    event = await provider(confirm_via_api="false").parse_webhook(note(body, sig))
    assert event.state is PaymentState.PAID and event.external_id == INV
    assert event.payment_id is None  # «order-1001» is not one of our payment ids
    assert event.amount == Decimal("1250.00") and event.currency == "RUB" and not event.is_test
    assert event.signed_at is None
    assert event.summary["credited"] == "1209.01" and event.summary["service"] == "sbp"
    assert "comment" not in event.summary


async def test_default_mode_hides_the_currency_to_force_an_api_check() -> None:
    body, sig = VECTORS["V1"]
    event = await provider().parse_webhook(note(body, sig))
    assert event.state is PaymentState.PAID and event.currency is None and event.amount == Decimal("1250")


async def test_v2_subscription_charge_is_an_invoice_without_our_id() -> None:
    body, sig = VECTORS["V2"]
    event = await provider(confirm_via_api="false").parse_webhook(note(body, sig))
    assert event.state is PaymentState.PAID and event.payment_id is None
    assert event.summary["subscription_id"] == "aeca1ac8-72e3-408b-81e3-0a6028751e3f"


async def test_v3_refund_direct() -> None:
    body, sig = VECTORS["V3"]
    event = await provider(confirm_via_api="false").parse_webhook(note(body, sig))
    assert event.state is PaymentState.REFUNDED and event.external_id == INV


async def test_v3_refund_is_confirmed_through_the_api() -> None:
    desk = FakeParityPay()
    desk.add(INV, "order-1001", 1250.0, status="PAID")
    body, sig = VECTORS["V3"]
    with pytest.raises(WebhookIgnored, match="not confirmed"):
        await provider(CountingHttp(desk)).parse_webhook(note(body, sig))
    desk.set_status(INV, "REFUNDED")
    event = await provider(CountingHttp(desk)).parse_webhook(note(body, sig))
    assert event.state is PaymentState.REFUNDED
    assert desk.status_calls[-1] == {"id": INV}


async def test_refund_confirmation_unavailable_is_retried_by_the_provider() -> None:
    desk = FakeParityPay()
    desk.fail_with = 502
    body, sig = VECTORS["V3"]
    with pytest.raises(WebhookRejected) as err:
        await provider(CountingHttp(desk)).parse_webhook(note(body, sig))
    assert err.value.status == 503


@pytest.mark.parametrize("name", ["V4", "V5", "V6"])
async def test_subscription_and_payoff_notifications_are_acknowledged(name: str) -> None:
    body, sig = VECTORS[name]
    with pytest.raises(WebhookIgnored):
        await provider().parse_webhook(note(body, sig))


async def test_n1_tampered_body_is_rejected() -> None:
    body = V1_BODY.replace('"1250.00"', '"1.00"')
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(note(body, VECTORS["V1"][1]))
    assert err.value.status == 401


@pytest.mark.parametrize(
    "signature",
    [None, "", "0" * 64, VECTORS["V2"][1], webhook_signature(OTHER_KEY, literal_fields(V1_BODY))],
    ids=["missing", "empty", "zeros", "other-body", "other-key"],
)
async def test_bad_signatures_are_401(signature: str | None) -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(note(V1_BODY, signature))
    assert err.value.status == 401


async def test_upper_case_hex_is_accepted() -> None:
    event = await provider().parse_webhook(note(V1_BODY, VECTORS["V1"][1].upper()))
    assert event.state is PaymentState.PAID


async def test_reformatted_body_keeps_the_signature() -> None:
    """Field order and whitespace do not matter; the values do."""
    data = json.loads(V1_BODY)
    pretty = json.dumps(dict(reversed(list(data.items()))), ensure_ascii=True, indent=2)
    event = await provider().parse_webhook(note(pretty, VECTORS["V1"][1]))
    assert event.external_id == INV


async def test_credited_written_differently_breaks_the_signature() -> None:
    body = V1_BODY.replace("1209.01", "1209.010")
    with pytest.raises(WebhookRejected):
        await provider().parse_webhook(note(body, VECTORS["V1"][1]))


async def test_another_shop_is_rejected() -> None:
    req = signed_invoice(shop_id="11111111-2222-3333-4444-555555555555")
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(req)
    assert err.value.status == 401


async def test_shop_id_case_does_not_matter() -> None:
    event = await provider().parse_webhook(signed_invoice(shop_id=SHOP_ID.upper()))
    assert event.payment_id == PID


@pytest.mark.parametrize(
    ("raw", "state"),
    [("PAID", PaymentState.PAID), ("EXPIRED", PaymentState.EXPIRED), ("ERROR", PaymentState.FAILED),
     ("NEW", PaymentState.CREATED), ("paid", PaymentState.PAID)],
)  # fmt: skip
async def test_statuses(raw: str, state: PaymentState) -> None:
    event = await provider(confirm_via_api="false").parse_webhook(signed_invoice(status=raw))
    assert event.state is state and event.payment_id == PID and event.amount == Decimal("179")


async def test_unknown_status_is_ignored_with_200() -> None:
    with pytest.raises(WebhookIgnored):
        await provider().parse_webhook(signed_invoice(status="CHARGEBACK"))


@pytest.mark.parametrize("amount", ["179", "179.0", "179.00", Raw("179.00"), Raw("179")])
async def test_amount_as_string_and_as_number(amount: str) -> None:
    event = await provider().parse_webhook(signed_invoice(amount=amount))
    assert event.amount == Decimal("179")


@pytest.mark.parametrize(
    ("fields", "status"),
    [({"amount": "abc"}, 400), ({"amount": None}, 400), ({"id": None}, 400), ({"id": ""}, 400),
     ({"paid": True}, 401)],
)  # fmt: skip
async def test_malformed_notifications(fields: dict[str, Any], status: int) -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(signed_invoice(**fields))
    assert err.value.status == status


@pytest.mark.parametrize("body", [b"not json", b"[]", b"{}", b'{"a":NaN}', b'{"a":"1","a":"2"}', b"\xff"])
async def test_unreadable_bodies_are_400(body: bytes) -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(note(body, "0" * 64))
    assert err.value.status == 400


async def test_ack_is_200() -> None:
    assert provider().ack(None).status == 200


# ------------------------------------------------------------------------------------------- create


async def test_create_without_method() -> None:
    desk = FakeParityPay()
    http = CountingHttp(desk)
    checkout = await provider(http).create(intent(return_url="https://t.me/shop_bot"))
    body = desk.created[0]
    assert body == {
        "order_id": PID,
        "amount": 179,
        "comment": "Пополнение баланса на 179 ₽",
        "expire": 60,
        "success_url": "https://t.me/shop_bot",
        "fail_url": "https://t.me/shop_bot",
        "callback_url": "https://shop.example/webhooks/pay/1/token",
    }
    call = http.calls[0]
    assert call.url == f"{FAKE_BASE_URL}/v2/invoice/create"
    assert call.headers["X-ShopId"] == SHOP_ID and call.headers["X-SecretKey"] == API_KEY
    assert checkout.kind == "url" and checkout.external_id in desk.invoices
    assert checkout.pay_url == f"https://pay.paritypay.fake/{checkout.external_id}"
    assert checkout.expires_at is not None


@pytest.mark.parametrize(("kind", "service"), [(MethodKind.SBP, "sbp"), (MethodKind.CARD, "card")])
async def test_create_with_method(kind: MethodKind, service: str) -> None:
    desk = FakeParityPay()
    await provider(CountingHttp(desk), invoice_minutes="15").create(
        intent(method_hint=kind, amount_minor=17_950)
    )
    assert desk.created[0]["service"] == service and desk.created[0]["expire"] == 15
    assert desk.created[0]["amount"] == 179.5


async def test_create_rejects_other_currencies_without_a_request() -> None:
    http = CountingHttp(FakeParityPay())
    with pytest.raises(ProviderError):
        await provider(http).create(intent(currency="USD", amount_minor=500))
    assert http.requests == 0


async def test_create_after_a_lost_answer_reads_the_invoice_back() -> None:
    desk = FakeParityPay()
    desk.lose_next_create_answer = True
    p = provider(CountingHttp(desk))
    with pytest.raises(ProviderError) as err:
        await p.create(intent())
    assert err.value.retryable
    checkout = await p.create(intent())
    assert checkout.external_id in desk.invoices and len(desk.invoices) == 1
    assert desk.status_calls[-1] == {"order_id": PID}


async def test_duplicate_order_with_another_amount_is_an_error() -> None:
    desk = FakeParityPay()
    desk.add(INV, PID, 500)
    with pytest.raises(ProviderError, match="not unique") as err:
        await provider(CountingHttp(desk)).create(intent())
    assert not err.value.retryable


@pytest.mark.parametrize(
    ("answer", "retryable", "words"),
    [
        (HttpResponse(401, b'{"error":"SecretKey is incorrect"}'), False, "ключ"),
        (HttpResponse(400, b'{"error":"Shop is inactive"}'), False, "ключ"),
        (HttpResponse(400, '{"error":"Параметр amount обязателен"}'.encode()), False, "amount"),
        (HttpResponse(422, b'{"error":"Service \'foo\' is not a valid."}'), False, "422"),
        (HttpResponse(429, b""), True, "недоступна"),
        (HttpResponse(502, b"<html>"), True, "недоступна"),
        (HttpResponse(200, b"not json"), True, "непонятный"),
        (HttpResponse(200, b'{"id":"x"}'), False, "непонятный"),
    ],
)
async def test_create_errors(answer: HttpResponse, retryable: bool, words: str) -> None:
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(lambda _call: answer)).create(intent())
    assert err.value.retryable is retryable and words in err.value.human


# ------------------------------------------------------------------------------------------- status


async def test_fetch_status_single_uses_status_endpoint() -> None:
    desk = FakeParityPay()
    desk.add(INV, PID, 179.0, status="PAID")
    http = CountingHttp(desk)
    [status] = await provider(http).fetch_status([INV])
    assert status.state is PaymentState.PAID and status.payment_id == PID and status.external_id == INV
    assert status.amount == Decimal("179") and status.currency == "RUB"
    assert http.requests == 1 and desk.status_calls == [{"id": INV}]
    assert await provider(CountingHttp(desk)).fetch_status(["11111111-0000-0000-0000-000000000000"]) == []


async def test_fetch_status_batch_is_one_list_request() -> None:
    desk = FakeParityPay()
    ids = [f"00000000-0000-0000-0000-{n:012d}" for n in range(5)]
    for n, i in enumerate(ids):
        desk.add(i, f"order-{n}", 100 + n, status=("NEW", "PAID", "EXPIRED", "ERROR", "REFUNDED")[n])
    http = CountingHttp(desk)
    result = await provider(http).fetch_status([*ids, "unknown-id"])
    assert [s.external_id for s in result] == ids
    assert [s.state for s in result] == [PaymentState.CREATED, PaymentState.PAID, PaymentState.EXPIRED,
                                         PaymentState.FAILED, PaymentState.REFUNDED]  # fmt: skip
    assert result[1].paid_at is not None and result[1].amount == Decimal("101")
    assert http.requests == 1 and desk.list_calls == [{"page": "1", "per_page": "100"}]


async def test_fetch_status_batch_pages_then_single_lookups() -> None:
    desk = FakeParityPay()
    old = "00000000-0000-0000-0000-00000000aaaa"
    desk.add(old, "order-old", 50, status="PAID")
    for n in range(350):
        desk.add(f"10000000-0000-0000-0000-{n:012d}", f"o-{n}", 10)
    newest = f"10000000-0000-0000-0000-{349:012d}"
    http = CountingHttp(desk)
    result = await provider(http).fetch_status([newest, old])
    assert {s.external_id for s in result} == {newest, old}
    assert len(desk.list_calls) == 3 and desk.status_calls == [{"id": old}]


async def test_fetch_status_errors() -> None:
    desk = FakeParityPay()
    desk.fail_with = 503
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(desk)).fetch_status([INV, PID])
    assert err.value.retryable
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(FakeParityPay()), api_key="wrong").fetch_status([INV])
    assert not err.value.retryable


# -------------------------------------------------------------------------------------------- probe


async def test_test_credentials() -> None:
    desk = FakeParityPay()
    probe = await provider(CountingHttp(desk)).test_credentials()
    assert probe.ok and desk.created == []
    assert not (await provider(CountingHttp(desk), api_key="wrong").test_credentials()).ok
    usdt = await provider(CountingHttp(FakeParityPay(currency="USDT"))).test_credentials()
    assert not usdt.ok and "USDT" in usdt.message
    html = await provider(CountingHttp(lambda _c: HttpResponse(200, b"<html></html>"))).test_credentials()
    assert not html.ok and "не ParityPay" in html.message


def test_default_base_url_is_official() -> None:
    assert DEFAULT_BASE_URL == "https://api.paritypay.net"
    assert provider(base_url="")._base == DEFAULT_BASE_URL


# ------------------------------------------------------------------------------ through the real core


async def test_notification_alone_never_credits_and_verify_does(
    make_harness: HarnessFactory, db: CountingDatabase
) -> None:
    desk = FakeParityPay()
    harness = await make_harness(ParityPay, CONFIG, http=CountingHttp(desk))
    result = await harness.core.create_payment(
        user_id=harness.user_id,
        instance_id=harness.instance.id,
        amount_minor=17_900,
        currency="RUB",
        description="Пополнение",
        method_kind="sbp",
    )
    ext = result.checkout.external_id
    assert ext is not None and desk.created[0]["order_id"] == result.payment_id
    # an authentic «PAID» while the desk still says NEW: nothing happens
    assert await harness.send(desk.notification(ext, "PAID")) == 200
    await harness.core.verify(harness.instance.id, [(ext, result.payment_id)])
    assert await harness.status(result.payment_id) == "pending" and harness.credited == []
    desk.set_status(ext, "PAID")
    assert await harness.send(desk.notification(ext, credited="173")) == 200
    await harness.core.verify(harness.instance.id, [(ext, result.payment_id)])
    await harness.core.verify(harness.instance.id, [(ext, result.payment_id)])
    assert await harness.status(result.payment_id) == "paid" and len(harness.credited) == 1
    assert "verify_queued" in await outcomes(db)
    # refund: confirmed through the API inside the webhook
    desk.set_status(ext, "REFUNDED")
    assert await harness.send(desk.notification(ext)) == 200
    assert await harness.status(result.payment_id) == "refunded" and len(harness.refunded) == 1


async def test_late_payment_after_expiry_and_amount_mismatch(make_harness: HarnessFactory) -> None:
    desk = FakeParityPay()
    harness = await make_harness(ParityPay, CONFIG, http=CountingHttp(desk))
    late = await harness.core.create_payment(
        user_id=harness.user_id, instance_id=harness.instance.id, amount_minor=17_900, currency="RUB",
        description="x",
    )  # fmt: skip
    ext = late.checkout.external_id
    assert ext is not None
    desk.set_status(ext, "EXPIRED")
    await harness.core.verify(harness.instance.id, [(ext, late.payment_id)])
    assert await harness.status(late.payment_id) == "expired"
    desk.set_status(ext, "PAID")
    await harness.core.verify(harness.instance.id, [(ext, late.payment_id)])
    assert await harness.status(late.payment_id) == "paid"
    short = await harness.core.create_payment(
        user_id=harness.user_id, instance_id=harness.instance.id, amount_minor=17_900, currency="RUB",
        description="x",
    )  # fmt: skip
    ext2 = short.checkout.external_id
    assert ext2 is not None
    desk.invoices[ext2].amount = 17.9
    desk.set_status(ext2, "PAID")
    await harness.core.verify(harness.instance.id, [(ext2, short.payment_id)])
    assert await harness.status(short.payment_id) == "mismatch" and len(harness.credited) == 1


async def test_end_to_end_over_real_http(make_harness: HarnessFactory) -> None:
    async with FakeParityPay() as desk:
        http = InstanceHttp()
        try:
            harness = await make_harness(ParityPay, {**CONFIG, "base_url": desk.base_url}, http=http)
            result = await harness.core.create_payment(
                user_id=harness.user_id,
                instance_id=harness.instance.id,
                amount_minor=49_900,
                currency="RUB",
                description="Пополнение баланса",
            )
            ext = result.checkout.external_id
            assert ext is not None and desk.invoices[ext].order_id == result.payment_id
            desk.set_status(ext, "PAID")
            assert await harness.send(desk.notification(ext)) == 200
            await harness.core.verify(harness.instance.id, [(ext, result.payment_id)])
            assert await harness.status(result.payment_id) == "paid" and len(harness.credited) == 1
        finally:
            await http.close()
