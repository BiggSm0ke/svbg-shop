"""Panel version gate, scope probes, API token expiry, self-test (02 §1, §2.3).

Version policy (02 §1.1):

==================  ================================================================================
``unsupported``     ≤ 2.x — refused: different user API (``uuid``); nothing is read or written.
``best_effort``     3.0–3.1 — works after a successful self-test, with an "unverified" banner.
``full``            3.2–3.4.x — supported.
``newer_minor``     3.5+ — works with a warning "newer than verified".
``unverified_major`` 4.x+ — read-only until the owner confirms writes (``confirmed_major``).
``unknown``         no ``system:metadata`` scope — works by probes, with a hint to grant the scope.
==================  ================================================================================
"""

from __future__ import annotations

import base64
import binascii
import enum
import json
import re
import secrets
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Literal

from svbg.core.clock import now
from svbg.remnawave.errors import ErrorKind, RemnawaveError

if TYPE_CHECKING:
    from svbg.remnawave.api import RemnawaveApi
    from svbg.remnawave.models import SubscriptionSettings, SystemConfig

__all__ = [
    "MIN_FULL",
    "TESTED_MAX",
    "Capabilities",
    "CheckItem",
    "ScopeCheck",
    "SelfTestReport",
    "Support",
    "TokenWarning",
    "VersionGate",
    "gate_version",
    "jwt_claims",
    "parse_version",
    "probe_scopes",
    "self_test",
    "token_expires_at",
    "token_warning",
]

#: Oldest fully supported (3.2.0) and newest verified minor (3.4.x).
MIN_FULL: Final = (3, 2)
TESTED_MAX: Final = (3, 4)
WARN_DAYS: Final = (14, 3, 1)

_VERSION_RE: Final = re.compile(r"^\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?")


class Support(enum.StrEnum):
    FULL = "full"
    BEST_EFFORT = "best_effort"
    NEWER_MINOR = "newer_minor"
    UNVERIFIED_MAJOR = "unverified_major"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


def parse_version(value: str | None) -> tuple[int, int, int] | None:
    """``"3.4.4"``, ``"v3.5.0-beta.1"``, ``"3.4"`` → ``(3, 4, 4)`` / ``(3, 5, 0)`` / ``(3, 4, 0)``."""
    if not value:
        return None
    m = _VERSION_RE.match(value)
    if m is None:
        return None
    return int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0)


@dataclass(frozen=True, slots=True)
class VersionGate:
    version: str | None
    support: Support
    writes_allowed: bool
    usable: bool
    message_ru: str

    @property
    def needs_attention(self) -> bool:
        return self.support not in (Support.FULL,)


def gate_version(version: str | None, *, confirmed_major: int | None = None) -> VersionGate:
    """Apply the version policy to the panel version string."""
    parsed = parse_version(version)
    if parsed is None:
        return VersionGate(
            version,
            Support.UNKNOWN,
            True,
            True,
            "Версия панели неизвестна: выдайте токену право system:metadata (или system:read). Бот работает "
            "по пробам возможностей.",
        )
    major, minor, _ = parsed
    shown = ".".join(map(str, parsed))
    if major < 3:
        return VersionGate(
            version,
            Support.UNSUPPORTED,
            False,
            False,
            f"Панель {shown} не поддерживается: обновите Remnawave до версии 3.2 или новее.",
        )
    if major > TESTED_MAX[0]:
        allowed = confirmed_major is not None and confirmed_major >= major
        text = f"Панель {shown} новее проверенной версии бота. " + (
            "Запись разрешена владельцем."
            if allowed
            else "Бот только читает данные; запись — после обновления бота или подтверждения владельцем."
        )
        return VersionGate(version, Support.UNVERIFIED_MAJOR, allowed, True, text)
    if (major, minor) > TESTED_MAX:
        return VersionGate(
            version,
            Support.NEWER_MINOR,
            True,
            True,
            f"Панель {shown} новее проверенной ({TESTED_MAX[0]}.{TESTED_MAX[1]}.x) — работает, но обновите "
            "бота, когда выйдет новая версия.",
        )
    if (major, minor) < MIN_FULL:
        return VersionGate(
            version,
            Support.BEST_EFFORT,
            True,
            True,
            f"Панель {shown} не проверялась с ботом (поддерживаются 3.2–3.4). Рекомендуем обновить панель.",
        )
    return VersionGate(version, Support.FULL, True, True, f"Панель {shown} · совместимо")


# ---------------------------------------------------------------------------------------------- token


def jwt_claims(token: str | None) -> dict[str, Any] | None:
    """Decode a JWT payload **without** verifying the signature (02 §2.3). ``None`` if not a JWT."""
    if not token:
        return None
    parts = token.strip().split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    try:
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        claims = json.loads(raw)
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    return claims if isinstance(claims, dict) else None


