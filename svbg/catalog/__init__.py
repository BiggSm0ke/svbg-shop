"""Catalog: plans, prices, locations (panel squads), the trial plan, paid extra devices (04 §5, 02 §3.3).

* :mod:`svbg.catalog.tables` — ``plans``, ``plan_prices``, ``locations``;
* :mod:`svbg.catalog.model` — immutable :class:`Plan` / :class:`PlanPrice` / :class:`Location` /
  :class:`DeviceAddon`, availability rule, ``plan_snapshot`` format;
* :mod:`svbg.catalog.service` — :class:`CatalogService` with the in-memory snapshot (hot path without SQL);
* :mod:`svbg.catalog.repo` — validated writes with audit; :mod:`svbg.catalog.locations` — sync from the panel;
* :mod:`svbg.catalog.squads_job` — «Применить к N текущим подписчикам» (durable job through the writer);
* :mod:`svbg.catalog.preset` — the owner's plans as a preset (``seed_owner_preset``).

Names are re-exported lazily (PEP 562) so importing the package stays cheap.
"""

from __future__ import annotations

import importlib
from typing import Any

__all__ = [
    "CatalogError",
    "CatalogService",
    "CatalogSnapshot",
    "DeviceAddon",
    "Location",
    "Plan",
    "PlanPrice",
    "is_available",
    "seed_owner_preset",
]

_LAZY: dict[str, str] = {
    "CatalogError": "svbg.catalog.model",
    "DeviceAddon": "svbg.catalog.model",
    "Location": "svbg.catalog.model",
    "Plan": "svbg.catalog.model",
    "PlanPrice": "svbg.catalog.model",
    "is_available": "svbg.catalog.model",
    "CatalogService": "svbg.catalog.service",
    "CatalogSnapshot": "svbg.catalog.service",
    "seed_owner_preset": "svbg.catalog.preset",
}


def __getattr__(name: str) -> Any:
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(module), name)
