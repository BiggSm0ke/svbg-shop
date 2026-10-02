"""Pure promo rules: definitions, eligibility, the checkout discount, Bedolaga mapping."""

from __future__ import annotations

import itertools
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from svbg.domain.pricing import apply_discounts
from svbg.promo.legacy import from_bedolaga
from svbg.promo.rules import (
    CODE_RE,
    KINDS,
    REFUSALS,
    Facts,
    Promo,
    PromoDiscount,
    PromoError,
    check_code,
    describe,
    discount_of,
    generate_code,
    normalize_input,
    pending_until,
    plural_days,
    promo_ids_of,
    refusal,
    validate,
    validate_limits,
)

AT = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


def promo(kind: str = "wallet", **kw: object) -> Promo:
    base: dict[str, object] = {
        "id": 1,
        "code": "GIFT",
        "kind": kind,
        "currency": "RUB",
        "amount_minor": 10_000,
    }
    base.update(kw)
    return Promo(**base)  # type: ignore[arg-type]


def money(amount: int, cur: str) -> str:
    return f"{amount // 100} {cur}"


# ------------------------------------------------------------------------------------------- definitions


@pytest.mark.parametrize(
    ("kind", "values", "expected"),
    [
        ("days", {"days": 7, "percent": 50}, {"days": 7, "percent": None}),
        ("percent", {"percent": 20}, {"percent": 20, "pending_hours": 72, "plan_ids": []}),
        ("percent", {"percent": 20, "plan_ids": [2, 1, 2]}, {"plan_ids": [1, 2]}),
        (
            "fixed",
            {"amount_minor": 5000, "currency": "RUB", "min_amount_minor": 10_000},
            {"amount_minor": 5000},
        ),
        ("wallet", {"amount_minor": 1, "currency": "RUB", "days": 3}, {"amount_minor": 1, "days": None}),
        ("trial_extend", {"days": 3}, {"days": 3}),
        ("plan_gift", {"days": 30, "plan_id": 2}, {"plan_id": 2}),
        (
            "wallet_days",
            {"amount_minor": 100, "currency": "RUB", "days": 2},
            {"days": 2, "amount_minor": 100},
        ),
    ],
)
def test_validate_keeps_only_the_columns_of_the_kind(kind: str, values: dict, expected: dict) -> None:
    out = validate(kind, values)
    for key, value in expected.items():
        assert out[key] == value
    assert set(out) >= {"days", "amount_minor", "currency", "percent", "plan_id", "plan_ids"}


@pytest.mark.parametrize(
    ("kind", "values", "message"),
    [
        ("nope", {}, "Неизвестный вид"),
        ("days", {"days": 0}, "Дней"),
        ("days", {"days": True}, "Дней"),
        ("days", {"days": "7"}, "Дней"),
        ("trial_extend", {"days": 400}, "Дней"),
        ("percent", {"percent": 101}, "Скидка"),
        ("percent", {"percent": 0}, "Скидка"),
        ("fixed", {"amount_minor": 0, "currency": "RUB"}, "Сумма"),
        ("wallet", {"amount_minor": 100}, "валюта"),
        ("wallet", {"amount_minor": 100, "currency": "rub"}, "валюта"),
        ("plan_gift", {"days": 30}, "Тариф"),
        ("percent", {"percent": 5, "plan_ids": "1,2"}, "список"),
        ("percent", {"percent": 5, "pending_hours": 0}, "Часов"),
    ],
)
def test_validate_refuses_bad_definitions(kind: str, values: dict, message: str) -> None:
    with pytest.raises(PromoError, match=message):
        validate(kind, values)


def test_limits_and_codes() -> None:
    validate_limits({"max_uses": 5, "starts_at": AT, "expires_at": AT + timedelta(days=1)})
    with pytest.raises(PromoError):
        validate_limits({"max_uses": 0})
    with pytest.raises(PromoError, match="раньше"):
        validate_limits({"starts_at": AT, "expires_at": AT})
    assert check_code(" AUTUMN-20 ") == "AUTUMN-20"
    for bad in ("ab", "with space", "кириллица", "x" * 49, None, 5):
        with pytest.raises(PromoError):
            check_code(bad)
    assert check_code("Старый-Код", legacy=True) == "Старый-Код"
    with pytest.raises(PromoError):
        check_code("a b", legacy=True)
    for _ in range(50):
        code = generate_code()
        assert CODE_RE.fullmatch(code) and not set(code) & set("01IO")
    assert normalize_input("  Promo ") == "Promo"
    assert normalize_input("two words") is None
    assert normalize_input("x" * 65) is None
    assert normalize_input(None) is None
    assert normalize_input("bad\x00") is None


