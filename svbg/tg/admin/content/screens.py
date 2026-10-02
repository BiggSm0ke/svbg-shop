"""Конструктор экранов в боте (07 §2.4.1): режим «✏️», редактор экрана, мастер кнопки, свои экраны,
«Как видит…», история и «↩️ Отменить».

Screens and actions live on the :class:`~svbg.tg.ui.router.ScreenRouter` (names start with ``ce.`` — never a
content code). Access: owner, or admin with ``content.edit`` — the router checks the cached context on every
callback, and every **write** re-reads the role from the database (:func:`svbg.services.roles.load_actor`), so
a right taken away a second ago already blocks the next save.

What the admin sends as a message (a formatted text → ``entities`` are kept, a photo/GIF/video, a Premium
emoji or a custom-emoji sticker, a label, a URL, a DSL condition) is captured by
:meth:`ContentScreens.handle_message` (aiogram router of :meth:`ContentScreens.aiogram_router`, which must
be included **before** the screen router and the user path, so the photo of a new screen picture is not taken
for a payment receipt).

Every save goes through :class:`svbg.content.editing.ContentEditor` (one transaction, CAS by
``screens.version``, ``content_audit`` batch, snapshot reload) and answers «⚡ Применено · ↩️ Отменить».

The default banner (:mod:`svbg.content.banner`) is told apart from the owner's pictures by its hash: the
screen card says «заглушка» and «🗑 Убрать картинку» takes it off one screen; the constructor's home takes it
off every screen or puts it back on the screens without a picture (one batch, «↩️ Отменить»).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from aiogram import Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from svbg.content import defaults
from svbg.content.banner import banner_mode, ensure_banner_media, is_banner
from svbg.content.editing import (
    CAPTION_LIMIT,
    MAX_BUTTONS,
    UNDO_WINDOW,
    ContentEditor,
    EditError,
    EditResult,
    StaleError,
    screen_action_target,
    utf16_len,
)
from svbg.content.media import MediaError
from svbg.content.model import (
    MAX_LABEL,
    MAX_TEXT,
    Button,
    ContentError,
    CopyAction,
    DeeplinkAction,
    ModuleAction,
    ScreenAction,
    ShareAction,
    SystemAction,
    UrlAction,
    WebAppAction,
    parse_action,
)
from svbg.services.roles import DENIED, load_actor
from svbg.tg.admin import nav
from svbg.tg.admin.content.telegram import (
    DOWNLOAD_LIMIT,
    TransportProbeSender,
    check_custom_emoji,
    entities_json,
    pick_icon,
)
from svbg.tg.ui import codec
from svbg.tg.ui.conditions import ConditionError, compile_condition
from svbg.tg.ui.context import UserCtx
from svbg.tg.ui.edit_mode import (
    ACTIONS,
    MARK_CONDITION,
    MARK_DISABLED,
    PERM,
    SCREEN_BUTTON,
    SCREEN_EDITOR,
    SCREEN_PREVIEW,
    EditMode,
    button_rows,
    can_edit,
    pages_of,
)
from svbg.tg.ui.renderer import MAX_ROW_WIDTH, MODULE_SCREEN, nav_button
from svbg.tg.ui.view import Redirect, Toast, View

if TYPE_CHECKING:
    from svbg.content.media import MediaLibrary
    from svbg.content.premium import PremiumService
    from svbg.content.store import ContentStore, ScreenEntry
    from svbg.db.engine import Database
    from svbg.tg.ui.router import HandlerResult, ScreenCtx, ScreenRouter

__all__ = [
    "CONDITION_PRESETS",
    "PREVIEW_STATES",
    "SCREEN_HOME",
    "SCREEN_LIST",
    "ContentScreens",
    "preview_user",
]

log = logging.getLogger("svbg.tg.admin.content")

SCREEN_HOME: Final = "ce.home"
SCREEN_LIST: Final = "ce.list"
SCREEN_BUTTONS: Final = "ce.btns"
SCREEN_STYLE: Final = "ce.btn.st"
SCREEN_ACTION: Final = "ce.btn.ac"
SCREEN_TARGET: Final = "ce.btn.sc"
SCREEN_SYSTEM: Final = "ce.btn.sy"
SCREEN_MODULE: Final = "ce.btn.mo"
SCREEN_COND: Final = "ce.btn.cd"
SCREEN_MOVE: Final = "ce.btn.mv"
SCREEN_HISTORY: Final = "ce.hist"
SCREEN_WAIT: Final = "ce.wait"
SCREEN_DELETE: Final = "ce.del"

CAPTURE_TTL: Final = 15 * 60.0
ALBUM_TTL: Final = 120.0
PAGE: Final = 8
PAGE_BUTTONS: Final = 80  # «🔘 Кнопки»: buttons per page (+ the navigation rows ≤ 100)
_COMMAND_RE: Final = re.compile(r"^/[A-Za-z0-9_]{1,32}(?:@\w+)?(?:\s+\S+)?\s*$")
_ID_RE: Final = re.compile(r"^\d{1,18}$")
_CODE_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_BATCH_RE: Final = re.compile(r"^[0-9a-f]{32}$")

STYLE_LABELS: Final[Mapping[str | None, str]] = {
    None: "обычный",
    "primary": "синий (primary)",
    "success": "зелёный (success)",
    "danger": "красный (danger)",
}

#: Visibility presets of the button wizard: (title, DSL). Anything else is entered as DSL JSON.
CONDITION_PRESETS: Final[tuple[tuple[str, Mapping[str, Any] | None], ...]] = (
    ("Всегда (без условия)", None),
    ("Подписка активна", {"sub": "active"}),
    ("Подписка истекла", {"sub": "expired"}),
    ("Нет подписки", {"sub": "none"}),
    ("Триал", {"sub": "trial"}),
    ("Подписка заморожена", {"sub": "frozen"}),
    ("Есть доступ (триал или активная)", {"sub": ["trial", "active"]}),
    ("Ещё ни разу не платил", {"has_paid": False}),
    ("Мало денег (баланс < 100)", {"balance_minor": {"lt": 10_000}}),
    ("Только админам", {"role": {"gte": "admin"}}),
    ("Поддержке и выше", {"role": {"gte": "support"}}),
    ("Язык: русский", {"lang": "ru"}),
    ("Язык: English", {"lang": "en"}),
)

#: «Как видит…» states: code → (title, UserCtx fields).
PREVIEW_STATES: Final[Mapping[str, tuple[str, Mapping[str, Any]]]] = {
    "new": ("новый", {"sub_state": "none", "is_new": True}),
    "trial": ("триал", {"sub_state": "trial", "days_left": 2}),
    "active": (
        "активная",
        {"sub_state": "active", "days_left": 23, "has_paid": True, "balance_minor": 15_000},
    ),
    "expired": ("истекла", {"sub_state": "expired", "days_left": 0, "has_paid": True}),
    "frozen": ("заморожена", {"sub_state": "frozen", "days_left": 12, "has_paid": True}),
    "poor": ("мало денег", {"sub_state": "active", "days_left": 2, "has_paid": True, "balance_minor": 1_000}),
}

ACTION_TYPES: Final[tuple[tuple[str, str], ...]] = (
    ("screen", "📄 Экран"),
    ("system", "⚙️ Системное"),
    ("url", "🔗 Ссылка"),
    ("webapp", "📱 Web App"),
    ("deeplink", "🧭 Диплинк"),
    ("copy", "📋 Копировать текст"),
    ("share", "📤 Поделиться"),
    ("module", "🧩 Модуль"),
)
_VALUE_PROMPTS: Final[Mapping[str, str]] = {
    "url": "Пришлите ссылку (https://…, http://… или tg://…).",
    "webapp": "Пришлите адрес Web App (только https://…).",
    "deeplink": "Пришлите параметр диплинка бота (то, что после ?start=): латиница, цифры, «_» и «-».",
    "copy": "Пришлите текст, который скопируется по нажатию (до 256 символов).",
    "share": "Пришлите текст, которым пользователь поделится.",
}
_ACTION_KEY: Final[Mapping[str, str]] = {
    "url": "url",
    "webapp": "url",
    "deeplink": "code",
    "copy": "text",
    "share": "text",
}

_T: Final[Mapping[str, str]] = {
    "applied": "⚡ Применено",
    "not_found": "Не найдено — возможно, уже удалили",
    "draft_lost": "Мастер устарел — начните заново",
    "cancelled": "Отменено",
    "home": (
        "🎨 Конструктор экранов\n\n"
        "Режим «✏️»: {mode}. Включите его и откройте любой экран — внизу появится служебный ряд "
        "«✏️ Экран · ➕ Кнопка · 👁 Как видит…», а нажатие на кнопку откроет её редактор. Изменения "
        "применяются сразу, «↩️ Отменить» работает 10 минут.\n\n"
        "Премиум-эмодзи: {premium}"
    ),
    "mode_on": "✏️ Режим правки включён",
    "mode_off": "Режим правки выключен",
    "wait_text": (
        "📝 Пришлите новый текст экрана ({lang}) одним сообщением. Форматирование (жирный, ссылки, спойлеры, "
        "премиум-эмодзи) сохранится как есть. Плейсхолдеры: {{balance}}, {{days_left}}.\n\n/cancel — отмена"
    ),
    "wait_media": ("🖼 Пришлите фото, GIF или видео для экрана (до 20 МБ).\n\n/cancel — отмена"),
    "wait_label": (
        "🔤 Пришлите текст кнопки ({lang}), до 128 символов. Можно с плейсхолдерами.\n\n/cancel — отмена"
    ),
    "wait_icon": (
        "😀 Пришлите премиум-эмодзи (одно, сообщением) или стикер из набора эмодзи — он станет иконкой "
        "кнопки.\n\n/cancel — отмена"
    ),
    "wait_dsl": (
        '✍️ Пришлите условие в JSON, например {"all": [{"sub": "active"}, {"days_left": {"lte": 3}}]}.\n'
        "Атомы: role, lang, sub (none/trial/active/expired/frozen), days_left, balance_minor, has_paid, "
        "is_new, channel_member, ref_count, source, plan, flag:<имя>, segment:<тег>; any / all / not.\n"
        "«-» — убрать условие.\n\n/cancel — отмена"
    ),
    "wait_new_screen": (
        "➕ Новый экран. Пришлите название, например «Акция мая». Можно с кодом латиницей в начале — "
        "«may_sale Акция мая»: по коду на экран можно ссылаться из кнопок и диплинков.\n\n/cancel — отмена"
    ),
    "wait_rename": "✏️ Пришлите новое название экрана ({lang}).\n\n/cancel — отмена",
    "text_only": "Нужен текст. Медиа меняется кнопкой «🖼 Медиа».",
    "media_only": "Нужно фото, GIF или видео (не файлом-документом).",
    "too_big_download": (
        "Файл больше 20 МБ — Telegram не даёт боту его скачать. Сожмите его и пришлите снова."
    ),
    "download_failed": "Не удалось скачать файл из Telegram — попробуйте ещё раз.",
    "no_library": "Хранилище медиа не настроено.",
    "emoji_unknown": "Telegram не знает такой эмодзи — пришлите другой.",
    "emoji_check_failed": "Не удалось проверить эмодзи в Telegram — попробуйте ещё раз через минуту.",
    "bad_json": 'Это не JSON-объект. Пример: {"sub": "active"}',
    "preview_toast": "Это предпросмотр",
    "not_live": (
        "💾 Сохранено, но меню пока не обновилось — пользователи увидят изменение через несколько секунд."
    ),
    "too_many": f"На экране уже {MAX_BUTTONS} кнопок — больше Telegram не покажет. Удалите лишние.",
    "banner": "🖼 Заглушка (картинка по умолчанию): на экранах — {on}, экранов без картинки — {free}.",
    "banner_off": "🖼 Заглушка: убрать со всех экранов",
    "banner_on": "🖼 Вернуть заглушку",
    "banner_removed": "⚡ Применено: заглушка убрана с {n} экр. Свои картинки экранов не тронуты.",
    "banner_restored": "⚡ Применено: заглушка возвращена на {n} экр. без своей картинки.",
    "no_banner_file": "Файла заглушки нет в этой установке.",
}


class _Stop(Exception):  # control flow, not an error
    def __init__(self, result: HandlerResult) -> None:
        super().__init__("stop")
        self.result = result


@dataclass(slots=True)
class _Capture:
    kind: str  # text | media | label | icon | value | dsl | new_screen | rename
    data: dict[str, Any]
    back: tuple[str, str | None]
    prompt: str
    expires: float
    error: str | None = None


@dataclass(slots=True)
class _Draft:
    """A new button between its label and its action."""

    screen_id: int
    version: int
    label: dict[str, str]
    expires: float


@dataclass(slots=True)
class _Index:
    version: int = -1
    buttons: dict[int, tuple[ScreenEntry, Button]] = field(default_factory=dict)


def preview_user(base: UserCtx, state: str) -> UserCtx:
    """A plain user in ``state`` (for «Как видит…»), in the editor's language."""
    _title, fields = PREVIEW_STATES[state]
    return UserCtx(
        user_id=base.user_id,
        telegram_id=base.telegram_id,
        role="user",
        lang=base.lang,
        currency=base.currency,
        **fields,
    )


