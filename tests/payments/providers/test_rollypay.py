"""RollyPay plugin: the shared TestKit plus protocol vectors from ``docs/providers/rollypay.md``."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from svbg.payments.core import CheckoutError
from svbg.payments.providers.rollypay import (
    DEFAULT_BASE_URL,
    RollyPay,
    paid_at_from,
    sign,
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
    PaymentIntent,
    PaymentState,
    ProviderError,
    WebhookIgnored,
    WebhookRejected,
    WebhookRequest,
)
from tests.dbkit import CountingDatabase
from tests.fakes.rollypay import (
    API_KEY,
    FAKE_BASE_URL,
    SIGNING_SECRET,
    FakeRollyPay,
    TimestampFormat,
    format_timestamp,
)
from tests.fakes.rollypay import sign as fake_sign
from tests.payments.conftest import FakeAttention
from tests.payments.providers.conftest import HarnessFactory, outcomes, payment_row

CONFIG = {"api_key": API_KEY, "signing_secret": SIGNING_SECRET, "base_url": FAKE_BASE_URL}
PID = "0190f5d2-7b1e-7c3a-9d4e-5f6a7b8c9d0e"
_STATUS = {
    PaymentState.CREATED: "created",
    PaymentState.PROCESSING: "processing",
    PaymentState.PAID: "paid",
    PaymentState.EXPIRED: "expired",
    PaymentState.CANCELED: "canceled",
    PaymentState.FAILED: "failed",
    PaymentState.CHARGEBACK: "chargeback",
    PaymentState.REFUNDED: "refunded",
}


def build(
    payload: dict[str, Any],
    *,
    at: datetime | None = None,
    fmt: TimestampFormat = "seconds",
    secret: str = SIGNING_SECRET,
    headers: dict[str, str] | None = None,
) -> WebhookRequest:
    body = json.dumps(payload).encode()
    stamp = format_timestamp(at or datetime.now(UTC), fmt)
    hdrs = {"X-Timestamp": stamp, "X-Signature": fake_sign(secret, stamp, body)}
    hdrs.update(headers or {})
    return WebhookRequest(body=body, headers=hdrs)


class RollyVectors:
    """:class:`~svbg.payments.testkit.WebhookVectors` of RollyPay (independent signing code)."""

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
            "payment_id": external_id,
            "order_id": payment_id,
            "status": _STATUS[state],
            "amount": amount,
            "currency": currency,
            "metadata": {"payment_id": payment_id},
        }
        return build(payload, at=signed_at, headers={"X-Test-Mode": "true"} if test else None)

    def tamper(self, req: WebhookRequest) -> WebhookRequest:
        return WebhookRequest(body=req.body.replace(b'"paid"', b'"PAID"') + b" ", headers=dict(req.headers))

    def strip_auth(self, req: WebhookRequest) -> WebhookRequest:
        return WebhookRequest(
            body=req.body, headers={k: v for k, v in req.headers.items() if k.lower() != "x-signature"}
        )


def provider(http: CountingHttp | None = None, *, is_test: bool = False, **config: Any) -> RollyPay:
    p = make_provider(RollyPay, {**CONFIG, **config}, http=http, is_test=is_test)
    assert isinstance(p, RollyPay)
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
    check_static(RollyPay)
    assert RollyPay.capabilities.replay_window_s == 300
    assert RollyPay.capabilities.fetch_status and not RollyPay.capabilities.refund
    assert RollyPay.manifest.min_minor == 17_900


async def test_testkit_plugin_level() -> None:
    await check_plugin(RollyPay, CONFIG, RollyVectors())


async def test_testkit_core_level(make_harness: HarnessFactory, db: CountingDatabase) -> None:
    harness = await make_harness(RollyPay, CONFIG, http=CountingHttp(FakeRollyPay()))
    await check_core(harness, RollyVectors())
    assert {"bad_signature", "stale", "applied", "mismatch", "test_rejected"} <= set(await outcomes(db))
    assert len(harness.credited) == 3 and len(harness.refunded) == 1


@pytest.mark.parametrize("domain", [True, False], ids=["domain", "no-domain"])
async def test_testkit_poll_budget(make_harness: HarnessFactory, domain: bool) -> None:
    desk = FakeRollyPay()
    http = CountingHttp(desk)
    harness = await make_harness(RollyPay, CONFIG, http=http, has_domain=domain)

    async def pending_status(call: Any) -> Any:  # every invoice stays «created» on the desk side
        ext = call.url.rsplit("/", 1)[-1]
        if ext not in desk.invoices:
            desk.add_invoice(ext, PID)
        return await desk(call)

    http.responder = pending_status
    used = await check_poll_budget(harness, domain=domain)
    assert used <= (3 if domain else 24)
    assert all(r[0] == "GET" for r in desk.requests)


# ---------------------------------------------------------------------------------- signature vectors


def test_signature_known_answer() -> None:
    """Computed with OpenSSL: ``openssl dgst -sha256 -hmac rp-kat-secret`` over ``1700000000.<body>``."""
    body = b'{"payment_id":"rp_1","status":"paid"}'
    expected = "46248aa0cb4c6a96602ade77c241baa289a8bfc6d74a24951bdf85ffe2e585dd"
    assert sign("rp-kat-secret", "1700000000", body) == expected
    assert fake_sign("rp-kat-secret", "1700000000", body) == expected


@pytest.mark.parametrize("fmt", ["seconds", "ms", "iso", "iso_naive", "iso_z"])
async def test_timestamp_formats_are_normalized(fmt: TimestampFormat) -> None:
    at = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
    req = build({"payment_id": "rp_1", "order_id": PID, "status": "paid", "amount": "179"}, at=at, fmt=fmt)
    event = await provider().parse_webhook(req)
    assert event.signed_at is not None
    assert abs((event.signed_at - at).total_seconds()) < 1


async def test_signature_is_bound_to_the_timestamp() -> None:
    req = build({"payment_id": "rp_1", "order_id": PID, "status": "paid", "amount": "179"})
    headers = dict(req.headers)
    headers["x-timestamp"] = str(int(headers["x-timestamp"]) + 1)  # replay with a «fresh» time
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(WebhookRequest(body=req.body, headers=headers))
    assert err.value.status == 401


@pytest.mark.parametrize(
    "headers",
    [{"X-Signature": ""}, {"X-Timestamp": ""}, {"X-Signature": "00" * 32}],
    ids=["no-signature", "no-timestamp", "zero-signature"],
)
async def test_missing_or_wrong_auth_is_401(headers: dict[str, str]) -> None:
    req = build({"payment_id": "rp_1", "status": "paid"}, headers=headers)
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(req)
    assert err.value.status == 401


async def test_signature_from_another_secret_is_rejected() -> None:
    req = build({"payment_id": "rp_1", "status": "paid"}, secret="someone-else")
    with pytest.raises(WebhookRejected):
        await provider().parse_webhook(req)


async def test_upper_case_signature_with_spaces_is_accepted() -> None:
    req = build({"payment_id": "rp_1", "order_id": PID, "status": "paid", "amount": "179"})
    headers = dict(req.headers)
    headers["x-signature"] = f"  {headers['x-signature'].upper()} "
    event = await provider().parse_webhook(WebhookRequest(body=req.body, headers=headers))
    assert event.state is PaymentState.PAID


async def test_signed_garbage_timestamp_is_401() -> None:
    body = b'{"payment_id":"rp_1","status":"paid"}'
    req = WebhookRequest(
        body=body,
        headers={"X-Timestamp": "yesterday", "X-Signature": fake_sign(SIGNING_SECRET, "yesterday", body)},
    )
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(req)
    assert err.value.status == 401


async def test_every_single_byte_change_is_rejected() -> None:
    """Property: flipping any one byte of the signed body breaks the signature (deterministic sweep)."""
    req = build({"payment_id": "rp_1", "order_id": PID, "status": "paid", "amount": "179.00"})
    plugin = provider()
    for i in range(len(req.body)):
        mutated = bytearray(req.body)
        mutated[i] ^= 0x01
        with pytest.raises(WebhookRejected):
            await plugin.parse_webhook(WebhookRequest(body=bytes(mutated), headers=dict(req.headers)))


# ------------------------------------------------------------------------------------- body parsing


@pytest.mark.parametrize("amount", ["179", "179.00", "179.0", 179, 179.0, "179,00"])
async def test_amount_forms_are_the_same_decimal(amount: Any) -> None:
    event = await provider().parse_webhook(
        build({"payment_id": "rp_1", "order_id": PID, "status": "paid", "amount": amount, "currency": "RUB"})
    )
    assert event.amount == Decimal("179") and event.currency == "RUB"


async def test_amount_property_random_cents() -> None:
    """Property: any amount in kopecks survives the textual forms the desk may use."""
    rnd = random.Random(20261001)
    plugin = provider()
    for _ in range(200):
        minor = rnd.randint(1, 10_000_000)
        rub, kop = divmod(minor, 100)
        forms = [f"{rub}.{kop:02d}", f"{rub}.{kop:02d}0"] + ([str(rub), rub] if kop == 0 else [])
        for form in forms:
            event = await plugin.parse_webhook(
                build({"payment_id": "rp_1", "order_id": PID, "status": "paid", "amount": form})
            )
            assert event.amount is not None and event.amount.scaleb(2) == minor


async def test_test_mode_header_and_body_flag() -> None:
    plugin = provider()
    base = {"payment_id": "rp_1", "order_id": PID, "status": "paid", "amount": "179"}
    assert (await plugin.parse_webhook(build(base, headers={"X-Test-Mode": "true"}))).is_test
    assert (await plugin.parse_webhook(build({**base, "test": True}))).is_test
    assert not (await plugin.parse_webhook(build(base, headers={"X-Test-Mode": "false"}))).is_test


async def test_ids_currency_and_legacy_orders() -> None:
    plugin = provider()
    ev = await plugin.parse_webhook(build({"payment_id": "rp_7", "order_id": PID.upper(), "status": "paid"}))
    assert ev.external_id == "rp_7" and ev.payment_id == PID and ev.currency == "RUB" and ev.amount is None
    legacy = await plugin.parse_webhook(
        build({"payment_id": "rp_8", "order_id": "tc_abc123", "status": "paid"})
    )
    assert legacy.payment_id is None and legacy.external_id == "rp_8"
    meta = await plugin.parse_webhook(build({"status": "expired", "metadata": {"payment_id": PID}}))
    assert meta.payment_id == PID and meta.external_id is None and meta.state is PaymentState.EXPIRED
    cancelled = await plugin.parse_webhook(build({"payment_id": "rp_9", "status": "CANCELLED"}))
    assert cancelled.state is PaymentState.CANCELED


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "paid", "amount": "179"},
        {"payment_id": "rp_1", "status": "paid", "amount": "abc"},
        {"payment_id": "rp_1", "status": "paid", "amount": "-5"},
        {"payment_id": "rp_1", "status": "paid", "currency": "₽₽₽"},
        {"payment_id": "x" * 300, "status": "paid"},
    ],
    ids=["no-ids", "amount-text", "amount-negative", "currency-garbage", "id-too-long"],
)
async def test_malformed_authentic_bodies_are_400(payload: dict[str, Any]) -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(build(payload))
    assert err.value.status == 400


async def test_non_json_and_non_object_bodies_are_400() -> None:
    for body in (b"not json", b"[1, 2]"):
        stamp = str(int(time.time()))
        req = WebhookRequest(
            body=body, headers={"X-Timestamp": stamp, "X-Signature": fake_sign(SIGNING_SECRET, stamp, body)}
        )
        with pytest.raises(WebhookRejected) as err:
            await provider().parse_webhook(req)
        assert err.value.status == 400


async def test_unknown_status_is_acknowledged_and_ignored() -> None:
    with pytest.raises(WebhookIgnored) as err:
        await provider().parse_webhook(build({"payment_id": "rp_1", "status": "on_hold"}))
    assert err.value.response.status == 200


def test_ack_is_json_ok() -> None:
    resp = provider().ack(None)
    assert resp.status == 200 and json.loads(resp.body) == {"ok": True}


def test_paid_at_heuristic() -> None:
    now = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    earlier = now - timedelta(minutes=10)
    assert paid_at_from({"paid_at": int(earlier.timestamp())}, now) == earlier
    assert paid_at_from({"paid_at": str(int(earlier.timestamp() * 1000))}, now) == earlier
    assert paid_at_from({"paid_at": "2026-10-01T11:50:00Z"}, now) == earlier
    assert paid_at_from({"paid_at": "2026-10-01T14:50:00+03:00"}, now) == earlier
    assert paid_at_from({"paid_at": "2026-10-01T11:50:00"}, now) is None  # no zone: MSK or UTC?
    assert (
        paid_at_from({"paid_at": "2026-10-01T11:50:00", "updated_at": int(earlier.timestamp())}, now)
        == earlier
    )
    assert paid_at_from({"paid_at": int((now + timedelta(hours=1)).timestamp())}, now) is None  # future
    assert paid_at_from({"paid_at": int((now - timedelta(days=30)).timestamp())}, now) is None  # too old
    assert (
        paid_at_from({"paid_at": int(earlier.timestamp())}, now, created_at=now - timedelta(minutes=1))
        is None
    )
    assert paid_at_from({"paid_at": True, "updated_at": "garbage"}, now) is None


# ------------------------------------------------------------------------------------------- create


async def test_create_request_shape_and_privacy() -> None:
    desk = FakeRollyPay()
    http = CountingHttp(desk)
    plugin = provider(http, payment_method="sbp")
    checkout = await plugin.create(intent(return_url="https://t.me/svbg_bot"))
    second = await plugin.create(intent(payment_id=str(uuid.uuid4())))
    assert checkout.kind == "url" and checkout.external_id == "rp_000001"
    assert checkout.pay_url == "https://pay.rollypay.fake/rp_000001"
    assert checkout.expires_at is not None and checkout.expires_at.tzinfo is not None
    assert second.external_id == "rp_000002"
    call = http.calls[0]
    assert call.method == "POST" and call.url == f"{FAKE_BASE_URL}/payments"
    assert call.headers["X-API-Key"] == API_KEY
    nonces = {c.headers["X-Nonce"] for c in http.calls}
    assert len(nonces) == 2 and all(uuid.UUID(n).version == 4 for n in nonces)
    body = desk.created[0]
    assert body["amount"] == "179.00" and body["payment_currency"] == "RUB"
    assert body["order_id"] == body["customer_id"] == body["metadata"]["payment_id"] == PID
    assert body["success_redirect_url"] == body["fail_redirect_url"] == "https://t.me/svbg_bot"
    assert body["payment_method"] == "sbp" and "test" not in body
    assert "c0ffee" not in json.dumps(desk.created)  # only the payment id leaves the bot


async def test_create_in_test_mode_sends_test_flag() -> None:
    desk = FakeRollyPay()
    await provider(CountingHttp(desk), is_test=True).create(intent())
    assert desk.created[0]["test"] is True
    assert "success_redirect_url" not in desk.created[0] and "payment_method" not in desk.created[0]


@pytest.mark.parametrize(
    ("status", "retryable", "words"),
    [
        (401, False, "API-ключ"),
        (403, False, "API-ключ"),
        (422, False, "HTTP 422"),
        (500, True, "недоступна"),
        (429, True, "недоступна"),
    ],
    ids=["401", "403", "422", "500", "429"],
)
async def test_create_errors(status: int, retryable: bool, words: str) -> None:
    desk = FakeRollyPay()
    desk.fail_create = status
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(desk)).create(intent())
    assert err.value.retryable is retryable and words in err.value.human
    assert API_KEY not in err.value.human


async def test_create_with_a_bad_answer() -> None:
    async def no_url(_call: Any) -> Any:
        from svbg.sdk import HttpResponse

        return HttpResponse(200, b'{"payment_id": "rp_1"}')

    with pytest.raises(ProviderError):
        await provider(CountingHttp(no_url)).create(intent())


async def test_wrong_api_key_is_reported_without_leaking_it() -> None:
    desk = FakeRollyPay(api_key="other")
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(desk)).create(intent())
    assert not err.value.retryable and API_KEY not in str(err.value)


# ------------------------------------------------------------------------------------------- status


async def test_fetch_status() -> None:
    desk = FakeRollyPay()
    desk.add_invoice("rp_1", PID, amount="179.00")
    desk.set_status("rp_1", "paid")
    desk.add_invoice("rp_2", "tc_legacy", status="expired")
    plugin = provider(CountingHttp(desk))
    statuses = await plugin.fetch_status(["rp_1", "rp_2", "rp_missing", "rp_1"])
    assert [s.external_id for s in statuses] == ["rp_1", "rp_2"]
    paid, expired = statuses
    assert paid.state is PaymentState.PAID and paid.amount == Decimal("179") and paid.payment_id == PID
    assert paid.paid_at is not None
    assert expired.state is PaymentState.EXPIRED and expired.payment_id is None
    assert desk.status_calls == ["rp_1", "rp_2", "rp_missing"]


async def test_fetch_status_reports_a_test_invoice() -> None:
    # The core refuses a test invoice on a live instance only if the status says it is one.
    desk = FakeRollyPay()
    desk.add_invoice("rp_t", PID).test = True
    desk.set_status("rp_t", "paid")
    desk.add_invoice("rp_l", "tc_legacy")
    statuses = await provider(CountingHttp(desk)).fetch_status(["rp_t", "rp_l"])
    assert [s.is_test for s in statuses] == [True, False]


async def test_fetch_status_path_is_quoted() -> None:
    desk = FakeRollyPay()
    http = CountingHttp(desk)
    await provider(http).fetch_status(["a/../b"])
    assert http.calls[0].url.endswith("/payments/a%2F..%2Fb")


@pytest.mark.parametrize(("status", "retryable"), [(503, True), (401, False)], ids=["503", "401"])
async def test_fetch_status_errors(status: int, retryable: bool) -> None:
    desk = FakeRollyPay()
    desk.add_invoice("rp_1", PID)
    desk.fail_status = status
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(desk)).fetch_status(["rp_1"])
    assert err.value.retryable is retryable


# -------------------------------------------------------------------------------------------- probe


async def test_test_credentials() -> None:
    desk = FakeRollyPay()
    ok = await provider(CountingHttp(desk)).test_credentials()
    assert ok.ok and desk.created == []  # no side effects
    bad = await provider(CountingHttp(FakeRollyPay(api_key="other"))).test_credentials()
    assert not bad.ok and "API-ключ" in bad.message
    down = FakeRollyPay()
    down.fail_with = 503
    assert not (await provider(CountingHttp(down)).test_credentials()).ok
    wrong_base = await provider(
        CountingHttp(FakeRollyPay()), base_url="https://rollypay.fake/v2"
    ).test_credentials()
    assert not wrong_base.ok and "Адрес API" in wrong_base.message


def test_config_hides_secrets_and_defaults() -> None:
    plugin = provider(base_url=None)
    text = repr(plugin.config)
    assert API_KEY not in text and SIGNING_SECRET not in text
    assert plugin.config.base_url == DEFAULT_BASE_URL
    assert set(plugin.config.secret_values()) == {API_KEY, SIGNING_SECRET}


# ------------------------------------------------------------------------------ through the real core


async def test_window_applies_to_every_status_and_ntp_alert(
    make_harness: HarnessFactory, db: CountingDatabase
) -> None:
    attention = FakeAttention()
    harness = await make_harness(
        RollyPay, CONFIG, http=CountingHttp(FakeRollyPay()), core_kwargs={"attention": attention}
    )
    vectors = RollyVectors()
    pid = await harness.pending(external_id="rp_w")
    now = datetime.now(UTC)
    assert (
        await harness.send(
            vectors.webhook(
                PaymentState.PAID,
                payment_id=pid,
                external_id="rp_w",
                amount="179",
                currency="RUB",
                signed_at=now,
            )
        )
        == 200
    )
    assert await harness.status(pid) == "paid"
    # a refund notice signed 301 s ago is rejected too (window for ALL webhooks), the payment stays paid
    for n in range(6):
        stale = vectors.webhook(
            PaymentState.REFUNDED,
            payment_id=pid,
            external_id="rp_w",
            amount="179",
            currency="RUB",
            signed_at=now - timedelta(seconds=301 + n),
        )
        assert await harness.send(stale) == 401
    assert await harness.status(pid) == "paid"
    await harness.core.drain()
    assert "payments:clock_skew" in attention.dedup_keys()
    assert (await outcomes(db)).count("stale") == 6
    # within the window from the future side (+299 s) it is accepted
    fresh = vectors.webhook(
        PaymentState.REFUNDED,
        payment_id=pid,
        external_id="rp_w",
        amount="179",
        currency="RUB",
        signed_at=now + timedelta(seconds=299),
    )
    assert await harness.send(fresh) == 200
    assert await harness.status(pid) == "refunded"


async def test_test_event_on_test_instance_is_accepted(make_harness: HarnessFactory) -> None:
    harness = await make_harness(RollyPay, CONFIG, http=CountingHttp(FakeRollyPay()), is_test=True)
    pid = await harness.pending(external_id="rp_t")
    req = RollyVectors().webhook(
        PaymentState.PAID,
        payment_id=pid,
        external_id="rp_t",
        amount="179.00",
        currency="RUB",
        signed_at=datetime.now(UTC),
        test=True,
    )
    assert await harness.send(req) == 200
    assert await harness.status(pid) == "paid"


async def test_concurrent_webhooks_credit_once(make_harness: HarnessFactory) -> None:
    harness = await make_harness(RollyPay, CONFIG, http=CountingHttp(FakeRollyPay()))
    pid = await harness.pending(external_id="rp_c")
    now = datetime.now(UTC)
    vectors = RollyVectors()
    reqs = [
        vectors.webhook(
            PaymentState.PAID,
            payment_id=pid,
            external_id="rp_c",
            amount=amount,
            currency="RUB",
            signed_at=now + timedelta(seconds=k % 3),
        )
        for k, amount in enumerate(["179", "179.00", "179", "179.0", "179.00", "179"])
    ]
    codes = await asyncio.gather(*(harness.send(r) for r in reqs))
    assert set(codes) == {200}
    assert len(harness.credited) == 1 and await harness.status(pid) == "paid"


async def test_end_to_end_over_real_http(make_harness: HarnessFactory, db: CountingDatabase) -> None:
    """Real aiohttp client (InstanceHttp) against the fake desk server: create, webhook, status check."""
    async with FakeRollyPay() as desk:
        http = InstanceHttp()
        try:
            harness = await make_harness(RollyPay, {**CONFIG, "base_url": desk.base_url}, http=http)
            result = await harness.core.create_payment(
                user_id=harness.user_id,
                instance_id=harness.instance.id,
                amount_minor=17_900,
                currency="RUB",
                description="Пополнение баланса",
            )
            ext = result.checkout.external_id
            assert ext is not None and desk.invoices[ext].order_id == result.payment_id
            row = await payment_row(db, result.payment_id)
            assert row["external_id"] == ext and row["poll_plan"] == "domain"
            # the desk says paid, the webhook is lost: the reconciler's status check credits it
            desk.set_status(ext, "paid")
            await harness.core.verify(harness.instance.id, [(ext, result.payment_id)])
            assert await harness.status(result.payment_id) == "paid"
            # the late webhook is a no-op
            assert await harness.send(desk.webhook(ext)) == 200
            assert len(harness.credited) == 1
            # probe through the same transport
            assert (await harness.instance.provider.test_credentials()).ok
        finally:
            await http.close()


async def test_create_failure_marks_payment_failed_and_late_payment_wins(
    make_harness: HarnessFactory, db: CountingDatabase
) -> None:
    desk = FakeRollyPay()
    desk.fail_create = 502
    harness = await make_harness(RollyPay, CONFIG, http=CountingHttp(desk))
    with pytest.raises(CheckoutError) as err:
        await harness.core.create_payment(
            user_id=harness.user_id,
            instance_id=harness.instance.id,
            amount_minor=17_900,
            currency="RUB",
            description="x",
        )
    assert err.value.retryable
    pid = (await db.raw("select id from payments"))[0]["id"]
    assert await harness.status(pid) == "failed"
    desk.add_invoice("rp_late", pid)
    assert await harness.send(desk.webhook("rp_late", "paid", amount="179.00")) == 200
    assert await harness.status(pid) == "paid"


async def test_below_minimum_is_refused_before_the_desk(make_harness: HarnessFactory) -> None:
    desk = FakeRollyPay()
    harness = await make_harness(RollyPay, CONFIG, http=CountingHttp(desk))
    with pytest.raises(CheckoutError, match="Минимальная сумма"):
        await harness.core.create_payment(
            user_id=harness.user_id,
            instance_id=harness.instance.id,
            amount_minor=10_000,
            currency="RUB",
            description="x",
        )
    assert desk.created == []


async def test_parse_is_fast_and_logs_no_secrets(caplog: pytest.LogCaptureFixture) -> None:
    plugin = provider()
    reqs = [
        build({"payment_id": f"rp_{i}", "order_id": PID, "status": "paid", "amount": "179"})
        for i in range(300)
    ]
    started = time.perf_counter()
    with caplog.at_level(logging.DEBUG):
        for req in reqs:
            await plugin.parse_webhook(req)
        with pytest.raises(WebhookIgnored):
            await plugin.parse_webhook(build({"payment_id": "rp_x", "status": "weird"}))
    per_call_ms = (time.perf_counter() - started) * 1000 / len(reqs)
    assert per_call_ms < 5, per_call_ms  # a tiny share of the 100 ms webhook budget
    assert SIGNING_SECRET not in caplog.text and API_KEY not in caplog.text


async def test_webhook_over_the_real_route(make_harness: HarnessFactory) -> None:
    """The fake desk POSTs a signed webhook to ``/webhooks/pay/{id}/{token}`` over a socket."""
    from aiohttp import web

    from svbg.web.routes.payments import payment_routes

    desk = FakeRollyPay()
    harness = await make_harness(RollyPay, CONFIG, http=CountingHttp(desk))
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
        timings: list[float] = []
        for n in range(5):
            pid = await harness.pending(external_id=f"rp_route_{n}")
            desk.add_invoice(f"rp_route_{n}", pid)
            desk.set_status(f"rp_route_{n}", "paid")
            started = time.perf_counter()
            assert await desk.send_webhook(url, f"rp_route_{n}") == 200
            timings.append(time.perf_counter() - started)
            assert await harness.status(pid) == "paid"
        assert await desk.send_webhook(url, "rp_route_0", secret="wrong") == 401
        assert await desk.send_webhook(url.replace(inst.webhook_token, "x" * 43), "rp_route_0") == 404
        assert len(harness.credited) == 5
        assert sorted(timings)[2] < 0.5, timings  # generous bound; the 100 ms budget is the core's gate
    finally:
        await runner.cleanup()
