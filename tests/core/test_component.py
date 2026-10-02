from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import pytest

from svbg.core import clock
from svbg.core.component import (
    Component,
    ComponentRegistry,
    Health,
    HealthReport,
    ProbeError,
    fix_screen,
    fix_setting,
)

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


class FakeComponent:
    def __init__(
        self,
        name: str,
        report: HealthReport | None = None,
        *,
        delay: float = 0.0,
        error: BaseException | None = None,
        result: Any = None,
    ) -> None:
        self.name = name
        self._report = report or HealthReport.ok("работает")
        self._delay = delay
        self._error = error
        self._result = result
        self.cfg: Mapping[str, Any] = {}

    async def probe(self, candidate: Mapping[str, Any]) -> None:
        if candidate.get("TOKEN") == "bad":
            raise ProbeError("Токен не подходит", "проверьте права токена", fix_action="setting:TOKEN")

    async def reconfigure(self, cfg: Mapping[str, Any]) -> None:
        self.cfg = dict(cfg)

    async def health(self) -> HealthReport:
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error is not None:
            raise self._error
        if self._result is not None:
            return self._result
        return self._report


@pytest.fixture
def frozen() -> Any:
    fc = clock.FrozenClock(T0)
    clock.set_clock(fc)
    yield fc
    clock.reset_clock()


# -- Health / HealthReport -----------------------------------------------------------------------


def test_health_rank_and_attention() -> None:
    assert Health.DOWN.rank > Health.DEGRADED.rank > Health.UNKNOWN.rank > Health.OK.rank
    assert Health.OK.rank > Health.DISABLED.rank
    assert {h for h in Health if h.needs_attention} == {Health.DEGRADED, Health.DOWN}
    assert Health("ok") is Health.OK


def test_report_defaults_use_clock(frozen: Any) -> None:
    r = HealthReport(Health.OK, "ok")
    assert r.checked_at == T0
    assert r.details == {}
    assert r.fix_action is None


def test_report_constructors() -> None:
    assert HealthReport.ok("x", latency_ms=5).details == {"latency_ms": 5}
    assert HealthReport.disabled().status is Health.DISABLED
    d = HealthReport.degraded("медленно", fix_action="screen:status", p95=900)
    assert (d.status, d.fix_action, d.details["p95"]) == (Health.DEGRADED, "screen:status", 900)
    assert HealthReport.down("нет связи", fix_action=fix_setting("REMNAWAVE_URL")).status is Health.DOWN


def test_report_is_frozen() -> None:
    r = HealthReport.ok()
    with pytest.raises(AttributeError):
        r.summary = "x"  # type: ignore[misc]


@pytest.mark.parametrize("bad", ["", "has space", "x" * 201, "line\nbreak"])
def test_report_rejects_bad_fix_action(bad: str) -> None:
    with pytest.raises(ValueError, match="fix_action"):
        HealthReport(Health.DOWN, "x", fix_action=bad)


def test_report_rejects_naive_time_and_bad_status() -> None:
    with pytest.raises(ValueError, match="aware"):
        HealthReport(Health.OK, "x", checked_at=datetime(2026, 1, 1))
    with pytest.raises(TypeError):
        HealthReport("ok", "x")  # type: ignore[arg-type]


def test_fix_helpers() -> None:
    assert fix_setting("REMNAWAVE_TOKEN") == "setting:REMNAWAVE_TOKEN"
    assert fix_screen("status") == "screen:status"
    assert fix_screen("jobs", "dead") == "screen:jobs:dead"
    with pytest.raises(ValueError):
        fix_setting("BAD KEY")


def test_probe_error() -> None:
    e = ProbeError("Панель недоступна", "проверьте адрес", fix_action="setting:REMNAWAVE_URL")
    assert e.human == "Панель недоступна"
    assert e.hint == "проверьте адрес"
    assert e.fix_action == "setting:REMNAWAVE_URL"
    assert str(e) == "Панель недоступна (проверьте адрес)"
    plain = ProbeError("Нет токена")
    assert plain.hint is None and str(plain) == "Нет токена"
    with pytest.raises(ValueError):
        ProbeError("x", fix_action="with space")


# -- Protocol & registry -------------------------------------------------------------------------


async def test_fake_satisfies_protocol() -> None:
    c = FakeComponent("remnawave")
    assert isinstance(c, Component)
    await c.reconfigure({"TOKEN": "good"})
    assert c.cfg == {"TOKEN": "good"}
    with pytest.raises(ProbeError) as ei:
        await c.probe({"TOKEN": "bad"})
    assert ei.value.fix_action == "setting:TOKEN"


