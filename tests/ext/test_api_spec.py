"""Extension API: manifests, validation, installation into the core registries (pure, no database)."""

from __future__ import annotations

import sys
from datetime import time
from pathlib import Path
from typing import Any

import pytest

from svbg.core.component import ComponentRegistry, Health
from svbg.core.settings import Apply, SettingDef, core_registry
from svbg.ext import (
    BusSub,
    ExtensionHost,
    JobDef,
    ModuleSpec,
    Periodic,
    Perm,
    Slot,
    SlotButton,
    SlotResult,
    Topic,
    ViewLoader,
    enabled_setting,
    lazy,
    load_specs,
)
from svbg.jobs.scheduler import Scheduler
from svbg.remnawave.contributors import ModuleState as SquadState
from svbg.remnawave.contributors import SquadContributors
from svbg.services.admin_chat import TopicDef
from svbg.tg.notifier import Priority


async def _noop(*_a: Any) -> None:
    return None


def _render(_call: Any) -> SlotResult:
    return SlotResult(lines=("x",))


class _Item:
    async def fulfill(self, conn: Any, order: Any, item: Any) -> None:
        return None


class _Kind:
    async def apply(self, conn: Any, order: Any, items: Any) -> int:
        return 1


def spec(name: str = "ip_guard", **kw: Any) -> ModuleSpec:
    base: dict[str, Any] = {
        "name": name,
        "title": "IP Guard",
        "enabled_key": f"{name.upper()}_ENABLED",
        "settings": (
            enabled_setting(name, "IP Guard", "Сбор IP и блоки."),
            SettingDef(f"{name.upper()}_LIMIT", int, 20, "modules", "Порог IP", "Сколько IP — уже раздача."),
        ),
    }
    base.update(kw)
    return ModuleSpec(**base)


# ------------------------------------------------------------------------------------------------ manifest


def test_enabled_setting_is_off_by_default_and_hot() -> None:
    d = enabled_setting("lte", "LTE", "Квоты LTE.")
    assert (d.key, d.default, d.apply, d.kind, d.section) == (
        "LTE_ENABLED",
        False,
        Apply.HOT,
        "bool",
        "modules",
    )
    assert d.owner_only


def test_valid_spec_and_derived_names() -> None:
    s = spec(
        topics=(Topic("antiabuse", "Антиабуз", "🛡", priority="high"),),
        perms=(Perm("ip_guard.view", "Просмотр"),),
        tasks=(Periodic("collect", _noop, every_s=60), Periodic("digest", _noop, daily_at=time(9))),
        jobs=(JobDef("ip_guard.card", _noop),),
        events=(BusSub("subscription.*", _noop),),
        slots=(Slot("subscription", "banner", _render),),
        views=(ViewLoader("subscription", _noop),),
        order_items={"ip_pack": _Item()},
        order_kinds={"addon_ip": _Kind()},
    )
    assert s.env_prefix == "IP_GUARD_"
    assert s.setting_keys == {"IP_GUARD_ENABLED", "IP_GUARD_LIMIT"}
    with pytest.raises(TypeError):
        s.order_items["x"] = _Item()  # type: ignore[index]


@pytest.mark.parametrize(
    ("kw", "match"),
    [
        ({"name": "Bad-Name"}, "invalid module name"),
        ({"title": " "}, "title is required"),
        (
            {"settings": (SettingDef("LTE_X", int, 1, "modules", "x", "y"),), "enabled_key": None},
            "must start",
        ),
        ({"enabled_key": "IP_GUARD_LIMIT"}, "enabled_key"),
        ({"enabled_key": "IP_GUARD_OTHER"}, "enabled_key"),
        ({"tasks": (Periodic("a", _noop, every_s=1), Periodic("a", _noop, every_s=2))}, "duplicate task"),
        ({"jobs": (JobDef("lte.x", _noop),)}, "must start with 'ip_guard.'"),
        ({"perms": (Perm("lte.view", "x"),)}, "must start with 'ip_guard.'"),
        ({"topics": (Topic("a", "A", "1"), Topic("a", "B", "2"))}, "duplicate topic"),
        ({"order_items": {"pack": object()}}, "no fulfill"),
        ({"order_kinds": {"addon": _Item()}}, "no apply"),
        ({"order_items": {"Bad Kind": _Item()}}, "invalid order item"),
        ({"section": ("", "x")}, "section"),
    ],
)
def test_spec_rejects_mistakes(kw: dict[str, Any], match: str) -> None:
    with pytest.raises((ValueError, TypeError), match=match):
        spec(**kw)


