"""Pal24 / PayPalych plugin: the shared TestKit plus protocol vectors from ``docs/providers/pal24.md``.

The postback signature ``md5(OutSum:InvId:token)`` does not cover ``Status``, so the plugin declares a weak
scheme: a correctly signed postback is accepted **for verification** and the core re-reads the bill before
anything changes. The weak TestKit therefore passes completely (bad / missing signatures → 401, nothing is
credited by a postback alone).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qsl, urlencode

import pytest

from svbg.jobs.queue import Job
from svbg.payments.core import VERIFY_JOB
from svbg.payments.providers.pal24 import (
    DEFAULT_BASE_URL,
    PAYMENT_REF,
    Pal24,
    chargeback_signature,
    payment_signature,
    refund_signature,
    transfer_signature,
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
    HttpResponse,
    PaymentIntent,
    PaymentState,
    ProviderError,
    WebhookIgnored,
    WebhookRejected,
    WebhookRequest,
)
from tests.dbkit import CountingDatabase
from tests.fakes.pal24 import (
    API_TOKEN,
    FAKE_BASE_URL,
    OTHER_TOKEN,
    SHOP_ID,
    FakePal24,
    md5_upper,
    postback,
)
from tests.payments.providers.conftest import HarnessFactory, outcomes, payment_row

CONFIG = {"api_token": API_TOKEN, "shop_id": SHOP_ID, "base_url": FAKE_BASE_URL}
PID = "0190f5d2-7b1e-7c3a-9d4e-5f6a7b8c9d0e"
SPEC_TOKEN = "test_token_svbg"  # the conditional token of the specification's vectors (§10)
_STATUS = {PaymentState.PAID: "SUCCESS", PaymentState.FAILED: "FAIL", PaymentState.PROCESSING: "PROCESS"}


def fields(**kw: Any) -> dict[str, str]:
    data = {
        "InvId": PID,
        "OutSum": "179.00",
        "Commission": "0.00",
        "TrsId": "Bill0001",
        "Status": "SUCCESS",
        "CurrencyIn": "RUB",
        "custom": "",
    }
    data.update(kw)
    token = data.pop("token", API_TOKEN)
    if "SignatureValue" not in data:
        data["SignatureValue"] = md5_upper(data["OutSum"], data["InvId"], token)
    return {k: v for k, v in data.items() if v is not None}


class Pal24Vectors:
    """:class:`~svbg.payments.testkit.WebhookVectors` of Pal24. Pal24 has no test mode: a «test» postback is
    one signed with another merchant's token."""

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
        return postback(
            fields(
                InvId=payment_id,
                OutSum=amount,
                TrsId=external_id,
                Status=_STATUS.get(state, "FAIL"),
                CurrencyIn=currency,
                token=OTHER_TOKEN if test else API_TOKEN,
            )
        )

    def tamper(self, req: WebhookRequest) -> WebhookRequest:
        data = dict(parse_qsl(req.body.decode()))
        data["OutSum"] = "1" + data["OutSum"]
        return postback(data)

    def strip_auth(self, req: WebhookRequest) -> WebhookRequest:
        return postback({k: v for k, v in parse_qsl(req.body.decode()) if k != "SignatureValue"})


def provider(http: CountingHttp | None = None, *, is_test: bool = False, **config: Any) -> Pal24:
    p = make_provider(Pal24, {**CONFIG, **config}, http=http, is_test=is_test)
    assert isinstance(p, Pal24)
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


def _job(payload: Any) -> Job:
    now = datetime.now(UTC)
    return Job(
        id=1, queue="default", lane="interactive", kind=VERIFY_JOB,
        payload=payload if isinstance(payload, dict) else json.loads(payload),
        attempts=1, max_attempts=8, ordering_key=None, dedup_key=None, caused_by=None, locked_by=None,
        locked_until=None, next_run_at=now, created_at=now,
    )  # fmt: skip


async def run_verify_jobs(db: CountingDatabase, harness: Any) -> int:
    """Run the queued ``payments.verify`` jobs the way the worker would; returns how many ran."""
    jobs = await db.raw("select id, payload from jobs where kind = $1 order by id", VERIFY_JOB)
    for job in jobs:
        await harness.core.verify_job(_job(job["payload"]), None)
    await db.raw("delete from jobs where kind = $1", VERIFY_JOB)
    return len(jobs)