def test_register_get_all_order() -> None:
    reg = ComponentRegistry()
    a, b, c = FakeComponent("bot"), FakeComponent("remnawave"), FakeComponent("payments.rollypay")
    for x in (a, b, c):
        assert reg.register(x) is x
    assert reg.all() == [a, b, c]
    assert reg.names() == ["bot", "remnawave", "payments.rollypay"]
    assert reg.get("remnawave") is b
    assert "bot" in reg and "nope" not in reg and len(reg) == 3
    assert reg.find("nope") is None
    with pytest.raises(KeyError, match="nope"):
        reg.get("nope")
    reg.unregister("bot")
    reg.unregister("bot")  # idempotent
    assert reg.names() == ["remnawave", "payments.rollypay"]


def test_register_duplicates_and_validation() -> None:
    reg = ComponentRegistry()
    a = FakeComponent("bot")
    reg.register(a)
    assert reg.register(a) is a  # same object again: no-op
    with pytest.raises(ValueError, match="already registered"):
        reg.register(FakeComponent("bot"))
    for bad in ("", "Bot", "1x", "a b", "x" * 65, "a..b"):
        with pytest.raises(ValueError, match="invalid component name"):
            reg.register(FakeComponent(bad))

    class NotComponent:
        name = "thing"

    with pytest.raises(TypeError):
        reg.register(NotComponent())  # type: ignore[arg-type]


def test_registry_rejects_bad_timeout() -> None:
    with pytest.raises(ValueError):
        ComponentRegistry(health_timeout=0)


async def test_health_all_empty() -> None:
    assert await ComponentRegistry().health_all() == {}


async def test_health_all_runs_concurrently() -> None:
    reg = ComponentRegistry()
    for i in range(5):
        reg.register(FakeComponent(f"c{i}", delay=0.2))
    started = time.perf_counter()
    reports = await reg.health_all()
    elapsed = time.perf_counter() - started
    assert list(reports) == [f"c{i}" for i in range(5)]
    assert all(r.status is Health.OK for r in reports.values())
    assert elapsed < 0.6  # sequential would be >= 1.0 s


async def test_health_all_isolates_failures_and_timeouts() -> None:
    hook_calls: list[tuple[str, BaseException]] = []

    async def hook(name: str, exc: BaseException) -> None:
        hook_calls.append((name, exc))

    reg = ComponentRegistry(health_timeout=0.1, on_health_error=hook)
    reg.register(FakeComponent("ok"))
    reg.register(FakeComponent("boom", error=RuntimeError("secret detail 123")))
    reg.register(FakeComponent("slow", delay=5))
    reg.register(FakeComponent("wrong", result={"status": "ok"}))
    reg.register(FakeComponent("deg", HealthReport.degraded("медленно", fix_action="screen:status")))

    reports = await reg.health_all()

    assert reports["ok"].status is Health.OK
    assert reports["boom"].status is Health.DOWN
    assert "RuntimeError" in reports["boom"].summary
    assert "secret detail" not in reports["boom"].summary  # raw messages never reach the owner UI
    assert reports["slow"].status is Health.UNKNOWN
    assert reports["slow"].details["timeout_s"] == 0.1
    assert reports["wrong"].status is Health.DOWN
    assert reports["deg"].fix_action == "screen:status"
    assert [n for n, _ in hook_calls] == ["boom", "wrong"]
    assert isinstance(hook_calls[1][1], TypeError)


async def test_health_hook_failure_is_swallowed() -> None:
    def bad_hook(name: str, exc: BaseException) -> None:
        raise ValueError("hook broke")

    reg = ComponentRegistry(on_health_error=bad_hook)
    reg.register(FakeComponent("boom", error=RuntimeError("x")))
    assert (await reg.health_all())["boom"].status is Health.DOWN


async def test_single_health_with_timeout_override() -> None:
    reg = ComponentRegistry(health_timeout=10)
    reg.register(FakeComponent("slow", delay=5))
    report = await reg.health("slow", limit_s=0.05)
    assert report.status is Health.UNKNOWN
    with pytest.raises(KeyError):
        await reg.health("missing")


async def test_health_all_propagates_cancellation() -> None:
    reg = ComponentRegistry()
    reg.register(FakeComponent("slow", delay=5))
    task = asyncio.create_task(reg.health_all())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
