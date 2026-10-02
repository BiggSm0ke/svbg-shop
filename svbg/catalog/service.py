"""Catalog service: an immutable in-memory snapshot of plans, prices and locations (07 §2.7).

The hot path (buy screens, checkout, trial) reads :attr:`CatalogService.snapshot` — **no SQL**. The snapshot
is rebuilt by :meth:`CatalogService.reload` after every change (the editor calls it after its commit; another
process can be told through ``NOTIFY svbg_catalog``, see :meth:`CatalogService.listen`). ``reload()``
builds the new snapshot completely, then swaps one reference: readers see either the old or the new
catalog, never a mix. A bad row never breaks the snapshot — it is skipped and listed in ``problems``.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import logging
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final

import sqlalchemy as sa

from svbg.catalog.model import (
    DEFAULT_LANG,
    Audience,
    CatalogError,
    DeviceAddon,
    Location,
    Plan,
    PlanPrice,
    is_available,
)
from svbg.catalog.tables import locations, plan_prices, plans
from svbg.subscriptions.tables import subscriptions

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from svbg.db.engine import Database

__all__ = [
    "NOTIFY_CHANNEL",
    "CatalogService",
    "CatalogSnapshot",
    "build_snapshot",
    "count_plan_subscribers",
]

log = logging.getLogger("svbg.catalog")

NOTIFY_CHANNEL: Final = "svbg_catalog"


@dataclass(frozen=True, slots=True)
class CatalogSnapshot:
    """Plans sorted by ``(sort, id)``, locations by ``(sort, panel name)``; lookups are dicts."""

    version: int = 0
    plans: tuple[Plan, ...] = ()
    locations: tuple[Location, ...] = ()
    problems: tuple[str, ...] = ()
    default_lang: str = DEFAULT_LANG
    _by_id: Mapping[int, Plan] = field(default_factory=dict, repr=False)
    _by_code: Mapping[str, Plan] = field(default_factory=dict, repr=False)
    _locations: Mapping[str, Location] = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------------ lookups

    def plan(self, plan_id: int | None) -> Plan | None:
        return None if plan_id is None else self._by_id.get(plan_id)

    def by_code(self, code: str | None) -> Plan | None:
        return None if not code else self._by_code.get(code)

    def location(self, squad_uuid: str) -> Location | None:
        return self._locations.get(squad_uuid)

    @property
    def trial(self) -> Plan | None:
        """The trial plan, when one exists, is enabled and not broken."""
        return next((p for p in self.plans if p.is_trial and p.enabled and not p.broken and p.squads), None)

    @property
    def is_empty(self) -> bool:
        return not self.plans

    # ------------------------------------------------------------------ sales

    def for_sale(self, user: Audience, *, currency: str, link_code: str | None = None) -> tuple[Plan, ...]:
        """Plans ``user`` may buy now (the buy list), in display order."""
        return tuple(p for p in self.plans if is_available(p, user, currency=currency, link_code=link_code))

    def purchasable(
        self, plan_id: int, user: Audience, *, currency: str, link_code: str | None = None
    ) -> Plan | None:
        """Re-validation of a plan id taken from a callback (callback data can be forged)."""
        plan = self.plan(plan_id)
        if plan is None or not is_available(plan, user, currency=currency, link_code=link_code):
            return None
        return plan

    def renewable(self, plan_id: int | None, *, currency: str) -> Plan | None:
        """A plan a current subscriber may renew: availability and the «hidden» flag do not matter (the
        plan is only withdrawn from *new* sales), but it must not be broken, must not be the trial and must
        have a price in ``currency``."""
        plan = self.plan(plan_id)
        if plan is None or plan.is_trial or plan.broken or not plan.squads or not plan.prices_in(currency):
            return None
        return plan

    # ------------------------------------------------------------------ locations

    def present_locations(self) -> tuple[Location, ...]:
        return tuple(loc for loc in self.locations if loc.present)

    def squad_labels(self, squads: Iterable[str], lang: str | None = None) -> list[str]:
        out: list[str] = []
        for s in squads:
            loc = self._locations.get(s)
            out.append(loc.label(lang or self.default_lang) if loc else s[:8])
        return out

    def locations_signature(self) -> str:
        """Short fingerprint of the location order (checkbox masks are positions in this order)."""
        joined = "|".join(loc.squad_uuid for loc in self.locations)
        return hashlib.sha256(joined.encode()).hexdigest()[:6]

    def squads_from_mask(self, mask: int) -> tuple[str, ...]:
        return tuple(loc.squad_uuid for i, loc in enumerate(self.locations) if mask >> i & 1)

    def mask_of(self, squads: Iterable[str]) -> int:
        wanted = set(squads)
        return sum(1 << i for i, loc in enumerate(self.locations) if loc.squad_uuid in wanted)


EMPTY: Final = CatalogSnapshot()


def _plan_from_row(row: Mapping[str, Any], prices: Sequence[PlanPrice], currency: str) -> Plan:
    squads = row["squads"]
    if not isinstance(squads, list) or not all(isinstance(s, str) for s in squads):
        raise CatalogError("squads is not a list of strings")
    name = row["name"]
    if not isinstance(name, dict):
        raise CatalogError("name is not an object")
    return Plan(
        id=int(row["id"]),
        code=str(row["code"]),
        name=MappingProxyType({str(k): str(v) for k, v in name.items() if isinstance(v, str)}),
        availability=str(row["availability"]),
        is_trial=bool(row["is_trial"]),
        enabled=bool(row["enabled"]),
        traffic_bytes=int(row["traffic_bytes"]),
        reset_strategy=str(row["reset_strategy"]),
        device_limit=None if row["device_limit"] is None else int(row["device_limit"]),
        squads=tuple(squads),
        ext_squad=row["ext_squad"],
        panel_tag=row["panel_tag"],
        traffic_on_renew=str(row["traffic_on_renew"]),
        devices_on_renew=str(row["devices_on_renew"]),
        device_addon=DeviceAddon.from_json(row["device_addon"], currency=currency),
        broken_reason=row["broken_reason"],
        sort=int(row["sort"]),
        version=int(row["version"]),
        prices=tuple(sorted(prices, key=lambda p: (p.currency, p.days))),
    )


def _location_from_row(row: Mapping[str, Any]) -> Location:
    title = row["title"] if isinstance(row["title"], dict) else {}
    return Location(
        squad_uuid=str(row["squad_uuid"]),
        title=MappingProxyType({str(k): str(v) for k, v in title.items() if isinstance(v, str)}),
        flag=row["flag"],
        sort=int(row["sort"]),
        panel_name=str(row["panel_name"] or ""),
        members=None if row["members"] is None else int(row["members"]),
        missing_since=row["missing_since"],
    )


def build_snapshot(
    plan_rows: Iterable[Mapping[str, Any]],
    price_rows: Iterable[Mapping[str, Any]],
    location_rows: Iterable[Mapping[str, Any]],
    *,
    version: int,
    default_lang: str = DEFAULT_LANG,
    currency: str = "RUB",
) -> CatalogSnapshot:
    """Pure: rows → snapshot. Invalid rows are skipped and reported in ``problems``."""
    problems: list[str] = []
    prices: dict[int, list[PlanPrice]] = {}
    for r in price_rows:
        prices.setdefault(int(r["plan_id"]), []).append(
            PlanPrice(
                plan_id=int(r["plan_id"]),
                days=int(r["days"]),
                currency=str(r["currency"]),
                amount_minor=int(r["amount_minor"]),
                highlight=bool(r["highlight"]),
            )
        )
    built: list[Plan] = []
    for r in plan_rows:
        try:
            built.append(_plan_from_row(r, prices.get(int(r["id"]), ()), currency))
        except (CatalogError, KeyError, TypeError, ValueError) as e:
            problems.append(f"тариф #{r.get('id')}: {e}")
    built.sort(key=lambda p: (p.sort, p.id))
    locs: list[Location] = []
    for r in location_rows:
        try:
            locs.append(_location_from_row(r))
        except (KeyError, TypeError, ValueError) as e:
            problems.append(f"локация {r.get('squad_uuid')}: {e}")
    locs.sort(key=lambda loc: (loc.sort, loc.panel_name, loc.squad_uuid))
    return CatalogSnapshot(
        version=version,
        plans=tuple(built),
        locations=tuple(locs),
        problems=tuple(problems),
        default_lang=default_lang,
        _by_id=MappingProxyType({p.id: p for p in built}),
        _by_code=MappingProxyType({p.code: p for p in built}),
        _locations=MappingProxyType({loc.squad_uuid: loc for loc in locs}),
    )


async def read_rows(
    conn: AsyncConnection,
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    plan_rows = (await conn.execute(sa.select(plans))).mappings().all()
    price_rows = (await conn.execute(sa.select(plan_prices))).mappings().all()
    location_rows = (await conn.execute(sa.select(locations))).mappings().all()
    return list(plan_rows), list(price_rows), list(location_rows)


async def count_plan_subscribers(conn: AsyncConnection, plan_id: int) -> tuple[int, int]:
    """``(subscriptions of the plan that are not closed, of them with a manual squads override)``."""
    manual = subscriptions.c.overrides.has_key("squads")
    row = (
        await conn.execute(
            sa.select(
                sa.func.count(),
                sa.func.count().filter(manual),
            ).where(subscriptions.c.plan_id == plan_id, subscriptions.c.link_state != "closed")
        )
    ).one()
    return int(row[0]), int(row[1])


class CatalogService:
    """Holds the current :class:`CatalogSnapshot`; ``reload()`` swaps it atomically.

    ``currency`` — the shop currency (``CURRENCY``) used for the default currency of device addons.
    """

    def __init__(
        self,
        db: Database,
        *,
        default_lang: str = DEFAULT_LANG,
        currency: str = "RUB",
    ) -> None:
        self._db = db
        self.default_lang = default_lang
        self.currency = currency
        self._snapshot: CatalogSnapshot = EMPTY
        self._versions = itertools.count(1)
        self._lock = asyncio.Lock()
        self._listener: Any = None

    @property
    def db(self) -> Database:
        return self._db

    @property
    def snapshot(self) -> CatalogSnapshot:
        return self._snapshot

    async def load(self) -> CatalogSnapshot:
        return await self.reload()

    async def reload(self) -> CatalogSnapshot:
        """Re-read the catalog (three SELECTs in one read transaction) and swap the snapshot."""
        async with self._lock:
            started = time.perf_counter()
            async with self._db.read() as conn:
                plan_rows, price_rows, location_rows = await read_rows(conn)
            snap = build_snapshot(
                plan_rows,
                price_rows,
                location_rows,
                version=next(self._versions),
                default_lang=self.default_lang,
                currency=self.currency,
            )
            self._snapshot = snap
            if snap.problems:
                log.warning("catalog v%d: %d problem row(s)", snap.version, len(snap.problems))
            log.debug(
                "catalog v%d: %d plans, %d locations, %.1f ms",
                snap.version,
                len(snap.plans),
                len(snap.locations),
                (time.perf_counter() - started) * 1000,
            )
            return snap

    async def changed(self) -> CatalogSnapshot:
        """Reload here and tell other processes (``NOTIFY svbg_catalog``); a failed NOTIFY is only logged."""
        snap = await self.reload()
        try:
            await self._db.notify(NOTIFY_CHANNEL, str(snap.version))
        except Exception as e:  # noqa: BLE001 - other processes reload on their next start / tick
            log.warning("catalog: NOTIFY failed: %s", type(e).__name__)
        return snap

    async def listen(self) -> None:
        """Reload on ``NOTIFY svbg_catalog`` from another process (optional; a single process needs none)."""

        async def on_notify(_channel: str, _payload: str) -> None:
            try:
                await self.reload()
            except Exception:
                log.exception("catalog reload on NOTIFY failed")

        self._listener = await self._db.listen(NOTIFY_CHANNEL, on_notify)