async def create(harness: Any, amount_minor: int = 17_900) -> tuple[str, str]:
    result = await harness.core.create_payment(
        user_id=harness.user_id,
        instance_id=harness.instance.id,
        amount_minor=amount_minor,
        currency="RUB",
        description="Пополнение баланса",
    )
    assert result.checkout.external_id
    return result.payment_id, result.checkout.external_id


# ------------------------------------------------------------------------------------------- TestKit


def test_static_contract() -> None:
    check_static(Pal24)
    caps = Pal24.capabilities
    assert caps.webhook_auth.is_weak and caps.fetch_status and not caps.batch_status
    assert caps.replay_window_s is None and not caps.refund and not caps.recurring
    assert Pal24.manifest.currencies == ("RUB", "USD", "EUR")
    assert Pal24.manifest.min_minor is None and Pal24.manifest.max_minor is None  # not published
    for name, fld in Pal24.manifest.config.fields().items():
        assert fld.title and fld.description, name
        if fld.required and fld.default is None:
            assert fld.where, f"{name}: «где взять» is missing"


async def test_testkit_plugin_level() -> None:
    await check_plugin(Pal24, CONFIG, Pal24Vectors())


async def test_testkit_core_level(make_harness: HarnessFactory, db: CountingDatabase) -> None:
    harness = await make_harness(Pal24, CONFIG, http=CountingHttp(FakePal24()))
    await check_core(harness, Pal24Vectors())
    assert harness.credited == []  # a postback never credits by itself
    assert {"bad_signature"} <= set(await outcomes(db))


@pytest.mark.parametrize("domain", [True, False], ids=["domain", "no-domain"])
async def test_testkit_poll_budget(make_harness: HarnessFactory, domain: bool) -> None:
    http = CountingHttp()
    harness = await make_harness(Pal24, CONFIG, http=http, has_domain=domain)

    def pending(call: Any) -> HttpResponse:
        bill = {
            "id": call.params["id"],
            "status": "NEW",
            "amount": 179.0,
            "currency_in": "RUB",
            "success": True,
        }
        return HttpResponse(200, json.dumps(bill).encode())

    http.responder = pending
    used = await check_poll_budget(harness, domain=domain, invoices=3)
    assert 0 < used <= (3 if domain else 24) * 3


# ------------------------------------------------------------------------------ signature vectors (§10)


@pytest.mark.parametrize(
    ("out_sum", "inv_id", "expected"),
    [
        ("179.00", "9f1c2d3e-0000-4000-8000-000000000001", "F458072D65D4B09CEF236402BBC66B2F"),  # P1
        ("18.54", "Заказ 123", "381FB1C68056EB88C4BDF52AE0A6230A"),  # P2: Cyrillic InvId, UTF-8
        ("179", "pay-1", "007A0AE489DA43DCC50364D609B93A13"),  # P3: no fraction, another signature
    ],
)
def test_payment_signature_vectors(out_sum: str, inv_id: str, expected: str) -> None:
    assert payment_signature(out_sum, inv_id, SPEC_TOKEN) == expected
    assert md5_upper(out_sum, inv_id, SPEC_TOKEN) == expected


def test_refund_and_chargeback_vectors() -> None:
    r1 = refund_signature("179.00", "RUB", "KSvv3R7123", "QSvv3R7123", "ZqmawDQ9B7l", SPEC_TOKEN)
    assert r1 == "1636822278CF2E3F74A050CFEB12A874"
    c1 = chargeback_signature("KSvv3R7123", "QSvv3R7123", "ZqmawDQ9B7l", SPEC_TOKEN)
    assert c1 == "15452FC19FD127531A1BCF67D11EE6AD"
    assert transfer_signature("100.00", "T1", SPEC_TOKEN) == md5_upper("100.00", "T1", SPEC_TOKEN)


P1 = {
    "InvId": "9f1c2d3e-0000-4000-8000-000000000001",
    "OutSum": "179.00",
    "TrsId": "Bill0001",
    "Status": "SUCCESS",
    "CurrencyIn": "RUB",
    "SignatureValue": "F458072D65D4B09CEF236402BBC66B2F",
}


