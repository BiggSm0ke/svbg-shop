"""Antilopay plugin: the shared TestKit plus protocol vectors from ``docs/providers/antilopay.md``."""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from svbg.payments.providers.antilopay import (
    ANTILOPAY_IPS,
    DEFAULT_BASE_URL,
    STATUS_MAP,
    Antilopay,
    RsaKeyError,
    amount_json,
    load_private_key,
    load_public_key,
    rsa_sha256_sign,
    rsa_sha256_verify,
    signed_body,
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
from tests.fakes.antilopay import (
    ANTILOPAY_IP,
    CUSTOMER_EMAIL,
    DOC_PRIVATE_KEY,
    DOC_PUBLIC_KEY,
    FAKE_BASE_URL,
    PROJECT_ID,
    SECRET_ID,
    FakeAntilopay,
    keypair,
    private_b64,
    public_b64,
    rsa_sign,
)
from tests.payments.providers.conftest import HarnessFactory, outcomes

#: As documented: the callback key «starts with MF» (512 bits) → every paid callback is re-read via the API.
WEAK = {
    "secret_id": SECRET_ID,
    "project_id": PROJECT_ID,
    "private_key": private_b64("merchant"),
    "callback_public_key": DOC_PUBLIC_KEY,
    "customer_email": CUSTOMER_EMAIL,
    "base_url": FAKE_BASE_URL,
}
STRONG = {**WEAK, "callback_public_key": public_b64("project")}
DIRECT = {**STRONG, "confirm_via_api": "false"}
PID = "0190f5d2-7b1e-7c3a-9d4e-5f6a7b8c9d0e"

#: spec §6 — vector 1 (request) and vector 2 (callback), computed on the document's key with the document's
#: example bodies (Python ``cryptography`` and OpenSSL agreed).
V1_BODY = (
    b'{"project_identificator":"PE8BED46C045139256","amount":100,"order_id":"test_payment_0001",'
    b'"currency":"rub","product_name":"Test coupon","description":"this is test payment"}'
)
V1_SIGN = "eUnKxs9BDGc90+vzxSXWZaxotyJoJqBBS8/4u3oEjwNwSOrYVm69rWPpo6T4pgcZsbiEGqk0ABxh27eqmCnrmA=="
V2_BODY = (
    b'{"payment_id":"APAY260B723F1703648835542","order_id":"test_payment_133","ctime":"2023-12-27 '
    b'10:47:15.550961","amount":77.51,"original_amount":77.51,"fee":0,"status":"PENDING","currency":"rub",'
    b'"product_name":"API Access","description":"X-Apay-Access","merchant_extra":"utm_source=telegram"}'
)
V2_SIGN = "JCjz83weF1aS63ZY4614Ekn+mv5zV0/bjwAcGkUT3Yw5DLmN8jJwVAG8CbMEiXj9mHvwbeR1f54F3C/D0DHwug=="
#: The «example signature» printed in the document — it does not verify (spec §6) and must not.
DOC_EXAMPLE_SIGN = "H2zlp7GrbMwG6i0lFWhOeFerqZ+gRlw9l2G8wrp3qzzHJXg+smNX2wglG8MR+AFdQ2ivasfq5SFtQpu+34yCoA=="

_STATUS = {
    PaymentState.CREATED: "PENDING",
    PaymentState.PROCESSING: "PENDING",
    PaymentState.PAID: "SUCCESS",
    PaymentState.EXPIRED: "EXPIRED",
    PaymentState.CANCELED: "CANCEL",
    PaymentState.FAILED: "FAIL",
    PaymentState.CHARGEBACK: "CHARGEBACK",
    PaymentState.REFUNDED: "REVERSED",
}


def payload(**kw: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "type": "payment",
        "payment_id": "APAY4AA6BB4B1701155257296",
        "order_id": PID,
        "ctime": "2026-10-02 10:47:15.550961",
        "amount": 173.63,
        "original_amount": 179,
        "fee": 5.37,
        "status": "SUCCESS",
        "currency": "RUB",
        "product_name": "Пополнение баланса",
        "description": "Пополнение баланса",
        "merchant_extra": None,
        "pay_method": "SBP",
        "pay_data": "+7 9**-***-12-34",
        "customer": {"email": CUSTOMER_EMAIL},
    }
    data.update(kw)
    return {k: v for k, v in data.items() if v is not ...}


def build(
    data: dict[str, Any] | bytes, *, key: str = "doc", remote: str | None = ANTILOPAY_IP, **headers: str
) -> WebhookRequest:
    body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode()
    hdrs = {
        "Content-Type": "application/json",
        "X-Apay-Callback": rsa_sign(body, key),
        "X-Apay-Callback-Version": "1",
    }
    hdrs.update(headers)
    return WebhookRequest(body=body, headers=hdrs, remote=remote)


class AntilopayVectors:
    """:class:`~svbg.payments.testkit.WebhookVectors` of Antilopay. ``order_id`` carries the external id (in
    production it is our payment id, which is also the external id). Antilopay has no test mode: a «test»
    event is a callback of another project, signed by another key, which cannot verify."""

    def __init__(self, key: str = "doc") -> None:
        self.key = key

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
        body = json.dumps(
            payload(
                order_id=external_id,
                status=_STATUS[state],
                currency=currency,
                amount="__AMOUNT__",
                original_amount="__AMOUNT__",
                fee=0,
                ctime=signed_at.strftime("%Y-%m-%d %H:%M:%S.%f"),
            ),
            ensure_ascii=False,
        ).replace('"__AMOUNT__"', amount)
        return build(body.encode(), key="other" if test else self.key)

    def tamper(self, req: WebhookRequest) -> WebhookRequest:
        body = req.body.replace(b'"original_amount": ', b'"original_amount": 1')
        return WebhookRequest(body=body, headers=dict(req.headers), remote=req.remote)

    def strip_auth(self, req: WebhookRequest) -> WebhookRequest:
        return WebhookRequest(body=req.body, headers={"Content-Type": "application/json"}, remote=req.remote)


def provider(http: CountingHttp | None = None, **config: Any) -> Antilopay:
    p = make_provider(Antilopay, {**WEAK, **config}, http=http)
    assert isinstance(p, Antilopay)
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
    check_static(Antilopay)
    caps = Antilopay.capabilities
    assert caps.webhook_auth is WebhookAuth.SIGNATURE and caps.replay_window_s is None
    assert caps.fetch_status and not caps.batch_status and caps.refund and not caps.recurring
    assert caps.redirect and not caps.in_chat_invoice
    assert Antilopay.manifest.currencies == ("RUB",)
    assert Antilopay.manifest.method_kinds == (MethodKind.CARD, MethodKind.SBP)
    for name, fld in Antilopay.manifest.config.fields().items():
        assert fld.title and fld.description and fld.where, name
    secrets_ = {n for n, f in Antilopay.manifest.config.fields().items() if f.is_secret}
    assert secrets_ == {"private_key"}


@pytest.mark.parametrize(
    ("config", "key"),
    [(WEAK, "doc"), (STRONG, "project"), (DIRECT, "project")],
    ids=["weak", "strong", "direct"],
)
async def test_testkit_plugin_level(config: dict[str, Any], key: str) -> None:
    await check_plugin(Antilopay, config, AntilopayVectors(key))


async def test_testkit_core_level_direct(make_harness: HarnessFactory) -> None:
    """A 2048-bit callback key and ``confirm_via_api`` off: the whole TestKit passes."""
    harness = await make_harness(Antilopay, DIRECT, http=CountingHttp(FakeAntilopay(callback_key="project")))
    await check_core(harness, AntilopayVectors("project"))
    assert len(harness.credited) == 3 and len(harness.refunded) == 1


async def test_testkit_core_level_weak_key(make_harness: HarnessFactory, db: CountingDatabase) -> None:
    """The documented 512-bit key: a callback never credits by itself — the core re-reads the status."""
    harness = await make_harness(Antilopay, WEAK, http=CountingHttp(FakeAntilopay()))
    with pytest.raises(KitFailure) as err:
        await check_core(harness, AntilopayVectors("doc"))
    assert "replayed body credited 0 times" in err.value.failures
    assert harness.credited == [] and "verify_queued" in await outcomes(db)


@pytest.mark.parametrize("domain", [True, False], ids=["domain", "no-domain"])
async def test_testkit_poll_budget(make_harness: HarnessFactory, domain: bool) -> None:
    desk = FakeAntilopay()
    harness = await make_harness(Antilopay, WEAK, http=CountingHttp(desk), has_domain=domain)
    used = await check_poll_budget(harness, domain=domain, invoices=3)
    assert used <= (3 if domain else 24) * 3
    assert all(r[1].endswith("/payment/check") for r in desk.requests)


# ---------------------------------------------------------------------------- spec §6 vectors (RSA)


def test_vector_1_request_signature() -> None:
    key = load_private_key(DOC_PRIVATE_KEY)
    assert key.public.bits == 512
    assert base64.b64encode(rsa_sha256_sign(key, V1_BODY)).decode() == V1_SIGN
    assert len(V1_BODY) == 173


def test_vector_2_callback_signature() -> None:
    pub = load_public_key(DOC_PUBLIC_KEY)
    assert len(V2_BODY) == 289
    assert rsa_sha256_verify(pub, base64.b64decode(V2_SIGN), V2_BODY)
    assert not rsa_sha256_verify(
        pub, base64.b64decode(V2_SIGN), V2_BODY.replace(b'77.51,"fee"', b'77.52,"fee"')
    )
    assert not rsa_sha256_verify(pub, base64.b64decode(DOC_EXAMPLE_SIGN), V1_BODY)


def test_public_key_is_derived_from_the_document_key() -> None:
    key = load_private_key(DOC_PRIVATE_KEY)
    pub = load_public_key(DOC_PUBLIC_KEY)
    assert (key.public.n, key.public.e) == (pub.n, pub.e)
    assert "n=" not in repr(key) and str(key.d) not in repr(key)


def test_cyrillic_letters_from_the_pdf_are_explained() -> None:
    pdf_key = DOC_PRIVATE_KEY.replace("2as57VabZaj2", "2аs57VаbZаj2")  # three Cyrillic «а» as in the PDF
    with pytest.raises(RsaKeyError, match="не-латинские"):
        load_private_key(pdf_key)


def test_stdlib_rsa_matches_cryptography_on_a_2048_bit_key() -> None:
    priv = keypair("merchant")
    ours = load_private_key(private_b64("merchant"))
    body = signed_body({"order_id": PID, "amount": 179, "product_name": "Пополнение"})
    expected = priv.sign(body, padding.PKCS1v15(), hashes.SHA256())
    assert rsa_sha256_sign(ours, body) == expected  # PKCS#1 v1.5 is deterministic; blinding cancels out
    pub = load_public_key(public_b64("merchant"))
    assert rsa_sha256_verify(pub, expected, body)
    assert not rsa_sha256_verify(pub, expected, body + b" ")
    assert not rsa_sha256_verify(pub, expected[:-1], body)
    assert not rsa_sha256_verify(pub, b"\xff" * len(expected), body)  # ≥ n
    assert not rsa_sha256_verify(pub, priv.sign(body, padding.PKCS1v15(), hashes.SHA512()), body)
    pss = priv.sign(body, padding.PSS(padding.MGF1(hashes.SHA256()), 32), hashes.SHA256())
    assert not rsa_sha256_verify(pub, pss, body)


@pytest.mark.parametrize("fmt", ["bare", "pem-pkcs8", "pem-pkcs1", "escaped"])
def test_private_key_formats(fmt: str) -> None:
    priv = keypair("merchant")
    if fmt == "bare":
        value = private_b64("merchant")
    else:
        kind = (
            serialization.PrivateFormat.TraditionalOpenSSL
            if fmt == "pem-pkcs1"
            else serialization.PrivateFormat.PKCS8
        )
        value = priv.private_bytes(serialization.Encoding.PEM, kind, serialization.NoEncryption()).decode()
        if fmt == "escaped":
            value = value.replace("\n", "\\n")
    assert load_private_key(value).public.n == priv.public_key().public_numbers().n


@pytest.mark.parametrize("fmt", ["bare", "pem", "pkcs1"])
def test_public_key_formats(fmt: str) -> None:
    pub = keypair("project").public_key()
    if fmt == "bare":
        value = public_b64("project")
    elif fmt == "pem":
        value = pub.public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode()
    else:
        value = pub.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.PKCS1).decode()
    key = load_public_key(value)
    assert key.n == pub.public_numbers().n and key.bits == 2048


