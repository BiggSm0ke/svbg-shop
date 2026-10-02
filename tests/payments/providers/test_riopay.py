"""RioPay plugin: the shared TestKit plus protocol vectors from ``docs/providers/riopay.md``."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from svbg.payments.providers.riopay import DEFAULT_BASE_URL, RioPay, amount_text, sign
from svbg.payments.registry import InstanceHttp
from svbg.payments.testkit import (
    CountingHttp,
    check_core,
    check_plugin,
    check_poll_budget,
    check_static,
    make_context,
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
from tests.fakes.riopay import API_TOKEN, FAKE_BASE_URL, FakeRioPay
from tests.fakes.riopay import sign as fake_sign
from tests.payments.providers.conftest import HarnessFactory, outcomes

CONFIG = {"api_token": API_TOKEN, "base_url": FAKE_BASE_URL}
PID = "0190f5d2-7b1e-7c3a-9d4e-5f6a7b8c9d0e"
OID = "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5c"
_STATUS = {
    PaymentState.CREATED: "CREATED",
    PaymentState.PROCESSING: "PENDING",
    PaymentState.PAID: "COMPLETED",
    PaymentState.CANCELED: "CANCELED",
    PaymentState.EXPIRED: "EXPIRED",
    PaymentState.FAILED: "FAILED",
    PaymentState.CHARGEBACK: "CHARGEBACK",
    PaymentState.REFUNDED: "REFUND",
}

# spec §6 — computed with OpenSSL by the specification session
SPEC_BODY_1 = (
    b'{"id":"0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5c","status":"COMPLETED","amount":"1000.5","currency":"RUB",'
    b'"paymentType":"SBP","externalId":"order_1234"}'
)
SPEC_SIG_1 = (
    "aff2473b08100cc04639ed55a9c6d1357be167f6005c922d86e905bbb6e6523c"
    "ee77da13711aebe955a58d05652d4b639457808872f03053ef49b8f40f7db690"
)
SPEC_BODY_2 = (
    b'{"id": "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5c", "status": "COMPLETED", "amount": "1000.5", '
    b'"currency": "RUB", "paymentType": "SBP", "externalId": "order_1234"}'
)
SPEC_SIG_2 = (
    "0fd2369f5cbaaeb0454f0f2f745ec733f42df82c7e4c2c6a8fd8f499499c4914"
    "5e9fc081ed782a6c1865055c376a9e83fb00b585b551815bc620e167a904b3b4"
)
SPEC_SIG_EMPTY = (
    "26e6d5f8405eedb9d03229497c041982f2d8191a4e7632a37ed8ef4f735b193a"
    "bce8eaf1c4917c0829fff6100d1903935c13ae9607c0291c0924f842432b325e"
)


def signed(body: bytes, *, token: str = API_TOKEN, kind: str | None = "ORDER_UPDATE") -> WebhookRequest:
    headers = {"Content-Type": "application/json", "X-Signature": fake_sign(token, body)}
    if kind is not None:
        headers["X-Type"] = kind
    return WebhookRequest(body=body, headers=headers)


def order_body(**fields: Any) -> bytes:
    payload: dict[str, Any] = {"id": OID, "status": "COMPLETED", "amount": "179", "currency": "RUB",
                               "externalId": PID}  # fmt: skip
    payload.update(fields)
    return json.dumps({k: v for k, v in payload.items() if v is not None}).encode()


class RioPayVectors:
    """:class:`~svbg.payments.testkit.WebhookVectors` of RioPay: the amount is a string (RioPay's schema),
    ``isTest: true`` marks an order of a test terminal."""

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
        body = json.dumps(
            {
                "id": external_id,
                "status": _STATUS[state],
                "amount": amount,
                "currency": currency,
                "externalId": payment_id,
                "isTest": test,
                "updatedAt": signed_at.isoformat(),
            },
            separators=(",", ":"),
        ).encode()
        return signed(body)

    def tamper(self, req: WebhookRequest) -> WebhookRequest:
        return WebhookRequest(body=req.body.replace(b'"amount":"', b'"amount":"1'), headers=req.headers)

    def strip_auth(self, req: WebhookRequest) -> WebhookRequest:
        return WebhookRequest(
            body=req.body, headers={"Content-Type": "application/json", "X-Type": "ORDER_UPDATE"}
        )


def provider(http: CountingHttp | None = None, **config: Any) -> RioPay:
    p = make_provider(RioPay, {**CONFIG, **config}, http=http)
    assert isinstance(p, RioPay)
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
    check_static(RioPay)
    caps = RioPay.capabilities
    assert caps.webhook_auth is WebhookAuth.SIGNATURE and caps.replay_window_s is None
    assert caps.fetch_status and not caps.batch_status and caps.refund and not caps.recurring
    assert caps.redirect and not caps.in_chat_invoice
    assert set(RioPay.manifest.method_kinds) == {MethodKind.SBP, MethodKind.CARD}
    assert RioPay.manifest.currencies == ("RUB",)
    assert RioPay.manifest.min_minor is None and RioPay.manifest.max_minor is None
    fields = RioPay.manifest.config.fields()
    assert {n for n, f in fields.items() if f.is_secret} == {"api_token"}
    for name, fld in fields.items():
        assert fld.title and fld.description, name
        if fld.required and fld.default is None:
            assert fld.where, f"{name}: «где взять» is missing"


async def test_testkit_plugin_level() -> None:
    await check_plugin(RioPay, CONFIG, RioPayVectors())


async def test_testkit_core_level(make_harness: HarnessFactory, db: CountingDatabase) -> None:
    harness = await make_harness(RioPay, CONFIG, http=CountingHttp(FakeRioPay()))
    await check_core(harness, RioPayVectors())
    assert "bad_signature" in await outcomes(db)


@pytest.mark.parametrize("domain", [True, False], ids=["domain", "no-domain"])
async def test_testkit_poll_budget(make_harness: HarnessFactory, domain: bool) -> None:
    async def pending(call: Any) -> HttpResponse:
        oid = call.url.rsplit("/", 1)[-1]
        body = {"id": oid, "status": "PENDING", "amount": "179", "currency": "RUB"}
        return HttpResponse(200, json.dumps(body).encode())

    harness = await make_harness(RioPay, CONFIG, http=CountingHttp(pending), has_domain=domain)
    used = await check_poll_budget(harness, domain=domain, invoices=3)
    assert used <= (3 if domain else 24) * 3


# ------------------------------------------------------------------------------------ signature vectors


def test_signature_vectors_from_the_spec() -> None:
    assert len(SPEC_BODY_1) == 147
    assert sign(API_TOKEN, SPEC_BODY_1) == SPEC_SIG_1 == fake_sign(API_TOKEN, SPEC_BODY_1)
    assert sign(API_TOKEN, SPEC_BODY_2) == SPEC_SIG_2
    assert sign(API_TOKEN, b"") == SPEC_SIG_EMPTY


async def test_spec_vector_1_is_accepted() -> None:
    req = WebhookRequest(body=SPEC_BODY_1, headers={"X-Type": "ORDER_UPDATE", "X-Signature": SPEC_SIG_1})
    event = await provider().parse_webhook(req)
    assert event.state is PaymentState.PAID and event.external_id == OID
    assert event.payment_id is None  # "order_1234" is not our opaque id
    assert event.amount == Decimal("1000.5") and event.currency == "RUB" and not event.is_test
    assert event.signed_at is None and event.summary["paymentType"] == "SBP"


async def test_spec_vector_2_signature_is_over_raw_bytes() -> None:
    p = provider()
    ok = await p.parse_webhook(
        WebhookRequest(body=SPEC_BODY_2, headers={"X-Type": "ORDER_UPDATE", "X-Signature": SPEC_SIG_2})
    )
    assert ok.amount == Decimal("1000.5")
    with pytest.raises(WebhookRejected) as err:  # same JSON, other bytes: signature of vector 1 → 403
        await p.parse_webhook(
            WebhookRequest(body=SPEC_BODY_2, headers={"X-Type": "ORDER_UPDATE", "X-Signature": SPEC_SIG_1})
        )
    assert err.value.status == 403


async def test_upper_case_signature_is_accepted() -> None:
    req = WebhookRequest(body=SPEC_BODY_1, headers={"X-Signature": "  " + SPEC_SIG_1.upper() + " "})
    assert (await provider().parse_webhook(req)).state is PaymentState.PAID


@pytest.mark.parametrize(
    "headers",
    [{}, {"X-Signature": ""}, {"X-Signature": "abc"}, {"Authorization": SPEC_SIG_1}],
    ids=["none", "empty", "short", "other-header"],
)
async def test_missing_signature_is_403(headers: dict[str, str]) -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(WebhookRequest(body=SPEC_BODY_1, headers=headers))
    assert err.value.status == 403


async def test_other_token_is_rejected() -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(signed(order_body(), token="another-merchant-token"))
    assert err.value.status == 403


async def test_empty_body_with_valid_signature_is_400() -> None:
    req = WebhookRequest(body=b"", headers={"X-Type": "ORDER_UPDATE", "X-Signature": SPEC_SIG_EMPTY})
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(req)
    assert err.value.status == 400


@pytest.mark.parametrize("kind", ["PAYOUT_UPDATE", "REFUND_UPDATE", "SOMETHING_NEW"])
async def test_other_webhook_types_are_200_without_changes(kind: str) -> None:
    with pytest.raises(WebhookIgnored) as err:
        await provider().parse_webhook(signed(order_body(), kind=kind))
    assert err.value.response.status == 200


async def test_missing_x_type_is_treated_as_an_order_update() -> None:
    event = await provider().parse_webhook(signed(order_body(), kind=None))
    assert event.state is PaymentState.PAID and event.payment_id == PID and event.external_id == OID


# --------------------------------------------------------------------------------------- statuses


@pytest.mark.parametrize(
    ("status", "state"),
    [
        ("CREATED", PaymentState.CREATED),
        ("PENDING", PaymentState.CREATED),
        ("COMPLETED", PaymentState.PAID),
        ("FAILED", PaymentState.FAILED),
        ("CANCELED", PaymentState.CANCELED),
        ("EXPIRED", PaymentState.EXPIRED),
        ("BLOCKED", PaymentState.FAILED),
        ("REFUND", PaymentState.REFUNDED),
        ("CHARGEBACK", PaymentState.CHARGEBACK),
        ("completed", PaymentState.PAID),
    ],
)
async def test_status_table(status: str, state: PaymentState) -> None:
    event = await provider().parse_webhook(signed(order_body(status=status)))
    assert event.state is state


async def test_unknown_status_is_ignored_with_200() -> None:
    with pytest.raises(WebhookIgnored) as err:
        await provider().parse_webhook(signed(order_body(status="ON_HOLD")))
    assert err.value.response.status == 200


@pytest.mark.parametrize("raw", ['"179"', '"179.0"', '"179.00"', "179", "179.0"])
async def test_amount_forms(raw: str) -> None:
    body = (
        f'{{"id":"{OID}","status":"COMPLETED","amount":{raw},"currency":"RUB","externalId":"{PID}"}}'.encode()
    )
    event = await provider().parse_webhook(signed(body))
    assert event.amount == Decimal("179")


@pytest.mark.parametrize(
    "fields",
    [
        {"id": None, "externalId": None},
        {"amount": "abc"},
        {"amount": "-1"},
        {"currency": "R$"},
    ],
    ids=["no-ids", "bad-amount", "negative", "bad-currency"],
)
async def test_malformed_orders_are_400(fields: dict[str, Any]) -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(signed(order_body(**fields)))
    assert err.value.status == 400


async def test_paid_at_and_test_flag() -> None:
    event = await provider().parse_webhook(
        signed(order_body(payedAt="2026-10-02T12:34:56Z", isTest=True, amount="1000"))
    )
    assert event.paid_at == datetime(2026, 10, 2, 12, 34, 56, tzinfo=UTC)
    assert event.is_test and event.summary["isTest"] is True


@pytest.mark.parametrize("amount", ["1000", "1001", "1002", "1003"])
async def test_test_terminal_amounts_are_flagged(amount: str) -> None:
    event = await provider().parse_webhook(signed(order_body(amount=amount, isTest=True)))
    assert event.is_test and event.amount == Decimal(amount)


# ------------------------------------------------------------------------------------------- create


async def test_create_is_idempotent_put() -> None:
    desk = FakeRioPay()
    http = CountingHttp(desk)
    ctx_url = make_context("riopay").webhook_url
    checkout = await provider(http, service_id="3").create(intent(return_url="https://t.me/svbg_bot"))
    method, body = desk.created[0]
    assert (
        method == "PUT"
        and http.calls[0].method == "PUT"
        and http.calls[0].url == f"{FAKE_BASE_URL}/v1/orders"
    )
    assert body == {
        "amount": "179",
        "currency": "RUB",
        "externalId": PID,
        "purpose": "Пополнение баланса на 179 ₽",
        "serviceId": 3,
        "successUrl": "https://t.me/svbg_bot",
        "failUrl": "https://t.me/svbg_bot",
        "callbackUrl": ctx_url,
    }
    assert "externalUserId" not in body and "c0ffee" not in json.dumps(body)
    assert http.calls[0].headers["X-Api-Token"] == API_TOKEN and "Authorization" not in http.calls[0].headers
    assert checkout.kind == "url" and checkout.external_id in desk.orders and checkout.expires_at is None
    assert checkout.pay_url == f"https://pay.riopay.fake/{checkout.external_id}"
    # a repeat after a timeout returns the same order, not a second one
    again = await provider(http, service_id="3").create(intent())
    assert again.external_id == checkout.external_id and len(desk.orders) == 1


async def test_create_without_optional_fields() -> None:
    desk = FakeRioPay()
    p = RioPay(
        RioPay.manifest.config.parse(CONFIG),
        make_context("riopay", http=CountingHttp(desk), webhook_url=None),
    )
    await p.create(intent(amount_minor=17_950, description=""))
    body = desk.created[0][1]
    assert body["amount"] == "179.5" and body["purpose"] == "Оплата"
    assert not {"serviceId", "successUrl", "failUrl", "callbackUrl"} & set(body)


async def test_create_return_url_from_config_wins() -> None:
    desk = FakeRioPay()
    await provider(CountingHttp(desk), return_url="https://t.me/owner_bot").create(
        intent(return_url="https://t.me/core_bot")
    )
    assert desk.created[0][1]["successUrl"] == "https://t.me/owner_bot"


def test_amount_text() -> None:
    assert amount_text(Decimal("179.00")) == "179"
    assert amount_text(Decimal("179.50")) == "179.5"
    assert amount_text(Decimal("1000")) == "1000"
    assert amount_text(Decimal("100000.00")) == "100000"
    assert amount_text(Decimal("0.01")) == "0.01"


async def test_create_rejects_other_currencies_without_a_request() -> None:
    http = CountingHttp(FakeRioPay())
    with pytest.raises(ProviderError):
        await provider(http).create(intent(currency="USD", amount_minor=200))
    assert http.requests == 0


@pytest.mark.parametrize(
    ("answer", "retryable", "words"),
    [
        (HttpResponse(400, b'{"statusCode":400,"message":"amount too small"}'), False, "amount too small"),
        (HttpResponse(400, b'{"statusCode":400,"message":["a","b"]}'), False, "a; b"),
        (HttpResponse(401, b'{"statusCode":401,"message":"Unauthorized"}'), False, "токен"),
        (HttpResponse(403, b'{"statusCode":403,"message":"Merchant not found"}'), False, "мерчант"),
        (HttpResponse(404, b'{"statusCode":404,"message":"No active shop"}'), False, "сервис"),
        (HttpResponse(408, b""), True, "недоступен"),
        (HttpResponse(429, b""), True, "недоступен"),
        (HttpResponse(502, b"<html>bad gateway</html>"), True, "недоступен"),
        (HttpResponse(200, b"not json"), True, "непонятный"),
        (HttpResponse(200, b'{"id": "x"}'), False, "непонятный"),
        (
            HttpResponse(200, b'{"id":"x","paymentLink":"https://p","externalId":"other"}'),
            False,
            "непонятный",
        ),
    ],
    ids=["400", "400-list", "401", "403", "404", "408", "429", "502", "not-json", "no-link", "other-order"],
)
async def test_create_errors(answer: HttpResponse, retryable: bool, words: str) -> None:
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(lambda _call: answer)).create(intent())
    assert err.value.retryable is retryable and words in err.value.human
    assert API_TOKEN not in err.value.human


async def test_create_unknown_service_is_404() -> None:
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(FakeRioPay()), service_id="99").create(intent())
    assert err.value.status == 404 and not err.value.retryable


async def test_create_accepts_a_data_envelope() -> None:
    order = {"id": OID, "status": "CREATED", "paymentLink": "https://pay.riopay.fake/x", "externalId": PID}
    answer = HttpResponse(200, json.dumps({"data": order}).encode())
    checkout = await provider(CountingHttp(lambda _c: answer)).create(intent())
    assert checkout.external_id == OID


# ------------------------------------------------------------------------------------------- status


async def test_fetch_status_one_request_per_order() -> None:
    desk = FakeRioPay()
    desk.add(OID, PID, amount="179", status="COMPLETED")
    desk.add("o-2", "foreign", amount="99.90", status="CANCELED")
    desk.add("o-3", None, status="WEIRD")
    desk.orders["o-3"].status = "WEIRD"
    http = CountingHttp(desk)
    statuses = await provider(http).fetch_status([OID, "o-2", "o-3", OID, "missing", ""])
    assert http.requests == 4 and desk.status_calls == [OID, "o-2", "o-3", "missing"]
    assert all(c.method == "GET" and c.headers["X-Api-Token"] == API_TOKEN for c in http.calls)
    by_id = {s.external_id: s for s in statuses}
    assert set(by_id) == {OID, "o-2"}
    assert by_id[OID].state is PaymentState.PAID and by_id[OID].payment_id == PID
    assert by_id[OID].amount == Decimal("179") and by_id[OID].currency == "RUB"
    assert by_id["o-2"].state is PaymentState.CANCELED and by_id["o-2"].amount == Decimal("99.9")
    assert by_id["o-2"].payment_id is None


async def test_fetch_status_errors() -> None:
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(lambda _c: HttpResponse(503, b""))).fetch_status([OID])
    assert err.value.retryable
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(lambda _c: HttpResponse(403, b""))).fetch_status([OID])
    assert not err.value.retryable
    bad = HttpResponse(200, b'{"id": "x", "status": "COMPLETED", "amount": "x"}')
    assert await provider(CountingHttp(lambda _c: bad)).fetch_status([OID]) == []


async def test_fetch_status_reports_test_orders() -> None:
    desk = FakeRioPay()
    desk.add(OID, PID, amount="1000", status="COMPLETED").is_test = True
    [status] = await provider(CountingHttp(desk)).fetch_status([OID])
    assert status.is_test


# ------------------------------------------------------------------------------------------- refund


async def test_refund_full_only_and_once() -> None:
    desk = FakeRioPay()
    desk.add(OID, PID, amount="179", status="COMPLETED")
    http = CountingHttp(desk)
    result = await provider(http).refund(OID, 17_900, "RUB")
    assert result.ok and result.external_id == desk.refunds[0]["id"]
    sent = http.calls[0].json
    assert sent["orderId"] == OID and sent["externalId"] == f"refund-{OID}" and sent["amount"] == "179"
    again = await provider(http).refund(OID, 17_900, "RUB")
    assert not again.ok and "Refund already exists" in again.message and len(desk.refunds) == 1


async def test_refund_errors() -> None:
    desk = FakeRioPay()
    desk.add(OID, PID, amount="179", status="CREATED")
    assert "COMPLETED" in (await provider(CountingHttp(desk)).refund(OID, 17_900, "RUB")).message
    desk.refunds_enabled = False
    off = await provider(CountingHttp(desk)).refund(OID, 17_900, "RUB")
    assert not off.ok and "не включены" in off.message
    down = await provider(CountingHttp(lambda _c: HttpResponse(502, b""))).refund(OID, 17_900, "RUB")
    assert not down.ok and "недоступен" in down.message
    failed = HttpResponse(201, b'{"id": "r-1", "status": "FAILED"}')
    assert not (await provider(CountingHttp(lambda _c: failed)).refund(OID, 17_900, "RUB")).ok


# ------------------------------------------------------------------------------------------- probe


async def test_test_credentials_lists_services() -> None:
    probe = await provider(CountingHttp(FakeRioPay())).test_credentials()
    assert probe.ok and "3 «SBP RUB» (по умолчанию)" in probe.message and "7 «Cards»" in probe.message
    services = probe.details["services"]
    assert services[0]["min"] == "150" and services[0]["max"] == "100000"  # currencyLimits.RUB wins
    assert services[1]["min"] == "500" and services[1]["max"] is None
    assert (await provider(CountingHttp(FakeRioPay()), service_id="7").test_credentials()).ok


async def test_test_credentials_failures() -> None:
    bad = await provider(CountingHttp(FakeRioPay(api_token="other"))).test_credentials()
    assert not bad.ok and "токен" in bad.message
    wrong = await provider(CountingHttp(FakeRioPay()), service_id="99").test_credentials()
    assert not wrong.ok and "99" in wrong.message
    no_merchant = await provider(CountingHttp(lambda _c: HttpResponse(403, b"{}"))).test_credentials()
    assert not no_merchant.ok and "мерчант" in no_merchant.message
    html = await provider(CountingHttp(lambda _c: HttpResponse(200, b"<html></html>"))).test_credentials()
    assert not html.ok
    down = await provider(CountingHttp(lambda _c: HttpResponse(502, b""))).test_credentials()
    assert not down.ok and "недоступен" in down.message
    empty = await provider(CountingHttp(lambda _c: HttpResponse(200, b'{"data": []}'))).test_credentials()
    assert not empty.ok


def test_default_base_url_is_official() -> None:
    assert DEFAULT_BASE_URL == "https://api.riopay.online"


# ------------------------------------------------------------------------------ through the real core


async def test_webhook_credits_once_and_late_payment_after_failed(
    make_harness: HarnessFactory, db: CountingDatabase
) -> None:
    desk = FakeRioPay()
    harness = await make_harness(RioPay, CONFIG, http=CountingHttp(desk))
    result = await harness.core.create_payment(
        user_id=harness.user_id,
        instance_id=harness.instance.id,
        amount_minor=17_900,
        currency="RUB",
        description="Пополнение",
        method_kind="sbp",
    )
    oid = result.checkout.external_id
    assert oid is not None and desk.orders[oid].external_id == result.payment_id
    assert await harness.send(desk.webhook(oid, "PENDING")) == 200
    assert await harness.send(desk.webhook(oid, "FAILED")) == 200
    assert await harness.status(result.payment_id) == "failed"
    desk.set_status(oid, "COMPLETED")
    paid = desk.webhook(oid)
    assert await harness.send(paid) == 200 and await harness.send(paid) == 200
    assert await harness.status(result.payment_id) == "paid" and len(harness.credited) == 1
    assert await harness.send(desk.webhook(oid, "REFUND")) == 200
    assert await harness.status(result.payment_id) == "refunded" and len(harness.refunded) == 1
    assert await outcomes(db) == ["ignored", "applied", "applied", "applied"]  # the replay is deduplicated


async def test_forged_webhook_changes_nothing(make_harness: HarnessFactory) -> None:
    desk = FakeRioPay()
    harness = await make_harness(RioPay, CONFIG, http=CountingHttp(desk))
    result = await harness.core.create_payment(
        user_id=harness.user_id, instance_id=harness.instance.id, amount_minor=17_900, currency="RUB",
        description="x",
    )  # fmt: skip
    oid = result.checkout.external_id
    assert oid is not None
    assert await harness.send(desk.webhook(oid, "COMPLETED", token="guessed")) == 403
    assert await harness.status(result.payment_id) == "pending" and harness.credited == []


async def test_test_terminal_order_is_not_credited_on_a_live_instance(make_harness: HarnessFactory) -> None:
    desk = FakeRioPay()
    desk.test_terminal = True
    harness = await make_harness(RioPay, CONFIG, http=CountingHttp(desk))
    result = await harness.core.create_payment(
        user_id=harness.user_id, instance_id=harness.instance.id, amount_minor=100_000, currency="RUB",
        description="x",
    )  # fmt: skip
    oid = result.checkout.external_id
    assert oid is not None
    desk.set_status(oid, "COMPLETED")
    assert await harness.send(desk.webhook(oid)) == 400
    await harness.core.verify(harness.instance.id, [(oid, result.payment_id)])
    assert await harness.status(result.payment_id) == "pending" and harness.credited == []


async def test_verify_reads_the_order(make_harness: HarnessFactory) -> None:
    desk = FakeRioPay()
    harness = await make_harness(RioPay, CONFIG, http=CountingHttp(desk))
    result = await harness.core.create_payment(
        user_id=harness.user_id, instance_id=harness.instance.id, amount_minor=17_900, currency="RUB",
        description="x",
    )  # fmt: skip
    oid = result.checkout.external_id
    assert oid is not None
    desk.set_status(oid, "COMPLETED", amount="17.9")
    await harness.core.verify(harness.instance.id, [(oid, result.payment_id)])
    assert await harness.status(result.payment_id) == "mismatch" and harness.credited == []


async def test_end_to_end_over_real_http(make_harness: HarnessFactory) -> None:
    async with FakeRioPay() as desk:
        http = InstanceHttp()
        try:
            harness = await make_harness(RioPay, {**CONFIG, "base_url": desk.base_url}, http=http)
            result = await harness.core.create_payment(
                user_id=harness.user_id,
                instance_id=harness.instance.id,
                amount_minor=49_900,
                currency="RUB",
                description="Пополнение баланса",
            )
            oid = result.checkout.external_id
            assert oid is not None and desk.orders[oid].external_id == result.payment_id
            assert desk.created[0][1]["amount"] == "499"
            desk.set_status(oid, "COMPLETED")
            await harness.core.verify(harness.instance.id, [(oid, result.payment_id)])
            assert await harness.status(result.payment_id) == "paid" and len(harness.credited) == 1
        finally:
            await http.close()