async def test_p1_postback_with_the_spec_token() -> None:
    event = await provider(api_token=SPEC_TOKEN).parse_webhook(postback(P1))
    assert event.state is PaymentState.PAID and event.external_id == "Bill0001"
    assert event.payment_id == "9f1c2d3e-0000-4000-8000-000000000001"
    assert event.amount == Decimal("179") and event.currency == "RUB" and event.signed_at is None


async def test_p1_lower_case_signature_passes() -> None:
    req = postback({**P1, "SignatureValue": P1["SignatureValue"].lower()})
    assert (await provider(api_token=SPEC_TOKEN).parse_webhook(req)).state is PaymentState.PAID


async def test_p1_with_another_spelling_of_the_amount_fails() -> None:
    """The signature is over the raw string: ``179.0`` is not ``179.00``."""
    with pytest.raises(WebhookRejected) as err:
        await provider(api_token=SPEC_TOKEN).parse_webhook(postback({**P1, "OutSum": "179.0"}))
    assert err.value.status == 401


async def test_status_is_not_signed_so_a_flip_still_verifies() -> None:
    """Expected protocol behaviour (§10): FAIL → SUCCESS keeps the signature. The plugin accepts it as a hint
    only; the weak scheme makes the core re-read the bill (see the core-level tests)."""
    plugin = provider(api_token=SPEC_TOKEN)
    failed = await plugin.parse_webhook(postback({**P1, "Status": "FAIL"}))
    flipped = await plugin.parse_webhook(postback({**P1, "Status": "SUCCESS"}))
    assert failed.state is PaymentState.FAILED and flipped.state is PaymentState.PAID
    assert Pal24.capabilities.webhook_auth.is_weak


async def test_p2_cyrillic_inv_id() -> None:
    req = postback({"InvId": "Заказ 123", "OutSum": "18.54", "Commission": "2.54", "TrsId": "GkLWvKx3",
                    "Status": "SUCCESS", "CurrencyIn": "RUB",
                    "SignatureValue": "381FB1C68056EB88C4BDF52AE0A6230A"})  # fmt: skip
    event = await provider(api_token=SPEC_TOKEN).parse_webhook(req)
    assert event.payment_id is None and event.external_id == "GkLWvKx3"  # not our id: the bill decides
    assert event.amount == Decimal("18.54") and event.summary["commission"] == "2.54"


@pytest.mark.parametrize(
    "data",
    [
        {k: v for k, v in P1.items() if k != "SignatureValue"},
        {**P1, "SignatureValue": ""},
        {**P1, "SignatureValue": md5_upper("179.00", P1["InvId"], OTHER_TOKEN)},
        {**P1, "InvId": PID},
    ],
    ids=["missing", "empty", "other-token", "other-order"],
)
async def test_bad_signatures_are_401(data: dict[str, str]) -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider(api_token=SPEC_TOKEN).parse_webhook(postback(data))
    assert err.value.status == 401


# ----------------------------------------------------------------------------------------- postbacks


@pytest.mark.parametrize(
    ("status", "state"),
    [
        ("SUCCESS", PaymentState.PAID),
        ("OVERPAID", PaymentState.PAID),
        ("UNDERPAID", PaymentState.PAID),
        ("FAIL", PaymentState.FAILED),
        ("success", PaymentState.PAID),
        ("SOMETHING_NEW", PaymentState.PROCESSING),
        ("", PaymentState.PROCESSING),
    ],
)
async def test_postback_status_is_only_a_hint(status: str, state: PaymentState) -> None:
    event = await provider().parse_webhook(postback(fields(Status=status)))
    assert event.state is state and event.summary["status"] == (status.upper() or None)


@pytest.mark.parametrize("amount", ["179", "179.0", "179.00"])
async def test_amount_forms(amount: str) -> None:
    assert (await provider().parse_webhook(postback(fields(OutSum=amount)))).amount == Decimal("179")


