from __future__ import annotations

from decimal import ROUND_DOWN, Decimal

import pytest

from svbg.core.money import (
    CURRENCY_EXPONENT,
    MAX_AMOUNT_MINOR,
    exponent,
    format_money,
    from_decimal,
    parse_money,
    to_decimal,
)

NBSP = "\N{NO-BREAK SPACE}"


def test_exponents() -> None:
    assert CURRENCY_EXPONENT["RUB"] == 2
    assert CURRENCY_EXPONENT["USD"] == 2
    assert CURRENCY_EXPONENT["EUR"] == 2
    assert CURRENCY_EXPONENT["USDT"] == 2
    assert CURRENCY_EXPONENT["XTR"] == 0
    assert exponent("rub") == 2
    with pytest.raises(TypeError):
        CURRENCY_EXPONENT["XXX"] = 1  # type: ignore[index]


def test_unknown_currency() -> None:
    with pytest.raises(ValueError, match="unknown currency"):
        exponent("ABC")
    with pytest.raises(ValueError, match="unknown currency"):
        format_money(100, "ABC")
    with pytest.raises(ValueError, match="unknown currency"):
        parse_money("1", "ABC")


@pytest.mark.parametrize(
    ("amount", "currency", "expected"),
    [
        (17900, "RUB", "179 ₽"),
        (169950, "RUB", "1 699,50 ₽"),
        (169905, "RUB", "1 699,05 ₽"),
        (100, "XTR", "100 ⭐"),
        (0, "RUB", "0 ₽"),
        (5, "RUB", "0,05 ₽"),
        (123456789, "RUB", "1 234 567,89 ₽"),
        (100000000, "RUB", "1 000 000 ₽"),
        (-17900, "RUB", "-179 ₽"),
        (1000, "USDT", "10 USDT"),
        (1050, "usd", "10,50 $"),
        (2500, "XTR", "2 500 ⭐"),
    ],
)
def test_format_money_ru(amount: int, currency: str, expected: str) -> None:
    assert format_money(amount, currency) == expected


def test_format_money_is_russian_only_and_nbsp() -> None:
    assert format_money(169950, "USD", "en") == "1 699,50 $"  # an old locale argument is ignored
    assert format_money(169950, "RUB", "en") == "1 699,50 ₽"
    assert format_money(169950, "RUB", nbsp=True) == f"1{NBSP}699,50{NBSP}₽"


@pytest.mark.parametrize("bad", [1.5, "100", None, True])
def test_format_money_requires_int(bad: object) -> None:
    with pytest.raises(TypeError):
        format_money(bad, "RUB")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("text", "currency", "expected"),
    [
        ("179", "RUB", 17900),
        ("179.00", "RUB", 17900),
        ("179,5", "RUB", 17950),
        ("179,05", "RUB", 17905),
        ("  179  ", "RUB", 17900),
        ("1 699,50", "RUB", 169950),
        (f"1{NBSP}699,50{NBSP}₽", "RUB", 169950),
        ("179 ₽", "RUB", 17900),
        ("179₽", "RUB", 17900),
        ("179 RUB", "RUB", 17900),
        ("0", "RUB", 0),
        ("0.5", "RUB", 50),
        ("100", "XTR", 100),
        ("100 ⭐", "XTR", 100),
        ("100.00", "XTR", 100),
        ("179.500", "RUB", 17950),  # trailing zeros beyond the exponent are harmless
        ("007", "RUB", 700),
    ],
)
def test_parse_money_accepts(text: str, currency: str, expected: int) -> None:
    assert parse_money(text, currency) == expected


@pytest.mark.parametrize(
    ("text", "currency"),
    [
        ("", "RUB"),
        ("   ", "RUB"),
        ("-179", "RUB"),
        ("+179", "RUB"),
        ("abc", "RUB"),
        ("179abc", "RUB"),
        ("1e3", "RUB"),
        ("179.", "RUB"),
        (".5", "RUB"),
        ("1.699,50", "RUB"),
        ("1,699.50", "RUB"),
        ("1.2.3", "RUB"),
        ("179.555", "RUB"),
        ("1,699", "RUB"),  # ambiguous thousands separator → too many decimals
        ("100.5", "XTR"),
        ("NaN", "RUB"),
        ("inf", "RUB"),
        ("١٢٣", "RUB"),  # non-ASCII digits
        ("1" * 65, "RUB"),
        ("99999999999999", "RUB"),  # above MAX_AMOUNT_MINOR
        ("179 $", "RUB"),
    ],
)
def test_parse_money_rejects(text: str, currency: str) -> None:
    with pytest.raises(ValueError):
        parse_money(text, currency)


def test_parse_money_rejects_non_str() -> None:
    with pytest.raises(TypeError):
        parse_money(179, "RUB")  # type: ignore[arg-type]


def test_parse_format_roundtrip() -> None:
    for amount in (0, 1, 99, 100, 17950, 169950, 123456789, MAX_AMOUNT_MINOR):
        assert parse_money(format_money(amount, "RUB"), "RUB") == amount
        assert parse_money(format_money(amount, "RUB", nbsp=True), "RUB") == amount


def test_decimal_helpers() -> None:
    assert to_decimal(17950, "RUB") == Decimal("179.50")
    assert to_decimal(100, "XTR") == Decimal(100)
    assert from_decimal(Decimal("179.50"), "RUB") == 17950
    assert from_decimal("179.505", "RUB") == 17951
    assert from_decimal("179.509", "RUB", rounding=ROUND_DOWN) == 17950
    assert from_decimal("-1.5", "RUB") == -150
    for bad in ("abc", "NaN", "Infinity", "1e999999999"):
        with pytest.raises(ValueError):
            from_decimal(bad, "RUB")
