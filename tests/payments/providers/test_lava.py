"""Lava Business plugin: the shared TestKit plus protocol vectors from ``docs/providers/lava.md``.

The webhook is HMAC-signed (strong), but it names no currency and the official sources disagree on what is
signed, so a ``success`` webhook is only a trigger: the core re-reads ``invoice/status`` before crediting
(spec §4.2 step 4). The core-level TestKit therefore fails exactly on the vectors that expect a webhook to
credit by itself.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from svbg.jobs.queue import Job
from svbg.payments.core import VERIFY_JOB
from svbg.payments.providers.lava import (
    DEFAULT_BASE_URL,
    Lava,
    _php_float,
    json_body,
    php_canonical,
    request_signature,
    webhook_candidates,
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
    PaymentIntent,
    PaymentState,
    ProviderError,
    WebhookIgnored,
    WebhookRejected,
    WebhookRequest,
    hmac_sha256_hex,
)
from tests.dbkit import CountingDatabase
from tests.fakes.lava import (
    ADDITIONAL_KEY,
    FAKE_BASE_URL,
    OTHER_KEY,
    SECRET_KEY,
    SHOP_ID,
    FakeLava,
    hmac_hex,
    sdk_canonical,
)
from tests.payments.providers.conftest import HarnessFactory, outcomes, payment_row

CONFIG = {
    "shop_id": SHOP_ID,
    "secret_key": SECRET_KEY,
    "additional_key": ADDITIONAL_KEY,
    "base_url": FAKE_BASE_URL,
}
PID = "0190f5d2-7b1e-7c3a-9d4e-5f6a7b8c9d0e"
JSON = {"Content-Type": "application/json"}
_STATUS = {
    PaymentState.CREATED: "created",
    PaymentState.PAID: "success",
    PaymentState.FAILED: "fail",
    PaymentState.CANCELED: "fail",
    PaymentState.EXPIRED: "expired",
    PaymentState.REFUNDED: "refund",
    PaymentState.CHARGEBACK: "refund",  # Lava documents no chargebacks: the closest status
}

# ---- spec §10 vectors
W1_KEY = "f4b91efb9b8da35737fcd97ab123c74566f9a654"
W1_SIG = "b0b011552beb994cc04401e088db7b296796a07fc76976b632518fe146ffa330"
W1_CANONICAL = (
    b'{"amount":"1.00","credited":"1.00","custom_fields":"test",'
    b'"invoice_id":"18cf0c0b-6539-4d7c-b3e9-479e4922b87c","order_id":"636a3c2f3e82b","pay_service":"card",'
    b'"pay_time":"2022-11-08 11:26:46","payer_details":"553691******8079","status":"success","type":1}'
)
W1_ORIGINAL = (
    b'{"invoice_id":"18cf0c0b-6539-4d7c-b3e9-479e4922b87c","status":"success",'
    b'"pay_time":"2022-11-08 11:26:46","amount":"1.00","order_id":"636a3c2f3e82b","pay_service":"card",'
    b'"payer_details":"553691******8079","custom_fields":"test","type":1,"credited":"1.00"}'
)
W2_KEY = "test_additional_key"
W2_BODY = (
    '{"invoice_id":"f3f0e0a2-7c9b-4d1e-8a5f-6b2c9d4e1a3b","order_id":"9f1c2d3e-0000-4000-8000-000000000001",'
    '"status":"success","pay_time":"2026-10-02 12:00:00","amount":179.0,"custom_fields":"a/b тест",'
    '"credited":172.73,"pay_service":"sbp","payer_details":null,"type":1}'
).encode()
W2_CANONICAL = (
    b'{"amount":179.0,"credited":172.73,"custom_fields":"a\\/b \\u0442\\u0435\\u0441\\u0442",'
    b'"invoice_id":"f3f0e0a2-7c9b-4d1e-8a5f-6b2c9d4e1a3b","order_id":"9f1c2d3e-0000-4000-8000-000000000001",'
    b'"pay_service":"sbp","pay_time":"2026-10-02 12:00:00","payer_details":null,"status":"success","type":1}'
)
W2_SIG_A = "51c21a5f0c354d6a819bb0f3cd8c7f231e5ee596dac317ed8225e19599726d81"
W2_SIG_B = "6af2a988b7926d67b22db267369e52ad736e9ca455bbfdcee5d48c332adb239a"


def signed(
    data: dict[str, Any],
    *,
    key: str = ADDITIONAL_KEY,
    mode: str = "sdk",
    header: str = "Authorization",
    body: bytes | None = None,
) -> WebhookRequest:
    raw = body if body is not None else json.dumps(data, ensure_ascii=False).encode()
    signature = hmac_hex(key, sdk_canonical(data) if mode == "sdk" else raw)
    return WebhookRequest(body=raw, headers={**JSON, header: signature})


def hook(**kw: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "invoice_id": "7e1a0c3b-0000-4000-8000-000000000001",
        "status": "success",
        "pay_time": "2026-10-02 12:00:00",
        "amount": "179.00",
        "order_id": PID,
        "pay_service": "card",
        "payer_details": "553691******8079",
        "custom_fields": None,
        "type": 1,
        "credited": "171.84",
    }
    data.update(kw)
    return {k: v for k, v in data.items() if v is not ...}


class LavaVectors:
    """:class:`~svbg.payments.testkit.WebhookVectors` of Lava. Lava has no test mode: a «test» webhook is one
    signed with another project's additional key."""

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
        data = hook(invoice_id=external_id, order_id=payment_id, status=_STATUS[state], amount=amount)
        return signed(data, key=OTHER_KEY if test else ADDITIONAL_KEY)

    def tamper(self, req: WebhookRequest) -> WebhookRequest:
        data = json.loads(req.body)
        data["amount"] = "1" + str(data["amount"])
        return WebhookRequest(body=json.dumps(data).encode(), headers=dict(req.headers))

    def strip_auth(self, req: WebhookRequest) -> WebhookRequest:
        return WebhookRequest(body=req.body, headers=JSON)


