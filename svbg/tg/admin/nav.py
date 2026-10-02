"""Admin navigation shared by every admin screen: the parent of a screen, the back row and the breadcrumb.

The admin is one tree (``svbg.tg.admin.menu``): the root ``adm`` and its sections. A list screen of a
section ends with ``[⬅️ <section>] [🛠 Админка]`` (:func:`back_row`) instead of the user's «🏠 Меню»; only
the root leads to the user home. Kept import-light (aiogram types and the callback codec only), so the
bundled modules (``svbg.ext.*``) can use it from their ``install``.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Final

from aiogram.types import InlineKeyboardButton

from svbg.tg.ui import codec

__all__ = [
    "HUB_COMM",
    "HUB_LOOK",
    "HUB_MARKETING",
    "HUB_MODULES",
    "HUB_PAY",
    "HUB_SYSTEM",
    "HUB_USERS",
    "PANEL",
    "PARENT",
    "ROOT",
    "ROOT_ALIAS",
    "STATS",
    "TITLES",
    "back_row",
    "back_to",
    "breadcrumb",
    "has_route",
    "has_screen",
    "reachable",
    "short",
]

ROOT: Final = "adm"
ROOT_ALIAS: Final = "admin"  # the old content hub; now the same screen as ``adm``
HUB_USERS: Final = "adm.u"
HUB_PAY: Final = "adm.pay"
HUB_MARKETING: Final = "adm.mk"
HUB_COMM: Final = "adm.c"
HUB_LOOK: Final = "adm.l"
STATS: Final = "adm.s"
HUB_SYSTEM: Final = "adm.sys"
HUB_MODULES: Final = "adm.x"
PANEL: Final = "adm.rw"

#: Screen code → title (emoji + name) used by back buttons and breadcrumbs.
TITLES: Final[Mapping[str, str]] = MappingProxyType(
    {
        ROOT: "🛠 Админка",
        ROOT_ALIAS: "🛠 Админка",
        HUB_USERS: "👥 Пользователи",
        "plans": "📦 Тарифы",
        HUB_PAY: "💳 Оплата",
        "apay": "🏦 Кассы",
        "apay.rc": "🧾 Ждут подтверждения",
        HUB_MARKETING: "🎯 Маркетинг",
        "prm": "🎟 Промокоды",
        "ads": "📢 Реклама",
        "dl": "🔗 Ссылки на разделы",
        HUB_COMM: "📣 Связь",
        "bc": "📨 Рассылки",
        "achat": "🛎 Админ-группа",
        HUB_LOOK: "🎨 Оформление",
        "ce.home": "✏️ Конструктор",
        "pgs": "📄 Страницы",
        STATS: "📊 Статистика",
        HUB_SYSTEM: "⚙️ Система",
        "status": "🩺 Состояние",
        "ops": "💾 Бэкапы",
        "roles": "👮 Команда",
        PANEL: "🔌 Панель",
        "settings_root": "🔎 Все настройки",
        HUB_MODULES: "🧩 Модули",
        "lte": "🌐 Трафик LTE",
        "ipguard": "🛡 IP Guard",
        "au.new": "🆕 Новые",
        "au.paid": "💳 Недавно оплатили",
        "au.ban": "⛔ Заблокированные",
    }
)

#: Screen → the screen its back button leads to (anything not listed goes to the root).
PARENT: Final[Mapping[str, str]] = MappingProxyType(
    {
        HUB_USERS: ROOT,
        "plans": ROOT,
        HUB_PAY: ROOT,
        HUB_MARKETING: ROOT,
        HUB_COMM: ROOT,
        HUB_LOOK: ROOT,
        STATS: ROOT,
        HUB_SYSTEM: ROOT,
        HUB_MODULES: ROOT,
        "au.new": HUB_USERS,
        "au.paid": HUB_USERS,
        "au.ban": HUB_USERS,
        "apay": HUB_PAY,
        "apay.rc": HUB_PAY,
        "apay.c": "apay",
        "prm": HUB_MARKETING,
        "ads": HUB_MARKETING,
        "dl": HUB_MARKETING,
        "bc": HUB_COMM,
        "achat": HUB_COMM,
        "ce.home": HUB_LOOK,
        "pgs": HUB_LOOK,
        "status": HUB_SYSTEM,
        "ops": HUB_SYSTEM,
        "roles": HUB_SYSTEM,
        "settings_root": HUB_SYSTEM,
        PANEL: HUB_SYSTEM,
        "status.att": "status",
        "status.panel": "status",
        "locs": "plans",
        "lte": HUB_MODULES,
        "ipguard": HUB_MODULES,
    }
)

_ADMIN_LABEL: Final = "🛠 Админка"


def short(title: str) -> str:
    """``"📣 Связь"`` → ``"Связь"`` (the emoji of a title is dropped in back buttons)."""
    head, sep, rest = title.partition(" ")
    return rest if sep and not head.isalnum() else title


def _button(text: str, screen: str, arg: str | None = None) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=codec.encode(screen, codec.ACTION_OPEN, arg))


def back_to(parent: str, *, arg: str | None = None, title: str | None = None) -> list[InlineKeyboardButton]:
    """``[⬅️ <parent>] [🛠 Админка]``; just ``[🛠 Админка]`` when the parent is the root itself."""
    row: list[InlineKeyboardButton] = []
    if parent not in (ROOT, ROOT_ALIAS):
        label = short(title or TITLES.get(parent, "Назад"))
        row.append(_button(f"⬅️ {label}", parent, arg))
    row.append(_button(_ADMIN_LABEL, ROOT))
    return row


def back_row(screen: str) -> list[InlineKeyboardButton]:
    """The last row of ``screen``: back to its section and to the admin root."""
    return back_to(PARENT.get(screen, ROOT))


def breadcrumb(screen: str, title: str | None = None) -> str:
    """``🛠 Админка › 📣 Связь › 🔔 Уведомления клиентам`` (``title``: the screen's own title when it is
    not in :data:`TITLES`, e.g. a settings slice)."""
    chain = [title or TITLES.get(screen, screen)]
    seen = {screen}
    node = PARENT.get(screen, ROOT) if screen not in (ROOT, ROOT_ALIAS) else None
    while node is not None and node not in seen:
        seen.add(node)
        chain.append(TITLES.get(node, node))
        node = None if node in (ROOT, ROOT_ALIAS) else PARENT.get(node, ROOT)
    return " › ".join(reversed(chain))


def has_screen(router: Any, code: str) -> bool:
    """True when ``code`` is a registered code screen of ``router`` (an entry to a module that is not wired
    is hidden instead of answering «Меню обновилось»)."""
    probe = getattr(router, "has_screen", None)
    if callable(probe):
        return bool(probe(code))
    screens = getattr(router, "_screens", None)
    return isinstance(screens, Mapping) and code in screens


def has_route(router: Any, screen: str, action: str = codec.ACTION_OPEN) -> bool:
    """:func:`has_screen` for a button: an open of a registered screen or a registered action."""
    if action == codec.ACTION_OPEN:
        return has_screen(router, screen)
    actions = getattr(router, "_actions", None)
    return not isinstance(actions, Mapping) or (screen, action) in actions


def reachable(router: Any, user: Any, code: str) -> str:
    """``code`` or the nearest screen above it that ``user`` may open (the root at last): a slice an admin
    reaches from «Все настройки» must not lead back into a section closed to them (``plans`` without the
    right «plans», ``adm.s`` without «stats»)."""
    screens = getattr(router, "_screens", None)
    seen: set[str] = set()
    node = code
    while node not in (ROOT, ROOT_ALIAS) and node not in seen:
        seen.add(node)
        route = screens.get(node) if isinstance(screens, Mapping) else None
        if route is not None and route.access.allows(user):
            return node
        node = PARENT.get(node, ROOT)
    return ROOT
