"""AuraPay plugin: the shared TestKit plus protocol vectors from ``docs/providers/aurapay.md`` (§11)."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from svbg.payments.providers.aurapay import (
    DEFAULT_BASE_URL,
    AuraPay,
    _raw_json,
    signature_candidates,
    signing_strings,
)
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
from tests.fakes.aurapay import API_KEY, FAKE_BASE_URL, SHOP_ID, WEBHOOK_SECRET, FakeAuraPay, sign, signed
from tests.payments.providers.conftest import HarnessFactory, outcomes

CONFIG = {"api_key": API_KEY, "shop_id": SHOP_ID, "webhook_secret": WEBHOOK_SECRET, "base_url": FAKE_BASE_URL}
PID = "0190f5d2-7b1e-7c3a-9d4e-5f6a7b8c9d0e"
INV = "3ed6f489-e8fe-4695-8846-9e6f0dbc3b25"
DOC_SHOP = "9bee9309-2585-4332-b63d-e1d897f7ce84"
OTHER_KEY = "secret-key-2-of-another-shop"
_STATUS = {
    PaymentState.CREATED: "PENDING",
    PaymentState.PAID: "PAID",
    PaymentState.EXPIRED: "EXPIRED",
    PaymentState.CANCELED: "EXPIRED",
    PaymentState.FAILED: "EXPIRED",
    PaymentState.REFUNDED: "REFUNDED",
    PaymentState.CHARGEBACK: "REFUNDED",
}

# --- spec §11 vectors (key «test_secret_key_2» is invented by the spec; «» is the empty key) ----------------
V1_BODY = (
    b'{"id": "9beea835-0937-4b5c-8f5a-c3a0d0e60346", "amount": "1250.00", "status": "PAID",'
    b' "comment": "test",'
    b' "created_at": "2024-01-30 19:07:45", "expires_at": "2024-01-30 20:07:45", "service": "sbp",'
    b' "payer_details": "794*****254", "payer_ip": "37.251.8.13",'
    b' "shop_id": "9bee9309-2585-4332-b63d-e1d897f7ce84", "order_id": "11113", "custom_fields": null}'
)
V1_STRING = (
    "1250.00test2024-01-30 19:07:452024-01-30 20:07:459beea835-0937-4b5c-8f5a-c3a0d0e6034611113794*****254"
    "37.251.8.13sbp9bee9309-2585-4332-b63d-e1d897f7ce84PAID"
)
V1_SIG = "a67c40308b7a9b8e9b1bdd537ae287f6469fb09d1fcd25da645ef0c65163063e"
V1_SIG_EMPTY = "06cb1155c1a2b663a063916e5789618bb55372a33e89123d4588e868796303aa"
V2_BODY = {
    "event": "PAID",
    "id": "0b7c2f4e-6a1d-4f3e-9c2b-5d8e1a7f3b90",
    "subscription_id": "SUB-10001",
    "shop_id": DOC_SHOP,
    "invoice_id": INV,
    "service": "card",
    "amount": "299.00",
    "period": 1,
    "interval": "month",
    "status": "ACTIVE",
    "next_pay_at": "2026-11-11 12:00:00",
    "payer_details": "411111******1111",
    "reason": None,
    "created_at": "2026-09-11 12:00:00",
}
V2_STRING = (
    "299.002026-09-11 12:00:00PAID0b7c2f4e-6a1d-4f3e-9c2b-5d8e1a7f3b90month"
    "3ed6f489-e8fe-4695-8846-9e6f0dbc3b25"
    "2026-11-11 12:00:00411111******11111card9bee9309-2585-4332-b63d-e1d897f7ce84ACTIVESUB-10001"
)
V2_SIG = "ffcdf5b96cb92d61433cd2965589177ccc31a7b9570795490cdccd2e11baa895"
V3_VALUES = [
    "1500.00", "1485.00", "15.00", "2026-04-19 14:30:00", "1500.00", "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "PAYOUT-10001", "sbp", DOC_SHOP, "SUCCESS", "+79001234567",
]  # fmt: skip
V3_SIG = "92ef4a0b69ae616b310c3aac94e433f147623c8c311b64af672de235ec8e88c9"
V4_BODY = (
    '{"id":"3ed6f489-e8fe-4695-8846-9e6f0dbc3b25","amount":"179.00","status":"PAID",'
    '"comment":"Подписка 30 дней","created_at":"2026-10-02 12:00:00","expires_at":"2026-10-02 13:00:00",'
    '"service":"sbp","payer_details":null,'
    '"payer_ip":"203.0.113.7","shop_id":"17f9db92-1c87-4ef1-9281-45e1ab74ae96","order_id":"pay_0001",'
    '"custom_fields":null}'
).encode()
V4_STRING = (
    "179.00Подписка 30 дней2026-10-02 12:00:002026-10-02 13:00:003ed6f489-e8fe-4695-8846-9e6f0dbc3b25pay_0001"
    "203.0.113.7sbp17f9db92-1c87-4ef1-9281-45e1ab74ae96PAID"
)
V4_SIG = "9c10d9a40b0f01430d3e1e6cb35b2fd36db540410a5c0fda7a882a72f5d0b25c"
V4_SIG_EMPTY = "e6fe9a676ffc44366dd052975e7e2aa6c434fc1a133e71f1dfb2bd5391d18ce0"


class AuraPayVectors:
    """:class:`~svbg.payments.testkit.WebhookVectors` of AuraPay: the amount goes as a string (as AuraPay
    sends it), the body is signed by spec §5.2. AuraPay has no test mode: a «test» webhook is one signed by
    another shop's key and cannot authenticate."""

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
        payload = {
            "id": external_id,
            "amount": amount,
            "status": _STATUS[state],
            "comment": "Пополнение",
            "created_at": "2026-10-02 12:00:00",
            "expires_at": "2026-10-02 13:00:00",
            "service": "sbp",
            "payer_details": "794*****254",
            "payer_ip": "203.0.113.7",
            "shop_id": SHOP_ID,
            "order_id": payment_id,
            "custom_fields": None,
        }
        return signed(payload, OTHER_KEY if test else WEBHOOK_SECRET)

    def tamper(self, req: WebhookRequest) -> WebhookRequest:
        data = json.loads(req.body)
        data["comment"] = data["comment"] + "!"
        return WebhookRequest(body=json.dumps(data).encode(), headers=dict(req.headers))

    def strip_auth(self, req: WebhookRequest) -> WebhookRequest:
        return WebhookRequest(body=req.body, headers={"Content-Type": "application/json"})


