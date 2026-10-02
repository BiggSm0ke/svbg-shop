"""Overpay plugin: the shared TestKit plus vectors V1–V17 from ``docs/providers/overpay.md``."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest

from svbg.payments.providers import overpay as overpay_module
from svbg.payments.providers.overpay import (
    DEFAULT_APM_URL,
    DEFAULT_CHECKOUT_URL,
    DEFAULT_GATEWAY_URL,
    TX_STATUS,
    Overpay,
    basic_auth,
    load_public_key,
    rsa_sha256_verify,
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
from tests.fakes.overpay import (
    CARD_DECLINED,
    CARD_OK,
    FAKE_APM_URL,
    FAKE_CHECKOUT_URL,
    FAKE_GATEWAY_URL,
    SECRET_KEY,
    SHOP_ID,
    FakeOverpay,
    basic,
    keypair,
    public_bare,
    public_pem,
    rsa_sign,
)
from tests.payments.providers.conftest import HarnessFactory, outcomes, payment_row

CONFIG: dict[str, Any] = {
    "shop_id": SHOP_ID,
    "secret_key": SECRET_KEY,
    "public_key": public_bare(),
    "checkout_url": FAKE_CHECKOUT_URL,
    "gateway_url": FAKE_GATEWAY_URL,
    "apm_url": FAKE_APM_URL,
}
PID = "0192f0c4-7d1e-7a00-8000-000000000001"
#: Specification §11: the exact body of the signature vector.
SPEC_BODY = (
    b'{"transaction":{"uid":"566fd40a-2379-46d6-aecd-67779afcf883","type":"payment","status":"successful",'
    b'"amount":17900,"currency":"RUB","tracking_id":"0192f0c4-7d1e-7a00-8000-000000000001","test":true}}'
)
_TX = {
    PaymentState.CREATED: "pending",
    PaymentState.PROCESSING: "pending",
    PaymentState.PAID: "successful",
    PaymentState.FAILED: "failed",
    PaymentState.EXPIRED: "expired",
    PaymentState.CANCELED: "deleted",
}


@pytest.fixture(autouse=True)
def _no_retry_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Overpay, "retry_delay_s", 0.0)


def signed(body: bytes, *, key: str = "shop", auth: str | None = None) -> WebhookRequest:
    return WebhookRequest(
        body=body,
        headers={
            "Authorization": auth if auth is not None else basic(),
            "Content-Signature": rsa_sign(body, key),
        },
    )


def tx_body(
    status: str = "successful", *, tracking: str | None = PID, amount: Any = 17_900, **extra: Any
) -> bytes:
    tx: dict[str, Any] = {
        "uid": "566fd40a-2379-46d6-aecd-67779afcf883",
        "type": "payment",
        "status": status,
        "amount": amount,
        "currency": "RUB",
        "tracking_id": tracking,
        "test": False,
        "paid_at": "2026-10-02T09:01:00.000Z" if status == "successful" else None,
    }
    tx.update(extra)
    return json.dumps({"transaction": tx}, separators=(",", ":")).encode()


class OverpayVectors:
    """:class:`~svbg.payments.testkit.WebhookVectors` of Overpay: transaction notifications linked by
    ``tracking_id`` (our payment id), amounts in kopecks, signed with the shop's RSA key plus Basic."""

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
        uid = f"tx-{state.value}-{int(signed_at.timestamp())}"
        if state in (PaymentState.CHARGEBACK, PaymentState.REFUNDED):
            tx: dict[str, Any] = {
                "uid": uid,
                "type": "chargeback",
                "status": "successful",
                "amount": minor,
                "currency": currency,
                "parent_uid": "tx-paid",
                "parent_transaction": {"uid": "tx-paid", "tracking_id": payment_id, "type": "payment"},
                "test": test,
            }
        else:
            tx = {
                "uid": uid,
                "type": "payment",
                "status": _TX[state],
                "amount": minor,
                "currency": currency,
                "tracking_id": payment_id,
                "test": test,
                "updated_at": signed_at.isoformat(),
            }
        return signed(json.dumps({"transaction": tx}).encode())

    def tamper(self, req: WebhookRequest) -> WebhookRequest:
        return WebhookRequest(body=req.body + b" ", headers=dict(req.headers))

    def strip_auth(self, req: WebhookRequest) -> WebhookRequest:
        return WebhookRequest(body=req.body, headers={"Content-Type": "application/json"})


