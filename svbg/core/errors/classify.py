"""Human classification of exceptions: what happened and what the owner should check.

Modules (remnawave, payments, telegram, ...) register their own rules::

    from svbg.core.errors.classify import Severity, register

    register(
        lambda e: isinstance(e, PanelHTTPError) and e.status == 401,
        "Панель отклонила токен",
        "Проверьте REMNAWAVE_TOKEN: «Настройки → Remnawave».",
        severity=Severity.ERROR,
    )

Rules are checked by priority (higher first), then by registration order (later first, so a specific
rule registered by a module wins over a generic built-in one). A predicate that raises is skipped.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

log = logging.getLogger("svbg.errors")


class Severity(StrEnum):
    INFO = "info"
    WARN = "warn"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class Classification:
    title_ru: str
    hint_ru: str
    severity: Severity = Severity.ERROR


Predicate = Callable[[BaseException], bool]

DEFAULT = Classification(
    title_ru="Непредвиденная ошибка",
    hint_ru="Откройте «Состояние» и проверьте компоненты. Если ошибка повторяется — "
    "передайте технические детали разработчику.",
    severity=Severity.ERROR,
)


@dataclass(frozen=True, slots=True)
class _Rule:
    predicate: Predicate
    result: Classification
    priority: int
    seq: int


class ClassifierRegistry:
    """Ordered set of classification rules. Thread-safe registration, lock-free lookup."""

    def __init__(self, default: Classification = DEFAULT) -> None:
        self._default = default
        self._rules: tuple[_Rule, ...] = ()
        self._seq = itertools.count()
        self._lock = threading.Lock()

    def register(
        self,
        predicate: Predicate,
        title: str,
        hint: str,
        *,
        severity: Severity = Severity.ERROR,
        priority: int = 0,
    ) -> Callable[[], None]:
        """Add a rule; returns a function that removes it."""
        rule = _Rule(predicate, Classification(title, hint, Severity(severity)), priority, next(self._seq))
        with self._lock:
            self._rules = tuple(sorted((*self._rules, rule), key=lambda r: (-r.priority, -r.seq)))

        def unregister() -> None:
            with self._lock:
                self._rules = tuple(r for r in self._rules if r is not rule)

        return unregister

    def register_types(
        self,
        types: type[BaseException] | tuple[type[BaseException], ...],
        title: str,
        hint: str,
        *,
        severity: Severity = Severity.ERROR,
        priority: int = 0,
    ) -> Callable[[], None]:
        return self.register(
            lambda e: isinstance(e, types), title, hint, severity=severity, priority=priority
        )

    def classify(self, exc: BaseException) -> Classification:
        for rule in self._rules:
            try:
                matched = bool(rule.predicate(exc))
            except Exception as e:  # noqa: BLE001 - a broken rule must never break error reporting
                log.warning("error classifier rule #%d failed (%s); skipped", rule.seq, type(e).__name__)
                continue
            if matched:
                return rule.result
        return self._default


def _is_db_error(exc: BaseException) -> bool:
    mod = type(exc).__module__
    return mod.startswith(("sqlalchemy", "asyncpg"))


def _install_builtin(reg: ClassifierRegistry) -> None:
    reg.register(
        _is_db_error,
        "Ошибка базы данных",
        "Проверьте, что PostgreSQL запущен и доступен (DATABASE_URL), и свободное место на диске.",
        priority=-10,
    )
    reg.register_types(
        (TimeoutError, asyncio.TimeoutError),
        "Операция не уложилась во время",
        "Внешний сервис отвечает слишком долго. Проверьте «Состояние» (панель, кассы, Telegram) и прокси.",
        severity=Severity.WARN,
        priority=-10,
    )
    reg.register_types(
        (ConnectionError,),
        "Нет связи с внешним сервисом",
        "Проверьте сеть сервера, прокси и доступность сервиса в «Состоянии».",
        severity=Severity.WARN,
        priority=-10,
    )
    reg.register_types(
        (MemoryError,),
        "Не хватает памяти",
        "Проверьте потребление памяти контейнером и лимиты Docker.",
        priority=-10,
    )


registry = ClassifierRegistry()
_install_builtin(registry)


def register(
    predicate: Predicate,
    title: str,
    hint: str,
    *,
    severity: Severity = Severity.ERROR,
    priority: int = 0,
) -> Callable[[], None]:
    """Register a rule in the global registry. Returns an ``unregister`` function."""
    return registry.register(predicate, title, hint, severity=severity, priority=priority)


def register_types(
    types: type[BaseException] | tuple[type[BaseException], ...],
    title: str,
    hint: str,
    *,
    severity: Severity = Severity.ERROR,
    priority: int = 0,
) -> Callable[[], None]:
    return registry.register_types(types, title, hint, severity=severity, priority=priority)


def classify(exc: BaseException) -> Classification:
    """Classification for ``exc`` from the global registry (never raises)."""
    return registry.classify(exc)
