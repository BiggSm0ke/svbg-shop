"""Fake Telegram Bot API server (aiohttp) for tests.

Compatible with ``TELEGRAM_API_URL`` / ``TelegramAPIServer.from_base(fake.url)``: requests go to
``/bot<token>/<method>``. Every call is recorded (:attr:`FakeTelegram.calls`). The fake models just
enough of Telegram to exercise the runner and UI realistically:

* several bots; one bot may have several tokens (``/revoke`` rotation keeps the bot id);
* ``getUpdates`` long polling with offsets, ``409`` when a webhook is set or a second poller arrives;
* ``setWebhook`` makes the fake push updates to the URL with ``X-Telegram-Bot-Api-Secret-Token``;
* realistic ``Message`` JSON that echoes ``entities`` / ``reply_markup`` (incl. button ``style`` and
  ``icon_custom_emoji_id``) so renderers can be asserted on;
* injectable faults (``429`` with ``retry_after``, ``403``, ``5xx``, ``400``), latency, blocked chats and
  optional Telegram-like flood limits;
* payments in the chat: ``createInvoiceLink`` (links are remembered in :attr:`FakeTelegram.invoices`),
  ``answerPreCheckoutQuery`` (answers in :attr:`FakeTelegram.pre_checkout_answers`), and users who send a
  ``pre_checkout_query`` / ``successful_payment`` / a photo or document (:meth:`push_pre_checkout`,
  :meth:`push_successful_payment`, :meth:`push_photo`), ``chat_member`` updates (:meth:`push_chat_member`);
* forum topics: ``createForumTopic`` / ``editForumTopic`` / ``closeForumTopic`` / ``reopenForumTopic`` /
  ``deleteForumTopic`` / ``getForumTopicIconStickers``; a topic deleted by a human (:meth:`delete_topic`)
  makes sends into it fail with ``message thread not found``; :attr:`chats` overrides ``getChat`` (e.g. a
  group without topics) and :meth:`make_admin` gives a member administrator rights (``createForumTopic``
  then requires ``can_manage_topics`` of the bot).

Usage::

    async with FakeTelegram() as tg:
        token = tg.add_bot(username="svbg_bot")
        bot = Bot(token, session=AiohttpSession(api=TelegramAPIServer.from_base(tg.url)))
        tg.push_message(user_id=1, text="/start")
        call = await tg.wait_for("sendMessage", lambda c: c.params["chat_id"] == 1)
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import secrets
import time
from collections import defaultdict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from aiohttp import ClientError, ClientSession, ClientTimeout, web

FaultKind = Literal["429", "403", "400", "409", "500", "502", "timeout"]

_JSON_FIELDS = frozenset(
    {
        "reply_markup",
        "entities",
        "caption_entities",
        "allowed_updates",
        "media",
        "link_preview_options",
        "reply_parameters",
        "results",
        "prices",
    }
)
_INT_FIELDS = frozenset(
    {
        "chat_id",
        "message_id",
        "message_thread_id",
        "offset",
        "limit",
        "timeout",
        "user_id",
        "from_chat_id",
        "icon_color",
        "max_connections",
        "cache_time",
    }
)
_BOOL_FIELDS = frozenset(
    {
        "drop_pending_updates",
        "show_alert",
        "disable_notification",
        "protect_content",
        "has_spoiler",
        "show_caption_above_media",
    }
)

_FILE_SIZES = ((90, 51), (320, 180), (800, 450))


@dataclass(slots=True)
class Call:
    """One recorded Bot API request."""

    bot_id: int | None
    token: str
    method: str
    params: dict[str, Any]
    ts: float
    status: int = 200
    result: Any = None
    description: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == 200


@dataclass(slots=True)
class Fault:
    """An injected failure, consumed by matching calls."""

    kind: FaultKind
    method: str | None = None
    chat_id: int | None = None
    retry_after: int = 1
    description: str | None = None
    remaining: int = 1
    delay: float = 0.0

    def matches(self, method: str, params: Mapping[str, Any]) -> bool:
        if self.remaining <= 0:
            return False
        if self.method is not None and self.method.lower() != method.lower():
            return False
        return self.chat_id is None or params.get("chat_id") == self.chat_id


@dataclass
class _Bot:
    bot_id: int
    username: str
    first_name: str
    updates: list[dict[str, Any]] = field(default_factory=list)
    update_ids: itertools.count[int] = field(default_factory=lambda: itertools.count(1))
    cond: asyncio.Condition = field(default_factory=asyncio.Condition)
    poller: object | None = None
    webhook_url: str = ""
    webhook_secret: str | None = None
    webhook_allowed: list[str] | None = None
    webhook_last_error: str | None = None
    webhook_task: asyncio.Task[None] | None = None
    message_ids: dict[int, itertools.count[int]] = field(default_factory=dict)
    messages: dict[tuple[int, int], dict[str, Any]] = field(default_factory=dict)
    topics: itertools.count[int] = field(default_factory=lambda: itertools.count(100))
    forum_topics: dict[tuple[int, int], dict[str, Any]] = field(default_factory=dict)

    def user_json(self) -> dict[str, Any]:
        return {
            "id": self.bot_id,
            "is_bot": True,
            "first_name": self.first_name,
            "username": self.username,
            "can_join_groups": True,
            "can_read_all_group_messages": False,
            "supports_inline_queries": False,
        }

    def next_message_id(self, chat_id: int) -> int:
        counter = self.message_ids.setdefault(chat_id, itertools.count(1))
        return next(counter)


class _ApiError(Exception):
    def __init__(self, code: int, description: str, parameters: dict[str, Any] | None = None) -> None:
        super().__init__(description)
        self.code = code
        self.description = description
        self.parameters = parameters


def _chat_json(chat_id: int) -> dict[str, Any]:
    if chat_id > 0:
        return {"id": chat_id, "type": "private", "first_name": f"User{chat_id}"}
    return {"id": chat_id, "type": "supergroup", "title": f"Group {chat_id}", "is_forum": True}


# A few icons of Telegram's forum icon set (emoji -> custom emoji id), enough to match the bot's topics.
FORUM_ICONS: dict[str, str] = {
    "💰": "5350452584119279096",
    "💸": "5350409016063823917",
    "🎉": "5377498341074542641",
    "👀": "5418085807791545980",
    "📝": "5373251851074415873",
    "❗️": "5379748062124056162",
    "🔥": "5312241539987020022",
    "📈": "5350305691942788490",
    "💻": "5350554349074391003",
    "🤖": "5309832892262654231",
    "💡": "5312536423851630001",
    "💬": "5417915203100613993",
    "🧳": "5348227245599105972",
}

_ADMIN_FALSE_FIELDS = (
    "can_be_edited",
    "is_anonymous",
    "can_manage_chat",
    "can_delete_messages",
    "can_manage_video_chats",
    "can_restrict_members",
    "can_promote_members",
    "can_change_info",
    "can_invite_users",
    "can_post_stories",
    "can_edit_stories",
    "can_delete_stories",
    "can_send_welcome_messages",
    "can_pin_messages",
    "can_manage_topics",
)


def _user_json(user_id: int) -> dict[str, Any]:
    return {"id": user_id, "is_bot": False, "first_name": f"User{user_id}", "language_code": "ru"}


class FakeTelegram:
    """Fake Bot API server bound to 127.0.0.1 on a random port."""

    def __init__(
        self,
        *,
        enforce_limits: bool = False,
        group_limit: int = 20,
        group_window: float = 60.0,
        global_limit: int = 30,
        global_window: float = 1.0,
        webhook_retry_delay: float = 0.2,
    ) -> None:
        self.calls: list[Call] = []
        self.latency: float = 0.0
        self.method_latency: dict[str, float] = {}
        self.blocked_chats: set[int] = set()
        self.chat_members: dict[tuple[int, int], str] = {}
        self.member_rights: dict[tuple[int, int], dict[str, bool]] = {}
        self.chats: dict[int, dict[str, Any]] = {}
        self.forum_icons: dict[str, str] = dict(FORUM_ICONS)
        #: ``createInvoiceLink`` results: link → the request parameters (payload, currency, prices…).
        self.invoices: dict[str, dict[str, Any]] = {}
        #: ``answerPreCheckoutQuery``: query id → ``(ok, error_message)``.
        self.pre_checkout_answers: dict[str, tuple[bool, str | None]] = {}
        self.enforce_limits = enforce_limits
        self.group_limit = group_limit
        self.group_window = group_window
        self.global_limit = global_limit
        self.global_window = global_window
        self.flood_errors = 0
        self.conflicts = 0
        self.webhook_retry_delay = webhook_retry_delay
        self._faults: list[Fault] = []
        self._tokens: dict[str, int] = {}
        self._bots: dict[int, _Bot] = {}
        self._bot_ids = itertools.count(7_000_000_001)
        self._callback_ids = itertools.count(1)
        self._file_ids = itertools.count(1)
        self._sent_global: deque[float] = deque()
        self._sent_chat: dict[int, deque[float]] = defaultdict(deque)
        self._calls_cond = asyncio.Condition()
        self._runner: web.AppRunner | None = None
        self._client: ClientSession | None = None
        self._port = 0

    # ----------------------------------------------------------------- lifecycle

    @property
    def url(self) -> str:
        if not self._port:
            raise RuntimeError("FakeTelegram is not started")
        return f"http://127.0.0.1:{self._port}"

    async def start(self) -> None:
        app = web.Application(client_max_size=50 * 1024 * 1024)
        app.router.add_route("*", "/bot{token}/{method}", self._handle)
        self._runner = web.AppRunner(app, handler_cancellation=True, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        server = site._server  # aiohttp does not expose the bound port publicly
        assert server is not None
        self._port = server.sockets[0].getsockname()[1]  # type: ignore[attr-defined]
        self._client = ClientSession(timeout=ClientTimeout(total=5))

    async def stop(self) -> None:
        for bot in self._bots.values():
            if bot.webhook_task is not None:
                bot.webhook_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await bot.webhook_task
                bot.webhook_task = None
        if self._client is not None:
            await self._client.close()
            self._client = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def __aenter__(self) -> FakeTelegram:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # ----------------------------------------------------------------- bots & tokens

    def add_bot(
        self,
        *,
        username: str = "svbg_test_bot",
        first_name: str = "SvBG Test",
        bot_id: int | None = None,
        token: str | None = None,
    ) -> str:
        """Create a bot (or a new token for an existing ``bot_id``) and return its token.

        ``token`` registers a given token (its numeric prefix is the bot id) — handy to serve the same
        bot from two fake servers.
        """
        if token is not None:
            bot_id = int(token.split(":", 1)[0])
        if bot_id is None:
            bot_id = next(self._bot_ids)
        if bot_id not in self._bots:
            self._bots[bot_id] = _Bot(bot_id=bot_id, username=username, first_name=first_name)
        token = token or f"{bot_id}:{secrets.token_urlsafe(26)[:35]}"
        self._tokens[token] = bot_id
        return token

    def rotate_token(self, token: str) -> str:
        """Like BotFather ``/revoke``: the old token stops working, a new one for the same bot is issued."""
        bot_id = self._tokens.pop(token)
        return self.add_bot(bot_id=bot_id)

    def revoke(self, token: str) -> None:
        self._tokens.pop(token, None)

    def bot_id(self, token: str) -> int:
        return int(token.split(":", 1)[0])

    def _bot(self, bot_id: int | None) -> _Bot:
        if bot_id is None:
            if len(self._bots) != 1:
                raise ValueError("several bots exist: pass bot_id explicitly")
            return next(iter(self._bots.values()))
        return self._bots[bot_id]

    def webhook_url(self, bot_id: int | None = None) -> str:
        return self._bot(bot_id).webhook_url

    def pending_updates(self, bot_id: int | None = None) -> list[dict[str, Any]]:
        return list(self._bot(bot_id).updates)

    def message(self, chat_id: int, message_id: int, *, bot_id: int | None = None) -> dict[str, Any] | None:
        return self._bot(bot_id).messages.get((chat_id, message_id))

    # ----------------------------------------------------------------- chats, admins, topics

    def make_admin(self, chat_id: int, user_id: int, **rights: bool) -> None:
        """Make ``user_id`` (e.g. the bot) an administrator of ``chat_id`` with the given ``can_*`` rights."""
        self.chat_members[(chat_id, user_id)] = "administrator"
        self.member_rights[(chat_id, user_id)] = dict(rights)

    def topics(self, chat_id: int, *, bot_id: int | None = None) -> dict[int, dict[str, Any]]:
        """Forum topics of ``chat_id`` created through the API: thread id -> state (deleted ones included)."""
        bot = self._bot(bot_id)
        return {thread: t for (chat, thread), t in bot.forum_topics.items() if chat == chat_id}

    def delete_topic(self, chat_id: int, thread_id: int, *, bot_id: int | None = None) -> None:
        """A human deleted the topic: later sends into it fail with ``message thread not found``."""
        bot = self._bot(bot_id)
        topic = bot.forum_topics.setdefault(
            (chat_id, thread_id),
            {"message_thread_id": thread_id, "name": "?", "icon_color": 7322096, "closed": False},
        )
        topic["deleted"] = True

    # ----------------------------------------------------------------- update injection

    def push_update(self, body: Mapping[str, Any], *, bot_id: int | None = None) -> dict[str, Any]:
        """Queue a raw update (``update_id`` is assigned) for polling or webhook delivery."""
        bot = self._bot(bot_id)
        update = {"update_id": next(bot.update_ids), **body}
        bot.updates.append(update)
        self._wake(bot)
        return update

    def push_message(
        self,
        user_id: int,
        text: str,
        *,
        chat_id: int | None = None,
        entities: list[dict[str, Any]] | None = None,
        bot_id: int | None = None,
    ) -> dict[str, Any]:
        bot = self._bot(bot_id)
        chat = chat_id if chat_id is not None else user_id
        msg: dict[str, Any] = {
            "message_id": bot.next_message_id(chat),
            "date": int(time.time()),
            "chat": _chat_json(chat),
            "from": _user_json(user_id),
            "text": text,
        }
        if entities is not None:
            msg["entities"] = entities
        elif text.startswith("/"):
            cmd = text.split(maxsplit=1)[0]
            msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(cmd)}]
        bot.messages[(chat, msg["message_id"])] = msg
        return self.push_update({"message": msg}, bot_id=bot.bot_id)

    def push_callback(
        self,
        user_id: int,
        data: str,
        message_id: int,
        *,
        chat_id: int | None = None,
        bot_id: int | None = None,
    ) -> dict[str, Any]:
        bot = self._bot(bot_id)
        chat = chat_id if chat_id is not None else user_id
        message = bot.messages.get((chat, message_id)) or {
            "message_id": message_id,
            "date": int(time.time()),
            "chat": _chat_json(chat),
            "from": bot.user_json(),
            "text": "…",
        }
        cq = {
            "id": str(next(self._callback_ids)),
            "from": _user_json(user_id),
            "chat_instance": str(chat),
            "message": message,
            "data": data,
        }
        return self.push_update({"callback_query": cq}, bot_id=bot.bot_id)

    def _user_message(self, bot: _Bot, user_id: int, chat_id: int | None = None) -> dict[str, Any]:
        chat = chat_id if chat_id is not None else user_id
        return {
            "message_id": bot.next_message_id(chat),
            "date": int(time.time()),
            "chat": _chat_json(chat),
            "from": _user_json(user_id),
        }

    def push_pre_checkout(
        self, user_id: int, payload: str, *, currency: str, total_amount: int, bot_id: int | None = None
    ) -> str:
        """The user pressed «Pay» in an invoice; returns the query id (see ``pre_checkout_answers``)."""
        query_id = f"pcq{next(self._callback_ids)}"
        self.push_update(
            {
                "pre_checkout_query": {
                    "id": query_id,
                    "from": _user_json(user_id),
                    "currency": currency,
                    "total_amount": total_amount,
                    "invoice_payload": payload,
                }
            },
            bot_id=bot_id,
        )
        return query_id

    def push_successful_payment(
        self,
        user_id: int,
        payload: str,
        *,
        currency: str,
        total_amount: int,
        charge_id: str,
        bot_id: int | None = None,
    ) -> dict[str, Any]:
        """Telegram's service message after the money moved (Stars: ``currency='XTR'``)."""
        bot = self._bot(bot_id)
        msg = self._user_message(bot, user_id)
        msg["successful_payment"] = {
            "currency": currency,
            "total_amount": total_amount,
            "invoice_payload": payload,
            "telegram_payment_charge_id": charge_id,
            "provider_payment_charge_id": "",
        }
        return self.push_update({"message": msg}, bot_id=bot.bot_id)

    def push_photo(
        self, user_id: int, file_id: str, *, caption: str | None = None, bot_id: int | None = None
    ) -> dict[str, Any]:
        """A photo from the user (``file_id`` is what the bot later re-sends, e.g. a receipt)."""
        bot = self._bot(bot_id)
        msg = self._user_message(bot, user_id)
        msg["photo"] = [
            {
                "file_id": file_id,
                "file_unique_id": f"u-{file_id}",
                "width": 800,
                "height": 450,
                "file_size": 9,
            }
        ]
        if caption is not None:
            msg["caption"] = caption
        bot.messages[(user_id, msg["message_id"])] = msg
        return self.push_update({"message": msg}, bot_id=bot.bot_id)

    def push_chat_member(
        self, chat_id: int, user_id: int, *, old: str, new: str, bot_id: int | None = None
    ) -> dict[str, Any]:
        """A member of ``chat_id`` changed status (e.g. ``member`` → ``left``); the bot must be an admin."""
        self.chat_members[(chat_id, user_id)] = new
        member = _user_json(user_id)
        return self.push_update(
            {
                "chat_member": {
                    "chat": {"id": chat_id, "type": "channel", "title": f"Channel {chat_id}"},
                    "from": member,
                    "date": int(time.time()),
                    "old_chat_member": {"status": old, "user": member},
                    "new_chat_member": {"status": new, "user": member},
                }
            },
            bot_id=bot_id,
        )

    def _wake(self, bot: _Bot) -> None:
        async def notify() -> None:
            async with bot.cond:
                bot.cond.notify_all()

        task = asyncio.get_running_loop().create_task(notify())
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        if bot.webhook_url:
            self._ensure_webhook_task(bot)

    # ----------------------------------------------------------------- faults

    def fail_next(
        self,
        kind: FaultKind,
        *,
        method: str | None = None,
        chat_id: int | None = None,
        count: int = 1,
        retry_after: int = 1,
        description: str | None = None,
        delay: float = 0.0,
    ) -> Fault:
        """Make the next ``count`` matching calls fail with ``kind``."""
        fault = Fault(
            kind=kind,
            method=method,
            chat_id=chat_id,
            retry_after=retry_after,
            description=description,
            remaining=count,
            delay=delay,
        )
        self._faults.append(fault)
        return fault

    def clear_faults(self) -> None:
        self._faults.clear()

    # ----------------------------------------------------------------- inspection

    def calls_for(self, method: str, *, bot_id: int | None = None, token: str | None = None) -> list[Call]:
        m = method.lower()
        return [
            c
            for c in self.calls
            if c.method.lower() == m
            and (bot_id is None or c.bot_id == bot_id)
            and (token is None or c.token == token)
        ]

    async def wait_for(
        self,
        method: str,
        predicate: Callable[[Call], bool] | None = None,
        timeout: float = 5.0,
        *,
        start: int = 0,
        ok_only: bool = False,
    ) -> Call:
        """Wait until a call of ``method`` (index >= ``start``) matching ``predicate`` is recorded."""
        m = method.lower()

        def find() -> Call | None:
            for call in self.calls[start:]:
                if call.method.lower() != m or (ok_only and not call.ok):
                    continue
                if predicate is None or predicate(call):
                    return call
            return None

        async with asyncio.timeout(timeout), self._calls_cond:
            await self._calls_cond.wait_for(lambda: find() is not None)
        found = find()
        assert found is not None
        return found

    async def _record(self, call: Call) -> None:
        self.calls.append(call)
        async with self._calls_cond:
            self._calls_cond.notify_all()

    # ----------------------------------------------------------------- HTTP

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        token = request.match_info["token"]
        method = request.match_info["method"]
        params = await self._read_params(request)
        bot_id = self._tokens.get(token)
        call = Call(bot_id=bot_id, token=token, method=method, params=params, ts=time.monotonic())
        try:
            delay = self.method_latency.get(method, self.latency)
            if delay:
                await asyncio.sleep(delay)
            if bot_id is None:
                raise _ApiError(401, "Unauthorized")
            await self._apply_fault(method, params)
            handler = getattr(self, f"_m_{method.lower()}", None)
            if handler is None:
                raise _ApiError(404, "Not Found: method not found")
            bot = self._bots[bot_id]
            self._check_limits(method, params)
            result = await handler(bot, params)
        except _ApiError as exc:
            call.status, call.description = exc.code, exc.description
            await self._record(call)
            body: dict[str, Any] = {"ok": False, "error_code": exc.code, "description": exc.description}
            if exc.parameters:
                body["parameters"] = exc.parameters
            return web.json_response(body, status=exc.code)
        call.result = result
        await self._record(call)
        return web.json_response({"ok": True, "result": result})

    async def _read_params(self, request: web.Request) -> dict[str, Any]:
        raw: dict[str, Any] = {}
        if request.content_type == "application/json":
            raw = dict(await request.json())
        elif request.can_read_body:
            form = await request.post()
            for key, value in form.items():
                if isinstance(value, web.FileField):
                    raw[key] = {"upload": value.filename, "size": len(value.file.read())}
                else:
                    raw[key] = value
        raw.update({k: v for k, v in request.query.items() if k not in raw})
        return {k: self._decode(k, v) for k, v in raw.items()}

    @staticmethod
    def _decode(key: str, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        if key in _JSON_FIELDS:
            try:
                return json.loads(value)
            except ValueError:
                return value
        if key in _INT_FIELDS:
            try:
                return int(value)
            except ValueError:
                return value
        if key in _BOOL_FIELDS:
            return value.lower() in {"true", "1"}
        return value

    async def _apply_fault(self, method: str, params: Mapping[str, Any]) -> None:
        for fault in self._faults:
            if not fault.matches(method, params):
                continue
            fault.remaining -= 1
            if fault.delay:
                await asyncio.sleep(fault.delay)
            match fault.kind:
                case "429":
                    raise _ApiError(
                        429,
                        fault.description or f"Too Many Requests: retry after {fault.retry_after}",
                        {"retry_after": fault.retry_after},
                    )
                case "403":
                    raise _ApiError(403, fault.description or "Forbidden: bot was blocked by the user")
                case "400":
                    raise _ApiError(400, fault.description or "Bad Request: chat not found")
                case "409":
                    raise _ApiError(
                        409, fault.description or "Conflict: terminated by other getUpdates request"
                    )
                case "500" | "502":
                    raise _ApiError(int(fault.kind), fault.description or "Internal Server Error")
                case "timeout":
                    await asyncio.sleep(3600)
            return

    def _check_limits(self, method: str, params: Mapping[str, Any]) -> None:
        if not self.enforce_limits or not method.lower().startswith(("send", "copy", "forward")):
            return
        now = time.monotonic()
        while self._sent_global and now - self._sent_global[0] >= self.global_window:
            self._sent_global.popleft()
        if len(self._sent_global) >= self.global_limit:
            self.flood_errors += 1
            raise _ApiError(429, "Too Many Requests: retry after 1", {"retry_after": 1})
        chat_id = params.get("chat_id")
        if isinstance(chat_id, int) and chat_id < 0:
            window = self._sent_chat[chat_id]
            while window and now - window[0] >= self.group_window:
                window.popleft()
            if len(window) >= self.group_limit:
                self.flood_errors += 1
                retry = max(1, int(self.group_window - (now - window[0])) + 1)
                raise _ApiError(429, f"Too Many Requests: retry after {retry}", {"retry_after": retry})
            window.append(now)
        self._sent_global.append(now)

    # ----------------------------------------------------------------- message helpers

    def _new_message(self, bot: _Bot, params: Mapping[str, Any], **content: Any) -> dict[str, Any]:
        chat_id = self._chat_id(params)
        if chat_id in self.blocked_chats:
            raise _ApiError(403, "Forbidden: bot was blocked by the user")
        msg: dict[str, Any] = {
            "message_id": bot.next_message_id(chat_id),
            "date": int(time.time()),
            "chat": _chat_json(chat_id),
            "from": bot.user_json(),
        }
        if (thread := params.get("message_thread_id")) is not None:
            topic = bot.forum_topics.get((chat_id, thread))
            if topic is not None and topic.get("deleted"):
                raise _ApiError(400, "Bad Request: message thread not found")
            msg["message_thread_id"] = thread
            msg["is_topic_message"] = True
        msg.update({k: v for k, v in content.items() if v is not None})
        if (markup := params.get("reply_markup")) is not None and "inline_keyboard" in markup:
            msg["reply_markup"] = markup
        bot.messages[(chat_id, msg["message_id"])] = msg
        return msg

    @staticmethod
    def _chat_id(params: Mapping[str, Any]) -> int:
        chat_id = params.get("chat_id")
        if not isinstance(chat_id, int):
            raise _ApiError(400, "Bad Request: chat not found")
        return chat_id

    def _existing(self, bot: _Bot, params: Mapping[str, Any], action: str) -> dict[str, Any]:
        key = (self._chat_id(params), params.get("message_id"))
        msg = bot.messages.get(key)  # type: ignore[arg-type]
        if msg is None:
            raise _ApiError(400, f"Bad Request: message to {action} not found")
        return msg

    def _photo(self, media: Any) -> list[dict[str, Any]]:
        n = next(self._file_ids)
        base = media if isinstance(media, str) and not media.startswith(("attach://", "http")) else f"F{n}"
        return [
            {
                "file_id": f"{base}_{w}",
                "file_unique_id": f"U{n}_{w}",
                "width": w,
                "height": h,
                "file_size": w * h // 10,
            }
            for w, h in _FILE_SIZES
        ]

    # ----------------------------------------------------------------- methods

    async def _m_getme(self, bot: _Bot, params: Mapping[str, Any]) -> Any:
        return bot.user_json()

    async def _m_getupdates(self, bot: _Bot, params: Mapping[str, Any]) -> Any:
        if bot.webhook_url:
            raise _ApiError(
                409,
                "Conflict: can't use getUpdates method while webhook is active; "
                "use deleteWebhook to delete the webhook first",
            )
        offset = params.get("offset")
        if isinstance(offset, int) and offset > 0:
            bot.updates = [u for u in bot.updates if u["update_id"] >= offset]
        allowed = params.get("allowed_updates") or None
        limit = int(params.get("limit") or 100)
        timeout = float(params.get("timeout") or 0)

        def ready() -> list[dict[str, Any]]:
            items = bot.updates
            if allowed:
                items = [u for u in items if any(k in u for k in allowed)]
            return items[:limit]

        me = object()
        if bot.poller is not None:
            self.conflicts += 1
        bot.poller = me
        try:
            async with bot.cond:
                bot.cond.notify_all()  # terminate a concurrent poller
                try:
                    async with asyncio.timeout(timeout):
                        await bot.cond.wait_for(lambda: bool(ready()) or bot.poller is not me)
                except TimeoutError:
                    pass
        except asyncio.CancelledError:  # client went away (handler_cancellation)
            if bot.poller is me:
                bot.poller = None
            raise
        if bot.poller is not me:
            raise _ApiError(
                409,
                "Conflict: terminated by other getUpdates request; "
                "make sure that only one bot instance is running",
            )
        bot.poller = None
        return ready()

    async def _m_setwebhook(self, bot: _Bot, params: Mapping[str, Any]) -> Any:
        url = str(params.get("url") or "")
        if not url:
            return await self._m_deletewebhook(bot, params)
        if not url.startswith(("http://", "https://")):
            raise _ApiError(400, "Bad Request: bad webhook: An HTTPS URL must be provided for webhook")
        if params.get("drop_pending_updates"):
            bot.updates.clear()
        bot.webhook_url = url
        bot.webhook_secret = params.get("secret_token") or None
        bot.webhook_allowed = params.get("allowed_updates") or None
        bot.webhook_last_error = None
        bot.poller = None
        async with bot.cond:
            bot.cond.notify_all()
        self._ensure_webhook_task(bot)
        return True

    async def _m_deletewebhook(self, bot: _Bot, params: Mapping[str, Any]) -> Any:
        if params.get("drop_pending_updates"):
            bot.updates.clear()
        bot.webhook_url = ""
        bot.webhook_secret = None
        if bot.webhook_task is not None:
            bot.webhook_task.cancel()
            bot.webhook_task = None
        return True

    async def _m_getwebhookinfo(self, bot: _Bot, params: Mapping[str, Any]) -> Any:
        info: dict[str, Any] = {
            "url": bot.webhook_url,
            "has_custom_certificate": False,
            "pending_update_count": len(bot.updates),
        }
        if bot.webhook_url:
            info["max_connections"] = 40
            if bot.webhook_allowed:
                info["allowed_updates"] = bot.webhook_allowed
            if bot.webhook_last_error:
                info["last_error_date"] = int(time.time())
                info["last_error_message"] = bot.webhook_last_error
        return info

    async def _m_sendmessage(self, bot: _Bot, params: Mapping[str, Any]) -> Any:
        text = params.get("text")
        if not text:
            raise _ApiError(400, "Bad Request: message text is empty")
        return self._new_message(bot, params, text=text, entities=params.get("entities"))

    async def _m_sendphoto(self, bot: _Bot, params: Mapping[str, Any]) -> Any:
        return self._new_message(
            bot,
            params,
            photo=self._photo(params.get("photo")),
            caption=params.get("caption"),
            caption_entities=params.get("caption_entities"),
        )

    async def _m_senddocument(self, bot: _Bot, params: Mapping[str, Any]) -> Any:
        document = params.get("document")
        file_id = document if isinstance(document, str) else f"D{next(self._file_ids)}"
        return self._new_message(
            bot,
            params,
            document={"file_id": file_id, "file_unique_id": f"u-{file_id}"},
            caption=params.get("caption"),
        )

    async def _m_createinvoicelink(self, bot: _Bot, params: Mapping[str, Any]) -> Any:
        prices = params.get("prices")
        if (
            not params.get("payload")
            or not params.get("currency")
            or not isinstance(prices, list)
            or not prices
        ):
            raise _ApiError(400, "Bad Request: invoice parameters are invalid")
        link = f"https://t.me/$fake-invoice-{len(self.invoices) + 1}"
        self.invoices[link] = dict(params)
        return link

    async def _m_answerprecheckoutquery(self, bot: _Bot, params: Mapping[str, Any]) -> Any:
        ok = params.get("ok")
        ok = ok is True or str(ok).lower() == "true"
        self.pre_checkout_answers[str(params.get("pre_checkout_query_id"))] = (
            ok,
            params.get("error_message"),
        )
        return True

    async def _m_copymessage(self, bot: _Bot, params: Mapping[str, Any]) -> Any:
        src = bot.messages.get((params.get("from_chat_id"), params.get("message_id")))  # type: ignore[arg-type]
        if src is None:
            raise _ApiError(400, "Bad Request: message to copy not found")
        content = {
            k: v for k, v in src.items() if k in {"text", "entities", "photo", "caption", "caption_entities"}
        }
        msg = self._new_message(bot, params, **content)
        return {"message_id": msg["message_id"]}

    async def _m_pinchatmessage(self, bot: _Bot, params: Mapping[str, Any]) -> Any:
        msg = self._existing(bot, params, "pin")
        msg["pinned"] = True  # fake-only marker for assertions
        return True

    async def _m_editmessagetext(self, bot: _Bot, params: Mapping[str, Any]) -> Any:
        msg = self._existing(bot, params, "edit")
        if "text" not in msg:
            raise _ApiError(400, "Bad Request: there is no text in the message to edit")
        markup = params.get("reply_markup")
        if (
            msg.get("text") == params.get("text")
            and msg.get("entities") == params.get("entities")
            and msg.get("reply_markup") == markup
        ):
            raise _ApiError(
                400,
                "Bad Request: message is not modified: specified new message content and reply markup "
                "are exactly the same as a current content and reply markup of the message",
            )
        msg["text"] = params.get("text")
        self._set_opt(msg, "entities", params.get("entities"))
        self._set_opt(msg, "reply_markup", markup)
        msg["edit_date"] = int(time.time())
        return msg

    async def _m_editmessagecaption(self, bot: _Bot, params: Mapping[str, Any]) -> Any:
        msg = self._existing(bot, params, "edit")
        self._set_opt(msg, "caption", params.get("caption"))
        self._set_opt(msg, "caption_entities", params.get("caption_entities"))
        self._set_opt(msg, "reply_markup", params.get("reply_markup"))
        msg["edit_date"] = int(time.time())
        return msg

    async def _m_editmessagemedia(self, bot: _Bot, params: Mapping[str, Any]) -> Any:
        msg = self._existing(bot, params, "edit")
        media = params.get("media")
        if not isinstance(media, dict) or "media" not in media:
            raise _ApiError(400, "Bad Request: can't parse InputMedia")
        for key in ("text", "entities", "photo", "caption", "caption_entities"):
            msg.pop(key, None)
        msg["photo"] = self._photo(media["media"])
        self._set_opt(msg, "caption", media.get("caption"))
        self._set_opt(msg, "caption_entities", media.get("caption_entities"))
        self._set_opt(msg, "reply_markup", params.get("reply_markup"))
        msg["edit_date"] = int(time.time())
        return msg

    async def _m_editmessagereplymarkup(self, bot: _Bot, params: Mapping[str, Any]) -> Any:
        msg = self._existing(bot, params, "edit")
        self._set_opt(msg, "reply_markup", params.get("reply_markup"))
        msg["edit_date"] = int(time.time())
        return msg

    async def _m_deletemessage(self, bot: _Bot, params: Mapping[str, Any]) -> Any:
        self._existing(bot, params, "delete")
        bot.messages.pop((self._chat_id(params), params["message_id"]))
        return True

    async def _m_answercallbackquery(self, bot: _Bot, params: Mapping[str, Any]) -> Any:
        if not params.get("callback_query_id"):
            raise _ApiError(
                400, "Bad Request: query is too old and response timeout expired or query ID is invalid"
            )
        return True

    async def _m_getchat(self, bot: _Bot, params: Mapping[str, Any]) -> Any:
        chat_id = self._chat_id(params)
        chat = {**_chat_json(chat_id), **self.chats.get(chat_id, {})}
        if chat.get("is_forum") is False:
            chat.pop("is_forum")
        chat.update(
            {
                "accent_color_id": 0,
                "max_reaction_count": 11,
                "accepted_gift_types": {
                    "unlimited_gifts": False,
                    "limited_gifts": False,
                    "unique_gifts": False,
                    "premium_subscription": False,
                    "gifts_from_channels": False,
                },
            }
        )
        return chat

    async def _m_getchatmember(self, bot: _Bot, params: Mapping[str, Any]) -> Any:
        chat_id = self._chat_id(params)
        user_id = params.get("user_id")
        if not isinstance(user_id, int):
            raise _ApiError(400, "Bad Request: invalid user_id specified")
        status = self.chat_members.get((chat_id, user_id), "member")
        user = bot.user_json() if user_id == bot.bot_id else _user_json(user_id)
        member: dict[str, Any] = {"status": status, "user": user}
        if status == "creator":
            member["is_anonymous"] = False
        elif status == "administrator":
            member.update(dict.fromkeys(_ADMIN_FALSE_FIELDS, False))
            member.update(self.member_rights.get((chat_id, user_id), {}))
        return member

    def _forum_chat(self, bot: _Bot, params: Mapping[str, Any]) -> int:
        chat_id = self._chat_id(params)
        if chat_id > 0 or self.chats.get(chat_id, {}).get("is_forum") is False:
            raise _ApiError(400, "Bad Request: the chat is not a forum")
        status = self.chat_members.get((chat_id, bot.bot_id))
        if status is not None:  # rights are modelled only when the test configured them
            rights = self.member_rights.get((chat_id, bot.bot_id), {})
            if status != "creator" and not (status == "administrator" and rights.get("can_manage_topics")):
                raise _ApiError(400, "Bad Request: not enough rights to manage topics")
        return chat_id

    def _topic(self, bot: _Bot, params: Mapping[str, Any]) -> dict[str, Any]:
        chat_id = self._forum_chat(bot, params)
        topic = bot.forum_topics.get((chat_id, params.get("message_thread_id")))  # type: ignore[arg-type]
        if topic is None or topic.get("deleted"):
            raise _ApiError(400, "Bad Request: message thread not found")
        return topic

    async def _m_createforumtopic(self, bot: _Bot, params: Mapping[str, Any]) -> Any:
        chat_id = self._forum_chat(bot, params)
        name = params.get("name")
        if not name:
            raise _ApiError(400, "Bad Request: topic name is empty")
        topic: dict[str, Any] = {
            "message_thread_id": next(bot.topics),
            "name": name,
            "icon_color": params.get("icon_color") or 7322096,
        }
        if params.get("icon_custom_emoji_id"):
            topic["icon_custom_emoji_id"] = str(params["icon_custom_emoji_id"])
        bot.forum_topics[(chat_id, topic["message_thread_id"])] = {**topic, "closed": False}
        return topic

    async def _m_editforumtopic(self, bot: _Bot, params: Mapping[str, Any]) -> Any:
        topic = self._topic(bot, params)
        if params.get("name"):
            topic["name"] = params["name"]
        if params.get("icon_custom_emoji_id") is not None:
            topic["icon_custom_emoji_id"] = str(params["icon_custom_emoji_id"])
        return True

    async def _m_closeforumtopic(self, bot: _Bot, params: Mapping[str, Any]) -> Any:
        topic = self._topic(bot, params)
        if topic["closed"]:
            raise _ApiError(400, "Bad Request: TOPIC_NOT_MODIFIED")
        topic["closed"] = True
        return True

    async def _m_reopenforumtopic(self, bot: _Bot, params: Mapping[str, Any]) -> Any:
        topic = self._topic(bot, params)
        if not topic["closed"]:
            raise _ApiError(400, "Bad Request: TOPIC_NOT_MODIFIED")
        topic["closed"] = False
        return True

    async def _m_deleteforumtopic(self, bot: _Bot, params: Mapping[str, Any]) -> Any:
        topic = self._topic(bot, params)
        topic["deleted"] = True
        return True

    async def _m_getforumtopiciconstickers(self, bot: _Bot, params: Mapping[str, Any]) -> Any:
        return [
            {
                "file_id": f"icon{n}",
                "file_unique_id": f"iconu{n}",
                "type": "custom_emoji",
                "width": 512,
                "height": 512,
                "is_animated": False,
                "is_video": False,
                "emoji": emoji,
                "custom_emoji_id": emoji_id,
            }
            for n, (emoji, emoji_id) in enumerate(self.forum_icons.items())
        ]

    @staticmethod
    def _set_opt(msg: dict[str, Any], key: str, value: Any) -> None:
        if value is None:
            msg.pop(key, None)
        else:
            msg[key] = value

    # ----------------------------------------------------------------- webhook delivery

    def _ensure_webhook_task(self, bot: _Bot) -> None:
        if bot.webhook_task is None or bot.webhook_task.done():
            bot.webhook_task = asyncio.get_running_loop().create_task(self._deliver(bot))

    async def _deliver(self, bot: _Bot) -> None:
        assert self._client is not None
        while bot.webhook_url:
            if not bot.updates:
                async with bot.cond:
                    await bot.cond.wait_for(lambda: bool(bot.updates) or not bot.webhook_url)
                continue
            update = bot.updates[0]
            headers = {"Content-Type": "application/json"}
            if bot.webhook_secret:
                headers["X-Telegram-Bot-Api-Secret-Token"] = bot.webhook_secret
            try:
                async with self._client.post(bot.webhook_url, data=json.dumps(update), headers=headers) as r:
                    status = r.status
            except (ClientError, TimeoutError) as exc:
                bot.webhook_last_error = f"Connection error: {type(exc).__name__}"
                await asyncio.sleep(self.webhook_retry_delay)
                continue
            if 200 <= status < 300:
                if bot.updates and bot.updates[0] is update:
                    bot.updates.pop(0)
                bot.webhook_last_error = None
            else:
                bot.webhook_last_error = f"Wrong response from the webhook: {status}"
                await asyncio.sleep(self.webhook_retry_delay)