def provider(http: CountingHttp | None = None, *, is_test: bool = False, **config: Any) -> Overpay:
    p = make_provider(Overpay, {**CONFIG, **config}, http=http, is_test=is_test)
    assert isinstance(p, Overpay)
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


def pending_responder(op: FakeOverpay) -> Any:
    """Every unknown token is an unpaid checkout on Overpay's side (abandoned checkouts)."""

    async def respond(call: Any) -> Any:
        token = call.url.rsplit("/", 1)[-1]
        if call.method == "GET" and token not in op.tokens:
            item = op.add_token(PID)
            op.tokens[token] = item
            item.token = token
        return await op(call)

    return respond


# ------------------------------------------------------------------------------------------- TestKit


def test_static_contract() -> None:
    check_static(Overpay)
    caps = Overpay.capabilities
    assert caps.webhook_auth is WebhookAuth.SIGNATURE and not caps.webhook_auth.is_weak
    assert caps.replay_window_s is None and caps.fetch_status and not caps.batch_status
    assert caps.refund and not caps.recurring and not caps.receipt_54fz and caps.redirect
    assert Overpay.manifest.method_kinds == (MethodKind.CARD, MethodKind.SBP)
    assert Overpay.manifest.currencies == ("RUB",)
    fields = Overpay.manifest.config.fields()
    assert [n for n, f in fields.items() if f.is_secret] == ["secret_key"]
    for name, fld in fields.items():
        assert fld.where, f"{name} has no «где взять»"


async def test_testkit_plugin_level() -> None:
    await check_plugin(Overpay, CONFIG, OverpayVectors())


async def test_testkit_core_level(make_harness: HarnessFactory, db: CountingDatabase) -> None:
    harness = await make_harness(Overpay, CONFIG, http=CountingHttp(FakeOverpay()))
    await check_core(harness, OverpayVectors())
    assert {"bad_signature", "applied", "mismatch", "test_rejected"} <= set(await outcomes(db))
    assert len(harness.credited) == 3 and len(harness.refunded) == 1


@pytest.mark.parametrize("domain", [True, False], ids=["domain", "no-domain"])
async def test_testkit_poll_budget(make_harness: HarnessFactory, domain: bool) -> None:
    op = FakeOverpay()
    harness = await make_harness(Overpay, CONFIG, http=CountingHttp(pending_responder(op)), has_domain=domain)
    used = await check_poll_budget(harness, domain=domain, invoices=3)
    assert used <= (3 if domain else 24) * 3
    assert all(r[0] == "GET" and r[2].startswith("/ctp/api/checkouts/") for r in op.requests)


# ------------------------------------------------------------------------------------------ RSA


def test_spec_body_control_hash() -> None:
    """The body is byte-exact with the ``printf`` line of §11. Its SHA-256 (also by ``sha256sum``) is
    ``95f0c83d…``; the control value printed in §11 (``d2a12f5d…``) does not match this body — reported to
    the specification author; the vector itself (sign → verify → one byte changed → fail) does not depend on
    it."""
    digest = hashlib.sha256(SPEC_BODY).hexdigest()
    assert digest == "95f0c83db97aa63789b98372af9d3631b8056091f8bfdc3b4ee9f2be13ce60ef"
    assert digest != "d2a12f5daedc6b510f050d33ba4e7c6da9cbc8e80c2e2ddf070a014fe72801d2"


def test_rsa_verifier_matches_cryptography() -> None:
    key = load_public_key(public_bare())
    sig = base64.b64decode(rsa_sign(SPEC_BODY))
    assert len(rsa_sign(SPEC_BODY)) == 344  # 2048-bit key, as in the specification
    assert rsa_sha256_verify(key, sig, SPEC_BODY)
    assert not rsa_sha256_verify(key, sig, SPEC_BODY + b" ")
    assert not rsa_sha256_verify(key, sig[:-1], SPEC_BODY)
    assert not rsa_sha256_verify(key, b"\x00" * len(sig), SPEC_BODY)
    assert not rsa_sha256_verify(key, b"\xff" * len(sig), SPEC_BODY)
    assert not rsa_sha256_verify(key, base64.b64decode(rsa_sign(SPEC_BODY, "other")), SPEC_BODY)


def test_rsa_rejects_other_hashes_and_paddings() -> None:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    key = load_public_key(public_bare())
    priv = keypair("shop")
    sha512 = priv.sign(SPEC_BODY, padding.PKCS1v15(), hashes.SHA512())
    sha1 = priv.sign(SPEC_BODY, padding.PKCS1v15(), hashes.SHA1())  # a negative vector
    pss = priv.sign(SPEC_BODY, padding.PSS(padding.MGF1(hashes.SHA256()), 32), hashes.SHA256())
    for sig in (sha512, sha1, pss):
        assert not rsa_sha256_verify(key, sig, SPEC_BODY)


