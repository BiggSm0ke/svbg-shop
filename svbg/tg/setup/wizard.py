"""Setup wizard (07 §5 stage 1, 03 §7.1 without payments; 02 §1.3, §5.7).

Steps (one screen — one action, «Шаг N из 5», resumable — the state lives in ``config_meta['wizard']``):

1. **Владелец** — done by the one-time owner link (:mod:`svbg.tg.setup.owner`);
2. **Remnawave** — «🐳 На этом сервере» (``http://remnawave:3000``) or «🌐 Ввести адрес», then the API token.
   Both messages are deleted right after reading (the address too: a token pasted by mistake must not stay in
   the chat). The pair goes through :meth:`SettingsService.apply` (source ``wizard``): the component
   probes the candidate on its own session (self-test: version, scopes, token ``exp``, configuration) and
   only then the running client is swapped — a wrong token gives a clear error and the previous connection
   keeps working;
3. **Админ-группа** — status of the ``admin_chat`` component and a link to «🔔 Админ-чат»;
4. **Вебхуки панели** — the panel cannot be configured through its API, so the wizard builds a shell snippet
   that edits ``/opt/remnawave/.env`` **in place** (02 §5.7) from the *actual* address of the bot (container
   hostname in the docker network, or ``PUBLIC_URL``), never from documentation text. Mode A: the bot
   generates the secret; mode B («панель уже шлёт вебхуки другому боту»): the panel has one secret for
   every URL, so the owner pastes the existing one. Warnings: no spaces after commas in ``WEBHOOK_URL``, no
   inline comments; a pasted ``WEBHOOK_URL=…`` line is checked with the panel's own rule;
5. **Готовность** — checklist «N/M» and «🚀 Готово».

Access: every screen, action and form is owner-only (checked by the router on every callback and input).
The API token and the webhook secret are never written to the wizard state, the UI state or logs.
"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import logging
import re
import secrets
import socket
import string
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol
from urllib.parse import urlsplit

import sqlalchemy as sa
from aiogram import Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, Message
from sqlalchemy.dialects.postgresql import insert as pg_insert

from svbg.core import clock
from svbg.core.component import Health, HealthReport
from svbg.core.log import mask, register_secret
from svbg.core.settings.service import ApplyResult, Change, SettingsError
from svbg.core.tables import config_meta
from svbg.db.meta import JSONB
from svbg.remnawave.component import RemnawaveComponent
from svbg.remnawave.errors import RemnawaveError
from svbg.remnawave.transport import normalize_url
from svbg.tg.ui.forms import Field, Form, ValidationError
from svbg.tg.ui.forms import text as text_validator
from svbg.tg.ui.renderer import nav_button
from svbg.tg.ui.view import Redirect, Toast, View

if TYPE_CHECKING:
    from svbg.core.component import ComponentRegistry
    from svbg.core.settings.service import SettingsService
    from svbg.remnawave.models import SystemConfig
    from svbg.tg.ui.context import UserCtx
    from svbg.tg.ui.router import HandlerResult, ScreenCtx, ScreenRouter

__all__ = [
    "ACTIONS",
    "LOCAL_PANEL_URL",
    "META_KEY",
    "NOTIFY_GROUPS",
    "SCREEN",
    "STEPS",
    "ReadyItem",
    "SetupWizard",
    "WebhookActivity",
    "WebhookTarget",
    "WizardState",
    "WizardStore",
    "build_env_snippet",
    "check_webhook_url_line",
    "generate_webhook_secret",
    "missing_notify_groups",
    "panel_features",
    "setup",
    "webhook_target",
]

log = logging.getLogger("svbg.tg.setup.wizard")

SCREEN: Final = "setup.wiz"
ACTIONS: Final = "wiz"
META_KEY: Final = "wizard"
STATE_VERSION: Final = 1

S_PANEL: Final = "rw"
S_CHAT: Final = "chat"
S_HOOKS: Final = "wh"
S_READY: Final = "ready"
S_DONE: Final = "done"
STEPS: Final[tuple[str, ...]] = (S_PANEL, S_CHAT, S_HOOKS, S_READY)
_STEP_NO: Final = {S_PANEL: 2, S_CHAT: 3, S_HOOKS: 4, S_READY: 5}
TOTAL_STEPS: Final = 5

A_LOCAL: Final = "loc"
A_URL: Final = "url"
A_CHECK: Final = "chk"
A_SKIP: Final = "skip"
A_MODE_A: Final = "wa"
A_MODE_B: Final = "wb"
A_MODE_RESET: Final = "wm"
A_HOOK_CHECK: Final = "wchk"
A_LINE: Final = "wline"
A_FINISH: Final = "fin"

FORM_URL: Final = "setup.rw.url"
FORM_TOKEN: Final = "setup.rw.tok"  # noqa: S105 - a form name
FORM_SECRET: Final = "setup.wh.sec"  # noqa: S105 - a form name
FORM_LINE: Final = "setup.wh.line"

LOCAL_PANEL_URL: Final = "http://remnawave:3000"
PANEL_ENV_DIR: Final = "/opt/remnawave"
WEBHOOK_PATH: Final = "/webhooks/remnawave"
DEFAULT_WEB_PORT: Final = 8080
SECRET_LEN: Final = 64
SETTINGS_SCREEN_KEY: Final = "set.key"  # svbg.tg.admin.settings.SCREEN_KEY
SETTINGS_SCREEN_SECTION: Final = "set.sec"  # svbg.tg.admin.settings.SCREEN_SECTION
ADMIN_CHAT_SCREEN: Final = "achat"  # svbg.services.admin_chat.SCREEN
STATUS_SCREEN: Final = "status"  # svbg.tg.admin.status.SCREEN
HOME_SCREEN: Final = "home"

_CHECK_TEXT_MAX: Final = 1500
_SECRET_RE: Final = re.compile(r"^[A-Za-z0-9]{32,256}$")
_SAFE_URL_RE: Final = re.compile(r"^https?://[A-Za-z0-9.-]{1,253}(?::\d{1,5})?(?:/[A-Za-z0-9._~/-]*)?$")
_HOSTNAME_RE: Final = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_CONTAINER_ID_RE: Final = re.compile(r"^[0-9a-f]{12}$")
_JWT_RE: Final = re.compile(r"^[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}$")
_TG_ERRORS: Final = (OSError, TimeoutError)
_DB_ERRORS: Final = (sa.exc.SQLAlchemyError, OSError)

#: Panel notification groups the bot benefits from (02 §5.7): ``.env`` lines and what the config shows.
NOTIFY_GROUPS: Final[Mapping[str, tuple[tuple[str, str], ...]]] = {
    "expiration": (
        ("EXPIRATION_NOTIFICATIONS_ENABLED", "true"),
        ("EXPIRATION_NOTIFICATIONS", "'[-72,-24,24]'"),
    ),
    "bandwidth": (
        ("BANDWIDTH_USAGE_NOTIFICATIONS_ENABLED", "true"),
        ("BANDWIDTH_USAGE_NOTIFICATIONS_THRESHOLD", "'[80,95]'"),
    ),
    "not_connected": (
        ("NOT_CONNECTED_USERS_NOTIFICATIONS_ENABLED", "true"),
        ("NOT_CONNECTED_USERS_NOTIFICATIONS_AFTER_HOURS", "'[2,24]'"),
    ),
}

# Owner-facing texts (Russian) in one place.
_T: Final[dict[str, str]] = {
    "title": "🧭 <b>Мастер настройки</b> · шаг {n} из {total}",
    "menu": "🏠 Меню",
    "back": "⬅️ Назад",
    "next": "➡️ Дальше",
    "skip": "⏭ Пропустить",
    "skipped": "Шаг пропущен. Вернуться к нему можно через /setup",
    "status": "⚙️ Состояние",
    "settings": "⚙️ Настройки",
    "applying": "⏳ <b>Проверяю подключение…</b>\nРезультат появится здесь через несколько секунд.",
    "stale": "Кнопка устарела",
    "db": "Не сохранено: база данных недоступна. Попробуйте через минуту.",
    # panel
    "rw_head": "<b>Remnawave</b>",
    "rw_off": "Панель не подключена.",
    "rw_on": "Подключена: <code>{url}</code>",
    "rw_hint": (
        "Панель стоит на этом же сервере (бот и панель в одной docker-сети <code>remnawave-network</code>)? "
        "Тогда нажмите «🐳 На этом сервере», адрес будет <code>http://remnawave:3000</code>. "
        "Если нет, нажмите «🌐 Ввести адрес».\n"
        "Потом бот попросит API-токен (Панель → Настройки → API-токены). Присылайте его только "
        "в ответ на этот запрос. Сообщения с адресом и токеном бот сразу удаляет из чата."
    ),
    "rw_local": "🐳 На этом сервере",
    "rw_url": "🌐 Ввести адрес",
    "rw_check": "🔄 Проверить",
    "rw_advanced": "🧰 Дополнительно (Caddy, cookie)",
    "rw_last": "<b>Последняя проверка</b> ({at}):",
    "rw_ok": "✅ Подключено",
    "rw_fail": "❌ Не подключено: {reason}",
    "rw_kept": "Прежнее подключение продолжает работать.",
    "rw_need_url": "Сначала укажите адрес панели.",
    "url_prompt": (
        "🌐 Пришлите адрес панели, например panel.example.com или http://10.0.0.5:3000. "
        "Сообщение бот сразу удалит."
    ),
    "token_prompt": (
        "🔑 Пришлите API-токен панели (Панель → Настройки → API-токены). "
        "Сообщение с токеном бот сразу удалит."
    ),
    "token_prompt_for": "Адрес панели: {url}",
    "token_like": "Похоже на токен, а не на адрес. Сообщение удалено, сначала пришлите адрес панели.",
    "url_like": "Это адрес, а нужен API-токен (Панель → Настройки → API-токены).",
    "token_space": "Токен должен быть одной строкой, без пробелов.",
    "token_short": "Слишком короткий токен, скопируйте его целиком.",
    "long_label": "Слишком длинное имя в адресе.",
    # admin chat
    "chat_head": "<b>Админ-группа</b>",
    "chat_hint": (
        "Создайте супергруппу, включите в ней «Темы» и нажмите «👥 Подключить группу»: бот сам создаст темы "
        "для оплат, ошибок, панели и т. д. Без группы уведомления приходят вам в личку.\n"
        "После подключения вернитесь сюда командой /setup."
    ),
    "chat_none": "В этой сборке нет модуля админ-чата, шаг можно пропустить.",
    "chat_off": "⚪ Не подключена, уведомления идут вам в личку.",
    "chat_connect": "👥 Подключить группу",
    "chat_check": "🔄 Проверить",
    # webhooks
    "wh_head": "<b>Вебхуки панели</b> (рекомендуется)",
    "wh_no_panel": "Сначала подключите панель (шаг 2). Без вебхуков бот работает через периодическую сверку.",
    "wh_panel_on": "В панели вебхуки: <b>включены</b>.",
    "wh_panel_off": "В панели вебхуки: <b>выключены</b>. Бот сверяется с панелью каждые 15 минут.",
    "wh_panel_unknown": "Включены ли вебхуки в панели, пока неизвестно (нажмите «🔄 Проверить»).",
    "wh_ask_b": (
        "Панель уже отправляет вебхуки. Они идут другому боту (Remnashop, Bedolaga, свой скрипт)? "
        "У панели один секрет на все адреса, поэтому в этом случае бот возьмёт секрет старого бота, "
        "а не создаст свой."
    ),
    "wh_ask_a": "Бот создаст секрет и покажет готовый блок для файла <code>.env</code> панели.",
    "wh_yes_b": "Да, другому боту — вставлю его секрет",
    "wh_no_a": "Нет — пусть бот создаст секрет",
    "wh_show_a": "📋 Показать настройки",
    "wh_have_b": "У панели уже есть секрет (другой бот)",
    "wh_mode_a": "Режим: бот создал свой секрет.",
    "wh_mode_b": "Режим: общий секрет с другим ботом.",
    "wh_target": "Адрес для панели: <code>{url}</code>",
    "wh_target_docker": "(по docker-сети, имя контейнера бота)",
    "wh_target_public": "(через PUBLIC_URL)",
    "wh_run": (
        "<b>Выполните на сервере панели</b> (строки правятся на месте, копия .env сохраняется, повторный "
        "запуск безопасен):"
    ),
    "wh_warn_commas": (
        "⚠️ В <code>WEBHOOK_URL</code> адреса пишутся через запятую <b>без пробелов</b>: с пробелом после "
        "запятой панель не запустится."
    ),
    "wh_warn_comments": "⚠️ Никаких комментариев (<code># …</code>) в строках <code>WEBHOOK_*</code>.",
    "wh_warn_shared_a": (
        "⚠️ Если панель уже шлёт вебхуки другому боту, новый секрет сломает ему проверку подписи. Тогда "
        "выберите «🔁 Сменить режим» → «другой бот»."
    ),
    "wh_warn_shared_b": (
        "⚠️ Секрет общий с другим ботом: не меняйте <code>WEBHOOK_SECRET_HEADER</code>, пока старый адрес "
        "есть в <code>WEBHOOK_URL</code>."
    ),
    "wh_secret_prompt": (
        "🔑 Пришлите значение WEBHOOK_SECRET_HEADER из /opt/remnawave/.env (у старого бота это обычно "
        "REMNAWAVE_WEBHOOK_SECRET). Сообщение бот сразу удалит."
    ),
    "wh_secret_bad": "Секрет панели должен быть не короче 32 символов, только латиница и цифры.",
    "wh_secret_missing": "Секрет ещё не сохранён. Нажмите «🔑 Вставить секрет».",
    "wh_secret_paste": "🔑 Вставить секрет",
    "wh_saved_fail": "❌ Секрет не сохранён: {reason}",
    "wh_check": "🔄 Проверить",
    "wh_line": "✍️ Проверить строку WEBHOOK_URL",
    "wh_mode_reset": "🔁 Сменить режим",
    "wh_line_prompt": "✍️ Пришлите строку WEBHOOK_URL=… из .env панели, бот проверит её по правилам панели.",
    "wh_line_ok": "✅ Строка WEBHOOK_URL корректна: панель её примет, адрес бота в списке.",
    "wh_line_bad": "❌ Строка WEBHOOK_URL:",
    "wh_last": "<b>Проверка</b> ({at}):",
    "wh_chk_err": "❌ Не удалось прочитать настройки панели: {error}",
    "wh_chk_scope": "⚠️ У токена нет права system:configuration, поэтому бот не видит, включены ли вебхуки.",
    "wh_chk_off": (
        "❌ Вебхуки в панели выключены: выполните блок выше и перезапустите панель (docker compose down && "
        "docker compose up -d)."
    ),
    "wh_chk_on": "✅ Вебхуки в панели включены.",
    "wh_chk_seen": "✅ Подписанные вебхуки доходят: последнее событие {at}.",
    "wh_chk_badsig": (
        "❌ Вебхуки доходят с неверной подписью: секрет не совпадает. Если панель шлёт вебхуки ещё и другому "
        "боту, выберите «🔁 Сменить режим» → «другой бот» и вставьте его секрет."
    ),
    "wh_chk_wait": (
        "⏳ Включены, ждём первое событие: создайте или измените любого пользователя в панели и нажмите "
        "«🔄 Проверить» ещё раз. Если событий так и нет, контейнер панели не достаёт до адреса бота."
    ),
    "wh_need_public": (
        "Имя контейнера бота похоже на случайный id ({host}), по нему панель бота не найдёт. Задайте в "
        "docker-compose бота <code>hostname: svbg-shop</code> или укажите PUBLIC_URL."
    ),
    "wh_bad_public": "В PUBLIC_URL есть символы, которые панель не примет в WEBHOOK_URL. Исправьте адрес.",
    "wh_insecure_public": (
        "Вебхуки через интернет должны идти по https://. В событиях панели есть пароли и ключи "
        "пользователей, по http они ушли бы открытым текстом. Укажите PUBLIC_URL с https:// (http подходит "
        "только для внутреннего адреса: приватный IP или имя без точек в docker-сети)."
    ),
    "wh_not_resolving": (
        "⚠️ Имя <code>{host}</code> не находится в DNS контейнера бота. Проверьте, что бот подключён к сети "
        "<code>remnawave-network</code> (или укажите PUBLIC_URL)."
    ),
    "wh_same_network": "⚠️ Панель должна быть в той же docker-сети, что и бот; иначе укажите PUBLIC_URL.",
    "public_url": "🌐 Указать PUBLIC_URL",
    # readiness
    "ready_head": "<b>Готовность: {ok}/{total}</b>",
    "ready_owner": "Владелец назначен",
    "ready_panel_off": "Remnawave не подключена",
    "ready_panel": "Remnawave: {summary}",
    "ready_scopes": "Права токена: все нужные",
    "ready_scopes_missing": "Токену не хватает прав: {scopes}",
    "ready_scopes_unknown": "Права токена не проверены. Нажмите «🔄 Проверить» на шаге Remnawave",
    "ready_chat": "Админ-группа: {summary}",
    "ready_chat_off": "Админ-группа не подключена, уведомления идут в личку",
    "ready_hooks_on": "Вебхуки панели включены",
    "ready_hooks_off": "Вебхуки панели выключены, бот сверяется с панелью по расписанию",
    "ready_hooks_unknown": "Вебхуки панели: неизвестно",
    "ready_hooks_seen": "Первое событие вебхука получено",
    "ready_hooks_wait": "Подписанных вебхуков от панели ещё не было",
    "ready_maint": "Включены техработы: {text}",
    "ready_fix": "Исправить: {label}",
    "finish": "🚀 Готово",
    "done": (
        "✅ <b>Настройка завершена</b> ({ok}/{total}).\n\n"
        "Мастер можно открыть снова командой /setup, состояние бота смотрите в /status. "
        "Тарифы и оплата настраиваются в «⚙️ Настройки»."
    ),
    "step_panel": "Remnawave",
    "step_chat": "Админ-группа",
    "step_hooks": "Вебхуки",
}

_FEATURES: Final[tuple[tuple[str, str, str], ...]] = (
    # key in ConfigNotifications, label, env names (shown to the owner)
    ("webhook", "Вебхуки", "WEBHOOK_ENABLED"),
    ("expiration_notifications", "Напоминания об истечении", "EXPIRATION_NOTIFICATIONS"),
    ("bandwidth_usage", "Пороги трафика", "BANDWIDTH_USAGE_NOTIFICATIONS_THRESHOLD"),
    (
        "not_connected_after",
        "Не подключившиеся пользователи",
        "NOT_CONNECTED_USERS_NOTIFICATIONS_AFTER_HOURS",
    ),
)
_GROUP_OF_FEATURE: Final = {
    "expiration_notifications": "expiration",
    "bandwidth_usage": "bandwidth",
    "not_connected_after": "not_connected",
}


# ================================================================================ pure helpers


def generate_webhook_secret(length: int = SECRET_LEN) -> str:
    """A panel-compatible ``WEBHOOK_SECRET_HEADER``: ≥ 32 characters, letters and digits only."""
    if length < 32:
        raise ValueError("the panel requires at least 32 characters")
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def is_panel_secret(value: str | None) -> bool:
    return bool(value) and _SECRET_RE.match(value or "") is not None


@dataclass(frozen=True, slots=True)
class WebhookTarget:
    """Where the panel should send webhooks. ``url`` is ``None`` when it cannot be built (see ``problem``)."""

    url: str | None
    source: Literal["docker", "public"] | None
    notes: tuple[str, ...] = ()
    problem: str | None = None


def _is_docker_name(host: str | None) -> bool:
    """``remnawave`` / ``svbg-shop``: a single-label name of a docker service (no dots, not an IP)."""
    if not host or "." in host or ":" in host:
        return False
    return _HOSTNAME_RE.match(host) is not None and not host.isdigit()


def _is_private_http_ok(url: str) -> bool:
    """``http://`` only inside a private network: a private/loopback IP or a single-label name."""
    parts = urlsplit(url)
    if parts.scheme == "https":
        return True
    host = parts.hostname or ""
    if _is_docker_name(host):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