def provider(http: CountingHttp | None = None, *, is_test: bool = False, **config: Any) -> Lava:
    p = make_provider(Lava, {**CONFIG, **config}, http=http, is_test=is_test)
    assert isinstance(p, Lava)
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


def _job(payload: Any) -> Job:
    now = datetime.now(UTC)
    return Job(
        id=1, queue="default", lane="interactive", kind=VERIFY_JOB,
        payload=payload if isinstance(payload, dict) else json.loads(payload),
        attempts=1, max_attempts=8, ordering_key=None, dedup_key=None, caused_by=None, locked_by=None,
        locked_until=None, next_run_at=now, created_at=now,
    )  # fmt: skip


async def run_verify_jobs(db: CountingDatabase, harness: Any) -> int:
    """Run the queued ``payments.verify`` jobs the way the worker would; returns how many ran."""
    jobs = await db.raw("select id, payload from jobs where kind = $1 order by id", VERIFY_JOB)
    for job in jobs:
        await harness.core.verify_job(_job(job["payload"]), None)
    await db.raw("delete from jobs where kind = $1", VERIFY_JOB)
    return len(jobs)


async def create(harness: Any, amount_minor: int = 17_900) -> tuple[str, str]:
    result = await harness.core.create_payment(
        user_id=harness.user_id,
        instance_id=harness.instance.id,
        amount_minor=amount_minor,
        currency="RUB",
        description="Пополнение баланса",
    )
    assert result.checkout.external_id
    return result.payment_id, result.checkout.external_id


# ------------------------------------------------------------------------------------------- TestKit


