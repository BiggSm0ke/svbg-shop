from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from svbg.domain.wallet_rules import (
    CREDIT_REASONS,
    DEBIT_REASONS,
    LEDGER_REASONS,
    AutocompleteFacts,
    Decision,
    WalletRuleError,
    check_entry,
    decide_autocomplete,
    parse_stars_rate,
    shortfall,
    stars_quote,
    suggest_topup,
)

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def test_reason_sets_are_disjoint_and_listed() -> None:
    assert not CREDIT_REASONS & DEBIT_REASONS
    assert set(LEDGER_REASONS) == CREDIT_REASONS | DEBIT_REASONS | {"admin_adjust"}


@pytest.mark.parametrize("reason", sorted(CREDIT_REASONS))
def test_credit_reasons_take_positive_amounts(reason: str) -> None:
    check_entry(reason, 1)
    with pytest.raises(WalletRuleError):
        check_entry(reason, -1)


@pytest.mark.parametrize("reason", sorted(DEBIT_REASONS))
def test_debit_reasons_take_negative_amounts(reason: str) -> None:
    check_entry(reason, -1)
    with pytest.raises(WalletRuleError):
        check_entry(reason, 1)


@pytest.mark.parametrize(
    "bad", [0, True, 1.5, 10**16, -(10**16)], ids=["zero", "bool", "float", "huge", "-huge"]
)
def test_entry_amount_must_be_a_sane_int(bad: object) -> None:
    with pytest.raises(WalletRuleError):
        check_entry("admin_adjust", bad)  # type: ignore[arg-type]


def test_unknown_reason_is_refused_and_adjust_takes_both_signs() -> None:
    check_entry("admin_adjust", 5)
    check_entry("admin_adjust", -5)
    with pytest.raises(WalletRuleError):
        check_entry("gift", 5)


def test_shortfall() -> None:
    assert shortfall(0, 17_900) == 17_900
    assert shortfall(5_000, 17_900) == 12_900
    assert shortfall(17_900, 17_900) == 0
    assert shortfall(20_000, 17_900) == 0
    with pytest.raises(WalletRuleError):
        shortfall(-1, 10)


def test_suggest_topup_rounds_up_to_the_method_minimum() -> None:
    assert suggest_topup(5_000, min_minor=17_900) == 17_900  # RollyPay: at least 179 ₽, surplus stays
    assert suggest_topup(20_000, min_minor=17_900) == 20_000
    assert suggest_topup(5_000) == 5_000
    assert suggest_topup(0, min_minor=100) is None
    assert suggest_topup(50_000, max_minor=40_000) is None
    assert suggest_topup(100, min_minor=200, max_minor=150) is None


@pytest.mark.parametrize(
    ("value", "minor"),
    [("1", 100), ("1.5", 150), ("1,25", 125), (Decimal("2"), 200), (3, 300)],
    ids=["one", "one-half", "comma", "decimal", "int"],
)
def test_stars_rate(value: object, minor: int) -> None:
    assert parse_stars_rate(value, "RUB") == minor  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "value", ["0", "-1", "abc", "1.001", "NaN"], ids=["zero", "neg", "text", "frac", "nan"]
)
def test_bad_stars_rate(value: str) -> None:
    with pytest.raises(WalletRuleError):
        parse_stars_rate(value, "RUB")


def test_stars_quote_never_credits_less_than_asked() -> None:
    assert stars_quote(17_900, 100).stars == 179
    q = stars_quote(17_950, 100)
    assert (q.stars, q.credit_minor) == (180, 18_000)
    rng = random.Random(4242)
    for _ in range(500):
        credit, rate = rng.randint(1, 10**7), rng.randint(1, 10**4)
        q = stars_quote(credit, rate)
        assert q.credit_minor >= credit
        assert q.credit_minor - credit < rate
        assert q.credit_minor == q.stars * rate


def _facts(**kw: object) -> AutocompleteFacts:
    base: dict[str, object] = {
        "parent_status": "awaiting_funds",
        "now": NOW,
        "autocomplete_until": NOW + timedelta(minutes=10),
        "can_spend": True,
        "balance_minor": 20_000,
        "price_minor": 17_900,
    }
    base.update(kw)
    return AutocompleteFacts(**base)  # type: ignore[arg-type]


def test_autocomplete_completes_only_a_waiting_purchase_in_time_with_money() -> None:
    assert decide_autocomplete(_facts()) is Decision.COMPLETE
    assert decide_autocomplete(_facts(balance_minor=17_900)) is Decision.COMPLETE
    assert decide_autocomplete(_facts(autocomplete_until=NOW)) is Decision.COMPLETE  # inclusive edge


@pytest.mark.parametrize(
    ("kw", "decision"),
    [
        ({"parent_status": "canceled"}, Decision.NOT_WAITING),
        ({"parent_status": "paid"}, Decision.NOT_WAITING),
        ({"autocomplete_until": NOW - timedelta(seconds=1)}, Decision.EXPIRED),
        ({"autocomplete_until": None}, Decision.EXPIRED),
        ({"can_spend": False}, Decision.HELD),
        ({"balance_minor": 17_899}, Decision.INSUFFICIENT),
        # a late payment of a frozen user is a late payment (money on the balance), not a hold
        ({"can_spend": False, "autocomplete_until": NOW - timedelta(hours=2)}, Decision.EXPIRED),
    ],
    ids=["canceled", "paid", "late", "no-window", "frozen", "short", "late-frozen"],
)
def test_autocomplete_refusals(kw: dict[str, object], decision: Decision) -> None:
    assert decide_autocomplete(_facts(**kw)) is decision