async def test_summary_has_no_personal_data() -> None:
    req = postback(fields(PayerEmail="buyer@example.com", PayerPhone="79990001122", PayerName="Иван",
                          AccountNumber="553691******8079"))  # fmt: skip
    event = await provider().parse_webhook(req)
    text = json.dumps(dict(event.summary), ensure_ascii=False)
    assert "buyer@" not in text and "7999" not in text and "Иван" not in text and "553691" not in text


async def test_is_test_follows_the_instance() -> None:
    assert not (await provider().parse_webhook(postback(fields()))).is_test
    assert (await provider(is_test=True).parse_webhook(postback(fields()))).is_test


async def test_refund_postback_points_at_the_payment() -> None:
    sig = md5_upper("179.00", "RUB", "Bill0001", "Pay0001", "Ref0001", API_TOKEN)
    req = postback({"Id": "Ref0001", "Amount": "179.00", "Currency": "RUB", "Status": "SUCCESS", "InvId": PID,
                    "BillId": "Bill0001", "PaymentId": "Pay0001", "SignatureValue": sig})  # fmt: skip
    event = await provider().parse_webhook(req)
    assert event.state is PaymentState.REFUNDED and event.external_id == f"{PAYMENT_REF}Pay0001"
    assert event.payment_id == PID and event.amount is None and event.summary["kind"] == "refund"


async def test_chargeback_postback() -> None:
    sig = md5_upper("Bill0001", "Pay0001", "Chb0001", API_TOKEN)
    base = {
        "Id": "Chb0001",
        "InvId": PID,
        "BillId": "Bill0001",
        "PaymentId": "Pay0001",
        "SignatureValue": sig,
    }
    event = await provider().parse_webhook(postback({**base, "Status": "SUCCESS"}))
    assert event.state is PaymentState.CHARGEBACK and event.external_id == f"{PAYMENT_REF}Pay0001"
    pending = await provider().parse_webhook(postback({**base, "Status": "FAIL"}))
    assert pending.state is PaymentState.PROCESSING  # still re-read: Status is not signed
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(postback({**base, "BillId": "Bill0002", "Status": "SUCCESS"}))
    assert err.value.status == 401


async def test_refund_postback_with_a_wrong_signature() -> None:
    sig = md5_upper("1.00", "RUB", "Bill0001", "Pay0001", "Ref0001", API_TOKEN)
    req = postback({"Id": "Ref0001", "Amount": "179.00", "Currency": "RUB", "Status": "SUCCESS", "InvId": PID,
                    "BillId": "Bill0001", "PaymentId": "Pay0001", "SignatureValue": sig})  # fmt: skip
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(req)
    assert err.value.status == 401


async def test_transfer_postbacks_are_acknowledged_without_changes() -> None:
    good = {"TrsId": "T1", "Amount": "100.00", "InvId": "deal-1", "Status": "SUCCESS",
            "Payment[TrsId]": "P1", "SignatureValue": md5_upper("100.00", "T1", API_TOKEN)}  # fmt: skip
    with pytest.raises(WebhookIgnored) as ignored:
        await provider().parse_webhook(postback(good))
    assert ignored.value.response.status == 200
    with pytest.raises(WebhookRejected):
        await provider().parse_webhook(postback({**good, "Amount": "1.00"}))


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"OutSum=1&OutSum=2&InvId=x&SignatureValue=A",
        b"Foo=bar",
        b"\xff\xfe",
        b"not a form",
    ],
    ids=["empty", "repeated", "unknown", "not-utf8", "not-a-form"],
)
async def test_malformed_postbacks_are_400(body: bytes) -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(WebhookRequest(body=body, headers={"Content-Type": "x"}))
    assert err.value.status == 400


@pytest.mark.parametrize(
    "data",
    [fields(OutSum="abc"), fields(TrsId="", InvId="not-ours")],
    ids=["bad-amount", "no-ids"],
)
async def test_signed_but_unusable_postbacks_are_400(data: dict[str, str]) -> None:
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(postback(data))
    assert err.value.status == 400


def test_ack_is_200_ok() -> None:
    resp = provider().ack(None)
    assert resp.status == 200 and resp.body == b"OK"


# ------------------------------------------------------------------------------------------- create


