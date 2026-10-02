"""Error reports and «Требует внимания» in the admin chat topics (07 §2.4.2, §2.4.3).

* :class:`AdminChatSink` — the :class:`~svbg.core.errors.ErrorSink` of the error hub once an admin chat is
  connected: the first occurrence of an error group is a message in «🚨 Ошибки» (``render_report``: what,
  where, who, what was done, what to check; technical details folded), repeats **edit** the same message
  (the hub throttles them to one per minute). Buttons: «🔕 Заглушить 1 ч / 24 ч» (or «🔔 Включить» while
  muted) and «⚙️ Состояние». Without an admin chat the reports go to the fallback sink (owner DMs).
  A report stays queued in the service until it is delivered even when the hub stops waiting for it
  (``sink_timeout``, e.g. during a Telegram outage): the hub's next attempt for the same group picks up that
  delivery instead of posting the report a second time.
  Delivery failures of the chat itself are never reported back into the hub (the hub's own guard + the
  service only logs them).
* :class:`ErrorActions` — the button handlers. Every click is checked by the screen router: a member of the
  group who is not staff of the bot gets «Нет прав»; muting needs the ``admin`` role, «Состояние» —
  ``system.view`` (the owner has every permission).
* :class:`AttentionRelay` — bus events ``attention.raised`` / ``attention.resolved`` → «⚙️ Система»;
  ``admin_chat.post`` (payload ``kind``, ``text``, optional ``html``, ``priority``) lets other parts post
  without a reference to the service.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Protocol

import sqlalchemy as sa
from aiogram.types import InlineKeyboardButton

from svbg.core import clock as core_clock
from svbg.core.errors import ErrorGroupView, ErrorSink, render_report
from svbg.services.admin_chat import K_ERRORS, K_SYSTEM, MAX_TEXT
from svbg.tg.notifier import Priority
from svbg.tg.ui.renderer import nav_button
from svbg.tg.ui.view import Toast

if TYPE_CHECKING:
    from svbg.core.attention import AttentionItem
    from svbg.core.bus import Event, EventBus
    from svbg.services.admin_chat import AdminChatService, PostResult
    from svbg.tg.ui.router import HandlerResult, ScreenCtx, ScreenRouter

__all__ = [
    "ACTIONS",
    "A_MUTE_1H",
    "A_MUTE_24H",
    "A_STATUS",
    "A_UNMUTE",
    "POST_EVENT",
    "STATUS_SCREEN",
    "AdminChatDeliveryError",
    "AdminChatSink",
    "AttentionRelay",
    "ErrorActions",
    "error_buttons",
]

log = logging.getLogger("svbg.tg.admin_chat")

ACTIONS: Final = "aerr"  # callback namespace of the report buttons
A_MUTE_1H: Final = "m1"
A_MUTE_24H: Final = "m24"
A_UNMUTE: Final = "um"
A_STATUS: Final = "st"
STATUS_SCREEN: Final = "status"  # «⚙️ Состояние» (svbg.tg.admin.status)
PERM_STATUS: Final = "system.view"
POST_EVENT: Final = "admin_chat.post"

_FP_RE: Final = re.compile(r"^[0-9a-f]{40}$")
_MUTES: Final[dict[str, timedelta]] = {A_MUTE_1H: timedelta(hours=1), A_MUTE_24H: timedelta(hours=24)}
_DB_ERRORS: Final[tuple[type[BaseException], ...]] = (sa.exc.SQLAlchemyError, OSError, TimeoutError)
_BODY_MAX: Final = 1500
_INFLIGHT_MAX: Final = 256  # error groups whose first report is still on its way

_T: Final[dict[str, str]] = {
    "mute_1h": "🔕 Заглушить 1 ч",
    "mute_24h": "🔕 24 ч",
    "unmute": "🔔 Включить",
    "status": "⚙️ Состояние",
    "muted": "🔕 Заглушено до {until:%d.%m %H:%M} UTC",
    "unmuted": "🔔 Уведомления об этой ошибке снова включены",
    "gone": "Эта ошибка уже удалена из журнала",
    "stale": "Кнопка устарела",
    "db": "База данных недоступна — попробуйте через минуту",
    "status_sent": "Открыл «Состояние» в личном чате с ботом",
    "status_no_dm": "Напишите боту в личку /start, чтобы открыть «Состояние»",
    "attention": "{icon} <b>Требует внимания:</b> {title}",
    "resolved": "✅ <b>Решено:</b> {title}",
}
_SEVERITY: Final[dict[str, tuple[str, Priority]]] = {
    "error": ("🔴", Priority.HIGH),
    "warn": ("🟠", Priority.NORMAL),
    "info": ("ℹ️", Priority.LOW),
}


class AdminChatDeliveryError(RuntimeError):
    """A report update could not be delivered (the hub retries it later)."""


def error_buttons(view: ErrorGroupView) -> list[list[InlineKeyboardButton]]:
    """Buttons under a report: mute 1 h / 24 h (or unmute while muted) and «Состояние»."""
    rows: list[list[InlineKeyboardButton]] = []
    fp = view.fingerprint
    if _FP_RE.match(fp):
        if view.status == "muted" and view.muted_until is not None:
            rows.append([nav_button(_T["unmute"], ACTIONS, A_UNMUTE, fp)])
        else:
            rows.append(
                [
                    nav_button(_T["mute_1h"], ACTIONS, A_MUTE_1H, fp),
                    nav_button(_T["mute_24h"], ACTIONS, A_MUTE_24H, fp),
                ]
            )
    rows.append([nav_button(_T["status"], ACTIONS, A_STATUS)])
    return rows


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class AdminChatSink:
    """:class:`~svbg.core.errors.ErrorSink` into «🚨 Ошибки»; ``fallback`` (owner DMs) without an admin chat.

    ``msg_ref``: ``{"chat": <chat_id>, "msg": <message_id>}``; ``{"dm": {...}}`` when the report went to the
    owners' DMs (fallback sink or the service's own DM fallback); ``{"dropped": true}`` when the owner
    disabled «Ошибки» with «не отправлять».
    """

    def __init__(self, service: AdminChatService, fallback: ErrorSink | None = None) -> None:
        self._service = service
        self._fallback = fallback
        # (fingerprint, episode) → the delivery of its first report. Shielded from the hub's timeout so a
        # report already in the queue (or being sent) is not abandoned and then posted again by the retry.
        self._inflight: OrderedDict[tuple[str, str], asyncio.Task[PostResult | None]] = OrderedDict()

    async def send_new(self, view: ErrorGroupView) -> dict[str, Any] | None:
        if not self._service.configured:
            return await self._fallback.send_new(view) if self._fallback is not None else None
        key = (view.fingerprint, view.episode_started_at.isoformat() if view.episode_started_at else "")
        task = self._inflight.get(key)
        if task is None or (task.done() and _failed(task)):
            task = asyncio.ensure_future(self._post_new(view))
            task.add_done_callback(_consume)
            self._inflight[key] = task
            self._inflight.move_to_end(key)
            while len(self._inflight) > _INFLIGHT_MAX:
                _, old = self._inflight.popitem(last=False)
                old.cancel()  # its item is then abandoned and dropped from the queue
        result = await asyncio.shield(task)  # the hub's timeout cancels this wait, not the delivery
        if self._inflight.get(key) is task:
            del self._inflight[key]
        if result is None:
            return None
        if result.dropped:
            return {"dropped": True}
        if result.chat_id is not None and result.message_id is not None:
            return {"chat": result.chat_id, "msg": result.message_id}
        if result.dm:
            return {"dm": {str(chat): msg for chat, msg in result.dm.items()}}
        return None

    async def _post_new(self, view: ErrorGroupView) -> PostResult | None:
        return await self._service.post(
            K_ERRORS,
            render_report(view),
            html=True,
            buttons=error_buttons(view),
            priority=Priority.CRITICAL,
            wait=True,
        )

    async def update(self, view: ErrorGroupView, msg_ref: Any) -> None:
        if not isinstance(msg_ref, Mapping) or msg_ref.get("dropped"):
            return
        text, buttons = render_report(view), error_buttons(view)
        dm = msg_ref.get("dm")
        if isinstance(dm, Mapping):
            if self._fallback is not None:
                await self._fallback.update(view, msg_ref)
                return
            targets = [(int(chat), _int(msg)) for chat, msg in dm.items() if str(chat).lstrip("-").isdigit()]
            for chat_id, message_id in targets:
                if message_id is not None:
                    await self._service.edit(
                        chat_id, message_id, text, html=True, buttons=buttons, priority=Priority.CRITICAL
                    )
            return
        chat_id, message_id = _int(msg_ref.get("chat")), _int(msg_ref.get("msg"))
        if chat_id is None or message_id is None:
            return
        result = await self._service.edit(
            chat_id,
            message_id,
            text,
            html=True,
            buttons=buttons,
            priority=Priority.CRITICAL,
            resend_kind=K_ERRORS,
            wait=True,
        )
        if result is None:
            raise AdminChatDeliveryError("error report update was not delivered")


def _failed(task: asyncio.Task[PostResult | None]) -> bool:
    return task.cancelled() or task.exception() is not None or task.result() is None


def _consume(task: asyncio.Task[Any]) -> None:
    if not task.cancelled() and task.exception() is not None:  # retrieved: no "never retrieved" warning
        log.warning("error report delivery failed: %s", type(task.exception()).__name__)


class _Hub(Protocol):
    async def mute(self, fp: str, until: datetime) -> bool: ...

    async def unmute(self, fp: str) -> bool: ...

    async def get(self, fp: str) -> ErrorGroupView | None: ...


class ErrorActions:
    """Handlers of the report buttons (role checked by the router on every click)."""

    def __init__(
        self,
        router: ScreenRouter,
        hub: _Hub,
        service: AdminChatService,
        *,
        clock: Callable[[], datetime] = core_clock.now,
    ) -> None:
        self._router = router
        self._hub = hub
        self._service = service
        self._clock = clock
        self._tasks: set[asyncio.Task[Any]] = set()

    def install(self) -> None:
        r = self._router
        r.action(ACTIONS, A_MUTE_1H, required_role="admin")(self._mute_1h)
        r.action(ACTIONS, A_MUTE_24H, required_role="admin")(self._mute_24h)
        r.action(ACTIONS, A_UNMUTE, required_role="admin")(self._unmute)
        r.action(ACTIONS, A_STATUS, required_role="admin", perm=PERM_STATUS)(self._status)

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.wait(list(self._tasks))

    async def _mute_1h(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        return await self._mute(ctx, arg, A_MUTE_1H)

    async def _mute_24h(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        return await self._mute(ctx, arg, A_MUTE_24H)

    async def _mute(self, ctx: ScreenCtx, arg: Any, action: str) -> HandlerResult:
        fp = arg if isinstance(arg, str) and _FP_RE.match(arg) else None
        if fp is None:
            return Toast(_T["stale"])
        until = self._clock() + _MUTES[action]
        try:
            found = await self._hub.mute(fp, until)
        except _DB_ERRORS as exc:
            log.warning("cannot mute error group %s: %s", fp[:12], type(exc).__name__)
            return Toast(_T["db"], alert=True)
        if not found:
            return Toast(_T["gone"])
        log.info("error group %s muted until %s by user %s", fp[:12], until.isoformat(), ctx.user.user_id)
        await self._refresh(ctx, fp)
        return Toast(_T["muted"].format(until=until))

    async def _unmute(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        fp = arg if isinstance(arg, str) and _FP_RE.match(arg) else None
        if fp is None:
            return Toast(_T["stale"])
        try:
            found = await self._hub.unmute(fp)
        except _DB_ERRORS as exc:
            log.warning("cannot unmute error group %s: %s", fp[:12], type(exc).__name__)
            return Toast(_T["db"], alert=True)
        if not found:
            return Toast(_T["gone"])
        await self._refresh(ctx, fp)
        return Toast(_T["unmuted"])

    async def _refresh(self, ctx: ScreenCtx, fp: str) -> None:
        """Re-render the report under the clicked button (mute state + buttons), through the queue."""
        if ctx.message_id is None:
            return
        try:
            view = await self._hub.get(fp)
        except _DB_ERRORS as exc:
            log.warning("cannot reload error group %s: %s", fp[:12], type(exc).__name__)
            return
        if view is None:
            return
        await self._service.edit(
            ctx.chat_id,
            ctx.message_id,
            render_report(view),
            html=True,
            buttons=error_buttons(view),
            priority=Priority.HIGH,
        )

    async def _status(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        """Open «Состояние» in the clicker's private chat (never inside the group, never over the report)."""
        tg_id = ctx.user.telegram_id
        if tg_id is None:
            return Toast(_T["status_no_dm"])
        user = ctx.user
        # The router holds this user's lock until the click is answered: show the screen right after.
        self._spawn(self._router.show(user, tg_id, STATUS_SCREEN, new=True), "admin-chat-status")
        return Toast(_T["status_sent"])

    def _spawn(self, coro: Awaitable[Any], name: str) -> None:
        task = asyncio.ensure_future(coro)
        task.set_name(name)
        self._tasks.add(task)

        def done(t: asyncio.Task[Any]) -> None:
            self._tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                log.warning("%s failed: %s", name, type(t.exception()).__name__)

        task.add_done_callback(done)


