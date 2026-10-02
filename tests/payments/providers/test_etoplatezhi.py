"""«Это платежи» plugin: the shared TestKit plus the vectors of ``docs/providers/etoplatezhi.md`` §12."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from svbg.payments.providers.etoplatezhi import (
    DEFAULT_API_URL,
    DEFAULT_PAYMENT_PAGE_URL,
    Etoplatezhi,
    payment_page_url,
    sign,
    signing_string,
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
from tests.fakes.etoplatezhi import (
    FAKE_API_URL,
    FAKE_PAGE_URL,
    PROJECT_ID,
    SECRET_KEY,
    FakeEtoplatezhi,
    signature,
    signed_body,
)
from tests.payments.providers.conftest import HarnessFactory, outcomes

CONFIG = {
    "project_id": str(PROJECT_ID),
    "secret_key": SECRET_KEY,
    "api_url": FAKE_API_URL,
    "payment_page_url": FAKE_PAGE_URL,
}
PID = "0190f5d2-7b1e-7c3a-9d4e-5f6a7b8c9d0e"
TEST_PROJECT_KEY = "secret-of-the-test-project"
_STATUS = {
    PaymentState.CREATED: "error",
    PaymentState.PROCESSING: "processing",
    PaymentState.PAID: "success",
    PaymentState.EXPIRED: "decline",  # no expiry status: an expired form ends in decline (spec §6.1)
    PaymentState.CANCELED: "decline",
    PaymentState.FAILED: "decline",
    PaymentState.CHARGEBACK: "reversed",  # chargebacks never come by callback (spec §6.2)
    PaymentState.REFUNDED: "refunded",
}

# --------------------------------------------------------------------- spec §12: official vectors (E, S)
E1 = {
    "project_id": 12345,
    "payment_id": "X03936",
    "payment_amount": 2035,
    "payment_currency": "USD",
    "payment_description": "Guyliner purchase",
    "customer_first_name": "Jack",
    "customer_id": "user007",
    "customer_last_name": "Sparrow",
    "customer_phone": "02081234567",
    "close_on_missclick": True,
}
E1_STRING = (
    "close_on_missclick:1;customer_first_name:Jack;customer_id:user007;customer_last_name:Sparrow;"
    "customer_phone:02081234567;payment_amount:2035;payment_currency:USD;"
    "payment_description:Guyliner purchase;"
    "payment_id:X03936;project_id:12345"
)
E1_SIG = "SyA3cx/dmFrwjRcpbnwEK9zaklWKR9buIfTctQob/EHUTutFLpI0zWpSDFEWEwbZt/04i83395RCdEhtUMw83A=="
E2 = {
    "general": {"project_id": 3254, "payment_id": "id_38202316"},
    "customer": {
        "id": "585741",
        "email": "johndoe@mycompany.com",
        "first_name": "John",
        "last_name": "Doe",
        "address": "Downing str., 23",
        "identify": {"doc_number": "54122312544"},
        "ip_address": "111.222.333.444",
    },
    "payment": {"amount": 10800, "currency": "USD", "description": "Computer keyboards"},
    "receipt_data": {"positions": [{"quantity": 10, "amount": 108, "description": "Computer keyboard"}]},
    "return_url": {
        "success": "https://paymentpage.mycompany.com/complete-redirect?id=success",
        "decline": "https://paymentpage.mycompany.com/complete-redirect?id=decline",
    },
}
E2_SIG = "VLLZzVNGevQNhr1b4TEhbC4qqHD17Kyn/M6FPNN93ttyk/amJgD/R6dayTKVvW6/QCRdq4hOf8R2w/xbUa8f2w=="
E3 = {
    "interval": {"from": "2020-01-01 14:53:55", "to": "2020-01-30 13:53:59"},
    "limit": 3,
    "offset": 0,
    "project_id": [183],
    "token": "WKiarERJ5pcceNerpM9R5TNnyPTQMl",
    "tz": "Asia/Singapore",
}
E3_SIG = "Ini3aKje6aZskajTuRS761YOzVqierlVRafZdxIz48wmVnL7yxgy9vDsp7T2/LGPGHJ/DHoKOgP7VqObJALrUA=="
E4 = {
    "project_id": 28051,
    "payment": {
        "id": "5242723",
        "type": "purchase",
        "status": "success",
        "date": "2023-03-10T12:26:17+0000",
        "method": "card",
        "sum": {"amount": 5200, "currency": "EUR"},
        "description": "",
    },
    "account": {
        "number": "424242******4242",
        "token": "c8175453f68ec7c8fb3f052b8d786c661261efebcb91155327a6c7b8f8e66359",
        "type": "visa",
        "card_holder": "TEST TEST",
        "expiry_month": "01",
        "expiry_year": "2025",
    },
    "customer": {"id": "782572"},
    "operation": {
        "id": 5028800010128225,
        "type": "sale",
        "status": "success",
        "date": "2023-03-10T12:26:17+0000",
        "created_date": "2023-03-10T12:26:15+0000",
        "request_id": (
            "1f6d3ac37444142f5bd27e7491faa360633fd5a2-fc98e73d475fa4cd6ee02fc6340c964f0267b3d8-05028801"
        ),
        "sum_initial": {"amount": 5200, "currency": "EUR"},
        "sum_converted": {"amount": 5200, "currency": "EUR"},
        "provider": {
            "id": 6,
            "payment_id": "16784511766816",
            "date": "2023-03-10T10:26:17+0000",
            "auth_code": "563253",
            "endpoint_id": 6,
        },
        "code": "0",
        "message": "Success",
    },
}
E4_SIG = "Y0qjN9dDnPTdddkVvXKS1pGp2z8ZpIl60P1CocND3YRxuBNx05ZMnhUaGFt90fPzgwsI/UpLw0q2RR/XTiDQBg=="
S1 = {"payment": {"id": "test-payment", "status": "success"}}
S1_SIG = "UGzKT0NC26f4u0niyJSQPx5q3kFFIndwLXeJVXahfCFwbY+Svg1WoXIxzrIyyjWUSLFhT8wAQ5SfBDRHnwm6Yg=="
S2 = {"payment": {"id": "test-payment", "status": "success"}, "some_bool_param": True, "frame_mode": "popup"}
S2_SIG = "TZS0J65ReNUMgKzeXUey9xOGGyC7r4OhsFXt/3H8XZM2Le8Wot2E1NIjeSPOyV1F3sUU6F3kfo9om2dhbe3ieA=="
S3 = {
    "project_id": "123",
    "recurring": {
        "id": 321, "status": "active", "type": "Y", "currency": "EUR", "exp_year": "2025", "exp_month": "12",
        "period": "D", "time": "11",
    },
}  # fmt: skip
S3_SIG = "AThqkBCZ6WZtY3WrMV28o7SM/vq6OIVF9qiVbELN4e/Ux59Lb5LFFnEuTq6bHa5pRvaPIkQGABXdpIrNLaeJdQ=="
S4 = {
    "payment": {"id": "test-payment"},
    "errors": [
        {"code": "123", "message": "grand crash", "description": ["description-str", {"description1": 1}]},
        {"code": "456", "message": "minor crash", "description": None},
    ],
}
S4_STRING = (
    "errors:0:code:123;errors:0:description:0:description-str;errors:0:description:1:description1:1;"
    "errors:0:message:grand crash;errors:1:code:456;errors:1:description:;errors:1:message:minor crash;"
    "payment:id:test-payment"
)
S4_SIG = "dxjnun8oySSl0CWlhhjy/k1V9CZcCtaHvu/Y5qJQbWHq8wd6TqTUt4bIfHrlWxT8ba9NOJfkgGHrGc5OgyQVGA=="

# ------------------------------------------------------------------------------- spec §12: own (O1–O3)
O1 = {
    "project_id": 12345,
    "payment_id": "pay_0001",
    "customer_id": "c_9f2a",
    "payment_amount": 17900,
    "payment_currency": "RUB",
    "payment_description": "Подписка 30 дней",
    "force_payment_method": "sbp-qr",
    "best_before": "2026-10-02T13:00:00+00",
    "merchant_callback_url": "https://shop.example/pay/etoplatezhi/webhook",
}
O1_URL = (
    "https://paymentpage.etoplatezhi.ru/payment?project_id=12345&payment_id=pay_0001&customer_id=c_9f2a"
    "&payment_amount=17900&payment_currency=RUB&payment_description=%D0%9F%D0%BE%D0%B4%D0%BF%D0%B8%D1%81"
    "%D0%BA%D0%B0+30+%D0%B4%D0%BD%D0%B5%D0%B9&force_payment_method=sbp-qr&best_before=2026-10-02T13%3A00%3A00"
    "%2B00&merchant_callback_url=https%3A%2F%2Fshop.example%2Fpay%2Fetoplatezhi%2Fwebhook&signature=x4I9EkN3X"
    "DL1u3k8XZzypzBRjH5kP4TAYrPK%2FqvzVM0APpX%2BTRdVNAdmVSD%2BtJ4pMAUlp3ZttxzrDRXGWBnX5Q%3D%3D"
)
O1_SIG = "x4I9EkN3XDL1u3k8XZzypzBRjH5kP4TAYrPK/qvzVM0APpX+TRdVNAdmVSD+tJ4pMAUlp3ZttxzrDRXGWBnX5Q=="
O2_SIG = "n9yx2olgl5v5TGj6rsICnbmmXQjB/UyJpuVPZzzB7lc+zvwd522yYuKHX6BOZhMRL2mSO6eyxNX2FM00Y5P79g=="
O3_BODY = (
    '{"project_id":12345,"payment":{"id":"pay_0001","type":"purchase","status":"success",'
    '"date":"2026-10-02T12:10:05+0000","method":"sbp-qr","sum":{"amount":17900,"currency":"RUB"},'
    '"description":"Подписка 30 дней"},"customer":{"id":"c_9f2a"},"account":{"number":"79*******01"},'
    '"operation":{"id":5028800010128999,"type":"sale","status":"success","date":"2026-10-02T12:10:05+0000",'
    '"created_date":"2026-10-02T12:09:40+0000","request_id":"abc123-def456","sum_initial":{"amount":17900,'
    '"currency":"RUB"},"sum_converted":{"amount":17900,"currency":"RUB"},"provider":{"id":6,'
    '"payment_id":"A1B2C3","auth_code":""},"code":"0","message":"Success"},"errors":[]}'
)
O3_SIG = "ComE3R3Ss///3ghyqlDDYaDMY40QhWNz1gIqvKtwlWRtywP1dnjmvd+lqqobtyh9MHI2azQM5yKDpygH7Pi8xQ=="
O3_BAD_SIG = "llu3uYlxTvJ4G4FwqscC1cQCtNuyDvVd9k3IaNYQWbuZTNF4SsvfgOprR7ktTm+DORHYn6boIhca5+1wFoGH6A=="


def with_signature(body: str | dict[str, Any], sig: str | None) -> WebhookRequest:
    data = json.loads(body) if isinstance(body, str) else dict(body)
    if sig is not None:
        data["signature"] = sig
    return WebhookRequest(body=json.dumps(data, ensure_ascii=False).encode())


class EtoplatezhiVectors:
    """:class:`~svbg.payments.testkit.WebhookVectors`: amounts go in kopecks (``"179.00"`` → ``17900``);
    a «test» callback is one from the separate test project (its own id and key) and cannot authenticate."""

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
        minor = int(Decimal(amount) * 100)
        body = {
            "project_id": 99999 if test else PROJECT_ID,
            "payment": {
                "id": payment_id,
                "type": "purchase",
                "status": _STATUS[state],
                "date": signed_at.strftime("%Y-%m-%dT%H:%M:%S+0000"),
                "method": "sbp-qr",
                "sum": {"amount": minor, "currency": currency},
                "description": "Пополнение",
            },
            "customer": {"id": "c_9f2a"},
            "operation": {"id": 1, "type": "sale", "status": "success", "code": "0", "message": "Success"},
        }
        return with_signature(body, signature(body, TEST_PROJECT_KEY if test else SECRET_KEY))

    def tamper(self, req: WebhookRequest) -> WebhookRequest:
        data = json.loads(req.body)
        data["payment"]["description"] += "!"
        return WebhookRequest(body=json.dumps(data, ensure_ascii=False).encode())

    def strip_auth(self, req: WebhookRequest) -> WebhookRequest:
        data = json.loads(req.body)
        data.pop("signature", None)
        return WebhookRequest(body=json.dumps(data, ensure_ascii=False).encode())


def provider(http: CountingHttp | None = None, **config: Any) -> Etoplatezhi:
    p = make_provider(Etoplatezhi, {**CONFIG, **config}, http=http)
    assert isinstance(p, Etoplatezhi)
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
    check_static(Etoplatezhi)
    caps = Etoplatezhi.capabilities
    assert caps.webhook_auth is WebhookAuth.SIGNATURE and caps.replay_window_s is None
    assert caps.fetch_status and not caps.batch_status and not caps.refund and caps.redirect
    assert set(Etoplatezhi.manifest.method_kinds) == {MethodKind.SBP, MethodKind.CARD}
    assert Etoplatezhi.manifest.currencies == ("RUB",)
    for name, fld in Etoplatezhi.manifest.config.fields().items():
        assert fld.title and fld.description, name
        if fld.required and fld.default is None:
            assert fld.where, f"{name}: «где взять» is missing"
    secrets = {n for n, f in Etoplatezhi.manifest.config.fields().items() if f.is_secret}
    assert secrets == {"secret_key"}


async def test_testkit_plugin_level() -> None:
    await check_plugin(Etoplatezhi, CONFIG, EtoplatezhiVectors())


async def test_testkit_core_level(make_harness: HarnessFactory, db: CountingDatabase) -> None:
    """Strong scheme through the real core. The only kit check that cannot pass is «expired → expired»: the
    platform has no expiry status, an expired form ends in ``decline`` (failed); late payment still wins."""
    harness = await make_harness(Etoplatezhi, CONFIG, http=CountingHttp(FakeEtoplatezhi()))
    with pytest.raises(KitFailure) as err:
        await check_core(harness, EtoplatezhiVectors())
    assert err.value.failures == ["expired webhook did not expire the payment"]
    assert len(harness.credited) == 3 and len(harness.refunded) == 1
    assert {"bad_signature", "applied", "mismatch"} <= set(await outcomes(db))


@pytest.mark.parametrize("domain", [True, False], ids=["domain", "no-domain"])
async def test_testkit_poll_budget(make_harness: HarnessFactory, domain: bool) -> None:
    harness = await make_harness(Etoplatezhi, CONFIG, http=CountingHttp(FakeEtoplatezhi()), has_domain=domain)
    used = await check_poll_budget(harness, domain=domain, invoices=3)
    assert used <= (3 if domain else 24) * 3


# --------------------------------------------------------------------------------- signature vectors


@pytest.mark.parametrize(
    ("data", "key", "expected"),
    [
        (E1, "secret", E1_SIG),
        (E2, "secret", E2_SIG),
        (E3, "secret", E3_SIG),
        (E4, "secret", E4_SIG),
        (S1, "qwerty", S1_SIG),
        (S2, "qwerty", S2_SIG),
        (S3, "qwerty", S3_SIG),
        (S4, "qwerty", S4_SIG),
        (O1, "secret", O1_SIG),
        ({"general": {"project_id": 12345, "payment_id": "pay_0001"}}, "secret", O2_SIG),
        (json.loads(O3_BODY), "secret", O3_SIG),
    ],
    ids=["E1", "E2", "E3", "E4", "S1", "S2", "S3", "S4", "O1", "O2", "O3"],
)
def test_signature_vectors(data: dict[str, Any], key: str, expected: str) -> None:
    assert sign(data, key) == expected
    assert signature(data, key) == expected  # the fake's independent implementation agrees


def test_signing_strings() -> None:
    assert signing_string(E1) == E1_STRING
    assert signing_string(S4) == S4_STRING
    assert signing_string(S2) == "payment:id:test-payment;payment:status:success;some_bool_param:1"
    assert signing_string({"general": {"project_id": 12345, "payment_id": "pay_0001", "signature": "x"}}) == (
        "general:payment_id:pay_0001;general:project_id:12345"
    )
    assert signing_string({"a": [], "b": {}, "c": "", "d": False, "e:f": 1}) == "c:;d:0;e::f:1"
    # signature inside an object inside an array is data, not the signature
    assert signing_string({"x": [{"signature": "s"}]}) == "x:0:signature:s"


def test_o1_payment_page_url() -> None:
    assert payment_page_url(DEFAULT_PAYMENT_PAGE_URL, O1, "secret") == O1_URL


async def test_e4_documented_callback_is_rejected() -> None:
    """The documentation's own example carries a wrong signature and says to reject it."""
    req = with_signature(E4, "IszjSnH+" + E4_SIG[8:-8] + "TTmg==")
    with pytest.raises(WebhookRejected) as err:
        await provider(project_id="28051").parse_webhook(req)
    assert err.value.status == 401
    event = await provider(project_id="28051").parse_webhook(with_signature(E4, E4_SIG))
    assert event.state is PaymentState.PAID and event.external_id == "5242723" and event.payment_id is None
    assert event.amount == Decimal("52.00") and event.currency == "EUR"
    assert "account" not in str(dict(event.summary)) and "782572" not in str(dict(event.summary))