def test_swapped_keys_are_recognized() -> None:
    with pytest.raises(RsaKeyError, match="вставлен публичный"):
        load_private_key(DOC_PUBLIC_KEY)
    with pytest.raises(RsaKeyError, match="вставлен секретный"):
        load_public_key(DOC_PRIVATE_KEY)


@pytest.mark.parametrize("value", ["", "not base64 !!!", base64.b64encode(b"\x30\x03\x02\x01\x05").decode()])
def test_garbage_keys(value: str) -> None:
    with pytest.raises(RsaKeyError):
        load_public_key(value)
    with pytest.raises(RsaKeyError):
        load_private_key(value)


def test_keys_shorter_than_512_bits_are_refused() -> None:
    n = load_public_key(DOC_PUBLIC_KEY).n >> 100  # a 412-bit modulus
    body = (n.bit_length() + 8) // 8
    modulus = b"\x02" + _len(body) + n.to_bytes(body, "big")
    der = b"\x30" + _len(len(modulus) + 5) + modulus + b"\x02\x03\x01\x00\x01"
    with pytest.raises(RsaKeyError, match="короче 512"):
        load_public_key(base64.b64encode(der).decode())


def _len(n: int) -> bytes:
    return bytes([n]) if n < 0x80 else bytes([0x81, n])