def test_static_contract() -> None:
    check_static(Lava)
    caps = Lava.capabilities
    assert not caps.webhook_auth.is_weak and caps.replay_window_s is None
    assert caps.fetch_status and not caps.batch_status and not caps.refund and not caps.recurring
    assert Lava.manifest.currencies == ("RUB",)
    assert (Lava.manifest.min_minor, Lava.manifest.max_minor) == (100, 200_000_000)
    for name, fld in Lava.manifest.config.fields().items():
        assert fld.title and fld.description, name
        if fld.required and fld.default is None:
            assert fld.where, f"{name}: «где взять» is missing"


async def test_testkit_plugin_level() -> None:
    await check_plugin(Lava, CONFIG, LavaVectors())


async def test_testkit_core_level(make_harness: HarnessFactory, db: CountingDatabase) -> None:
    """Everything a signed webhook may do directly passes (bad signature → 401, expiry, test key → rejected);
    the vectors that expect a «success» webhook to credit by itself fail: it is re-read first."""
    harness = await make_harness(Lava, CONFIG, http=CountingHttp(FakeLava()))
    with pytest.raises(KitFailure) as err:
        await check_core(harness, LavaVectors())
    assert err.value.failures == [
        "replayed body credited 0 times",
        "179.00 did not match an invoice of 179",
        "late payment after expiry was not credited",
        "chargeback did not mark the payment refunded",
        "a different amount was not turned into mismatch",
    ]
    assert harness.credited == []
    assert {"bad_signature", "verify_queued"} <= set(await outcomes(db))


@pytest.mark.parametrize("domain", [True, False], ids=["domain", "no-domain"])
async def test_testkit_poll_budget(make_harness: HarnessFactory, domain: bool) -> None:
    http = CountingHttp()
    harness = await make_harness(Lava, CONFIG, http=http, has_domain=domain)

    def pending(call: Any) -> HttpResponse:
        invoice_id = json.loads(call.data)["invoiceId"]
        data = {"status": "created", "id": invoice_id, "amount": 179, "order_id": PID}
        return HttpResponse(200, json.dumps({"data": data, "status": 200, "status_check": True}).encode())

    http.responder = pending
    used = await check_poll_budget(harness, domain=domain, invoices=3)
    assert 0 < used <= (3 if domain else 24) * 3


# ------------------------------------------------------------------------------ signature vectors (§10)


def test_w1_official_vector() -> None:
    assert php_canonical(W1_ORIGINAL) == W1_CANONICAL
    assert hmac_sha256_hex(W1_KEY, W1_CANONICAL) == W1_SIG
    candidates = webhook_candidates(W1_KEY, W1_ORIGINAL)
    assert candidates == [W1_SIG, "fe4b556f1d59ddc21bf730dca2de9a319778401d41588fcc92828003137a1d0a"]


async def test_w1_body_in_original_order_is_accepted() -> None:
    plugin = provider(additional_key=W1_KEY)
    for header in ("Authorization", "Signature"):
        event = await plugin.parse_webhook(WebhookRequest(body=W1_ORIGINAL, headers={**JSON, header: W1_SIG}))
        assert (
            event.state is PaymentState.PAID and event.external_id == "18cf0c0b-6539-4d7c-b3e9-479e4922b87c"
        )
        assert event.payment_id is None  # "636a3c2f3e82b" is not one of our ids
        assert event.amount == Decimal("1") and event.currency is None


def test_w2_canonical_form_and_both_candidates() -> None:
    assert php_canonical(W2_BODY) == W2_CANONICAL
    assert webhook_candidates(W2_KEY, W2_BODY) == [W2_SIG_A, W2_SIG_B]


@pytest.mark.parametrize("sig", [W2_SIG_A, W2_SIG_B, W2_SIG_A.upper(), f"Bearer {W2_SIG_B}"])
async def test_w2_both_signatures_are_accepted(sig: str) -> None:
    event = await provider(additional_key=W2_KEY).parse_webhook(
        WebhookRequest(body=W2_BODY, headers={**JSON, "Signature": sig})
    )
    assert event.amount == Decimal("179") and event.payment_id == "9f1c2d3e-0000-4000-8000-000000000001"


