"""Remnawave panel integration: client, webhooks, runtime component (02, stage 1).

Public surface for the rest of the bot::

    from svbg.remnawave import RemnawaveComponent, RemnawaveError, ErrorKind, Lane

    api = component.client            # current RemnawaveApi (hot-swapped on settings change)
    user = await api.get_user(panel_user_id)

Mutating API methods are for ``svbg.remnawave.writer`` only (see ``api.MUTATING_METHODS``).
"""

from __future__ import annotations

from svbg.remnawave.api import MUTATING_METHODS, RemnawaveApi, iso_utc
from svbg.remnawave.capabilities import (
    Capabilities,
    SelfTestReport,
    Support,
    TokenWarning,
    VersionGate,
    gate_version,
    self_test,
    token_expires_at,
    token_warning,
)
from svbg.remnawave.component import RemnawaveComponent
from svbg.remnawave.errors import (
    ErrorKind,
    PanelNotConfiguredError,
    PanelUnavailableError,
    RemnawaveError,
    WriteBlockedError,
)
from svbg.remnawave.models import FOREVER, PanelUser, ResetStrategy, UsersPage, UserStatus
from svbg.remnawave.transport import Lane, Transport, TransportConfig, normalize_url
from svbg.remnawave.webhooks import WebhookEnvelope, WebhookParseError, parse_envelope, verify_signature

__all__ = [
    "FOREVER",
    "MUTATING_METHODS",
    "Capabilities",
    "ErrorKind",
    "Lane",
    "PanelNotConfiguredError",
    "PanelUnavailableError",
    "PanelUser",
    "RemnawaveApi",
    "RemnawaveComponent",
    "RemnawaveError",
    "ResetStrategy",
    "SelfTestReport",
    "Support",
    "TokenWarning",
    "Transport",
    "TransportConfig",
    "UserStatus",
    "UsersPage",
    "VersionGate",
    "WebhookEnvelope",
    "WebhookParseError",
    "WriteBlockedError",
    "gate_version",
    "iso_utc",
    "normalize_url",
    "parse_envelope",
    "self_test",
    "token_expires_at",
    "token_warning",
    "verify_signature",
]