# ---------------------------------------------------------------------------------------- amounts


@pytest.mark.parametrize(
    ("minor", "text"),
    [(10_000, b'"amount":100,'), (10_010, b'"amount":100.1,'), (10_001, b'"amount":100.01,')],
)
async def test_create_amount_bytes_have_no_binary_noise(minor: int, text: bytes) -> None:
    desk = FakeAntilopay()
    await provider(CountingHttp(desk)).create(intent(amount_minor=minor))
    assert text in desk.raw_bodies[-1]
    assert desk.created[0]["amount"] == Decimal(minor).scaleb(-2)


def test_amount_json() -> None:
    assert amount_json(Decimal("179.00")) == 179 and isinstance(amount_json(Decimal("179.00")), int)
    assert json.dumps(amount_json(Decimal("0.07"))) == "0.07"
    assert json.dumps(amount_json(Decimal("99999.99"))) == "99999.99"
    with pytest.raises(ValueError):
        amount_json(Decimal("1.005"))


# ---------------------------------------------------------------------------------------- callbacks


async def test_paid_callback_with_the_weak_key_hides_the_currency() -> None:
    plugin = provider()
    assert plugin.confirm_via_api
    event = await plugin.parse_webhook(build(payload()))
    assert event.state is PaymentState.PAID and event.external_id == PID and event.payment_id == PID
    assert event.amount == Decimal("179")  # original_amount, not the amount after the fee
    assert event.currency is None  # → the core re-reads payment/check before crediting
    assert event.signed_at is None and not event.is_test
    assert event.summary["payment_id"] == "APAY4AA6BB4B1701155257296"
    assert event.summary["amount"] == "173.63" and event.summary["fee"] == "5.37"
    assert "pay_data" not in event.summary and "customer" not in event.summary