@pytest.mark.parametrize("fmt", ["bare", "pem", "pem_escaped", "bare_wrapped", "pkcs1"])
def test_v6_public_key_formats(fmt: str) -> None:
    from cryptography.hazmat.primitives import serialization

    pub = keypair("shop").public_key()
    value = {
        "bare": public_bare(),
        "pem": public_pem(),
        "pem_escaped": public_pem().replace("\n", "\\n"),
        "bare_wrapped": "\n".join(public_bare()[i : i + 64] for i in range(0, len(public_bare()), 64)),
        "pkcs1": pub.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.PKCS1).decode(),
    }[fmt]
    key = load_public_key(value)
    numbers = pub.public_numbers()
    assert (key.n, key.e) == (numbers.n, numbers.e)
    assert rsa_sha256_verify(key, base64.b64decode(rsa_sign(SPEC_BODY)), SPEC_BODY)


async def test_v6_pem_and_bare_give_the_same_result() -> None:
    for public_key in (public_bare(), public_pem()):
        plugin = provider(public_key=public_key)
        assert (await plugin.parse_webhook(signed(SPEC_BODY))).state is PaymentState.PAID
        with pytest.raises(WebhookRejected):
            await plugin.parse_webhook(signed(SPEC_BODY, key="other"))


@pytest.mark.parametrize("value", ["", "not base64 !!!", base64.b64encode(b"\x30\x03\x02\x01\x05").decode()])
def test_bad_public_key_is_a_config_error(value: str) -> None:
    with pytest.raises(ConfigError) as err:
        provider(public_key=value or " ")
    assert "public_key" in err.value.errors


# ------------------------------------------------------------------------------- webhook: authenticity


def test_v_basic_auth_vector() -> None:
    assert basic_auth("1", "test_secret_key") == "Basic MTp0ZXN0X3NlY3JldF9rZXk="


async def test_v1_spec_body_is_accepted() -> None:
    event = await provider(is_test=True).parse_webhook(signed(SPEC_BODY))
    assert event.state is PaymentState.PAID and event.payment_id == PID and event.external_id is None
    assert event.amount == Decimal("179.00") and event.currency == "RUB" and event.is_test
    assert event.signed_at is None and event.summary["uid"] == "566fd40a-2379-46d6-aecd-67779afcf883"


async def test_v2_one_more_byte_is_401() -> None:
    req = signed(SPEC_BODY)
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(WebhookRequest(body=SPEC_BODY + b" ", headers=dict(req.headers)))
    assert err.value.status == 401


async def test_v3_reserialized_body_is_401() -> None:
    req = signed(SPEC_BODY)
    data = json.loads(SPEC_BODY)
    for other in (
        json.dumps(data).encode(),  # other whitespace
        json.dumps(
            {"transaction": dict(reversed(list(data["transaction"].items())))}, separators=(",", ":")
        ).encode(),
    ):
        with pytest.raises(WebhookRejected) as err:
            await provider().parse_webhook(WebhookRequest(body=other, headers=dict(req.headers)))
        assert err.value.status == 401


async def test_every_single_byte_change_is_rejected() -> None:
    req = signed(SPEC_BODY)
    plugin = provider(is_test=True)
    for i in range(0, len(SPEC_BODY), 7):
        body = SPEC_BODY[:i] + bytes([SPEC_BODY[i] ^ 0x01]) + SPEC_BODY[i + 1 :]
        with pytest.raises(WebhookRejected):
            await plugin.parse_webhook(WebhookRequest(body=body, headers=dict(req.headers)))


@pytest.mark.parametrize(
    "auth",
    [
        "",
        basic(SHOP_ID, "other_secret"),
        basic("2", SECRET_KEY),
        basic(SHOP_ID, SECRET_KEY + "x"),
        "Bearer " + SECRET_KEY,
        "Basic !!!",
        "Basic " + base64.b64encode(b"1test_secret_key").decode(),
    ],
)
async def test_v4_missing_or_wrong_basic_is_401(auth: str) -> None:
    body = tx_body()
    headers = {"Content-Signature": rsa_sign(body)}
    if auth:
        headers["Authorization"] = auth
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(WebhookRequest(body=body, headers=headers))
    assert err.value.status == 401