def webhook_target(
    *,
    hostname: str,
    public_url: str | None,
    panel_url: str | None,
    port: int = DEFAULT_WEB_PORT,
    resolves: bool | None = None,
) -> WebhookTarget:
    """Build the webhook URL from the actual hostname / ``PUBLIC_URL`` (02 §5.7 p.1, 04 §2.5).

    * the panel is reached by a docker name (``http://remnawave:3000``) → the bot is in the same docker
      network:
      ``http://<hostname>:<port>/webhooks/remnawave``;
    * otherwise ``{PUBLIC_URL}/webhooks/remnawave`` when ``PUBLIC_URL`` is set;
    * otherwise the docker URL with a warning; a hostname that looks like a container id (12 hex) cannot be
      reached by the panel — the owner is asked for ``PUBLIC_URL`` or a fixed ``hostname``;
    * an ``http://`` ``PUBLIC_URL`` is used only for a private IP or a single-label name — webhook bodies
      carry user passwords and keys (02 §5.1), so the internet requires ``https://``.
    """
    host_ok = _HOSTNAME_RE.match(hostname or "") is not None and not _CONTAINER_ID_RE.match(hostname or "")
    docker_url = f"http://{hostname}:{port}{WEBHOOK_PATH}" if host_ok else None
    panel_host = urlsplit(panel_url).hostname if panel_url else None
    public = (public_url or "").strip().rstrip("/") or None
    public_hook = f"{public}{WEBHOOK_PATH}" if public else None
    if public_hook is not None and not _SAFE_URL_RE.match(public_hook):
        public_hook = None
        if not (docker_url and _is_docker_name(panel_host)):
            return WebhookTarget(None, None, problem=_T["wh_bad_public"])
    elif public_hook is not None and not _is_private_http_ok(public_hook):
        # webhook bodies carry user passwords/keys (02 §5.1): HMAC protects integrity, not secrecy
        public_hook = None
        if not (docker_url and _is_docker_name(panel_host)):
            return WebhookTarget(None, None, problem=_T["wh_insecure_public"])
    notes: list[str] = []
    if docker_url is not None and resolves is False:
        notes.append(_T["wh_not_resolving"].format(host=html.escape(hostname)))
    if docker_url is not None and _is_docker_name(panel_host):
        return WebhookTarget(docker_url, "docker", tuple(notes))
    if public_hook is not None:
        return WebhookTarget(public_hook, "public")
    if docker_url is not None:
        return WebhookTarget(docker_url, "docker", (*notes, _T["wh_same_network"]))
    return WebhookTarget(None, None, problem=_T["wh_need_public"].format(host=html.escape(hostname or "?")))


