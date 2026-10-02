"""Bot lifecycle: polling / webhook ingress and hot reconfiguration (03 §5.3–5.4).

* :class:`BotHolder` keeps the *current* :class:`aiogram.Bot`. Handlers get ``bot`` from the update;
  background code (notifier, jobs) reads ``holder.current`` at the moment of the call and never keeps
  a reference, so a token/proxy swap is picked up everywhere at once.
* :class:`BotRunner` is the ``bot`` component. ``reconfigure`` switches ``BOT_TOKEN``,
  ``TELEGRAM_PROXY``, ``TELEGRAM_API_URL``, ``BOT_MODE``, ``PUBLIC_URL`` and ``WEBHOOK_SECRET`` without a
  process restart:

  1. a new ``Bot`` (new HTTP session) is created and checked with ``getMe`` — on failure nothing changes;
  2. the old ingress stops cleanly (polling: the long poll is cancelled, processed updates are confirmed
     with a short ``getUpdates(offset)`` so they are not delivered twice; webhook: new requests get 503
     and Telegram retries them);
  3. the new ingress is configured: polling → ``getWebhookInfo`` and, if a (foreign) webhook is set,
     ``deleteWebhook(drop_pending_updates=False)``; webhook → ``setWebhook(url, secret_token)``;
     pending updates are never dropped;
  4. the holder is swapped, the new ingress starts and the old session is closed after a grace period;
  5. any failure in 2–4 restores the previous bot and ingress and raises :class:`ProbeError`.

Polling is implemented here (not ``Dispatcher.start_polling``) so that it can be stopped and restarted
any number of times without re-running startup hooks, with an offset carried over between tokens of the
same bot and with backpressure (bounded number of concurrent update handlers).

Webhook route: ``POST /tg/{secret}`` where the path part is derived from ``WEBHOOK_SECRET`` (the raw
secret never appears in URLs) and the ``X-Telegram-Bot-Api-Secret-Token`` header must equal
``WEBHOOK_SECRET`` (constant-time comparison). ``GET /tg/ping/{nonce}`` answers the self-reachability
check done by :meth:`BotRunner.probe` before switching to webhook mode.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol
from urllib.parse import urlsplit

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import PRODUCTION, TelegramAPIServer
from aiogram.exceptions import (
    AiogramError,
    TelegramAPIError,
    TelegramConflictError,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramServerError,
    TelegramUnauthorizedError,
)
from aiogram.methods import DeleteWebhook, GetMe, GetUpdates, GetWebhookInfo, SetWebhook
from aiogram.types import Update, User
from aiogram.utils.token import TokenValidationError
from aiohttp import ClientError, ClientSession, ClientTimeout, web
from pydantic import ValidationError

from svbg.core.clock import now
from svbg.core.component import Health, HealthReport, ProbeError, fix_setting
from svbg.core.log import mask, register_secret
from svbg.tg.notifier import TRANSPORT_ERRORS

if TYPE_CHECKING:
    from aiogram import Dispatcher
    from aiogram.client.session.middlewares.base import BaseRequestMiddleware

__all__ = [
    "BotConfig",
    "BotHolder",
    "BotRunner",
    "BotUnavailableError",
    "Mode",
    "TransientProbeError",
    "webhook_path_secret",
]

log = logging.getLogger("svbg.tg.runner")

Mode = Literal["polling", "webhook"]

SECRET_HEADER: Final = "X-Telegram-Bot-Api-Secret-Token"  # noqa: S105 - header name
_WEBHOOK_SECRET_RE: Final = re.compile(r"^[A-Za-z0-9_-]{16,256}$")
# Everything a Telegram call may raise because of Telegram, the network or the proxy.
_TG_FAILURES: Final = (TelegramAPIError, AiogramError, TimeoutError, *TRANSPORT_ERRORS)
_TRANSIENT: Final = (TelegramNetworkError, TelegramServerError, TimeoutError, *TRANSPORT_ERRORS)

# Owner-facing texts (Russian), in one place.
_TXT: Final = {
    "no_token": "Токен бота не задан",
    "bad_token_format": "Неверный формат токена: ожидается вида 123456789:AA…",
    "token_rejected": "Telegram отклонил токен — проверьте его в @BotFather",
    "network": "Не удалось связаться с Telegram ({reason})",
    "network_hint": "Проверьте TELEGRAM_PROXY / TELEGRAM_API_URL и доступ сервера в интернет",
    "bad_proxy": "Неверный адрес прокси: {reason}",
    "bad_api_url": "Неверный TELEGRAM_API_URL: нужен адрес вида http(s)://host[:port]",
    "bad_mode": "BOT_MODE должен быть polling или webhook",
    "need_public_url": "Для режима webhook нужен PUBLIC_URL (https://ваш-домен)",
    "public_url_https": "PUBLIC_URL должен начинаться с https://",
    "need_secret": "Для режима webhook нужен WEBHOOK_SECRET: 16–256 символов A-Z a-z 0-9 _ -",
    "unreachable": "PUBLIC_URL недоступен снаружи: запрос {url}/tg/ping/… не дошёл до бота ({reason})",
    "unreachable_hint": "Проверьте домен, DNS и reverse-proxy (Caddy) — маршрут /tg/* должен вести в бота",
    "other_bot": "Это токен другого бота (@{new}, был @{old}) — смена не подтверждена",
    "setup_failed": "Не удалось переключить приём апдейтов: {reason}. Оставлена прежняя конфигурация",
    "ok_polling": "Работает (polling), @{username}",
    "ok_webhook": "Работает (webhook), @{username}",
    "polling_errors": "Ошибки получения апдейтов: {reason}",
    "conflict": "Конфликт: этого бота опрашивает другой процесс или у него установлен webhook",
    "revoked": "Telegram перестал принимать токен (отозван?) — задайте новый BOT_TOKEN",
    "webhook_errors": "Telegram не может доставить апдейты: {reason}",
    "stopped": "Остановлен",
    "starting": "Запускается…",
}


class BotUnavailableError(RuntimeError):
    """No bot is configured (missing/invalid token or the runner is not started)."""


class TransientProbeError(ProbeError):
    """Telegram (or the proxy) could not be reached — the same config may work a bit later."""


class _SettingsSource(Protocol):
    def current(self) -> Mapping[str, Any]: ...


class _ErrorHub(Protocol):
    async def capture(
        self,
        exc: BaseException,
        place: str,
        *,
        module: str | None = None,
        user_id: int | None = None,
        context: Mapping[str, Any] | None = None,
        handled: str = ...,
    ) -> str | None: ...


BotChangeHook = Callable[[User, User], Awaitable[bool]]
BotFactory = Callable[["BotConfig"], Bot]


def webhook_path_secret(webhook_secret: str) -> str:
    """Path part of ``/tg/{secret}``, derived from ``WEBHOOK_SECRET`` so the secret itself is not in URLs."""
    return hmac.new(webhook_secret.encode(), b"svbg:tg-webhook-path", hashlib.sha256).hexdigest()[:40]


def _plain(value: Any) -> str | None:
    if value is None:
        return None
    getter = getattr(value, "get_secret_value", None)
    if callable(getter):
        value = getter()
    text = str(value).strip()
    return text or None


def _cfg_get(cfg: Mapping[str, Any], key: str) -> Any:
    try:
        return cfg[key]
    except KeyError:
        return None


@dataclass(frozen=True, slots=True)
class BotConfig:
    """Effective configuration of the bot component (parsed from a settings snapshot)."""

    token: str = field(repr=False)
    mode: Mode = "polling"
    proxy: str | None = field(default=None, repr=False)
    api_url: str | None = None
    public_url: str | None = None
    webhook_secret: str | None = field(default=None, repr=False)

    @classmethod
    def from_mapping(cls, cfg: Mapping[str, Any]) -> BotConfig:
        """Parse and validate; raises :class:`ProbeError` with an owner-facing message."""
        token = _plain(_cfg_get(cfg, "BOT_TOKEN"))
        if token is None:
            raise ProbeError(_TXT["no_token"], fix_action=fix_setting("BOT_TOKEN"))
        mode_raw = (_plain(_cfg_get(cfg, "BOT_MODE")) or "polling").lower()
        if mode_raw not in ("polling", "webhook"):
            raise ProbeError(_TXT["bad_mode"], fix_action=fix_setting("BOT_MODE"))
        mode: Mode = "webhook" if mode_raw == "webhook" else "polling"
        api_url = _plain(_cfg_get(cfg, "TELEGRAM_API_URL"))
        if api_url is not None:
            parts = urlsplit(api_url)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise ProbeError(_TXT["bad_api_url"], fix_action=fix_setting("TELEGRAM_API_URL"))
            api_url = api_url.rstrip("/")
        public_url = _plain(_cfg_get(cfg, "PUBLIC_URL"))
        if public_url is not None:
            public_url = public_url.rstrip("/")
        return cls(
            token=token,
            mode=mode,
            proxy=_plain(_cfg_get(cfg, "TELEGRAM_PROXY")),
            api_url=api_url,
            public_url=public_url,
            webhook_secret=_plain(_cfg_get(cfg, "WEBHOOK_SECRET")),
        )

    @property
    def session_key(self) -> tuple[str, str | None, str | None]:
        return (self.token, self.proxy, self.api_url)

    @property
    def bot_id(self) -> int | None:
        left = self.token.partition(":")[0]
        return int(left) if left.isdigit() else None

    def webhook_url(self) -> str:
        assert self.public_url is not None and self.webhook_secret is not None
        return f"{self.public_url}/tg/{webhook_path_secret(self.webhook_secret)}"


class BotHolder:
    """Atomic reference to the current :class:`Bot`."""

    def __init__(self, bot: Bot | None = None, me: User | None = None) -> None:
        self._bot = bot
        self._me = me
        self._version = 0
        self._ready = asyncio.Event()
        if bot is not None:
            self._ready.set()

    @property
    def current(self) -> Bot:
        bot = self._bot
        if bot is None:
            raise BotUnavailableError("bot is not configured")
        return bot

    def get(self) -> Bot | None:
        return self._bot

    @property
    def me(self) -> User | None:
        return self._me

    @property
    def version(self) -> int:
        return self._version

    def swap(self, bot: Bot | None, me: User | None = None) -> Bot | None:
        """Install ``bot`` (``None`` clears it) and return the previous one."""
        old, self._bot, self._me = self._bot, bot, me
        self._version += 1
        if bot is None:
            self._ready.clear()
        else:
            self._ready.set()
        return old

    async def wait_ready(self) -> Bot:
        """Wait until a bot is installed (wrap in ``asyncio.timeout`` to bound the wait)."""
        await self._ready.wait()
        return self.current


class _State(enum.Enum):
    STOPPED = "stopped"
    RUNNING = "running"
    FAILED = "failed"


@dataclass(slots=True)
class _Active:
    cfg: BotConfig
    bot: Bot
    me: User


@dataclass(frozen=True, slots=True)
class _WebhookCheck:
    """Cached result of ``getWebhookInfo`` for one activation (``active`` identity)."""

    active: _Active
    at: float  # time.monotonic()
    problem: str | None = None  # owner-facing summary when degraded
    pending: int | None = None


class BotRunner:
    """The ``bot`` component: owns the Bot instance(s) and the update ingress."""

    name = "bot"

    def __init__(
        self,
        settings: _SettingsSource,
        dispatcher: Dispatcher,
        hub: _ErrorHub | None = None,
        *,
        holder: BotHolder | None = None,
        bot_factory: BotFactory | None = None,
        default: DefaultBotProperties | None = None,
        allowed_updates: list[str] | None = None,
        polling_timeout: int = 25,
        request_timeout: float = 15.0,
        max_concurrent_updates: int = 64,
        drain_timeout: float = 10.0,
        start_retry_delay: float = 5.0,
        session_grace: float = 5.0,
        verify_public_url: bool = True,
        allow_http_webhook: bool = False,
        confirm_bot_change: BotChangeHook | None = None,
        workflow_data: Mapping[str, Any] | None = None,
        webhook_health_ttl: float = 30.0,
        webhook_health_wait: float = 2.0,
        request_middlewares: Sequence[BaseRequestMiddleware] = (),
    ) -> None:
        self.settings = settings
        #: Installed on every Bot's session (the default banner on every outgoing message).
        self._request_middlewares = tuple(request_middlewares)
        self.dp = dispatcher
        self.hub = hub
        self.holder = holder or BotHolder()
        self._factory = bot_factory or self._default_factory
        self._default = default
        self._allowed_updates = allowed_updates
        self.polling_timeout = polling_timeout
        self.request_timeout = request_timeout
        self.drain_timeout = drain_timeout
        self.start_retry_delay = start_retry_delay
        self.session_grace = session_grace
        self.verify_public_url = verify_public_url
        self.allow_http_webhook = allow_http_webhook
        self.confirm_bot_change = confirm_bot_change
        # Webhook health asks Telegram (getWebhookInfo); the answer is cached for ``webhook_health_ttl``
        # seconds and refreshed by a single in-flight request. While a refresh runs, callers that already
        # have a (stale) answer wait at most ``webhook_health_wait`` seconds and then get the stale one,
        # so /ready, the settings screen and the periodic check never queue up behind a slow Telegram.
        self.webhook_health_ttl = webhook_health_ttl
        self.webhook_health_wait = webhook_health_wait
        self._wh_check: _WebhookCheck | None = None
        self._wh_refresh: asyncio.Task[None] | None = None
        self._wh_refresh_for: _Active | None = None
        self._workflow = dict(workflow_data or {})
        self._max_updates = max_concurrent_updates
        self._slots = asyncio.Semaphore(max_concurrent_updates)
        self._lock = asyncio.Lock()
        self._state = _State.STOPPED
        self._active: _Active | None = None
        self._failure: ProbeError | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self._offset: dict[int, int] = {}
        self._webhook: tuple[str, bytes, Bot] | None = None  # (path secret, header secret, bot)
        self._webhook_paused = False
        self._handlers: set[asyncio.Task[None]] = set()
        self._background: set[asyncio.Task[None]] = set()
        self._retired: list[Bot] = []  # replaced bots whose sessions close after ``session_grace``
        self._ping_nonces: set[str] = set()
        self._startup_done = False
        self._poll_errors = 0
        self._poll_last_error: str | None = None
        self._revoked = False
        self._last_update_at: datetime | None = None
        self._updates_total = 0

    # ------------------------------------------------------------------ public API

    @property
    def mode(self) -> Mode | None:
        return self._active.cfg.mode if self._active else None

    @property
    def me(self) -> User | None:
        return self._active.me if self._active else None

    def web_routes(self) -> list[web.RouteDef]:
        """Routes to mount into the web app (``svbg.web.build_web_app``)."""
        return [
            web.get("/tg/ping/{nonce}", self._handle_ping),
            web.post("/tg/{secret}", self._handle_webhook),
        ]

    async def start(self) -> None:
        """Start with the current settings. A bad config does not raise: the component reports DOWN."""
        async with self._lock:
            if self._state is _State.RUNNING:
                return
            try:
                cfg = BotConfig.from_mapping(self.settings.current())
                await self._activate(cfg, previous=None)
            except ProbeError as exc:
                self._state = _State.FAILED
                self._failure = exc
                log.warning("bot not started: %s", mask(str(exc)))
                if isinstance(exc, TransientProbeError):
                    self._spawn_background(self._retry_start())

    async def _retry_start(self) -> None:
        """Keep trying the current settings while Telegram is unreachable at boot (backoff up to 60 s)."""
        delay = self.start_retry_delay
        while True:
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)
            async with self._lock:
                if self._state is not _State.FAILED or self._active is not None:
                    return
                try:
                    await self._activate(BotConfig.from_mapping(self.settings.current()), previous=None)
                except TransientProbeError as exc:
                    self._failure = exc
                except ProbeError as exc:
                    self._failure = exc
                    return
                else:
                    return

    async def stop(self) -> None:
        """Stop ingress, drain in-flight handlers (``drain_timeout``), run shutdown hooks, close sessions."""
        async with self._lock:
            active, self._active = self._active, None
            await self._stop_ingress(active, confirm=True)
            await self._drain()
            if self._startup_done and active is not None:
                self._startup_done = False
                try:
                    await self.dp.emit_shutdown(**self._hook_kwargs(active.bot))
                except Exception:
                    log.exception("dispatcher shutdown hooks failed")
            self.holder.swap(None)
            self._wh_check = None
            for task in list(self._background):
                task.cancel()
            await asyncio.gather(*self._background, return_exceptions=True)
            retired, self._retired = self._retired, []
            for bot in retired:
                await self._close_bot(bot)
            if active is not None:
                await self._close_bot(active.bot)
            self._state = _State.STOPPED

    async def probe(self, candidate: Mapping[str, Any]) -> None:
        """Check ``candidate`` without side effects on the running bot (03 §5.4)."""
        cfg = BotConfig.from_mapping(candidate)
        self._check_mode_config(cfg)
        active = self._active
        if active is not None and active.cfg.session_key == cfg.session_key:
            me = await self._get_me(active.bot)
        else:
            bot = self._build_bot(cfg)
            try:
                me = await self._get_me(bot)
            finally:
                await self._close_bot(bot)
        if active is not None and me.id != active.me.id:
            await self._confirm_change(active.me, me)
        if cfg.mode == "webhook" and self.verify_public_url:
            await self._check_reachable(cfg)

    async def reconfigure(self, cfg: Mapping[str, Any]) -> None:
        """Atomically switch to ``cfg``; idempotent. On failure the previous bot keeps working."""
        new = BotConfig.from_mapping(cfg)
        async with self._lock:
            active = self._active
            if active is not None and active.cfg == new and self._state is _State.RUNNING:
                return
            try:
                await self._activate(new, previous=active)
            except ProbeError as exc:
                if self._active is None:
                    self._state = _State.FAILED
                    self._failure = exc
                raise

    async def health(self) -> HealthReport:
        active = self._active
        if active is None:
            if self._failure is not None:
                return HealthReport.down(self._failure.human, fix_action=self._failure.fix_action)
            return HealthReport(Health.UNKNOWN, _TXT["stopped"])
        details: dict[str, Any] = {
            "mode": active.cfg.mode,
            "bot_id": active.me.id,
            "username": active.me.username,
            "updates_total": self._updates_total,
            "handlers_in_flight": len(self._handlers),
            "last_update_at": self._last_update_at.isoformat() if self._last_update_at else None,
        }
        username = active.me.username or str(active.me.id)
        if active.cfg.mode == "polling":
            if self._revoked:
                return HealthReport.down(_TXT["revoked"], fix_action=fix_setting("BOT_TOKEN"), **details)
            if self._poll_errors >= 3:
                reason = self._poll_last_error or "?"
                summary = (
                    _TXT["conflict"] if reason == "conflict" else _TXT["polling_errors"].format(reason=reason)
                )
                return HealthReport.degraded(summary, errors=self._poll_errors, **details)
            return HealthReport.ok(_TXT["ok_polling"].format(username=username), **details)
        check = await self._webhook_check(active)
        if check.pending is not None:
            details["pending_update_count"] = check.pending
        if check.problem is not None:
            return HealthReport.degraded(check.problem, **details)
        return HealthReport.ok(_TXT["ok_webhook"].format(username=username), **details)

    async def _webhook_check(self, active: _Active) -> _WebhookCheck:
        """Cached ``getWebhookInfo`` verdict for ``active`` (see ``webhook_health_ttl``)."""
        cached = self._wh_check
        if cached is not None and cached.active is not active:
            cached = None
        if cached is not None and time.monotonic() - cached.at < self.webhook_health_ttl:
            return cached
        task = self._wh_refresh
        if task is None or task.done() or self._wh_refresh_for is not active:
            # A refresh still running for a replaced activation is left alone: its result is discarded.
            task = asyncio.get_running_loop().create_task(
                self._refresh_webhook_check(active), name="svbg-bot-webhook-health"
            )
            task.add_done_callback(self._webhook_refresh_done)
            self._background.add(task)  # cancelled and awaited by stop()
            task.add_done_callback(self._background.discard)
            self._wh_refresh, self._wh_refresh_for = task, active
        if cached is None:
            # Nothing to fall back to: wait for the answer. A caller's timeout cancels only the wait,
            # the shared request finishes and fills the cache for the next caller.
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if not task.cancelled() or (current is not None and current.cancelling()):
                    raise  # we were cancelled, not the shared refresh (stop() cancels that one)
        else:
            await asyncio.wait({task}, timeout=self.webhook_health_wait)
        fresh = self._wh_check
        if fresh is not None and fresh.active is active:
            return fresh
        # The activation changed while we waited (or the refresh failed unexpectedly).
        return cached or _WebhookCheck(active, time.monotonic(), _TXT["starting"])

    async def _refresh_webhook_check(self, active: _Active) -> None:
        try:
            async with asyncio.timeout(self.request_timeout):
                info = await active.bot(GetWebhookInfo(), request_timeout=int(self.request_timeout))
        except _TG_FAILURES as exc:
            check = _WebhookCheck(active, time.monotonic(), _TXT["network"].format(reason=type(exc).__name__))
        else:
            problem: str | None = None
            if info.url != active.cfg.webhook_url():
                problem = _TXT["webhook_errors"].format(reason="url")
            elif info.last_error_message and info.pending_update_count > 0:
                problem = _TXT["webhook_errors"].format(reason=info.last_error_message[:200])
            check = _WebhookCheck(active, time.monotonic(), problem, info.pending_update_count)
        if self._active is active:
            self._wh_check = check

    def _webhook_refresh_done(self, task: asyncio.Task[None]) -> None:
        if self._wh_refresh is task:
            self._wh_refresh = self._wh_refresh_for = None
        if not task.cancelled() and (exc := task.exception()) is not None:
            log.error("webhook health check failed: %s", type(exc).__name__)

    # ------------------------------------------------------------------ activation

    async def _activate(self, cfg: BotConfig, previous: _Active | None) -> None:
        """Bring ``cfg`` up, replacing ``previous``. Raises ProbeError and keeps ``previous`` on failure."""
        self._check_mode_config(cfg)
        register_secret(cfg.token)
        register_secret(cfg.webhook_secret)
        reuse = previous is not None and previous.cfg.session_key == cfg.session_key
        if reuse:
            assert previous is not None
            bot, me = previous.bot, previous.me
        else:
            bot = self._build_bot(cfg)
            try:
                me = await self._get_me(bot)
            except BaseException:
                await self._close_bot(bot)
                raise
        new = _Active(cfg=cfg, bot=bot, me=me)
        await self._stop_ingress(previous, confirm=previous is not None and previous.me.id == me.id)
        try:
            if previous is not None and previous.me.id != me.id and previous.cfg.mode == "webhook":
                await self._quiet(previous.bot(DeleteWebhook(drop_pending_updates=False)))
            await self._setup_ingress(new)
        except _TG_FAILURES as exc:
            reason = self._reason(exc)
            log.warning("bot switch failed (%s), restoring previous configuration", reason)
            if not reuse:
                await self._close_bot(bot)
            if previous is not None:
                await self._restore(previous)
            else:
                self._state = _State.FAILED
            error_type = TransientProbeError if isinstance(exc, _TRANSIENT) else ProbeError
            raise error_type(_TXT["setup_failed"].format(reason=reason)) from exc
        self._active = new
        self._seed_webhook_check(new)
        self._failure = None
        self._revoked = False
        self._poll_errors = 0
        self._poll_last_error = None
        self.holder.swap(bot, me)
        self._start_ingress(new)
        self._state = _State.RUNNING
        if not self._startup_done:
            self._startup_done = True
            try:
                await self.dp.emit_startup(**self._hook_kwargs(bot))
            except Exception as exc:
                log.exception("dispatcher startup hooks failed")
                await self._capture(exc, "tg:startup", "bot keeps running")
        if previous is not None and not reuse:
            self._retired.append(previous.bot)
            self._spawn_background(self._close_later(previous.bot))
        log.info(
            "bot @%s (id=%s) running in %s mode%s",
            me.username,
            me.id,
            cfg.mode,
            "" if previous is None else " (reconfigured)",
        )

    async def _restore(self, previous: _Active) -> None:
        try:
            await self._setup_ingress(previous)
        except _TG_FAILURES as exc:
            log.error("could not restore previous bot ingress: %s", self._reason(exc))  # noqa: TRY400
        self._active = previous
        self._wh_check = None
        self.holder.swap(previous.bot, previous.me)
        self._start_ingress(previous)
        self._state = _State.RUNNING

    def _seed_webhook_check(self, active: _Active) -> None:
        """``setWebhook`` has just succeeded: the URL is ours, no need to ask Telegram right away."""
        self._wh_check = _WebhookCheck(active, time.monotonic()) if active.cfg.mode == "webhook" else None

    async def _setup_ingress(self, active: _Active) -> None:
        bot, cfg = active.bot, active.cfg
        if cfg.mode == "polling":
            info = await self._call(bot, GetWebhookInfo())
            if info.url:
                log.info("deleting webhook set for bot id=%s before polling", active.me.id)
                await self._call(bot, DeleteWebhook(drop_pending_updates=False))
        else:
            assert cfg.webhook_secret is not None
            await self._call(
                bot,
                SetWebhook(
                    url=cfg.webhook_url(),
                    secret_token=cfg.webhook_secret,
                    allowed_updates=self._resolve_allowed(),
                    drop_pending_updates=False,
                    max_connections=40,
                ),
            )

    def _start_ingress(self, active: _Active) -> None:
        if active.cfg.mode == "polling":
            self._webhook = None
            self._poll_task = asyncio.get_running_loop().create_task(
                self._poll_loop(active.bot, active.me.id), name="svbg-bot-polling"
            )
            self._poll_task.add_done_callback(self._poll_done)
        else:
            assert active.cfg.webhook_secret is not None
            self._webhook = (
                webhook_path_secret(active.cfg.webhook_secret),
                active.cfg.webhook_secret.encode(),
                active.bot,
            )
        self._webhook_paused = False

    async def _stop_ingress(self, active: _Active | None, *, confirm: bool) -> None:
        if self._webhook is not None:
            self._webhook_paused = True
            self._webhook = None
        task, self._poll_task = self._poll_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            if confirm and active is not None:
                await self._confirm_offset(active)

    async def _confirm_offset(self, active: _Active) -> None:
        offset = self._offset.get(active.me.id)
        if offset is None:
            return
        await self._quiet(active.bot(GetUpdates(offset=offset, limit=1, timeout=0), request_timeout=5))

    # ------------------------------------------------------------------ polling

    async def _poll_loop(self, bot: Bot, bot_id: int) -> None:
        delay = 1.0
        allowed = self._resolve_allowed()
        request_timeout = int(self.polling_timeout + self.request_timeout)
        while True:
            method = GetUpdates(
                offset=self._offset.get(bot_id), timeout=self.polling_timeout, allowed_updates=allowed
            )
            try:
                updates = await bot(method, request_timeout=request_timeout)
            except TelegramRetryAfter as exc:
                self._poll_failed("flood")
                await asyncio.sleep(exc.retry_after)
                continue
            except TelegramUnauthorizedError:
                self._revoked = True
                self._poll_failed("unauthorized")
                log.error("Telegram rejected the bot token while polling")  # noqa: TRY400
                await asyncio.sleep(60)
                continue
            except TelegramConflictError:
                self._poll_failed("conflict")
            except TelegramAPIError as exc:
                self._poll_failed(type(exc).__name__)
                log.warning("getUpdates failed: %s", mask(str(exc)))
            except (AiogramError, ValidationError) as exc:
                self._poll_failed(type(exc).__name__)
                log.warning("getUpdates returned an unreadable response: %s", type(exc).__name__)
            except TRANSPORT_ERRORS as exc:
                self._poll_failed(type(exc).__name__)
                log.warning("getUpdates: network error %s", type(exc).__name__)
            else:
                delay = 1.0
                self._poll_errors = 0
                self._poll_last_error = None
                self._revoked = False
                for update in updates:
                    await self._slots.acquire()
                    self._spawn_handler(bot, update)
                    self._offset[bot_id] = update.update_id + 1
                continue
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)

    def _poll_failed(self, reason: str) -> None:
        self._poll_errors += 1
        self._poll_last_error = reason

    def _poll_done(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error("polling loop crashed: %s", type(exc).__name__, exc_info=exc)
            self._poll_failed(type(exc).__name__)
            self._poll_errors = max(self._poll_errors, 3)
            self._spawn_background(self._capture(exc, "tg:polling", "polling stopped"))

    # ------------------------------------------------------------------ webhook

    async def _handle_webhook(self, request: web.Request) -> web.Response:
        hook = self._webhook
        if hook is None:
            if self._webhook_paused:
                return web.Response(status=503)
            raise web.HTTPNotFound
        path_secret, header_secret, bot = hook
        given_path = request.match_info["secret"].encode()
        if not hmac.compare_digest(given_path, path_secret.encode()):
            raise web.HTTPNotFound
        header = request.headers.get(SECRET_HEADER, "").encode()
        if not hmac.compare_digest(header, header_secret):
            log.warning("webhook request with a wrong secret header from %s", request.remote)
            return web.Response(status=401)
        body = await request.read()
        try:
            update = Update.model_validate(json.loads(body), context={"bot": bot})
        except (ValueError, ValidationError):
            log.warning("webhook: unreadable update skipped (%d bytes)", len(body))
            return web.Response(status=200)
        if self._slots.locked():
            return web.Response(status=503)  # Telegram retries later — natural backpressure
        await self._slots.acquire()
        self._spawn_handler(bot, update)
        return web.Response(status=200)

    async def _handle_ping(self, request: web.Request) -> web.Response:
        nonce = request.match_info["nonce"]
        if nonce not in self._ping_nonces:
            raise web.HTTPNotFound
        return web.Response(text=nonce)

    async def _check_reachable(self, cfg: BotConfig) -> None:
        assert cfg.public_url is not None
        nonce = secrets.token_urlsafe(16)
        self._ping_nonces.add(nonce)
        url = f"{cfg.public_url}/tg/ping/{nonce}"
        try:
            async with (
                ClientSession(timeout=ClientTimeout(total=self.request_timeout)) as http,
                http.get(url, allow_redirects=False) as resp,
            ):
                body = await resp.text()
                ok = resp.status == 200 and body == nonce
                reason = f"HTTP {resp.status}"
        except (ClientError, OSError, ValueError) as exc:
            ok, reason = False, type(exc).__name__
        finally:
            self._ping_nonces.discard(nonce)
        if not ok:
            raise ProbeError(
                _TXT["unreachable"].format(url=cfg.public_url, reason=reason),
                _TXT["unreachable_hint"],
                fix_action=fix_setting("PUBLIC_URL"),
            )

    # ------------------------------------------------------------------ update handling

    def _spawn_handler(self, bot: Bot, update: Update) -> None:
        """Run one update in its own task; the caller already holds a slot of ``_slots``."""
        self._updates_total += 1
        self._last_update_at = now()
        task = asyncio.get_running_loop().create_task(self._process(bot, update))
        self._handlers.add(task)
        task.add_done_callback(self._handler_done)

    def _handler_done(self, task: asyncio.Task[None]) -> None:
        self._handlers.discard(task)
        self._slots.release()

    async def _process(self, bot: Bot, update: Update) -> None:
        try:
            await self.dp.feed_update(bot, update, **self._workflow)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("update %s failed", update.update_id)
            await self._capture(exc, "tg:update", "update skipped", update_id=update.update_id)

    async def _drain(self) -> None:
        if not self._handlers:
            return
        _done, pending = await asyncio.wait(set(self._handlers), timeout=self.drain_timeout)
        for task in pending:
            task.cancel()
        if pending:
            log.warning(
                "cancelled %d update handlers still running after %.0fs", len(pending), self.drain_timeout
            )
            await asyncio.gather(*pending, return_exceptions=True)

    async def _capture(self, exc: BaseException, place: str, handled: str, **context: Any) -> None:
        if self.hub is None:
            return
        try:
            await self.hub.capture(exc, place, module="bot", context=context or None, handled=handled)
        except Exception:
            log.exception("error hub failed while capturing %s", place)

    # ------------------------------------------------------------------ helpers

    def _resolve_allowed(self) -> list[str]:
        if self._allowed_updates is not None:
            return list(self._allowed_updates)
        return self.dp.resolve_used_update_types()

    def _hook_kwargs(self, bot: Bot) -> dict[str, Any]:
        return {
            **self.dp.workflow_data,
            **self._workflow,
            "bot": bot,
            "bots": [bot],
            "dispatcher": self.dp,
            "runner": self,
        }

    def _check_mode_config(self, cfg: BotConfig) -> None:
        if cfg.mode != "webhook":
            return
        if not cfg.public_url:
            raise ProbeError(_TXT["need_public_url"], fix_action=fix_setting("PUBLIC_URL"))
        scheme = urlsplit(cfg.public_url).scheme
        if scheme != "https" and not (self.allow_http_webhook and scheme == "http"):
            raise ProbeError(_TXT["public_url_https"], fix_action=fix_setting("PUBLIC_URL"))
        if not cfg.webhook_secret or not _WEBHOOK_SECRET_RE.match(cfg.webhook_secret):
            raise ProbeError(_TXT["need_secret"], fix_action=fix_setting("WEBHOOK_SECRET"))

    def _default_factory(self, cfg: BotConfig) -> Bot:
        api = TelegramAPIServer.from_base(cfg.api_url) if cfg.api_url else PRODUCTION
        session = AiohttpSession(api=api, proxy=cfg.proxy) if cfg.proxy else AiohttpSession(api=api)
        return Bot(cfg.token, session=session, default=self._default)

    def _build_bot(self, cfg: BotConfig) -> Bot:
        try:
            bot = self._factory(cfg)
        except TokenValidationError:
            raise ProbeError(_TXT["bad_token_format"], fix_action=fix_setting("BOT_TOKEN")) from None
        except (ValueError, RuntimeError, TypeError) as exc:
            raise ProbeError(
                _TXT["bad_proxy"].format(reason=type(exc).__name__),
                fix_action=fix_setting("TELEGRAM_PROXY"),
            ) from None
        for mw in self._request_middlewares:
            if mw not in bot.session.middleware:  # a reused session keeps what it has
                bot.session.middleware.register(mw)
        return bot

    async def _get_me(self, bot: Bot) -> User:
        try:
            return await self._call(bot, GetMe())
        except TelegramUnauthorizedError:
            raise ProbeError(_TXT["token_rejected"], fix_action=fix_setting("BOT_TOKEN")) from None
        except TelegramNotFound:
            # some Bot API servers answer 404 for an unknown token
            raise ProbeError(_TXT["token_rejected"], fix_action=fix_setting("BOT_TOKEN")) from None
        except _TG_FAILURES as exc:
            raise TransientProbeError(
                _TXT["network"].format(reason=self._reason(exc)), _TXT["network_hint"]
            ) from None

    async def _call[R](self, bot: Bot, method: Any) -> R:
        async with asyncio.timeout(self.request_timeout):
            result: R = await bot(method, request_timeout=int(self.request_timeout))
        return result

    async def _quiet(self, aw: Awaitable[Any]) -> None:
        try:
            async with asyncio.timeout(self.request_timeout):
                await aw
        except _TG_FAILURES as exc:
            log.info("best-effort Telegram call failed: %s", self._reason(exc))

    async def _confirm_change(self, old: User, new: User) -> None:
        hook = self.confirm_bot_change
        if hook is None:
            log.warning("switching to a different bot: @%s -> @%s", old.username, new.username)
            return
        if not await hook(old, new):
            raise ProbeError(
                _TXT["other_bot"].format(new=new.username or new.id, old=old.username or old.id),
                fix_action=fix_setting("BOT_TOKEN"),
            )

    @staticmethod
    def _reason(exc: BaseException) -> str:
        if isinstance(exc, TimeoutError):
            return "таймаут"
        if isinstance(exc, TelegramNetworkError):
            return "сеть недоступна"
        if isinstance(exc, TelegramAPIError):
            return mask(exc.message)[:200]
        return type(exc).__name__

    async def _close_bot(self, bot: Bot) -> None:
        try:
            await bot.session.close()
        except (ClientError, OSError, RuntimeError) as exc:
            log.debug("closing bot session failed: %s", type(exc).__name__)

    async def _close_later(self, bot: Bot) -> None:
        await asyncio.sleep(self.session_grace)
        if bot in self._retired and self.holder.get() is not bot:
            self._retired.remove(bot)
            await self._close_bot(bot)

    def _spawn_background(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
