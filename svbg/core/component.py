"""Runtime components: probe / reconfigure / health (04 D15, 07 §2.4.3).

A *component* is anything with external configuration and a lifecycle that the owner may need to fix
from the bot: the Telegram bot itself, the Remnawave client, each payment provider instance, the admin
chat, owner modules. Settings use ``probe`` before persisting a change and ``reconfigure`` to swap the
running config atomically; the «Состояние» screen and «Требует внимания» are built from ``health``.

``fix_action`` is an opaque, short, machine-readable reference that the UI turns into a «Исправить»
button. Use the helpers :func:`fix_setting` / :func:`fix_screen` to build it so that the format stays
uniform (``setting:<KEY>`` / ``screen:<code>[:<arg>]``).
"""

from __future__ import annotations

import asyncio
import enum
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from svbg.core.clock import now

log = logging.getLogger("svbg.core.component")

__all__ = [
    "Component",
    "ComponentRegistry",
    "Health",
    "HealthReport",
    "ProbeError",
    "fix_screen",
    "fix_setting",
]

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*(?:[.:-][a-z0-9_]+)*$")
_NAME_MAX = 64
FIX_ACTION_MAX = 200
DEFAULT_HEALTH_TIMEOUT = 10.0

# Owner-facing strings (Russian) kept in one place.
_TXT_HEALTH_TIMEOUT = "Не ответил на проверку за {seconds:g} с"
_TXT_HEALTH_FAILED = "Проверка состояния завершилась ошибкой: {exc_type}"


class Health(enum.Enum):
    """Component state. Ordered by how much attention it needs (see :attr:`rank`)."""

    OK = "ok"
    DEGRADED = "degraded"
    DOWN = "down"
    DISABLED = "disabled"
    UNKNOWN = "unknown"

    @property
    def rank(self) -> int:
        """Bigger is worse: DOWN > DEGRADED > UNKNOWN > OK > DISABLED."""
        return _RANK[self]

    @property
    def needs_attention(self) -> bool:
        return self in (Health.DEGRADED, Health.DOWN)


_RANK: dict[Health, int] = {
    Health.DISABLED: 0,
    Health.OK: 1,
    Health.UNKNOWN: 2,
    Health.DEGRADED: 3,
    Health.DOWN: 4,
}


def _validate_fix_action(value: str | None) -> str | None:
    if value is None:
        return None
    if not value or len(value) > FIX_ACTION_MAX or any(ch.isspace() for ch in value):
        raise ValueError(f"fix_action must be 1..{FIX_ACTION_MAX} chars without whitespace")
    return value


@dataclass(frozen=True, slots=True)
class HealthReport:
    """Snapshot of a component's health, safe to show to the owner (no secrets in ``details``)."""

    status: Health
    summary: str
    details: Mapping[str, Any] = field(default_factory=dict)
    checked_at: datetime = field(default_factory=now)
    fix_action: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, Health):
            raise TypeError("status must be a Health member")
        if self.checked_at.tzinfo is None:
            raise ValueError("checked_at must be timezone-aware")
        _validate_fix_action(self.fix_action)

    @classmethod
    def ok(cls, summary: str = "", **details: Any) -> HealthReport:
        return cls(Health.OK, summary, details)

    @classmethod
    def disabled(cls, summary: str = "", **details: Any) -> HealthReport:
        return cls(Health.DISABLED, summary, details)

    @classmethod
    def degraded(cls, summary: str, *, fix_action: str | None = None, **details: Any) -> HealthReport:
        return cls(Health.DEGRADED, summary, details, fix_action=fix_action)

    @classmethod
    def down(cls, summary: str, *, fix_action: str | None = None, **details: Any) -> HealthReport:
        return cls(Health.DOWN, summary, details, fix_action=fix_action)


class ProbeError(Exception):
    """A candidate config is unusable. ``human`` is shown to the owner as is (Russian)."""

    def __init__(self, human: str, hint: str | None = None, *, fix_action: str | None = None) -> None:
        super().__init__(human)
        self.human = human
        self.hint = hint
        self.fix_action = _validate_fix_action(fix_action)

    def __str__(self) -> str:
        return self.human if self.hint is None else f"{self.human} ({self.hint})"