def test_plural_days() -> None:
    assert [plural_days(n) for n in (1, 2, 5, 11, 21, 22, 112)] == [
        "1 день",
        "2 дня",
        "5 дней",
        "11 дней",
        "21 день",
        "22 дня",
        "112 дней",
    ]


def test_describe_every_kind() -> None:
    lines = {
        k: describe(
            promo(k, days=7, percent=20, plan_id=2, amount_minor=10_000),
            money=money,
            plan_title=lambda pid: f"Тариф {pid}",
        )
        for k in KINDS
    }
    assert lines == {
        "days": "+7 дней к подписке",
        "percent": "−20 % на покупку",
        "fixed": "−100 RUB на покупку",
        "wallet": "+100 RUB на баланс",
        "trial_extend": "пробный период +7 дней",
        "plan_gift": "тариф «Тариф 2» на 7 дней",
        "wallet_days": "+100 RUB на баланс и +7 дней",
    }


# ------------------------------------------------------------------------------------------- eligibility


def test_refusal_order_and_kinds() -> None:
    ok = Facts(live_sub=True)
    assert refusal(promo(), ok, AT, currency="RUB") is None
    assert refusal(promo(enabled=False), ok, AT, currency="RUB") == "inactive"
    assert refusal(promo(starts_at=AT + timedelta(seconds=1)), ok, AT, currency="RUB") == "not_started"
    assert refusal(promo(expires_at=AT), ok, AT, currency="RUB") == "expired"
    assert refusal(promo(max_uses=3, uses=3), ok, AT, currency="RUB") == "exhausted"
    assert refusal(promo(), replace(ok, banned=True), AT, currency="RUB") == "banned"
    assert refusal(promo(), replace(ok, used=True), AT, currency="RUB") == "used"
    assert refusal(promo(once_per_user=False), replace(ok, used=True), AT, currency="RUB") is None
    assert refusal(promo(new_users_only=True), replace(ok, has_paid=True), AT, currency="RUB") == "not_new"
    assert refusal(promo(), ok, AT, currency="USD") == "currency"
    assert refusal(promo("days", days=7), Facts(), AT, currency="RUB") == "no_sub"
    assert refusal(promo("wallet_days", days=7), Facts(), AT, currency="RUB") == "no_sub"
    trial = promo("trial_extend", days=3, amount_minor=None)
    assert refusal(trial, Facts(live_sub=True, live_trial=True), AT, currency="RUB") is None
    assert refusal(trial, Facts(live_sub=True), AT, currency="RUB") == "has_paid_sub"
    assert refusal(trial, Facts(trial_used=True), AT, currency="RUB") == "trial_used"
    assert refusal(trial, Facts(), AT, currency="RUB") is None
    gift = promo("plan_gift", days=30, plan_id=2, amount_minor=None)
    assert refusal(gift, Facts(), AT, currency="RUB") is None
    assert refusal(gift, Facts(live_sub=True, live_plan_id=1), AT, currency="RUB") == "plan_conflict"
    assert refusal(gift, Facts(live_sub=True, live_plan_id=2), AT, currency="RUB") is None
    assert refusal(gift, Facts(live_sub=True, live_trial=True, live_plan_id=9), AT, currency="RUB") is None
    for key in ("inactive", "not_started", "expired", "exhausted", "used", "no_sub", "too_many"):
        assert REFUSALS[key]


@pytest.mark.parametrize(
    ("enabled", "used", "has_paid", "max_uses", "uses"),
    list(itertools.product((True, False), (True, False), (True, False), (None, 1), (0, 1))),
)
def test_refusal_is_none_only_when_every_limit_passes(
    enabled: bool, used: bool, has_paid: bool, max_uses: int | None, uses: int
) -> None:
    p = promo(enabled=enabled, max_uses=max_uses, uses=uses, new_users_only=True)
    reason = refusal(p, Facts(used=used, has_paid=has_paid, live_sub=True), AT, currency="RUB")
    allowed = enabled and not used and not has_paid and not (max_uses is not None and uses >= max_uses)
    assert (reason is None) == allowed


def test_pending_until_is_capped_by_the_promo_expiry() -> None:
    p = promo("percent", percent=10, pending_hours=48, amount_minor=None)
    assert pending_until(p, AT) == AT + timedelta(hours=48)
    assert pending_until(replace(p, pending_hours=None), AT) == AT + timedelta(hours=72)
    assert pending_until(replace(p, expires_at=AT + timedelta(hours=5)), AT) == AT + timedelta(hours=5)


