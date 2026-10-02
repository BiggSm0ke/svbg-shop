"""SeverPay plugin: the shared TestKit plus protocol vectors from ``docs/providers/severpay.md``."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from svbg.payments.providers.severpay import (
    DEFAULT_BASE_URL,
    WEBHOOK_IPS,
    SeverPay,
    php_json,
    request_body,
    request_json,
    webhook_candidates,
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
    hmac_sha256_hex,
)
from tests.dbkit import CountingDatabase
from tests.fakes.severpay import FAKE_BASE_URL, MID, TOKEN, WEBHOOK_IP, FakeSeverPay, hmac_hex, webhook_canon
from tests.payments.providers.conftest import HarnessFactory, outcomes

CONFIG = {"mid": str(MID), "token": TOKEN, "base_url": FAKE_BASE_URL}
PID = "0190f5d2-7b1e-7c3a-9d4e-5f6a7b8c9d0e"
OTHER_TOKEN = "token-of-another-severpay-shop-000"
_STATUS = {
    PaymentState.CREATED: "new",
    PaymentState.PROCESSING: "process",
    PaymentState.PAID: "success",
    PaymentState.FAILED: "decline",
    PaymentState.EXPIRED: "fail",
    PaymentState.CANCELED: "fail",
    PaymentState.CHARGEBACK: "chargeback",  # SeverPay has no chargebacks: an unknown status
    PaymentState.REFUNDED: "refunded",
}

# spec §6 — computed with OpenSSL by the specification session (key: the sample token of the documentation)
REQ_1 = '{"mid":1,"salt":"AnyRandomString"}'
REQ_1_SIGN = "f5b95acbd0676b27e3c77f593dbdff43af7851635ac398cda3ca0e6e65e34fab"
REQ_2 = (
    '{"amount":100.5,"client_email":"123456789@telegram.org","client_id":"123456789","currency":"RUB",'
    '"lifetime":60,"mid":1,"order_id":"svbg-0001","salt":"a9f3c2d8e4b1c0d7","url_return":"https://t.me/svbg_bot"}'
)
REQ_2_SIGN = "572f5a9549f8c74f1773a5a39e83b183f6826cec5322cbfe884e9ab73f5ae771"
REQ_3_SIGN = "942242df27b5c6a33eae0ce4ffcc6ab64bd6ed80b8967eb658196e753c820cd1"
DOC_PAYIN = (
    '{"type": "payin", "data": {"id": 123456, "order_id": "ORDER-789", "amount": 100.50, "currency": "USD",\n'
    ' "status": "success"}, "salt": "a9f3c2d8e4b1", "sign": "%s"}'
)
DOC_PAYIN_CANON = (
    '{"type":"payin","data":{"id":123456,"order_id":"ORDER-789","amount":100.5,"currency":"USD",'
    '"status":"success"},"salt":"a9f3c2d8e4b1"}'
)
DOC_PAYIN_SIGN = "a2d25252b7fffe25c6ae8c9db8f6f068cee1d80755464c922d6b2ab89885251b"
DOC_TEST = '{"type": "test", "data": {"timestamp": 1735555200}, "salt": "c4f7a1d9e2b3", "sign": "%s"}'
DOC_TEST_CANON = '{"type":"test","data":{"timestamp":1735555200},"salt":"c4f7a1d9e2b3"}'
DOC_TEST_SIGN = "92a9a0a8bd110ab63428e5a24bb74e749b9cea13cb9ba4901346d700fecf1c9d"
SLASH_BODY = (
    '{"type":"payin","data":{"id":123457,"order_id":"svbg/42","amount":179,"currency":"RUB",'
    '"status":"success"},"salt":"0f1e2d3c4b5a","sign":"%s"}'
)
SLASH_CANON = (
    '{"type":"payin","data":{"id":123457,"order_id":"svbg\\/42","amount":179,"currency":"RUB",'
    '"status":"success"},"salt":"0f1e2d3c4b5a"}'
)
SLASH_SIGN = "51d86370aa401c94ef48c0631f4ea2b465b702776ade45a32e023723fa10cf6f"
DOC_STUB_SIGN = "d41d8cd98f00b204e9800998ecf8427e"


def payin(data: dict[str, Any], *, token: str = TOKEN, salt: str = "s1", remote: str | None = WEBHOOK_IP,
          amount_literal: str | None = None) -> WebhookRequest:  # fmt: skip
    """A signed ``payin`` body; ``amount_literal`` writes the amount verbatim (``179.00``)."""
    body: dict[str, Any] = {"type": "payin", "data": data, "salt": salt}
    text = json.dumps(body, separators=(",", ":"))
    if amount_literal is not None:
        text = text.replace('"amount":"@"', f'"amount":{amount_literal}')
    decoded = json.loads(text)
    sign = hmac_hex(token, webhook_canon(decoded))
    return WebhookRequest(body=(text[:-1] + f',"sign":"{sign}"}}').encode(), remote=remote)


class SeverPayVectors:
    """:class:`~svbg.payments.testkit.WebhookVectors` of SeverPay. The amount is a JSON number written
    verbatim (``179.00``); SeverPay has no test flag: a «test» webhook is one of another shop (another
    token)."""

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
        data = {"id": external_id, "order_id": payment_id, "amount": "@", "currency": currency,
                "status": _STATUS[state]}  # fmt: skip
        salt = f"{int(signed_at.timestamp() * 1000):x}"
        return payin(data, token=OTHER_TOKEN if test else TOKEN, salt=salt, amount_literal=amount)

    def tamper(self, req: WebhookRequest) -> WebhookRequest:
        return WebhookRequest(body=req.body.replace(b'"amount":', b'"amount":1'), remote=req.remote)

    def strip_auth(self, req: WebhookRequest) -> WebhookRequest:
        data = json.loads(req.body)
        data.pop("sign")
        return WebhookRequest(body=json.dumps(data).encode(), remote=req.remote)


def provider(http: CountingHttp | None = None, **config: Any) -> SeverPay:
    p = make_provider(SeverPay, {**CONFIG, **config}, http=http)
    assert isinstance(p, SeverPay)
    return p


def intent(**kw: Any) -> PaymentIntent:
    values: dict[str, Any] = {
        "payment_id": PID,
        "amount_minor": 17_900,
        "currency": "RUB",
        "description": "Пополнение баланса на 179 ₽",
        "customer_ref": "c0ffee00c0ffee00c0ffee00",
    }
    values.update(kw)
    return PaymentIntent(**values)


# ------------------------------------------------------------------------------------------- TestKit


def test_static_contract() -> None:
    check_static(SeverPay)
    caps = SeverPay.capabilities
    assert caps.webhook_auth is WebhookAuth.SIGNATURE and caps.replay_window_s is None
    assert caps.fetch_status and not caps.batch_status and not caps.refund and not caps.recurring
    assert caps.redirect and not caps.in_chat_invoice
    assert set(SeverPay.manifest.method_kinds) == {MethodKind.SBP, MethodKind.CARD}
    assert SeverPay.manifest.currencies == ("RUB",)
    fields = SeverPay.manifest.config.fields()
    assert {n for n, f in fields.items() if f.is_secret} == {"token"}
    for name, fld in fields.items():
        assert fld.title and fld.description, name
        if fld.required and fld.default is None:
            assert fld.where, f"{name}: «где взять» is missing"


async def test_testkit_plugin_level() -> None:
    await check_plugin(SeverPay, CONFIG, SeverPayVectors())


async def test_testkit_core_level(make_harness: HarnessFactory, db: CountingDatabase) -> None:
    """Signatures, the test flag and replays pass; the rest are the documented deviations: a ``success`` never
    credits from the webhook alone (it is re-read with ``/payin/get``), ``fail`` means expired *or* declined
    (→ failed), and SeverPay has no chargebacks."""
    harness = await make_harness(SeverPay, CONFIG, http=CountingHttp(FakeSeverPay()))
    with pytest.raises(KitFailure) as err:
        await check_core(harness, SeverPayVectors())
    assert err.value.failures == [
        "replayed body credited 0 times",
        "179.00 did not match an invoice of 179",
        "expired webhook did not expire the payment",
        "late payment after expiry was not credited",
        "chargeback did not mark the payment refunded",
        "a different amount was not turned into mismatch",
    ]
    assert harness.credited == []
    found = await outcomes(db)
    assert "bad_signature" in found and "verify_queued" in found


@pytest.mark.parametrize("domain", [True, False], ids=["domain", "no-domain"])
async def test_testkit_poll_budget(make_harness: HarnessFactory, domain: bool) -> None:
    async def pending(call: Any) -> HttpResponse:
        ref = json.loads(call.data)
        data = {**ref, "order_id": "x", "amount": 179, "currency": "RUB", "status": "new"}
        return HttpResponse(200, json.dumps({"status": True, "msg": "", "data": data}).encode())

    harness = await make_harness(SeverPay, CONFIG, http=CountingHttp(pending), has_domain=domain)
    used = await check_poll_budget(harness, domain=domain, invoices=3)
    assert used <= (3 if domain else 24) * 3


# -------------------------------------------------------------------------------- request signatures


def test_request_vectors_from_the_spec() -> None:
    assert request_json({"salt": "AnyRandomString", "mid": 1}) == REQ_1
    assert hmac_sha256_hex(TOKEN, REQ_1.encode()) == REQ_1_SIGN
    params = {"mid": 1, "salt": "a9f3c2d8e4b1c0d7", "order_id": "svbg-0001", "amount": 100.5,
              "currency": "RUB", "client_email": "123456789@telegram.org", "client_id": "123456789",
              "lifetime": 60, "url_return": "https://t.me/svbg_bot"}  # fmt: skip
    assert request_json(params) == REQ_2
    body, sign = request_body(TOKEN, params)
    assert sign == REQ_2_SIGN and body == (REQ_2[:-1] + f',"sign":"{REQ_2_SIGN}"}}').encode()
    escaped = REQ_2.replace("https://t.me/svbg_bot", "https:\\/\\/t.me\\/svbg_bot")
    assert hmac_sha256_hex(TOKEN, escaped.encode()) == REQ_3_SIGN != REQ_2_SIGN


def test_request_json_keeps_unicode_and_slashes() -> None:
    assert request_json({"b": "Оплата/1", "a": 179}) == '{"a":179,"b":"Оплата/1"}'


# -------------------------------------------------------------------------------- webhook signatures


def test_webhook_canonicalization_vectors() -> None:
    doc = json.loads(DOC_PAYIN % "x")
    doc.pop("sign")
    assert webhook_candidates(doc) == [DOC_PAYIN_CANON.encode()]
    assert hmac_sha256_hex(TOKEN, DOC_PAYIN_CANON.encode()) == DOC_PAYIN_SIGN
    test_doc = json.loads(DOC_TEST % "x")
    test_doc.pop("sign")
    assert php_json(test_doc) == DOC_TEST_CANON
    assert hmac_sha256_hex(TOKEN, DOC_TEST_CANON.encode()) == DOC_TEST_SIGN
    slash = json.loads(SLASH_BODY % "x")
    slash.pop("sign")
    assert webhook_candidates(slash)[0] == SLASH_CANON.encode()
    assert hmac_sha256_hex(TOKEN, SLASH_CANON.encode()) == SLASH_SIGN


def test_php_json_edge_cases() -> None:
    assert php_json({"a": "Привет/€"}) == '{"a":"\\u041f\\u0440\\u0438\\u0432\\u0435\\u0442\\/\\u20ac"}'
    assert php_json({"a": "😀"}) == '{"a":"\\ud83d\\ude00"}'
    assert php_json({"a": "Привет/€"}, escape=False) == '{"a":"Привет/€"}'
    assert (
        php_json({"a": 179.0, "b": 100.5, "c": 1e25, "d": 1e-5})
        == '{"a":179.0,"b":100.5,"c":1.0e+25,"d":1.0e-5}'
    )
    assert php_json({"data": {}}) == '{"data":[]}' and php_json({"0": "x", "1": "y"}) == '["x","y"]'
    assert (
        php_json({"t": True, "f": False, "n": None, "l": [1, "a\n"]})
        == '{"t":true,"f":false,"n":null,"l":[1,"a\\n"]}'
    )


async def test_doc_example_payin_is_accepted_with_spaces_and_newlines() -> None:
    req = WebhookRequest(body=(DOC_PAYIN % DOC_PAYIN_SIGN).encode(), remote=WEBHOOK_IP)
    event = await provider().parse_webhook(req)
    assert event.state is PaymentState.PAID and event.external_id == "123456"
    assert event.payment_id is None  # "ORDER-789" is not our opaque id
    assert event.amount == Decimal("100.50") and event.summary["currency"] == "USD"
    assert event.currency is None  # success → re-read through /payin/get before crediting


async def test_doc_example_test_webhook_is_200_without_changes() -> None:
    req = WebhookRequest(body=(DOC_TEST % DOC_TEST_SIGN).encode(), remote=WEBHOOK_IP)
    with pytest.raises(WebhookIgnored) as err:
        await provider().parse_webhook(req)
    resp = err.value.response
    assert resp.status == 200 and resp.content_type == "application/json"
    assert json.loads(resp.body) == {"status": True}


async def test_slash_vector_is_accepted() -> None:
    event = await provider().parse_webhook(WebhookRequest(body=(SLASH_BODY % SLASH_SIGN).encode()))
    assert event.amount == Decimal("179") and event.external_id == "123457"


async def test_unescaped_form_is_accepted_too() -> None:
    data = {"type": "payin", "data": {"id": 5, "order_id": PID, "amount": 179, "currency": "RUB",
                                      "status": "decline"}, "salt": "Соль/1"}  # fmt: skip
    canon = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    body = {**data, "sign": hmac_hex(TOKEN, canon)}
    event = await provider().parse_webhook(WebhookRequest(body=json.dumps(body).encode()))
    assert event.state is PaymentState.FAILED and event.currency == "RUB"


@pytest.mark.parametrize(
    "body",
    [
        DOC_PAYIN % DOC_STUB_SIGN,
        DOC_PAYIN % ("0" * 64),
        # keys reordered, signature of vector 4: the order is significant
        '{"data":{"id":123456,"order_id":"ORDER-789","amount":100.5,"currency":"USD","status":"success"},'
        f'"type":"payin","salt":"a9f3c2d8e4b1","sign":"{DOC_PAYIN_SIGN}"}}',
        (DOC_PAYIN % DOC_PAYIN_SIGN).replace("100.50", "100.51"),
        DOC_PAYIN.replace(', "sign": "%s"', ""),
        DOC_PAYIN % 123,
    ],
    ids=["doc-stub", "zeros", "reordered", "amount", "no-sign", "numeric-sign"],
)
async def test_bad_signatures_are_rejected(body: str) -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(WebhookRequest(body=body.encode(), remote=WEBHOOK_IP))
    assert err.value.status == 401


async def test_other_token_is_rejected() -> None:
    req = payin(
        {"id": 1, "order_id": PID, "amount": 179, "currency": "RUB", "status": "success"}, token=OTHER_TOKEN
    )
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(req)
    assert err.value.status == 401


@pytest.mark.parametrize(
    "body",
    [b"not json", b"[1,2]", b'{"a":1,"a":2,"sign":"x"}', b'{"amount":NaN}'],
    ids=["not-json", "list", "duplicate-key", "nan"],
)
async def test_malformed_bodies_are_400(body: bytes) -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(WebhookRequest(body=body))
    assert err.value.status == 400


@pytest.mark.parametrize(
    "data",
    [
        {"id": 1, "amount": 179, "currency": "RUB", "status": "success"},
        {"id": 1, "order_id": PID, "amount": "abc", "currency": "RUB", "status": "success"},
        {"id": 1, "order_id": PID, "amount": -1, "currency": "RUB", "status": "success"},
        {"id": 1, "order_id": PID, "amount": 179, "currency": "R$", "status": "decline"},
        "not-an-object",
    ],
    ids=["no-order-id", "bad-amount", "negative", "bad-currency", "no-data"],
)
async def test_malformed_payins_are_400(data: Any) -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(payin(data))
    assert err.value.status == 400


async def test_unknown_type_and_status_are_acknowledged() -> None:
    p = provider()
    body: dict[str, Any] = {"type": "payout", "data": {"id": 1}, "salt": "x"}
    body["sign"] = hmac_hex(TOKEN, webhook_canon(body))
    with pytest.raises(WebhookIgnored) as err:
        await p.parse_webhook(WebhookRequest(body=json.dumps(body).encode()))
    assert json.loads(err.value.response.body) == {"status": True}
    with pytest.raises(WebhookIgnored) as err:
        await p.parse_webhook(
            payin({"id": 1, "order_id": PID, "amount": 1, "currency": "RUB", "status": "hold"})
        )
    assert err.value.response.content_type == "application/json"


@pytest.mark.parametrize(
    ("status", "state"),
    [
        ("new", PaymentState.CREATED),
        ("process", PaymentState.PROCESSING),
        ("success", PaymentState.PAID),
        ("decline", PaymentState.FAILED),
        ("fail", PaymentState.FAILED),
        ("SUCCESS", PaymentState.PAID),
    ],
)
async def test_status_table(status: str, state: PaymentState) -> None:
    event = await provider().parse_webhook(
        payin({"id": 7, "order_id": PID, "amount": 179, "currency": "rub", "status": status})
    )
    assert event.state is state and event.payment_id == PID and event.external_id == "7"
    assert event.currency == (None if state is PaymentState.PAID else "RUB") and not event.is_test


@pytest.mark.parametrize("literal", ["179", "179.0", "179.00", '"179.00"'])
async def test_amount_forms(literal: str) -> None:
    data = {"id": 7, "order_id": PID, "amount": "@", "currency": "RUB", "status": "fail"}
    event = await provider().parse_webhook(payin(data, amount_literal=literal))
    assert event.amount == Decimal("179")


async def test_float_noise_never_reaches_the_amount() -> None:
    event = await provider().parse_webhook(
        payin(
            {"id": 7, "order_id": PID, "amount": "@", "currency": "RUB", "status": "fail"},
            amount_literal="0.1",
        )
    )
    assert str(event.amount) == "0.1"


async def test_ack_is_json_status_true() -> None:
    resp = provider().ack(None)
    assert resp.status == 200 and resp.content_type == "application/json"
    assert json.loads(resp.body) == {"status": True}


async def test_source_address_check() -> None:
    data = {"id": 7, "order_id": PID, "amount": 179, "currency": "RUB", "status": "fail"}
    loose = provider()
    assert (await loose.parse_webhook(payin(data, remote="203.0.113.9"))).state is PaymentState.FAILED
    strict = provider(check_ip="true")
    with pytest.raises(WebhookRejected) as err:
        await strict.parse_webhook(payin(data, remote="203.0.113.9"))
    assert err.value.status == 403
    with pytest.raises(WebhookRejected):
        await strict.parse_webhook(payin(data, remote=None))
    for ip in (*WEBHOOK_IPS, "::ffff:45.76.81.14", "2001:19F0:6C01:878:5400:5FF:FE38:50D1"):
        assert (await strict.parse_webhook(payin(data, remote=ip))).state is PaymentState.FAILED


# ------------------------------------------------------------------------------------------- create


async def test_create_sends_exactly_the_signed_bytes() -> None:
    desk = FakeSeverPay()
    http = CountingHttp(desk)
    checkout = await provider(http, lifetime_min="60").create(intent(return_url="https://t.me/svbg_bot"))
    body = desk.created[0]
    assert body["order_id"] == PID and body["amount"] == 179 and body["currency"] == "RUB"
    assert body["client_id"] == "c0ffee00c0ffee00c0ffee00"
    assert body["client_email"] == "c0ffee00c0ffee00c0ffee00@telegram.org"
    assert body["url_return"] == "https://t.me/svbg_bot" and body["lifetime"] == 60 and body["mid"] == MID
    assert len(body["salt"]) == 16
    call = http.calls[0]
    assert call.method == "POST" and call.url == f"{FAKE_BASE_URL}/payin/create" and call.json is None
    raw = call.data.decode()
    sent = json.loads(raw)
    keys = list(sent)
    assert keys[-1] == "sign" and keys[:-1] == sorted(keys[:-1])
    assert raw.startswith('{"amount":179,"client_email"') and raw.endswith(f'"sign":"{sent["sign"]}"}}')
    assert "/" in raw and "\\/" not in raw  # slashes are not escaped in requests
    assert checkout.kind == "url" and checkout.external_id == "1001"
    assert checkout.pay_url is not None and checkout.pay_url.startswith("https://severpay.fake/pay/")
    assert checkout.expires_at is not None
    left = checkout.expires_at - datetime.now(UTC)
    assert timedelta(minutes=58) < left <= timedelta(minutes=61)


async def test_create_fraction_and_defaults() -> None:
    desk = FakeSeverPay()
    http = CountingHttp(desk)
    p = provider(http)
    await p.create(intent(amount_minor=17_950, customer_ref="weird ref/with spaces"))
    body = desk.created[0]
    assert body["amount"] == 179.5 and b'"amount":179.5,' in http.calls[0].data
    assert "lifetime" not in body and "url_return" not in body
    assert body["client_id"] != "weird ref/with spaces" and len(body["client_id"]) == 24
    await provider(CountingHttp(desk), return_url="https://t.me/owner_bot").create(
        intent(return_url="https://t.me/core_bot")
    )
    assert desk.created[1]["url_return"] == "https://t.me/owner_bot"


async def test_salt_is_fresh_per_request() -> None:
    desk = FakeSeverPay()
    p = provider(CountingHttp(desk))
    await p.create(intent())
    await p.create(intent())
    assert desk.created[0]["salt"] != desk.created[1]["salt"]


async def test_create_rejects_other_currencies_without_a_request() -> None:
    http = CountingHttp(FakeSeverPay())
    with pytest.raises(ProviderError):
        await provider(http).create(intent(currency="USD", amount_minor=200))
    assert http.requests == 0


@pytest.mark.parametrize(
    ("answer", "retryable", "words"),
    [
        (HttpResponse(200, b'{"status": false, "msg": "Amount too small"}'), False, "Amount too small"),
        (HttpResponse(401, b'{"status": false, "msg": "Invalid sign"}'), False, "MID или ключ"),
        (HttpResponse(403, b"<html>forbidden</html>"), False, "MID или ключ"),
        (HttpResponse(502, b"<html>bad gateway</html>"), True, "недоступен"),
        (HttpResponse(429, b""), True, "недоступен"),
        (HttpResponse(200, b"not json"), True, "непонятный"),
        (HttpResponse(200, b'{"status": true, "msg": "", "data": {"id": 1}}'), False, "непонятный"),
        (HttpResponse(200, b'{"ok": 1}'), False, "непонятный"),
    ],
    ids=["status-false", "401", "403-html", "502", "429", "not-json", "no-url", "no-status"],
)
async def test_create_errors(answer: HttpResponse, retryable: bool, words: str) -> None:
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(lambda _call: answer)).create(intent())
    assert err.value.retryable is retryable and words in err.value.human
    assert TOKEN not in err.value.human


async def test_wrong_token_is_rejected_by_the_desk() -> None:
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(FakeSeverPay(token="another"))).create(intent())
    assert "Invalid sign" in err.value.human and not err.value.retryable


# ------------------------------------------------------------------------------------------- status


async def test_fetch_status_one_request_per_payment() -> None:
    desk = FakeSeverPay()
    desk.add(11, PID, amount=179, status="success")
    desk.add(12, "foreign", amount=99.9, status="decline")
    desk.add(13, PID, status="weird")
    http = CountingHttp(desk)
    statuses = await provider(http).fetch_status(["11", "12", "13", "11", "99", "UID12", ""])
    assert http.requests == 5
    assert desk.status_calls == [{"id": 11}, {"id": 12}, {"id": 13}, {"id": 99}, {"uid": "UID12"}]
    by_id = {s.external_id: s for s in statuses}
    assert set(by_id) == {"11", "12"}
    assert by_id["11"].state is PaymentState.PAID and by_id["11"].payment_id == PID
    assert by_id["11"].amount == Decimal("179") and by_id["11"].currency == "RUB"
    assert by_id["12"].state is PaymentState.FAILED and by_id["12"].amount == Decimal("99.9")
    assert by_id["12"].payment_id is None and len(statuses) == 3  # the uid lookup reports id 12 again


async def test_fetch_status_errors() -> None:
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(lambda _c: HttpResponse(503, b""))).fetch_status(["1"])
    assert err.value.retryable
    with pytest.raises(ProviderError) as err:
        await provider(
            CountingHttp(lambda _c: HttpResponse(401, b'{"status":false,"msg":"x"}'))
        ).fetch_status(["1"])
    assert not err.value.retryable
    bad = HttpResponse(200, b'{"status": true, "data": {"id": 1, "status": "success", "amount": "x"}}')
    assert await provider(CountingHttp(lambda _c: bad)).fetch_status(["1"]) == []


# ------------------------------------------------------------------------------------------- probe


async def test_test_credentials() -> None:
    probe = await provider(CountingHttp(FakeSeverPay())).test_credentials()
    assert (
        probe.ok and "Demo shop" in probe.message and "RUB" in probe.message and "100–150000" in probe.message
    )
    assert probe.details["min"] == "100" and probe.details["max"] == "150000"
    bad = await provider(CountingHttp(FakeSeverPay(token="x"))).test_credentials()
    assert not bad.ok and "Invalid sign" in bad.message
    other_mid = await provider(CountingHttp(FakeSeverPay()), mid="2").test_credentials()
    assert not other_mid.ok and "Merchant not found" in other_mid.message
    usd = await provider(CountingHttp(FakeSeverPay(currency="USD"))).test_credentials()
    assert not usd.ok and "USD" in usd.message
    down = await provider(CountingHttp(lambda _c: HttpResponse(502, b""))).test_credentials()
    assert not down.ok and "недоступен" in down.message
    foreign = HttpResponse(200, b'{"status": true, "data": {"mid": 9, "currency": "RUB"}}')
    assert not (await provider(CountingHttp(lambda _c: foreign)).test_credentials()).ok


def test_default_base_url_is_official() -> None:
    assert DEFAULT_BASE_URL == "https://severpay.io/api/merchant"


# ------------------------------------------------------------------------------ through the real core


async def test_success_is_credited_only_after_payin_get(
    make_harness: HarnessFactory, db: CountingDatabase
) -> None:
    desk = FakeSeverPay()
    harness = await make_harness(SeverPay, CONFIG, http=CountingHttp(desk))
    result = await harness.core.create_payment(
        user_id=harness.user_id,
        instance_id=harness.instance.id,
        amount_minor=17_900,
        currency="RUB",
        description="Пополнение",
        method_kind="sbp",
    )
    ext = result.checkout.external_id
    assert ext is not None and desk.payins[int(ext)].order_id == result.payment_id
    # «success» webhook while /payin/get still says «new»: nothing is credited (spec vector 8)
    assert await harness.send(desk.webhook(int(ext), "success")) == 200
    await harness.core.verify(harness.instance.id, [(ext, result.payment_id)])
    assert await harness.status(result.payment_id) == "pending" and harness.credited == []
    # really paid: the re-read credits once
    desk.set_status(int(ext), "success")
    assert await harness.send(desk.webhook(int(ext), salt="second")) == 200
    await harness.core.verify(harness.instance.id, [(ext, result.payment_id)])
    await harness.core.verify(harness.instance.id, [(ext, result.payment_id)])
    assert await harness.status(result.payment_id) == "paid" and len(harness.credited) == 1
    assert "verify_queued" in await outcomes(db)


async def test_fail_from_webhook_then_late_success(make_harness: HarnessFactory) -> None:
    desk = FakeSeverPay()
    harness = await make_harness(SeverPay, CONFIG, http=CountingHttp(desk))
    result = await harness.core.create_payment(
        user_id=harness.user_id, instance_id=harness.instance.id, amount_minor=17_900, currency="RUB",
        description="x",
    )  # fmt: skip
    ext = result.checkout.external_id
    assert ext is not None
    desk.set_status(int(ext), "fail")
    assert await harness.send(desk.webhook(int(ext))) == 200
    assert await harness.status(result.payment_id) == "failed"
    desk.set_status(int(ext), "success")
    assert await harness.send(desk.webhook(int(ext))) == 200
    await harness.core.verify(harness.instance.id, [(ext, result.payment_id)])
    assert await harness.status(result.payment_id) == "paid" and len(harness.credited) == 1


async def test_forged_webhook_and_wrong_amount(make_harness: HarnessFactory) -> None:
    desk = FakeSeverPay()
    harness = await make_harness(SeverPay, CONFIG, http=CountingHttp(desk))
    result = await harness.core.create_payment(
        user_id=harness.user_id, instance_id=harness.instance.id, amount_minor=17_900, currency="RUB",
        description="x",
    )  # fmt: skip
    ext = result.checkout.external_id
    assert ext is not None
    assert await harness.send(desk.webhook(int(ext), "success", token="guessed")) == 401
    desk.set_status(int(ext), "success", amount=17.9)
    await harness.core.verify(harness.instance.id, [(ext, result.payment_id)])
    assert await harness.status(result.payment_id) == "mismatch" and harness.credited == []


async def test_end_to_end_over_real_http(make_harness: HarnessFactory) -> None:
    async with FakeSeverPay() as desk:
        http = InstanceHttp()
        try:
            harness = await make_harness(SeverPay, {**CONFIG, "base_url": desk.base_url}, http=http)
            result = await harness.core.create_payment(
                user_id=harness.user_id,
                instance_id=harness.instance.id,
                amount_minor=49_950,
                currency="RUB",
                description="Пополнение баланса",
            )
            ext = result.checkout.external_id
            assert ext is not None and desk.payins[int(ext)].order_id == result.payment_id
            assert desk.created[0]["amount"] == 499.5
            desk.set_status(int(ext), "success")
            assert await harness.send(desk.webhook(int(ext))) == 200
            await harness.core.verify(harness.instance.id, [(ext, result.payment_id)])
            assert await harness.status(result.payment_id) == "paid" and len(harness.credited) == 1
        finally:
            await http.close()