def build_env_snippet(
    *,
    our_url: str | None,
    secret: str | None = None,
    notify: Sequence[str] = (),
    env_dir: str = PANEL_ENV_DIR,
) -> str:
    """Shell block for the panel host: modify ``.env`` lines in place, then restart the panel (02 §5.7).

    ``our_url`` adds the bot to ``WEBHOOK_URL`` (keeping other bots' URLs; a repeated run is a no-op) and
    turns webhooks on; ``secret`` (mode A only) sets ``WEBHOOK_SECRET_HEADER``; ``notify`` — groups of
    :data:`NOTIFY_GROUPS` to switch on. Values are validated so the block is safe to paste into a shell.
    """
    if our_url is not None and not _SAFE_URL_RE.match(our_url):
        raise ValueError("unsafe webhook URL")
    if secret is not None and not _SECRET_RE.match(secret):
        raise ValueError("the secret must be 32+ letters and digits")
    unknown = [g for g in notify if g not in NOTIFY_GROUPS]
    if unknown:
        raise ValueError(f"unknown notification groups: {unknown}")
    if not re.fullmatch(r"/[A-Za-z0-9._/-]+", env_dir):
        raise ValueError("unsafe directory")
    lines = [
        f'cd {env_dir} && cp .env ".env.bak.$(date +%s)"',
        # $2 is escaped for sed (& | and backslash stay literal); lines are replaced, never duplicated
        "setkv() { v=$(printf '%s' \"$2\" | sed 's/[&|\\]/\\\\&/g'); "
        'if grep -q "^$1=" .env; then sed -i "s|^$1=.*|$1=$v|" .env; '
        'else printf \'%s=%s\\n\' "$1" "$2" >> .env; fi; }',
    ]
    if our_url is not None:
        lines += [
            f"OUR_URL='{our_url}'",
            "CUR=$(grep '^WEBHOOK_URL=' .env | tail -n1 | cut -d= -f2- | sed 's/[[:space:]]#.*//' "
            '| tr -d " \\"\'")',
            'case ",$CUR," in',
            '  *",$OUR_URL,"*) ;;',
            '  ",,") setkv WEBHOOK_URL "$OUR_URL" ;;',
            '  *) setkv WEBHOOK_URL "$CUR,$OUR_URL" ;;',
            "esac",
            "setkv WEBHOOK_ENABLED true",
        ]
        if secret is not None:
            lines.append(f"setkv WEBHOOK_SECRET_HEADER '{secret}'")
    for group in notify:
        lines += [f"setkv {key} {value}" for key, value in NOTIFY_GROUPS[group]]
    if our_url is not None:
        lines.append("grep '^WEBHOOK_' .env")
    lines.append("docker compose down && docker compose up -d")
    return "\n".join(lines)


