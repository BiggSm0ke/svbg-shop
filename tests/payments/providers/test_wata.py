"""WATA plugin: the shared TestKit plus protocol vectors from ``docs/providers/wata.md``."""

from __future__ import annotations

import base64
import json
import logging
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from svbg.payments.providers.wata import (
    LIVE_URL,
    SANDBOX_URL,
    Wata,
    load_public_key,
    rsa_sha512_verify,
)
from svbg.payments.registry import InstanceHttp
from svbg.payments.testkit import (
    CountingHttp,
    KitFailure,
    MemoryKV,
    check_core,
    check_plugin,
    check_poll_budget,
    check_static,
    make_context,
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
from tests.fakes.wata import (
    API_TOKEN,
    FAKE_BASE_URL,
    SANDBOX_TOKEN,
    FakeWata,
    keypair,
    public_pem,
    rsa_sign,
)
from tests.payments.providers.conftest import HarnessFactory, outcomes, payment_row

CONFIG = {"api_key": API_TOKEN, "base_url": FAKE_BASE_URL}
PINNED = {**CONFIG, "public_key": public_pem("live")}
PID = "0190f5d2-7b1e-7c3a-9d4e-5f6a7b8c9d0e"
LINK = "3fa85f64-5717-4562-b3fc-2c963f66afa6"
#: SDK state → (kind, transactionStatus). WATA has no «expired» webhook: a declined attempt stands in for it
#: and is (correctly) not treated as the end of the invoice.
_STATUS = {
    PaymentState.CREATED: ("Payment", "Created"),
    PaymentState.PROCESSING: ("Payment", "Pending"),
    PaymentState.PAID: ("Payment", "Paid"),
    PaymentState.EXPIRED: ("Payment", "Declined"),
    PaymentState.FAILED: ("Payment", "Declined"),
    PaymentState.CHARGEBACK: ("Refund", "Paid"),
    PaymentState.REFUNDED: ("Refund", "Paid"),
}


def payload(**kw: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "transactionType": "SBP",
        "kind": "Payment",
        "id": LINK,
        "transactionId": "2a4d7c1e-0000-4000-8000-00000000aaaa",
        "transactionStatus": "Paid",
        "amount": 179.0,
        "currency": "RUB",
        "orderId": PID,
        "paymentTime": "2026-10-01T11:59:00.123456Z",
        "paymentLinkId": LINK,
        "payerData": {"payerId": "*456789"},
    }
    data.update(kw)
    return {k: v for k, v in data.items() if v is not ...}


def build(data: dict[str, Any] | bytes, *, key: str = "live") -> WebhookRequest:
    body = data if isinstance(data, bytes) else json.dumps(data).encode()
    return WebhookRequest(body=body, headers={"X-Signature": rsa_sign(body, key)})


class WataVectors:
    """:class:`~svbg.payments.testkit.WebhookVectors` of WATA. ``orderId`` carries the external id (in
    production it is our payment id, which is also the external id). A «test» event is a sandbox webhook,
    signed by the sandbox key, which a live instance cannot verify."""

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
        kind, status = _STATUS[state]
        # the amount goes out as a JSON number literal exactly as given ("179", "179.00", "179.0")
        body = json.dumps(
            payload(
                kind=kind,
                transactionStatus=status,
                orderId=external_id,
                currency=currency,
                amount="__AMOUNT__",
                transactionId=f"tx-{state.value}-{signed_at.timestamp()}",
            )
        ).replace('"__AMOUNT__"', amount)
        return build(body.encode(), key="sandbox" if test else "live")

    def tamper(self, req: WebhookRequest) -> WebhookRequest:
        return WebhookRequest(body=req.body.replace(b'"amount": ', b'"amount": 1'), headers=dict(req.headers))

    def strip_auth(self, req: WebhookRequest) -> WebhookRequest:
        return WebhookRequest(body=req.body, headers={})


def provider(
    http: CountingHttp | None = None, *, is_test: bool = False, kv: MemoryKV | None = None, **config: Any
) -> Wata:
    cfg = Wata.manifest.config.parse({**PINNED, **config})
    return Wata(cfg, make_context("wata", http=http, is_test=is_test, kv=kv))


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
    check_static(Wata)
    caps = Wata.capabilities
    assert caps.webhook_auth == "signature" and caps.replay_window_s is None
    assert caps.fetch_status and not caps.batch_status and not caps.refund
    assert Wata.manifest.currencies == ("RUB", "USD", "EUR")
    for field in Wata.manifest.config.fields().values():
        assert field.where, field.name


async def test_testkit_plugin_level() -> None:
    await check_plugin(Wata, PINNED, WataVectors())


async def test_testkit_core_level(make_harness: HarnessFactory, db: CountingDatabase) -> None:
    """Everything passes except «expired»: WATA has no expiry webhook, and a declined attempt does not end a
    one-time link (the payer may retry), so the plugin reports it as ``created``."""
    desk = FakeWata()
    harness = await make_harness(Wata, CONFIG, http=CountingHttp(desk))
    with pytest.raises(KitFailure) as err:
        await check_core(harness, WataVectors())
    assert err.value.failures == ["expired webhook did not expire the payment"]
    assert len(harness.credited) == 3 and len(harness.refunded) == 1
    # a sandbox event cannot even be verified by a live instance: it is a bad signature, not «test_rejected»
    assert {"bad_signature", "applied", "mismatch"} <= set(await outcomes(db))
    assert desk.public_key_calls <= 3  # cached; re-fetched only on a failed verification, with a cooldown


@pytest.mark.parametrize("domain", [True, False], ids=["domain", "no-domain"])
async def test_testkit_poll_budget(make_harness: HarnessFactory, domain: bool) -> None:
    desk = FakeWata()
    harness = await make_harness(Wata, PINNED, http=CountingHttp(desk), has_domain=domain)
    used = await check_poll_budget(harness, domain=domain, invoices=3)
    assert used <= (3 if domain else 24) * 3
    assert all(r[0] == "GET" and r[1].endswith("/transactions/") for r in desk.requests)


# ------------------------------------------------------------------------------------------ RSA


def test_rsa_verifier_matches_cryptography() -> None:
    key = load_public_key(public_pem("live"))
    body = b'{"orderId":"x","amount":179.00}'
    sig = base64.b64decode(rsa_sign(body))
    assert rsa_sha512_verify(key, sig, body)
    assert not rsa_sha512_verify(key, sig, body + b" ")
    assert not rsa_sha512_verify(key, sig[:-1], body)  # wrong length
    assert not rsa_sha512_verify(key, b"\x00" * len(sig), body)
    assert not rsa_sha512_verify(key, b"\xff" * len(sig), body)  # ≥ n
    other = base64.b64decode(rsa_sign(body, "attacker"))
    assert not rsa_sha512_verify(key, other, body)


def test_rsa_rejects_other_hashes_and_paddings() -> None:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    key = load_public_key(public_pem("live"))
    body = b"payload"
    priv = keypair("live")
    sha256 = priv.sign(body, padding.PKCS1v15(), hashes.SHA256())
    pss = priv.sign(body, padding.PSS(padding.MGF1(hashes.SHA512()), 32), hashes.SHA512())
    assert not rsa_sha512_verify(key, sha256, body)
    assert not rsa_sha512_verify(key, pss, body)


@pytest.mark.parametrize("fmt", ["spki", "pkcs1", "bare", "escaped"])
def test_public_key_formats(fmt: str) -> None:
    pub = keypair("live").public_key().public_numbers()
    if fmt in ("spki", "pkcs1"):
        value = public_pem("live", fmt)
    elif fmt == "bare":
        value = "".join(public_pem("live").splitlines()[1:-1])
    else:
        value = public_pem("live").replace("\n", "\\n")
    key = load_public_key(value)
    assert (key.n, key.e) == (pub.n, pub.e)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not base64 !!!",
        base64.b64encode(b"\x30\x03\x02\x01\x05").decode(),
        "-----BEGIN PUBLIC KEY-----\nAAAA\n",
    ],
)
def test_bad_public_keys(value: str) -> None:
    with pytest.raises(ValueError):
        load_public_key(value)