async def test_w2_signed_with_the_secret_key_is_401() -> None:
    plugin = provider(additional_key=W2_KEY, secret_key="test_secret_key")
    wrong = hmac_sha256_hex("test_secret_key", W2_CANONICAL)
    with pytest.raises(WebhookRejected) as err:
        await plugin.parse_webhook(WebhookRequest(body=W2_BODY, headers={**JSON, "Signature": wrong}))
    assert err.value.status == 401


def test_r1_request_signature_over_exact_bytes() -> None:
    body = json_body({"sum": 179.0, "orderId": "9f1c2d3e-0000-4000-8000-000000000001", "shopId": SHOP_ID})
    assert (
        body
        == b'{"sum":179.0,"orderId":"9f1c2d3e-0000-4000-8000-000000000001","shopId":"'
        + SHOP_ID.encode()
        + b'"}'
    )
    assert request_signature("test_secret_key", body) == (
        "14012f24a3a7b203d6b278e817974c4fddf86dd43e212984970adca942356471"
    )
    spaced = body.replace(b":", b": ").replace(b",", b", ")
    assert request_signature("test_secret_key", spaced) == (
        "2323ad6ff3a7073a00c70a1465cd51baef6dcfc43793aad1010d4d065c64405b"
    )


def test_r2_sdk_variant_vector() -> None:
    """Diagnostic only (spec §9 p. 1): the SDK's body-field signature; the plugin signs the header way."""
    data = (
        b'{"orderId":"9f1c2d3e-0000-4000-8000-000000000001","shopId":"'
        + SHOP_ID.encode()
        + b'","sum":"179.00"}'
    )
    assert (
        hmac_hex("test_secret_key", data)
        == "9b257c3ad0f576288a975a7c486598945f313b4ef3211e64bf3ef60d1236b1ee"
    )


@pytest.mark.parametrize(
    ("value", "php"),
    [
        (179.0, "179.0"),
        (172.73, "172.73"),
        (2.5, "2.5"),
        (100.0, "100.0"),
        (0.0001, "0.0001"),
        (1e-5, "1.0e-5"),
        (1e16, "10000000000000000.0"),
        (1e25, "1.0e+25"),
        (-0.5, "-0.5"),
        (0.0, "0.0"),
    ],
)
def test_php_float_format(value: float, php: str) -> None:
    assert _php_float(value) == php


def test_php_canonical_edge_cases() -> None:
    assert (
        php_canonical(b'{"b":{},"a":[1,2.50,"x/y"],"c":{"0":"z"}}')
        == b'{"a":[1,2.5,"x\\/y"],"b":[],"c":["z"]}'
    )
    assert php_canonical(b'{"e":"\\ud83d\\ude00","n":1e2}') == b'{"e":"\\ud83d\\ude00","n":100.0}'
    assert php_canonical(b"[1]") is None and php_canonical(b"nope") is None


# ------------------------------------------------------------------------------------------- webhook


async def test_paid_webhook_fields_and_privacy() -> None:
    event = await provider().parse_webhook(signed(hook()))
    assert event.state is PaymentState.PAID and event.payment_id == PID
    assert event.external_id == "7e1a0c3b-0000-4000-8000-000000000001"
    assert event.amount == Decimal("179") and event.currency is None and not event.is_test
    assert event.signed_at is None and event.summary["credited"] == "171.84"
    assert "553691" not in json.dumps(dict(event.summary))


@pytest.mark.parametrize("mode", ["sdk", "raw"])
@pytest.mark.parametrize("header", ["Authorization", "Signature"])
async def test_both_headers_and_both_forms(mode: str, header: str) -> None:
    event = await provider().parse_webhook(signed(hook(), mode=mode, header=header))
    assert event.state is PaymentState.PAID


@pytest.mark.parametrize("amount", ["1.00", 1, "1", 1.0])
async def test_amount_as_number_or_string(amount: Any) -> None:
    event = await provider().parse_webhook(signed(hook(amount=amount), mode="raw"))
    assert event.amount == Decimal("1")