class _Attention(Protocol):
    async def get_by_id(self, id: int) -> AttentionItem | None: ...


class AttentionRelay:
    """Bus → admin chat: «Требует внимания» items and generic ``admin_chat.post`` events."""

    def __init__(self, service: AdminChatService, attention: _Attention | None) -> None:
        self._service = service
        self._attention = attention

    def install(self, bus: EventBus) -> Callable[[], None]:
        """Subscribe; returns a function that unsubscribes everything."""
        offs = [bus.subscribe(POST_EVENT, self.on_post)]
        if self._attention is not None:
            offs.append(bus.subscribe("attention.raised", self.on_raised))
            offs.append(bus.subscribe("attention.resolved", self.on_resolved))

        def off() -> None:
            for unsubscribe in offs:
                unsubscribe()

        return off

    async def _item(self, event: Event) -> AttentionItem | None:
        item_id = _int(event.payload.get("id"))
        if item_id is None or self._attention is None:
            return None
        try:
            return await self._attention.get_by_id(item_id)
        except _DB_ERRORS as exc:
            log.warning("attention relay: cannot load item %s (%s)", item_id, type(exc).__name__)
            return None

    async def on_raised(self, event: Event) -> None:
        item = await self._item(event)
        if item is None or not item.is_open:
            return
        icon, priority = _SEVERITY.get(item.severity, ("⚠️", Priority.NORMAL))
        lines = [_T["attention"].format(icon=icon, title=html.escape(item.title, quote=False))]
        if item.body:
            body = item.body if len(item.body) <= _BODY_MAX else item.body[: _BODY_MAX - 1] + "…"
            lines.append(html.escape(body, quote=False))
        buttons = [[nav_button(_T["status"], ACTIONS, A_STATUS)]]
        await self._service.post(K_SYSTEM, "\n".join(lines), html=True, buttons=buttons, priority=priority)

    async def on_resolved(self, event: Event) -> None:
        item = await self._item(event)
        if item is None:
            return
        text = _T["resolved"].format(title=html.escape(item.title, quote=False))
        await self._service.post(K_SYSTEM, text, html=True, priority=Priority.LOW)

    async def on_post(self, event: Event) -> None:
        p = event.payload
        kind, text = p.get("kind"), p.get("text")
        if not isinstance(kind, str) or not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
            log.warning("admin_chat.post event ignored: needs str kind and a non-empty text ≤ 4096")
            return
        raw_priority = p.get("priority")
        priority: Priority | None = None
        if isinstance(raw_priority, str) and raw_priority.upper() in Priority.__members__:
            priority = Priority[raw_priority.upper()]
        await self._service.post(kind, text, html=bool(p.get("html")), priority=priority)
