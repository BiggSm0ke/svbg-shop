"""ЮKassa plugin: the shared TestKit plus protocol vectors from ``docs/providers/yookassa.md``."""

from __future__ import annotations

import base64
import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from svbg.payments.core import CheckoutError
from svbg.payments.providers.yookassa import (
    DEFAULT_BASE_URL,
    YOOKASSA_NETWORKS,
    YooKassa,
    client_ip,
    parse_networks,
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
    ConfigError,
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
from tests.fakes.yookassa import (
    FAKE_BASE_URL,
    SECRET_KEY,
    SHOP_ID,
    YOOKASSA_IP,
    YOOKASSA_IP6,
    FakeYooKassa,
)
from tests.payments.providers.conftest import HarnessFactory, outcomes, payment_row

CONFIG: dict[str, Any] = {
    "shop_id": SHOP_ID,
    "secret_key": SECRET_KEY,
    "base_url": FAKE_BASE_URL,
    "return_url": "https://t.me/svbg_test_bot",
}
PID = "0190f5d2-7b1e-7c3a-9d4e-5f6a7b8c9d0e"
EXT = "2e7a1c3d-000f-5000-9000-1b2c3d4e5f60"
FOREIGN_IP = "203.0.113.7"


@pytest.fixture(autouse=True)
def _no_retry_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(YooKassa, "retry_delay_s", 0.0)


def payment_obj(
    state: PaymentState | str,
    *,
    ext: str = EXT,
    pid: str | None = PID,
    amount: Any = "179.00",
    currency: str = "RUB",
    test: bool = False,
) -> dict[str, Any]:
    status = {
        PaymentState.CREATED: "pending",
        PaymentState.PROCESSING: "waiting_for_capture",
        PaymentState.PAID: "succeeded",
        PaymentState.CANCELED: "canceled",
        PaymentState.EXPIRED: "canceled",
        PaymentState.FAILED: "canceled",
    }.get(state, str(state))  # type: ignore[call-overload]
    obj: dict[str, Any] = {
        "id": ext,
        "status": status,
        "paid": status == "succeeded",
        "amount": {"value": amount, "currency": currency},
        "metadata": {"payment_id": pid} if pid else {},
        "test": test,
        "created_at": "2026-10-02T09:00:00.000Z",
    }
    if status == "succeeded":
        obj["captured_at"] = "2026-10-02T09:01:00.000Z"
    if state is PaymentState.EXPIRED:
        obj["cancellation_details"] = {"party": "yoo_money", "reason": "expired_on_confirmation"}
    if state in (PaymentState.CANCELED, PaymentState.FAILED):
        obj["cancellation_details"] = {"party": "payment_network", "reason": "insufficient_funds"}
    return obj


def note(
    obj: dict[str, Any],
    event: str | None = None,
    *,
    remote: str | None = YOOKASSA_IP,
    headers: dict[str, str] | None = None,
) -> WebhookRequest:
    body = json.dumps(
        {"type": "notification", "event": event or f"payment.{obj['status']}", "object": obj}
    ).encode()
    return WebhookRequest(body=body, headers=headers or {}, remote=remote)


class YooVectors:
    """:class:`~svbg.payments.testkit.WebhookVectors` of ЮKassa: «authentic» = sent from a ЮKassa network."""

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
        if state in (PaymentState.CHARGEBACK, PaymentState.REFUNDED):
            obj = {"id": "rf-1", "payment_id": external_id, "status": "succeeded", "test": test}
            return note(obj, "refund.succeeded")
        obj = payment_obj(state, ext=external_id, pid=payment_id, amount=amount, currency=currency, test=test)
        return note(obj)

    def tamper(self, req: WebhookRequest) -> WebhookRequest:
        return WebhookRequest(body=req.body, headers=dict(req.headers), remote=FOREIGN_IP)

    def strip_auth(self, req: WebhookRequest) -> WebhookRequest:
        return WebhookRequest(body=req.body, headers=dict(req.headers), remote=None)


def provider(http: CountingHttp | None = None, *, is_test: bool = False, **config: Any) -> YooKassa:
    p = make_provider(YooKassa, {**CONFIG, **config}, http=http, is_test=is_test)
    assert isinstance(p, YooKassa)
    return p


def intent(**kw: Any) -> PaymentIntent:
    values: dict[str, Any] = {
        "payment_id": PID,
        "amount_minor": 17_900,
        "currency": "RUB",
        "description": "Пополнение баланса на 179 ₽",
        "customer_ref": "c0ffee",
        "return_url": "https://t.me/svbg_bot?start=paid",
    }
    values.update(kw)
    return PaymentIntent(**values)


def pending_responder(kassa: FakeYooKassa) -> Any:
    """Every unknown id is a «pending» payment on the kassa side (abandoned checkouts)."""

    async def respond(call: Any) -> Any:
        ext = call.url.rsplit("/", 1)[-1]
        if call.method == "GET" and ext not in kassa.payments and ext != "me":
            kassa.add_payment(ext, PID)
        return await kassa(call)

    return respond


# ------------------------------------------------------------------------------------------- TestKit


def test_static_contract() -> None:
    check_static(YooKassa)
    caps = YooKassa.capabilities
    assert caps.webhook_auth is WebhookAuth.IP_ONLY and caps.webhook_auth.is_weak
    assert caps.fetch_status and not caps.batch_status and caps.replay_window_s is None
    assert caps.refund and caps.receipt_54fz and caps.redirect
    assert YooKassa.manifest.method_kinds == (MethodKind.SBP, MethodKind.CARD)
    assert YooKassa.manifest.min_minor == 100
    fields = YooKassa.manifest.config.fields()
    assert fields["secret_key"].is_secret and not fields["shop_id"].is_secret
    for name, fld in fields.items():
        if not fld.advanced or name in ("shop_id", "secret_key"):
            assert fld.where, f"{name} has no «где взять»"


async def test_testkit_plugin_level() -> None:
    await check_plugin(YooKassa, CONFIG, YooVectors())


async def test_testkit_core_level(make_harness: HarnessFactory, db: CountingDatabase) -> None:
    harness = await make_harness(YooKassa, CONFIG, http=CountingHttp(FakeYooKassa()))
    await check_core(harness, YooVectors())
    assert {"bad_signature", "test_rejected"} <= set(await outcomes(db))
    assert harness.credited == []


@pytest.mark.parametrize("domain", [True, False], ids=["domain", "no-domain"])
async def test_testkit_poll_budget(make_harness: HarnessFactory, domain: bool) -> None:
    kassa = FakeYooKassa()
    http = CountingHttp(pending_responder(kassa))
    harness = await make_harness(YooKassa, CONFIG, http=http, has_domain=domain)
    used = await check_poll_budget(harness, domain=domain)
    assert used <= (3 if domain else 24)
    assert all(r[0] == "GET" for r in kassa.requests)


async def test_poll_budget_is_per_invoice_without_batch(make_harness: HarnessFactory) -> None:
    kassa = FakeYooKassa()
    harness = await make_harness(
        YooKassa, CONFIG, http=CountingHttp(pending_responder(kassa)), has_domain=True
    )
    used = await check_poll_budget(harness, domain=True, invoices=3)
    assert used <= 9


# --------------------------------------------------------------------------------- webhook: source IP


@pytest.mark.parametrize(
    "remote",
    [YOOKASSA_IP, "185.71.77.31", "77.75.156.11", "77.75.154.200", YOOKASSA_IP6, f"::ffff:{YOOKASSA_IP}"],
)
async def test_notifications_from_yookassa_networks_are_accepted(remote: str) -> None:
    event = await provider().parse_webhook(note(payment_obj(PaymentState.PAID), remote=remote))
    assert event.state is PaymentState.PAID and event.external_id == EXT


@pytest.mark.parametrize(
    "remote", [FOREIGN_IP, "185.71.76.32", "77.75.156.12", "2a02:5181::1", "127.0.0.1", "", "garbage", None]
)
async def test_notifications_from_elsewhere_are_403(remote: str | None) -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(note(payment_obj(PaymentState.PAID), remote=remote))
    assert err.value.status == 403


async def test_forwarded_headers_from_an_untrusted_peer_are_ignored() -> None:
    req = note(
        payment_obj(PaymentState.PAID),
        remote=FOREIGN_IP,
        headers={"X-Forwarded-For": YOOKASSA_IP, "X-Real-IP": YOOKASSA_IP},
    )
    with pytest.raises(WebhookRejected):
        await provider().parse_webhook(req)


@pytest.mark.parametrize(
    ("headers", "ok"),
    [
        ({"X-Forwarded-For": YOOKASSA_IP}, True),
        ({"X-Forwarded-For": f"{FOREIGN_IP}, {YOOKASSA_IP}"}, True),  # client junk on the left
        ({"X-Forwarded-For": f"{YOOKASSA_IP}, {FOREIGN_IP}"}, False),  # spoofed left part
        ({"X-Forwarded-For": f"{YOOKASSA_IP}, 127.0.0.1"}, True),  # a chain of trusted proxies
        ({"X-Forwarded-For": f"{YOOKASSA_IP}:5443"}, True),
        ({"X-Forwarded-For": "nonsense"}, False),
        ({"X-Real-IP": YOOKASSA_IP}, True),
        ({"X-Real-IP": FOREIGN_IP}, False),
        ({}, False),  # the proxy itself is not ЮKassa
    ],
)
async def test_trusted_proxy_forwarding(headers: dict[str, str], ok: bool) -> None:
    req = note(payment_obj(PaymentState.PAID), remote="127.0.0.1", headers=headers)
    if ok:
        assert (await provider().parse_webhook(req)).state is PaymentState.PAID
    else:
        with pytest.raises(WebhookRejected):
            await provider().parse_webhook(req)


async def test_docker_proxy_network_is_configurable() -> None:
    req = note(payment_obj(PaymentState.PAID), remote="172.18.0.5", headers={"X-Forwarded-For": YOOKASSA_IP})
    with pytest.raises(WebhookRejected):
        await provider().parse_webhook(req)
    plugin = provider(trusted_proxies="127.0.0.1, 172.16.0.0/12")
    assert (await plugin.parse_webhook(req)).state is PaymentState.PAID


async def test_ip_check_can_be_switched_off_and_networks_overridden() -> None:
    req = note(payment_obj(PaymentState.PAID), remote=FOREIGN_IP)
    assert (await provider(verify_ip=False).parse_webhook(req)).state is PaymentState.PAID
    assert (await provider(allowed_networks="203.0.113.0/24").parse_webhook(req)).external_id == EXT
    with pytest.raises(WebhookRejected):
        await provider(allowed_networks="203.0.113.0/24").parse_webhook(note(payment_obj(PaymentState.PAID)))


def test_network_helpers() -> None:
    assert len(parse_networks(", ".join(YOOKASSA_NETWORKS))) == len(YOOKASSA_NETWORKS)
    with pytest.raises(ValueError, match="does not appear"):
        parse_networks("10.0.0.0/8, 300.1.1.1")
    with pytest.raises(ConfigError) as err:
        provider(trusted_proxies="10.0.0.0/33")
    assert "trusted_proxies" in err.value.errors
    trusted = parse_networks("127.0.0.1/32")
    req = WebhookRequest(body=b"", headers={"x-forwarded-for": "[2a02:5180::5]:443"}, remote="127.0.0.1")
    assert str(client_ip(req, trusted)) == "2a02:5180::5"


# ------------------------------------------------------------------------------ webhook: parsing


@pytest.mark.parametrize("amount", ["179", "179.00", "179.0", 179, 179.0])
async def test_amount_forms_are_the_same_decimal(amount: Any) -> None:
    event = await provider().parse_webhook(note(payment_obj(PaymentState.PAID, amount=amount)))
    assert event.amount == Decimal("179") and event.currency == "RUB"


async def test_paid_notification_fields() -> None:
    event = await provider().parse_webhook(note(payment_obj(PaymentState.PAID)))
    assert event.payment_id == PID and event.external_id == EXT
    assert event.paid_at == datetime(2026, 10, 2, 9, 1, tzinfo=UTC)
    assert event.signed_at is None and not event.is_test
    assert event.summary["event"] == "payment.succeeded"


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (PaymentState.CREATED, PaymentState.CREATED),
        (PaymentState.PROCESSING, PaymentState.PROCESSING),  # waiting_for_capture
        (PaymentState.EXPIRED, PaymentState.EXPIRED),  # canceled / expired_on_confirmation
        (PaymentState.CANCELED, PaymentState.CANCELED),  # canceled / insufficient_funds
    ],
)
async def test_status_mapping(state: PaymentState, expected: PaymentState) -> None:
    event = await provider().parse_webhook(note(payment_obj(state)))
    assert event.state is expected