def check_webhook_url_line(line: str, our_url: str | None) -> list[str]:
    """Check a ``WEBHOOK_URL=…`` line with the panel's rule (``split(',')`` without trim,
    ``startsWith('http')``).

    Returns human problems (Russian, plain text); empty — the panel accepts it and our URL is listed.
    """
    text = line.strip()
    if not text:
        return ["Пустая строка."]
    if "\n" in text:
        return ["Пришлите одну строку WEBHOOK_URL=…"]
    problems: list[str] = []
    if text.startswith("WEBHOOK_URL="):
        value = text[len("WEBHOOK_URL=") :]
    elif text.startswith(("http://", "https://")):
        value = text
    else:
        return ["Строка должна начинаться с WEBHOOK_URL="]
    if "#" in value:
        problems.append("Уберите комментарий (# …): в строках WEBHOOK_* комментариев быть не должно.")
        value = value.split("#", 1)[0]
    if any(ch in value for ch in "\"'"):
        problems.append("Уберите кавычки вокруг адресов.")
    if any(ch.isspace() for ch in value.rstrip()):
        problems.append(
            "Уберите пробелы: адреса пишутся через запятую без пробелов, иначе панель не запустится."
        )
    parts = value.rstrip().split(",")
    for part in parts:
        if not part.startswith(("http://", "https://")):
            shown = part[:80] if part else "пусто"
            problems.append(
                f"«{shown}» не начинается с http:// или https://, с таким адресом панель не запустится."
            )
    clean = [p.strip(" \"'") for p in parts]
    if our_url is not None and our_url not in clean:
        problems.append(f"В строке нет адреса бота: {our_url}")
    if len({p for p in clean if p}) != len([p for p in clean if p]):
        problems.append("Один и тот же адрес указан дважды, панель будет слать каждое событие два раза.")
    return problems


def panel_features(config: SystemConfig) -> list[tuple[str, str, bool, str]]:
    """``(label, env name, enabled, shown value)`` for «Что включено в панели»."""
    out: list[tuple[str, str, bool, str]] = []
    notes = config.notifications
    for attr, label, env in _FEATURES:
        value = getattr(notes, attr)
        if attr == "webhook":
            out.append((label, env, bool(value), "включены" if value else "выключены"))
            continue
        enabled = value is not None
        shown = json.dumps(list(value), separators=(",", ":")) if enabled else "выключено"
        out.append((label, env, enabled, shown))
    return out


def missing_notify_groups(config: SystemConfig) -> list[str]:
    """Notification groups that are off in the panel (their lines go into the snippet)."""
    notes = config.notifications
    return [group for attr, group in _GROUP_OF_FEATURE.items() if getattr(notes, attr) is None]


def snippet_block(snippet: str) -> str:
    return f'<pre><code class="language-bash">{html.escape(snippet, quote=False)}</code></pre>'


# ================================================================================ state


@dataclass(slots=True)
class WizardState:
    """Persisted progress (``config_meta['wizard']``). Never holds a token or a secret."""

    step: str = S_PANEL
    skipped: list[str] = field(default_factory=list)
    webhook_mode: Literal["A", "B"] | None = None
    panel_check: dict[str, Any] | None = None  # {"ok": bool, "text": str, "at": iso}
    webhook_check: dict[str, Any] | None = None  # {"text": str, "at": iso}
    line_check: dict[str, Any] | None = None  # {"text": str, "at": iso}
    finished_at: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "v": STATE_VERSION,
            "step": self.step,
            "skipped": list(self.skipped),
            "webhook_mode": self.webhook_mode,
            "panel_check": self.panel_check,
            "webhook_check": self.webhook_check,
            "line_check": self.line_check,
            "finished_at": self.finished_at,
        }

    @classmethod
    def from_json(cls, value: Any) -> WizardState:
        if not isinstance(value, Mapping) or value.get("v") != STATE_VERSION:
            return cls()
        step = value.get("step")
        mode = value.get("webhook_mode")
        skipped = value.get("skipped")
        return cls(
            step=step if step in (*STEPS, S_DONE) else S_PANEL,
            skipped=[s for s in skipped if s in STEPS] if isinstance(skipped, list) else [],
            webhook_mode=mode if mode in ("A", "B") else None,
            panel_check=_check_dict(value.get("panel_check")),
            webhook_check=_check_dict(value.get("webhook_check")),
            line_check=_check_dict(value.get("line_check")),
            finished_at=value.get("finished_at") if isinstance(value.get("finished_at"), str) else None,
        )


def _check_dict(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping) or not isinstance(value.get("text"), str):
        return None
    return {"ok": bool(value.get("ok")), "text": value["text"], "at": str(value.get("at") or "")}


def _jsonb(value: Any) -> sa.ColumnElement[Any]:
    return sa.cast(sa.literal(json.dumps(value, ensure_ascii=False), sa.Text), JSONB)


class _Database(Protocol):
    def tx(self) -> Any: ...

    def read(self) -> Any: ...


class WizardStore:
    """``config_meta['wizard']`` with a write-through cache (one owner process)."""

    def __init__(self, db: _Database) -> None:
        self._db = db
        self._cache: WizardState | None = None

    async def load(self) -> WizardState:
        if self._cache is not None:
            return self._cache
        async with self._db.read() as conn:
            raw = (
                await conn.execute(
                    sa.select(sa.cast(config_meta.c.value, sa.Text)).where(config_meta.c.key == META_KEY)
                )
            ).scalar()
        try:
            data = json.loads(raw) if raw else None
        except ValueError:
            data = None
        self._cache = WizardState.from_json(data)
        return self._cache

    async def save(self, state: WizardState) -> None:
        ts = clock.now()
        stmt = pg_insert(config_meta).values(key=META_KEY, value=_jsonb(state.to_json()), updated_at=ts)
        stmt = stmt.on_conflict_do_update(
            index_elements=[config_meta.c.key],
            set_={"value": stmt.excluded.value, "updated_at": stmt.excluded.updated_at},
        )
        async with self._db.tx() as conn:
            await conn.execute(stmt)
        self._cache = state

    def forget(self) -> None:
        self._cache = None


# ================================================================================ wizard


@dataclass(frozen=True, slots=True)
class WebhookActivity:
    """What the webhook receiver saw (wired by the integration from ``rw_inbox``)."""

    last_ok_at: datetime | None = None
    last_bad_signature_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ReadyItem:
    status: Literal["ok", "warn", "fail"]
    text: str
    fix_step: str | None = None

    @property
    def icon(self) -> str:
        return {"ok": "✅", "warn": "⚠️", "fail": "❌"}[self.status]


WebhookSeen = Callable[[], Awaitable[WebhookActivity | None]]
Resolver = Callable[[str], Awaitable[bool]]


class _Maintenance(Protocol):
    @property
    def active(self) -> bool: ...

    @property
    def state(self) -> Any: ...


