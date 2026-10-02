"""Owner modules (``svbg.ext.<name>``) and the extension API they are built on (05 §3).

The core never imports ``svbg.ext.*``; modules import only :mod:`svbg.ext.api` and public core interfaces.
"""

from __future__ import annotations

from svbg.ext.api import (
    KNOWN_SLOTS,
    BusSub,
    ExtensionHost,
    JobDef,
    ModuleContext,
    ModuleOverview,
    ModuleSpec,
    ModuleState,
    OrderItemHandler,
    OrderKindHandler,
    Periodic,
    Perm,
    Slot,
    SlotButton,
    SlotCall,
    SlotResult,
    Topic,
    ViewLoader,
    enabled_setting,
    lazy,
    load_specs,
)

__all__ = [
    "KNOWN_SLOTS",
    "BusSub",
    "ExtensionHost",
    "JobDef",
    "ModuleContext",
    "ModuleOverview",
    "ModuleSpec",
    "ModuleState",
    "OrderItemHandler",
    "OrderKindHandler",
    "Periodic",
    "Perm",
    "Slot",
    "SlotButton",
    "SlotCall",
    "SlotResult",
    "Topic",
    "ViewLoader",
    "enabled_setting",
    "lazy",
    "load_specs",
]
