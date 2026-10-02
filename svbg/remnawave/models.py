"""Tolerant msgspec models of the panel responses we read (02 §2.6).

Rules:

* only fields the bot uses are declared; unknown fields are ignored, so a new panel minor that adds fields
  never breaks decoding;
* almost every field has a default, so a field the panel drops decodes as ``None`` / empty instead of
  failing the whole page of a ``users/stream`` pass (identity fields ``id``/``shortUuid``/``username`` stay
  required: a user without them is unusable);
* enums (``status``, ``trafficLimitStrategy``) are plain ``str``; :attr:`PanelUser.status_known` and
  :func:`known_status` tell whether the value is one the bot understands — an unknown status means "do not
  notify, do not automate";
* ``nextCursor`` and byte counters that may come as strings decode as ``str | int`` and are normalized;
* **secrets are never declared**: ``trojanPassword``, ``ssPassword``, ``vlessUuid`` (and the webhook's
  ``loginAttempt.password``) are dropped by the decoder, so they reach neither the database nor logs.
"""

from __future__ import annotations

import enum
import logging
from datetime import datetime
from typing import Any, Final

import msgspec

log = logging.getLogger("svbg.remnawave")

__all__ = [
    "FOREVER",
    "AccessibleNode",
    "AccessibleNodes",
    "ConnectionKeys",
    "ExternalSquad",
    "HwidDevice",
    "HwidDevices",
    "HwidSettings",
    "InternalSquad",
    "Metadata",
    "Node",
    "PanelUser",
    "RequestHistory",
    "RequestHistoryRecord",
    "ResetStrategy",
    "ResolvedUser",
    "SquadInfo",
    "SquadRef",
    "SubpageConfig",
    "SubpagePageConfig",
    "SubscriptionSettings",
    "SystemConfig",
    "SystemStats",
    "UserStatus",
    "UserTraffic",
    "UsersPage",
    "known_status",
    "known_strategy",
    "to_int",
]

#: "Forever" for the panel: year 2099 → ``expire=0`` in subscription-userinfo (02 §3.2).
FOREVER: Final = datetime.fromisoformat("2099-12-31T00:00:00+00:00")


