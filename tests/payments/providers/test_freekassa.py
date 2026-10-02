"""Freekassa plugin: the shared TestKit plus protocol vectors from ``docs/providers/freekassa.md``."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

import pytest

from svbg.payments.providers.freekassa import (
    DEFAULT_API_URL,
    DEFAULT_FORM_URL,
    FREEKASSA_IPS,
    Freekassa,
    api_signature,
    form_signature,
    notification_signature,
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
    WebhookAuth,
    WebhookIgnored,
    WebhookRejected,
    WebhookRequest,
)
from tests.dbkit import CountingDatabase
from tests.fakes.freekassa import (
    API_KEY,
    FAKE_API_URL,
    FREEKASSA_IP,
    SECRET_WORD,
    SECRET_WORD_2,
    SHOP_ID,
    FakeFreekassa,
    api_sign,
    encode_form,
    md5,
)
from tests.payments.providers.conftest import HarnessFactory, outcomes

CONFIG = {
    "shop_id": SHOP_ID,
    "secret_word": SECRET_WORD,
    "secret_word_2": SECRET_WORD_2,
    "api_key": API_KEY,
    "api_url": FAKE_API_URL,
}
DIRECT = {**CONFIG, "confirm_via_api": "false"}
API_MODE = {**CONFIG, "checkout_mode": "api", "payment_system": "44", "payer_email": "shop@example.com",
            "payer_ip": "203.0.113.7"}  # fmt: skip
PID = "0190f5d2-7b1e-7c3a-9d4e-5f6a7b8c9d0e"
OTHER_SECRET_2 = "secret-2-of-the-test-shop"


def note(fields: dict[str, str], *, remote: str | None = FREEKASSA_IP, **headers: str) -> WebhookRequest:
    body, ctype = encode_form(fields)
    return WebhookRequest(body=body, headers={"Content-Type": ctype, **headers}, remote=remote)


def signed(order: str = PID, amount: str = "179", *, secret: str = SECRET_WORD_2, shop: str = SHOP_ID,
           us_cur: str | None = "RUB") -> dict[str, str]:  # fmt: skip
    fields = {
        "MERCHANT_ID": shop,
        "AMOUNT": amount,
        "intid": "123456",
        "MERCHANT_ORDER_ID": order,
        "CUR_ID": "42",
    }
    if us_cur:
        fields["us_cur"] = us_cur
    fields["SIGN"] = md5(f"{shop}:{amount}:{secret}:{order}")
    return fields


class FreekassaVectors:
    """:class:`~svbg.payments.testkit.WebhookVectors` of Freekassa. Freekassa notifies only about successful
    payments: other states have no notification (an empty call, ignored with 200). Freekassa has no test flag:
    a «test» notification is one of another (test) shop and cannot authenticate."""

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
        if state is not PaymentState.PAID:
            return WebhookRequest(body=b"", remote=FREEKASSA_IP)
        # our order number is the external id (MERCHANT_ORDER_ID = o = payment id)
        return note(
            signed(external_id, amount, secret=OTHER_SECRET_2 if test else SECRET_WORD_2, us_cur=currency)
        )

    def tamper(self, req: WebhookRequest) -> WebhookRequest:
        fields = dict(parse_qsl(req.body.decode()))
        fields["AMOUNT"] = "1" + fields["AMOUNT"]
        return note(fields)

    def strip_auth(self, req: WebhookRequest) -> WebhookRequest:
        fields = {k: v for k, v in parse_qsl(req.body.decode()) if k != "SIGN"}
        return note(fields)


def provider(http: CountingHttp | None = None, **config: Any) -> Freekassa:
    p = make_provider(Freekassa, {**CONFIG, **config}, http=http)
    assert isinstance(p, Freekassa)
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
    check_static(Freekassa)
    caps = Freekassa.capabilities
    assert caps.webhook_auth is WebhookAuth.SIGNATURE and caps.replay_window_s is None
    assert caps.fetch_status and not caps.batch_status and caps.refund
    assert Freekassa.manifest.currencies == ("RUB", "USD", "EUR", "UAH", "KZT")
    for name, fld in Freekassa.manifest.config.fields().items():
        assert fld.title and fld.description, name
        if fld.required and fld.default is None:
            assert fld.where, f"{name}: «где взять» is missing"


@pytest.mark.parametrize("config", [CONFIG, DIRECT], ids=["confirm-via-api", "direct"])
async def test_testkit_plugin_level(config: dict[str, Any]) -> None:
    await check_plugin(Freekassa, config, FreekassaVectors())


async def test_testkit_core_level_direct(make_harness: HarnessFactory) -> None:
    """Direct crediting passes everything except the vectors Freekassa cannot express: it never notifies about
    an expired invoice or a chargeback (refunds are read with ``fetch_status``)."""
    harness = await make_harness(Freekassa, DIRECT, http=CountingHttp(FakeFreekassa()))
    with pytest.raises(KitFailure) as err:
        await check_core(harness, FreekassaVectors())
    assert err.value.failures == [
        "expired webhook did not expire the payment",
        "chargeback did not mark the payment refunded",
    ]
    assert len(harness.credited) == 3


async def test_testkit_core_level_confirm_via_api(make_harness: HarnessFactory, db: CountingDatabase) -> None:
    """Default mode: a notification never credits by itself — the amount is checked again through the API."""
    harness = await make_harness(Freekassa, CONFIG, http=CountingHttp(FakeFreekassa()))
    with pytest.raises(KitFailure) as err:
        await check_core(harness, FreekassaVectors())
    assert "replayed body credited 0 times" in err.value.failures
    assert harness.credited == [] and "verify_queued" in await outcomes(db)


@pytest.mark.parametrize("domain", [True, False], ids=["domain", "no-domain"])
async def test_testkit_poll_budget(make_harness: HarnessFactory, domain: bool) -> None:
    desk = FakeFreekassa()
    harness = await make_harness(Freekassa, CONFIG, http=CountingHttp(desk), has_domain=domain)
    used = await check_poll_budget(harness, domain=domain, invoices=3)
    assert used <= (3 if domain else 24) * 3


# ------------------------------------------------------------------------------------ signature vectors


def test_signature_known_answers() -> None:
    """Vectors from the official page (md5('7012:100.11:secret:RUB:154')), cross-checked with hashlib/hmac."""
    assert (
        form_signature("7012", "100.11", "secret", "RUB", "154")
        == hashlib.md5(b"7012:100.11:secret:RUB:154").hexdigest()
    )
    assert form_signature("7012", "100.11", "secret", "RUB", "154") == "64d0581f4a08af485a619950e023696a"
    assert notification_signature("123", "100", "secret2", "test_order") == md5("123:100:secret2:test_order")
    data = {"shopId": 777, "nonce": 1_700_000_000}
    assert api_signature("key", data) == hmac.new(b"key", b"1700000000|777", hashlib.sha256).hexdigest()
    assert api_signature("key", data) == api_sign("key", data)


async def test_paid_notification_fields() -> None:
    event = await provider(confirm_via_api="false").parse_webhook(note(signed(amount="179.00")))
    assert event.state is PaymentState.PAID and event.external_id == PID and event.payment_id == PID
    assert event.amount == Decimal("179") and event.currency == "RUB" and not event.is_test
    assert event.summary["intid"] == "123456" and event.summary["cur_id"] == "42"
    assert "P_EMAIL" not in event.summary and "payer_account" not in str(dict(event.summary))


async def test_default_mode_hides_the_currency_to_force_an_api_check() -> None:
    event = await provider().parse_webhook(note(signed()))
    assert event.currency is None and event.amount == Decimal("179")


async def test_without_echoed_currency_the_api_decides() -> None:
    event = await provider(confirm_via_api="false").parse_webhook(note(signed(us_cur=None)))
    assert event.currency is None


async def test_multipart_notification() -> None:
    desk = FakeFreekassa()
    req = desk.notification(PID, multipart=True)
    event = await provider(confirm_via_api="false").parse_webhook(req)
    assert event.payment_id == PID and event.currency == "RUB"


async def test_ack_is_yes() -> None:
    resp = provider().ack(None)
    assert resp.status == 200 and resp.body == b"YES"


@pytest.mark.parametrize(
    ("fields", "status"),
    [
        (signed(secret="wrong"), 401),
        (signed(shop="9999", secret=SECRET_WORD_2), 401),
        ({k: v for k, v in signed().items() if k != "SIGN"}, 401),
        ({**signed(), "AMOUNT": "17.9"}, 401),
        ({**signed(), "MERCHANT_ORDER_ID": "other"}, 401),
        (signed(amount="abc"), 400),
        (signed(order=""), 400),
    ],
    ids=["bad-secret", "other-shop", "no-sign", "amount-changed", "order-changed", "bad-amount", "no-order"],
)
async def test_bad_notifications(fields: dict[str, str], status: int) -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(note(fields))
    assert err.value.status == status


async def test_repeated_field_is_ambiguous() -> None:
    body = urlencode([*signed().items(), ("AMOUNT", "1")]).encode()
    req = WebhookRequest(
        body=body, headers={"Content-Type": "application/x-www-form-urlencoded"}, remote=FREEKASSA_IP
    )
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(req)
    assert err.value.status == 400


async def test_empty_call_is_ignored() -> None:
    with pytest.raises(WebhookIgnored):
        await provider().parse_webhook(WebhookRequest(body=b"", remote=FREEKASSA_IP))


# ----------------------------------------------------------------------------------------- IP check


@pytest.mark.parametrize("ip", FREEKASSA_IPS)
async def test_every_published_ip_is_accepted(ip: str) -> None:
    assert (await provider().parse_webhook(note(signed(), remote=ip))).state is PaymentState.PAID


@pytest.mark.parametrize(
    ("remote", "headers", "ok"),
    [
        ("93.184.216.34", {}, False),
        (None, {}, False),
        ("93.184.216.34", {"X-Forwarded-For": FREEKASSA_IP}, False),  # a public peer: headers are not trusted
        (
            "127.0.0.1",
            {"X-Forwarded-For": f"10.1.1.1, {FREEKASSA_IP}"},
            True,
        ),  # our proxy appended the sender
        ("127.0.0.1", {"X-Forwarded-For": f"{FREEKASSA_IP}, 93.184.216.34"}, False),  # client-supplied entry
        ("172.18.0.2", {"X-Real-IP": FREEKASSA_IP}, True),
        ("127.0.0.1", {}, False),
        ("::ffff:168.119.157.136", {}, True),
    ],
    ids=[
        "public-other",
        "no-peer",
        "public-xff",
        "proxy-xff",
        "spoofed-xff",
        "docker-real-ip",
        "local",
        "mapped",
    ],
)
async def test_sender_address(remote: str | None, headers: dict[str, str], ok: bool) -> None:
    req = note(signed(), remote=remote, **headers)
    if ok:
        assert (await provider().parse_webhook(req)).payment_id == PID
    else:
        with pytest.raises(WebhookRejected) as err:
            await provider().parse_webhook(req)
        assert err.value.status == 403


async def test_ip_check_can_be_turned_off_or_extended() -> None:
    req = note(signed(), remote="93.184.216.34")
    assert (await provider(allowed_ips="off").parse_webhook(req)).state is PaymentState.PAID
    assert (await provider(allowed_ips="93.184.216.0/24").parse_webhook(req)).state is PaymentState.PAID


# ------------------------------------------------------------------------------------------- create


async def test_form_link_needs_no_request_and_is_accepted_by_the_form() -> None:
    http = CountingHttp(FakeFreekassa())
    checkout = await provider(http).create(intent(amount_minor=17_950))
    assert http.requests == 0 and checkout.kind == "url" and checkout.external_id == PID
    assert checkout.pay_url is not None and checkout.pay_url.startswith(DEFAULT_FORM_URL + "?")
    query = dict(parse_qsl(urlsplit(checkout.pay_url).query))
    assert (
        query["m"] == SHOP_ID and query["oa"] == "179.50" and query["o"] == PID and query["currency"] == "RUB"
    )
    assert query["s"] == md5(f"{SHOP_ID}:179.50:{SECRET_WORD}:RUB:{PID}") and query["us_cur"] == "RUB"
    assert "i" not in query and "c0ffee" not in checkout.pay_url
    desk = FakeFreekassa()
    assert desk.open_form(checkout.pay_url).payment_id == PID


async def test_form_link_with_payment_system_and_custom_form_url() -> None:
    checkout = await provider(payment_system="42", form_url="https://pay.example/form?x=1").create(intent())
    assert checkout.pay_url is not None and checkout.pay_url.startswith("https://pay.example/form?x=1&")
    assert dict(parse_qsl(urlsplit(checkout.pay_url).query))["i"] == "42"


async def test_api_mode_request_shape() -> None:
    desk = FakeFreekassa()
    http = CountingHttp(desk)
    plugin = provider(http, **{k: v for k, v in API_MODE.items() if k not in CONFIG})
    checkout = await plugin.create(intent(currency="USD", amount_minor=500))
    body = desk.created[0]
    assert (
        body["paymentId"] == PID
        and body["i"] == 44
        and body["amount"] == "5.00"
        and body["currency"] == "USD"
    )
    assert body["email"] == "shop@example.com" and body["ip"] == "203.0.113.7" and body["shopId"] == 7012
    assert "notification_url" not in body and "c0ffee" not in json.dumps(body)
    assert checkout.external_id == PID and checkout.pay_url is not None
    assert checkout.pay_url.startswith("https://pay.freekassa.fake/form/")
    assert http.calls[0].url == f"{FAKE_API_URL}/orders/create"


async def test_api_mode_needs_its_fields() -> None:
    http = CountingHttp(FakeFreekassa())
    with pytest.raises(ProviderError) as err:
        await provider(http, checkout_mode="api").create(intent())
    assert "API" in err.value.human and http.requests == 0


async def test_nonce_strictly_increases() -> None:
    desk = FakeFreekassa()
    plugin = provider(CountingHttp(desk))
    for _ in range(5):
        await plugin.fetch_status([PID])
    assert len(desk.status_calls) == 5


@pytest.mark.parametrize(
    ("answer", "retryable", "words"),
    [
        (HttpResponse(401, b'{"type":"error","message":"Wrong signature"}'), False, "API-ключ"),
        (HttpResponse(400, b'{"type":"error","message":"Wrong amount"}'), False, "Wrong amount"),
        (HttpResponse(200, b'{"type":"error","message":"Shop disabled"}'), False, "Shop disabled"),
        (HttpResponse(502, b"<html>bad gateway</html>"), True, "недоступна"),
        (HttpResponse(200, b"not json"), True, "непонятный"),
        (HttpResponse(200, b'{"type":"success","orderId":1}'), False, "непонятный"),
    ],
    ids=["401", "400", "type-error", "502", "not-json", "no-location"],
)
async def test_api_create_errors(answer: HttpResponse, retryable: bool, words: str) -> None:
    plugin = provider(
        CountingHttp(lambda _call: answer), **{k: v for k, v in API_MODE.items() if k not in CONFIG}
    )
    with pytest.raises(ProviderError) as err:
        await plugin.create(intent())
    assert err.value.retryable is retryable and words in err.value.human
    assert API_KEY not in err.value.human


async def test_unsupported_currency() -> None:
    with pytest.raises(ProviderError):
        await provider().create(intent(currency="GBP", amount_minor=100))


# ------------------------------------------------------------------------------------- status/refund


async def test_fetch_status_reads_orders_by_payment_id() -> None:
    desk = FakeFreekassa()
    desk.add(PID, 179.0, status=0)
    desk.add(PID, 179.5, status=1)  # the form was opened twice; one order is paid
    desk.add("other-order", 10, "USD", status=9)
    desk.add("weird", 10, status=42)
    http = CountingHttp(desk)
    statuses = await provider(http).fetch_status([PID, "other-order", "weird", "missing", PID, ""])
    assert http.requests == 4 and desk.status_calls == [PID, "other-order", "weird", "missing"]
    by_id = {s.external_id: s for s in statuses}
    assert set(by_id) == {PID, "other-order"}
    assert by_id[PID].state is PaymentState.PAID and by_id[PID].amount == Decimal("179.5")
    assert by_id[PID].payment_id == PID and by_id[PID].currency == "RUB"
    assert by_id["other-order"].state is PaymentState.CANCELED and by_id["other-order"].currency == "USD"
    assert by_id["other-order"].payment_id is None


@pytest.mark.parametrize(("code", "state"), [(0, PaymentState.CREATED), (1, PaymentState.PAID),
                                             (6, PaymentState.REFUNDED), (8, PaymentState.FAILED),
                                             (9, PaymentState.CANCELED)])  # fmt: skip
async def test_order_statuses(code: int, state: PaymentState) -> None:
    desk = FakeFreekassa()
    desk.add(PID, 179, status=code)
    [status] = await provider(CountingHttp(desk)).fetch_status([PID])
    assert status.state is state


async def test_fetch_status_errors() -> None:
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(lambda _c: HttpResponse(503, b""))).fetch_status([PID])
    assert err.value.retryable
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(FakeFreekassa(api_key="other"))).fetch_status([PID])
    assert not err.value.retryable
    bad = HttpResponse(200, json.dumps({"type": "success", "orders": [
        {"merchant_order_id": PID, "amount": "x", "status": 1}]}).encode())  # fmt: skip
    assert await provider(CountingHttp(lambda _c: bad)).fetch_status([PID]) == []


async def test_refund() -> None:
    desk = FakeFreekassa()
    desk.add(PID, 179, status=1)
    result = await provider(CountingHttp(desk)).refund(PID, 17_900, "RUB")
    assert result.ok and result.external_id == "77" and desk.refunds[0]["orderAmount"] == "179.00"
    again = await provider(CountingHttp(desk)).refund(PID, 17_900, "RUB")
    assert not again.ok and "Order not found" in again.message


async def test_test_credentials() -> None:
    probe = await provider(CountingHttp(FakeFreekassa())).test_credentials()
    assert probe.ok and DEFAULT_FORM_URL in probe.message
    bad = await provider(CountingHttp(FakeFreekassa(api_key="other"))).test_credentials()
    assert not bad.ok and "API-ключ" in bad.message
    html = await provider(CountingHttp(lambda _c: HttpResponse(404, b"<html></html>"))).test_credentials()
    assert not html.ok and "не Freekassa" in html.message
    api = await provider(CountingHttp(FakeFreekassa()), checkout_mode="api").test_credentials()
    assert not api.ok and "API" in api.message


def test_default_urls_are_official() -> None:
    assert DEFAULT_FORM_URL == "https://pay.fk.money/" and DEFAULT_API_URL == "https://api.fk.life/v1"


# ------------------------------------------------------------------------------ through the real core


async def test_notification_then_api_check_credits_once(make_harness: HarnessFactory) -> None:
    desk = FakeFreekassa()
    harness = await make_harness(Freekassa, CONFIG, http=CountingHttp(desk))
    result = await harness.core.create_payment(
        user_id=harness.user_id,
        instance_id=harness.instance.id,
        amount_minor=17_900,
        currency="RUB",
        description="x",
    )
    assert result.checkout.pay_url is not None and result.checkout.external_id == result.payment_id
    order = desk.open_form(result.checkout.pay_url)
    # a notification before Freekassa marks the order paid: the API says «new», nothing is credited
    assert await harness.send(desk.notification(result.payment_id)) == 200
    await harness.core.verify(harness.instance.id, [(result.payment_id, result.payment_id)])
    assert await harness.status(result.payment_id) == "pending"
    order.status = 1
    await harness.core.verify(harness.instance.id, [(result.payment_id, result.payment_id)])
    await harness.core.verify(harness.instance.id, [(result.payment_id, result.payment_id)])
    assert await harness.status(result.payment_id) == "paid" and len(harness.credited) == 1
    order.status = 6
    await harness.core.verify(harness.instance.id, [(result.payment_id, result.payment_id)])
    assert await harness.status(result.payment_id) == "refunded"


async def test_api_amount_mismatch_is_not_credited(make_harness: HarnessFactory) -> None:
    desk = FakeFreekassa()
    harness = await make_harness(Freekassa, CONFIG, http=CountingHttp(desk))
    result = await harness.core.create_payment(
        user_id=harness.user_id,
        instance_id=harness.instance.id,
        amount_minor=17_900,
        currency="RUB",
        description="x",
    )
    desk.add(result.payment_id, 17.9, status=1)
    await harness.core.verify(harness.instance.id, [(result.payment_id, result.payment_id)])
    assert await harness.status(result.payment_id) == "mismatch" and harness.credited == []


async def test_end_to_end_over_real_http(make_harness: HarnessFactory, db: CountingDatabase) -> None:
    async with FakeFreekassa() as desk:
        http = InstanceHttp()
        try:
            config = {**DIRECT, "api_url": desk.api_url}
            harness = await make_harness(Freekassa, config, http=http)
            result = await harness.core.create_payment(
                user_id=harness.user_id,
                instance_id=harness.instance.id,
                amount_minor=49_900,
                currency="RUB",
                description="Пополнение баланса",
            )
            assert result.checkout.pay_url is not None
            desk.open_form(result.checkout.pay_url).status = 1
            resp = await harness.core.handle_webhook(
                harness.instance.id, harness.instance.webhook_token, desk.notification(result.payment_id)
            )
            assert resp.status == 200 and resp.body == b"YES"
            assert await harness.status(result.payment_id) == "paid"
            await harness.core.verify(harness.instance.id, [(result.payment_id, result.payment_id)])
            assert len(harness.credited) == 1
            row = (await db.raw("select paid_amount_minor, paid_currency from payments"))[0]
            assert row["paid_amount_minor"] == 49_900 and row["paid_currency"] == "RUB"
        finally:
            await http.close()


async def test_unparsable_ip_list_accepts_nobody() -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider(allowed_ips="not-an-ip").parse_webhook(note(signed()))
    assert err.value.status == 403
