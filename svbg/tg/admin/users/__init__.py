"""«👤 Пользователи» — smart search, the user card and its operations (stage 3a, 04 §9 / §9.1).

* :mod:`.search` — the search query (``pg_trgm``), :mod:`.queries` — the read side of the card and histories,
* :mod:`.ops` — the operations (through services and the panel writer; role re-check, limits, reason and
  ``admin_audit`` in one transaction),
* :mod:`.screens` — screens, forms, ``/user`` and the admin-group «👤 Карточка» button.

:func:`setup` is the module entry point for ``svbg.app`` (``setup(router, deps)``).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import Router

from svbg.core.money import exponent
from svbg.services.roles import Limits
from svbg.tg.admin.users.ops import UserOps
from svbg.tg.admin.users.screens import SCREEN_CARD, SCREEN_FIND, UserScreens, card_button

__all__ = ["SCREEN_CARD", "SCREEN_FIND", "UserOps", "UserScreens", "card_button", "settings_reader", "setup"]

log = logging.getLogger("svbg.tg.admin.users")


def settings_reader(settings: Any) -> Callable[[], Mapping[str, Any]]:
    """``settings.current()`` (the service) or a plain mapping (tests); never raises."""

    def read() -> Mapping[str, Any]:
        if settings is None:
            return {}
        current = getattr(settings, "current", None)
        try:
            value = current() if callable(current) else settings
        except RuntimeError:
            return {}
        return value if isinstance(value, Mapping) else {}

    return read


def _get(values: Mapping[str, Any], key: str, default: Any) -> Any:
    try:
        value = values[key]
    except (KeyError, RuntimeError):
        return default
    return default if value is None else value


def max_plan_price(catalog: Any, currency: str) -> int | None:
    """The price of the most expensive plan in ``currency`` (default admin wallet limit, 04 §9.1)."""
    snap = getattr(catalog, "snapshot", None)
    prices = [
        price.amount_minor
        for plan in getattr(snap, "plans", ())
        if not getattr(plan, "is_trial", False)
        for price in plan.prices_in(currency)
    ]
    return max(prices) if prices else None


def build(router: Any, deps: Any) -> UserScreens:
    """Build and install the screens over ``deps`` (``db``, ``settings``, ``users``, ``notifier``,
    ``catalog``)."""
    read = settings_reader(getattr(deps, "settings", None))
    directory = getattr(deps, "users", None)
    catalog = getattr(deps, "catalog", None)

    def currency() -> str:
        return str(_get(read(), "CURRENCY", "RUB"))

    def timezone() -> str:
        return str(_get(read(), "TIMEZONE", "Europe/Moscow"))

    def limits() -> Limits:
        cur = currency()
        try:
            exp = exponent(cur)
        except (KeyError, ValueError):
            exp = 2
        return Limits.from_settings(
            read(), currency_exponent=exp, max_plan_price_minor=max_plan_price(catalog, cur)
        )

    async def owner_ids() -> frozenset[int]:
        """``OWNER_IDS`` only: stored owners are read fresh by every role check (no 30 s owner cache)."""
        if directory is None:
            raw = _get(read(), "OWNER_IDS", [])
            return frozenset(int(x) for x in raw if isinstance(x, int) and not isinstance(x, bool))
        return frozenset(directory.configured_owner_ids())

    def invalidate(telegram_id: int | None) -> None:
        if directory is not None and telegram_id is not None:
            directory.invalidate(telegram_id)

    def plans() -> Any:
        return None if catalog is None else catalog.snapshot

    def format_date(value: datetime | None) -> str:
        if value is None:
            return "—"
        try:
            zone: tzinfo = ZoneInfo(timezone())
        except (ZoneInfoNotFoundError, ValueError):
            zone = UTC
        return value.astimezone(zone).strftime("%d.%m.%Y")

    screens = UserScreens(
        router,
        deps.db,
        UserOps(
            deps.db,
            owner_ids=owner_ids,
            limits=limits,
            currency=currency,
            plans=plans,
            notifier=getattr(deps, "notifier", None),
            format_date=format_date,
        ),
        currency=currency,
        timezone=timezone,
        owner_ids=owner_ids,
        invalidate=invalidate,
        plans=plans,
    )
    screens.install()
    return screens


def setup(router: Any, deps: Any) -> Router:
    """Module entry point for ``svbg.app``: registers the screens and returns the commands' aiogram router."""
    return build(router, deps).aiogram_router()