def _der(tag: int, content: bytes) -> bytes:
    n = len(content)
    size = (n.bit_length() + 7) // 8
    length = bytes([n]) if n < 0x80 else bytes([0x80 | size]) + n.to_bytes(size, "big")
    return bytes([tag]) + length + content


def _pkcs1(n: int, e: int) -> str:
    modulus = bytes(1) + n.to_bytes((n.bit_length() + 7) // 8, "big")
    der = _der(0x30, _der(0x02, modulus) + _der(0x02, bytes([e])))
    b64 = base64.b64encode(der).decode()
    return f"-----BEGIN RSA PUBLIC KEY-----\n{b64}\n-----END RSA PUBLIC KEY-----"


@pytest.mark.parametrize(("bits", "e"), [(512, 3), (1023, 3), (2048, 1), (2048, 4)])
def test_weak_rsa_keys_are_refused(bits: int, e: int) -> None:
    with pytest.raises(ValueError):
        load_public_key(_pkcs1((1 << (bits - 1)) | 1, e))
    assert load_public_key(_pkcs1((1 << 2047) | 1, 3)).e == 3


# ---------------------------------------------------------------------------------------- webhooks


async def test_paid_webhook_fields() -> None:
    event = await provider().parse_webhook(build(payload()))
    assert event.state is PaymentState.PAID and event.external_id == PID and event.payment_id == PID
    assert event.amount == Decimal("179") and event.currency == "RUB" and not event.is_test
    assert event.paid_at == datetime(2026, 10, 1, 11, 59, 0, 123456, tzinfo=UTC)
    assert event.signed_at is None
    assert event.summary["link_id"] == LINK and event.summary["type"] == "SBP"


@pytest.mark.parametrize("amount", [179, 179.0, "179", "179.00", 179.00])
async def test_amount_forms(amount: Any) -> None:
    event = await provider().parse_webhook(build(payload(amount=amount)))
    assert event.amount == Decimal("179")


async def test_amount_with_kopecks_is_exact() -> None:
    event = await provider().parse_webhook(build(payload(amount=1188.1)))
    assert event.amount == Decimal("1188.1")


@pytest.mark.parametrize(
    ("status", "state"),
    [
        ("Created", PaymentState.CREATED),
        ("Pending", PaymentState.PROCESSING),
        ("Declined", PaymentState.CREATED),
    ],
)
async def test_non_final_statuses(status: str, state: PaymentState) -> None:
    event = await provider().parse_webhook(
        build(payload(transactionStatus=status, errorCode="Payment:TRA_2002"))
    )
    assert event.state is state
    assert event.summary["status"] == status.lower()


async def test_refund_webhook() -> None:
    event = await provider().parse_webhook(build(payload(kind="Refund", transactionStatus="Paid")))
    assert event.state is PaymentState.REFUNDED and event.payment_id == PID
    with pytest.raises(WebhookIgnored):
        await provider().parse_webhook(build(payload(kind="Refund", transactionStatus="Pending")))


@pytest.mark.parametrize("data", [payload(orderId=...), payload(kind="Refund", orderId=None)])
async def test_authentic_webhooks_without_order_are_acknowledged(data: dict[str, Any]) -> None:
    with pytest.raises(WebhookIgnored) as err:
        await provider().parse_webhook(build(data))
    assert err.value.response.status == 200


async def test_unknown_status_is_acknowledged() -> None:
    with pytest.raises(WebhookIgnored):
        await provider().parse_webhook(build(payload(transactionStatus="Weird")))


@pytest.mark.parametrize("data", [payload(amount="abc"), payload(currency=None), payload(currency="RUBLES")])
async def test_malformed_authentic_bodies_are_400(data: dict[str, Any]) -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(build(data))
    assert err.value.status == 400


async def test_non_json_body_is_400() -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(build(b"not json"))
    assert err.value.status == 400


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-Signature": ""},
        {"X-Signature": "%%%not-base64"},
        {"X-Signature": base64.b64encode(b"x").decode()},
    ],
)
async def test_missing_or_garbage_signature_is_401(headers: dict[str, str]) -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(WebhookRequest(body=json.dumps(payload()).encode(), headers=headers))
    assert err.value.status == 401