@pytest.mark.parametrize("signature", [None, "", "not base64 !!!", "AAAA", "other-key"])
async def test_v5_missing_garbage_or_foreign_signature_is_401(signature: str | None) -> None:
    body = tx_body()
    headers = {"Authorization": basic()}
    if signature == "other-key":
        headers["Content-Signature"] = rsa_sign(body, "other")
    elif signature is not None:
        headers["Content-Signature"] = signature
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(WebhookRequest(body=body, headers=headers))
    assert err.value.status == 401


async def test_signature_with_line_breaks_is_accepted() -> None:
    body = tx_body()
    sig = rsa_sign(body)
    wrapped = "\n".join(sig[i : i + 76] for i in range(0, len(sig), 76))
    req = WebhookRequest(body=body, headers={"Authorization": basic(), "Content-Signature": wrapped})
    assert (await provider().parse_webhook(req)).state is PaymentState.PAID


# ------------------------------------------------------------------------------ webhook: parsing


@pytest.mark.parametrize(("status", "state"), list(TX_STATUS.items()))
async def test_v9_status_mapping(status: str, state: PaymentState) -> None:
    assert (await provider().parse_webhook(signed(tx_body(status)))).state is state


def test_v9_status_table() -> None:
    assert {k: v.value for k, v in TX_STATUS.items()} == {
        "successful": "paid",
        "failed": "failed",
        "incomplete": "processing",
        "pending": "processing",
        "expired": "expired",
        "error": "failed",
        "deleted": "canceled",
    }


@pytest.mark.parametrize("amount", [17_900, "17900"])
async def test_amount_in_kopecks_is_exact(amount: Any) -> None:
    event = await provider().parse_webhook(signed(tx_body(amount=amount)))
    assert event.amount == Decimal("179") and event.amount == Decimal("179.00")
    event = await provider().parse_webhook(signed(tx_body(amount=17_950)))
    assert event.amount == Decimal("179.50")


@pytest.mark.parametrize("amount", [179.0, "179.00", -1, None, True])
async def test_bad_amount_is_400(amount: Any) -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(signed(tx_body(amount=amount)))
    assert err.value.status == 400


async def test_sbp_transaction_and_paid_at() -> None:
    event = await provider().parse_webhook(signed(tx_body(method_type="sbp")))
    assert event.paid_at is not None and event.summary["method"] == "sbp"


async def test_v10_expired_checkout_object() -> None:
    body = json.dumps(
        {
            "token": "a" * 64,
            "shop_id": 1,
            "transaction_type": "payment",
            "order": {
                "amount": 17_900,
                "currency": "RUB",
                "tracking_id": PID,
                "expired_at": "2026-10-02T13:00:00Z",
            },
            "finished": False,
            "expired": True,
            "status": "error",
            "message": "Token is expired.",
            "test": False,
        }
    ).encode()
    event = await provider().parse_webhook(signed(body))
    assert event.state is PaymentState.EXPIRED and event.external_id == "a" * 64 and event.payment_id == PID
    foreign = json.loads(body)
    foreign["shop_id"] = 2
    with pytest.raises(WebhookRejected):
        await provider().parse_webhook(signed(json.dumps(foreign).encode()))


async def test_v11_chargeback_points_at_the_payment() -> None:
    body = json.dumps(
        {
            "transaction": {
                "uid": "cb-1",
                "type": "chargeback",
                "status": "successful",
                "reason": "return",
                "parent_uid": "566fd40a",
                "parent_transaction": {"uid": "566fd40a", "tracking_id": PID},
                "amount": 17_900,
                "currency": "RUB",
            }
        }
    ).encode()
    event = await provider().parse_webhook(signed(body))
    assert event.state is PaymentState.CHARGEBACK and event.payment_id == PID and event.amount is None


@pytest.mark.parametrize(
    "tx",
    [
        {"type": "refund", "status": "successful", "parent_uid": "x", "amount": 100, "currency": "RUB"},
        {
            "type": "authorization",
            "status": "successful",
            "tracking_id": PID,
            "amount": 100,
            "currency": "RUB",
        },
        {"type": "chargeback", "status": "pending", "parent_transaction": {"tracking_id": PID}},
        {"type": "chargeback", "status": "successful", "parent_transaction": {"tracking_id": "foreign"}},
        {"type": "payment", "status": "weird", "tracking_id": PID, "amount": 100, "currency": "RUB"},
        {
            "type": "payment",
            "status": "successful",
            "tracking_id": "order-77",
            "amount": 100,
            "currency": "RUB",
        },
    ],
)
async def test_authentic_but_irrelevant_transactions_are_acknowledged(tx: dict[str, Any]) -> None:
    with pytest.raises(WebhookIgnored):
        await provider().parse_webhook(signed(json.dumps({"transaction": tx}).encode()))