async def test_refund_notification_points_at_the_payment() -> None:
    obj = {
        "id": "rf-1",
        "payment_id": EXT,
        "status": "succeeded",
        "amount": {"value": "179.00", "currency": "RUB"},
    }
    event = await provider().parse_webhook(note(obj, "refund.succeeded"))
    assert event.state is PaymentState.REFUNDED and event.external_id == EXT and event.amount is None


async def test_test_shop_flag() -> None:
    event = await provider().parse_webhook(note(payment_obj(PaymentState.PAID, test=True)))
    assert event.is_test


async def test_foreign_metadata_is_not_a_payment_id() -> None:
    obj = payment_obj(PaymentState.PAID, pid=None)
    obj["metadata"] = {"payment_id": "tc_12345", "order": PID}
    event = await provider().parse_webhook(note(obj))
    assert event.payment_id is None and event.external_id == EXT


@pytest.mark.parametrize("event", ["payout.succeeded", "deal.closed", "payment_method.active"])
async def test_other_events_are_acknowledged(event: str) -> None:
    with pytest.raises(WebhookIgnored):
        await provider().parse_webhook(note({"id": "x"}, event))


async def test_unknown_payment_status_is_acknowledged() -> None:
    with pytest.raises(WebhookIgnored):
        await provider().parse_webhook(note(payment_obj("weird")))


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        b'{"type":"notification","event":"payment.succeeded"}',
        b'{"type":"other","event":"payment.succeeded","object":{"id":"x","status":"succeeded"}}',
        b'{"type":"notification","event":"payment.succeeded","object":{"status":"succeeded"}}',
        b'{"type":"notification","event":"refund.succeeded","object":{"id":"r"}}',
        b'{"type":"notification","event":"payment.succeeded","object":{"id":"x","status":"succeeded",'
        b'"amount":{"value":"abc","currency":"RUB"}}}',
        b'{"type":"notification","event":"payment.succeeded","object":{"id":"x","status":"succeeded",'
        b'"amount":"179"}}',
    ],
)
async def test_malformed_bodies_are_400(body: bytes) -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(WebhookRequest(body=body, remote=YOOKASSA_IP))
    assert err.value.status == 400


