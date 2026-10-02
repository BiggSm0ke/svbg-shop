"""System screens created on first run: the stage-0 service screens, the user path (stage 2) and the
entries of the stage 3–4 modules.

Seeding is additive and idempotent: a missing system screen is created, a missing system button
(``system_key``) is added to an existing system screen, and nothing the owner already edited is touched.

:data:`SYSTEM_SCREENS` = the stage-0 screens (``menu_fallback``, ``error``, ``settings_root``) + the user path
screens of :mod:`svbg.tg.user.seeds` (whose ``home`` replaces the stage-0 one) + the module buttons on
``home`` (:data:`HOME_MODULE_BUTTONS`: «🛠 Админка» — the one staff entry —, «🎟 Промокод», «🤝 Пригласить»,
«ℹ️ Информация») + the screen :data:`INFO` (FAQ / rules / offer pages). It is computed on first access
(PEP 562): the seeds module imports this one.

The admin itself is code (``svbg.tg.admin.menu``): ``admin`` — the code of the old content hub — is the same
screen as ``adm``. The old staff buttons of ``home`` («⚙️ Настройки», «📦 Тарифы») are retired
(:data:`RETIRED_SYSTEM_BUTTONS`): an install that still has them untouched loses them on the next start; a
button the owner edited stays.

Module buttons are shown only when the module is wired: the app adds ``flag:promo`` / ``flag:referral``
(program on) / ``flag:pages`` to every user context. «💬 Поддержка» is not a content button: the home screen
draws it by ``SUPPORT_MODE`` (hot): ``link`` — the ``SUPPORT_URL`` link; ``tickets`` / ``both`` — the system
action ``system:support`` (also usable on any constructor button), which opens the ticket dialog of
:mod:`svbg.support` (plus the link in ``both``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final

__all__ = [
    "ADMIN_BUTTON",
    "ADMIN_MENU",
    "ADMIN_SCREEN",
    "BASE_SCREENS",
    "ERROR",
    "HOME",
    "HOME_MODULE_BUTTONS",
    "INFO",
    "INFO_SCREEN",
    "MENU_FALLBACK",
    "MODULE_SCREENS",
    "PLANS_BUTTON",
    "RELAYOUT_SYSTEM_BUTTONS",
    "RESERVED_CODES",
    "RETIRED_SYSTEM_BUTTONS",
    "SCREEN_ACCESS",
    "SETTINGS_ROOT",
    "SYSTEM_SCREENS",
    "ScreenAccess",
    "SeedButton",
    "SeedScreen",
]

HOME: Final = "home"
MENU_FALLBACK: Final = "menu_fallback"
ERROR: Final = "error"
SETTINGS_ROOT: Final = "settings_root"

# Callback "screen" names used by the router itself; content screens can never take these codes.
RESERVED_CODES: Final = frozenset({"sys", "mod", "form", "ui"})


@dataclass(frozen=True, slots=True)
class ScreenAccess:
    """Who may open a data-only screen (code screens declare access in ``@router.screen``)."""

    required_role: str | None = None
    perm: str | None = None


@dataclass(frozen=True, slots=True)
class SeedButton:
    system_key: str
    label: Mapping[str, str]
    action: Mapping[str, Any]
    row: int = 0
    sort: int = 0
    style: str | None = None
    visible_if: Mapping[str, Any] | None = None
    icon_custom_emoji_id: str | None = None


@dataclass(frozen=True, slots=True)
class SeedScreen:
    code: str
    title: Mapping[str, str]
    body: Mapping[str, Mapping[str, Any]]
    buttons: tuple[SeedButton, ...] = ()
    media_mode: str = "attach"


def _utf16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _bold_first_line(text: str) -> dict[str, Any]:
    """Body block whose first line is bold (offsets in UTF-16 code units)."""
    first = text.split("\n", 1)[0]
    return {"text": text, "entities": [{"type": "bold", "offset": 0, "length": _utf16(first)}]}


_MENU_BUTTON: Final = SeedButton(
    system_key="home",
    label={"ru": "🏠 Меню", "en": "🏠 Menu"},
    action={"type": "screen", "target": HOME},
    row=9,
)

#: Stage-0 screens; their ``home`` is only the fallback when the user path is not installed.
BASE_SCREENS: Final[tuple[SeedScreen, ...]] = (
    SeedScreen(
        code=HOME,
        title={"ru": "Главное меню", "en": "Main menu"},
        body={
            "ru": _bold_first_line("👋 Добро пожаловать!\n\nВыберите, что хотите сделать."),
            "en": _bold_first_line("👋 Welcome!\n\nChoose what you want to do."),
        },
    ),
    SeedScreen(
        code=MENU_FALLBACK,
        title={"ru": "Меню обновилось", "en": "Menu updated"},
        body={
            "ru": {"text": "Меню обновилось. Откройте нужный раздел ещё раз."},
            "en": {"text": "The menu has been updated. Please open the section again."},
        },
        buttons=(_MENU_BUTTON,),
    ),
    SeedScreen(
        code=ERROR,
        title={"ru": "Ошибка", "en": "Error"},
        body={
            "ru": _bold_first_line(
                "⚠️ Что-то пошло не так\n\nМы уже знаем об ошибке и разбираемся. "
                "Попробуйте ещё раз или вернитесь в меню."
            ),
            "en": _bold_first_line(
                "⚠️ Something went wrong\n\nWe already know about it. Please try again or go back to the menu."
            ),
        },
        buttons=(_MENU_BUTTON,),
    ),
    SeedScreen(
        code=SETTINGS_ROOT,
        title={"ru": "Настройки", "en": "Settings"},
        body={
            "ru": _bold_first_line("⚙️ Настройки\n\nРазделы настроек появятся здесь."),
            "en": _bold_first_line("⚙️ Settings\n\nSettings sections will appear here."),
        },
        buttons=(_MENU_BUTTON,),
    ),
)

#: Codes of the admin hub (a content screen) and of the information screen.
ADMIN_MENU: Final = "admin"
INFO: Final = "info"

SCREEN_ACCESS: Final[Mapping[str, ScreenAccess]] = MappingProxyType(
    {SETTINGS_ROOT: ScreenAccess(required_role="admin"), ADMIN_MENU: ScreenAccess(required_role="support")}
)

#: The former «📦 Тарифы» of the home screen (now 🛠 Админка → 📦 Тарифы): kept to recognise an untouched
#: seeded row (:data:`RETIRED_SYSTEM_BUTTONS`), never seeded again.
PLANS_BUTTON: Final = SeedButton(
    system_key="plans",
    label={"ru": "📦 Тарифы", "en": "📦 Plans"},
    action={"type": "screen", "target": "plans"},
    row=9,
    sort=1,
    visible_if={"role": {"gte": "admin"}},
)

_STAFF: Final = {"role": {"gte": "support"}}
_ADMIN: Final = {"role": {"gte": "admin"}}
_OWNER: Final = {"role": "owner"}

#: «🛠 Админка» on the home screen (staff only) → the admin root (``admin`` is the code alias of ``adm``).
ADMIN_BUTTON: Final = SeedButton(
    system_key="admin",
    label={"ru": "🛠 Админка", "en": "🛠 Admin"},
    action={"type": "screen", "target": ADMIN_MENU},
    row=9,
    sort=2,
    visible_if=_STAFF,
)

#: User buttons of the stage 3–4 modules on the home screen (each hidden while its module is not wired).
HOME_MODULE_BUTTONS: Final[tuple[SeedButton, ...]] = (
    ADMIN_BUTTON,
    SeedButton(
        system_key="promo",
        label={"ru": "🎟 Промокод", "en": "🎟 Promo code"},
        action={"type": "system", "name": "promo"},
        row=4,
        visible_if={"flag:promo": True},
    ),
    SeedButton(
        system_key="referral",
        label={"ru": "🤝 Пригласить", "en": "🤝 Invite"},
        action={"type": "system", "name": "referral"},
        row=4,
        sort=1,
        visible_if={"flag:referral": True},
    ),
    SeedButton(
        system_key="info",
        label={"ru": "ℹ️ Информация", "en": "ℹ️ Information"},
        action={"type": "screen", "target": INFO},
        row=5,
        visible_if={"flag:pages": True},
    ),
)


def _entry(  # noqa: PLR0917 - a flat table row
    key: str, ru: str, en: str, target: str, row: int, sort: int, visible: Mapping[str, Any]
) -> SeedButton:
    return SeedButton(
        system_key=key,
        label={"ru": ru, "en": en},
        action={"type": "screen", "target": target},
        row=row,
        sort=sort,
        visible_if=visible,
    )


def _sys(key: str, ru: str, en: str, row: int) -> SeedButton:
    return SeedButton(key, {"ru": ru, "en": en}, {"type": "system", "name": key}, row=row)


#: «ℹ️ Информация»: the pages of :mod:`svbg.pages` (a switched-off page answers «Страница недоступна»).
INFO_SCREEN: Final = SeedScreen(
    code=INFO,
    title={"ru": "Информация", "en": "Information"},
    body={
        "ru": _bold_first_line("ℹ️ Информация\n\nОтветы на частые вопросы, правила и оферта."),
        "en": _bold_first_line("ℹ️ Information\n\nFAQ, rules and the offer."),
    },
    buttons=(
        _sys("faq", "❓ Частые вопросы", "❓ FAQ", 0),
        _sys("rules", "📜 Правила", "📜 Rules", 1),
        _sys("offer", "📄 Оферта", "📄 Offer", 2),
        _MENU_BUTTON,
    ),
)

#: The former content hub «🛠 Админка» (stage 3). New installs do not get it: the admin is code now and the
#: code screen ``admin`` shadows the row an older install still has.
ADMIN_SCREEN: Final = SeedScreen(
    code=ADMIN_MENU,
    title={"ru": "Админка", "en": "Admin"},
    body={
        "ru": _bold_first_line("🛠 Админка\n\nВыберите раздел."),
        "en": _bold_first_line("🛠 Admin\n\nChoose a section."),
    },
    buttons=(
        _entry("dashboard", "📊 Сводка", "📊 Dashboard", "adm", 0, 0, _STAFF),
        _entry("users", "👤 Пользователи", "👤 Users", "au.find", 0, 1, _STAFF),
        _entry("promo", "🎟 Промокоды", "🎟 Promo codes", "prm", 1, 0, _ADMIN),
        _entry("ads", "📢 Реклама", "📢 Ad links", "ads", 1, 1, _ADMIN),
        _entry("deeplinks", "🔗 Ссылки", "🔗 Deep links", "dl", 2, 0, _ADMIN),
        _entry("broadcasts", "📣 Рассылки", "📣 Broadcasts", "bc", 2, 1, _ADMIN),
        _entry("pages", "📄 Страницы", "📄 Pages", "pgs", 3, 0, _ADMIN),
        _entry("constructor", "✏️ Конструктор", "✏️ Constructor", "ce.home", 3, 1, _ADMIN),
        _entry("plans", "📦 Тарифы", "📦 Plans", "plans", 4, 0, _ADMIN),
        _entry("roles", "🔐 Роли", "🔐 Roles", "roles", 4, 1, _ADMIN),
        _entry("lte", "🌐 Трафик LTE", "🌐 LTE traffic", "lte", 5, 0, _ADMIN),
        _entry("ip_guard", "🛡 IP Guard", "🛡 IP Guard", "ipguard", 5, 1, _ADMIN),
        _entry("settings", "⚙️ Настройки", "⚙️ Settings", SETTINGS_ROOT, 6, 0, _ADMIN),
        _entry("status", "🩺 Состояние", "🩺 Status", "status", 6, 1, _ADMIN),
        _entry("ops", "💾 Бэкапы и обновления", "💾 Backups and updates", "ops", 7, 0, _OWNER),
        _MENU_BUTTON,
    ),
)

#: Screens of the stage 3–4 modules (seeded like every system screen).
MODULE_SCREENS: Final[tuple[SeedScreen, ...]] = (INFO_SCREEN,)

#: The old «⚙️ Настройки» of the home screen (both seeds had the same row).
_SETTINGS_BUTTON: Final = SeedButton(
    system_key="settings",
    label={"ru": "⚙️ Настройки", "en": "⚙️ Settings"},
    action={"type": "screen", "target": SETTINGS_ROOT},
    row=9,
    visible_if={"role": {"gte": "admin"}},
)

#: ``(screen code, old seed)`` of system buttons that are no longer seeded. On start a row that still equals
#: its old seed (label, action, condition) is deleted; a row the owner changed is left alone.
RETIRED_SYSTEM_BUTTONS: Final[tuple[tuple[str, SeedButton], ...]] = (
    (HOME, PLANS_BUTTON),
    (HOME, _SETTINGS_BUTTON),
)

_system_screens: tuple[SeedScreen, ...] | None = None
if TYPE_CHECKING:
    SYSTEM_SCREENS: tuple[SeedScreen, ...]  # provided by the module __getattr__ below
    #: ``(screen code, old seed, new seed or None)`` of system buttons whose seed changed (the home layout of
    #: the «Подписка» section): on start a row that still equals its old seed (label, action, condition, row,
    #: order, colour) takes the new one, or goes away for ``None``; a row the owner changed is left alone.
    RELAYOUT_SYSTEM_BUTTONS: tuple[tuple[str, SeedButton, SeedButton | None], ...]


def _build_relayout() -> tuple[tuple[str, SeedButton, SeedButton | None], ...]:
    try:
        from svbg.tg.user.seeds import HOME_RELAYOUT
    except ImportError:  # pragma: no cover - a tree without the user path
        return ()
    return tuple((HOME, old, new) for old, new in HOME_RELAYOUT)


def _build_system_screens() -> tuple[SeedScreen, ...]:
    try:
        from svbg.tg.user.seeds import USER_SCREENS
    except ImportError:  # pragma: no cover - a tree without the user path: stage-0 screens only
        return BASE_SCREENS
    out: list[SeedScreen] = [s for s in BASE_SCREENS if s.code != HOME]
    for seed in USER_SCREENS:
        if seed.code == HOME:
            have = {b.system_key for b in seed.buttons}
            extra = tuple(b for b in HOME_MODULE_BUTTONS if b.system_key not in have)
            out.append(replace(seed, buttons=(*seed.buttons, *extra)))
        else:
            out.append(seed)
    codes = {s.code for s in out}
    out.extend(s for s in MODULE_SCREENS if s.code not in codes)
    return tuple(out)


def __getattr__(name: str) -> Any:
    if name == "SYSTEM_SCREENS":
        global _system_screens  # noqa: PLW0603 - computed once, then cached
        if _system_screens is None:
            _system_screens = _build_system_screens()
        return _system_screens
    if name == "RELAYOUT_SYSTEM_BUTTONS":
        return _build_relayout()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
