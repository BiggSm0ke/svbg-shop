"""Auto-maintenance against the real RemnawaveComponent + fake panel and the real settings pipeline."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from svbg.core.attention import AttentionService
from svbg.core.bus import Event, EventBus
from svbg.core.component import ComponentRegistry
from svbg.core.crypto import Crypto, generate_key
from svbg.core.errors.breaker import BreakerState
from svbg.core.settings.registry import SettingDef, core_registry
from svbg.core.settings.service import Change, SettingsService
from svbg.remnawave.component import RemnawaveComponent
from svbg.remnawave.errors import RemnawaveError
from svbg.services.maintenance import ATT_AUTO, EVENT_OFF, EVENT_ON, MODE_KEY, start_service
from tests.dbkit import CountingDatabase, open_db
from tests.fakes.remnawave import FakeRemnawave


@pytest.fixture
async def db(pg_dsn: str) -> AsyncIterator[CountingDatabase]:
    async with open_db(pg_dsn) as database:
        yield database


@pytest.fixture
async def panel() -> AsyncIterator[FakeRemnawave]:
    async with FakeRemnawave() as fake:
        yield fake


class Deps:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


def registry_with_mode() -> Any:
    reg = core_registry()
    if reg.find(MODE_KEY) is None:  # the integration adds the key to the core registry
        reg.add(
            SettingDef(
                MODE_KEY,
                "enum",
                "auto",
                "system",
                "Техработы",
                "off | on | auto",
                choices=("off", "on", "auto"),
            )
        )
    return reg


async def wait_for(cond: Any, timeout: float = 5.0) -> None:
    for _ in range(int(timeout / 0.02)):
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached")


async def test_breaker_open_turns_maintenance_on_and_recovery_off(
    db: CountingDatabase, panel: FakeRemnawave, tmp_path: Path
) -> None:
    bus = EventBus()
    events: list[str] = []

    async def seen(event: Event) -> None:
        events.append(event.name)

    bus.subscribe("maintenance.*", seen)
    components = ComponentRegistry()
    comp = RemnawaveComponent(bus=bus, transport_overrides={"breaker_cooldown": 0.2, "max_attempts": 1})
    components.register(comp)
    settings = SettingsService(
        db, registry_with_mode(), Crypto([generate_key()]), components, environ={}, env_path=tmp_path / ".env"
    )
    await settings.load()
    attention = AttentionService(db, bus=bus)
    deps = Deps(settings=settings, components=components, attention=attention, bus=bus, hub=None)
    await comp.reconfigure(panel.settings(panel.add_token()))
    service = await start_service(deps, auto_after=0.3, interval=0.1)
    try:
        assert service.active is False
        # The panel stays down until the «on» state has been checked: with a one-shot fault the timer's
        # recovery probe (every 0.1 s, cooldown 0.2 s) could close the breaker before the attention item
        # is read.
        panel.inject("503", times=None)
        for _ in range(5):
            with pytest.raises(RemnawaveError):
                await comp.client.nodes()
        assert comp.breaker_state is BreakerState.OPEN
        await wait_for(lambda: service.active)
        assert service.state.reason == "auto"
        await wait_for(lambda: EVENT_ON in events)
        item = await attention.get(ATT_AUTO)
        assert item is not None and item.is_open
        assert service.active  # HALF_OPEN trials keep failing: still in maintenance, ``opened_at`` kept

        panel.clear_faults()
        # the panel is back: the timer's health check runs the HALF_OPEN trial, the breaker closes
        await wait_for(lambda: not service.active)
        assert comp.breaker_state is BreakerState.CLOSED
        await wait_for(lambda: EVENT_OFF in events)
        closed = await attention.get(ATT_AUTO)
        assert closed is not None and not closed.is_open

        # manual mode through the settings pipeline applies at once (subscription, not the timer)
        result = await settings.apply([Change(MODE_KEY, "on")], source="bot", actor_id=None)
        assert result.ok, result.rejected
        assert service.active is True and service.state.reason == "manual"
        await settings.apply([Change(MODE_KEY, "auto")], source="bot", actor_id=None)
        assert service.active is False
    finally:
        await service.stop()
        await comp.aclose(grace=0)