async def test_strong_key_still_confirms_unless_turned_off() -> None:
    assert provider(**STRONG).confirm_via_api
    event = await provider(**DIRECT).parse_webhook(build(payload(), key="project"))
    assert event.currency == "RUB" and event.amount == Decimal("179")
    # turning the check off does not help a short key
    assert provider(confirm_via_api="false").confirm_via_api


@pytest.mark.parametrize(("raw", "state"), list(STATUS_MAP.items()))
async def test_every_status(raw: str, state: PaymentState) -> None:
    event = await provider(**DIRECT).parse_webhook(build(payload(status=raw), key="project"))
    assert event.state is state and event.summary["status"] == raw


@pytest.mark.parametrize("currency", ["rub", "RUB"])
async def test_currency_case_does_not_matter(currency: str) -> None:
    event = await provider(**DIRECT).parse_webhook(build(payload(currency=currency), key="project"))
    assert event.currency == "RUB"


async def test_missing_original_amount_leaves_the_amount_to_the_api() -> None:
    event = await provider(**DIRECT).parse_webhook(build(payload(original_amount=...), key="project"))
    assert event.amount is None


@pytest.mark.parametrize("kind", ["withdraw", "topup", "popup", "refund", "something"])
async def test_other_callback_types_are_acknowledged(kind: str) -> None:
    data = payload(type=kind, refund_id="RFND1", status="COMPLETE")
    with pytest.raises(WebhookIgnored) as err:
        await provider().parse_webhook(build(data))
    assert err.value.response.status == 200


