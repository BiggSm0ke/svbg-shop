"""PayPear (Pear) plugin: the shared TestKit plus vectors V1–V16 from ``docs/providers/paypear.md``."""

from __future__ import annotations

import base64
import json
from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest

from svbg.payments.providers.paypear import (
    DEFAULT_BASE_URL,
    PEAR_NETWORKS,
    STATUS_MAP,
    PayPear,
    basic_auth,
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
from tests.fakes.paypear import FAKE_BASE_URL, PEAR_IP, SECRET_KEY, SHOP_ID, FakePayPear
from tests.payments.providers.conftest import HarnessFactory, outcomes, payment_row

CONFIG: dict[str, Any] = {
    "shop_id": SHOP_ID,
    "secret_key": SECRET_KEY,
    "base_url": FAKE_BASE_URL,
    "return_url": "https://t.me/svbg_test_bot",
}
PID = "0190f5d2-7b1e-7c3a-9d4e-5f6a7b8c9d0e"
EXT = "6f1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
FOREIGN_IP = "203.0.113.10"
_STATUS = {
    PaymentState.CREATED: "NEW",
    PaymentState.PROCESSING: "PROCESS",
    PaymentState.PAID: "CONFIRMED",
    PaymentState.CANCELED: "CANCELED",
    PaymentState.FAILED: "CANCELED",
    PaymentState.EXPIRED: "EXPIRED",
    PaymentState.REFUNDED: "REFUNDED",
}


def payment_obj(
    status: str = "CONFIRMED",
    *,
    ext: str | None = EXT,
    order: str | None = PID,
    amount: Any = "179.00",
    shop: Any = int(SHOP_ID),
    **extra: Any,
) -> dict[str, Any]:
    obj: dict[str, Any] = {
        "id": ext,
        "shop_id": shop,
        "order_id": order,
        "status": status,
        "description": "Пополнение баланса",
        "amount": {"value": amount, "currency": "RUB"},
        "created_at": "2026-10-02T09:00:00.000Z",
        "expires_at": "2026-10-02T10:00:00.000Z",
        "paid": status == "CONFIRMED",
        "metadata": {"payment_id": PID},
    }
    obj.update(extra)
    return {k: v for k, v in obj.items() if v is not None}


def note(
    obj: dict[str, Any],
    event: str | None = None,
    *,
    remote: str | None = PEAR_IP,
    headers: dict[str, str] | None = None,
    signature: str | None = "f5e180a3ea7b6aa31173ac988efc3806f5e180a3ea7b6aa31173ac988efc3806",
) -> WebhookRequest:
    data: dict[str, Any] = {
        "type": "notification",
        "event": event or f"payment.{str(obj.get('status', '')).lower()}",
        "object": obj,
    }
    if signature is not None:
        data["signature"] = signature
    return WebhookRequest(body=json.dumps(data).encode(), headers=headers or {}, remote=remote)


class PearVectors:
    """:class:`~svbg.payments.testkit.WebhookVectors` of Pear: «authentic» = sent from Pear's address with our
    ``shop_id``. Pear has no test mode: a «test» event is one of another shop (a separate test shop) and is
    refused with ``403``."""

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
        if state is PaymentState.CHARGEBACK:
            obj = {"id": "rf-1", "payment_id": external_id, "status": "CONFIRMED"}
            return note(obj, "refund.confirmed")
        obj = payment_obj(_STATUS[state], ext=external_id, order=payment_id, amount=amount)
        obj["amount"]["currency"] = currency
        if test:
            obj["shop_id"] = 1
        return note(obj)

    def tamper(self, req: WebhookRequest) -> WebhookRequest:
        return WebhookRequest(body=req.body, headers=dict(req.headers), remote=FOREIGN_IP)

    def strip_auth(self, req: WebhookRequest) -> WebhookRequest:
        return WebhookRequest(body=req.body, headers=dict(req.headers), remote=None)


def provider(http: CountingHttp | None = None, **config: Any) -> PayPear:
    p = make_provider(PayPear, {**CONFIG, **config}, http=http)
    assert isinstance(p, PayPear)
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


def pending_responder(pear: FakePayPear) -> Any:
    """Every unknown id is a «NEW» payment on Pear's side (abandoned checkouts)."""

    async def respond(call: Any) -> Any:
        ext = call.url.rstrip("/").rsplit("/", 1)[-1]
        if call.method == "GET" and "/order/" not in call.url and ext not in pear.payments:
            pear.add_payment(ext, PID)
        return await pear(call)

    return respond


# ------------------------------------------------------------------------------------------- TestKit


def test_static_contract() -> None:
    check_static(PayPear)
    caps = PayPear.capabilities
    assert caps.webhook_auth is WebhookAuth.IP_ONLY and caps.webhook_auth.is_weak
    assert caps.fetch_status and not caps.batch_status and caps.replay_window_s is None
    assert not caps.refund and not caps.recurring and not caps.receipt_54fz and caps.redirect
    assert PayPear.manifest.method_kinds == (MethodKind.SBP, MethodKind.CARD)
    assert PayPear.manifest.currencies == ("RUB",)
    assert PayPear.manifest.min_minor is None and PayPear.manifest.max_minor is None
    fields = PayPear.manifest.config.fields()
    assert fields["secret_key"].is_secret and not fields["shop_id"].is_secret
    assert [n for n, f in fields.items() if f.is_secret] == ["secret_key"]
    for name, fld in fields.items():
        assert fld.where, f"{name} has no «где взять»"


async def test_testkit_plugin_level() -> None:
    await check_plugin(PayPear, CONFIG, PearVectors())


async def test_testkit_core_level(make_harness: HarnessFactory, db: CountingDatabase) -> None:
    harness = await make_harness(PayPear, CONFIG, http=CountingHttp(FakePayPear()))
    await check_core(harness, PearVectors())
    assert {"bad_signature"} <= set(await outcomes(db))
    assert harness.credited == []


@pytest.mark.parametrize("domain", [True, False], ids=["domain", "no-domain"])
async def test_testkit_poll_budget(make_harness: HarnessFactory, domain: bool) -> None:
    pear = FakePayPear()
    harness = await make_harness(
        PayPear, CONFIG, http=CountingHttp(pending_responder(pear)), has_domain=domain
    )
    used = await check_poll_budget(harness, domain=domain, invoices=3)
    assert used <= (3 if domain else 24) * 3
    assert all(r[0] == "GET" for r in pear.requests)


# ------------------------------------------------------------------------------------- V1: auth header


def test_v1_basic_auth_header() -> None:
    assert basic_auth("33661", "test_secret_key") == "Basic MzM2NjE6dGVzdF9zZWNyZXRfa2V5"


# --------------------------------------------------------------------------- V3–V6: who sent it


@pytest.mark.parametrize("remote", [PEAR_IP, f"{PEAR_IP}:443", f"::ffff:{PEAR_IP}"])
async def test_notifications_from_pear_are_accepted(remote: str) -> None:
    event = await provider().parse_webhook(note(payment_obj(), remote=remote))
    assert event.state is PaymentState.PAID and event.external_id == EXT and event.payment_id == PID


@pytest.mark.parametrize("remote", [FOREIGN_IP, "158.160.85.102", "127.0.0.1", "", "garbage", None])
async def test_v3_notifications_from_elsewhere_are_403(remote: str | None) -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(note(payment_obj(), remote=remote))
    assert err.value.status == 403


async def test_v4_forwarded_headers_from_an_untrusted_peer_are_ignored() -> None:
    req = note(payment_obj(), remote=FOREIGN_IP, headers={"X-Forwarded-For": PEAR_IP, "X-Real-IP": PEAR_IP})
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(req)
    assert err.value.status == 403


@pytest.mark.parametrize(
    ("headers", "ok"),
    [
        ({"X-Forwarded-For": PEAR_IP}, True),
        ({"X-Forwarded-For": f"{FOREIGN_IP}, {PEAR_IP}"}, True),  # client junk on the left
        ({"X-Forwarded-For": f"{PEAR_IP}, {FOREIGN_IP}"}, False),  # spoofed left part
        ({"X-Forwarded-For": f"{PEAR_IP}, 127.0.0.1"}, True),  # a chain of trusted proxies
        ({"X-Forwarded-For": "nonsense"}, False),
        ({"X-Real-IP": PEAR_IP}, True),
        ({"X-Real-IP": FOREIGN_IP}, False),
        ({}, False),  # the proxy itself is not Pear
    ],
)
async def test_trusted_proxy_forwarding(headers: dict[str, str], ok: bool) -> None:
    req = note(payment_obj(), remote="127.0.0.1", headers=headers)
    if ok:
        assert (await provider().parse_webhook(req)).state is PaymentState.PAID
    else:
        with pytest.raises(WebhookRejected):
            await provider().parse_webhook(req)


async def test_docker_proxy_and_network_overrides() -> None:
    req = note(payment_obj(), remote="172.18.0.5", headers={"X-Forwarded-For": PEAR_IP})
    with pytest.raises(WebhookRejected):
        await provider().parse_webhook(req)
    assert (await provider(trusted_proxies="127.0.0.1, 172.16.0.0/12").parse_webhook(req)).external_id == EXT
    foreign = note(payment_obj(), remote=FOREIGN_IP)
    assert (await provider(verify_ip=False).parse_webhook(foreign)).state is PaymentState.PAID
    assert (await provider(allowed_networks="203.0.113.0/24").parse_webhook(foreign)).external_id == EXT
    with pytest.raises(WebhookRejected):
        await provider(allowed_networks="203.0.113.0/24").parse_webhook(note(payment_obj()))


def test_network_helpers_and_config_errors() -> None:
    assert [str(n) for n in parse_networks(", ".join(PEAR_NETWORKS))] == ["158.160.85.101/32"]
    with pytest.raises(ConfigError) as err:
        provider(trusted_proxies="10.0.0.0/33")
    assert "trusted_proxies" in err.value.errors
    with pytest.raises(ConfigError) as err:
        provider(min_amount="500", max_amount="100")
    assert "min_amount" in err.value.errors
    with pytest.raises(ConfigError):
        provider(shop_id="shop-1")
    req = WebhookRequest(body=b"", headers={"x-forwarded-for": f"{PEAR_IP}:5443"}, remote="127.0.0.1")
    assert str(client_ip(req, parse_networks("127.0.0.1/32"))) == PEAR_IP


@pytest.mark.parametrize("shop", [1, "1", None, "abc", True, 336610])
async def test_v5_foreign_or_missing_shop_id_is_403(shop: Any) -> None:
    obj = payment_obj()
    if shop is None:
        obj.pop("shop_id")
    else:
        obj["shop_id"] = shop
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(note(obj))
    assert err.value.status == 403


async def test_shop_id_as_string_matches() -> None:
    assert (await provider().parse_webhook(note(payment_obj(shop=SHOP_ID)))).state is PaymentState.PAID


@pytest.mark.parametrize("signature", ["f" * 64, "", "garbage", None])
async def test_v6_signature_field_is_ignored(signature: str | None) -> None:
    event = await provider().parse_webhook(note(payment_obj(), signature=signature))
    assert event.state is PaymentState.PAID and event.external_id == EXT


# ------------------------------------------------------------------------------ webhook: parsing


@pytest.mark.parametrize(("status", "state"), list(STATUS_MAP.items()))
async def test_v14_status_mapping(status: str, state: PaymentState) -> None:
    assert (await provider().parse_webhook(note(payment_obj(status)))).state is state


async def test_v14_full_status_table() -> None:
    assert {k: v.value for k, v in STATUS_MAP.items()} == {
        "NEW": "created",
        "PROCESS": "processing",
        "CONFIRMED": "paid",
        "CANCELED": "canceled",
        "EXPIRED": "expired",
        "REFUNDED": "refunded",
    }


@pytest.mark.parametrize("amount", ["179", "179.00", "179.0", 179, 179.0])
async def test_amount_forms_are_the_same_decimal(amount: Any) -> None:
    event = await provider().parse_webhook(note(payment_obj(amount=amount)))
    assert event.amount == Decimal("179") and event.currency == "RUB" and not event.is_test


async def test_paid_notification_fields() -> None:
    event = await provider().parse_webhook(note(payment_obj()))
    assert event.payment_id == PID and event.external_id == EXT and event.signed_at is None
    assert event.summary["event"] == "payment.confirmed" and event.summary["status"] == "CONFIRMED"


async def test_full_refund_inside_a_confirmed_payment_is_refunded() -> None:
    obj = payment_obj(refunded_amount={"value": "179.00", "currency": "RUB"})
    assert (await provider().parse_webhook(note(obj))).state is PaymentState.REFUNDED
    part = payment_obj(refunded_amount={"value": 50, "currency": "RUB"})
    assert (await provider().parse_webhook(note(part))).state is PaymentState.PAID


async def test_refund_notification_points_at_the_payment() -> None:
    obj = {
        "id": "rf-1",
        "payment_id": EXT,
        "status": "CONFIRMED",
        "amount": {"value": "179.00", "currency": "RUB"},
    }
    event = await provider().parse_webhook(note(obj, "refund.confirmed"))
    assert event.state is PaymentState.REFUNDED and event.external_id == EXT and event.amount is None
    with pytest.raises(WebhookIgnored):
        await provider().parse_webhook(note({**obj, "status": "CANCELED"}, "refund.canceled"))
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(note({**obj, "shop_id": 1}, "refund.confirmed"))
    assert err.value.status == 403


@pytest.mark.parametrize("event", ["payout.done", "payout.canceled", "deal.closed"])
async def test_other_events_are_acknowledged(event: str) -> None:
    with pytest.raises(WebhookIgnored):
        await provider().parse_webhook(note({"id": "x"}, event))


async def test_unknown_status_is_acknowledged() -> None:
    with pytest.raises(WebhookIgnored):
        await provider().parse_webhook(note(payment_obj("WEIRD"), "payment.weird"))


async def test_status_falls_back_to_the_event_name() -> None:
    obj = payment_obj()
    obj.pop("status")
    assert (await provider().parse_webhook(note(obj, "payment.canceled"))).state is PaymentState.CANCELED


async def test_v16_foreign_order_id_still_parses_by_the_pear_id() -> None:
    event = await provider().parse_webhook(note(payment_obj(order="shop-order-77")))
    assert event.payment_id is None and event.external_id == EXT


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        b'{"type":"notification","event":"payment.confirmed"}',
        b'{"type":"other","event":"payment.confirmed","object":{"id":"x"}}',
        b'{"type":"notification","event":7,"object":{"id":"x"}}',
        b'{"type":"notification","event":"payment.confirmed","object":{"shop_id":33661,"status":"CONFIRMED"}}',
        b'{"type":"notification","event":"payment.confirmed","object":{"id":"x","shop_id":33661,'
        b'"status":"CONFIRMED","amount":{"value":"abc","currency":"RUB"}}}',
        b'{"type":"notification","event":"payment.confirmed","object":{"id":"x","shop_id":33661,'
        b'"status":"CONFIRMED","amount":"179"}}',
        b'{"type":"notification","event":"refund.confirmed","object":{"id":"r"}}',
        # the documentation's own example is invalid JSON (missing comma) — such a body is malformed
        b'{"type":"notification","event":"payment.confirmed","object":{"id":"x" "shop_id":33661}}',
    ],
)
async def test_v15_malformed_bodies_are_400(body: bytes) -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(WebhookRequest(body=body, remote=PEAR_IP))
    assert err.value.status == 400


