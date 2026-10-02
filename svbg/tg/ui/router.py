"""Screen router: callbacks → screens/actions, one main message per user, forms, error isolation.

Per callback:

1. decode ``callback_data`` (``v1`` codec; long args resolved through ``short_tokens``);
   unknown/old/expired → the home screen with the toast "Меню обновилось";
2. load :class:`UserCtx` (the app's loader is LRU-cached) and check the screen's or action's
   ``required_role`` / ``perm`` — on **every** callback; denial → toast "Нет прав" and the ``on_denied`` hook;
3. answer the callback query right away (screens: immediately; actions: as soon as the handler returns or
   after ``answer_deadline``, whichever is first, so the handler's toast can be shown);
4. run the handler inside :func:`svbg.core.errors.timeout_guard`: an exception is captured by the error hub
   and the user sees the fallback ("error") screen with a "Меню" button — the bot keeps working;
5. render: the renderer picks ``editMessageText`` / ``editMessageCaption`` / ``editMessageMedia`` or
   "send new + delete old"; the main message id and shape are kept in ``ui_state``.

Callbacks, form input and :meth:`ScreenRouter.show` of one user are processed one at a time (a per-user lock);
a user hammering a button or ``/start`` cannot pile up more than a few waiting updates — extra ones are
dropped (callbacks get a short toast), and waiting for the lock is bounded too, so one user
never ties up the bot's shared handler slots.

Telegram flood control (``429 retry_after``) on an interactive reply is retried once when the wait is short,
otherwise the user gets a "too fast" toast; it is not an error and is not reported to the error hub.

Text messages are routed to an active :class:`~svbg.tg.ui.forms.Form` through ``ui_state.awaiting``.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
import re
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol, TypeVar

import sqlalchemy as sa
from aiogram import Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import AnswerCallbackQuery, DeleteMessage, TelegramMethod
from aiogram.types import CallbackQuery, FSInputFile, InputFile, Message
from aiogram.types import User as TgUser
from sqlalchemy.dialects.postgresql import insert as pg_insert

from svbg.content import defaults
from svbg.core.errors import Capturer, timeout_guard
from svbg.core.log import mask, register_secret
from svbg.tg.ui import codec as codec_mod
from svbg.tg.ui import forms as forms_mod
from svbg.tg.ui import texts
from svbg.tg.ui.context import ROLE_RANK, UserCtx, role_at_least
from svbg.tg.ui.renderer import (
    MODULE_SCREEN,
    SYSTEM_SCREEN,
    MessageShape,
    Op,
    as_markup,
    build_edit,
    build_send,
    content_view,
    nav_button,
    plan_transition,
)
from svbg.tg.ui.tables import ui_state
from svbg.tg.ui.view import MediaRef, Redirect, Toast, View

if TYPE_CHECKING:
    from aiogram import Bot

    from svbg.content.model import Media
    from svbg.content.store import ContentSnapshot, ContentStore, ScreenEntry
    from svbg.db.engine import Database
    from svbg.tg.notifier import Notifier
    from svbg.tg.ui.codec import CallbackCodec, Decoded
    from svbg.tg.ui.forms import Form

__all__ = [
    "Access",
    "AccessDeniedError",
    "BotTransport",
    "HandlerResult",
    "MediaRef",
    "Redirect",
    "ScreenCtx",
    "ScreenRouter",
    "Toast",
    "UiState",
    "UiStateStore",
    "UiTransport",
    "View",
    "looks_like_secret",
]

log = logging.getLogger("svbg.tg.ui")

T = TypeVar("T")

HandlerResult = View | Toast | Redirect | None
ScreenFn = Callable[["ScreenCtx", Any], Awaitable[View | Redirect]]
ActionFn = Callable[["ScreenCtx", Any], Awaitable[HandlerResult]]
UserLoader = Callable[[TgUser], Awaitable[UserCtx | None]]
DeniedHook = Callable[[UserCtx, str], Awaitable[None] | None]

RESERVED: Final = frozenset({SYSTEM_SCREEN, MODULE_SCREEN, forms_mod.FORM_SCREEN, "ui"})
MAX_REDIRECTS: Final = 3
MAX_WAITING_PER_USER: Final = 3  # updates of one user running + waiting for the per-user lock
FALLBACK_TIMEOUT: Final = 5.0  # the error toast + fallback screen run outside the handler timeout
RETRY_AFTER_MAX: Final = 2  # a 429 with retry_after <= this (s) is waited out and retried once
#: Staff whose stray secret-looking messages are deleted (a token pasted outside a form).
STRAY_SECRET_ROLE: Final = "admin"  # noqa: S105 - a role name
_JWT_RE: Final = re.compile(r"^[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}$")
_OPAQUE_RE: Final = re.compile(r"^[A-Za-z0-9_\-+=]{32,}$")  # one opaque word: no dots, slashes
_SECRET_TEXT_MAX: Final = 8192

# Bot API error descriptions (lowercase substrings) meaning "edit is impossible, send a new message".
_MISSING: Final = (  # the old message is gone: just send a new one
    "message to edit not found",
    "message_id_invalid",
    "message not found",
    "message can't be found",
)
_UNEDITABLE: Final = (  # the old message exists but cannot become this screen: replace it
    "message can't be edited",
    "there is no text in the message to edit",
    "there is no caption in the message to edit",
    "there is no media in the message to edit",
)


class AccessDeniedError(Exception):
    """Raised by :meth:`ScreenCtx.start_form` when the user may not use the form."""


# ---------------------------------------------------------------- transport


class UiTransport(Protocol):
    """How the router talks to Telegram (a real bot, the notifier, or a test fake)."""

    @property
    def bot_id(self) -> int | None: ...

    @property
    def bot_username(self) -> str | None: ...

    async def answer(self, method: AnswerCallbackQuery) -> None: ...

    async def call(self, method: TelegramMethod[T], *, chat_id: int) -> T | None: ...


class _BotSource(Protocol):
    def get(self) -> Bot | None: ...

    @property
    def me(self) -> TgUser | None: ...


class BotTransport:
    """:class:`UiTransport` over ``BotHolder``; chat-bound calls optionally go through the notifier.

    Interactive replies default to direct calls (a click must not wait behind the 1 msg/s per-chat pacing
    of proactive notifications); pass ``notifier`` to route them through it with ``HIGH`` priority.
    """

    def __init__(
        self,
        holder: _BotSource,
        *,
        notifier: Notifier | None = None,
        answer_timeout: float = 5.0,
        request_timeout: float = 10.0,
    ) -> None:
        if answer_timeout <= 0 or request_timeout <= 0:
            raise ValueError("timeouts must be positive")
        self._holder = holder
        self._notifier = notifier
        self._answer_timeout = answer_timeout
        self._request_timeout = request_timeout

    def _bot(self) -> Bot:
        bot = self._holder.get()
        if bot is None:
            raise RuntimeError("bot is not configured")
        return bot

    @property
    def bot_id(self) -> int | None:
        bot = self._holder.get()
        return None if bot is None else bot.id

    @property
    def bot_username(self) -> str | None:
        me = self._holder.me
        return None if me is None else me.username

    async def answer(self, method: AnswerCallbackQuery) -> None:
        async with asyncio.timeout(self._answer_timeout):
            await self._bot()(method, request_timeout=math.ceil(self._answer_timeout))

    async def call(self, method: TelegramMethod[T], *, chat_id: int) -> T | None:
        if self._notifier is not None:
            from svbg.tg.notifier import Priority

            return await self._notifier.call(method, chat_id=chat_id, priority=Priority.HIGH)
        # aiogram's default HTTP timeout is 60 s: an interactive reply must fail much sooner
        async with asyncio.timeout(self._request_timeout):
            return await self._bot()(method, request_timeout=math.ceil(self._request_timeout))


# ---------------------------------------------------------------- ui_state


@dataclass(slots=True)
class UiState:
    user_id: int
    chat_id: int | None = None
    main_msg_id: int | None = None
    main_shape: MessageShape | None = None
    awaiting: dict[str, Any] | None = None
    pending_intent: dict[str, Any] | None = None


class UiStateStore:
    """Write-through LRU cache over ``ui_state`` (reads on the hot path normally cost no SQL)."""

    def __init__(self, db: Database, *, cache_size: int = 10_000) -> None:
        self._db = db
        self._cache: OrderedDict[int, UiState] = OrderedDict()
        self._cache_size = max(cache_size, 16)

    def _remember(self, state: UiState) -> UiState:
        self._cache[state.user_id] = state
        self._cache.move_to_end(state.user_id)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return state

    def forget(self, user_id: int) -> None:
        self._cache.pop(user_id, None)

    async def get(self, user_id: int) -> UiState:
        cached = self._cache.get(user_id)
        if cached is not None:
            self._cache.move_to_end(user_id)
            return cached
        async with self._db.read() as conn:
            row = (
                (await conn.execute(sa.select(ui_state).where(ui_state.c.user_id == user_id)))
                .mappings()
                .first()
            )
        if row is None:
            return self._remember(UiState(user_id))
        return self._remember(
            UiState(
                user_id=user_id,
                chat_id=row["chat_id"],
                main_msg_id=row["main_msg_id"],
                main_shape=MessageShape.from_json(row["main_shape"]),
                awaiting=row["awaiting"] if isinstance(row["awaiting"], dict) else None,
                pending_intent=row["pending_intent"] if isinstance(row["pending_intent"], dict) else None,
            )
        )

    async def _upsert(self, user_id: int, raw: Mapping[str, Any]) -> None:
        # None must become SQL NULL, not the JSON value 'null'.
        values = {k: sa.null() if v is None else v for k, v in raw.items()}
        stmt = pg_insert(ui_state).values(user_id=user_id, **values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[ui_state.c.user_id], set_={**values, "updated_at": sa.func.now()}
        )
        async with self._db.tx() as conn:
            await conn.execute(stmt)

    async def set_main(self, user_id: int, chat_id: int, msg_id: int, shape: MessageShape | None) -> None:
        state = await self.get(user_id)
        if state.chat_id == chat_id and state.main_msg_id == msg_id and state.main_shape == shape:
            return
        await self._upsert(
            user_id,
            {"chat_id": chat_id, "main_msg_id": msg_id, "main_shape": shape.to_json() if shape else None},
        )
        state.chat_id, state.main_msg_id, state.main_shape = chat_id, msg_id, shape

    async def set_awaiting(self, user_id: int, awaiting: dict[str, Any] | None) -> None:
        state = await self.get(user_id)
        if state.awaiting is None and awaiting is None:
            return
        await self._upsert(user_id, {"awaiting": awaiting})
        state.awaiting = awaiting

    async def set_pending_intent(self, user_id: int, intent: dict[str, Any] | None) -> None:
        state = await self.get(user_id)
        if state.pending_intent is None and intent is None:
            return
        await self._upsert(user_id, {"pending_intent": intent})
        state.pending_intent = intent

    async def pop_pending_intent(self, user_id: int) -> dict[str, Any] | None:
        state = await self.get(user_id)
        intent = state.pending_intent
        if intent is not None:
            await self.set_pending_intent(user_id, None)
        return intent


# ---------------------------------------------------------------- registry


@dataclass(frozen=True, slots=True)
class Access:
    required_role: str | None = None
    perm: str | None = None

    def __post_init__(self) -> None:
        if self.required_role is not None and self.required_role not in ROLE_RANK:
            raise ValueError(f"unknown role {self.required_role!r}")

    def allows(self, user: UserCtx) -> bool:
        if self.required_role is not None and not role_at_least(user.role, self.required_role):
            return False
        return self.perm is None or user.has_perm(self.perm)


PUBLIC: Final = Access()


@dataclass(frozen=True, slots=True)
class _ScreenRoute:
    code: str
    fn: ScreenFn
    access: Access


@dataclass(frozen=True, slots=True)
class _ActionRoute:
    screen: str
    action: str
    fn: ActionFn
    access: Access


class _KeyedLocks:
    """Per-key asyncio locks that disappear when nobody holds or waits for them."""

    def __init__(self) -> None:
        self._locks: dict[int, tuple[asyncio.Lock, int]] = {}

    def waiting(self, key: int) -> int:
        entry = self._locks.get(key)
        return 0 if entry is None else entry[1]

    @asynccontextmanager
    async def hold(
        self, key: int, *, limit: int | None = None, wait: float | None = None
    ) -> AsyncIterator[bool]:
        """Hold the lock of ``key``: yields ``True`` with the lock held.

        Yields ``False`` without the lock when ``limit`` holders/waiters are already queued for ``key`` or the
        lock was not acquired within ``wait`` seconds (the caller drops the update).
        """
        lock, users = self._locks.get(key, (None, 0))
        if limit is not None and users >= limit:
            yield False
            return
        if lock is None:
            lock = asyncio.Lock()
        self._locks[key] = (lock, users + 1)
        acquired = False
        try:
            try:
                async with asyncio.timeout(wait):
                    await lock.acquire()
                acquired = True
            except TimeoutError:
                log.debug("gave up waiting for the lock of %s after %ss", key, wait)
            yield acquired
        finally:
            if acquired:
                lock.release()
            lock, users = self._locks[key]
            if users <= 1:
                del self._locks[key]
            else:
                self._locks[key] = (lock, users - 1)


# ---------------------------------------------------------------- request context


@dataclass(slots=True, eq=False)
class ScreenCtx:
    """Request context handed to screen renderers and action handlers."""

    router: ScreenRouter
    user: UserCtx
    chat_id: int
    message_id: int | None = None
    shape: MessageShape | None = None
    callback_id: str | None = None
    tg_user: TgUser | None = None
    answered: bool = False
    input_mode: bool = False  # rendering after a text message: send a new message instead of editing

    @property
    def lang(self) -> str:
        return self.user.lang

    @property
    def content(self) -> ContentSnapshot | None:
        store = self.router.content
        return None if store is None else store.snapshot

    async def answer(self, text: str | None = None, *, alert: bool = False) -> None:
        """Answer the callback query once; later calls are ignored."""
        if self.answered or self.callback_id is None:
            if text:
                log.debug("toast dropped: the callback was already answered")
            return
        self.answered = True
        await self.router._answer(self.callback_id, text, alert)

    def content_view(self, code: str, *, extra_rows: Sequence[Sequence[Any]] | None = None) -> View | None:
        """Content screen ``code`` rendered for this user, or ``None`` if it does not exist."""
        entry = None if self.router.content is None else self.router.content.get_screen(code)
        if entry is None or not entry.screen.enabled:
            return None
        return self.router._content_view(self, entry, extra_rows)

    async def callback(self, screen: str, action: str = codec_mod.ACTION_OPEN, arg: Any = None) -> str:
        """``callback_data`` for a button; long or non-string args go through short tokens."""
        if arg is None or (isinstance(arg, str) and codec_mod.fits(screen, action, arg)):
            return codec_mod.encode(screen, action, arg)
        if self.router.codec is None:
            raise codec_mod.CallbackTooLongError("long callback args need a CallbackCodec with a database")
        return await self.router.codec.encode_long(screen, action, arg)

    async def start_form(self, name: str, initial: Mapping[str, Any] | None = None) -> View:
        """Begin form ``name`` and return its first prompt (the handler returns this view)."""
        form = self.router.forms.get(name)
        if form is None:
            raise KeyError(f"unknown form {name!r}")
        if not Access(form.required_role, form.perm).allows(self.user):
            raise AccessDeniedError(name)
        state = forms_mod.start_state(form, initial)
        await self.router.ui_state.set_awaiting(self.user.user_id, state.to_json())
        return forms_mod.prompt_view(form, state, self.lang)


def looks_like_secret(text: str | None) -> bool:
    """Heuristic for "the user pasted a token": a JWT (Remnawave API token), something :func:`mask`
    would hide (bot token, ``password=...``, a registered secret) or one long opaque word with letters and
    digits."""
    if not text or len(text) > _SECRET_TEXT_MAX:
        return False
    value = text.strip()
    if not value or value.startswith("/"):
        return False
    if _JWT_RE.match(value) or mask(value) != value:
        return True
    return (
        bool(_OPAQUE_RE.match(value)) and any(c.isdigit() for c in value) and any(c.isalpha() for c in value)
    )


# ---------------------------------------------------------------- router


class ScreenRouter:
    def __init__(
        self,
        *,
        transport: UiTransport,
        user_loader: UserLoader,
        ui_state: UiStateStore,
        content: ContentStore | None = None,
        codec: CallbackCodec | None = None,
        hub: Capturer | None = None,
        home: str = defaults.HOME,
        error_screen: str = defaults.ERROR,
        answer_deadline: float = 0.4,
        handler_timeout: float = 20.0,
        public_url: Callable[[], str | None] | None = None,
        media_url: Callable[[str, Any], str | None] | None = None,
        media_root: Path | None = None,
        on_denied: DeniedHook | None = None,
        lock_wait: float | None = None,
    ) -> None:
        self.transport = transport
        self.user_loader = user_loader
        self.ui_state = ui_state
        self.content = content
        self.codec = codec
        self.hub = hub
        self.home = home
        self.error_screen = error_screen
        self.answer_deadline = answer_deadline
        self.handler_timeout = handler_timeout
        # how long an update may wait behind the same user's previous one before it is dropped
        self.lock_wait = lock_wait if lock_wait is not None else handler_timeout + FALLBACK_TIMEOUT
        self._public_url = public_url
        #: ``(base, media) → public link`` of the ``/m/<token>`` route (``PublicMedia.url``); the app sets it
        #: once the media module is up. Without it the preview mode falls back to sending the media itself.
        self.media_url = media_url
        self._media_root = media_root.resolve() if media_root is not None else None
        self.on_denied = on_denied
        self._screens: dict[str, _ScreenRoute] = {}
        self._actions: dict[tuple[str, str], _ActionRoute] = {}
        self._policies: dict[str, Access] = {
            code: Access(a.required_role, a.perm) for code, a in defaults.SCREEN_ACCESS.items()
        }
        self.forms: dict[str, Form] = {}
        self._locks = _KeyedLocks()
        #: One first upload per content media at a time: concurrent first sends (the default banner is on
        #: every screen) wait for it and reuse the learned ``file_id`` instead of uploading the file again.
        self._uploads: dict[int, asyncio.Lock] = {}

    # ------------------------------------------------------------ registration

    def screen(
        self, code: str, *, required_role: str | None = None, perm: str | None = None
    ) -> Callable[[ScreenFn], ScreenFn]:
        """Register a code-defined screen renderer ``async (ctx, arg) -> View | Redirect``."""
        self._check_name(code)
        access = Access(required_role, perm)

        def deco(fn: ScreenFn) -> ScreenFn:
            _require_async(fn)
            if code in self._screens:
                raise ValueError(f"screen {code!r} is already registered")
            self._screens[code] = _ScreenRoute(code, fn, access)
            return fn

        return deco

    def action(
        self, screen: str, action: str, *, required_role: str | None = None, perm: str | None = None
    ) -> Callable[[ActionFn], ActionFn]:
        """Register an action ``async (ctx, arg) -> View | Toast | Redirect | None``.

        ``screen`` may be ``"sys"`` (content ``system:<name>`` buttons) or ``"mod"`` (``module:<ext>.<act>``).
        """
        if screen not in (SYSTEM_SCREEN, MODULE_SCREEN):
            self._check_name(screen)
        codec_mod.encode(screen, action)  # validates both names
        if action == codec_mod.ACTION_OPEN:
            raise ValueError(f"action name {codec_mod.ACTION_OPEN!r} is reserved for opening screens")
        access = Access(required_role, perm)

        def deco(fn: ActionFn) -> ActionFn:
            _require_async(fn)
            key = (screen, action)
            if key in self._actions:
                raise ValueError(f"action {screen}:{action} is already registered")
            self._actions[key] = _ActionRoute(screen, action, fn, access)
            return fn

        return deco

    def policy(self, code: str, *, required_role: str | None = None, perm: str | None = None) -> None:
        """Access rule for a data-only (content) screen."""
        self._policies[code] = Access(required_role, perm)

    def form(self, form: Form) -> Form:
        if form.name in self.forms:
            raise ValueError(f"form {form.name!r} is already registered")
        self.forms[form.name] = form
        return form

    @staticmethod
    def _check_name(code: str) -> None:
        codec_mod.encode(code)  # validates the name
        if code in RESERVED:
            raise ValueError(f"screen name {code!r} is reserved")

    # ------------------------------------------------------------ aiogram glue

    def aiogram_router(self, name: str = "svbg-ui") -> Router:
        """aiogram router: every callback query, and text messages while a form awaits input."""
        router = Router(name=name)

        async def on_callback(query: CallbackQuery) -> None:
            await self.dispatch_callback(query)

        async def awaiting_filter(message: Message) -> bool:
            return await self.is_awaiting(message)

        async def on_message(message: Message) -> None:
            if not await self.dispatch_message(message):
                raise SkipHandler  # the form expired meanwhile: let other handlers see the message

        async def stray_filter(message: Message) -> bool:
            return message.chat.type == "private" and looks_like_secret(message.text)

        async def on_stray(message: Message) -> None:
            if not await self.dispatch_stray_secret(message):
                raise SkipHandler

        router.callback_query.register(on_callback)
        router.message.register(on_message, awaiting_filter)
        router.message.register(on_stray, stray_filter)  # after forms: only text no form is waiting for
        return router

    # ------------------------------------------------------------ callbacks

    def _hold_user(self, key: int) -> AbstractAsyncContextManager[bool]:
        return self._locks.hold(key, limit=MAX_WAITING_PER_USER, wait=self.lock_wait)

    async def dispatch_callback(self, query: CallbackQuery) -> None:
        tg_user = query.from_user
        async with self._hold_user(tg_user.id) as held:
            if held:
                await self._handle_callback(query)
                return
        await self._answer(query.id, texts.t(tg_user.language_code, "busy"), False)

    async def _handle_callback(self, query: CallbackQuery) -> None:
        tg_user = query.from_user
        message = query.message
        chat_id = message.chat.id if message is not None else tg_user.id
        user = await self._load_user(tg_user, query.id)
        if user is None:
            return
        ctx = ScreenCtx(self, user, chat_id, callback_id=query.id, tg_user=tg_user)
        async with timeout_guard(
            "ui:callback",
            self.handler_timeout,
            hub=self.hub,
            user_id=user.user_id,
            on_error=lambda exc: self._on_handler_error(ctx),
            context={"data": (query.data or "")[:64]},
        ):
            try:
                await self._callback_body(ctx, query.data, message)
            except TelegramRetryAfter as e:
                await self._flood(ctx, e)

    async def _callback_body(self, ctx: ScreenCtx, data: str | None, message: Any) -> None:
        await self._bind_target(ctx, message)
        decoded = codec_mod.decode(data)
        if decoded is not None and decoded.token is not None:
            decoded = None if self.codec is None else await self.codec.resolve(decoded)
        if decoded is None:
            await self._stale(ctx)
            return
        try:
            await self._route(ctx, decoded)
        except AccessDeniedError as e:
            await self._deny(ctx, f"form:{e}")

    async def _flood(self, ctx: ScreenCtx, exc: TelegramRetryAfter) -> None:
        """Telegram flood control while replying is not a bug: ask the user to slow down, do not report."""
        log.info("flood control for user %s: retry in %ss", ctx.user.user_id, exc.retry_after)
        # shown only while the click is unanswered (screens answer before rendering); nothing is sent to the
        # throttled chat itself, and no fallback screen is attempted
        await ctx.answer(texts.t(ctx.lang, "flood"))

    async def _route(self, ctx: ScreenCtx, decoded: Decoded) -> None:
        screen, action, arg = decoded.screen, decoded.action, decoded.arg
        if screen == forms_mod.FORM_SCREEN:
            await self._form_callback(ctx, action)
            return
        route = self._actions.get((screen, action))
        if route is not None:
            if not await self._allowed(ctx, route.access, f"action:{screen}.{action}"):
                return
            await self._run_action(ctx, route, arg)
            return
        if action == codec_mod.ACTION_OPEN:
            await self._open(ctx, screen, arg, depth=0)
            return
        await self._stale(ctx)

    async def _allowed(self, ctx: ScreenCtx, access: Access, place: str) -> bool:
        if access.allows(ctx.user):
            return True
        await self._deny(ctx, place)
        return False

    async def _deny(self, ctx: ScreenCtx, place: str) -> None:
        """Toast "Нет прав", log and report the attempt to ``on_denied`` (admin audit)."""
        await ctx.answer(texts.t(ctx.lang, "denied"), alert=False)
        log.info("access denied: user %s at %s", ctx.user.user_id, place)
        if self.on_denied is not None:
            try:
                res = self.on_denied(ctx.user, place)
                if inspect.isawaitable(res):
                    await res
            except Exception:
                log.exception("on_denied hook failed")

    async def _open(
        self, ctx: ScreenCtx, screen: str, arg: Any, *, depth: int, toast: str | None = None
    ) -> None:
        """Render screen ``screen`` (code route or content screen) after the access check."""
        route = self._screens.get(screen)
        entry: ScreenEntry | None = None
        if route is None:
            entry = None if self.content is None else self.content.get_screen(screen)
            if entry is None or not entry.screen.enabled:
                if screen == self.home:
                    await ctx.answer(toast)
                    await self._deliver(ctx, self._builtin_home(ctx))
                    return
                await self._stale(ctx)
                return
            access = self._policies.get(entry.code or "", PUBLIC)
            place = f"screen:{entry.code or entry.id}"
        else:
            access = route.access
            place = f"screen:{screen}"
        if not await self._allowed(ctx, access, place):
            return
        await ctx.answer(toast)  # screens answer immediately, before any rendering work
        if route is not None:
            result: HandlerResult = await route.fn(ctx, arg)
        else:
            assert entry is not None
            result = self._content_view(ctx, entry, None)
        await self._apply_result(ctx, result, depth=depth)

    async def _redirect(self, ctx: ScreenCtx, redirect: Redirect, depth: int) -> None:
        if depth >= MAX_REDIRECTS:
            raise RuntimeError(f"too many redirects (last: {redirect.screen})")
        await self._open(ctx, redirect.screen, redirect.arg, depth=depth + 1, toast=redirect.toast)

    async def _run_action(self, ctx: ScreenCtx, route: _ActionRoute, arg: Any) -> None:
        task: asyncio.Task[HandlerResult] = asyncio.create_task(route.fn(ctx, arg))
        try:
            done, _ = await asyncio.wait({task}, timeout=self.answer_deadline)
            if not done:
                await ctx.answer()  # stop the spinner; the handler keeps running
            result = await task
        finally:
            if not task.done():
                task.cancel()
        await self._apply_result(ctx, result, depth=0)

    async def _apply_result(self, ctx: ScreenCtx, result: HandlerResult, *, depth: int) -> None:
        if result is None:
            await ctx.answer()
        elif isinstance(result, Toast):
            await ctx.answer(result.text, alert=result.alert)
        elif isinstance(result, Redirect):
            await self._redirect(ctx, result, depth)
        elif isinstance(result, View):
            await ctx.answer(result.toast, alert=result.toast_alert)
            await self._deliver(ctx, result)
        else:
            raise TypeError(f"handler returned {type(result).__name__}, expected View/Toast/Redirect/None")

    async def _stale(self, ctx: ScreenCtx) -> None:
        """Unknown, outdated or expired button: show home with the toast "Меню обновилось"."""
        await self._open(ctx, self.home, None, depth=MAX_REDIRECTS - 1, toast=texts.t(ctx.lang, "stale"))

    async def _on_handler_error(self, ctx: ScreenCtx) -> None:
        # Runs after the handler timeout and still under the per-user lock: bounded, no flood-control waits.
        try:
            async with asyncio.timeout(FALLBACK_TIMEOUT):
                await ctx.answer(texts.t(ctx.lang, "error_toast"))
                view = self._fallback_view(ctx)
                await self._deliver(ctx, view, retry=False)
        except (TelegramAPIError, OSError, TimeoutError, ValueError) as e:
            log.warning("fallback screen failed for user %s: %s", ctx.user.user_id, type(e).__name__)

    def _fallback_view(self, ctx: ScreenCtx) -> View:
        """The "error" content screen (text + Menu button, no slots) or a built-in equivalent."""
        entry = None if self.content is None else self.content.get_screen(self.error_screen)
        if entry is not None and entry.screen.enabled:
            try:
                view = self._content_view(ctx, entry, None, with_media=False)
            except (ValueError, TypeError, KeyError) as e:
                log.warning("error screen content is broken: %s", e)
            else:
                if view.keyboard:
                    return view
        return View(
            text=texts.t(ctx.lang, "error_text"),
            keyboard=[[nav_button(texts.t(ctx.lang, "menu"), self.home)]],
        )

    def _builtin_home(self, ctx: ScreenCtx) -> View:
        return View(text=texts.t(ctx.lang, "home_text"))

    # ------------------------------------------------------------ helpers

    async def _load_user(self, tg_user: TgUser, callback_id: str | None) -> UserCtx | None:
        user: UserCtx | None = None
        failed = False

        async def on_error(_exc: BaseException) -> None:
            nonlocal failed
            failed = True

        async with timeout_guard("ui:user_loader", self.handler_timeout, hub=self.hub, on_error=on_error):
            user = await self.user_loader(tg_user)
        if user is None and callback_id is not None:
            lang = tg_user.language_code if failed else None
            await self._answer(callback_id, texts.t(lang, "error_toast") if failed else None, False)
        return user

    async def _bind_target(self, ctx: ScreenCtx, message: Any) -> None:
        """Remember which message to edit and its shape (from ui_state when it is the main message)."""
        if not isinstance(message, Message):  # None or InaccessibleMessage: cannot edit, send a new one
            return
        ctx.message_id = message.message_id
        state = await self.ui_state.get(ctx.user.user_id)
        if state.main_msg_id == message.message_id and state.chat_id == ctx.chat_id and state.main_shape:
            ctx.shape = state.main_shape
        else:
            ctx.shape = MessageShape.of_message(message)
            if ctx.shape is None:
                ctx.message_id = None

    async def _answer(self, callback_id: str, text: str | None, alert: bool) -> None:
        method = AnswerCallbackQuery(
            callback_query_id=callback_id, text=text or None, show_alert=alert if text else None
        )
        try:
            await self.transport.answer(method)
        except TelegramBadRequest as e:  # query is too old / already answered
            log.debug("answerCallbackQuery rejected: %s", e.message)
        except (TelegramAPIError, OSError, TimeoutError) as e:
            log.warning("answerCallbackQuery failed: %s", type(e).__name__)

    def _media_ref(self, media: Media) -> MediaRef | None:
        key = f"m:{media.id}"
        bot_id = self.transport.bot_id
        if bot_id is not None and self.content is not None:
            file_id = self.content.file_id(media.id, bot_id)
            if file_id:
                return MediaRef(media.kind, file_id, key, media.id)
        if media.path and self._media_root is not None:
            path = (self._media_root / media.path).resolve()
            if not path.is_relative_to(self._media_root):
                log.warning("media %s path escapes the media directory; ignored", media.id)
                return None
            if path.is_file():
                return MediaRef(media.kind, FSInputFile(path), key, media.id)
        log.warning("media %s has neither a file_id for this bot nor a readable file", media.id)
        return None

    def _content_view(
        self,
        ctx: ScreenCtx,
        entry: ScreenEntry,
        extra_rows: Sequence[Sequence[Any]] | None,
        *,
        with_media: bool = True,
    ) -> View:
        media_ref: MediaRef | None = None
        preview_url: str | None = None
        if with_media and entry.screen.media_id is not None and self.content is not None:
            media = self.content.get_media(entry.screen.media_id)
            if media is not None:
                base = self._public_url() if self._public_url is not None else None
                if entry.screen.media_mode == "preview" and base and self.media_url is not None:
                    try:
                        preview_url = self.media_url(base, media)
                    except Exception:  # a broken link never breaks the screen
                        log.warning("public link of media %s is not available", media.id, exc_info=True)
                        preview_url = None
                if preview_url:
                    media_ref = MediaRef(media.kind, preview_url, f"m:{media.id}", media.id)
                else:
                    media_ref = self._media_ref(media)
        return content_view(
            entry,
            ctx.user,
            media=media_ref,
            preview_url=preview_url,
            extra_rows=extra_rows,
            bot_username=self.transport.bot_username,
        )

    # ------------------------------------------------------------ delivery

    async def _call(self, method: TelegramMethod[T], chat_id: int, *, retry: bool = True) -> T | None:
        """``transport.call`` waiting out a short flood-control pause once (inside the caller's timeout)."""
        try:
            return await self.transport.call(method, chat_id=chat_id)
        except TelegramRetryAfter as e:
            if not retry or e.retry_after > RETRY_AFTER_MAX:
                raise
            log.debug("flood control in chat %s: retry in %ss", chat_id, e.retry_after)
            await asyncio.sleep(max(e.retry_after, 0))
        return await self.transport.call(method, chat_id=chat_id)

    async def _deliver(self, ctx: ScreenCtx, view: View, *, retry: bool = True) -> None:
        media = view.media
        if (
            media is None
            or media.media_id is None
            or not isinstance(media.file, InputFile)
            or self.content is None
            or self.transport.bot_id is None
        ):
            await self._deliver_now(ctx, view, retry=retry)
            return
        # an upload whose file_id will be cached: one at a time per media; the others wait for it and send
        # the learned file_id (outside the lock, concurrently)
        async with self._uploads.setdefault(media.media_id, asyncio.Lock()):
            file_id = self.content.file_id(media.media_id, self.transport.bot_id)
            if not file_id:
                await self._deliver_now(ctx, view, retry=retry)
                return
        view.media = MediaRef(media.kind, file_id, media.key, media.media_id)
        await self._deliver_now(ctx, view, retry=retry)

    async def _deliver_now(self, ctx: ScreenCtx, view: View, *, retry: bool = True) -> None:
        markup = as_markup(view.keyboard)
        new_shape = MessageShape.of_view(view)
        prev = ctx.shape if ctx.message_id is not None else None
        op = plan_transition(prev, new_shape, force_new=view.mode == "new" or ctx.input_mode)
        sent: Any = None
        if op in (Op.EDIT_TEXT, Op.EDIT_CAPTION, Op.EDIT_MEDIA):
            assert ctx.message_id is not None
            try:
                sent = await self._call(
                    build_edit(op, view, ctx.chat_id, ctx.message_id, markup), ctx.chat_id, retry=retry
                )
            except TelegramBadRequest as e:
                desc = (e.message or "").lower()
                if "message is not modified" in desc:
                    sent = None
                elif any(s in desc for s in _MISSING):
                    op = Op.SEND_NEW
                elif any(s in desc for s in _UNEDITABLE):
                    op = Op.SEND_NEW_DELETE_OLD
                else:
                    raise
            if op not in (Op.SEND_NEW, Op.SEND_NEW_DELETE_OLD):
                await self._after_send(ctx, view, sent, ctx.message_id, new_shape)
                return
        old_id = ctx.message_id
        sent = await self._call(build_send(view, ctx.chat_id, markup), ctx.chat_id, retry=retry)
        if not isinstance(sent, Message):
            return  # the user blocked the bot (notifier returned None)
        if op is Op.SEND_NEW_DELETE_OLD and old_id is not None and old_id != sent.message_id:
            await self._delete_quietly(ctx.chat_id, old_id)
        await self._after_send(ctx, view, sent, sent.message_id, new_shape)

    async def _after_send(
        self, ctx: ScreenCtx, view: View, sent: Any, msg_id: int, shape: MessageShape
    ) -> None:
        ctx.message_id, ctx.shape = msg_id, shape
        try:
            await self.ui_state.set_main(ctx.user.user_id, ctx.chat_id, msg_id, shape)
        except (sa.exc.SQLAlchemyError, OSError) as e:  # the message is shown; only tracking is degraded
            log.warning("could not store the main message of user %s: %s", ctx.user.user_id, type(e).__name__)
        media = view.media
        if (
            media is not None
            and media.media_id is not None
            and isinstance(media.file, InputFile)
            and isinstance(sent, Message)
            and self.content is not None
            and self.transport.bot_id is not None
        ):
            file_id = _file_id_of(sent, media.kind)
            if file_id:
                try:
                    await self.content.remember_file_id(media.media_id, self.transport.bot_id, file_id)
                except (sa.exc.SQLAlchemyError, OSError) as e:
                    log.warning("could not cache file_id for media %s: %s", media.media_id, type(e).__name__)

    async def _delete_quietly(self, chat_id: int, message_id: int) -> None:
        try:
            await self.transport.call(DeleteMessage(chat_id=chat_id, message_id=message_id), chat_id=chat_id)
        except TelegramAPIError as e:  # older than 48 h, already deleted, ...
            log.debug("could not delete old message: %s", e.message)

    # ------------------------------------------------------------ programmatic display

    async def show(
        self,
        user: UserCtx,
        chat_id: int,
        screen: str,
        arg: Any = None,
        *,
        new: bool = False,
        toast: str | None = None,
    ) -> bool:
        """Show ``screen`` outside a callback (``/start``, deep links, after a payment).

        Edits the user's main message when it belongs to ``chat_id``; ``new=True`` sends a fresh message
        and deletes the old main message. Returns ``False`` when the request was dropped because the user
        already has too many updates in progress (e.g. ``/start`` spam).
        """
        async with self._hold_user(user.telegram_id or user.user_id) as held:
            if not held:
                log.info("show(%s) dropped: user %s is busy", screen, user.user_id)
                return False
            ctx = ScreenCtx(self, user, chat_id, input_mode=new)
            async with timeout_guard(
                f"ui:show:{screen}",
                self.handler_timeout,
                hub=self.hub,
                user_id=user.user_id,
                on_error=lambda exc: self._on_handler_error(ctx),
            ):
                state = await self.ui_state.get(user.user_id)
                if state.chat_id == chat_id and state.main_msg_id is not None:
                    ctx.message_id = state.main_msg_id
                    ctx.shape = state.main_shape or MessageShape("text")
                try:
                    await self._open(ctx, screen, arg, depth=0, toast=toast)
                except TelegramRetryAfter as e:
                    await self._flood(ctx, e)
            return True

    # ------------------------------------------------------------ forms / text input

    async def is_awaiting(self, message: Message) -> bool:
        """True when this message should go to an active form (used as an aiogram filter)."""
        if message.from_user is None or message.chat.type != "private":
            return False
        user = await self._load_user(message.from_user, None)
        if user is None:
            return False
        state = await self.ui_state.get(user.user_id)
        return state.awaiting is not None

    async def dispatch_message(self, message: Message) -> bool:
        """Route a text message into the active form. Returns ``False`` if nothing awaited it.

        A message dropped because the user already has too many updates in progress counts as handled.
        """
        if message.from_user is None:
            return False
        async with self._hold_user(message.from_user.id) as held:
            if not held:
                log.info("input of telegram user %s dropped: busy", message.from_user.id)
                return True
            user = await self._load_user(message.from_user, None)
            if user is None:
                return False
            ctx = ScreenCtx(self, user, message.chat.id, tg_user=message.from_user, input_mode=True)
            state = await self.ui_state.get(user.user_id)
            if state.chat_id == ctx.chat_id and state.main_msg_id is not None:
                ctx.message_id, ctx.shape = state.main_msg_id, state.main_shape or MessageShape("text")
            consumed = state.awaiting is not None  # on failure the input still counts as handled
            async with timeout_guard(
                "ui:input",
                self.handler_timeout,
                hub=self.hub,
                user_id=user.user_id,
                on_error=lambda exc: self._on_handler_error(ctx),
            ):
                try:
                    consumed = await self._form_input(ctx, state.awaiting, message)
                except AccessDeniedError as e:
                    await self.ui_state.set_awaiting(user.user_id, None)
                    await self._deliver(ctx, View(text=texts.t(ctx.lang, "denied")))
                    log.info("access denied: user %s at form:%s", user.user_id, e)
                except TelegramRetryAfter as e:
                    await self._flood(ctx, e)
            return consumed

    async def dispatch_stray_secret(self, message: Message) -> bool:
        """A secret-looking text from staff that no form was waiting for (e.g. the panel token pasted before
        pressing the wizard's button): delete it, mask it in logs, explain. ``False`` = not handled here.

        Ordinary users are left alone: their messages go on to other handlers untouched.
        """
        text = message.text
        if message.from_user is None or message.chat.type != "private" or not looks_like_secret(text):
            return False
        user = await self._load_user(message.from_user, None)
        if user is None or not role_at_least(user.role, STRAY_SECRET_ROLE):
            return False
        assert text is not None
        await self._forget_secret_message(message.chat.id, message.message_id, text)
        async with self._hold_user(message.from_user.id) as held:
            if not held:
                return True  # deleted anyway; the explanation is skipped while the user is busy
            ctx = ScreenCtx(self, user, message.chat.id, tg_user=message.from_user, input_mode=True)
            async with timeout_guard(
                "ui:stray_secret", self.handler_timeout, hub=self.hub, user_id=user.user_id
            ):
                try:
                    await self._deliver(ctx, View(text=texts.t(ctx.lang, "stray_secret")))
                except TelegramRetryAfter as e:
                    await self._flood(ctx, e)
        log.info("a secret-looking message of user %s was deleted (no form was waiting)", user.user_id)
        return True

    async def _forget_secret_message(self, chat_id: int, message_id: int, text: str) -> None:
        register_secret(text.strip())  # keep it out of every log line from now on
        await self._delete_quietly(chat_id, message_id)

    async def _form_input(self, ctx: ScreenCtx, awaiting: dict[str, Any] | None, message: Message) -> bool:
        if awaiting is None:
            return False
        state = forms_mod.FormState.from_json(awaiting)
        form = None if state is None else self.forms.get(state.form)
        if state is None or form is None or state.expired():
            await self.ui_state.set_awaiting(ctx.user.user_id, None)
            text_value = message.text
            if (
                state is not None
                and form is not None
                and _awaits_secret(form, state)
                and text_value
                and not text_value.startswith("/")
            ):
                # The token form timed out, but the message is still the token: never leave it in the chat.
                await self._forget_secret_message(ctx.chat_id, message.message_id, text_value)
                await self._deliver(ctx, View(text=texts.t(ctx.lang, "form_secret_expired")))
                return True
            return False
        text_value = message.text
        if text_value is not None and text_value.startswith("/"):
            command = text_value.split(maxsplit=1)[0].split("@", 1)[0].lower()
            if command == "/cancel":
                await self._cancel_form(ctx, form)
                return True
            await self.ui_state.set_awaiting(ctx.user.user_id, None)  # another command abandons the form
            return False
        if not Access(form.required_role, form.perm).allows(ctx.user):
            await self.ui_state.set_awaiting(ctx.user.user_id, None)
            await self._deliver(ctx, View(text=texts.t(ctx.lang, "denied")))
            return True
        if text_value is None:
            await self._deliver(
                ctx, forms_mod.prompt_view(form, state, ctx.lang, error=texts.t(ctx.lang, "form_text_only"))
            )
            return True
        field_def = form.fields[min(state.step, len(form.fields) - 1)]
        if field_def.secret:
            await self._delete_quietly(ctx.chat_id, message.message_id)
        step = forms_mod.advance(form, state, text_value)
        await self._after_step(ctx, form, step)
        return True

    async def _after_step(self, ctx: ScreenCtx, form: Form, step: forms_mod.StepResult) -> None:
        if step.error is not None:
            await self._deliver(
                ctx, forms_mod.prompt_view(form, step.state, ctx.lang, error=step.error_for(ctx.lang))
            )
            return
        if not step.done:
            await self.ui_state.set_awaiting(ctx.user.user_id, step.state.to_json())
            await self._deliver(ctx, forms_mod.prompt_view(form, step.state, ctx.lang))
            return
        await self.ui_state.set_awaiting(ctx.user.user_id, None)
        data = dict(step.state.data)
        if step.secret:
            data.update(step.secret)
        result = await form.on_done(ctx, data)
        await self._apply_result(ctx, result, depth=0)

    async def _cancel_form(self, ctx: ScreenCtx, form: Form | None) -> None:
        await self.ui_state.set_awaiting(ctx.user.user_id, None)
        result: HandlerResult = None
        if form is not None and form.on_cancel is not None:
            result = await form.on_cancel(ctx)
        if result is None:
            result = Redirect(self.home, toast=texts.t(ctx.lang, "form_cancelled"))
        await self._apply_result(ctx, result, depth=0)

    async def _form_callback(self, ctx: ScreenCtx, action: str) -> None:
        state_row = await self.ui_state.get(ctx.user.user_id)
        state = forms_mod.FormState.from_json(state_row.awaiting)
        form = None if state is None else self.forms.get(state.form)
        if action == "cancel":
            await self._cancel_form(ctx, form)
            return
        if action != "skip" or state is None or form is None or state.expired():
            if state_row.awaiting is not None:
                await self.ui_state.set_awaiting(ctx.user.user_id, None)
            await self._stale(ctx)
            return
        if not Access(form.required_role, form.perm).allows(ctx.user):
            await self.ui_state.set_awaiting(ctx.user.user_id, None)
            await self._allowed(ctx, Access(form.required_role, form.perm), f"form:{form.name}")
            return
        step = forms_mod.skip(form, state)
        if step.error is not None:
            await ctx.answer(step.error_for(ctx.lang))
            return
        await ctx.answer()
        await self._after_step(ctx, form, step)


def _awaits_secret(form: Form, state: forms_mod.FormState) -> bool:
    return form.fields[min(state.step, len(form.fields) - 1)].secret


def _require_async(fn: Callable[..., Any]) -> None:
    if not inspect.iscoroutinefunction(fn):
        raise TypeError(f"{getattr(fn, '__name__', fn)!r} must be an async function")


def _file_id_of(message: Message, kind: str) -> str | None:
    if kind == "photo" and message.photo:
        return message.photo[-1].file_id
    obj = getattr(message, kind, None)
    file_id = getattr(obj, "file_id", None)
    return file_id if isinstance(file_id, str) else None