async def test_every_single_byte_change_is_rejected() -> None:
    req = build(payload())
    plugin = provider()
    for i in range(0, len(req.body), 3):
        mutated = bytearray(req.body)
        mutated[i] ^= 0x01
        with pytest.raises(WebhookRejected):
            await plugin.parse_webhook(WebhookRequest(body=bytes(mutated), headers=dict(req.headers)))


async def test_sandbox_webhook_is_rejected_live_and_test_flagged_in_sandbox() -> None:
    req = build(payload(), key="sandbox")
    with pytest.raises(WebhookRejected):
        await provider().parse_webhook(req)
    sandbox = provider(is_test=True, api_key=SANDBOX_TOKEN, public_key=public_pem("sandbox"))
    event = await sandbox.parse_webhook(req)
    assert event.is_test and event.state is PaymentState.PAID


def test_sandbox_url_follows_test_mode() -> None:
    assert provider(is_test=True, base_url="")._base == SANDBOX_URL
    assert provider(base_url="")._base == LIVE_URL
    assert provider()._base == FAKE_BASE_URL


# ------------------------------------------------------------------------------------- public key


async def test_public_key_is_fetched_once_and_cached_in_kv() -> None:
    desk = FakeWata()
    kv = MemoryKV()
    plugin = provider(CountingHttp(desk), kv=kv, public_key="")
    for _ in range(5):
        await plugin.parse_webhook(build(payload()))
    assert desk.public_key_calls == 1
    assert "BEGIN PUBLIC KEY" in kv.data["wata_public_key"]["pem"]
    # a new provider object (restart) takes it from kv, not from WATA
    again = provider(CountingHttp(desk), kv=kv, public_key="")
    await again.parse_webhook(build(payload()))
    assert desk.public_key_calls == 1
    # an expired kv entry is re-fetched
    kv.data["wata_public_key"]["fetched_at"] -= 7 * 3600
    third = provider(CountingHttp(desk), kv=kv, public_key="")
    await third.parse_webhook(build(payload()))
    assert desk.public_key_calls == 2
    assert not any("authorization" in r[2] for r in desk.requests if r[1].endswith("/public-key"))