# ------------------------------------------------------------------------------------------ create


def _auth(headers: dict[str, str]) -> tuple[str, str]:
    user, _, password = base64.b64decode(headers["authorization"][6:]).decode().partition(":")
    return user, password


async def test_create_request_shape_and_privacy() -> None:
    pear = FakePayPear()
    checkout = await provider(CountingHttp(pear)).create(intent())
    body = pear.created[0]
    assert body["order_id"] == PID and len(body["order_id"]) == 36
    assert body["amount"] == {"value": "179.00", "currency": "RUB"}
    assert body["payment_method_data"] == {"type": "sbp"}
    assert body["confirmation"] == {"type": "redirect", "return_url": "https://t.me/svbg_bot?start=paid"}
    assert body["metadata"] == {"payment_id": PID}
    assert body["webhook_url"] == "https://shop.example/webhooks/pay/1/token"
    assert "expires_at" not in body and "c0ffee" not in json.dumps(body)
    method, path, headers = pear.requests[0]
    assert (method, path) == ("POST", "/v1/payment/")
    assert headers["idempotence-key"] == headers["idempotency-key"] == PID
    assert headers["authorization"] == "Basic MzM2NjE6dGVzdF9zZWNyZXRfa2V5"  # V1
    assert _auth(headers) == (SHOP_ID, SECRET_KEY)
    assert checkout.kind == "url" and checkout.external_id in pear.payments
    assert checkout.pay_url is not None and checkout.pay_url.startswith("https://pay.paypear.fake/sbp/")
    assert checkout.expires_at is not None