def provider(http: CountingHttp | None = None, **config: Any) -> AuraPay:
    p = make_provider(AuraPay, {**CONFIG, **config}, http=http)
    assert isinstance(p, AuraPay)
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


def with_sig(body: bytes, sig: str) -> WebhookRequest:
    return WebhookRequest(body=body, headers={"X-SIGNATURE": sig})


# ------------------------------------------------------------------------------------------- TestKit


def test_static_contract() -> None:
    check_static(AuraPay)
    caps = AuraPay.capabilities
    assert caps.webhook_auth.is_weak and caps.fetch_status and not caps.batch_status
    assert caps.webhook_auth is WebhookAuth.SECRET_HEADER and caps.replay_window_s is None
    assert not caps.refund and caps.redirect and caps.webhook
    assert set(AuraPay.manifest.method_kinds) == {MethodKind.SBP, MethodKind.CARD}
    assert AuraPay.manifest.currencies == ("RUB",) and AuraPay.manifest.min_minor == 1
    for name, fld in AuraPay.manifest.config.fields().items():
        assert fld.title and fld.description, name
        if fld.required and fld.default is None:
            assert fld.where, f"{name}: «где взять» is missing"
    secrets = {n for n, f in AuraPay.manifest.config.fields().items() if f.is_secret}
    assert secrets == {"api_key", "webhook_secret"}


async def test_testkit_plugin_level() -> None:
    await check_plugin(AuraPay, CONFIG, AuraPayVectors())


async def test_testkit_core_level_weak_scheme(make_harness: HarnessFactory, db: CountingDatabase) -> None:
    """Weak scheme: the kit checks bad signatures and the test flag; nothing is credited from a webhook."""
    harness = await make_harness(AuraPay, CONFIG, http=CountingHttp(FakeAuraPay()))
    await check_core(harness, AuraPayVectors())
    assert harness.credited == []
    assert "bad_signature" in await outcomes(db)