async def test_document_example_without_type_is_a_payment() -> None:
    event = await provider().parse_webhook(build(payload(type=..., status="PENDING")))
    assert event.state is PaymentState.CREATED


@pytest.mark.parametrize(
    "data", [payload(status="WEIRD"), payload(order_id=None), payload(order_id="x" * 101)]
)
async def test_unknown_or_foreign_callbacks_are_acknowledged(data: dict[str, Any]) -> None:
    with pytest.raises(WebhookIgnored):
        await provider().parse_webhook(build(data))


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Apay-Callback": ""},
        {"X-Apay-Callback": "%%%not-base64"},
        {"X-Apay-Callback": base64.b64encode(b"x").decode()},
        {"X-Apay-Callback-Version": "2"},
        {"X-Apay-Callback-Version": ""},
    ],
)
async def test_bad_signature_headers_are_401(headers: dict[str, str]) -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(build(payload(), **headers))
    assert err.value.status == 401


async def test_reserialized_json_is_rejected() -> None:
    """The signature covers the raw bytes: the same JSON with other spacing does not verify."""
    req = build(json.dumps(payload(), separators=(",", ":")).encode())
    spaced = json.dumps(json.loads(req.body), indent=1).encode()
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(
            WebhookRequest(body=spaced, headers=dict(req.headers), remote=ANTILOPAY_IP)
        )
    assert err.value.status == 401


async def test_every_single_byte_change_is_rejected() -> None:
    req = build(payload())
    plugin = provider()
    for i in range(0, len(req.body), 5):
        mutated = bytearray(req.body)
        mutated[i] ^= 0x01
        with pytest.raises(WebhookRejected):
            await plugin.parse_webhook(
                WebhookRequest(body=bytes(mutated), headers=dict(req.headers), remote=ANTILOPAY_IP)
            )


async def test_another_project_key_is_rejected() -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(build(payload(), key="other"))
    assert err.value.status == 401


async def test_malformed_authentic_bodies_are_400() -> None:
    for data in (payload(original_amount="abc"), b"not json", b"[1, 2]"):
        with pytest.raises(WebhookRejected) as err:
            await provider().parse_webhook(build(data))
        assert err.value.status == 400


@pytest.mark.parametrize("ip", ANTILOPAY_IPS)
async def test_every_published_ip_is_accepted(ip: str) -> None:
    assert (await provider().parse_webhook(build(payload(), remote=ip))).state is PaymentState.PAID


@pytest.mark.parametrize(
    ("remote", "headers", "ok"),
    [
        ("203.0.113.9", {}, False),
        (None, {}, False),
        ("127.0.0.1", {"X-Forwarded-For": f"203.0.113.9, {ANTILOPAY_IP}"}, True),
        ("127.0.0.1", {"X-Forwarded-For": f"{ANTILOPAY_IP}, 203.0.113.9"}, False),
        ("10.0.0.2", {"X-Real-IP": ANTILOPAY_IP}, True),
        (f"::ffff:{ANTILOPAY_IP}", {}, True),
    ],
)
async def test_sender_address(remote: str | None, headers: dict[str, str], ok: bool) -> None:
    req = build(payload(), remote=remote, **headers)
    if ok:
        assert (await provider().parse_webhook(req)).state is PaymentState.PAID
    else:
        with pytest.raises(WebhookRejected) as err:
            await provider().parse_webhook(req)
        assert err.value.status == 403


async def test_ip_check_can_be_turned_off_and_garbage_accepts_nobody() -> None:
    assert (await provider(allowed_ips="off").parse_webhook(build(payload(), remote="203.0.113.9"))).amount
    with pytest.raises(WebhookRejected):
        await provider(allowed_ips="not-an-ip").parse_webhook(build(payload()))


async def test_unusable_callback_key_is_503_so_antilopay_retries() -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider(callback_public_key="garbage").parse_webhook(build(payload()))
    assert err.value.status == 503