@pytest.mark.parametrize(
    ("hint", "configured", "expected"),
    [
        (MethodKind.SBP, "card", "sbp"),
        (MethodKind.CARD, "sbp", "card"),
        (None, "card", "card"),
        (None, "sbp", "sbp"),
    ],
)
async def test_payment_method_follows_the_button(
    hint: MethodKind | None, configured: str, expected: str
) -> None:
    pear = FakePayPear()
    await provider(CountingHttp(pear), payment_method=configured).create(intent(method_hint=hint))
    assert pear.created[0]["payment_method_data"] == {"type": expected}


async def test_lifetime_return_url_and_description() -> None:
    pear = FakePayPear()
    await provider(CountingHttp(pear), payment_lifetime_min=30).create(
        intent(return_url=None, description="Очень длинное описание " * 20)
    )
    body = pear.created[0]
    assert body["confirmation"]["return_url"] == "https://t.me/svbg_test_bot"
    assert len(body["description"]) == 128
    assert body["expires_at"].endswith("Z")
    with pytest.raises(ProviderError, match="адрес возврата") as err:
        await provider(CountingHttp(pear), return_url="").create(intent(return_url=None))
    assert not err.value.retryable


@pytest.mark.parametrize("minor", [100, 17_950, 99_999_999, 12_345])
async def test_amount_is_sent_as_an_exact_string(minor: int) -> None:
    pear = FakePayPear()
    await provider(CountingHttp(pear)).create(intent(amount_minor=minor))
    assert pear.created[0]["amount"]["value"] == f"{Decimal(minor) / 100:.2f}"


