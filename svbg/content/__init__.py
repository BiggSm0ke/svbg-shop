"""Content: screens, buttons and media as data, and the in-memory snapshot (07 §2.4.1, §2.7).

Telegram-agnostic (no aiogram): ``svbg.tg.ui.renderer`` turns the snapshot into Telegram messages.
Names are re-exported lazily so importing the package stays cheap.
"""

from __future__ import annotations

import importlib
from typing import Any

__all__ = ["Button", "ContentSnapshot", "ContentStore", "Media", "Screen"]

_LAZY: dict[str, str] = {
    "Button": "svbg.content.model",
    "Media": "svbg.content.model",
    "Screen": "svbg.content.model",
    "ContentSnapshot": "svbg.content.store",
    "ContentStore": "svbg.content.store",
}


def __getattr__(name: str) -> Any:
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(module), name)
