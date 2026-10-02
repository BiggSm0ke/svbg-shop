"""RemnawaveComponent: probe / reconfigure (hot swap) / health / bus events (02 §2.7, 03 §5.3–5.4)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from svbg.core.bus import Event, EventBus
from svbg.core.component import Component, ComponentRegistry, Health, ProbeError
from svbg.core.errors.breaker import BreakerState
from svbg.remnawave.capabilities import Support
from svbg.remnawave.component import EVENT_TOKEN, EVENT_VERSION, RemnawaveComponent
from svbg.remnawave.errors import ErrorKind, PanelNotConfiguredError, RemnawaveError
from tests.fakes.remnawave import FakeRemnawave

pytestmark = pytest.mark.timeout(60)


@pytest.fixture
async def panel() -> AsyncIterator[FakeRemnawave]:
    async with FakeRemnawave() as fake:
        fake.add_internal_squad()
        yield fake


@pytest.fixture
async def component() -> AsyncIterator[RemnawaveComponent]:
    comp = RemnawaveComponent(close_grace=5.0, probe_timeout=5.0)
    yield comp
    await comp.aclose(grace=0)


class Collector:
    def __init__(self, bus: EventBus) -> None:
        self.events: list[Event] = []
        bus.subscribe("remnawave.*", self.handle)

    async def handle(self, event: Event) -> None:
        self.events.append(event)

    def names(self) -> list[str]:
        return [e.name for e in self.events]


def test_implements_component_protocol() -> None:
    comp = RemnawaveComponent()
    assert isinstance(comp, Component)
    assert comp.name == "remnawave"
    registry = ComponentRegistry()
    assert registry.register(comp) is comp


# ------------------------------------------------------------------------------------------- probe


async def test_probe_good_candidate_does_not_touch_current(
    panel: FakeRemnawave, component: RemnawaveComponent
) -> None:
    await component.probe(panel.settings(panel.add_token()))
    assert component.current is None  # probe never swaps
    assert component.last_report is not None and component.last_report.ok
    with pytest.raises(PanelNotConfiguredError):
        _ = component.client


async def test_probe_unconfigured_is_allowed_and_half_configured_is_not(
    component: RemnawaveComponent,
) -> None:
    await component.probe({})
    with pytest.raises(ProbeError) as info:
        await component.probe({"REMNAWAVE_URL": "https://panel.example.com"})
    assert "токен" in info.value.human and info.value.fix_action == "setting:REMNAWAVE_TOKEN"
    with pytest.raises(ProbeError) as info2:
        await component.probe({"REMNAWAVE_URL": "ftp://x", "REMNAWAVE_TOKEN": "t"})
    assert info2.value.fix_action == "setting:REMNAWAVE_URL"


async def test_bad_token_keeps_the_previous_connection_working(
    panel: FakeRemnawave, component: RemnawaveComponent
) -> None:
    good = panel.settings(panel.add_token())
    await component.probe(good)
    await component.reconfigure(good)
    working = component.client
    with pytest.raises(ProbeError) as info:
        await component.probe(panel.settings("wrong-token"))
    assert info.value.human == "Панель отклонила API-токен"
    assert info.value.hint is not None and "API-токены" in info.value.hint
    assert info.value.fix_action == "setting:REMNAWAVE_TOKEN"
    assert component.client is working
    assert (await component.client.metadata()).version == "3.4.4"


async def test_probe_errors_are_human(component: RemnawaveComponent) -> None:
    async with FakeRemnawave(version="2.8.0") as old:
        with pytest.raises(ProbeError) as info:
            await component.probe(old.settings(old.add_token()))
        assert "не поддерживается" in info.value.human
    async with FakeRemnawave(production=True) as prod:
        token = prod.add_token()
        await component.probe(prod.settings(token))  # http:// gets X-Forwarded-* automatically
        no_headers = RemnawaveComponent(transport_overrides={"forwarded_headers": False})
        with pytest.raises(ProbeError) as info2:
            await no_headers.probe(prod.settings(token))
        assert info2.value.human == "Панель закрыла соединение без ответа"
        assert info2.value.hint is not None and "X-Forwarded" in info2.value.hint
        assert info2.value.fix_action == "setting:REMNAWAVE_URL"
    async with FakeRemnawave() as limited:
        with pytest.raises(ProbeError) as info3:
            await component.probe(limited.settings(limited.add_token(["system:*"])))
        assert "users:stream" in info3.value.human
        assert info3.value.fix_action == "setting:REMNAWAVE_TOKEN"


async def test_probe_timeout(panel: FakeRemnawave) -> None:
    comp = RemnawaveComponent(probe_timeout=0.3)
    panel.inject("latency", delay=2.0, times=None)
    with pytest.raises(ProbeError) as info:
        await comp.probe(panel.settings(panel.add_token()))
    assert "не ответила за 0.3 с" in info.value.human


# ------------------------------------------------------------------------------------- reconfigure


async def test_reconfigure_installs_reuses_probe_and_is_idempotent(
    panel: FakeRemnawave, component: RemnawaveComponent
) -> None:
    cfg = panel.settings(panel.add_token())
    await component.probe(cfg)
    metadata_calls = len(panel.calls("/system/metadata"))
    await component.reconfigure(cfg)
    api = component.client
    assert api.gate is not None and api.gate.support is Support.FULL  # carried over from the probe
    assert component.capabilities is not None and component.capabilities.webhooks_enabled is True
    await component.reconfigure(dict(cfg))  # same config → same client
    assert component.client is api
    await asyncio.sleep(0.05)
    assert len(panel.calls("/system/metadata")) == metadata_calls  # no extra detection was needed


async def test_reconfigure_without_probe_detects_in_background(
    panel: FakeRemnawave, component: RemnawaveComponent
) -> None:
    await component.reconfigure(panel.settings(panel.add_token()))
    for _ in range(100):
        if component.capabilities is not None:
            break
        await asyncio.sleep(0.01)
    assert component.capabilities is not None
    assert component.capabilities.gate.version == "3.4.4"


async def test_reconfigure_when_panel_is_down_does_not_fail() -> None:
    component = RemnawaveComponent(transport_overrides={"max_attempts": 1})
    async with FakeRemnawave() as gone:
        settings = gone.settings(gone.add_token())
    try:
        await component.reconfigure(settings)  # startup with a dead panel: no crash-loop
        assert component.configured
        report = await component.health()
        assert report.status in (Health.DEGRADED, Health.DOWN)
        assert report.summary.startswith("Панель не ответила на проверку")
    finally:
        await component.aclose(grace=0)


async def test_reconfigure_to_nothing_disables(panel: FakeRemnawave, component: RemnawaveComponent) -> None:
    await component.reconfigure(panel.settings(panel.add_token()))
    old = component.client
    await component.reconfigure({"REMNAWAVE_URL": None, "REMNAWAVE_TOKEN": None})
    assert component.current is None
    report = await component.health()
    assert report.status is Health.DISABLED and report.fix_action == "setting:REMNAWAVE_URL"
    for _ in range(100):
        if old.transport.closed:
            break
        await asyncio.sleep(0.01)
    assert old.transport.closed


async def test_reconfigure_rejects_invalid_config(component: RemnawaveComponent) -> None:
    with pytest.raises(RuntimeError, match="неверные настройки"):
        await component.reconfigure({"REMNAWAVE_TOKEN": "only-token"})


async def test_hot_swap_keeps_inflight_requests(component: RemnawaveComponent) -> None:
    async with FakeRemnawave() as old_panel, FakeRemnawave(version="3.4.3") as new_panel:
        await component.reconfigure(old_panel.settings(old_panel.add_token()))
        old_api = component.client
        old_panel.inject("latency", path="/nodes", delay=0.4, times=None)
        inflight = [asyncio.create_task(old_api.nodes()) for _ in range(3)]
        await asyncio.sleep(0.1)
        assert old_api.transport.inflight == 3
        await component.reconfigure(new_panel.settings(new_panel.add_token()))
        new_api = component.client
        assert new_api is not old_api
        assert (await new_api.metadata()).version == "3.4.3"  # new calls go to the new panel at once
        assert old_api.transport.closing and not old_api.transport.closed  # still finishing its requests
        assert (await old_api.metadata()).version == "3.4.4"  # a late caller holding the old client is served
        results = await asyncio.gather(*inflight)
        assert results == [[], [], []]  # nothing in flight was lost
        for _ in range(200):
            if old_api.transport.closed:
                break
            await asyncio.sleep(0.01)
        assert old_api.transport.closed
        assert len(old_panel.calls("/nodes")) == 3 and new_panel.calls("/nodes") == []


async def test_hot_swap_old_session_closed_after_grace() -> None:
    comp = RemnawaveComponent(close_grace=0.2)
    async with FakeRemnawave() as a, FakeRemnawave() as b:
        await comp.reconfigure(a.settings(a.add_token()))
        old_api = comp.client
        a.inject("latency", path="/nodes", delay=3.0, times=None)
        stuck = asyncio.create_task(old_api.nodes())
        await asyncio.sleep(0.05)
        await comp.reconfigure(b.settings(b.add_token()))
        await asyncio.sleep(0.5)
        assert old_api.transport.closed  # the grace period bounds the wait
        with pytest.raises(RemnawaveError):
            await stuck
        await comp.aclose(grace=0)


async def test_confirmed_major_unblocks_writes_without_new_session(component: RemnawaveComponent) -> None:
    async with FakeRemnawave(version="4.0.0") as panel:
        settings = panel.settings(panel.add_token())
        await component.probe(settings)
        await component.reconfigure(settings)
        api = component.client
        seeded = panel.add_user()
        assert api.gate is not None and not api.gate.writes_allowed
        await component.reconfigure({**settings, "REMNAWAVE_CONFIRMED_MAJOR": 4})
        assert component.client is api  # same session
        assert api.gate is not None and api.gate.writes_allowed
        assert await api.disable(seeded["id"]) is not None
        report = await component.health()
        assert report.status is Health.OK


# ------------------------------------------------------------------------------------------ health


async def test_health_ok_with_version_and_latency(
    panel: FakeRemnawave, component: RemnawaveComponent
) -> None:
    settings = panel.settings(panel.add_token(exp_days=200))
    await component.probe(settings)
    await component.reconfigure(settings)
    report = await component.health()
    assert report.status is Health.OK, report.summary
    assert report.summary.startswith("Панель 3.4.4 · ")
    assert report.details["version"] == "3.4.4"
    assert report.details["breaker"] == "closed"
    assert report.details["latency_ms"] is not None
    assert report.details["webhooks"] is True
    assert all("Bearer" not in str(v) for v in report.details.values())


async def test_health_down_when_breaker_open_and_recovers(panel: FakeRemnawave) -> None:
    bus = EventBus()
    seen = Collector(bus)
    comp = RemnawaveComponent(bus=bus, transport_overrides={"breaker_cooldown": 0.2, "max_attempts": 1})
    try:
        await comp.reconfigure(panel.settings(panel.add_token()))
        api = comp.client
        panel.inject("503", times=5)
        for _ in range(5):
            with pytest.raises(RemnawaveError):
                await api.nodes()
        assert comp.breaker_state is BreakerState.OPEN
        assert comp.breaker_open_since is not None
        report = await comp.health()
        assert report.status is Health.DOWN
        assert report.summary.startswith("Панель недоступна с ")
        assert report.details["breaker"] == "open"
        await asyncio.sleep(0.25)
        healed = await comp.health()  # health runs the HALF_OPEN trial itself
        assert healed.status is Health.OK, healed.summary
        assert comp.breaker_state is BreakerState.CLOSED
        await asyncio.sleep(0.05)
        assert "remnawave.breaker.opened" in seen.names()
        assert seen.names()[-1] == "remnawave.breaker.closed"
    finally:
        await comp.aclose(grace=0)


async def test_health_down_on_auth(panel: FakeRemnawave, component: RemnawaveComponent) -> None:
    await component.reconfigure(panel.settings("revoked-token"))
    await asyncio.sleep(0.05)
    report = await component.health()
    assert report.status is Health.DOWN
    assert report.fix_action == "setting:REMNAWAVE_TOKEN"


@pytest.mark.parametrize(
    ("days", "status", "fix"),
    [(200, Health.OK, None), (10, Health.OK, None), (2, Health.DEGRADED, "setting:REMNAWAVE_TOKEN")],
)
async def test_health_token_expiry(
    panel: FakeRemnawave, component: RemnawaveComponent, days: float, status: Health, fix: str | None
) -> None:
    settings = panel.settings(panel.add_token(exp_days=days))
    await component.probe(settings)
    await component.reconfigure(settings)
    report = await component.health()
    assert report.status is status
    assert report.fix_action == fix
    if days <= 14:
        assert "истекает через" in report.summary
    warning = component.token_warning()
    assert (warning is None) == (days > 14)


async def test_health_token_expired_is_down(panel: FakeRemnawave, component: RemnawaveComponent) -> None:
    token = panel.add_token(exp=datetime.now(UTC) - timedelta(hours=1))
    await component.reconfigure(panel.settings(token))
    report = await component.health()
    assert report.status is Health.DOWN and report.summary == "API-токен панели истёк"


async def test_health_unverified_major_and_unsupported(component: RemnawaveComponent) -> None:
    async with FakeRemnawave(version="4.0.0") as panel:
        settings = panel.settings(panel.add_token())
        await component.probe(settings)
        await component.reconfigure(settings)
        report = await component.health()
        assert report.status is Health.DEGRADED
        assert report.fix_action == "screen:status"
        assert report.details["writes_allowed"] is False
    async with FakeRemnawave(version="2.8.0") as old:
        await component.reconfigure(old.settings(old.add_token()))
        report2 = await component.health()
        assert report2.status is Health.DOWN and "не поддерживается" in report2.summary


async def test_health_newer_minor_is_ok_with_note(component: RemnawaveComponent) -> None:
    async with FakeRemnawave(version="3.6.1") as panel:
        settings = panel.settings(panel.add_token())
        await component.probe(settings)
        await component.reconfigure(settings)
        report = await component.health()
        assert report.status is Health.OK
        assert "новее проверенной" in report.summary


async def test_registry_health_all(panel: FakeRemnawave, component: RemnawaveComponent) -> None:
    registry = ComponentRegistry()
    registry.register(component)
    assert (await registry.health_all())["remnawave"].status is Health.DISABLED
    await component.reconfigure(panel.settings(panel.add_token()))
    assert (await registry.health_all())["remnawave"].status is Health.OK


# --------------------------------------------------------------------------------- refresh, events


async def test_refresh_publishes_version_and_token_events(panel: FakeRemnawave) -> None:
    bus = EventBus()
    seen = Collector(bus)
    comp = RemnawaveComponent(bus=bus)
    try:
        await comp.reconfigure(panel.settings(panel.add_token(exp_days=2)))
        caps = await comp.refresh()
        assert caps is not None and caps.gate.version == "3.4.4" and caps.hwid_enabled is False
        await comp.refresh()  # same version, same threshold: no duplicates
        await asyncio.sleep(0.05)
        names = seen.names()
        assert names.count(EVENT_VERSION) == 1
        assert names.count(EVENT_TOKEN) == 1
        token_event = next(e for e in seen.events if e.name == EVENT_TOKEN)
        assert token_event.payload["level"] == 3
        assert token_event.payload["severity"] == "warn"
        panel.version = "3.5.0"
        await comp.refresh()
        await asyncio.sleep(0.05)
        version_events = [e for e in seen.events if e.name == EVENT_VERSION]
        assert version_events[-1].payload["support"] == "newer_minor"
    finally:
        await comp.aclose(grace=0)


async def test_refresh_without_connection_and_on_failure(
    panel: FakeRemnawave, component: RemnawaveComponent
) -> None:
    assert await component.refresh() is None
    await component.reconfigure(panel.settings(panel.add_token()))
    panel.inject("disconnect", times=None)
    with pytest.raises(RemnawaveError) as info:
        await component.refresh()
    assert info.value.kind in (ErrorKind.PROXY_CHECK, ErrorKind.TRANSIENT)


async def test_aclose_closes_everything(panel: FakeRemnawave) -> None:
    comp = RemnawaveComponent(close_grace=30)
    await comp.reconfigure(panel.settings(panel.add_token()))
    first = comp.client
    panel.inject("latency", path="/nodes", delay=5, times=None)
    pending = asyncio.create_task(first.nodes())
    await asyncio.sleep(0.05)
    await comp.reconfigure(panel.settings(panel.add_token()))
    second = comp.client
    await comp.aclose(grace=0)
    assert first.transport.closed and second.transport.closed
    assert comp.current is None
    with pytest.raises(RemnawaveError):
        await pending


async def test_check_runs_self_test_on_the_running_connection(
    panel: FakeRemnawave, component: RemnawaveComponent
) -> None:
    assert await component.check() is None
    await component.reconfigure(panel.settings(panel.add_token(["users:*", "internal-squads:*", "system:*"])))
    report = await component.check()
    assert report is not None and report.ok
    assert component.capabilities is not None
    assert "nodes:list" in component.capabilities.missing_scopes
    health = await component.health()
    assert health.details["missing_scopes"] == [
        "external-squads:list",
        "nodes:list",
        "subscription-settings:get",
    ]


async def test_old_session_breaker_does_not_publish() -> None:
    bus = EventBus()
    seen = Collector(bus)
    comp = RemnawaveComponent(bus=bus, close_grace=0)
    try:
        async with FakeRemnawave() as a, FakeRemnawave() as b:
            await comp.reconfigure(a.settings(a.add_token()))
            old = comp.client
            await comp.reconfigure(b.settings(b.add_token()))
            old.transport.breaker.trip()
            await asyncio.sleep(0.05)
            assert seen.names() == [n for n in seen.names() if not n.startswith("remnawave.breaker")]
            comp.client.transport.breaker.trip()
            await asyncio.sleep(0.05)
            assert "remnawave.breaker.opened" in seen.names()
    finally:
        await comp.aclose(grace=0)


async def test_health_metadata_forbidden_is_unknown_version(
    panel: FakeRemnawave, component: RemnawaveComponent
) -> None:
    await component.reconfigure(panel.settings(panel.add_token(["users:*"])))
    report = await component.health()
    assert report.status is Health.OK
    assert report.details["support"] == "unknown"
    assert "system:metadata" in report.summary
