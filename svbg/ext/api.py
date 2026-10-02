"""Extension API for owner modules (05 §3.1–3.2, 07 §2.4.3, §2.4.7).

A module (``svbg.ext.lte``, ``svbg.ext.ip_guard``, core ``svbg.referral``) describes itself with one
:class:`ModuleSpec` — a light manifest without heavy imports (point at heavy code with :func:`lazy`)::

    SPEC = ModuleSpec(
        name="ip_guard",
        title="IP Guard",
        enabled_key="IP_GUARD_ENABLED",
        settings=(enabled_setting("ip_guard", "IP Guard", "Сбор IP и блоки за раздачу ссылок."), ...),
        topics=(Topic("antiabuse", "Антиабуз", "🛡", priority="high"),),
        perms=(Perm("ip_guard.view", "IP Guard: просмотр"), Perm("ip_guard.unblock", "IP Guard: снять блок")),
        tasks=(Periodic("collect", lazy("svbg.ext.ip_guard.collector:run"), every_s=60),),
        jobs=(JobDef("ip_guard.card", lazy("svbg.ext.ip_guard.cards:handle")),),
        slots=(Slot("subscription", "banner", render_banner),),
        setup=lazy("svbg.ext.ip_guard.service:setup"),
    )

The core builds one :class:`ExtensionHost` with all specs and installs each kind of contribution where the
core keeps it (settings registry, admin chat topics, scheduler, job worker, bus, billing, squad contributors,
component registry). Everything is registered once at start; the host **gates** it at run time:

* a module is enabled by its setting (``enabled_key``), changes apply at once
  (:meth:`ExtensionHost.bind_settings`); a disabled module runs nothing: tasks return at once, events are
  ignored, slots are not shown;
* errors of the module open its circuit breaker (07 §2.4.3) → ``degraded``: optional parts (periodic tasks
  marked ``optional``, slots, status lines) stop until the breaker closes; must-do work (jobs, order items)
  keeps running;
* a module whose ``setup`` fails is ``failed`` and never blocks the bot's start;
* panel contributions are fail-closed in the core (:mod:`svbg.remnawave.contributors`): the host reports the
  module's liveness there, so a degraded, failed or disabled module freezes its squad substitutions instead of
  silently dropping them.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import inspect
import logging
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import time
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol, cast

from svbg.core.component import Health, HealthReport, fix_screen
from svbg.core.errors.boundary import guard
from svbg.core.errors.breaker import BreakerState, CircuitBreaker
from svbg.core.log import mask
from svbg.core.settings.registry import Apply, Registry, SettingDef

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from svbg.core.bus import Event, EventBus
    from svbg.core.component import ComponentRegistry
    from svbg.jobs.queue import Job
    from svbg.jobs.scheduler import Scheduler
    from svbg.jobs.worker import JobContext
    from svbg.remnawave.contributors import SquadContributors

__all__ = [
    "KNOWN_SLOTS",
    "BusSub",
    "ExtensionHost",
    "JobDef",
    "ModuleContext",
    "ModuleOverview",
    "ModuleSpec",
    "ModuleState",
    "OrderItemHandler",
    "OrderKindHandler",
    "Periodic",
    "Perm",
    "Slot",
    "SlotButton",
    "SlotCall",
    "SlotResult",
    "Topic",
    "ViewLoader",
    "enabled_setting",
    "lazy",
    "load_specs",
]

log = logging.getLogger("svbg.ext")

# ------------------------------------------------------------------------------------------------ constants

_MODULE_RE: Final = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
_PART_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_TOPIC_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_DOTTED_RE: Final = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+$")
_LAZY_RE: Final = re.compile(r"^[a-z_][a-z0-9_.]*:[A-Za-z_][A-Za-z0-9_.]*$")

#: Screen slots of 05 §3.2 X7–X9 as ``(screen_code, slot)``. Rendering is wired by the screens themselves.
KNOWN_SLOTS: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("home", "status_lines"),  # X7: lines under the greeting
        ("subscription", "blocks"),  # X7: text blocks of «Моя подписка»
        ("subscription", "buttons"),  # X7: buttons (``SlotButton.after`` = "connect" puts them after it)
        ("subscription", "banner"),  # X7: replaces the whole screen (freeze)
        ("invite", "blocks"),  # X7: invite screen
        ("checkout", "steps"),  # X8: checkout step (``SlotResult.data`` carries the step)
        ("admin.user_card", "sections"),  # X9: user card sections (text + buttons with rights)
        ("admin.home", "entries"),  # X9: admin home entries
    }
)

TOPIC_PRIORITIES: Final = ("critical", "high", "normal", "low")
TOPIC_FALLBACKS: Final = ("system", "drop")
WHEN_DISABLED: Final = ("skip", "defer", "run")

DEFAULT_SETUP_TIMEOUT: Final = 30.0
DEFAULT_HOOK_TIMEOUT: Final = 10.0
DEFAULT_SLOT_TIMEOUT: Final = 1.5
DEFAULT_STATUS_TIMEOUT: Final = 5.0
DEFER_DELAY_S: Final = 300.0
_TOPIC_COLOR: Final = 7322096  # blue, the core default

# Owner-facing texts (Russian).
_TXT: Final = {
    "disabled": "Выключен",
    "starting": "Запускается",
    "failed": "Не запустился: {error}. Проверьте настройки модуля и нажмите «Включить снова»",
    "degraded": "Много ошибок: необязательные части выключены до восстановления ({errors} за 5 мин)",
    "ok": "Работает",
    "item_off": "Позиция заказа сейчас недоступна.",
    "setup_handled": "модуль не запущен, остальной бот работает",
    "hook_handled": "действие модуля пропущено",
    "slot_handled": "экран показан без блока модуля",
}


class ModuleState(StrEnum):
    DISABLED = "disabled"  # switched off in settings
    STARTING = "starting"  # enabled, setup not finished yet
    ACTIVE = "active"
    DEGRADED = "degraded"  # breaker open: optional parts are off
    FAILED = "failed"  # setup raised; nothing of the module runs


# ------------------------------------------------------------------------------------------------ helpers


def lazy(target: str) -> Callable[..., Awaitable[Any]]:
    """An async callable ``"package.module:attr"`` imported on first call (keeps manifests import-light).

    The target may be an async function or a sync function returning an awaitable or a plain value.
    """
    if not _LAZY_RE.match(target):
        raise ValueError(f"lazy target must look like 'package.module:attr', got {target!r}")
    module_name, attr = target.split(":", 1)

    async def call(*args: Any, **kwargs: Any) -> Any:
        # Resolved on every call (``sys.modules`` + getattr, no I/O): a cached function would outlive a
        # reloaded or patched module and leak between applications of one process (tests).
        obj: Any = importlib.import_module(module_name)
        for part in attr.split("."):
            obj = getattr(obj, part)
        if not callable(obj):
            raise TypeError(f"{target} is not callable")
        result = obj(*args, **kwargs)
        if inspect.isawaitable(result):
            return await result
        return result

    call.__qualname__ = call.__name__ = f"lazy:{target}"
    return call


def enabled_setting(
    module: str,
    title: str,
    description: str,
    *,
    section: str = "modules",
    default: bool = False,
) -> SettingDef:
    """The module's on/off switch ``<MODULE>_ENABLED`` (bool, off by default, applied at once)."""
    _check_module_name(module)
    return SettingDef(
        f"{module.upper()}_ENABLED",
        bool,
        default,
        section,
        f"{title}: включить",
        description,
        apply=Apply.HOT,
        owner_only=True,
        tags=(module, title.lower(), "модуль"),
    )