@pytest.mark.parametrize("domain", [True, False], ids=["domain", "no-domain"])
async def test_testkit_poll_budget(make_harness: HarnessFactory, domain: bool) -> None:
    async def pending(call: Any) -> HttpResponse:
        inv = call.json["id"]
        body = {"id": inv, "order_id": "x", "amount": 179, "status": "PENDING"}
        return HttpResponse(200, json.dumps(body).encode())

    harness = await make_harness(AuraPay, CONFIG, http=CountingHttp(pending), has_domain=domain)
    used = await check_poll_budget(harness, domain=domain, invoices=3)
    assert used <= (3 if domain else 24) * 3


# ---------------------------------------------------------------------------------- spec vectors §11


def test_v1_documented_invoice_webhook() -> None:
    data = _raw_json(V1_BODY)
    assert signing_strings(data) == [V1_STRING]
    assert signature_candidates(data, "test_secret_key_2") == [V1_SIG]
    assert signature_candidates(data, "") == [V1_SIG_EMPTY]


async def test_v1_parses_with_the_documented_shop() -> None:
    event = await provider(shop_id=DOC_SHOP).parse_webhook(with_sig(V1_BODY, V1_SIG))
    assert event.state is PaymentState.PAID and event.external_id == "9beea835-0937-4b5c-8f5a-c3a0d0e60346"
    assert event.payment_id is None  # «11113» is not our opaque id
    assert event.amount == Decimal("1250.00") and event.currency == "RUB" and event.signed_at is None
    assert "payer_details" not in event.summary and "payer_ip" not in event.summary


def test_v2_documented_subscription_webhook() -> None:
    raw = _raw_json(json.dumps(V2_BODY).encode())
    assert signing_strings(raw) == [V2_STRING]
    assert signature_candidates(raw, "test_secret_key_2") == [V2_SIG]


async def test_v2_subscription_event_is_ignored_after_the_signature() -> None:
    body = json.dumps(V2_BODY).encode()
    with pytest.raises(WebhookIgnored) as err:
        await provider(shop_id=DOC_SHOP).parse_webhook(with_sig(body, V2_SIG))
    assert err.value.response.status == 200
    with pytest.raises(WebhookRejected):
        await provider(shop_id=DOC_SHOP).parse_webhook(with_sig(body, V1_SIG))


def test_v3_documented_payout_string() -> None:
    payload = {f"k{n:02d}": v for n, v in enumerate(V3_VALUES)}
    raw = _raw_json(json.dumps(payload).encode())
    assert signature_candidates(raw, "test_secret_key_2") == [V3_SIG]


def test_v4_cyrillic_and_nulls() -> None:
    data = _raw_json(V4_BODY)
    assert signing_strings(data) == [V4_STRING]
    assert signature_candidates(data, "test_secret_key_2") == [V4_SIG]
    assert signature_candidates(data, "") == [V4_SIG_EMPTY]


async def test_v4_parses_and_upper_case_hex_is_accepted() -> None:
    for sig in (V4_SIG, V4_SIG.upper(), f"  {V4_SIG} "):
        event = await provider().parse_webhook(with_sig(V4_BODY, sig))
        assert event.state is PaymentState.PAID and event.external_id == INV and event.payment_id is None


@pytest.mark.parametrize(
    ("body", "sig"),
    [
        (V4_BODY.replace(b'"PAID"', b'"EXPIRED"'), V4_SIG),
        (V4_BODY.replace(b'"179.00"', b'"1790.0"'), V4_SIG),
        (V4_BODY, V4_SIG_EMPTY),
        (V4_BODY, V4_SIG[:-1]),
        (V4_BODY, "z" * 64),
        (V4_BODY, ""),
    ],
    ids=["status-changed", "amount-changed", "other-key", "short", "not-hex", "empty"],
)
async def test_negative_vectors_are_401(body: bytes, sig: str) -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(with_sig(body, sig))
    assert err.value.status == 401


async def test_missing_header_is_401() -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(WebhookRequest(body=V4_BODY, headers={}))
    assert err.value.status == 401


async def test_foreign_shop_is_401_even_when_signed() -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(with_sig(V1_BODY, V1_SIG))  # signed, but shop 9bee… is not ours
    assert err.value.status == 401 and err.value.reason == "foreign shop"


# ---------------------------------------------------------------------- ambiguities of the algorithm


def _hmac(text: str) -> str:
    return hmac.new(WEBHOOK_SECRET.encode(), text.encode(), hashlib.sha256).hexdigest()


