"""Money as integer minor units + ISO-like currency code.

``XTR`` (Telegram Stars) has no fractional part. All arithmetic stays in ``int``; :class:`~decimal.Decimal`
is only used at the edges (provider APIs) via :func:`to_decimal` / :func:`from_decimal`.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, DecimalException
from types import MappingProxyType
from typing import Final

__all__ = [
    "CURRENCY_EXPONENT",
    "CURRENCY_SYMBOL",
    "MAX_AMOUNT_MINOR",
    "exponent",
    "format_money",
    "from_decimal",
    "parse_money",
    "to_decimal",
]

CURRENCY_EXPONENT: Final = MappingProxyType(
    {
        "RUB": 2,
        "USD": 2,
        "EUR": 2,
        "GBP": 2,
        "KZT": 2,
        "UAH": 2,
        "BYN": 2,
        "UZS": 2,
        "KGS": 2,
        "AMD": 2,
        "GEL": 2,
        "AZN": 2,
        "TRY": 2,
        "CNY": 2,
        "XTR": 0,
        "USDT": 2,
        "USDC": 2,
    }
)

CURRENCY_SYMBOL: Final = MappingProxyType(
    {
        "RUB": "₽",
        "USD": "$",
        "EUR": "€",
        "GBP": "£",
        "KZT": "₸",
        "UAH": "₴",
        "BYN": "Br",
        "AMD": "֏",
        "GEL": "₾",
        "AZN": "₼",
        "TRY": "₺",
        "CNY": "¥",
        "XTR": "⭐",
    }
)

# Symbols written before the number in English formatting ("$1,699.50").
_PREFIX_SYMBOLS_EN: Final = frozenset({"USD", "EUR", "GBP", "CNY"})

# Upper bound for parsed/formatted amounts: far below BIGINT, far above any real payment.
MAX_AMOUNT_MINOR: Final = 10**15

_MAX_INPUT_LEN: Final = 64
_SPACES: Final = str.maketrans("", "", " \u00a0\u202f\u2009\u2007\ufe0f_'")
_NUMBER_RE: Final = re.compile(r"(?P<int>[0-9]+)(?:(?P<sep>[.,])(?P<frac>[0-9]+))?")


def exponent(currency: str) -> int:
    """Number of minor-unit digits for ``currency``; ``ValueError`` for unknown codes."""
    try:
        return CURRENCY_EXPONENT[currency.upper()]
    except (KeyError, AttributeError):
        raise ValueError(f"unknown currency: {currency!r}") from None


def _group(digits: str, sep: str) -> str:
    head = len(digits) % 3 or 3
    parts = [digits[:head]] + [digits[i : i + 3] for i in range(head, len(digits), 3)]
    return sep.join(parts)


def format_money(amount_minor: int, currency: str, locale: str = "ru", *, nbsp: bool = False) -> str:
    """Human-readable amount: ``179 ₽``, ``1 699,50 ₽``, ``100 ⭐`` (ru); ``$1,699.50`` (en).

    Whole amounts are shown without a fractional part. ``nbsp=True`` uses non-breaking spaces so that
    Telegram never wraps the symbol away from the number.
    """
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
        raise TypeError("amount_minor must be int")
    code = currency.upper()
    exp = exponent(code)
    space = "\N{NO-BREAK SPACE}" if nbsp else " "
    english = locale.lower().startswith("en")

    sign = "-" if amount_minor < 0 else ""
    units, frac = divmod(abs(amount_minor), 10**exp) if exp else (abs(amount_minor), 0)
    if english:
        number = _group(str(units), ",")
        if frac:
            number += "." + str(frac).rjust(exp, "0")
    else:
        number = _group(str(units), space)
        if frac:
            number += "," + str(frac).rjust(exp, "0")

    symbol = CURRENCY_SYMBOL.get(code, code)
    if english and code in _PREFIX_SYMBOLS_EN:
        return f"{sign}{symbol}{number}"
    return f"{sign}{number}{space}{symbol}"


def _strip_currency(text: str, code: str) -> str:
    symbol = CURRENCY_SYMBOL.get(code)
    for marker in (symbol, code, code.lower()):
        if not marker:
            continue
        if text.endswith(marker):
            return text[: -len(marker)]
        if text.startswith(marker):
            return text[len(marker) :]
    return text


def parse_money(text: str, currency: str) -> int:
    """Parse user input into minor units.

    Accepts ``"179"``, ``"179.00"``, ``"179,5"``, ``"1 699,50"``, optionally with the currency symbol/code.
    Rejects negatives, signs, exponents, thousand separators mixed with decimals, more fractional digits
    than the currency has (unless they are zeros), empty and oversized input → ``ValueError``.
    """
    if not isinstance(text, str):
        raise TypeError("text must be str")
    code = currency.upper()
    exp = exponent(code)
    if len(text) > _MAX_INPUT_LEN:
        raise ValueError("amount text is too long")

    cleaned = _strip_currency(text.strip().translate(_SPACES), code)
    m = _NUMBER_RE.fullmatch(cleaned)
    if m is None:
        raise ValueError(f"not a valid amount: {text!r}")

    frac = (m.group("frac") or "").rstrip("0") if m.group("sep") else ""
    if len(frac) > exp:
        raise ValueError(f"too many decimal places for {code} (max {exp})")
    amount = int(m.group("int")) * 10**exp + (int(frac.ljust(exp, "0")) if frac else 0)
    if amount > MAX_AMOUNT_MINOR:
        raise ValueError("amount is too large")
    return amount


def to_decimal(amount_minor: int, currency: str) -> Decimal:
    """Minor units → major-unit ``Decimal`` (e.g. ``17950, "RUB"`` → ``Decimal("179.50")``)."""
    exp = exponent(currency)
    return Decimal(amount_minor).scaleb(-exp)


def from_decimal(value: Decimal | str, currency: str, *, rounding: str = ROUND_HALF_UP) -> int:
    """Major-unit decimal (from a provider API) → minor units, rounded with ``rounding``."""
    exp = exponent(currency)
    try:
        dec = value if isinstance(value, Decimal) else Decimal(value)
        if not dec.is_finite():
            raise ValueError("amount must be finite")
        return int(dec.scaleb(exp).quantize(Decimal(1), rounding=rounding))
    except DecimalException:
        raise ValueError(f"not a decimal amount: {value!r}") from None
