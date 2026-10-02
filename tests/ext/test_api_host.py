"""Extension API runtime: enable/disable from settings, failure isolation, breaker → degraded, gating."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import pytest

from svbg.core.bus import Event, EventBus
from svbg.core.clock import FrozenClock
from svbg.core.component import Health, HealthReport, ProbeError
from svbg.core.errors import CircuitBreaker, ErrorHub
from svbg.core.settings import SettingDef
from svbg.ext import (
    BusSub,
    ExtensionHost,
    JobDef,
    ModuleContext,
    ModuleSpec,
    ModuleState,
    Periodic,
    Perm,
    Slot,
    SlotButton,
    SlotCall,
    SlotResult,
    ViewLoader,
    enabled_setting,
)
from svbg.ext.api import DEFER_DELAY_S
from svbg.jobs.scheduler import Scheduler
from svbg.jobs.worker import RetryJob
from svbg.subscriptions.lifecycle import SubscriptionError
from svbg.tg.ui.context import UserCtx

T0 = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


class Hub:
    """Records captures like ``ErrorHub.capture`` (keyword ``module``)."""

    def __init__(self) -> None:
        self.captured: list[tuple[str, str | None, str, str]] = []

    async def capture(
        self,
        exc: BaseException,
        place: str,
        *,
        module: str | None = None,
        user_id: int | None = None,
        context: Mapping[str, Any] | None = None,
        handled: str = "",
    ) -> None:
        self.captured.append((place, module, type(exc).__name__, handled))


class Cfg:
    def __init__(self, **values: Any) -> None:
        self.values: dict[str, Any] = {"LTE_ENABLED": False, "LTE_LIMIT": 10, **values}

    def __call__(self) -> Mapping[str, Any]:
        return dict(self.values)


class Settings:
    """The part of ``SettingsService`` the host uses."""

    def __init__(self, cfg: Cfg) -> None:
        self.cfg = cfg
        self.subs: list[tuple[list[str], Any]] = []

    def current(self) -> Mapping[str, Any]:
        return self.cfg()

    def subscribe(self, keys: list[str], cb: Any) -> None:
        self.subs.append((list(keys), cb))

    async def change(self, **values: Any) -> None:
        self.cfg.values.update(values)
        for keys, cb in self.subs:
            hit = set(values) & set(keys)
            if hit:
                await cb(self.cfg(), hit)


class Recorder:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def add(self, what: str) -> None:
        self.calls.append(what)


def lte(rec: Recorder, **kw: Any) -> ModuleSpec:
    async def setup(ctx: ModuleContext) -> None:
        rec.add("setup")

    async def teardown(ctx: ModuleContext) -> None:
        rec.add("teardown")

    async def on_config(ctx: ModuleContext, cfg: Mapping[str, Any], keys: frozenset[str]) -> None:
        rec.add(f"config:{sorted(keys)}")

    base: dict[str, Any] = {
        "name": "lte",
        "title": "LTE",
        "enabled_key": "LTE_ENABLED",
        "settings": (
            enabled_setting("lte", "LTE", "Квоты LTE."),
            SettingDef("LTE_LIMIT", int, 10, "modules", "Лимит", "Лимит ГБ."),
        ),
        "setup": setup,
        "teardown": teardown,
        "on_config": on_config,
    }
    base.update(kw)
    return ModuleSpec(**base)


def breakers(clock: FrozenClock) -> Any:
    return lambda name: CircuitBreaker(name, clock=clock)


def make(
    *specs: ModuleSpec, hub: Any = None, clock: FrozenClock | None = None, **kw: Any
) -> tuple[ExtensionHost, Hub, FrozenClock]:
    hub = hub if hub is not None else Hub()
    clock = clock or FrozenClock(T0)
    host = ExtensionHost(specs, hub=hub, breaker_factory=breakers(clock), **kw)
    return host, hub, clock


def trip(host: ExtensionHost, name: str) -> None:
    for _ in range(6):
        host.context(name).breaker.record_error()


ADMIN = UserCtx(user_id=7, role="admin", perms=frozenset({"lte.view", "lte.config"}))
USER = UserCtx(user_id=8)


# ------------------------------------------------------------------------------------------- lifecycle


async def test_disabled_by_default_runs_nothing() -> None:
    rec = Recorder()
    ran: list[str] = []

    async def collect(ctx: ModuleContext) -> None:
        ran.append("collect")

    host, _, _ = make(lte(rec, tasks=(Periodic("collect", collect, every_s=60, optional=False),)))
    sched = Scheduler(None)
    host.install_tasks(sched)
    await host.start(Cfg())
    assert host.state("lte") is ModuleState.DISABLED
    await sched.tasks()["lte.collect"].fn()
    assert ran == [] and rec.calls == []
    assert (await host.health("lte")).status is Health.DISABLED


async def test_enable_reconfigure_disable_from_settings() -> None:
    rec = Recorder()
    cfg = Cfg()
    settings = Settings(cfg)
    host, _, _ = make(lte(rec))
    host.bind_settings(settings)
    assert settings.subs[0][0] == ["LTE_ENABLED", "LTE_LIMIT"]
    await host.start()
    await settings.change(LTE_ENABLED=True)
    await host.drain()
    assert host.state("lte") is ModuleState.ACTIVE
    assert host.context("lte").active
    await settings.change(LTE_LIMIT=50)
    await host.drain()
    await settings.change(LTE_ENABLED=False)
    await host.drain()
    assert host.state("lte") is ModuleState.DISABLED
    assert rec.calls == ["setup", "config:['LTE_LIMIT']", "teardown"]
    await settings.change(LTE_LIMIT=60)  # off: no reconfigure
    await host.drain()
    assert rec.calls[-1] == "teardown"


async def test_settings_click_does_not_wait_for_setup() -> None:
    gate = asyncio.Event()

    async def slow_setup(ctx: ModuleContext) -> None:
        await gate.wait()

    cfg = Cfg()
    host, _, _ = make(lte(Recorder(), setup=slow_setup))
    await host.start(cfg)
    cfg.values["LTE_ENABLED"] = True
    await asyncio.wait_for(host.on_settings(cfg(), {"LTE_ENABLED"}), 1)
    await asyncio.sleep(0)
    assert host.state("lte") is ModuleState.STARTING
    gate.set()
    await host.drain()
    assert host.state("lte") is ModuleState.ACTIVE


async def test_failed_setup_is_isolated_and_secret_free() -> None:
    rec = Recorder()

    async def bad_setup(ctx: ModuleContext) -> None:
        raise RuntimeError("REMNAWAVE_TOKEN=abc123 leaked?")

    other = ModuleSpec(name="referral", title="Рефералка", setup=lambda ctx: rec.add("ref"))
    host, hub, _ = make(lte(rec, setup=bad_setup), other)
    await host.start(Cfg(LTE_ENABLED=True))
    assert host.state("lte") is ModuleState.FAILED
    assert host.state("referral") is ModuleState.ACTIVE  # always-on module unaffected
    report = await host.health("lte")
    assert report.status is Health.DOWN
    assert "RuntimeError" in report.summary and "abc123" not in report.summary
    assert report.fix_action == "screen:status"
    assert hub.captured == [
        ("module:lte:setup", None, "RuntimeError", "модуль не запущен, остальной бот работает")
    ]
    assert not host.context("lte").breaker.degraded  # a failed start is not an error burst


async def test_restart_retries_a_failed_setup() -> None:
    attempts: list[int] = []

    async def flaky(ctx: ModuleContext) -> None:
        attempts.append(1)
        if len(attempts) == 1:
            raise ConnectionError("down")

    host, _, _ = make(lte(Recorder(), setup=flaky))
    await host.start(Cfg(LTE_ENABLED=True))
    assert host.state("lte") is ModuleState.FAILED
    await host.sync()  # still enabled: no automatic retry loop
    assert len(attempts) == 1
    assert await host.restart("lte") is ModuleState.ACTIVE
    assert len(attempts) == 2


async def test_setup_timeout_marks_failed() -> None:
    async def hang(ctx: ModuleContext) -> None:
        await asyncio.sleep(10)

    host, _, _ = make(lte(Recorder(), setup=hang), setup_timeout=0.05)
    await host.start(Cfg(LTE_ENABLED=True))
    assert host.state("lte") is ModuleState.FAILED
    assert "таймаут" in (await host.health("lte")).summary


async def test_failing_teardown_and_config_are_isolated() -> None:
    async def bad(*_a: Any) -> None:
        raise ValueError("x")

    cfg = Cfg(LTE_ENABLED=True)
    host, hub, _ = make(lte(Recorder(), teardown=bad, on_config=bad))
    await host.start(cfg)
    await host.on_settings(cfg(), {"LTE_LIMIT"})
    await host.drain()
    cfg.values["LTE_ENABLED"] = False
    await host.sync()
    assert host.state("lte") is ModuleState.DISABLED
    assert [c[0] for c in hub.captured] == ["module:lte:config", "module:lte:teardown"]


async def test_stop_tears_down_running_modules() -> None:
    rec = Recorder()
    host, _, _ = make(lte(rec))
    await host.start(Cfg(LTE_ENABLED=True))
    await host.stop()
    assert rec.calls == ["setup", "teardown"]
    assert host.state("lte") is ModuleState.STARTING  # enabled, not running


async def test_probe_only_when_candidate_enables_module() -> None:
    seen: list[Any] = []

    async def probe(ctx: ModuleContext, cand: Mapping[str, Any]) -> None:
        seen.append(cand["LTE_LIMIT"])
        if cand["LTE_LIMIT"] < 0:
            raise ProbeError("Лимит не может быть отрицательным")

    host, _, _ = make(lte(Recorder(), probe=probe))
    await host.probe("lte", {"LTE_ENABLED": False, "LTE_LIMIT": -1})
    await host.probe("lte", {"LTE_ENABLED": True, "LTE_LIMIT": 5})
    with pytest.raises(ProbeError):
        await host.probe("lte", {"LTE_ENABLED": True, "LTE_LIMIT": -1})
    assert seen == [5, -1]


# ------------------------------------------------------------------------------------- breaker / gating


async def test_task_errors_open_breaker_and_pause_optional_work() -> None:
    ran: list[str] = []

    async def boom(ctx: ModuleContext) -> None:
        raise RuntimeError("x")

    async def must(ctx: ModuleContext) -> None:
        ran.append("must")

    async def nice(ctx: ModuleContext) -> None:
        ran.append("nice")

    host, _, clock = make(
        lte(
            Recorder(),
            tasks=(
                Periodic("boom", boom, every_s=1),
                Periodic("must", must, every_s=1, optional=False),
                Periodic("nice", nice, every_s=1),
            ),
        )
    )
    sched = Scheduler(None)
    host.install_tasks(sched)
    await host.start(Cfg(LTE_ENABLED=True))
    tasks = sched.tasks()
    for _ in range(6):
        with pytest.raises(RuntimeError):
            await tasks["lte.boom"].fn()
    assert host.state("lte") is ModuleState.DEGRADED
    report = await host.health("lte")
    assert report.status is Health.DEGRADED and report.fix_action == "screen:status"
    await tasks["lte.must"].fn()
    await tasks["lte.nice"].fn()
    await tasks["lte.boom"].fn()  # optional: skipped while degraded, no error
    assert ran == ["must"]
    clock.advance(minutes=10)  # quiet → half-open: optional work may try again
    await tasks["lte.nice"].fn()
    assert ran == ["must", "nice"]
    assert host.state("lte") is ModuleState.ACTIVE


async def test_restart_closes_breaker() -> None:
    host, _, _ = make(lte(Recorder()))
    await host.start(Cfg(LTE_ENABLED=True))
    trip(host, "lte")
    assert host.state("lte") is ModuleState.DEGRADED
    assert await host.restart("lte") is ModuleState.ACTIVE


@pytest.mark.parametrize(
    ("mode", "expect_ran", "expect_retry"),
    [("skip", False, False), ("defer", False, True), ("run", True, False)],
)
async def test_job_of_disabled_module(mode: str, expect_ran: bool, expect_retry: bool) -> None:
    ran: list[int] = []

    async def handler(job: Any, ctx: Any) -> None:
        ran.append(1)

    host, _, _ = make(lte(Recorder(), jobs=(JobDef("lte.unblock", handler, when_disabled=mode),)))  # type: ignore[arg-type]
    handlers: dict[str, Any] = {}
    host.install_jobs(handlers)
    await host.start(Cfg())
    job = type("J", (), {"kind": "lte.unblock"})()
    if expect_retry:
        with pytest.raises(RetryJob) as info:
            await handlers["lte.unblock"](job, None)
        assert info.value.delay == DEFER_DELAY_S
    else:
        await handlers["lte.unblock"](job, None)
    assert bool(ran) is expect_ran


async def test_jobs_run_while_degraded_and_count_errors() -> None:
    calls: list[str] = []

    async def ok(job: Any, ctx: Any) -> None:
        calls.append("ok")

    async def bad(job: Any, ctx: Any) -> None:
        raise ValueError("x")

    async def retry(job: Any, ctx: Any) -> None:
        raise RetryJob(5)

    async def slow(job: Any, ctx: Any) -> None:
        await asyncio.sleep(1)

    host, _, _ = make(
        lte(
            Recorder(),
            jobs=(
                JobDef("lte.ok", ok),
                JobDef("lte.bad", bad),
                JobDef("lte.retry", retry),
                JobDef("lte.slow", slow, timeout_s=0.02),
            ),
        )
    )
    h: dict[str, Any] = {}
    host.install_jobs(h)
    await host.start(Cfg(LTE_ENABLED=True))
    br = host.context("lte").breaker
    with pytest.raises(RetryJob):
        await h["lte.retry"](None, None)
    assert br.errors_in_window() == 0  # an asked-for retry is not an error
    with pytest.raises(ValueError, match="x"):
        await h["lte.bad"](None, None)
    with pytest.raises(TimeoutError):
        await h["lte.slow"](None, None)
    assert br.errors_in_window() == 2
    trip(host, "lte")
    await h["lte.ok"](None, None)  # must-do work keeps running while degraded
    assert calls == ["ok"]


async def test_events_are_ignored_while_disabled() -> None:
    got: list[str] = []

    async def on_term(event: Event) -> None:
        got.append(event.name)

    async def broken(event: Event) -> None:
        raise RuntimeError("x")

    errors: list[str] = []
    bus = EventBus(on_error=lambda e, h, exc: errors.append(type(exc).__name__))
    cfg = Cfg()
    host, _, _ = make(
        lte(Recorder(), events=(BusSub("subscription.*", on_term), BusSub("trial.activated", broken)))
    )
    uninstall = host.install_bus(bus)
    await host.start(cfg)
    await bus.publish(Event("subscription.term_changed", {}))
    cfg.values["LTE_ENABLED"] = True
    await host.sync()
    await bus.publish(Event("subscription.term_changed", {}))
    await bus.publish(Event("trial.activated", {}))
    assert got == ["subscription.term_changed"]
    assert errors == ["RuntimeError"]
    assert host.context("lte").breaker.errors_in_window() == 1
    uninstall()
    await bus.publish(Event("subscription.term_changed", {}))
    assert got == ["subscription.term_changed"]


async def test_order_item_refused_while_module_off() -> None:
    done: list[Any] = []

    class Pack:
        async def fulfill(self, conn: Any, order: Any, item: Any) -> None:
            if item.get("bad"):
                raise KeyError("x")
            if item.get("refuse"):
                raise SubscriptionError("no", "Нельзя.")
            done.append(item["id"])

    class Addon:
        async def apply(self, conn: Any, order: Any, items: Any) -> int:
            return int(order["subscription_id"])

    reg: dict[str, Any] = {}
    cfg = Cfg()
    host, _, _ = make(lte(Recorder(), order_items={"lte_pack": Pack()}, order_kinds={"addon_lte": Addon()}))
    host.install_order_items(type("F", (), {"register_item": lambda self, t, hnd: reg.__setitem__(t, hnd)})())
    await host.start(cfg)
    with pytest.raises(SubscriptionError) as info:
        await reg["lte_pack"].fulfill(None, {}, {"id": 1})
    assert info.value.code == "module_off"
    kind = host.order_kind("addon_lte")
    assert kind is not None
    with pytest.raises(SubscriptionError):
        await kind.apply(None, {"subscription_id": 3}, [])
    cfg.values["LTE_ENABLED"] = True
    await host.sync()
    await reg["lte_pack"].fulfill(None, {}, {"id": 2})
    assert await kind.apply(None, {"subscription_id": 3}, []) == 3
    with pytest.raises(SubscriptionError):
        await reg["lte_pack"].fulfill(None, {}, {"id": 3, "refuse": True})
    assert host.context("lte").breaker.errors_in_window() == 0  # a refusal is not a module error
    with pytest.raises(KeyError):
        await reg["lte_pack"].fulfill(None, {}, {"id": 4, "bad": True})
    assert host.context("lte").breaker.errors_in_window() == 1
    assert done == [2]


# --------------------------------------------------------------------------------------------- slots


async def test_slots_order_perms_and_isolation() -> None:
    def first(call: SlotCall) -> SlotResult:
        return SlotResult(
            lines=(f"🛜 {call.model['gb']} ГБ",),
            buttons=(
                SlotButton("Докупить", action="lte.buy", after="connect"),
                SlotButton("Админ", action="lte.admin", perm="lte.config"),
            ),
        )

    async def boom(call: SlotCall) -> SlotResult:
        raise RuntimeError("x")

    async def slow(call: SlotCall) -> SlotResult:
        await asyncio.sleep(1)
        return SlotResult(lines=("late",))

    def wrong(call: SlotCall) -> Any:
        return ["not a result"]

    def empty(call: SlotCall) -> SlotResult | None:
        return None

    def admin_only(call: SlotCall) -> SlotResult:
        return SlotResult(lines=("admin",))

    def second(call: SlotCall) -> SlotResult:
        return SlotResult(lines=(f"view={call.view['plan']}",))

    async def load(conn: Any, user: Any, view: Mapping[str, Any]) -> dict[str, int]:
        return {"gb": 3}

    host, hub, clock = make(
        lte(
            Recorder(),
            slots=(
                Slot("subscription", "blocks", second, order=50),
                Slot("subscription", "blocks", first, order=10),
                Slot("subscription", "blocks", boom, order=20),
                Slot("subscription", "blocks", slow, order=30),
                Slot("subscription", "blocks", wrong, order=40),
                Slot("subscription", "blocks", empty, order=45),
                Slot("subscription", "blocks", admin_only, order=60, perm="lte.view"),
            ),
            views=(ViewLoader("subscription", load),),
        ),
        slot_timeout=0.05,
    )
    await host.start(Cfg(LTE_ENABLED=True))
    models = await host.load_views("subscription", None, USER, {})  # type: ignore[arg-type]
    assert models == {"lte": {"gb": 3}}
    out = await host.render_slots("subscription", "blocks", user=USER, view={"plan": "pro"}, models=models)
    assert [r.lines for r in out] == [("🛜 3 ГБ",), ("view=pro",)]
    assert [b.action for b in out[0].buttons] == ["lte.buy"]  # the admin button is hidden from a user
    places = [c[0] for c in hub.captured]
    assert places == ["module:lte:slot:subscription.blocks"] * 3
    assert {c[1] for c in hub.captured} == {"lte"}
    clock.advance(minutes=6)  # the first render's 3 errors leave the 5-minute window
    admin = await host.render_slots("subscription", "blocks", user=ADMIN, view={"plan": "pro"}, models=models)
    assert [r.lines for r in admin] == [("🛜 3 ГБ",), ("view=pro",), ("admin",)]
    assert [b.action for b in admin[0].buttons] == ["lte.buy", "lte.admin"]
    await host.render_slots("subscription", "blocks", user=ADMIN, view={"plan": "pro"}, models=models)
    assert host.state("lte") is ModuleState.DEGRADED  # 6 errors within 5 minutes: slots are off
    assert await host.render_slots("subscription", "blocks", user=ADMIN, view={"plan": "pro"}) == []


async def test_slots_hidden_when_disabled_or_degraded_and_failed_view() -> None:
    async def bad_load(conn: Any, user: Any, view: Mapping[str, Any]) -> None:
        raise RuntimeError("db")

    def render(call: SlotCall) -> SlotResult:
        return SlotResult(lines=(f"model={call.model}",))

    cfg = Cfg()
    host, hub, _ = make(
        lte(Recorder(), slots=(Slot("home", "status_lines", render),), views=(ViewLoader("home", bad_load),))
    )
    await host.start(cfg)
    assert await host.load_views("home", None, USER, {}) == {}  # type: ignore[arg-type]
    assert await host.render_slots("home", "status_lines", user=USER) == []
    cfg.values["LTE_ENABLED"] = True
    await host.sync()
    models = await host.load_views("home", None, USER, {})  # type: ignore[arg-type]
    assert models == {}
    assert hub.captured[-1][0] == "module:lte:view:home"
    out = await host.render_slots("home", "status_lines", user=USER, models=models)
    assert out[0].lines == ("model=None",)
    trip(host, "lte")
    assert await host.render_slots("home", "status_lines", user=USER) == []


# ------------------------------------------------------------------------------------------ status, ctx


async def test_overview_and_report_sections() -> None:
    async def status(ctx: ModuleContext) -> list[str]:
        return ["Последний цикл: 2 мин назад"]

    async def bad_status(ctx: ModuleContext) -> list[str]:
        raise RuntimeError("x")

    async def report(ctx: ModuleContext) -> list[str]:
        return ["Новых блоков: 1"]

    async def health(ctx: ModuleContext) -> HealthReport:
        return HealthReport.ok("Цикл идёт")

    other = ModuleSpec(name="ip_guard", title="IP Guard", status=bad_status, report=report)
    host, _, _ = make(lte(Recorder(), status=status, report=report, health=health), other)
    await host.start(Cfg(LTE_ENABLED=True))
    rows = await host.overview()
    assert [(r.name, r.state, r.health.summary, r.lines) for r in rows] == [
        ("lte", ModuleState.ACTIVE, "Цикл идёт", ("Последний цикл: 2 мин назад",)),
        ("ip_guard", ModuleState.ACTIVE, "Работает", ()),
    ]
    assert await host.report_sections() == [("LTE", ("Новых блоков: 1",)), ("IP Guard", ("Новых блоков: 1",))]


async def test_overview_bounds_a_hanging_health() -> None:
    async def hang(ctx: ModuleContext) -> HealthReport:
        await asyncio.sleep(1)
        return HealthReport.ok()

    host, _, _ = make(lte(Recorder(), health=hang), status_timeout=0.05)
    await host.start(Cfg(LTE_ENABLED=True))
    (row,) = await host.overview()
    assert row.health.status is Health.UNKNOWN


async def test_context_helpers() -> None:
    ran: list[str] = []

    async def tick(ctx: ModuleContext) -> None:
        ran.append("tick")

    host, hub, _ = make(lte(Recorder(), tasks=(Periodic("tick", tick, every_s=3600),)), deps={"db": "DB"})
    sched = Scheduler(None)
    host.install_tasks(sched)
    await host.start(Cfg(LTE_ENABLED=True, LTE_LIMIT=42))
    ctx = host.context("lte")
    assert ctx.dep("db") == "DB"
    with pytest.raises(LookupError, match="queue"):
        ctx.dep("queue")
    assert ctx.config()["LTE_LIMIT"] == 42
    assert ctx.wake("tick") is True
    assert sched.tasks()["lte.tick"].trigger.is_set()
    assert ctx.wake("nope") is False
    async with ctx.guard("collect"):
        raise RuntimeError("swallowed")
    with pytest.raises(RuntimeError):
        async with ctx.guard("collect", reraise=True):
            raise RuntimeError("again")
    await ctx.capture(ValueError("x"), "manual")
    assert [c[:3] for c in hub.captured] == [
        ("module:lte:collect", "lte", "RuntimeError"),
        ("module:lte:collect", "lte", "RuntimeError"),
        ("module:lte:manual", "lte", "ValueError"),
    ]
    assert ctx.breaker.errors_in_window() == 3


async def test_shared_hub_breaker_counts_once() -> None:
    clock = FrozenClock(T0)
    hub = ErrorHub(None, clock=clock)
    host = ExtensionHost([lte(Recorder(), perms=(Perm("lte.view", "LTE"),))], hub=hub)
    await host.start(Cfg(LTE_ENABLED=True))
    ctx = host.context("lte")
    assert ctx.breaker is hub.breaker("lte")
    for _ in range(6):
        await ctx.capture(RuntimeError("x"), "loop")
    assert hub.breakers()["lte"].value == "open"
    assert host.state("lte") is ModuleState.DEGRADED
    await hub.drain(0.5)


async def test_attach_late_hub_and_deps() -> None:
    host = ExtensionHost([lte(Recorder())])
    # the settings registry is built first (install_settings), the hub later
    hub = ErrorHub(None, clock=FrozenClock(T0))
    host.attach(hub=hub, deps={"queue": "Q"})
    await host.start(Cfg(LTE_ENABLED=True))
    ctx = host.context("lte")
    assert ctx.dep("queue") == "Q"
    assert ctx.breaker is hub.breaker("lte")
    await hub.drain(0.5)