def test_status_icons() -> None:
    assert promo().status(AT) == "on"
    assert promo(enabled=False).status(AT) == "off"
    assert promo(expires_at=AT).status(AT) == "expired"
    assert promo(max_uses=1, uses=1).status(AT) == "exhausted"
    assert promo(starts_at=AT + timedelta(days=1)).status(AT) == "scheduled"
    assert promo(code="ok_code").linkable and not promo(code="Старый").linkable


# ------------------------------------------------------------------------------------------- discount


def test_discount_math() -> None:
    pct = discount_of(promo("percent", percent=20, amount_minor=None), 1)
    assert isinstance(pct, PromoDiscount)
    assert pct.source == "promo:1" and pct.label == "Промокод GIFT −20 %"
    assert apply_discounts(17_900, [pct])[1] == 14_320
    assert pct.amount_off(0) == 0
    fixed = PromoDiscount(2, "FIX", amount_minor=50_000)
    assert apply_discounts(17_900, [fixed])[1] == 0  # never below zero
    floor = PromoDiscount(3, "MIN", percent=50, min_amount_minor=20_000)
    assert floor.amount_off(17_900) == 0 and floor.amount_off(49_900) == 24_950
    only2 = promo("percent", percent=10, plan_ids=(2,), amount_minor=None)
    assert discount_of(only2, 1) is None and discount_of(only2, 2) is not None
    assert discount_of(promo("wallet"), 1) is None


@pytest.mark.parametrize("subtotal", [0, 1, 99, 17_900, 10**12])
@pytest.mark.parametrize("percent", [1, 33, 99, 100])
def test_percent_never_exceeds_the_subtotal(subtotal: int, percent: int) -> None:
    off = PromoDiscount(1, "X", percent=percent).amount_off(subtotal)
    assert 0 <= off <= subtotal
    assert off == subtotal * percent // 100


def test_promo_ids_of_a_snapshot() -> None:
    snap = {
        "discounts": [
            {"source": "promo:5", "amount_minor": 10},
            {"source": "personal"},
            {"source": "promo:x"},
            "garbage",
            {"source": "promo:5"},
        ]
    }
    assert promo_ids_of(snap) == [5]
    assert promo_ids_of(None) == [] and promo_ids_of({}) == []


# ------------------------------------------------------------------------------------------- bedolaga


@pytest.mark.parametrize(
    ("row", "kind", "values"),
    [
        ({"type": "balance", "balance_bonus_kopeks": 10_000}, "wallet", {"amount_minor": 10_000}),
        ({"type": "subscription_days", "subscription_days": 7}, "days", {"days": 7}),
        ({"type": "trial_subscription", "subscription_days": 3}, "trial_extend", {"days": 3}),
        (
            {"type": "discount", "balance_bonus_kopeks": 20, "subscription_days": 48, "tariff_id": 7},
            "percent",
            {"percent": 20, "pending_hours": 48, "plan_ids": [70]},
        ),
        (
            {"type": "balance_and_days", "balance_bonus_kopeks": 5000, "subscription_days": 2},
            "wallet_days",
            {"amount_minor": 5000, "days": 2},
        ),
    ],
)
def test_bedolaga_types(row: dict, kind: str, values: dict) -> None:
    base = {
        "id": 11,
        "code": "Bedo-Code",
        "max_uses": 0,
        "is_active": True,
        "valid_until": "2027-01-01T00:00:00",
    }
    got = from_bedolaga({**base, **row}, plan_of=lambda t: t * 10)
    assert got.kind == kind and got.code == "Bedo-Code" and got.legacy_id == "11"
    for key, value in values.items():
        assert got.values[key] == value
    assert got.limits["max_uses"] is None and got.limits["enabled"] is True
    assert got.limits["expires_at"] == datetime(2027, 1, 1, tzinfo=UTC)


def test_bedolaga_rejects_promo_groups_and_bad_rows() -> None:
    with pytest.raises(PromoError, match="не переносится"):
        from_bedolaga({"code": "G", "type": "promo_group"})
    with pytest.raises(PromoError):
        from_bedolaga({"code": "with space", "type": "balance", "balance_bonus_kopeks": 1})
    with pytest.raises(PromoError):
        from_bedolaga({"code": "ZERO", "type": "balance", "balance_bonus_kopeks": 0})
    got = from_bedolaga(
        {
            "code": "LIM",
            "type": "balance",
            "balance_bonus_kopeks": 1,
            "max_uses": 5,
            "first_purchase_only": True,
        }
    )
    assert got.limits["max_uses"] == 5 and got.limits["new_users_only"] is True
