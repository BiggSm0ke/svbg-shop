"""Manual transfer plugin: the details screen, receipt rules and the admin confirmation through the core."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import pytest

from svbg.payments.core import CheckoutError, Outcome, PermissionDeniedError
from svbg.payments.providers.manual import (
    DEFAULT_INSTRUCTIONS,
    ManualTransfer,
    format_amount,
    receipt_card,
    receipt_code,
    receipt_problem,
)
from svbg.payments.testkit import CoreHarness, check_plugin, check_static, make_provider
from svbg.sdk import PaymentIntent
from tests.dbkit import CountingDatabase
from tests.payments.providers.conftest import HarnessFactory, add_user, payment_row

PID = "0190f5d2-7b1e-7c3a-9d4e-5f6a7b8c9d0e"
DETAILS = "Карта Т-Банк 2200 7000 0000 0000\nПолучатель: Иван И."
NBSP = "\N{NO-BREAK SPACE}"


def manual(**config: Any) -> ManualTransfer:
    p = make_provider(ManualTransfer, {"details": DETAILS, **config})
    assert isinstance(p, ManualTransfer)
    return p


def test_static_contract() -> None:
    check_static(ManualTransfer)
    caps = ManualTransfer.capabilities
    assert not caps.webhook and not caps.fetch_status and not caps.redirect
    assert [k.value for k in ManualTransfer.manifest.method_kinds] == ["manual"]


async def test_testkit_plugin_level_is_static_only() -> None:
    await check_plugin(ManualTransfer, {"details": DETAILS}, vectors=None)  # type: ignore[arg-type]


async def test_details_checkout() -> None:
    intent = PaymentIntent(
        payment_id=PID, amount_minor=169_950, currency="RUB", description="Пополнение", customer_ref="c"
    )
    checkout = await manual().create(intent)
    assert checkout.kind == "details" and checkout.details is not None and checkout.external_id is None
    text = checkout.details
    assert f"1{NBSP}699,50{NBSP}₽" in text and DETAILS in text and "7B1E" not in text
    assert receipt_code(PID) in text and DEFAULT_INSTRUCTIONS in text
    custom = await manual(instructions="Чек — сюда.").create(intent)
    assert "Чек — сюда." in (custom.details or "") and DEFAULT_INSTRUCTIONS not in (custom.details or "")


def test_details_are_required() -> None:
    from svbg.sdk import ConfigError

    with pytest.raises(ConfigError) as err:
        make_provider(ManualTransfer, {"details": "  "})
    assert "details" in err.value.errors


def test_receipt_code_and_amounts() -> None:
    assert receipt_code(PID) == "7B8C-9D0E"
    assert receipt_code("abc") == "ABC"
    assert format_amount(Decimal("179"), "RUB") == f"179{NBSP}₽"
    assert format_amount(Decimal("1699.5"), "rub") == f"1{NBSP}699,50{NBSP}₽"
    assert format_amount(Decimal("1234567.01"), "USD") == f"1{NBSP}234{NBSP}567,01{NBSP}$"
    assert format_amount(Decimal("5"), "AMD") == f"5{NBSP}AMD"


@pytest.mark.parametrize(
    ("kind", "mime", "size", "ok"),
    [
        ("photo", None, 1_000, True),
        ("document", "application/pdf", 1_000, True),
        ("document", "IMAGE/PNG", 1_000, True),
        ("document", "application/zip", 1_000, False),
        ("document", None, 1_000, False),
        ("photo", None, 11 * 1024 * 1024, False),
        ("sticker", None, 10, False),
    ],
    ids=["photo", "pdf", "png-upper", "zip", "no-mime", "too-big", "sticker"],
)
def test_receipt_rules(kind: str, mime: str | None, size: int, ok: bool) -> None:
    assert (receipt_problem(kind, mime_type=mime, size=size) is None) is ok
    assert manual(max_receipt_mb=5).receipt_rules()["max_mb"] == 5


def test_receipt_card_escapes_user_text() -> None:
    card = receipt_card(
        payment_id=PID,
        amount=Decimal("179"),
        currency="RUB",
        user_label="<b>@evil</b> & co",
        created_text="01.10.2026 15:00 МСК",
    )
    assert "&lt;b&gt;@evil&lt;/b&gt; &amp; co" in card and "<code>7B8C-9D0E</code>" in card
    assert "01.10.2026 15:00 МСК" in card and "сумму из чека" in card


async def test_test_credentials() -> None:
    assert (await manual().test_credentials()).ok


# ------------------------------------------------------------------------------ through the real core


async def _manual_payment(harness: CoreHarness, amount_minor: int = 17_900) -> str:
    result = await harness.core.create_payment(
        user_id=harness.user_id,
        instance_id=harness.instance.id,
        amount_minor=amount_minor,
        currency="RUB",
        description="Пополнение",
    )
    assert result.checkout.kind == "details"
    return result.payment_id


async def test_admin_with_right_confirms_once(make_harness: HarnessFactory, db: CountingDatabase) -> None:
    harness = await make_harness(ManualTransfer, {"details": DETAILS})
    admin_tg, other_tg = 900_001, 900_002
    await add_user(db, admin_tg, "admin", ["payments.confirm"])
    await add_user(db, other_tg, "admin", ["*"])
    pid = await _manual_payment(harness)
    row = await payment_row(db, pid)
    assert row["poll_plan"] is None and row["next_check_at"] is None  # never polled
    first = await harness.core.confirm_manual(
        pid, actor_telegram_id=admin_tg, owner_ids=frozenset(), paid_amount_minor=17_900, reason="чек №1"
    )
    second = await harness.core.confirm_manual(
        pid, actor_telegram_id=other_tg, owner_ids=frozenset(), paid_amount_minor=17_900
    )
    assert first.credited and not second.credited and len(harness.credited) == 1
    assert await harness.status(pid) == "paid"
    audit = await db.raw("select action, amount_minor, reason from admin_audit order by id")
    assert [a["action"] for a in audit] == ["payments.confirm", "payments.confirm"]
    assert audit[0]["amount_minor"] == 17_900 and audit[0]["reason"] == "чек №1"


@pytest.mark.parametrize(
    ("role", "perms", "registered"),
    [("user", [], True), ("admin", ["payments.refund"], True), ("support", ["*"], True), ("user", [], False)],
    ids=["group-member", "admin-without-right", "support", "stranger"],
)
async def test_without_rights_is_refused(
    make_harness: HarnessFactory, db: CountingDatabase, role: str, perms: list[str], registered: bool
) -> None:
    harness = await make_harness(ManualTransfer, {"details": DETAILS})
    if registered:
        await add_user(db, 900_100, role, perms)
    pid = await _manual_payment(harness)
    with pytest.raises(PermissionDeniedError) as err:
        await harness.core.confirm_manual(
            pid, actor_telegram_id=900_100, owner_ids=frozenset(), paid_amount_minor=17_900
        )
    assert err.value.human == "Нет прав"
    with pytest.raises(PermissionDeniedError):
        await harness.core.reject_manual(pid, actor_telegram_id=900_100, owner_ids=frozenset(), reason="нет")
    assert await harness.status(pid) == "pending" and harness.credited == []
    actions = [r["action"] for r in await db.raw("select action from admin_audit order by id")]
    assert actions == ["payments.confirm.denied", "payments.reject.denied"]


async def test_owner_confirms_and_receipt_amount_mismatch(
    make_harness: HarnessFactory, db: CountingDatabase
) -> None:
    harness = await make_harness(ManualTransfer, {"details": DETAILS})
    owner_tg = 900_200
    pid = await _manual_payment(harness)
    result = await harness.core.confirm_manual(
        pid, actor_telegram_id=owner_tg, owner_ids={owner_tg}, paid_amount_minor=17_000
    )
    assert result.outcome is Outcome.MISMATCH and harness.credited == []
    row = await payment_row(db, pid)
    assert row["status"] == "mismatch" and row["paid_amount_minor"] == 17_000


async def test_reject_then_late_confirmation_wins(make_harness: HarnessFactory, db: CountingDatabase) -> None:
    harness = await make_harness(ManualTransfer, {"details": DETAILS})
    admin_tg = 900_300
    await add_user(db, admin_tg, "admin", ["payments.confirm"])
    pid = await _manual_payment(harness)
    with pytest.raises(ValueError):
        await harness.core.reject_manual(pid, actor_telegram_id=admin_tg, owner_ids=frozenset(), reason=" ")
    assert await harness.core.reject_manual(
        pid, actor_telegram_id=admin_tg, owner_ids=frozenset(), reason="чек не найден в выписке"
    )
    assert await harness.status(pid) == "canceled"
    late = await harness.core.confirm_manual(
        pid, actor_telegram_id=admin_tg, owner_ids=frozenset(), paid_amount_minor=17_900, reason="нашёлся"
    )
    assert late.credited and await harness.status(pid) == "paid"


async def test_concurrent_confirmations_credit_once(
    make_harness: HarnessFactory, db: CountingDatabase
) -> None:
    harness = await make_harness(ManualTransfer, {"details": DETAILS})
    admins = [900_400 + i for i in range(4)]
    for tg in admins:
        await add_user(db, tg, "admin", ["payments.confirm"])
    pid = await _manual_payment(harness)
    results = await asyncio.gather(
        *(
            harness.core.confirm_manual(
                pid, actor_telegram_id=tg, owner_ids=frozenset(), paid_amount_minor=17_900
            )
            for tg in admins
        )
    )
    assert sum(r.credited for r in results) == 1 and len(harness.credited) == 1


async def test_only_manual_payments_can_be_confirmed(
    make_harness: HarnessFactory, db: CountingDatabase
) -> None:
    from svbg.payments.providers.stars import TelegramStars

    harness = await make_harness(TelegramStars, {"rate": "1"})
    owner_tg = 900_500
    result = await harness.core.create_payment(
        user_id=harness.user_id,
        instance_id=harness.instance.id,
        amount_minor=179,
        currency="XTR",
        description="x",
    )
    with pytest.raises(CheckoutError):
        await harness.core.confirm_manual(
            result.payment_id, actor_telegram_id=owner_tg, owner_ids={owner_tg}, paid_amount_minor=179
        )