@runtime_checkable
class Component(Protocol):
    """Structural interface every runtime component implements."""

    name: str

    async def probe(self, candidate: Mapping[str, Any]) -> None:
        """Check a candidate config without side effects; raise :class:`ProbeError` if unusable."""
        ...

    async def reconfigure(self, cfg: Mapping[str, Any]) -> None:
        """Atomically switch to ``cfg``. Must be idempotent."""
        ...

    async def health(self) -> HealthReport: ...


def fix_setting(key: str) -> str:
    """``fix_action`` that opens the editor of one setting, e.g. ``setting:REMNAWAVE_TOKEN``."""
    return _validate_fix_action(f"setting:{key}") or ""


def fix_screen(code: str, arg: str | None = None) -> str:
    """``fix_action`` that opens a screen, e.g. ``screen:status`` or ``screen:jobs:dead``."""
    value = f"screen:{code}" if arg is None else f"screen:{code}:{arg}"
    return _validate_fix_action(value) or ""


def _ensure_report(report: object) -> HealthReport:
    if not isinstance(report, HealthReport):
        raise TypeError(f"health() returned {type(report).__name__}, expected HealthReport")
    return report


HealthErrorHook = Callable[[str, BaseException], Awaitable[None] | None]


class ComponentRegistry:
    """Named components in registration order. Not thread-safe; use from the event loop only."""

    def __init__(
        self,
        *,
        health_timeout: float = DEFAULT_HEALTH_TIMEOUT,
        on_health_error: HealthErrorHook | None = None,
    ) -> None:
        if health_timeout <= 0:
            raise ValueError("health_timeout must be positive")
        self._items: dict[str, Component] = {}
        self._health_timeout = health_timeout
        self._on_health_error = on_health_error

    def register(self, component: Component) -> Component:
        name = getattr(component, "name", None)
        if not isinstance(name, str) or len(name) > _NAME_MAX or not _NAME_RE.match(name):
            raise ValueError(f"invalid component name: {name!r}")
        if not isinstance(component, Component):
            raise TypeError(f"{name}: object does not implement Component")
        if name in self._items:
            if self._items[name] is component:
                return component
            raise ValueError(f"component {name!r} is already registered")
        self._items[name] = component
        return component

    def unregister(self, name: str) -> None:
        self._items.pop(name, None)

    def get(self, name: str) -> Component:
        try:
            return self._items[name]
        except KeyError:
            raise KeyError(f"unknown component: {name!r}") from None

    def find(self, name: str) -> Component | None:
        return self._items.get(name)

    def all(self) -> list[Component]:
        return list(self._items.values())

    def names(self) -> list[str]:
        return list(self._items)

    def __contains__(self, name: object) -> bool:
        return name in self._items

    def __len__(self) -> int:
        return len(self._items)

    async def health(self, name: str, *, limit_s: float | None = None) -> HealthReport:
        """Health of one component; never raises (except cancellation and unknown name)."""
        return await self._safe_health(self.get(name), limit_s or self._health_timeout)

    async def health_all(self, *, limit_s: float | None = None) -> dict[str, HealthReport]:
        """Run every ``health()`` concurrently, each bounded by ``limit_s`` seconds and isolated.

        A component that hangs is reported as ``UNKNOWN``; one that raises — as ``DOWN``.
        Result keys follow registration order.
        """
        components = list(self._items.values())
        if not components:
            return {}
        limit = limit_s or self._health_timeout
        reports = await asyncio.gather(*(self._safe_health(c, limit) for c in components))
        return {c.name: r for c, r in zip(components, reports, strict=True)}

    async def _safe_health(self, component: Component, limit: float) -> HealthReport:
        try:
            async with asyncio.timeout(limit):
                report = await component.health()
            return _ensure_report(report)
        except TimeoutError:
            log.warning("component %s: health() timed out after %.1fs", component.name, limit)
            return HealthReport(
                Health.UNKNOWN, _TXT_HEALTH_TIMEOUT.format(seconds=limit), {"timeout_s": limit}
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("component %s: health() failed", component.name)
            await self._report_error(component.name, exc)
            return HealthReport(
                Health.DOWN,
                _TXT_HEALTH_FAILED.format(exc_type=type(exc).__name__),
                {"error_type": type(exc).__name__},
            )

    async def _report_error(self, name: str, exc: BaseException) -> None:
        hook = self._on_health_error
        if hook is None:
            return
        try:
            res = hook(name, exc)
            if res is not None:
                await res
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("component %s: on_health_error hook failed", name)
