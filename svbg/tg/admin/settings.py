"""Settings screens generated from the registry, search and the owner's ``/set`` (03 §7.2–7.3, 07 §3.6).

Screens (all code-defined, registered on a :class:`~svbg.tg.ui.router.ScreenRouter`):

* ``settings_root`` — «🔎 Все настройки» (admin → «⚙️ Система»): «🔎 Поиск» first, then the sections of the
  registry the user may see (component status in the label for the owner); overrides the placeholder content
  screen of the same code. The cash desks are not listed there: «Платёжки» leads to «🏦 Кассы»;
* ``set.sec`` — keys of one section, paginated; «Расширенные» keys are behind a button;
* ``set.v`` — a *slice* (:mod:`svbg.tg.admin.slices`): the keys of one admin section (trial days next to the
  plans, notifications under «📣 Связь»…), switches toggled in place (``set:vtog``);
* ``set.key`` — the card of one key: title, description, current value (secrets as ``••••a1B9`` plus a keyed
  fingerprint), default, source, how a change applies, edit controls (enum values by their labels, ready
  values as buttons), the ``.env`` name on the last line. «⬅️» leads to the key's slice (``KEY@t``: back to
  the registry tree);
* ``set.hist`` — the last changes of a key (secrets only as fingerprints);
* ``set.find`` / ``set.done`` — search results and the result of ``/set`` (shown outside a callback).

Every change goes through :meth:`SettingsService.apply` (source ``bot``) and answers honestly:
«⚡ Применено · ↩️ Отменить» (undo by ``batch_id``, the button works for 10 minutes) or «❌ Не применено» with
the reason (validation error, ``ProbeError`` text, database outage …).

Access (04 §9.1, 03 §6.3) is checked on **every** callback and input, not only by hiding buttons: the owner
sees everything; an admin with ``settings.business`` sees business keys only (not secrets, not bootstrap, not
``owner_only``). Callback data can be forged by a client, so every key, choice and batch id taken from a
callback is re-validated here.

Secrets typed by the owner are deleted from the chat right after reading (the form engine does it for edit
forms; ``/set`` does it itself and warns when Telegram refuses).
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import inspect
import logging
import zoneinfo
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, NoReturn, Protocol, TypeVar

from aiogram import Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.methods import DeleteMessage, SendMessage
from aiogram.types import InlineKeyboardButton, Message
from sqlalchemy.exc import SQLAlchemyError

from svbg.content import defaults
from svbg.core import clock
from svbg.core.component import Health
from svbg.core.errors import timeout_guard
from svbg.core.ids import is_uuid7
from svbg.core.log import mask, register_secret
from svbg.core.settings import labels, values
from svbg.core.settings.registry import PAYMENTS_SECTION, Apply, SettingDef
from svbg.core.settings.service import RESET, ApplyResult, Change, SettingsError
from svbg.tg.admin import nav, slices
from svbg.tg.ui import codec as codec_mod
from svbg.tg.ui import texts as ui_texts
from svbg.tg.ui.forms import Field, Form, ValidationError
from svbg.tg.ui.forms import text as text_validator
from svbg.tg.ui.renderer import nav_button
from svbg.tg.ui.view import Redirect, Toast, View

if TYPE_CHECKING:
    from aiogram.types import User as TgUser

    from svbg.core.settings.service import SettingsService
    from svbg.core.settings.store import AuditEntry
    from svbg.tg.ui.context import UserCtx
    from svbg.tg.ui.router import HandlerResult, ScreenCtx, ScreenRouter

__all__ = [
    "ACTIONS",
    "PERM_BUSINESS",
    "SCREEN_DONE",
    "SCREEN_FIND",
    "SCREEN_HISTORY",
    "SCREEN_KEY",
    "SCREEN_ROOT",
    "SCREEN_SECTION",
    "SCREEN_SLICE",
    "SettingsScreens",
    "can_edit_keys",
    "can_open_settings",
    "can_view",
    "form_name",
    "is_business",
    "setup",
]

log = logging.getLogger("svbg.tg.admin")

_R = TypeVar("_R")

PERM_BUSINESS: Final = "settings.business"

SCREEN_ROOT: Final = defaults.SETTINGS_ROOT  # replaces the placeholder content screen
SCREEN_SECTION: Final = "set.sec"
SCREEN_KEY: Final = "set.key"
SCREEN_HISTORY: Final = "set.hist"
SCREEN_FIND: Final = "set.find"
SCREEN_DONE: Final = "set.done"
SCREEN_SLICE: Final = slices.SCREEN
SCREEN_KASSAS: Final = "apay"  # svbg.tg.admin.payments.SCREEN_LIST
ACTIONS: Final = "set"  # callback namespace of the actions below
A_EDIT: Final = "edit"
A_TOGGLE: Final = "tog"
A_PICK: Final = "pick"
A_RESET: Final = "def"
A_UNDO: Final = "undo"
A_SEARCH: Final = "find"
A_VTOGGLE: Final = "vtog"  # a switch toggled on its slice (arg ``<slice>:<KEY>``)
TREE_MARK: Final = "@t"  # ``set.key`` arg suffix: the card was opened from the registry tree
FORM_SEARCH: Final = "set.search"
FORM_PREFIX: Final = "set.e."

PAGE_SIZE: Final = 10
SEARCH_LIMIT: Final = 8
HISTORY_LIMIT: Final = 10
MAX_VALUE_CHARS: Final = 300  # longest value shown in a card (lists can be long)
MAX_LABEL_CHARS: Final = 60
_RESULT_CACHE: Final = 512
_GROUP_WARN_COOLDOWN: Final = 60.0  # one "delete your secret" warning per group per minute
_GROUP_WARN_CACHE: Final = 1024

# Owner-facing texts (Russian), in one place.
_T: Final[dict[str, str]] = {
    "root_title": "🔎 <b>Все настройки</b>",
    "root_hint": "Все настройки бота по разделам, как в файле .env. Обычно быстрее открыть нужный раздел "
    "админки или найти настройку поиском. Правки сохраняются в .env, почти все работают сразу.",
    "root_empty": "Доступных вам разделов нет.",
    "restart_pending": "♻️ Ждут перезапуска: {keys}",
    "problems": "⚠️ Требуют внимания:",
    "search": "🔎 Поиск",
    "menu": "🏠 Меню",
    "back": "⬅️ Назад",
    "to_settings": "🔎 Все настройки",
    "kassas": "🏦 Кассы",
    "key_line": "Ключ в .env: <code>{key}</code>",
    "slice_more": "🧰 Ещё ({n})",
    "slice_less": "🙈 Скрыть редкие",
    "slice_empty": "Здесь нет настроек, которые вам можно менять.",
    "slice_done": "Готово: «{title}» {state}.",
    "slice_failed": "Не применено: {reason}",
    "state_on": "включено",
    "state_off": "выключено",
    "to_key": "⬅️ К настройке",
    "advanced_show": "🧰 Расширенные ({n})",
    "advanced_hide": "🙈 Скрыть расширенные",
    "advanced_title": "🧰 <b>Расширенные</b>",
    "section_hint": "Нажмите на настройку, чтобы посмотреть или изменить её.",
    "only_advanced": "В разделе только расширенные настройки.",
    "page": "{page}/{pages}",
    "prev": "◀️",
    "next": "▶️",
    "now": "Сейчас: {value}",
    "now_secret": "Сейчас: {value} · отпечаток <code>{fp}</code>",
    "not_set": "не задано",
    "default": "По умолчанию: {value}",
    "range": "Диапазон: {range}",
    "choices": "Варианты: {choices}",
    "example": "Пример: {hint}",
    "format_list": "Формат: значения через запятую",
    "format_duration": "Формат: 30d, 12h, 1h30m или число секунд",
    "format_bool": "Формат: да / нет",
    "edit": "✏️ Изменить",
    "turn_on": "🟢 Включить",
    "turn_off": "⚪️ Выключить",
    "reset": "↩️ По умолчанию",
    "history": "🕘 История",
    "undo": "↩️ Отменить",
    "on": "✅ вкл",
    "off": "❌ выкл",
    "src_default": "· по умолчанию",
    "src_bot": "🤖 из бота",
    "src_env_file": "✏️ из .env",
    "src_environ": "🌐 из окружения контейнера",
    "src_locked": "🔒 задано окружением (LOCKED_KEYS)",
    "src_cli": "⌨️ из командной строки",
    "src_import": "📥 из импорта",
    "apply_hot": "⚡ применяется мгновенно",
    "apply_reload": "🔄 переподключит компонент «{component}»",
    "apply_restart": "♻️ нужен перезапуск",
    "blocked_locked": "🔒 Меняется только в окружении контейнера (LOCKED_KEYS).",
    "blocked_readonly": "🔒 Задаётся только в docker-compose/окружении, в боте не меняется.",
    "blocked_secret_key": "🔒 Ключ меняется только командой ротации ключа на сервере.",
    "blocked_file_only": "📄 Меняется только в файле .env.",
    "env_override": "ℹ️ В окружении контейнера задано другое значение, бот его не использует.",
    "restart_wait": "♻️ Новое значение заработает после перезапуска.",
    "changed": "🕘 Изменено {when} · {source}",
    "just_now": "только что",
    "min_ago": "{n} мин назад",
    "hours_ago": "{n} ч назад",
    "applied_hot": "⚡ <b>Применено</b>",
    "applied_reload": "🔄 <b>Применено</b> · «{component}» переподключён",
    "applied_saved": "✅ <b>Сохранено</b>",
    "applied_restart": "♻️ <b>Сохранено</b> · заработает после перезапуска",
    "undone": "↩️ <b>Отменено</b>",
    "rejected": "❌ <b>Не применено</b>",
    "unchanged": "Значение не изменилось.",
    "reason": "Причина: {reason}",
    "secret_not_deleted": "⚠️ Не получилось удалить сообщение с секретом, удалите его вручную.",
    "secret_in_group": "⚠️ Похоже, в сообщении секрет, а удалить его не получилось. Удалите сообщение и "
    "смените этот секрет. Настройки меняются только в личном чате с ботом.",
    "applying": "⏳ <b>Применяется…</b>\nПроверка и переподключение занимают время, результат придёт сюда.",
    "applying_plain": "⏳ Применяется… Проверка и переподключение занимают время, результат придёт сюда.",
    "prompt_title": "✏️ {title}",
    "prompt_send": "Отправьте новое значение одним сообщением.",
    "prompt_secret": "🔐 Сообщение с секретом будет удалено сразу после чтения.",
    "search_prompt": "🔎 Что найти? Например: «триал», «токен», «валюта» или имя ключа.",
    "search_title": "🔎 <b>Поиск</b>: «{query}»",
    "search_none": "Ничего не найдено. Попробуйте другое слово.",
    "search_again": "🔎 Искать ещё",
    "history_title": "🕘 <b>История</b> · {title}",
    "history_empty": "Изменений ещё не было.",
    "history_unavailable": "История сейчас недоступна (база данных не отвечает).",
    "denied_view": "⛔ Нет прав на эту настройку.",
    "stale": "Меню обновилось",
    "cancelled": "Отменено",
    "undo_expired": "Отменить уже нельзя: прошло больше {minutes} минут",
    "undo_foreign": "Это изменение сделал другой администратор",
    "undo_failed": "Не удалось отменить: {reason}",
    "db_down": "База данных не отвечает, попробуйте позже",
    "pick_unknown": "Такого варианта нет",
    "not_bool": "Эта настройка не переключается",
}

_ADMIN_HOME: Final = ("🛠 Админка", nav.ROOT)

_HEALTH_ICON: Final = {
    Health.OK: "✅",
    Health.DEGRADED: "⚠️",
    Health.DOWN: "❌",
    Health.DISABLED: "⏸",
    Health.UNKNOWN: "❔",
}

_SECTION_ICON: Final = {
    "boot": "🚀",
    "database": "🗄",
    "telegram": "✈️",
    "remnawave": "🔌",
    "admin_chat": "🔔",
    "sales": "📦",
    "wallet": "💳",
    "payments": "💳",
    "referral": "🎁",
    "promo": "🏷",
    "support": "🆘",
    "broadcast": "📣",
    "reports": "📊",
    "logs": "🪵",
    "modules": "🧩",
    "system": "🛠",
}

_HISTORY_SOURCE: Final = {
    "bot": "🤖 бот",
    "env_file": "✏️ .env",
    "env_seed": "🌐 окружение",
    "cli": "⌨️ CLI",
    "import": "📥 импорт",
    "wizard": "🧙 мастер",
    "system": "⚙️ система",
    "undo": "↩️ отмена",
    "rollback": "⏪ откат",
}

_DB_ERRORS: Final = (SQLAlchemyError, OSError)
_TG_ERRORS: Final = (TelegramAPIError, OSError, TimeoutError)

UserLoader = Callable[["TgUser"], Awaitable["UserCtx | None"]]
#: ``extras(slice_id, user)`` → extra text lines and links of a slice (a stats line, the trial plan's card).
SliceExtras = Callable[[str, "UserCtx"], Awaitable[tuple[list[str], list[slices.Link]]]]


# ---------------------------------------------------------------- access rules


def is_business(defn: SettingDef) -> bool:
    """A key an admin with ``settings.business`` may see and change (not a secret, not bootstrap)."""
    return not (defn.owner_only or defn.is_secret or defn.bootstrap)


def can_open_settings(user: UserCtx) -> bool:
    return user.role == "owner" or (user.at_least("admin") and user.has_perm(PERM_BUSINESS))


def can_view(user: UserCtx, defn: SettingDef) -> bool:
    """Owner: every key. Admin with ``settings.business``: business keys only. Others: nothing."""
    if user.role == "owner":
        return True
    return can_open_settings(user) and is_business(defn)


def can_edit_keys(user: UserCtx, defs: Sequence[SettingDef]) -> bool:
    return all(can_view(user, d) for d in defs)


def form_name(key: str) -> str:
    """Name of the edit form of ``key`` (form names are ``[a-z][a-z0-9_.]{0,47}``)."""
    candidate = FORM_PREFIX + key.lower()
    if len(candidate) <= 48:
        return candidate
    return FORM_PREFIX + "h" + hashlib.sha256(key.encode()).hexdigest()[:16]


# ---------------------------------------------------------------- small helpers


class _Stop(Exception):
    """Ends a handler early with a ready result (denial, stale button …)."""

    def __init__(self, result: HandlerResult) -> None:
        super().__init__()
        self.result = result


@dataclass(slots=True)
class _Done:
    """Result of a ``/set`` change kept for the ``set.done`` screen."""

    user_id: int
    key: str
    result: ApplyResult
    note: str | None
    created: float
    undone: bool = False


def _esc(value: object) -> str:
    return html.escape(str(value), quote=False)


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(limit - 1, 1)] + "…"


def _num(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def _section_arg(sid: str, page: int = 0, advanced: bool = False) -> str:
    return sid if page == 0 and not advanced else f"{sid}:{page}:{int(advanced)}"


def _parse_section_arg(arg: Any) -> tuple[str, int, bool] | None:
    if not isinstance(arg, str) or not arg:
        return None
    parts = arg.split(":")
    if len(parts) == 1:
        return parts[0], 0, False
    if len(parts) != 3 or not parts[1].isdigit() or parts[2] not in ("0", "1") or len(parts[1]) > 4:
        return None
    return parts[0], int(parts[1]), parts[2] == "1"


async def _maybe_await(value: Any) -> None:
    if inspect.isawaitable(value):
        await value


class SettingsScreens:
    """Registers the settings screens, actions and forms on a router; serves ``/set`` and ``/settings``.

    ``undo_ttl`` — how long «↩️ Отменить» works; ``health_timeout`` — per component health check on the root
    screen; ``apply_wait`` — how long a handler waits for a change (probe + reconfigure may take longer than
    the router's handler timeout): after it the user sees «Применяется…» and the result is delivered as soon
    as the change finishes (default: a bit less than the handler/command timeout).
    Call :meth:`install` once at startup (before the router handles updates) and :meth:`drain` on shutdown.
    """

    def __init__(
        self,
        router: ScreenRouter,
        service: SettingsService,
        *,
        undo_ttl: timedelta = timedelta(minutes=10),
        health_timeout: float = 1.0,
        page_size: int = PAGE_SIZE,
        command_timeout: float = 60.0,
        apply_wait: float | None = None,
        notes: Callable[[str], Sequence[str]] | None = None,
        extras: SliceExtras | None = None,
    ) -> None:
        if undo_ttl <= timedelta(0):
            raise ValueError("undo_ttl must be positive")
        if not 1 <= page_size <= 40:
            raise ValueError("page_size must be 1..40")
        self.router = router
        self.service = service
        self.registry = service.registry
        self.undo_ttl = undo_ttl
        self.health_timeout = health_timeout
        self.page_size = page_size
        self.command_timeout = command_timeout
        self.apply_wait = apply_wait
        self.notes = notes
        self.extras = extras
        self._group_warned: OrderedDict[int, float] = OrderedDict()
        self._results: OrderedDict[str, _Done] = OrderedDict()
        self._tasks: set[asyncio.Future[Any]] = set()
        self._installed = False

    # ------------------------------------------------------------ registration

    def install(self) -> None:
        """Register screens, actions and forms (idempotent)."""
        if self._installed:
            return
        self._installed = True
        r = self.router
        guard = {"required_role": "admin", "perm": PERM_BUSINESS}
        r.screen(SCREEN_ROOT, **guard)(self._wrap(self._root_screen))
        r.screen(SCREEN_SECTION, **guard)(self._wrap(self._section_screen))
        r.screen(SCREEN_KEY, **guard)(self._wrap(self._key_screen))
        r.screen(SCREEN_HISTORY, **guard)(self._wrap(self._history_screen))
        r.screen(SCREEN_FIND, **guard)(self._wrap(self._find_screen))
        r.screen(SCREEN_DONE, **guard)(self._wrap(self._done_screen))
        r.screen(SCREEN_SLICE, **guard)(self._wrap(self._slice_screen))
        r.action(ACTIONS, A_EDIT, **guard)(self._wrap(self._edit_action))
        r.action(ACTIONS, A_TOGGLE, **guard)(self._wrap(self._toggle_action))
        r.action(ACTIONS, A_PICK, **guard)(self._wrap(self._pick_action))
        r.action(ACTIONS, A_RESET, **guard)(self._wrap(self._reset_action))
        r.action(ACTIONS, A_UNDO, **guard)(self._wrap(self._undo_action))
        r.action(ACTIONS, A_SEARCH, **guard)(self._wrap(self._search_action))
        r.action(ACTIONS, A_VTOGGLE, **guard)(self._wrap(self._vtoggle_action))
        r.form(
            Form(
                FORM_SEARCH,
                (Field("q", _T["search_prompt"], text_validator(min_len=2, max_len=64)),),
                on_done=self._search_done,
                on_cancel=self._to_root,
                **guard,
            )
        )
        for defn in self.registry.all():
            self._ensure_form(defn)

    def _ensure_form(self, defn: SettingDef) -> str | None:
        """Register the edit form of ``defn`` if needed (keys added by plugins later get theirs lazily)."""
        if defn.readonly or defn.file_only:
            return None
        name = form_name(defn.key)
        if name not in self.router.forms:
            business = is_business(defn)
            self.router.form(
                Form(
                    name,
                    (
                        Field(
                            "value",
                            self._prompt(defn),
                            _value_validator(defn),
                            secret=defn.is_secret,
                        ),
                    ),
                    on_done=self._form_done(defn.key),
                    on_cancel=self._form_cancel(defn.key),
                    required_role="admin" if business else "owner",
                    perm=PERM_BUSINESS if business else None,
                )
            )
        return name

    def _wrap(
        self, fn: Callable[[ScreenCtx, Any], Awaitable[HandlerResult]]
    ) -> Callable[[ScreenCtx, Any], Awaitable[Any]]:
        async def run(ctx: ScreenCtx, arg: Any) -> Any:
            try:
                return await fn(ctx, arg)
            except _Stop as stop:
                return stop.result

        run.__name__ = getattr(fn, "__name__", "settings_handler")
        return run

    # ------------------------------------------------------------ access helpers

    async def _report_denied(self, user: UserCtx, place: str) -> None:
        log.info("settings access denied: user %s at %s", user.user_id, place)
        hook = self.router.on_denied
        if hook is None:
            return
        try:
            await _maybe_await(hook(user, place))
        except Exception:
            log.exception("on_denied hook failed")

    async def _deny(self, ctx: ScreenCtx, place: str, *, screen: bool) -> NoReturn:
        await self._report_denied(ctx.user, place)
        if screen:  # the callback is already answered: say it in the message itself
            raise _Stop(View(text=_T["denied_view"], keyboard=[[nav_button(_T["to_settings"], SCREEN_ROOT)]]))
        raise _Stop(Toast(ui_texts.t(ctx.lang, "denied")))

    def _stale(self, *, screen: bool) -> NoReturn:
        if screen:
            raise _Stop(Redirect(SCREEN_ROOT, toast=_T["stale"]))
        raise _Stop(Toast(_T["stale"]))

    async def _need_key(self, ctx: ScreenCtx, arg: Any, place: str, *, screen: bool) -> SettingDef:
        if isinstance(arg, str):
            arg = arg.removesuffix(TREE_MARK)
        defn = self.registry.find(arg) if isinstance(arg, str) else None
        if defn is None:
            self._stale(screen=screen)
        if not can_view(ctx.user, defn):
            await self._deny(ctx, f"{place}:{defn.key}", screen=screen)
        return defn

    def _edit_block(self, defn: SettingDef) -> str | None:
        """Why ``defn`` cannot be changed in the bot (``None`` if it can)."""
        if defn.key in self.service.locked:
            return _T["blocked_locked"]
        if defn.readonly:
            return _T["blocked_secret_key"] if defn.key == "SECRET_KEY" else _T["blocked_readonly"]
        if defn.file_only:
            return _T["blocked_file_only"]
        return None

    async def _btn(
        self, ctx: ScreenCtx, text: str, screen: str, action: str, arg: str
    ) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            text=_cut(text, MAX_LABEL_CHARS), callback_data=await ctx.callback(screen, action, arg)
        )

    # ------------------------------------------------------------ value formatting

    def _short_value(self, defn: SettingDef, value: Any, limit: int = 24) -> str:
        if defn.kind == "bool" and value is not None:
            return _T["on"] if value else _T["off"]
        return _cut(values.display(defn, value, human=True), limit)

    def _source_label(self, source: str) -> str:
        if source in ("wizard", "system"):
            source = "bot"
        return _T.get(f"src_{source}", _T["src_bot"])

    def _apply_label(self, defn: SettingDef) -> str:
        if defn.apply is Apply.RELOAD:
            return _T["apply_reload"].format(component=_esc(defn.component or "?"))
        if defn.apply is Apply.RESTART:
            return _T["apply_restart"]
        return _T["apply_hot"]

    def _format_hint(self, defn: SettingDef) -> list[str]:
        lines: list[str] = []
        kind = defn.kind
        if kind in ("list[str]", "list[int]"):
            lines.append(_T["format_list"])
        elif kind == "duration":
            lines.append(_T["format_duration"])
        elif kind == "bool":
            lines.append(_T["format_bool"])
        if defn.min is not None or defn.max is not None:
            low = _num(defn.min) if defn.min is not None else "…"
            high = _num(defn.max) if defn.max is not None else "…"
            lines.append(_T["range"].format(range=f"{low}–{high}"))
        if defn.choices and kind != "enum":
            lines.append(_T["choices"].format(choices=" | ".join(defn.choices)))
        if defn.hint:
            lines.append(_T["example"].format(hint=defn.hint))
        return lines

    def _prompt(self, defn: SettingDef) -> str:
        lines = [_T["prompt_title"].format(title=defn.title), labels.description(defn), ""]
        lines += self._format_hint(defn)
        if defn.choices and defn.kind == "enum":
            lines.append(_T["choices"].format(choices=" | ".join(defn.choices)))
        lines.append(_T["prompt_send"])
        if defn.is_secret:
            lines.append(_T["prompt_secret"])
        return "\n".join(line for line in lines if line is not None)

    def _when(self, ts: datetime) -> str:
        delta = clock.now() - ts
        seconds = max(delta.total_seconds(), 0)
        if seconds < 60:
            return _T["just_now"]
        if seconds < 3600:
            return _T["min_ago"].format(n=int(seconds // 60))
        if seconds < 86400:
            return _T["hours_ago"].format(n=int(seconds // 3600))
        return self._local(ts).strftime("%d.%m.%Y %H:%M")

    def _local(self, ts: datetime) -> datetime:
        name = self.service.current().get("TIMEZONE")
        try:
            return ts.astimezone(zoneinfo.ZoneInfo(str(name))) if name else ts
        except (zoneinfo.ZoneInfoNotFoundError, ValueError):
            return ts

    # ------------------------------------------------------------ root

    def _visible_sections(self, user: UserCtx) -> list[tuple[str, str, list[SettingDef]]]:
        """Top-level sections with something visible; a section's defs include its subsections' (07 §3.2:
        payment instances and modules are subsections, so the root stays short)."""
        out: list[tuple[str, str, list[SettingDef]]] = []
        by_section = self.registry.by_section()
        for sid, title in self.registry.top_sections():
            defs = [*by_section.get(sid, [])]
            for sub in self.registry.subsections(sid):
                defs += by_section.get(sub, [])
            visible = [d for d in defs if can_view(user, d)]
            if visible:
                out.append((sid, title, visible))
        return out

    def _visible_subsections(self, user: UserCtx, sid: str) -> list[tuple[str, str]]:
        by_section = self.registry.by_section()
        return [
            (sub, self.registry.section_title(sub))
            for sub in self.registry.subsections(sid)
            if any(can_view(user, d) for d in by_section.get(sub, []))
        ]

    def _section_icon(self, sid: str) -> str:
        parent = self.registry.parent(sid)
        return _SECTION_ICON.get(sid) or _SECTION_ICON.get(parent or "", "⚙️")

    async def _component_icons(self, names: set[str]) -> dict[str, str]:
        components = self.service.components
        present = [n for n in sorted(names) if n in components]
        if not present:
            return {}
        reports = await asyncio.gather(*(components.health(n, limit_s=self.health_timeout) for n in present))
        return {n: _HEALTH_ICON.get(r.status, "❔") for n, r in zip(present, reports, strict=True)}

    async def _root_screen(self, ctx: ScreenCtx, arg: Any) -> View:
        user = ctx.user
        sections = self._visible_sections(user)
        owner = user.role == "owner"
        icons: dict[str, str] = {}
        if owner:
            names = {d.component for _, _, defs in sections for d in defs if d.component}
            icons = await self._component_icons({n for n in names if n})
        lines = [
            _T["root_title"],
            _esc(nav.breadcrumb(SCREEN_ROOT)),
            "",
            _T["root_hint"] if sections else _T["root_empty"],
        ]
        if owner:
            if self.service.restart_pending:
                keys = ", ".join(sorted(self.service.restart_pending))
                lines += ["", _T["restart_pending"].format(keys=_esc(keys))]
            if self.service.problems:
                lines += ["", _T["problems"]]
                for key, problem in sorted(self.service.problems.items())[:5]:
                    lines.append(f"• <code>{_esc(key)}</code>: {_esc(_cut(problem, 200))}")
        rows: list[list[InlineKeyboardButton]] = [[nav_button(_T["search"], ACTIONS, A_SEARCH)]]
        for sid, title, defs in sections:
            label = f"{self._section_icon(sid)} {title}"
            marks = []
            for comp in dict.fromkeys(d.component for d in defs if d.component):
                if comp in icons:
                    marks.append(icons[comp])
            if marks:
                label += " " + "".join(marks)
            rows.append([await self._btn(ctx, label, SCREEN_SECTION, codec_mod.ACTION_OPEN, sid)])
        rows.append(nav.back_row(SCREEN_ROOT))
        return View(text="\n".join(lines), parse_mode="HTML", keyboard=rows)

    # ------------------------------------------------------------ section

    def _notes(self, sid: str) -> list[str]:
        """Extra HTML lines the app adds under a section; a failing hook never breaks the screen."""
        if self.notes is None:
            return []
        try:
            extra = list(self.notes(sid))
        except Exception:  # optional hint only
            log.exception("settings notes for %s failed", sid)
            return []
        return ["", *extra] if extra else []

    async def _section_screen(self, ctx: ScreenCtx, arg: Any) -> View:
        parsed = _parse_section_arg(arg)
        titles = dict(self.registry.sections)
        if parsed is None or parsed[0] not in titles:
            self._stale(screen=True)
        sid, page, advanced = parsed
        visible = [d for d in self.registry.by_section()[sid] if can_view(ctx.user, d)]
        subs = self._visible_subsections(ctx.user, sid)
        if not visible and not subs:
            await self._deny(ctx, f"section:{sid}", screen=True)
        regular = [d for d in visible if not d.advanced]
        hidden = [d for d in visible if d.advanced]
        if not regular:
            advanced = True
        shown = regular + (hidden if advanced else [])
        pages = max(1, -(-len(shown) // self.page_size))
        page = min(page, pages - 1)
        chunk = shown[page * self.page_size : (page + 1) * self.page_size]
        snap = self.service.current()

        lines = [f"{self._section_icon(sid)} <b>{_esc(titles[sid])}</b>", ""]
        if visible:
            lines.append(_T["section_hint"] if regular else _T["only_advanced"])
        lines += self._notes(sid)
        rows: list[list[InlineKeyboardButton]] = []
        if subs and sid == PAYMENTS_SECTION and nav.has_screen(self.router, SCREEN_KASSAS):
            # 28 cash desks are one list with a status and a card each, not 28 setting sections here
            if page == 0:
                rows.append([nav_button(_T["kassas"], SCREEN_KASSAS)])
            subs = []
        if page == 0:
            for sub, sub_title in subs:
                label = f"{self._section_icon(sub)} {sub_title}"
                rows.append([await self._btn(ctx, label, SCREEN_SECTION, codec_mod.ACTION_OPEN, sub)])
        for defn in chunk:
            marks = ""
            if defn.key in self.service.locked:
                marks += "🔒 "
            if defn.key in self.service.problems:
                marks += "⚠️ "
            label = f"{marks}{defn.title}: {self._short_value(defn, snap[defn.key])}"
            rows.append(
                [await self._btn(ctx, label, SCREEN_KEY, codec_mod.ACTION_OPEN, defn.key + TREE_MARK)]
            )
        if pages > 1:
            pager: list[InlineKeyboardButton] = []
            if page > 0:
                pager.append(
                    await self._btn(
                        ctx,
                        _T["prev"],
                        SCREEN_SECTION,
                        codec_mod.ACTION_OPEN,
                        _section_arg(sid, page - 1, advanced),
                    )
                )
            pager.append(
                await self._btn(
                    ctx,
                    _T["page"].format(page=page + 1, pages=pages),
                    SCREEN_SECTION,
                    codec_mod.ACTION_OPEN,
                    _section_arg(sid, page, advanced),
                )
            )
            if page < pages - 1:
                pager.append(
                    await self._btn(
                        ctx,
                        _T["next"],
                        SCREEN_SECTION,
                        codec_mod.ACTION_OPEN,
                        _section_arg(sid, page + 1, advanced),
                    )
                )
            rows.append(pager)
        if hidden and regular:
            text = _T["advanced_hide"] if advanced else _T["advanced_show"].format(n=len(hidden))
            rows.append(
                [
                    await self._btn(
                        ctx, text, SCREEN_SECTION, codec_mod.ACTION_OPEN, _section_arg(sid, 0, not advanced)
                    )
                ]
            )
        parent = self.registry.parent(sid)
        if parent is None:
            rows.append([nav_button(_T["back"], SCREEN_ROOT)])
        else:
            rows.append([await self._btn(ctx, _T["back"], SCREEN_SECTION, codec_mod.ACTION_OPEN, parent)])
        return View(text="\n".join(lines), parse_mode="HTML", keyboard=rows)

    # ------------------------------------------------------------ key card

    async def _last_change(self, key: str) -> AuditEntry | None:
        try:
            entries = await self.service.history(key, limit=5)
        except _DB_ERRORS as e:
            log.debug("settings history of %s unavailable: %s", key, type(e).__name__)
            return None
        return next((e for e in entries if e.applied), None)

    async def _home_button(self, ctx: ScreenCtx, defn: SettingDef) -> InlineKeyboardButton:
        """«⬅️ <slice>»: where the key lives in the admin (its card's way back)."""
        home = slices.home_of(defn)
        screen, arg = slices.target_of(home)
        label = "⬅️ " + nav.short(slices.title_of(home))
        if arg is None:
            return nav_button(label, screen)
        return await self._btn(ctx, label, screen, codec_mod.ACTION_OPEN, arg)

    async def _card(
        self, ctx: ScreenCtx, defn: SettingDef, *, extra: Sequence[str] = (), tree: bool = False
    ) -> View:
        key = defn.key
        snap = self.service.current()
        value = snap[key]
        source = snap.source(key)
        lines = [f"<b>{_esc(defn.title)}</b>", _esc(labels.description(defn)), ""]
        if defn.is_secret and value not in (None, ""):
            fp = self.service.crypto.value_fingerprint(str(value))
            lines.append(_T["now_secret"].format(value=_esc(values.display(defn, value)), fp=_esc(fp)))
        elif value is None or value in ("", []):
            lines.append(_T["now"].format(value=_T["not_set"]))
        else:
            shown = self._short_value(defn, value, MAX_VALUE_CHARS)
            lines.append(_T["now"].format(value=f"<b>{_esc(shown)}</b>"))
        if not defn.is_secret:
            default = defn.default
            default_text = (
                _T["not_set"] if default in (None, "", []) else self._short_value(defn, default, 80)
            )
            lines.append(_T["default"].format(value=_esc(default_text)))
        lines.append(f"{self._source_label(source)} · {self._apply_label(defn)}")
        lines += [_esc(line) for line in self._format_hint(defn)]
        last = await self._last_change(key)
        if last is not None:
            lines.append(
                _T["changed"].format(
                    when=self._when(last.ts), source=_HISTORY_SOURCE.get(last.source, last.source)
                )
            )
        if key in self.service.restart_pending:
            lines.append(_T["restart_wait"])
        problem = self.service.problems.get(key)
        if problem:
            lines.append(f"⚠️ {_esc(problem)}")
        if key in self.service.env_overrides():
            lines.append(_T["env_override"])
        block = self._edit_block(defn)
        if block:
            lines += ["", block]
        if extra:
            lines += ["", *extra]
        lines += ["", _T["key_line"].format(key=_esc(key))]

        rows: list[list[InlineKeyboardButton]] = []
        if block is None:
            if defn.kind == "bool":
                label = _T["turn_off"] if value else _T["turn_on"]
                rows.append([await self._btn(ctx, label, ACTIONS, A_TOGGLE, key)])
            elif defn.kind == "enum" and defn.choices:
                rows += await self._choice_rows(ctx, defn, value)
            else:
                rows += await self._preset_rows(ctx, defn, value)
                rows.append([await self._btn(ctx, _T["edit"], ACTIONS, A_EDIT, key)])
            if source != "default":
                rows.append([await self._btn(ctx, _T["reset"], ACTIONS, A_RESET, key)])
        rows.append([await self._btn(ctx, _T["history"], SCREEN_HISTORY, codec_mod.ACTION_OPEN, key)])
        if tree:
            back = _section_arg(defn.section, 0, defn.advanced)
            rows.append([await self._btn(ctx, _T["back"], SCREEN_SECTION, codec_mod.ACTION_OPEN, back)])
        else:
            rows.append([await self._home_button(ctx, defn), nav_button(*_ADMIN_HOME)])
        return View(text="\n".join(lines), parse_mode="HTML", keyboard=rows)

    async def _choice_rows(
        self, ctx: ScreenCtx, defn: SettingDef, value: Any
    ) -> list[list[InlineKeyboardButton]]:
        """Enum values by their labels, ✅ on the current one (long labels one per row)."""
        shown = [(choice, labels.choice_label(defn, choice) or choice) for choice in defn.choices or ()]
        per_row = 1 if any(len(text) > 14 for _, text in shown) else 3
        rows: list[list[InlineKeyboardButton]] = []
        row: list[InlineKeyboardButton] = []
        for choice, text in shown:
            label = f"✅ {text}" if choice == value else text
            row.append(await self._btn(ctx, label, ACTIONS, A_PICK, f"{defn.key}:{choice}"))
            if len(row) == per_row:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        return rows

    async def _preset_rows(
        self, ctx: ScreenCtx, defn: SettingDef, value: Any
    ) -> list[list[InlineKeyboardButton]]:
        """Ready values (``TRIAL_DAYS``: 1 / 3 / 7) as one row of buttons, ✅ on the current one."""
        row: list[InlineKeyboardButton] = []
        for preset in labels.presets(defn):
            text = values.to_text(defn, preset)
            label = f"✅ {text}" if preset == value else text
            row.append(await self._btn(ctx, label, ACTIONS, A_PICK, f"{defn.key}:{text}"))
        return [row] if row else []

    async def _key_screen(self, ctx: ScreenCtx, arg: Any) -> View:
        defn = await self._need_key(ctx, arg, "card", screen=True)
        return await self._card(ctx, defn, tree=isinstance(arg, str) and arg.endswith(TREE_MARK))

    # ------------------------------------------------------------ slices (set.v)

    def _slice_defs(self, sl: slices.Slice) -> tuple[list[SettingDef], list[SettingDef]]:
        """``(shown, more)`` definitions of a slice, in its order (keys of other modules may be absent)."""
        if sl.id == slices.OTHER:
            loose = [d for d in self.registry.all() if slices.home_of(d) == slices.OTHER]
            return [d for d in loose if not d.advanced], [d for d in loose if d.advanced]
        if sl.sections:
            by_section = self.registry.by_section()
            defs = [d for sid in sl.sections for d in by_section.get(sid, [])]
            return [d for d in defs if not d.advanced], [d for d in defs if d.advanced]

        def found(keys: Sequence[str]) -> list[SettingDef]:
            return [d for d in (self.registry.find(k) for k in keys) if d is not None]

        return found((*sl.keys, *sl.mirrors)), found(sl.more)

    def _slice_members(self, sl: slices.Slice) -> set[str]:
        shown, more = self._slice_defs(sl)
        return {d.key for d in (*shown, *more)}

    @staticmethod
    def _slice_arg(arg: Any) -> tuple[slices.Slice, bool] | None:
        if not isinstance(arg, str):
            return None
        sid, _, flag = arg.partition(":")
        sl = slices.slice_of(sid)
        if sl is None or flag not in ("", "m"):
            return None
        return sl, flag == "m"

    async def _slice_screen(self, ctx: ScreenCtx, arg: Any) -> View:
        parsed = self._slice_arg(arg)
        if parsed is None:
            self._stale(screen=True)
        return await self.slice_view(ctx, parsed[0], expanded=parsed[1])

    async def slice_view(
        self,
        ctx: ScreenCtx,
        sl: slices.Slice,
        *,
        expanded: bool = False,
        note: str | None = None,
        undo: str | None = None,
    ) -> View:
        """A slice: header, one sentence, the keys the viewer may see, «🧰 Ещё», links, the way back."""
        user = ctx.user
        shown, more = self._slice_defs(sl)
        shown = [d for d in shown if can_view(user, d)]
        more = [d for d in more if can_view(user, d)]
        extra_lines: list[str] = []
        extra_links: list[slices.Link] = []
        if self.extras is not None:
            try:
                extra_lines, extra_links = await self.extras(sl.id, user)
            except Exception:  # an optional line never breaks the screen
                log.exception("slice extras for %s failed", sl.id)
        links = [
            link
            for link in (*sl.links, *extra_links)
            if user.at_least(link.role)
            and (link.perm is None or user.has_perm(link.perm))
            and nav.has_route(self.router, link.screen, link.action)  # a module that is not wired: no button
        ]
        if not shown and not more and not links:
            await self._deny(ctx, f"slice:{sl.id}", screen=True)
        if not shown:  # a small slice of rarely needed keys: no lonely «Ещё» button
            shown, more, expanded = more, [], False
        lines = [f"{_esc(nav.breadcrumb(sl.hub))} › <b>{_esc(sl.title)}</b>", "", _esc(sl.intro)]
        lines += extra_lines
        if note:
            lines += ["", note]
        rows: list[list[InlineKeyboardButton]] = []
        if undo:
            rows.append([nav_button(_T["undo"], ACTIONS, A_UNDO, undo)])
        snap = self.service.current()
        for defn in shown + (more if expanded else []):
            rows.append([await self._slice_button(ctx, sl, defn, snap[defn.key])])
        if more:
            if expanded:
                rows.append([nav_button(_T["slice_less"], SCREEN_SLICE, arg=sl.id)])
            else:
                label = _T["slice_more"].format(n=len(more))
                rows.append([nav_button(label, SCREEN_SLICE, arg=f"{sl.id}:m")])
        for link in links:
            data = await ctx.callback(link.screen, link.action, link.arg)
            rows.append([InlineKeyboardButton(text=_cut(link.label, MAX_LABEL_CHARS), callback_data=data)])
        rows.append(nav.back_to(nav.reachable(self.router, user, sl.hub)))
        return View(text="\n".join(lines), parse_mode="HTML", keyboard=rows)

    async def _slice_button(
        self, ctx: ScreenCtx, sl: slices.Slice, defn: SettingDef, value: Any
    ) -> InlineKeyboardButton:
        marks = ("🔒 " if defn.key in self.service.locked else "") + (
            "⚠️ " if defn.key in self.service.problems else ""
        )
        if sl.toggles and defn.kind == "bool" and self._edit_block(defn) is None:
            label = f"{marks}{'✅' if value else '⬜'} {defn.title}"
            return await self._btn(ctx, label, ACTIONS, A_VTOGGLE, f"{sl.id}:{defn.key}")
        label = f"{marks}{defn.title}: {self._short_value(defn, value)}"
        return await self._btn(ctx, label, SCREEN_KEY, codec_mod.ACTION_OPEN, defn.key)

    async def _vtoggle_action(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        """A switch of a slice toggled in place: the same ``apply()`` as the card, then the slice again with
        «↩️ Отменить» on top."""
        sid, _, key = arg.partition(":") if isinstance(arg, str) else ("", "", "")
        sl = slices.slice_of(sid)
        if sl is None or not sl.toggles:
            self._stale(screen=False)
        defn = await self._need_editable(ctx, key, "vtog")
        if defn.kind != "bool" or defn.key not in self._slice_members(sl):
            return Toast(_T["not_bool"])
        task = self._start_apply(ctx.user, defn, not bool(self.service.current()[defn.key]))
        result = await self._settle(
            task, user=ctx.user, chat_id=ctx.chat_id, key=defn.key, guard_s=self.router.handler_timeout
        )
        if result is None:
            return self._pending_view()
        expanded = defn.key in {d.key for d in self._slice_defs(sl)[1]}
        if result.rejected:
            reason = next(iter(result.rejected.values()))
            note = _esc(_T["slice_failed"].format(reason=_cut(reason, 300)))
            return await self.slice_view(ctx, sl, expanded=expanded, note=note)
        if defn.key not in result.applied:
            return await self.slice_view(ctx, sl, expanded=expanded, note=_T["unchanged"])
        state = _T["state_on"] if self.service.current()[defn.key] else _T["state_off"]
        note = _esc(_T["slice_done"].format(title=defn.title, state=state))
        view = await self.slice_view(ctx, sl, expanded=expanded, note=note, undo=result.batch_id)
        view.toast = state.capitalize()
        return view

    # ------------------------------------------------------------ history

    def _envelope_text(self, defn: SettingDef, env: dict[str, Any] | None) -> str:
        if env is None:
            return "по умолчанию"
        if "fp" in env:
            return f"•••• ({_cut(str(env['fp']), 16)})"
        if "raw" in env and not defn.is_secret:
            return "«" + _cut(str(env["raw"]), 40) + "»"
        if "v" in env:
            try:
                return _cut(values.display(defn, values.from_json(defn, env["v"])), 40)
            except values.SettingValueError:
                return "—" if defn.is_secret else _cut(str(env["v"]), 40)
        return "—"

    async def _history_screen(self, ctx: ScreenCtx, arg: Any) -> View:
        defn = await self._need_key(ctx, arg, "history", screen=True)
        lines = [_T["history_title"].format(title=_esc(defn.title)), f"<code>{_esc(defn.key)}</code>", ""]
        try:
            entries = await self.service.history(defn.key, limit=HISTORY_LIMIT)
        except _DB_ERRORS as e:
            log.warning("settings history unavailable: %s", type(e).__name__)
            entries = None
        if entries is None:
            lines.append(_T["history_unavailable"])
        elif not entries:
            lines.append(_T["history_empty"])
        for entry in entries or []:
            when = self._local(entry.ts).strftime("%d.%m %H:%M")
            src = _HISTORY_SOURCE.get(entry.source, entry.source)
            change = f"{self._envelope_text(defn, entry.old)} → {self._envelope_text(defn, entry.new)}"
            mark = "✅" if entry.applied else "❌"
            line = f"{mark} {when} · {_esc(src)}: {_esc(change)}"
            if not entry.applied and entry.error:
                line += f"\n    {_esc(_cut(entry.error, 160))}"
            lines.append(line)
        rows = [[await self._btn(ctx, _T["to_key"], SCREEN_KEY, codec_mod.ACTION_OPEN, defn.key)]]
        return View(text="\n".join(lines), parse_mode="HTML", keyboard=rows)

    # ------------------------------------------------------------ changes

    def _spawn(self, coro: Coroutine[Any, Any, _R]) -> asyncio.Task[_R]:
        """Run ``coro`` as a tracked task: a handler timeout cannot cancel it half-way (persisted but not
        swapped), and :meth:`drain` waits for it on shutdown."""
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Future[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("settings change failed in the background", exc_info=task.exception())

    def _wait_budget(self, timeout: float) -> float:
        """How long a handler guarded by ``timeout`` may wait for a change before answering «Применяется…»."""
        budget = max(timeout - 3.0, timeout * 0.5)
        return budget if self.apply_wait is None else min(self.apply_wait, budget)

    async def _settle(
        self,
        task: asyncio.Task[ApplyResult],
        *,
        user: UserCtx,
        chat_id: int,
        key: str,
        guard_s: float,
        note: str | None = None,
        undone: bool = False,
        new: bool = False,
    ) -> ApplyResult | None:
        """The result if the change finishes within the handler's budget; otherwise ``None``, and the result
        is shown to the user (``set.done``) as soon as it is known — never a false «ошибка»."""
        done, _ = await asyncio.wait({task}, timeout=self._wait_budget(guard_s))
        if task in done:
            return task.result()
        self._spawn(
            self._deliver_later(task, user=user, chat_id=chat_id, key=key, note=note, undone=undone, new=new)
        )
        return None

    async def _deliver_later(
        self,
        task: asyncio.Task[ApplyResult],
        *,
        user: UserCtx,
        chat_id: int,
        key: str,
        note: str | None,
        undone: bool,
        new: bool,
    ) -> None:
        try:
            result = await asyncio.shield(task)
        except (SettingsError, *_DB_ERRORS) as e:
            log.warning("delayed settings change failed: %s", type(e).__name__)
            return
        self._remember(result.batch_id, _Done(user.user_id, key, result, note, clock.monotonic(), undone))
        try:
            await self.router.show(user, chat_id, SCREEN_DONE, result.batch_id, new=new)
        except (*_TG_ERRORS, *_DB_ERRORS) as e:
            log.warning("could not show the settings result: %s", type(e).__name__)

    def _start_apply(self, user: UserCtx, defn: SettingDef, raw: Any) -> asyncio.Task[ApplyResult]:
        if defn.is_secret and isinstance(raw, str):
            register_secret(raw.strip())
        change = Change(defn.key, raw)
        return self._spawn(self.service.apply([change], source="bot", actor_id=user.user_id))

    @staticmethod
    def _pending_view() -> View:
        return View(
            text=_T["applying"], parse_mode="HTML", keyboard=[[nav_button(_T["to_settings"], SCREEN_ROOT)]]
        )

    async def _result_view(
        self,
        ctx: ScreenCtx,
        defn: SettingDef,
        result: ApplyResult,
        *,
        undo: bool = True,
        undone: bool = False,
        note: str | None = None,
    ) -> View:
        lines: list[str] = []
        applied = [k for k in result.applied if k in self.registry]
        if applied:
            if undone:
                lines.append(_T["undone"])
            elif defn.apply is Apply.RESTART:
                lines.append(_T["applied_restart"])
            elif defn.apply is Apply.RELOAD and defn.component in result.reloaded:
                lines.append(_T["applied_reload"].format(component=_esc(defn.component)))
            elif defn.apply is Apply.RELOAD:
                lines.append(_T["applied_saved"])
            else:
                lines.append(_T["applied_hot"])
            snap = self.service.current()
            for key in applied:
                d = self.registry.get(key)
                lines.append(f"<b>{_esc(d.title)}</b>: {_esc(self._short_value(d, snap[key], 200))}")
        if result.rejected:
            if applied:
                lines.append("")
            lines.append(_T["rejected"])
            for key, reason in result.rejected.items():
                d = self.registry.find(key)
                title = d.title if d is not None else key
                lines.append(f"<b>{_esc(title)}</b>")
                lines.append(_T["reason"].format(reason=_esc(_cut(reason, 600))))
        if not applied and not result.rejected:
            lines.append(_T["unchanged"])
        if note:
            lines += ["", note]
        rows: list[list[InlineKeyboardButton]] = []
        if undo and applied and not result.rejected:
            rows.append([nav_button(_T["undo"], ACTIONS, A_UNDO, result.batch_id)])
        rows.append(
            [
                await self._btn(ctx, _T["to_key"], SCREEN_KEY, codec_mod.ACTION_OPEN, defn.key),
                await self._home_button(ctx, defn),
            ]
        )
        return View(text="\n".join(lines), parse_mode="HTML", keyboard=rows)

    async def _change(self, ctx: ScreenCtx, defn: SettingDef, raw: Any) -> View:
        task = self._start_apply(ctx.user, defn, raw)
        result = await self._settle(
            task, user=ctx.user, chat_id=ctx.chat_id, key=defn.key, guard_s=self.router.handler_timeout
        )
        if result is None:
            return self._pending_view()
        return await self._result_view(ctx, defn, result)

    async def _need_editable(self, ctx: ScreenCtx, arg: Any, place: str) -> SettingDef:
        defn = await self._need_key(ctx, arg, place, screen=False)
        block = self._edit_block(defn)
        if block is not None:
            raise _Stop(Toast(_cut(block, 190), alert=True))
        return defn

    async def _edit_action(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        defn = await self._need_editable(ctx, arg, "edit")
        if defn.kind == "bool":
            return await self._toggle_action(ctx, defn.key)
        if defn.kind == "enum":
            return Redirect(SCREEN_KEY, defn.key)
        name = self._ensure_form(defn)
        if name is None:  # readonly / file-only: _need_editable already refused
            self._stale(screen=False)
        return await ctx.start_form(name)

    async def _toggle_action(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        defn = await self._need_editable(ctx, arg, "toggle")
        if defn.kind != "bool":
            return Toast(_T["not_bool"])
        current = self.service.current()[defn.key]
        return await self._change(ctx, defn, not bool(current))

    async def _pick_action(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        key, sep, choice = arg.partition(":") if isinstance(arg, str) else ("", "", "")
        if not sep:
            self._stale(screen=False)
        defn = await self._need_editable(ctx, key, "pick")
        if defn.kind == "enum":
            if not defn.choices or choice not in defn.choices:
                return Toast(_T["pick_unknown"])
        elif choice not in {values.to_text(defn, p) for p in labels.presets(defn)}:
            return Toast(_T["pick_unknown"])  # a ready value of the card only, never free text
        return await self._change(ctx, defn, choice)

    async def _reset_action(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        defn = await self._need_editable(ctx, arg, "reset")
        return await self._change(ctx, defn, RESET)

    async def _undo_action(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        if not is_uuid7(arg):
            self._stale(screen=False)
        try:
            entries = await self.service.store.batch(arg)
        except _DB_ERRORS:
            return Toast(_T["db_down"])
        applied = [e for e in entries if e.applied]
        if not entries or entries[0].source != "bot" or not applied:
            self._stale(screen=False)
        user = ctx.user
        if user.role != "owner" and any(e.actor_id != user.user_id for e in entries):
            await self._report_denied(user, f"undo:{arg}")
            return Toast(_T["undo_foreign"], alert=True)
        defs = [self.registry.find(e.key) for e in applied]
        known = [d for d in defs if d is not None]
        if not known:
            self._stale(screen=False)
        if not can_edit_keys(user, known):
            await self._deny(ctx, f"undo:{arg}", screen=False)
        if min(e.ts for e in entries) + self.undo_ttl < clock.now():
            minutes = int(self.undo_ttl.total_seconds() // 60)
            return Toast(_T["undo_expired"].format(minutes=minutes), alert=True)
        task = self._spawn(self.service.undo(arg, actor_id=user.user_id))
        try:
            result = await self._settle(
                task,
                user=user,
                chat_id=ctx.chat_id,
                key=known[0].key,
                guard_s=self.router.handler_timeout,
                undone=True,
            )
        except SettingsError as e:
            return Toast(_cut(_T["undo_failed"].format(reason=e), 190), alert=True)
        except _DB_ERRORS:
            return Toast(_T["db_down"])
        if result is None:
            return self._pending_view()
        return await self._result_view(ctx, known[0], result, undo=False, undone=True)

    # ------------------------------------------------------------ forms

    def _form_done(self, key: str) -> Callable[[ScreenCtx, dict[str, Any]], Awaitable[HandlerResult]]:
        async def done(ctx: ScreenCtx, data: dict[str, Any]) -> HandlerResult:
            try:
                defn = await self._need_editable(ctx, key, "form")
                return await self._change(ctx, defn, data.get("value"))
            except _Stop as stop:
                result = stop.result
                if isinstance(result, Toast):  # no callback to answer after text input: show it as text
                    return View(text=result.text, keyboard=[[nav_button(_T["to_settings"], SCREEN_ROOT)]])
                return result

        return done

    def _form_cancel(self, key: str) -> Callable[[ScreenCtx], Awaitable[HandlerResult]]:
        async def cancel(ctx: ScreenCtx) -> HandlerResult:
            return Redirect(SCREEN_KEY, key, toast=_T["cancelled"])

        return cancel

    async def _to_root(self, ctx: ScreenCtx) -> HandlerResult:
        return Redirect(SCREEN_ROOT, toast=_T["cancelled"])

    async def _search_action(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        return await ctx.start_form(FORM_SEARCH)

    async def _search_done(self, ctx: ScreenCtx, data: dict[str, Any]) -> HandlerResult:
        return await self._find_view(ctx, str(data.get("q") or ""))

    async def _find_screen(self, ctx: ScreenCtx, arg: Any) -> View:
        if not isinstance(arg, str) or not arg.strip():
            self._stale(screen=True)
        return await self._find_view(ctx, arg)

    async def _find_view(self, ctx: ScreenCtx, query: str) -> View:
        query = _cut(query.strip(), 64)
        found = [d for d in self.service.search(query, limit=50) if can_view(ctx.user, d)][:SEARCH_LIMIT]
        snap = self.service.current()
        lines = [_T["search_title"].format(query=_esc(mask(query))), ""]
        rows: list[list[InlineKeyboardButton]] = []
        if not found:
            lines.append(_T["search_none"])
        for defn in found:
            label = f"{defn.title}: {self._short_value(defn, snap[defn.key])}"
            rows.append([await self._btn(ctx, label, SCREEN_KEY, codec_mod.ACTION_OPEN, defn.key)])
        rows.append(
            [nav_button(_T["search_again"], ACTIONS, A_SEARCH), nav_button(_T["to_settings"], SCREEN_ROOT)]
        )
        return View(text="\n".join(lines), parse_mode="HTML", keyboard=rows)

    # ------------------------------------------------------------ /set result screen

    def _remember(self, batch_id: str, done: _Done) -> None:
        self._results[batch_id] = done
        self._results.move_to_end(batch_id)
        while len(self._results) > _RESULT_CACHE:
            self._results.popitem(last=False)

    async def _done_screen(self, ctx: ScreenCtx, arg: Any) -> View:
        done = self._results.get(arg) if isinstance(arg, str) else None
        if done is None or done.user_id != ctx.user.user_id:
            self._stale(screen=True)
        defn = await self._need_key(ctx, done.key, "done", screen=True)
        fresh = not done.undone and clock.monotonic() - done.created < self.undo_ttl.total_seconds()
        return await self._result_view(ctx, defn, done.result, undo=fresh, undone=done.undone, note=done.note)

    # ------------------------------------------------------------ commands

    def aiogram_router(self, name: str = "svbg-settings") -> Router:
        """``/set`` (owner) and ``/settings`` (owner and admins with ``settings.business``).

        Include it **before** :meth:`ScreenRouter.aiogram_router`.
        """
        router = Router(name=name)

        async def on_set(message: Message) -> None:
            if not await self.handle_set(message):
                raise SkipHandler

        async def on_settings(message: Message) -> None:
            if not await self.handle_settings(message):
                raise SkipHandler

        router.message.register(on_set, Command("set"))
        router.message.register(on_settings, Command("settings"))
        return router

    async def _load_user(self, tg_user: TgUser) -> UserCtx | None:
        try:
            return await self.router.user_loader(tg_user)
        except Exception:
            log.exception("user loader failed for a settings command")
            return None

    async def _delete(self, message: Message) -> bool:
        try:
            ok = await self.router.transport.call(
                DeleteMessage(chat_id=message.chat.id, message_id=message.message_id), chat_id=message.chat.id
            )
        except _TG_ERRORS as e:
            log.warning("could not delete a message with a secret: %s", type(e).__name__)
            return False
        return ok is not False and ok is not None

    async def _say(self, chat_id: int, text: str) -> None:
        try:
            await self.router.transport.call(SendMessage(chat_id=chat_id, text=text), chat_id=chat_id)
        except _TG_ERRORS as e:
            log.warning("settings reply failed: %s", type(e).__name__)

    async def handle_settings(self, message: Message) -> bool:
        """``/settings``: open «⚙️ Настройки» as a new main message. ``False`` = not for us."""
        if message.from_user is None or message.chat.type != "private":
            return False
        user = await self._load_user(message.from_user)
        if user is None or not can_open_settings(user):
            return False
        await self.router.show(user, message.chat.id, SCREEN_ROOT, new=True)
        return True

    async def handle_set(self, message: Message) -> bool:
        """``/set``, ``/set <query>``, ``/set KEY``, ``/set KEY value`` (owner only). ``False`` = not handled.

        A message whose value is a secret (or looks like a token) is deleted before anything else, in any
        chat; in a group nothing is applied. Values are registered for log masking only for the owner: an
        arbitrary user must not grow the process-wide secret registry or blank out ordinary strings in logs.
        """
        tg_user = message.from_user
        if tg_user is None:
            return False
        parts = (message.text or "").split(maxsplit=2)
        key_token = parts[1] if len(parts) > 1 else None
        value = parts[2] if len(parts) > 2 else None
        defn = self.registry.find(key_token) if key_token else None
        sensitive = value is not None and ((defn is not None and defn.is_secret) or mask(value) != value)
        if message.chat.type != "private":
            if not sensitive:
                return False
            if not await self._delete(message):
                await self._warn_group(message.chat.id)
            return True
        note: str | None = None
        if sensitive and not await self._delete(message):
            note = _T["secret_not_deleted"]
        user = await self._load_user(tg_user)
        if user is None:
            return sensitive
        if user.role != "owner":
            if user.at_least("support"):
                await self._report_denied(user, "command:/set")
                await self._say(message.chat.id, ui_texts.t(user.lang, "denied"))
                return True
            return sensitive
        if sensitive and value is not None:
            register_secret(value.strip())
        chat_id = message.chat.id
        try:  # a command abandons a half-filled form (as the form engine does for other commands)
            await self.router.ui_state.set_awaiting(user.user_id, None)
        except _DB_ERRORS as e:
            log.warning("could not reset the form state: %s", type(e).__name__)
        async with timeout_guard(
            "ui:/set", self.command_timeout, hub=self.router.hub, user_id=user.user_id, module="settings"
        ):
            if key_token is None:
                await self.router.show(user, chat_id, SCREEN_ROOT, new=True)
            elif defn is None:
                query = key_token if sensitive else " ".join(parts[1:])
                await self.router.show(user, chat_id, SCREEN_FIND, query, new=True)
            elif value is None:
                await self.router.show(user, chat_id, SCREEN_KEY, defn.key, new=True)
            else:
                task = self._start_apply(user, defn, value)
                # Keep time for rendering the result screen within the command timeout.
                budget = max(self.command_timeout - self.router.handler_timeout, self.command_timeout / 2)
                result = await self._settle(
                    task, user=user, chat_id=chat_id, key=defn.key, guard_s=budget, note=note, new=True
                )
                if result is None:
                    await self._say(chat_id, _T["applying_plain"])
                    return True
                self._remember(
                    result.batch_id, _Done(user.user_id, defn.key, result, note, clock.monotonic())
                )
                await self.router.show(user, chat_id, SCREEN_DONE, result.batch_id, new=True)
        return True

    async def _warn_group(self, chat_id: int) -> None:
        """Ask to delete a secret we could not delete — at most once a minute per group (no spam relay)."""
        moment = clock.monotonic()
        last = self._group_warned.get(chat_id)
        if last is not None and moment - last < _GROUP_WARN_COOLDOWN:
            return
        self._group_warned[chat_id] = moment
        self._group_warned.move_to_end(chat_id)
        while len(self._group_warned) > _GROUP_WARN_CACHE:
            self._group_warned.popitem(last=False)
        await self._say(chat_id, _T["secret_in_group"])

    async def drain(self) -> None:
        """Wait for changes still running in the background, including the delivery of their results
        (graceful shutdown). If the caller gives up (stop timeout), the changes themselves are not cancelled:
        ``asyncio.wait`` never cancels what it waits for, unlike ``gather``."""
        while self._tasks:
            await asyncio.wait(list(self._tasks))


def _value_validator(defn: SettingDef) -> Callable[[str], str]:
    """Form validator: the value must parse for ``defn``; returns the stripped text for ``apply``."""

    def check(raw: str) -> str:
        text = raw.strip()
        if defn.is_secret:
            register_secret(text)
        if values.is_unchanged_marker(defn, text):
            return text
        try:
            values.parse(defn, text)
        except values.SettingValueError as e:
            raise ValidationError(str(e)) from None
        return text

    return check


class _Deps(Protocol):
    @property
    def settings(self) -> SettingsService: ...


_REFERRAL_SQL: Final = """
SELECT (SELECT count(*) FROM referrals WHERE attached_at >= now() - interval '30 days') AS invited,
       (SELECT count(*) FROM referral_rewards
         WHERE status = 'granted' AND granted_at >= now() - interval '30 days') AS rewards
"""


def slice_extras(deps: Any) -> SliceExtras:
    """Live bits of two slices: the trial plan's card on «🎁 Пробный период», a 30-day line on «🤝 Рефералка»
    (one SQL, only while the program is on)."""
    import sqlalchemy as sa

    db = getattr(deps, "db", None)
    catalog = getattr(deps, "catalog", None)
    settings = getattr(deps, "settings", None)

    async def extras(slice_id: str, user: UserCtx) -> tuple[list[str], list[slices.Link]]:
        if slice_id == "p.trial" and catalog is not None and user.has_perm("plans"):
            plans = getattr(getattr(catalog, "snapshot", None), "plans", ())
            trial = next((p for p in plans if getattr(p, "is_trial", False)), None)
            if trial is not None:
                label = f"📦 Тариф «{_cut(trial.title('ru'), 30)}»"
                return [], [slices.Link(label, "pl", arg=str(trial.id), perm="plans")]
        if slice_id == "m.ref" and db is not None and settings is not None:
            if not settings.current().get("REFERRAL_ENABLED"):
                return [], []
            try:
                async with db.read() as conn:
                    row = (await conn.execute(sa.text(_REFERRAL_SQL))).mappings().one()
            except _DB_ERRORS as e:
                log.debug("referral numbers unavailable: %s", type(e).__name__)
                return [], []
            line = f"За 30 дней пришло по приглашениям: {row['invited']}, наград выдано: {row['rewards']}."
            return ["", line], []
        return [], []

    return extras


def setup(router: ScreenRouter, deps: _Deps) -> Router:
    """Module entry point for ``svbg.app`` (``setup(router, deps)``): register screens, return the commands
    router (include it before the screen router's catch-all handlers)."""
    screens = SettingsScreens(
        router,
        deps.settings,
        notes=getattr(deps, "settings_notes", None),
        extras=slice_extras(deps),
    )
    screens.install()
    on_stop = getattr(deps, "on_stop", None)
    if callable(on_stop):  # graceful shutdown waits for changes still being applied
        on_stop("settings ui", screens.drain)
    return screens.aiogram_router()
