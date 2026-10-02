"""Shared test suite of the Cryptomus protocol family (Cryptomus, Heleket) — vectors from
``docs/providers/cryptomus.md`` and ``docs/providers/heleket.md``.

``test_cryptomus.py`` and ``test_heleket.py`` import every ``test_*`` function from here and define the
``fam`` fixture (:class:`Family`) that binds them to one provider.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

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
    PaymentIntent,
    PaymentProvider,
    PaymentState,
    ProviderError,
    WebhookIgnored,
    WebhookRejected,
    WebhookRequest,
)
from tests.dbkit import CountingDatabase
from tests.fakes.cryptomus import MERCHANT, FakeCryptomus, php_encode, signed_body
from tests.fakes.cryptomus import sign as fake_sign
from tests.payments.providers.conftest import HarnessFactory

PID = "0190f5d2-7b1e-7c3a-9d4e-5f6a7b8c9d0e"
EXT = "62f88b36-a9d5-4fa6-aa26-e040c3dbf26d"

#: OpenSSL known answers: ``printf '%s%s' "$(base64 -w0 body)" KEY | openssl md5`` (KEY = ``uK3yP4ymentKey``).
KAT_KEY = "uK3yP4ymentKey"
KAT_BODY = (
    '{"type":"payment","uuid":"62f88b36-a9d5-4fa6-aa26-e040c3dbf26d",'
    '"order_id":"0190f5d2-7b1e-7c3a-9d4e-5f6a7b8c9d0e","amount":"179.00","currency":"RUB","status":"paid",'
    '"is_final":true,"additional_data":"https:\\/\\/t.me\\/svbg_bot Привет"}'
).encode()
KAT_SIGN = "74bbcda2645b46d6143b4e4b69170911"
KAT_EMPTY_SIGN = "25a58d07041033259964afdadc295990"  # md5(base64("") + key) = md5(key)
KAT_SHORT = (b'{"uuid":"x"}', "eyJ1dWlkIjoieCJ9", "568a9bed03b5b02f53c100cb4a6aff70")

_STATUS = {
    PaymentState.CREATED: "check",
    PaymentState.PROCESSING: "process",
    PaymentState.PAID: "paid",
    PaymentState.EXPIRED: "cancel",
    PaymentState.CANCELED: "cancel",
    PaymentState.FAILED: "fail",
    PaymentState.CHARGEBACK: "refund_paid",
    PaymentState.REFUNDED: "refund_paid",
}


@dataclass(frozen=True)
class Family:
    provider: type[PaymentProvider]
    fake: type[FakeCryptomus]
    api_url: str
    webhook_ip: str
    order_id_max: int

    @property
    def key(self) -> str:
        return self.fake.default_key

    @property
    def config(self) -> dict[str, Any]:
        return {"merchant_id": MERCHANT, "api_key": self.key}


def body_for(key: str, **fields: Any) -> WebhookRequest:
    data: dict[str, Any] = {
        "type": "payment",
        "uuid": EXT,
        "order_id": PID,
        "amount": "179.00000000",
        "payment_amount": "1.93000000",
        "is_final": True,
        "status": "paid",
        "network": "tron",
        "currency": "RUB",
        "payer_currency": "USDT",
        "additional_data": None,
        "txid": "6f0d9c8374db57cac0d806251473de754f361c83a03cd805f74aa9da3193486b",
    }
    data.update(fields)
    return WebhookRequest(body=signed_body(key, data), headers={"Content-Type": "application/json"})


class Vectors:
    """:class:`~svbg.payments.testkit.WebhookVectors` of the family. A «test» event is what
    ``/v1/test-webhook/payment`` sends: signed with the live key, ``order_id`` a random 32-char string."""

    def __init__(self, key: str) -> None:
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
        return body_for(
            self.key,
            uuid=external_id,
            order_id=hashlib.md5(payment_id.encode()).hexdigest() if test else payment_id,
            amount=amount,
            currency=currency,
            status=_STATUS[state],
        )

    def tamper(self, req: WebhookRequest) -> WebhookRequest:
        return WebhookRequest(body=req.body.replace(b'"amount":"', b'"amount":"1'), headers=dict(req.headers))

    def strip_auth(self, req: WebhookRequest) -> WebhookRequest:
        data = json.loads(req.body)
        data.pop("sign")
        return WebhookRequest(body=php_encode(data).encode(), headers=dict(req.headers))


def provider(fam: Family, http: CountingHttp | None = None, *, is_test: bool = False, **config: Any) -> Any:
    return make_provider(fam.provider, {**fam.config, **config}, http=http, is_test=is_test)


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


def test_static_contract(fam: Family) -> None:
    check_static(fam.provider)
    caps = fam.provider.capabilities
    assert caps.fetch_status and not caps.batch_status and caps.replay_window_s is None
    assert caps.webhook_auth == "signature" and not caps.refund
    manifest = fam.provider.manifest
    assert manifest.method_kinds == ("crypto",) and "RUB" in manifest.currencies
    for field_def in manifest.config.fields().values():
        assert field_def.description or field_def.where


async def test_testkit_plugin_level(fam: Family) -> None:
    await check_plugin(fam.provider, fam.config, Vectors(fam.key))


async def test_testkit_core_level(make_harness: HarnessFactory, fam: Family) -> None:
    """The full core TestKit passes, chargeback included (``refund_paid`` → refunded)."""
    harness = await make_harness(fam.provider, fam.config, http=CountingHttp(fam.fake()))
    await check_core(harness, Vectors(fam.key), amount_text="179")
    assert len(harness.credited) == 3 and len(harness.refunded) == 1


@pytest.mark.parametrize("domain", [True, False], ids=["domain", "no-domain"])
async def test_testkit_poll_budget(make_harness: HarnessFactory, fam: Family, domain: bool) -> None:
    harness = await make_harness(fam.provider, fam.config, http=CountingHttp(), has_domain=domain)

    def still_checking(call: Any) -> HttpResponse:
        ref = json.loads(call.data)["uuid"]
        result = {"uuid": ref, "order_id": PID, "amount": "179.00", "currency": "RUB", "status": "check"}
        return HttpResponse(200, json.dumps({"state": 0, "result": result}).encode())

    harness.http.responder = still_checking
    used = await check_poll_budget(harness, domain=domain, invoices=2)
    assert 0 < used <= (3 if domain else 24) * 2


# ---------------------------------------------------------------------------------- signature vectors


def test_signature_known_answers(fam: Family) -> None:
    module = __import__(fam.provider.__module__, fromlist=["sign"])
    assert module.sign(KAT_KEY, KAT_BODY) == KAT_SIGN == fake_sign(KAT_KEY, KAT_BODY)
    assert module.sign(KAT_KEY, b"") == KAT_EMPTY_SIGN == hashlib.md5(KAT_KEY.encode()).hexdigest()
    body, b64, expected = KAT_SHORT
    assert base64.b64encode(body).decode() == b64 and module.sign(KAT_KEY, body) == expected


def test_php_encoding_matches_php(fam: Family) -> None:
    module = __import__(fam.provider.__module__, fromlist=["php_json"])
    data = json.loads(KAT_BODY)
    assert module.php_json(data).encode() == KAT_BODY
    assert module.php_json({"a": 'x/y\u2028\x01"\\é', "e": {}, "l": [1, None, False]}) == (
        '{"a":"x\\/y\\u2028\\u0001\\"\\\\é","e":[],"l":[1,null,false]}'
    )


async def test_known_answer_webhook_is_accepted(fam: Family) -> None:
    data = json.loads(KAT_BODY)
    data["sign"] = KAT_SIGN
    req = WebhookRequest(body=php_encode(data).encode())
    event = await provider(fam, api_key=KAT_KEY).parse_webhook(req)
    assert event.state is PaymentState.PAID and event.payment_id == PID and event.external_id == EXT
    assert event.amount == Decimal("179") and event.currency == "RUB"


async def test_slashes_and_unicode_follow_php(fam: Family) -> None:
    """Remnashop re-encoded with Python defaults (``/`` unescaped, ``\\uXXXX`` for non-ASCII) — such a
    signature is not what the provider sends, and a slash in any field broke verification."""
    req = body_for(fam.key, **{"additional_data": "https://t.me/svbg_bot — оплата", "from": "a/b"})
    assert b"https:\\/\\/t.me" in req.body and "оплата".encode() in req.body
    event = await provider(fam).parse_webhook(req)
    assert event.state is PaymentState.PAID
    data = json.loads(req.body)
    data.pop("sign")
    python_style = fake_sign(fam.key, json.dumps(data, separators=(",", ":")).encode())
    data["sign"] = python_style
    with pytest.raises(WebhookRejected) as err:
        await provider(fam).parse_webhook(WebhookRequest(body=php_encode(data).encode()))
    assert err.value.status == 401


async def test_reformatted_body_is_checked_by_its_php_encoding(fam: Family) -> None:
    """A proxy that pretty-prints or unescapes slashes does not break verification (the PHP re-encoding)."""
    req = body_for(fam.key, additional_data="a/b")
    pretty = json.dumps(json.loads(req.body), ensure_ascii=True, indent=2).encode()
    assert b"\\/" not in pretty
    event = await provider(fam).parse_webhook(WebhookRequest(body=pretty))
    assert event.state is PaymentState.PAID


async def test_number_literals_are_kept_verbatim(fam: Family) -> None:
    """Numbers are signed as PHP printed them (``-5``, ``0.07700000``), never re-rendered by Python."""
    text = (
        f'{{"type":"payment","uuid":"{EXT}","order_id":"{PID}","amount":"179.00","currency":"RUB",'
        '"status":"paid","discount_percent":-5,"convert":{"to_currency":"USDT","commission":null,'
        '"rate":0.07700000,"amount":"0.22638000"},"is_final":true}'
    )
    signature = fake_sign(fam.key, text.encode())
    body = (text[:-1] + f',"sign":"{signature}"}}').encode()
    assert (await provider(fam).parse_webhook(WebhookRequest(body=body))).state is PaymentState.PAID
    head = (f'{{"sign":"{signature}",' + text[1:]).encode()  # sign first: cut out textually
    assert (await provider(fam).parse_webhook(WebhookRequest(body=head))).state is PaymentState.PAID


async def test_every_single_byte_change_is_rejected(fam: Family) -> None:
    req = body_for(fam.key)
    plugin = provider(fam)
    for i in range(len(req.body)):
        mutated = bytearray(req.body)
        mutated[i] ^= 0x01
        with pytest.raises(WebhookRejected):
            await plugin.parse_webhook(WebhookRequest(body=bytes(mutated)))


async def test_wrong_key_is_rejected(fam: Family) -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider(fam).parse_webhook(body_for("another-merchant-key"))
    assert err.value.status == 401


@pytest.mark.parametrize(
    ("body", "status"),
    [
        (b"not json", 400),
        (b"[1, 2]", 400),
        (b'{"uuid": "x", "status": "paid"}', 401),
        (b'{"uuid": "x", "status": "paid", "sign": "zz"}', 401),
        (b'{"uuid": "x", "sign": "00000000000000000000000000000000", "sign": "1"}', 400),
        (b'{"uuid": NaN, "sign": "00000000000000000000000000000000"}', 400),
    ],
    ids=["not-json", "array", "no-sign", "sign-not-hex", "duplicate-key", "nan"],
)
async def test_malformed_or_unsigned(fam: Family, body: bytes, status: int) -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider(fam).parse_webhook(WebhookRequest(body=body))
    assert err.value.status == status


async def test_source_address_check_is_optional(fam: Family) -> None:
    desk = fam.fake()
    desk.add_invoice(EXT, PID)
    desk.pay(EXT)
    req = desk.webhook(EXT)
    assert req.remote == fam.webhook_ip
    assert (await provider(fam).parse_webhook(WebhookRequest(body=req.body, remote="10.0.0.1"))).amount
    strict = provider(fam, allowed_ips=f"{fam.webhook_ip}, 127.0.0.1")
    assert (await strict.parse_webhook(req)).state is PaymentState.PAID
    with pytest.raises(WebhookRejected) as err:
        await strict.parse_webhook(WebhookRequest(body=req.body, remote="10.0.0.1"))
    assert err.value.status == 403


# ------------------------------------------------------------------------------------------ statuses


@pytest.mark.parametrize(
    ("status", "state"),
    [
        ("check", PaymentState.CREATED),
        ("process", PaymentState.PROCESSING),
        ("confirm_check", PaymentState.PROCESSING),
        ("wrong_amount_waiting", PaymentState.PROCESSING),
        ("locked", PaymentState.PROCESSING),
        ("paid", PaymentState.PAID),
        ("paid_over", PaymentState.PAID),
        ("cancel", PaymentState.EXPIRED),
        ("fail", PaymentState.FAILED),
        ("system_fail", PaymentState.FAILED),
        ("refund_paid", PaymentState.REFUNDED),
    ],
)
async def test_status_mapping(fam: Family, status: str, state: PaymentState) -> None:
    event = await provider(fam).parse_webhook(body_for(fam.key, status=status))
    assert event.state is state and event.external_id == EXT and event.payment_id == PID


@pytest.mark.parametrize("status", ["refund_process", "refund_fail", "something_new"])
async def test_statuses_that_change_nothing_are_ignored(fam: Family, status: str) -> None:
    with pytest.raises(WebhookIgnored):
        await provider(fam).parse_webhook(body_for(fam.key, status=status))


async def test_static_wallet_deposits_are_ignored(fam: Family) -> None:
    with pytest.raises(WebhookIgnored):
        await provider(fam).parse_webhook(body_for(fam.key, type="wallet"))


async def test_paid_over_credits_the_invoice_amount(fam: Family) -> None:
    event = await provider(fam).parse_webhook(
        body_for(fam.key, status="paid_over", payment_amount="2.50000000")
    )
    assert event.state is PaymentState.PAID and event.amount == Decimal("179") and event.currency == "RUB"
    assert event.summary["payment_amount"] == "2.50000000" and event.summary["payer_currency"] == "USDT"


async def test_wrong_amount_reports_what_was_sent(fam: Family) -> None:
    event = await provider(fam).parse_webhook(
        body_for(fam.key, status="wrong_amount", payment_amount="0.90000000", payer_currency="USDT")
    )
    assert event.state is PaymentState.PAID
    assert event.amount == Decimal("0.9") and event.currency == "USDT"
    no_data = await provider(fam).parse_webhook(
        body_for(fam.key, status="wrong_amount", payment_amount=None, payer_currency=None)
    )
    assert no_data.state is PaymentState.PAID and no_data.amount is None and no_data.currency is None
    same = await provider(fam).parse_webhook(
        body_for(fam.key, status="wrong_amount", payment_amount="179", payer_currency="RUB")
    )
    assert same.amount is None  # never credit an underpayment that claims the full amount


@pytest.mark.parametrize("amount", ["179", "179.00", "179.00000000", "179.0"])
async def test_amount_forms(fam: Family, amount: str) -> None:
    event = await provider(fam).parse_webhook(body_for(fam.key, amount=amount))
    assert event.amount == Decimal("179")


@pytest.mark.parametrize(
    "fields",
    [{"uuid": None}, {"amount": "abc"}, {"currency": "R$B"}],
    ids=["no-uuid", "bad-amount", "bad-currency"],
)
async def test_unreadable_invoices_are_400(fam: Family, fields: dict[str, Any]) -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider(fam).parse_webhook(body_for(fam.key, **fields))
    assert err.value.status == 400


async def test_test_webhooks_and_foreign_orders_are_test_events(fam: Family) -> None:
    """``/v1/test-webhook/payment`` cannot carry our UUID order id (≤ 32 chars [A-Za-z0-9_])."""
    event = await provider(fam).parse_webhook(body_for(fam.key, order_id="a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"))
    assert event.is_test and event.payment_id is None and event.external_id == EXT
    live = await provider(fam).parse_webhook(body_for(fam.key))
    assert not live.is_test
    on_test_instance = await provider(fam, is_test=True).parse_webhook(body_for(fam.key))
    assert on_test_instance.is_test and on_test_instance.payment_id == PID


# ------------------------------------------------------------------------------------------- create


async def test_create_request_shape(fam: Family) -> None:
    desk = fam.fake()
    http = CountingHttp(desk)
    plugin = provider(fam, http, accepted_coins="usdt:TRON, ton", subtract=50, invoice_minutes=30)
    checkout = await plugin.create(intent(return_url="https://t.me/svbg_bot"))
    call = http.calls[0]
    assert call.method == "POST" and call.url == f"{fam.api_url}/v1/payment"
    assert call.headers["merchant"] == MERCHANT and call.headers["sign"] == fake_sign(fam.key, call.data)
    body = desk.created[0]
    assert body["amount"] == "179.00" and body["currency"] == "RUB" and body["order_id"] == PID
    assert body["lifetime"] == 1800 and body["is_payment_multiple"] is False and body["subtract"] == 50
    assert body["currencies"] == [{"currency": "USDT", "network": "tron"}, {"currency": "TON"}]
    assert body["url_callback"] == "https://shop.example/webhooks/pay/1/token"
    assert body["url_return"] == body["url_success"] == "https://t.me/svbg_bot"
    raw = call.data.decode()
    assert "c0ffee" not in raw and "Пополнение" not in raw
    inv = desk.invoices[checkout.external_id or ""]
    assert checkout.kind == "url" and checkout.pay_url == f"https://{desk.pay_host}/pay/{inv.uuid}"
    assert checkout.expires_at is not None and checkout.expires_at > datetime.now(UTC)


async def test_create_minimal(fam: Family) -> None:
    desk = fam.fake()
    await provider(fam, CountingHttp(desk)).create(intent(amount_minor=50, currency="USD"))
    body = desk.created[0]
    assert body["amount"] == "0.50" and body["currency"] == "USD" and body["lifetime"] == 3600
    assert "subtract" not in body and "currencies" not in body and "url_return" not in body


@pytest.mark.parametrize(
    ("answer", "retryable", "words"),
    [
        (HttpResponse(401, b'{"state": 1, "message": "Unauthorized"}'), False, "ключ"),
        (
            HttpResponse(422, b'{"state": 1, "message": "Minimum amount 1 USD", "errors": {}}'),
            False,
            "Minimum amount 1 USD",
        ),
        (
            HttpResponse(422, b'{"state": 1, "errors": {"currency": ["validation.required"]}}'),
            False,
            "currency: validation.required",
        ),
        (HttpResponse(502, b"<html>bad gateway</html>"), True, "недоступен"),
        (HttpResponse(429, b""), True, "недоступен"),
        (HttpResponse(200, b"not json"), False, "непонятный"),
        (HttpResponse(200, b'{"state": 0, "result": {"uuid": "x"}}'), False, "непонятный"),
        (HttpResponse(200, b'{"state": 0, "result": []}'), False, "непонятный"),
    ],
    ids=["401", "422-message", "422-errors", "502", "429", "not-json", "no-url", "not-object"],
)
async def test_create_errors(fam: Family, answer: HttpResponse, retryable: bool, words: str) -> None:
    with pytest.raises(ProviderError) as err:
        await provider(fam, CountingHttp(lambda _call: answer)).create(intent())
    assert err.value.retryable is retryable and words in err.value.human
    assert fam.key not in err.value.human


# ------------------------------------------------------------------------------------------- status


async def test_fetch_status_reads_each_invoice(fam: Family) -> None:
    desk = fam.fake()
    paid = desk.add_invoice("11111111-1111-4111-8111-111111111111", PID)
    gone = desk.add_invoice("22222222-2222-4222-8222-222222222222", "0190f5d2-7b1e-7c3a-9d4e-000000000002")
    desk.add_invoice("33333333-3333-4333-8333-333333333333", "0190f5d2-7b1e-7c3a-9d4e-000000000003")
    desk.pay(paid.uuid)
    desk.set_status(gone.uuid, "cancel")
    http = CountingHttp(desk)
    ids = [paid.uuid, gone.uuid, "33333333-3333-4333-8333-333333333333", paid.uuid, "unknown"]
    statuses = await provider(fam, http).fetch_status(ids)
    assert http.requests == 4 and [c["uuid"] for c in desk.info_calls] == [*ids[:3], "unknown"]
    by_id = {s.external_id: s for s in statuses}
    assert set(by_id) == {paid.uuid, gone.uuid, ids[2]}
    assert by_id[ids[2]].state is PaymentState.CREATED
    assert by_id[paid.uuid].state is PaymentState.PAID and by_id[paid.uuid].payment_id == PID
    assert by_id[paid.uuid].amount == Decimal("179") and by_id[paid.uuid].currency == "RUB"
    assert by_id[gone.uuid].state is PaymentState.EXPIRED


async def test_fetch_status_errors(fam: Family) -> None:
    with pytest.raises(ProviderError) as err:
        await provider(fam, CountingHttp(lambda _c: HttpResponse(503, b""))).fetch_status([EXT])
    assert err.value.retryable
    with pytest.raises(ProviderError) as err:
        await provider(fam, CountingHttp(lambda _c: HttpResponse(401, b"{}"))).fetch_status([EXT])
    assert not err.value.retryable and "ключ" in err.value.human
    bad_request = CountingHttp(lambda _c: HttpResponse(400, b'{"state": 1, "message": "bad uuid"}'))
    assert await provider(fam, bad_request).fetch_status([EXT, "x"]) == [] and bad_request.requests == 2
    assert await provider(fam, CountingHttp()).fetch_status([]) == []


async def test_test_credentials(fam: Family) -> None:
    http = CountingHttp(fam.fake())
    probe = await provider(fam, http).test_credentials()
    assert probe.ok and "2" in probe.message
    call = http.calls[0]
    assert call.url == f"{fam.api_url}/v1/payment/services" and call.data == b""
    assert call.headers["sign"] == hashlib.md5(fam.key.encode()).hexdigest()
    bad = await provider(fam, CountingHttp(fam.fake(key="other"))).test_credentials()
    assert not bad.ok and "ключ" in bad.message
    with pytest.raises(ProviderError):
        await provider(fam, CountingHttp(lambda _c: HttpResponse(502, b""))).test_credentials()


# ------------------------------------------------------------------------------ through the real core


async def test_end_to_end_over_real_http(
    make_harness: HarnessFactory, db: CountingDatabase, fam: Family
) -> None:
    async with fam.fake() as desk:
        http = InstanceHttp()
        try:
            harness = await make_harness(fam.provider, {**fam.config, "base_url": desk.base_url}, http=http)
            result = await harness.core.create_payment(
                user_id=harness.user_id,
                instance_id=harness.instance.id,
                amount_minor=49_900,
                currency="RUB",
                description="Пополнение баланса",
            )
            ext = result.checkout.external_id
            assert ext is not None
            invoice = desk.invoices[ext]
            assert invoice.order_id == result.payment_id and invoice.amount == "499.00"
            desk.pay(ext)
            assert await harness.send(desk.webhook(ext)) == 200
            assert await harness.status(result.payment_id) == "paid"
            assert await harness.send(desk.webhook(ext)) == 200  # the same body again: deduplicated
            await harness.core.verify(harness.instance.id, [(ext, result.payment_id)])
            assert len(harness.credited) == 1
            row = (await db.raw("select paid_amount_minor, paid_currency from payments"))[0]
            assert row["paid_amount_minor"] == 49_900 and row["paid_currency"] == "RUB"
        finally:
            await http.close()


async def _payment(harness: Any) -> tuple[str, str]:
    result = await harness.core.create_payment(
        user_id=harness.user_id,
        instance_id=harness.instance.id,
        amount_minor=17_900,
        currency="RUB",
        description="x",
    )
    return result.checkout.external_id, result.payment_id


async def test_core_outcomes_of_special_statuses(make_harness: HarnessFactory, fam: Family) -> None:
    desk = fam.fake()
    harness = await make_harness(fam.provider, fam.config, http=CountingHttp(desk))
    over_ext, over_pid = await _payment(harness)
    desk.pay(over_ext, status="paid_over", payment_amount="2.50000000")
    assert await harness.send(desk.webhook(over_ext)) == 200
    assert await harness.status(over_pid) == "paid"

    short_ext, short_pid = await _payment(harness)
    desk.pay(short_ext, status="wrong_amount", payment_amount="0.90000000")
    assert await harness.send(desk.webhook(short_ext)) == 200
    assert await harness.status(short_pid) == "mismatch"
    assert len(harness.credited) == 1

    desk.set_status(over_ext, "refund_paid")
    assert await harness.send(desk.webhook(over_ext)) == 200
    assert await harness.status(over_pid) == "refunded" and len(harness.refunded) == 1

    late_ext, late_pid = await _payment(harness)
    desk.set_status(late_ext, "cancel")
    await harness.send(desk.webhook(late_ext))
    assert await harness.status(late_pid) == "expired"
    desk.pay(late_ext)
    await harness.send(desk.webhook(late_ext))
    assert await harness.status(late_pid) == "paid"


async def test_test_webhook_with_a_real_uuid_cannot_credit_a_live_payment(
    make_harness: HarnessFactory, fam: Family
) -> None:
    desk = fam.fake()
    harness = await make_harness(fam.provider, fam.config, http=CountingHttp(desk))
    ext, pid = await _payment(harness)
    desk.pay(ext)
    code = await harness.send(desk.webhook(ext, order_id="TestOrder_0001"))
    assert code == 400 and await harness.status(pid) == "pending" and harness.credited == []


async def test_lost_webhook_is_found_by_the_status_check(make_harness: HarnessFactory, fam: Family) -> None:
    desk = fam.fake()
    harness = await make_harness(fam.provider, fam.config, http=CountingHttp(desk))
    refs = [await _payment(harness) for _ in range(3)]
    desk.pay(refs[0][0])
    desk.set_status(refs[1][0], "cancel")
    before = harness.http.requests
    await harness.core.verify(harness.instance.id, refs)
    assert harness.http.requests - before == 3  # one info request per invoice — no batch method exists
    assert [await harness.status(p) for _, p in refs] == ["paid", "expired", "pending"]
