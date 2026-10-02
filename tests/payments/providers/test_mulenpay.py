"""MulenPay plugin: the shared TestKit plus protocol vectors from ``docs/providers/mulenpay.md``.

MulenPay callbacks are unsigned by protocol, so the plugin declares a weak scheme and the core re-reads every
reported payment with ``fetch_status``; the TestKit vectors that demand a signature therefore fail in exactly
the documented way (an unsigned callback is *accepted for verification*, never credited by itself).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest

from svbg.payments.providers.mulenpay import DEFAULT_BASE_URL, MulenPay, sign
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
)
from tests.dbkit import CountingDatabase
from tests.fakes.mulenpay import API_KEY, FAKE_BASE_URL, SECRET_KEY, SHOP_ID, FakeMulenPay
from tests.fakes.mulenpay import sign as fake_sign
from tests.payments.providers.conftest import HarnessFactory, outcomes, payment_row

CONFIG = {"api_key": API_KEY, "secret_key": SECRET_KEY, "shop_id": str(SHOP_ID), "base_url": FAKE_BASE_URL}
PID = "0190f5d2-7b1e-7c3a-9d4e-5f6a7b8c9d0e"
_CALLBACK = {PaymentState.PAID: "success", PaymentState.CANCELED: "cancel", PaymentState.EXPIRED: "cancel"}


def body(**kw: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": 1001,
        "amount": 179.0,
        "currency": "RUB",
        "uuid": PID,
        "payment_status": "success",
    }
    data.update(kw)
    return {k: v for k, v in data.items() if v is not ...}


def build(data: dict[str, Any] | bytes) -> WebhookRequest:
    raw = data if isinstance(data, bytes) else json.dumps(data).encode()
    return WebhookRequest(body=raw, headers={"Content-Type": "application/json"})


class MulenVectors:
    """:class:`~svbg.payments.testkit.WebhookVectors` of MulenPay. «Authentic» = with a correct optional
    ``sign``; a «test» event is signed with another shop's secret (MulenPay has no test flag)."""

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
        status = _CALLBACK.get(state, state.value)
        secret = "another-shop-secret" if test else SECRET_KEY
        data = body(
            id=external_id,
            uuid=payment_id,
            currency=currency,
            payment_status=status,
            amount="__AMOUNT__",
            sign=fake_sign("rub", f"{Decimal(amount):.2f}", SHOP_ID, secret),
        )
        return build(json.dumps(data).replace('"__AMOUNT__"', amount).encode())

    def tamper(self, req: WebhookRequest) -> WebhookRequest:
        return WebhookRequest(body=req.body.replace(b'"amount": ', b'"amount": 1'), headers=dict(req.headers))

    def strip_auth(self, req: WebhookRequest) -> WebhookRequest:
        data = json.loads(req.body)
        data.pop("sign", None)
        return build(data)


def provider(http: CountingHttp | None = None, *, is_test: bool = False, **config: Any) -> MulenPay:
    p = make_provider(MulenPay, {**CONFIG, **config}, http=http, is_test=is_test)
    assert isinstance(p, MulenPay)
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
    check_static(MulenPay)
    caps = MulenPay.capabilities
    assert caps.webhook_auth.is_weak and caps.fetch_status and not caps.batch_status
    assert caps.receipt_54fz and not caps.refund and caps.replay_window_s is None
    assert MulenPay.manifest.currencies == ("RUB",)
    for field in MulenPay.manifest.config.fields().values():
        assert field.where, field.name


async def test_testkit_plugin_level() -> None:
    """The only failure: a callback without ``sign`` is accepted — that is the protocol (sign is optional),
    and the weak scheme makes the core verify it with fetch_status."""
    with pytest.raises(KitFailure) as err:
        await check_plugin(MulenPay, CONFIG, MulenVectors())
    assert err.value.failures == ["a unsigned webhook was accepted"]


async def test_testkit_core_level(make_harness: HarnessFactory, db: CountingDatabase) -> None:
    harness = await make_harness(MulenPay, CONFIG, http=CountingHttp(FakeMulenPay()))
    with pytest.raises(KitFailure) as err:
        await check_core(harness, MulenVectors())
    assert err.value.failures == ["unsigned webhook answered 200, expected 401"]
    # nothing was credited from callbacks alone
    assert harness.credited == []
    assert {"bad_signature", "verify_queued"} <= set(await outcomes(db))