def _ids(arg: Any, n: int) -> list[str] | None:
    if not isinstance(arg, str):
        return None
    parts = arg.split(".")
    return parts if len(parts) == n else None


def _split_ref(arg: Any) -> tuple[str, str]:
    """``<ref>.<rest>`` of the action wizard: ``ref`` is ``n<screen id>`` (a new button) or ``<button id>.<the
    screen version the admin saw>``."""
    if not isinstance(arg, str):
        return "", ""
    parts = arg.split(".")
    k = 1 if arg.startswith("n") else 2
    return ".".join(parts[:k]), ".".join(parts[k:])


def _is_command(text: str) -> bool:
    """A bot command (``/cancel``, ``/start abc``) — not a text that merely starts with «/»."""
    return _COMMAND_RE.match(text.strip()) is not None


def _int(value: str | None) -> int | None:
    return int(value) if value is not None and _ID_RE.match(value) else None


def _first(labels: Mapping[str, str], lang: str = "ru") -> str:
    return labels.get(lang) or next((v for v in labels.values() if v.strip()), "") or "…"


def _title(entry: ScreenEntry) -> str:
    s = entry.screen
    return _first(s.title) if s.title else (s.code or f"#{s.id}")


def _describe_condition(cond: Mapping[str, Any] | None) -> str:
    if not cond:
        return "всегда"
    for title, dsl in CONDITION_PRESETS:
        if dsl == cond:
            return title
    text = json.dumps(cond, ensure_ascii=False, separators=(",", ":"))
    return text if len(text) <= 200 else text[:199] + "…"


ScreenFn = Callable[["ScreenCtx", Any], Awaitable[Any]]
OwnerIds = Callable[[], Awaitable[Iterable[int]]]
Downloader = Callable[[str], Awaitable[bytes]]


