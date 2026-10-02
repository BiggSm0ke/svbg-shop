"""Catalog writes. Every function takes the caller's open transaction (``conn``) and validates its input.

Admin actions are audited (``admin_audit``, 04 §9.1) in the same transaction when an ``actor`` is given.
After the commit the caller reloads the snapshot (:meth:`CatalogService.changed`).

Edits bump ``plans.version``; :func:`update_plan` with ``expected_version`` is a compare-and-set (used when a
change has side effects on live subscriptions, e.g. squads: a double click cannot apply twice).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from svbg.catalog.model import (
    AVAILABILITY,
    DEFAULT_LANG,
    DEVICES_ON_RENEW,
    MAX_DAYS,
    MAX_DEVICES,
    MAX_LOCATION_TITLE,
    RESET_STRATEGIES,
    TRAFFIC_ON_RENEW,
    CatalogError,
    DeviceAddon,
    slugify,
    validate_code,
    validate_name,
    validate_squads,
    validate_tag,
)
from svbg.catalog.tables import locations, plan_prices, plans
from svbg.core.money import MAX_AMOUNT_MINOR
from svbg.core.tables import admin_audit

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

__all__ = [
    "EDITABLE",
    "Actor",
    "StalePlanError",
    "audit",
    "create_plan",
    "delete_price",
    "set_location",
    "set_price",
    "toggle_highlight",
    "unique_code",
    "update_plan",
]

#: Columns :func:`update_plan` may change.
EDITABLE: Final = frozenset(
    {
        "name",
        "availability",
        "is_trial",
        "enabled",
        "traffic_bytes",
        "reset_strategy",
        "device_limit",
        "squads",
        "ext_squad",
        "panel_tag",
        "traffic_on_renew",
        "devices_on_renew",
        "device_addon",
        "broken_reason",
        "sort",
    }
)
MAX_TRAFFIC_BYTES: Final = 2**62
_JSONB: Final = plans.c.name.type


def _jsonb(value: Mapping[str, Any]) -> sa.ColumnElement[Any]:
    return sa.cast(sa.literal(dict(value), _JSONB), _JSONB)


class StalePlanError(CatalogError):
    """The plan changed (or vanished) since the editor showed it."""

    def __init__(self, message: str = "Тариф уже изменили — откройте его заново") -> None:
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class Actor:
    """Who made the change (for ``admin_audit``)."""

    user_id: int | None
    role: str | None = None


async def audit(
    conn: AsyncConnection,
    actor: Actor | None,
    action: str,
    target: str,
    details: Mapping[str, Any] | None = None,
) -> None:
    if actor is None:
        return
    await conn.execute(
        sa.insert(admin_audit).values(
            actor_id=actor.user_id,
            role=actor.role,
            action=action,
            target=target[:200],
            details=dict(details or {}),
        )
    )


def _check_int(value: Any, what: str, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise CatalogError(f"{what}: от {lo} до {hi}")
    return value


def _choice(value: Any, allowed: tuple[str, ...], what: str) -> str:
    if value not in allowed:
        raise CatalogError(what)
    return str(value)


def _clean(changes: Mapping[str, Any]) -> dict[str, Any]:
    """Validated, JSON-safe column values (also what goes into the audit details)."""
    unknown = set(changes) - EDITABLE
    if unknown:
        raise ValueError(f"not editable: {sorted(unknown)}")
    out: dict[str, Any] = {}
    for key, value in changes.items():
        match key:
            case "name":
                if not isinstance(value, Mapping) or not value:
                    raise CatalogError("Название не может быть пустым")
                out[key] = {str(lang): validate_name(str(text)) for lang, text in value.items()}
            case "availability":
                out[key] = _choice(value, AVAILABILITY, "Неизвестная доступность")
            case "reset_strategy":
                out[key] = _choice(value, RESET_STRATEGIES, "Неизвестная стратегия сброса трафика")
            case "traffic_on_renew":
                out[key] = _choice(value, TRAFFIC_ON_RENEW, "Неизвестная политика трафика при продлении")
            case "devices_on_renew":
                out[key] = _choice(value, DEVICES_ON_RENEW, "Неизвестная политика устройств при продлении")
            case "is_trial" | "enabled":
                if not isinstance(value, bool):
                    raise CatalogError("Нужно да или нет")
                out[key] = value
            case "traffic_bytes":
                out[key] = _check_int(value, "Лимит трафика", 0, MAX_TRAFFIC_BYTES)
            case "device_limit":
                out[key] = None if value is None else _check_int(value, "Устройств", 0, MAX_DEVICES)
            case "squads":
                out[key] = list(validate_squads(value))
            case "ext_squad":
                if value is not None and (not isinstance(value, str) or not 1 <= len(value) <= 64):
                    raise CatalogError("Неверный внешний сквад")
                out[key] = value
            case "panel_tag":
                out[key] = validate_tag(value)
            case "device_addon":
                if value is None:
                    out[key] = {}
                elif isinstance(value, DeviceAddon):
                    out[key] = value.to_json()
                else:
                    raise CatalogError("Неверная доплата за устройства")
            case "broken_reason":
                out[key] = None if value is None else str(value)[:500]
            case "sort":
                out[key] = _check_int(value, "Порядок", -1_000_000, 1_000_000)
    return out


async def unique_code(conn: AsyncConnection, base: str) -> str:
    """``base`` or ``base_2``, ``base_3``… — the first code not taken (codes are deep-link names)."""
    base = validate_code(base)
    pattern = base.replace("_", r"\_") + r"\_%"
    taken = set(
        (
            await conn.execute(
                sa.select(plans.c.code).where(sa.or_(plans.c.code == base, plans.c.code.like(pattern)))
            )
        ).scalars()
    )
    if base not in taken:
        return base
    n = 2
    while True:
        suffix = f"_{n}"
        candidate = base[: 32 - len(suffix)] + suffix
        if candidate not in taken:
            return candidate
        n += 1


async def create_plan(
    conn: AsyncConnection,
    *,
    name: str,
    lang: str = DEFAULT_LANG,
    code: str | None = None,
    actor: Actor | None = None,
    **fields: Any,
) -> int:
    """A new plan (hidden until it has squads and prices and is put on sale). Returns its id."""
    title = validate_name(name)
    code = await unique_code(conn, validate_code(code) if code else slugify(title))
    values = _clean(fields)
    values.setdefault("enabled", False)
    if values["enabled"] and not values.get("squads"):
        raise CatalogError("Нельзя включить тариф без сквадов")
    if "sort" not in values:
        top = await conn.scalar(sa.select(sa.func.coalesce(sa.func.max(plans.c.sort), 0)))
        values["sort"] = int(top or 0) + 10
    try:
        plan_id = await conn.scalar(
            sa.insert(plans).values(code=code, name={lang: title}, **values).returning(plans.c.id)
        )
    except sa.exc.IntegrityError as e:
        if "uq_plans_trial" in str(e.orig):
            raise CatalogError("Пробный тариф уже есть: сначала снимите отметку с него") from None
        raise
    await audit(conn, actor, "plan.create", f"plan:{plan_id}", {"code": code, "name": title})
    return int(plan_id)


async def update_plan(
    conn: AsyncConnection,
    plan_id: int,
    *,
    expected_version: int | None = None,
    actor: Actor | None = None,
    audit_action: str = "plan.update",
    **changes: Any,
) -> int:
    """Apply ``changes`` and bump ``version``; returns the new version.

    ``expected_version`` makes it a compare-and-set: :class:`StalePlanError` when the plan changed meanwhile.
    ``name`` merges per language (``{"ru": "…"}`` keeps the other languages).
    """
    cleaned = _clean(changes)
    if not cleaned:
        raise ValueError("nothing to change")
    values: dict[str, Any] = dict(cleaned)
    if "name" in values:
        values["name"] = plans.c.name.op("||")(_jsonb(cleaned["name"]))
    if cleaned.get("enabled") is True or "squads" in cleaned:
        # a plan on sale always has squads (also a DB CHECK): say it in words first
        row = (
            await conn.execute(sa.select(plans.c.squads, plans.c.enabled).where(plans.c.id == plan_id))
        ).first()
        if row is None:
            raise StalePlanError("Тариф не найден")
        if cleaned.get("enabled", row.enabled) and not cleaned.get("squads", row.squads):
            raise CatalogError("Сначала выберите сквады тарифа")
    cond = [plans.c.id == plan_id]
    if expected_version is not None:
        cond.append(plans.c.version == expected_version)
    stmt = (
        sa.update(plans)
        .where(*cond)
        .values(**values, version=plans.c.version + 1, updated_at=sa.func.now())
        .returning(plans.c.version)
    )
    try:
        version = await conn.scalar(stmt)
    except sa.exc.IntegrityError as e:
        if "uq_plans_trial" in str(e.orig):
            raise CatalogError("Пробный тариф уже есть: сначала снимите отметку с него") from None
        raise
    if version is None:
        raise StalePlanError()
    await audit(conn, actor, audit_action, f"plan:{plan_id}", cleaned)
    return int(version)


async def _bump(conn: AsyncConnection, plan_id: int) -> None:
    found = await conn.scalar(
        sa.update(plans)
        .where(plans.c.id == plan_id)
        .values(version=plans.c.version + 1, updated_at=sa.func.now())
        .returning(plans.c.id)
    )
    if found is None:
        raise StalePlanError("Тариф не найден")


def _price_row(plan_id: int, days: int, currency: str) -> sa.ColumnElement[bool]:
    return sa.and_(
        plan_prices.c.plan_id == plan_id, plan_prices.c.days == days, plan_prices.c.currency == currency
    )


async def set_price(
    conn: AsyncConnection,
    plan_id: int,
    *,
    days: int,
    amount_minor: int,
    currency: str,
    actor: Actor | None = None,
) -> None:
    """Add or change the price of one period (UNIQUE ``(plan, days, currency)``)."""
    _check_int(days, "Период", 1, MAX_DAYS)
    _check_int(amount_minor, "Цена", 1, MAX_AMOUNT_MINOR)
    await _bump(conn, plan_id)
    stmt = pg_insert(plan_prices).values(
        plan_id=plan_id, days=days, currency=currency, amount_minor=amount_minor
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_plan_prices_plan_days_currency", set_={"amount_minor": stmt.excluded.amount_minor}
    )
    await conn.execute(stmt)
    details = {"days": days, "amount_minor": amount_minor, "cur": currency}
    await audit(conn, actor, "plan.price", f"plan:{plan_id}", details)


async def delete_price(
    conn: AsyncConnection, plan_id: int, *, days: int, currency: str, actor: Actor | None = None
) -> bool:
    gone = await conn.scalar(
        sa.delete(plan_prices).where(_price_row(plan_id, days, currency)).returning(plan_prices.c.id)
    )
    if gone is None:
        return False
    await _bump(conn, plan_id)
    await audit(conn, actor, "plan.price_delete", f"plan:{plan_id}", {"days": days, "cur": currency})
    return True


async def toggle_highlight(
    conn: AsyncConnection, plan_id: int, *, days: int, currency: str, actor: Actor | None = None
) -> bool | None:
    """Make ``days`` the highlighted period or un-highlight it. New state; ``None`` when there is no price."""
    current = await conn.scalar(sa.select(plan_prices.c.highlight).where(_price_row(plan_id, days, currency)))
    if current is None:
        return None
    new = not current
    if new:  # one highlighted period per plan and currency
        await conn.execute(
            sa.update(plan_prices)
            .where(plan_prices.c.plan_id == plan_id, plan_prices.c.currency == currency)
            .values(highlight=False)
        )
    await conn.execute(
        sa.update(plan_prices).where(_price_row(plan_id, days, currency)).values(highlight=new)
    )
    await _bump(conn, plan_id)
    await audit(conn, actor, "plan.highlight", f"plan:{plan_id}", {"days": days, "on": new})
    return new


async def set_location(
    conn: AsyncConnection,
    squad_uuid: str,
    *,
    title: str | None = None,
    lang: str = DEFAULT_LANG,
    flag: str | None = None,
    clear_flag: bool = False,
    sort: int | None = None,
    actor: Actor | None = None,
) -> bool:
    """Change the display title / flag / order of a known location. ``False`` if there is no such row."""
    values: dict[str, Any] = {}
    details: dict[str, Any] = {}
    if title is not None:
        text = validate_name(title, limit=MAX_LOCATION_TITLE)
        values["title"] = locations.c.title.op("||")(_jsonb({lang: text}))
        details["title"] = text
    if clear_flag:
        values["flag"] = details["flag"] = None
    elif flag is not None:
        f = flag.strip()
        if not 1 <= len(f) <= 16 or any(ch.isspace() for ch in f):
            raise CatalogError("Флаг: один эмодзи, например 🇳🇱")
        values["flag"] = details["flag"] = f
    if sort is not None:
        values["sort"] = details["sort"] = _check_int(sort, "Порядок", -1_000_000, 1_000_000)
    if not values:
        raise ValueError("nothing to change")
    found = await conn.scalar(
        sa.update(locations)
        .where(locations.c.squad_uuid == squad_uuid)
        .values(**values, updated_at=sa.func.now())
        .returning(locations.c.squad_uuid)
    )
    if found is None:
        return False
    await audit(conn, actor, "location.update", f"squad:{squad_uuid}", details)
    return True