@pytest.mark.parametrize(
    ("status", "state"),
    [
        ("success", PaymentState.PAID),
        ("fail", PaymentState.FAILED),
        ("expired", PaymentState.EXPIRED),
        ("refund", PaymentState.REFUNDED),
        ("created", PaymentState.CREATED),
        ("SUCCESS", PaymentState.PAID),
    ],
)
async def test_webhook_statuses(status: str, state: PaymentState) -> None:
    assert (await provider().parse_webhook(signed(hook(status=status)))).state is state


@pytest.mark.parametrize(
    "data",
    [hook(type=4, status="activated"), hook(type=3), hook(status="weird")],
    ids=["recurring", "payoff", "unknown-status"],
)
async def test_other_webhooks_are_acknowledged(data: dict[str, Any]) -> None:
    with pytest.raises(WebhookIgnored) as err:
        await provider().parse_webhook(signed(data))
    assert err.value.response.status == 200


@pytest.mark.parametrize(
    "req",
    [
        WebhookRequest(body=json.dumps(hook()).encode(), headers=JSON),
        WebhookRequest(body=json.dumps(hook()).encode(), headers={**JSON, "Signature": "  "}),
        signed(hook(), key=OTHER_KEY),
        signed(hook(), key=SECRET_KEY),
        signed(hook(), body=json.dumps(hook(amount="1.00")).encode()),
    ],
    ids=["no-header", "blank", "other-project", "secret-key", "body-changed"],
)
async def test_bad_signatures_are_401(req: WebhookRequest) -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(req)
    assert err.value.status == 401


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[1, 2]",
        json.dumps(hook(invoice_id=None, order_id="x")).encode(),
        json.dumps(hook(amount="abc")).encode(),
    ],
    ids=["not-json", "array", "no-ids", "bad-amount"],
)
async def test_signed_but_malformed_is_400(body: bytes) -> None:
    req = WebhookRequest(body=body, headers={**JSON, "Signature": hmac_hex(ADDITIONAL_KEY, body)})
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(req)
    assert err.value.status == 400


async def test_is_test_follows_the_instance() -> None:
    assert (await provider(is_test=True).parse_webhook(signed(hook()))).is_test


def test_ack_is_200_ok() -> None:
    resp = provider().ack(None)
    assert resp.status == 200 and resp.body == b"OK"


# ------------------------------------------------------------------------------------------- create


async def test_create_signs_the_exact_bytes_it_sends() -> None:
    desk = FakeLava()
    http = CountingHttp(desk)
    before = datetime.now(UTC)
    checkout = await provider(http).create(intent())
    call = http.calls[0]
    assert call.method == "POST" and call.url == f"{FAKE_BASE_URL}/business/invoice/create"
    assert call.json is None and isinstance(call.data, bytes)
    assert call.headers["Signature"] == hmac_hex(SECRET_KEY, call.data)
    assert call.headers["Content-Type"] == "application/json" and call.headers["Accept"] == "application/json"
    assert call.data.startswith(b'{"sum":179,"orderId":"' + PID.encode() + b'","shopId":"')
    sent = desk.created[0]
    assert sent["hookUrl"] == "https://shop.example/webhooks/pay/1/token" and sent["expire"] == 300
    assert "successUrl" not in sent and "includeService" not in sent and "customFields" not in sent
    assert sent["comment"] == "Пополнение баланса на 179 ₽" and "c0ffee" not in call.data.decode()
    assert checkout.kind == "url" and checkout.external_id == "7e1a0c3b-0000-4000-8000-000000000001"
    assert checkout.pay_url == f"https://pay.lava.fake/invoice/{checkout.external_id}"
    assert checkout.expires_at is not None
    assert 299 * 60 <= (checkout.expires_at - before).total_seconds() <= 301 * 60