def _check_module_name(name: str) -> None:
    if not isinstance(name, str) or not _MODULE_RE.match(name):
        raise ValueError(f"invalid module name {name!r}: expected [a-z][a-z0-9_]{{1,31}}")


def _check_part(what: str, value: str) -> None:
    if not isinstance(value, str) or not _PART_RE.match(value):
        raise ValueError(f"invalid {what} {value!r}: expected [a-z][a-z0-9_]{{0,63}}")


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _err(exc: BaseException) -> str:
    """Short masked error description for owner-facing status lines (no messages: they may carry data)."""
    return mask(type(exc).__name__)


# ------------------------------------------------------------------------------------------- declarations

TaskFn = Callable[["ModuleContext"], Awaitable[None]]
HookFn = Callable[["ModuleContext"], Awaitable[None] | None]
HealthFn = Callable[["ModuleContext"], Awaitable[HealthReport]]
ProbeFn = Callable[["ModuleContext", Mapping[str, Any]], Awaitable[None] | None]
ConfigFn = Callable[["ModuleContext", Mapping[str, Any], frozenset[str]], Awaitable[None] | None]
LinesFn = Callable[["ModuleContext"], Awaitable[Sequence[str]]]
UiFn = Callable[[Any, "ModuleContext"], Awaitable[None] | None]
JobFn = Callable[["Job", "JobContext"], Awaitable[None]]
EventFn = Callable[["Event"], Awaitable[None]]
SlotRender = Callable[["SlotCall"], "Awaitable[SlotResult | None] | SlotResult | None"]
ViewLoad = Callable[["AsyncConnection", Any, Mapping[str, Any]], Awaitable[Any]]


class OrderItemHandler(Protocol):
    """X4 order item (same shape as :class:`svbg.billing.fulfill.OrderItemHandler`): ``fulfill`` runs in the
    fulfill transaction after the term was applied; :class:`~svbg.subscriptions.lifecycle.SubscriptionError`
    refunds the whole order."""

    async def fulfill(
        self, conn: AsyncConnection, order: Mapping[str, Any], item: Mapping[str, Any]
    ) -> None: ...


class OrderKindHandler(Protocol):
    """X4 order kind (e.g. ``addon_lte``): applies the order in the fulfill transaction instead of the plan
    term; returns the subscription id. Items of the order are fulfilled by their own handlers afterwards."""

    async def apply(
        self, conn: AsyncConnection, order: Mapping[str, Any], items: Sequence[Mapping[str, Any]]
    ) -> int: ...


@dataclass(frozen=True, slots=True)
class Periodic:
    """X6 periodic task, registered in the scheduler as ``<module>.<name>``.

    Exactly one of ``every_s`` (fixed rate) / ``daily_at`` (wall time in ``tz``, Moscow by default).
    ``optional`` tasks pause while the module is degraded; others run as long as the module is enabled.
    """

    name: str
    run: TaskFn
    every_s: float | None = None
    daily_at: time | None = None
    tz: str = "Europe/Moscow"
    jitter_s: float = 0.0
    timeout_s: float = 300.0
    run_at_start: bool = False
    optional: bool = True

    def __post_init__(self) -> None:
        _check_part("task name", self.name)
        if (self.every_s is None) == (self.daily_at is None):
            raise ValueError(f"task {self.name}: set exactly one of every_s / daily_at")
        if self.every_s is not None and self.every_s <= 0:
            raise ValueError(f"task {self.name}: every_s must be > 0")
        if self.jitter_s < 0 or self.timeout_s <= 0:
            raise ValueError(f"task {self.name}: jitter_s must be >= 0 and timeout_s > 0")
        if not callable(self.run):
            raise TypeError(f"task {self.name}: run is not callable")


@dataclass(frozen=True, slots=True)
class JobDef:
    """Handler of a durable job kind ``<module>.<…>``.

    ``when_disabled``: what a job of a switched-off module does — ``skip`` (complete without work), ``defer``
    (retry in :data:`DEFER_DELAY_S`, the job waits for the module) or ``run`` (e.g. releasing blocks).
    """

    kind: str
    handler: JobFn
    when_disabled: Literal["skip", "defer", "run"] = "skip"
    timeout_s: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not _DOTTED_RE.match(self.kind) or len(self.kind) > 100:
            raise ValueError(f"invalid job kind {self.kind!r}: expected '<module>.<name>'")
        if self.when_disabled not in WHEN_DISABLED:
            raise ValueError(f"job {self.kind}: when_disabled must be one of {WHEN_DISABLED}")
        if self.timeout_s is not None and self.timeout_s <= 0:
            raise ValueError(f"job {self.kind}: timeout_s must be > 0")
        if not callable(self.handler):
            raise TypeError(f"job {self.kind}: handler is not callable")


@dataclass(frozen=True, slots=True)
class BusSub:
    """X3 subscription to a domain event (exact name, ``prefix.*`` or ``*``). Ignored while disabled.

    The bus is best effort: a handler that must not lose the event enqueues its own job."""

    pattern: str
    handler: EventFn

    def __post_init__(self) -> None:
        if not isinstance(self.pattern, str) or not self.pattern:
            raise ValueError("event pattern must not be empty")
        if not callable(self.handler):
            raise TypeError(f"event {self.pattern}: handler is not callable")


@dataclass(frozen=True, slots=True)
class Topic:
    """X11 admin chat topic of the module (turned into the core ``TopicDef`` at install)."""

    kind: str
    title: str
    icon: str
    priority: Literal["critical", "high", "normal", "low"] = "normal"
    icon_alternatives: tuple[str, ...] = ()
    noun: tuple[str, str, str] = ("уведомление", "уведомления", "уведомлений")
    default_enabled: bool = True
    default_fallback: Literal["system", "drop"] = "system"
    color: int = _TOPIC_COLOR

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not _TOPIC_RE.match(self.kind):
            raise ValueError(f"invalid topic kind {self.kind!r}")
        if not self.title.strip() or len(self.title) > 100 or not self.icon.strip():
            raise ValueError(f"topic {self.kind}: title (1..100) and icon are required")
        if self.priority not in TOPIC_PRIORITIES:
            raise ValueError(f"topic {self.kind}: priority must be one of {TOPIC_PRIORITIES}")
        if self.default_fallback not in TOPIC_FALLBACKS:
            raise ValueError(f"topic {self.kind}: fallback must be one of {TOPIC_FALLBACKS}")