@pytest.mark.parametrize(
    ("factory", "match"),
    [
        (lambda: Periodic("a", _noop), "exactly one"),
        (lambda: Periodic("a", _noop, every_s=1, daily_at=time(1)), "exactly one"),
        (lambda: Periodic("a", _noop, every_s=0), "every_s"),
        (lambda: Periodic("a", _noop, every_s=1, timeout_s=0), "timeout_s"),
        (lambda: Periodic("A b", _noop, every_s=1), "task name"),
        (lambda: JobDef("nodot", _noop), "invalid job kind"),
        (lambda: JobDef("lte.x", _noop, when_disabled="drop"), "when_disabled"),  # type: ignore[arg-type]
        (lambda: BusSub("", _noop), "pattern"),
        (lambda: Topic("Bad", "x", "i"), "invalid topic kind"),
        (lambda: Topic("ok", "x", "i", priority="urgent"), "priority"),  # type: ignore[arg-type]
        (lambda: Topic("ok", "x", "i", default_fallback="dm"), "fallback"),  # type: ignore[arg-type]
        (lambda: Perm("view", "x"), "invalid permission"),
        (lambda: Perm("lte.view", " "), "title"),
        (lambda: Slot("home", "footer", _render), "unknown slot"),
        (lambda: ViewLoader("profile", _noop), "no slots"),
        (lambda: SlotButton("Go"), "exactly one"),
        (lambda: SlotButton("Go", action="a", url="https://x"), "exactly one"),
        (lambda: SlotButton("Go", url="http://x"), "https://"),
        (lambda: SlotButton(" ", action="a"), "text"),
    ],
)
def test_declarations_reject_mistakes(factory: Any, match: str) -> None:
    with pytest.raises((ValueError, TypeError), match=match):
        factory()


def test_host_rejects_conflicts_between_modules() -> None:
    with pytest.raises(ValueError, match="registered twice"):
        ExtensionHost([spec(), spec()])
    t = (Topic("antiabuse", "A", "🛡"),)
    with pytest.raises(ValueError, match="already used by ip_guard"):
        ExtensionHost([spec(topics=t), spec("lte", title="LTE", topics=t)])
    with pytest.raises(ValueError, match="already handled by ip_guard"):
        ExtensionHost(
            [spec(order_items={"pack": _Item()}), spec("lte", title="LTE", order_items={"pack": _Item()})]
        )


# --------------------------------------------------------------------------------------------------- lazy


async def test_lazy_imports_on_first_call_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pkg = tmp_path / "svbg_lazy_probe"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        "CALLS = []\nasync def run(x):\n    CALLS.append(x)\n    return x * 2\n"
        "def plain(x):\n    return x + 1\nVALUE = 5\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    fn = lazy("svbg_lazy_probe:run")
    assert "svbg_lazy_probe" not in sys.modules
    assert await fn(21) == 42
    assert await lazy("svbg_lazy_probe:plain")(1) == 2
    assert sys.modules["svbg_lazy_probe"].CALLS == [21]
    with pytest.raises(TypeError, match="not callable"):
        await lazy("svbg_lazy_probe:VALUE")()
    with pytest.raises(ValueError, match=r"package\.module:attr"):
        lazy("no_colon")


async def test_lazy_missing_module_raises_on_call_not_on_declaration() -> None:
    fn = lazy("svbg_absent_module_xyz:run")
    with pytest.raises(ModuleNotFoundError):
        await fn()