async def test_o3_sbp_callback() -> None:
    event = await provider().parse_webhook(with_signature(O3_BODY, O3_SIG))
    assert event.state is PaymentState.PAID and event.external_id == "pay_0001" and event.payment_id is None
    assert event.amount == Decimal("179.00") and event.currency == "RUB" and event.signed_at is None
    assert event.summary["method"] == "sbp-qr" and event.summary["operation_type"] == "sale"
    assert not event.is_test


async def test_o3_negative_and_s5_s6() -> None:
    changed = O3_BODY.replace("Подписка 30 дней", "Подписка 31 дней")
    assert sign(json.loads(changed), "secret") == O3_BAD_SIG
    for req in (
        with_signature(changed, O3_SIG),
        with_signature(O3_BODY, None),
        with_signature(O3_BODY, ""),
        with_signature(O3_BODY, O3_SIG.lower()),
        with_signature(S1, "UGzKT0NC26f4u0niyJSQPx5q3kFFIndwLXeJVXahfCFwbYg34h32gh3"),
    ):
        with pytest.raises(WebhookRejected) as err:
            await provider().parse_webhook(req)
        assert err.value.status == 401


async def test_foreign_project_is_401_even_when_signed() -> None:
    body = json.loads(O3_BODY) | {"project_id": 54321}
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(with_signature(body, sign(body, SECRET_KEY)))
    assert err.value.reason == "foreign project" and err.value.status == 401