# ------------------------------------------------------------------------------------------ create


def _auth(headers: dict[str, str]) -> tuple[str, str]:
    user, _, password = base64.b64decode(headers["authorization"][6:]).decode().partition(":")
    return user, password


async def test_create_request_shape_and_privacy() -> None:
    kassa = FakeYooKassa()
    checkout = await provider(CountingHttp(kassa)).create(intent())
    body = kassa.created[0]
    assert body["amount"] == {"value": "179.00", "currency": "RUB"}
    assert body["capture"] is True
    assert body["confirmation"] == {"type": "redirect", "return_url": "https://t.me/svbg_bot?start=paid"}
    assert body["metadata"] == {"payment_id": PID}
    assert "payment_method_data" not in body and "receipt" not in body
    assert "c0ffee" not in json.dumps(body)
    method, path, headers = kassa.requests[0]
    assert (method, path) == ("POST", "/v3/payments")
    assert headers["idempotence-key"] == PID
    assert _auth(headers) == (SHOP_ID, SECRET_KEY)
    assert checkout.kind == "url" and checkout.external_id in kassa.payments
    assert checkout.pay_url is not None and checkout.pay_url.startswith("https://yoomoney.fake/")


@pytest.mark.parametrize(
    ("hint", "configured", "expected"),
    [
        (MethodKind.SBP, "auto", "sbp"),
        (MethodKind.CARD, "sbp", "bank_card"),
        (None, "sbp", "sbp"),
        (None, "bank_card", "bank_card"),
        (None, "auto", None),
    ],
)
async def test_payment_method_follows_the_button(
    hint: MethodKind | None, configured: str, expected: str | None
) -> None:
    kassa = FakeYooKassa()
    await provider(CountingHttp(kassa), payment_method=configured).create(intent(method_hint=hint))
    sent = kassa.created[0].get("payment_method_data")
    assert sent == ({"type": expected} if expected else None)


