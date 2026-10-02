"""Platega plugin: the shared TestKit plus protocol vectors from ``docs/providers/platega.md``."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from svbg.payments.providers.platega import DEFAULT_BASE_URL, Platega, parse_expires_in
from svbg.payments.registry import InstanceHttp
from svbg.payments.testkit import (
    CountingHttp,
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
from tests.fakes.platega import API_SECRET, FAKE_BASE_URL, MERCHANT_ID, FakePlatega
from tests.payments.providers.conftest import HarnessFactory, outcomes

CONFIG = {"merchant_id": MERCHANT_ID, "api_secret": API_SECRET, "base_url": FAKE_BASE_URL}
PID = "0190f5d2-7b1e-7c3a-9d4e-5f6a7b8c9d0e"
TX = "3fa85f64-5717-4562-b3fc-2c963f66afa6"
OTHER_SECRET = "pl-secret-of-another-merchant-000000"
_STATUS = {
    PaymentState.CREATED: "PENDING",
    PaymentState.PAID: "CONFIRMED",
    PaymentState.CANCELED: "CANCELED",
    PaymentState.EXPIRED: "CANCELED",
    PaymentState.FAILED: "CANCELED",
    PaymentState.CHARGEBACK: "CHARGEBACKED",
    PaymentState.REFUNDED: "CHARGEBACKED",
}


def callback(
    payload: dict[str, Any], *, secret: str = API_SECRET, merchant: str = MERCHANT_ID
) -> WebhookRequest:
    return WebhookRequest(
        body=json.dumps(payload).encode(), headers={"X-MerchantId": merchant, "X-Secret": secret}
    )


class PlategaVectors:
    """:class:`~svbg.payments.testkit.WebhookVectors` of Platega. The amount goes as a JSON number (Platega's
    schema), the textual forms ``"179.00"`` are kept verbatim as numbers. Platega has no test flag: a «test»
    callback is one from another merchant (a separate test shop) and cannot authenticate."""

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
        body = (
            f'{{"id": "{external_id}", "amount": {amount}, "currency": "{currency}", '
            f'"status": "{_STATUS[state]}", "paymentMethod": 2, "payload": "{payment_id}"}}'
        ).encode()
        return WebhookRequest(
            body=body,
            headers={"X-MerchantId": MERCHANT_ID, "X-Secret": OTHER_SECRET if test else API_SECRET},
        )

    def tamper(self, req: WebhookRequest) -> WebhookRequest:
        return WebhookRequest(body=req.body, headers={**dict(req.headers), "x-secret": API_SECRET + "x"})

    def strip_auth(self, req: WebhookRequest) -> WebhookRequest:
        return WebhookRequest(body=req.body, headers={"x-merchantid": MERCHANT_ID})


def provider(http: CountingHttp | None = None, **config: Any) -> Platega:
    p = make_provider(Platega, {**CONFIG, **config}, http=http)
    assert isinstance(p, Platega)
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
    check_static(Platega)
    caps = Platega.capabilities
    assert caps.webhook_auth is WebhookAuth.SECRET_HEADER and caps.webhook_auth.is_weak
    assert caps.fetch_status and not caps.batch_status and caps.replay_window_s is None
    assert set(Platega.manifest.method_kinds) == {
        MethodKind.SBP,
        MethodKind.CARD,
        MethodKind.INTL_CARD,
        MethodKind.CRYPTO,
    }
    for name, fld in Platega.manifest.config.fields().items():
        assert fld.title and fld.description, name
        if fld.required and fld.default is None:
            assert fld.where, f"{name}: «где взять» is missing"


async def test_testkit_plugin_level() -> None:
    await check_plugin(Platega, CONFIG, PlategaVectors())


async def test_testkit_core_level_weak_scheme(make_harness: HarnessFactory, db: CountingDatabase) -> None:
    """Weak scheme: the kit checks bad credentials and the test flag; nothing is credited from a callback."""
    harness = await make_harness(Platega, CONFIG, http=CountingHttp(FakePlatega()))
    await check_core(harness, PlategaVectors())
    assert harness.credited == []
    assert "bad_signature" in await outcomes(db)


@pytest.mark.parametrize("domain", [True, False], ids=["domain", "no-domain"])
async def test_testkit_poll_budget(make_harness: HarnessFactory, domain: bool) -> None:
    async def pending(call: Any) -> HttpResponse:
        tx = call.url.rsplit("/", 1)[-1]
        body = {"id": tx, "status": "PENDING", "paymentDetails": {"amount": 179, "currency": "RUB"}}
        return HttpResponse(200, json.dumps(body).encode())

    harness = await make_harness(Platega, CONFIG, http=CountingHttp(pending), has_domain=domain)
    used = await check_poll_budget(harness, domain=domain, invoices=3)
    assert used <= (3 if domain else 24) * 3


# ---------------------------------------------------------------------------------- callback vectors


async def test_paid_callback_fields() -> None:
    event = await provider().parse_webhook(
        callback(
            {
                "id": TX,
                "amount": 179.5,
                "currency": "rub",
                "status": "CONFIRMED",
                "paymentMethod": 2,
                "payload": PID,
            }
        )
    )
    assert event.state is PaymentState.PAID and event.external_id == TX and event.payment_id == PID
    assert event.amount == Decimal("179.5") and event.currency == "RUB" and not event.is_test
    assert event.signed_at is None and event.summary["paymentMethod"] == "2"


@pytest.mark.parametrize("raw", ["179", "179.0", "179.00", '"179.00"', '"179"'])
async def test_amount_as_number_and_as_string(raw: str) -> None:
    body = f'{{"id": "{TX}", "amount": {raw}, "currency": "RUB", "status": "CONFIRMED"}}'.encode()
    event = await provider().parse_webhook(
        WebhookRequest(body=body, headers={"X-MerchantId": MERCHANT_ID, "X-Secret": API_SECRET})
    )
    assert event.amount == Decimal("179")


async def test_float_noise_never_reaches_the_amount() -> None:
    body = f'{{"id": "{TX}", "amount": 0.1, "currency": "RUB", "status": "CONFIRMED"}}'.encode()
    event = await provider().parse_webhook(
        WebhookRequest(body=body, headers={"X-MerchantId": MERCHANT_ID, "X-Secret": API_SECRET})
    )
    assert event.amount == Decimal("0.1") and str(event.amount) == "0.1"


@pytest.mark.parametrize(
    ("status", "state"),
    [
        ("PENDING", PaymentState.CREATED),
        ("CONFIRMED", PaymentState.PAID),
        ("confirmed", PaymentState.PAID),
        ("CANCELED", PaymentState.CANCELED),
        ("CHARGEBACKED", PaymentState.CHARGEBACK),
    ],
)
async def test_statuses(status: str, state: PaymentState) -> None:
    event = await provider().parse_webhook(
        callback({"id": TX, "amount": 1, "currency": "RUB", "status": status})
    )
    assert event.state is state


@pytest.mark.parametrize(
    "headers",
    [
        {"X-MerchantId": MERCHANT_ID, "X-Secret": "wrong"},
        {"X-MerchantId": "00000000-0000-0000-0000-000000000000", "X-Secret": API_SECRET},
        {"X-Secret": API_SECRET},
        {"X-MerchantId": MERCHANT_ID},
        {},
    ],
    ids=["bad-secret", "other-merchant", "no-merchant", "no-secret", "nothing"],
)
async def test_bad_credentials_are_401(headers: dict[str, str]) -> None:
    body = json.dumps({"id": TX, "amount": 179, "currency": "RUB", "status": "CONFIRMED"}).encode()
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(WebhookRequest(body=body, headers=headers))
    assert err.value.status == 401


async def test_merchant_id_case_does_not_matter() -> None:
    event = await provider().parse_webhook(
        callback(
            {"id": TX, "amount": 1, "currency": "RUB", "status": "CONFIRMED"}, merchant=MERCHANT_ID.upper()
        )
    )
    assert event.state is PaymentState.PAID


async def test_unknown_status_is_ignored_with_200() -> None:
    with pytest.raises(WebhookIgnored) as err:
        await provider().parse_webhook(callback({"id": TX, "status": "SOMETHING"}))
    assert err.value.response.status == 200


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "CONFIRMED", "amount": 1},
        {"id": TX, "status": "CONFIRMED", "amount": "abc"},
        {"id": TX, "status": "CONFIRMED", "amount": -1},
        {"id": TX, "status": "CONFIRMED", "amount": 1, "currency": "R$"},
    ],
    ids=["no-id", "bad-amount", "negative", "bad-currency"],
)
async def test_malformed_callbacks_are_400(payload: dict[str, Any]) -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(callback(payload))
    assert err.value.status == 400


async def test_not_json_is_400() -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(
            WebhookRequest(body=b"<xml/>", headers={"X-MerchantId": MERCHANT_ID, "X-Secret": API_SECRET})
        )
    assert err.value.status == 400


async def test_foreign_payload_is_not_taken_for_a_payment_id() -> None:
    event = await provider().parse_webhook(
        callback({"id": TX, "amount": 1, "currency": "RUB", "status": "CONFIRMED", "payload": "user 12345"})
    )
    assert event.payment_id is None and event.external_id == TX


# ------------------------------------------------------------------------------------------- create


async def test_create_without_method_uses_v2() -> None:
    desk = FakePlatega()
    http = CountingHttp(desk)
    checkout = await provider(http).create(intent(return_url="https://t.me/svbg_bot"))
    path, body = desk.created[0]
    assert path == "/v2/transaction/process" and "paymentMethod" not in body
    assert body["paymentDetails"] == {"amount": 179, "currency": "RUB"}
    assert body["payload"] == PID and body["return"] == body["failedUrl"] == "https://t.me/svbg_bot"
    assert "metadata" not in body and "c0ffee" not in json.dumps(body)
    assert checkout.kind == "url" and checkout.external_id in desk.transactions
    assert checkout.pay_url is not None and checkout.pay_url.startswith("https://pay.platega.fake/")
    assert checkout.expires_at is not None
    left = checkout.expires_at - datetime.now(UTC)
    assert timedelta(minutes=14) < left <= timedelta(minutes=15)
    call = http.calls[0]
    assert call.headers["X-MerchantId"] == MERCHANT_ID and call.headers["X-Secret"] == API_SECRET


@pytest.mark.parametrize(
    ("kind", "code"),
    [(MethodKind.SBP, 2), (MethodKind.CARD, 11), (MethodKind.INTL_CARD, 12), (MethodKind.CRYPTO, 13)],
)
async def test_create_with_method_uses_v1(kind: MethodKind, code: int) -> None:
    desk = FakePlatega()
    checkout = await provider(CountingHttp(desk)).create(intent(method_hint=kind, amount_minor=17_950))
    path, body = desk.created[0]
    assert path == "/transaction/process" and body["paymentMethod"] == code
    assert body["paymentDetails"]["amount"] == 179.5
    assert checkout.pay_url is not None and "platega.fake" in checkout.pay_url


async def test_create_return_url_from_config_and_fallback() -> None:
    desk = FakePlatega()
    await provider(CountingHttp(desk), return_url="https://t.me/owner_bot").create(
        intent(return_url="https://t.me/core_bot")
    )
    await provider(CountingHttp(desk)).create(intent(description=""))
    assert desk.created[0][1]["return"] == "https://t.me/owner_bot"
    assert desk.created[1][1]["return"] == "https://t.me" and desk.created[1][1]["description"] == "Оплата"


async def test_create_rejects_other_currencies_without_a_request() -> None:
    http = CountingHttp(FakePlatega())
    with pytest.raises(ProviderError):
        await provider(http).create(intent(currency="USD", amount_minor=200))
    assert http.requests == 0


@pytest.mark.parametrize(
    ("answer", "retryable", "words"),
    [
        (HttpResponse(401, b'{"message":"Unauthorized"}'), False, "MerchantId"),
        (HttpResponse(400, b'{"message":"amount too small"}'), False, "amount too small"),
        (HttpResponse(502, b"<html>bad gateway</html>"), True, "недоступна"),
        (HttpResponse(429, b""), True, "недоступна"),
        (HttpResponse(200, b"not json"), True, "непонятный"),
        (HttpResponse(200, b'{"transactionId": "x"}'), False, "непонятный"),
    ],
    ids=["401", "400", "502", "429", "not-json", "no-url"],
)
async def test_create_errors(answer: HttpResponse, retryable: bool, words: str) -> None:
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(lambda _call: answer)).create(intent())
    assert err.value.retryable is retryable and words in err.value.human
    assert API_SECRET not in err.value.human


def test_parse_expires_in() -> None:
    assert parse_expires_in("00:15:00") == timedelta(minutes=15)
    assert parse_expires_in("1.02:00:00") == timedelta(days=1, hours=2)
    assert parse_expires_in("00:00:00") is None
    assert parse_expires_in("15 min") is None and parse_expires_in(900) is None


# ------------------------------------------------------------------------------------------- status


async def test_fetch_status_one_request_per_transaction() -> None:
    desk = FakePlatega()
    desk.add(TX, PID, amount=179, status="CONFIRMED")
    desk.add("tx-2", "foreign", amount=99.9, status="CANCELED")
    desk.add("tx-3", None, amount=10, status="WEIRD")
    http = CountingHttp(desk)
    statuses = await provider(http).fetch_status([TX, "tx-2", "tx-3", TX, "missing", ""])
    assert http.requests == 4 and desk.status_calls == [TX, "tx-2", "tx-3", "missing"]
    by_id = {s.external_id: s for s in statuses}
    assert set(by_id) == {TX, "tx-2"}
    assert by_id[TX].state is PaymentState.PAID and by_id[TX].payment_id == PID
    assert by_id[TX].amount == Decimal("179") and by_id[TX].currency == "RUB"
    assert by_id["tx-2"].state is PaymentState.CANCELED and by_id["tx-2"].amount == Decimal("99.9")
    assert by_id["tx-2"].payment_id is None


async def test_fetch_status_errors() -> None:
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(lambda _c: HttpResponse(503, b""))).fetch_status([TX])
    assert err.value.retryable
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(lambda _c: HttpResponse(401, b""))).fetch_status([TX])
    assert not err.value.retryable
    bad = HttpResponse(200, b'{"id": "x", "status": "CONFIRMED", "paymentDetails": {"amount": "x"}}')
    assert await provider(CountingHttp(lambda _c: bad)).fetch_status([TX]) == []


async def test_test_credentials() -> None:
    assert (await provider(CountingHttp(FakePlatega())).test_credentials()).ok
    bad = await provider(CountingHttp(FakePlatega(api_secret="other"))).test_credentials()
    assert not bad.ok and "MerchantId" in bad.message
    html = await provider(
        CountingHttp(lambda _c: HttpResponse(404, b"<html></html>", {"Content-Type": "text/html"}))
    ).test_credentials()
    assert not html.ok
    down = await provider(CountingHttp(lambda _c: HttpResponse(502, b""))).test_credentials()
    assert not down.ok and "недоступна" in down.message


def test_default_base_url_is_official() -> None:
    assert DEFAULT_BASE_URL == "https://app.platega.io"


# ------------------------------------------------------------------------------ through the real core


async def test_callback_alone_never_credits_and_verify_does(
    make_harness: HarnessFactory, db: CountingDatabase
) -> None:
    desk = FakePlatega()
    harness = await make_harness(Platega, CONFIG, http=CountingHttp(desk))
    result = await harness.core.create_payment(
        user_id=harness.user_id,
        instance_id=harness.instance.id,
        amount_minor=17_900,
        currency="RUB",
        description="Пополнение",
        method_kind="sbp",
    )
    ext = result.checkout.external_id
    assert ext is not None and desk.created[0][1]["paymentMethod"] == 2
    # a forged «CONFIRMED» with the right credentials but the desk still PENDING: nothing happens
    assert await harness.send(desk.callback(ext, "CONFIRMED")) == 200
    assert await harness.status(result.payment_id) == "pending"
    await harness.core.verify(harness.instance.id, [(ext, result.payment_id)])
    assert await harness.status(result.payment_id) == "pending" and harness.credited == []
    # really paid on the desk: the verify job credits once
    desk.set_status(ext, "CONFIRMED")
    assert await harness.send(desk.callback(ext)) == 200
    await harness.core.verify(harness.instance.id, [(ext, result.payment_id)])
    await harness.core.verify(harness.instance.id, [(ext, result.payment_id)])
    assert await harness.status(result.payment_id) == "paid" and len(harness.credited) == 1
    assert "verify_queued" in await outcomes(db)
    # chargeback is read from the desk as well
    desk.set_status(ext, "CHARGEBACKED")
    await harness.core.verify(harness.instance.id, [(ext, result.payment_id)])
    assert await harness.status(result.payment_id) == "refunded" and len(harness.refunded) == 1


async def test_late_payment_after_cancel_and_amount_mismatch(make_harness: HarnessFactory) -> None:
    desk = FakePlatega()
    harness = await make_harness(Platega, CONFIG, http=CountingHttp(desk))
    late = await harness.core.create_payment(
        user_id=harness.user_id,
        instance_id=harness.instance.id,
        amount_minor=17_900,
        currency="RUB",
        description="x",
    )
    ext = late.checkout.external_id
    assert ext is not None
    desk.set_status(ext, "CANCELED")
    await harness.core.verify(harness.instance.id, [(ext, late.payment_id)])
    assert await harness.status(late.payment_id) == "canceled"
    desk.set_status(ext, "CONFIRMED", amount=179.0)
    await harness.core.verify(harness.instance.id, [(ext, late.payment_id)])
    assert await harness.status(late.payment_id) == "paid"
    short = await harness.core.create_payment(
        user_id=harness.user_id,
        instance_id=harness.instance.id,
        amount_minor=17_900,
        currency="RUB",
        description="x",
    )
    ext2 = short.checkout.external_id
    assert ext2 is not None
    desk.set_status(ext2, "CONFIRMED", amount=17.9)
    await harness.core.verify(harness.instance.id, [(ext2, short.payment_id)])
    assert await harness.status(short.payment_id) == "mismatch" and len(harness.credited) == 1


async def test_end_to_end_over_real_http(make_harness: HarnessFactory) -> None:
    async with FakePlatega() as desk:
        http = InstanceHttp()
        try:
            harness = await make_harness(Platega, {**CONFIG, "base_url": desk.base_url}, http=http)
            result = await harness.core.create_payment(
                user_id=harness.user_id,
                instance_id=harness.instance.id,
                amount_minor=49_900,
                currency="RUB",
                description="Пополнение баланса",
            )
            ext = result.checkout.external_id
            assert ext is not None and desk.transactions[ext].payload == result.payment_id
            desk.set_status(ext, "CONFIRMED")
            assert await harness.send(desk.callback(ext)) == 200
            await harness.core.verify(harness.instance.id, [(ext, result.payment_id)])
            assert await harness.status(result.payment_id) == "paid" and len(harness.credited) == 1
        finally:
            await http.close()
