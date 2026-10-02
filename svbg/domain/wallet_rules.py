"""Wallet rules (07 §4.5): ledger reasons and signs, shortfall, top-up suggestions, Stars conversion and the
auto-complete decision after a top-up is credited.

Pure functions over integers (minor units of the shop currency); the billing services apply them inside their
transactions.
"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, DecimalException
from typing import Final

from svbg.core.money import MAX_AMOUNT_MINOR, exponent

__all__ = [
    "CREDIT_REASONS",
    "DEBIT_REASONS",
    "LEDGER_REASONS",
    "SIGNED_REASONS",
    "AutocompleteFacts",
    "Decision",
    "StarsQuote",
    "WalletRuleError",
    "check_entry",
    "decide_autocomplete",
    "parse_stars_rate",
    "shortfall",
    "stars_quote",
    "suggest_topup",
]


class WalletRuleError(ValueError):
    """A wallet rule was violated (programming error or forged input)."""


#: Money comes in: a paid top-up, a purchase refunded to the wallet, bonuses, the opening balance of an
#: import, a legacy Stars payment credited to the payer (06 §2.4.4), a paid payment without an order, a
#: staff payment through a test-mode cash desk (``test_topup``: not real money, kept apart in the ledger).
CREDIT_REASONS: Final = frozenset(
    {"topup", "test_topup", "purchase_refund", "bonus", "import_opening", "stars_legacy", "payment_credit"}
)
#: Money goes out: a purchase paid from the wallet, a chargeback / refund taken back from the wallet.
DEBIT_REASONS: Final = frozenset({"purchase", "chargeback"})
#: Either sign (an admin correction, 04 §9.1 ``wallet.adjust``).
SIGNED_REASONS: Final = frozenset({"admin_adjust"})
LEDGER_REASONS: Final = tuple(sorted(CREDIT_REASONS | DEBIT_REASONS | SIGNED_REASONS))


def check_entry(reason: str, amount_minor: int) -> None:
    """Validate one ledger entry: known reason, non-zero int amount with the sign the reason allows."""
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
        raise WalletRuleError("amount_minor must be an int")
    if amount_minor == 0 or abs(amount_minor) > MAX_AMOUNT_MINOR:
        raise WalletRuleError("amount_minor must be non-zero and within limits")
    if reason in CREDIT_REASONS:
        if amount_minor < 0:
            raise WalletRuleError(f"{reason} must be a credit (> 0)")
    elif reason in DEBIT_REASONS:
        if amount_minor > 0:
            raise WalletRuleError(f"{reason} must be a debit (< 0)")
    elif reason not in SIGNED_REASONS:
        raise WalletRuleError(f"unknown ledger reason {reason!r}")


def shortfall(balance_minor: int, price_minor: int) -> int:
    """How much is missing to pay ``price_minor`` from ``balance_minor`` (0 = enough)."""
    if balance_minor < 0 or price_minor < 0:
        raise WalletRuleError("balance and price must be >= 0")
    return max(0, price_minor - balance_minor)


def suggest_topup(
    missing_minor: int, *, min_minor: int | None = None, max_minor: int | None = None
) -> int | None:
    """Default top-up for a shortfall (07 §4.5 step 2): the shortfall rounded **up** to the method's minimum
    (e.g. RollyPay accepts at least 179 ₽ — the surplus stays on the balance). ``None`` when the method cannot
    take that much (above its maximum) or nothing is missing."""
    if missing_minor <= 0:
        return None
    amount = missing_minor if min_minor is None else max(missing_minor, min_minor)
    if max_minor is not None and amount > max_minor:
        return None
    return amount


def parse_stars_rate(value: Decimal | str | int | float, currency: str) -> int:
    """``PAY_STARS_RATE`` (how many units of the shop currency one ⭐ is worth, e.g. ``"1"`` ₽) → minor units
    of
    the shop currency per star. Must be a positive whole number of minor units."""
    try:
        dec = value if isinstance(value, Decimal) else Decimal(str(value).strip().replace(",", "."))
    except DecimalException:
        raise WalletRuleError(f"bad stars rate {value!r}") from None
    if not dec.is_finite() or dec <= 0:
        raise WalletRuleError("stars rate must be positive")
    scaled = dec.scaleb(exponent(currency))
    if scaled != scaled.to_integral_value():
        raise WalletRuleError("stars rate has more decimals than the currency")
    return int(scaled)


@dataclass(frozen=True, slots=True)
class StarsQuote:
    """``stars`` to invoice and the amount (shop currency, minor units) they credit — never less than
    asked."""

    stars: int
    credit_minor: int


def stars_quote(credit_minor: int, rate_minor: int) -> StarsQuote:
    """Stars needed to credit at least ``credit_minor`` at ``rate_minor`` per star (rounded up)."""
    if credit_minor <= 0 or rate_minor <= 0:
        raise WalletRuleError("amount and rate must be positive")
    stars = math.ceil(credit_minor / rate_minor)
    return StarsQuote(stars, stars * rate_minor)


class Decision(enum.StrEnum):
    """What to do with the purchase that waits for a top-up, once the top-up is credited (07 §4.5 step 4)."""

    COMPLETE = "complete"  # debit now, purchase → paid, fulfill
    EXPIRED = "expired"  # the auto-complete window is over: money stays on the balance, purchase → expired
    HELD = "held"  # the user is frozen / banned: purchase → held, «Требует внимания»
    INSUFFICIENT = "insufficient"  # still not enough money: the purchase keeps waiting
    NOT_WAITING = "not_waiting"  # the purchase is no longer waiting (canceled, replaced, paid): balance only


@dataclass(frozen=True, slots=True)
class AutocompleteFacts:
    parent_status: str
    now: datetime
    autocomplete_until: datetime | None
    can_spend: bool
    balance_minor: int
    price_minor: int


def decide_autocomplete(facts: AutocompleteFacts) -> Decision:
    """The rule of 07 §4.5 step 4: complete only a waiting purchase, inside its window, for a user who may
    spend and has enough money. The window is checked before the freeze, so a late payment never «holds»."""
    if facts.parent_status != "awaiting_funds":
        return Decision.NOT_WAITING
    if facts.autocomplete_until is None or facts.now > facts.autocomplete_until:
        return Decision.EXPIRED
    if not facts.can_spend:
        return Decision.HELD
    if facts.balance_minor < facts.price_minor:
        return Decision.INSUFFICIENT
    return Decision.COMPLETE
