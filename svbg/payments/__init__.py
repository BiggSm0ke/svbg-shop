"""Payment core (07 §4, 04 D12–D14): instances, the webhook pipeline, CAS crediting, reconciler and polling.

Plugins live in :mod:`svbg.payments.providers` and import only :mod:`svbg.sdk`.
"""

from __future__ import annotations

from svbg.payments.core import (
    VERIFY_JOB,
    ApplyResult,
    CheckoutError,
    CheckoutResult,
    OnPaid,
    Outcome,
    PaymentCore,
    PaymentRecord,
    PendingPayment,
    PermissionDeniedError,
    SpendDeniedError,
    SpendGuard,
)
from svbg.payments.poller import Poller
from svbg.payments.registry import (
    InstanceRegistry,
    InstanceSpec,
    LiveInstance,
    PaymentInstanceComponent,
    PluginRejectedError,
    ProviderCatalog,
    instance_setting_defs,
)

__all__ = [
    "VERIFY_JOB",
    "ApplyResult",
    "CheckoutError",
    "CheckoutResult",
    "InstanceRegistry",
    "InstanceSpec",
    "LiveInstance",
    "OnPaid",
    "Outcome",
    "PaymentCore",
    "PaymentInstanceComponent",
    "PaymentRecord",
    "PendingPayment",
    "PermissionDeniedError",
    "PluginRejectedError",
    "Poller",
    "ProviderCatalog",
    "SpendDeniedError",
    "SpendGuard",
    "instance_setting_defs",
]