async def test_key_rotation_is_picked_up_and_forgeries_do_not_hammer_wata() -> None:
    desk = FakeWata()
    plugin = provider(CountingHttp(desk), public_key="")
    await plugin.parse_webhook(build(payload()))
    desk.key_name = "rotated"
    plugin._fetch_at = (plugin._fetch_at or 0) - 61  # the refresh cooldown has passed
    event = await plugin.parse_webhook(build(payload(), key="rotated"))
    assert event.state is PaymentState.PAID and desk.public_key_calls == 2
    for _ in range(20):  # forged signatures: at most one re-fetch per cooldown
        with pytest.raises(WebhookRejected):
            await plugin.parse_webhook(build(payload(), key="attacker"))
    assert desk.public_key_calls == 2


async def test_unavailable_public_key_is_503_so_wata_retries() -> None:
    desk = FakeWata()
    desk.fail_public_key = 502
    with pytest.raises(WebhookRejected) as err:
        await provider(CountingHttp(desk), public_key="").parse_webhook(build(payload()))
    assert err.value.status == 503


async def test_pinned_key_never_fetches() -> None:
    desk = FakeWata()
    plugin = provider(CountingHttp(desk))
    await plugin.parse_webhook(build(payload()))
    with pytest.raises(WebhookRejected):
        await plugin.parse_webhook(build(payload(), key="rotated"))
    assert desk.public_key_calls == 0


# ------------------------------------------------------------------------------------------ create


async def test_create_request_shape_and_privacy() -> None:
    desk = FakeWata()
    http = CountingHttp(desk)
    checkout = await provider(http).create(intent(return_url="https://t.me/svbg_bot"))
    body = desk.created[0]
    assert body["type"] == "OneTime" and body["currency"] == "RUB" and body["orderId"] == PID
    assert body["amount"] == 179.0 and isinstance(body["amount"], float)
    assert body["successRedirectUrl"] == body["failRedirectUrl"] == "https://t.me/svbg_bot"
    expires = datetime.fromisoformat(body["expirationDateTime"].replace("Z", "+00:00"))
    assert timedelta(hours=23) < expires - datetime.now(UTC) <= timedelta(hours=24)
    assert checkout.kind == "url" and checkout.external_id == PID
    assert checkout.pay_url and checkout.pay_url.startswith("https://payment.wata.fake/")
    assert checkout.expires_at is not None
    call = http.calls[0]
    assert call.headers["Authorization"] == f"Bearer {API_TOKEN}"
    assert "c0ffee" not in json.dumps(body)


