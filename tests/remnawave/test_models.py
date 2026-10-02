"""Tolerant msgspec models (02 §2.6, §8.2 A «Декодер моделей»)."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import msgspec
import pytest

from svbg.remnawave.api import _decoder
from svbg.remnawave.models import (
    FOREVER,
    ConnectionKeys,
    HwidDevices,
    Metadata,
    Node,
    PanelUser,
    ResetStrategy,
    SubscriptionSettings,
    SystemConfig,
    SystemStats,
    UsersPage,
    UserStatus,
    known_status,
    known_strategy,
    to_int,
)

SECRETS = {
    "trojanPassword": "trojan-SECRET",
    "ssPassword": "ss-SECRET",
    "vlessUuid": "11111111-2222-3333-4444-555555555555",
}


def full_user(**over: Any) -> dict[str, Any]:
    user: dict[str, Any] = {
        "id": 7,
        "shortUuid": "abcDEF1234567890",
        "username": "sv_123",
        "status": "ACTIVE",
        "trafficLimitBytes": 107374182400,
        "trafficLimitStrategy": "MONTH",
        "expireAt": "2026-11-01T10:00:00.000Z",
        "telegramId": 123456789,
        "email": None,
        "description": "Иван @ivan · sv:abc",
        "tag": "TRIAL",
        "hwidDeviceLimit": None,
        "externalSquadUuid": None,
        "lastTriggeredThreshold": 0,
        "subRevokedAt": None,
        "lastTrafficResetAt": "2026-10-01T00:05:00.000Z",
        "createdAt": "2026-09-01T00:00:00.000Z",
        "updatedAt": "2026-10-01T00:00:00.000Z",
        "subscriptionUrl": "https://sub.example.com/path/abcDEF1234567890",
        "activeInternalSquads": [{"uuid": "9b9f0a52-7d3c-4f0c-9d55-1e9bd2a1c111", "name": "NL"}],
        "userTraffic": {
            "usedTrafficBytes": 77,
            "lifetimeUsedTrafficBytes": 1000,
            "onlineAt": None,
            "firstConnectedAt": "2026-09-02T00:00:00+03:00",
            "lastConnectedNodeUuid": None,
        },
        **SECRETS,
    }
    user.update(over)
    return user


def decode(tp: Any, payload: Any) -> Any:
    return _decoder(tp).decode(json.dumps({"response": payload}).encode()).response


def test_full_user_decodes_and_drops_secrets() -> None:
    user = decode(PanelUser, full_user())
    assert user.id == 7
    assert user.short_uuid == "abcDEF1234567890"
    assert user.expire_at == datetime(2026, 11, 1, 10, tzinfo=UTC)
    assert user.status_known is UserStatus.ACTIVE
    assert user.strategy_known is ResetStrategy.MONTH
    assert user.squad_uuids == ["9b9f0a52-7d3c-4f0c-9d55-1e9bd2a1c111"]
    assert user.used_traffic_bytes == 77
    # Offsets are accepted and kept aware.
    assert user.user_traffic.first_connected_at == datetime(2026, 9, 1, 21, tzinfo=UTC)
    assert user.user_traffic.first_connected_at.tzinfo is not None
    for name in ("trojan_password", "ss_password", "vless_uuid"):
        assert not hasattr(user, name)
    encoded = msgspec.json.encode(user).decode()
    for value in SECRETS.values():
        assert value not in encoded
        assert value not in repr(user)


def test_unknown_fields_are_ignored_and_missing_optional_fields_default() -> None:
    minimal = {"id": 1, "shortUuid": "s", "username": "u", "brandNewField": {"x": 1}, "anotherOne": [1, 2]}
    user = decode(PanelUser, minimal)
    assert user.expire_at is None
    assert user.active_internal_squads == []
    assert user.user_traffic.used_traffic_bytes == 0
    assert user.status == "UNKNOWN"
    assert user.status_known is None


def test_identity_fields_are_required() -> None:
    with pytest.raises(msgspec.ValidationError):
        decode(PanelUser, {"shortUuid": "s", "username": "u"})


def test_unknown_enum_values_are_kept_as_strings_and_warned(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="svbg.remnawave"):
        user = decode(PanelUser, full_user(status="ON_HOLD", trafficLimitStrategy="YEAR"))
        assert user.status == "ON_HOLD"
        assert user.status_known is None
        assert user.strategy_known is None
    assert "ON_HOLD" in caplog.text
    assert known_status("LIMITED") is UserStatus.LIMITED
    assert known_status(None) is None
    assert known_strategy("MONTH_ROLLING") is ResetStrategy.MONTH_ROLLING


@pytest.mark.parametrize(("raw", "expected"), [("1200", "1200"), (1200, "1200"), (None, None), ("", None)])
def test_next_cursor_string_number_or_null(raw: Any, expected: str | None) -> None:
    page = decode(UsersPage, {"users": [full_user()], "nextCursor": raw, "hasMore": raw is not None})
    assert page.cursor == expected
    assert len(page.users) == 1


def test_users_page_with_one_odd_user_field() -> None:
    odd = full_user(id=8, status="NEW_STATUS", extra={"nested": True})
    page = decode(UsersPage, {"users": [full_user(), odd], "nextCursor": None, "hasMore": False})
    assert [u.id for u in page.users] == [7, 8]


def test_meta_null_and_absent_in_lists() -> None:
    settings = decode(
        SubscriptionSettings, {"uuid": "u", "hwidSettings": None, "customResponseHeaders": None}
    )
    assert settings.hwid_enabled is False
    settings2 = decode(
        SubscriptionSettings,
        {
            "uuid": "u",
            "hwidSettings": {"enabled": True, "fallbackDeviceLimit": 2, "maxDevicesAnnounce": None},
        },
    )
    assert settings2.hwid_enabled is True
    assert settings2.hwid_settings is not None and settings2.hwid_settings.fallback_device_limit == 2


def test_system_models() -> None:
    meta = decode(Metadata, {"version": "3.4.4", "build": {"time": "t", "number": "1"}, "git": {"x": 1}})
    assert meta.version == "3.4.4"
    config = decode(
        SystemConfig,
        {
            "notifications": {"webhook": True, "bandwidthUsage": [80, 95], "notConnectedAfter": None},
            "misc": {"subPublicDomain": "sub.example.com/p", "shortUuidLength": 16},
            "service": {"cleanUsageHistory": False},
        },
    )
    assert config.notifications.webhook is True
    assert config.notifications.bandwidth_usage == [80, 95]
    assert config.notifications.expiration_notifications is None
    assert config.misc.sub_public_domain == "sub.example.com/p"
    stats = decode(
        SystemStats,
        {
            "users": {"statusCounts": {"ACTIVE": 3}, "totalUsers": 3},
            "nodes": {"totalOnline": 1, "totalBytesLifetime": "123456789012345"},
        },
    )
    assert stats.nodes.total_bytes == 123456789012345
    assert stats.users.status_counts == {"ACTIVE": 3}


def test_nodes_array_and_devices() -> None:
    nodes = decode(
        list[Node], [{"uuid": "n1", "name": "NL", "isConnected": True, "usersOnline": 4, "newThing": 1}]
    )
    assert nodes[0].is_connected and nodes[0].users_online == 4
    devices = decode(
        HwidDevices,
        {
            "total": 1,
            "devices": [
                {
                    "hwid": "h1",
                    "userId": 7,
                    "platform": "iOS",
                    "requestIp": "203.0.113.1",
                    "createdAt": "2026-10-01T00:00:00Z",
                }
            ],
        },
    )
    assert devices.has("h1") and not devices.has("h2")
    assert not hasattr(devices.devices[0], "request_ip")


def test_connection_keys_repr_hides_keys() -> None:
    keys = ConnectionKeys(enabled_keys=["vless://secret@host"], hidden_keys=[], disabled_keys=[])
    assert "secret" not in repr(keys)
    assert "enabled=1" in repr(keys)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, None), (5, 5), ("17", 17), (" 18 ", 18), ("1.5e3", 1500), (2.9, 2), ("abc", None), (True, None)],
)
def test_to_int(value: Any, expected: int | None) -> None:
    assert to_int(value) == expected


def test_forever_sentinel() -> None:
    assert FOREVER.year == 2099
    assert FOREVER.tzinfo is not None
    assert FOREVER - datetime(2099, 12, 30, tzinfo=timezone(timedelta(hours=0))) == timedelta(days=1)
