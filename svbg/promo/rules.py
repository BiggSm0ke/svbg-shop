"""Promo rules — pure (no SQL, no Telegram): kinds, definition checks, eligibility, the checkout discount.

Kinds (01 §1.4, 06 §2.6):

=================  ===========================================================================================
``days``           +N days to the live subscription
``percent``        −P % on one purchase (a pending discount applied at checkout)
``fixed``          −X on one purchase (a pending discount applied at checkout)
``wallet``         +X to the balance
``trial_extend``   +N days to a trial; a user who never had a subscription gets an N-day trial
                   (Bedolaga ``trial_subscription``)
``plan_gift``      the plan P for N days (bought for free: new / renewal / trial conversion)
``wallet_days``    +X to the balance and +N days (Bedolaga ``balance_and_days``)
=================  ===========================================================================================

Limits: total uses, once per user, start / expiry, new users only (never paid), and for discounts the minimal
order subtotal and the allowed plans.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from svbg.core.money import MAX_AMOUNT_MINOR

__all__ = [
    "CODE_RE",
    "DEFAULT_PENDING_HOURS",
    "DISCOUNT_KINDS",
    "KINDS",
    "KIND_FIELDS",
    "KIND_TITLES",
    "MAX_DAYS",
    "MAX_USES",
    "REFUSALS",
    "REFUSALS_EN",
    "VALUE_KINDS",
    "Facts",
    "Promo",
    "PromoDiscount",
    "PromoError",
    "check_code",
    "describe",
    "discount_of",
    "generate_code",
    "normalize_input",
    "pending_until",
    "plural_days",
    "promo_ids_of",
    "refusal",
    "validate",
    "validate_limits",
]

KINDS: Final = ("days", "percent", "fixed", "wallet", "trial_extend", "plan_gift", "wallet_days")
DISCOUNT_KINDS: Final = frozenset({"percent", "fixed"})
#: Kinds that give money or paid time right away: an admin's code is held to ``ADMIN_WALLET_ADJUST_MAX`` /
#: ``ADMIN_GRANT_DAYS_MAX`` per use and needs a reason (04 §9.1).
VALUE_KINDS: Final = frozenset({"days", "wallet", "trial_extend", "plan_gift", "wallet_days"})
KIND_TITLES: Final[Mapping[str, str]] = {
    "days": "📅 Дни к подписке",
    "percent": "🏷 Скидка %",
    "fixed": "💸 Скидка суммой",
    "wallet": "💰 На баланс",
    "trial_extend": "🎁 Пробный период",
    "plan_gift": "🎀 Тариф в подарок",
    "wallet_days": "💰+📅 Баланс и дни",
}
#: Which value columns a kind uses; the rest are stored as NULL.
KIND_FIELDS: Final[Mapping[str, frozenset[str]]] = {
    "days": frozenset({"days"}),
    "percent": frozenset({"percent", "pending_hours", "min_amount_minor", "plan_ids"}),
    "fixed": frozenset({"amount_minor", "currency", "pending_hours", "min_amount_minor", "plan_ids"}),
    "wallet": frozenset({"amount_minor", "currency"}),
    "trial_extend": frozenset({"days"}),
    "plan_gift": frozenset({"days", "plan_id"}),
    "wallet_days": frozenset({"amount_minor", "currency", "days"}),
}
_VALUE_FIELDS: Final = (
    "days",
    "amount_minor",
    "currency",
    "percent",
    "plan_id",
    "pending_hours",
    "min_amount_minor",
    "plan_ids",
)

MAX_DAYS: Final = 3650
MAX_USES: Final = 10_000_000
MAX_PENDING_HOURS: Final = 87_600
DEFAULT_PENDING_HOURS: Final = 72
#: Codes created in the bot: deep-linkable as ``pr_<code>`` (≤ 64 characters ``[A-Za-z0-9_-]``).
CODE_RE: Final = re.compile(r"^[A-Za-z0-9_-]{3,48}$")
#: Anything a user may type (imported Bedolaga codes keep their own spelling): no spaces / control chars.
_INPUT_RE: Final = re.compile(r"^[^\s\x00-\x1f\x7f]{1,64}$")
_GEN_ALPHABET: Final = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I

REFUSALS: Final[Mapping[str, str]] = {
    "not_found": "Такого промокода нет. Проверьте написание.",
    "inactive": "Этот промокод больше не действует.",
    "not_started": "Промокод ещё не начал действовать.",
    "expired": "Срок действия промокода закончился.",
    "exhausted": "Промокод уже использовали максимальное число раз.",
    "used": "Вы уже использовали этот промокод.",
    "not_new": "Промокод только для новых покупателей.",
    "banned": "Аккаунт заблокирован.",
    "currency": "Промокод сейчас не работает. Напишите в поддержку.",
    "no_sub": "Промокод добавляет дни к подписке — сначала оформите её.",
    "has_paid_sub": "Промокод на пробный период — у вас уже есть оплаченная подписка.",
    "trial_used": "Пробный период уже использован.",
    "plan_conflict": "У вас другая оплаченная подписка — подарок на этот тариф недоступен.",
    "plan_missing": "Тариф промокода больше недоступен. Напишите в поддержку.",
    "plan_not_allowed": "Промокод не действует на этот тариф.",
    "too_many": "Слишком много попыток. Попробуйте через несколько минут.",
    "no_trial": "Пробный период сейчас недоступен.",
    "own": "Этот промокод создали вы — активировать его нельзя.",
    "claim_gone": "Скидка по промокоду уже использована — оформите покупку заново.",
}
#: English of :data:`REFUSALS` (same keys).
REFUSALS_EN: Final[Mapping[str, str]] = {
    "not_found": "There is no such promo code. Check the spelling.",
    "inactive": "This promo code is no longer valid.",
    "not_started": "This promo code is not active yet.",
    "expired": "This promo code has expired.",
    "exhausted": "This promo code has already been used the maximum number of times.",
    "used": "You have already used this promo code.",
    "not_new": "This promo code is for new customers only.",
    "banned": "The account is blocked.",
    "currency": "This promo code does not work right now. Please contact support.",
    "no_sub": "This promo code adds days to a subscription — get one first.",
    "has_paid_sub": "This promo code is for a trial — you already have a paid subscription.",
    "trial_used": "The trial has already been used.",
    "plan_conflict": "You have another paid subscription — the gift for this plan is not available.",
    "plan_missing": "The plan of this promo code is no longer available. Please contact support.",
    "plan_not_allowed": "This promo code does not apply to this plan.",
    "too_many": "Too many attempts. Please try again in a few minutes.",
    "no_trial": "The trial is not available right now.",
    "own": "You created this promo code — you cannot activate it.",
    "claim_gone": "The promo code discount has already been used — start the purchase again.",
}


class PromoError(ValueError):
    """A bad promo definition; ``str(error)`` is a short Russian message for the owner."""


def plural_days(n: int, lang: str = "ru") -> str:
    if lang == "en":
        return f"{n} day" if n == 1 else f"{n} days"
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        word = "день"
    elif 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        word = "дня"
    else:
        word = "дней"
    return f"{n} {word}"


# ------------------------------------------------------------------------------------------------ model


def _opt_int(value: Any) -> int | None:
    return None if value is None else int(value)


@dataclass(frozen=True, slots=True)
class Promo:
    id: int
    code: str
    kind: str
    title: str | None = None
    days: int | None = None
    amount_minor: int | None = None
    currency: str | None = None
    percent: int | None = None
    plan_id: int | None = None
    pending_hours: int | None = None
    max_uses: int | None = None
    uses: int = 0
    once_per_user: bool = True
    new_users_only: bool = False
    min_amount_minor: int | None = None
    plan_ids: tuple[int, ...] = ()
    starts_at: datetime | None = None
    expires_at: datetime | None = None
    enabled: bool = True
    source: str = "bot"
    version: int = 1
    created_at: datetime | None = None
    created_by: int | None = None  # the staff member who made it (``None``: imported / seeded)

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Promo:
        raw_plans = row.get("plan_ids") or []
        plans = tuple(int(p) for p in raw_plans if isinstance(p, int) and not isinstance(p, bool))
        return cls(
            id=int(row["id"]),
            code=str(row["code"]),
            kind=str(row["kind"]),
            title=row.get("title"),
            days=_opt_int(row.get("days")),
            amount_minor=_opt_int(row.get("amount_minor")),
            currency=row.get("currency"),
            percent=_opt_int(row.get("percent")),
            plan_id=_opt_int(row.get("plan_id")),
            pending_hours=_opt_int(row.get("pending_hours")),
            max_uses=_opt_int(row.get("max_uses")),
            uses=int(row.get("uses") or 0),
            once_per_user=bool(row.get("once_per_user", True)),
            new_users_only=bool(row.get("new_users_only", False)),
            min_amount_minor=_opt_int(row.get("min_amount_minor")),
            plan_ids=plans,
            starts_at=row.get("starts_at"),
            expires_at=row.get("expires_at"),
            enabled=bool(row.get("enabled", True)),
            source=str(row.get("source") or "bot"),
            version=int(row.get("version") or 1),
            created_at=row.get("created_at"),
            created_by=_opt_int(row.get("created_by")),
        )

    @property
    def is_discount(self) -> bool:
        return self.kind in DISCOUNT_KINDS

    @property
    def linkable(self) -> bool:
        """Fits a ``pr_<code>`` deep link."""
        return bool(re.fullmatch(r"[A-Za-z0-9_-]{1,61}", self.code))

    def exhausted(self) -> bool:
        return self.max_uses is not None and self.uses >= self.max_uses

    def status(self, at: datetime) -> str:
        """``on`` | ``off`` | ``scheduled`` | ``expired`` | ``exhausted`` (the owner's list icon)."""
        if not self.enabled:
            return "off"
        if self.expires_at is not None and self.expires_at <= at:
            return "expired"
        if self.exhausted():
            return "exhausted"
        if self.starts_at is not None and self.starts_at > at:
            return "scheduled"
        return "on"


# ----------------------------------------------------------------------------------------------- checks


def normalize_input(raw: Any) -> str | None:
    """What a user typed (or a ``pr_`` deep link carried) → a lookup key, ``None`` if it cannot be a code."""
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    return value if _INPUT_RE.fullmatch(value) else None


def check_code(code: Any, *, legacy: bool = False) -> str:
    """A code for a new promo (``legacy``: an imported one, any spelling without spaces)."""
    value = code.strip() if isinstance(code, str) else ""
    if legacy:
        if not _INPUT_RE.fullmatch(value):
            raise PromoError("Код: от 1 до 64 символов без пробелов")
        return value
    if not CODE_RE.fullmatch(value):
        raise PromoError("Код: от 3 до 48 символов — латиница, цифры, «_» и «-»")
    return value


def generate_code(length: int = 8, *, prefix: str = "") -> str:
    return prefix + "".join(secrets.choice(_GEN_ALPHABET) for _ in range(length))


def _int_in(value: Any, lo: int, hi: int, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise PromoError(f"{what}: от {lo} до {hi}")
    return value


def validate(kind: str, values: Mapping[str, Any]) -> dict[str, Any]:
    """Check the value columns of ``kind`` and return them normalized (unused columns → ``None``)."""
    if kind not in KINDS:
        raise PromoError("Неизвестный вид промокода")
    used = KIND_FIELDS[kind]
    out: dict[str, Any] = {name: None for name in _VALUE_FIELDS}
    out["plan_ids"] = []
    if "days" in used:
        hi = 365 if kind == "trial_extend" else MAX_DAYS
        out["days"] = _int_in(values.get("days"), 1, hi, "Дней")
    if "percent" in used:
        out["percent"] = _int_in(values.get("percent"), 1, 100, "Скидка, %")
    if "amount_minor" in used:
        out["amount_minor"] = _int_in(values.get("amount_minor"), 1, MAX_AMOUNT_MINOR, "Сумма")
        currency = values.get("currency")
        if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
            raise PromoError("Не задана валюта")
        out["currency"] = currency
    if "plan_id" in used:
        out["plan_id"] = _int_in(values.get("plan_id"), 1, 2**62, "Тариф")
    if "pending_hours" in used:
        hours = values.get("pending_hours")
        out["pending_hours"] = (
            DEFAULT_PENDING_HOURS if hours is None else _int_in(hours, 1, MAX_PENDING_HOURS, "Часов")
        )
    if "min_amount_minor" in used and values.get("min_amount_minor") is not None:
        out["min_amount_minor"] = _int_in(values.get("min_amount_minor"), 1, MAX_AMOUNT_MINOR, "Минимум")
    if "plan_ids" in used:
        raw = values.get("plan_ids") or []
        if not isinstance(raw, Iterable) or isinstance(raw, (str, bytes)):
            raise PromoError("Тарифы: нужен список")
        out["plan_ids"] = sorted({_int_in(p, 1, 2**62, "Тариф") for p in raw})
    return out


def validate_limits(values: Mapping[str, Any]) -> None:
    """Cross-field checks of the limit columns."""
    max_uses = values.get("max_uses")
    if max_uses is not None:
        _int_in(max_uses, 1, MAX_USES, "Лимит использований")
    starts, expires = values.get("starts_at"), values.get("expires_at")
    if isinstance(starts, datetime) and isinstance(expires, datetime) and expires <= starts:
        raise PromoError("Срок окончания раньше начала")


# ------------------------------------------------------------------------------------------- eligibility


@dataclass(frozen=True, slots=True)
class Facts:
    """What :func:`refusal` needs to know about the user (one SQL read)."""

    has_paid: bool = False
    used: bool = False
    live_sub: bool = False
    live_trial: bool = False
    live_plan_id: int | None = None
    trial_used: bool = False
    banned: bool = False


def refusal(promo: Promo, facts: Facts, at: datetime, *, currency: str) -> str | None:
    """Why ``promo`` cannot be activated by this user now (a key of :data:`REFUSALS`), or ``None``."""
    if not promo.enabled:
        return "inactive"
    if promo.starts_at is not None and promo.starts_at > at:
        return "not_started"
    if promo.expires_at is not None and promo.expires_at <= at:
        return "expired"
    if promo.exhausted():
        return "exhausted"
    if facts.banned:
        return "banned"
    if promo.once_per_user and facts.used:
        return "used"
    if promo.new_users_only and facts.has_paid:
        return "not_new"
    if promo.amount_minor is not None and promo.currency != currency:
        return "currency"
    kind = promo.kind
    if kind in ("days", "wallet_days") and not facts.live_sub:
        return "no_sub"
    if kind == "trial_extend":
        if facts.live_sub and not facts.live_trial:
            return "has_paid_sub"
        if not facts.live_sub and facts.trial_used:
            return "trial_used"
    if (
        kind == "plan_gift"
        and facts.live_sub
        and not facts.live_trial
        and facts.live_plan_id != promo.plan_id
    ):
        return "plan_conflict"
    return None


def pending_until(promo: Promo, at: datetime) -> datetime:
    """How long an activated discount waits for checkout (never past the promo's own expiry)."""
    until = at + timedelta(hours=promo.pending_hours or DEFAULT_PENDING_HOURS)
    if promo.expires_at is not None:
        until = min(until, promo.expires_at)
    return until


# -------------------------------------------------------------------------------------------- discount


@dataclass(frozen=True, slots=True)
class PromoDiscount:
    """:class:`svbg.domain.pricing.Discount` of a promo code. ``source`` = ``promo:<id>`` — the redeemer finds
    the promo in ``orders.snapshot.discounts`` by it."""

    promo_id: int
    code: str
    percent: int | None = None
    amount_minor: int | None = None
    min_amount_minor: int | None = None

    lang: str = "ru"

    @property
    def label(self) -> str:
        if self.lang == "en":
            off = f"−{self.percent}%" if self.percent is not None else "discount"
            return f"Promo code {self.code} {off}"
        off = f"−{self.percent} %" if self.percent is not None else "скидка"
        return f"Промокод {self.code} {off}"

    @property
    def source(self) -> str:
        return f"promo:{self.promo_id}"

    def amount_off(self, subtotal_minor: int) -> int:
        if subtotal_minor <= 0:
            return 0
        if self.min_amount_minor is not None and subtotal_minor < self.min_amount_minor:
            return 0
        if self.percent is not None:
            return subtotal_minor * self.percent // 100
        return min(self.amount_minor or 0, subtotal_minor)


def discount_of(promo: Promo, plan_id: int | None) -> PromoDiscount | None:
    """The checkout discount of ``promo`` for ``plan_id`` (``None``: not a discount / plan not allowed)."""
    if not promo.is_discount:
        return None
    if promo.plan_ids and plan_id not in promo.plan_ids:
        return None
    return PromoDiscount(promo.id, promo.code, promo.percent, promo.amount_minor, promo.min_amount_minor)


def promo_ids_of(snapshot: Mapping[str, Any] | None) -> list[int]:
    """Promo ids in an order snapshot's ``discounts`` (``source = promo:<id>``)."""
    out: list[int] = []
    for d in (snapshot or {}).get("discounts") or ():
        src = d.get("source") if isinstance(d, Mapping) else None
        if isinstance(src, str) and src.startswith("promo:") and src[6:].isdigit():
            out.append(int(src[6:]))
    return list(dict.fromkeys(out))


# ------------------------------------------------------------------------------------------------ texts


def describe(
    promo: Promo,
    *,
    money: Callable[[int, str], str],
    plan_title: Callable[[int], str | None] | None = None,
    lang: str = "ru",
) -> str:
    """One line for users and the owner: «−20 % на покупку», «+7 дней к подписке», «+100 ₽ на баланс»…
    (``lang="en"``: «−20% off your purchase», «+7 days to your subscription»…)."""
    cur = promo.currency or ""
    kind = promo.kind
    if lang == "en":
        days = plural_days(promo.days or 0, "en")
        if kind == "days":
            return f"+{days} to your subscription"
        if kind == "percent":
            return f"−{promo.percent}% off your purchase"
        if kind == "fixed":
            return f"−{money(promo.amount_minor or 0, cur)} off your purchase"
        if kind == "wallet":
            return f"+{money(promo.amount_minor or 0, cur)} to your balance"
        if kind == "trial_extend":
            return f"trial +{days}"
        if kind == "plan_gift":
            title = (
                plan_title(promo.plan_id) if plan_title and promo.plan_id else None
            ) or f"#{promo.plan_id}"
            return f"«{title}» plan for {days}"
        if kind == "wallet_days":
            return f"+{money(promo.amount_minor or 0, cur)} to your balance and +{days}"
        return kind
    if kind == "days":
        return f"+{plural_days(promo.days or 0)} к подписке"
    if kind == "percent":
        return f"−{promo.percent} % на покупку"
    if kind == "fixed":
        return f"−{money(promo.amount_minor or 0, cur)} на покупку"
    if kind == "wallet":
        return f"+{money(promo.amount_minor or 0, cur)} на баланс"
    if kind == "trial_extend":
        return f"пробный период +{plural_days(promo.days or 0)}"
    if kind == "plan_gift":
        title = (plan_title(promo.plan_id) if plan_title and promo.plan_id else None) or f"#{promo.plan_id}"
        return f"тариф «{title}» на {plural_days(promo.days or 0)}"
    if kind == "wallet_days":
        return f"+{money(promo.amount_minor or 0, cur)} на баланс и +{plural_days(promo.days or 0)}"
    return kind