# ------------------------------------------------------------------------------------ callback fields


def _callback(status: str, **payment: Any) -> WebhookRequest:
    body = {
        "project_id": PROJECT_ID,
        "payment": {"id": PID, "status": status, "sum": {"amount": 17900, "currency": "RUB"}} | payment,
    }
    return with_signature(body, sign(body, SECRET_KEY))


@pytest.mark.parametrize(
    ("status", "state"),
    [
        ("success", PaymentState.PAID),
        ("decline", PaymentState.FAILED),
        ("processing", PaymentState.PROCESSING),
        ("awaiting 3ds result", PaymentState.PROCESSING),
        ("awaiting customer", PaymentState.PROCESSING),
        ("error", PaymentState.CREATED),
        ("reversed", PaymentState.REFUNDED),
        ("partially refunded", PaymentState.REFUNDED),
        ("refunded", PaymentState.REFUNDED),
        ("cancelled", PaymentState.CANCELED),
    ],
)
async def test_statuses(status: str, state: PaymentState) -> None:
    event = await provider().parse_webhook(_callback(status))
    assert event.state is state and event.payment_id == PID and event.external_id is None
    assert event.amount == Decimal("179.00") and event.currency == "RUB"


async def test_payment_id_is_case_insensitive() -> None:
    event = await provider().parse_webhook(_callback("success", id=PID.upper()))
    assert event.payment_id == PID


