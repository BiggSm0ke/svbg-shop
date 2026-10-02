"""The TestKit itself: a correct provider passes, broken ones are caught (07 §4.1)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from svbg.core.crypto import Crypto
from svbg.payments.testkit import (
    CoreHarness,
    CountingHttp,
    KitFailure,
    MemoryKV,
    check_core,
    check_plugin,
    check_static,
    make_context,
    make_provider,
)
from svbg.sdk import (
    Capabilities,
    HttpResponse,
    PaymentState,
    ProviderError,
    ProviderEvent,
    WebhookAuth,
    WebhookRequest,
    parse_amount,
)
from tests.dbkit import CountingDatabase
from tests.payments.conftest import (
    STUB_CONFIG,
    FakeStubServer,
    ManualPay,
    StubPay,
    StubVectors,
)


class NoCheckPay(StubPay):
    """Accepts anything: never verifies the signature."""

    async def parse_webhook(self, req: WebhookRequest) -> ProviderEvent:
        data = req.json()
        return ProviderEvent(
            state=PaymentState(data["status"]),
            external_id=data.get("id"),
            payment_id=data.get("order"),
            amount=parse_amount(data["amount"]),
            currency=data.get("currency"),
            signed_at=datetime.now(UTC),
        )


class NoTimePay(StubPay):
    """Declares a replay window but does not return the signed time."""

    async def parse_webhook(self, req: WebhookRequest) -> ProviderEvent:
        event = await super().parse_webhook(req)
        return ProviderEvent(
            state=event.state,
            external_id=event.external_id,
            payment_id=event.payment_id,
            amount=event.amount,
            currency=event.currency,
        )


class FloatPay(StubPay):
    """Compares amounts as floats/strings badly: "179.00" becomes 17900."""

    async def parse_webhook(self, req: WebhookRequest) -> ProviderEvent:
        event = await super().parse_webhook(req)
        amount = event.amount
        if amount is not None and "." in str(req.json().get("amount")):
            amount = amount * 100
        return ProviderEvent(
            state=event.state,
            external_id=event.external_id,
            payment_id=event.payment_id,
            amount=amount,
            currency=event.currency,
            signed_at=event.signed_at,
            is_test=event.is_test,
        )


class WeakNoFetch(StubPay):
    capabilities = Capabilities(webhook_auth=WebhookAuth.NONE)


async def test_correct_provider_passes_plugin_checks() -> None:
    await check_plugin(StubPay, STUB_CONFIG, StubVectors())
    await check_plugin(ManualPay, {"details": "x"}, StubVectors())  # no webhooks: static checks only


@pytest.mark.parametrize(
    ("cls", "message"),
    [
        (NoCheckPay, "tampered webhook was accepted"),
        (NoTimePay, "signed_at is not returned"),
        (FloatPay, "amount '179.00' parsed as"),
    ],
    ids=["no-signature-check", "no-signed-at", "bad-amounts"],
)
async def test_broken_providers_fail_plugin_checks(cls: type[StubPay], message: str) -> None:
    with pytest.raises(KitFailure) as err:
        await check_plugin(cls, STUB_CONFIG, StubVectors())
    assert any(message in f for f in err.value.failures), err.value.failures


def test_static_check_rejects_weak_without_fetch() -> None:
    check_static(StubPay)
    with pytest.raises(KitFailure, match="weak webhook authentication"):
        check_static(WeakNoFetch)


async def test_correct_provider_passes_core_checks(db: CountingDatabase, crypto: Crypto) -> None:
    harness = await CoreHarness.create(db, crypto, StubPay, STUB_CONFIG, http=CountingHttp(FakeStubServer()))
    try:
        await check_core(harness, StubVectors())
        outcomes = await harness.outcomes()
    finally:
        await harness.close()
    assert {"bad_signature", "stale", "applied", "mismatch", "test_rejected"} <= set(outcomes)
    assert len(harness.credited) == 3 and len(harness.refunded) == 1


async def test_core_checks_catch_a_provider_that_trusts_anything(
    db: CountingDatabase, crypto: Crypto
) -> None:
    harness = await CoreHarness.create(
        db, crypto, NoCheckPay, STUB_CONFIG, http=CountingHttp(FakeStubServer())
    )
    try:
        with pytest.raises(KitFailure) as err:
            await check_core(harness, StubVectors())
    finally:
        await harness.close()
    assert any("tampered webhook answered 200" in f for f in err.value.failures)


async def test_doubles() -> None:
    kv = MemoryKV()
    await kv.set("a", 1)
    assert await kv.get("a") == 1
    await kv.delete("a")
    assert await kv.get("a") is None
    http = CountingHttp()
    with pytest.raises(ProviderError):
        await http.request("GET", "https://x")
    http.responder = lambda call: HttpResponse(200, call.url.encode())
    assert (await http.request("GET", "https://y", params={"q": "1"})).text() == "https://y"
    assert http.requests == 2 and http.calls[1].params == {"q": "1"}
    ctx = make_context("stubpay", http=http, kv=kv, is_test=True)
    assert ctx.is_test and ctx.http is http
    provider = make_provider(StubPay, STUB_CONFIG, http=http)
    assert provider.config.api_key == STUB_CONFIG["api_key"]