async def test_subscription_event_is_acknowledged() -> None:
    body = json.dumps({"id": "sbs_123", "state": "active", "event": "created.subscription"}).encode()
    with pytest.raises(WebhookIgnored):
        await provider().parse_webhook(signed(body))


@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"foo":1}', b'{"transaction":"x"}'])
async def test_malformed_signed_bodies_are_400(body: bytes) -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(signed(body))
    assert err.value.status == 400


async def test_test_flag_is_reported() -> None:
    assert (await provider().parse_webhook(signed(tx_body(test=True)))).is_test
    assert not (await provider().parse_webhook(signed(tx_body()))).is_test


# ------------------------------------------------------------------------------------------ create


async def test_v13_create_request_shape_and_privacy() -> None:
    op = FakeOverpay()
    checkout = await provider(CountingHttp(op)).create(intent())
    sent = op.created[0]
    assert sent["transaction_type"] == "payment" and sent["test"] is False and sent["attempts"] == 1
    assert sent["order"]["amount"] == 17_900 and isinstance(sent["order"]["amount"], int)
    assert sent["order"]["currency"] == "RUB" and sent["order"]["tracking_id"] == PID
    assert "expired_at" not in sent["order"]
    assert sent["settings"] == {
        "language": "ru",
        "return_url": "https://t.me/svbg_bot?start=paid",
        "notification_url": "https://shop.example/webhooks/pay/1/token",
    }
    assert "payment_method" not in sent and "customer" not in sent
    assert "c0ffee" not in json.dumps(sent)
    method, host, path, headers = op.requests[0]
    assert (method, host, path) == ("POST", "checkout.overpay.fake", "/ctp/api/checkouts")
    assert headers["x-api-version"] == "2" and headers["authorization"] == "Basic MTp0ZXN0X3NlY3JldF9rZXk="
    assert checkout.kind == "url" and checkout.external_id in op.tokens
    assert checkout.pay_url == f"https://checkout.overpay.fake/v2/checkout?token={checkout.external_id}"
    assert checkout.expires_at is not None


async def test_test_mode_lifetime_and_options() -> None:
    op = FakeOverpay()
    plugin = provider(
        CountingHttp(op),
        is_test=True,
        payment_lifetime_min=60,
        language="en",
        attempts=3,
        payment_types="sbp, credit_card",
    )
    await plugin.create(intent(return_url=None))
    sent = op.created[0]
    assert sent["test"] is True and sent["attempts"] == 3
    assert sent["settings"]["language"] == "en" and "return_url" not in sent["settings"]
    assert sent["order"]["expired_at"].endswith("+00:00")
    assert sent["payment_method"] == {"types": ["sbp", "credit_card"]}


@pytest.mark.parametrize(
    ("hint", "configured", "expected"),
    [
        (MethodKind.SBP, "", ["sbp"]),
        (MethodKind.CARD, "", ["credit_card"]),
        (MethodKind.CARD, "sbp", ["sbp"]),  # not enabled for the shop: the configured list wins
        (None, "credit_card", ["credit_card"]),
        (None, "", None),
    ],
)
async def test_payment_types_follow_the_button(
    hint: MethodKind | None, configured: str, expected: list[str] | None
) -> None:
    op = FakeOverpay()
    await provider(CountingHttp(op), payment_types=configured).create(intent(method_hint=hint))
    assert op.created[0].get("payment_method") == ({"types": expected} if expected else None)


def test_payment_types_are_validated() -> None:
    with pytest.raises(ConfigError):
        provider(payment_types="credit_card, bitcoin")
    with pytest.raises(ConfigError):
        provider(min_amount="500", max_amount="100")


async def test_limits_and_currency_are_checked_before_any_request() -> None:
    http = CountingHttp(FakeOverpay())
    plugin = provider(http, min_amount="200", max_amount="1000")
    with pytest.raises(ProviderError, match="меньше минимальной"):
        await plugin.create(intent())
    with pytest.raises(ProviderError, match="больше максимальной"):
        await plugin.create(intent(amount_minor=100_001))
    with pytest.raises(ProviderError, match="только RUB"):
        await plugin.create(intent(currency="EUR"))
    assert http.requests == 0