async def test_float_amount_php_and_raw_forms_both_authenticate() -> None:
    body = f'{{"amount": 299.0, "id": "{INV}", "order_id": "{PID}", "status": "PAID"}}'.encode()
    data = _raw_json(body)
    assert set(signing_strings(data)) == {f"299.0{INV}{PID}PAID", f"299{INV}{PID}PAID"}
    for text in (f"299{INV}{PID}PAID", f"299.0{INV}{PID}PAID"):
        event = await provider().parse_webhook(with_sig(body, _hmac(text)))
        assert event.amount == Decimal("299") and event.payment_id == PID


async def test_integer_and_boolean_values() -> None:
    body = f'{{"id": "{INV}", "period": 1, "flag": true, "status": "PAID", "amount": "5.00"}}'.encode()
    assert set(signing_strings(_raw_json(body))) == {f"5.001{INV}1PAID", f"5.00True{INV}1PAID"}
    event = await provider().parse_webhook(with_sig(body, _hmac(f"5.001{INV}1PAID")))
    assert event.state is PaymentState.PAID


async def test_nested_values_cannot_authenticate() -> None:
    body = f'{{"id": "{INV}", "status": "PAID", "amount": "1.00", "x": {{"a": 1}}}}'.encode()
    assert signing_strings(_raw_json(body)) == []
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(with_sig(body, _hmac(f"1.00{INV}PAID")))
    assert err.value.status == 401


def test_fake_and_plugin_agree() -> None:
    payload = {"id": INV, "amount": "179.00", "status": "PAID", "custom_fields": None, "order_id": PID}
    assert signature_candidates(_raw_json(json.dumps(payload).encode()), WEBHOOK_SECRET) == [sign(payload)]


# ---------------------------------------------------------------------------------- webhook fields


@pytest.mark.parametrize(
    ("status", "state"),
    [
        ("PENDING", PaymentState.CREATED),
        ("PAID", PaymentState.PAID),
        ("paid", PaymentState.PAID),
        ("EXPIRED", PaymentState.EXPIRED),
        ("REFUNDED", PaymentState.REFUNDED),
    ],
)
async def test_statuses(status: str, state: PaymentState) -> None:
    event = await provider().parse_webhook(
        signed({"id": INV, "order_id": PID, "amount": "1.00", "status": status})
    )
    assert event.state is state and event.payment_id == PID and event.external_id == INV


async def test_unknown_status_is_ignored_with_200() -> None:
    with pytest.raises(WebhookIgnored) as err:
        await provider().parse_webhook(signed({"id": INV, "status": "SUCCESS", "amount": "1.00"}))
    assert err.value.response.status == 200


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "PAID", "amount": "1.00"},
        {"id": INV, "status": "PAID", "amount": "abc"},
        {"id": INV, "status": "PAID", "amount": "-1.00"},
    ],
    ids=["no-id", "bad-amount", "negative"],
)
async def test_malformed_webhooks_are_400(payload: dict[str, Any]) -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(signed(payload))
    assert err.value.status == 400


async def test_not_json_is_400() -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(with_sig(b"<xml/>", V4_SIG))
    assert err.value.status == 400


# ------------------------------------------------------------------------------------------- create


async def test_create_body_and_checkout() -> None:
    desk = FakeAuraPay()
    http = CountingHttp(desk)
    checkout = await provider(http).create(
        intent(return_url="https://t.me/svbg_bot", method_hint=MethodKind.SBP)
    )
    body = desk.created[0]
    assert body == {
        "amount": 179,
        "order_id": PID,
        "comment": "Пополнение баланса на 179 ₽",
        "lifetime": 60,
        "success_url": "https://t.me/svbg_bot",
        "fail_url": "https://t.me/svbg_bot",
        "callback_url": "https://shop.example/webhooks/pay/1/token",
        "service": "sbp",
    }
    assert "custom_fields" not in body and "c0ffee" not in json.dumps(body)
    assert checkout.kind == "url" and checkout.external_id in desk.invoices
    assert checkout.pay_url == f"https://pay.aurapay.fake/{checkout.external_id}"
    assert checkout.expires_at is not None
    left = checkout.expires_at - datetime.now(UTC)
    assert timedelta(minutes=59) < left <= timedelta(minutes=60)
    call = http.calls[0]
    assert call.method == "POST" and call.url == f"{FAKE_BASE_URL}/invoice/create"
    assert call.headers["X-ApiKey"] == API_KEY and call.headers["X-ShopId"] == SHOP_ID


