"""TabPay plugin: the shared TestKit plus the vectors V1–V6 and behaviours of ``docs/providers/tabpay.md``."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import random
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from svbg.payments.core import CheckoutError
from svbg.payments.providers.tabpay import (
    DEFAULT_BASE_URL,
    TabPay,
    kopecks_to_rub,
    sign_v2,
)
from svbg.payments.registry import InstanceHttp
from svbg.payments.testkit import (
    CountingHttp,
    HttpCall,
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
    WebhookIgnored,
    WebhookRejected,
    WebhookRequest,
)
from tests.dbkit import CountingDatabase
from tests.fakes.tabpay import (
    API_KEY,
    FAKE_BASE_URL,
    WEBHOOK_SECRET,
    FakeTabPay,
    signed,
)
from tests.fakes.tabpay import sign_v1 as fake_sign_v1
from tests.fakes.tabpay import sign_v2 as fake_sign_v2
from tests.payments.providers.conftest import HarnessFactory, outcomes, payment_row

CONFIG = {"api_key": API_KEY, "webhook_secret": WEBHOOK_SECRET, "base_url": FAKE_BASE_URL}
PID = "0190f5d2-7b1e-7c3a-9d4e-5f6a7b8c9d0e"
EXT = "6b9d2c88-4b1a-4f0e-9c37-1f2ab34cd561"
_STATUS = {
    PaymentState.CREATED: "CREATED",
    PaymentState.PROCESSING: "PENDING",
    PaymentState.PAID: "SUCCESS",
    PaymentState.EXPIRED: "EXPIRED",
    PaymentState.CANCELED: "CANCELED",
    PaymentState.FAILED: "FAILED",
    PaymentState.CHARGEBACK: "REFUNDED",  # TabPay has no chargeback status; REFUNDED is the only reversal
    PaymentState.REFUNDED: "REFUNDED",
}

# ---- specification §4: secret, bodies and expected hex (computed with OpenSSL by the spec session) ----
SPEC_SECRET = "whsec_svbg_test"
B1 = (
    b'{"id":"6b9d2c88-4b1a-4f0e-9c37-1f2ab34cd561","orderId":"order-1001","status":"SUCCESS",'
    b'"amountKopecks":19900,"telegramId":"987654321","metadata":{"productId":42,"tariff":"month"},'
    b'"test":false}'
)
B2 = (
    b'{"id":"test-3f1c","orderId":"order-1001","status":"SUCCESS","amountKopecks":19900,'
    b'"telegramId":null,"metadata":null,"test":true}'
)
TS = "1785400000"
V1 = "330609ec0101721771995149fd99bf3fca7ecd714cfbe49ad37d27fe45ff9b88"
V2 = "e62ad06f6f6a06605285d8309401df31650d8ea56791bdf266fec9a6ed04d400"
V3 = "ffced8cc61f1d74a5c5f9cf74994697ca7671926690f54ba4c09a1bd47c8c20d"
V4 = "9b73ab9f3199c3f256ab5358b9e0d7dc3536176a6348f7e6139c76e146025bc7"
V5 = "06a1934c5f7e27eb8dfbd3b04b1a9836812855781852e9a0e870e748caeb2c99"
V6 = "b1a2c56f9d3b371a64efe34a8c097bd0fea3f839dbbee78ac3e4103f023e462b"
SPEC_CONFIG = {**CONFIG, "webhook_secret": SPEC_SECRET}


def kopecks(amount: str) -> int:
    value = Decimal(amount).scaleb(2)
    assert value == value.to_integral_value()
    return int(value)


def build(
    payload: dict[str, Any] | bytes,
    *,
    at: datetime | None = None,
    stamp: str | None = None,
    secret: str = WEBHOOK_SECRET,
    drop: tuple[str, ...] = (),
) -> WebhookRequest:
    body, headers = signed(payload, at=at, stamp=stamp, secret=secret)
    return WebhookRequest(body=body, headers={k: v for k, v in headers.items() if k not in drop})


def spec_req(body: bytes, stamp: str, signature: str, **extra: str) -> WebhookRequest:
    return WebhookRequest(body=body, headers={"X-Timestamp": stamp, "X-Signature-V2": signature, **extra})


class TabPayVectors:
    """:class:`~svbg.payments.testkit.WebhookVectors` of TabPay (independent signing code)."""

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
        assert currency == "RUB"
        payload = {
            "id": external_id,
            "orderId": payment_id,
            "status": _STATUS[state],
            "amountKopecks": kopecks(amount),
            "telegramId": None,
            "metadata": None,
            "test": test,
        }
        return build(payload, at=signed_at)

    def tamper(self, req: WebhookRequest) -> WebhookRequest:
        return WebhookRequest(
            body=req.body.replace(b'"SUCCESS"', b'"success"') + b" ", headers=dict(req.headers)
        )

    def strip_auth(self, req: WebhookRequest) -> WebhookRequest:
        """Without ``X-Signature-V2`` — the legacy v1 header stays, and must not be enough."""
        return WebhookRequest(
            body=req.body, headers={k: v for k, v in req.headers.items() if k.lower() != "x-signature-v2"}
        )


def provider(http: CountingHttp | None = None, **config: Any) -> TabPay:
    p = make_provider(TabPay, {**CONFIG, **config}, http=http)
    assert isinstance(p, TabPay)
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


def at_ts(stamp: str) -> datetime:
    return datetime.fromtimestamp(int(stamp), tz=UTC)


# ------------------------------------------------------------------------------------------- TestKit


def test_static_contract() -> None:
    check_static(TabPay)
    caps, man = TabPay.capabilities, TabPay.manifest
    assert caps.webhook_auth == "signature" and caps.replay_window_s == 300
    assert caps.fetch_status and not caps.batch_status and not caps.refund and not caps.recurring
    assert caps.redirect and not caps.in_chat_invoice
    assert man.method_kinds == (MethodKind.SBP, MethodKind.CARD) and man.currencies == ("RUB",)
    assert (man.min_minor, man.max_minor) == (100, 10_000_000_000)
    for name, fld in man.config.fields().items():
        assert fld.where, f"no «где взять» for {name}"


async def test_testkit_plugin_level() -> None:
    await check_plugin(TabPay, CONFIG, TabPayVectors())


async def test_testkit_core_level(make_harness: HarnessFactory, db: CountingDatabase) -> None:
    harness = await make_harness(TabPay, CONFIG, http=CountingHttp(FakeTabPay()))
    await check_core(harness, TabPayVectors())
    assert {"bad_signature", "stale", "applied", "mismatch", "test_rejected"} <= set(await outcomes(db))
    assert len(harness.credited) == 3 and len(harness.refunded) == 1


@pytest.mark.parametrize("domain", [True, False], ids=["domain", "no-domain"])
async def test_testkit_poll_budget(make_harness: HarnessFactory, domain: bool) -> None:
    desk = FakeTabPay()
    http = CountingHttp(desk)
    harness = await make_harness(TabPay, CONFIG, http=http, has_domain=domain)

    def still_created(call: HttpCall) -> HttpResponse:  # every invoice stays CREATED at TabPay
        ext = call.url.rsplit("/", 1)[-1]
        data = {"id": ext, "orderId": PID, "status": "CREATED", "amountKopecks": 17_900, "isTest": False}
        return HttpResponse(200, json.dumps(data).encode())

    http.responder = still_created
    used = await check_poll_budget(harness, domain=domain)
    assert 0 < used <= (3 if domain else 24)
    assert all(c.method == "GET" and "/v1/payments/" in c.url for c in http.calls)


# --------------------------------------------------------------------------- specification vectors §4


def test_spec_vectors_v1_to_v6() -> None:
    assert len(B1) == 192
    assert (
        hashlib.sha256(B1).hexdigest() == "47016104d641bbb131cd25d437d46adb8c882a03742ec5d2fcba7fac04d1ae48"
    )
    assert sign_v2(SPEC_SECRET, TS, B1) == fake_sign_v2(SPEC_SECRET, TS, B1) == V2
    assert sign_v2(SPEC_SECRET, "1785400001", B1) == V3
    assert sign_v2("whsec_svbg_tesT", TS, B1) == V4
    assert sign_v2(SPEC_SECRET, TS, B2) == V6
    assert fake_sign_v1(SPEC_SECRET, B1) == V1 and fake_sign_v1(SPEC_SECRET, B2) == V5


async def test_v2_accepts_b1() -> None:
    event = await provider(**SPEC_CONFIG).parse_webhook(spec_req(B1, TS, V2, **{"X-Signature": V1}))
    assert event.state is PaymentState.PAID and event.external_id == EXT
    assert event.payment_id is None  # «order-1001» is not one of our ids
    assert event.amount == Decimal("199.00") and event.currency == "RUB"
    assert event.signed_at == at_ts(TS) and not event.is_test
    assert "987654321" not in json.dumps(dict(event.summary))  # telegramId stays out of the log


async def test_v3_timestamp_is_bound_to_the_signature() -> None:
    plugin = provider(**SPEC_CONFIG)
    with pytest.raises(WebhookRejected) as err:
        await plugin.parse_webhook(spec_req(B1, "1785400001", V2))
    assert err.value.status == 401
    event = await plugin.parse_webhook(spec_req(B1, "1785400001", V3))
    assert event.signed_at == at_ts("1785400001")


async def test_v4_foreign_secret_is_rejected() -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider(**SPEC_CONFIG).parse_webhook(spec_req(B1, TS, V4))
    assert err.value.status == 401


async def test_v1_alone_is_not_enough() -> None:
    req = WebhookRequest(body=B1, headers={"X-Timestamp": TS, "X-Signature": V1})
    with pytest.raises(WebhookRejected) as err:
        await provider(**SPEC_CONFIG).parse_webhook(req)
    assert err.value.status == 401


async def test_reformatted_body_is_rejected() -> None:
    pretty = json.dumps(json.loads(B1), indent=1).encode()
    reordered = json.dumps(dict(reversed(json.loads(B1).items())), separators=(",", ":")).encode()
    for body in (pretty, reordered):
        with pytest.raises(WebhookRejected):
            await provider(**SPEC_CONFIG).parse_webhook(spec_req(body, TS, V2))


async def test_upper_case_signature_is_normalized() -> None:
    """Decision (§4 asks to make one): hex is case-insensitive, an upper-case V2 is accepted."""
    event = await provider(**SPEC_CONFIG).parse_webhook(spec_req(B1, TS, f" {V2.upper()} "))
    assert event.state is PaymentState.PAID


async def test_v6_test_webhook_from_the_cabinet_button() -> None:
    event = await provider(**SPEC_CONFIG).parse_webhook(spec_req(B2, TS, V6, **{"X-Signature": V5}))
    assert event.is_test and event.external_id == "test-3f1c" and event.payment_id is None
    assert event.amount == Decimal("199")


@pytest.mark.parametrize(
    ("offset", "code"), [(-300, 200), (0, 200), (300, 200), (301, 401), (-301, 401)], ids=str
)
async def test_freshness_window_on_b1(make_harness: HarnessFactory, offset: int, code: int) -> None:
    harness = await make_harness(TabPay, SPEC_CONFIG, http=CountingHttp(FakeTabPay()))
    harness.core._clock = lambda: at_ts(TS) + timedelta(seconds=offset)
    assert await harness.send(spec_req(B1, TS, V2)) == code


async def test_b2_on_a_live_instance_credits_nothing(
    make_harness: HarnessFactory, db: CountingDatabase
) -> None:
    """Spec: «test: true» must not credit. The core answers 400 (``test_rejected``) on a live instance —
    TabPay retries, which is harmless."""
    harness = await make_harness(TabPay, SPEC_CONFIG, http=CountingHttp(FakeTabPay()))
    harness.core._clock = lambda: at_ts(TS)
    assert await harness.send(spec_req(B2, TS, V6)) == 400
    assert harness.credited == [] and await outcomes(db) == ["test_rejected"]


# ------------------------------------------------------------------------------------ signature edges


@pytest.mark.parametrize(
    "drop", [("X-Signature-V2",), ("X-Timestamp",)], ids=["no-signature", "no-timestamp"]
)
async def test_missing_headers_are_401(drop: tuple[str, ...]) -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(build({"id": EXT, "status": "SUCCESS"}, drop=drop))
    assert err.value.status == 401


@pytest.mark.parametrize("stamp", ["yesterday", "1785400000.5", "-1785400000", "", "2026-10-02T00:00:00Z"])
async def test_signed_non_numeric_timestamp_is_401(stamp: str) -> None:
    body = b'{"id":"x","status":"SUCCESS"}'
    req = spec_req(body, stamp, fake_sign_v2(WEBHOOK_SECRET, stamp, body))
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(req)
    assert err.value.status == 401


async def test_every_single_byte_change_is_rejected() -> None:
    req = build({"id": EXT, "orderId": PID, "status": "SUCCESS", "amountKopecks": 17_900, "test": False})
    plugin = provider()
    for i in range(len(req.body)):
        mutated = bytearray(req.body)
        mutated[i] ^= 0x01
        with pytest.raises(WebhookRejected):
            await plugin.parse_webhook(WebhookRequest(body=bytes(mutated), headers=dict(req.headers)))


# ------------------------------------------------------------------------------------- body parsing


@pytest.mark.parametrize(
    ("raw", "state"),
    [
        ("SUCCESS", PaymentState.PAID),
        ("FAILED", PaymentState.FAILED),
        ("EXPIRED", PaymentState.EXPIRED),
        ("REFUNDED", PaymentState.REFUNDED),
        ("CANCELED", PaymentState.CANCELED),
        ("PENDING", PaymentState.PROCESSING),
        ("CREATED", PaymentState.CREATED),
    ],
)
async def test_status_map(raw: str, state: PaymentState) -> None:
    event = await provider().parse_webhook(
        build({"id": EXT, "orderId": PID, "status": raw, "amountKopecks": 100})
    )
    assert event.state is state and event.payment_id == PID and event.amount == Decimal("1")


async def test_unknown_status_is_acknowledged_and_ignored(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING), pytest.raises(WebhookIgnored) as err:
        await provider().parse_webhook(build({"id": EXT, "orderId": PID, "status": "CHARGEBACK"}))
    assert err.value.response.status == 200 and "CHARGEBACK" in caplog.text


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "SUCCESS", "amountKopecks": 100},
        {"id": EXT, "status": "SUCCESS", "amountKopecks": 199.5},
        {"id": EXT, "status": "SUCCESS", "amountKopecks": -100},
        {"id": EXT, "status": "SUCCESS", "amountKopecks": "19900"},
        {"id": EXT, "status": "SUCCESS", "amountKopecks": True},
        {"id": "x" * 300, "status": "SUCCESS"},
    ],
    ids=["no-ids", "fraction", "negative", "string", "bool", "id-too-long"],
)
async def test_malformed_authentic_bodies_are_400(payload: dict[str, Any]) -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(build(payload))
    assert err.value.status == 400


async def test_non_json_and_non_object_bodies_are_400() -> None:
    for body in (b"not json", b"[1, 2]", b"\xff\xfe"):
        with pytest.raises(WebhookRejected) as err:
            await provider().parse_webhook(build(body))
        assert err.value.status == 400


async def test_test_flag_is_read_by_value() -> None:
    base = {"id": EXT, "orderId": PID, "status": "SUCCESS", "amountKopecks": 17_900}
    plugin = provider()
    assert (await plugin.parse_webhook(build({**base, "test": True}))).is_test
    for value in (False, "false", 0, None):
        assert not (await plugin.parse_webhook(build({**base, "test": value}))).is_test


def test_kopecks_to_rub_property() -> None:
    rnd = random.Random(20261002)
    for _ in range(500):
        minor = rnd.randint(100, 10_000_000_000)
        rub = kopecks_to_rub(minor)
        assert rub.scaleb(2) == minor and rub == Decimal(f"{minor // 100}.{minor % 100:02d}")
    assert kopecks_to_rub(Decimal("19900")) == Decimal("199")
    for bad in (1.5, "100", True, -1, Decimal("1.5"), Decimal("NaN"), None):
        with pytest.raises(ValueError):
            kopecks_to_rub(bad)


# ------------------------------------------------------------------------------ through the real core


async def test_lifecycle_through_the_core(make_harness: HarnessFactory, db: CountingDatabase) -> None:
    """Retries with a fresh timestamp, late payment after EXPIRED/FAILED, REFUNDED, unknown status."""
    desk = FakeTabPay()
    harness = await make_harness(TabPay, CONFIG, http=CountingHttp(desk))
    now = datetime.now(UTC)
    pid = await harness.pending()
    pay = desk.add_payment(pid)
    # the same body redelivered hours later with a new X-Timestamp / X-Signature-V2: one credit
    for k in range(3):
        assert await harness.send(desk.webhook(pay.id, "SUCCESS", at=now + timedelta(seconds=k))) == 200
    assert len(harness.credited) == 1 and await harness.status(pid) == "paid"
    assert (await payment_row(db, pid))["external_id"] == pay.id
    # REFUNDED after SUCCESS
    assert await harness.send(desk.webhook(pay.id, "REFUNDED", at=now)) == 200
    assert await harness.status(pid) == "refunded" and len(harness.refunded) == 1
    # unknown status: 200, nothing changes
    assert await harness.send(desk.webhook(pay.id, "CHARGEBACK", at=now)) == 200
    assert await harness.status(pid) == "refunded"
    # SUCCESS after EXPIRED and after FAILED (late SBP payment) is credited
    for final in ("EXPIRED", "FAILED"):
        late = await harness.pending()
        lp = desk.add_payment(late)
        assert await harness.send(desk.webhook(lp.id, final, at=now)) == 200
        assert await harness.status(late) == final.lower()
        assert await harness.send(desk.webhook(lp.id, "SUCCESS", at=now)) == 200
        assert await harness.status(late) == "paid"
    # CANCELED closes a pending payment
    canceled = await harness.pending()
    cp = desk.add_payment(canceled)
    assert await harness.send(desk.webhook(cp.id, "CANCELED", at=now)) == 200
    assert await harness.status(canceled) == "canceled"
    # a different amount → mismatch, nothing credited
    mm = await harness.pending()
    mp = desk.add_payment(mm)
    before = len(harness.credited)
    assert await harness.send(desk.webhook(mp.id, "SUCCESS", amount_kopecks=17_800, at=now)) == 200
    assert await harness.status(mm) == "mismatch" and len(harness.credited) == before


async def test_sandbox_instance_accepts_test_payments(make_harness: HarnessFactory) -> None:
    desk = FakeTabPay(sandbox=True)
    harness = await make_harness(TabPay, CONFIG, http=CountingHttp(desk), is_test=True)
    pid = await harness.pending()
    pay = desk.add_payment(pid)
    pay.is_test = True
    assert await harness.send(desk.webhook(pay.id, "SUCCESS")) == 200
    assert await harness.status(pid) == "paid"


async def test_below_minimum_is_refused_before_tabpay(make_harness: HarnessFactory) -> None:
    desk = FakeTabPay()
    harness = await make_harness(TabPay, CONFIG, http=CountingHttp(desk))
    with pytest.raises(CheckoutError, match="Минимальная сумма"):
        await harness.core.create_payment(
            user_id=harness.user_id,
            instance_id=harness.instance.id,
            amount_minor=99,
            currency="RUB",
            description="x",
        )
    assert desk.created == []


async def test_end_to_end_over_real_http(make_harness: HarnessFactory, db: CountingDatabase) -> None:
    """Real aiohttp client against the fake server: create, lost webhook → status check, late webhook."""
    async with FakeTabPay() as desk:
        http = InstanceHttp()
        try:
            harness = await make_harness(TabPay, {**CONFIG, "base_url": desk.base_url}, http=http)
            result = await harness.core.create_payment(
                user_id=harness.user_id,
                instance_id=harness.instance.id,
                amount_minor=17_900,
                currency="RUB",
                description="Пополнение баланса",
            )
            ext = result.checkout.external_id
            assert ext is not None and desk.payments[ext].order_id == result.payment_id
            assert result.checkout.pay_url == f"https://tabpay.fake/pay/{ext}"
            assert (await payment_row(db, result.payment_id))["poll_plan"] == "domain"
            desk.set_status(ext, "SUCCESS")
            await harness.core.verify(harness.instance.id, [(ext, result.payment_id)])
            assert await harness.status(result.payment_id) == "paid"
            assert await harness.send(desk.webhook(ext)) == 200
            assert len(harness.credited) == 1
            assert (await harness.instance.provider.test_credentials()).ok
        finally:
            await http.close()


# ------------------------------------------------------------------------------------------- create


async def test_create_request_shape_and_privacy() -> None:
    desk = FakeTabPay()
    http = CountingHttp(desk)
    checkout = await provider(http).create(intent(return_url="https://t.me/svbg_bot"))
    pay = desk.by_order(PID)
    assert checkout.kind == "url" and checkout.external_id == pay.id
    assert checkout.pay_url == f"https://tabpay.fake/pay/{pay.id}" and checkout.expires_at is None
    call = http.calls[0]
    assert call.method == "POST" and call.url == f"{FAKE_BASE_URL}/v1/payments"
    assert call.headers["X-Api-Key"] == API_KEY
    body = desk.created[0]
    assert body == {
        "orderId": PID,
        "amountKopecks": 17_900,
        "description": "Пополнение баланса на 179 ₽",
        "successUrl": "https://t.me/svbg_bot",
        "failUrl": "https://t.me/svbg_bot",
    }
    assert type(body["amountKopecks"]) is int and "c0ffee" not in json.dumps(desk.created)


async def test_create_method_comes_from_config_not_the_hint() -> None:
    desk = FakeTabPay()
    await provider(CountingHttp(desk)).create(intent(method_hint=MethodKind.SBP))
    assert "method" not in desk.created[0]  # empty setting: the buyer chooses on the TabPay page
    desk2 = FakeTabPay()
    await provider(CountingHttp(desk2), method="CARD").create(intent())
    assert desk2.created[0]["method"] == "CARD"


async def test_create_with_a_disabled_method_is_a_clear_error() -> None:
    desk = FakeTabPay(methods=("SBP",))
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(desk), method="CARD").create(intent())
    assert not err.value.retryable and "способ оплаты" in err.value.human


async def test_lost_answer_reuses_the_invoice() -> None:
    """5xx after TabPay stored the invoice: GET ?orderId= finds it, no second POST."""
    desk = FakeTabPay()
    desk.lose_create = True
    http = CountingHttp(desk)
    checkout = await provider(http).create(intent())
    assert len(desk.payments) == 1 and checkout.external_id == desk.by_order(PID).id
    assert [(c.method, c.params.get("orderId")) for c in http.calls] == [("POST", None), ("GET", PID)]


async def test_transport_failure_looks_up_then_creates_once() -> None:
    desk = FakeTabPay()
    attempts: list[str] = []

    async def flaky(call: HttpCall) -> HttpResponse:
        attempts.append(call.method)
        if call.method == "POST" and attempts.count("POST") == 1:
            raise ProviderError("нет связи", retryable=True)
        return await desk(call)

    checkout = await provider(CountingHttp(flaky)).create(intent())
    assert attempts == ["POST", "GET", "POST"] and checkout.external_id == desk.by_order(PID).id


async def test_repeated_create_after_409_reuses_a_pending_invoice() -> None:
    desk = FakeTabPay()
    existing = desk.add_payment(PID, status="PENDING")
    checkout = await provider(CountingHttp(desk)).create(intent())
    assert checkout.external_id == existing.id and len(desk.payments) == 1


@pytest.mark.parametrize(
    ("status", "amount"), [("CANCELED", 17_900), ("SUCCESS", 17_900), ("CREATED", 100)], ids=str
)
async def test_409_with_a_closed_or_different_invoice_is_refused(status: str, amount: int) -> None:
    desk = FakeTabPay()
    desk.add_payment(PID, amount_kopecks=amount, status=status)
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(desk)).create(intent())
    assert not err.value.retryable and err.value.status == 409


@pytest.mark.parametrize(
    ("status", "retryable", "words"),
    [
        (401, False, "API-ключ"),
        (400, False, "(HTTP 400): fault"),
        (429, True, "недоступна"),
        (503, True, "недоступна"),
    ],
    ids=["401", "400", "429", "503"],
)
async def test_create_errors(status: int, retryable: bool, words: str) -> None:
    desk = FakeTabPay()
    desk.fail_create = status
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(desk)).create(intent())
    assert err.value.retryable is retryable and words in err.value.human
    assert API_KEY not in err.value.human


async def test_validation_messages_reach_the_owner() -> None:
    async def invalid(_call: HttpCall) -> HttpResponse:
        body = {
            "statusCode": 400,
            "message": ["amountKopecks must not be less than 100"],
            "error": "Bad Request",
        }
        return HttpResponse(400, json.dumps(body).encode())

    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(invalid)).create(intent())
    assert "amountKopecks must not be less than 100" in err.value.human


@pytest.mark.parametrize(
    "kw", [{"currency": "USD"}, {"amount_minor": 99}, {"amount_minor": 10_000_000_001}], ids=str
)
async def test_create_refuses_what_tabpay_cannot_take(kw: dict[str, Any]) -> None:
    http = CountingHttp(FakeTabPay())
    with pytest.raises(ProviderError):
        await provider(http).create(intent(**kw))
    assert http.requests == 0


async def test_create_with_a_bad_answer() -> None:
    async def no_url(_call: HttpCall) -> HttpResponse:
        return HttpResponse(201, json.dumps({"id": EXT}).encode())

    with pytest.raises(ProviderError):
        await provider(CountingHttp(no_url)).create(intent())


# ------------------------------------------------------------------------------------------- status


async def test_fetch_status() -> None:
    desk = FakeTabPay()
    paid = desk.add_payment(PID)
    desk.set_status(paid.id, "SUCCESS")
    expired = desk.add_payment("legacy-order", status="EXPIRED")
    sandbox = desk.add_payment(str(uuid.uuid4()), status="SUCCESS")
    sandbox.is_test = True
    missing = str(uuid.uuid4())
    plugin = provider(CountingHttp(desk))
    statuses = await plugin.fetch_status(
        [paid.id, expired.id, missing, "kit-not-a-uuid", paid.id, sandbox.id]
    )
    assert [s.external_id for s in statuses] == [paid.id, expired.id, sandbox.id]
    first, second, third = statuses
    assert first.state is PaymentState.PAID and first.amount == Decimal("179") and first.payment_id == PID
    assert first.currency == "RUB" and first.paid_at is not None and not first.is_test
    assert second.state is PaymentState.EXPIRED and second.payment_id is None
    assert third.is_test
    assert desk.status_calls == [paid.id, expired.id, missing, "kit-not-a-uuid", sandbox.id]


@pytest.mark.parametrize(("status", "retryable"), [(503, True), (429, True), (401, False)], ids=str)
async def test_fetch_status_errors(status: int, retryable: bool) -> None:
    desk = FakeTabPay()
    pay = desk.add_payment(PID)
    desk.fail_status = status
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(desk)).fetch_status([pay.id])
    assert err.value.retryable is retryable


async def test_fetch_status_path_is_quoted() -> None:
    http = CountingHttp(FakeTabPay())
    await provider(http).fetch_status(["a/../b"])
    assert http.calls[0].url.endswith("/v1/payments/a%2F..%2Fb")


# --------------------------------------------------------------------------------------- probe/config


async def test_test_credentials() -> None:
    desk = FakeTabPay()
    http = CountingHttp(desk)
    ok = await provider(http).test_credentials()
    assert ok.ok and desk.created == [] and http.calls[0].url == f"{FAKE_BASE_URL}/v1/balance"
    bad = await provider(CountingHttp(FakeTabPay(api_key="tp_other_key"))).test_credentials()
    assert not bad.ok and "API-ключ" in bad.message
    same = await provider(CountingHttp(FakeTabPay()), webhook_secret=API_KEY).test_credentials()
    assert not same.ok and "совпадает" in same.message
    down = FakeTabPay()
    down.fail_with = 503
    assert not (await provider(CountingHttp(down)).test_credentials()).ok
    wrong = await provider(CountingHttp(FakeTabPay()), base_url="https://tabpay.fake/v2").test_credentials()
    assert not wrong.ok and "Адрес API" in wrong.message


def test_config_hides_secrets_and_validates() -> None:
    plugin = provider(base_url=None, method="")
    text = repr(plugin.config)
    assert API_KEY not in text and WEBHOOK_SECRET not in text
    assert plugin.config.base_url == DEFAULT_BASE_URL and plugin.config.method is None
    assert set(plugin.config.secret_values()) == {API_KEY, WEBHOOK_SECRET}
    with pytest.raises(ConfigError) as err:
        TabPay.manifest.config.parse({**CONFIG, "api_key": "sk_live_123456"})
    assert "api_key" in err.value.errors
    with pytest.raises(ConfigError):
        TabPay.manifest.config.parse({**CONFIG, "method": "QR"})
    keys = {f.env_suffix for f in TabPay.manifest.config.fields().values()}
    assert keys == {"API_KEY", "WEBHOOK_SECRET", "METHOD", "BASE_URL"}


async def test_parse_logs_no_secrets(caplog: pytest.LogCaptureFixture) -> None:
    plugin = provider()
    with caplog.at_level(logging.DEBUG):
        for status in ("SUCCESS", "WHATEVER"):
            with contextlib.suppress(WebhookIgnored):
                await plugin.parse_webhook(
                    build({"id": EXT, "orderId": PID, "status": status, "amountKopecks": 100})
                )
        with pytest.raises(WebhookRejected):
            await plugin.parse_webhook(build({"id": EXT}, secret="wrong"))
    assert WEBHOOK_SECRET not in caplog.text and API_KEY not in caplog.text