@pytest.mark.parametrize("domain", [True, False], ids=["domain", "no-domain"])
async def test_testkit_poll_budget(make_harness: HarnessFactory, domain: bool) -> None:
    desk = FakeMulenPay()
    http = CountingHttp(desk)
    harness = await make_harness(MulenPay, CONFIG, http=http, has_domain=domain)

    async def pending(call: Any) -> HttpResponse:
        ext = call.url.rsplit("/", 1)[-1]
        payment = {"id": ext, "uuid": PID, "amount": "179.00", "currency": "rub", "status": 0}
        return HttpResponse(200, json.dumps({"success": True, "payment": payment}).encode())

    http.responder = pending
    used = await check_poll_budget(harness, domain=domain, invoices=3)
    assert 0 < used <= (3 if domain else 24) * 3


# ---------------------------------------------------------------------------------------- signature


def test_signature_known_answer() -> None:
    """``sha1("rub" + "179.00" + "42" + "secret")`` computed with ``printf … | sha1sum``."""
    expected = "7022619dc63372251fbaee08fdfb786ca46aa7a3"
    assert sign("rub", "179.00", 42, "secret") == fake_sign("rub", "179.00", 42, "secret") == expected


def test_signature_vector_from_hashlib() -> None:
    import hashlib

    assert sign("rub", "1000.50", 5, "k") == hashlib.sha1(b"rub1000.505k").hexdigest()


@pytest.mark.parametrize(
    ("amount", "currency"),
    [(179.0, "RUB"), (179.0, "rub"), ("179.00", "RUB"), (179, "RUB"), (100.5, "RUB"), ("100.50", "rub")],
)
async def test_signed_callback_spellings(amount: Any, currency: str) -> None:
    value = f"{Decimal(str(amount)):.2f}"
    data = body(amount=amount, currency=currency, sign=fake_sign("rub", value, SHOP_ID, SECRET_KEY))
    event = await provider().parse_webhook(build(data))
    assert event.summary["signed"] is True and event.amount == Decimal(str(amount))


@pytest.mark.parametrize("bad", ["0" * 40, " ", 12345, "deadbeef"])
async def test_wrong_sign_is_401(bad: Any) -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(build(body(sign=bad)))
    assert err.value.status == 401


async def test_sign_of_another_shop_is_401() -> None:
    data = body(sign=fake_sign("rub", "179.00", SHOP_ID + 1, SECRET_KEY))
    with pytest.raises(WebhookRejected):
        await provider().parse_webhook(build(data))


async def test_unsigned_callback_is_accepted_for_verification() -> None:
    event = await provider().parse_webhook(build(body()))
    assert event.state is PaymentState.PAID and event.summary["signed"] is False
    assert event.external_id == "1001" and event.payment_id == PID
    assert event.amount == Decimal("179") and event.currency == "RUB" and not event.is_test


# ----------------------------------------------------------------------------------------- callbacks


@pytest.mark.parametrize(
    ("status", "state"), [("success", PaymentState.PAID), ("cancel", PaymentState.CANCELED)]
)
async def test_callback_statuses(status: str, state: PaymentState) -> None:
    event = await provider().parse_webhook(build(body(payment_status=status)))
    assert event.state is state


async def test_unknown_status_is_acknowledged() -> None:
    with pytest.raises(WebhookIgnored) as err:
        await provider().parse_webhook(build(body(payment_status="hold")))
    assert err.value.response.status == 200


@pytest.mark.parametrize("amount", [179, 179.0, "179", "179.00", "179,00"])
async def test_amount_forms(amount: Any) -> None:
    assert (await provider().parse_webhook(build(body(amount=amount)))).amount == Decimal("179")


async def test_foreign_uuid_is_not_taken_for_a_payment_id() -> None:
    event = await provider().parse_webhook(build(body(uuid="invoice_123")))
    assert event.payment_id is None and event.external_id == "1001"


@pytest.mark.parametrize(
    "data", [body(id=..., uuid=...), body(amount="abc"), body(currency="RUBLES"), body(currency=5)]
)
async def test_malformed_callbacks_are_400(data: dict[str, Any]) -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(build(data))
    assert err.value.status == 400


@pytest.mark.parametrize("raw", [b"not json", b"[1, 2]", b"null"])
async def test_non_object_bodies_are_400(raw: bytes) -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(build(raw))
    assert err.value.status == 400


def test_ack_is_json_success() -> None:
    resp = provider().ack(None)
    assert resp.status == 200 and json.loads(resp.body) == {"success": True}


# ------------------------------------------------------------------------------------------- create


async def test_create_request_shape_signature_and_privacy() -> None:
    desk = FakeMulenPay()
    http = CountingHttp(desk)
    checkout = await provider(http).create(intent(amount_minor=17_950))
    sent = desk.created[0]
    assert sent["currency"] == "rub" and sent["amount"] == "179.50" and sent["shopId"] == SHOP_ID
    assert sent["uuid"] == PID and sent["sign"] == fake_sign("rub", "179.50", SHOP_ID, SECRET_KEY)
    [item] = sent["items"]
    assert item["price"] == 179.5 and item["quantity"] == 1
    assert (item["vat_code"], item["payment_subject"], item["payment_mode"]) == (0, 4, 4)
    assert "subscribe" not in sent and "holdTime" not in sent
    assert checkout.kind == "url" and checkout.external_id == "1001"
    assert checkout.pay_url == "https://mulenpay.fake/payment/1001"
    assert http.calls[0].headers["Authorization"] == f"Bearer {API_KEY}"
    assert "c0ffee" not in json.dumps(sent)