@dataclass(frozen=True, slots=True)
class Perm:
    """X13 permission ``<module>.<action>`` for the admin role matrix (the owner has every permission)."""

    code: str
    title: str
    description: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not _DOTTED_RE.match(self.code) or len(self.code) > 64:
            raise ValueError(f"invalid permission {self.code!r}: expected '<module>.<action>'")
        if not self.title.strip():
            raise ValueError(f"permission {self.code}: title is required")


@dataclass(frozen=True, slots=True)
class SlotButton:
    """A button of a slot: ``action`` (module action, routed by the screen engine) or ``url``.

    ``perm`` hides it from admins without the permission; ``style`` is a colour hint (``primary``,
    ``success``, ``danger``); ``after`` places it after a core button (``"connect"``)."""

    text: str
    action: str | None = None
    arg: str | None = None
    url: str | None = None
    style: str | None = None
    perm: str | None = None
    after: str | None = None

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("button text is required")
        if (self.action is None) == (self.url is None):
            raise ValueError("a slot button needs exactly one of action / url")
        if self.url is not None and not self.url.startswith(("https://", "tg://")):
            raise ValueError("slot button url must start with https:// or tg://")


@dataclass(frozen=True, slots=True)
class SlotResult:
    """What a slot adds to a screen. ``banner=True`` (``subscription.banner``) replaces the screen body."""

    lines: tuple[str, ...] = ()
    buttons: tuple[SlotButton, ...] = ()
    banner: bool = False
    data: Mapping[str, Any] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not (self.lines or self.buttons or self.banner or self.data)


@dataclass(frozen=True, slots=True)
class SlotCall:
    """Input of a slot renderer: the viewer, what the screen already loaded, the module's read model."""

    user: Any  # svbg.tg.ui.context.UserCtx (anything with ``has_perm``)
    view: Mapping[str, Any]
    model: Any
    module: ModuleContext


@dataclass(frozen=True, slots=True)
class Slot:
    """X7–X9 slot provider. Pure function over loaded data: no SQL and no HTTP inside ``render``."""

    screen: str
    slot: str
    render: SlotRender
    order: int = 100
    perm: str | None = None

    def __post_init__(self) -> None:
        if (self.screen, self.slot) not in KNOWN_SLOTS:
            raise ValueError(f"unknown slot {self.screen}.{self.slot}; known: {sorted(KNOWN_SLOTS)}")
        if not callable(self.render):
            raise TypeError(f"slot {self.screen}.{self.slot}: render is not callable")


@dataclass(frozen=True, slots=True)
class ViewLoader:
    """The module's read model for one screen: **one** query, run once per render of that screen."""

    screen: str
    load: ViewLoad

    def __post_init__(self) -> None:
        if not any(s == self.screen for s, _ in KNOWN_SLOTS):
            raise ValueError(f"no slots on screen {self.screen!r}")
        if not callable(self.load):
            raise TypeError(f"view loader {self.screen}: load is not callable")


@dataclass(frozen=True)
class ModuleSpec:
    """Manifest of a module. Keys of its settings start with ``<NAME>_`` (``ip_guard`` → ``IP_GUARD_``)."""

    name: str
    title: str
    enabled_key: str | None = None  # None: always on (a core part such as referral with its own mode)
    settings: tuple[SettingDef, ...] = ()
    section: tuple[str, str] | None = None  # own settings section (id, title); else use an existing one
    topics: tuple[Topic, ...] = ()
    perms: tuple[Perm, ...] = ()
    tasks: tuple[Periodic, ...] = ()
    jobs: tuple[JobDef, ...] = ()
    events: tuple[BusSub, ...] = ()
    slots: tuple[Slot, ...] = ()
    views: tuple[ViewLoader, ...] = ()
    order_items: Mapping[str, OrderItemHandler] = field(default_factory=dict)
    order_kinds: Mapping[str, OrderKindHandler] = field(default_factory=dict)
    owns_substitutions: bool = False  # writes panel_squad_substitutions (fail-closed X1)
    setup: HookFn | None = None  # on enable (and at start when enabled)
    teardown: HookFn | None = None  # on disable and at shutdown
    health: HealthFn | None = None  # while active; the host adds disabled / failed / degraded itself
    probe: ProbeFn | None = None  # check a candidate config (Component.probe)
    on_config: ConfigFn | None = None  # a key of the module changed while it is running
    ui: UiFn | None = None  # ``ui(router, ctx)``: the module's own screens and actions
    status: LinesFn | None = None  # lines for «Состояние»
    report: LinesFn | None = None  # section of the daily report

    def __post_init__(self) -> None:
        _check_module_name(self.name)
        if not self.title.strip():
            raise ValueError(f"module {self.name}: title is required")
        object.__setattr__(self, "order_items", MappingProxyType(dict(self.order_items)))
        object.__setattr__(self, "order_kinds", MappingProxyType(dict(self.order_kinds)))
        prefix = self.env_prefix
        keys = [d.key for d in self.settings]
        if len(set(keys)) != len(keys):
            raise ValueError(f"module {self.name}: duplicate setting keys")
        for key in keys:
            if not key.startswith(prefix):
                raise ValueError(f"module {self.name}: setting {key} must start with {prefix}")
        if self.enabled_key is not None:
            defn = next((d for d in self.settings if d.key == self.enabled_key), None)
            if defn is None or defn.kind != "bool":
                raise ValueError(f"module {self.name}: enabled_key must be one of its bool settings")
        if self.section is not None:
            sid, stitle = self.section
            if not sid or not stitle.strip():
                raise ValueError(f"module {self.name}: section needs an id and a title")
        self._unique("task", [t.name for t in self.tasks])
        self._unique("job", [j.kind for j in self.jobs])
        self._unique("topic", [t.kind for t in self.topics])
        self._unique("permission", [p.code for p in self.perms])
        self._unique("view loader", [v.screen for v in self.views])
        for job in self.jobs:
            if not job.kind.startswith(f"{self.name}."):
                raise ValueError(f"module {self.name}: job kind {job.kind} must start with '{self.name}.'")
        for perm in self.perms:
            if not perm.code.startswith(f"{self.name}."):
                raise ValueError(f"module {self.name}: permission {perm.code} must start with '{self.name}.'")
        for kind, handler in self.order_items.items():
            _check_part("order item type", kind)
            if not callable(getattr(handler, "fulfill", None)):
                raise TypeError(f"module {self.name}: order item {kind} has no fulfill()")
        for kind, handler in self.order_kinds.items():
            _check_part("order kind", kind)
            if not callable(getattr(handler, "apply", None)):
                raise TypeError(f"module {self.name}: order kind {kind} has no apply()")

    def _unique(self, what: str, names: list[str]) -> None:
        if len(set(names)) != len(names):
            raise ValueError(f"module {self.name}: duplicate {what} names")

    @property
    def env_prefix(self) -> str:
        return f"{self.name.upper()}_"

    @property
    def setting_keys(self) -> frozenset[str]:
        return frozenset(d.key for d in self.settings)


