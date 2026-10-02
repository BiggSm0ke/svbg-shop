"""Public SDK for payment plugins, version 1.0-beta (07 §4.1, 04 D13).

A plugin (``svbg/payments/providers/<slug>.py`` or a third-party package) imports **only** this package::

    from svbg.sdk import (Capabilities, Checkout, ConfigModel, Manifest, PaymentIntent, PaymentProvider,
                          ProviderEvent, WebhookRequest, secret)
"""

from __future__ import annotations

from svbg.sdk.config import (
    ConfigError,
    ConfigField,
    ConfigModel,
    choice,
    flag,
    integer,
    number,
    secret,
    text,
    url,
)
from svbg.sdk.context import HttpClient, HttpResponse, KeyValue, PluginContext
from svbg.sdk.payments import (
    SDK_VERSION,
    Capabilities,
    Checkout,
    Manifest,
    MethodKind,
    PaymentIntent,
    PaymentProvider,
    PaymentState,
    Probe,
    ProviderError,
    ProviderEvent,
    ProviderStatus,
    RefundResult,
    WebhookAuth,
    WebhookIgnored,
    WebhookRejected,
    WebhookRequest,
    WebhookResponse,
    constant_time_equal,
    hmac_sha256_hex,
    parse_amount,
    parse_timestamp,
)

__all__ = [
    "SDK_VERSION",
    "Capabilities",
    "Checkout",
    "ConfigError",
    "ConfigField",
    "ConfigModel",
    "HttpClient",
    "HttpResponse",
    "KeyValue",
    "Manifest",
    "MethodKind",
    "PaymentIntent",
    "PaymentProvider",
    "PaymentState",
    "PluginContext",
    "Probe",
    "ProviderError",
    "ProviderEvent",
    "ProviderStatus",
    "RefundResult",
    "WebhookAuth",
    "WebhookIgnored",
    "WebhookRejected",
    "WebhookRequest",
    "WebhookResponse",
    "choice",
    "constant_time_equal",
    "flag",
    "hmac_sha256_hex",
    "integer",
    "number",
    "parse_amount",
    "parse_timestamp",
    "secret",
    "text",
    "url",
]
