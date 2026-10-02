"""Bedolaga promo codes → SvBG promos (06 §2.6). Pure mapping for the importer (stage 4).

=====================  =====================================================================================
Bedolaga ``type``      SvBG
=====================  =====================================================================================
``balance``            ``wallet`` (``balance_bonus_kopeks``)
``subscription_days``  ``days`` (``subscription_days``)
``trial_subscription`` ``trial_extend`` (``subscription_days``)
``discount``           ``percent``: **non-standard** — ``balance_bonus_kopeks`` is the percent,
                       ``subscription_days`` is how many **hours** the discount waits for checkout
``balance_and_days``   ``wallet_days``
``promo_group``        not imported (promo groups are not carried over)
=====================  =====================================================================================

Kept 1:1: ``code`` (as is, with its case), ``max_uses`` (0 → unlimited), ``valid_from``/``valid_until``,
``is_active``, ``first_purchase_only`` → ``new_users_only``, ``tariff_id`` → the only allowed / gifted plan.
``traffic_gb`` is ignored (unlimited traffic).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from svbg.promo.rules import PromoError, check_code, validate

__all__ = ["LegacyPromo", "from_bedolaga"]


@dataclass(frozen=True, slots=True)
class LegacyPromo:
    code: str
    kind: str
    values: Mapping[str, Any]
    limits: Mapping[str, Any] = field(default_factory=dict)
    legacy_id: str | None = None


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def from_bedolaga(
    row: Mapping[str, Any],
    *,
    currency: str = "RUB",
    plan_of: Callable[[int], int | None] = lambda _tariff: None,
) -> LegacyPromo:
    """Map one ``promocodes`` row; raises :class:`PromoError` for rows that are not imported (the importer
    lists them in its «не перенесено» report)."""
    kind_raw = str(row.get("type") or "")
    code = check_code(row.get("code"), legacy=True)
    bonus = _int(row.get("balance_bonus_kopeks")) or 0
    days = _int(row.get("subscription_days")) or 0
    tariff = _int(row.get("tariff_id"))
    plan_id = plan_of(tariff) if tariff is not None else None
    values: dict[str, Any] = {"currency": currency}
    if kind_raw == "balance":
        kind = "wallet"
        values["amount_minor"] = bonus
    elif kind_raw == "subscription_days":
        kind = "days"
        values["days"] = days
    elif kind_raw == "trial_subscription":
        kind = "trial_extend"
        values["days"] = days
    elif kind_raw == "discount":
        kind = "percent"
        values["percent"] = bonus
        values["pending_hours"] = days or None
        values["plan_ids"] = [plan_id] if plan_id is not None else []
    elif kind_raw == "balance_and_days":
        kind = "wallet_days"
        values.update(amount_minor=bonus, days=days)
    else:
        raise PromoError(f"тип «{kind_raw}» не переносится")
    clean = validate(kind, values)
    max_uses = _int(row.get("max_uses"))
    limits: dict[str, Any] = {
        "max_uses": max_uses if max_uses and max_uses > 0 else None,
        "enabled": bool(row.get("is_active", True)),
        "new_users_only": bool(row.get("first_purchase_only", False)),
        "starts_at": _ts(row.get("valid_from")),
        "expires_at": _ts(row.get("valid_until")),
        "once_per_user": True,
    }
    legacy_id = row.get("id")
    return LegacyPromo(code, kind, clean, limits, None if legacy_id is None else str(legacy_id))