def load_specs(paths: Iterable[str]) -> tuple[list[ModuleSpec], dict[str, str]]:
    """Import ``SPEC`` (or ``spec()``) from each package path; a broken module is skipped and reported.

    Returns ``(specs, errors)`` where ``errors`` maps the path to a short Russian reason.
    """
    specs: list[ModuleSpec] = []
    errors: dict[str, str] = {}
    for path in paths:
        try:
            mod = importlib.import_module(path)
            spec = getattr(mod, "SPEC", None)
            if spec is None and callable(getattr(mod, "spec", None)):
                spec = mod.spec()
            if not isinstance(spec, ModuleSpec):
                errors[path] = "нет манифеста SPEC"
                continue
        except ModuleNotFoundError as exc:
            if exc.name is not None and path.startswith(exc.name):
                errors[path] = "модуль не установлен"
            else:
                errors[path] = f"ошибка импорта ({_err(exc)})"
                log.exception("extension %s: import failed", path)
            continue
        except Exception as exc:
            errors[path] = f"ошибка импорта ({_err(exc)})"
            log.exception("extension %s: import failed", path)
            continue
        if any(s.name == spec.name for s in specs):
            errors[path] = f"модуль {spec.name} уже загружен"
            continue
        specs.append(spec)
    return specs, errors


# ----------------------------------------------------------------------------------------------- runtime


class _Capture(Protocol):
    async def capture(
        self,
        exc: BaseException,
        place: str,
        *,
        module: str | None = ...,
        user_id: int | None = ...,
        context: Mapping[str, Any] | None = ...,
        handled: str = ...,
    ) -> Any: ...


class ModuleContext:
    """What a module's code gets: its settings, shared dependencies, error reporting, state."""

    __slots__ = ("_host", "_rt", "name", "title")

    def __init__(self, host: ExtensionHost, rt: _Runtime) -> None:
        self._host = host
        self._rt = rt
        self.name = rt.spec.name
        self.title = rt.spec.title

    @property
    def deps(self) -> Mapping[str, Any]:
        return self._host.deps

    def config(self) -> Mapping[str, Any]:
        """The current settings snapshot (take it once per operation)."""
        return self._host.config()

    def dep(self, key: str) -> Any:
        """A shared dependency (``db``, ``queue``, ``admin_chat``…); ``LookupError`` if there is none."""
        value = self.deps.get(key)
        if value is None:
            raise LookupError(f"module {self.name}: dependency {key!r} is not available")
        return value

    @property
    def state(self) -> ModuleState:
        return self._rt.state

    @property
    def enabled(self) -> bool:
        return self._rt.enabled

    @property
    def active(self) -> bool:
        """Enabled, started and not degraded: optional work may run."""
        return self._rt.state is ModuleState.ACTIVE

    @property
    def breaker(self) -> CircuitBreaker:
        return self._rt.breaker

    async def capture(
        self,
        exc: BaseException,
        place: str,
        *,
        user_id: int | None = None,
        context: Mapping[str, Any] | None = None,
        handled: str = _TXT["hook_handled"],
    ) -> None:
        """Report an error of the module (counts towards its breaker). Never raises."""
        await self._host._capture(
            self._rt, exc, f"module:{self.name}:{place}", user_id=user_id, context=context, handled=handled
        )

    def guard(self, place: str, *, user_id: int | None = None, reraise: bool = False) -> guard:
        """Error boundary bound to this module (``async with ctx.guard("collect"): ...``)."""
        return self._host._guard(self._rt, place, user_id=user_id, reraise=reraise)

    def wake(self, task: str) -> bool:
        """Run a periodic task of the module now; ``False`` if the scheduler has no such task."""
        return self._host._wake(f"{self.name}.{task}")


class _Runtime:
    __slots__ = ("breaker", "ctx", "enabled", "error", "own_breaker", "spec", "started")

    def __init__(self, spec: ModuleSpec, breaker: CircuitBreaker, own_breaker: bool) -> None:
        self.spec = spec
        self.breaker = breaker
        self.own_breaker = own_breaker  # not shared with the error hub: count captured errors here
        self.enabled = False
        self.started = False
        self.error: str | None = None
        self.ctx: ModuleContext | None = None

    @property
    def state(self) -> ModuleState:
        if not self.enabled:
            return ModuleState.DISABLED
        if self.error is not None:
            return ModuleState.FAILED
        if not self.started:
            return ModuleState.STARTING
        if self.breaker.state is BreakerState.OPEN:
            return ModuleState.DEGRADED
        return ModuleState.ACTIVE

    @property
    def running(self) -> bool:
        """Must-do work may run (enabled and started, degraded or not)."""
        return self.enabled and self.started and self.error is None


@dataclass(frozen=True, slots=True)
class ModuleOverview:
    """One row of «Состояние → Модули»."""

    name: str
    title: str
    state: ModuleState
    health: HealthReport
    lines: tuple[str, ...] = ()


class _TopicSink(Protocol):
    def register_topic(self, defn: Any) -> None: ...


class _ItemSink(Protocol):
    def register_item(self, item_type: str, handler: Any) -> None: ...


class _ModuleComponent:
    """Adapter: a module in the core :class:`~svbg.core.component.ComponentRegistry` (``module:<name>``)."""

    def __init__(self, host: ExtensionHost, module: str) -> None:
        self.name = f"module:{module}"
        self._host = host
        self._module = module

    async def probe(self, candidate: Mapping[str, Any]) -> None:
        await self._host.probe(self._module, candidate)

    async def reconfigure(self, cfg: Mapping[str, Any]) -> None:
        await self._host.sync(cfg)

    async def health(self) -> HealthReport:
        return await self._host.health(self._module)


