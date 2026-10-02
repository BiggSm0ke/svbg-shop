"""LTE quotas: shared vocabulary of the pure engines (05 §2.1).

Units, the Moscow clock, the value sets stored in the ``lte_*`` tables (05 §2.1.12) and the mapping of
core ``subscription_events`` kinds (X2) onto the kinds of the period engine. No I/O, no settings lookups: the
runtime passes everything in.

Limit triple semantics (05 §2.1.1): a limit row that is **absent** means "not set", ``None`` means
"unlimited", ``0`` means "the group is unavailable". Rows are keyed ``default`` / ``trial`` for a group and
``all`` / ``paid`` / ``trial`` for an individual override (``lte_overrides.applies_to``).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any, Final, Literal

__all__ = [
    "ADMIN_SOURCES",
    "ANCHOR_KINDS",
    "BLOCK_MODES",
    "BLOCK_REASONS",
    "BLOCK_STATUSES",
    "CREDIT_SOURCES",
    "CREDIT_STATUSES",
    "DEFAULT_GB_BYTES",
    "ENFORCE_MODES",
    "EXEMPT_KINDS",
    "FAR_FUTURE",
    "GIFT_SOURCES",
    "GROUP_STATES",
    "LIVE_BLOCK_STATUSES",
    "MSK",
    "NON_MONEY_SOURCES",
    "OVERRIDE_APPLIES_TO",
    "OVERRIDE_KINDS",
    "PAID_CORE_KINDS",
    "PERIOD_STATES",
    "AnchorKind",
    "BlockMode",
    "BlockReason",
    "BlockStatus",
    "EnforceMode",
    "EventKind",
    "ExemptKind",
    "PeriodStateName",
    "aware",
    "cap",
    "engine_event_kind",
    "group_limit_rows",
    "int_param",
    "msk_date",
    "msk_midnight",
    "utc_date",
]

#: "ГБ" for users and the admin is 10⁹ bytes (05 §2.1.1); the registry may switch it to 2³⁰.
DEFAULT_GB_BYTES: Final = 10**9

#: Period boundaries live at 00:00 Moscow time. Moscow has no DST since 2014, so a fixed offset is exact and
#: does not depend on the tz database of the host (Windows has none by default).
MSK: Final = timezone(timedelta(hours=3), "MSK")

#: "Forever" in SvBG is 2099-12-31 (02 §3.2): every date computation is capped there (lesson 4, 05 §2.1.16).
FAR_FUTURE: Final = datetime(2099, 12, 31, 23, 59, 59, tzinfo=UTC)

AnchorKind = Literal["paid", "admin", "trial", "provisional", "manual", "import"]
ANCHOR_KINDS: Final[tuple[str, ...]] = ("paid", "admin", "trial", "provisional", "manual", "import")

PeriodStateName = Literal["open", "deferred", "closed"]
PERIOD_STATES: Final[tuple[str, ...]] = ("open", "deferred", "closed")

GROUP_STATES: Final[tuple[str, ...]] = ("draft", "active", "suspended")

OVERRIDE_KINDS: Final[tuple[str, ...]] = ("limit", "exempt", "no_block")
OVERRIDE_APPLIES_TO: Final[tuple[str, ...]] = ("all", "paid", "trial")
ExemptKind = Literal["owner", "launch_trial", "manual"]
EXEMPT_KINDS: Final[tuple[str, ...]] = ("owner", "launch_trial", "manual")

BlockReason = Literal["quota", "unavailable", "manual"]
BLOCK_REASONS: Final[tuple[str, ...]] = ("quota", "unavailable", "manual")
BlockMode = Literal["enforce", "shadow"]
BLOCK_MODES: Final[tuple[str, ...]] = ("enforce", "shadow")
BlockStatus = Literal["active", "releasing", "released", "cancelled"]
BLOCK_STATUSES: Final[tuple[str, ...]] = ("active", "releasing", "released", "cancelled")
#: Statuses covered by the partial UNIQUE "one live block per (subscription, group)".
LIVE_BLOCK_STATUSES: Final[tuple[str, ...]] = ("active", "releasing")

CREDIT_SOURCES: Final[tuple[str, ...]] = ("order", "admin", "import")
CREDIT_STATUSES: Final[tuple[str, ...]] = ("active", "expired", "refunded")

#: ``lte.enforce``: ``off`` — no decisions (blocks released or held, see ``release_when_off``), ``shadow`` —
#: decisions are journaled with ``mode='shadow'`` and never reach the panel, ``on`` — applied.
EnforceMode = Literal["off", "shadow", "on"]
ENFORCE_MODES: Final[tuple[str, ...]] = ("off", "shadow", "on")

#: Kinds of the period engine (:mod:`svbg.ext.lte.periods`).
#: ``paid`` — money paid for the term; ``admin`` — term granted by an admin; ``bonus`` — referral/promo days;
#: ``trial`` — a trial started; ``import`` — imported state; ``freeze`` — the term was frozen (no effect on
#: the series, E8 is held by the hold intervals); ``unfreeze`` — the frozen term returned (E1); ``close`` —
#: the term ends now (E2); ``unclassified`` — anything else (a new series from it needs a review).
EventKind = Literal[
    "paid", "admin", "bonus", "trial", "import", "freeze", "unfreeze", "close", "unclassified"
]

#: Core ``subscription_events.kind`` (svbg.subscriptions) → engine kind. ``extended`` depends on its source.
_CORE_KINDS: Final[Mapping[str, str]] = {
    "purchase_new": "paid",
    "purchase_renew": "paid",
    "plan_changed": "paid",
    "trial_converted": "paid",
    "site": "paid",
    "new": "paid",
    "renew": "paid",
    "change": "paid",
    "trial_convert": "paid",
    "trial_started": "trial",
    "trial": "trial",
    "referral": "bonus",
    "bonus": "bonus",
    "compensation": "bonus",
    "admin": "admin",
    "import": "import",
    "frozen": "freeze",
    "frozen_credit": "freeze",
    "freeze": "freeze",
    "hold_zeroed": "freeze",
    "unfrozen": "unfreeze",
    "unfreeze": "unfreeze",
    "closed": "close",
    "close": "close",
}
#: Paid core kinds: the first of them revokes a live ``launch_trial`` exemption (05 §2.1.9) — but only when
#: money paid for it (see :data:`NON_MONEY_SOURCES`).
PAID_CORE_KINDS: Final = frozenset(k for k, v in _CORE_KINDS.items() if v == "paid")
#: ``subscription_events.source`` of a purchase written without money: an admin giving a plan
#: (``lifecycle.purchase(source="admin")``) and gifts (a ``plan_gift`` promo, referral or bonus days).
ADMIN_SOURCES: Final = frozenset({"admin"})
GIFT_SOURCES: Final = frozenset({"promo", "referral", "bonus", "compensation", "gift"})
NON_MONEY_SOURCES: Final = ADMIN_SOURCES | GIFT_SOURCES


def engine_event_kind(core_kind: str, *, source: str = "", details: Mapping[str, Any] | None = None) -> str:
    """Kind of the period engine for a core ``subscription_events`` row.

    A purchase kind is ``paid`` only for real orders: written by an admin (``source='admin'``) it is
    ``admin``, by a gift (promo / referral / bonus) it is ``bonus`` — neither revokes ``launch_trial`` nor
    anchors as paid (05 §2.1.9, scenario 61). ``extended`` (granted time) is ``admin`` when an admin granted
    it, ``bonus`` for referral/promo days (``details.reason``/``source``), otherwise ``unclassified``.
    Unknown kinds are ``unclassified`` too: the engine then asks for a review instead of guessing.
    """
    kind = _CORE_KINDS.get(core_kind)
    if kind == "paid":
        if source in ADMIN_SOURCES:
            return "admin"
        if source in GIFT_SOURCES:
            return "bonus"
    if kind is not None:
        return kind
    if core_kind == "extended":
        reason = str((details or {}).get("reason") or "")
        if source == "admin" or reason == "admin":
            return "admin"
        if source in {"referral", "promo", "bonus"} or reason in {
            "referral",
            "promo",
            "bonus",
            "compensation",
        }:
            return "bonus"
    return "unclassified"


def aware(moment: datetime) -> datetime:
    """``moment`` as an aware UTC datetime; naive values are rejected (they hide timezone bugs)."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("ожидается время с часовым поясом")
    return moment.astimezone(UTC)