async def test_create_request_shape_and_privacy() -> None:
    desk = FakePal24()
    http = CountingHttp(desk)
    checkout = await provider(http).create(intent(amount_minor=17_950))
    sent = desk.created[0]
    assert sent["amount"] == "179.50" and sent["shop_id"] == SHOP_ID and sent["order_id"] == PID
    assert sent["type"] == "normal" and sent["currency_in"] == "RUB" and sent["payer_pays_commission"] == "0"
    assert "payment_method" not in sent and "ttl" not in sent and "custom" not in sent
    assert "success_url" not in sent and "c0ffee" not in json.dumps(sent)
    assert checkout.kind == "url" and checkout.external_id == "Bill0001" and checkout.expires_at is None
    assert checkout.pay_url == "https://pally.fake/transfer/Bill0001"  # link_page_url, not the QR page
    call = http.calls[0]
    assert call.method == "POST" and call.url == f"{FAKE_BASE_URL}/api/v1/bill/create"
    assert call.headers["Authorization"] == f"Bearer {API_TOKEN}" and isinstance(call.data, dict)


async def test_create_with_options() -> None:
    desk = FakePal24()
    plugin = provider(CountingHttp(desk), payment_method="SBP", bill_ttl="600", payer_pays_commission="true")
    before = datetime.now(UTC)
    checkout = await plugin.create(intent(currency="USD"))
    sent = desk.created[0]
    assert sent["payment_method"] == "SBP" and sent["ttl"] == "600" and sent["payer_pays_commission"] == "1"
    assert sent["currency_in"] == "USD"
    assert checkout.expires_at is not None and 590 <= (checkout.expires_at - before).total_seconds() <= 610


async def test_create_accepts_boolean_success() -> None:
    async def answer(_call: Any) -> HttpResponse:
        data = {"success": True, "link_page_url": "https://pally.info/transfer/X1", "bill_id": "X1"}
        return HttpResponse(200, json.dumps(data).encode())

    checkout = await provider(CountingHttp(answer)).create(intent())
    assert checkout.external_id == "X1"


async def test_create_refuses_foreign_currency() -> None:
    http = CountingHttp(FakePal24())
    with pytest.raises(ProviderError, match="RUB, USD"):
        await provider(http).create(intent(currency="KZT", amount_minor=100_000))
    assert http.requests == 0


@pytest.mark.parametrize(
    ("status", "key", "retryable", "words"),
    [
        (403, "api:error.ip_access_denied", False, "вайтлист"),
        (403, "api:error.access_denied", False, "магазин"),
        (403, "api:error.merchant_banned", False, "заблокировала"),
        (403, "api:error.invalid_amount", False, "сумму"),
        (403, "api:error.rate-not-found", False, "недоступно"),
        (400, "api:error.transaction_rejected", False, "transaction_rejected"),
        (422, None, False, "параметры"),
        (429, None, True, "временно"),
        (500, "api:error.general_error", True, "временно"),
    ],
)
async def test_create_errors(status: int, key: str | None, retryable: bool, words: str) -> None:
    desk = FakePal24()
    desk.fail_with = (status, key)
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(desk)).create(intent())
    assert err.value.retryable is retryable and words in err.value.human and API_TOKEN not in err.value.human


async def test_create_with_a_wrong_token() -> None:
    with pytest.raises(ProviderError, match="токен") as err:
        await provider(CountingHttp(FakePal24(api_token="1|other"))).create(intent())
    assert err.value.status == 401 and not err.value.retryable


@pytest.mark.parametrize(
    "answer",
    [
        {"success": "false", "link_page_url": "https://x", "bill_id": "B"},
        {"success": "true", "bill_id": "B"},
        {"success": "true", "link_page_url": "ftp://x", "bill_id": "B"},
        {"success": "true", "link_page_url": "https://x"},
        [1, 2],
    ],
)
async def test_create_with_a_bad_answer(answer: Any) -> None:
    async def junk(_call: Any) -> HttpResponse:
        return HttpResponse(200, json.dumps(answer).encode())

    with pytest.raises(ProviderError):
        await provider(CountingHttp(junk)).create(intent())


# ------------------------------------------------------------------------------------------- status