async def test_create_kopecks_card_lifetime_and_no_method() -> None:
    desk = FakeAuraPay()
    await provider(CountingHttp(desk), lifetime_min="15").create(
        intent(amount_minor=17_950, method_hint=MethodKind.CARD)
    )
    await provider(CountingHttp(desk)).create(intent(payment_id="0190f5d2-7b1e-7c3a-9d4e-000000000002"))
    assert desk.created[0]["amount"] == 179.5 and desk.created[0]["service"] == "card"
    assert desk.created[0]["lifetime"] == 15
    assert "service" not in desk.created[1] and "success_url" not in desk.created[1]


async def test_create_rejects_other_currencies_without_a_request() -> None:
    http = CountingHttp(FakeAuraPay())
    with pytest.raises(ProviderError):
        await provider(http).create(intent(currency="USD", amount_minor=200))
    assert http.requests == 0


async def test_create_retry_after_a_lost_answer_finds_the_invoice() -> None:
    desk = FakeAuraPay()
    desk.drop_create_answer = True
    p = provider(CountingHttp(desk))
    with pytest.raises(ProviderError) as err:
        await p.create(intent())
    assert err.value.retryable
    checkout = await p.create(intent())
    assert len(desk.invoices) == 1 and checkout.external_id in desk.invoices
    assert desk.status_calls == [{"order_id": PID}]


async def test_create_duplicate_with_another_amount_is_an_error() -> None:
    desk = FakeAuraPay()
    desk.add(INV, PID, amount="99.00")
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(desk)).create(intent())
    assert not err.value.retryable and "другой суммой" in err.value.human


@pytest.mark.parametrize(
    ("answer", "retryable", "words"),
    [
        (HttpResponse(401, b'{"error":"Unauthorized"}'), False, "API-ключ"),
        (HttpResponse(400, b'{"error":"Amount too small","data":[]}'), False, "Amount too small"),
        (HttpResponse(502, b"<html>bad gateway</html>"), True, "недоступна"),
        (HttpResponse(429, b""), True, "недоступна"),
        (HttpResponse(200, b"not json"), True, "непонятный"),
        (HttpResponse(200, b'{"id": "x"}'), False, "непонятный"),
    ],
    ids=["401", "400", "502", "429", "not-json", "no-url"],
)
async def test_create_errors(answer: HttpResponse, retryable: bool, words: str) -> None:
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(lambda _call: answer)).create(intent())
    assert err.value.retryable is retryable and words in err.value.human
    assert API_KEY not in err.value.human


# ------------------------------------------------------------------------------------------- status


async def test_fetch_status_one_request_per_invoice() -> None:
    desk = FakeAuraPay()
    desk.add(INV, PID, amount="179.00", status="PAID")
    desk.add("inv-2", "foreign", amount="99.90", status="REFUNDED")
    desk.add("inv-3", PID, amount="10.00", status="WEIRD")
    http = CountingHttp(desk)
    statuses = await provider(http).fetch_status([INV, "inv-2", "inv-3", INV, "missing", ""])
    assert http.requests == 4
    assert desk.status_calls == [{"id": INV}, {"id": "inv-2"}, {"id": "inv-3"}, {"id": "missing"}]
    by_id = {s.external_id: s for s in statuses}
    assert set(by_id) == {INV, "inv-2"}
    assert by_id[INV].state is PaymentState.PAID and by_id[INV].payment_id == PID
    assert by_id[INV].amount == Decimal("179") and by_id[INV].currency == "RUB"
    assert by_id["inv-2"].state is PaymentState.REFUNDED and by_id["inv-2"].amount == Decimal("99.9")
    assert by_id["inv-2"].payment_id is None


async def test_fetch_status_errors() -> None:
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(lambda _c: HttpResponse(503, b""))).fetch_status([INV])
    assert err.value.retryable
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(lambda _c: HttpResponse(401, b""))).fetch_status([INV])
    assert not err.value.retryable
    bad = HttpResponse(200, json.dumps({"id": INV, "status": "PAID", "amount": "x"}).encode())
    assert await provider(CountingHttp(lambda _c: bad)).fetch_status([INV]) == []
    other = HttpResponse(200, json.dumps({"id": "another", "status": "PAID", "amount": 1}).encode())
    assert await provider(CountingHttp(lambda _c: other)).fetch_status([INV]) == []