async def test_amount_as_digit_string_and_sum_real() -> None:
    event = await provider().parse_webhook(_callback("success", sum={"amount": "17950", "currency": "rub"}))
    assert event.amount == Decimal("179.50") and event.currency == "RUB"
    real = {"amount": 100, "currency": "RUB"}
    body = {"project_id": PROJECT_ID, "payment": {"id": PID, "status": "success", "sum_real": real}}
    event = await provider().parse_webhook(with_signature(body, sign(body, SECRET_KEY)))
    assert event.amount == Decimal("1.00")


async def test_unknown_currency_has_no_amount() -> None:
    event = await provider().parse_webhook(_callback("success", sum={"amount": 100, "currency": "XYZ"}))
    assert event.amount is None and event.currency == "XYZ"


@pytest.mark.parametrize(
    "payment",
    [
        {"sum": {"amount": 179.5, "currency": "RUB"}},
        {"sum": {"amount": -1, "currency": "RUB"}},
        {"sum": {"amount": True, "currency": "RUB"}},
        {"sum": {"amount": 100, "currency": "R$"}},
        {"id": None},
    ],
    ids=["float", "negative", "bool", "bad-currency", "no-id"],
)
async def test_malformed_callbacks_are_400(payment: dict[str, Any]) -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(_callback("success", **payment))
    assert err.value.status == 400


