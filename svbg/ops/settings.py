"""Settings of the ops module (backups, update check, daily report): declared here, registered by the app.

The integration adds them to the core registry with ``for d in OPS_SETTINGS: registry.add(d)``. Until then
:func:`opt` reads a key from the live snapshot and falls back to the default declared here, so the module
works (with defaults) on a registry that does not know its keys yet.

All keys are ``HOT``: they are read from the snapshot on every scheduler tick, so a change in the bot or in
``.env`` applies within a minute without a restart.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Final

from svbg.core.settings.registry import Apply, SettingDef

__all__ = [
    "BACKUP_PASSWORD_MIN",
    "OPS_SETTINGS",
    "opt",
    "parse_hhmm",
]

BACKUP_PASSWORD_MIN: Final = 8
_HHMM_RE: Final = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")
_REPO_RE: Final = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}")


def parse_hhmm(value: Any) -> tuple[int, int]:
    """``"04:30"`` → ``(4, 30)``; ``ValueError`` with an owner-facing message otherwise."""
    m = _HHMM_RE.fullmatch(str(value).strip())
    if m is None:
        raise ValueError("ожидалось время в формате ЧЧ:ММ, например 04:00")
    return int(m.group(1)), int(m.group(2))


def _hhmm(value: Any) -> None:
    parse_hhmm(value)


def _password(value: Any) -> None:
    if value is None or value == "":
        return
    text = str(value)
    if len(text) < BACKUP_PASSWORD_MIN:
        raise ValueError(f"пароль слишком короткий: нужно не меньше {BACKUP_PASSWORD_MIN} символов")
    if text != text.strip():
        raise ValueError("пароль не должен начинаться или заканчиваться пробелом")


def _repo(value: Any) -> None:
    if value is None or value == "":
        return
    if not _REPO_RE.fullmatch(str(value)):
        raise ValueError("ожидалось имя репозитория GitHub вида владелец/проект")


OPS_SETTINGS: Final[tuple[SettingDef, ...]] = (
    SettingDef(
        "BACKUP_ENABLED",
        bool,
        True,
        "reports",
        "Ежедневный бэкап",
        "Каждый день в BACKUP_AT (по TIMEZONE) делать бэкап базы, контента и зашифрованной копии .env.",
        apply=Apply.HOT,
        owner_only=True,
        tags=("бэкап", "backup", "резервная копия"),
    ),
    SettingDef(
        "BACKUP_AT",
        str,
        "04:00",
        "reports",
        "Время бэкапа",
        "Во сколько (ЧЧ:ММ, по часовому поясу TIMEZONE) делать ежедневный бэкап.",
        apply=Apply.HOT,
        owner_only=True,
        validator=_hhmm,
        tags=("бэкап", "backup"),
    ),
    SettingDef(
        "BACKUP_KEEP",
        int,
        7,
        "reports",
        "Сколько бэкапов хранить",
        "Сколько последних бэкапов хранить на сервере в data/backups. Старые удаляются после нового.",
        apply=Apply.HOT,
        owner_only=True,
        min=1,
        max=100,
        tags=("бэкап", "backup"),
    ),
    SettingDef(
        "BACKUP_PASSWORD",
        "secret",
        None,
        "reports",
        "Пароль бэкапов",
        "Шифрует бэкапы (scrypt + Fernet). Без пароля бэкап не отправляется в Telegram, а копия .env в него "
        "не попадает. Сохраните пароль в менеджере паролей: без него бэкап не восстановить.",
        apply=Apply.HOT,
        nullable=True,
        owner_only=True,
        validator=_password,
        tags=("бэкап", "backup", "пароль", "password"),
    ),
    SettingDef(
        "BACKUP_TO_TELEGRAM",
        bool,
        True,
        "reports",
        "Бэкапы в Telegram",
        "Отправлять бэкап в тему «💾 Бэкапы» админ-группы (частями до 45 МБ). Нужен BACKUP_PASSWORD.",
        apply=Apply.HOT,
        owner_only=True,
        tags=("бэкап", "backup", "telegram"),
    ),
    SettingDef(
        "REPORT_DAILY_ENABLED",
        bool,
        True,
        "reports",
        "Ежедневный отчёт",
        "Присылать отчёт за вчера в тему «📊 Отчёты» в REPORT_DAILY_AT (по TIMEZONE).",
        apply=Apply.HOT,
        tags=("отчёт", "report"),
    ),
    SettingDef(
        "UPDATE_CHECK",
        bool,
        True,
        "system",
        "Проверять обновления",
        "Раз в 12 часов смотреть новые релизы на GitHub и писать о них в тему «⚙️ Система».",
        apply=Apply.HOT,
        owner_only=True,
        tags=("обновления", "update", "версия"),
    ),
    SettingDef(
        "UPDATE_REPO",
        str,
        None,
        "system",
        "Репозиторий релизов",
        "GitHub-репозиторий, где выходят релизы бота (владелец/проект). Пусто — проверка выключена.",
        apply=Apply.HOT,
        nullable=True,
        owner_only=True,
        advanced=True,
        validator=_repo,
        tags=("обновления", "update", "github"),
        hint="owner/svbg-shop",
    ),
    SettingDef(
        "UPDATE_PRERELEASES",
        bool,
        False,
        "system",
        "Бета-версии",
        "Сообщать и о предварительных версиях (beta, rc).",
        apply=Apply.HOT,
        owner_only=True,
        advanced=True,
        tags=("обновления", "update", "beta"),
    ),
)

#: Keys of other modules that ops reads, with the defaults of the core registry (fallback only).
_FOREIGN_DEFAULTS: Final[Mapping[str, Any]] = {
    "REPORT_DAILY_AT": "09:00",
    "TIMEZONE": "Europe/Moscow",
    "CURRENCY": "RUB",
}
_DEFAULTS: Final[Mapping[str, Any]] = {
    **_FOREIGN_DEFAULTS,
    **{d.key: d.default for d in OPS_SETTINGS},
}


def opt(snap: Mapping[str, Any] | None, key: str) -> Any:
    """Value of ``key`` from the snapshot, or the declared default when the registry lacks the key."""
    if snap is not None:
        try:
            return snap[key]
        except KeyError:
            pass
    if key not in _DEFAULTS:
        raise KeyError(key)
    return _DEFAULTS[key]
