"""Screen engine: callback codec, visibility DSL, renderer, router, forms (07 §2.4.1).

Kept import-light: ``svbg.content`` imports :mod:`svbg.tg.ui.conditions` without pulling aiogram, so names
are re-exported lazily (PEP 562).
"""

from __future__ import annotations

import importlib
from typing import Any

__all__ = [
    "CallbackCodec",
    "ScreenCtx",
    "ScreenRouter",
    "UiStateStore",
    "UserCtx",
    "View",
    "compile_condition",
    "decode",
    "encode",
]

_LAZY: dict[str, str] = {
    "CallbackCodec": "svbg.tg.ui.codec",
    "decode": "svbg.tg.ui.codec",
    "encode": "svbg.tg.ui.codec",
    "compile_condition": "svbg.tg.ui.conditions",
    "UserCtx": "svbg.tg.ui.context",
    "ScreenCtx": "svbg.tg.ui.router",
    "ScreenRouter": "svbg.tg.ui.router",
    "UiStateStore": "svbg.tg.ui.router",
    "View": "svbg.tg.ui.view",
}


def __getattr__(name: str) -> Any:
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(module), name)