@pytest.mark.parametrize(("minor", "token"), [(17_950, b"179.5"), (17_999, b"179.99"), (100, b"1")])
async def test_sum_is_a_json_number_without_noise(minor: int, token: bytes) -> None:
    desk = FakeLava()
    http = CountingHttp(desk)
    await provider(http).create(intent(amount_minor=minor))
    assert http.calls[0].data.startswith(b'{"sum":' + token + b",")
    assert desk.created[0]["sum"] == Decimal(token.decode())


async def test_create_with_options() -> None:
    desk = FakeLava()
    plugin = provider(CountingHttp(desk), expire_minutes="60", services="sbp, card,sbp")
    await plugin.create(intent(return_url="https://t.me/svbg_bot"))
    sent = desk.created[0]
    assert sent["expire"] == 60 and sent["includeService"] == ["sbp", "card"]
    assert sent["successUrl"] == sent["failUrl"] == "https://t.me/svbg_bot"


async def test_create_refuses_foreign_currency_and_bad_amounts() -> None:
    http = CountingHttp(FakeLava())
    with pytest.raises(ProviderError, match="рубли"):
        await provider(http).create(intent(currency="USD"))
    with pytest.raises(ProviderError, match="от 1"):
        await provider(http).create(intent(amount_minor=50))
    assert http.requests == 0


@pytest.mark.parametrize(
    ("desk", "words", "status"),
    [
        (FakeLava(secret_key="other-secret"), "Секретный ключ", 401),
        (FakeLava(shop_id="00000000-0000-4000-8000-000000000000"), "ID проекта", 404),
    ],
)
async def test_create_with_wrong_keys(desk: FakeLava, words: str, status: int) -> None:
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(desk)).create(intent())
    assert words in err.value.human and err.value.status == status and not err.value.retryable
    assert SECRET_KEY not in err.value.human


async def test_duplicate_order_id_is_422() -> None:
    desk = FakeLava()
    plugin = provider(CountingHttp(desk))
    await plugin.create(intent())
    with pytest.raises(ProviderError) as err:
        await plugin.create(intent())
    assert err.value.status == 422 and "already been taken" in err.value.human and not err.value.retryable


@pytest.mark.parametrize(("status", "retryable"), [(429, True), (500, True), (502, True), (403, False)])
async def test_create_errors(status: int, retryable: bool) -> None:
    desk = FakeLava()
    desk.fail_with = status
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(desk)).create(intent())
    assert err.value.retryable is retryable


@pytest.mark.parametrize(
    "answer",
    [
        {"data": {"id": "x"}, "status": 200, "status_check": True},
        {"data": {"url": "https://x"}, "status": 200, "status_check": True},
        {"data": {"id": "x", "url": "https://x"}, "status": 200, "status_check": False},
        {"data": None, "status": 200, "status_check": True},
        [1, 2],
    ],
)
async def test_create_with_a_bad_answer(answer: Any) -> None:
    async def junk(_call: Any) -> HttpResponse:
        return HttpResponse(200, json.dumps(answer).encode())

    with pytest.raises(ProviderError):
        await provider(CountingHttp(junk)).create(intent())


# ------------------------------------------------------------------------------------------- status


@pytest.mark.parametrize(
    ("status", "state"),
    [
        ("created", PaymentState.CREATED),
        ("success", PaymentState.PAID),
        ("fail", PaymentState.FAILED),
        ("expired", PaymentState.EXPIRED),
        ("refund", PaymentState.REFUNDED),
    ],
)
async def test_fetch_status(status: str, state: PaymentState) -> None:
    desk = FakeLava()
    http = CountingHttp(desk)
    plugin = provider(http)
    checkout = await plugin.create(intent())
    assert checkout.external_id
    desk.invoices[checkout.external_id].status = status
    [st] = await plugin.fetch_status([checkout.external_id])
    assert st.state is state and st.external_id == checkout.external_id and st.payment_id == PID
    assert st.amount == Decimal("179") and st.currency == "RUB"
    call = http.calls[-1]
    assert json.loads(call.data) == {"shopId": SHOP_ID, "invoiceId": checkout.external_id}
    assert call.headers["Signature"] == hmac_hex(SECRET_KEY, call.data)