async def test_chargeback_with_the_weak_key_is_confirmed_by_the_api() -> None:
    desk = FakeAntilopay()
    plugin = provider(CountingHttp(desk))
    await plugin.create(intent())
    desk.pay(PID)
    forged = build(payload(status="CHARGEBACK"))
    with pytest.raises(WebhookIgnored):  # the API still says SUCCESS
        await plugin.parse_webhook(forged)
    desk.pay(PID, "CHARGEBACK")
    event = await plugin.parse_webhook(forged)
    assert (
        event.state is PaymentState.CHARGEBACK and event.amount == Decimal("179") and event.currency == "RUB"
    )
    desk.fail_with = 503
    with pytest.raises(WebhookRejected) as err:
        await plugin.parse_webhook(forged)
    assert err.value.status == 503


# ------------------------------------------------------------------------------------------ create


async def test_create_request_shape_and_privacy() -> None:
    desk = FakeAntilopay()
    http = CountingHttp(desk)
    checkout = await provider(http).create(
        intent(return_url="https://t.me/svbg_bot", method_hint=MethodKind.SBP)
    )
    body = desk.created[0]
    assert body["project_identificator"] == PROJECT_ID and body["order_id"] == PID
    assert body["currency"] == "RUB" and body["product_type"] == "services"
    assert body["customer"] == {"email": CUSTOMER_EMAIL}
    assert body["prefer_methods"] == ["SBP"]
    assert body["success_url"] == body["fail_url"] == "https://t.me/svbg_bot"
    assert "vat" not in body and "c0ffee" not in json.dumps(body)
    raw = desk.raw_bodies[0]
    assert b" " not in raw.replace("Пополнение баланса на 179 ₽".encode(), b"")  # compact JSON
    call = http.calls[0]
    assert call.headers["X-Apay-Secret-Id"] == SECRET_ID and call.headers["X-Apay-Sign-Version"] == "1"
    assert call.url == FAKE_BASE_URL + "payment/create" and call.data == raw
    assert checkout.kind == "url" and checkout.external_id == PID
    assert checkout.pay_url and checkout.pay_url.startswith("https://gate.antilopay.fake/payment/APAY")


async def test_create_options() -> None:
    desk = FakeAntilopay()
    await provider(CountingHttp(desk), vat="22", product_type="goods", customer_ip="198.51.100.7").create(
        intent(return_url="http://insecure.example", method_hint=MethodKind.CARD)
    )
    body = desk.created[0]
    assert body["vat"] == 22 and body["product_type"] == "goods"
    assert body["customer"] == {"email": CUSTOMER_EMAIL, "ip": "198.51.100.7"}
    assert body["prefer_methods"] == ["CARD_RU"] and "success_url" not in body


async def test_create_refuses_foreign_currency() -> None:
    http = CountingHttp(FakeAntilopay())
    with pytest.raises(ProviderError, match="только RUB"):
        await provider(http).create(intent(currency="USD"))
    assert http.requests == 0


async def test_lost_answer_is_read_back_before_a_retry() -> None:
    desk = FakeAntilopay()
    desk.lose_create_answer = True
    http = CountingHttp(desk)
    checkout = await provider(http).create(intent())
    assert checkout.pay_url == desk.payments[PID].payment_url
    assert [c.url.rsplit("/", 2)[-2:] for c in http.calls] == [["payment", "create"], ["payment", "check"]]


async def test_repeated_order_returns_the_existing_invoice() -> None:
    desk = FakeAntilopay()
    plugin = provider(CountingHttp(desk))
    first = await plugin.create(intent())
    again = await plugin.create(intent())
    assert again.pay_url == first.pay_url and len(desk.created) == 1
    desk.pay(PID)
    with pytest.raises(ProviderError, match="уже закрыт"):
        await plugin.create(intent())


async def test_transport_failure_without_an_invoice_stays_retryable() -> None:
    desk = FakeAntilopay()
    desk.fail_with = 503
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(desk)).create(intent())
    assert err.value.retryable and err.value.status == 503


@pytest.mark.parametrize(
    ("code", "retryable", "words"),
    [
        (3, False, "отклонила ключи"),
        (22, False, "IP"),
        (32, False, "IP покупателя"),
        (6, False, "не подтверждён"),
        (20, False, "меньше минимальной"),
        (31, False, "больше максимальной"),
        (23, False, "лимит"),
        (500, True, "временно недоступна"),
        (14, False, "код 14"),
    ],
)
async def test_create_business_errors(code: int, retryable: bool, words: str) -> None:
    desk = FakeAntilopay()
    desk.fail_code = code
    with pytest.raises(ProviderError, match=words) as err:
        await provider(CountingHttp(desk)).create(intent())
    assert err.value.retryable is retryable