async def test_v14_429_is_retried_with_a_growing_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    pauses: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        pauses.append(seconds)

    monkeypatch.setattr(Overpay, "retry_delay_s", 0.5)
    monkeypatch.setattr(overpay_module.asyncio, "sleep", fake_sleep)
    op = FakeOverpay()
    op.too_many = 2
    checkout = await provider(CountingHttp(op)).create(intent())
    assert checkout.external_id in op.tokens and len(op.requests) == 3 and len(op.tokens) == 1
    assert pauses == [0.5, 1.0]
    op.too_many = 3
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(op)).create(intent())
    assert err.value.retryable and err.value.status == 429


@pytest.mark.parametrize(
    ("status", "retryable", "words"),
    [
        (401, False, "ID магазина"),
        (403, False, "ID магазина"),
        (502, True, "недоступна"),
        (422, False, "fault"),
    ],
)
async def test_create_errors(status: int, retryable: bool, words: str) -> None:
    op = FakeOverpay()
    op.fail_with = status
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(op)).create(intent())
    assert err.value.retryable is retryable and words in err.value.human
    assert SECRET_KEY not in err.value.human and len(op.requests) == 1  # no blind re-create


async def test_validation_error_shows_only_the_message() -> None:
    answer = HttpResponse(
        422,
        json.dumps(
            {"message": "Validation failed", "errors": {"checkout": {"customer": ["+79991234567"]}}}
        ).encode(),
    )
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(lambda _c: answer)).create(intent())
    assert "Validation failed" in err.value.human and "7999" not in err.value.human


@pytest.mark.parametrize(
    "answer",
    [
        b"<html>",
        b'{"checkout":{"redirect_url":"https://x"}}',
        b'{"checkout":{"token":"t","redirect_url":"javascript:alert(1)"}}',
        b'{"token":"t","redirect_url":"https://x"}',
    ],
)
async def test_create_with_a_bad_answer(answer: bytes) -> None:
    with pytest.raises(ProviderError):
        await provider(CountingHttp(lambda _c: HttpResponse(200, answer))).create(intent())


# ------------------------------------------------------------------------------------ fetch_status


async def test_v15_test_cards_through_checkout() -> None:
    op = FakeOverpay()
    ok, declined, waiting = op.add_token(PID, test=True), op.add_token(PID, test=True), op.add_token(PID)
    op.pay_card(ok.token, CARD_OK)
    op.pay_card(declined.token, CARD_DECLINED)
    http = CountingHttp(op)
    result = await provider(http, is_test=True).fetch_status(
        [ok.token, declined.token, waiting.token, "missing"]
    )
    assert [(s.external_id, s.state) for s in result] == [
        (ok.token, PaymentState.PAID),
        (declined.token, PaymentState.FAILED),
        (waiting.token, PaymentState.CREATED),
    ]
    paid = result[0]
    assert paid.amount == Decimal("179.00") and paid.currency == "RUB" and paid.payment_id == PID
    assert paid.is_test and paid.paid_at is not None and not result[2].is_test
    assert all(c.method == "GET" and c.headers["X-API-Version"] == "2" for c in http.calls)


async def test_v16_bogus_apm_amounts() -> None:
    op = FakeOverpay()
    small, large = op.add_token(PID, amount=9_999, test=True), op.add_token(PID, amount=15_000, test=True)
    op.pay_bogus(small.token)
    op.pay_bogus(large.token)
    result = {
        s.external_id: s
        for s in await provider(CountingHttp(op), is_test=True).fetch_status([small.token, large.token])
    }
    assert result[small.token].state is PaymentState.PAID and result[small.token].amount == Decimal("99.99")
    assert result[large.token].state is PaymentState.FAILED


async def test_expired_and_pending_tokens() -> None:
    op = FakeOverpay()
    gone, sbp = op.add_token(PID), op.add_token(PID)
    op.expire(gone.token)
    op.pending_sbp(sbp.token)
    result = {
        s.external_id: s.state for s in await provider(CountingHttp(op)).fetch_status([gone.token, sbp.token])
    }
    assert result == {gone.token: PaymentState.EXPIRED, sbp.token: PaymentState.PROCESSING}


async def test_foreign_shop_token_is_skipped() -> None:
    op = FakeOverpay()
    item = op.add_token(PID)
    item.shop_id = 2
    assert await provider(CountingHttp(op)).fetch_status([item.token]) == []


async def test_fetch_status_path_is_quoted() -> None:
    http = CountingHttp(lambda _c: HttpResponse(404, b"{}"))
    assert await provider(http).fetch_status(["a/b?c"]) == []
    assert http.calls[0].url == f"{FAKE_CHECKOUT_URL}/ctp/api/checkouts/a%2Fb%3Fc"