@pytest.mark.parametrize(
    "body",
    [
        S3 | {"project_id": PROJECT_ID},
        {"project_id": PROJECT_ID, "payment": {"id": PID, "status": "teleported"}},
    ],
    ids=["recurring-only", "unknown-status"],
)
async def test_signed_callbacks_without_a_payment_state_are_ignored(body: dict[str, Any]) -> None:
    with pytest.raises(WebhookIgnored) as err:
        await provider().parse_webhook(with_signature(body, sign(body, SECRET_KEY)))
    assert err.value.response.status == 200


async def test_not_json_is_400() -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(WebhookRequest(body=b"<xml/>"))
    assert err.value.status == 400


def test_ack_is_200() -> None:
    assert provider().ack(None).status == 200


# ------------------------------------------------------------------------------------------- create


async def test_create_builds_a_signed_link_without_requests() -> None:
    http = CountingHttp(FakeEtoplatezhi())
    checkout = await provider(http).create(
        intent(return_url="https://t.me/svbg_bot", method_hint=MethodKind.SBP)
    )
    assert http.requests == 0
    assert checkout.kind == "url" and checkout.external_id == PID
    assert checkout.pay_url is not None and checkout.pay_url.startswith(
        f"{FAKE_PAGE_URL}/payment?project_id="
    )
    params, sig = FakeEtoplatezhi.form_params(checkout.pay_url)
    assert sig == signature(params, SECRET_KEY)
    assert params == {
        "project_id": str(PROJECT_ID),
        "payment_id": PID,
        "customer_id": "c0ffee",
        "payment_amount": "17900",
        "payment_currency": "RUB",
        "payment_description": "Пополнение баланса на 179 ₽",
        "force_payment_method": "sbp-qr",
        "best_before": params["best_before"],
        "merchant_callback_url": "https://shop.example/webhooks/pay/1/token",
        "merchant_success_url": "https://t.me/svbg_bot",
        "merchant_fail_url": "https://t.me/svbg_bot",
        "language_code": "ru",
        "operation_type": "sale",
    }
    best_before = datetime.strptime(params["best_before"], "%Y-%m-%dT%H:%M:%S+00").replace(tzinfo=UTC)
    assert checkout.expires_at == best_before
    assert timedelta(minutes=59) < best_before - datetime.now(UTC) <= timedelta(minutes=60)