async def test_wrong_credentials_are_reported_by_the_fake_as_codes() -> None:
    with pytest.raises(ProviderError, match="код 2"):
        await provider(CountingHttp(FakeAntilopay(secret_id="other"))).create(intent())
    with pytest.raises(ProviderError, match="код 3"):
        await provider(CountingHttp(FakeAntilopay(merchant_key="other"))).create(intent())


async def test_unusable_private_key_never_reaches_the_network() -> None:
    http = CountingHttp(FakeAntilopay())
    with pytest.raises(ProviderError, match="публичный"):
        await provider(http, private_key=DOC_PUBLIC_KEY).create(intent())
    assert http.requests == 0


async def test_bad_answers() -> None:
    def junk(_call: Any) -> HttpResponse:
        return HttpResponse(200, b"<html>maintenance</html>")

    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(junk)).fetch_status([PID])
    assert err.value.retryable

    def no_url(_call: Any) -> HttpResponse:
        return HttpResponse(200, b'{"code":0,"payment_id":"APAY1"}')

    with pytest.raises(ProviderError):
        await provider(CountingHttp(no_url)).create(intent())


# ------------------------------------------------------------------------------------------ status


async def test_fetch_status_by_order_id() -> None:
    desk = FakeAntilopay()
    plugin = provider(CountingHttp(desk))
    await plugin.create(intent())
    [st] = await plugin.fetch_status([PID, "unknown-order"])
    assert st.state is PaymentState.CREATED and st.external_id == PID and st.payment_id == PID
    desk.pay(PID)
    [st] = await plugin.fetch_status([PID])
    assert st.state is PaymentState.PAID and st.amount == Decimal("179") and st.currency == "RUB"
    assert desk.status_calls == [PID, "unknown-order", PID]


@pytest.mark.parametrize(("raw", "state"), list(STATUS_MAP.items()))
async def test_fetch_status_states(raw: str, state: PaymentState) -> None:
    desk = FakeAntilopay()
    desk.add(PID, "179", status=raw)
    [st] = await provider(CountingHttp(desk)).fetch_status([PID])
    assert st.state is state


async def test_refunds_in_payment_check() -> None:
    desk = FakeAntilopay()
    plugin = provider(CountingHttp(desk))
    desk.add(PID, "179.50", status="SUCCESS")
    assert (await plugin.refund(PID, 5_000, "RUB")).ok
    [st] = await plugin.fetch_status([PID])
    assert st.state is PaymentState.PAID  # partial refund: still paid (owner attention in the log)
    result = await plugin.refund(PID, 12_950, "RUB")
    assert result.ok and result.external_id and result.external_id.startswith("RFND")
    [st] = await plugin.fetch_status([PID])
    assert st.state is PaymentState.REFUNDED


async def test_refund_is_idempotent_per_amount() -> None:
    desk = FakeAntilopay()
    plugin = provider(CountingHttp(desk))
    desk.add(PID, "179", status="SUCCESS")
    first = await plugin.refund(PID, 17_900, "RUB")
    again = await plugin.refund(PID, 17_900, "RUB")
    assert first.ok and again.ok and first.external_id == again.external_id
    assert len(desk.payments[PID].refunds) == 1


async def test_refund_failures() -> None:
    desk = FakeAntilopay()
    plugin = provider(CountingHttp(desk))
    assert not (await plugin.refund(PID, 100, "RUB")).ok  # unknown payment
    desk.add(PID, "179")
    result = await plugin.refund(PID, 17_900, "RUB")  # not paid → code 9
    assert not result.ok and "код 9" in result.message
    assert not (await plugin.refund(PID, 17_900, "USD")).ok