async def test_create_with_receipt_settings() -> None:
    desk = FakeMulenPay()
    await provider(CountingHttp(desk), vat_code="6", payment_subject="1", payment_mode="1").create(intent())
    [item] = desk.created[0]["items"]
    assert (item["vat_code"], item["payment_subject"], item["payment_mode"]) == (6, 1, 1)


async def test_wrong_secret_is_reported_as_invalid_parameters() -> None:
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(FakeMulenPay()), secret_key="wrong").create(intent())
    assert not err.value.retryable and "секретный ключ" in err.value.human
    assert "wrong" not in err.value.human


async def test_create_refuses_foreign_currency() -> None:
    http = CountingHttp(FakeMulenPay())
    with pytest.raises(ProviderError, match="рубли"):
        await provider(http).create(intent(currency="USD"))
    assert http.requests == 0


@pytest.mark.parametrize(
    ("status", "retryable"), [(401, False), (403, False), (422, False), (429, True), (500, True), (502, True)]
)
async def test_create_errors(status: int, retryable: bool) -> None:
    desk = FakeMulenPay()
    desk.fail_with = status
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(desk)).create(intent())
    assert err.value.retryable is retryable and API_KEY not in err.value.human


@pytest.mark.parametrize(
    "answer",
    [{"success": False, "id": 1, "paymentUrl": "https://x"}, {"success": True, "id": 1}, {"success": True}],
)
async def test_create_with_a_bad_answer(answer: dict[str, Any]) -> None:
    async def junk(_call: Any) -> HttpResponse:
        return HttpResponse(201, json.dumps(answer).encode())

    with pytest.raises(ProviderError):
        await provider(CountingHttp(junk)).create(intent())


# ------------------------------------------------------------------------------------------- status


@pytest.mark.parametrize(
    ("code", "state"),
    [
        (0, PaymentState.CREATED),
        (1, PaymentState.PROCESSING),
        (2, PaymentState.CANCELED),
        (3, PaymentState.PAID),
        (4, PaymentState.FAILED),
        (5, PaymentState.PROCESSING),
        ("3", PaymentState.PAID),
    ],
)
async def test_fetch_status_codes(code: Any, state: PaymentState) -> None:
    desk = FakeMulenPay()
    plugin = provider(CountingHttp(desk))
    await plugin.create(intent())
    desk.payments[1001].status = code  # type: ignore[assignment]
    [st] = await plugin.fetch_status(["1001"])
    assert st.state is state and st.external_id == "1001" and st.payment_id == PID
    assert st.amount == Decimal("179") and st.currency == "RUB"


async def test_fetch_status_unknown_and_weird() -> None:
    desk = FakeMulenPay()
    plugin = provider(CountingHttp(desk))
    await plugin.create(intent())
    desk.payments[1001].status = 99
    assert await plugin.fetch_status(["1001", "777", "budget-x"]) == []
    assert desk.status_calls == ["1001", "777", "budget-x"]


async def test_fetch_status_rejects_an_answer_about_another_payment() -> None:
    async def other(_call: Any) -> HttpResponse:
        payment = {"id": 5, "uuid": PID, "amount": "179.00", "currency": "rub", "status": 3}
        return HttpResponse(200, json.dumps({"success": True, "payment": payment}).encode())

    assert await provider(CountingHttp(other)).fetch_status(["1001"]) == []


@pytest.mark.parametrize(("status", "retryable"), [(401, False), (429, True), (503, True), (400, False)])
async def test_fetch_status_errors(status: int, retryable: bool) -> None:
    desk = FakeMulenPay()
    desk.fail_with = status
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(desk)).fetch_status(["1001"])
    assert err.value.retryable is retryable


async def test_test_credentials() -> None:
    desk = FakeMulenPay()
    probe = await provider(CountingHttp(desk)).test_credentials()
    assert probe.ok and desk.created == [] and desk.requests[0][1] == f"/api/v2/shops/{SHOP_ID}/balances"
    assert not (await provider(CountingHttp(FakeMulenPay(api_key="other"))).test_credentials()).ok
    foreign = await provider(CountingHttp(FakeMulenPay(shop_id=7))).test_credentials()
    assert not foreign.ok and "магазин" in foreign.message