class ExtensionHost:
    """Registry and lifecycle of modules. Build once; install contributions; ``start`` after settings load."""

    def __init__(
        self,
        specs: Iterable[ModuleSpec],
        *,
        deps: Mapping[str, Any] | None = None,
        hub: _Capture | None = None,
        config: Callable[[], Mapping[str, Any]] | None = None,
        setup_timeout: float = DEFAULT_SETUP_TIMEOUT,
        hook_timeout: float = DEFAULT_HOOK_TIMEOUT,
        slot_timeout: float = DEFAULT_SLOT_TIMEOUT,
        status_timeout: float = DEFAULT_STATUS_TIMEOUT,
        breaker_factory: Callable[[str], CircuitBreaker] | None = None,
    ) -> None:
        self._hub = hub
        self._config = config
        self._deps: Mapping[str, Any] = MappingProxyType(dict(deps or {}))
        self._setup_timeout = setup_timeout
        self._hook_timeout = hook_timeout
        self._slot_timeout = slot_timeout
        self._status_timeout = status_timeout
        self._scheduler: Scheduler | None = None
        self._lock = asyncio.Lock()
        self._bg: set[asyncio.Task[None]] = set()
        self._rts: dict[str, _Runtime] = {}
        hub_breaker = getattr(hub, "breaker", None)
        self._custom_breakers = breaker_factory is not None
        seen_topics: dict[str, str] = {}
        seen_perms: set[str] = set()
        seen_items: dict[str, str] = {}
        for spec in specs:
            if spec.name in self._rts:
                raise ValueError(f"module {spec.name!r} is registered twice")
            for t in spec.topics:
                if t.kind in seen_topics:
                    raise ValueError(
                        f"topic {t.kind!r} of {spec.name} is already used by {seen_topics[t.kind]}"
                    )
                seen_topics[t.kind] = spec.name
            seen_perms.update(p.code for p in spec.perms)
            for kind in (*spec.order_items, *(f"kind:{k}" for k in spec.order_kinds)):
                if kind in seen_items:
                    raise ValueError(
                        f"order {kind!r} of {spec.name} is already handled by {seen_items[kind]}"
                    )
                seen_items[kind] = spec.name
            if breaker_factory is not None:
                breaker, own = breaker_factory(spec.name), True
            elif callable(hub_breaker):
                breaker, own = cast("CircuitBreaker", hub_breaker(spec.name)), False
            else:
                breaker, own = CircuitBreaker(spec.name), True
            rt = _Runtime(spec, breaker, own)
            rt.ctx = ModuleContext(self, rt)
            self._rts[spec.name] = rt

    # ------------------------------------------------------------------------------------------ queries

    def attach(self, *, hub: _Capture | None = None, deps: Mapping[str, Any] | None = None) -> None:
        """Late wiring: the settings registry is built before the error hub and the services exist."""
        if deps is not None:
            self._deps = MappingProxyType({**self._deps, **deps})
        if hub is None:
            return
        self._hub = hub
        hub_breaker = getattr(hub, "breaker", None)
        if callable(hub_breaker) and not self._custom_breakers:
            for rt in self._rts.values():
                rt.breaker, rt.own_breaker = cast("CircuitBreaker", hub_breaker(rt.spec.name)), False

    @property
    def deps(self) -> Mapping[str, Any]:
        return self._deps

    def config(self) -> Mapping[str, Any]:
        if self._config is None:
            return MappingProxyType({})
        return self._config()

    @property
    def names(self) -> list[str]:
        return list(self._rts)

    def spec(self, name: str) -> ModuleSpec:
        return self._rt(name).spec

    def context(self, name: str) -> ModuleContext:
        ctx = self._rt(name).ctx
        assert ctx is not None
        return ctx

    def state(self, name: str) -> ModuleState:
        return self._rt(name).state

    def is_active(self, name: str) -> bool:
        rt = self._rts.get(name)
        return rt is not None and rt.state is ModuleState.ACTIVE

    def permissions(self) -> list[tuple[str, Perm]]:
        """X13 catalogue for the role matrix: ``(module, perm)`` in registration order."""
        return [(rt.spec.name, p) for rt in self._rts.values() for p in rt.spec.perms]

    def _rt(self, name: str) -> _Runtime:
        try:
            return self._rts[name]
        except KeyError:
            raise KeyError(f"unknown module: {name!r}") from None

    # ------------------------------------------------------------------------------------- installation

    def install_settings(self, registry: Registry) -> int:
        """X12: add sections and settings of every module to the registry (before ``SettingsService``)."""
        added = 0
        known = {sid for sid, _ in registry.sections}
        for rt in self._rts.values():
            spec = rt.spec
            if spec.section is not None and spec.section[0] not in known:
                registry.add_section(*spec.section)
                known.add(spec.section[0])
            for defn in spec.settings:
                registry.add(defn)
                added += 1
        return added

    def install_topics(self, admin_chat: _TopicSink) -> int:
        """X11: register the module topics in the admin chat (created there on demand)."""
        from svbg.services.admin_chat import TopicDef
        from svbg.tg.notifier import Priority

        n = 0
        for rt in self._rts.values():
            for t in rt.spec.topics:
                admin_chat.register_topic(
                    TopicDef(
                        t.kind,
                        t.title,
                        t.icon,
                        Priority[t.priority.upper()],
                        t.icon_alternatives,
                        t.color,
                        t.noun,
                        t.default_enabled,
                        t.default_fallback,
                        rt.spec.name,
                    )
                )
                n += 1
        return n

    def install_jobs(self, register: Callable[[str, Any], None] | dict[str, Any]) -> int:
        """Job handlers (gated, counted in the breaker): ``worker.register`` or ``App.job_handlers``."""
        add = register.__setitem__ if isinstance(register, dict) else register
        n = 0
        for rt in self._rts.values():
            for job in rt.spec.jobs:
                add(job.kind, self._job_wrapper(rt, job))
                n += 1
        return n

    def install_tasks(self, scheduler: Scheduler) -> int:
        """X6: periodic tasks as ``<module>.<task>`` (they idle while the module is off)."""
        self._scheduler = scheduler
        n = 0
        for rt in self._rts.values():
            for task in rt.spec.tasks:
                name = f"{rt.spec.name}.{task.name}"
                fn = self._task_wrapper(rt, task)
                if task.every_s is not None:
                    scheduler.every(
                        name,
                        task.every_s,
                        fn,
                        jitter_s=task.jitter_s,
                        run_at_start=task.run_at_start,
                        timeout_s=task.timeout_s,
                    )
                else:
                    assert task.daily_at is not None
                    scheduler.daily(name, task.daily_at, task.tz, fn, timeout_s=task.timeout_s)
                n += 1
        return n

    def install_bus(self, bus: EventBus) -> Callable[[], None]:
        """X3: event subscriptions; returns ``uninstall``."""
        offs = [
            bus.subscribe(sub.pattern, self._event_wrapper(rt, sub))
            for rt in self._rts.values()
            for sub in rt.spec.events
        ]

        def uninstall() -> None:
            for off in offs:
                off()

        return uninstall

    def install_order_items(self, fulfiller: _ItemSink) -> int:
        """X4: order item handlers in billing's ``Fulfiller.register_item``."""
        n = 0
        for rt in self._rts.values():
            for item_type, handler in rt.spec.order_items.items():
                fulfiller.register_item(item_type, _GatedItem(self, rt, item_type, handler))
                n += 1
        return n

    def order_kind(self, kind: str) -> OrderKindHandler | None:
        """X4: handler of a module order kind (``addon_lte``) for billing's fulfill; ``None`` if unknown."""
        for rt in self._rts.values():
            handler = rt.spec.order_kinds.get(kind)
            if handler is not None:
                return _GatedKind(self, rt, kind, handler)
        return None

    def order_kinds(self) -> list[str]:
        return [k for rt in self._rts.values() for k in rt.spec.order_kinds]

    def install_contributors(self, contributors: SquadContributors) -> list[Callable[[], None]]:
        """X1 fail-closed: report liveness of substitution owners to the core squad contributors."""
        offs: list[Callable[[], None]] = []
        for rt in self._rts.values():
            if rt.spec.owns_substitutions:
                offs.append(contributors.register(rt.spec.name, self._squad_status(rt)))
        return offs

    def install_components(self, components: ComponentRegistry) -> int:
        """Every module as component ``module:<name>``: «Состояние», «Требует внимания», probe of settings."""
        for name in self._rts:
            components.register(_ModuleComponent(self, name))
        return len(self._rts)

    async def install_ui(self, router: Any) -> dict[str, str]:
        """The modules' own screens: ``spec.ui(router, ctx)``; a failing module is skipped and reported."""
        failed: dict[str, str] = {}
        for rt in self._rts.values():
            if rt.spec.ui is None:
                continue
            try:
                await _maybe_await(rt.spec.ui(router, self._ctx(rt)))
            except Exception as exc:  # noqa: BLE001 - isolation: the bot works without the module's screens
                failed[rt.spec.name] = _err(exc)
                await self._capture(rt, exc, f"module:{rt.spec.name}:ui", handled=_TXT["setup_handled"])
        return failed

    def bind_settings(self, settings: Any) -> None:
        """Apply module switches and settings at once (``SettingsService.subscribe``)."""
        keys = sorted({k for rt in self._rts.values() for k in rt.spec.setting_keys})
        if not keys:
            return
        if self._config is None:
            self._config = settings.current
        settings.subscribe(keys, self.on_settings)

    # -------------------------------------------------------------------------------------- lifecycle

    async def start(self, config: Callable[[], Mapping[str, Any]] | None = None) -> None:
        """Start enabled modules. Never raises: a failing module becomes ``failed``."""
        if config is not None:
            self._config = config
        await self.sync()

    async def stop(self) -> None:
        """Tear down running modules (shutdown) after pending settings changes."""
        await self.drain()
        async with self._lock:
            for rt in reversed(list(self._rts.values())):
                if rt.started:
                    await self._teardown(rt)

    async def sync(self, cfg: Mapping[str, Any] | None = None) -> None:
        """Bring every module to what the settings ask: start newly enabled, stop newly disabled."""
        async with self._lock:
            snap = cfg if cfg is not None else self.config()
            for rt in self._rts.values():
                want = self._wanted(rt.spec, snap)
                if want and not rt.enabled:
                    rt.enabled, rt.error = True, None
                    await self._setup(rt)
                elif not want and rt.enabled:
                    if rt.started:
                        await self._teardown(rt)
                    rt.enabled, rt.error = False, None

    async def on_settings(self, snap: Mapping[str, Any], changed: Iterable[str]) -> None:
        """``SettingsService`` subscriber. Returns at once (the owner's click never waits for a module's
        setup); the change is applied in the background, in order — :meth:`drain` waits for it."""
        keys = frozenset(changed)
        task = asyncio.create_task(self._apply_settings(snap, keys), name="ext:settings")
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    async def drain(self) -> None:
        """Wait until settings changes queued by :meth:`on_settings` are applied."""
        while self._bg:
            await asyncio.gather(*list(self._bg), return_exceptions=True)

    async def _apply_settings(self, snap: Mapping[str, Any], keys: frozenset[str]) -> None:
        await self.sync(snap)
        for rt in self._rts.values():
            mine = keys & rt.spec.setting_keys
            if rt.spec.enabled_key is not None:
                mine -= {rt.spec.enabled_key}
            if not mine or rt.spec.on_config is None or not rt.running:
                continue
            await self._call_hook(rt, "config", rt.spec.on_config, self._ctx(rt), snap, mine)

    async def restart(self, name: str) -> ModuleState:
        """«Включить снова»: close the breaker and retry a failed setup."""
        rt = self._rt(name)
        rt.breaker.reset()
        async with self._lock:
            if rt.enabled and (rt.error is not None or not rt.started):
                rt.error = None
                await self._setup(rt)
        return rt.state

    async def probe(self, name: str, candidate: Mapping[str, Any]) -> None:
        rt = self._rt(name)
        if rt.spec.probe is not None and self._wanted(rt.spec, candidate):
            await _maybe_await(rt.spec.probe(self._ctx(rt), candidate))

    async def health(self, name: str) -> HealthReport:
        rt = self._rt(name)
        state = rt.state
        if state is ModuleState.DISABLED:
            return HealthReport.disabled(_TXT["disabled"])
        if state is ModuleState.FAILED:
            return HealthReport.down(_TXT["failed"].format(error=rt.error), fix_action=fix_screen("status"))
        if state is ModuleState.STARTING:
            return HealthReport(Health.UNKNOWN, _TXT["starting"])
        if state is ModuleState.DEGRADED:
            return HealthReport.degraded(
                _TXT["degraded"].format(errors=rt.breaker.errors_in_window()),
                fix_action=fix_screen("status"),
            )
        if rt.spec.health is None:
            return HealthReport.ok(_TXT["ok"])
        return await rt.spec.health(self._ctx(rt))

    async def overview(self) -> list[ModuleOverview]:
        """«Состояние → Модули»: state, health and the module's own lines (each call bounded and isolated)."""

        async def one(rt: _Runtime) -> ModuleOverview:
            report = await self._bounded_health(rt)
            lines: tuple[str, ...] = ()
            if rt.spec.status is not None and rt.state is ModuleState.ACTIVE:
                lines = await self._lines(rt, rt.spec.status, "status")
            return ModuleOverview(rt.spec.name, rt.spec.title, rt.state, report, lines)

        return list(await asyncio.gather(*(one(rt) for rt in self._rts.values())))

    async def report_sections(self) -> list[tuple[str, tuple[str, ...]]]:
        """Sections of the daily report from running modules: ``(title, lines)``; empty ones are omitted."""
        out: list[tuple[str, tuple[str, ...]]] = []
        for rt in self._rts.values():
            if rt.spec.report is None or not rt.running:
                continue
            lines = await self._lines(rt, rt.spec.report, "report")
            if lines:
                out.append((rt.spec.title, lines))
        return out

    # ------------------------------------------------------------------------------------------ slots

    def slots(self, screen: str, slot: str) -> list[tuple[str, Slot]]:
        """``(module, slot)`` providers of a slot in display order (``order``, then module name)."""
        found = [
            (rt.spec.name, s)
            for rt in self._rts.values()
            for s in rt.spec.slots
            if (s.screen, s.slot) == (screen, slot)
        ]
        return sorted(found, key=lambda pair: (pair[1].order, pair[0]))

    async def load_views(
        self,
        screen: str,
        conn: AsyncConnection | None,
        user: Any,
        view: Mapping[str, Any],
        *,
        db: Any | None = None,
    ) -> dict[str, Any]:
        """Read models of active modules for ``screen`` (one query per module); failures → no model.

        A loader never breaks the screen's own connection: in PostgreSQL an SQL error aborts the whole
        transaction and a cancelled query (the slot timeout) invalidates the connection. With ``db`` (or
        ``deps["db"]``) the loaders read on their **own** short connection — a fresh one after a failure;
        without it each loader runs inside a SAVEPOINT of ``conn`` (an SQL error rolls back to it).
        """
        loaders = [
            (rt, loader)
            for rt in self._rts.values()
            if rt.state is ModuleState.ACTIVE
            for loader in rt.spec.views
            if loader.screen == screen
        ]
        if not loaders:
            return {}
        source = db if db is not None else self._deps.get("db")
        models: dict[str, Any] = {}
        own: contextlib.AsyncExitStack | None = None
        own_conn: Any = None
        try:
            for rt, loader in loaders:
                try:
                    if source is not None and callable(getattr(source, "read", None)):
                        if own is None:
                            own = contextlib.AsyncExitStack()
                            own_conn = await own.enter_async_context(source.read())
                        async with asyncio.timeout(self._slot_timeout):
                            models[rt.spec.name] = await loader.load(own_conn, user, view)
                    elif conn is not None:
                        async with conn.begin_nested(), asyncio.timeout(self._slot_timeout):
                            models[rt.spec.name] = await loader.load(conn, user, view)
                    else:
                        async with asyncio.timeout(self._slot_timeout):
                            models[rt.spec.name] = await loader.load(conn, user, view)
                except Exception as exc:  # noqa: BLE001 - a module never breaks a screen
                    if own is not None:  # an aborted / invalidated connection is not reused
                        stack, own, own_conn = own, None, None
                        with contextlib.suppress(Exception):
                            await stack.aclose()
                    await self._capture(
                        rt,
                        exc,
                        f"module:{rt.spec.name}:view:{screen}",
                        user_id=_uid(user),
                        handled=_TXT["slot_handled"],
                    )
        finally:
            if own is not None:
                with contextlib.suppress(Exception):
                    await own.aclose()
        return models

    async def render_slots(
        self,
        screen: str,
        slot: str,
        *,
        user: Any,
        view: Mapping[str, Any] | None = None,
        models: Mapping[str, Any] | None = None,
    ) -> list[SlotResult]:
        """Render one slot. Disabled/degraded modules and slots the user may not see are skipped; an error or
        a timeout drops only that provider (reported), the screen renders without it."""
        out: list[SlotResult] = []
        view = view if view is not None else MappingProxyType({})
        for module, s in self.slots(screen, slot):
            rt = self._rts[module]
            if rt.state is not ModuleState.ACTIVE:
                continue
            if s.perm is not None and not _has_perm(user, s.perm):
                continue
            call = SlotCall(user, view, (models or {}).get(module), self._ctx(rt))
            try:
                async with asyncio.timeout(self._slot_timeout):
                    result = await _maybe_await(s.render(call))
            except Exception as exc:  # noqa: BLE001 - slot boundary (05 §3.6)
                await self._capture(
                    rt,
                    exc,
                    f"module:{module}:slot:{screen}.{slot}",
                    user_id=_uid(user),
                    handled=_TXT["slot_handled"],
                )
                continue
            if result is None:
                continue
            if not isinstance(result, SlotResult):
                await self._capture(
                    rt,
                    TypeError(f"slot returned {type(result).__name__}, expected SlotResult"),
                    f"module:{module}:slot:{screen}.{slot}",
                    user_id=_uid(user),
                    handled=_TXT["slot_handled"],
                )
                continue
            buttons = tuple(b for b in result.buttons if b.perm is None or _has_perm(user, b.perm))
            if buttons != result.buttons:
                result = SlotResult(result.lines, buttons, result.banner, result.data)
            if not result.empty:
                out.append(result)
        return out

    # --------------------------------------------------------------------------------------- internals

    def _ctx(self, rt: _Runtime) -> ModuleContext:
        assert rt.ctx is not None
        return rt.ctx

    @staticmethod
    def _wanted(spec: ModuleSpec, cfg: Mapping[str, Any]) -> bool:
        if spec.enabled_key is None:
            return True
        try:
            return cfg.get(spec.enabled_key) is True
        except Exception:  # noqa: BLE001 - an odd mapping means "off", never a crash
            return False

    async def _setup(self, rt: _Runtime) -> None:
        rt.started = False
        if rt.spec.setup is not None:
            try:
                async with asyncio.timeout(self._setup_timeout):
                    await _maybe_await(rt.spec.setup(self._ctx(rt)))
            except Exception as exc:  # noqa: BLE001 - a module never stops the bot (07 §2.4.3)
                rt.error = "таймаут запуска" if isinstance(exc, TimeoutError) else _err(exc)
                log.warning("module %s: setup failed: %s", rt.spec.name, rt.error)
                await self._capture(
                    rt, exc, f"module:{rt.spec.name}:setup", handled=_TXT["setup_handled"], count=False
                )
                return
        rt.started = True
        log.info("module %s started", rt.spec.name)

    async def _teardown(self, rt: _Runtime) -> None:
        rt.started = False
        if rt.spec.teardown is not None:
            await self._call_hook(rt, "teardown", rt.spec.teardown, self._ctx(rt), limit=self._setup_timeout)
        log.info("module %s stopped", rt.spec.name)

    async def _call_hook(
        self, rt: _Runtime, place: str, fn: Callable[..., Any], *args: Any, limit: float | None = None
    ) -> None:
        try:
            async with asyncio.timeout(limit or self._hook_timeout):
                await _maybe_await(fn(*args))
        except Exception as exc:  # noqa: BLE001 - hooks are isolated
            await self._capture(rt, exc, f"module:{rt.spec.name}:{place}")

    async def _bounded_health(self, rt: _Runtime) -> HealthReport:
        try:
            async with asyncio.timeout(self._status_timeout):
                return await self.health(rt.spec.name)
        except TimeoutError:
            return HealthReport(Health.UNKNOWN, "Не ответил на проверку вовремя")
        except Exception as exc:  # noqa: BLE001 - the status screen never fails because of a module
            await self._capture(rt, exc, f"module:{rt.spec.name}:health")
            return HealthReport.down(f"Проверка состояния завершилась ошибкой: {_err(exc)}")

    async def _lines(self, rt: _Runtime, fn: LinesFn, place: str) -> tuple[str, ...]:
        try:
            async with asyncio.timeout(self._status_timeout):
                lines = await fn(self._ctx(rt))
            return tuple(str(line) for line in lines)
        except Exception as exc:  # noqa: BLE001 - isolated
            await self._capture(rt, exc, f"module:{rt.spec.name}:{place}")
            return ()

    async def _capture(
        self,
        rt: _Runtime,
        exc: BaseException,
        place: str,
        *,
        user_id: int | None = None,
        context: Mapping[str, Any] | None = None,
        handled: str = _TXT["hook_handled"],
        count: bool = True,
    ) -> None:
        """Report to the hub with ``module=`` (the hub counts it in the shared breaker); without a hub or with
        an own breaker count it here."""
        if count and (self._hub is None or rt.own_breaker):
            rt.breaker.record_error()
        if self._hub is None:
            log.error("module %s: %s at %s", rt.spec.name, _err(exc), place)
            return
        try:
            await self._hub.capture(
                exc,
                place,
                module=rt.spec.name if count else None,
                user_id=user_id,
                context=context,
                handled=handled,
            )
        except Exception:
            log.exception("module %s: error hub failed at %s", rt.spec.name, place)

    def _guard(self, rt: _Runtime, place: str, *, user_id: int | None, reraise: bool) -> guard:
        host = self

        class _Bound:
            async def capture(
                self,
                exc: BaseException,
                place: str,
                *,
                module: str | None = None,
                user_id: int | None = None,
                context: Mapping[str, Any] | None = None,
                handled: str = _TXT["hook_handled"],
            ) -> None:
                await host._capture(rt, exc, place, user_id=user_id, context=context, handled=handled)

        return guard(
            f"module:{rt.spec.name}:{place}",
            hub=_Bound(),
            module=rt.spec.name,
            user_id=user_id,
            reraise=reraise,
        )

    def _wake(self, task: str) -> bool:
        scheduler = self._scheduler
        if scheduler is None or task not in scheduler.tasks():
            return False
        scheduler.trigger(task)
        return True

    def _failed_work(self, rt: _Runtime) -> None:
        """Errors that propagate to the scheduler/worker/bus are reported there without ``module``."""
        rt.breaker.record_error()

    def _task_wrapper(self, rt: _Runtime, task: Periodic) -> Callable[[], Awaitable[None]]:
        async def run() -> None:
            if not rt.running or (task.optional and rt.state is ModuleState.DEGRADED):
                return
            try:
                await task.run(self._ctx(rt))
            except Exception:
                self._failed_work(rt)
                raise
            rt.breaker.record_success()

        run.__qualname__ = f"ext:{rt.spec.name}.{task.name}"
        return run

    def _job_wrapper(self, rt: _Runtime, job: JobDef) -> Callable[[Job, JobContext], Awaitable[None]]:
        async def run(j: Job, jctx: JobContext) -> None:
            if not rt.running and job.when_disabled != "run":
                if job.when_disabled == "defer":
                    from svbg.jobs.worker import RetryJob

                    raise RetryJob(DEFER_DELAY_S, f"модуль {rt.spec.name} выключен")
                log.info("job %s skipped: module %s is off", j.kind, rt.spec.name)
                return
            try:
                if job.timeout_s is None:
                    await job.handler(j, jctx)
                else:
                    async with asyncio.timeout(job.timeout_s):
                        await job.handler(j, jctx)
            except Exception as exc:
                from svbg.jobs.worker import RetryJob

                if not isinstance(exc, RetryJob):
                    self._failed_work(rt)
                raise
            rt.breaker.record_success()

        run.__qualname__ = f"ext:{job.kind}"
        return run

    def _event_wrapper(self, rt: _Runtime, sub: BusSub) -> Callable[[Event], Awaitable[None]]:
        async def run(event: Event) -> None:
            if not rt.running:
                return
            try:
                await sub.handler(event)
            except Exception:
                self._failed_work(rt)
                raise

        run.__qualname__ = f"ext:{rt.spec.name}:{sub.pattern}"
        return run

    @staticmethod
    def _squad_status(rt: _Runtime) -> Callable[[], str]:
        def status() -> str:
            state = rt.state
            if state is ModuleState.ACTIVE:
                return "ok"
            if state is ModuleState.DEGRADED:
                return "degraded"
            return "unloaded"  # disabled / failed / starting: the core freezes the squads (fail-closed)

        return status