async def test_test_credentials() -> None:
    desk = FakeAntilopay()
    probe = await provider(CountingHttp(desk)).test_credentials()
    assert probe.ok and "512 бит" in probe.message and probe.details["callback_key_bits"] == 512
    assert desk.created == [] and desk.requests[0][1].endswith("/signature/check")
    strong = await provider(CountingHttp(desk), **STRONG).test_credentials()
    assert strong.ok and "бит" not in strong.message and strong.details["callback_key_bits"] == 2048
    bad = await provider(CountingHttp(FakeAntilopay(merchant_key="other"))).test_credentials()
    assert not bad.ok and "код 3" in bad.message
    swapped = await provider(CountingHttp(desk), callback_public_key=DOC_PRIVATE_KEY).test_credentials()
    assert not swapped.ok and "секретный" in swapped.message

    def html(_call: Any) -> HttpResponse:
        return HttpResponse(404, b"<html>nope</html>")

    assert "не Antilopay" in (await provider(CountingHttp(html)).test_credentials()).message


def test_config_defaults_and_secrets() -> None:
    cfg = Antilopay.manifest.config.parse({k.upper(): v for k, v in WEAK.items() if k != "base_url"})
    assert cfg.base_url == DEFAULT_BASE_URL == "https://lk.antilopay.com/api/v1/"
    assert cfg.product_type == "services" and cfg.vat is None and cfg.confirm_via_api is True
    assert cfg.allowed_ips == "81.177.221.226, 87.228.9.243"
    assert WEAK["private_key"] not in repr(cfg) and cfg.secret_values() == [WEAK["private_key"]]


async def test_logs_carry_no_secrets(caplog: pytest.LogCaptureFixture) -> None:
    desk = FakeAntilopay()
    with caplog.at_level(logging.DEBUG):
        plugin = provider(CountingHttp(desk))
        await plugin.create(intent())
        desk.fail_code = 14
        with pytest.raises(ProviderError):
            await plugin.fetch_status([PID])
    assert WEAK["private_key"][:40] not in caplog.text


# --------------------------------------------------------------------------------------- core level


async def test_paid_callback_then_api_check_credits_once(
    make_harness: HarnessFactory, db: CountingDatabase
) -> None:
    desk = FakeAntilopay()
    harness = await make_harness(Antilopay, WEAK, http=CountingHttp(desk))
    result = await harness.core.create_payment(
        user_id=harness.user_id,
        instance_id=harness.instance.id,
        amount_minor=17_900,
        currency="RUB",
        description="Пополнение баланса",
    )
    assert result.checkout.external_id == result.payment_id
    payment = desk.pay(result.payment_id)
    assert await harness.send(desk.callback(payment)) == 200
    assert await harness.status(result.payment_id) == "pending"  # a 512-bit signature alone credits nothing
    assert "verify_queued" in await outcomes(db)
    await harness.core.verify(harness.instance.id, [(result.payment_id, result.payment_id)])
    await harness.core.verify(harness.instance.id, [(result.payment_id, result.payment_id)])
    assert await harness.status(result.payment_id) == "paid" and len(harness.credited) == 1


async def test_api_amount_mismatch_is_not_credited(make_harness: HarnessFactory) -> None:
    desk = FakeAntilopay()
    harness = await make_harness(Antilopay, WEAK, http=CountingHttp(desk))
    pid = await harness.pending(17_900)
    desk.add(pid, "17.90", status="SUCCESS")
    await harness.core.verify(harness.instance.id, [(pid, pid)])
    assert await harness.status(pid) == "mismatch" and harness.credited == []


async def test_end_to_end_over_real_http(make_harness: HarnessFactory) -> None:
    """Real aiohttp client against the fake server: the signed bytes are exactly the bytes on the wire."""
    async with FakeAntilopay(callback_key="project") as desk:
        http = InstanceHttp()
        try:
            harness = await make_harness(Antilopay, {**DIRECT, "base_url": desk.base_url}, http=http)
            result = await harness.core.create_payment(
                user_id=harness.user_id,
                instance_id=harness.instance.id,
                amount_minor=118_850,
                currency="RUB",
                description="Пополнение баланса «Год»",
            )
            assert desk.created[0]["amount"] == Decimal("1188.5")
            payment = desk.pay(result.payment_id)
            assert await harness.send(desk.callback(payment)) == 200
            assert await harness.status(result.payment_id) == "paid"
            assert await harness.send(desk.callback(payment)) == 200  # retry every 3 minutes: deduplicated
            await harness.core.verify(harness.instance.id, [(result.payment_id, result.payment_id)])
            assert len(harness.credited) == 1
            assert (await harness.instance.provider.test_credentials()).ok
        finally:
            await http.close()