async def test_create_amount_has_two_decimals() -> None:
    desk = FakeWata()
    await provider(CountingHttp(desk)).create(intent(amount_minor=118_850))
    assert desk.created[0]["amount"] == 1188.5


@pytest.mark.parametrize(
    ("minor", "currency", "words"),
    [(999, "RUB", "минимальной"), (99, "USD", "минимальной"), (100_000_000, "RUB", "максимальной")],
)
async def test_create_limits(minor: int, currency: str, words: str) -> None:
    http = CountingHttp(FakeWata())
    with pytest.raises(ProviderError, match=words):
        await provider(http).create(intent(amount_minor=minor, currency=currency))
    assert http.requests == 0


async def test_create_refuses_foreign_currency() -> None:
    with pytest.raises(ProviderError, match="RUB, USD и EUR"):
        await provider(CountingHttp(FakeWata())).create(intent(currency="KZT"))


@pytest.mark.parametrize(
    ("status", "retryable"), [(401, False), (403, False), (400, False), (429, True), (500, True), (503, True)]
)
async def test_create_errors(status: int, retryable: bool) -> None:
    desk = FakeWata()
    desk.fail_with = status
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(desk)).create(intent())
    assert err.value.retryable is retryable and err.value.status == status
    assert API_TOKEN not in err.value.human


async def test_create_with_a_bad_answer() -> None:
    async def junk(_call: Any) -> Any:
        from svbg.sdk import HttpResponse

        return HttpResponse(200, b'{"id": "x"}')

    with pytest.raises(ProviderError):
        await provider(CountingHttp(junk)).create(intent())


# ------------------------------------------------------------------------------------------ status


async def test_fetch_status_paid_declined_refunded() -> None:
    desk = FakeWata()
    plugin = provider(CountingHttp(desk))
    await plugin.create(intent())
    other = "0190f5d2-7b1e-7c3a-9d4e-000000000002"
    await plugin.create(intent(payment_id=other))
    assert await plugin.fetch_status([PID, other, "unknown"]) == []
    desk.pay(PID, status="Declined")
    [st] = await plugin.fetch_status([PID])
    assert st.state is PaymentState.CREATED
    tx = desk.pay(PID, status="Paid")
    desk.pay(other, status="Pending")
    statuses = {s.external_id: s for s in await plugin.fetch_status([PID, other])}
    assert statuses[PID].state is PaymentState.PAID and statuses[PID].amount == Decimal("179")
    assert statuses[PID].payment_id == PID and statuses[PID].paid_at is not None
    assert statuses[other].state is PaymentState.PROCESSING
    desk.refund(tx)
    [st] = await plugin.fetch_status([PID])
    assert st.state is PaymentState.REFUNDED
    assert desk.status_calls[-1] == PID


async def test_fetch_status_never_trusts_a_foreign_transaction() -> None:
    """If WATA ignored the orderId filter, transactions of other orders must not be taken for ours."""
    desk = FakeWata()
    plugin = provider(CountingHttp(desk))
    await plugin.create(intent(payment_id="0190f5d2-7b1e-7c3a-9d4e-000000000003"))
    desk.pay("0190f5d2-7b1e-7c3a-9d4e-000000000003")
    original = desk._transactions
    desk._transactions = lambda _q: original({})  # type: ignore[method-assign]
    assert await plugin.fetch_status([PID]) == []


@pytest.mark.parametrize(("status", "retryable"), [(401, False), (429, True), (502, True), (400, False)])
async def test_fetch_status_errors(status: int, retryable: bool) -> None:
    desk = FakeWata()
    desk.fail_with = status
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(desk)).fetch_status([PID])
    assert err.value.retryable is retryable


async def test_rate_limit_of_one_get_per_30_seconds_is_retryable() -> None:
    desk = FakeWata(rate_limit=True)
    plugin = provider(CountingHttp(desk))
    await plugin.create(intent())
    await plugin.fetch_status([PID])
    with pytest.raises(ProviderError) as err:
        await plugin.fetch_status([PID])
    assert err.value.retryable and err.value.status == 429