@pytest.mark.parametrize(("status", "retryable"), [(401, False), (429, True), (500, True)])
async def test_fetch_status_errors(status: int, retryable: bool) -> None:
    op = FakeOverpay()
    item = op.add_token(PID)
    if status == 401:
        op.secret_key = "other"
    elif status == 429:
        op.too_many = 5
    else:
        op.fail_with = status
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(op)).fetch_status([item.token])
    assert err.value.retryable is retryable


# ------------------------------------------------------------------------------------------ refund


async def test_card_refund() -> None:
    op = FakeOverpay()
    item = op.add_token(PID)
    tx = op.pay_card(item.token)
    plugin = provider(CountingHttp(op))
    result = await plugin.refund(item.token, 5_000, "RUB")
    assert result.ok and result.external_id
    path, request = op.refunds[0]
    assert path == "/transactions/refunds"
    assert request == {
        "parent_uid": tx.uid,
        "amount": 5_000,
        "reason": "Возврат по запросу магазина",
        "tracking_id": PID,
    }
    post = next(r for r in op.requests if r[0] == "POST")
    assert post[1] == "gateway.overpay.fake" and post[3]["requestid"]
    again = await plugin.refund(item.token, 5_000, "RUB")  # same RequestID: no second refund
    assert again.ok and len(op.refunds) == 1


async def test_sbp_refund_goes_to_the_apm_api() -> None:
    op = FakeOverpay()
    item = op.add_token(PID, amount=9_999)
    op.pay_bogus(item.token)
    assert (await provider(CountingHttp(op)).refund(item.token, 9_999, "RUB")).ok
    assert op.refunds[0][0] == "/beyag/transactions/refunds"
    assert next(r for r in op.requests if r[0] == "POST")[1] == "api.overpay.fake"


async def test_v17_refund_impossible_is_explained() -> None:
    op = FakeOverpay()
    item = op.add_token(PID)
    op.pay_card(item.token)
    plugin = provider(CountingHttp(op))
    assert (await plugin.refund(item.token, 17_900, "RUB")).ok
    result = await plugin.refund(item.token, 100, "RUB")  # more than what is left
    assert not result.ok and "возврат невозможен" in result.message and "can't be refunded" in result.message


async def test_refund_of_an_unpaid_or_unknown_token() -> None:
    op = FakeOverpay()
    unpaid = op.add_token(PID)
    plugin = provider(CountingHttp(op))
    assert not (await plugin.refund(unpaid.token, 100, "RUB")).ok
    assert not (await plugin.refund("missing", 100, "RUB")).ok
    assert op.refunds == []


# ------------------------------------------------------------------------------------------- probe


async def test_test_credentials_has_no_side_effects() -> None:
    op = FakeOverpay()
    probe = await provider(CountingHttp(op)).test_credentials()
    assert probe.ok and SHOP_ID in probe.message and probe.details["key_bits"] == 2048
    assert [r[0] for r in op.requests] == ["GET"] and op.created == []
    assert "тестовый" in (await provider(CountingHttp(op), is_test=True).test_credentials()).message
    bad = await provider(CountingHttp(FakeOverpay(secret_key="x"))).test_credentials()
    assert not bad.ok and "ID магазина" in bad.message
    down = FakeOverpay()
    down.fail_with = 503
    assert not (await provider(CountingHttp(down)).test_credentials()).ok
    html = await provider(CountingHttp(lambda _c: HttpResponse(404, b"<html>"))).test_credentials()
    assert not html.ok and "не Overpay" in html.message


def test_default_urls() -> None:
    assert DEFAULT_CHECKOUT_URL == "https://checkout.overpay.io"
    assert DEFAULT_GATEWAY_URL == "https://gateway.overpay.io"
    assert DEFAULT_APM_URL == "https://api.overpay.io"
    plugin = make_provider(
        Overpay, {"shop_id": SHOP_ID, "secret_key": SECRET_KEY, "public_key": public_bare()}
    )
    assert plugin._url("checkout_url", "x") == DEFAULT_CHECKOUT_URL  # type: ignore[attr-defined]


# --------------------------------------------------------------------------------------- core level


async def test_v1_v12_signed_payment_is_credited_once(make_harness: HarnessFactory) -> None:
    harness = await make_harness(Overpay, CONFIG, http=CountingHttp(FakeOverpay()), is_test=True)
    pid = await harness.pending(external_id="tok-1")
    body = SPEC_BODY.replace(PID.encode(), pid.encode())
    for _ in range(3):
        assert await harness.send(signed(body)) == 200
    assert await harness.status(pid) == "paid" and len(harness.credited) == 1