class _GatedItem:
    """Order item of a module: refused (order refunded) while the module is off; errors open the breaker."""

    def __init__(self, host: ExtensionHost, rt: _Runtime, item_type: str, handler: OrderItemHandler) -> None:
        self._host = host
        self._rt = rt
        self.item_type = item_type
        self._handler = handler

    async def fulfill(self, conn: AsyncConnection, order: Mapping[str, Any], item: Mapping[str, Any]) -> None:
        from svbg.subscriptions.lifecycle import SubscriptionError

        if not self._rt.running:
            raise SubscriptionError("module_off", _TXT["item_off"])
        try:
            await self._handler.fulfill(conn, order, item)
        except SubscriptionError:
            raise
        except Exception:
            self._host._failed_work(self._rt)
            raise


class _GatedKind:
    def __init__(self, host: ExtensionHost, rt: _Runtime, kind: str, handler: OrderKindHandler) -> None:
        self._host = host
        self._rt = rt
        self.kind = kind
        self._handler = handler

    async def apply(
        self, conn: AsyncConnection, order: Mapping[str, Any], items: Sequence[Mapping[str, Any]]
    ) -> int:
        from svbg.subscriptions.lifecycle import SubscriptionError

        if not self._rt.running:
            raise SubscriptionError("module_off", _TXT["item_off"])
        try:
            return await self._handler.apply(conn, order, items)
        except SubscriptionError:
            raise
        except Exception:
            self._host._failed_work(self._rt)
            raise


def _has_perm(user: Any, perm: str) -> bool:
    check = getattr(user, "has_perm", None)
    if not callable(check):
        return False
    try:
        return bool(check(perm))
    except Exception:  # noqa: BLE001 - a broken user context sees nothing extra
        return False


def _uid(user: Any) -> int | None:
    value = getattr(user, "user_id", None)
    return value if isinstance(value, int) and not isinstance(value, bool) else None
