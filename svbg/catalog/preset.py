"""The owner's catalog as a preset (06 M1–M2): «Стандарт» 179/499/899/1699 ₽ for 30/90/180/360 days, 5 devices
included, an extra device 19 ₽ per 30 days up to 15, unlimited traffic, panel tag ``PAID``; and the trial
plan (5 devices, unlimited traffic; its length is ``TRIAL_DAYS`` and its audience ``TRIAL_AUDIENCE``).

Not applied by default: the owner (or the importer) calls :func:`seed_owner_preset`, or presses the preset
button in the empty plan editor. Idempotent: an existing ``standard`` / ``trial`` plan is left as it is.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import sqlalchemy as sa

from svbg.catalog.model import DeviceAddon
from svbg.catalog.repo import Actor, audit, create_plan, set_price
from svbg.catalog.tables import locations, plans
from svbg.core.money import exponent

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

__all__ = ["OWNER_PRICES", "PresetResult", "seed_owner_preset"]

#: (days, rubles) of the owner's paid plan.
OWNER_PRICES: Final[tuple[tuple[int, int], ...]] = ((30, 179), (90, 499), (180, 899), (360, 1699))
OWNER_DEVICES: Final = 5
#: Extra device: (price in whole currency units per `per_days`, per_days, max devices in total).
OWNER_ADDON: Final = (19, 30, 15)
STANDARD_CODE: Final = "standard"
TRIAL_CODE: Final = "trial"


@dataclass(frozen=True, slots=True)
class PresetResult:
    plan_id: int
    trial_id: int | None
    created: bool  # False: the standard plan already existed (nothing changed)
    enabled: bool  # the standard plan is on sale (squads were known)


async def _existing(conn: AsyncConnection, code: str) -> int | None:
    found = await conn.scalar(sa.select(plans.c.id).where(plans.c.code == code))
    return None if found is None else int(found)


async def seed_owner_preset(
    conn: AsyncConnection,
    *,
    squads: Sequence[str] | None = None,
    currency: str = "RUB",
    with_trial: bool = True,
    actor: Actor | None = None,
) -> PresetResult:
    """Create the owner's plans in the caller's transaction.

    ``squads`` — internal squads of the plans; default: every location present in the panel. Without squads
    the plans are created hidden (the editor asks to pick squads before putting them on sale).
    """
    if squads is None:
        squads = list(
            (
                await conn.execute(
                    sa.select(locations.c.squad_uuid)
                    .where(locations.c.missing_since.is_(None))
                    .order_by(locations.c.sort, locations.c.squad_uuid)
                )
            ).scalars()
        )
    chosen = list(dict.fromkeys(squads))
    trial_id: int | None = None
    plan_id = await _existing(conn, STANDARD_CODE)
    if plan_id is not None:
        trial_id = await _existing(conn, TRIAL_CODE)
        enabled = bool(await conn.scalar(sa.select(plans.c.enabled).where(plans.c.id == plan_id)))
        return PresetResult(plan_id, trial_id, created=False, enabled=enabled)
    common = {
        "traffic_bytes": 0,
        "reset_strategy": "NO_RESET",
        "device_limit": OWNER_DEVICES,
        "enabled": bool(chosen),
    }
    if chosen:
        common["squads"] = chosen
    price, per_days, cap = OWNER_ADDON
    addon = DeviceAddon(price * 10 ** exponent(currency), per_days, cap, currency)
    plan_id = await create_plan(
        conn,
        name="Стандарт",
        code=STANDARD_CODE,
        availability="all",
        panel_tag="PAID",
        device_addon=addon,
        sort=10,
        **common,
    )
    for days, rubles in OWNER_PRICES:
        await set_price(
            conn, plan_id, days=days, amount_minor=rubles * 10 ** exponent(currency), currency=currency
        )
    if with_trial:
        trial_id = await _existing(conn, TRIAL_CODE)
        trial_taken = await conn.scalar(sa.select(plans.c.id).where(plans.c.is_trial))
        if trial_id is None and trial_taken is None:
            trial_id = await create_plan(
                conn, name="Пробный", code=TRIAL_CODE, is_trial=True, sort=0, **common
            )
    await audit(conn, actor, "plan.preset", f"plan:{plan_id}", {"preset": "owner", "trial_id": trial_id})
    return PresetResult(plan_id, trial_id, created=True, enabled=bool(chosen))