async def _default_resolver(host: str) -> bool:
    try:
        async with asyncio.timeout(2.0):
            await asyncio.get_running_loop().getaddrinfo(host, None)
    except (OSError, TimeoutError):
        return False
    return True


def _at(value: datetime | None = None) -> str:
    return (value or clock.now()).strftime("%d.%m %H:%M UTC")


def _clip(text: str, limit: int = _CHECK_TEXT_MAX) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _url_validator(value: str) -> str:
    raw = value.strip()
    if _JWT_RE.match(raw) or (len(raw) > 60 and "." not in raw and "/" not in raw):
        register_secret(raw)  # it is a token: keep it out of every log line from now on
        raise ValidationError(_T["token_like"])
    try:
        url = normalize_url(raw)
    except ValueError as exc:
        text = str(exc)
        raise ValidationError(text[:1].upper() + text[1:]) from None
    host = urlsplit(url).hostname or ""
    if any(len(label) > 63 for label in host.split(".")):
        raise ValidationError(_T["long_label"])
    return url


def _token_validator(value: str) -> str:
    token = value.strip()
    if token.startswith(("http://", "https://")):
        raise ValidationError(_T["url_like"])
    if any(ch.isspace() for ch in token):
        raise ValidationError(_T["token_space"])
    if len(token) < 20:
        raise ValidationError(_T["token_short"])
    if len(token) > 4096:
        raise ValidationError("Слишком длинный токен")
    register_secret(token)  # only a plausible token: a URL or a short word must not be masked everywhere
    return token


def _secret_validator(value: str) -> str:
    secret = value.strip()
    if len(secret) >= 16 and not any(ch.isspace() for ch in secret):
        register_secret(secret)  # also a slightly malformed real secret
    if not _SECRET_RE.match(secret):
        raise ValidationError(_T["wh_secret_bad"])
    return secret