async def test_limits_and_currency_are_checked_before_any_request() -> None:
    http = CountingHttp(FakePayPear())
    plugin = provider(http, min_amount="200", max_amount="1000.50")
    with pytest.raises(ProviderError, match="меньше минимальной"):
        await plugin.create(intent(amount_minor=17_900))
    with pytest.raises(ProviderError, match="больше максимальной"):
        await plugin.create(intent(amount_minor=100_051))
    with pytest.raises(ProviderError, match="только RUB"):
        await plugin.create(intent(currency="USD"))
    assert http.requests == 0
    assert (await plugin.create(intent(amount_minor=100_050))).kind == "url"


async def test_repeated_create_returns_the_same_invoice() -> None:
    pear = FakePayPear()
    plugin = provider(CountingHttp(pear))
    first = await plugin.create(intent())
    second = await plugin.create(intent())
    assert first.external_id == second.external_id and len(pear.payments) == 1


async def test_v11_response_envelope_is_read_too() -> None:
    pear = FakePayPear(envelope="response")
    plugin = provider(CountingHttp(pear))
    checkout = await plugin.create(intent())
    assert checkout.external_id in pear.payments
    assert checkout.external_id is not None
    pear.set_status(checkout.external_id, "CONFIRMED")
    [status] = await plugin.fetch_status([checkout.external_id])
    assert status.state is PaymentState.PAID and status.payment_id == PID


