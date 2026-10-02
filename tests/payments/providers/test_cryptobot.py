"""CryptoBot plugin: the shared TestKit plus Crypto Pay vectors from ``docs/providers/cryptobot.md``."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from svbg.payments.providers.cryptobot import MAINNET_URL, TESTNET_URL, CryptoBot, sign
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
from tests.fakes.cryptobot import API_TOKEN, FAKE_BASE_URL, TESTNET_TOKEN, FakeCryptoBot, iso
from tests.fakes.cryptobot import sign as fake_sign
from tests.payments.providers.conftest import HarnessFactory

CONFIG = {"api_token": API_TOKEN}
PID = "0190f5d2-7b1e-7c3a-9d4e-5f6a7b8c9d0e"
_STATUS = {PaymentState.CREATED: "active", PaymentState.PAID: "paid", PaymentState.EXPIRED: "expired"}


def build(
    invoice: dict[str, Any],
    *,
    at: datetime | None = None,
    token: str = API_TOKEN,
    update_type: str = "invoice_paid",
) -> WebhookRequest:
    body = json.dumps(
        {
            "update_id": 1,
            "update_type": update_type,
            "request_date": iso(at or datetime.now(UTC)),
            "payload": invoice,
        }
    ).encode()
    return WebhookRequest(body=body, headers={"crypto-pay-api-signature": fake_sign(token, body)})


class CryptoVectors:
    """:class:`~svbg.payments.testkit.WebhookVectors` of Crypto Pay. A «test» event is a testnet webhook: it
    is signed with the testnet token, so a mainnet instance cannot even authenticate it."""

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
        invoice = {
            "invoice_id": external_id,
            "currency_type": "fiat",
            "fiat": currency,
            "amount": amount,
            "status": _STATUS.get(state, state.value),
            "payload": payment_id,
        }
        return build(invoice, at=signed_at, token=TESTNET_TOKEN if test else API_TOKEN)

    def tamper(self, req: WebhookRequest) -> WebhookRequest:
        return WebhookRequest(
            body=req.body.replace(b'"amount": "', b'"amount": "1'), headers=dict(req.headers)
        )

    def strip_auth(self, req: WebhookRequest) -> WebhookRequest:
        return WebhookRequest(body=req.body, headers={})


def provider(http: CountingHttp | None = None, *, is_test: bool = False, **config: Any) -> CryptoBot:
    p = make_provider(CryptoBot, {**CONFIG, "base_url": FAKE_BASE_URL, **config}, http=http, is_test=is_test)
    assert isinstance(p, CryptoBot)
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
    check_static(CryptoBot)
    caps = CryptoBot.capabilities
    assert caps.batch_status and caps.fetch_status and caps.replay_window_s is None
    assert "RUB" in CryptoBot.manifest.currencies and "XTR" not in CryptoBot.manifest.currencies


async def test_testkit_plugin_level() -> None:
    await check_plugin(CryptoBot, CONFIG, CryptoVectors())


async def test_testkit_core_level(make_harness: HarnessFactory) -> None:
    """Everything passes except the chargeback vector: Crypto Pay has no chargebacks or refunds, so the
    plugin has no status to map it to (documented in docs/providers/cryptobot.md)."""
    harness = await make_harness(CryptoBot, CONFIG, http=CountingHttp(FakeCryptoBot()))
    with pytest.raises(KitFailure) as err:
        await check_core(harness, CryptoVectors(), amount_text="179")
    assert err.value.failures == ["chargeback did not mark the payment refunded"]
    assert len(harness.credited) == 3 and harness.refunded == []


@pytest.mark.parametrize("domain", [True, False], ids=["domain", "no-domain"])
async def test_testkit_poll_budget_is_one_batch_per_tick(make_harness: HarnessFactory, domain: bool) -> None:
    desk = FakeCryptoBot()
    harness = await make_harness(CryptoBot, CONFIG, http=CountingHttp(desk), has_domain=domain)
    # check_poll_budget gives invoices non-numeric ids; answer them all as «active» from a stub
    http = harness.http

    async def active(call: Any) -> HttpResponse:
        ids = str(call.params.get("invoice_ids", "")).split(",")
        items = [{"invoice_id": i, "status": "active", "fiat": "RUB", "amount": "179"} for i in ids if i]
        return HttpResponse(200, json.dumps({"ok": True, "result": {"items": items}}).encode())

    http.responder = active
    used = await check_poll_budget(harness, domain=domain, invoices=5)
    assert used <= (3 if domain else 24)


# ---------------------------------------------------------------------------------- signature vectors


def test_signature_known_answer() -> None:
    """Computed with OpenSSL: key = ``sha256("12345:AAkat")``, ``openssl dgst -sha256 -mac HMAC``."""
    body = b'{"update_type":"invoice_paid"}'
    expected = "3fe6fa1f99eab081755f517980f8210ffb048bfe6e1cd6f2133db45acf2552d5"
    assert sign("12345:AAkat", body) == expected == fake_sign("12345:AAkat", body)


async def test_testnet_webhook_cannot_authenticate_on_mainnet() -> None:
    req = build(
        {"invoice_id": 1, "status": "paid", "fiat": "RUB", "amount": "179", "payload": PID},
        token=TESTNET_TOKEN,
    )
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(req)
    assert err.value.status == 401


async def test_testnet_instance_reports_test_events() -> None:
    plugin = provider(is_test=True, api_token=TESTNET_TOKEN)
    req = build(
        {"invoice_id": 1, "status": "paid", "fiat": "RUB", "amount": "179", "payload": PID},
        token=TESTNET_TOKEN,
    )
    event = await plugin.parse_webhook(req)
    assert event.is_test and event.state is PaymentState.PAID


async def test_every_single_byte_change_is_rejected() -> None:
    req = build({"invoice_id": 7, "status": "paid", "fiat": "RUB", "amount": "179", "payload": PID})
    plugin = provider()
    for i in range(len(req.body)):
        mutated = bytearray(req.body)
        mutated[i] ^= 0x01
        with pytest.raises(WebhookRejected):
            await plugin.parse_webhook(WebhookRequest(body=bytes(mutated), headers=dict(req.headers)))


async def test_paid_webhook_fields() -> None:
    at = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    invoice = {
        "invoice_id": 1001,
        "currency_type": "fiat",
        "fiat": "RUB",
        "amount": "179",
        "status": "paid",
        "payload": PID,
        "paid_asset": "USDT",
        "paid_amount": "1.93",
        "paid_at": "2026-10-01T11:59:00.000Z",
    }
    event = await provider().parse_webhook(build(invoice, at=at))
    assert event.external_id == "1001" and event.payment_id == PID
    assert event.amount == Decimal("179") and event.currency == "RUB"
    assert event.signed_at == at and event.paid_at is not None and not event.is_test
    assert event.summary["paid_asset"] == "USDT"


async def test_legacy_crypto_priced_invoice_reports_the_asset() -> None:
    invoice = {"invoice_id": 5, "currency_type": "crypto", "asset": "USDT", "amount": "2.5", "status": "paid"}
    event = await provider().parse_webhook(build(invoice))
    assert event.currency == "USDT" and event.amount == Decimal("2.5") and event.payment_id is None


async def test_foreign_payload_is_not_taken_for_a_payment_id() -> None:
    invoice = {
        "invoice_id": 6,
        "fiat": "RUB",
        "amount": "179",
        "status": "paid",
        "payload": "balance_5_17900",
    }
    event = await provider().parse_webhook(build(invoice))
    assert event.payment_id is None and event.external_id == "6"


async def test_other_updates_are_ignored() -> None:
    with pytest.raises(WebhookIgnored):
        await provider().parse_webhook(build({"invoice_id": 1}, update_type="invoice_created"))
    with pytest.raises(WebhookIgnored):
        await provider().parse_webhook(build({"invoice_id": 1, "status": "weird"}))


@pytest.mark.parametrize(
    "invoice",
    [
        {"status": "paid"},
        {"invoice_id": 1, "status": "paid", "amount": "x", "fiat": "RUB"},
        {"invoice_id": 1, "status": "paid", "fiat": "R$"},
    ],
    ids=["no-id", "bad-amount", "bad-currency"],
)
async def test_malformed_invoices_are_400(invoice: dict[str, Any]) -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(build(invoice))
    assert err.value.status == 400


async def test_payload_must_be_an_object() -> None:
    body = json.dumps({"update_type": "invoice_paid", "payload": "x"}).encode()
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(
            WebhookRequest(body=body, headers={"crypto-pay-api-signature": fake_sign(API_TOKEN, body)})
        )
    assert err.value.status == 400


# ------------------------------------------------------------------------------------------- create


async def test_create_request_shape() -> None:
    desk = FakeCryptoBot()
    http = CountingHttp(desk)
    plugin = provider(http, accepted_assets="usdt, ton", invoice_hours=2)
    checkout = await plugin.create(intent(return_url="https://t.me/svbg_bot"))
    assert checkout.kind == "url" and checkout.external_id == "1001"
    assert checkout.pay_url == "https://t.me/CryptoBot?start=IV00001001" and checkout.expires_at is not None
    call = http.calls[0]
    assert call.url == f"{FAKE_BASE_URL}/createInvoice" and call.headers["Crypto-Pay-API-Token"] == API_TOKEN
    body = desk.created[0]
    assert body["currency_type"] == "fiat" and body["fiat"] == "RUB" and body["amount"] == "179.00"
    assert body["payload"] == PID and body["expires_in"] == 7200 and body["accepted_assets"] == "USDT,TON"
    assert body["paid_btn_name"] == "callback" and body["paid_btn_url"] == "https://t.me/svbg_bot"
    assert "c0ffee" not in json.dumps(body)


async def test_create_without_return_url_and_assets() -> None:
    desk = FakeCryptoBot()
    await provider(CountingHttp(desk)).create(intent(description=""))
    body = desk.created[0]
    assert "paid_btn_url" not in body and "accepted_assets" not in body and body["expires_in"] == 86_400
    assert body["description"] == "Оплата"


@pytest.mark.parametrize(
    ("is_test", "expected"), [(False, MAINNET_URL), (True, TESTNET_URL)], ids=["mainnet", "testnet"]
)
async def test_network_follows_the_test_mode(is_test: bool, expected: str) -> None:
    http = CountingHttp(lambda _call: HttpResponse(200, b'{"ok": true, "result": {"name": "x"}}'))
    plugin = make_provider(CryptoBot, CONFIG, http=http, is_test=is_test)
    probe = await plugin.test_credentials()
    assert probe.ok and http.calls[0].url == f"{expected}/getMe"
    assert ("testnet" in probe.message) is is_test


@pytest.mark.parametrize(
    ("answer", "retryable", "words"),
    [
        (HttpResponse(401, b'{"ok": false, "error": {"code": 401, "name": "UNAUTHORIZED"}}'), False, "токен"),
        (
            HttpResponse(400, b'{"ok": false, "error": {"code": 400, "name": "AMOUNT_TOO_SMALL"}}'),
            False,
            "AMOUNT_TOO_SMALL",
        ),
        (HttpResponse(502, b"<html>bad gateway</html>"), True, "недоступен"),
        (HttpResponse(200, b"not json"), False, "непонятный"),
        (HttpResponse(200, b'{"ok": true, "result": {"invoice_id": 1}}'), False, "непонятный"),
    ],
    ids=["401", "400", "502", "not-json", "no-url"],
)
async def test_create_errors(answer: HttpResponse, retryable: bool, words: str) -> None:
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(lambda _call: answer)).create(intent())
    assert err.value.retryable is retryable and words in err.value.human
    assert API_TOKEN not in err.value.human


# ------------------------------------------------------------------------------------------- status


async def test_fetch_status_is_one_batch_request() -> None:
    desk = FakeCryptoBot()
    desk.add_invoice(1001, PID)
    desk.add_invoice(1002, "other")
    desk.add_invoice(1003, "x")
    desk.pay(1001)
    desk.expire(1002)
    http = CountingHttp(desk)
    statuses = await provider(http).fetch_status(["1001", "1002", "not-a-number", "1001", "9999"])
    assert http.requests == 1 and desk.get_invoices_calls == [["1001", "1002", "9999"]]
    assert http.calls[0].params["count"] == "3"
    by_id = {s.external_id: s for s in statuses}
    assert set(by_id) == {"1001", "1002"}
    assert by_id["1001"].state is PaymentState.PAID and by_id["1001"].payment_id == PID
    assert by_id["1001"].amount == Decimal("179") and by_id["1001"].currency == "RUB"
    assert by_id["1002"].state is PaymentState.EXPIRED


async def test_fetch_status_accepts_a_plain_list_and_splits_big_batches() -> None:
    seen: list[int] = []

    def answer(call: Any) -> HttpResponse:
        ids = call.params["invoice_ids"].split(",")
        seen.append(len(ids))
        items = [{"invoice_id": int(i), "status": "active", "fiat": "RUB", "amount": "1"} for i in ids]
        return HttpResponse(200, json.dumps({"ok": True, "result": items}).encode())

    statuses = await provider(CountingHttp(answer)).fetch_status([str(i) for i in range(1, 251)])
    assert seen == [100, 100, 50] and len(statuses) == 250
    assert all(s.state is PaymentState.CREATED for s in statuses)


async def test_fetch_status_errors() -> None:
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(lambda _c: HttpResponse(503, b""))).fetch_status(["1"])
    assert err.value.retryable
    with pytest.raises(ProviderError):
        await provider(CountingHttp(lambda _c: HttpResponse(200, b'{"ok": true, "result": 5}'))).fetch_status(
            ["1"]
        )
    assert await provider(CountingHttp()).fetch_status(["abc"]) == []  # nothing numeric: no request at all


async def test_test_credentials() -> None:
    assert (await provider(CountingHttp(FakeCryptoBot())).test_credentials()).ok
    bad = await provider(CountingHttp(FakeCryptoBot(token="other"))).test_credentials()
    assert not bad.ok and "@CryptoTestnetBot" in bad.message
    with pytest.raises(ProviderError):
        await provider(CountingHttp(lambda _c: HttpResponse(502, b""))).test_credentials()


# ------------------------------------------------------------------------------ through the real core


async def test_end_to_end_over_real_http(make_harness: HarnessFactory, db: CountingDatabase) -> None:
    async with FakeCryptoBot() as desk:
        http = InstanceHttp()
        try:
            harness = await make_harness(CryptoBot, {**CONFIG, "base_url": desk.base_url}, http=http)
            result = await harness.core.create_payment(
                user_id=harness.user_id,
                instance_id=harness.instance.id,
                amount_minor=49_900,
                currency="RUB",
                description="Пополнение баланса",
            )
            ext = result.checkout.external_id
            assert ext is not None
            invoice = desk.invoices[int(ext)]
            assert invoice.payload == result.payment_id and invoice.amount == "499.00"
            desk.pay(int(ext))
            assert await harness.send(desk.webhook(int(ext))) == 200
            assert await harness.status(result.payment_id) == "paid"
            assert await harness.send(desk.webhook(int(ext))) == 200  # a retry with a new update_id
            await harness.core.verify(harness.instance.id, [(ext, result.payment_id)])
            assert len(harness.credited) == 1
            row = (await db.raw("select paid_amount_minor, paid_currency from payments"))[0]
            assert row["paid_amount_minor"] == 49_900 and row["paid_currency"] == "RUB"
        finally:
            await http.close()


async def test_lost_webhook_is_found_by_the_batch_check(make_harness: HarnessFactory) -> None:
    desk = FakeCryptoBot()
    harness = await make_harness(CryptoBot, CONFIG | {"base_url": FAKE_BASE_URL}, http=CountingHttp(desk))
    ids = []
    for _ in range(3):
        result = await harness.core.create_payment(
            user_id=harness.user_id,
            instance_id=harness.instance.id,
            amount_minor=17_900,
            currency="RUB",
            description="x",
        )
        ids.append((result.checkout.external_id, result.payment_id))
    desk.pay(int(ids[0][0]))
    desk.expire(int(ids[1][0]))
    before = harness.http.requests
    await harness.core.verify(harness.instance.id, ids)
    assert harness.http.requests - before == 1
    assert [await harness.status(p) for _, p in ids] == ["paid", "expired", "pending"]