class ContentScreens:
    """Registers the constructor on a screen router."""

    def __init__(
        self,
        router: ScreenRouter,
        store: ContentStore,
        editor: ContentEditor,
        db: Database,
        *,
        edit_mode: EditMode | None = None,
        library: MediaLibrary | None = None,
        premium: PremiumService | None = None,
        owner_ids: OwnerIds | None = None,
        download: Downloader | None = None,
        langs: Callable[[], Sequence[str]] = lambda: ("ru", "en"),
        public_url: Callable[[], str | None] | None = None,
    ) -> None:
        self.router = router
        self.store = store
        self.editor = editor
        self.db = db
        self.edit_mode = edit_mode or EditMode()
        self.library = library
        self.premium = premium
        self.owner_ids = owner_ids
        self.download = download
        self.langs = langs
        self.public_url = public_url
        self._captures: dict[int, _Capture] = {}
        self._drafts: dict[int, _Draft] = {}
        self._notes: dict[int, str] = {}
        self._last: dict[int, tuple[str, int, float, str]] = {}  # user → (batch, screen, mono, summary)
        self._bulk: dict[int, tuple[str, float, str]] = {}  # user → (batch, mono, summary) of a banner batch
        self._index = _Index()
        self._installed = False
        self._locks: dict[int, asyncio.Lock] = {}
        self._albums: dict[int, tuple[str, float]] = {}  # user → (media_group_id, expires)

    # ------------------------------------------------------------ registration

    def install(self) -> None:
        if self._installed:
            return
        self._installed = True
        r = self.router
        self.edit_mode.install(r)
        guard: dict[str, Any] = {"required_role": "admin", "perm": PERM}
        screens: dict[str, ScreenFn] = {
            SCREEN_HOME: self._home_screen,
            SCREEN_LIST: self._list_screen,
            SCREEN_EDITOR: self._screen_card,
            SCREEN_BUTTONS: self._buttons_screen,
            SCREEN_BUTTON: self._button_card,
            SCREEN_STYLE: self._style_screen,
            SCREEN_ACTION: self._action_screen,
            SCREEN_TARGET: self._target_screen,
            SCREEN_SYSTEM: self._system_screen,
            SCREEN_MODULE: self._module_screen,
            SCREEN_COND: self._cond_screen,
            SCREEN_MOVE: self._move_screen,
            SCREEN_PREVIEW: self._preview_screen,
            SCREEN_HISTORY: self._history_screen,
            SCREEN_WAIT: self._wait_screen,
            SCREEN_DELETE: self._delete_screen,
        }
        for code, fn in screens.items():
            r.screen(code, **guard)(self._wrap(fn))
        actions: dict[str, ScreenFn] = {
            "mode": self._a_mode,
            "nb": self._a_new_button,
            "ns": self._a_new_screen,
            "tx": self._a_text,
            "md": self._a_media,
            "mrm": self._a_media_remove,
            "mm": self._a_media_mode,
            "rn": self._a_rename,
            "en": self._a_screen_enabled,
            "dely": self._a_delete_screen,
            "bnx": self._a_banner_off,
            "bnr": self._a_banner_on,
            "undo": self._a_undo,
            "cancel": self._a_cancel,
            "pr": self._a_probe,
            "b.lb": self._a_label,
            "b.ic": self._a_icon,
            "b.icx": self._a_icon_remove,
            "b.st": self._a_style,
            "b.at": self._a_action_type,
            "b.as": self._a_action_screen,
            "b.sy": self._a_action_system,
            "b.mo": self._a_action_module,
            "b.cd": self._a_condition,
            "b.cdx": self._a_condition_dsl,
            "b.en": self._a_button_enabled,
            "b.mv": self._a_move,
            "b.del": self._a_button_delete,
            "b.go": self._a_follow,
        }
        for name, fn in actions.items():
            r.action(ACTIONS, name, **guard)(self._wrap(fn))
        r.action(ACTIONS, "noop")(self._a_noop)

    def _wrap(self, fn: ScreenFn) -> ScreenFn:
        async def run(ctx: ScreenCtx, arg: Any) -> Any:
            try:
                return await fn(ctx, arg)
            except _Stop as stop:
                return stop.result

        run.__name__ = getattr(fn, "__name__", "content_handler")
        return run

    def aiogram_router(self, name: str = "svbg-content-editor") -> Router:
        """``/edit`` and the captured messages of the editor (include before the screen router)."""
        router = Router(name=name)

        async def on_edit(message: Message) -> None:
            if not await self.handle_edit_command(message):
                raise SkipHandler

        async def capturing(message: Message) -> bool:
            return self.capturing(message)

        async def on_message(message: Message) -> None:
            if not await self.handle_message(message):
                raise SkipHandler

        router.message.register(on_message, capturing)
        router.message.register(on_edit, Command("edit"))
        return router

    # ------------------------------------------------------------ helpers

    @property
    def snap(self) -> Any:
        return self.store.snapshot

    def _entry(self, arg: Any, *, screen: bool = True) -> ScreenEntry:
        sid = _int(arg) if isinstance(arg, str) else None
        entry = None if sid is None else self.store.get_screen(sid)
        if entry is None:
            raise _Stop(Redirect(SCREEN_LIST, toast=_T["not_found"]) if screen else Toast(_T["not_found"]))
        return entry

    def _button(self, bid: int | None) -> tuple[ScreenEntry, Button]:
        snap = self.snap
        if self._index.version != snap.version:
            index = _Index(snap.version)
            for entry in snap.by_id.values():
                for b in entry.screen.buttons:
                    if b.id is not None:
                        index.buttons[b.id] = (entry, b)
            self._index = index
        found = None if bid is None else self._index.buttons.get(bid)
        if found is None:
            raise _Stop(Redirect(SCREEN_LIST, toast=_T["not_found"]))
        return found

    def _langs(self, entry: ScreenEntry | None = None) -> list[str]:
        out: list[str] = []
        for lang in [*self.langs(), *((entry.screen.body.keys()) if entry else ())]:
            if lang not in out:
                out.append(lang)
        return out

    async def _owners(self) -> frozenset[int]:
        if self.owner_ids is None:
            return frozenset()
        try:
            return frozenset(await self.owner_ids())
        except Exception:  # noqa: BLE001 - fall back to stored roles only
            log.warning("owner ids are unavailable; using stored roles")
            return frozenset()

    async def actor_id(self, user: UserCtx) -> int:
        """Re-read the role now (04 §9.1): the editor must still have ``content.edit``."""
        owners = await self._owners()
        async with self.db.read() as conn:
            if user.telegram_id is not None:
                actor = await load_actor(conn, telegram_id=user.telegram_id, owner_ids=owners)
            else:
                actor = await load_actor(conn, user_id=user.user_id, owner_ids=owners)
        if actor is None or not actor.has_perm(PERM):
            log.info("content edit refused for user %s: no right", user.user_id)
            raise _Stop(Toast(DENIED, alert=True))
        return actor.user_id if actor.user_id is not None else user.user_id

    def _remember(self, user: UserCtx, result: EditResult) -> None:
        if result.bulk:
            self._bulk[user.user_id] = (result.batch_id, time.monotonic(), result.summary)
        elif result.screen_id is not None:
            self._last[user.user_id] = (result.batch_id, result.screen_id, time.monotonic(), result.summary)
        note = _T["applied"] if result.live else _T["not_live"]
        self._notes[user.user_id] = note + "".join(f"\n⚠️ {w}" for w in result.warnings)

    async def _save(
        self, ctx: ScreenCtx, op: Callable[[int], Awaitable[EditResult]], back: tuple[str, str | None]
    ) -> HandlerResult:
        """Run a write for a callback: rights → editor → «⚡ Применено» on the ``back`` screen."""
        actor = await self.actor_id(ctx.user)
        try:
            result = await op(actor)
        except StaleError as e:
            return Redirect(back[0], back[1], toast=e.message)
        except EditError as e:
            return Toast(e.message, alert=True)
        self._remember(ctx.user, result)
        return Redirect(back[0], back[1], toast=_T["applied"])

    def _note(self, user: UserCtx) -> str:
        note = self._notes.pop(user.user_id, None)
        return f"\n\n{note}" if note else ""

    def _undo_row(self, user: UserCtx, screen_id: int) -> list[InlineKeyboardButton]:
        last = self._last.get(user.user_id)
        if last is None:
            return []
        batch, sid, at, summary = last
        if sid != screen_id or time.monotonic() - at > UNDO_WINDOW.total_seconds():
            return []
        return [nav_button(f"↩️ Отменить: {summary[:40]}", ACTIONS, "undo", batch)]

    # ------------------------------------------------------------ home & list

    def entry_button(self, user: UserCtx) -> InlineKeyboardButton:
        """«✏️ Конструктор» as a button (the admin's «🎨 Оформление» links ``ce.home`` directly)."""
        on = self.edit_mode.is_on(user)
        return nav_button("✏️ Конструктор" + (" · вкл" if on else ""), SCREEN_HOME)

    def banner_counts(self) -> tuple[int, int]:
        """``(screens showing the default banner, screens without a picture that can get it)``."""
        snap = self.snap
        preview_ok = self.editor.preview_ok()
        on = free = 0
        for entry in snap.by_id.values():
            s = entry.screen
            if s.media_id is None:
                free += banner_mode(s.body, preview_ok) is not None
            elif is_banner(snap.get_media(s.media_id)):
                on += 1
        return on, free

    def _bulk_undo_row(self, user: UserCtx) -> list[InlineKeyboardButton]:
        last = self._bulk.get(user.user_id)
        if last is None or time.monotonic() - last[1] > UNDO_WINDOW.total_seconds():
            return []
        return [nav_button(f"↩️ Отменить: {last[2][:40]}", ACTIONS, "undo", last[0])]

    async def _home_screen(self, ctx: ScreenCtx, _arg: Any) -> View:
        on = self.edit_mode.is_on(ctx.user)
        premium = self.premium.state.label if self.premium is not None else "проверка не настроена"
        banner_on, banner_free = self.banner_counts()
        text = (
            _T["home"].format(mode="включён" if on else "выключен", premium=premium)
            + "\n\n"
            + _T["banner"].format(on=banner_on, free=banner_free)
            + self._note(ctx.user)
        )
        rows: list[list[InlineKeyboardButton]] = []
        undo = self._bulk_undo_row(ctx.user)
        if undo:
            rows.append(undo)
        rows += [
            [nav_button("✏️ Выключить режим правки" if on else "✏️ Включить режим правки", ACTIONS, "mode")],
            [nav_button("📋 Экраны", SCREEN_LIST), nav_button("➕ Новый экран", ACTIONS, "ns")],
            [nav_button("🔎 Проверить премиум-эмодзи", ACTIONS, "pr")],
        ]
        banner_row: list[InlineKeyboardButton] = []
        if banner_on:
            banner_row.append(nav_button(_T["banner_off"], ACTIONS, "bnx"))
        if banner_free:
            banner_row.append(nav_button(_T["banner_on"], ACTIONS, "bnr"))
        rows += [[b] for b in banner_row]
        rows.append(nav.back_row(SCREEN_HOME))
        return View(text=text, keyboard=rows)

    async def _list_screen(self, ctx: ScreenCtx, arg: Any) -> View:
        entries = sorted(
            self.snap.by_id.values(),
            key=lambda e: (e.screen.kind != "custom", e.screen.code or "", e.id),
        )
        page = _int(arg) or 0
        pages = max((len(entries) + PAGE - 1) // PAGE, 1)
        page = min(page, pages - 1)
        chunk = entries[page * PAGE : (page + 1) * PAGE]
        rows: list[list[InlineKeyboardButton]] = []
        for e in chunk:
            icon = "📄" if e.screen.kind == "custom" else "⚙️"
            off = " 🚫" if not e.screen.enabled else ""
            code = f" · {e.code}" if e.code else ""
            rows.append([nav_button(f"{icon} {_title(e)[:40]}{code}{off}", SCREEN_EDITOR, arg=str(e.id))])
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(nav_button("⬅️", SCREEN_LIST, arg=str(page - 1)))
        if page + 1 < pages:
            nav.append(nav_button("➡️", SCREEN_LIST, arg=str(page + 1)))
        if nav:
            rows.append(nav)
        rows.append([nav_button("➕ Новый экран", ACTIONS, "ns"), nav_button("⬅️ Назад", SCREEN_HOME)])
        text = (
            f"📋 Экраны ({len(entries)}), стр. {page + 1}/{pages}\n⚙️ — системные (путь покупки), 📄 — свои."
            + self._note(ctx.user)
        )
        return View(text=text, keyboard=rows)

    # ------------------------------------------------------------ screen card

    async def _screen_card(self, ctx: ScreenCtx, arg: Any) -> View:
        entry = self._entry(arg)
        return self.screen_view(ctx.user, entry)

    def screen_view(self, user: UserCtx, entry: ScreenEntry) -> View:
        s = entry.screen
        sid, ver = str(s.id), s.version
        media = self.snap.get_media(s.media_id)
        banner = is_banner(media)
        kind_ru = {"photo": "фото", "animation": "GIF", "video": "видео", "document": "файл"}
        media_line = f"{kind_ru.get(media.kind, media.kind)} #{media.id}" if media else "нет"
        if banner:
            media_line = "🖼 заглушка (картинка по умолчанию)"
        mode = "вложение" if s.media_mode == "attach" else "превью-ссылка"
        limit = CAPTION_LIMIT if media and self.editor.as_attachment(s.media_mode) else MAX_TEXT
        texts = ", ".join(f"{lang} ({utf16_len(b.text)}/{limit})" for lang, b in s.body.items()) or "нет"
        disabled = sum(1 for b in s.buttons if not b.enabled)
        conditional = sum(1 for b in s.buttons if b.visible_if)
        lines = [
            f"✏️ Экран «{_title(entry)}»",
            f"Код: {s.code or '—'} · {'системный' if s.kind == 'system' else 'свой'}"
            + ("" if s.enabled else " · 🚫 выключен"),
            f"Версия {ver}" + (f" · изменён {s.updated_at:%d.%m %H:%M} UTC" if s.updated_at else ""),
            f"Медиа: {media_line} · режим: {mode}",
            f"Тексты: {texts}",
            f"Кнопок: {len(s.buttons)} (выключено {disabled}, с условием {conditional})",
        ]
        if s.media_mode == "preview" and media and not (self.public_url and self.public_url()):
            lines.append("⚠️ PUBLIC_URL не задан — превью-ссылка не работает, медиа покажется вложением.")
        if banner:
            lines.append("Пришлите свою картинку кнопкой «🖼 Медиа» или уберите заглушку.")
        rows: list[list[InlineKeyboardButton]] = []
        undo = self._undo_row(user, s.id)
        if undo:
            rows.append(undo)
        rows.append(
            [
                nav_button(f"📝 Текст {lang.upper()}", ACTIONS, "tx", f"{sid}.{lang}")
                for lang in self._langs(entry)
            ]
        )
        media_row = [nav_button("🖼 Медиа", ACTIONS, "md", sid)]
        if s.media_id is not None:
            other = "превью-ссылка" if s.media_mode == "attach" else "вложение"
            media_row.append(nav_button(f"🔁 → {other}", ACTIONS, "mm", f"{sid}.{ver}"))
            remove = "🗑 Убрать картинку" if banner else "🗑 Убрать медиа"
            media_row.append(nav_button(remove, ACTIONS, "mrm", f"{sid}.{ver}"))
        rows.append(media_row)
        rows.append(
            [
                nav_button(f"🔘 Кнопки ({len(s.buttons)})", SCREEN_BUTTONS, arg=sid),
                nav_button("➕ Кнопка", ACTIONS, "nb", sid),
            ]
        )
        rows.append(
            [
                nav_button("👁 Как видит…", SCREEN_PREVIEW, arg=sid),
                nav_button("🕘 История", SCREEN_HISTORY, arg=sid),
            ]
        )
        manage = [nav_button("✏️ Название", ACTIONS, "rn", sid)]
        if s.kind == "custom":
            manage.append(
                nav_button("🚫 Выключить" if s.enabled else "✅ Включить", ACTIONS, "en", f"{sid}.{ver}")
            )
            manage.append(nav_button("🗑 Удалить", SCREEN_DELETE, arg=f"{sid}.{ver}"))
        rows.append(manage)
        rows.append([nav_button("▶️ Открыть экран", s.code or sid), nav_button("📋 Все экраны", SCREEN_LIST)])
        return View(text="\n".join(lines) + self._note(user), keyboard=rows)

    async def _buttons_screen(self, ctx: ScreenCtx, arg: Any) -> View:
        sid, _, page_s = arg.partition(".") if isinstance(arg, str) else ("", "", "")
        entry = self._entry(sid)
        pages = pages_of(button_rows(entry, ctx.user), PAGE_BUTTONS)
        page = min(_int(page_s) or 0, len(pages) - 1)
        rows = list(pages[page])
        if len(pages) > 1:
            nav: list[InlineKeyboardButton] = []
            if page > 0:
                nav.append(nav_button("⬅️", SCREEN_BUTTONS, arg=f"{entry.id}.{page - 1}"))
            nav.append(nav_button(f"{page + 1}/{len(pages)}", SCREEN_BUTTONS, arg=f"{entry.id}.{page}"))
            if page + 1 < len(pages):
                nav.append(nav_button("➡️", SCREEN_BUTTONS, arg=f"{entry.id}.{page + 1}"))
            rows.append(nav)
        rows.append(
            [
                nav_button("➕ Кнопка", ACTIONS, "nb", str(entry.id)),
                nav_button("⬅️ К экрану", SCREEN_EDITOR, arg=str(entry.id)),
            ]
        )
        text = (
            f"🔘 Кнопки экрана «{_title(entry)}» — как в Telegram, по рядам. "
            "Нажмите кнопку, чтобы изменить её.\n"
            f"{MARK_DISABLED.strip()} — выключена, {MARK_CONDITION.strip()} — видна по условию."
        )
        if len(pages) > 1:
            text += f"\nСтраница {page + 1} из {len(pages)}."
        return View(text=text + self._note(ctx.user), keyboard=rows)

    # ------------------------------------------------------------ screen actions

    def _sid_ver(self, arg: Any) -> tuple[ScreenEntry, int]:
        parts = _ids(arg, 2)
        ver = _int(parts[1]) if parts else None
        if parts is None or ver is None:
            raise _Stop(Toast(_T["not_found"]))
        return self._entry(parts[0], screen=False), ver

    def _start_capture(
        self, ctx: ScreenCtx, kind: str, data: dict[str, Any], back: tuple[str, str | None], prompt: str
    ) -> View:
        tg = ctx.user.telegram_id or ctx.chat_id
        self._captures[tg] = _Capture(kind, data, back, prompt, time.monotonic() + CAPTURE_TTL)
        return self._capture_view(prompt)

    @staticmethod
    def _capture_view(prompt: str, error: str | None = None) -> View:
        text = (f"⚠️ {error}\n\n" if error else "") + prompt
        return View(text=text, keyboard=[[nav_button("✖️ Отмена", ACTIONS, "cancel")]])

    async def _a_mode(self, ctx: ScreenCtx, _arg: Any) -> HandlerResult:
        await self.actor_id(ctx.user)
        state = self.edit_mode.toggle(ctx.user)
        if state is None:
            return Toast(DENIED, alert=True)
        if state:
            return Redirect(defaults.HOME, toast=_T["mode_on"])
        return Redirect(SCREEN_HOME, toast=_T["mode_off"])

    async def _a_text(self, ctx: ScreenCtx, arg: Any) -> View:
        parts = _ids(arg, 2)
        if parts is None:
            raise _Stop(Toast(_T["not_found"]))
        entry = self._entry(parts[0], screen=False)
        lang = parts[1]
        if lang not in self._langs(entry):
            raise _Stop(Toast(_T["not_found"]))
        return self._start_capture(
            ctx,
            "text",
            {"sid": entry.id, "ver": entry.screen.version, "lang": lang},
            (SCREEN_EDITOR, str(entry.id)),
            _T["wait_text"].format(lang=lang),
        )

    async def _a_media(self, ctx: ScreenCtx, arg: Any) -> View:
        entry = self._entry(arg, screen=False)
        if self.library is None or self.download is None:
            raise _Stop(Toast(_T["no_library"], alert=True))
        return self._start_capture(
            ctx,
            "media",
            {"sid": entry.id, "ver": entry.screen.version},
            (SCREEN_EDITOR, str(entry.id)),
            _T["wait_media"],
        )

    async def _a_media_remove(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        entry, ver = self._sid_ver(arg)
        summary = "Заглушка убрана" if is_banner(self.snap.get_media(entry.screen.media_id)) else None
        return await self._save(
            ctx,
            lambda actor: self.editor.set_media(
                entry.id, None, expected_version=ver, actor=actor, summary=summary
            ),
            (SCREEN_EDITOR, str(entry.id)),
        )

    async def _a_banner_off(self, ctx: ScreenCtx, _arg: Any) -> HandlerResult:
        """«🖼 Заглушка: убрать со всех экранов» — the owner's pictures stay."""
        actor = await self.actor_id(ctx.user)
        try:
            result = await self.editor.remove_banner(actor=actor)
        except EditError as e:
            return Toast(e.message, alert=True)
        self._remember(ctx.user, result)
        if result.live:
            self._notes[ctx.user.user_id] = _T["banner_removed"].format(n=result.changed)
        return Redirect(SCREEN_HOME, toast=_T["applied"])

    async def _a_banner_on(self, ctx: ScreenCtx, _arg: Any) -> HandlerResult:
        """«🖼 Вернуть заглушку» — on every screen without a picture of its own."""
        actor = await self.actor_id(ctx.user)
        if self.library is None:
            return Toast(_T["no_library"], alert=True)
        media_id = await ensure_banner_media(self.db, self.library.root)
        if media_id is None:
            return Toast(_T["no_banner_file"], alert=True)
        try:
            result = await self.editor.restore_banner(media_id, actor=actor)
        except EditError as e:
            return Toast(e.message, alert=True)
        self._remember(ctx.user, result)
        if result.live:
            self._notes[ctx.user.user_id] = _T["banner_restored"].format(n=result.changed) + "".join(
                f"\n⚠️ {w}" for w in result.warnings
            )
        return Redirect(SCREEN_HOME, toast=_T["applied"])

    async def _a_media_mode(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        entry, ver = self._sid_ver(arg)
        mode = "preview" if entry.screen.media_mode == "attach" else "attach"
        return await self._save(
            ctx,
            lambda actor: self.editor.set_media_mode(entry.id, mode, expected_version=ver, actor=actor),
            (SCREEN_EDITOR, str(entry.id)),
        )

    async def _a_rename(self, ctx: ScreenCtx, arg: Any) -> View:
        entry = self._entry(arg, screen=False)
        return self._start_capture(
            ctx,
            "rename",
            {"sid": entry.id, "ver": entry.screen.version, "lang": ctx.lang},
            (SCREEN_EDITOR, str(entry.id)),
            _T["wait_rename"].format(lang=ctx.lang),
        )

    async def _a_screen_enabled(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        entry, ver = self._sid_ver(arg)
        return await self._save(
            ctx,
            lambda actor: self.editor.set_screen_enabled(
                entry.id, not entry.screen.enabled, expected_version=ver, actor=actor
            ),
            (SCREEN_EDITOR, str(entry.id)),
        )

    async def _delete_screen(self, ctx: ScreenCtx, arg: Any) -> View:
        entry, ver = self._sid_ver(arg)
        text = f"🗑 Удалить экран «{_title(entry)}» вместе с его кнопками? Отменить можно 10 минут."
        refs, warnings = await self.editor.references(entry.id, entry.code)
        if refs:
            text += (
                "\n\n⛔ На экран ведут: " + "; ".join(refs[:5]) + ". Удаление откажет — сначала уберите их."
            )
        if warnings:
            text += (
                "\n\n⚠️ Ещё ведут: "
                + "; ".join(warnings[:5])
                + " — если их включат, они откроют «Меню обновилось»."
            )
        return View(
            text=text,
            keyboard=[
                [
                    nav_button("🗑 Да, удалить", ACTIONS, "dely", f"{entry.id}.{ver}", style="danger"),
                    nav_button("✖️ Нет", SCREEN_EDITOR, arg=str(entry.id)),
                ]
            ],
        )

    async def _a_delete_screen(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        entry, ver = self._sid_ver(arg)
        actor = await self.actor_id(ctx.user)
        try:
            result = await self.editor.delete_screen(entry.id, expected_version=ver, actor=actor)
        except EditError as e:
            return Toast(e.message, alert=True)
        self._remember(ctx.user, result)
        note = f"{_T['applied']}: экран удалён" if result.live else _T["not_live"]
        if result.warnings:
            note += "\n⚠️ На него ещё ведут: " + "; ".join(result.warnings[:5])
        self._notes[ctx.user.user_id] = note
        undo = [nav_button("↩️ Отменить удаление", ACTIONS, "undo", result.batch_id)]
        view = await self._list_screen(ctx, None)
        view.keyboard = [undo, *list(view.keyboard or [])]  # type: ignore[arg-type]
        view.toast = _T["applied"]
        return view

    async def _a_new_screen(self, ctx: ScreenCtx, _arg: Any) -> View:
        return self._start_capture(
            ctx, "new_screen", {"lang": ctx.lang}, (SCREEN_LIST, None), _T["wait_new_screen"]
        )

    async def _a_undo(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        if not isinstance(arg, str) or not _BATCH_RE.match(arg):
            return Toast(_T["not_found"])
        actor = await self.actor_id(ctx.user)
        try:
            result = await self.editor.undo(arg, actor=actor)
        except EditError as e:
            return Toast(e.message, alert=True)
        last = self._last.get(ctx.user.user_id)
        if last is not None and last[0] == arg:
            del self._last[ctx.user.user_id]
        bulk = self._bulk.get(ctx.user.user_id)
        if bulk is not None and bulk[0] == arg:
            del self._bulk[ctx.user.user_id]
        self._notes[ctx.user.user_id] = "↩️ Отменено" if result.live else _T["not_live"]
        if result.bulk:
            return Redirect(SCREEN_HOME, toast="↩️ Отменено")
        if result.screen_id is not None and self.store.get_screen(result.screen_id) is not None:
            return Redirect(SCREEN_EDITOR, str(result.screen_id), toast="↩️ Отменено")
        return Redirect(SCREEN_LIST, toast="↩️ Отменено")

    async def _a_cancel(self, ctx: ScreenCtx, _arg: Any) -> HandlerResult:
        cap = self._captures.pop(ctx.user.telegram_id or ctx.chat_id, None)
        self._drafts.pop(ctx.user.user_id, None)
        if cap is None:
            return Redirect(SCREEN_HOME, toast=_T["cancelled"])
        return Redirect(cap.back[0], cap.back[1], toast=_T["cancelled"])

    async def _a_probe(self, ctx: ScreenCtx, _arg: Any) -> HandlerResult:
        if self.premium is None:
            return Toast("Проверка не настроена", alert=True)
        await self.actor_id(ctx.user)
        state = await self.premium.probe(ctx.chat_id)
        self._notes[ctx.user.user_id] = state.label + (f"\n{state.detail}" if state.detail else "")
        return Redirect(SCREEN_HOME, toast=state.label[:190])

    async def _a_noop(self, _ctx: ScreenCtx, _arg: Any) -> HandlerResult:
        return Toast(_T["preview_toast"])

    # ------------------------------------------------------------ «Как видит…» & history

    async def _preview_screen(self, ctx: ScreenCtx, arg: Any) -> View:
        sid, _, state = (arg or "").partition(".") if isinstance(arg, str) else ("", "", "")
        entry = self._entry(sid)
        state = state if state in PREVIEW_STATES else "new"
        view = self.edit_mode.render_as(ctx, entry, preview_user(ctx.user, state))
        rows = _inert(view.keyboard)
        states = list(PREVIEW_STATES.items())
        for i in range(0, len(states), 3):
            rows.append(
                [
                    nav_button(
                        ("• " if code == state else "") + title, SCREEN_PREVIEW, arg=f"{entry.id}.{code}"
                    )
                    for code, (title, _f) in states[i : i + 3]
                ]
            )
        rows.append([nav_button("⬅️ К редактору", SCREEN_EDITOR, arg=str(entry.id))])
        view.keyboard = rows
        prefix = f"👁 Так видит: {PREVIEW_STATES[state][0]}\n\n"
        shift = len(prefix.encode("utf-16-le")) // 2
        view.text = prefix + view.text
        if view.entities:
            view.entities = [e.model_copy(update={"offset": e.offset + shift}) for e in view.entities]
        return view

    async def _history_screen(self, ctx: ScreenCtx, arg: Any) -> View:
        entry = self._entry(arg)
        items = await self.editor.history(entry.id, limit=10)
        from svbg.core import clock

        now = clock.now()
        lines = [f"🕘 История экрана «{_title(entry)}»"]
        rows: list[list[InlineKeyboardButton]] = []
        if not items:
            lines.append("Пока без изменений.")
        for it in items:
            mark = " (отменено)" if it.undone else ""
            who = f" · #{it.actor}" if it.actor else ""
            lines.append(f"• {it.ts:%d.%m %H:%M} {it.summary}{who}{mark}")
            if it.undoable(now, self.editor.undo_window) and len(rows) < 5:
                rows.append([nav_button(f"↩️ {it.summary[:48]}", ACTIONS, "undo", it.batch_id)])
        lines.append("\n«↩️» отменяет изменение в течение 10 минут, если экран после него не меняли.")
        rows.append([nav_button("⬅️ К экрану", SCREEN_EDITOR, arg=str(entry.id))])
        return View(text="\n".join(lines) + self._note(ctx.user), keyboard=rows)

    async def _wait_screen(self, ctx: ScreenCtx, _arg: Any) -> HandlerResult:
        cap = self._captures.get(ctx.user.telegram_id or ctx.chat_id)
        if cap is None:
            return Redirect(SCREEN_HOME)
        return self._capture_view(cap.prompt, cap.error)

    # ------------------------------------------------------------ button card

    def _bid(self, arg: Any) -> tuple[ScreenEntry, Button]:
        return self._button(_int(arg) if isinstance(arg, str) else None)

    def _bid_ver(self, arg: Any, n: int = 2) -> tuple[ScreenEntry, Button, int, list[str]]:
        parts = _ids(arg, n)
        ver = _int(parts[1]) if parts else None
        if parts is None or ver is None:
            raise _Stop(Toast(_T["not_found"]))
        entry, button = self._button(_int(parts[0]))
        return entry, button, ver, parts

    async def _button_card(self, ctx: ScreenCtx, arg: Any) -> View:
        entry, b = self._bid(arg)
        return self.button_view(ctx.user, entry, b)

    def _describe_action(self, b: Button) -> str:
        a = b.action
        match a:
            case ScreenAction(target=target):
                e = self.store.get_screen(target)
                return f"экран «{_title(e)}»" if e is not None else f"экран {target} (не найден!)"
            case SystemAction(name=name):
                return f"системное: {name}"
            case UrlAction(url=url):
                return f"ссылка {url}"
            case WebAppAction(url=url):
                return f"Web App {url}"
            case DeeplinkAction(code=code):
                return f"диплинк ?start={code}"
            case CopyAction(text=text):
                return f"копировать «{text[:60]}»"
            case ShareAction(text=text):
                return f"поделиться «{text[:60]}»"
            case ModuleAction(ext=ext, action=act):
                return f"модуль {ext}.{act}"
        return "?"  # pragma: no cover

    def button_view(self, user: UserCtx, entry: ScreenEntry, b: Button) -> View:
        assert b.id is not None
        bid, ver = str(b.id), entry.screen.version
        labels = ", ".join(f"{lang} «{text}»" for lang, text in b.label.items())
        lines = [
            f"🔘 Кнопка «{_first(b.label, user.lang)}» · экран «{_title(entry)}»",
            f"Текст: {labels}",
            f"Иконка: {b.icon_custom_emoji_id or 'нет'}",
            f"Цвет: {STYLE_LABELS.get(b.style, b.style or '')}",
            f"Действие: {self._describe_action(b)}",
            f"Ряд {b.row}, место {b.sort + 1}",
            f"Видна: {_describe_condition(b.visible_if)}",
            "Состояние: " + ("✅ включена" if b.enabled else "🚫 выключена"),
        ]
        if b.system_key:
            lines.append("⚙️ Системная кнопка: её действие менять и удалять нельзя (можно скрыть).")
        if self.premium is not None:
            st = self.premium.state
            if b.icon_custom_emoji_id or st.status == "stripped":
                lines.append(st.label)
            if b.icon_custom_emoji_id and st.warning:
                lines.append(st.warning)
        rows: list[list[InlineKeyboardButton]] = []
        undo = self._undo_row(user, entry.id)
        if undo:
            rows.append(undo)
        rows.append(
            [
                nav_button(f"🔤 Текст {lang.upper()}", ACTIONS, "b.lb", f"{bid}.{lang}")
                for lang in self._langs(entry)
            ]
        )
        icon_row = [nav_button("😀 Иконка", ACTIONS, "b.ic", bid)]
        if b.icon_custom_emoji_id:
            icon_row.append(nav_button("✖️ Без иконки", ACTIONS, "b.icx", f"{bid}.{ver}"))
        icon_row.append(nav_button("🎨 Цвет", SCREEN_STYLE, arg=bid))
        rows.append(icon_row)
        act_row = [
            nav_button("↕️ Позиция", SCREEN_MOVE, arg=bid),
            nav_button("👁 Условие", SCREEN_COND, arg=bid),
        ]
        if not b.system_key:
            act_row.insert(0, nav_button("⚡ Действие", SCREEN_ACTION, arg=f"{bid}.{ver}"))
        rows.append(act_row)
        state_row = [
            nav_button("🚫 Выключить" if b.enabled else "✅ Включить", ACTIONS, "b.en", f"{bid}.{ver}")
        ]
        if not b.system_key:
            state_row.append(nav_button("🗑 Удалить", ACTIONS, "b.del", f"{bid}.{ver}"))
        rows.append(state_row)
        nav = [nav_button("⬅️ К экрану", SCREEN_EDITOR, arg=str(entry.id))]
        if screen_action_target(b.action.to_json()) is not None:
            nav.append(nav_button("➡️ Перейти", ACTIONS, "b.go", bid))
        nav.append(nav_button("▶️ Открыть экран", entry.code or str(entry.id)))
        rows.append(nav)
        return View(text="\n".join(lines) + self._note(user), keyboard=rows)

    # ------------------------------------------------------------ button actions

    async def _update(
        self, ctx: ScreenCtx, entry: ScreenEntry, b: Button, ver: int, summary: str, **changes: Any
    ) -> HandlerResult:
        assert b.id is not None
        bid = b.id
        return await self._save(
            ctx,
            lambda actor: self.editor.update_button(
                bid, expected_version=ver, actor=actor, summary=summary, **changes
            ),
            (SCREEN_BUTTON, str(bid)),
        )

    async def _a_new_button(self, ctx: ScreenCtx, arg: Any) -> View:
        entry = self._entry(arg, screen=False)
        if len(entry.screen.buttons) >= MAX_BUTTONS:
            raise _Stop(Toast(_T["too_many"], alert=True))
        lang = ctx.lang if ctx.lang in self._langs(entry) else self._langs(entry)[0]
        return self._start_capture(
            ctx,
            "label",
            {"sid": entry.id, "ver": entry.screen.version, "lang": lang, "new": True},
            (SCREEN_EDITOR, str(entry.id)),
            "➕ Новая кнопка, шаг 1 из 2.\n" + _T["wait_label"].format(lang=lang),
        )

    async def _a_label(self, ctx: ScreenCtx, arg: Any) -> View:
        parts = _ids(arg, 2)
        if parts is None:
            raise _Stop(Toast(_T["not_found"]))
        entry, b = self._button(_int(parts[0]))
        lang = parts[1]
        if lang not in self._langs(entry):
            raise _Stop(Toast(_T["not_found"]))
        return self._start_capture(
            ctx,
            "label",
            {"bid": b.id, "ver": entry.screen.version, "lang": lang},
            (SCREEN_BUTTON, str(b.id)),
            _T["wait_label"].format(lang=lang),
        )

    async def _a_icon(self, ctx: ScreenCtx, arg: Any) -> View:
        entry, b = self._bid(arg)
        prompt = _T["wait_icon"]
        if self.premium is not None and self.premium.state.warning:
            prompt = f"{self.premium.state.warning}\n\n{prompt}"
        return self._start_capture(
            ctx, "icon", {"bid": b.id, "ver": entry.screen.version}, (SCREEN_BUTTON, str(b.id)), prompt
        )

    async def _a_icon_remove(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        entry, b, ver, _ = self._bid_ver(arg)
        return await self._update(ctx, entry, b, ver, "Иконка убрана", icon_custom_emoji_id=None)

    async def _style_screen(self, ctx: ScreenCtx, arg: Any) -> View:
        entry, b = self._bid(arg)
        bid, ver = str(b.id), entry.screen.version
        rows = [
            [
                nav_button(
                    ("✓ " if b.style == style else "") + STYLE_LABELS[style],
                    ACTIONS,
                    "b.st",
                    f"{bid}.{ver}.{style or 'x'}",
                    style=style,
                )
            ]
            for style in (None, "primary", "success", "danger")
        ]
        rows.append([nav_button("⬅️ Назад", SCREEN_BUTTON, arg=bid)])
        return View(text="🎨 Цвет кнопки (так он выглядит в Telegram):", keyboard=rows)

    async def _a_style(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        entry, b, ver, parts = self._bid_ver(arg, 3)
        style = None if parts[2] == "x" else parts[2]
        if style not in STYLE_LABELS:
            return Toast(_T["not_found"])
        return await self._update(ctx, entry, b, ver, f"Цвет: {STYLE_LABELS[style]}", style=style)

    def _draft_or_button(
        self, ref: str, user: UserCtx
    ) -> tuple[_Draft | None, ScreenEntry, Button | None, int]:
        """``n<screen id>`` → the new-button draft, ``<button id>.<version>`` → an existing button and the
        screen version the admin saw when opening «⚡ Действие» (the CAS of the whole wizard)."""
        if ref.startswith("n"):
            draft = self._drafts.get(user.user_id)
            sid = _int(ref[1:])
            if draft is None or draft.screen_id != sid or draft.expires < time.monotonic():
                raise _Stop(Redirect(SCREEN_EDITOR, ref[1:], toast=_T["draft_lost"]))
            return draft, self._entry(ref[1:]), None, draft.version
        parts = _ids(ref, 2)
        ver = _int(parts[1]) if parts else None
        if parts is None or ver is None:  # a wizard message of an older version
            bid = _int(ref.partition(".")[0])
            if bid is None:
                raise _Stop(Toast(_T["not_found"]))
            raise _Stop(Redirect(SCREEN_BUTTON, str(bid), toast=_T["draft_lost"]))
        entry, b = self._button(_int(parts[0]))
        if b.system_key:
            raise _Stop(Toast("У системной кнопки действие менять нельзя", alert=True))
        return None, entry, b, ver

    async def _action_screen(self, ctx: ScreenCtx, arg: Any) -> View:
        ref = arg if isinstance(arg, str) else ""
        draft, entry, b, _ver = self._draft_or_button(ref, ctx.user)
        rows = [[nav_button(title, ACTIONS, "b.at", f"{ref}.{code}")] for code, title in ACTION_TYPES]
        back = (SCREEN_EDITOR, str(entry.id)) if draft or b is None else (SCREEN_BUTTON, str(b.id))
        rows.append([nav_button("✖️ Отмена" if draft else "⬅️ Назад", back[0], arg=back[1])])
        head = "➕ Новая кнопка, шаг 2 из 2.\n" if draft else ""
        current = f"\nСейчас: {self._describe_action(b)}" if b else ""
        return View(text=f"{head}⚡ Что делает кнопка?{current}", keyboard=rows)

    async def _a_action_type(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        ref, kind = _split_ref(arg)
        _draft, _entry, b, ver = self._draft_or_button(ref, ctx.user)
        if kind == "screen":
            return Redirect(SCREEN_TARGET, f"{ref}.0")
        if kind == "system":
            return Redirect(SCREEN_SYSTEM, ref)
        if kind == "module":
            return Redirect(SCREEN_MODULE, ref)
        if kind not in _VALUE_PROMPTS:
            return Toast(_T["not_found"])
        data: dict[str, Any] = {"type": kind, "ref": ref, "ver": ver, "bid": b.id if b else None}
        # an existing button returns to its card: after a conflict the old version must not be reused
        back = (SCREEN_BUTTON, str(b.id)) if b is not None else (SCREEN_ACTION, ref)
        return self._start_capture(ctx, "value", data, back, _VALUE_PROMPTS[kind] + "\n\n/cancel — отмена")

    async def _target_screen(self, ctx: ScreenCtx, arg: Any) -> View:
        ref, page_s = _split_ref(arg)
        self._draft_or_button(ref, ctx.user)
        entries = sorted(
            self.snap.by_id.values(), key=lambda e: (e.screen.kind != "custom", e.code or "", e.id)
        )
        page = _int(page_s) or 0
        pages = max((len(entries) + PAGE - 1) // PAGE, 1)
        page = min(page, pages - 1)
        rows = [
            [
                nav_button(
                    f"{_title(e)[:40]}" + (f" · {e.code}" if e.code else ""), ACTIONS, "b.as", f"{ref}.{e.id}"
                )
            ]
            for e in entries[page * PAGE : (page + 1) * PAGE]
        ]
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(nav_button("⬅️", SCREEN_TARGET, arg=f"{ref}.{page - 1}"))
        if page + 1 < pages:
            nav.append(nav_button("➡️", SCREEN_TARGET, arg=f"{ref}.{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([nav_button("⬅️ Назад", SCREEN_ACTION, arg=ref)])
        return View(text=f"📄 На какой экран ведёт кнопка? (стр. {page + 1}/{pages})", keyboard=rows)

    @staticmethod
    def _grid(items: Iterable[InlineKeyboardButton], width: int) -> list[list[InlineKeyboardButton]]:
        rows: list[list[InlineKeyboardButton]] = []
        for item in items:
            if not rows or len(rows[-1]) == width:
                rows.append([])
            rows[-1].append(item)
        return rows

    async def _system_screen(self, ctx: ScreenCtx, arg: Any) -> View:
        ref = arg if isinstance(arg, str) else ""
        self._draft_or_button(ref, ctx.user)
        names = self.system_actions()
        rows = self._grid((nav_button(name, ACTIONS, "b.sy", f"{ref}.{name}") for name in names), 3)
        rows.append([nav_button("⬅️ Назад", SCREEN_ACTION, arg=ref)])
        text = "⚙️ Системное действие (покупка, продление, пополнение, подключение…):"
        if not names:
            text = "Системных действий пока нет."
        return View(text=text, keyboard=rows)

    def system_actions(self) -> list[str]:
        registered = getattr(self.router, "_actions", {})
        return sorted({a for (s, a) in registered if s == "sys"})

    def module_actions(self) -> list[str]:
        """``<ext>.<action>`` of the modules' user actions (no role or right required)."""
        registered = getattr(self.router, "_actions", {})
        out: set[str] = set()
        for (screen, name), route in registered.items():
            access = getattr(route, "access", None)
            if screen != MODULE_SCREEN or "." not in name:
                continue
            if access is not None and (access.required_role is not None or access.perm is not None):
                continue
            out.add(name)
        return sorted(out)

    async def _module_screen(self, ctx: ScreenCtx, arg: Any) -> View:
        ref = arg if isinstance(arg, str) else ""
        self._draft_or_button(ref, ctx.user)
        buttons: list[InlineKeyboardButton] = []
        for name in self.module_actions():
            try:
                buttons.append(nav_button(name, ACTIONS, "b.mo", f"{ref}.{name}"))
            except ValueError:  # a name too long for callback data: cannot be picked here
                log.warning("module action %s does not fit a callback", name)
        rows = self._grid(buttons, 2)
        rows.append([nav_button("⬅️ Назад", SCREEN_ACTION, arg=ref)])
        text = "🧩 Действие модуля (кнопка запустит его у пользователя):"
        if not buttons:
            text = "Модули пока не дают действий для кнопок."
        return View(text=text, keyboard=rows)

    async def _apply_action(self, ctx: ScreenCtx, ref: str, action: dict[str, Any]) -> HandlerResult:
        draft, entry, b, ver = self._draft_or_button(ref, ctx.user)
        try:
            parse_action(action)
        except ContentError as e:
            return Toast(f"Не подходит: {e.message}", alert=True)
        if draft is not None:
            actor = await self.actor_id(ctx.user)
            try:
                result = await self.editor.add_button(
                    draft.screen_id,
                    label=draft.label,
                    action=action,
                    expected_version=draft.version,
                    actor=actor,
                )
            except EditError as e:
                if isinstance(e, StaleError):
                    self._drafts.pop(ctx.user.user_id, None)
                    return Redirect(SCREEN_EDITOR, str(draft.screen_id), toast=e.message)
                return Toast(e.message, alert=True)
            self._drafts.pop(ctx.user.user_id, None)
            self._remember(ctx.user, result)
            if result.live:
                self._notes[ctx.user.user_id] = (
                    f"{_T['applied']}: кнопка добавлена. Настройте иконку, цвет, условие и позицию здесь."
                )
            return Redirect(SCREEN_BUTTON, str(result.created_id), toast=_T["applied"])
        assert b is not None
        return await self._update(ctx, entry, b, ver, "Действие кнопки", action=action)

    async def _a_action_screen(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        ref, sid = _split_ref(arg)
        target = self.store.get_screen(_int(sid) or 0)
        if target is None:
            return Toast(_T["not_found"])
        return await self._apply_action(ctx, ref, {"type": "screen", "target": target.code or str(target.id)})

    async def _a_action_system(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        ref, name = _split_ref(arg)
        if name not in self.system_actions():
            return Toast(_T["not_found"])
        return await self._apply_action(ctx, ref, {"type": "system", "name": name})

    async def _a_action_module(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        ref, name = _split_ref(arg)
        if name not in self.module_actions():
            return Toast(_T["not_found"])
        ext, _, act = name.partition(".")
        return await self._apply_action(ctx, ref, {"type": "module", "ext": ext, "action": act})

    async def _cond_screen(self, ctx: ScreenCtx, arg: Any) -> View:
        entry, b = self._bid(arg)
        bid, ver = str(b.id), entry.screen.version
        rows = [
            [
                nav_button(
                    ("✓ " if (dsl or None) == (b.visible_if or None) else "") + title,
                    ACTIONS,
                    "b.cd",
                    f"{bid}.{ver}.{i}",
                )
            ]
            for i, (title, dsl) in enumerate(CONDITION_PRESETS)
        ]
        rows.append([nav_button("✍️ Своё условие (DSL)", ACTIONS, "b.cdx", bid)])
        rows.append([nav_button("⬅️ Назад", SCREEN_BUTTON, arg=bid)])
        text = (
            f"👁 Кому видна кнопка? Сейчас: {_describe_condition(b.visible_if)}.\n"
            "Проверка идёт на каждом клике по уже загруженным данным — без запросов к базе."
        )
        return View(text=text, keyboard=rows)

    async def _a_condition(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        entry, b, ver, parts = self._bid_ver(arg, 3)
        idx = _int(parts[2])
        if idx is None or idx >= len(CONDITION_PRESETS):
            return Toast(_T["not_found"])
        title, dsl = CONDITION_PRESETS[idx]
        return await self._update(ctx, entry, b, ver, f"Условие: {title}", visible_if=dsl)

    async def _a_condition_dsl(self, ctx: ScreenCtx, arg: Any) -> View:
        entry, b = self._bid(arg)
        current = json.dumps(b.visible_if, ensure_ascii=False) if b.visible_if else "нет"
        return self._start_capture(
            ctx,
            "dsl",
            {"bid": b.id, "ver": entry.screen.version},
            (SCREEN_COND, str(b.id)),
            f"Сейчас: {current}\n\n" + _T["wait_dsl"],
        )

    async def _a_button_enabled(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        entry, b, ver, _ = self._bid_ver(arg)
        return await self._update(
            ctx,
            entry,
            b,
            ver,
            "Кнопка включена" if not b.enabled else "Кнопка выключена",
            enabled=not b.enabled,
        )

    async def _move_screen(self, ctx: ScreenCtx, arg: Any) -> View:
        entry, b = self._bid(arg)
        bid, ver = str(b.id), entry.screen.version
        lines = [f"↕️ Позиция кнопки «{_first(b.label, ctx.lang)}»: ряд {b.row}, место {b.sort + 1}.", ""]
        by_row: dict[int, list[str]] = {}
        for x in sorted(entry.screen.buttons, key=lambda x: (x.row, x.sort, x.id or 0)):
            name = _first(x.label, ctx.lang)[:16]
            by_row.setdefault(x.row, []).append(f"[{name}]" if x.id == b.id else name)
        lines.extend(f"Ряд {r}: " + " | ".join(names) for r, names in by_row.items())
        lines.append(f"\nВ ряду — не больше {MAX_ROW_WIDTH} кнопок.")
        rows = [
            [
                nav_button("⬅️", ACTIONS, "b.mv", f"{bid}.{ver}.left"),
                nav_button("⬆️", ACTIONS, "b.mv", f"{bid}.{ver}.up"),
                nav_button("⬇️", ACTIONS, "b.mv", f"{bid}.{ver}.down"),
                nav_button("➡️", ACTIONS, "b.mv", f"{bid}.{ver}.right"),
            ],
            [nav_button("✅ Готово", SCREEN_BUTTON, arg=bid)],
        ]
        return View(text="\n".join(lines) + self._note(ctx.user), keyboard=rows)

    async def _a_move(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        entry, b, ver, parts = self._bid_ver(arg, 3)
        direction = parts[2]
        if direction not in ("left", "right", "up", "down"):
            return Toast(_T["not_found"])
        assert b.id is not None
        bid = b.id
        result = await self._save(
            ctx,
            lambda actor: self.editor.move_button(bid, direction, expected_version=ver, actor=actor),
            (SCREEN_MOVE, str(bid)),
        )
        _ = entry
        return result

    async def _a_button_delete(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        entry, b, ver, _ = self._bid_ver(arg)
        assert b.id is not None
        bid = b.id
        return await self._save(
            ctx,
            lambda actor: self.editor.delete_button(bid, expected_version=ver, actor=actor),
            (SCREEN_EDITOR, str(entry.id)),
        )

    async def _a_follow(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        _entry, b = self._bid(arg)
        target = screen_action_target(b.action.to_json())
        if target is None or self.store.get_screen(target) is None:
            return Toast(_T["not_found"])
        return Redirect(target)

    # ------------------------------------------------------------ /edit

    async def _load(self, message: Message) -> UserCtx | None:
        if message.from_user is None or message.chat.type != "private":
            return None
        try:
            return await self.router.user_loader(message.from_user)
        except Exception:
            log.exception("user loader failed in the content editor")
            return None

    async def handle_edit_command(self, message: Message) -> bool:
        user = await self._load(message)
        if user is None or not can_edit(user):
            return False
        try:
            await self.actor_id(user)
        except _Stop:
            return False
        state = self.edit_mode.toggle(user)
        if state is None:
            return False
        screen = defaults.HOME if state else SCREEN_HOME
        toast = _T["mode_on"] if state else _T["mode_off"]
        self._notes[user.user_id] = toast
        await self.router.show(user, message.chat.id, screen, new=True)
        return True

    # ------------------------------------------------------------ captured messages

    def _album_tail(self, message: Message) -> bool:
        """The rest of an album whose first file was already taken (one file per screen)."""
        if message.from_user is None or message.media_group_id is None:
            return False
        seen = self._albums.get(message.from_user.id)
        return seen is not None and seen[0] == message.media_group_id and seen[1] > time.monotonic()

    def capturing(self, message: Message) -> bool:
        if message.from_user is None or message.chat.type != "private":
            return False
        if self._album_tail(message):
            return True
        cap = self._captures.get(message.from_user.id)
        if cap is None:
            return False
        if cap.expires < time.monotonic():
            self._captures.pop(message.from_user.id, None)
            return False
        return True

    async def handle_message(self, message: Message) -> bool:
        """A message the editor sent for a pending capture. ``False``: not ours (the update goes on).

        Messages of one editor are handled one at a time (an album's files arrive together); the files of an
        album after the first are swallowed.
        """
        if message.from_user is None:
            return False
        tg = message.from_user.id
        lock = self._locks.setdefault(tg, asyncio.Lock())
        async with lock:
            if self._album_tail(message):
                return True
            if not self.capturing(message):
                return False
            if message.media_group_id is not None:
                self._albums[tg] = (message.media_group_id, time.monotonic() + ALBUM_TTL)
            return await self._handle_captured(message, tg)

    async def _handle_captured(self, message: Message, tg: int) -> bool:
        user = await self._load(message)
        if user is None or not can_edit(user):
            self._captures.pop(tg, None)
            return False
        cap = self._captures[tg]
        text = message.text
        if text is not None and _is_command(text):
            self._captures.pop(tg, None)
            if text.split()[0].split("@")[0].lower() != "/cancel":
                return False  # another command abandons the capture
            self._notes[user.user_id] = _T["cancelled"]
            await self.router.show(user, message.chat.id, cap.back[0], cap.back[1], new=True)
            return True
        try:
            actor = await self.actor_id(user)
        except _Stop:
            self._captures.pop(tg, None)
            await self.router.show(user, message.chat.id, SCREEN_HOME, new=True, toast=DENIED)
            return True
        try:
            screen, arg = await self._consume(user, actor, cap, message)
        except EditError as e:
            if isinstance(e, StaleError):
                self._captures.pop(tg, None)
                self._drafts.pop(user.user_id, None)
                self._notes[user.user_id] = e.message
                await self.router.show(user, message.chat.id, cap.back[0], cap.back[1], new=True)
                return True
            cap.error = e.message
            cap.expires = time.monotonic() + CAPTURE_TTL
            await self.router.show(user, message.chat.id, SCREEN_WAIT, new=True)
            return True
        self._captures.pop(tg, None)
        await self.router.show(user, message.chat.id, screen, arg, new=True)
        return True

    async def _consume(
        self, user: UserCtx, actor: int, cap: _Capture, message: Message
    ) -> tuple[str, str | None]:
        """Apply the captured message; returns the screen to show. :class:`EditError` re-asks."""
        d = cap.data
        kind = cap.kind
        if kind == "media":
            return await self._consume_media(user, actor, cap, message)
        if kind == "icon":
            return await self._consume_icon(user, actor, cap, message)
        if message.text is None:
            raise EditError("text_only", _T["text_only"])
        text = message.text
        if kind == "text":
            result = await self.editor.set_text(
                d["sid"],
                d["lang"],
                text,
                entities_json(message.entities),
                expected_version=d["ver"],
                actor=actor,
            )
            self._remember(user, result)
            return SCREEN_EDITOR, str(d["sid"])
        if kind == "rename":
            result = await self.editor.set_title(
                d["sid"], {d["lang"]: text.strip()[:256]}, expected_version=d["ver"], actor=actor
            )
            self._remember(user, result)
            return SCREEN_EDITOR, str(d["sid"])
        if kind == "new_screen":
            head, _, rest = text.strip().partition(" ")
            code: str | None = None
            title = text.strip()
            if rest.strip() and _CODE_RE.match(head):
                code, title = head, rest.strip()
            result = await self.editor.create_screen(title, code=code, lang=d.get("lang", "ru"), actor=actor)
            self._remember(user, result)
            if result.live:
                self._notes[user.user_id] = f"{_T['applied']}: экран создан. Пришлите ему текст и медиа."
            return SCREEN_EDITOR, str(result.screen_id)
        if kind == "label":
            label = text.strip()
            if not label:
                raise EditError("empty", "Пустой текст кнопки.")
            if len(label) > MAX_LABEL:
                raise EditError("too_long", f"Слишком длинно: до {MAX_LABEL} символов.")
            if d.get("new"):
                self._drafts[user.user_id] = _Draft(
                    d["sid"], d["ver"], {d["lang"]: label}, time.monotonic() + CAPTURE_TTL
                )
                return SCREEN_ACTION, f"n{d['sid']}"
            result = await self.editor.update_button(
                d["bid"],
                expected_version=d["ver"],
                actor=actor,
                summary=f"Текст кнопки ({d['lang']})",
                label={d["lang"]: label},
            )
            self._remember(user, result)
            return SCREEN_BUTTON, str(d["bid"])
        if kind == "value":
            action = {"type": d["type"], _ACTION_KEY[d["type"]]: text.strip()}
            try:
                parse_action(action)
            except ContentError as e:
                raise EditError("invalid", f"Не подходит: {e.message}") from None
            ref = d["ref"]
            if ref.startswith("n"):
                draft = self._drafts.get(user.user_id)
                if draft is None or str(draft.screen_id) != ref[1:]:
                    raise StaleError(int(ref[1:]) if ref[1:].isdigit() else 0, -1)
                result = await self.editor.add_button(
                    draft.screen_id,
                    label=draft.label,
                    action=action,
                    expected_version=draft.version,
                    actor=actor,
                )
                self._drafts.pop(user.user_id, None)
                self._remember(user, result)
                if result.live:
                    self._notes[user.user_id] = (
                        f"{_T['applied']}: кнопка добавлена. Настройте иконку, цвет, условие и позицию здесь."
                    )
                return SCREEN_BUTTON, str(result.created_id)
            result = await self.editor.update_button(
                d["bid"], expected_version=d["ver"], actor=actor, summary="Действие кнопки", action=action
            )
            self._remember(user, result)
            return SCREEN_BUTTON, str(d["bid"])
        if kind == "dsl":
            raw = text.strip()
            cond: dict[str, Any] | None
            if raw in ("-", "—", "{}", "нет"):
                cond = None
            else:
                try:
                    parsed = json.loads(raw)
                except ValueError:
                    raise EditError("bad_json", _T["bad_json"]) from None
                if not isinstance(parsed, dict):
                    raise EditError("bad_json", _T["bad_json"])
                try:
                    compile_condition(parsed)
                except ConditionError as e:
                    raise EditError("bad_dsl", f"Ошибка в условии: {e}") from None
                cond = parsed
            result = await self.editor.update_button(
                d["bid"], expected_version=d["ver"], actor=actor, summary="Условие (DSL)", visible_if=cond
            )
            self._remember(user, result)
            return SCREEN_BUTTON, str(d["bid"])
        raise EditError("unknown", "Неизвестный шаг мастера — начните заново.")  # pragma: no cover

    async def _consume_media(
        self, user: UserCtx, actor: int, cap: _Capture, message: Message
    ) -> tuple[str, str | None]:
        d = cap.data
        if self.library is None or self.download is None:
            raise EditError("no_library", _T["no_library"])
        if message.animation is not None:
            obj: Any = message.animation
            kind, width, height, duration = "animation", obj.width, obj.height, obj.duration
        elif message.photo:
            obj = message.photo[-1]
            kind, width, height, duration = "photo", obj.width, obj.height, None
        elif message.video is not None:
            obj = message.video
            kind, width, height, duration = "video", obj.width, obj.height, obj.duration
        else:
            raise EditError("media_only", _T["media_only"])
        size = getattr(obj, "file_size", None)
        if size is not None and size > DOWNLOAD_LIMIT:
            raise EditError("too_big", _T["too_big_download"])
        try:
            self.library.check_declared(kind, size)
        except MediaError as e:
            raise EditError(e.code, e.message) from None
        try:
            data = await self.download(obj.file_id)
        except Exception as e:  # noqa: BLE001 - network/Telegram failures are re-asked, not crashed
            log.warning("media download failed: %s", type(e).__name__)
            raise EditError("download", _T["download_failed"]) from None
        try:
            stored = await self.library.add(
                data,
                kind,
                bot_id=self.router.transport.bot_id,
                file_id=obj.file_id,
                width=width,
                height=height,
                duration=duration,
            )
        except MediaError as e:
            raise EditError(e.code, e.message) from None
        result = await self.editor.set_media(
            d["sid"], stored.media.id, expected_version=d["ver"], actor=actor
        )
        self._remember(user, result)
        return SCREEN_EDITOR, str(d["sid"])

    async def _consume_icon(
        self, user: UserCtx, actor: int, cap: _Capture, message: Message
    ) -> tuple[str, str | None]:
        d = cap.data
        pick = pick_icon(message)
        if pick.error is not None or pick.emoji_id is None:
            raise EditError("not_emoji", pick.error or _T["emoji_unknown"])
        known = await check_custom_emoji(self.router.transport, pick.emoji_id, message.chat.id)
        if known is None:
            raise EditError("check_failed", _T["emoji_check_failed"])
        if not known:
            raise EditError("unknown_emoji", _T["emoji_unknown"])
        result = await self.editor.update_button(
            d["bid"],
            expected_version=d["ver"],
            actor=actor,
            summary="Иконка",
            icon_custom_emoji_id=pick.emoji_id,
        )
        self._remember(user, result)
        if self.premium is not None:
            state = await self.premium.probe(message.chat.id, pick.emoji_id)
            note = f"{_T['applied'] if result.live else _T['not_live']}\n{state.label}"
            if state.warning:
                note += f"\n{state.warning}"
            self._notes[user.user_id] = note
        return SCREEN_BUTTON, str(d["bid"])

    def probe_sender(self) -> TransportProbeSender:
        return TransportProbeSender(self.router.transport)


def _inert(keyboard: Any) -> list[list[InlineKeyboardButton]]:
    """Preview keyboard: same look, but callback buttons only answer «Это предпросмотр»."""
    rows = keyboard.inline_keyboard if isinstance(keyboard, InlineKeyboardMarkup) else (keyboard or [])
    noop = codec.encode(ACTIONS, "noop")
    out: list[list[InlineKeyboardButton]] = []
    for row in rows:
        out.append(
            [b.model_copy(update={"callback_data": noop}) if b.callback_data is not None else b for b in row]
        )
    return out