async def test_return_url_fallback_and_absence() -> None:
    kassa = FakeYooKassa()
    await provider(CountingHttp(kassa)).create(intent(return_url=None))
    assert kassa.created[0]["confirmation"]["return_url"] == "https://t.me/svbg_test_bot"
    with pytest.raises(ProviderError, match="адрес возврата") as err:
        await provider(CountingHttp(kassa), return_url="").create(intent(return_url=None))
    assert not err.value.retryable


async def test_long_description_is_clipped_to_128() -> None:
    kassa = FakeYooKassa()
    await provider(CountingHttp(kassa)).create(intent(description="Очень длинное описание " * 20))
    assert len(kassa.created[0]["description"]) == 128


@pytest.mark.parametrize("minor", [100, 17_950, 99_999_999, 12_345])
async def test_amount_is_sent_as_an_exact_string(minor: int) -> None:
    kassa = FakeYooKassa()
    await provider(CountingHttp(kassa)).create(intent(amount_minor=minor))
    assert kassa.created[0]["amount"]["value"] == f"{Decimal(minor) / 100:.2f}"


async def test_receipt_54fz_with_email() -> None:
    kassa = FakeYooKassa()
    plugin = provider(
        CountingHttp(kassa),
        receipt_enabled=True,
        receipt_email="shop@example.ru",
        vat_code="11",
        payment_subject="service",
        payment_mode="full_prepayment",
        tax_system_code="2",
        receipt_item_name="Доступ к VPN-сервису",
    )
    await plugin.create(intent(amount_minor=17_950))
    receipt = kassa.created[0]["receipt"]
    assert receipt["customer"] == {"email": "shop@example.ru"}
    assert receipt["tax_system_code"] == 2
    assert receipt["items"] == [
        {
            "description": "Доступ к VPN-сервису",
            "quantity": "1.00",
            "amount": {"value": "179.50", "currency": "RUB"},
            "vat_code": 11,
            "payment_subject": "service",
            "payment_mode": "full_prepayment",
        }
    ]