async def test_create_card_lifetime_language_and_no_method() -> None:
    p = provider(lifetime_min="15", language="en")
    card = await p.create(intent(method_hint=MethodKind.CARD, amount_minor=17_950))
    plain = await provider().create(intent(description=""))
    assert card.pay_url is not None and plain.pay_url is not None
    card_params, _ = FakeEtoplatezhi.form_params(card.pay_url)
    plain_params, _ = FakeEtoplatezhi.form_params(plain.pay_url)
    assert card_params["force_payment_method"] == "card" and card_params["payment_amount"] == "17950"
    assert card_params["language_code"] == "en"
    assert card.expires_at is not None and card.expires_at - datetime.now(UTC) <= timedelta(minutes=15)
    assert "force_payment_method" not in plain_params and "merchant_success_url" not in plain_params
    assert plain_params["payment_description"] == "Оплата"


async def test_create_refuses_other_currencies_and_missing_customer() -> None:
    with pytest.raises(ProviderError):
        await provider().create(intent(currency="USD", amount_minor=200))
    with pytest.raises(ProviderError) as err:
        await provider().create(intent(customer_ref=" "))
    assert "customer_id" in err.value.human


def test_default_urls_are_official() -> None:
    assert DEFAULT_PAYMENT_PAGE_URL == "https://paymentpage.etoplatezhi.ru"
    assert DEFAULT_API_URL == "https://api.etoplatezhi.ru"


