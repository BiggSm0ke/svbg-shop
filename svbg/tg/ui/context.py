"""Per-user context the UI engine works with (no aiogram, no SQL).

:class:`UserCtx` is a small immutable snapshot of what screens and visibility conditions may look at. It is
built by the user loader (LRU-cached in the app) once per update; checks on it never touch the database.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final, Literal

from svbg.core.money import format_money

__all__ = [
    "PLACEHOLDER_NONE",
    "ROLE_RANK",
    "SUB_STATES",
    "USER_ROLES",
    "SubState",
    "UserCtx",
    "role_at_least",
]

USER_ROLES: Final[tuple[str, ...]] = ("user", "support", "admin", "owner")
ROLE_RANK: Final[Mapping[str, int]] = {role: rank for rank, role in enumerate(USER_ROLES)}
SUB_STATES: Final[tuple[str, ...]] = ("none", "trial", "active", "expired", "frozen")

SubState = Literal["none", "trial", "active", "expired", "frozen"]

# Shown in place of a placeholder whose value is unknown (e.g. ``{days_left}`` without a subscription).
PLACEHOLDER_NONE: Final = "—"


def role_at_least(role: str, minimum: str) -> bool:
    """True if ``role`` ranks at or above ``minimum`` (unknown roles rank below ``user``)."""
    return ROLE_RANK.get(role, -1) >= ROLE_RANK[minimum]


@dataclass(frozen=True, slots=True)
class UserCtx:
    """Everything visibility conditions and screens may know about the current user."""

    user_id: int
    telegram_id: int | None = None
    role: str = "user"
    perms: frozenset[str] = frozenset()
    lang: str = "ru"
    sub_state: str = "none"
    days_left: int | None = None
    balance_minor: int = 0
    has_paid: bool = False
    is_new: bool = False
    channel_member: bool | None = None
    ref_count: int = 0
    source: str | None = None
    plan_code: str | None = None
    flags: frozenset[str] = frozenset()
    # Extensions over the stage-0 contract (defaults keep the original constructor valid):
    segments: frozenset[str] = frozenset()  # tags for the ``segment:<tag>`` condition atom
    currency: str = "RUB"  # shop currency used by the ``{balance}`` placeholder
    _placeholders: dict[str, str] | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.role not in ROLE_RANK:
            raise ValueError(f"unknown role {self.role!r}")
        if self.sub_state not in SUB_STATES:
            raise ValueError(f"unknown subscription state {self.sub_state!r}")

    def at_least(self, minimum: str) -> bool:
        return role_at_least(self.role, minimum)

    def has_perm(self, perm: str) -> bool:
        """Owner has every permission; others need it in ``perms`` (``*`` grants all non-owner perms)."""
        if self.role == "owner":
            return True
        if self.role == "user":
            return False
        return perm in self.perms or "*" in self.perms

    def placeholders(self) -> Mapping[str, str]:
        """Values for ``{days_left}`` / ``{balance}`` placeholders (computed once per context)."""
        cached = self._placeholders
        if cached is None:
            cached = {
                "days_left": str(self.days_left) if self.days_left is not None else PLACEHOLDER_NONE,
                "balance": _format_balance(self.balance_minor, self.currency, self.lang),
            }
            object.__setattr__(self, "_placeholders", cached)
        return cached


def _format_balance(amount_minor: int, currency: str, lang: str) -> str:
    try:
        return format_money(amount_minor, currency, lang)
    except (ValueError, KeyError, TypeError):  # unknown currency/locale must never break a screen
        return f"{amount_minor} {currency}"