async def test_test_credentials() -> None:
    http = CountingHttp(FakeAuraPay())
    assert (await provider(http).test_credentials()).ok
    assert http.calls[0].method == "GET" and http.calls[0].url.endswith("/shop/balance")
    bad = await provider(CountingHttp(FakeAuraPay(api_key="other"))).test_credentials()
    assert not bad.ok and "API-ключ" in bad.message
    error = HttpResponse(400, b'{"error":"Error get balance","data":[]}')
    assert not (await provider(CountingHttp(lambda _c: error)).test_credentials()).ok
    html = await provider(
        CountingHttp(lambda _c: HttpResponse(200, b"<html></html>", {"Content-Type": "text/html"}))
    ).test_credentials()
    assert not html.ok
    down = await provider(CountingHttp(lambda _c: HttpResponse(502, b""))).test_credentials()
    assert not down.ok and "недоступна" in down.message


def test_default_base_url_is_official() -> None:
    assert DEFAULT_BASE_URL == "https://app.aurapay.tech"


# ------------------------------------------------------------------------------ through the real core


async def _create(harness: Any, method_kind: str | None = "sbp") -> Any:
    return await harness.core.create_payment(
        user_id=harness.user_id,
        instance_id=harness.instance.id,
        amount_minor=17_900,
        currency="RUB",
        description="Пополнение",
        method_kind=method_kind,
    )


async def test_webhook_alone_never_credits_and_verify_does(
    make_harness: HarnessFactory, db: CountingDatabase
) -> None:
    desk = FakeAuraPay()
    harness = await make_harness(AuraPay, CONFIG, http=CountingHttp(desk))
    result = await _create(harness)
    ext = result.checkout.external_id
    assert ext is not None and desk.created[0]["service"] == "sbp"
    # a correctly signed «PAID» while the desk is still PENDING (a replay or a moved field): nothing happens
    assert await harness.send(desk.webhook(ext, "PAID")) == 200
    assert await harness.status(result.payment_id) == "pending"
    await harness.core.verify(harness.instance.id, [(ext, result.payment_id)])
    assert await harness.status(result.payment_id) == "pending" and harness.credited == []
    # really paid on the desk: the verify job credits once
    desk.set_status(ext, "PAID")
    assert await harness.send(desk.webhook(ext)) == 200
    await harness.core.verify(harness.instance.id, [(ext, result.payment_id)])
    await harness.core.verify(harness.instance.id, [(ext, result.payment_id)])
    assert await harness.status(result.payment_id) == "paid" and len(harness.credited) == 1
    assert "verify_queued" in await outcomes(db)
    # a refund is visible only by polling
    desk.set_status(ext, "REFUNDED")
    await harness.core.verify(harness.instance.id, [(ext, result.payment_id)])
    assert await harness.status(result.payment_id) == "refunded" and len(harness.refunded) == 1


async def test_late_payment_after_expiry_and_amount_mismatch(make_harness: HarnessFactory) -> None:
    desk = FakeAuraPay()
    harness = await make_harness(AuraPay, CONFIG, http=CountingHttp(desk))
    late = await _create(harness, None)
    ext = late.checkout.external_id
    assert ext is not None
    desk.set_status(ext, "EXPIRED")
    await harness.core.verify(harness.instance.id, [(ext, late.payment_id)])
    assert await harness.status(late.payment_id) == "expired"
    desk.set_status(ext, "PAID")
    await harness.core.verify(harness.instance.id, [(ext, late.payment_id)])
    assert await harness.status(late.payment_id) == "paid"
    short = await _create(harness, None)
    ext2 = short.checkout.external_id
    assert ext2 is not None
    desk.set_status(ext2, "PAID", amount="17.90")
    await harness.core.verify(harness.instance.id, [(ext2, short.payment_id)])
    assert await harness.status(short.payment_id) == "mismatch" and len(harness.credited) == 1


async def test_end_to_end_over_real_http(make_harness: HarnessFactory) -> None:
    async with FakeAuraPay() as desk:
        http = InstanceHttp()
        try:
            harness = await make_harness(AuraPay, {**CONFIG, "base_url": desk.base_url}, http=http)
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
            assert await harness.send(desk.webhook(ext)) == 200
            await harness.core.verify(harness.instance.id, [(ext, result.payment_id)])
            assert await harness.status(result.payment_id) == "paid" and len(harness.credited) == 1
        finally:
            await http.close()