async def test_receipt_with_phone_and_defaults() -> None:
    kassa = FakeYooKassa()
    plugin = provider(
        CountingHttp(kassa), receipt_enabled=True, receipt_contact="phone", receipt_phone="+79991234567"
    )
    await plugin.create(intent(description="x" * 300))
    receipt = kassa.created[0]["receipt"]
    assert receipt["customer"] == {"phone": "79991234567"}
    item = receipt["items"][0]
    assert item["vat_code"] == 1 and item["payment_subject"] == "service"
    assert item["payment_mode"] == "full_payment" and len(item["description"]) == 128
    assert "tax_system_code" not in receipt


def test_receipt_without_a_contact_is_a_config_error() -> None:
    with pytest.raises(ConfigError) as err:
        provider(receipt_enabled=True)
    assert "receipt_email" in err.value.errors
    with pytest.raises(ConfigError) as err:
        provider(receipt_enabled=True, receipt_contact="phone", receipt_email="a@b.ru")
    assert "receipt_phone" in err.value.errors
    with pytest.raises(ConfigError):
        provider(vat_code="13")
    with pytest.raises(ConfigError):
        provider(receipt_email="not-an-email")


@pytest.mark.parametrize("fault", [500, 202])
async def test_unknown_result_is_retried_with_the_same_key(fault: int) -> None:
    kassa = FakeYooKassa()
    kassa.create_faults = [(fault, b'{"type":"processing","retry_after":1}')]
    http = CountingHttp(kassa)
    checkout = await provider(http).create(intent())
    assert http.requests == 2 and len(kassa.payments) == 1
    keys = {r[2]["idempotence-key"] for r in kassa.requests}
    assert keys == {PID} and checkout.external_id in kassa.payments


async def test_repeated_create_of_one_payment_returns_the_same_invoice() -> None:
    kassa = FakeYooKassa()
    plugin = provider(CountingHttp(kassa))
    first = await plugin.create(intent())
    second = await plugin.create(intent())
    assert first.external_id == second.external_id and len(kassa.payments) == 1


async def test_transport_errors_are_retried_then_reported() -> None:
    kassa = FakeYooKassa()
    calls = 0

    async def flaky(call: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ProviderError("нет связи", retryable=True)
        return await kassa(call)

    assert (await provider(CountingHttp(flaky)).create(intent())).external_id in kassa.payments
    kassa.create_faults = [(500, b"{}")] * 3
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(kassa)).create(intent(payment_id="0190f5d2-7b1e-7c3a-9d4e-000000000001"))
    assert err.value.retryable and err.value.status == 500


@pytest.mark.parametrize(
    ("status", "retryable", "words"),
    [(401, False, "shopId"), (403, False, "запретила"), (429, True, "недоступна"), (502, True, "недоступна")],
)
async def test_create_errors(status: int, retryable: bool, words: str) -> None:
    kassa = FakeYooKassa()
    kassa.fail_with = status
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(kassa)).create(intent())
    assert err.value.retryable is retryable and words in err.value.human
    assert SECRET_KEY not in err.value.human


