"""Tribute plugin: the shared TestKit plus protocol vectors from ``docs/providers/tribute.md``."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from svbg.payments.providers.tribute import DEFAULT_BASE_URL, Tribute, signatures
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
    PaymentIntent,
    PaymentState,
    ProviderError,
    WebhookAuth,
    WebhookIgnored,
    WebhookRejected,
    WebhookRequest,
)
from tests.dbkit import CountingDatabase
from tests.fakes.tribute import API_KEY, FAKE_BASE_URL, SHOP_ID, Encoding, FakeTribute, sign
from tests.payments.providers.conftest import HarnessFactory, outcomes

CONFIG = {"api_key": API_KEY, "base_url": FAKE_BASE_URL}
PID = "0190f5d2-7b1e-7c3a-9d4e-5f6a7b8c9d0e"
ORDER = "550e8400-e29b-41d4-a716-446655440000"
OTHER_KEY = "trb-api-key-of-another-account-000000"

# Specification §11 (computed with OpenSSL and Python hmac; the documentation publishes no vectors).
SPEC_KEY = "test_api_key"
SPEC_BODY = (
    b'{"name":"shop_order","created_at":"2025-03-20T01:15:58.33246Z","sent_at":"2025-03-20T01:15:58.542279448Z",'
    b'"payload":{"uuid":"550e8400-e29b-41d4-a716-446655440000","shopId":1,"amount":100000,"currency":"rub",'
    b'"fee":8000,"status":"paid","customerId":"user_12345","isRecurrent":false}}'
)
SPEC_HEX = "6fe599ed004d3ac1a4c2ed8fbc11c2c71663e46b548c470374f0e18b72e49504"
SPEC_B64 = "b+WZ7QBNOsGkwu2PvBHCxxZj5GtUjEcDdPDhi3LklQQ="
SPEC_EMPTY_HEX = "69539e2347ee5f163dcbd0f40a706df5819648cd537301a2a4ffffc4556748ec"

_EVENT = {
    PaymentState.CREATED: "shop_order_payment_failed",
    PaymentState.PROCESSING: "shop_order_payment_received",
    PaymentState.PAID: "shop_order",
    PaymentState.EXPIRED: "shop_order_payment_failed",  # Tribute has no expiry event
    PaymentState.CANCELED: "shop_order_payment_failed",
    PaymentState.FAILED: "shop_order_payment_failed",
    PaymentState.CHARGEBACK: "shop_order_refunded",  # a bank chargeback arrives as a completed refund
    PaymentState.REFUNDED: "shop_order_refunded",
}


def signed(
    name: str, payload: dict[str, Any], *, key: str = API_KEY, encoding: Encoding = "hex"
) -> WebhookRequest:
    body = json.dumps(
        {
            "name": name,
            "created_at": "2026-10-02T10:00:00Z",
            "sent_at": "2026-10-02T10:00:01Z",
            "payload": payload,
        }
    ).encode()
    return WebhookRequest(body=body, headers={"trbt-signature": sign(key, body, encoding)})


def paid_payload(**extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "uuid": ORDER,
        "shopId": SHOP_ID,
        "amount": 17_900,
        "currency": "rub",
        "fee": 1790,
        "status": "paid",
        "customerId": PID,
        "isRecurrent": False,
    }
    payload.update(extra)
    return payload


class TributeVectors:
    """:class:`~svbg.payments.testkit.WebhookVectors` of Tribute. Amounts go as integers in minor units.
    Tribute has no test mode: a «test» webhook is one signed by another account's key and cannot
    authenticate."""

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
        name = _EVENT[state]
        payload: dict[str, Any] = {
            "uuid": external_id,
            "shopId": SHOP_ID,
            "amount": int(Decimal(amount) * 100),
            "currency": currency.lower(),
            "customerId": payment_id,
        }
        if name == "shop_order":
            payload |= {"status": "paid", "fee": 0, "isRecurrent": False}
        if name == "shop_order_refunded":
            payload |= {"status": "completed", "transactionId": 1, "refundedAt": signed_at.isoformat()}
        body = json.dumps(
            {
                "name": name,
                "created_at": signed_at.isoformat(),
                "sent_at": signed_at.isoformat(),
                "payload": payload,
            }
        ).encode()
        return WebhookRequest(
            body=body, headers={"trbt-signature": sign(OTHER_KEY if test else API_KEY, body)}
        )

    def tamper(self, req: WebhookRequest) -> WebhookRequest:
        return WebhookRequest(body=req.body.replace(b'"amount": ', b'"amount":  '), headers=dict(req.headers))

    def strip_auth(self, req: WebhookRequest) -> WebhookRequest:
        return WebhookRequest(body=req.body, headers={"Content-Type": "application/json"})


def provider(http: CountingHttp | None = None, **config: Any) -> Tribute:
    p = make_provider(Tribute, {**CONFIG, **config}, http=http)
    assert isinstance(p, Tribute)
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
    check_static(Tribute)
    caps = Tribute.capabilities
    assert caps.webhook_auth is WebhookAuth.SIGNATURE and caps.replay_window_s is None
    assert caps.fetch_status and not caps.batch_status and caps.refund and not caps.recurring
    assert Tribute.manifest.currencies == ("RUB", "EUR", "USD")
    for name, fld in Tribute.manifest.config.fields().items():
        assert fld.title and fld.description and fld.where, name
    assert Tribute.manifest.config.fields()["api_key"].is_secret
    assert Tribute.manifest.config.fields()["base_url"].default == DEFAULT_BASE_URL


async def test_testkit_plugin_level() -> None:
    await check_plugin(Tribute, CONFIG, TributeVectors())


async def test_testkit_core_level(make_harness: HarnessFactory, db: CountingDatabase) -> None:
    """Everything passes except the expiry vector: Tribute never notifies about an expired order
    (``shop_order_payment_failed`` is not terminal — specification §6)."""
    harness = await make_harness(Tribute, CONFIG, http=CountingHttp(FakeTribute()))
    with pytest.raises(KitFailure) as err:
        await check_core(harness, TributeVectors())
    assert err.value.failures == ["expired webhook did not expire the payment"]
    assert len(harness.credited) == 3 and len(harness.refunded) == 1
    assert "bad_signature" in await outcomes(db)


@pytest.mark.parametrize("domain", [True, False], ids=["domain", "no-domain"])
async def test_testkit_poll_budget(make_harness: HarnessFactory, domain: bool) -> None:
    async def pending(call: Any) -> HttpResponse:
        ext = call.url.rsplit("/", 1)[-1]
        body = {"uuid": ext, "status": "pending", "amount": 17_900, "currency": "rub"}
        return HttpResponse(200, json.dumps(body).encode())

    harness = await make_harness(Tribute, CONFIG, http=CountingHttp(pending), has_domain=domain)
    used = await check_poll_budget(harness, domain=domain, invoices=3)
    assert used <= (3 if domain else 24) * 3


# ------------------------------------------------------------------------------- signature vectors


def test_spec_vector_both_encodings() -> None:
    assert len(SPEC_BODY) == 281
    assert signatures(SPEC_KEY, SPEC_BODY) == (SPEC_HEX, SPEC_B64)
    assert signatures(SPEC_KEY, b"")[0] == SPEC_EMPTY_HEX
    assert sign(SPEC_KEY, SPEC_BODY) == SPEC_HEX and sign(SPEC_KEY, SPEC_BODY, "base64") == SPEC_B64


@pytest.mark.parametrize("header", [SPEC_HEX, SPEC_HEX.upper(), f"  {SPEC_HEX} ", SPEC_B64])
async def test_spec_vector_is_accepted_as_hex_or_base64(header: str) -> None:
    plugin = provider(api_key=SPEC_KEY)
    event = await plugin.parse_webhook(WebhookRequest(body=SPEC_BODY, headers={"trbt-signature": header}))
    assert event.state is PaymentState.PAID and event.external_id == ORDER
    assert event.payment_id is None  # "user_12345" is not one of our payment ids
    assert event.amount == Decimal("1000") and event.currency == "RUB" and not event.is_test
    assert event.signed_at == datetime(2025, 3, 20, 1, 15, 58, 542279, tzinfo=UTC)  # 9 digits truncated
    assert event.paid_at == datetime(2025, 3, 20, 1, 15, 58, 332460, tzinfo=UTC)
    assert event.summary["event"] == "shop_order"


@pytest.mark.parametrize(
    "header",
    [
        "",
        SPEC_HEX[:-1],
        SPEC_B64.rstrip("="),
        SPEC_B64.lower(),
        sign("another_key", SPEC_BODY),
        sign("another_key", SPEC_BODY, "base64"),
        SPEC_EMPTY_HEX,
    ],
)
async def test_wrong_or_missing_signature_is_401(header: str) -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider(api_key=SPEC_KEY).parse_webhook(
            WebhookRequest(body=SPEC_BODY, headers={"trbt-signature": header} if header else {})
        )
    assert err.value.status == 401


async def test_signature_is_over_the_raw_bytes() -> None:
    """Every single-byte change, and a re-serialization of the same JSON, is rejected."""
    plugin = provider(api_key=SPEC_KEY)
    for i in range(0, len(SPEC_BODY), 7):
        body = SPEC_BODY[:i] + bytes([SPEC_BODY[i] ^ 0x01]) + SPEC_BODY[i + 1 :]
        with pytest.raises(WebhookRejected) as err:
            await plugin.parse_webhook(WebhookRequest(body=body, headers={"trbt-signature": SPEC_HEX}))
        assert err.value.status == 401
    pretty = json.dumps(json.loads(SPEC_BODY)).encode()
    with pytest.raises(WebhookRejected):
        await plugin.parse_webhook(WebhookRequest(body=pretty, headers={"trbt-signature": SPEC_HEX}))


async def test_signed_empty_body_is_400() -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider(api_key=SPEC_KEY).parse_webhook(
            WebhookRequest(body=b"", headers={"trbt-signature": SPEC_EMPTY_HEX})
        )
    assert err.value.status == 400


# ----------------------------------------------------------------------------------- event vectors


async def test_paid_order_correlates_by_uuid_and_customer_id() -> None:
    event = await provider().parse_webhook(signed("shop_order", paid_payload(), encoding="base64"))
    assert event.state is PaymentState.PAID and event.external_id == ORDER and event.payment_id == PID
    assert event.amount == Decimal("179") and event.currency == "RUB"


async def test_first_period_amount_is_what_was_charged() -> None:
    event = await provider().parse_webhook(
        signed("shop_order", paid_payload(amount=50_000, firstPeriodAmount=17_900, period="monthly"))
    )
    assert event.amount == Decimal("179")


async def test_trial_activation_carries_no_money() -> None:
    with pytest.raises(WebhookIgnored) as err:
        await provider().parse_webhook(signed("shop_order", paid_payload(isTrial=True)))
    assert json.loads(err.value.response.body) == {"status": "ok"}


async def test_refund_initiated_then_completed() -> None:
    plugin = provider()
    refund = {"uuid": ORDER, "transactionId": 1001, "amount": 17_900, "currency": "rub", "customerId": PID}
    with pytest.raises(WebhookIgnored):
        await plugin.parse_webhook(signed("shop_order_refunded", {**refund, "status": "initiated"}))
    event = await plugin.parse_webhook(
        signed("shop_order_refunded", {**refund, "status": "completed", "refundedAt": "2026-10-02T11:00:00Z"})
    )
    assert event.state is PaymentState.REFUNDED and event.external_id == ORDER and event.payment_id == PID
    assert event.summary["transaction_id"] == 1001 and event.summary["refund_status"] == "completed"


@pytest.mark.parametrize("name", ["shop_order_payment_received", "shop_order_prepaid"])
async def test_money_on_the_way_is_processing(name: str) -> None:
    event = await provider().parse_webhook(signed(name, {"uuid": ORDER, "amount": 17_900, "currency": "rub"}))
    assert event.state is PaymentState.PROCESSING and event.payment_id is None


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("shop_order_payment_failed", {"uuid": ORDER, "errorCode": "payment_declined", "amount": 17_900}),
        ("shop_order_charge_success", {"uuid": ORDER, "amount": 17_900, "period": "monthly"}),
        ("shop_order_charge_failed", {"uuid": ORDER, "chargeRetries": 3}),
        ("shop_order_cancelled", {"uuid": ORDER, "cancelReason": "charge_failed"}),
        ("new_donation", {"donation_request_id": 1, "amount": 500, "currency": "rub", "anonymously": True}),
        ("new_subscription", {"subscription_id": 1, "period": "weekly", "telegram_user_id": 1}),
        ("new_digital_product", {"product_id": 1, "purchase_id": 2, "amount": 100, "currency": "usd"}),
        ("something_new", {}),
    ],
)
async def test_events_without_an_invoice_of_ours_are_acknowledged(name: str, payload: dict[str, Any]) -> None:
    with pytest.raises(WebhookIgnored) as err:
        await provider().parse_webhook(signed(name, payload))
    assert err.value.response.status == 200 and json.loads(err.value.response.body) == {"status": "ok"}


@pytest.mark.parametrize(
    "payload",
    [
        paid_payload(amount="179.00"),
        paid_payload(amount=179.5),
        paid_payload(amount=-1),
        paid_payload(currency="btc"),
        paid_payload(currency=None),
        {"customerId": "not-ours", "amount": 17_900, "currency": "rub"},
    ],
)
async def test_malformed_authentic_bodies_are_400(payload: dict[str, Any]) -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(signed("shop_order", payload))
    assert err.value.status == 400


@pytest.mark.parametrize("body", [b"[]", b'{"name":"shop_order"}', b'{"payload":{}}', b"not json"])
async def test_non_event_bodies_are_400(body: bytes) -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(
            WebhookRequest(body=body, headers={"trbt-signature": sign(API_KEY, body)})
        )
    assert err.value.status == 400


def test_ack_is_status_ok() -> None:
    resp = provider().ack(None)
    assert resp.status == 200 and json.loads(resp.body) == {"status": "ok"}


# ------------------------------------------------------------------------------------------ create


async def test_create_request_shape_and_privacy() -> None:
    desk = FakeTribute()
    http = CountingHttp(desk)
    checkout = await provider(http).create(intent(return_url="https://t.me/demo_bot"))
    call = http.calls[0]
    assert call.method == "POST" and call.url == f"{FAKE_BASE_URL}/shop/orders"
    assert call.headers["Api-Key"] == API_KEY
    assert call.json == {
        "amount": 17_900,
        "currency": "rub",
        "title": "Пополнение баланса на 179 ₽",
        "description": "Пополнение баланса на 179 ₽",
        "customerId": PID,
        "period": "onetime",
        "successUrl": "https://t.me/demo_bot",
        "failUrl": "https://t.me/demo_bot",
    }
    assert "c0ffee" not in json.dumps(call.json)  # only the opaque payment id leaves the bot
    assert checkout.kind == "url" and checkout.external_id in desk.orders
    assert checkout.pay_url == f"https://t.me/tribute/app?startapp=s{checkout.external_id}"


async def test_create_web_link_shop_id_and_long_texts() -> None:
    desk = FakeTribute()
    http = CountingHttp(desk)
    checkout = await provider(http, pay_link="web", shop_id=str(SHOP_ID)).create(
        intent(description="Я" * 400, return_url="http://localhost/back", currency="EUR", amount_minor=100)
    )
    body = http.calls[0].json
    assert body["shopId"] == SHOP_ID and body["currency"] == "eur" and body["amount"] == 100
    assert len(body["title"]) == 100 and len(body["description"]) == 300 and body["title"].endswith("…")
    assert "successUrl" not in body  # only https:// is accepted by Tribute
    assert checkout.pay_url == f"https://web.tribute.fake/shop/pay/{checkout.external_id}"


async def test_titles_are_measured_in_utf16_units() -> None:
    http = CountingHttp(FakeTribute())
    await provider(http).create(intent(description="😀" * 80))
    title = http.calls[0].json["title"]
    assert len(title.encode("utf-16-le")) // 2 <= 100


@pytest.mark.parametrize(
    ("status", "body", "retryable", "words"),
    [
        (401, {"error": "error_unauthorized"}, False, "API-ключ"),
        (400, {"error": "error_amount_too_small"}, False, "меньше минимальной"),
        (400, {"error": "error_shop_inactive"}, False, "не активен"),
        (403, {"error": "error_forbidden"}, False, "другому аккаунту"),
        (404, {"error": "error_shop_not_found"}, False, "error_shop_not_found"),
        (429, {"error": "error_rate_limited"}, True, "временно недоступен"),
        (502, {}, True, "временно недоступен"),
    ],
)
async def test_create_errors(status: int, body: dict[str, Any], retryable: bool, words: str) -> None:
    http = CountingHttp(lambda _call: HttpResponse(status, json.dumps(body).encode()))
    with pytest.raises(ProviderError) as err:
        await provider(http).create(intent())
    assert err.value.retryable is retryable and words in err.value.human
    assert API_KEY not in err.value.human and http.requests == 1  # never retried: no idempotency key


async def test_create_with_a_bad_answer_or_currency() -> None:
    http = CountingHttp(lambda _call: HttpResponse(200, b'{"uuid": "x"}'))
    with pytest.raises(ProviderError):
        await provider(http).create(intent())
    with pytest.raises(ProviderError, match="RUB, EUR и USD"):
        await provider(CountingHttp(FakeTribute())).create(intent(currency="GBP"))


# ----------------------------------------------------------------------------- status, refund, probe


async def test_fetch_status() -> None:
    desk = FakeTribute()
    desk.add("o-pending", PID)
    desk.add("o-paid", PID, status="paid").first_period_amount = 9_900
    desk.add("o-failed", PID, status="failed")
    desk.add("o-foreign", PID).shop_id = 99
    statuses = await provider(CountingHttp(desk)).fetch_status(
        ["o-pending", "o-paid", "o-failed", "o-foreign", "o-unknown", "o-paid"]
    )
    by_id = {s.external_id: s for s in statuses}
    assert set(by_id) == {"o-pending", "o-paid", "o-failed"}
    assert by_id["o-pending"].state is PaymentState.CREATED
    assert by_id["o-paid"].state is PaymentState.PAID and by_id["o-paid"].amount == Decimal("99")
    assert by_id["o-paid"].currency == "RUB" and by_id["o-paid"].paid_at is not None
    assert by_id["o-failed"].state is PaymentState.FAILED


async def test_fetch_status_errors_and_quoting() -> None:
    http = CountingHttp(lambda _call: HttpResponse(503, b""))
    with pytest.raises(ProviderError) as err:
        await provider(http).fetch_status(["a/b?c"])
    assert err.value.retryable and http.calls[0].url == f"{FAKE_BASE_URL}/shop/orders/a%2Fb%3Fc"


async def test_refund_whole_order() -> None:
    desk = FakeTribute()
    order = desk.add("o-1", PID, status="paid")
    result = await provider(CountingHttp(desk)).refund("o-1", 17_900, "RUB")
    assert result.ok and result.external_id == str(order.transactions[0]["id"])
    assert desk.refunds == [("o-1", order.transactions[0]["id"])]
    again = await provider(CountingHttp(desk)).refund("o-1", 17_900, "RUB")
    assert not again.ok and desk.refunds == [("o-1", order.transactions[0]["id"])]


@pytest.mark.parametrize(("amount", "currency"), [(10_000, "RUB"), (17_900, "EUR")])
async def test_partial_or_foreign_refund_is_refused(amount: int, currency: str) -> None:
    desk = FakeTribute()
    desk.add("o-1", PID, status="paid")
    result = await provider(CountingHttp(desk)).refund("o-1", amount, currency)
    assert not result.ok and "частичный" in result.message and desk.refunds == []


async def test_refund_of_unpaid_order_is_refused() -> None:
    desk = FakeTribute()
    desk.add("o-1", PID)
    result = await provider(CountingHttp(desk)).refund("o-1", 17_900, "RUB")
    assert not result.ok and desk.refunds == []


async def test_test_credentials() -> None:
    desk = FakeTribute()
    probe = await provider(CountingHttp(desk)).test_credentials()
    assert probe.ok and "Demo Shop" in probe.message and probe.details["shops"] == [SHOP_ID]
    assert not (await provider(CountingHttp(desk), shop_id="8").test_credentials()).ok
    assert not (await provider(CountingHttp(desk), api_key="wrong-key").test_credentials()).ok
    desk.shops[0]["callbackUrl"] = None
    assert "Webhook URL" in (await provider(CountingHttp(desk)).test_credentials()).message
    desk.shops = []
    empty = await provider(CountingHttp(desk)).test_credentials()
    assert not empty.ok and "нет магазина" in empty.message


# ------------------------------------------------------------------------------------ through the core


async def test_end_to_end_through_the_core(make_harness: HarnessFactory, db: CountingDatabase) -> None:
    desk = FakeTribute()
    harness = await make_harness(Tribute, CONFIG, http=CountingHttp(desk))
    result = await harness.core.create_payment(
        user_id=harness.user_id,
        instance_id=harness.instance.id,
        amount_minor=17_900,
        currency="RUB",
        description="Пополнение баланса",
    )
    ext = result.checkout.external_id
    assert ext is not None and desk.orders[ext].customer_id == result.payment_id
    # a declined card does not end the order
    assert await harness.send(desk.order_webhook(ext, "shop_order_payment_failed", errorCode="x")) == 200
    assert await harness.status(result.payment_id) == "pending"
    desk.pay(ext)
    assert await harness.send(desk.order_webhook(ext)) == 200
    assert await harness.status(result.payment_id) == "paid" and len(harness.credited) == 1
    # the reconciler's status check after the webhook is a no-op
    await harness.core.verify(harness.instance.id, [(ext, result.payment_id)])
    assert len(harness.credited) == 1
    refund = {"status": "completed", "transactionId": 1001, "refundedAt": "2026-10-02T11:00:00Z"}
    assert await harness.send(desk.order_webhook(ext, "shop_order_refunded", **refund)) == 200
    assert await harness.status(result.payment_id) == "refunded" and len(harness.refunded) == 1
    assert "applied" in await outcomes(db)


async def test_lost_webhook_is_found_by_the_status_check(make_harness: HarnessFactory) -> None:
    desk = FakeTribute()
    harness = await make_harness(Tribute, CONFIG, http=CountingHttp(desk))
    result = await harness.core.create_payment(
        user_id=harness.user_id,
        instance_id=harness.instance.id,
        amount_minor=17_900,
        currency="RUB",
        description="x",
    )
    ext = result.checkout.external_id
    assert ext is not None
    desk.pay(ext)
    await harness.core.verify(harness.instance.id, [(ext, result.payment_id)])
    assert await harness.status(result.payment_id) == "paid"