class UserStatus(enum.StrEnum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"
    LIMITED = "LIMITED"
    EXPIRED = "EXPIRED"


class ResetStrategy(enum.StrEnum):
    NO_RESET = "NO_RESET"
    DAY = "DAY"
    WEEK = "WEEK"
    MONTH = "MONTH"
    MONTH_ROLLING = "MONTH_ROLLING"


_STATUSES: Final = frozenset(s.value for s in UserStatus)
_STRATEGIES: Final = frozenset(s.value for s in ResetStrategy)
_warned: set[tuple[str, str]] = set()


def _warn_unknown(field: str, value: str) -> None:
    key = (field, value)
    if key not in _warned and len(_warned) < 64:
        _warned.add(key)
        log.warning("remnawave: unknown %s %r — treated conservatively", field, value)


def known_status(value: str | None) -> UserStatus | None:
    """The status as an enum, or ``None`` for a value this bot version does not know."""
    if value in _STATUSES:
        return UserStatus(value)
    if value is not None:
        _warn_unknown("status", value)
    return None


def known_strategy(value: str | None) -> ResetStrategy | None:
    if value in _STRATEGIES:
        return ResetStrategy(value)
    if value is not None:
        _warn_unknown("trafficLimitStrategy", value)
    return None


def to_int(value: str | int | float | None) -> int | None:
    """Normalize a counter that may come as a number or a numeric string (``"123"``)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = value.strip()
    try:
        return int(text)
    except ValueError:
        try:
            return int(float(text))
        except ValueError:
            return None


class _Model(msgspec.Struct, kw_only=True, rename="camel", frozen=True):
    """Base: camelCase on the wire, keyword-only, immutable, unknown fields ignored."""


# ------------------------------------------------------------------------------------------------ users


class SquadRef(_Model):
    uuid: str
    name: str = ""


class UserTraffic(_Model):
    used_traffic_bytes: int = 0
    lifetime_used_traffic_bytes: int = 0
    online_at: datetime | None = None
    first_connected_at: datetime | None = None
    last_connected_node_uuid: str | None = None


class PanelUser(_Model):
    """``ExtendedUser`` of the panel without secrets (``RWC/models/extended-users.schema.ts``)."""

    id: int
    short_uuid: str
    username: str
    status: str = "UNKNOWN"
    traffic_limit_bytes: int = 0
    traffic_limit_strategy: str = "NO_RESET"
    expire_at: datetime | None = None
    telegram_id: int | None = None
    email: str | None = None
    description: str | None = None
    tag: str | None = None
    hwid_device_limit: int | None = None
    external_squad_uuid: str | None = None
    last_triggered_threshold: int = 0
    sub_revoked_at: datetime | None = None
    last_traffic_reset_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    subscription_url: str = ""
    active_internal_squads: list[SquadRef] = msgspec.field(default_factory=list)
    user_traffic: UserTraffic = msgspec.field(default_factory=UserTraffic)

    @property
    def status_known(self) -> UserStatus | None:
        return known_status(self.status)

    @property
    def strategy_known(self) -> ResetStrategy | None:
        return known_strategy(self.traffic_limit_strategy)

    @property
    def squad_uuids(self) -> list[str]:
        return [s.uuid for s in self.active_internal_squads]

    @property
    def used_traffic_bytes(self) -> int:
        return self.user_traffic.used_traffic_bytes


class UsersPage(_Model):
    """One ``users/stream`` page. ``next_cursor`` is normalized to ``str`` (the panel sends a string)."""

    users: list[PanelUser] = msgspec.field(default_factory=list)
    next_cursor: str | int | None = None
    has_more: bool = False

    @property
    def cursor(self) -> str | None:
        if self.next_cursor is None or self.next_cursor == "":
            return None
        return str(self.next_cursor)


class ResolvedUser(_Model):
    id: int
    username: str = ""
    short_uuid: str = ""


class AccessibleSquad(_Model):
    squad_name: str = ""
    active_inbounds: list[str] = msgspec.field(default_factory=list)


class AccessibleNode(_Model):
    uuid: str
    node_name: str = ""
    country_code: str = ""
    config_profile_uuid: str | None = None
    config_profile_name: str | None = None
    active_squads: list[AccessibleSquad] = msgspec.field(default_factory=list)


class AccessibleNodes(_Model):
    user_id: int = 0
    active_nodes: list[AccessibleNode] = msgspec.field(default_factory=list)


class RequestHistoryRecord(_Model):
    id: int = 0
    user_id: int = 0
    request_at: datetime | None = None
    request_ip: str | None = None
    user_agent: str | None = None


class RequestHistory(_Model):
    total: int = 0
    records: list[RequestHistoryRecord] = msgspec.field(default_factory=list)


# ------------------------------------------------------------------------------------------------- hwid


class HwidDevice(_Model):
    """A registered device. ``requestIp`` is deliberately not declared (personal data we do not need)."""

    hwid: str
    user_id: int = 0
    platform: str | None = None
    os_version: str | None = None
    device_model: str | None = None
    user_agent: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class HwidDevices(_Model):
    total: int = 0
    devices: list[HwidDevice] = msgspec.field(default_factory=list)

    def has(self, hwid: str) -> bool:
        return any(d.hwid == hwid for d in self.devices)


# ------------------------------------------------------------------------------------- squads and nodes


class SquadInfo(_Model):
    members_count: int = 0
    inbounds_count: int = 0


class HwidSettings(_Model):
    enabled: bool = False
    fallback_device_limit: int = 0
    max_devices_announce: str | None = None


class InternalSquad(_Model):
    uuid: str
    name: str = ""
    view_position: int = 0
    tags: list[str] = msgspec.field(default_factory=list)
    info: SquadInfo = msgspec.field(default_factory=SquadInfo)


class ExternalSquad(_Model):
    uuid: str
    name: str = ""
    view_position: int = 0
    tags: list[str] = msgspec.field(default_factory=list)
    info: SquadInfo = msgspec.field(default_factory=SquadInfo)
    hwid_settings: HwidSettings | None = None
    subpage_config_uuid: str | None = None


class NodeVersions(_Model):
    xray: str = ""
    node: str = ""


class Node(_Model):
    uuid: str
    id: int | None = None
    name: str = ""
    address: str = ""
    country_code: str = ""
    is_connected: bool = False
    is_disabled: bool = False
    is_connecting: bool = False
    last_status_change: datetime | None = None
    last_status_message: str | None = None
    traffic_limit_bytes: int | None = None
    traffic_used_bytes: int | None = None
    users_online: int = 0
    view_position: int = 0
    tags: list[str] = msgspec.field(default_factory=list)
    versions: NodeVersions | None = None


# ------------------------------------------------------------------------------ settings and system


class SubscriptionSettings(_Model):
    uuid: str
    hwid_settings: HwidSettings | None = None
    custom_response_headers: dict[str, str] | None = None
    serve_json_at_base_subscription: bool = False
    randomize_hosts: bool = False

    @property
    def hwid_enabled(self) -> bool:
        return bool(self.hwid_settings and self.hwid_settings.enabled)


class ConnectionKeys(_Model):
    """Ready-to-import connection URIs of a user. They ARE credentials: show to the owner/user, never log."""

    enabled_keys: list[str] = msgspec.field(default_factory=list)
    hidden_keys: list[str] = msgspec.field(default_factory=list)
    disabled_keys: list[str] = msgspec.field(default_factory=list)

    def __repr__(self) -> str:
        return (
            f"ConnectionKeys(enabled={len(self.enabled_keys)}, hidden={len(self.hidden_keys)}, "
            f"disabled={len(self.disabled_keys)})"
        )


class SubpageConfig(_Model):
    subpage_config_uuid: str | None = None
    webpage_allowed: bool = True


class SubpagePageConfig(_Model):
    uuid: str
    name: str = ""
    config: Any = None


class MetadataBuild(_Model):
    time: str = ""
    number: str = ""


class Metadata(_Model):
    """``GET /system/metadata``: the only reliable source of the panel version (02 §1.2)."""

    version: str = ""
    build: MetadataBuild = msgspec.field(default_factory=MetadataBuild)


class ConfigNotifications(_Model):
    webhook: bool = False
    bandwidth_usage: list[int] | None = None
    not_connected_after: list[int] | None = None
    expiration_notifications: list[int] | None = None


class ConfigMisc(_Model):
    short_uuid_length: int | None = None
    sub_public_domain: str = ""
    user_usage_ignore_below_bytes: int | None = None


class SystemConfig(_Model):
    """``GET /system/configuration`` — what is enabled in the panel's ``.env`` (02 §1.3, §5.7)."""

    notifications: ConfigNotifications = msgspec.field(default_factory=ConfigNotifications)
    misc: ConfigMisc = msgspec.field(default_factory=ConfigMisc)


class StatsUsers(_Model):
    status_counts: dict[str, int] = msgspec.field(default_factory=dict)
    total_users: int = 0


class StatsOnline(_Model):
    last_day: int = 0
    last_week: int = 0
    never_online: int = 0
    online_now: int = 0


class StatsNodes(_Model):
    total_online: int = 0
    total_bytes_lifetime: str | int = 0

    @property
    def total_bytes(self) -> int:
        return to_int(self.total_bytes_lifetime) or 0


class SystemStats(_Model):
    uptime: float = 0.0
    users: StatsUsers = msgspec.field(default_factory=StatsUsers)
    online_stats: StatsOnline = msgspec.field(default_factory=StatsOnline)
    nodes: StatsNodes = msgspec.field(default_factory=StatsNodes)


class UserMetadata(_Model):
    metadata: dict[str, Any] = msgspec.field(default_factory=dict)


# Internal list wrappers (the panel wraps lists in objects with a total).
class _InternalSquads(_Model):
    total: int = 0
    internal_squads: list[InternalSquad] = msgspec.field(default_factory=list)


class _ExternalSquads(_Model):
    total: int = 0
    external_squads: list[ExternalSquad] = msgspec.field(default_factory=list)
