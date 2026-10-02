"""Review fixes of RemnawaveComponent: breaker survives a hot swap, health never strands a trial, plain HTTP
to an external address is refused by the wizard and flagged when the owner allowed it explicitly."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

import pytest

from svbg.core.component import Health, ProbeError
from svbg.core.errors.breaker import BreakerState
from svbg.remnawave.capabilities import self_test
from svbg.remnawave.component import RemnawaveComponent
from svbg.remnawave.transport import TransportConfig
from tests.fakes.remnawave import FakeRemnawave

pytestmark = pytest.mark.timeout(60)


@pytest.fixture
async def panel() -> AsyncIterator[FakeRemnawave]:
    async with FakeRemnawave() as fake:
        fake.add_internal_squad()
        yield fake


async def test_hot_swap_of_the_same_panel_keeps_the_outage(panel: FakeRemnawave) -> None:
    comp = RemnawaveComponent(close_grace=0, transport_overrides={"breaker_cooldown": 60})
    try:
        token = panel.add_token()
        await comp.reconfigure(panel.settings(token))
        comp.client.transport.breaker.trip()
        since = comp.breaker_open_since
        assert since is not None
        await comp.reconfigure(panel.settings(token, REMNAWAVE_RPS_INTERACTIVE=10))  # hot key, new session
        assert comp.client.transport.config.interactive_rps == 10
        assert comp.breaker_state is BreakerState.OPEN
        assert comp.breaker_open_since == since  # auto-maintenance's 3-minute timer is not reset
        report = await comp.health()  # the trial is due at once and the panel is fine
        assert report.status is Health.OK, report.summary
        assert comp.breaker_state is BreakerState.CLOSED and comp.breaker_open_since is None
    finally:
        await comp.aclose(grace=0)


async def test_hot_swap_to_another_panel_starts_clean(panel: FakeRemnawave) -> None:
    comp = RemnawaveComponent(close_grace=0)
    try:
        async with FakeRemnawave() as other:
            await comp.reconfigure(other.settings(other.add_token()))
            comp.client.transport.breaker.trip()
            await comp.reconfigure(panel.settings(panel.add_token()))
            assert comp.breaker_state is BreakerState.CLOSED and comp.breaker_open_since is None
    finally:
        await comp.aclose(grace=0)


async def test_health_cancelled_by_registry_timeout_does_not_strand_breaker(panel: FakeRemnawave) -> None:
    comp = RemnawaveComponent(close_grace=0, transport_overrides={"breaker_cooldown": 0.05})
    try:
        await comp.reconfigure(panel.settings(panel.add_token()))
        await asyncio.sleep(0.05)  # background capability refresh
        comp.client.transport.breaker.trip()
        await asyncio.sleep(0.06)
        panel.inject("latency", path="/system/metadata", delay=0.5)
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(0.1):  # like ComponentRegistry.health_all's limit
                await comp.health()
        await asyncio.sleep(0.6)
        assert comp.breaker_state is BreakerState.CLOSED
        assert (await comp.health()).status is Health.OK
    finally:
        await comp.aclose(grace=0)


async def test_probe_refuses_plain_http_to_external_address() -> None:
    comp = RemnawaveComponent()
    with pytest.raises(ProbeError) as info:
        await comp.probe({"REMNAWAVE_URL": "http://8.8.8.8:3000", "REMNAWAVE_TOKEN": "T"})
    assert "https://" in info.value.human
    assert info.value.fix_action == "setting:REMNAWAVE_URL"


async def test_allowed_plain_http_is_flagged_in_health_and_self_test(
    panel: FakeRemnawave, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The fake listens on loopback; pretend it is an external address the owner allowed explicitly.
    monkeypatch.setattr(TransportConfig, "plain_http_external", property(lambda _self: True))
    comp = RemnawaveComponent(close_grace=0)
    try:
        token = panel.add_token()
        await comp.reconfigure(panel.settings(token, REMNAWAVE_ALLOW_PLAIN_HTTP=True))
        report = await comp.health()
        assert report.status is Health.DEGRADED
        assert "не шифруется" in report.summary and report.details["plain_http"] is True
        assert report.fix_action == "setting:REMNAWAVE_URL"
        selftest = await self_test(comp.client, token)
        assert selftest.ok
        assert any(i.status == "warn" and "не шифруется" in i.text for i in selftest.items)
    finally:
        await comp.aclose(grace=0)