def test_config_defaults_and_secrets() -> None:
    cfg = MulenPay.manifest.config.parse({"API_KEY": API_KEY, "SECRET_KEY": SECRET_KEY, "SHOP_ID": "42"})
    assert cfg.shop_id == 42 and cfg.vat_code == 0 and cfg.payment_subject == 4 and cfg.payment_mode == 4
    assert cfg.base_url == DEFAULT_BASE_URL
    assert API_KEY not in repr(cfg) and SECRET_KEY not in repr(cfg)
    assert sorted(cfg.secret_values()) == sorted([API_KEY, SECRET_KEY])


# --------------------------------------------------------------------------------------- core level


async def test_forged_callback_never_credits(make_harness: HarnessFactory, db: CountingDatabase) -> None:
    """An unsigned «success» for an unpaid payment is queued for verification; MulenPay says «created»."""
    desk = FakeMulenPay()
    harness = await make_harness(MulenPay, CONFIG, http=CountingHttp(desk))
    result = await harness.core.create_payment(
        user_id=harness.user_id,
        instance_id=harness.instance.id,
        amount_minor=17_900,
        currency="RUB",
        description="x",
    )
    ext = result.checkout.external_id
    assert ext == "1001"
    assert await harness.send(desk.callback(1001)) == 200
    assert await harness.status(result.payment_id) == "pending" and harness.credited == []
    await harness.core.verify(harness.instance.id, [(ext, result.payment_id)])
    assert await harness.status(result.payment_id) == "pending" and harness.credited == []


async def test_callback_then_verification_credits_once(
    make_harness: HarnessFactory, db: CountingDatabase
) -> None:
    desk = FakeMulenPay()
    harness = await make_harness(MulenPay, CONFIG, http=CountingHttp(desk))
    result = await harness.core.create_payment(
        user_id=harness.user_id,
        instance_id=harness.instance.id,
        amount_minor=17_900,
        currency="RUB",
        description="x",
    )
    desk.set_status(1001, "paid")
    for _ in range(3):
        assert await harness.send(desk.callback(1001, signed=True)) == 200
    assert await harness.status(result.payment_id) == "pending"  # a callback never changes state by itself
    assert "verify_queued" in await outcomes(db)
    await harness.core.verify(harness.instance.id, [("1001", None)])
    await harness.core.verify(harness.instance.id, [("1001", None)])
    assert await harness.status(result.payment_id) == "paid" and len(harness.credited) == 1
    row = await payment_row(db, result.payment_id)
    assert row["paid_amount_minor"] == 17_900 and row["external_id"] == "1001"


async def test_callback_cannot_redirect_a_payment(make_harness: HarnessFactory) -> None:
    """A callback pairing a real paid MulenPay id with the uuid of another pending payment changes nothing:
    the verification reads the truth by id, and the truth names its own uuid."""
    desk = FakeMulenPay()
    harness = await make_harness(MulenPay, CONFIG, http=CountingHttp(desk))
    paid = await harness.core.create_payment(
        user_id=harness.user_id,
        instance_id=harness.instance.id,
        amount_minor=17_900,
        currency="RUB",
        description="a",
    )
    victim = await harness.pending()
    desk.set_status(1001, "paid")
    forged = build(body(id=1001, uuid=victim))
    assert await harness.send(forged) == 200
    await harness.core.verify(harness.instance.id, [("1001", victim)])
    assert await harness.status(victim) == "pending"
    assert await harness.status(paid.payment_id) == "paid" and len(harness.credited) == 1


async def test_end_to_end_over_real_http(make_harness: HarnessFactory, db: CountingDatabase) -> None:
    async with FakeMulenPay() as desk:
        http = InstanceHttp()
        try:
            harness = await make_harness(MulenPay, {**CONFIG, "base_url": desk.base_url}, http=http)
            result = await harness.core.create_payment(
                user_id=harness.user_id,
                instance_id=harness.instance.id,
                amount_minor=17_900,
                currency="RUB",
                description="Пополнение баланса",
            )
            assert result.checkout.external_id == "1001"
            row = await payment_row(db, result.payment_id)
            assert row["poll_plan"] == "domain"
            desk.set_status(1001, "paid")
            await harness.core.verify(harness.instance.id, [("1001", result.payment_id)])
            assert await harness.status(result.payment_id) == "paid"
            assert (await harness.instance.provider.test_credentials()).ok
        finally:
            await http.close()


async def test_logs_no_secrets(caplog: pytest.LogCaptureFixture) -> None:
    desk = FakeMulenPay()
    desk.fail_with = 400
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(ProviderError):
            await provider(CountingHttp(desk)).create(intent())
        with pytest.raises(WebhookIgnored):
            await provider().parse_webhook(build(body(payment_status="weird")))
    assert API_KEY not in caplog.text and SECRET_KEY not in caplog.text