def token_expires_at(token: str | None) -> datetime | None:
    """``exp`` of the API token as aware UTC, or ``None`` (no ``exp`` → the token does not expire)."""
    claims = jwt_claims(token)
    if not claims:
        return None
    exp = claims.get("exp")
    if isinstance(exp, bool) or not isinstance(exp, int | float):
        return None
    try:
        return datetime.fromtimestamp(exp, UTC)
    except (OverflowError, OSError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class TokenWarning:
    #: 14 / 3 / 1 — the threshold crossed; 0 — already expired.
    level: int
    expires_at: datetime
    days_left: float
    severity: Literal["info", "warn", "error"]
    message_ru: str


def token_warning(expires_at: datetime | None, at: datetime | None = None) -> TokenWarning | None:
    """Warning for the owner 14 / 3 / 1 days before the token expires and after (02 §2.3)."""
    if expires_at is None:
        return None
    current = at or now()
    left = (expires_at - current).total_seconds() / 86400
    when = expires_at.strftime("%d.%m.%Y %H:%M UTC")
    if left <= 0:
        return TokenWarning(
            0,
            expires_at,
            left,
            "error",
            f"API-токен панели истёк {when}. Создайте новый токен в панели и вставьте его в «Настройки → "
            "Remnawave».",
        )
    for level in reversed(WARN_DAYS):  # 1, 3, 14
        if left <= level:
            severity: Literal["info", "warn", "error"] = "error" if level == 1 else "warn"
            days = max(1, int(left + 0.999))
            return TokenWarning(
                level,
                expires_at,
                left,
                severity,
                f"API-токен панели истекает через {days} дн. ({when}). Создайте новый токен в панели заранее "
                "и вставьте его в «Настройки → Remnawave».",
            )
    return None


# ------------------------------------------------------------------------------------------- scopes


@dataclass(frozen=True, slots=True)
class ScopeCheck:
    feature: str
    scope: str
    #: True — works; False — 403 (missing scope); None — could not be checked (other error).
    ok: bool | None
    required: bool
    detail: str = ""


Probe = Callable[["RemnawaveApi"], Awaitable[object]]


async def _probe_stream(api: RemnawaveApi) -> object:
    return await api.stream(size=1)


async def _probe_resolve(api: RemnawaveApi) -> object:
    try:
        return await api.resolve(username=f"svbg_probe_{secrets.token_hex(6)}")
    except RemnawaveError as err:
        if err.kind is ErrorKind.NOT_FOUND:
            return None
        raise


async def _probe_internal(api: RemnawaveApi) -> object:
    return await api.internal_squads()


async def _probe_external(api: RemnawaveApi) -> object:
    return await api.external_squads()


async def _probe_nodes(api: RemnawaveApi) -> object:
    return await api.nodes()


async def _probe_config(api: RemnawaveApi) -> object:
    return await api.configuration()


async def _probe_sub_settings(api: RemnawaveApi) -> object:
    return await api.subscription_settings()


#: (feature in Russian, scope, probe, required for selling)
SCOPE_PROBES: Final[Sequence[tuple[str, str, Probe, bool]]] = (
    ("Список пользователей (сверка)", "users:stream", _probe_stream, True),
    ("Поиск пользователя", "users:resolve", _probe_resolve, True),
    ("Сквады (локации)", "internal-squads:list", _probe_internal, True),
    ("Внешние сквады", "external-squads:list", _probe_external, False),
    ("Ноды", "nodes:list", _probe_nodes, False),
    ("Настройки панели (вебхуки)", "system:configuration", _probe_config, False),
    ("Настройки подписки (HWID)", "subscription-settings:get", _probe_sub_settings, False),
)


async def probe_scopes(api: RemnawaveApi) -> list[ScopeCheck]:
    """Read probes of every resource the bot needs (02 §1.3). ``write`` does not imply ``read``."""
    results: list[ScopeCheck] = []
    for feature, scope, probe, required in SCOPE_PROBES:
        try:
            await probe(api)
        except RemnawaveError as err:
            if err.kind is ErrorKind.FORBIDDEN_SCOPE:
                results.append(ScopeCheck(feature, scope, False, required, f"нет права {scope}"))
                continue
            if err.kind in (ErrorKind.AUTH, ErrorKind.TRANSIENT, ErrorKind.PROXY_CHECK):
                raise
            results.append(ScopeCheck(feature, scope, None, required, err.message or err.kind.value))
            continue
        results.append(ScopeCheck(feature, scope, True, required))
    return results


# ---------------------------------------------------------------------------------------- self-test


@dataclass(frozen=True, slots=True)
class CheckItem:
    status: Literal["ok", "warn", "fail"]
    text: str

    @property
    def icon(self) -> str:
        return {"ok": "✅", "warn": "⚠️", "fail": "❌"}[self.status]


@dataclass(frozen=True, slots=True)
class Capabilities:
    """What the connected panel can do; feeds feature flags and «Что включено в панели»."""

    gate: VersionGate
    scopes: tuple[ScopeCheck, ...] = ()
    config: SystemConfig | None = None
    hwid_enabled: bool | None = None
    token_expires_at: datetime | None = None
    checked_at: datetime = field(default_factory=now)

    @property
    def webhooks_enabled(self) -> bool | None:
        return None if self.config is None else self.config.notifications.webhook

    @property
    def missing_scopes(self) -> list[str]:
        return [s.scope for s in self.scopes if s.ok is False]

    @property
    def missing_required(self) -> list[str]:
        return [s.scope for s in self.scopes if s.ok is False and s.required]

    def scope_ok(self, scope: str) -> bool | None:
        for s in self.scopes:
            if s.scope == scope:
                return s.ok
        return None


@dataclass(frozen=True, slots=True)
class SelfTestReport:
    capabilities: Capabilities | None
    items: tuple[CheckItem, ...]
    #: Set when the connection is unusable; Russian, shown as is.
    fatal: str | None = None
    fatal_hint: str | None = None

    @property
    def ok(self) -> bool:
        return self.fatal is None

    def render(self) -> str:
        return "\n".join(f"{i.icon} {i.text}" for i in self.items)


async def self_test(
    api: RemnawaveApi,
    token: str | None,
    *,
    confirmed_major: int | None = None,
    at: datetime | None = None,
) -> SelfTestReport:
    """«Проверить подключение» (02 §1.3): metadata → scope probes → token ``exp`` → configuration.

    Never raises :class:`RemnawaveError`: failures become a ``fatal`` report with a Russian hint.
    """
    items: list[CheckItem] = []
    if api.transport.config.plain_http_external:
        items.append(
            CheckItem(
                "warn",
                "Соединение не шифруется: http:// на внешний адрес, API-токен виден в сети. "
                "Используйте https:// или адрес из docker-сети.",
            )
        )
    try:
        meta = await api.metadata()
        version: str | None = meta.version
    except RemnawaveError as err:
        if err.kind is not ErrorKind.FORBIDDEN_SCOPE:
            return _fatal(items, err)
        version = None
    gate = gate_version(version, confirmed_major=confirmed_major)
    api.gate = gate
    items.append(CheckItem(_gate_status(gate), gate.message_ru))
    if not gate.usable:
        return SelfTestReport(None, tuple(items), gate.message_ru, None)

    try:
        scopes = await probe_scopes(api)
    except RemnawaveError as err:
        return _fatal(items, err)
    for s in scopes:
        if s.ok:
            items.append(CheckItem("ok", f"{s.feature}: доступно"))
        elif s.ok is False:
            items.append(CheckItem("fail" if s.required else "warn", f"{s.feature}: нет права «{s.scope}»"))
        else:
            items.append(CheckItem("warn", f"{s.feature}: проверить не удалось ({s.detail})"))

    expires = token_expires_at(token)
    warn = token_warning(expires, at)
    if warn is not None:
        items.append(CheckItem("fail" if warn.level == 0 else "warn", warn.message_ru))
    elif expires is not None:
        items.append(CheckItem("ok", f"API-токен действует до {expires.strftime('%d.%m.%Y')}"))
    else:
        items.append(CheckItem("ok", "API-токен бессрочный"))

    config: SystemConfig | None = None
    hwid: bool | None = None
    if any(s.scope == "system:configuration" and s.ok for s in scopes):
        try:
            config = await api.configuration()
        except RemnawaveError:
            config = None
    if config is not None:
        items.append(
            CheckItem("ok", "Вебхуки панели включены")
            if config.notifications.webhook
            else CheckItem("warn", "Вебхуки панели выключены — бот работает через периодическую сверку")
        )
    if any(s.scope == "subscription-settings:get" and s.ok for s in scopes):
        try:
            settings: SubscriptionSettings = await api.subscription_settings()
            hwid = settings.hwid_enabled
        except RemnawaveError:
            hwid = None
    caps = Capabilities(gate, tuple(scopes), config, hwid, expires)
    missing = caps.missing_required
    if missing:
        text = "У токена нет обязательных прав: " + ", ".join(missing)
        return SelfTestReport(
            caps,
            tuple(items),
            text,
            "Выдайте эти права токену в панели (или создайте токен с «*»). Панель кеширует права до 1 часа.",
        )
    if warn is not None and warn.level == 0:
        return SelfTestReport(caps, tuple(items), warn.message_ru, None)
    return SelfTestReport(caps, tuple(items))


def _gate_status(gate: VersionGate) -> Literal["ok", "warn", "fail"]:
    if not gate.usable:
        return "fail"
    return "ok" if gate.support is Support.FULL else "warn"


def _fatal(items: list[CheckItem], err: RemnawaveError) -> SelfTestReport:
    text = {
        ErrorKind.AUTH: "Панель отклонила API-токен",
        ErrorKind.PROXY_CHECK: "Панель закрыла соединение без ответа",
        ErrorKind.TRANSIENT: "Панель недоступна",
    }.get(err.kind, f"Панель ответила ошибкой ({err.kind.value})")
    items.append(CheckItem("fail", text))
    return SelfTestReport(None, tuple(items), text, err.hint_ru)


def expiry_window(expires_at: datetime | None, at: datetime | None = None) -> timedelta | None:
    """Time left until the token expires (negative when expired)."""
    if expires_at is None:
        return None
    return expires_at - (at or now())