async def test_v13_lost_create_answer_reuses_the_invoice_pear_made() -> None:
    pear = FakePayPear()
    pear.create_faults = [(500, True)]
    http = CountingHttp(pear)
    checkout = await provider(http).create(intent())
    assert len(pear.payments) == 1 and checkout.external_id in pear.payments
    assert [(r[0], r[1]) for r in pear.requests] == [
        ("POST", "/v1/payment/"),
        ("GET", f"/v1/payment/order/{PID}/"),
    ]


async def test_v13_create_500_without_an_invoice_is_retryable() -> None:
    pear = FakePayPear()
    pear.create_faults = [(500, False)]
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(pear)).create(intent())
    assert err.value.retryable and pear.payments == {}


async def test_v13_lost_answer_on_the_transport_is_recovered() -> None:
    pear = FakePayPear()
    calls = 0

    async def flaky(call: Any) -> Any:
        nonlocal calls
        calls += 1
        answer = await pear(call)
        if calls == 1:
            raise ProviderError("таймаут", retryable=True)  # Pear made it, the answer was lost
        return answer

    checkout = await provider(CountingHttp(flaky)).create(intent())
    assert checkout.external_id in pear.payments and len(pear.payments) == 1


async def test_v13_closed_order_is_not_reused() -> None:
    pear = FakePayPear()
    pear.add_payment(EXT, PID, status="CANCELED")
    pear.create_faults = [(500, False)]
    with pytest.raises(ProviderError, match="уже закрыт"):
        await provider(CountingHttp(pear)).create(intent())