async def test_invalid_request_shows_the_kassa_reason() -> None:
    answer = HttpResponse(
        400,
        json.dumps(
            {
                "type": "error",
                "code": "invalid_request",
                "description": "Receipt is missing",
                "parameter": "receipt",
            }
        ).encode(),
    )
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(lambda _c: answer)).create(intent())
    assert (
        not err.value.retryable and "Receipt is missing" in err.value.human and "receipt" in err.value.human
    )


async def test_wrong_key_is_reported(caplog: pytest.LogCaptureFixture) -> None:
    kassa = FakeYooKassa(secret_key="live_other")
    with caplog.at_level(logging.DEBUG), pytest.raises(ProviderError) as err:
        await provider(CountingHttp(kassa)).create(intent())
    assert err.value.status == 401 and SECRET_KEY not in caplog.text


@pytest.mark.parametrize(
    "answer",
    [
        b"<html>",
        b'{"id":"x","status":"pending"}',
        b'{"status":"pending","confirmation":{"confirmation_url":"https://x"}}',
        b'{"id":"x","confirmation":{"confirmation_url":"javascript:alert(1)"}}',
    ],
)
async def test_create_with_a_bad_answer(answer: bytes) -> None:
    with pytest.raises(ProviderError):
        await provider(CountingHttp(lambda _c: HttpResponse(200, answer))).create(intent())


# ------------------------------------------------------------------------------------ fetch_status


async def test_fetch_status_one_get_per_invoice() -> None:
    kassa = FakeYooKassa()
    kassa.add_payment("p1", PID, status="succeeded")
    kassa.add_payment("p2", None, status="pending")
    kassa.add_payment("p3", PID, status="canceled").cancel_reason = "expired_on_confirmation"
    http = CountingHttp(kassa)
    result = await provider(http).fetch_status(["p1", "p2", "p3", "p1", "missing", ""])
    assert [(s.external_id, s.state) for s in result] == [
        ("p1", PaymentState.PAID),
        ("p2", PaymentState.CREATED),
        ("p3", PaymentState.EXPIRED),
    ]
    assert http.requests == 4 and all(c.method == "GET" for c in http.calls)
    paid = result[0]
    assert paid.amount == Decimal("179.00") and paid.currency == "RUB" and paid.payment_id == PID
    assert paid.paid_at is not None


async def test_fetch_status_refunds() -> None:
    kassa = FakeYooKassa()
    kassa.add_payment("full", PID, status="succeeded").refunded = "179.00"
    kassa.add_payment("part", PID, status="succeeded").refunded = "50.00"
    result = {
        s.external_id: s.state for s in await provider(CountingHttp(kassa)).fetch_status(["full", "part"])
    }
    assert result == {"full": PaymentState.REFUNDED, "part": PaymentState.PAID}


async def test_held_payment_is_captured_once() -> None:
    kassa = FakeYooKassa()
    kassa.add_payment("held", PID, status="waiting_for_capture")
    plugin = provider(CountingHttp(kassa))
    [status] = await plugin.fetch_status(["held"])
    assert status.state is PaymentState.PAID and kassa.captures == ["held"]
    capture = [r for r in kassa.requests if r[0] == "POST"]
    assert capture[0][1] == "/v3/payments/held/capture"
    assert capture[0][2]["idempotence-key"] == "svbg-capture-held"
    [again] = await plugin.fetch_status(["held"])
    assert again.state is PaymentState.PAID and kassa.captures == ["held"]


async def test_held_payment_without_auto_capture_stays_processing() -> None:
    kassa = FakeYooKassa()
    kassa.add_payment("held", PID, status="waiting_for_capture")
    [status] = await provider(CountingHttp(kassa), auto_capture=False).fetch_status(["held"])
    assert status.state is PaymentState.PROCESSING and kassa.captures == []


async def test_fetch_status_path_is_quoted() -> None:
    http = CountingHttp(lambda _c: HttpResponse(404, b"{}"))
    assert await provider(http).fetch_status(["a/b?c"]) == []
    assert http.calls[0].url == f"{FAKE_BASE_URL}/payments/a%2Fb%3Fc"