# ------------------------------------------------------------------------------------------- status


async def test_fetch_status_signed_requests_and_answers() -> None:
    desk = FakeEtoplatezhi()
    desk.add(PID, 17_900, "success")
    desk.add("0190f5d2-7b1e-7c3a-9d4e-000000000002", 9_990, "refunded")
    desk.add("0190f5d2-7b1e-7c3a-9d4e-000000000003", 100, "teleported")
    http = CountingHttp(desk)
    ids = [
        PID,
        "0190f5d2-7b1e-7c3a-9d4e-000000000002",
        "0190f5d2-7b1e-7c3a-9d4e-000000000003",
        PID,
        "nobody",
        "",
    ]
    statuses = await provider(http).fetch_status(ids)
    assert http.requests == 4 and len(desk.status_calls) == 4
    first = http.calls[0]
    assert first.method == "POST" and first.url == f"{FAKE_API_URL}/v2/payment/status"
    assert first.json["general"]["project_id"] == PROJECT_ID and first.json["general"]["payment_id"] == PID
    assert first.json["general"]["signature"] == signature(first.json, SECRET_KEY)
    by_id = {s.external_id: s for s in statuses}
    assert set(by_id) == {PID, "0190f5d2-7b1e-7c3a-9d4e-000000000002"}
    assert by_id[PID].state is PaymentState.PAID and by_id[PID].payment_id == PID
    assert by_id[PID].amount == Decimal("179.00") and by_id[PID].currency == "RUB"
    assert by_id["0190f5d2-7b1e-7c3a-9d4e-000000000002"].state is PaymentState.REFUNDED


async def test_o2_status_request_signature() -> None:
    p = provider()
    body = p._status_request("pay_0001")
    assert body == {"general": {"project_id": PROJECT_ID, "payment_id": "pay_0001", "signature": O2_SIG}}


async def test_fetch_status_rejects_unsigned_or_foreign_answers() -> None:
    desk = FakeEtoplatezhi()
    desk.add(PID)
    desk.unsigned_answers = True
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(desk)).fetch_status([PID])
    assert "подписи" in err.value.human and not err.value.retryable
    answer = signed_body({"project_id": PROJECT_ID, "payment": {"id": PID, "status": "success"}}, "other-key")
    forged = HttpResponse(200, json.dumps(answer).encode())
    with pytest.raises(ProviderError):
        await provider(CountingHttp(lambda _c: forged)).fetch_status([PID])
    other = signed_body(
        {
            "project_id": PROJECT_ID,
            "payment": {"id": "0190f5d2-7b1e-7c3a-9d4e-ffffffffffff", "status": "success"},
        }
    )
    swapped = HttpResponse(200, json.dumps(other).encode())
    assert await provider(CountingHttp(lambda _c: swapped)).fetch_status([PID]) == []


async def test_fetch_status_errors() -> None:
    desk = FakeEtoplatezhi()
    desk.ip_blocked = True
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(desk)).fetch_status([PID])
    assert err.value.status == 403 and "IP" in err.value.human and not err.value.retryable
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(lambda _c: HttpResponse(503, b""))).fetch_status([PID])
    assert err.value.retryable
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(lambda _c: HttpResponse(200, b"not json"))).fetch_status([PID])
    assert err.value.retryable
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(FakeEtoplatezhi(secret_key="another"))).fetch_status([PID])
    assert err.value.status == 400 and "Signature is invalid" in err.value.human
    assert SECRET_KEY not in err.value.human


