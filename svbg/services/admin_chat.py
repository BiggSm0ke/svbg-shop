"""Admin supergroup with topics (07 §2.4.2): routing by kind, group rate limit, priorities, digests,
self-healing topics, cards edited in place. Implements :class:`~svbg.core.component.Component`
(``name="admin_chat"``, setting ``ADMIN_CHAT_ID``).

How a notification travels::

    post(kind, text, …)  ──enqueue (no I/O)──▶  priority queue  ──pump (one task)──▶  Notifier  ──▶ Telegram
                                                 + per-kind digests
                                                 + coalescing of edits/cards

* **Queue.** :meth:`AdminChatService.post` only enqueues and returns at once (``wait=True`` waits for the
  delivery and returns a :class:`PostResult`). One pump task sends items in ``(priority, arrival)`` order and
  never more than ``group_limit`` (20) per ``group_window`` (60 s) — the same budget as the notifier's group
  gate, so the notifier never has to delay us and Telegram never answers 429. Errors and payments therefore
  overtake new users and trials. Lower priorities may only use part of the window (``LOW`` 70 %, ``NORMAL``
  80 %, ``HIGH`` 90 %), so a burst of new users never leaves an error report waiting for a whole minute; while
  they wait, low-priority items fold into digests.
* **Digests.** Low-priority items (new users, trials) are accumulated per kind; when the pump gets to them it
  sends one message if there is one, or a digest «👤 +37 новых пользователей за 5 мин» with the first lines of
  each. Nothing is lost: every notification is either sent or counted in a digest. When the queue overflows
  (``max_queue``), non-critical items are folded into digests too.
* **Topics.** The bot creates the topics itself (``createForumTopic`` with an icon from
  ``getForumTopicIconStickers`` when the set has a matching emoji, otherwise a colour and the emoji in the
  name) and stores ``message_thread_id`` in ``admin_topics``. A disabled topic sends to «⚙️ Система» with a
  header line or drops the message (per topic). A deleted topic (``message thread not found``) is
  recreated, the id updated and the message re-sent.
* **Failures.** *Transient* ones (no network, Telegram 5xx/429, the notifier overloaded or stopping, the bot
  being restarted) say nothing about the chat: the item goes back to its place in the queue and the whole
  pump pauses with exponential backoff (``retry_base`` … ``retry_max``, at least Telegram's ``retry_after``),
  so an outage only delays the queue and drains nothing into the void; ``health()`` turns ``DEGRADED`` after
  ``max_attempts`` of them in a row. Only an item that keeps failing for ``give_up_after`` (1 h) is logged
  and dropped. *Permanent* chat errors (kicked, no rights, not a forum, chat not found, migrated) are retried
  with exponential backoff; after ``max_attempts`` (5) failures — or once the chat has failed 5 times in a row
  — the message goes to the owners' private chats with a note that the admin chat is unavailable, and
  ``health()`` turns ``DOWN`` (a transient failure of that DM puts the item back too). While down, one
  delivery per ``recheck_interval`` tries the group again. Our own delivery errors are only logged — never
  reported to the error hub, which would route them back into the same broken chat.
* **Cards** (``card_ref``): the first post sends a message and remembers it in ``admin_cards``; later posts
  with the same ``(kind, card_ref)`` edit it in place (an unchanged card is not edited at all).

With no ``ADMIN_CHAT_ID`` everything goes to the owners' private chats (header line with the topic name).
The clock and ``sleep`` are injectable so tests can run minutes of traffic in virtual time.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import heapq
import html
import itertools
import json
import logging
import math
import re
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol, TypeVar

import sqlalchemy as sa
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramMigrateToChat,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.methods import (
    CloseForumTopic,
    CreateForumTopic,
    EditForumTopic,
    EditMessageText,
    GetChat,
    GetChatMember,
    GetForumTopicIconStickers,
    ReopenForumTopic,
    SendMessage,
    SendRichMessage,
    TelegramMethod,
)
from aiogram.types import (
    ChatMemberAdministrator,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
    MessageEntity,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert

from svbg.core import clock as core_clock
from svbg.core.component import HealthReport, ProbeError, fix_screen
from svbg.core.errors.report import format_duration_ru, plural_ru, utf16_len
from svbg.core.log import mask
from svbg.services.tables import TOPIC_FALLBACKS, admin_cards, admin_topics
from svbg.tg.notifier import TRANSPORT_ERRORS, NotifierError, Priority
from svbg.tg.report import Report, RichGate, banner_media
from svbg.tg.runner import BotUnavailableError

if TYPE_CHECKING:
    from aiogram import Bot

    from svbg.db.engine import Database
    from svbg.tg.notifier import Notifier

__all__ = [
    "CORE_TOPICS",
    "K_BACKUPS",
    "K_ERRORS",
    "K_NEW_USERS",
    "K_PANEL",
    "K_PAYMENTS",
    "K_REPORTS",
    "K_SUBSCRIPTIONS",
    "K_SYSTEM",
    "K_TICKETS",
    "K_TRIALS",
    "MAX_TEXT",
    "SCREEN",
    "AdminChatService",
    "ChatCheck",
    "EnsureReport",
    "Fallback",
    "PostResult",
    "TopicDef",
    "TopicState",
]

log = logging.getLogger("svbg.services.admin_chat")

T = TypeVar("T")

Fallback = Literal["system", "drop"]
Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]
Owners = Callable[[], Awaitable[frozenset[int]]]
Keyboard = Sequence[Sequence[InlineKeyboardButton]] | InlineKeyboardMarkup

SCREEN: Final = "achat"  # the owner's «Админ-чат» screen (svbg.tg.admin.connect_chat), used in fix_action
MAX_TEXT: Final = 4096  # Telegram limit, UTF-16 code units

K_PAYMENTS: Final = "payments"
K_TRIALS: Final = "trials"
K_NEW_USERS: Final = "new_users"
K_SUBSCRIPTIONS: Final = "subscriptions"
K_ERRORS: Final = "errors"
K_REPORTS: Final = "reports"
K_BACKUPS: Final = "backups"
K_PANEL: Final = "panel"
K_SYSTEM: Final = "system"
K_TICKETS: Final = "tickets"

# Telegram's palette for topics without a custom icon.
_BLUE, _YELLOW, _VIOLET, _GREEN, _ROSE, _RED = 7322096, 16766590, 13338331, 9367192, 16749490, 16478047

_KIND_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_CONTROL_RE: Final = re.compile(r"[\x00-\x1f\x7f]")
_TAG_RE: Final = re.compile(r"<[^>]*>")
_DB_ERRORS: Final[tuple[type[BaseException], ...]] = (sa.exc.SQLAlchemyError, OSError, TimeoutError)
_SEND_ERRORS: Final[tuple[type[BaseException], ...]] = (
    TelegramAPIError,
    NotifierError,
    BotUnavailableError,
    TimeoutError,
    *TRANSPORT_ERRORS,
)
_DIRECT_TIMEOUT: Final = 10.0
_MIN_WAIT: Final = 0.001  # s
_CARD_CACHE: Final = 1024
_MOVED_CACHE: Final = 1024
_LAST_ERROR_MAX: Final = 300

# Bot API error descriptions (lowercase substrings).
_THREAD_GONE: Final = (
    "message thread not found",
    "thread not found",
    "topic_deleted",
    "topic not found",
    "topic_id_invalid",
)
_MESSAGE_GONE: Final = (
    "message to edit not found",
    "message_id_invalid",
    "message can't be edited",
    "message not found",
    "there is no text in the message to edit",
)
_NOT_MODIFIED: Final = "message is not modified"

# Owner-facing texts (Russian) in one place.
_TXT: Final[dict[str, str]] = {
    "not_supergroup": (
        "Это не супергруппа. Создайте группу, включите в её настройках «Темы» — Telegram сам сделает её "
        "супергруппой — и подключите заново."
    ),
    "not_forum": (
        "В группе выключены темы. Откройте настройки группы → «Темы», включите их и нажмите "
        "«Проверить снова»."
    ),
    "bad_id": "Нужен ID супергруппы — отрицательное число, обычно начинается с -100.",
    "chat_not_found": (
        "Бот не видит эту группу. Добавьте бота в группу администратором и проверьте ID "
        "(у супергрупп он начинается с -100)."
    ),
    "kicked": "Бота удалили из группы. Добавьте его снова администратором и нажмите «Проверить снова».",
    "not_admin": (
        "Бот не администратор группы. Назначьте его админом с правами «Управление темами», "
        "«Закрепление сообщений» и «Удаление сообщений», затем нажмите «Проверить снова»."
    ),
    "no_rights": "У бота нет прав: {rights}. Дайте их в настройках администраторов группы и нажмите "
    "«Проверить снова».",
    "public": (
        "Группа публичная (@{username}) — её сообщения сможет читать кто угодно, а любой вступивший увидит "
        "оплаты, ошибки и события панели. Сделайте группу частной: настройки группы → «Тип группы» → "
        "«Частная», затем нажмите «Проверить снова»."
    ),
    "no_bot": "Бот ещё не подключён к Telegram — проверьте BOT_TOKEN в «Состоянии» и повторите.",
    "tg_down": "Telegram сейчас не отвечает. Повторите через минуту.",
    "migrated": "Группа получила новый ID ({chat_id}) — подключите её заново.",
    "dm_header_down": "⚠️ Админ-чат недоступен: {reason}. Сообщение из темы «{topic}»:",
    "digest": "{icon} +{n} {noun} за {period}",
    "digest_more": "…и ещё {n}",
    "minute": "минуту",
    "health_off": "Не подключён — уведомления идут в личку владельцам",
    "health_down": "Админ-чат недоступен ({reason}) — уведомления идут в личку владельцам",
    "health_missing": "Не созданы темы: {topics}",
    "health_ok": "Подключён, тем: {topics}; в очереди: {pending}",
    "health_waiting": "Telegram временно недоступен ({reason}) — уведомления ждут в очереди: {pending}",
    "f_rights": "у бота нет прав в группе (нужен администратор с управлением темами)",
    "f_chat": "группа не найдена: бота удалили или ID неверный",
    "f_forum": "в группе выключены темы",
    "f_closed": "тема закрыта",
    "f_rejected": "Telegram отклонил сообщение ({desc})",
    "f_kicked": "бота удалили из группы или запретили ему писать",
    "f_flood": "Telegram ограничил частоту сообщений",
    "f_network": "нет связи с Telegram",
    "f_stopped": "отправка остановлена",
    "f_no_bot": "бот не подключён к Telegram",
    "f_api": "ошибка Telegram ({name})",
    "f_db": "база данных недоступна — темы не создаются, чтобы не появились дубликаты",
    "f_owners": "база данных недоступна — не удалось узнать владельцев",
    "f_gone_again": "тема удалена и не пересоздаётся",
    "recreated": "тема была удалена — создана заново",
}

_FATAL_FOR_ALL_TOPICS: Final = frozenset({_TXT["f_rights"], _TXT["f_forum"], _TXT["f_chat"]})

# Share of the group window a priority may NOT fill: headroom kept for more urgent items.
_HEADROOM: Final[dict[Priority, float]] = {
    Priority.CRITICAL: 0.0,
    Priority.HIGH: 0.1,
    Priority.NORMAL: 0.2,
    Priority.LOW: 0.3,
}
_TRANSIENT_ERRORS: Final[tuple[type[BaseException], ...]] = (
    TelegramRetryAfter,
    TelegramServerError,
    TelegramNetworkError,
    TimeoutError,
    NotifierError,
    BotUnavailableError,
    *TRANSPORT_ERRORS,
)

_RIGHTS_REQUIRED: Final = (("can_manage_topics", "управление темами"),)
_RIGHTS_OPTIONAL: Final = (
    ("can_pin_messages", "закрепление сообщений"),
    ("can_delete_messages", "удаление сообщений"),
)


# ---------------------------------------------------------------------------------------------- topics


@dataclass(frozen=True, slots=True)
class TopicDef:
    """A topic kind: core topics are built in, modules register theirs (X11)."""

    kind: str
    title: str
    icon: str
    priority: Priority = Priority.NORMAL
    icon_alternatives: tuple[str, ...] = ()  # emojis of the forum icon set that fit, best first
    icon_color: int = _BLUE
    noun: tuple[str, str, str] = ("уведомление", "уведомления", "уведомлений")  # digest: 1 / 2–4 / 5+
    default_enabled: bool = True
    default_fallback: Fallback = "system"
    owner_module: str | None = None  # None = core

    def __post_init__(self) -> None:
        if not _KIND_RE.match(self.kind):
            raise ValueError(f"invalid topic kind {self.kind!r}: expected [a-z][a-z0-9_]{{0,31}}")
        if not self.title.strip() or len(self.title) > 100:
            raise ValueError("topic title must be 1..100 characters")
        if self.default_fallback not in TOPIC_FALLBACKS:
            raise ValueError(f"fallback must be one of {TOPIC_FALLBACKS}")

    @property
    def label(self) -> str:
        return f"{self.icon} {self.title}"


CORE_TOPICS: Final[tuple[TopicDef, ...]] = (
    TopicDef(K_PAYMENTS, "Оплаты и пополнения", "💳", Priority.HIGH, ("💳", "💰", "💸", "💎"), _GREEN,
             ("оплата", "оплаты", "оплат")),
    TopicDef(K_TRIALS, "Триалы", "🎁", Priority.LOW, ("🎁", "🎉", "🎟"), _ROSE,
             ("триал", "триала", "триалов")),
    TopicDef(K_NEW_USERS, "Новые пользователи", "👤", Priority.LOW, ("👤", "👋", "👀"), _BLUE,
             ("новый пользователь", "новых пользователя", "новых пользователей")),
    TopicDef(K_SUBSCRIPTIONS, "Подписки", "📦", Priority.NORMAL, ("📦", "🗂", "📝"), _VIOLET,
             ("событие подписок", "события подписок", "событий подписок")),
    TopicDef(K_ERRORS, "Ошибки", "🚨", Priority.CRITICAL, ("🚨", "❗", "‼", "🔥"), _RED,
             ("ошибка", "ошибки", "ошибок")),
    TopicDef(K_REPORTS, "Отчёты", "📊", Priority.NORMAL, ("📊", "📈", "📰"), _YELLOW,
             ("отчёт", "отчёта", "отчётов")),
    TopicDef(K_BACKUPS, "Бэкапы", "💾", Priority.NORMAL, ("💾", "🗂", "🧳"), _BLUE,
             ("бэкап", "бэкапа", "бэкапов")),
    TopicDef(K_PANEL, "Панель и ноды", "🖥", Priority.HIGH, ("🖥", "💻", "🤖"), _VIOLET,
             ("событие панели", "события панели", "событий панели")),
    TopicDef(K_SYSTEM, "Система", "⚙️", Priority.HIGH, ("⚙", "🔎", "💡"), _YELLOW),
    TopicDef(K_TICKETS, "Тикеты", "🎫", Priority.HIGH, ("🎫", "🎟", "💬"), _GREEN,
             ("тикет", "тикета", "тикетов"), default_enabled=False),
)  # fmt: skip


@dataclass(slots=True)
class TopicState:
    """Live state of one topic (a row of ``admin_topics``)."""

    kind: str
    title: str
    icon: str | None
    enabled: bool
    fallback: str = "system"
    chat_id: int | None = None
    thread_id: int | None = None
    icon_emoji_id: str | None = None
    last_error: str | None = None
    recreated_at: datetime | None = None

    def thread_in(self, chat_id: int) -> int | None:
        return self.thread_id if self.chat_id == chat_id else None


@dataclass(frozen=True, slots=True)
class PostResult:
    """Where a notification ended up."""

    kind: str
    chat_id: int | None = None
    message_id: int | None = None
    thread_id: int | None = None
    digest: int = 0  # > 1: delivered as part of a digest of this many notifications
    dm: Mapping[int, int] = field(default_factory=dict)  # owner chat id → message id (private fallback)
    dropped: bool = False  # the topic is disabled with fallback «не отправлять»

    @property
    def delivered(self) -> bool:
        return self.message_id is not None or bool(self.dm)


@dataclass(frozen=True, slots=True)
class ChatCheck:
    """A chat that passed the probe; ``warnings`` lists missing optional rights (Russian)."""

    chat_id: int
    title: str
    warnings: tuple[str, ...] = ()


@dataclass(slots=True)
class EnsureReport:
    created: list[str] = field(default_factory=list)
    existing: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.failed


# ---------------------------------------------------------------------------------------------- internals


class _Failure(Exception):
    """A delivery attempt failed; ``human`` is a short Russian reason (no secrets).

    ``transient``: Telegram / the bot / the notifier is unavailable for a while — nothing is wrong with the
    chat, the item waits in the queue. ``local``: only this item is affected (its topic cannot be created
    while the database is down) — retried, eventually sent to the owners, not counted against the chat.
    """

    def __init__(
        self, human: str, *, transient: bool = False, local: bool = False, retry_after: float = 0.0
    ) -> None:
        super().__init__(human)
        self.human = human
        self.transient = transient
        self.local = local
        self.retry_after = retry_after


class _ThreadGone(_Failure):
    pass


class _MessageGone(_Failure):
    pass


class _RichRejected(_Failure):
    """Telegram refused a rich message (old server, a block it does not take, no media rights).

    ``unsupported``: the server has no such method at all, rich is off for every chat."""

    def __init__(self, human: str, *, unsupported: bool = False) -> None:
        super().__init__(human)
        self.unsupported = unsupported


class _NotModified(_Failure):
    pass


class _BotSource(Protocol):
    def get(self) -> Bot | None: ...


@dataclass(eq=False, slots=True)
class _Item:
    seq: int
    kind: str
    priority: Priority
    text: str
    html: bool = False
    entities: list[MessageEntity] | None = None
    markup: InlineKeyboardMarkup | None = None
    silent: bool = False
    card: tuple[str, str] | None = None
    edit: tuple[int, int] | None = None  # (chat_id, message_id) of a message to edit
    resend_kind: str | None = None  # edit target gone → send a new message into this topic
    created: float = 0.0
    futures: list[asyncio.Future[PostResult | None]] = field(default_factory=list)
    detached: bool = False  # someone posted it without waiting: never abandoned
    attempts: int = 0
    key: str | None = None  # coalescing key (edits, cards)
    digest: int = 0
    to_dm: bool = False  # the group failed permanently for it: deliver to the owners' DMs
    dm_reason: str | None = None
    stuck_since: float | None = None  # first transient failure of this item
    report: Report | None = None  # sent as a rich message when the chat takes them, else ``text`` (its HTML)

    def abandoned(self) -> bool:
        return not self.detached and bool(self.futures) and all(f.done() for f in self.futures)


@dataclass(eq=False, slots=True)
class _Digest:
    kind: str
    priority: Priority
    seq: int
    first_ts: float
    first: _Item
    count: int = 1
    lines: list[str] = field(default_factory=list)
    futures: list[asyncio.Future[PostResult | None]] = field(default_factory=list)


@dataclass(slots=True)
class _Card:
    chat_id: int | None
    msg_id: int | None
    thread_id: int | None
    digest: str | None


class _Window:
    """Exact sliding window: at most ``limit`` events in any ``period`` seconds."""

    def __init__(self, limit: int, period: float) -> None:
        self.limit = limit
        self.period = period
        self.stamps: deque[float] = deque()

    def delay(self, now: float, cap: int | None = None) -> float:
        """Seconds until fewer than ``cap`` (default ``limit``) events are in the window."""
        cap = self.limit if cap is None else max(1, min(cap, self.limit))
        while self.stamps and now - self.stamps[0] >= self.period:
            self.stamps.popleft()
        n = len(self.stamps)
        if n < cap:
            return 0.0
        # The (n - cap + 1)-th oldest stamp must expire. Float rounding can make ``stamp + period - now``
        # come out as 0.0 here; never report "free" while the window is full.
        return max(self.stamps[n - cap] + self.period - now, _MIN_WAIT)

    def take(self, now: float) -> None:
        self.stamps.append(now)

    def give_back(self) -> None:
        """Undo the last :meth:`take`: the request never reached the chat."""
        if self.stamps:
            self.stamps.pop()


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


def _norm_emoji(value: str) -> str:
    return value.replace("️", "").strip()


def _plain(item: _Item) -> str:
    text = html.unescape(_TAG_RE.sub("", item.text)) if item.html else item.text
    first = text.strip().split("\n", 1)[0].strip()
    return first if len(first) <= 120 else first[:119] + "…"


def _markup(buttons: Keyboard | None) -> InlineKeyboardMarkup | None:
    if buttons is None:
        return None
    if isinstance(buttons, InlineKeyboardMarkup):
        return buttons if buttons.inline_keyboard else None
    rows = [list(row) for row in buttons if row]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def _with_header(
    text: str, html_mode: bool, entities: list[MessageEntity] | None, header: str
) -> tuple[str, list[MessageEntity] | None]:
    """``header`` (bold) on its own line above the text; the original text if the result is too long."""
    if html_mode:
        out = f"<b>{_esc(header)}</b>\n{text}"
        return (out, None) if utf16_len(out) <= MAX_TEXT else (text, None)
    head = header + "\n"
    out = head + text
    if utf16_len(out) > MAX_TEXT:
        return text, entities
    shift = utf16_len(head)
    shifted = [e.model_copy(update={"offset": e.offset + shift}) for e in entities or []]
    return out, [MessageEntity(type="bold", offset=0, length=utf16_len(header)), *shifted]


def _content_digest(item: _Item) -> str:
    markup = item.markup.model_dump(mode="json", exclude_none=True) if item.markup else None
    ents = [e.model_dump(mode="json", exclude_none=True) for e in item.entities or []]
    raw = json.dumps([item.text, item.html, ents, markup], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _transient_reason(exc: BaseException) -> _Failure:
    if isinstance(exc, TelegramRetryAfter):
        return _Failure(_TXT["f_flood"], transient=True, retry_after=float(exc.retry_after))
    if isinstance(exc, NotifierError):
        return _Failure(_TXT["f_stopped"], transient=True)
    if isinstance(exc, BotUnavailableError):
        return _Failure(_TXT["f_no_bot"], transient=True)
    return _Failure(_TXT["f_network"], transient=True)


def _human_bad_request(desc: str) -> str:
    d = desc.lower()
    if "chat not found" in d or "chat_id_invalid" in d:
        return _TXT["f_chat"]
    if any(s in d for s in ("not enough rights", "have no rights", "chat_admin_required", "administrator")):
        return _TXT["f_rights"]
    if "not a forum" in d or ("forum" in d and "disabled" in d):
        return _TXT["f_forum"]
    if "topic_closed" in d:
        return _TXT["f_closed"]
    short = mask(desc)[:120]
    return _TXT["f_rejected"].format(desc=short)


# ---------------------------------------------------------------------------------------------- service


class AdminChatService:
    """See the module docstring. One instance per process; use from the event loop only."""

    name = "admin_chat"

    def __init__(
        self,
        db: Database,
        notifier: Notifier,
        holder: _BotSource,
        *,
        owners: Owners,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
        group_limit: int | None = None,
        group_window: float | None = None,
        max_attempts: int = 5,
        retry_base: float = 1.0,
        retry_max: float = 30.0,
        recheck_interval: float = 60.0,
        max_queue: int = 2000,
        digest_lines: int = 25,
        give_up_after: float = 3600.0,
        topics: Iterable[TopicDef] = CORE_TOPICS,
    ) -> None:
        if max_attempts < 1 or max_queue < 1 or digest_lines < 1:
            raise ValueError("max_attempts, max_queue and digest_lines must be >= 1")
        if give_up_after <= 0:
            raise ValueError("give_up_after must be > 0")
        self._db = db
        self._notifier = notifier
        self._holder = holder
        self._owners = owners
        self._clock = clock
        self._sleep = sleep
        limits = notifier.limits
        self._window = _Window(group_limit or limits.group_limit, group_window or limits.group_window)
        self._max_attempts = max_attempts
        self._retry_base = retry_base
        self._retry_max = retry_max
        self._recheck_interval = recheck_interval
        self._max_queue = max_queue
        self._digest_lines = digest_lines
        self._give_up_after = give_up_after

        self._defs: dict[str, TopicDef] = {}
        for d in topics:
            self.register_topic(d)
        if K_SYSTEM not in self._defs:
            raise ValueError("the «system» topic is required (fallback of disabled topics)")
        self._states: dict[str, TopicState] = {}
        self._loaded = False
        self._chat_id: int | None = None
        self._icons: dict[str, str] | None = None

        self._seq = itertools.count()
        self._heap: list[tuple[int, int, _Item]] = []
        self._digests: dict[tuple[str, int], _Digest] = {}
        self._by_key: dict[str, _Item] = {}
        self._wake = asyncio.Event()
        self._pump: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._topic_lock = asyncio.Lock()
        self._busy = False
        self._closed = False

        self._rich = RichGate(clock=clock)
        self._cards: OrderedDict[tuple[str, str], _Card] = OrderedDict()
        self._moved: OrderedDict[tuple[int, int], tuple[int, int]] = OrderedDict()
        self._fail_streak = 0
        self._last_error: str | None = None
        self._last_group_try = -math.inf
        self._transient_streak = 0  # transient failures in a row (any chat): the pump pauses meanwhile
        self._last_transient: str | None = None
        self._pause_until = -math.inf
        self.stats: dict[str, int] = {
            "posted": 0,
            "sent": 0,
            "edited": 0,
            "digests": 0,
            "digested": 0,
            "dropped": 0,
            "recreated": 0,
            "failures": 0,
            "dm": 0,
            "undelivered": 0,
            "requeued": 0,
        }

    # ------------------------------------------------------------------ configuration

    @property
    def chat_id(self) -> int | None:
        return self._chat_id

    @property
    def configured(self) -> bool:
        return self._chat_id is not None

    @property
    def down(self) -> bool:
        """The chat failed ``max_attempts`` times in a row (messages go to the owners' DMs)."""
        return self._chat_id is not None and self._fail_streak >= self._max_attempts

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def waiting(self) -> bool:
        """Telegram / the bot was unavailable ``max_attempts`` times in a row; the queue is on hold."""
        return self._transient_streak >= self._max_attempts

    @property
    def pending(self) -> int:
        return len(self._heap) + sum(d.count for d in self._digests.values())

    def register_topic(self, defn: TopicDef) -> None:
        """Add a topic kind (modules: «🛡 Антиабуз», «🌐 Трафик LTE» …); the same def twice is a no-op."""
        known = self._defs.get(defn.kind)
        if known is not None and known != defn:
            raise ValueError(f"topic {defn.kind!r} is already registered")
        self._defs[defn.kind] = defn

    def topic_defs(self) -> list[TopicDef]:
        return list(self._defs.values())

    def topic(self, kind: str) -> TopicDef | None:
        return self._defs.get(kind)

    def state(self, kind: str) -> TopicState:
        st = self._states.get(kind)
        if st is None:
            d = self._defs[kind]
            st = TopicState(
                kind=kind,
                title=d.title,
                icon=d.icon,
                enabled=d.default_enabled,
                fallback=d.default_fallback,
            )
            self._states[kind] = st
        return st

    def set_chat(self, chat_id: int | None) -> bool:
        """Switch the target chat without side effects; returns ``True`` if it changed."""
        if chat_id == self._chat_id:
            return False
        self._chat_id = chat_id
        self._fail_streak = 0
        self._last_error = None
        self._last_group_try = -math.inf
        log.info("admin chat %s", "disabled (owner DMs)" if chat_id is None else f"set to {chat_id}")
        return True

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        """Load topic states and start the pump. Idempotent."""
        if not self._loaded:
            await self._try_load()
        if self._pump is None or self._pump.done():
            self._closed = False
            self._pump = asyncio.create_task(self._run(), name="admin-chat-pump")

    async def stop(self, grace: float = 5.0) -> None:
        """Deliver what is queued for up to ``grace`` seconds, then stop; leftovers are logged."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + grace
        while (self._heap or self._digests or self._busy) and loop.time() < deadline:
            if self._pump is None or self._pump.done():
                break
            await asyncio.sleep(0.05)
        self._closed = True
        left = self.pending
        if left:
            log.warning("admin chat stopped with %d undelivered notification(s)", left)
        tasks = [t for t in (self._pump, *self._tasks) if t is not None and not t.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._pump = None
        for _, _, item in self._heap:
            self._resolve(item.futures, None)
        for acc in self._digests.values():
            self._resolve(acc.futures, None)
        self._heap.clear()
        self._digests.clear()
        self._by_key.clear()

    # ------------------------------------------------------------------ posting

    async def post(
        self,
        kind: str,
        text: str,
        *,
        entities: Sequence[MessageEntity] | None = None,
        html: bool = False,
        buttons: Keyboard | None = None,
        priority: Priority | None = None,
        card_ref: str | None = None,
        silent: bool | None = None,
        wait: bool = False,
        report: Report | None = None,
    ) -> PostResult | None:
        """Queue a notification for topic ``kind``.

        ``entities`` *or* ``html=True`` (Telegram HTML) format the text; ``priority`` defaults to the topic's;
        ``card_ref`` makes it a card edited in place on later posts with the same ``(kind, card_ref)``.
        Returns at once with ``None``; with ``wait=True`` waits for the delivery and returns where the message
        went (``None`` if it could not be delivered at all).
        """
        if entities and html:
            raise ValueError("pass either entities or html=True, not both")
        if not text or not text.strip():
            raise ValueError("notification text is empty")
        if card_ref is not None and (not card_ref or len(card_ref) > 200 or _CONTROL_RE.search(card_ref)):
            raise ValueError("card_ref must be 1..200 characters without control characters")
        defn = self._defs.get(kind)
        if defn is None:
            log.warning("admin chat: unknown topic kind %r, using «system»", kind)
            kind, defn = K_SYSTEM, self._defs[K_SYSTEM]
        prio = defn.priority if priority is None else Priority(priority)
        item = _Item(
            seq=next(self._seq),
            kind=kind,
            priority=prio,
            text=text,
            html=html,
            entities=list(entities) if entities else None,
            markup=_markup(buttons),
            silent=prio >= Priority.LOW if silent is None else silent,
            card=(kind, card_ref) if card_ref is not None else None,
            created=self._clock(),
            key=f"card:{kind}:{card_ref}" if card_ref is not None else None,
            report=report if html and not entities else None,
        )
        self.stats["posted"] += 1
        return await self._submit(item, wait)

    async def post_report(
        self,
        kind: str,
        report: Report,
        *,
        buttons: Keyboard | None = None,
        priority: Priority | None = None,
        card_ref: str | None = None,
        silent: bool | None = None,
        wait: bool = False,
    ) -> PostResult | None:
        """Queue a :class:`~svbg.tg.report.Report`: a rich message (tables, banner) where the chat takes
        them, its HTML text otherwise. Digests and logs quote the HTML text."""
        return await self.post(
            kind,
            report.html(),
            html=True,
            buttons=buttons,
            priority=priority,
            card_ref=card_ref,
            silent=silent,
            wait=wait,
            report=report,
        )

    async def edit(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        entities: Sequence[MessageEntity] | None = None,
        html: bool = False,
        buttons: Keyboard | None = None,
        priority: Priority = Priority.NORMAL,
        resend_kind: str | None = None,
        wait: bool = False,
    ) -> PostResult | None:
        """Queue an edit of a message in the admin chat (newer edits of a waiting one replace it).

        ``resend_kind``: if the message is gone, send the text as a new message into that topic (and edit
        the new message from then on).
        """
        if entities and html:
            raise ValueError("pass either entities or html=True, not both")
        if not text or not text.strip():
            raise ValueError("message text is empty")
        item = _Item(
            seq=next(self._seq),
            kind=resend_kind or K_SYSTEM,
            priority=Priority(priority),
            text=text,
            html=html,
            entities=list(entities) if entities else None,
            markup=_markup(buttons),
            edit=(chat_id, message_id),
            resend_kind=resend_kind,
            created=self._clock(),
            key=f"edit:{chat_id}:{message_id}",
        )
        return await self._submit(item, wait)

    async def call(
        self, method: TelegramMethod[T], chat_id: int, *, priority: Priority = Priority.HIGH
    ) -> T | None:
        """A module's own request to a forum group (support ticket topics: create / copy / rename / close).

        Not queued: runs at once through the notifier (its group gate keeps Telegram's 20/min); in the admin
        chat it also takes a slot of our window, so the pump keeps an exact view of the group budget.
        Telegram errors propagate to the caller; ``None`` = 403 (the bot was kicked).
        """
        if chat_id == self._chat_id:
            self._window.take(self._clock())
        return await self._notifier.call(method, chat_id=chat_id, priority=priority)

    async def _submit(self, item: _Item, wait: bool) -> PostResult | None:
        if self._closed:
            log.warning("admin chat is stopped: notification for %s dropped", item.kind)
            return None
        fut: asyncio.Future[PostResult | None] | None = None
        if wait:
            fut = asyncio.get_running_loop().create_future()
            item.futures.append(fut)
        else:
            item.detached = True
        self._enqueue(item)
        if fut is None:
            return None
        return await fut

    def _enqueue(self, item: _Item) -> None:
        if item.key is not None:
            pending = self._by_key.get(item.key)
            if pending is not None:  # replace the content of the waiting item, keep its place in the queue
                pending.text, pending.html, pending.entities = item.text, item.html, item.entities
                pending.report = item.report
                pending.markup, pending.silent = item.markup, item.silent
                pending.futures.extend(item.futures)
                pending.detached = pending.detached or item.detached
                if item.priority < pending.priority:
                    pending.priority = item.priority
                    heapq.heappush(self._heap, (int(item.priority), pending.seq, pending))
                self._wake.set()
                return
            self._by_key[item.key] = item
        folded = item.key is None and (
            item.priority >= Priority.LOW
            or (item.priority > Priority.CRITICAL and len(self._heap) >= self._max_queue)
        )
        if folded:
            self._fold(item)
        else:
            heapq.heappush(self._heap, (int(item.priority), item.seq, item))
        self._wake.set()

    def _fold(self, item: _Item) -> None:
        key = (item.kind, int(item.priority))
        acc = self._digests.get(key)
        if acc is None:
            self._digests[key] = _Digest(
                kind=item.kind,
                priority=item.priority,
                seq=item.seq,
                first_ts=item.created,
                first=item,
                lines=[_plain(item)],
                futures=list(item.futures),
            )
            return
        acc.count += 1
        if len(acc.lines) < self._digest_lines:
            acc.lines.append(_plain(item))
        acc.futures.extend(item.futures)

    # ------------------------------------------------------------------ pump

    def _peek(self) -> tuple[int, int] | None:
        heap = self._heap
        while heap:
            prio, seq, item = heap[0]
            if item.abandoned() or prio != int(item.priority) or seq != item.seq:
                heapq.heappop(heap)  # cancelled waiter or a stale entry after a priority bump
                if item.abandoned() and item.key is not None and self._by_key.get(item.key) is item:
                    del self._by_key[item.key]
                continue
            break
        best = (heap[0][0], heap[0][1]) if heap else None
        for acc in self._digests.values():
            key = (int(acc.priority), acc.seq)
            if best is None or key < best:
                best = key
        return best

    def _pop(self) -> _Item | None:
        best = self._peek()
        if best is None:
            return None
        if self._heap and (self._heap[0][0], self._heap[0][1]) == best:
            _, _, item = heapq.heappop(self._heap)
            if item.key is not None and self._by_key.get(item.key) is item:
                del self._by_key[item.key]
            return item
        for key, acc in list(self._digests.items()):
            if (int(acc.priority), acc.seq) == best:
                del self._digests[key]
                return self._from_digest(acc)
        return None  # pragma: no cover - best always comes from the heap or a digest

    def _from_digest(self, acc: _Digest) -> _Item:
        if acc.count == 1:
            acc.first.futures = acc.futures
            return acc.first
        defn = self._defs.get(acc.kind) or self._defs[K_SYSTEM]
        elapsed = max(0.0, self._clock() - acc.first_ts)
        period = _TXT["minute"] if elapsed < 60 else format_duration_ru_seconds(elapsed)
        head = _TXT["digest"].format(
            icon=defn.icon, n=acc.count, noun=plural_ru(acc.count, *defn.noun), period=period
        )
        lines = [f"<b>{_esc(head)}</b>", *(f"• {_esc(line)}" for line in acc.lines if line)]
        rest = acc.count - len(acc.lines)
        if rest > 0:
            lines.append(_esc(_TXT["digest_more"].format(n=rest)))
        self.stats["digests"] += 1
        self.stats["digested"] += acc.count
        return _Item(
            seq=acc.seq,
            kind=acc.kind,
            priority=acc.priority,
            text="\n".join(lines),
            html=True,
            silent=acc.first.silent,
            created=acc.first_ts,
            futures=acc.futures,
            detached=True,
            digest=acc.count,
        )

    async def _run(self) -> None:
        while not self._closed:
            best = self._peek()
            if best is None:
                self._wake.clear()
                await self._wake.wait()
                continue
            pause = self._pause_until - self._clock()
            if pause > 0:  # Telegram / the bot is unavailable: everything waits in the queue
                await self._sleep(pause)
                continue
            group = self._group_usable()
            if group:
                delay = self._window.delay(self._clock(), self._cap(best[0]))
                if delay > 0:
                    await self._nap(delay)
                    continue  # re-pick: something more urgent may have arrived meanwhile
            item = self._pop()
            if item is None:
                continue
            self._busy = True
            try:
                await self._deliver(item, group)
            except asyncio.CancelledError:
                self._resolve(item.futures, None)
                raise
            except Exception:  # a bug here must not stop the pump; never reported to the (same) chat
                log.exception("admin chat: delivery of a %s notification failed unexpectedly", item.kind)
                self.stats["undelivered"] += 1
                self._resolve(item.futures, None)
            finally:
                self._busy = False

    def _cap(self, priority: int) -> int:
        """How much of the group window items of ``priority`` may fill (the rest is kept for urgent ones)."""
        limit = self._window.limit
        share = _HEADROOM.get(Priority(priority), _HEADROOM[Priority.LOW])
        return max(1, limit - math.floor(limit * share))

    async def _nap(self, delay: float) -> None:
        """Sleep up to ``delay``, waking early when something is queued (it may be allowed to go now)."""
        self._wake.clear()
        waker = asyncio.ensure_future(self._wake.wait())
        sleeper = asyncio.ensure_future(self._sleep(delay))
        try:
            await asyncio.wait((waker, sleeper), return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (waker, sleeper):
                task.cancel()
            await asyncio.gather(waker, sleeper, return_exceptions=True)

    def _group_usable(self) -> bool:
        if self._chat_id is None:
            return False
        if self._fail_streak < self._max_attempts:
            return True
        return self._clock() - self._last_group_try >= self._recheck_interval  # half-open: try once more

    async def _deliver(self, item: _Item, group: bool) -> None:
        try:
            if item.edit is not None:
                result = await self._deliver_edit(item)
            elif item.to_dm or not group:
                reason = item.dm_reason or (self._last_error if self._chat_id is not None else None)
                result = await self._deliver_dm(item, reason)
            else:
                self._last_group_try = self._clock()
                try:
                    result = await self._deliver_group(item)
                except _Failure as failure:
                    if failure.transient:
                        raise
                    if not self._on_failure(item, failure):
                        return  # retried later
                    result = await self._deliver_dm(item, item.dm_reason)
                else:
                    if self._fail_streak >= self._max_attempts:
                        log.info("admin chat is reachable again")
                    self._fail_streak = 0
                    self._last_error = None
        except _Failure as failure:
            if not failure.transient:
                raise
            self._requeue(item, failure)
            return
        if self._transient_streak:
            log.info("admin chat: Telegram is reachable again; %d notification(s) queued", self.pending)
        self._transient_streak = 0
        self._last_transient = None
        self._resolve(item.futures, result)

    def _on_failure(self, item: _Item, failure: _Failure) -> bool:
        """A permanent failure; ``True``: give up on the group and deliver to the owners' DMs now."""
        if not failure.local:
            self._fail_streak += 1
            self._last_error = failure.human
        self.stats["failures"] += 1
        item.attempts += 1
        log.warning(
            "admin chat: %s notification not delivered (attempt %d): %s",
            item.kind,
            item.attempts,
            failure.human,
        )
        if item.attempts >= self._max_attempts or self._fail_streak >= self._max_attempts:
            item.to_dm, item.dm_reason = True, failure.human
            return True
        delay = min(self._retry_base * 2 ** (item.attempts - 1), self._retry_max)
        self._spawn(self._retry_later(item, delay), f"admin-chat-retry:{item.kind}")
        return False

    def _requeue(self, item: _Item, failure: _Failure) -> None:
        """A transient failure: put the item back in its place and pause the pump with exponential backoff."""
        now = self._clock()
        self._transient_streak += 1
        self._last_transient = failure.human
        self.stats["failures"] += 1
        backoff = min(self._retry_base * 2 ** min(self._transient_streak - 1, 32), self._retry_max)
        self._pause_until = max(self._pause_until, now + max(backoff, failure.retry_after))
        if item.stuck_since is None:
            item.stuck_since = now
        elif now - item.stuck_since >= self._give_up_after:
            log.error(
                "admin chat: %s notification dropped after %.0f s of failed deliveries: %s",
                item.kind,
                now - item.stuck_since,
                failure.human,
            )
            self.stats["undelivered"] += 1
            self._resolve(item.futures, None)
            return
        (log.warning if self._transient_streak == 1 else log.info)(
            "admin chat: %s notification waits in the queue (%s); next try in %.0f s",
            item.kind,
            failure.human,
            self._pause_until - now,
        )
        self.stats["requeued"] += 1
        heapq.heappush(self._heap, (int(item.priority), item.seq, item))
        if item.key is not None and item.key not in self._by_key:
            self._by_key[item.key] = item  # newer edits merge into it again
        self._wake.set()

    async def _retry_later(self, item: _Item, delay: float) -> None:
        try:
            await self._sleep(delay)
        except asyncio.CancelledError:  # stopped meanwhile: release the waiters
            self._resolve(item.futures, None)
            raise
        if self._closed:
            self._resolve(item.futures, None)
            return
        heapq.heappush(self._heap, (int(item.priority), item.seq, item))
        self._wake.set()

    @staticmethod
    def _resolve(futures: Iterable[asyncio.Future[PostResult | None]], result: PostResult | None) -> None:
        for fut in futures:
            if not fut.done():
                fut.set_result(result)

    def _spawn(self, coro: Awaitable[Any], name: str) -> None:
        task = asyncio.ensure_future(coro)
        task.set_name(name)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("admin chat background task %s failed", task.get_name(), exc_info=task.exception())

    # ------------------------------------------------------------------ delivery: group

    def _route(self, kind: str) -> tuple[TopicDef | None, str | None] | None:
        """Target topic (``None`` = the General topic) and a header line, or ``None`` to drop."""
        defn = self._defs.get(kind) or self._defs[K_SYSTEM]
        st = self.state(defn.kind)
        if st.enabled:
            return defn, None
        if st.fallback == "drop":
            return None
        system = self._defs[K_SYSTEM]
        if self.state(K_SYSTEM).enabled:
            return system, defn.label
        return None, defn.label

    async def _deliver_group(self, item: _Item) -> PostResult:
        chat_id = self._chat_id
        assert chat_id is not None
        route = self._route(item.kind)
        if route is None:
            self.stats["dropped"] += 1
            return PostResult(item.kind, dropped=True, digest=item.digest)
        target, header = route
        text, entities = item.text, item.entities
        if header is not None:
            text, entities = _with_header(text, item.html, entities, header)
        if item.card is not None:
            return await self._deliver_card(item, chat_id, target, text, entities, header=header)
        return await self._send_topic(item, chat_id, target, text, entities, header=header)

    async def _send_topic(
        self,
        item: _Item,
        chat_id: int,
        target: TopicDef | None,
        text: str,
        entities: list[MessageEntity] | None,
        *,
        header: str | None = None,
    ) -> PostResult:
        for heal in range(2):
            thread = await self._thread(chat_id, target)
            try:
                msg = await self._send_rich(item, chat_id, thread, header)
            except _ThreadGone:
                if target is None or heal:
                    raise _Failure(_TXT["f_gone_again"]) from None
                await self._recreate(chat_id, target)
                continue
            if msg is not None:
                self.stats["sent"] += 1
                return PostResult(
                    item.kind,
                    chat_id=chat_id,
                    message_id=msg.message_id,
                    thread_id=thread,
                    digest=item.digest,
                )
            method = SendMessage(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML" if item.html else None,
                entities=entities,
                reply_markup=item.markup,
                message_thread_id=thread,
                disable_notification=item.silent or None,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
            try:
                msg = await self._call(method, chat_id, item.priority)
            except _ThreadGone:
                if target is None or heal:
                    raise _Failure(_TXT["f_gone_again"]) from None
                await self._recreate(chat_id, target)
                continue
            self.stats["sent"] += 1
            return PostResult(
                item.kind,
                chat_id=chat_id,
                message_id=msg.message_id,
                thread_id=thread,
                digest=item.digest,
            )
        raise _Failure(_TXT["f_gone_again"])  # pragma: no cover - the loop always returns or raises

    async def _deliver_card(
        self,
        item: _Item,
        chat_id: int,
        target: TopicDef | None,
        text: str,
        entities: list[MessageEntity] | None,
        *,
        header: str | None = None,
    ) -> PostResult:
        assert item.card is not None
        card = await self._card_get(item.card)
        digest = _content_digest(item)
        if card is not None and card.chat_id == chat_id and card.msg_id is not None:
            same = PostResult(item.kind, chat_id=chat_id, message_id=card.msg_id, thread_id=card.thread_id)
            if card.digest == digest:
                return same
            try:
                if await self._edit_rich(item, chat_id, card.msg_id, header):
                    await self._card_put(item.card, chat_id, card.msg_id, card.thread_id, digest)
                    return same
            except _MessageGone:
                result = await self._send_topic(item, chat_id, target, text, entities, header=header)
                await self._card_put(item.card, chat_id, result.message_id, result.thread_id, digest)
                return result
            method = EditMessageText(
                chat_id=chat_id,
                message_id=card.msg_id,
                text=text,
                parse_mode="HTML" if item.html else None,
                entities=entities,
                reply_markup=item.markup,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
            try:
                await self._call(method, chat_id, item.priority)
            except _NotModified:
                pass
            except _MessageGone:
                card = None  # deleted by someone: send a new card below
            else:
                self.stats["edited"] += 1
            if card is not None:
                await self._card_put(item.card, chat_id, card.msg_id, card.thread_id, digest)
                return same
        result = await self._send_topic(item, chat_id, target, text, entities, header=header)
        await self._card_put(item.card, chat_id, result.message_id, result.thread_id, digest)
        return result

    # ------------------------------------------------------------------ rich messages

    async def _rich_message(self, item: _Item, chat_id: int, header: str | None, *, banner: bool) -> Any:
        """The item's report as ``InputRichMessage``; ``None``: no report, rich is off there, or too big."""
        report = item.report
        if report is None or not self._rich.ok(chat_id):
            return None
        file_id = None
        if banner and self._rich.banner_ok(chat_id):
            file_id = await banner_media(self._holder.get())
        try:
            return report.rich(banner=file_id, header=header)
        except ValueError:  # past the rich limits: the HTML text goes instead
            return None

    async def _send_rich(
        self, item: _Item, chat_id: int, thread: int | None, header: str | None
    ) -> Message | None:
        """Send the report as a rich message; ``None``: send the text instead (refused or not wanted)."""
        rich = await self._rich_message(item, chat_id, header, banner=item.card is None)
        while rich is not None:
            method = SendRichMessage(
                chat_id=chat_id,
                rich_message=rich,
                reply_markup=item.markup,
                message_thread_id=thread,
                disable_notification=item.silent or None,
            )
            try:
                return await self._call(method, chat_id, item.priority, rich=True)
            except _RichRejected as exc:
                if exc.unsupported:
                    self._rich.off_everywhere(exc.human)
                    return None
                blocks = rich.blocks or []
                if blocks and blocks[0].type == "photo":  # maybe no media rights here: once more without it
                    self._rich.banner_off(chat_id)
                    rich = rich.model_copy(update={"blocks": blocks[1:]})
                    continue
                self._rich.off(chat_id, exc.human)
                return None
        return None

    async def _edit_rich(self, item: _Item, chat_id: int, msg_id: int, header: str | None) -> bool:
        """Edit a card into the report's rich form; ``False``: edit it as text instead."""
        rich = await self._rich_message(item, chat_id, header, banner=False)
        if rich is None:
            return False
        method = EditMessageText(
            chat_id=chat_id, message_id=msg_id, rich_message=rich, parse_mode=None, reply_markup=item.markup
        )
        try:
            await self._call(method, chat_id, item.priority, rich=True)
        except _NotModified:
            return True
        except _RichRejected as exc:
            if exc.unsupported:
                self._rich.off_everywhere(exc.human)
            else:
                self._rich.off(chat_id, exc.human)
            return False
        self.stats["edited"] += 1
        return True

    async def _deliver_edit(self, item: _Item) -> PostResult | None:
        assert item.edit is not None
        chat_id, msg_id = self._moved.get(item.edit, item.edit)
        method = EditMessageText(
            chat_id=chat_id,
            message_id=msg_id,
            text=item.text,
            parse_mode="HTML" if item.html else None,
            entities=item.entities,
            reply_markup=item.markup,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        try:
            await self._call(method, chat_id, item.priority)
        except _NotModified:
            pass
        except _MessageGone:
            return await self._resend_edit(item, (chat_id, msg_id))
        except _Failure as failure:
            if failure.transient:
                raise
            log.warning("admin chat: edit of a message failed: %s", failure.human)
            return None
        else:
            self.stats["edited"] += 1
        return PostResult(item.kind, chat_id=chat_id, message_id=msg_id)

    async def _resend_edit(self, item: _Item, gone: tuple[int, int]) -> PostResult | None:
        assert item.edit is not None
        if item.resend_kind is None or self._chat_id is None or gone[0] != self._chat_id:
            return None
        try:
            route = self._route(item.resend_kind)
            if route is None:
                return None
            target, header = route
            text, entities = item.text, item.entities
            if header is not None:
                text, entities = _with_header(text, item.html, entities, header)
            result = await self._send_topic(item, self._chat_id, target, text, entities, header=header)
        except _Failure as failure:
            if failure.transient:
                raise
            log.warning("admin chat: message to edit is gone and could not be re-sent: %s", failure.human)
            return None
        assert result.chat_id is not None and result.message_id is not None
        self._moved[item.edit] = (result.chat_id, result.message_id)
        self._moved.move_to_end(item.edit)
        while len(self._moved) > _MOVED_CACHE:
            self._moved.popitem(last=False)
        return result

    async def _call(
        self, method: TelegramMethod[T], chat_id: int, priority: Priority, *, rich: bool = False
    ) -> T:
        """Run a chat-bound method through the notifier, translating failures into :class:`_Failure`.

        Every request to a group (messages, edits, topic management) takes a slot of our window, which
        mirrors the notifier's group gate: the pump then never picks an item the gate would hold back, so a
        more urgent notification arriving meanwhile is not stuck behind it.
        """
        if chat_id < 0:
            self._window.take(self._clock())
        try:
            result = await self._notifier.call(method, chat_id=chat_id, priority=priority)
        except TelegramBadRequest as exc:
            desc = (exc.message or "").lower()
            if any(s in desc for s in _THREAD_GONE):
                raise _ThreadGone(_TXT["recreated"]) from None
            if _NOT_MODIFIED in desc:
                raise _NotModified("not modified") from None
            if any(s in desc for s in _MESSAGE_GONE):
                raise _MessageGone("message gone") from None
            if rich:  # whatever it is, the plain text may still pass
                raise _RichRejected(mask(exc.message or "")[:120]) from None
            raise _Failure(_human_bad_request(exc.message or "")) from None
        except TelegramMigrateToChat as exc:
            raise _Failure(_TXT["migrated"].format(chat_id=exc.migrate_to_chat_id)) from None
        except _TRANSIENT_ERRORS as exc:
            raise _transient_reason(exc) from None
        except TelegramNotFound as exc:
            if rich:  # an older Bot API server without rich messages: nothing reached the chat
                if chat_id < 0:
                    self._window.give_back()
                raise _RichRejected("method not found", unsupported=True) from None
            raise _Failure(_TXT["f_api"].format(name=type(exc).__name__)) from None
        except TelegramAPIError as exc:
            raise _Failure(_TXT["f_api"].format(name=type(exc).__name__)) from None
        if result is None:  # 403: the bot was kicked or may not write
            raise _Failure(_TXT["f_kicked"])
        return result

    # ------------------------------------------------------------------ delivery: owners' DMs

    async def _deliver_dm(self, item: _Item, reason: str | None) -> PostResult | None:
        try:
            owners = sorted(await self._owners())
        except _DB_ERRORS as exc:
            log.warning("admin chat: cannot resolve owners (%s)", type(exc).__name__)
            raise _Failure(_TXT["f_owners"], transient=True) from None
        if not owners:
            log.warning("admin chat: %s notification not delivered: no owner is configured", item.kind)
            self.stats["undelivered"] += 1
            return None
        defn = self._defs.get(item.kind) or self._defs[K_SYSTEM]
        header = (
            defn.label if reason is None else _TXT["dm_header_down"].format(reason=reason, topic=defn.title)
        )
        text, entities = _with_header(item.text, item.html, item.entities, header)
        refs: dict[int, int] = {}
        transient: _Failure | None = None
        for owner in owners:
            rich = await self._rich_message(item, owner, header, banner=True)
            if rich is not None:
                try:
                    msg = await self._notifier.call(
                        SendRichMessage(
                            chat_id=owner,
                            rich_message=rich,
                            reply_markup=item.markup,
                            disable_notification=item.silent or None,
                        ),
                        chat_id=owner,
                        priority=item.priority,
                    )
                except TelegramNotFound as exc:
                    self._rich.off_everywhere(exc.message or type(exc).__name__)
                except TelegramBadRequest as exc:
                    self._rich.off(owner, exc.message or type(exc).__name__)
                except _SEND_ERRORS as exc:
                    log.warning("admin chat: owner DM failed: %s", type(exc).__name__)
                    if isinstance(exc, _TRANSIENT_ERRORS) and transient is None:
                        transient = _transient_reason(exc)
                    continue
                else:
                    if isinstance(msg, Message):
                        refs[owner] = msg.message_id
                    continue
            method = SendMessage(
                chat_id=owner,
                text=text,
                parse_mode="HTML" if item.html else None,
                entities=entities,
                reply_markup=item.markup,
                disable_notification=item.silent or None,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
            try:
                msg = await self._notifier.call(method, chat_id=owner, priority=item.priority)
            except _SEND_ERRORS as exc:
                log.warning("admin chat: owner DM failed: %s", type(exc).__name__)
                if isinstance(exc, _TRANSIENT_ERRORS) and transient is None:
                    transient = _transient_reason(exc)
                continue
            if isinstance(msg, Message):
                refs[owner] = msg.message_id
        if not refs:
            if transient is not None:
                raise transient  # Telegram / the bot is unavailable: the item waits in the queue
            self.stats["undelivered"] += 1
            return None
        self.stats["dm"] += 1
        return PostResult(item.kind, dm=refs, digest=item.digest)

    # ------------------------------------------------------------------ topics

    async def _thread(self, chat_id: int, target: TopicDef | None) -> int | None:
        if target is None:
            return None
        thread = self.state(target.kind).thread_in(chat_id)
        if thread is not None:
            return thread
        async with self._topic_lock:
            thread = self.state(target.kind).thread_in(chat_id)  # created meanwhile
            if thread is not None:
                return thread
            return await self._create_topic(chat_id, target)

    async def _create_topic(self, chat_id: int, defn: TopicDef) -> int:
        if not self._loaded and not await self._try_load():
            raise _Failure(_TXT["f_db"], local=True)
        icons = await self._icon_ids()
        emoji_id = next(
            (icons[e] for e in map(_norm_emoji, (defn.icon, *defn.icon_alternatives)) if e in icons), None
        )
        name = (defn.title if emoji_id else defn.label)[:128]
        method = CreateForumTopic(
            chat_id=chat_id, name=name, icon_color=defn.icon_color, icon_custom_emoji_id=emoji_id
        )
        st = self.state(defn.kind)
        try:
            topic = await self._call(method, chat_id, Priority.HIGH)
        except _Failure as failure:
            st.last_error = failure.human[:_LAST_ERROR_MAX]
            raise
        st.chat_id, st.thread_id = chat_id, int(topic.message_thread_id)
        st.title, st.icon, st.icon_emoji_id = defn.title, defn.icon, emoji_id
        st.last_error = None
        await self._save(st)
        log.info("admin chat: topic %s created (thread %s)", defn.kind, st.thread_id)
        return st.thread_id

    async def _recreate(self, chat_id: int, defn: TopicDef) -> None:
        st = self.state(defn.kind)
        log.warning("admin chat: topic %s (thread %s) was deleted; recreating", defn.kind, st.thread_id)
        async with self._topic_lock:
            if st.thread_id is not None and st.chat_id == chat_id:
                st.thread_id = None
            st.recreated_at = core_clock.now()
            await self._create_topic(chat_id, defn)
            st.last_error = _TXT["recreated"]
            await self._save(st)
        self.stats["recreated"] += 1

    async def _icon_ids(self) -> dict[str, str]:
        """emoji → custom_emoji_id of the forum icon set (cached; empty when unavailable)."""
        if self._icons is not None:
            return self._icons
        try:
            stickers = await self._direct(GetForumTopicIconStickers())
        except (TelegramAPIError, BotUnavailableError, TimeoutError, *TRANSPORT_ERRORS) as exc:
            log.info("admin chat: topic icons unavailable (%s); using colours", type(exc).__name__)
            return {}
        icons: dict[str, str] = {}
        for sticker in stickers:
            if sticker.emoji and sticker.custom_emoji_id:
                icons.setdefault(_norm_emoji(sticker.emoji), sticker.custom_emoji_id)
        self._icons = icons
        return icons

    async def ensure_topics(self) -> EnsureReport:
        """Create every enabled topic that has no thread in the current chat (idempotent)."""
        report = EnsureReport()
        chat_id = self._chat_id
        if chat_id is None:
            return report
        for defn in list(self._defs.values()):
            st = self.state(defn.kind)
            if not st.enabled:
                continue
            if st.thread_in(chat_id) is not None:
                report.existing.append(defn.kind)
                continue
            try:
                await self._thread(chat_id, defn)
            except _Failure as failure:
                report.failed[defn.kind] = failure.human
                if failure.human in _FATAL_FOR_ALL_TOPICS:
                    break  # the same for every topic: do not hammer Telegram
                continue
            report.created.append(defn.kind)
        return report

    async def set_enabled(self, kind: str, enabled: bool) -> TopicState:
        """Owner's switch. Disabling closes the topic in the group, enabling reopens (or creates) it."""
        defn = self._defs[kind]
        st = self.state(kind)
        if st.enabled == enabled:
            return st
        st.enabled = enabled
        await self._save(st)
        chat_id = self._chat_id
        if chat_id is None:
            return st
        thread = st.thread_in(chat_id)
        try:
            if thread is None:
                if enabled:
                    await self._thread(chat_id, defn)
            elif enabled:
                await self._call(
                    ReopenForumTopic(chat_id=chat_id, message_thread_id=thread), chat_id, Priority.HIGH
                )
            else:
                await self._call(
                    CloseForumTopic(chat_id=chat_id, message_thread_id=thread), chat_id, Priority.HIGH
                )
        except _ThreadGone:
            st.thread_id = None
            await self._save(st)
            if enabled:
                with contextlib.suppress(_Failure):
                    await self._thread(chat_id, defn)
        except _Failure as failure:  # cosmetic: the switch itself is already applied
            log.info("admin chat: could not %s topic %s: %s", "reopen" if enabled else "close", kind,
                     failure.human)  # fmt: skip
        return st

    async def set_fallback(self, kind: str, fallback: Fallback) -> TopicState:
        if fallback not in TOPIC_FALLBACKS:
            raise ValueError(f"fallback must be one of {TOPIC_FALLBACKS}")
        st = self.state(self._defs[kind].kind)
        if st.fallback != fallback:
            st.fallback = fallback
            await self._save(st)
        return st

    async def rename_topics(self) -> int:
        """Bring topic names in the group in line with the definitions (after an update). Returns renames."""
        chat_id = self._chat_id
        if chat_id is None:
            return 0
        renamed = 0
        for defn in self._defs.values():
            st = self.state(defn.kind)
            thread = st.thread_in(chat_id)
            if thread is None or (st.title == defn.title and st.icon == defn.icon):
                continue
            name = (defn.title if st.icon_emoji_id else defn.label)[:128]
            try:
                method = EditForumTopic(chat_id=chat_id, message_thread_id=thread, name=name)
                await self._call(method, chat_id, Priority.LOW)
            except _Failure as failure:
                log.info("admin chat: topic %s not renamed: %s", defn.kind, failure.human)
                continue
            st.title, st.icon = defn.title, defn.icon
            await self._save(st)
            renamed += 1
        return renamed

    # ------------------------------------------------------------------ chat probe

    async def check_chat(self, chat_id: int) -> ChatCheck:
        """Private forum supergroup, the bot an admin with ``can_manage_topics``; raises :class:`ProbeError`.

        A public group (``username`` / ``active_usernames``) is refused: its history is readable by anyone and
        anyone may join it.
        """
        if not isinstance(chat_id, int) or isinstance(chat_id, bool) or chat_id >= 0:
            raise ProbeError(_TXT["bad_id"])
        bot = self._holder.get()
        if bot is None:
            raise ProbeError(_TXT["no_bot"])
        try:
            chat = await self._direct(GetChat(chat_id=chat_id))
            member = await self._direct(GetChatMember(chat_id=chat_id, user_id=bot.id))
        except TelegramMigrateToChat as exc:
            raise ProbeError(_TXT["migrated"].format(chat_id=exc.migrate_to_chat_id)) from None
        except TelegramForbiddenError:
            raise ProbeError(_TXT["kicked"]) from None
        except TelegramBadRequest:
            raise ProbeError(_TXT["chat_not_found"]) from None
        except (TelegramAPIError, TimeoutError, *TRANSPORT_ERRORS):
            raise ProbeError(_TXT["tg_down"]) from None
        if chat.type != "supergroup":
            raise ProbeError(_TXT["not_supergroup"])
        if not chat.is_forum:
            raise ProbeError(_TXT["not_forum"])
        public = chat.username or next(iter(chat.active_usernames or ()), None)
        if public:  # anyone could read payments, errors and panel events, and anyone could join
            raise ProbeError(_TXT["public"].format(username=public))
        if not isinstance(member, ChatMemberAdministrator):
            raise ProbeError(_TXT["not_admin"])
        missing = [label for attr, label in _RIGHTS_REQUIRED if not getattr(member, attr, False)]
        if missing:
            raise ProbeError(_TXT["no_rights"].format(rights=", ".join(missing)))
        warnings = tuple(label for attr, label in _RIGHTS_OPTIONAL if not getattr(member, attr, False))
        return ChatCheck(chat_id=chat_id, title=chat.title or str(chat_id), warnings=warnings)

    async def _direct(self, method: TelegramMethod[T]) -> T:
        """Read-only call outside the message limits (getChat, getChatMember, icon set), bounded."""
        bot = self._holder.get()
        if bot is None:
            raise BotUnavailableError("bot is not configured")
        async with asyncio.timeout(_DIRECT_TIMEOUT):
            return await bot(method, request_timeout=math.ceil(_DIRECT_TIMEOUT))

    # ------------------------------------------------------------------ Component

    async def probe(self, candidate: Mapping[str, Any]) -> None:
        value = candidate["ADMIN_CHAT_ID"]
        if value is None:
            return
        await self.check_chat(int(value))

    async def reconfigure(self, cfg: Mapping[str, Any]) -> None:
        value = cfg["ADMIN_CHAT_ID"]
        chat_id = None if value is None else int(value)
        if self.set_chat(chat_id) and chat_id is not None and self._holder.get() is not None:
            self._spawn(self._ensure_quietly(), "admin-chat-topics")

    async def _ensure_quietly(self) -> None:
        report = await self.ensure_topics()
        if report.failed:
            log.warning("admin chat: topics not created: %s", ", ".join(sorted(report.failed)))

    async def health(self) -> HealthReport:
        details = {"pending": self.pending, **self.stats}
        chat_id = self._chat_id
        if chat_id is None:
            return HealthReport.disabled(_TXT["health_off"], **details)
        if self.down:
            return HealthReport.down(
                _TXT["health_down"].format(reason=self._last_error or "?"),
                fix_action=fix_screen(SCREEN),
                **details,
            )
        if self.waiting:
            summary = _TXT["health_waiting"].format(reason=self._last_transient or "?", pending=self.pending)
            return HealthReport.degraded(summary, **details)
        enabled = [d for d in self._defs.values() if self.state(d.kind).enabled]
        missing = [
            d.title
            for d in enabled
            if self.state(d.kind).thread_in(chat_id) is None and self.state(d.kind).last_error
        ]
        if missing:
            summary = _TXT["health_missing"].format(topics=", ".join(missing))
            return HealthReport.degraded(summary, fix_action=fix_screen(SCREEN), **details)
        ready = sum(1 for d in enabled if self.state(d.kind).thread_in(chat_id) is not None)
        return HealthReport.ok(_TXT["health_ok"].format(topics=ready, pending=self.pending), **details)

    def snapshot(self) -> dict[str, Any]:
        return {
            "chat_id": self._chat_id,
            "pending": self.pending,
            "down": self.down,
            "waiting": self.waiting,
            **self.stats,
        }

    # ------------------------------------------------------------------ persistence

    async def _try_load(self) -> bool:
        try:
            async with self._db.read() as conn:
                rows = (await conn.execute(sa.select(admin_topics))).mappings().all()
        except _DB_ERRORS as exc:
            log.warning("admin chat: cannot load topics (%s)", type(exc).__name__)
            return False
        for row in rows:
            defn = self._defs.get(row["kind"])
            self._states[row["kind"]] = TopicState(
                kind=row["kind"],
                title=row["title"],
                icon=row["icon"],
                enabled=bool(row["enabled"]),
                fallback=row["fallback"] if row["fallback"] in TOPIC_FALLBACKS else "system",
                chat_id=row["chat_id"],
                thread_id=row["thread_id"],
                icon_emoji_id=row["icon_emoji_id"],
                last_error=row["last_error"],
                recreated_at=row["recreated_at"],
            )
            if defn is None:
                log.debug("admin chat: topic %s has no definition (module not loaded)", row["kind"])
        self._loaded = True
        return True

    async def _save(self, st: TopicState) -> bool:
        values = {
            "chat_id": st.chat_id,
            "thread_id": st.thread_id if st.chat_id is not None else None,
            "title": st.title,
            "icon": st.icon,
            "icon_emoji_id": st.icon_emoji_id,
            "enabled": st.enabled,
            "fallback": st.fallback,
            "last_error": mask(st.last_error)[:_LAST_ERROR_MAX] if st.last_error else None,
            "recreated_at": st.recreated_at,
            "updated_at": sa.func.now(),
        }
        stmt = pg_insert(admin_topics).values(kind=st.kind, **values)
        stmt = stmt.on_conflict_do_update(index_elements=[admin_topics.c.kind], set_=values)
        try:
            async with self._db.tx() as conn:
                await conn.execute(stmt)
        except _DB_ERRORS as exc:
            log.warning("admin chat: cannot save topic %s (%s)", st.kind, type(exc).__name__)
            return False
        return True

    async def card(self, kind: str, ref: str) -> _Card | None:
        """The message a card lives in (``None`` if it was never sent)."""
        return await self._card_get((kind, ref))

    async def _card_get(self, key: tuple[str, str]) -> _Card | None:
        cached = self._cards.get(key)
        if cached is not None:
            self._cards.move_to_end(key)
            return cached
        c = admin_cards.c
        try:
            async with self._db.read() as conn:
                row = (
                    (await conn.execute(sa.select(admin_cards).where(c.kind == key[0], c.ref == key[1])))
                    .mappings()
                    .first()
                )
        except _DB_ERRORS as exc:
            log.warning("admin chat: cannot read card %s (%s)", key[0], type(exc).__name__)
            return None
        if row is None:
            return None
        state = row["state"] if isinstance(row["state"], dict) else {}
        card = _Card(row["chat_id"], row["msg_id"], row["thread_id"], state.get("h"))
        self._remember_card(key, card)
        return card

    async def _card_put(
        self, key: tuple[str, str], chat_id: int, msg_id: int | None, thread_id: int | None, digest: str
    ) -> None:
        card = _Card(chat_id, msg_id, thread_id, digest)
        self._remember_card(key, card)
        values = {
            "chat_id": chat_id,
            "msg_id": msg_id,
            "thread_id": thread_id,
            "state": {"h": digest},
            "updated_at": sa.func.now(),
        }
        stmt = pg_insert(admin_cards).values(kind=key[0], ref=key[1], **values)
        stmt = stmt.on_conflict_do_update(index_elements=[admin_cards.c.kind, admin_cards.c.ref], set_=values)
        try:
            async with self._db.tx() as conn:
                await conn.execute(stmt)
        except _DB_ERRORS as exc:
            log.warning("admin chat: cannot save card %s (%s)", key[0], type(exc).__name__)

    def _remember_card(self, key: tuple[str, str], card: _Card) -> None:
        self._cards[key] = card
        self._cards.move_to_end(key)
        while len(self._cards) > _CARD_CACHE:
            self._cards.popitem(last=False)


def format_duration_ru_seconds(seconds: float) -> str:
    """«5 мин», «1 ч 20 мин» for a number of seconds (digest headers)."""
    return format_duration_ru(timedelta(seconds=seconds))
