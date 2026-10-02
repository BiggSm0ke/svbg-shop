"""What a plan gives a subscription (04 §5 ``plans``, 02 §3.3) — the subscription side of the catalog.

:class:`PlanTerms` is parsed from a plan snapshot (``orders.snapshot`` / ``subscriptions.plan_snapshot`` / the
catalog's plan row): squads, traffic, devices, tag and the device addon cap. The subscription service never
prices anything — prices belong to the catalog and billing; here only the *terms* are applied.

:class:`TrialPlanSource` is the only thing the trial needs from ``svbg.catalog``: "the plan marked as trial".
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Protocol, runtime_checkable

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

__all__ = ["RESET_STRATEGIES", "CatalogTrialSource", "PlanTerms", "TrialPlanSource"]

RESET_STRATEGIES: Final = ("NO_RESET", "DAY", "WEEK", "MONTH", "MONTH_ROLLING")
_TAG_MAX: Final = 16


def _int(value: Any, what: str, *, allow_none: bool = False) -> int | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"тариф: {what} должно быть целым числом")
    return value


@dataclass(frozen=True, slots=True)
class PlanTerms:
    """Panel-facing terms of one plan.

    ``device_limit``: ``None`` = panel fallback, ``0`` = no limit, ``N`` = included devices (02 §3.2).
    ``addon``: extra devices can be bought; ``max_devices`` caps the total (``None`` = no cap), as the
    catalog's ``device_addon`` says.
    """

    squads: tuple[str, ...]
    plan_id: int | None = None
    code: str | None = None
    traffic_bytes: int = 0
    reset_strategy: str = "NO_RESET"
    device_limit: int | None = None
    ext_squad: str | None = None
    panel_tag: str | None = None
    is_trial: bool = False
    addon: bool = False
    max_devices: int | None = None
    snapshot: Mapping[str, Any] = field(default_factory=dict, compare=False)

    def __post_init__(self) -> None:
        if not self.squads or any(not isinstance(s, str) or not s for s in self.squads):
            raise ValueError("тариф: нужен хотя бы один сквад")
        if self.traffic_bytes < 0:
            raise ValueError("тариф: лимит трафика не может быть отрицательным")
        if self.reset_strategy not in RESET_STRATEGIES:
            raise ValueError(f"тариф: неизвестная стратегия сброса {self.reset_strategy!r}")
        if self.device_limit is not None and self.device_limit < 0:
            raise ValueError("тариф: лимит устройств не может быть отрицательным")
        if self.max_devices is not None and self.max_devices < 0:
            raise ValueError("тариф: максимум устройств не может быть отрицательным")
        if self.panel_tag is not None and len(self.panel_tag) > _TAG_MAX:
            raise ValueError("тариф: тег панели длиннее 16 символов")

    @classmethod
    def from_snapshot(cls, snap: Mapping[str, Any] | PlanTerms | Any) -> PlanTerms:
        """Parse a catalog ``Plan`` (or its ``snapshot()``, a plan row) or an order snapshot.

        Unknown keys are kept in ``snapshot`` untouched.
        """
        if isinstance(snap, PlanTerms):
            return snap
        to_snapshot = getattr(snap, "snapshot", None)
        if not isinstance(snap, Mapping) and callable(to_snapshot):
            snap = to_snapshot()  # a catalog Plan object
        if not isinstance(snap, Mapping):
            raise TypeError("plan snapshot must be a mapping")
        squads_raw = snap.get("squads") or ()
        if isinstance(squads_raw, str) or not isinstance(squads_raw, Sequence):
            raise TypeError("тариф: squads должен быть списком")
        if "addon" in snap:  # our own snapshot (to_snapshot)
            addon = bool(snap["addon"])
            max_devices = snap.get("max_devices")
        else:  # the catalog's ``device_addon`` JSON: {price_minor, per_days, max_devices?}
            raw = snap.get("device_addon")
            addon = isinstance(raw, Mapping) and bool(raw) and raw.get("enabled", True) is not False
            max_devices = raw.get("max_devices") if addon and isinstance(raw, Mapping) else None
        plan_id = snap.get("plan_id", snap.get("id"))
        return cls(
            squads=tuple(dict.fromkeys(str(s) for s in squads_raw)),
            plan_id=_int(plan_id, "id тарифа", allow_none=True),
            code=snap.get("code"),
            traffic_bytes=_int(snap.get("traffic_bytes") or 0, "лимит трафика") or 0,
            reset_strategy=str(snap.get("reset_strategy") or "NO_RESET"),
            device_limit=_int(snap.get("device_limit"), "лимит устройств", allow_none=True),
            ext_squad=snap.get("ext_squad") or None,
            panel_tag=snap.get("panel_tag") or None,
            is_trial=bool(snap.get("is_trial", False)),
            addon=addon,
            max_devices=_int(max_devices, "максимум устройств", allow_none=True),
            snapshot=dict(snap),
        )

    def to_snapshot(self) -> dict[str, Any]:
        """What is stored in ``subscriptions.plan_snapshot`` (the catalog's keys win when present)."""
        out = dict(self.snapshot)
        out.update(
            plan_id=self.plan_id,
            code=self.code,
            squads=list(self.squads),
            traffic_bytes=self.traffic_bytes,
            reset_strategy=self.reset_strategy,
            device_limit=self.device_limit,
            ext_squad=self.ext_squad,
            panel_tag=self.panel_tag,
            is_trial=self.is_trial,
            addon=self.addon,
            max_devices=self.max_devices,
        )
        return out

    @property
    def addon_available(self) -> bool:
        """Extra devices can be bought: the addon is on, the included limit is finite and positive and the
        cap (if any) is above it (02 §4.6)."""
        if not self.addon or not self.device_limit:
            return False
        return self.max_devices is None or self.max_devices > self.device_limit

    def device_limit_with(self, extra: int) -> int | None:
        """``hwidDeviceLimit`` for ``extra`` paid devices (``None``/``0`` limits are never raised)."""
        if extra < 0:
            raise ValueError("extra devices must be >= 0")
        if not self.device_limit:
            return self.device_limit
        return self.device_limit + extra


@runtime_checkable
class TrialPlanSource(Protocol):
    """The catalog's trial plan (``plans.is_trial``), read inside the caller's transaction.

    Returns a plan row / snapshot mapping (or :class:`PlanTerms`), or ``None`` when no available trial plan
    is configured (or its squads are marked broken).
    """

    async def trial_plan(self, conn: AsyncConnection) -> Mapping[str, Any] | PlanTerms | None: ...


class CatalogTrialSource:
    """:class:`TrialPlanSource` over ``svbg.catalog.CatalogService`` (in-memory ``snapshot.trial``, no SQL).

    Duck-typed on purpose: ``svbg.catalog`` imports the subscription tables, so it is not imported here.
    """

    def __init__(self, catalog: Any) -> None:
        self._catalog = catalog

    async def trial_plan(self, conn: AsyncConnection) -> Mapping[str, Any] | None:
        plan = self._catalog.snapshot.trial
        return None if plan is None else plan.snapshot()
