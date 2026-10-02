"""User-facing Telegram path: ``/start``, the home card, buying with the wallet, connecting, devices.

* :mod:`~svbg.tg.user.directory` — Telegram user → ``UserCtx`` (LRU); :mod:`~svbg.tg.user.start` — ``/start``;
* :mod:`~svbg.tg.user.seeds` — the user screens as content defaults; :mod:`~svbg.tg.user.render` — rendering;
* :mod:`~svbg.tg.user.wiring` — :class:`~svbg.tg.user.wiring.UserPath`, what the app builds and registers.

Names are re-exported lazily (PEP 562): importing the package must not pull the billing/payment stack or
attach the user path's tables to the shared metadata.
"""

from __future__ import annotations

import importlib
from typing import Any

__all__ = ["SUPPORTED_LANGS", "UserDirectory", "UserPath", "UserPathDeps", "build_start_router"]

_LAZY: dict[str, str] = {
    "SUPPORTED_LANGS": "svbg.tg.user.directory",
    "UserDirectory": "svbg.tg.user.directory",
    "build_start_router": "svbg.tg.user.start",
    "UserPath": "svbg.tg.user.wiring",
    "UserPathDeps": "svbg.tg.user.deps",
}


def __getattr__(name: str) -> Any:
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(module), name)