async def test_fetch_status_unknown_and_weird() -> None:
    desk = FakeLava()
    plugin = provider(CountingHttp(desk))
    checkout = await plugin.create(intent())
    assert checkout.external_id
    desk.invoices[checkout.external_id].status = "strange"
    assert await plugin.fetch_status([checkout.external_id, "nope", ""]) == []
    assert desk.status_calls == [checkout.external_id, "nope"]


async def test_fetch_status_rejects_an_answer_about_another_invoice() -> None:
    async def other(_call: Any) -> HttpResponse:
        data = {"status": "success", "id": "other", "amount": 179, "order_id": PID}
        return HttpResponse(200, json.dumps({"data": data, "status": 200, "status_check": True}).encode())

    assert await provider(CountingHttp(other)).fetch_status(["inv-1"]) == []


@pytest.mark.parametrize(("status", "retryable"), [(401, False), (429, True), (503, True), (422, False)])
async def test_fetch_status_errors(status: int, retryable: bool) -> None:
    desk = FakeLava()
    desk.fail_with = status
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(desk)).fetch_status(["inv-1"])
    assert err.value.retryable is retryable


async def test_test_credentials() -> None:
    desk = FakeLava()
    probe = await provider(CountingHttp(desk)).test_credentials()
    assert probe.ok and desk.created == [] and probe.details["services"] == ["card", "sbp"]
    assert desk.requests[0][1] == "/business/invoice/get-available-tariffs"
    bad = await provider(CountingHttp(FakeLava(secret_key="other"))).test_credentials()
    assert not bad.ok and "Секретный ключ" in bad.message
    foreign = await provider(CountingHttp(FakeLava(shop_id=PID))).test_credentials()
    assert not foreign.ok and "ID проекта" in foreign.message

    async def html(_call: Any) -> HttpResponse:
        return HttpResponse(404, b"<html>nginx</html>")

    assert "Адрес API" in (await provider(CountingHttp(html)).test_credentials()).message


def test_config_defaults_and_secrets() -> None:
    cfg = Lava.manifest.config.parse(
        {"SHOP_ID": SHOP_ID, "SECRET_KEY": SECRET_KEY, "ADDITIONAL_KEY": ADDITIONAL_KEY}
    )
    assert cfg.base_url == DEFAULT_BASE_URL and cfg.expire_minutes == 300 and cfg.services is None
    assert SECRET_KEY not in repr(cfg) and ADDITIONAL_KEY not in repr(cfg)
    assert sorted(cfg.secret_values()) == sorted([SECRET_KEY, ADDITIONAL_KEY])
    with pytest.raises(ValueError, match="services"):
        Lava.manifest.config.parse({**CONFIG, "services": "card,qiwi"})
    with pytest.raises(ValueError, match="expire_minutes"):
        Lava.manifest.config.parse({**CONFIG, "expire_minutes": "7201"})


# --------------------------------------------------------------------------------------- core level


async def test_webhook_then_verification_credits_once(
    make_harness: HarnessFactory, db: CountingDatabase
) -> None:
    desk = FakeLava()
    harness = await make_harness(Lava, CONFIG, http=CountingHttp(desk))
    pid, invoice_id = await create(harness)
    invoice = desk.invoices[invoice_id]
    invoice.status = "success"
    for _ in range(3):
        assert await harness.send(desk.webhook(invoice)) == 200
    assert await harness.status(pid) == "pending"  # a webhook never credits by itself
    assert "verify_queued" in await outcomes(db)
    await run_verify_jobs(db, harness)
    await harness.core.verify(harness.instance.id, [(invoice_id, None)])
    assert await harness.status(pid) == "paid" and len(harness.credited) == 1
    row = await payment_row(db, pid)
    assert row["paid_amount_minor"] == 17_900 and row["external_id"] == invoice_id