@pytest.mark.parametrize(
    ("status", "code", "retryable", "words"),
    [
        (401, "UNAUTHORIZED", False, "ID магазина"),
        (403, "FORBIDDEN", False, "запретила"),
        (409, "TOO_MANY_REQUESTS", True, "частоту"),  # V12
        (429, "TOO_MANY_REQUESTS", True, "частоту"),
        (502, "BAD_GATEWAY", True, "не подтвердила"),  # 5xx: looked up, not found
        (400, "BAD_REQUEST", False, "BAD_REQUEST"),
        (409, "CONFLICT", False, "CONFLICT"),
    ],
)
async def test_create_errors(status: int, code: str, retryable: bool, words: str) -> None:
    pear = FakePayPear()
    pear.fail_with, pear.fail_code = status, code
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(pear)).create(intent())
    assert err.value.retryable is retryable and words in err.value.human
    assert SECRET_KEY not in err.value.human


@pytest.mark.parametrize(
    "answer",
    [
        b"<html>",
        b'{"success":true,"result":{"status":"NEW"}}',
        b'{"success":true,"result":{"id":"x","confirmation":{"confirmation_url":"javascript:alert(1)"}}}',
        b'{"success":true,"result":{"id":"x","shop_id":1,"confirmation":{"confirmation_url":"https://x"}}}',
        b'{"success":false,"result":{"id":"x"}}',
        b'{"success":true}',
    ],
)
async def test_create_with_a_bad_answer(answer: bytes) -> None:
    with pytest.raises(ProviderError):
        await provider(CountingHttp(lambda _c: HttpResponse(200, answer))).create(intent())


# ------------------------------------------------------------------------------------ fetch_status


async def test_fetch_status_one_get_per_invoice() -> None:
    pear = FakePayPear()
    pear.add_payment("p1", PID, status="CONFIRMED")
    pear.add_payment("p2", "other-order", status="NEW")
    pear.add_payment("p3", PID, status="EXPIRED")
    http = CountingHttp(pear)
    result = await provider(http).fetch_status(["p1", "p2", "p3", "p1", "missing", ""])
    assert [(s.external_id, s.state) for s in result] == [
        ("p1", PaymentState.PAID),
        ("p2", PaymentState.CREATED),
        ("p3", PaymentState.EXPIRED),
    ]
    assert http.requests == 4 and all(c.method == "GET" for c in http.calls)
    assert http.calls[0].url == f"{FAKE_BASE_URL}/payment/p1/"
    assert (
        result[0].amount == Decimal("179.00") and result[0].currency == "RUB" and result[0].payment_id == PID
    )
    assert result[1].payment_id is None and not result[0].is_test


