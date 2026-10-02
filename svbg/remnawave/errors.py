"""Remnawave errors: one exception type with a ``kind`` the rest of the bot can act on (02 §2.5).

Kinds and what callers do with them:

=================  ==========================================================================================
``auth``           401 (or the access layer in front of the panel refused us). No retries, breaker opens.
``forbidden_scope`` 403: the API token lacks a scope. No retries, the feature is switched off with a hint.
``not_found``      404 ``A025``/``A063`` from a *user* method — "this panel user does not exist". Any 404
                   from a non-user method (a device, a page config). Other 404 codes from user methods
                   (``A118`` squad, ``A182`` external squad, ``A204`` device) are **not** "user is gone".
``conflict``       ``A019`` (username taken) / ``A020`` (shortUuid taken).
``validation``     400 (zod ``errors[]`` or a panel code), and 404s that reference a missing *other* entity.
``already``        ``A029`` / ``A030`` — already disabled / enabled. Callers treat it as success.
``server``         500 and other non-gateway 5xx (``A018``/``A039`` often mean a deleted squad), bad JSON.
``transient``      network errors, timeouts, 502/503/504, 429, breaker open. Retried for idempotent calls.
``proxy_check``    the panel closed the socket without an HTTP response (production ProxyCheck), TLS errors.
=================  ==========================================================================================

Error bodies of the panel never end up in exceptions as a whole (webhook-like bodies may carry secrets); only
``errorCode``, a short ``message`` and zod issue paths are kept, and they are masked and truncated.
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

import msgspec

from svbg.core.errors.classify import Severity
from svbg.core.errors.classify import register as _register_rule
from svbg.core.log import mask

__all__ = [
    "USER_NOT_FOUND_CODES",
    "ErrorKind",
    "PanelNotConfiguredError",
    "PanelUnavailableError",
    "RemnawaveError",
    "ValidationIssue",
    "WriteBlockedError",
    "error_from_response",
    "install_classifiers",
    "safe_path",
]


class ErrorKind(enum.StrEnum):
    AUTH = "auth"
    FORBIDDEN_SCOPE = "forbidden_scope"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    VALIDATION = "validation"
    ALREADY = "already"
    SERVER = "server"
    TRANSIENT = "transient"
    PROXY_CHECK = "proxy_check"


#: 404 codes that mean "this panel user does not exist" (A063 from get-by-*, A025 from actions/update/delete).
USER_NOT_FOUND_CODES: Final = frozenset({"A025", "A063"})
_CONFLICT_CODES: Final = frozenset({"A019", "A020"})
_ALREADY_CODES: Final = frozenset({"A029", "A030"})
_AUTH_CODES: Final = frozenset({"A003", "A004", "A068"})
_GATEWAY_STATUSES: Final = frozenset({502, 503, 504})
_MESSAGE_MAX = 300
_PAST_MARKER = "in the past"
#: Path segments followed by a value that grants access (``shortUuid`` opens the user's subscription page).
_SECRET_PATH_PARENTS: Final = frozenset({"by-short-uuid", "subpage-config"})

# Owner-facing hints (Russian) kept in one place.
HINTS: Final[Mapping[ErrorKind, str]] = {
    ErrorKind.AUTH: (
        "Панель отклонила API-токен. Создайте новый токен в панели (Настройки → API-токены) и вставьте его "
        "в «Настройки → Remnawave» (REMNAWAVE_TOKEN)."
    ),
    ErrorKind.FORBIDDEN_SCOPE: (
        "У API-токена нет нужных прав. Выдайте токену права (scope) в панели или используйте токен с «*». "
        "Панель кеширует права до 1 часа."
    ),
    ErrorKind.NOT_FOUND: "Объект не найден в панели.",
    ErrorKind.CONFLICT: "Имя пользователя или shortUuid уже заняты в панели.",
    ErrorKind.VALIDATION: "Панель отклонила данные запроса. Подробности — в техническом описании ошибки.",
    ErrorKind.ALREADY: "Действие уже выполнено в панели — это не ошибка.",
    ErrorKind.SERVER: (
        "Внутренняя ошибка панели. Посмотрите логи панели (docker compose logs remnawave). Коды A018/A039 "
        "часто означают удалённый сквад."
    ),
    ErrorKind.TRANSIENT: (
        "Панель временно недоступна или перегружена. Бот повторит запрос сам; проверьте, что контейнер "
        "панели запущен и сеть до него работает."
    ),
    ErrorKind.PROXY_CHECK: (
        "Панель закрыла соединение без ответа. Если бот в одной docker-сети с панелью — используйте адрес "
        "http://remnawave:3000; иначе проверьте, что reverse proxy передаёт X-Forwarded-For и "
        "X-Forwarded-Proto: https."
    ),
}

_TITLES: Final[Mapping[ErrorKind, str]] = {
    ErrorKind.AUTH: "Панель Remnawave отклонила токен",
    ErrorKind.FORBIDDEN_SCOPE: "У токена панели не хватает прав",
    ErrorKind.NOT_FOUND: "Не найдено в панели Remnawave",
    ErrorKind.CONFLICT: "Конфликт данных в панели Remnawave",
    ErrorKind.VALIDATION: "Панель Remnawave отклонила запрос",
    ErrorKind.ALREADY: "Действие в панели уже выполнено",
    ErrorKind.SERVER: "Внутренняя ошибка панели Remnawave",
    ErrorKind.TRANSIENT: "Панель Remnawave временно недоступна",
    ErrorKind.PROXY_CHECK: "Панель Remnawave закрыла соединение без ответа",
}

_SEVERITY: Final[Mapping[ErrorKind, Severity]] = {
    ErrorKind.AUTH: Severity.ERROR,
    ErrorKind.FORBIDDEN_SCOPE: Severity.ERROR,
    ErrorKind.NOT_FOUND: Severity.WARN,
    ErrorKind.CONFLICT: Severity.WARN,
    ErrorKind.VALIDATION: Severity.ERROR,
    ErrorKind.ALREADY: Severity.INFO,
    ErrorKind.SERVER: Severity.ERROR,
    ErrorKind.TRANSIENT: Severity.WARN,
    ErrorKind.PROXY_CHECK: Severity.ERROR,
}


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """One zod issue from a 400 response (``path`` joined with dots)."""

    path: str
    message: str
    code: str = ""


class RemnawaveError(Exception):
    """Any failure of a panel call. ``str()`` is safe to log (masked, no response bodies)."""

    def __init__(
        self,
        kind: ErrorKind | str,
        status: int | None = None,
        code: str | None = None,
        message: str = "",
        hint_ru: str | None = None,
        *,
        method: str | None = None,
        path: str | None = None,
        retry_after: float | None = None,
        issues: Sequence[ValidationIssue] = (),
    ) -> None:
        self.kind = ErrorKind(kind)
        self.status = status
        self.code = code
        self.message = _clip(mask(message or ""))
        self.hint_ru = hint_ru if hint_ru is not None else HINTS[self.kind]
        self.method = method
        self.path = safe_path(path)
        self.retry_after = retry_after
        self.issues = tuple(issues)
        super().__init__(self._render())

    def _render(self) -> str:
        parts = [self.kind.value]
        if self.status is not None:
            parts.append(str(self.status))
        if self.code:
            parts.append(self.code)
        head = " ".join(parts)
        where = f" {self.method} {self.path}" if self.method and self.path else ""
        text = f": {self.message}" if self.message else ""
        return f"Remnawave {head}{where}{text}"

    # ---- helpers for callers

    @property
    def retryable(self) -> bool:
        """True when a repeat of an *idempotent* call may succeed (network / gateway / 429)."""
        return self.kind is ErrorKind.TRANSIENT

    @property
    def is_success_like(self) -> bool:
        """``already`` (A029/A030): the panel is already in the requested state."""
        return self.kind is ErrorKind.ALREADY

    @property
    def is_expire_in_past(self) -> bool:
        """400 «Expiration date cannot be in the past» (clock skew between bot and panel)."""
        if self.kind is not ErrorKind.VALIDATION:
            return False
        texts = [self.message, *(i.message for i in self.issues)]
        return any(_PAST_MARKER in t.lower() for t in texts)


class PanelUnavailableError(RemnawaveError):
    """The circuit breaker is open: the call was not sent. Kind ``transient``."""

    def __init__(self, *, method: str | None = None, path: str | None = None, retry_in: float = 0.0) -> None:
        super().__init__(
            ErrorKind.TRANSIENT,
            None,
            "BREAKER_OPEN",
            f"панель недоступна, запросы приостановлены (повтор через {max(retry_in, 0.0):.0f} с)",
            "Панель не отвечает несколько раз подряд; бот временно не шлёт ей запросы и проверит её сам. "
            "Откройте «Состояние» и проверьте, что панель запущена.",
            method=method,
            path=path,
            retry_after=retry_in,
        )


class PanelNotConfiguredError(RemnawaveError):
    """No panel URL / token: the client does not exist. Kind ``auth``."""

    def __init__(self) -> None:
        super().__init__(
            ErrorKind.AUTH,
            None,
            "NOT_CONFIGURED",
            "панель не подключена",
            "Подключите панель: «Настройки → Remnawave» (адрес и API-токен).",
        )


class WriteBlockedError(RemnawaveError):
    """A mutating call on a panel version the bot has not verified (new major) — kind ``transient``.

    The writer keeps the operation and retries later; the owner unblocks writes in «Состояние».
    """

    def __init__(self, version: str | None, *, method: str | None = None, path: str | None = None) -> None:
        super().__init__(
            ErrorKind.TRANSIENT,
            None,
            "WRITE_BLOCKED",
            f"запись в панель версии {version or '?'} заблокирована до подтверждения владельцем",
            "Версия панели новее проверенной мажорной версии. Обновите бота или подтвердите запись кнопкой "
            "«Я понимаю риск, разрешить запись» на экране «Состояние».",
            method=method,
            path=path,
        )


def safe_path(path: str | None) -> str | None:
    """``path`` with access keys hidden: ``/users/by-short-uuid/AbC`` → ``/users/by-short-uuid/***``.

    Used for everything that leaves the transport (``str(error)``, logs, ErrorHub reports).
    """
    if not path:
        return path
    head, sep, query = path.partition("?")
    parts = head.split("/")
    for i in range(len(parts) - 1):
        if parts[i] in _SECRET_PATH_PARENTS and parts[i + 1]:
            parts[i + 1] = "***"
    return "/".join(parts) + (sep + query if sep else "")


# ---------------------------------------------------------------------------------------------------- parsing


class _ZodIssue(msgspec.Struct, kw_only=True):
    message: str = ""
    code: str = ""
    path: list[str | int] = msgspec.field(default_factory=list)


class _ErrorBody(msgspec.Struct, kw_only=True, rename="camel"):
    message: str | list[str] | None = None
    error_code: str | None = None
    status_code: int | None = None
    errors: list[_ZodIssue] | None = None


_error_decoder = msgspec.json.Decoder(_ErrorBody)


def _clip(text: str, limit: int = _MESSAGE_MAX) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _parse_error_body(body: bytes) -> _ErrorBody:
    if not body:
        return _ErrorBody()
    try:
        return _error_decoder.decode(body)
    except (msgspec.DecodeError, msgspec.ValidationError):
        return _ErrorBody()


def error_from_response(
    status: int,
    body: bytes,
    *,
    user_scoped: bool,
    method: str | None = None,
    path: str | None = None,
    scope: str | None = None,
    retry_after: float | None = None,
) -> RemnawaveError:
    """Classify a non-2xx HTTP response of the panel (02 §2.5)."""
    parsed = _parse_error_body(body)
    code = parsed.error_code
    if code == "E000":
        code = None
    raw_message = parsed.message
    message = "; ".join(raw_message) if isinstance(raw_message, list) else (raw_message or "")
    issues = tuple(
        ValidationIssue(".".join(str(p) for p in i.path), _clip(mask(i.message), 200), i.code)
        for i in (parsed.errors or [])
    )
    common: dict[str, Any] = {"method": method, "path": path}

    def make(kind: ErrorKind, hint: str | None = None, **extra: Any) -> RemnawaveError:
        return RemnawaveError(kind, status, code, message, hint, **common, **extra)

    if 300 <= status < 400:
        return make(
            ErrorKind.AUTH,
            "Панель перенаправляет запрос (скорее всего на страницу входа). Проверьте Cloudflare Access, "
            "Caddy/TinyAuth или cookie в «Настройки → Remnawave».",
        )
    if status == 401 or (code in _AUTH_CODES and status in (401, 403)):
        return make(ErrorKind.AUTH)
    if status == 403:
        hint = HINTS[ErrorKind.FORBIDDEN_SCOPE]
        if scope:
            hint = (
                f"У API-токена нет права «{scope}». Выдайте его токену в панели или используйте токен с «*». "
                "Панель кеширует права до 1 часа."
            )
        return make(ErrorKind.FORBIDDEN_SCOPE, hint)
    if status == 429:
        return make(ErrorKind.TRANSIENT, retry_after=retry_after)
    if status == 404:
        if not user_scoped or code in USER_NOT_FOUND_CODES:
            return make(ErrorKind.NOT_FOUND)
        return make(
            ErrorKind.VALIDATION,
            f"Панель не нашла связанный объект (код {code or 'нет'}): это не значит, что пользователя нет. "
            "Проверьте сквады и устройства в панели.",
        )
    if code in _CONFLICT_CODES:
        return make(ErrorKind.CONFLICT)
    if code in _ALREADY_CODES:
        return make(ErrorKind.ALREADY)
    if 400 <= status < 500:
        return RemnawaveError(ErrorKind.VALIDATION, status, code, message, None, issues=issues, **common)
    if status in _GATEWAY_STATUSES:
        return make(ErrorKind.TRANSIENT, retry_after=retry_after)
    return make(ErrorKind.SERVER)


# ------------------------------------------------------------------------------------------- classifiers

_unregister: Callable[[], None] | None = None


def install_classifiers() -> Callable[[], None]:
    """Register ErrorHub rules for :class:`RemnawaveError` (idempotent). Returns an ``unregister``."""
    global _unregister  # noqa: PLW0603 - one set of rules per process
    if _unregister is not None:
        return _unregister
    removers: list[Callable[[], None]] = []
    for kind in ErrorKind:
        removers.append(
            _register_rule(
                lambda e, k=kind: isinstance(e, RemnawaveError) and e.kind is k,
                _TITLES[kind],
                HINTS[kind],
                severity=_SEVERITY[kind],
                priority=10,
            )
        )
    specific: tuple[tuple[type[RemnawaveError], str, str, Severity], ...] = (
        (
            PanelUnavailableError,
            "Панель Remnawave недоступна: запросы приостановлены",
            "Бот сам проверит панель и возобновит запросы. Откройте «Состояние» и проверьте, что панель "
            "запущена и доступна из контейнера бота.",
            Severity.WARN,
        ),
        (
            PanelNotConfiguredError,
            "Панель Remnawave не подключена",
            "Укажите адрес панели и API-токен: «Настройки → Remnawave».",
            Severity.WARN,
        ),
        (
            WriteBlockedError,
            "Запись в панель Remnawave заблокирована",
            "Версия панели не проверена с этой версией бота. Обновите бота или разрешите запись на экране "
            "«Состояние».",
            Severity.WARN,
        ),
    )
    for exc_type, title, hint, severity in specific:
        removers.append(
            _register_rule(
                lambda e, t=exc_type: isinstance(e, t), title, hint, severity=severity, priority=11
            )
        )

    def unregister() -> None:
        global _unregister  # noqa: PLW0603
        for remove in removers:
            remove()
        _unregister = None

    _unregister = unregister
    return unregister


install_classifiers()