def cap(moment: datetime | None) -> datetime | None:
    """Clamp a date to :data:`FAR_FUTURE` ("forever" must not overflow date arithmetic)."""
    if moment is None:
        return None
    return moment if moment <= FAR_FUTURE else FAR_FUTURE


def utc_date(moment: datetime) -> date:
    """UTC date of a panel history row (``nodes_user_usage_history`` dates rows by ``NOW()`` in UTC)."""
    return moment.astimezone(UTC).date()


def msk_date(moment: datetime) -> date:
    """Moscow date — the key of ``lte_usage_daily``."""
    return moment.astimezone(MSK).date()


def msk_midnight(day: date) -> datetime:
    """00:00 Moscow time of ``day`` as an aware UTC datetime."""
    return datetime(day.year, day.month, day.day, tzinfo=MSK).astimezone(UTC)


def group_limit_rows(
    *, has_default: bool, limit_default: int | None, has_trial: bool, limit_trial: int | None
) -> dict[str, int | None]:
    """``lte_groups`` columns → limit rows (key present = set; ``None`` = unlimited; ``0`` = unavailable)."""
    rows: dict[str, int | None] = {}
    if has_default:
        rows["default"] = limit_default
    if has_trial:
        rows["trial"] = limit_trial
    return rows


def int_param(values: Mapping[str, Any], key: str, default: int, low: int, high: int) -> int:
    """An integer setting clamped to ``[low, high]``; garbage gives the default (the engines never crash on
    a bad setting — the registry validates on write, this is the second line)."""
    raw = values.get(key, default)
    if isinstance(raw, bool):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return min(max(value, low), high)