@pytest.mark.parametrize(("amount", "expected"), [(1000.99, "1000.99"), ("100.00", "100.00")])
async def test_v8_v9_amount_as_number_or_string(amount: Any, expected: str) -> None:
    pear = FakePayPear()
    pear.add_payment("p1", PID, amount=amount, status="CONFIRMED")
    [status] = await provider(CountingHttp(pear)).fetch_status(["p1"])
    assert status.amount == Decimal(expected)


async def test_fetch_status_refunds_and_foreign_shop() -> None:
    pear = FakePayPear()
    pear.add_payment("full", PID, status="CONFIRMED", refunded="179.00")
    pear.add_payment("part", PID, status="CONFIRMED", refunded=50)
    pear.add_payment("ref", PID, status="REFUNDED")
    pear.add_payment("foreign", PID, status="CONFIRMED", shop_id=1)
    pear.add_payment("weird", PID, status="WEIRD")
    result = {
        s.external_id: s.state
        for s in await provider(CountingHttp(pear)).fetch_status(["full", "part", "ref", "foreign", "weird"])
    }
    assert result == {"full": PaymentState.REFUNDED, "part": PaymentState.PAID, "ref": PaymentState.REFUNDED}


async def test_fetch_status_path_is_quoted() -> None:
    http = CountingHttp(lambda _c: HttpResponse(404, b"{}"))
    assert await provider(http).fetch_status(["a/b?c"]) == []
    assert http.calls[0].url == f"{FAKE_BASE_URL}/payment/a%2Fb%3Fc/"


@pytest.mark.parametrize(("status", "retryable"), [(401, False), (409, True), (429, True), (500, True)])
async def test_fetch_status_errors(status: int, retryable: bool) -> None:
    pear = FakePayPear()
    pear.add_payment("p1", PID)
    if status == 401:
        pear.secret_key = "other"
    else:
        pear.fail_status = status
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(pear)).fetch_status(["p1"])
    assert err.value.retryable is retryable


# ------------------------------------------------------------------------------------------- probe


async def test_test_credentials_has_no_side_effects() -> None:
    pear = FakePayPear()
    probe = await provider(CountingHttp(pear)).test_credentials()
    assert probe.ok and SHOP_ID in probe.message
    assert [r[0] for r in pear.requests] == ["GET"] and pear.created == []
    bad = await provider(CountingHttp(FakePayPear(secret_key="x"))).test_credentials()
    assert not bad.ok and "ID магазина" in bad.message
    down = FakePayPear()
    down.fail_with = 503
    assert not (await provider(CountingHttp(down)).test_credentials()).ok
    html = await provider(CountingHttp(lambda _c: HttpResponse(404, b"<html>"))).test_credentials()
    assert not html.ok and "не Pear" in html.message


def test_default_base_url() -> None:
    assert DEFAULT_BASE_URL == "https://api.paypear.ru/v1"
    plugin = make_provider(PayPear, {"shop_id": SHOP_ID, "secret_key": SECRET_KEY})
    assert plugin._base == DEFAULT_BASE_URL  # type: ignore[attr-defined]


# --------------------------------------------------------------------------------------- core level


async def test_v2_v7_notification_alone_never_credits(
    make_harness: HarnessFactory, db: CountingDatabase
) -> None:
    """V2/V7: a «confirmed» notification while Pear says NEW credits nothing; only the API answer does."""
    pear = FakePayPear()
    harness = await make_harness(PayPear, CONFIG, http=CountingHttp(pear))
    pid = await harness.pending(external_id="pp-1")
    pear.add_payment("pp-1", pid)
    forged = note(payment_obj(ext="pp-1", order=pid))
    assert await harness.send(forged) == 200
    assert await harness.status(pid) == "pending"
    assert (await outcomes(db))[-1] == "verify_queued"
    await harness.core.verify(harness.instance.id, [("pp-1", pid)])
    assert await harness.status(pid) == "pending" and harness.credited == []
    pear.set_status("pp-1", "CONFIRMED")
    await harness.core.verify(harness.instance.id, [("pp-1", pid)])
    assert await harness.status(pid) == "paid" and len(harness.credited) == 1


