"""Telegram Stars plugin: invoice parameters, the pre-checkout decision, settlement through the core."""

from __future__ import annotations

from typing import Any

import pytest

from svbg.payments.core import CheckoutError, Outcome, SpendDeniedError
from svbg.payments.providers.stars import (
    TEXTS,
    PayloadKind,
    TelegramStars,
    classify_payload,
)
from svbg.payments.testkit import CountingHttp, check_plugin, check_static, make_provider
from svbg.sdk import ConfigError, PaymentIntent, ProviderError
from tests.dbkit import CountingDatabase
from tests.payments.providers.conftest import HarnessFactory, add_user, payment_row

PID = "0190f5d2-7b1e-7c3a-9d4e-5f6a7b8c9d0e"


def stars(rate: str = "1", **config: Any) -> TelegramStars:
    p = make_provider(TelegramStars, {"rate": rate, **config})
    assert isinstance(p, TelegramStars)
    return p


def intent(amount_minor: int = 179, **kw: Any) -> PaymentIntent:
    values: dict[str, Any] = {
        "payment_id": PID,
        "amount_minor": amount_minor,
        "currency": "XTR",
        "description": "Пополнение баланса на 179 ₽",
        "customer_ref": "c0ffee",
    }
    values.update(kw)
    return PaymentIntent(**values)


def test_static_contract() -> None:
    check_static(TelegramStars)
    caps = TelegramStars.capabilities
    assert not caps.webhook and not caps.fetch_status and caps.in_chat_invoice and not caps.redirect
    assert TelegramStars.manifest.currencies == ("XTR",)


async def test_testkit_plugin_level_is_static_only() -> None:
    await check_plugin(TelegramStars, {"rate": "1"}, vectors=None)  # type: ignore[arg-type] - no webhooks


@pytest.mark.parametrize(
    ("raw", "ok"),
    [
        ("1", True),
        ("1.79", True),
        ("1,8", True),
        ("0.5", True),
        ("1.795", False),
        ("-1", False),
        ("abc", False),
    ],
    ids=["1", "1.79", "comma", "half", "three-decimals", "negative", "text"],
)
def test_rate_is_whole_kopecks(raw: str, ok: bool) -> None:
    """Billing reads the same ``PAY_STARS_RATE`` and needs whole minor units per star."""
    if ok:
        assert stars(raw).rate > 0
    else:
        with pytest.raises(ConfigError):
            stars(raw)


def test_default_rate_is_one() -> None:
    assert make_provider(TelegramStars, {}).config.rate == "1"


async def test_create_returns_bot_api_invoice_parameters() -> None:
    http = CountingHttp()
    plugin = make_provider(TelegramStars, {"rate": "1.79"}, http=http)
    checkout = await plugin.create(
        intent(100, description="Пополнение баланса для покупки тарифа «Год» со скидкой")
    )
    assert checkout.kind == "invoice" and checkout.external_id is None and checkout.invoice is not None
    inv = checkout.invoice
    assert inv["currency"] == "XTR" and inv["provider_token"] == "" and inv["payload"] == PID
    assert inv["prices"] == [{"label": inv["prices"][0]["label"], "amount": 100}]
    assert 1 <= len(inv["title"]) <= 32 and 1 <= len(inv["description"]) <= 255
    assert len(inv["prices"][0]["label"]) <= 32 and inv["title"].endswith("…")
    assert "100 ⭐" in inv["description"]
    assert http.requests == 0  # the bot sends the invoice; the plugin never calls Telegram


async def test_create_refusals() -> None:
    with pytest.raises(ProviderError) as err:
        await stars(max_stars=50).create(intent(51))
    assert "50 ⭐" in err.value.human
    with pytest.raises(ProviderError):
        await stars().create(intent(17_900, currency="RUB"))
    assert (await stars(max_stars=50).create(intent(50, description=" "))).invoice is not None


def test_classify_payload() -> None:
    assert classify_payload(PID) is PayloadKind.OURS
    assert classify_payload("balance_123_17900") is PayloadKind.LEGACY_BALANCE
    for refused in ("trial_1", "guest_purchase_2", "wheel_spin_3"):
        assert classify_payload(refused) is PayloadKind.REFUSED
    assert classify_payload("") is PayloadKind.UNKNOWN and classify_payload(None) is PayloadKind.UNKNOWN
    assert classify_payload("balance_x_1") is PayloadKind.UNKNOWN


INVOICE = {"payload": PID, "currency": "XTR", "prices": [{"label": "x", "amount": 179}], "provider_token": ""}