@pytest.mark.parametrize(
    ("status", "state", "amount"),
    [
        ("NEW", PaymentState.CREATED, Decimal("179")),
        ("PROCESS", PaymentState.PROCESSING, Decimal("179")),
        ("SUCCESS", PaymentState.PAID, Decimal("179")),
        ("OVERPAID", PaymentState.PAID, Decimal("179")),
        ("UNDERPAID", PaymentState.PAID, None),
        ("FAIL", PaymentState.FAILED, Decimal("179")),
    ],
)
async def test_fetch_bill_status(status: str, state: PaymentState, amount: Decimal | None) -> None:
    desk = FakePal24()
    plugin = provider(CountingHttp(desk))
    await plugin.create(intent())
    desk.bills["Bill0001"].status = status
    [st] = await plugin.fetch_status(["Bill0001"])
    assert st.state is state and st.external_id == "Bill0001" and st.payment_id == PID
    assert st.currency == "RUB"
    if state is PaymentState.PAID:
        assert st.amount == amount
    assert desk.status_calls == [("bill", "Bill0001")]


async def test_fetch_status_unknown_and_weird() -> None:
    desk = FakePal24()
    plugin = provider(CountingHttp(desk))
    await plugin.create(intent())
    desk.bills["Bill0001"].status = "STRANGE"
    assert await plugin.fetch_status(["Bill0001", "Nope", "", "Bill0001"]) == []
    assert desk.status_calls == [("bill", "Bill0001"), ("bill", "Nope")]


async def test_fetch_status_rejects_an_answer_about_another_bill() -> None:
    async def other(_call: Any) -> HttpResponse:
        bill = {"id": "Other", "order_id": PID, "status": "SUCCESS", "amount": 179, "currency_in": "RUB"}
        return HttpResponse(200, json.dumps({**bill, "success": True}).encode())

    assert await provider(CountingHttp(other)).fetch_status(["Bill0001"]) == []


async def test_fetch_status_reads_wrapped_resources_and_foreign_order_ids() -> None:
    async def wrapped(_call: Any) -> HttpResponse:
        bill = {"id": "Bill0001", "order_id": "order-1", "status": "SUCCESS", "amount": "179.00",
                "currency_in": "RUB"}  # fmt: skip
        return HttpResponse(200, json.dumps({"success": True, "data": bill}).encode())

    [st] = await provider(CountingHttp(wrapped)).fetch_status(["Bill0001"])
    assert st.state is PaymentState.PAID and st.payment_id is None and st.amount == Decimal("179")


async def test_fetch_payment_status_for_refunds_and_chargebacks() -> None:
    desk = FakePal24()
    plugin = provider(CountingHttp(desk))
    await plugin.create(intent())
    payment = desk.pay("Bill0001")
    ref = f"{PAYMENT_REF}{payment.id}"
    [paid] = await plugin.fetch_status([ref])
    assert paid.state is PaymentState.PAID and paid.external_id == "Bill0001" and paid.payment_id == PID
    assert paid.amount == Decimal("179")
    desk.refund(payment, status="PROCESS")
    assert (await plugin.fetch_status([ref]))[0].state is PaymentState.PAID
    desk.refund(payment, amount="50.00")  # partial — still taken back (spec §6)
    assert (await plugin.fetch_status([ref]))[0].state is PaymentState.REFUNDED
    desk.chargeback(payment)
    assert (await plugin.fetch_status([ref]))[0].state is PaymentState.CHARGEBACK
    assert desk.status_calls[-1] == ("payment", payment.id)
    assert await plugin.fetch_status([f"{PAYMENT_REF}Nope"]) == []


async def test_fetch_payment_status_with_commission_reports_the_bill_amount() -> None:
    desk = FakePal24()
    plugin = provider(CountingHttp(desk))
    await plugin.create(intent())
    payment = desk.pay("Bill0001", commission="2.54")
    [st] = await plugin.fetch_status([f"{PAYMENT_REF}{payment.id}"])
    assert st.amount == Decimal("179")


@pytest.mark.parametrize(("status", "retryable"), [(429, True), (503, True), (400, False), (403, False)])
async def test_fetch_status_errors(status: int, retryable: bool) -> None:
    desk = FakePal24()
    desk.fail_with = (status, "api:error.ip_access_denied" if status == 403 else None)
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(desk)).fetch_status(["Bill0001"])
    assert err.value.retryable is retryable