async def test_v3_v5_rejected_notifications_queue_nothing(
    make_harness: HarnessFactory, db: CountingDatabase
) -> None:
    harness = await make_harness(PayPear, CONFIG, http=CountingHttp(FakePayPear()))
    pid = await harness.pending(external_id="pp-x")
    assert await harness.send(note(payment_obj(ext="pp-x", order=pid), remote=FOREIGN_IP)) == 403
    assert await harness.send(note(payment_obj(ext="pp-x", order=pid, shop=1))) == 403
    assert "verify_queued" not in await outcomes(db)


async def test_v8_v10_verified_amounts(make_harness: HarnessFactory) -> None:
    pear = FakePayPear()
    harness = await make_harness(PayPear, CONFIG, http=CountingHttp(pear))
    pid = await harness.pending(amount_minor=100_099, external_id="pp-num")
    pear.add_payment("pp-num", pid, amount=1000.99, status="CONFIRMED")
    await harness.core.verify(harness.instance.id, [("pp-num", pid)])
    assert await harness.status(pid) == "paid"
    pid2 = await harness.pending(external_id="pp-mm")
    pear.add_payment("pp-mm", pid2, amount="1.00", status="CONFIRMED")
    await harness.core.verify(harness.instance.id, [("pp-mm", pid2)])
    assert await harness.status(pid2) == "mismatch" and len(harness.credited) == 1


async def test_v16_unknown_order_is_acknowledged(make_harness: HarnessFactory) -> None:
    pear = FakePayPear()
    harness = await make_harness(PayPear, CONFIG, http=CountingHttp(pear))
    pear.add_payment("pp-alien", "alien-order", status="CONFIRMED")
    assert await harness.send(pear.webhook("pp-alien")) == 200
    results = await harness.core.verify(harness.instance.id, [("pp-alien", None)])
    assert [r.outcome.value for r in results] == ["unknown_payment"] and harness.credited == []


async def test_full_refund_is_applied_after_payment(make_harness: HarnessFactory) -> None:
    pear = FakePayPear()
    harness = await make_harness(PayPear, CONFIG, http=CountingHttp(pear))
    pid = await harness.pending(external_id="pp-r")
    pear.add_payment("pp-r", pid, status="CONFIRMED")
    await harness.core.verify(harness.instance.id, [("pp-r", pid)])
    pear.set_status("pp-r", "REFUNDED")
    assert await harness.send(pear.webhook("pp-r", "refund.confirmed")) == 200
    await harness.core.verify(harness.instance.id, [("pp-r", pid)])
    assert await harness.status(pid) == "refunded" and len(harness.refunded) == 1


async def test_end_to_end_over_real_http(make_harness: HarnessFactory, db: CountingDatabase) -> None:
    """Real aiohttp client (InstanceHttp) against the fake Pear: create, notification, status check."""
    async with FakePayPear() as pear:
        http = InstanceHttp()
        try:
            harness = await make_harness(PayPear, {**CONFIG, "base_url": pear.base_url}, http=http)
            result = await harness.core.create_payment(
                user_id=harness.user_id,
                instance_id=harness.instance.id,
                amount_minor=17_900,
                currency="RUB",
                description="Пополнение баланса",
            )
            ext = result.checkout.external_id
            assert ext is not None and pear.payments[ext].order_id == result.payment_id
            row = await payment_row(db, result.payment_id)
            assert row["external_id"] == ext
            pear.set_status(ext, "CONFIRMED")
            assert await harness.send(pear.webhook(ext)) == 200
            await harness.core.verify(harness.instance.id, [(ext, result.payment_id)])
            assert await harness.status(result.payment_id) == "paid" and len(harness.credited) == 1
            assert (await harness.instance.provider.test_credentials()).ok
        finally:
            await http.close()
