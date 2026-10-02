"""The module manifest: settings register cleanly (owner defaults, auto block off), topic, rights, tasks,
jobs that run even while the module is off, admin screen registration."""

from __future__ import annotations

from typing import Any

import pytest

from svbg.core.settings.registry import Apply, Registry
from svbg.core.settings.values import SettingValueError, parse
from svbg.ext.api import ExtensionHost, ModuleState
from svbg.ext.ip_guard import SPEC, runtime
from svbg.ext.ip_guard.config import K_AUTO_BLOCK, K_ENABLED, SETTINGS
from svbg.jobs.queue import Job


def test_manifest_and_settings() -> None:
    assert SPEC.name == "ip_guard" and SPEC.enabled_key == K_ENABLED
    defaults = {d.key: d.default for d in SETTINGS}
    assert defaults[K_ENABLED] is False and defaults[K_AUTO_BLOCK] is False
    assert (defaults["IP_GUARD_WARN_IPS"], defaults["IP_GUARD_BLOCK_IPS"]) == (20, 25)
    assert (defaults["IP_GUARD_MIN_SUBNETS"], defaults["IP_GUARD_CONFIRM_LIVE_IPS"]) == (10, 10)
    assert defaults["IP_GUARD_CONFIRM_CHECKS"] == 2 and defaults["IP_GUARD_WINDOW_MINUTES"] == 10
    assert all(d.apply is Apply.HOT for d in SETTINGS)
    visible = [d for d in SETTINGS if not d.advanced]
    assert len(visible) <= 14  # 05 §3.5: ~12 visible keys per module
    registry = Registry()
    host = ExtensionHost([SPEC])
    assert host.install_settings(registry) == len(SETTINGS)
    assert registry.get("IP_GUARD_IGNORE_CIDRS").type == "list[str]"
    with pytest.raises(SettingValueError):
        parse(registry.get("IP_GUARD_IGNORE_CIDRS"), "203.0.113.0/24, not-a-net")
    assert parse(registry.get("IP_GUARD_IGNORE_CIDRS"), "203.0.113.0/24, 2001:db8::/32") == [
        "203.0.113.0/24",
        "2001:db8::/32",
    ]
    with pytest.raises(SettingValueError):
        parse(registry.get("IP_GUARD_CONFIRM_CHECKS"), "9")


def test_topic_rights_tasks_jobs() -> None:
    host = ExtensionHost([SPEC])
    registered: list[Any] = []

    class Chat:
        def register_topic(self, defn: Any) -> None:
            registered.append(defn)

    assert host.install_topics(Chat()) == 1
    assert registered[0].kind == "antiabuse" and registered[0].icon == "🛡"
    assert [p.code for _, p in host.permissions()] == [
        "ip_guard.view",
        "ip_guard.block",
        "ip_guard.unblock",
        "ip_guard.config",
    ]
    handlers: dict[str, Any] = {}
    assert host.install_jobs(handlers) == 3
    assert set(handlers) == {"ip_guard.card", "ip_guard.notify", "ip_guard.drop_ips"}
    assert all(j.when_disabled == "run" for j in SPEC.jobs)  # drops and cards of earlier blocks still run
    assert {t.name for t in SPEC.tasks} == {"collect", "purge"}
    assert host.state("ip_guard") is ModuleState.DISABLED


async def test_jobs_run_while_disabled_through_the_bound_context() -> None:
    seen: list[str] = []

    class Service:
        async def card_job(self, job: Job, ctx: Any) -> None:
            seen.append(job.payload["ref"])

    host = ExtensionHost([SPEC], deps={"db": object(), "api": lambda: None})
    handlers: dict[str, Any] = {}
    host.install_jobs(handlers)

    class Router:
        def __init__(self) -> None:
            self.screens: list[str] = []
            self.actions: list[tuple[str, str]] = []

        def screen(self, code: str, **kw: Any) -> Any:
            self.screens.append(code)
            return lambda fn: fn

        def action(self, screen: str, action: str, **kw: Any) -> Any:
            self.actions.append((screen, action))
            return lambda fn: fn

    router = Router()
    assert await host.install_ui(router) == {}
    assert router.screens == ["ipguard"]
    assert ("mod", "ip_guard.unblock") in router.actions and ("ipguard", "node") in router.actions
    runtime.RUNTIME.set_service(Service())  # type: ignore[arg-type]
    from datetime import UTC, datetime

    job = Job(
        1,
        "tg_send",
        "background",
        "ip_guard.card",
        {"ref": "block:1"},
        1,
        5,
        None,
        None,
        None,
        None,
        None,
        datetime.now(UTC),
        datetime.now(UTC),
    )
    await handlers["ip_guard.card"](job, None)
    assert seen == ["block:1"]


async def test_setup_requires_db_and_api() -> None:
    host = ExtensionHost([SPEC], deps={}, config=lambda: {K_ENABLED: True})
    await host.start()
    assert host.state("ip_guard") is ModuleState.FAILED  # the bot keeps working, «Состояние» says why
