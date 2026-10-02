"""Billing settings, read from the settings snapshot on every use (all ⚡ hot)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from svbg.core.money import exponent
from svbg.domain.wallet_rules import WalletRuleError, parse_stars_rate

__all__ = ["DEFAULTS", "BillingConfig", "ConfigSource"]

ConfigSource = Callable[[], Mapping[str, Any]]

#: Keys billing reads and their defaults (the registry declares them; see the stage 2 report).
DEFAULTS: Final[Mapping[str, Any]] = {
    "CURRENCY": "RUB",
    "WALLET_AUTOCOMPLETE_MINUTES": 60,
    "WALLET_TOPUP_MIN": 10,  # major units of CURRENCY
    "WALLET_TOPUP_MAX": 100_000,
    "PAY_STARS_RATE": "1",  # CURRENCY per ⭐
    "TIMEZONE": "Europe/Moscow",
    "DEFAULT_LANGUAGE": "ru",  # notices for a user without a stored language
}


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True, slots=True)
class BillingConfig:
    currency: str
    autocomplete_minutes: int
    topup_min_minor: int
    topup_max_minor: int
    stars_rate_minor: int | None  # None: misconfigured rate (Stars top-ups refused)
    timezone: str
    default_lang: str = "ru"

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> BillingConfig:
        def get(key: str) -> Any:
            value = raw.get(key)
            return DEFAULTS[key] if value is None else value

        currency = str(get("CURRENCY")).upper()
        try:
            scale = 10 ** exponent(currency)
        except ValueError:
            currency, scale = "RUB", 100
        minutes = min(1440, max(5, _int(get("WALLET_AUTOCOMPLETE_MINUTES"), 60)))
        lo = max(1, _int(get("WALLET_TOPUP_MIN"), 10)) * scale
        hi = max(lo, _int(get("WALLET_TOPUP_MAX"), 100_000) * scale)
        try:
            rate: int | None = parse_stars_rate(get("PAY_STARS_RATE"), currency)
        except (WalletRuleError, ValueError):
            rate = None
        lang = str(get("DEFAULT_LANGUAGE")).strip().lower()[:2]
        return cls(
            currency, minutes, lo, hi, rate, str(get("TIMEZONE")), lang if lang in ("ru", "en") else "ru"
        )