async def test_test_credentials() -> None:
    desk = FakeWata()
    probe = await provider(CountingHttp(desk), public_key="").test_credentials()
    assert probe.ok and desk.public_key_calls == 1 and desk.created == []
    bad = await provider(CountingHttp(FakeWata(token="other")), public_key="").test_credentials()
    assert not bad.ok and "токен" in bad.message
    no_key = FakeWata()
    no_key.fail_public_key = 500
    probe = await provider(CountingHttp(no_key), public_key="").test_credentials()
    assert not probe.ok and "ключ" in probe.message


def test_config_hides_secrets_and_defaults() -> None:
    cfg = Wata.manifest.config.parse({"API_KEY": API_TOKEN})
    assert API_TOKEN not in repr(cfg) and cfg.link_hours == 24 and cfg.base_url is None
    assert cfg.secret_values() == [API_TOKEN]


# --------------------------------------------------------------------------------------- core level


async def test_end_to_end_over_real_http(make_harness: HarnessFactory, db: CountingDatabase) -> None:
    """Real aiohttp client against the fake server: create, key fetch, webhook, late webhook, status."""
    async with FakeWata() as desk:
        http = InstanceHttp()
        try:
            harness = await make_harness(Wata, {**CONFIG, "base_url": desk.base_url}, http=http)
            result = await harness.core.create_payment(
                user_id=harness.user_id,
                instance_id=harness.instance.id,
                amount_minor=17_900,
                currency="RUB",
                description="Пополнение баланса",
            )
            assert result.checkout.external_id == result.payment_id
            row = await payment_row(db, result.payment_id)
            assert row["external_id"] == result.payment_id and row["poll_plan"] == "domain"
            # a pre-payment webhook must be answered 200, otherwise WATA declines the payment
            pre = desk.pay(result.payment_id, status="Created")
            assert await harness.send(desk.webhook(pre)) == 200
            assert await harness.status(result.payment_id) == "pending"
            declined = desk.pay(result.payment_id, status="Declined")
            assert await harness.send(desk.webhook(declined)) == 200
            assert await harness.status(result.payment_id) == "pending"
            paid = desk.pay(result.payment_id)
            assert await harness.send(desk.webhook(paid)) == 200
            assert await harness.status(result.payment_id) == "paid"
            assert await harness.send(desk.webhook(paid)) == 200  # retry: deduplicated
            assert len(harness.credited) == 1
            await harness.core.verify(harness.instance.id, [(result.payment_id, result.payment_id)])
            assert len(harness.credited) == 1
            assert (await harness.instance.provider.test_credentials()).ok
        finally:
            await http.close()


async def test_lost_webhook_is_recovered_by_status_check(make_harness: HarnessFactory) -> None:
    desk = FakeWata()
    harness = await make_harness(Wata, PINNED, http=CountingHttp(desk))
    result = await harness.core.create_payment(
        user_id=harness.user_id,
        instance_id=harness.instance.id,
        amount_minor=17_900,
        currency="RUB",
        description="x",
    )
    desk.pay(result.payment_id)
    await harness.core.verify(harness.instance.id, [(result.payment_id, None)])
    assert await harness.status(result.payment_id) == "paid"


async def test_test_event_on_sandbox_instance_is_accepted(make_harness: HarnessFactory) -> None:
    harness = await make_harness(
        Wata,
        {**CONFIG, "api_key": SANDBOX_TOKEN, "public_key": public_pem("sandbox")},
        http=CountingHttp(FakeWata(token=SANDBOX_TOKEN, key_name="sandbox")),
        is_test=True,
    )
    pid = await harness.pending(external_id="wata-t")
    req = WataVectors().webhook(
        PaymentState.PAID,
        payment_id=pid,
        external_id="wata-t",
        amount="179.00",
        currency="RUB",
        signed_at=datetime.now(UTC),
        test=True,
    )
    assert await harness.send(req) == 200
    assert await harness.status(pid) == "paid"


async def test_parse_is_fast_and_logs_no_secrets(caplog: pytest.LogCaptureFixture) -> None:
    plugin = provider()
    reqs = [build(payload(transactionId=f"tx-{i}")) for i in range(100)]
    started = time.perf_counter()
    with caplog.at_level(logging.DEBUG):
        for req in reqs:
            await plugin.parse_webhook(req)
    per_call_ms = (time.perf_counter() - started) * 1000 / len(reqs)
    assert per_call_ms < 10, per_call_ms  # one 2048-bit public-key operation
    assert API_TOKEN not in caplog.text