async def test_test_credentials() -> None:
    probe = await provider(CountingHttp(FakeEtoplatezhi())).test_credentials()
    assert probe.ok and probe.details == {"found": False}
    bad = await provider(CountingHttp(FakeEtoplatezhi(secret_key="another"))).test_credentials()
    assert not bad.ok and "HTTP 400" in bad.message
    desk = FakeEtoplatezhi()
    desk.ip_blocked = True
    blocked = await provider(CountingHttp(desk)).test_credentials()
    assert not blocked.ok and "IP" in blocked.message
    down = await provider(CountingHttp(lambda _c: HttpResponse(502, b""))).test_credentials()
    assert not down.ok and "недоступны" in down.message


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


async def test_callback_credits_once_and_refund_follows(
    make_harness: HarnessFactory, db: CountingDatabase
) -> None:
    desk = FakeEtoplatezhi()
    harness = await make_harness(Etoplatezhi, CONFIG, http=CountingHttp(desk))
    result = await _create(harness)
    assert result.checkout.external_id == result.payment_id and harness.http.requests == 0
    payment = desk.open_form(result.checkout.pay_url)
    assert payment.method == "sbp-qr" and payment.customer_id != str(777_000_001)
    assert await harness.send(desk.callback(result.payment_id, "processing")) == 200
    assert await harness.status(result.payment_id) == "pending"
    desk.set_status(result.payment_id, "success")
    paid = desk.callback(result.payment_id)
    for _ in range(3):
        assert await harness.send(paid) == 200
    assert await harness.status(result.payment_id) == "paid" and len(harness.credited) == 1
    desk.set_status(result.payment_id, "refunded")
    assert await harness.send(desk.callback(result.payment_id)) == 200
    assert await harness.status(result.payment_id) == "refunded" and len(harness.refunded) == 1
    forged = desk.callback(result.payment_id, "success", key="not-our-key")
    assert await harness.send(forged) == 401
    assert "bad_signature" in await outcomes(db)


async def test_decline_then_late_success_and_mismatch_by_polling(make_harness: HarnessFactory) -> None:
    desk = FakeEtoplatezhi()
    harness = await make_harness(Etoplatezhi, CONFIG, http=CountingHttp(desk))
    late = await _create(harness, None)
    ref = (late.payment_id, late.payment_id)
    await harness.core.verify(harness.instance.id, [ref])  # the form is not confirmed yet: 3061
    assert await harness.status(late.payment_id) == "pending"
    desk.open_form(late.checkout.pay_url, status="decline")
    await harness.core.verify(harness.instance.id, [ref])
    assert await harness.status(late.payment_id) == "failed"
    desk.set_status(late.payment_id, "success")
    await harness.core.verify(harness.instance.id, [ref])
    assert await harness.status(late.payment_id) == "paid"
    short = await _create(harness, None)
    desk.open_form(short.checkout.pay_url, status="success")
    desk.set_status(short.payment_id, "success", amount=1_790)
    await harness.core.verify(harness.instance.id, [(short.payment_id, short.payment_id)])
    assert await harness.status(short.payment_id) == "mismatch" and len(harness.credited) == 1


async def test_end_to_end_over_real_http(make_harness: HarnessFactory) -> None:
    async with FakeEtoplatezhi() as desk:
        http = InstanceHttp()
        try:
            harness = await make_harness(Etoplatezhi, {**CONFIG, "api_url": desk.api_url}, http=http)
            result = await harness.core.create_payment(
                user_id=harness.user_id,
                instance_id=harness.instance.id,
                amount_minor=49_900,
                currency="RUB",
                description="Пополнение баланса",
            )
            desk.open_form(result.checkout.pay_url, status="success")
            await harness.core.verify(harness.instance.id, [(result.payment_id, result.payment_id)])
            assert await harness.status(result.payment_id) == "paid" and len(harness.credited) == 1
            assert desk.status_calls == [result.payment_id]
        finally:
            await http.close()