@pytest.mark.parametrize(
    ("kw", "expected"),
    [
        ({}, None),
        ({"payment_status": "expired"}, None),  # a late payment still wins (credited to the balance)
        ({"payment_status": "canceled"}, None),
        ({"payment_status": "paid"}, "paid"),
        ({"payment_status": "refunded"}, "closed"),
        ({"payment_status": "mismatch"}, "closed"),
        ({"payment_status": None}, "not_found"),
        ({"invoice": None}, "not_found"),
        ({"total_amount": 178}, "changed"),
        ({"total_amount": True}, "changed"),
        ({"currency": "RUB"}, "changed"),
        ({"payload": "0190f5d2-7b1e-7c3a-9d4e-000000000000"}, "changed"),
        ({"invoice": {**INVOICE, "prices": []}}, "changed"),
        ({"invoice": {**INVOICE, "currency": "RUB"}}, "changed"),
        ({"payload": "balance_1_100"}, "outdated"),
    ],
    ids=[
        "ok",
        "expired",
        "canceled",
        "paid",
        "refunded",
        "mismatch",
        "no-payment",
        "no-invoice",
        "fewer-stars",
        "bool-amount",
        "wrong-currency",
        "other-payload",
        "no-prices",
        "not-xtr",
        "legacy",
    ],
)
def test_pre_checkout_decision(kw: dict[str, Any], expected: str | None) -> None:
    args: dict[str, Any] = {
        "payload": PID,
        "currency": "XTR",
        "total_amount": 179,
        "payment_status": "pending",
        "invoice": INVOICE,
    }
    args.update(kw)
    answer = stars().check_pre_checkout(**args)
    assert answer == (None if expected is None else TEXTS[expected])


async def test_test_credentials_needs_no_keys() -> None:
    probe = await stars("1,79").test_credentials()
    assert probe.ok and "1.79" in probe.message


# ------------------------------------------------------------------------------ through the real core


async def _invoice(harness: Any, stars_count: int = 100) -> str:
    result = await harness.core.create_payment(
        user_id=harness.user_id,
        instance_id=harness.instance.id,
        amount_minor=stars_count,
        currency="XTR",
        description="Пополнение",
    )
    return str(result.payment_id)


async def test_invoice_to_successful_payment(make_harness: HarnessFactory, db: CountingDatabase) -> None:
    harness = await make_harness(TelegramStars, {"rate": "1.79"})
    inst = harness.instance
    plugin = inst.provider
    assert isinstance(plugin, TelegramStars)
    pid = await _invoice(harness)
    row = await payment_row(db, pid)
    invoice = row["checkout"]["invoice"]
    assert row["currency"] == "XTR" and row["amount_minor"] == 100 and row["poll_plan"] is None
    assert invoice["prices"][0]["amount"] == 100
    # pre_checkout_query: the bot reads the payment (1 SQL) and asks the plugin
    decision = plugin.check_pre_checkout(
        payload=pid, currency="XTR", total_amount=100, payment_status=row["status"], invoice=invoice
    )
    assert decision is None
    # successful_payment: the stars as paid
    for _ in range(2):  # Telegram may deliver the update twice
        await harness.core.credit_external(
            inst.id,
            user_id=harness.user_id,
            external_id="charge-1",
            amount_minor=100,
            currency="XTR",
            payment_id=pid,
        )
    assert len(harness.credited) == 1 and await harness.status(pid) == "paid"
    paid = await payment_row(db, pid)
    assert paid["external_id"] == "charge-1" and paid["paid_amount_minor"] == 100
    again = plugin.check_pre_checkout(
        payload=pid, currency="XTR", total_amount=100, payment_status=paid["status"], invoice=invoice
    )
    assert again == TEXTS["paid"]


async def test_wrong_star_count_becomes_mismatch(make_harness: HarnessFactory) -> None:
    harness = await make_harness(TelegramStars, {"rate": "1"})
    pid = await _invoice(harness, 179)
    outcome = await harness.core.credit_external(
        harness.instance.id,
        user_id=harness.user_id,
        external_id="charge-x",
        amount_minor=1,
        currency="XTR",
        payment_id=pid,
    )
    assert outcome.outcome is Outcome.MISMATCH and harness.credited == []
    assert await harness.status(pid) == "mismatch"


async def test_too_many_stars_is_refused(make_harness: HarnessFactory, db: CountingDatabase) -> None:
    harness = await make_harness(TelegramStars, {"rate": "1", "max_stars": 500})
    with pytest.raises(CheckoutError, match="500 ⭐"):
        await _invoice(harness, 501)
    assert [r["status"] for r in await db.raw("select status from payments")] == ["failed"]


async def test_legacy_payload_credits_the_payer_once(
    make_harness: HarnessFactory, db: CountingDatabase
) -> None:
    harness = await make_harness(TelegramStars, {"rate": "1"})
    payer = await add_user(db, 555_123)
    assert classify_payload("balance_17_9900") is PayloadKind.LEGACY_BALANCE
    for _ in range(2):
        await harness.core.credit_external(
            harness.instance.id,
            user_id=payer,
            external_id="legacy-charge",
            amount_minor=99,
            currency="XTR",
            metadata={"legacy_payload": "balance_17_9900"},
        )
    rows = await db.raw("select user_id, currency, amount_minor, metadata from payments")
    assert len(rows) == 1 and rows[0]["user_id"] == payer and rows[0]["amount_minor"] == 99
    assert rows[0]["currency"] == "XTR" and rows[0]["metadata"]["legacy_payload"] == "balance_17_9900"
    assert len(harness.credited) == 1


async def test_frozen_user_gets_no_invoice(make_harness: HarnessFactory, db: CountingDatabase) -> None:
    harness = await make_harness(TelegramStars, {"rate": "1"})

    async def frozen(_conn: Any, _user_id: int) -> str | None:
        return "Подписка заморожена — напишите в поддержку."

    harness.core.spend_guard(frozen)
    with pytest.raises(SpendDeniedError):
        await _invoice(harness)
    assert await db.raw("select id from payments") == []