@pytest.mark.parametrize(("status", "retryable"), [(401, False), (429, True), (500, True), (503, True)])
async def test_fetch_status_errors(status: int, retryable: bool) -> None:
    kassa = FakeYooKassa()
    kassa.add_payment("p1", PID)
    kassa.fail_status = status if status != 401 else None
    if status == 401:
        kassa.secret_key = "other"
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(kassa)).fetch_status(["p1"])
    assert err.value.retryable is retryable


# ------------------------------------------------------------------------------------------ refund


async def test_refund() -> None:
    kassa = FakeYooKassa()
    kassa.add_payment("p1", PID, status="succeeded")
    plugin = provider(CountingHttp(kassa), receipt_enabled=True, receipt_email="shop@example.ru")
    result = await plugin.refund("p1", 17_900, "RUB")
    assert result.ok and result.external_id
    assert kassa.refunds[0]["amount"] == {"value": "179.00", "currency": "RUB"}
    assert kassa.refunds[0]["receipt"]["items"][0]["amount"]["value"] == "179.00"
    again = await plugin.refund("p1", 17_900, "RUB")  # same key: no second refund
    assert again.ok and len(kassa.refunds) == 1
    [status] = await plugin.fetch_status(["p1"])
    assert status.state is PaymentState.REFUNDED
    assert not (await plugin.refund("p1", 100, "USD")).ok


# ------------------------------------------------------------------------------------------- probe


async def test_test_credentials() -> None:
    kassa = FakeYooKassa()
    probe = await provider(CountingHttp(kassa)).test_credentials()
    assert probe.ok and SHOP_ID in probe.message and "внимание" not in probe.message
    assert all(r[0] == "GET" for r in kassa.requests) and kassa.created == []
    probe = await provider(CountingHttp(FakeYooKassa(test_shop=True))).test_credentials()
    assert probe.ok and "тестовый магазин, а инстанс в боевом" in probe.message
    probe = await provider(
        CountingHttp(FakeYooKassa(fiscalization=False)), receipt_enabled=True, receipt_email="a@b.ru"
    ).test_credentials()
    assert probe.ok and "чеки" in probe.message
    bad = await provider(CountingHttp(FakeYooKassa(secret_key="x"))).test_credentials()
    assert not bad.ok and "shopId" in bad.message
    down = FakeYooKassa()
    down.fail_with = 503
    assert not (await provider(CountingHttp(down)).test_credentials()).ok


def test_default_base_url() -> None:
    assert DEFAULT_BASE_URL == "https://api.yookassa.ru/v3"
    plugin = make_provider(YooKassa, {"shop_id": SHOP_ID, "secret_key": SECRET_KEY})
    assert plugin._base == DEFAULT_BASE_URL  # type: ignore[attr-defined]


# --------------------------------------------------------------------------------------- core level


async def test_notification_alone_never_credits(make_harness: HarnessFactory, db: CountingDatabase) -> None:
    """A «succeeded» notification from a ЮKassa address while the kassa says pending: nothing is credited —
    the core re-reads the payment (fetch_status is mandatory for an IP-only scheme)."""
    kassa = FakeYooKassa()
    harness = await make_harness(YooKassa, CONFIG, http=CountingHttp(kassa))
    pid = await harness.pending(external_id="yk-1")
    kassa.add_payment("yk-1", pid)
    forged = note(payment_obj(PaymentState.PAID, ext="yk-1", pid=pid))
    assert await harness.send(forged) == 200
    assert await harness.status(pid) == "pending"
    assert (await outcomes(db))[-1] == "verify_queued"
    await harness.core.verify(harness.instance.id, [("yk-1", pid)])
    assert await harness.status(pid) == "pending" and harness.credited == []
    kassa.set_status("yk-1", "succeeded")
    await harness.core.verify(harness.instance.id, [("yk-1", pid)])
    assert await harness.status(pid) == "paid" and len(harness.credited) == 1


async def test_verified_amount_mismatch_and_test_payment(make_harness: HarnessFactory) -> None:
    kassa = FakeYooKassa()
    harness = await make_harness(YooKassa, CONFIG, http=CountingHttp(kassa))
    pid = await harness.pending(external_id="yk-mm")
    kassa.add_payment("yk-mm", pid, amount="1.00", status="succeeded")
    await harness.core.verify(harness.instance.id, [("yk-mm", pid)])
    assert await harness.status(pid) == "mismatch"
    pid2 = await harness.pending(external_id="yk-test")
    kassa.add_payment("yk-test", pid2, status="succeeded", test=True)
    await harness.core.verify(harness.instance.id, [("yk-test", pid2)])
    assert await harness.status(pid2) == "pending" and harness.credited == []


