"""Auto-maintenance: breaker OPEN > 3 min → flag, event, attention; auto-off; MAINTENANCE_MODE."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from svbg.core.attention import AttentionService
from svbg.core.bus import Event, EventBus
from svbg.core.errors.breaker import BreakerState
from svbg.services.maintenance import (
    ATT_AUTO,
    ATT_MANUAL,
    EVENT_OFF,
    EVENT_ON,
    MaintenanceService,
    Mode,
    parse_mode,
)
from tests.dbkit import CountingDatabase, open_db

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


@dataclass
class Clock:
    value: datetime = T0

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


@dataclass
class Panel:
    configured: bool = True
    breaker_state: BreakerState | None = BreakerState.CLOSED
    breaker_open_since: datetime | None = None
    health_calls: int = 0

    def down(self, since: datetime, state: BreakerState = BreakerState.OPEN) -> None:
        self.breaker_state, self.breaker_open_since = state, since

    def up(self) -> None:
        self.breaker_state, self.breaker_open_since = BreakerState.CLOSED, None

    async def health(self) -> None:
        self.health_calls += 1


@dataclass
class Settings:
    values: dict[str, Any] = field(default_factory=dict)

    def current(self) -> dict[str, Any]:
        return self.values


@dataclass
class Seen:
    events: list[Event] = field(default_factory=list)

    async def __call__(self, event: Event) -> None:
        self.events.append(event)

    def names(self) -> list[str]:
        return [e.name for e in self.events]


@pytest.fixture
async def db(pg_dsn: str) -> AsyncIterator[CountingDatabase]:
    async with open_db(pg_dsn) as database:
        yield database


@dataclass
class MEnv:
    service: MaintenanceService
    panel: Panel
    settings: Settings
    clock: Clock
    attention: AttentionService
    seen: Seen
    bus: EventBus


@pytest.fixture
async def menv(db: CountingDatabase) -> AsyncIterator[MEnv]:
    bus = EventBus()
    seen = Seen()
    bus.subscribe("maintenance.*", seen)
    panel, settings, clock = Panel(), Settings(), Clock()
    attention = AttentionService(db, bus=bus)
    service = MaintenanceService(
        settings=settings, panel=lambda: panel, attention=attention, bus=bus, clock=clock
    )
    yield MEnv(service, panel, settings, clock, attention, seen, bus)
    await service.stop()


def test_parse_mode() -> None:
    assert parse_mode("on") is Mode.ON
    assert parse_mode(" OFF ") is Mode.OFF
    assert parse_mode("auto") is Mode.AUTO
    assert parse_mode("weird") is Mode.AUTO
    assert parse_mode(None) is Mode.AUTO
    assert parse_mode(Mode.ON) is Mode.ON


async def test_auto_turns_on_after_three_minutes_and_off_on_recovery(menv: MEnv) -> None:
    s, panel, clock = menv.service, menv.panel, menv.clock
    assert (await s.tick()).active is False

    panel.down(clock.value)
    clock.advance(179)
    state = await s.tick()
    assert state.active is False
    assert state.panel_down_since == T0
    assert "включатся автоматически в 12:03 UTC" in state.text()
    assert menv.seen.names() == []

    clock.advance(1)
    state = await s.tick()
    assert state.active and state.reason == "auto" and state.since == T0
    assert s.active is True
    assert menv.seen.names() == [EVENT_ON]
    assert menv.seen.events[0].payload["reason"] == "auto"
    item = await menv.attention.get(ATT_AUTO)
    assert item is not None and item.is_open and item.severity == "error"
    assert item.fix_action == "screen:status"
    assert "12:00 UTC" in item.body

    # repeated ticks: no duplicate events, the attention item is not rewritten
    updated = item.updated_at
    for _ in range(3):
        clock.advance(15)
        await s.tick()
    assert menv.seen.names() == [EVENT_ON]
    again = await menv.attention.get(ATT_AUTO)
    assert again is not None and again.updated_at == updated

    clock.advance(60)
    panel.up()
    state = await s.tick()
    assert state.active is False
    assert menv.seen.names() == [EVENT_ON, EVENT_OFF]
    off = menv.seen.events[1].payload
    assert off["reason"] == "auto" and off["duration_s"] == pytest.approx(180 + 45 + 60)
    closed = await menv.attention.get(ATT_AUTO)
    assert closed is not None and not closed.is_open


async def test_half_open_still_counts_as_down(menv: MEnv) -> None:
    menv.panel.down(T0, BreakerState.HALF_OPEN)
    menv.clock.advance(181)
    assert (await menv.service.tick()).active is True


async def test_manual_on_and_off(menv: MEnv) -> None:
    s = menv.service
    menv.settings.values["MAINTENANCE_MODE"] = "on"
    state = await s.tick()
    assert state.active and state.reason == "manual"
    assert state.text() == "Техработы включены вручную (MAINTENANCE_MODE=on)"
    item = await menv.attention.get(ATT_MANUAL)
    assert item is not None and item.is_open and item.severity == "warn"
    assert item.fix_action == "setting:MAINTENANCE_MODE"
    since = state.since
    menv.clock.advance(30)
    assert (await s.tick()).since == since  # stable while on

    menv.settings.values["MAINTENANCE_MODE"] = "off"
    menv.panel.down(menv.clock.value - timedelta(hours=1))
    state = await s.tick()
    assert state.active is False  # off wins even with the panel down for an hour
    assert state.text() == "Техработы выключены (MAINTENANCE_MODE=off)"
    assert menv.seen.names() == [EVENT_ON, EVENT_OFF]
    gone = await menv.attention.get(ATT_MANUAL)
    assert gone is not None and not gone.is_open


async def test_auto_to_manual_switch_swaps_attention(menv: MEnv) -> None:
    menv.panel.down(T0)
    menv.clock.advance(200)
    await menv.service.tick()
    menv.settings.values["MAINTENANCE_MODE"] = "on"
    state = await menv.service.tick()
    assert state.reason == "manual"
    auto = await menv.attention.get(ATT_AUTO)
    manual = await menv.attention.get(ATT_MANUAL)
    assert auto is not None and not auto.is_open
    assert manual is not None and manual.is_open
    assert menv.seen.names() == [EVENT_ON, EVENT_ON]


async def test_unconfigured_panel_and_unknown_mode(menv: MEnv) -> None:
    menv.settings.values["MAINTENANCE_MODE"] = "sometimes"
    menv.panel.configured = False
    menv.panel.down(T0)
    menv.clock.advance(3600)
    state = await menv.service.tick()
    assert state.active is False and state.mode is Mode.AUTO


async def test_broken_panel_provider_does_not_break_the_flag(db: CountingDatabase) -> None:
    def broken() -> Panel:
        raise RuntimeError("boom")

    service = MaintenanceService(settings=Settings(), panel=broken)
    assert (await service.tick()).active is False


async def test_start_resolves_stale_items(menv: MEnv) -> None:
    await menv.attention.raise_item(ATT_AUTO, "error", "старый пункт")
    await menv.service.start()
    item = await menv.attention.get(ATT_AUTO)
    assert item is not None and not item.is_open


@dataclass
class FlakyAttention:
    fail: int = 1
    calls: list[tuple[str, str]] = field(default_factory=list)

    async def raise_item(
        self, dedup_key: str, severity: Any, title: str, body: str = "", fix_action: Any = None
    ) -> None:
        if self.fail:
            self.fail -= 1
            raise OSError("db down")
        self.calls.append(("raise", dedup_key))

    async def resolve(self, dedup_key: str) -> bool:
        self.calls.append(("resolve", dedup_key))
        return True


@dataclass
class Hub:
    captured: list[str] = field(default_factory=list)

    async def capture(self, exc: BaseException, place: str, **kw: Any) -> str | None:
        self.captured.append(place)
        return None


async def test_attention_failure_is_retried_and_reported() -> None:
    panel, clock, hub = Panel(), Clock(), Hub()
    att = FlakyAttention()
    service = MaintenanceService(
        settings=Settings(), panel=lambda: panel, attention=att, hub=hub, clock=clock
    )
    panel.down(T0)
    clock.advance(300)
    assert (await service.tick()).active is True  # the flag works although attention failed
    assert hub.captured == ["maintenance:attention"]
    assert att.calls == []
    await service.tick()
    assert ("raise", ATT_AUTO) in att.calls


async def test_breaker_event_and_settings_change_trigger_evaluation(menv: MEnv) -> None:
    menv.service.install(menv.bus)
    menv.panel.down(T0)
    menv.clock.advance(200)
    await menv.bus.publish(Event("remnawave.breaker.opened", {}))
    assert menv.service.active is True
    menv.panel.up()
    await menv.bus.publish(Event("remnawave.breaker.closed", {}))
    assert menv.service.active is False


async def test_timer_fires_at_the_three_minute_mark_and_probes_recovery() -> None:
    panel = Panel()
    service = MaintenanceService(settings=Settings(), panel=lambda: panel, auto_after=0.3, interval=5.0)
    try:
        panel.down(datetime.now(UTC))
        await service.start()
        assert service.active is False
        for _ in range(100):
            if service.active:
                break
            await asyncio.sleep(0.02)
        assert service.active is True  # woke up at the mark, not after the 5 s interval
    finally:
        await service.stop()

    probing = MaintenanceService(settings=Settings(), panel=lambda: panel, auto_after=0.0, interval=0.05)
    try:
        await probing.start()
        for _ in range(100):
            if panel.health_calls >= 2:
                break
            await asyncio.sleep(0.02)
        assert panel.health_calls >= 2  # health() runs the breaker's HALF_OPEN trial while down
        panel.up()
        for _ in range(100):
            if not probing.active:
                break
            await asyncio.sleep(0.02)
        assert probing.active is False
    finally:
        await probing.stop()


def test_invalid_arguments() -> None:
    with pytest.raises(ValueError, match="interval"):
        MaintenanceService(settings=Settings(), panel=lambda: None, interval=0)