async def test_v2_v4_v5_bad_auth_never_changes_the_payment(make_harness: HarnessFactory) -> None:
    harness = await make_harness(Overpay, CONFIG, http=CountingHttp(FakeOverpay()))
    pid = await harness.pending(external_id="tok-2")
    body = tx_body(tracking=pid)
    good = signed(body)
    assert await harness.send(WebhookRequest(body=body + b" ", headers=dict(good.headers))) == 401
    assert await harness.send(signed(body, auth=basic(SHOP_ID, "x"))) == 401
    assert await harness.send(signed(body, key="other")) == 401
    assert await harness.status(pid) == "pending"


async def test_v7_amount_mismatch(make_harness: HarnessFactory) -> None:
    harness = await make_harness(Overpay, CONFIG, http=CountingHttp(FakeOverpay()))
    pid = await harness.pending(external_id="tok-3")
    assert await harness.send(signed(tx_body(tracking=pid, amount=17_800))) == 200
    assert await harness.status(pid) == "mismatch" and harness.credited == []


async def test_v8_test_payment_on_a_live_instance_is_not_credited(
    make_harness: HarnessFactory, db: CountingDatabase
) -> None:
    harness = await make_harness(Overpay, CONFIG, http=CountingHttp(FakeOverpay()))
    pid = await harness.pending(external_id="tok-4")
    code = await harness.send(signed(tx_body(tracking=pid, test=True)))
    assert code >= 400  # the core refuses a test event on a live instance (it answers 400, not 200)
    assert await harness.status(pid) == "pending" and (await outcomes(db))[-1] == "test_rejected"


async def test_v10_v11_expiry_late_payment_and_chargeback(make_harness: HarnessFactory) -> None:
    op = FakeOverpay()
    harness = await make_harness(Overpay, CONFIG, http=CountingHttp(op))
    item = op.add_token(None)
    pid = await harness.pending(external_id=item.token)
    item.tracking_id = pid
    op.expire(item.token)
    assert await harness.send(signed(op.checkout_body(item.token))) == 200
    assert await harness.status(pid) == "expired"
    op.pay_card(item.token)
    assert await harness.send(signed(op.transaction_body(item.token))) == 200
    assert await harness.status(pid) == "paid" and len(harness.credited) == 1
    assert await harness.send(signed(op.chargeback_body(item.token))) == 200
    assert await harness.status(pid) == "refunded" and len(harness.refunded) == 1


async def test_status_poll_credits_without_a_notification(make_harness: HarnessFactory) -> None:
    op = FakeOverpay()
    harness = await make_harness(Overpay, CONFIG, http=CountingHttp(op))
    item = op.add_token(None)
    pid = await harness.pending(external_id=item.token)
    item.tracking_id = pid
    await harness.core.verify(harness.instance.id, [(item.token, pid)])
    assert await harness.status(pid) == "pending"
    op.pay_card(item.token)
    await harness.core.verify(harness.instance.id, [(item.token, pid)])
    assert await harness.status(pid) == "paid" and len(harness.credited) == 1


async def test_end_to_end_over_real_http(make_harness: HarnessFactory, db: CountingDatabase) -> None:
    """Real aiohttp client (InstanceHttp) against the fake Overpay: create, signed notification, status."""
    async with FakeOverpay() as op:
        http = InstanceHttp()
        try:
            urls = {"checkout_url": op.base_url, "gateway_url": op.base_url, "apm_url": op.base_url}
            harness = await make_harness(Overpay, {**CONFIG, **urls}, http=http)
            result = await harness.core.create_payment(
                user_id=harness.user_id,
                instance_id=harness.instance.id,
                amount_minor=17_900,
                currency="RUB",
                description="Пополнение баланса",
            )
            token = result.checkout.external_id
            assert token is not None and op.tokens[token].tracking_id == result.payment_id
            row = await payment_row(db, result.payment_id)
            assert row["external_id"] == token
            op.pay_card(token)
            assert await harness.send(op.signed(op.transaction_body(token))) == 200
            assert await harness.status(result.payment_id) == "paid" and len(harness.credited) == 1
            await harness.core.verify(harness.instance.id, [(token, result.payment_id)])
            assert len(harness.credited) == 1
            assert (await harness.instance.provider.test_credentials()).ok
        finally:
            await http.close()