async def test_full_refund_is_applied_after_payment(make_harness: HarnessFactory) -> None:
    kassa = FakeYooKassa()
    harness = await make_harness(YooKassa, CONFIG, http=CountingHttp(kassa))
    pid = await harness.pending(external_id="yk-r")
    kassa.add_payment("yk-r", pid, status="succeeded")
    await harness.core.verify(harness.instance.id, [("yk-r", pid)])
    kassa.set_status("yk-r", "succeeded", refunded="179.00")
    await harness.core.verify(harness.instance.id, [("yk-r", pid)])
    assert await harness.status(pid) == "refunded" and len(harness.refunded) == 1


async def test_test_shop_on_test_instance(make_harness: HarnessFactory) -> None:
    kassa = FakeYooKassa(test_shop=True)
    harness = await make_harness(YooKassa, CONFIG, http=CountingHttp(kassa), is_test=True)
    pid = await harness.pending(external_id="yk-t")
    kassa.add_payment("yk-t", pid, status="succeeded", test=True)
    assert await harness.send(kassa.webhook("yk-t")) == 200
    await harness.core.verify(harness.instance.id, [("yk-t", pid)])
    assert await harness.status(pid) == "paid"


async def test_create_failure_marks_payment_failed(
    make_harness: HarnessFactory, db: CountingDatabase
) -> None:
    kassa = FakeYooKassa()
    kassa.fail_with = 401
    harness = await make_harness(YooKassa, CONFIG, http=CountingHttp(kassa))
    with pytest.raises(CheckoutError) as err:
        await harness.core.create_payment(
            user_id=harness.user_id,
            instance_id=harness.instance.id,
            amount_minor=17_900,
            currency="RUB",
            description="x",
        )
    assert not err.value.retryable
    pid = (await db.raw("select id from payments"))[0]["id"]
    assert await harness.status(pid) == "failed"


async def test_end_to_end_over_real_http(make_harness: HarnessFactory, db: CountingDatabase) -> None:
    """Real aiohttp client (InstanceHttp) against the fake kassa: create, notification, status check."""
    async with FakeYooKassa() as kassa:
        http = InstanceHttp()
        try:
            harness = await make_harness(YooKassa, {**CONFIG, "base_url": kassa.base_url}, http=http)
            result = await harness.core.create_payment(
                user_id=harness.user_id,
                instance_id=harness.instance.id,
                amount_minor=17_900,
                currency="RUB",
                description="Пополнение баланса",
            )
            ext = result.checkout.external_id
            assert ext is not None and kassa.payments[ext].metadata == {"payment_id": result.payment_id}
            row = await payment_row(db, result.payment_id)
            assert row["external_id"] == ext and row["poll_plan"] == "domain"
            kassa.set_status(ext, "succeeded")
            assert await harness.send(kassa.webhook(ext)) == 200
            await harness.core.verify(harness.instance.id, [(ext, result.payment_id)])
            assert await harness.status(result.payment_id) == "paid" and len(harness.credited) == 1
            assert (await harness.instance.provider.test_credentials()).ok
        finally:
            await http.close()


async def test_notification_over_the_real_route_behind_a_proxy(make_harness: HarnessFactory) -> None:
    """The route sees a loopback peer (the reverse proxy) and X-Forwarded-For with the ЮKassa address."""
    from aiohttp import web

    from svbg.web.routes.payments import payment_routes

    kassa = FakeYooKassa()
    harness = await make_harness(YooKassa, CONFIG, http=CountingHttp(kassa))
    app = web.Application()
    app.add_routes(payment_routes(harness.core))
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        inst = harness.instance
        url = f"http://127.0.0.1:{port}/webhooks/pay/{inst.id}/{inst.webhook_token}"
        pid = await harness.pending(external_id="yk-route")
        kassa.add_payment("yk-route", pid, status="succeeded")
        assert await kassa.send_webhook(url, "yk-route") == 200
        assert await kassa.send_webhook(url, "yk-route", forwarded_for=FOREIGN_IP) == 403
        assert await kassa.send_webhook(url, "yk-route", forwarded_for=None) == 403
        await harness.core.verify(inst.id, [("yk-route", pid)])
        assert await harness.status(pid) == "paid"
    finally:
        await runner.cleanup()
