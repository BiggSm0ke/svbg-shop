"""cisPay plugin: the shared TestKit plus protocol vectors from ``docs/providers/cispay.md``."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from svbg.payments.providers.cispay import (
    CARD_MIN_MINOR,
    DEFAULT_BASE_URL,
    STATUS_MAP,
    CisPay,
    webhook_signature,
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
from tests.fakes.cispay import API_KEY, FAKE_BASE_URL, SHOP_ID, FakeCisPay, sign
from tests.payments.providers.conftest import HarnessFactory, outcomes, payment_row

CONFIG = {"shop_id": SHOP_ID, "api_key": API_KEY, "base_url": FAKE_BASE_URL}
PID = "0190f5d2-7b1e-7c3a-9d4e-5f6a7b8c9d0e"
TX = "0192a0b0-0000-7000-8000-000000000001"

#: spec §6: the documentation's example body with substituted values, one line, 381 bytes.
VECTOR_BODY = (
    b'{"id":"0192a0b0-0000-7000-8000-000000000001","store_id":"0192a0b0-0000-7000-8000-0000000000aa",'
    b'"order_id":"order-10293","payment_method":"CARD","status":"PAID","amount":100000,"currency":"RUB",'
    b'"charged_amount":100000,"merchant_revenue":96500,"is_sandbox":false,"description":null,'
    b'"subscription_id":null,"paid_at":"2026-07-13T10:00:00+00:00","timestamp":"2026-07-13T10:00:01+00:00"}'
)
VECTOR_SIGN = "617d6234e9ac64fb0c63f176429cfbd23e5a38176479ed825eb3d404371f31bd"
VECTOR_TAMPERED_SIGN = "c856f04496f23a588bd4c2208af9b40a76bebea8628e652cb58e81d0aad36e66"

#: SDK state → cisPay status. cisPay has no chargeback status: a refund stands in for it.
_STATUS = {
    PaymentState.CREATED: "PENDING",
    PaymentState.PROCESSING: "PENDING",
    PaymentState.PAID: "PAID",
    PaymentState.EXPIRED: "EXPIRED",
    PaymentState.CANCELED: "FAILED",
    PaymentState.FAILED: "FAILED",
    PaymentState.CHARGEBACK: "REFUNDED",
    PaymentState.REFUNDED: "REFUNDED",
}


def payload(**kw: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": TX,
        "store_id": SHOP_ID,
        "order_id": PID,
        "payment_method": "SBP",
        "status": "PAID",
        "amount": 17_900,
        "currency": "RUB",
        "charged_amount": 18_527,
        "merchant_revenue": 17_273,
        "is_sandbox": False,
        "description": "Пополнение баланса",
        "subscription_id": None,
        "paid_at": "2026-10-02T09:59:58+00:00",
        "payload": PID,
        "timestamp": "2026-10-02T10:00:01+00:00",
    }
    data.update(kw)
    return {k: v for k, v in data.items() if v is not ...}


def build(data: dict[str, Any] | bytes, *, key: str = API_KEY, **headers: str) -> WebhookRequest:
    body = data if isinstance(data, bytes) else json.dumps(data, separators=(",", ":")).encode()
    return WebhookRequest(body=body, headers={"X-Signature": sign(body, key), **headers})


class CisPayVectors:
    """:class:`~svbg.payments.testkit.WebhookVectors` of cisPay: the transaction ``id`` is the external id,
    ``order_id``/``payload`` carry our payment id; a test event is a sandbox webhook (``is_sandbox``)."""

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
        kopecks = int(Decimal(amount) * 100)
        return build(
            payload(
                id=external_id,
                order_id=payment_id,
                payload=payment_id,
                status=_STATUS[state],
                amount=kopecks,
                charged_amount=kopecks,
                currency=currency,
                is_sandbox=test,
                timestamp=signed_at.astimezone(UTC).isoformat(),
            )
        )

    def tamper(self, req: WebhookRequest) -> WebhookRequest:
        return WebhookRequest(body=req.body.replace(b'"amount":', b'"amount":1'), headers=dict(req.headers))

    def strip_auth(self, req: WebhookRequest) -> WebhookRequest:
        return WebhookRequest(body=req.body, headers={})


def provider(http: CountingHttp | None = None, **config: Any) -> CisPay:
    p = make_provider(CisPay, {**CONFIG, **config}, http=http)
    assert isinstance(p, CisPay)
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
    check_static(CisPay)
    caps = CisPay.capabilities
    assert caps.webhook_auth is WebhookAuth.SIGNATURE and caps.replay_window_s is None
    assert caps.fetch_status and not caps.batch_status and caps.refund and not caps.recurring
    assert CisPay.manifest.currencies == ("RUB",)
    assert set(CisPay.manifest.method_kinds) == {MethodKind.CARD, MethodKind.SBP}
    assert CisPay.manifest.min_minor is None  # SBP has no minimum; CARD's 50 ₽ is checked in create()
    for name, fld in CisPay.manifest.config.fields().items():
        assert fld.title and fld.description and fld.where, name
    secrets_ = {n for n, f in CisPay.manifest.config.fields().items() if f.is_secret}
    assert secrets_ == {"api_key"}


async def test_testkit_plugin_level() -> None:
    await check_plugin(CisPay, CONFIG, CisPayVectors())


async def test_testkit_core_level(make_harness: HarnessFactory, db: CountingDatabase) -> None:
    harness = await make_harness(CisPay, CONFIG, http=CountingHttp(FakeCisPay()))
    await check_core(harness, CisPayVectors())
    assert len(harness.credited) == 3 and len(harness.refunded) == 1
    assert "test_rejected" in await outcomes(db)


@pytest.mark.parametrize("domain", [True, False], ids=["domain", "no-domain"])
async def test_testkit_poll_budget(make_harness: HarnessFactory, domain: bool) -> None:
    desk = FakeCisPay()
    harness = await make_harness(CisPay, CONFIG, http=CountingHttp(desk), has_domain=domain)
    used = await check_poll_budget(harness, domain=domain, invoices=3)
    assert used <= (3 if domain else 24) * 3
    assert all(r[1] == "/payments/status" for r in desk.requests)


# --------------------------------------------------------------------------------- spec §6 vectors


def test_signature_vector() -> None:
    assert len(VECTOR_BODY) == 381
    assert webhook_signature(API_KEY, VECTOR_BODY) == VECTOR_SIGN
    tampered = VECTOR_BODY.replace(b'"merchant_revenue":96500', b'"merchant_revenue":96501')
    assert webhook_signature(API_KEY, tampered) == VECTOR_TAMPERED_SIGN


async def test_vector_is_accepted_and_tampered_body_is_401() -> None:
    plugin = provider(shop_id="0192a0b0-0000-7000-8000-0000000000aa")
    event = await plugin.parse_webhook(WebhookRequest(body=VECTOR_BODY, headers={"X-Signature": VECTOR_SIGN}))
    assert event.state is PaymentState.PAID and event.external_id == TX
    assert event.amount == Decimal("1000") and event.currency == "RUB"
    assert event.payment_id is None  # «order-10293» is not one of our ids
    tampered = VECTOR_BODY.replace(b'"merchant_revenue":96500', b'"merchant_revenue":96501')
    with pytest.raises(WebhookRejected) as err:
        await plugin.parse_webhook(WebhookRequest(body=tampered, headers={"X-Signature": VECTOR_SIGN}))
    assert err.value.status == 401


async def test_upper_case_signature_is_accepted() -> None:
    req = WebhookRequest(body=VECTOR_BODY, headers={"X-Signature": VECTOR_SIGN.upper()})
    assert (await provider().parse_webhook(req)).external_id == TX


# ---------------------------------------------------------------------------------------- webhooks


async def test_paid_webhook_fields() -> None:
    event = await provider().parse_webhook(build(payload()))
    assert event.state is PaymentState.PAID and event.external_id == TX and event.payment_id == PID
    assert event.amount == Decimal("179") and event.currency == "RUB" and not event.is_test
    assert event.paid_at == datetime(2026, 10, 2, 9, 59, 58, tzinfo=UTC)
    assert event.signed_at == datetime(2026, 10, 2, 10, 0, 1, tzinfo=UTC)  # logged only
    assert event.summary["charged_amount"] == "18527" and event.summary["method"] == "SBP"


async def test_net_amount_is_reconciled_not_the_charged_one() -> None:
    event = await provider().parse_webhook(build(payload(amount=17_900, charged_amount=18_527)))
    assert event.amount == Decimal("179")


@pytest.mark.parametrize(("raw", "state"), list(STATUS_MAP.items()))
async def test_every_status(raw: str, state: PaymentState) -> None:
    event = await provider().parse_webhook(build(payload(status=raw, paid_at=None)))
    assert event.state is state and event.summary["status"] == raw


async def test_payload_absent_or_present() -> None:
    event = await provider().parse_webhook(build(payload(payload=...)))
    assert event.payment_id == PID  # from order_id
    other = "0190f5d2-7b1e-7c3a-9d4e-000000000002"
    event = await provider().parse_webhook(build(payload(payload=other, order_id="legacy")))
    assert event.payment_id == other


async def test_sandbox_webhook_is_test() -> None:
    event = await provider().parse_webhook(build(payload(is_sandbox=True)))
    assert event.is_test and event.summary["is_sandbox"] is True


@pytest.mark.parametrize(
    "headers",
    [{}, {"X-Signature": ""}, {"X-Signature": "zz" * 32}, {"X-Signature": "ab"}],
)
async def test_missing_or_garbage_signature_is_401(headers: dict[str, str]) -> None:
    body = json.dumps(payload(), separators=(",", ":")).encode()
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(WebhookRequest(body=body, headers=headers))
    assert err.value.status == 401


async def test_reserialized_json_is_rejected() -> None:
    """«Байты как есть»: the same JSON with spaces (``json.dumps`` defaults) does not verify."""
    req = build(payload())
    spaced = json.dumps(json.loads(req.body)).encode()
    assert spaced != req.body
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(WebhookRequest(body=spaced, headers=dict(req.headers)))
    assert err.value.status == 401


async def test_other_key_and_other_shop_are_401() -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(build(payload(), key="cis_sec_other"))
    assert err.value.status == 401
    with pytest.raises(WebhookRejected, match="another shop") as err:
        await provider().parse_webhook(build(payload(store_id="0192a0b0-0000-7000-8000-0000000000bb")))
    assert err.value.status == 401


async def test_unknown_status_and_subscription_cycles_are_acknowledged() -> None:
    for data in (payload(status="WEIRD"), payload(subscription_id="0192a0b0-0000-7000-8000-00000000cafe")):
        with pytest.raises(WebhookIgnored) as err:
            await provider().parse_webhook(build(data))
        assert err.value.response.status == 200


@pytest.mark.parametrize(
    "data",
    [
        payload(amount="179"),
        payload(amount=179.5),
        payload(amount=None),
        payload(id=None),
        payload(currency="RUBLES"),
    ],
)
async def test_malformed_authentic_bodies_are_400(data: dict[str, Any]) -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(build(data))
    assert err.value.status == 400


async def test_non_json_body_is_400() -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(build(b"not json"))
    assert err.value.status == 400


async def test_every_single_byte_change_is_rejected() -> None:
    req = build(payload())
    plugin = provider()
    for i in range(0, len(req.body), 4):
        mutated = bytearray(req.body)
        mutated[i] ^= 0x01
        with pytest.raises(WebhookRejected):
            await plugin.parse_webhook(WebhookRequest(body=bytes(mutated), headers=dict(req.headers)))


# ------------------------------------------------------------------------------------------ create


async def test_create_request_shape_and_privacy() -> None:
    desk = FakeCisPay()
    http = CountingHttp(desk)
    checkout = await provider(http).create(
        intent(return_url="https://t.me/svbg_bot", method_hint=MethodKind.SBP)
    )
    body = desk.created[0]
    assert body == {
        "amount": 17_900,
        "currency": "RUB",
        "order_id": PID,
        "payment_method": "SBP",
        "description": "Пополнение баланса на 179 ₽",
        "payload": PID,
        "customer_id": "c0ffee",
        "redirect_success_url": "https://t.me/svbg_bot",
        "redirect_fail_url": "https://t.me/svbg_bot",
    }
    call = http.calls[0]
    assert call.headers["X-Shop-ID"] == SHOP_ID and call.headers["X-Api-Key"] == API_KEY
    tx = desk.by_order(PID)
    assert checkout.kind == "url" and checkout.external_id == tx.id and checkout.pay_url == tx.payment_url


async def test_card_sends_no_customer_id_and_has_a_minimum() -> None:
    desk = FakeCisPay()
    http = CountingHttp(desk)
    await provider(http).create(intent(method_hint=MethodKind.CARD, amount_minor=CARD_MIN_MINOR))
    assert desk.created[0]["payment_method"] == "CARD" and "customer_id" not in desk.created[0]
    with pytest.raises(ProviderError, match="от 50"):
        await provider(http).create(
            intent(
                payment_id="0190f5d2-7b1e-7c3a-9d4e-000000000003",
                method_hint=MethodKind.CARD,
                amount_minor=4999,
            )
        )
    assert http.requests == 1


@pytest.mark.parametrize(
    ("methods", "default", "expected"),
    [
        (("CARD", "SBP"), "auto", "SBP"),
        (("CARD",), "auto", "CARD"),
        (("CARD", "SBP"), "card", "CARD"),
        (("SBP",), "sbp", "SBP"),
    ],
)
async def test_method_without_a_hint(methods: tuple[str, ...], default: str, expected: str) -> None:
    desk = FakeCisPay(methods=methods)
    http = CountingHttp(desk)
    plugin = provider(http, default_method=default)
    await plugin.create(intent())
    await plugin.create(intent(payment_id="0190f5d2-7b1e-7c3a-9d4e-000000000004"))
    assert [c["payment_method"] for c in desk.created] == [expected, expected]
    caps_calls = sum(1 for r in desk.requests if r[1] == "/store/capabilities")
    assert caps_calls == (1 if default == "auto" else 0)  # cached


async def test_no_active_method() -> None:
    with pytest.raises(ProviderError, match="нет активного"):
        await provider(CountingHttp(FakeCisPay(methods=()))).create(intent())


async def test_inactive_method_is_403() -> None:
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(FakeCisPay(methods=("CARD",)))).create(intent(method_hint=MethodKind.SBP))
    assert err.value.status == 403 and not err.value.retryable


async def test_configured_return_url_is_the_fallback() -> None:
    desk = FakeCisPay()
    await provider(CountingHttp(desk), return_url="https://t.me/shop_bot").create(intent())
    assert desk.created[0]["redirect_success_url"] == "https://t.me/shop_bot"


async def test_create_refuses_foreign_currency() -> None:
    http = CountingHttp(FakeCisPay())
    with pytest.raises(ProviderError, match="только RUB"):
        await provider(http).create(intent(currency="USD"))
    assert http.requests == 0


async def test_repeated_order_id_is_looked_up() -> None:
    desk = FakeCisPay()
    plugin = provider(CountingHttp(desk))
    first = await plugin.create(intent(method_hint=MethodKind.SBP))
    with pytest.raises(ProviderError, match="уже занят") as err:
        await plugin.create(intent(method_hint=MethodKind.SBP))
    assert first.external_id and first.external_id[:20] in err.value.human and not err.value.retryable
    assert desk.status_calls[-1] == {"order_id": PID}


@pytest.mark.parametrize(
    ("status", "retryable"), [(401, False), (403, False), (422, False), (429, True), (500, True), (503, True)]
)
async def test_create_errors(status: int, retryable: bool) -> None:
    desk = FakeCisPay()
    desk.fail_with = status
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(desk)).create(intent(method_hint=MethodKind.SBP))
    assert err.value.retryable is retryable and err.value.status == status
    assert API_KEY not in err.value.human


async def test_validation_detail_is_shown() -> None:
    def answer(_call: Any) -> HttpResponse:
        body = {"detail": [{"loc": ["body", "amount"], "msg": "Input should be greater than 0", "type": "x"}]}
        return HttpResponse(422, json.dumps(body).encode())

    with pytest.raises(ProviderError, match="amount: Input should be greater than 0"):
        await provider(CountingHttp(answer)).create(intent(method_hint=MethodKind.SBP))


async def test_create_with_a_bad_answer() -> None:
    def junk(_call: Any) -> HttpResponse:
        return HttpResponse(201, b'{"id": "x"}')

    with pytest.raises(ProviderError):
        await provider(CountingHttp(junk)).create(intent(method_hint=MethodKind.SBP))


# ------------------------------------------------------------------------------------------ status


async def test_fetch_status() -> None:
    desk = FakeCisPay()
    plugin = provider(CountingHttp(desk))
    checkout = await plugin.create(intent(method_hint=MethodKind.SBP))
    assert checkout.external_id
    [st] = await plugin.fetch_status([checkout.external_id, "0192a0b0-0000-7000-8000-00000000dead"])
    assert st.state is PaymentState.CREATED and st.payment_id == PID
    desk.pay(PID)
    [st] = await plugin.fetch_status([checkout.external_id])
    assert st.state is PaymentState.PAID and st.amount == Decimal("179") and st.paid_at is not None
    assert desk.status_calls[0] == {"id": checkout.external_id}


@pytest.mark.parametrize(("raw", "state"), list(STATUS_MAP.items()))
async def test_fetch_status_states(raw: str, state: PaymentState) -> None:
    desk = FakeCisPay()
    plugin = provider(CountingHttp(desk))
    checkout = await plugin.create(intent(method_hint=MethodKind.SBP))
    desk.pay(PID, status=raw)
    [st] = await plugin.fetch_status([checkout.external_id or ""])
    assert st.state is state and not st.is_test


async def test_blocked_buyer_is_failed_not_test() -> None:
    desk = FakeCisPay()
    plugin = provider(CountingHttp(desk))
    checkout = await plugin.create(intent(method_hint=MethodKind.SBP))
    tx = desk.pay(PID)
    tx.block_reason = "Подозрение на мошенничество"
    [st] = await plugin.fetch_status([checkout.external_id or ""])
    assert st.state is PaymentState.FAILED and not st.is_test


async def test_sandbox_status_is_test() -> None:
    desk = FakeCisPay()
    plugin = provider(CountingHttp(desk))
    checkout = await plugin.create(intent(method_hint=MethodKind.SBP))
    desk.pay(PID, sandbox=True)
    [st] = await plugin.fetch_status([checkout.external_id or ""])
    assert st.state is PaymentState.PAID and st.is_test


@pytest.mark.parametrize(("status", "retryable"), [(401, False), (429, True), (502, True), (400, False)])
async def test_fetch_status_errors(status: int, retryable: bool) -> None:
    desk = FakeCisPay()
    desk.fail_with = status
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(desk)).fetch_status([TX])
    assert err.value.retryable is retryable


# ------------------------------------------------------------------------------------------ refund


async def test_full_refund_confirms_the_charged_amount() -> None:
    desk = FakeCisPay()
    plugin = provider(CountingHttp(desk))
    checkout = await plugin.create(intent(method_hint=MethodKind.SBP))
    tx = desk.pay(PID)
    tx.charged_amount = 18_527  # the commission is passed on to the buyer
    assert not (await plugin.refund(tx.id, 10_000, "RUB")).ok  # partial: refused before any refund call
    result = await plugin.refund(checkout.external_id or "", 17_900, "RUB")
    assert result.ok and result.external_id == tx.id
    assert desk.refunds == [{"id": tx.id, "confirm_amount": 18_527}]
    assert not any(r[1] == "/payouts" for r in desk.requests)


async def test_refund_failures() -> None:
    desk = FakeCisPay()
    plugin = provider(CountingHttp(desk))
    assert not (await plugin.refund(TX, 17_900, "RUB")).ok  # unknown
    checkout = await plugin.create(intent(method_hint=MethodKind.SBP))
    result = await plugin.refund(checkout.external_id or "", 17_900, "RUB")  # not paid
    assert not result.ok and "HTTP 400" in result.message
    assert not (await plugin.refund(checkout.external_id or "", 17_900, "USD")).ok


# ------------------------------------------------------------------------------------------- probe


async def test_test_credentials() -> None:
    desk = FakeCisPay()
    probe = await provider(CountingHttp(desk)).test_credentials()
    assert probe.ok and "CARD, SBP" in probe.message and probe.details["methods"] == ["CARD", "SBP"]
    assert desk.created == []
    bad = await provider(CountingHttp(FakeCisPay(api_key="cis_sec_other"))).test_credentials()
    assert not bad.ok and "API-ключ" in bad.message
    other = await provider(
        CountingHttp(FakeCisPay(shop_id="0192a0b0-0000-7000-8000-0000000000bb")),
        shop_id="0192a0b0-0000-7000-8000-0000000000bb",
    ).test_credentials()
    assert other.ok
    none = await provider(CountingHttp(FakeCisPay(methods=()))).test_credentials()
    assert not none.ok and "нет активных" in none.message

    def html(_call: Any) -> HttpResponse:
        return HttpResponse(404, b"<html>nope</html>")

    assert "не cisPay" in (await provider(CountingHttp(html)).test_credentials()).message


def test_config_defaults_and_secrets() -> None:
    cfg = CisPay.manifest.config.parse({"SHOP_ID": SHOP_ID, "API_KEY": API_KEY})
    assert cfg.base_url == DEFAULT_BASE_URL == "https://api.cispay.app"
    assert cfg.default_method == "auto" and cfg.return_url is None
    assert API_KEY not in repr(cfg) and cfg.secret_values() == [API_KEY]
    with pytest.raises(ValueError):
        CisPay.manifest.config.parse({"SHOP_ID": "not-a-uuid", "API_KEY": API_KEY})


async def test_logs_carry_no_secrets(caplog: pytest.LogCaptureFixture) -> None:
    desk = FakeCisPay()
    with caplog.at_level(logging.DEBUG):
        plugin = provider(CountingHttp(desk))
        await plugin.create(intent(method_hint=MethodKind.SBP))
        desk.fail_with = 400
        with pytest.raises(ProviderError):
            await plugin.fetch_status([TX])
    assert API_KEY not in caplog.text


# --------------------------------------------------------------------------------------- core level


async def test_end_to_end_over_real_http(make_harness: HarnessFactory, db: CountingDatabase) -> None:
    """Real aiohttp client against the fake server: create, webhook, retried webhook, sandbox, status."""
    async with FakeCisPay() as desk:
        http = InstanceHttp()
        try:
            harness = await make_harness(CisPay, {**CONFIG, "base_url": desk.base_url}, http=http)
            result = await harness.core.create_payment(
                user_id=harness.user_id,
                instance_id=harness.instance.id,
                amount_minor=17_900,
                currency="RUB",
                description="Пополнение баланса",
            )
            tx = desk.by_order(result.payment_id)
            assert result.checkout.external_id == tx.id
            row = await payment_row(db, result.payment_id)
            assert row["external_id"] == tx.id
            sandbox = desk.pay(result.payment_id, sandbox=True)
            assert await harness.send(desk.webhook(sandbox)) == 400  # a sandbox event on a live instance
            assert await harness.status(result.payment_id) == "pending"
            paid = desk.pay(result.payment_id)
            req = desk.webhook(paid)
            assert await harness.send(req) == 200
            assert await harness.status(result.payment_id) == "paid"
            for _ in range(4):  # retries after 1, 5, 15, 60 minutes: deduplicated
                assert await harness.send(req) == 200
            await harness.core.verify(harness.instance.id, [(tx.id, result.payment_id)])
            assert len(harness.credited) == 1
            assert (await harness.instance.provider.test_credentials()).ok
        finally:
            await http.close()


async def test_lost_webhook_is_recovered_by_status_check(make_harness: HarnessFactory) -> None:
    desk = FakeCisPay()
    harness = await make_harness(CisPay, CONFIG, http=CountingHttp(desk))
    result = await harness.core.create_payment(
        user_id=harness.user_id,
        instance_id=harness.instance.id,
        amount_minor=17_900,
        currency="RUB",
        description="x",
    )
    desk.pay(result.payment_id)
    await harness.core.verify(harness.instance.id, [(None, result.payment_id)])
    assert await harness.status(result.payment_id) == "paid"


async def test_test_event_on_sandbox_instance_is_accepted(make_harness: HarnessFactory) -> None:
    harness = await make_harness(CisPay, CONFIG, http=CountingHttp(FakeCisPay()), is_test=True)
    pid = await harness.pending()
    req = CisPayVectors().webhook(
        PaymentState.PAID,
        payment_id=pid,
        external_id="0192a0b0-0000-7000-8000-00000000beef",
        amount="179",
        currency="RUB",
        signed_at=datetime.now(UTC),
        test=True,
    )
    assert await harness.send(req) == 200
    assert await harness.status(pid) == "paid"