async def test_success_webhook_for_an_unpaid_invoice_never_credits(
    make_harness: HarnessFactory, db: CountingDatabase
) -> None:
    desk = FakeLava()
    harness = await make_harness(Lava, CONFIG, http=CountingHttp(desk))
    pid, invoice_id = await create(harness)
    assert await harness.send(desk.webhook(desk.invoices[invoice_id], mode="raw", header="Signature")) == 200
    assert await run_verify_jobs(db, harness) == 1
    assert await harness.status(pid) == "pending" and harness.credited == []


async def test_fail_expiry_late_payment_and_refund(
    make_harness: HarnessFactory, db: CountingDatabase
) -> None:
    desk = FakeLava()
    harness = await make_harness(Lava, CONFIG, http=CountingHttp(desk))
    failed_pid, failed_id = await create(harness)
    late_pid, late_id = await create(harness)
    desk.invoices[failed_id].status = "fail"
    assert await harness.send(desk.webhook(desk.invoices[failed_id], "fail")) == 200
    assert await harness.status(failed_pid) == "failed"  # authentic, nothing to credit: applied directly
    desk.invoices[late_id].status = "expired"
    await harness.core.verify(harness.instance.id, [(late_id, None)])
    assert await harness.status(late_pid) == "expired"
    desk.invoices[late_id].status = "success"  # a late payment wins after verification
    assert await harness.send(desk.webhook(desk.invoices[late_id])) == 200
    await run_verify_jobs(db, harness)
    assert await harness.status(late_pid) == "paid" and [p.id for p in harness.credited] == [late_pid]
    desk.invoices[late_id].status = "refund"
    await harness.core.verify(harness.instance.id, [(late_id, None)])
    assert await harness.status(late_pid) == "refunded" and [p.id for p in harness.refunded] == [late_pid]


async def test_amount_difference_is_a_mismatch(make_harness: HarnessFactory, db: CountingDatabase) -> None:
    desk = FakeLava()
    harness = await make_harness(Lava, CONFIG, http=CountingHttp(desk))
    pid, invoice_id = await create(harness)
    invoice = desk.invoices[invoice_id]
    invoice.status, invoice.amount = "success", Decimal("100")
    assert await harness.send(desk.webhook(invoice)) == 200
    await run_verify_jobs(db, harness)
    assert await harness.status(pid) == "mismatch" and harness.credited == []


async def test_end_to_end_over_real_http(make_harness: HarnessFactory, db: CountingDatabase) -> None:
    """The request signature survives the real transport: Lava checks the bytes it receives."""
    async with FakeLava() as desk:
        http = InstanceHttp()
        try:
            harness = await make_harness(Lava, {**CONFIG, "base_url": desk.base_url}, http=http)
            pid, invoice_id = await create(harness)
            assert desk.created[0]["orderId"] == pid
            row = await payment_row(db, pid)
            assert row["poll_plan"] == "domain"
            desk.invoices[invoice_id].status = "success"
            assert await harness.send(desk.webhook(desk.invoices[invoice_id])) == 200
            await run_verify_jobs(db, harness)
            assert await harness.status(pid) == "paid"
            assert (await harness.instance.provider.test_credentials()).ok
        finally:
            await http.close()


async def test_logs_no_secrets(caplog: pytest.LogCaptureFixture) -> None:
    desk = FakeLava()
    desk.fail_with = 400
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(ProviderError):
            await provider(CountingHttp(desk)).create(intent())
        with pytest.raises(WebhookRejected):
            await provider().parse_webhook(signed(hook(), key=OTHER_KEY))
        with pytest.raises(WebhookIgnored):
            await provider().parse_webhook(signed(hook(status="weird")))
    assert SECRET_KEY not in caplog.text and ADDITIONAL_KEY not in caplog.text