def test_load_specs_isolates_broken_modules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name, body in {
        "svbg_ext_good": ("from svbg.ext import ModuleSpec\nSPEC = ModuleSpec(name='good', title='Good')\n"),
        "svbg_ext_fn": (
            "from svbg.ext import ModuleSpec\ndef spec():\n    return ModuleSpec(name='fn', title='Fn')\n"
        ),
        "svbg_ext_nospec": "X = 1\n",
        "svbg_ext_boom": "raise RuntimeError('token=secret123')\n",
        "svbg_ext_dup": "from svbg.ext import ModuleSpec\nSPEC = ModuleSpec(name='good', title='Again')\n",
    }.items():
        (tmp_path / f"{name}.py").write_text(body, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    specs, errors = load_specs(
        [
            "svbg_ext_good",
            "svbg_ext_fn",
            "svbg_ext_nospec",
            "svbg_ext_boom",
            "svbg_ext_absent",
            "svbg_ext_dup",
        ]
    )
    assert [s.name for s in specs] == ["good", "fn"]
    assert errors == {
        "svbg_ext_nospec": "нет манифеста SPEC",
        "svbg_ext_boom": "ошибка импорта (RuntimeError)",
        "svbg_ext_absent": "модуль не установлен",
        "svbg_ext_dup": "модуль good уже загружен",
    }
    assert "secret123" not in repr(errors)


# ------------------------------------------------------------------------------------------- installation


def test_install_settings_adds_keys_and_section() -> None:
    reg = core_registry()
    before = len(reg)
    host = ExtensionHost([spec(section=("ip_guard", "IP Guard")), spec("lte", title="LTE", enabled_key=None)])
    assert host.install_settings(reg) == 4
    assert len(reg) == before + 4
    assert reg.get("IP_GUARD_ENABLED").default is False
    assert ("ip_guard", "IP Guard") in reg.sections


def test_install_settings_never_overrides_core_keys() -> None:
    reg = core_registry()
    clash = SettingDef("REFERRAL_MODE", str, "x", "referral", "t", "d")
    if "REFERRAL_MODE" not in reg:  # the core may not have it yet: make one up front
        reg.add(clash)
    host = ExtensionHost([ModuleSpec(name="referral", title="Рефералка", settings=(clash,))])
    with pytest.raises(ValueError, match="already used"):
        host.install_settings(reg)


def test_install_topics_builds_core_topic_defs() -> None:
    got: list[TopicDef] = []

    class Sink:
        def register_topic(self, defn: TopicDef) -> None:
            got.append(defn)

    host = ExtensionHost(
        [spec(topics=(Topic("antiabuse", "Антиабуз", "🛡", priority="high", noun=("a", "b", "c")),))]
    )
    assert host.install_topics(Sink()) == 1
    (t,) = got
    assert (t.kind, t.title, t.icon, t.priority, t.owner_module, t.noun) == (
        "antiabuse",
        "Антиабуз",
        "🛡",
        Priority.HIGH,
        "ip_guard",
        ("a", "b", "c"),
    )


def test_install_tasks_jobs_and_permissions() -> None:
    host = ExtensionHost(
        [
            spec(
                perms=(Perm("ip_guard.view", "Просмотр"), Perm("ip_guard.unblock", "Разблокировка")),
                tasks=(Periodic("collect", _noop, every_s=60), Periodic("digest", _noop, daily_at=time(9))),
                jobs=(JobDef("ip_guard.card", _noop),),
            ),
            spec("lte", title="LTE", perms=(Perm("lte.view", "LTE"),)),
        ]
    )
    sched = Scheduler(None)
    assert host.install_tasks(sched) == 2
    tasks = sched.tasks()
    assert set(tasks) == {"ip_guard.collect", "ip_guard.digest"}
    assert tasks["ip_guard.collect"].interval_s == 60
    assert tasks["ip_guard.collect"].timeout_s == 300
    assert str(tasks["ip_guard.digest"].tz) == "Europe/Moscow"
    handlers: dict[str, Any] = {}
    assert host.install_jobs(handlers) == 1
    assert set(handlers) == {"ip_guard.card"}
    seen: list[str] = []
    host.install_jobs(lambda kind, _h: seen.append(kind))
    assert seen == ["ip_guard.card"]
    assert [(m, p.code) for m, p in host.permissions()] == [
        ("ip_guard", "ip_guard.view"),
        ("ip_guard", "ip_guard.unblock"),
        ("lte", "lte.view"),
    ]


def test_install_order_items_and_kinds() -> None:
    reg: dict[str, Any] = {}

    class Fulfiller:
        def register_item(self, item_type: str, handler: Any) -> None:
            reg[item_type] = handler

    host = ExtensionHost([spec(order_items={"lte_pack": _Item()}, order_kinds={"addon_lte": _Kind()})])
    assert host.install_order_items(Fulfiller()) == 1
    assert callable(reg["lte_pack"].fulfill)
    assert host.order_kinds() == ["addon_lte"]
    assert host.order_kind("addon_lte") is not None
    assert host.order_kind("nope") is None


async def test_components_and_contributors_reflect_state() -> None:
    host = ExtensionHost([spec(owns_substitutions=True), spec("lte", title="LTE")])
    components = ComponentRegistry()
    assert host.install_components(components) == 2
    contributors = SquadContributors()
    offs = host.install_contributors(contributors)
    assert len(offs) == 1  # only modules that own substitutions
    assert contributors.state("ip_guard") is SquadState.UNLOADED  # not started: fail-closed
    await host.start(lambda: {"IP_GUARD_ENABLED": True})
    assert contributors.state("ip_guard") is SquadState.OK
    health = await components.health_all()
    assert health["module:ip_guard"].status is Health.OK
    assert health["module:lte"].status is Health.DISABLED
    for _ in range(6):
        host.context("ip_guard").breaker.record_error()
    assert contributors.state("ip_guard") is SquadState.DEGRADED
    offs[0]()
    assert contributors.state("ip_guard") is SquadState.UNLOADED


async def test_install_ui_isolates_a_failing_module() -> None:
    calls: list[str] = []

    def ok_ui(router: Any, ctx: Any) -> None:
        calls.append(f"{ctx.name}:{router}")

    async def bad_ui(router: Any, ctx: Any) -> None:
        raise RuntimeError("boom")

    host = ExtensionHost([spec(ui=bad_ui), spec("lte", title="LTE", ui=ok_ui)])
    failed = await host.install_ui("R")
    assert failed == {"ip_guard": "RuntimeError"}
    assert calls == ["lte:R"]


def test_manifest_import_is_light() -> None:
    """``import svbg.ext`` (what every manifest does) must not pull aiogram or billing (05 §3.1: 0 MB off)."""
    import subprocess

    code = "import sys, svbg.ext; print(sorted(m for m in ('aiogram', 'svbg.billing') if m in sys.modules))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=60)
    assert out.stdout.strip() == "[]"