class SetupWizard:
    """Registers the wizard screens, actions and forms on a router; serves ``/setup``.

    ``webhook_seen`` (optional) reports what the webhook receiver saw (first signed event, bad signatures);
    ``hostname`` / ``resolver`` are injectable for tests; ``apply_wait`` — how long a handler waits for the
    panel check before answering «Проверяю…» (the result is shown as soon as it is known).
    """

    def __init__(
        self,
        router: ScreenRouter,
        *,
        db: _Database,
        settings: SettingsService,
        components: ComponentRegistry,
        maintenance: _Maintenance | None = None,
        webhook_seen: WebhookSeen | None = None,
        hostname: Callable[[], str] = socket.gethostname,
        resolver: Resolver = _default_resolver,
        web_port: int = DEFAULT_WEB_PORT,
        apply_wait: float | None = None,
        call_timeout: float = 10.0,
    ) -> None:
        self.router = router
        self.store = WizardStore(db)
        self.settings = settings
        self.components = components
        self.maintenance = maintenance
        self.webhook_seen = webhook_seen
        self._hostname = hostname
        self._resolver = resolver
        self.web_port = web_port
        self.apply_wait = apply_wait
        self.call_timeout = call_timeout
        self._tasks: set[asyncio.Future[Any]] = set()
        self._installed = False

    # ------------------------------------------------------------ registration

    def install(self) -> None:
        if self._installed:
            return
        self._installed = True
        r, owner = self.router, {"required_role": "owner"}
        r.screen(SCREEN, **owner)(self._screen)
        for name, fn in (
            (A_LOCAL, self._act_local),
            (A_URL, self._act_url),
            (A_CHECK, self._act_check),
            (A_SKIP, self._act_skip),
            (A_MODE_A, self._act_mode_a),
            (A_MODE_B, self._act_mode_b),
            (A_MODE_RESET, self._act_mode_reset),
            (A_HOOK_CHECK, self._act_hook_check),
            (A_LINE, self._act_line),
            (A_FINISH, self._act_finish),
        ):
            r.action(ACTIONS, name, **owner)(fn)
        back = self._back_to(S_PANEL)
        r.form(
            Form(
                FORM_URL,
                (Field("url", _T["url_prompt"], _url_validator, secret=True),),
                on_done=self._url_done,
                on_cancel=back,
                **owner,
            )
        )
        r.form(
            Form(
                FORM_TOKEN,
                (Field("token", _T["token_prompt"], _token_validator, secret=True),),
                on_done=self._token_done,
                on_cancel=back,
                **owner,
            )
        )
        r.form(
            Form(
                FORM_SECRET,
                (Field("secret", _T["wh_secret_prompt"], _secret_validator, secret=True),),
                on_done=self._secret_done,
                on_cancel=self._back_to(S_HOOKS),
                **owner,
            )
        )
        r.form(
            Form(
                FORM_LINE,
                (Field("line", _T["wh_line_prompt"], text_validator(max_len=2000)),),
                on_done=self._line_done,
                on_cancel=self._back_to(S_HOOKS),
                **owner,
            )
        )

    def aiogram_router(self, name: str = "svbg-setup-wizard") -> Router:
        """``/setup`` (owner, private chat): open the wizard at the saved step."""
        router = Router(name=name)

        async def on_setup(message: Message) -> None:
            if not await self.handle_setup(message):
                raise SkipHandler

        router.message.register(on_setup, Command("setup"))
        return router

    async def handle_setup(self, message: Message) -> bool:
        if message.from_user is None or message.chat.type != "private":
            return False
        try:
            user = await self.router.user_loader(message.from_user)
        except Exception:
            log.exception("user loader failed for /setup")
            return False
        if user is None or user.role != "owner":
            return False
        await self.router.show(user, message.chat.id, SCREEN, new=True)
        return True

    async def drain(self, grace: float = 10.0) -> None:
        """Shutdown: wait for checks still running, then cancel them."""
        pending = set(self._tasks)
        if not pending:
            return
        _done, still = await asyncio.wait(pending, timeout=grace)
        for task in still:
            task.cancel()

    def _spawn(self, coro: Awaitable[Any]) -> asyncio.Future[Any]:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Future[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("setup wizard background task failed", exc_info=task.exception())

    def _back_to(self, step: str) -> Callable[[ScreenCtx], Awaitable[HandlerResult]]:
        async def back(_ctx: ScreenCtx) -> HandlerResult:
            return Redirect(SCREEN, step)

        return back

    # ------------------------------------------------------------ helpers

    def panel(self) -> RemnawaveComponent | None:
        comp = self.components.find("remnawave")
        return comp if isinstance(comp, RemnawaveComponent) else None

    def _wait_budget(self) -> float:
        timeout = self.router.handler_timeout
        budget = max(timeout - 3.0, timeout * 0.5)
        return budget if self.apply_wait is None else min(self.apply_wait, budget)

    def _call_budget(self) -> float:
        return max(0.5, min(self.call_timeout, self.router.handler_timeout - 2.0))

    async def _state(self) -> WizardState:
        return await self.store.load()

    async def _save(self, state: WizardState) -> bool:
        try:
            await self.store.save(state)
        except _DB_ERRORS as exc:
            log.warning("setup wizard: cannot save the state: %s", type(exc).__name__)
            self.store.forget()
            return False
        return True

    def _header(self, step: str) -> str:
        return _T["title"].format(n=_STEP_NO.get(step, TOTAL_STEPS), total=TOTAL_STEPS)

    @staticmethod
    def _nav(step: str, *, can_skip: bool, done: bool) -> list[list[InlineKeyboardButton]]:
        idx = STEPS.index(step)
        rows: list[list[InlineKeyboardButton]] = []
        nxt = STEPS[idx + 1] if idx + 1 < len(STEPS) else None
        if nxt is not None:
            if done or not can_skip:
                rows.append([nav_button(_T["next"], SCREEN, arg=nxt)])
            else:
                rows.append([nav_button(_T["skip"], ACTIONS, A_SKIP, step)])
        bottom = [nav_button(_T["menu"], HOME_SCREEN)]
        if idx > 0:
            bottom.insert(0, nav_button(_T["back"], SCREEN, arg=STEPS[idx - 1]))
        rows.append(bottom)
        return rows

    # ------------------------------------------------------------ screen

    async def _screen(self, ctx: ScreenCtx, arg: Any) -> View | Redirect:
        state = await self._state()
        step = arg if isinstance(arg, str) and arg in (*STEPS, S_DONE) else None
        if step is None:
            step = S_READY if state.step == S_DONE else state.step
        if S_DONE not in (step, state.step) and state.step != step:
            state.step = step
            await self._save(state)
        return await self._render(ctx, step, state)

    async def _render(self, ctx: ScreenCtx, step: str, state: WizardState) -> View:
        if step == S_PANEL:
            return self._panel_view(state)
        if step == S_CHAT:
            return await self._chat_view()
        if step == S_HOOKS:
            return await self._hooks_view(ctx, state)
        if step == S_DONE:
            return await self._done_view()
        return await self._ready_view()

    # ------------------------------------------------------------ step: Remnawave

    def _panel_view(self, state: WizardState) -> View:
        comp = self.panel()
        lines = [self._header(S_PANEL), _T["rw_head"], ""]
        url = self.settings.current().get("REMNAWAVE_URL")
        configured = comp is not None and comp.configured
        if configured and url:
            lines.append(_T["rw_on"].format(url=html.escape(str(url))))
            caps = comp.capabilities if comp is not None else None
            if caps is not None:
                lines.append(html.escape(caps.gate.message_ru))
        else:
            lines.append(_T["rw_off"])
        check = state.panel_check
        if check is not None:
            lines += ["", _T["rw_last"].format(at=html.escape(check["at"])), html.escape(check["text"])]
        lines += ["", _T["rw_hint"]]
        keyboard: list[list[InlineKeyboardButton]] = [
            [nav_button(_T["rw_local"], ACTIONS, A_LOCAL), nav_button(_T["rw_url"], ACTIONS, A_URL)]
        ]
        if configured:
            keyboard.append([nav_button(_T["rw_check"], ACTIONS, A_CHECK)])
        keyboard.append([nav_button(_T["rw_advanced"], SETTINGS_SCREEN_SECTION, arg="remnawave")])
        keyboard += self._nav(S_PANEL, can_skip=True, done=configured)
        return View(text="\n".join(lines), parse_mode="HTML", keyboard=keyboard)

    async def _act_local(self, ctx: ScreenCtx, _arg: Any) -> HandlerResult:
        return await self._token_prompt(ctx, LOCAL_PANEL_URL)

    async def _act_url(self, ctx: ScreenCtx, _arg: Any) -> HandlerResult:
        return await ctx.start_form(FORM_URL)

    async def _url_done(self, ctx: ScreenCtx, data: dict[str, Any]) -> HandlerResult:
        url = data.get("url")
        if not isinstance(url, str) or not url:
            return Redirect(SCREEN, S_PANEL, toast=_T["rw_need_url"])
        return await self._token_prompt(ctx, url)

    async def _token_prompt(self, ctx: ScreenCtx, url: str) -> View:
        view = await ctx.start_form(FORM_TOKEN, {"url": url})
        view.text = _T["token_prompt_for"].format(url=url) + "\n\n" + view.text
        return view

    async def _token_done(self, ctx: ScreenCtx, data: dict[str, Any]) -> HandlerResult:
        url, token = data.get("url"), data.get("token")
        if not isinstance(url, str) or not url:
            return Redirect(SCREEN, S_PANEL, toast=_T["rw_need_url"])
        if not isinstance(token, str) or not token:
            return Redirect(SCREEN, S_PANEL)
        return await self._connect(ctx, url, token)

    async def _connect(self, ctx: ScreenCtx, url: str, token: str) -> HandlerResult:
        register_secret(token)
        comp = self.panel()
        before = comp.last_report if comp is not None else None
        was_configured = comp is not None and comp.configured
        changes = [Change("REMNAWAVE_URL", url), Change("REMNAWAVE_TOKEN", token)]
        task = self._spawn(
            self._apply_and_record(changes, ctx.user.user_id, before=before, was_configured=was_configured)
        )
        done, _ = await asyncio.wait({task}, timeout=self._wait_budget())
        if task in done:
            task.result()
            return Redirect(SCREEN, S_PANEL)
        self._spawn(self._show_later(task, ctx.user, ctx.chat_id, S_PANEL))
        return View(text=_T["applying"], parse_mode="HTML", keyboard=[[nav_button(_T["menu"], HOME_SCREEN)]])

    async def _show_later(self, task: asyncio.Future[Any], user: UserCtx, chat_id: int, step: str) -> None:
        try:
            await asyncio.shield(task)
        except Exception:
            log.debug("delayed wizard check failed", exc_info=True)
        try:
            await self.router.show(user, chat_id, SCREEN, step)
        except (*_TG_ERRORS, *_DB_ERRORS) as exc:
            log.warning("setup wizard: cannot show the result: %s", type(exc).__name__)

    async def _apply_and_record(
        self, changes: list[Change], actor_id: int, *, before: Any, was_configured: bool
    ) -> None:
        """Apply URL + token through the settings pipeline and store a human result (no secrets)."""
        ok = False
        try:
            result = await self.settings.apply(changes, source="wizard", actor_id=actor_id)
        except (SettingsError, *_DB_ERRORS) as exc:
            text = _T["rw_fail"].format(reason=_T["db"] if not isinstance(exc, SettingsError) else str(exc))
            if was_configured:
                text += "\n" + _T["rw_kept"]
        else:
            ok, text = await self._panel_outcome(result, before, was_configured)
        state = await self._state()
        state.panel_check = {"ok": ok, "text": _clip(mask(text)), "at": _at()}
        await self._save(state)

    async def _panel_outcome(
        self, result: ApplyResult, before: Any, was_configured: bool
    ) -> tuple[bool, str]:
        comp = self.panel()
        report = comp.last_report if comp is not None and comp.last_report is not before else None
        if result.rejected:
            reason = (
                result.rejected.get("REMNAWAVE_TOKEN")
                or result.rejected.get("REMNAWAVE_URL")
                or next(iter(result.rejected.values()))
            )
            lines = [_T["rw_fail"].format(reason=reason)]
            if report is not None and report.items:
                lines.append(report.render())
            if was_configured:
                lines.append(_T["rw_kept"])
            return False, "\n".join(lines)
        if not result.applied and (comp is None or not comp.configured):
            return False, _T["rw_fail"].format(reason=_T["rw_off"])
        if not result.applied and comp is not None:
            try:  # the same URL and token again: re-check the running connection
                async with asyncio.timeout(self._call_budget()):
                    report = await comp.check()
            except (TimeoutError, RemnawaveError) as exc:
                return False, _T["rw_fail"].format(reason=_err_text(exc))
            if report is not None and not report.ok:
                return False, "\n".join([_T["rw_fail"].format(reason=report.fatal or "?"), report.render()])
        lines = [_T["rw_ok"]]
        if report is not None and report.items:
            lines.append(report.render())
        return True, "\n".join(lines)

    async def _act_check(self, ctx: ScreenCtx, _arg: Any) -> HandlerResult:
        comp = self.panel()
        if comp is None or not comp.configured:
            return Redirect(SCREEN, S_PANEL, toast=_T["rw_off"])
        ok = False
        try:
            async with asyncio.timeout(self._call_budget()):
                report = await comp.check()
        except (TimeoutError, RemnawaveError) as exc:
            text = _T["rw_fail"].format(reason=_err_text(exc))
        else:
            if report is None:
                text = _T["rw_off"]
            elif report.ok:
                ok, text = True, _T["rw_ok"] + "\n" + report.render()
            else:
                text = _T["rw_fail"].format(reason=report.fatal or "?") + "\n" + report.render()
        state = await self._state()
        state.panel_check = {"ok": ok, "text": _clip(mask(text)), "at": _at()}
        await self._save(state)
        return self._panel_view(state)

    # ------------------------------------------------------------ step: admin chat

    async def _chat_view(self) -> View:
        lines = [self._header(S_CHAT), _T["chat_head"], ""]
        done = False
        if "admin_chat" not in self.components:
            lines.append(_T["chat_none"])
        else:
            report = await self.components.health("admin_chat", limit_s=3.0)
            if report.status is Health.DISABLED:
                lines.append(_T["chat_off"])
            else:
                done = report.status is Health.OK
                lines.append(f"{_health_icon(report.status)} {html.escape(report.summary)}")
            lines += ["", _T["chat_hint"]]
        keyboard: list[list[InlineKeyboardButton]] = []
        if "admin_chat" in self.components:
            keyboard.append(
                [
                    nav_button(_T["chat_connect"], ADMIN_CHAT_SCREEN),
                    nav_button(_T["chat_check"], SCREEN, arg=S_CHAT),
                ]
            )
        keyboard += self._nav(S_CHAT, can_skip=True, done=done)
        return View(text="\n".join(lines), parse_mode="HTML", keyboard=keyboard)

    # ------------------------------------------------------------ step: webhooks

    async def target(self) -> WebhookTarget:
        cfg = self.settings.current()
        try:
            hostname = self._hostname()
        except OSError:
            hostname = ""
        resolves: bool | None = None
        if hostname and _HOSTNAME_RE.match(hostname) and not _CONTAINER_ID_RE.match(hostname):
            try:
                resolves = await self._resolver(hostname)
            except Exception:  # noqa: BLE001 - a DNS check is advisory
                resolves = None
        return webhook_target(
            hostname=hostname,
            public_url=cfg.get("PUBLIC_URL"),
            panel_url=cfg.get("REMNAWAVE_URL"),
            port=self.web_port,
            resolves=resolves,
        )

    async def _hooks_view(self, ctx: ScreenCtx, state: WizardState, *, note: str | None = None) -> View:
        comp = self.panel()
        lines = [self._header(S_HOOKS), _T["wh_head"], ""]
        keyboard: list[list[InlineKeyboardButton]] = []
        if comp is None or not comp.configured:
            lines.append(_T["wh_no_panel"])
            keyboard += self._nav(S_HOOKS, can_skip=True, done=False)
            return View(text="\n".join(lines), parse_mode="HTML", keyboard=keyboard)
        caps = comp.capabilities
        enabled = caps.webhooks_enabled if caps is not None else None
        lines.append(
            _T["wh_panel_on"]
            if enabled
            else _T["wh_panel_off"]
            if enabled is False
            else _T["wh_panel_unknown"]
        )
        if note:
            lines += ["", note]
        mode = state.webhook_mode
        if mode is None:
            lines += ["", _T["wh_ask_b"] if enabled else _T["wh_ask_a"]]
            if enabled:
                keyboard += [
                    [nav_button(_T["wh_yes_b"], ACTIONS, A_MODE_B)],
                    [nav_button(_T["wh_no_a"], ACTIONS, A_MODE_A)],
                ]
            else:
                keyboard += [
                    [nav_button(_T["wh_show_a"], ACTIONS, A_MODE_A)],
                    [nav_button(_T["wh_have_b"], ACTIONS, A_MODE_B)],
                ]
            keyboard.append([nav_button(_T["wh_check"], ACTIONS, A_HOOK_CHECK)])
            keyboard += self._nav(S_HOOKS, can_skip=True, done=False)
            return View(text="\n".join(lines), parse_mode="HTML", keyboard=keyboard)

        target = await self.target()
        secret_value = self.settings.current().get("REMNAWAVE_WEBHOOK_SECRET")
        secret = secret_value if isinstance(secret_value, str) and is_panel_secret(secret_value) else None
        lines += ["", _T["wh_mode_a"] if mode == "A" else _T["wh_mode_b"]]
        if target.url is None:
            lines += ["", target.problem or ""]
            keyboard.append([await self._fix_button(ctx, _T["public_url"], "PUBLIC_URL")])
        elif mode == "B" and secret is None:
            lines += ["", _T["wh_secret_missing"]]
            keyboard.append([nav_button(_T["wh_secret_paste"], ACTIONS, A_MODE_B)])
        else:
            where = _T["wh_target_docker"] if target.source == "docker" else _T["wh_target_public"]
            lines += ["", _T["wh_target"].format(url=html.escape(target.url)) + " " + where]
            lines += list(target.notes)
            snippet = build_env_snippet(our_url=target.url, secret=secret if mode == "A" else None)
            lines += ["", _T["wh_run"], snippet_block(snippet), _T["wh_warn_commas"], _T["wh_warn_comments"]]
            lines.append(_T["wh_warn_shared_a"] if mode == "A" else _T["wh_warn_shared_b"])
        for check in (state.webhook_check, state.line_check):
            if check is not None:
                lines += ["", _T["wh_last"].format(at=html.escape(check["at"])), html.escape(check["text"])]
        keyboard += [
            [nav_button(_T["wh_check"], ACTIONS, A_HOOK_CHECK), nav_button(_T["wh_line"], ACTIONS, A_LINE)],
            [nav_button(_T["wh_mode_reset"], ACTIONS, A_MODE_RESET)],
        ]
        keyboard += self._nav(S_HOOKS, can_skip=True, done=bool(enabled))
        return View(text="\n".join(lines), parse_mode="HTML", keyboard=keyboard)

    async def _fix_button(self, ctx: ScreenCtx, label: str, key: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            text=label, callback_data=await ctx.callback(SETTINGS_SCREEN_KEY, arg=key)
        )

    async def _act_mode_a(self, ctx: ScreenCtx, _arg: Any) -> HandlerResult:
        state = await self._state()
        state.webhook_mode = "A"
        note: str | None = None
        current = self.settings.current().get("REMNAWAVE_WEBHOOK_SECRET")
        if not (isinstance(current, str) and is_panel_secret(current)):
            secret = generate_webhook_secret()
            register_secret(secret)
            note = await self._save_secret(ctx, secret)
        await self._save(state)
        return await self._hooks_view(ctx, state, note=note)

    async def _save_secret(self, ctx: ScreenCtx, secret: str) -> str | None:
        """Store the webhook secret through the settings pipeline; ``None`` on success, else a note."""
        task = self._spawn(
            self.settings.apply(
                [Change("REMNAWAVE_WEBHOOK_SECRET", secret)], source="wizard", actor_id=ctx.user.user_id
            )
        )
        done, _ = await asyncio.wait({task}, timeout=self._wait_budget())
        if task not in done:
            self._spawn(self._show_later(task, ctx.user, ctx.chat_id, S_HOOKS))
            return _T["applying"]
        try:
            result: ApplyResult = task.result()
        except (SettingsError, *_DB_ERRORS):
            return _T["wh_saved_fail"].format(reason=_T["db"])
        if result.rejected:
            reason = html.escape(mask(next(iter(result.rejected.values()))))
            return _T["wh_saved_fail"].format(reason=reason)
        return None

    async def _act_mode_b(self, ctx: ScreenCtx, _arg: Any) -> HandlerResult:
        state = await self._state()
        if state.webhook_mode != "B":
            state.webhook_mode = "B"
            await self._save(state)
        return await ctx.start_form(FORM_SECRET)

    async def _secret_done(self, ctx: ScreenCtx, data: dict[str, Any]) -> HandlerResult:
        secret = data.get("secret")
        if not isinstance(secret, str) or not is_panel_secret(secret):
            return Redirect(SCREEN, S_HOOKS)
        note = await self._save_secret(ctx, secret)
        state = await self._state()
        return await self._hooks_view(ctx, state, note=note)

    async def _act_mode_reset(self, ctx: ScreenCtx, _arg: Any) -> HandlerResult:
        state = await self._state()
        state.webhook_mode = None
        state.line_check = None
        await self._save(state)
        return await self._hooks_view(ctx, state)

    async def _act_hook_check(self, ctx: ScreenCtx, _arg: Any) -> HandlerResult:
        comp = self.panel()
        if comp is None or not comp.configured:
            return Redirect(SCREEN, S_HOOKS, toast=_T["rw_off"])
        text = await self._diagnose_hooks(comp)
        state = await self._state()
        state.webhook_check = {"ok": text.startswith("✅"), "text": _clip(text), "at": _at()}
        await self._save(state)
        return await self._hooks_view(ctx, state)

    async def _diagnose_hooks(self, comp: RemnawaveComponent) -> str:
        try:
            async with asyncio.timeout(self._call_budget()):
                caps = await comp.refresh()
        except (TimeoutError, RemnawaveError) as exc:
            return _T["wh_chk_err"].format(error=mask(_err_text(exc)))
        if caps is None or caps.config is None:
            return _T["wh_chk_scope"]
        if not caps.webhooks_enabled:
            return _T["wh_chk_off"]
        activity = await self._activity()
        if activity is None:
            return _T["wh_chk_on"]
        ok_at, bad_at = activity.last_ok_at, activity.last_bad_signature_at
        if bad_at is not None and (ok_at is None or bad_at > ok_at):
            return _T["wh_chk_badsig"]
        if ok_at is not None:
            return _T["wh_chk_seen"].format(at=_at(ok_at))
        return _T["wh_chk_wait"]

    async def _activity(self) -> WebhookActivity | None:
        hook = self.webhook_seen
        if hook is None:
            return None
        try:
            async with asyncio.timeout(3.0):
                return await hook()
        except Exception:
            log.warning("setup wizard: webhook activity check failed", exc_info=True)
            return None

    async def _act_line(self, ctx: ScreenCtx, _arg: Any) -> HandlerResult:
        return await ctx.start_form(FORM_LINE)

    async def _line_done(self, ctx: ScreenCtx, data: dict[str, Any]) -> HandlerResult:
        line = data.get("line")
        target = await self.target()
        problems = check_webhook_url_line(line if isinstance(line, str) else "", target.url)
        if problems:
            text = _T["wh_line_bad"] + "\n" + "\n".join(f"• {p}" for p in problems)
        else:
            text = _T["wh_line_ok"]
        state = await self._state()
        state.line_check = {"ok": not problems, "text": _clip(text), "at": _at()}
        await self._save(state)
        return await self._hooks_view(ctx, state)

    # ------------------------------------------------------------ step: readiness

    async def checklist(self) -> list[ReadyItem]:
        """The readiness checklist (no panel calls: cached capabilities and bounded health checks)."""
        items = [ReadyItem("ok", _T["ready_owner"])]
        comp = self.panel()
        if comp is None or not comp.configured:
            items.append(ReadyItem("fail", _T["ready_panel_off"], S_PANEL))
        else:
            report = await self.components.health("remnawave", limit_s=4.0)
            status = _ready_status(report)
            panel_text = _T["ready_panel"].format(summary=report.summary or "подключена")
            items.append(ReadyItem(status, panel_text, S_PANEL))
            caps = comp.capabilities
            if caps is None or not caps.scopes:
                items.append(ReadyItem("warn", _T["ready_scopes_unknown"], S_PANEL))
            elif caps.missing_required:
                missing = ", ".join(caps.missing_required)
                items.append(ReadyItem("fail", _T["ready_scopes_missing"].format(scopes=missing), S_PANEL))
            else:
                items.append(ReadyItem("ok", _T["ready_scopes"]))
            warning = comp.token_warning()
            if warning is not None:
                items.append(ReadyItem("fail" if warning.level == 0 else "warn", warning.message_ru, S_PANEL))
            hooks = caps.webhooks_enabled if caps is not None else None
            if hooks:
                items.append(ReadyItem("ok", _T["ready_hooks_on"]))
            elif hooks is False:
                items.append(ReadyItem("warn", _T["ready_hooks_off"], S_HOOKS))
            else:
                items.append(ReadyItem("warn", _T["ready_hooks_unknown"], S_HOOKS))
            if hooks and self.webhook_seen is not None:
                activity = await self._activity()
                seen = activity is not None and activity.last_ok_at is not None
                items.append(
                    ReadyItem("ok", _T["ready_hooks_seen"])
                    if seen
                    else ReadyItem("warn", _T["ready_hooks_wait"], S_HOOKS)
                )
        if "admin_chat" in self.components:
            report = await self.components.health("admin_chat", limit_s=3.0)
            if report.status is Health.DISABLED:
                items.append(ReadyItem("warn", _T["ready_chat_off"], S_CHAT))
            else:
                chat_text = _T["ready_chat"].format(summary=report.summary or "подключена")
                items.append(ReadyItem(_ready_status(report), chat_text, S_CHAT))
        maint = self.maintenance
        if maint is not None and maint.active:
            items.append(ReadyItem("warn", _T["ready_maint"].format(text=maint.state.text()), None))
        return items

    async def _ready_view(self) -> View:
        items = await self.checklist()
        ok = sum(1 for i in items if i.status == "ok")
        lines = [self._header(S_READY), _T["ready_head"].format(ok=ok, total=len(items)), ""]
        lines += [f"{i.icon} {html.escape(mask(i.text), quote=False)}" for i in items]
        keyboard: list[list[InlineKeyboardButton]] = []
        labels = {S_PANEL: _T["step_panel"], S_CHAT: _T["step_chat"], S_HOOKS: _T["step_hooks"]}
        fixes = sorted({i.fix_step for i in items if i.status != "ok" and i.fix_step}, key=STEPS.index)
        keyboard += [
            [nav_button(_T["ready_fix"].format(label=labels[s]), SCREEN, arg=s)] for s in fixes if s in labels
        ]
        keyboard.append([nav_button(_T["finish"], ACTIONS, A_FINISH)])
        keyboard += self._nav(S_READY, can_skip=False, done=True)
        return View(text="\n".join(lines), parse_mode="HTML", keyboard=keyboard)

    async def _act_skip(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        if not isinstance(arg, str) or arg not in STEPS[:-1]:
            return Toast(_T["stale"])
        state = await self._state()
        if arg not in state.skipped:
            state.skipped.append(arg)
        nxt = STEPS[STEPS.index(arg) + 1]
        state.step = nxt
        await self._save(state)
        return Redirect(SCREEN, nxt, toast=_T["skipped"])

    async def _act_finish(self, ctx: ScreenCtx, _arg: Any) -> HandlerResult:
        state = await self._state()
        state.step = S_DONE
        state.finished_at = clock.now().isoformat()
        if not await self._save(state):
            return Toast(_T["db"], alert=True)
        log.info("setup wizard finished by user %s", ctx.user.user_id)
        return Redirect(SCREEN, S_DONE)

    async def _done_view(self) -> View:
        items = await self.checklist()
        ok = sum(1 for i in items if i.status == "ok")
        return View(
            text=_T["done"].format(ok=ok, total=len(items)),
            parse_mode="HTML",
            keyboard=[
                [nav_button(_T["status"], STATUS_SCREEN), nav_button(_T["settings"], "settings_root")],
                [nav_button(_T["menu"], HOME_SCREEN)],
            ],
        )


def _health_icon(status: Health) -> str:
    return {
        Health.OK: "✅",
        Health.DEGRADED: "⚠️",
        Health.DOWN: "🔴",
        Health.DISABLED: "⚪",
        Health.UNKNOWN: "❔",
    }[status]


def _ready_status(report: HealthReport) -> Literal["ok", "warn", "fail"]:
    if report.status is Health.OK:
        return "ok"
    if report.status is Health.DOWN:
        return "fail"
    return "warn"


def _err_text(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return "панель не ответила вовремя"
    if isinstance(exc, RemnawaveError):
        hint = getattr(exc, "hint_ru", None)
        base = exc.message or exc.kind.value
        return f"{base}. {hint}" if hint else base
    return type(exc).__name__


class _Deps(Protocol):
    @property
    def db(self) -> Any: ...

    @property
    def settings(self) -> SettingsService: ...

    @property
    def components(self) -> ComponentRegistry: ...


def setup(router: ScreenRouter, deps: _Deps) -> Router:
    """Module entry point for ``svbg.app`` (``setup(router, deps)``): the wizard screens and ``/setup``.

    Optional ``deps.maintenance`` (``MaintenanceService``) and ``deps.webhook_seen`` (async callable returning
    :class:`WebhookActivity`) enrich the checklist.
    """
    wizard = SetupWizard(
        router,
        db=deps.db,
        settings=deps.settings,
        components=deps.components,
        maintenance=getattr(deps, "maintenance", None),
        webhook_seen=getattr(deps, "webhook_seen", None),
    )
    wizard.install()
    on_stop = getattr(deps, "on_stop", None)
    if callable(on_stop):
        on_stop("setup wizard", wizard.drain)
    return wizard.aiogram_router()
