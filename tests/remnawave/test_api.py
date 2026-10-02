"""Every client method against the fake panel (02 §2.2, §8.2 A/B)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import msgspec
import pytest

from svbg.remnawave.api import MUTATING_METHODS, RemnawaveApi, iso_utc
from svbg.remnawave.capabilities import Support
from svbg.remnawave.errors import ErrorKind, RemnawaveError, WriteBlockedError
from svbg.remnawave.models import FOREVER, UserStatus
from svbg.remnawave.transport import Transport, TransportConfig
from tests.fakes.remnawave import FakeRemnawave

pytestmark = pytest.mark.timeout(60)


@pytest.fixture
async def panel() -> AsyncIterator[FakeRemnawave]:
    async with FakeRemnawave() as fake:
        yield fake


@pytest.fixture
async def api(panel: FakeRemnawave) -> AsyncIterator[RemnawaveApi]:
    transport = Transport(TransportConfig(base_url=panel.url, token=panel.add_token()))
    try:
        yield RemnawaveApi(transport)
    finally:
        await transport.aclose()


def soon(days: float = 30) -> datetime:
    return datetime.now(UTC) + timedelta(days=days)


# ------------------------------------------------------------------------------------------ system


async def test_system_methods(api: RemnawaveApi, panel: FakeRemnawave) -> None:
    panel.add_user()
    panel.add_node()
    assert (await api.metadata()).version == "3.4.4"
    config = await api.configuration()
    assert config.notifications.webhook is True
    assert config.misc.sub_public_domain == "sub.example.com"
    stats = await api.stats()
    assert stats.users.total_users == 1
    assert stats.nodes.total_bytes == 123456789012


# ------------------------------------------------------------------------------------------- users


async def test_create_user_full_body_and_201(api: RemnawaveApi, panel: FakeRemnawave) -> None:
    squad = panel.add_internal_squad("NL")
    ext = panel.add_external_squad()
    expire = datetime(2030, 1, 2, 3, 4, 5, 678000, tzinfo=UTC)
    user = await api.create_user(
        username="sv_123456789",
        expire_at=expire,
        telegram_id=123456789,
        description="Иван @ivan · sv:abc",
        tag="TRIAL",
        traffic_limit_bytes=10 * 2**30,
        traffic_limit_strategy="NO_RESET",
        active_internal_squads=[squad],
        external_squad_uuid=ext,
        hwid_device_limit=3,
    )
    assert user.id == 1 and user.username == "sv_123456789"
    assert user.subscription_url == f"https://sub.example.com/{user.short_uuid}"
    assert user.expire_at == expire.replace(microsecond=678000)
    assert user.squad_uuids == [squad]
    assert user.hwid_device_limit == 3
    sent = panel.calls("/users", "POST")[-1].body
    assert sent["expireAt"] == "2030-01-02T03:04:05.678Z"
    assert sent["activeInternalSquads"] == [squad]
    assert "status" not in sent


async def test_create_user_omits_unset_fields_and_never_sends_null_device_limit(
    api: RemnawaveApi, panel: FakeRemnawave
) -> None:
    await api.create_user(username="sv_1", expire_at=soon())
    sent = panel.calls("/users", "POST")[-1].body
    assert set(sent) == {"username", "expireAt"}
    with pytest.raises(RemnawaveError) as info:
        await api.create_user(username="sv_2", expire_at=soon(), hwid_device_limit=None)  # type: ignore[arg-type]
    assert info.value.code == "CLIENT_GUARD"
    assert len(panel.calls("/users", "POST")) == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"username": "ab"},
        {"username": "x" * 37},
        {"username": "bad name"},
        {"username": "ok_name", "tag": "lower"},
        {"username": "ok_name", "active_internal_squads": []},
        {"username": "ok_name", "hwid_device_limit": -1},
        {"username": "ok_name", "traffic_limit_bytes": -5},
    ],
)
async def test_create_user_client_guards(
    api: RemnawaveApi, panel: FakeRemnawave, kwargs: dict[str, Any]
) -> None:
    with pytest.raises(RemnawaveError) as info:
        await api.create_user(expire_at=soon(), **kwargs)
    assert info.value.kind is ErrorKind.VALIDATION
    assert panel.calls("/users", "POST") == []


async def test_create_user_conflict_and_squad_errors(api: RemnawaveApi, panel: FakeRemnawave) -> None:
    panel.add_user(username="sv_taken")
    with pytest.raises(RemnawaveError) as info:
        await api.create_user(username="sv_taken", expire_at=soon())
    assert info.value.kind is ErrorKind.CONFLICT and info.value.code == "A019"
    taken_short = next(iter(panel.users.values()))["shortUuid"]
    with pytest.raises(RemnawaveError) as info2:
        await api.create_user(username="sv_new", expire_at=soon(), short_uuid=taken_short)
    assert info2.value.code == "A020" and info2.value.kind is ErrorKind.CONFLICT
    with pytest.raises(RemnawaveError) as info3:
        await api.create_user(
            username="sv_new",
            expire_at=soon(),
            active_internal_squads=["8d3c3f63-0000-4000-8000-000000000000"],
        )
    assert info3.value.kind is ErrorKind.SERVER and info3.value.code == "A018"
    with pytest.raises(RemnawaveError) as info4:
        await api.create_user(
            username="sv_new", expire_at=soon(), external_squad_uuid="8d3c3f63-0000-4000-8000-000000000001"
        )
    # A182 on a *user* method is NOT "user not found".
    assert info4.value.kind is ErrorKind.VALIDATION and info4.value.code == "A182"


async def test_update_user_absolute_and_idempotent(api: RemnawaveApi, panel: FakeRemnawave) -> None:
    squad = panel.add_internal_squad()
    seeded = panel.add_user(status="EXPIRED", expireAt=datetime.now(UTC) - timedelta(days=1))
    target = soon(10).replace(microsecond=0)
    first = await api.update_user(
        seeded["id"], expire_at=target, active_internal_squads=[squad], hwid_device_limit=None
    )
    second = await api.update_user(
        seeded["id"], expire_at=target, active_internal_squads=[squad], hwid_device_limit=None
    )
    assert first.expire_at == second.expire_at == target  # a repeat does not move the date
    assert first.status_known is UserStatus.ACTIVE  # EXPIRED + future expireAt, no status sent
    body = panel.calls("/users", "PATCH")[-1].body
    assert body["id"] == seeded["id"]
    assert body["hwidDeviceLimit"] is None  # null is allowed in PATCH (fallback limit)
    assert "status" not in body and "username" not in body


async def test_update_user_forever_and_limited_rule(api: RemnawaveApi, panel: FakeRemnawave) -> None:
    seeded = panel.add_user(status="LIMITED", trafficLimitBytes=100, usedTrafficBytes=100)
    same = await api.update_user(seeded["id"], traffic_limit_bytes=100)
    assert same.status == "LIMITED"
    more = await api.update_user(seeded["id"], traffic_limit_bytes=200, expire_at=FOREVER)
    assert more.status == "ACTIVE"
    assert more.expire_at == FOREVER


async def test_update_user_past_expire_and_guards(api: RemnawaveApi, panel: FakeRemnawave) -> None:
    seeded = panel.add_user()
    with pytest.raises(RemnawaveError) as info:
        await api.update_user(seeded["id"], expire_at=datetime.now(UTC) - timedelta(minutes=1))
    assert info.value.kind is ErrorKind.VALIDATION
    assert info.value.is_expire_in_past
    with pytest.raises(RemnawaveError):
        await api.update_user(seeded["id"])  # nothing to change
    with pytest.raises(RemnawaveError):
        await api.update_user(seeded["id"], active_internal_squads=[])  # never send []
    with pytest.raises(RemnawaveError) as missing:
        await api.update_user(99999, traffic_limit_bytes=1)
    assert missing.value.kind is ErrorKind.NOT_FOUND and missing.value.code == "A025"
    assert len(panel.calls("/users", "PATCH")) == 2


async def test_patch_survives_a_dropped_connection_once(api: RemnawaveApi, panel: FakeRemnawave) -> None:
    seeded = panel.add_user()
    panel.inject("disconnect", path="/users", method="PATCH", times=1)
    user = await api.update_user(seeded["id"], traffic_limit_bytes=5)
    assert user.traffic_limit_bytes == 5
    assert len(panel.calls("/users", "PATCH")) == 2


async def test_get_user_by_keys_and_not_found(api: RemnawaveApi, panel: FakeRemnawave) -> None:
    seeded = panel.add_user(username="sv_77", telegramId=77)
    assert (await api.get_user(seeded["id"])).telegram_id == 77
    assert (await api.get_by_short_uuid(seeded["shortUuid"])).id == seeded["id"]
    assert (await api.get_by_username("sv_77")).id == seeded["id"]
    for call in (api.get_user(4242), api.get_by_username("nobody"), api.get_by_short_uuid("nope")):
        with pytest.raises(RemnawaveError) as info:
            await call
        assert info.value.kind is ErrorKind.NOT_FOUND and info.value.code == "A063"


@pytest.mark.parametrize("bad", [0, -1, True, "1", "9b9f0a52-7d3c-4f0c-9d55-1e9bd2a1c111", 1.5, None])
async def test_user_id_guard_refuses_before_network(
    api: RemnawaveApi, panel: FakeRemnawave, bad: Any
) -> None:
    before = len(panel.requests)
    for call in (api.get_user, api.enable, api.disable, api.delete_user, api.devices, api.get_meta):
        with pytest.raises(RemnawaveError) as info:
            await call(bad)
        assert info.value.kind is ErrorKind.VALIDATION and info.value.code == "CLIENT_GUARD"
    assert len(panel.requests) == before


async def test_path_parts_are_guarded_and_quoted(api: RemnawaveApi, panel: FakeRemnawave) -> None:
    with pytest.raises(RemnawaveError):
        await api.get_by_username("../system/metadata")
    with pytest.raises(RemnawaveError):
        await api.get_by_short_uuid("")
    with pytest.raises(RemnawaveError) as info:
        await api.get_by_username("a b?c")
    assert info.value.kind is ErrorKind.NOT_FOUND
    assert panel.requests[-1].path == "/users/by-username/a b?c"


async def test_resolve(api: RemnawaveApi, panel: FakeRemnawave) -> None:
    seeded = panel.add_user(username="sv_r")
    assert (await api.resolve(username="sv_r")).id == seeded["id"]
    assert (await api.resolve(short_uuid=seeded["shortUuid"])).username == "sv_r"
    assert (await api.resolve(id=seeded["id"])).short_uuid == seeded["shortUuid"]
    with pytest.raises(RemnawaveError) as info:
        await api.resolve(username="ghost")
    assert info.value.kind is ErrorKind.NOT_FOUND
    for kwargs in ({}, {"id": 1, "username": "x"}):
        with pytest.raises(RemnawaveError) as guard:
            await api.resolve(**kwargs)
        assert guard.value.code == "CLIENT_GUARD"


async def test_stream_keyset_pagination_1200_users(api: RemnawaveApi, panel: FakeRemnawave) -> None:
    for i in range(1200):
        panel.add_user(telegramId=555 if i % 400 == 0 else None)
    pages = [page async for page in api.iter_users(500)]
    assert [len(p.users) for p in pages] == [500, 500, 200]
    cursors = [r.query.get("cursor") for r in panel.calls("/users/stream")]
    assert cursors == [None, "500", "1000"]
    assert pages[0].cursor == "500" and isinstance(pages[0].next_cursor, str)
    assert pages[-1].cursor is None and not pages[-1].has_more
    ids = [u.id for p in pages for u in p.users]
    assert ids == sorted(ids) and len(set(ids)) == 1200
    by_tg = await api.stream(size=10, telegram_id=555)
    assert len(by_tg.users) == 3
    assert panel.calls("/users/stream")[-1].query == {"size": "10", "telegramId": "555"}
    for bad in (0, 1001):
        with pytest.raises(RemnawaveError):
            await api.stream(size=bad)


async def test_enable_disable_already_is_success(api: RemnawaveApi, panel: FakeRemnawave) -> None:
    seeded = panel.add_user()
    assert await api.enable(seeded["id"]) is None  # A030
    disabled = await api.disable(seeded["id"])
    assert disabled is not None and disabled.status == "DISABLED"
    assert await api.disable(seeded["id"]) is None  # A029
    enabled = await api.enable(seeded["id"])
    assert enabled is not None and enabled.status == "ACTIVE"
    with pytest.raises(RemnawaveError) as info:
        await api.enable(31337)
    assert info.value.kind is ErrorKind.NOT_FOUND and info.value.code == "A025"


async def test_reset_traffic_and_revoke(api: RemnawaveApi, panel: FakeRemnawave) -> None:
    seeded = panel.add_user(status="LIMITED", usedTrafficBytes=500)
    reset = await api.reset_traffic(seeded["id"])
    assert (
        reset.used_traffic_bytes == 0 and reset.status == "ACTIVE" and reset.last_traffic_reset_at is not None
    )
    old_short = seeded["shortUuid"]
    keep = await api.revoke(seeded["id"], only_passwords=True)
    assert keep.short_uuid == old_short and keep.sub_revoked_at is not None
    assert panel.calls(f"/users/{seeded['id']}/actions/revoke")[-1].body == {"revokeOnlyPasswords": True}
    fresh = await api.revoke(seeded["id"])
    assert fresh.short_uuid != old_short
    assert fresh.subscription_url.endswith(fresh.short_uuid)
    assert panel.calls(f"/users/{seeded['id']}/actions/revoke")[-1].body == {}
    with pytest.raises(RemnawaveError):
        await api.revoke(seeded["id"], short_uuid="short")


async def test_delete_user_204_then_already_gone(api: RemnawaveApi, panel: FakeRemnawave) -> None:
    seeded = panel.add_user()
    assert await api.delete_user(seeded["id"]) is True
    assert seeded["id"] not in panel.users
    assert await api.delete_user(seeded["id"]) is False  # 404 after a retry is success


async def test_accessible_nodes_and_request_history(api: RemnawaveApi, panel: FakeRemnawave) -> None:
    panel.add_node("DE-1")
    seeded = panel.add_user()
    nodes = await api.accessible_nodes(seeded["id"])
    assert nodes.user_id == seeded["id"] and nodes.active_nodes[0].node_name == "DE-1"
    history = await api.request_history(seeded["id"])
    assert history.total == 1 and history.records[0].user_agent == "Happ/1.0"


async def test_bulk_methods(api: RemnawaveApi, panel: FakeRemnawave) -> None:
    squad = panel.add_internal_squad()
    users = [panel.add_user() for _ in range(3)]
    ids = [u["id"] for u in users]
    before = users[0]["expireAt"]
    assert await api.bulk_update_squads(ids, [squad]) is None
    assert all(u["activeInternalSquads"] == [squad] for u in users)
    assert await api.bulk_extend(ids, 5) is None
    assert users[0]["expireAt"] - before == timedelta(days=5)
    for call in (
        api.bulk_update_squads(list(range(1, 502)), [squad]),
        api.bulk_update_squads(ids, []),
        api.bulk_update_squads([], [squad]),
        api.bulk_extend(ids, 0),
        api.bulk_extend([0], 1),
    ):
        with pytest.raises(RemnawaveError) as info:
            await call
        assert info.value.code == "CLIENT_GUARD"


async def test_bulk_extend_is_not_retried(panel: FakeRemnawave) -> None:
    transport = Transport(TransportConfig(base_url=panel.url, token=panel.add_token()))
    api = RemnawaveApi(transport)
    try:
        seeded = panel.add_user()
        panel.inject("503", path="/users/bulk/extend-expiration-date", times=None)
        with pytest.raises(RemnawaveError) as info:
            await api.bulk_extend([seeded["id"]], 1)
        assert info.value.kind is ErrorKind.TRANSIENT
        assert len(panel.calls("/users/bulk/extend-expiration-date")) == 1
    finally:
        await transport.aclose()


async def test_hwid_devices(api: RemnawaveApi, panel: FakeRemnawave) -> None:
    seeded = panel.add_user()
    panel.add_device(seeded["id"], "h1")
    panel.add_device(seeded["id"], "h2", platform="iOS")
    devices = await api.devices(seeded["id"])
    assert devices.total == 2 and {d.hwid for d in devices.devices} == {"h1", "h2"}
    left = await api.delete_device(seeded["id"], "h1")
    assert not left.has("h1") and left.has("h2")
    with pytest.raises(RemnawaveError) as info:
        await api.delete_device(seeded["id"], "h1")
    assert info.value.kind is ErrorKind.NOT_FOUND and info.value.code == "A204"
    empty = await api.delete_all_devices(seeded["id"])
    assert empty.total == 0
    with pytest.raises(RemnawaveError):
        await api.delete_device(seeded["id"], "")


async def test_drop_connections(api: RemnawaveApi, panel: FakeRemnawave) -> None:
    assert await api.drop_connections([3, 4]) is None
    assert panel.dropped[-1] == {
        "dropBy": {"by": "userIds", "userIds": [3, 4]},
        "targetNodes": {"target": "allNodes"},
    }
    with pytest.raises(RemnawaveError):
        await api.drop_connections([])


async def test_squads_and_nodes(api: RemnawaveApi, panel: FakeRemnawave) -> None:
    squad = panel.add_internal_squad("NL")
    panel.add_user(activeInternalSquads=[squad])
    ext = panel.add_external_squad("Brand")
    panel.add_node("NL-1")
    internal = await api.internal_squads()
    assert internal[0].uuid == squad and internal[0].info.members_count == 1
    external = await api.external_squads()
    assert external[0].uuid == ext and external[0].name == "Brand"
    nodes = await api.nodes()
    assert nodes[0].name == "NL-1" and nodes[0].is_connected


async def test_subscription_settings(api: RemnawaveApi, panel: FakeRemnawave) -> None:
    settings = await api.subscription_settings()
    assert settings.hwid_enabled is False
    hwid = {"enabled": True, "fallbackDeviceLimit": 2, "maxDevicesAnnounce": None}
    patched = await api.patch_subscription_settings(settings.uuid, hwidSettings=hwid)
    assert patched.hwid_enabled is True
    with pytest.raises(RemnawaveError):
        await api.patch_subscription_settings(settings.uuid)


async def test_connection_keys_subpage_and_page_config(api: RemnawaveApi, panel: FakeRemnawave) -> None:
    seeded = panel.add_user()
    keys = await api.connection_keys(seeded["id"])
    assert keys.enabled_keys and keys.enabled_keys[0].startswith("vless://")
    panel.page_configs["7a2f5d9e-0000-4000-8000-00000000abcd"] = {
        "uuid": "7a2f5d9e-0000-4000-8000-00000000abcd",
        "name": "Default",
        "config": {"apps": [{"name": "Happ"}]},
    }
    sub = await api.subpage_config(seeded["shortUuid"], {"user-agent": "Happ/1.0"})
    assert sub.subpage_config_uuid == "7a2f5d9e-0000-4000-8000-00000000abcd" and sub.webpage_allowed
    req = panel.calls(f"/subscriptions/subpage-config/{seeded['shortUuid']}")[-1]
    assert req.method == "GET" and req.body == {"requestHeaders": {"user-agent": "Happ/1.0"}}
    page = await api.subpage_page_config("7a2f5d9e-0000-4000-8000-00000000abcd")
    assert page.config == {"apps": [{"name": "Happ"}]}
    with pytest.raises(RemnawaveError) as info:
        await api.subpage_page_config("7a2f5d9e-0000-4000-8000-00000000ffff")
    assert info.value.kind is ErrorKind.NOT_FOUND  # not a user method: any 404 is "not found"


async def test_metadata_read_merge_write(api: RemnawaveApi, panel: FakeRemnawave) -> None:
    seeded = panel.add_user()
    panel.user_metadata[seeded["id"]] = {"other_bot": {"x": 1}}
    current = await api.get_meta(seeded["id"])
    merged = {**current, "svbg": {"sub": "abc", "bot": "svbg_bot"}}
    saved = await api.put_meta(seeded["id"], merged)
    assert saved == {"other_bot": {"x": 1}, "svbg": {"sub": "abc", "bot": "svbg_bot"}}


async def test_responses_never_carry_secrets(api: RemnawaveApi, panel: FakeRemnawave) -> None:
    seeded = panel.add_user()
    user = await api.get_user(seeded["id"])
    dumped = msgspec.json.encode(user).decode() + repr(user)
    for key in ("trojanPassword", "ssPassword", "vlessUuid"):
        assert seeded[key] not in dumped


# ---------------------------------------------------------------------------------- version gate


async def test_writes_blocked_on_unverified_major() -> None:
    async with FakeRemnawave(version="4.0.0") as panel:
        transport = Transport(TransportConfig(base_url=panel.url, token=panel.add_token()))
        try:
            api = RemnawaveApi(transport)
            seeded = panel.add_user()
            assert (await api.get_user(seeded["id"])).id == seeded["id"]  # reads work
            with pytest.raises(WriteBlockedError) as info:
                await api.disable(seeded["id"])
            assert info.value.kind is ErrorKind.TRANSIENT
            assert api.gate is not None and api.gate.support is Support.UNVERIFIED_MAJOR
            assert panel.calls(f"/users/{seeded['id']}/actions/disable") == []
            confirmed = RemnawaveApi(transport, confirmed_major=4)
            assert (await confirmed.disable(seeded["id"])) is not None
        finally:
            await transport.aclose()


async def test_writes_refused_on_2x_panel() -> None:
    async with FakeRemnawave(version="2.8.0") as panel:
        transport = Transport(TransportConfig(base_url=panel.url, token=panel.add_token()))
        try:
            with pytest.raises(WriteBlockedError):
                await RemnawaveApi(transport).create_user(username="sv_1", expire_at=soon())
            assert panel.calls("/users", "POST") == []
        finally:
            await transport.aclose()


async def test_gate_detected_once_and_unknown_without_metadata_scope(panel: FakeRemnawave) -> None:
    token = panel.add_token(["users:*"])
    transport = Transport(TransportConfig(base_url=panel.url, token=token))
    try:
        api = RemnawaveApi(transport)
        seeded = panel.add_user()
        await api.update_user(seeded["id"], traffic_limit_bytes=1)
        await api.update_user(seeded["id"], traffic_limit_bytes=2)
        assert api.gate is not None and api.gate.support is Support.UNKNOWN and api.gate.writes_allowed
        assert len(panel.calls("/system/metadata")) == 1
    finally:
        await transport.aclose()


def test_iso_utc_and_mutating_list() -> None:
    assert iso_utc(datetime(2026, 1, 2, 3, 4, 5, 999999, tzinfo=UTC)) == "2026-01-02T03:04:05.999Z"
    with pytest.raises(ValueError):
        iso_utc(datetime(2026, 1, 1))
    assert {"create_user", "update_user", "delete_user", "revoke"} <= MUTATING_METHODS
    assert "get_user" not in MUTATING_METHODS
    for name in MUTATING_METHODS:
        assert callable(getattr(RemnawaveApi, name))