async def test_test_credentials() -> None:
    desk = FakePal24()
    probe = await provider(CountingHttp(desk)).test_credentials()
    assert probe.ok and desk.created == []
    assert desk.requests[0][:2] == ("GET", "/api/v1/bill/search")
    bad = await provider(CountingHttp(FakePal24(api_token="1|other"))).test_credentials()
    assert not bad.ok and "токен" in bad.message
    foreign = await provider(CountingHttp(FakePal24(shop_id="Other"))).test_credentials()
    assert not foreign.ok and "магазин" in foreign.message
    blocked = FakePal24()
    blocked.fail_with = (403, "api:error.ip_access_denied")
    probe = await provider(CountingHttp(blocked)).test_credentials()
    assert not probe.ok and "вайтлист" in probe.message

    async def html(_call: Any) -> HttpResponse:
        return HttpResponse(404, b"<html>nginx</html>")

    assert "Адрес API" in (await provider(CountingHttp(html)).test_credentials()).message


def test_config_defaults_and_secrets() -> None:
    cfg = Pal24.manifest.config.parse({"API_TOKEN": API_TOKEN, "SHOP_ID": SHOP_ID})
    assert cfg.base_url == DEFAULT_BASE_URL and cfg.payment_method == "any" and cfg.bill_ttl == 0
    assert cfg.payer_pays_commission is False
    assert API_TOKEN not in repr(cfg) and cfg.secret_values() == [API_TOKEN]


# --------------------------------------------------------------------------------------- core level


async def test_flipped_status_never_credits(make_harness: HarnessFactory, db: CountingDatabase) -> None:
    """A FAIL postback replayed with Status=SUCCESS keeps its signature; the bill says FAIL → nothing."""
    desk = FakePal24()
    harness = await make_harness(Pal24, CONFIG, http=CountingHttp(desk))
    pid, bill_id = await create(harness)
    payment = desk.pay(bill_id, "FAIL")
    forged = desk.payment_postback(payment, Status="SUCCESS")
    assert await harness.send(forged) == 200
    assert await harness.status(pid) == "pending" and harness.credited == []
    assert await run_verify_jobs(db, harness) == 1
    assert await harness.status(pid) == "failed" and harness.credited == []


async def test_postback_then_verification_credits_once(
    make_harness: HarnessFactory, db: CountingDatabase
) -> None:
    desk = FakePal24()
    harness = await make_harness(Pal24, CONFIG, http=CountingHttp(desk))
    pid, bill_id = await create(harness)
    payment = desk.pay(bill_id)
    for _ in range(3):
        assert await harness.send(desk.payment_postback(payment)) == 200
    assert await harness.status(pid) == "pending"  # a postback never changes state by itself
    assert "verify_queued" in await outcomes(db)
    await run_verify_jobs(db, harness)
    await harness.core.verify(harness.instance.id, [(bill_id, None)])
    assert await harness.status(pid) == "paid" and len(harness.credited) == 1
    row = await payment_row(db, pid)
    assert row["paid_amount_minor"] == 17_900 and row["external_id"] == bill_id


async def test_commission_on_the_payer_credits_the_bill_amount(
    make_harness: HarnessFactory, db: CountingDatabase
) -> None:
    desk = FakePal24()
    harness = await make_harness(Pal24, {**CONFIG, "payer_pays_commission": "true"}, http=CountingHttp(desk))
    pid, bill_id = await create(harness)
    payment = desk.pay(bill_id, commission="2.54")
    assert await harness.send(desk.payment_postback(payment)) == 200  # OutSum=181.54
    await run_verify_jobs(db, harness)
    assert await harness.status(pid) == "paid"
    assert (await payment_row(db, pid))["paid_amount_minor"] == 17_900


async def test_underpaid_is_a_mismatch_and_overpaid_is_paid(
    make_harness: HarnessFactory, db: CountingDatabase
) -> None:
    desk = FakePal24()
    harness = await make_harness(Pal24, CONFIG, http=CountingHttp(desk))
    under_pid, under_bill = await create(harness)
    over_pid, over_bill = await create(harness)
    under = desk.pay(under_bill, "UNDERPAID", paid="100.00")
    over = desk.pay(over_bill, "OVERPAID", paid="200.00")
    assert await harness.send(desk.payment_postback(under)) == 200
    assert await harness.send(desk.payment_postback(over)) == 200
    assert await run_verify_jobs(db, harness) == 2
    assert await harness.status(under_pid) == "mismatch"
    assert await harness.status(over_pid) == "paid"
    assert (await payment_row(db, over_pid))["paid_amount_minor"] == 17_900
    assert [p.id for p in harness.credited] == [over_pid]


async def test_refund_and_chargeback_postbacks(make_harness: HarnessFactory, db: CountingDatabase) -> None:
    desk = FakePal24()
    harness = await make_harness(Pal24, CONFIG, http=CountingHttp(desk))
    pid, bill_id = await create(harness)
    pid2, bill2 = await create(harness)
    payment = desk.pay(bill_id)
    payment2 = desk.pay(bill2)
    await harness.send(desk.payment_postback(payment))
    await harness.send(desk.payment_postback(payment2))
    await run_verify_jobs(db, harness)
    assert await harness.status(pid) == "paid" and await harness.status(pid2) == "paid"
    # a refund postback that Pal24 does not confirm changes nothing
    pending_refund = desk.refund(payment, status="PROCESS")
    assert await harness.send(desk.refund_postback(payment, {**pending_refund, "status": "SUCCESS"})) == 200
    await run_verify_jobs(db, harness)
    assert await harness.status(pid) == "paid" and harness.refunded == []
    refund = desk.refund(payment)
    assert await harness.send(desk.refund_postback(payment, refund)) == 200
    await run_verify_jobs(db, harness)
    assert await harness.status(pid) == "refunded" and [p.id for p in harness.refunded] == [pid]
    desk.chargeback(payment2)
    assert await harness.send(desk.chargeback_postback(payment2)) == 200
    await run_verify_jobs(db, harness)
    assert await harness.status(pid2) == "refunded" and len(harness.refunded) == 2


async def test_postback_signed_with_another_token_is_401(
    make_harness: HarnessFactory, db: CountingDatabase
) -> None:
    desk = FakePal24()
    harness = await make_harness(Pal24, CONFIG, http=CountingHttp(desk))
    pid, bill_id = await create(harness)
    payment = desk.pay(bill_id)
    assert await harness.send(desk.payment_postback(payment, token=OTHER_TOKEN)) == 401
    assert await run_verify_jobs(db, harness) == 0
    assert await harness.status(pid) == "pending"


async def test_end_to_end_over_real_http(make_harness: HarnessFactory, db: CountingDatabase) -> None:
    async with FakePal24() as desk:
        http = InstanceHttp()
        try:
            harness = await make_harness(Pal24, {**CONFIG, "base_url": desk.base_url}, http=http)
            pid, bill_id = await create(harness)
            assert desk.created[0]["order_id"] == pid and desk.created[0]["amount"] == "179.00"
            row = await payment_row(db, pid)
            assert row["poll_plan"] == "domain"
            payment = desk.pay(bill_id)
            assert await harness.send(desk.payment_postback(payment)) == 200
            await run_verify_jobs(db, harness)
            assert await harness.status(pid) == "paid"
            assert (await harness.instance.provider.test_credentials()).ok
        finally:
            await http.close()


async def test_logs_no_secrets(caplog: pytest.LogCaptureFixture) -> None:
    desk = FakePal24()
    desk.fail_with = (400, "api:error.transaction_rejected")
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(ProviderError):
            await provider(CountingHttp(desk)).create(intent())
        with pytest.raises(WebhookRejected):
            await provider().parse_webhook(postback(fields(token=OTHER_TOKEN)))
        with pytest.raises(WebhookIgnored):
            good = {"TrsId": "T1", "Amount": "1.00", "SignatureValue": md5_upper("1.00", "T1", API_TOKEN)}
            await provider().parse_webhook(postback(good))
    assert API_TOKEN not in caplog.text and urlencode({"t": API_TOKEN})[2:] not in caplog.text
