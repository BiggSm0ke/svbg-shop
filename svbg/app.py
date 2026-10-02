"""Composition root: builds every service once, starts them in order and stops them in reverse.

Start order (03 §8.3, stage-0 and stage-1 contracts)::

    bootstrap (.env, SECRET_KEY, wait for BOT_TOKEN) → database → crypto → settings (+ LOG_LEVEL, payment
    instance keys of extra slugs) → notifier → error hub (sink: owner DMs until the admin chat is connected)
    → content → jobs (queue, worker, scheduler) → Remnawave (component, writer jobs, reconciliation, inbox,
    importer) → admin chat service → auto-maintenance → sales (stage 2: catalog, payment instances + core +
    poller, billing, trial, required channel, receipts, admin topic events) → Telegram wiring (dispatcher,
    screen router, bot runner, the user path, UI modules: settings, status, setup wizard, admin chat, plans)
    → stage 3–4 modules (media + content export/import, promo, pages, ads, referral, pay freeze, Bedolaga
    shadow pass, owner modules' extension host: topics, jobs, tasks, bus, squad contributors, order items)
    → Telegram wiring (+ deep links on ``/start``, module screens, constructor, admin sections, ops)
    → owner modules start (``LTE``/``IP Guard``: off by default, switched by settings without a restart)
    → web server (+ ``/webhooks/remnawave``, ``/webhooks/pay/{id}/{token}``, ``/m/<token>``) → bot → job
    worker, scheduler, webhook inbox → env mirror

The web server starts *before* the bot: in webhook mode the runner verifies that ``PUBLIC_URL`` reaches
``/tg/ping`` before it switches Telegram to the webhook. A component that cannot start (bad token, Telegram
unreachable) reports ``DOWN`` and the process keeps running — the owner fixes the setting in ``.env`` and the
change is applied without a restart.

Every dependency is handed over explicitly (no globals). The database class is injectable
(:attr:`AppOptions.database_factory`) so tests can run the whole application on a real PostgreSQL.

Graceful stop: :func:`run` waits for SIGINT/SIGTERM (on Windows: Ctrl+C / Ctrl+Break), then stops the
components in reverse order, each bounded by a timeout, so a hung part cannot block the shutdown.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import importlib
import inspect
import logging
import os
import signal
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from datetime import time as dtime
from html import escape
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol

import asyncpg
import sqlalchemy as sa
from aiogram import Dispatcher, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.methods import EditMessageText

import svbg
from svbg import app_modules
from svbg.boot.envfile import EnvDocument, EnvFileError, EnvLine, RenderKey, quote, read_text, render_full
from svbg.core.attention import AttentionService
from svbg.core.bus import Event, EventBus
from svbg.core.clock import now
from svbg.core.component import ComponentRegistry, Health, HealthReport
from svbg.core.crypto import Crypto
from svbg.core.errors import BreakerState, ErrorGroupView, ErrorHub, render_report
from svbg.core.log import mask, set_level, setup_logging
from svbg.core.settings import (
    BootstrapConfig,
    BootstrapError,
    EnvMirror,
    MirrorNotice,
    Registry,
    SettingsService,
    SettingsSnapshot,
    core_registry,
    load_bootstrap,
    wait_for_token,
)
from svbg.core.settings import values as setting_values
from svbg.core.settings.bootstrap import file_values, read_bootstrap
from svbg.core.settings.envtext import HEADER, comment_lines
from svbg.core.settings.registry import PAYMENTS_SECTION
from svbg.core.tables import admin_audit
from svbg.jobs.worker import PermanentJobError, RetryJob
from svbg.remnawave import ErrorKind, PanelNotConfiguredError, RemnawaveComponent, RemnawaveError
from svbg.remnawave.contributors import SquadContributors
from svbg.remnawave.importer import PanelImporter
from svbg.remnawave.inbox import InboxProcessor
from svbg.remnawave.sync import TICK_S, Reconciler
from svbg.remnawave.tables import import_runs, rw_inbox
from svbg.remnawave.transport import Lane
from svbg.remnawave.writer import PanelWriter
from svbg.tg import banner as banner_mod
from svbg.tg.banner import BannerMiddleware, BannerPolicy, banner_scope
from svbg.tg.notifier import TRANSPORT_ERRORS, Limits, Notifier, NotifierError, Priority
from svbg.tg.report import Report, num, send_report
from svbg.tg.runner import BotHolder, BotRunner, BotUnavailableError
from svbg.tg.ui.codec import CallbackCodec
from svbg.tg.ui.router import BotTransport, ScreenRouter, UiStateStore
from svbg.tg.user import UserDirectory, build_start_router
from svbg.web.app import WebServer, build_web_app, components_ready
from svbg.web.routes.remnawave import WebhookStats, remnawave_routes

if TYPE_CHECKING:
    from aiogram.methods import TelegramMethod
    from aiohttp import web

    from svbg.ads.service import AdService
    from svbg.billing.receipts import Receipts
    from svbg.billing.service import Billing
    from svbg.catalog.service import CatalogService
    from svbg.content.export_import import ContentTransfer
    from svbg.content.media import MediaLibrary, PublicMedia
    from svbg.content.store import ContentStore
    from svbg.deeplinks.service import DeeplinkService
    from svbg.ext.api import ExtensionHost
    from svbg.importers.shadow import ShadowService
    from svbg.jobs import Handler, Job, JobContext, JobQueue, JobWorker, Scheduler
    from svbg.pages.service import PageService
    from svbg.payments import InstanceRegistry, PaymentCore, Poller
    from svbg.promo.service import PromoService
    from svbg.referral.service import ReferralService
    from svbg.services.admin_chat import AdminChatService
    from svbg.services.admin_events import AdminEvents
    from svbg.services.maintenance import MaintenanceService
    from svbg.subscriptions.channel import ChannelService, Membership
    from svbg.subscriptions.devices import SubscriptionActions
    from svbg.subscriptions.trial import TrialService
    from svbg.tg.setup.wizard import WebhookActivity
    from svbg.tg.ui.context import UserCtx
    from svbg.tg.user.deeplink import DeepLink
    from svbg.tg.user.deps import PanelDevice
    from svbg.tg.user.wiring import UserPath

__all__ = [
    "DEFAULT_WEB_HOST",
    "DEFAULT_WEB_PORT",
    "App",
    "AppDeps",
    "AppError",
    "AppOptions",
    "DatabaseLike",
    "OwnerDmSink",
    "PanelEventRelay",
    "derive_key",
    "render_env_offline",
    "run",
]

log = logging.getLogger("svbg.app")

DEFAULT_WEB_HOST: Final = "0.0.0.0"  # noqa: S104 - inside the container; /health and /ready are loopback-only
DEFAULT_WEB_PORT: Final = 8080
_STOP_TIMEOUT: Final = 15.0
_BUS_GRACE_S: Final = 5.0  # background event deliveries finishing on shutdown
_DB_RETRY_MAX_DELAY: Final = 5.0
_DENIED_AUDIT_INTERVAL: Final = 60.0  # one admin_audit row per (staff member, place) per minute
_DENIED_AUDIT_MAX_KEYS: Final = 1024
_DB_HEALTH_TIMEOUT: Final = 2.0
_STOP_RESERVE: Final = 3.0  # of a stop step: releasing interrupted jobs, closing connections
_RUNNER_STOP_RESERVE: Final = 8.0  # of the bot's stop step: offset confirm (≤ 5 s), hooks, sessions
# A report into the admin chat may wait for the group's token bucket (20 messages / min) and a topic
# re-creation; the hub must not cut that off as a hung sink (admin-chat report, 07 §2.4.2).
_HUB_SINK_TIMEOUT: Final = 90.0
_PANEL_STARTED_TRIGGER: Final = "panel_started"
_WEBHOOK_ROTATION_S: Final = 86_400.0  # the previous panel webhook secret is accepted for 24 h (02 §5.7)
_RW_REFRESH_S: Final = 6 * 3600.0  # version / configuration re-read (02 §1.2)
IMPORT_JOB: Final = "remnawave.import"
# ---- stage 2 schedules
_PAY_POLL_S: Final = 2.0  # presence polling tick (D14: a check every 10 s per payment, ≤ 2 rps per instance)
_BILLING_SWEEP_S: Final = 60.0  # expired auto-complete windows, stale drafts and top-ups
_LOCATIONS_SYNC_S: Final = 600.0  # panel squads → ``locations`` (catalog)
_DAY_S: Final = 86_400.0


def _inner_budget(total: float, *, reserve: float = _STOP_RESERVE, share: float = 0.5) -> float:
    """The part of a stop step's ``total`` budget a component may spend waiting for its work to finish:
    ``total - reserve``, but never less than ``share`` of it (small budgets in tests)."""
    return max(total * share, total - reserve)


# Optional modules of other parts of the bot, wired when present (stage 0: settings screens, owner link;
# stage 1: setup wizard, «Состояние», admin chat connection + error reports into its topics).
OPTIONAL_UI_MODULES: Final[tuple[str, ...]] = (
    "svbg.tg.setup.owner",
    "svbg.tg.admin.settings",
    "svbg.tg.setup.wizard",
    "svbg.tg.admin.status",
    "svbg.tg.admin.connect_chat",
    "svbg.tg.admin.plans",
    # stage 3: admin users / roles, the admin home and its sections (statistics, staff command menus, the
    # search by a typed id: before svbg.support), cash desks, constructor, broadcasts, deep links, ops
    "svbg.tg.admin.users",
    "svbg.tg.admin.roles",
    "svbg.tg.admin.menu",
    "svbg.tg.admin.payments",
    "svbg.tg.admin.content",
    "svbg.tg.admin.broadcasts",
    "svbg.tg.admin.deeplinks",
    "svbg.ops.module",
    # 07 §2.4.6: support tickets in forum topics (SUPPORT_MODE=tickets|both; link mode needs nothing)
    "svbg.support",
)
#: Modules whose aiogram router must come before the user path's (their in-memory capture of an admin's photo
#: or text must win over the receipt handler of ``chat_payments``).
EARLY_ROUTER_MODULES: Final[frozenset[str]] = frozenset({"svbg.tg.admin.content"})

_TXT: Final = {
    "no_db_url": "DATABASE_URL не задан: впишите его в {path} или в окружение контейнера",
    "db_unreachable": "База данных недоступна ({error}). Проверьте, что контейнер PostgreSQL запущен "
    "и DATABASE_URL верный",
    "no_token": "BOT_TOKEN не задан: впишите его в {path}",
    "no_secret_key": "SECRET_KEY не задан и не сохранён в {path}: проверьте права на файл "
    "и перезапустите бота",
    "started": "✅ SvBG Shop v{version} запущен за {seconds:.1f} с",
    "bot_ok": "Бот: {summary}",
    "problems": "⚠️ Требует внимания:",
    "restart": "♻️ Ждут перезапуска: {keys}",
    "no_owner": "Владелец не задан (OWNER_IDS пуст). Получите ссылку владельца командой: svbg owner-link",
    "db_health_ok": "PostgreSQL на связи",
    "db_health_down": "База данных недоступна ({error}). Проверьте контейнер PostgreSQL",
    "db_health_timeout": "База данных не ответила за {seconds:g} с. Проверьте контейнер PostgreSQL",
    "rw_bad_settings": "Настройки панели Remnawave не применены: {error}. Исправьте REMNAWAVE_URL / "
    "REMNAWAVE_TOKEN в .env или мастером /setup",
    "import_no_panel": "Импорт из панели невозможен: панель не подключена (REMNAWAVE_URL, REMNAWAVE_TOKEN)",
    "import_bad_mode": "Импорт из панели: неизвестный режим {mode!r} (ожидался dry_run или apply)",
    "import_done": "Импорт из панели",
    "mode_dry_run": "проверка без записи",
    "mode_apply": "запись",
}


def import_summary(mode: str, report: Any) -> Report:
    """The summary of a panel import run (``remnawave.import``) for the admin chat or the owners."""
    rep = Report("📥", _TXT["import_done"], subtitle=_TXT.get(f"mode_{mode}", mode))
    rep.line("Пользователей в панели", num(report.total))
    rep.line("Создано подписок", num(report.subscriptions_created))
    rep.line("Новых пользователей бота", num(report.users_created))
    rep.line("Уже были связаны", num(report.already_linked))
    return rep.line("Конфликтов", num(report.conflicts))


class AppError(Exception):
    """The application cannot start; the message is owner-facing (Russian) and contains no secrets."""


class DatabaseLike(Protocol):
    """What the application needs from :class:`svbg.db.engine.Database`."""

    async def start(self) -> None: ...

    async def close(self) -> None: ...

    def tx(self) -> contextlib.AbstractAsyncContextManager[Any]: ...

    def read(self) -> contextlib.AbstractAsyncContextManager[Any]: ...

    async def listen(self, channel: str, callback: Callable[[str, str], Any]) -> Any: ...

    async def notify(self, channel: str, payload: str = "") -> None: ...


DatabaseFactory = Callable[[str], DatabaseLike]
SetupHook = Callable[["App"], Awaitable[None] | None]


def _default_database(dsn: str) -> DatabaseLike:
    from svbg.db.engine import Database  # imported lazily: SQLAlchemy asyncio needs greenlet

    return Database(dsn)


def derive_key(secret_key: str, purpose: str, size: int = 32) -> bytes:
    """A stable sub-key of ``SECRET_KEY`` for one purpose (HMAC-SHA256); never reveals the master key."""
    digest = hmac.new(secret_key.encode(), f"svbg:{purpose}".encode(), hashlib.sha256).digest()
    return digest[:size]


@dataclass(frozen=True)
class AppOptions:
    """Process-level options (not owner settings): paths, ports, test hooks."""

    env_path: Path
    environ: Mapping[str, str]
    web_host: str = DEFAULT_WEB_HOST
    web_port: int = DEFAULT_WEB_PORT
    configure_logging: bool = True
    log_json: bool = False
    wait_for_token: bool = True
    token_poll_interval: float = 2.0
    db_connect_timeout: float = 60.0
    mirror_poll_interval: float = 2.0
    mirror_debounce: float = 0.3
    jobs_poll_interval: float = 5.0
    notify_owners_on_start: bool = True
    notifier_limits: Limits | None = None
    database_factory: DatabaseFactory | None = None
    setup_hooks: Sequence[SetupHook] = ()
    optional_modules: Sequence[str] = OPTIONAL_UI_MODULES
    stop_timeout: float = _STOP_TIMEOUT
    runner_kwargs: Mapping[str, Any] = field(default_factory=dict)
    #: Test hooks of stage-1 services (production uses the defaults of each class).
    remnawave_kwargs: Mapping[str, Any] = field(default_factory=dict)  # RemnawaveComponent(...)
    maintenance_kwargs: Mapping[str, Any] = field(default_factory=dict)  # MaintenanceService(...)
    admin_chat_kwargs: Mapping[str, Any] = field(default_factory=dict)  # AdminChatService(...)
    inbox_poll_interval: float = 5.0
    hub_kwargs: Mapping[str, Any] = field(default_factory=dict)  # ErrorHub(...)

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] | None = None, **overrides: Any) -> AppOptions:
        """Options from the process environment: ``DATA_DIR``, ``SVBG_WEB_HOST``, ``SVBG_WEB_PORT``,
        ``SVBG_LOG_JSON``."""
        from svbg.core.settings.bootstrap import default_env_path

        env = dict(os.environ if environ is None else environ)
        port_raw = env.get("SVBG_WEB_PORT", "").strip()
        try:
            port = int(port_raw) if port_raw else DEFAULT_WEB_PORT
        except ValueError:
            raise AppError(f"SVBG_WEB_PORT должен быть числом, сейчас: {port_raw!r}") from None
        if not 0 <= port <= 65535:
            raise AppError(f"SVBG_WEB_PORT вне диапазона 0–65535: {port}")
        kwargs: dict[str, Any] = {
            "env_path": default_env_path(env),
            "environ": env,
            "web_host": env.get("SVBG_WEB_HOST", "").strip() or DEFAULT_WEB_HOST,
            "web_port": port,
            "log_json": env.get("SVBG_LOG_JSON", "").strip().lower() in ("1", "true", "yes"),
        }
        kwargs.update(overrides)
        return cls(**kwargs)


@dataclass(frozen=True)
class AppDeps:
    """What UI modules (``setup(router, deps)``) may use. Interfaces only; no globals."""

    db: DatabaseLike
    settings: SettingsService
    registry: Registry
    components: ComponentRegistry
    hub: ErrorHub
    notifier: Notifier
    holder: BotHolder
    users: UserDirectory
    content: ContentStore
    screens: ScreenRouter
    attention: AttentionService
    bus: EventBus
    crypto: Crypto
    env_path: Path
    mirror: Callable[[], EnvMirror | None]
    runner: Callable[[], BotRunner | None]
    #: ``on_stop(name, fn)``: a module registers a graceful-stop step (e.g. wait for background work). Steps
    #: run first on shutdown, while the bot, the database and the notifier are still up.
    on_stop: Callable[[str, Callable[[], Awaitable[Any]]], None] = field(default=lambda _name, _fn: None)
    # ---- stage 1 (None when the part could not be built; modules check before use)
    remnawave: RemnawaveComponent | None = None
    #: ``post(kind, text, …)`` into the admin supergroup topics (owner DMs while no chat is connected).
    admin_chat: AdminChatService | None = None
    #: ``.active`` — purchase paths show «техработы» (a plain attribute read).
    maintenance: MaintenanceService | None = None
    queue: JobQueue | None = None
    scheduler: Scheduler | None = None
    started_at: float | None = None  # time.monotonic() of the process start («Состояние»: uptime)
    #: What the panel webhook receiver saw (setup wizard checklist).
    webhook_seen: Callable[[], Awaitable[WebhookActivity | None]] | None = None
    importer: PanelImporter | None = None
    reconciler: Reconciler | None = None
    # ---- stage 2 (None when sales could not be built)
    catalog: CatalogService | None = None  # plans, prices, locations (in-memory snapshot)
    payments: PaymentCore | None = None
    billing: Billing | None = None
    poller: Poller | None = None
    receipts: Receipts | None = None
    # ---- stages 3–4 (None when the part could not be built)
    #: ``PUBLIC_URL`` of the moment (preview links of the constructor, ``/m/<token>``).
    public_url: Callable[[], str | None] = field(default=lambda: None)
    media: MediaLibrary | None = None  # constructor uploads (``DATA_DIR/media``)
    public_media: PublicMedia | None = None  # ``/m/<token>`` ids
    content_transfer: ContentTransfer | None = None  # ``content.zip`` export / whole import / undo
    #: ``content_export(dest)``: ``content.zip`` into ``dest`` (ops: backups include it).
    content_export: Callable[[Path], Awaitable[Any]] | None = None
    #: ``register_job(kind, handler)``: a module's durable job handler (``broadcast.run`` …).
    register_job: Callable[[str, Handler], None] | None = None
    extensions: ExtensionHost | None = None  # owner modules (X13 rights for the roles editor)
    deeplinks: DeeplinkService | None = None
    promo: PromoService | None = None
    pages: PageService | None = None
    ads: AdService | None = None
    referral: ReferralService | None = None
    #: ``settings_notes(section_id)``: extra HTML lines for a settings section (a payment's webhook address).
    settings_notes: Callable[[str], list[str]] | None = None

    async def owner_ids(self) -> frozenset[int]:
        return await self.users.owner_ids()


class _Throttle:
    """``allow(key)`` is true at most once per ``interval`` seconds per key; memory stays bounded."""

    def __init__(self, interval: float, max_keys: int) -> None:
        self._interval = interval
        self._max_keys = max_keys
        self._seen: dict[Any, float] = {}

    def allow(self, key: Any) -> bool:
        now = time.monotonic()
        last = self._seen.get(key)
        if last is not None and now - last < self._interval:
            return False
        if last is None and len(self._seen) >= self._max_keys:
            self._seen = {k: t for k, t in self._seen.items() if now - t < self._interval}
            while len(self._seen) >= self._max_keys:  # still full: forget the oldest
                del self._seen[next(iter(self._seen))]
        self._seen.pop(key, None)
        self._seen[key] = now
        return True


class _FlaggedUsers:
    """The user directory for the ``/start`` router: ``load`` adds the module flags (``flag:promo`` …) like
    the screen router's loader, everything else is the directory itself."""

    def __init__(self, users: UserDirectory, load: Callable[[Any], Awaitable[UserCtx | None]]) -> None:
        self._users = users
        self.load = load

    def __getattr__(self, name: str) -> Any:
        return getattr(self._users, name)


class DatabaseComponent:
    """The ``database`` component: ``/ready`` and the attention list see a PostgreSQL outage.

    ``health()`` runs ``select 1`` bounded by ``timeout`` seconds; a connection error, a database error or the
    timeout is ``DOWN`` (a hung check must not be reported as ``UNKNOWN``, which ``/ready`` treats as ready).
    There is nothing to probe or reconfigure: ``DATABASE_URL`` needs a restart.
    """

    name = "database"

    def __init__(self, db: DatabaseLike, *, timeout: float = _DB_HEALTH_TIMEOUT) -> None:
        self._db = db
        self._timeout = timeout

    async def probe(self, candidate: Mapping[str, Any]) -> None:
        return None

    async def reconfigure(self, cfg: Mapping[str, Any]) -> None:
        return None

    async def health(self) -> HealthReport:
        began = time.perf_counter()
        try:
            async with asyncio.timeout(self._timeout):
                async with self._db.read() as conn:
                    await conn.execute(sa.text("select 1"))
        except TimeoutError:
            return HealthReport.down(_TXT["db_health_timeout"].format(seconds=self._timeout))
        except (OSError, sa.exc.SQLAlchemyError, asyncpg.PostgresError, asyncpg.InterfaceError) as exc:
            return HealthReport.down(_TXT["db_health_down"].format(error=type(exc).__name__))
        latency_ms = round((time.perf_counter() - began) * 1000, 1)
        return HealthReport.ok(_TXT["db_health_ok"], latency_ms=latency_ms)


# --------------------------------------------------------------------------------------- owner DMs


class OwnerDmSink:
    """Stage-0 :class:`~svbg.core.errors.ErrorSink`: reports go to every owner's private chat.

    ``msg_ref`` = ``{"dm": {"<chat_id>": <message_id>}}``; updates edit those messages in place.
    """

    def __init__(self, notifier: Notifier, owners: Callable[[], Awaitable[frozenset[int]]]) -> None:
        self._notifier = notifier
        self._owners = owners

    async def send_new(self, view: ErrorGroupView) -> dict[str, Any] | None:
        text = render_report(view)
        refs: dict[str, int] = {}
        owners = await self._owners()
        if not owners:
            log.warning("error report not delivered: no owner is configured (OWNER_IDS)")
            return None
        for chat_id in sorted(owners):
            try:
                with banner_scope("text"):  # the report is edited when it repeats: keep it a text message
                    msg = await self._notifier.send(
                        chat_id, text, parse_mode="HTML", priority=Priority.CRITICAL
                    )
            except (TelegramAPIError, NotifierError, BotUnavailableError, *TRANSPORT_ERRORS) as exc:
                log.warning("error report to an owner failed: %s", type(exc).__name__)
                continue
            if msg is not None:
                refs[str(chat_id)] = msg.message_id
        return {"dm": refs} if refs else None

    async def update(self, view: ErrorGroupView, msg_ref: Any) -> None:
        refs = msg_ref.get("dm") if isinstance(msg_ref, Mapping) else None
        if not isinstance(refs, Mapping):
            return
        text = render_report(view)
        for chat_raw, message_id in refs.items():
            try:
                chat_id = int(chat_raw)
                method = EditMessageText(
                    chat_id=chat_id, message_id=int(message_id), text=text, parse_mode="HTML"
                )
                await self._notifier.call(method, chat_id=chat_id, priority=Priority.CRITICAL)
            except TelegramBadRequest as exc:
                if "not modified" not in (exc.message or "").lower():
                    log.warning("error report update rejected: %s", exc.message)
            except (
                TelegramAPIError,
                NotifierError,
                BotUnavailableError,
                ValueError,
                *TRANSPORT_ERRORS,
            ) as exc:
                log.warning("error report update failed: %s", type(exc).__name__)


# --------------------------------------------------------------------------------------- panel events


class _WebhookStats(WebhookStats):
    """Webhook route counters plus the time of the last bad signature (wizard: «секрет не совпадает»)."""

    last_bad_signature_at: datetime | None

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "bad_signature" and isinstance(value, int) and value > getattr(self, name, value):
            object.__setattr__(self, "last_bad_signature_at", now())
        object.__setattr__(self, name, value)


_PANEL_TEXT: Final[dict[str, str]] = {
    "node.connection_lost": "🔴 Нода «{node}» потеряла связь с панелью",
    "node.connection_restored": "🟢 Нода «{node}» снова на связи",
    "node.traffic_notify": "📶 Нода «{node}»: превышен порог трафика",
    "service.panel_started": "🔄 Панель Remnawave перезапущена{version}",
    "service.api_token_deleted": "⛔ В панели удалён API-токен «{token}». Если это токен бота — создайте "
    "новый в панели и вставьте его: «⚙️ Настройки → Remnawave» или /setup",
    "service.login_attempt_failed": "🔐 Неудачный вход в панель: {login}",
    "errors.bandwidth_usage_threshold_reached_max_notifications": "⚠️ Панель: {description}",
    "torrent_blocker.report": "🧲 Торрент-блокер: пользователь {user} на ноде «{node}»",
    "remnawave.token.expiring": "⏳ {message}",
    "remnawave.version.detected": "ℹ️ {message}",
}
_PANEL_HIGH: Final = frozenset(
    {"node.connection_lost", "service.api_token_deleted", "remnawave.token.expiring"}
)


class PanelEventRelay:
    """Owner-relevant panel events from the bus → the admin chat topic «🖥 Панель и ноды» (07 §2.4.2).

    Sources: the webhook inbox (``remnawave.<panel event>`` with ``owner=True``) and the panel component
    (token expiry warnings, an unusual panel version). Values from the panel are HTML-escaped; nothing else
    from the payload is shown (no ids, no IP addresses). Without an admin chat the service itself falls back
    to the owners' private chats.
    """

    def __init__(self, post: Callable[..., Awaitable[Any]]) -> None:
        self._post = post

    def install(self, bus: EventBus) -> Callable[[], None]:
        off = [bus.subscribe("remnawave.*", self.on_event)]

        def uninstall() -> None:
            for fn in off:
                fn()

        return uninstall

    @staticmethod
    def render(event: Event) -> str | None:
        payload = event.payload if isinstance(event.payload, Mapping) else {}
        name = event.name.removeprefix("remnawave.")
        if event.name in ("remnawave.token.expiring", "remnawave.version.detected"):
            if event.name.endswith("version.detected") and payload.get("support") == "full":
                return None
            name = event.name
        elif not payload.get("owner"):
            return None
        template = _PANEL_TEXT.get(name)
        if template is None:
            return None

        def field_(src: Any, key: str) -> str:
            value = src.get(key) if isinstance(src, Mapping) else None
            return escape(str(value))[:200] if value not in (None, "") else "?"

        node, token = payload.get("node"), payload.get("apiToken")
        version = payload.get("panelVersion")
        return template.format(
            node=field_(node, "name"),
            token=field_(token, "tokenName"),
            login=field_(payload.get("loginAttempt"), "username"),
            user=field_(payload.get("user"), "username"),
            description=field_(payload, "description"),
            message=field_(payload, "message"),
            version=f" (версия {escape(str(version))[:64]})" if version else "",
        )

    async def on_event(self, event: Event) -> None:
        text = self.render(event)
        if text is None:
            return
        from svbg.services.admin_chat import K_PANEL

        name = event.name.removeprefix("remnawave.")
        priority = Priority.HIGH if name in _PANEL_HIGH or event.name in _PANEL_HIGH else None
        await self._post(K_PANEL, text, html=True, priority=priority)


# --------------------------------------------------------------------------------------- .env offline


def _rename_aliases(doc: EnvDocument, registry: Registry) -> None:
    present = {line.key for line in doc.lines if line.kind == "kv"}
    for defn in registry.all():
        for alias in defn.aliases:
            if alias not in present:
                continue
            if defn.key in present:
                doc.remove(alias)
                continue
            for index, line in enumerate(doc.lines):
                if line.kind == "kv" and line.key == alias:
                    value = line.value or ""
                    doc.lines[index] = EnvLine("kv", f"{defn.key}={quote(value)}", defn.key, value, line.eol)
            present.add(defn.key)


def render_env_offline(
    env_path: Path, environ: Mapping[str, str], *, registry: Registry | None = None
) -> str:
    """The full ``.env`` without a database (``svbg env init`` / ``env render``, first start).

    Values already in the file are kept as they are; missing keys get their registry default. The owner's own
    lines and unknown keys are preserved (same renderer as the live mirror).
    """
    reg = registry or app_modules.full_registry()
    try:
        text = read_text(env_path)
    except (EnvFileError, OSError) as exc:
        raise AppError(f"Файл {env_path} не читается: {exc}") from None
    doc = EnvDocument.parse(text) if text is not None else None
    if doc is not None:
        _rename_aliases(doc, reg)
    in_file = file_values(doc, reg)
    boot = read_bootstrap(env_path, environ, registry=reg)
    keys: list[RenderKey] = []
    for defn in reg.all():
        if not defn.in_file:
            continue
        value = in_file[defn.key] if defn.key in in_file else setting_values.to_text(defn, defn.default)
        keys.append(
            RenderKey(defn.key, value, comment_lines(defn, locked=defn.key in boot.locked), defn.section)
        )
    return render_full(reg.env_sections, keys, list(HEADER), existing=doc)


# --------------------------------------------------------------------------------------- the app


class App:
    """All services of one bot process. ``await start()`` … ``await stop()`` (idempotent)."""

    def __init__(self, options: AppOptions) -> None:
        self.options = options
        self.registry: Registry = core_registry()
        self.boot: BootstrapConfig | None = None
        self.db: DatabaseLike | None = None
        self.crypto: Crypto | None = None
        self.components = ComponentRegistry(on_health_error=self._on_health_error)
        self.settings: SettingsService | None = None
        self.holder = BotHolder()
        self.notifier: Notifier | None = None
        self.users: UserDirectory | None = None
        self.hub: ErrorHub | None = None
        self.bus = EventBus(on_error=self._on_bus_error)
        self.attention: AttentionService | None = None
        self.content: ContentStore | None = None
        self.queue: JobQueue | None = None
        self.worker: JobWorker | None = None
        self.scheduler: Scheduler | None = None
        self.job_handlers: dict[str, Handler] = {}
        self.screens: ScreenRouter | None = None
        self.banner: BannerPolicy | None = None
        self.dispatcher: Dispatcher | None = None
        self.runner: BotRunner | None = None
        self.web: WebServer | None = None
        self.mirror: EnvMirror | None = None
        self.deps: AppDeps | None = None
        # ---- stage 1
        self.remnawave: RemnawaveComponent | None = None
        self.contributors: SquadContributors | None = None
        self.panel_writer: PanelWriter | None = None
        self.inbox: InboxProcessor | None = None
        self.reconciler: Reconciler | None = None
        self.importer: PanelImporter | None = None
        self.admin_chat: AdminChatService | None = None
        self.maintenance: MaintenanceService | None = None
        self.webhook_stats = _WebhookStats()
        # ---- stage 2
        self.catalog: CatalogService | None = None
        self.pay_instances: InstanceRegistry | None = None
        self.payments: PaymentCore | None = None
        self.poller: Poller | None = None
        self.billing: Billing | None = None
        self.channel: ChannelService | None = None
        self.trial: TrialService | None = None
        self.actions: SubscriptionActions | None = None
        self.receipts: Receipts | None = None
        self.admin_events: AdminEvents | None = None
        self.user_path: UserPath | None = None
        self._webhook_secret: str | None = None
        self._webhook_previous: tuple[str, float] | None = None  # (secret, accepted until monotonic)
        self._began_at: float | None = None
        self.wired_modules: list[str] = []  # OPTIONAL_UI_MODULES that are wired
        self.wired_parts: list[str] = []  # stage 3–4 parts wired by the app itself (deep links, promo …)
        self.missing_modules: dict[str, str] = {}
        self.started_at: float | None = None
        self.startup_seconds: float | None = None
        self._stack: AsyncExitStack | None = None
        self._codec: CallbackCodec | None = None
        self._start_router: Router | None = None
        self._background: set[asyncio.Task[Any]] = set()
        self._restart_pass: asyncio.Future[Any] | None = None  # full sync after ``service.panel_started``
        self._module_stops: list[tuple[str, Callable[[], Awaitable[Any]]]] = []
        self._lock = asyncio.Lock()
        self._denied_throttle = _Throttle(_DENIED_AUDIT_INTERVAL, _DENIED_AUDIT_MAX_KEYS)
        # ---- stages 3–4
        self.media: MediaLibrary | None = None
        self.public_media: PublicMedia | None = None
        self.content_transfer: ContentTransfer | None = None
        self.promo: PromoService | None = None
        self.pages: PageService | None = None
        self.ads: AdService | None = None
        self.referral: ReferralService | None = None
        self.deeplinks: DeeplinkService | None = None
        self.shadow: ShadowService | None = None
        #: ``ImportConfig`` of the shadow importer (Bedolaga's ``.env``, T0, crypto); ``None`` = defaults.
        self.import_config: Callable[[], Any] | None = None
        self._pages_wired = False
        self._user_routers: list[Router] = []
        #: Set by the shadow pass when the panel token turns out to be writable (06 §4.1 p.3): panel writes
        #: are postponed (never lost: the outbox keeps them) until the process is restarted.
        self._writer_stopped: str | None = None
        # Module settings (ops, referral, shadow) and the owner modules' manifests (X12) join the registry
        # before the settings are loaded; a module that cannot be loaded is reported, never fatal.
        self.missing_modules.update(app_modules.module_settings(self.registry))
        self.ext: ExtensionHost | None
        self.ext, ext_errors = app_modules.load_extensions(self.registry)
        self.missing_modules.update(ext_errors)

    # ------------------------------------------------------------------ lifecycle

    @property
    def running(self) -> bool:
        return self._stack is not None

    async def start(self) -> None:
        async with self._lock:
            if self._stack is not None:
                return
            stack = AsyncExitStack()
            began = time.perf_counter()
            try:
                await self._start(stack)
            except BaseException:
                await self._close_stack(stack)
                raise
            self._stack = stack
            self.started_at = time.monotonic()
            self.startup_seconds = time.perf_counter() - began
            log.info("SvBG Shop %s started in %.2fs", svbg.__version__, self.startup_seconds)
            if self.options.notify_owners_on_start:
                self._spawn(self._startup_report(), "startup-report")

    async def stop(self) -> None:
        async with self._lock:
            stack, self._stack = self._stack, None
            if stack is None:
                return
            log.info("stopping SvBG Shop")
            for task in list(self._background):
                task.cancel()
            await asyncio.gather(*self._background, return_exceptions=True)
            await self._close_stack(stack)
            banner_mod.uninstall(self.banner)
            log.info("SvBG Shop stopped")

    async def _close_stack(self, stack: AsyncExitStack) -> None:
        try:
            await stack.aclose()
        except Exception:
            log.exception("error while stopping")

    def _on_stop(self, stack: AsyncExitStack, name: str, fn: Callable[[], Awaitable[Any]]) -> None:
        """Register a bounded, isolated stop step (LIFO)."""
        timeout = self.options.stop_timeout

        async def step() -> None:
            try:
                async with asyncio.timeout(timeout):
                    await fn()
            except TimeoutError:
                log.error("stopping %s took longer than %.0fs, skipped", name, timeout)  # noqa: TRY400
            except Exception:
                log.exception("stopping %s failed", name)

        stack.push_async_callback(step)

    # ------------------------------------------------------------------ start sequence

    async def _start(self, stack: AsyncExitStack) -> None:
        opts = self.options
        if opts.configure_logging:
            setup_logging("INFO", json=opts.log_json)

        self._module_stops = []
        self._began_at = time.monotonic()
        boot = await self._bootstrap()
        db = await self._connect_db(boot)
        self._on_stop(stack, "database", db.close)
        self.components.register(DatabaseComponent(db))  # /ready and attention see a PostgreSQL outage

        self.crypto = crypto = Crypto([_secret_key(boot)])
        await self._register_extra_payment_slugs(db)
        self.settings = settings = SettingsService(
            db, self.registry, crypto, self.components, environ=opts.environ, env_path=opts.env_path
        )
        snap = await settings.load()
        self._apply_log_level(snap)
        settings.subscribe(["LOG_LEVEL"], self._on_log_level)
        for key, problem in settings.problems.items():
            log.warning("settings: %s: %s", key, mask(problem))

        self.users = users = UserDirectory(db, settings)
        settings.subscribe(["OWNER_IDS"], self._on_owners_changed)
        self.notifier = notifier = Notifier(
            self.holder, limits=opts.notifier_limits, on_blocked=users.mark_blocked
        )
        self._on_stop(stack, "notifier", notifier.close)

        hub_kwargs: dict[str, Any] = {"sink_timeout": _HUB_SINK_TIMEOUT, **opts.hub_kwargs}
        self.hub = hub = ErrorHub(
            db, OwnerDmSink(notifier, users.owner_ids), on_state_change=self._on_breaker, **hub_kwargs
        )
        await hub.start()
        self._on_stop(stack, "error hub", hub.stop)
        # Background event deliveries finish (bounded) while the hub, notifier and database are still up.
        self._on_stop(stack, "event bus", lambda: self.bus.drain(_BUS_GRACE_S))

        self.attention = AttentionService(db, bus=self.bus)
        self.content = content = await self._load_content(db, snap)

        await self._build_jobs(db, hub)
        await self._build_remnawave(stack, db, hub, settings, snap)
        await self._build_admin_chat(stack, db, users, snap)
        await self._start_maintenance(stack)
        await self._build_sales(stack, db, hub, settings, snap)
        await self._build_modules(stack, db, settings)
        self._build_telegram(db=db, boot=boot, settings=settings, hub=hub, users=users, content=content)
        await self._wire_modules()
        for hook in opts.setup_hooks:
            res = hook(self)
            if res is not None:
                await res
        # Owner modules start after their screens exist; registered here, their stop runs after the bot, the
        # job worker and the scheduler stopped (LIFO), while the database is still open.
        await self._start_extensions(stack, settings)
        assert self.screens is not None and self.dispatcher is not None
        self.dispatcher.include_router(self.screens.aiogram_router())  # last: catches every callback

        assert self.runner is not None
        self.components.register(self.runner)
        self.web = web = WebServer(
            opts.web_host,
            opts.web_port,
            build_web_app(
                [
                    *self.runner.web_routes(),
                    *self._remnawave_routes(db),
                    *self._payment_routes(),
                    *self._media_routes(),
                ],
                ready_check=components_ready(self.components),
            ),
        )
        await web.start()
        self._on_stop(stack, "web server", web.stop)

        await self.runner.start()
        self._on_stop(stack, "bot", self.runner.stop)
        if self.admin_chat is not None:  # stops before the bot: queued notifications are still delivered
            self._on_stop(stack, "admin chat", self.admin_chat.stop)

        assert self.worker is not None and self.scheduler is not None
        await self.worker.start()
        # Inside the step's own budget, so running jobs are released (not cut off) before the DB closes.
        worker, job_grace = self.worker, _inner_budget(opts.stop_timeout)
        self._on_stop(stack, "job worker", lambda: worker.stop(timeout=job_grace))
        await self.scheduler.start()
        self._on_stop(stack, "scheduler", self.scheduler.stop)
        if self.poller is not None:  # an in-memory tick (no scheduler_state row every 2 s)
            self._spawn(self._payments_poll_loop(self.poller), "payments-poll")
        if self.inbox is not None:
            await self.inbox.start()
            inbox, inbox_grace = self.inbox, _inner_budget(opts.stop_timeout)
            self._on_stop(stack, "remnawave inbox", lambda: inbox.stop(timeout=inbox_grace))

        self.mirror = mirror = EnvMirror(
            settings,
            self.registry,
            opts.env_path,
            poll_interval=opts.mirror_poll_interval,
            debounce=opts.mirror_debounce,
            on_notice=self._on_mirror_notice,
        )
        await mirror.start()
        self._on_stop(stack, "env mirror", mirror.stop)
        # Registered last → run first: e.g. settings changes still running finish (and get mirrored).
        for name, fn in self._module_stops:
            self._on_stop(stack, name, fn)

        if not await users.has_owner():
            log.warning("%s", _TXT["no_owner"])

    async def _bootstrap(self) -> BootstrapConfig:
        opts = self.options
        try:
            boot = await asyncio.to_thread(
                load_bootstrap, opts.env_path, opts.environ, registry=self.registry
            )
        except BootstrapError as exc:
            raise AppError(str(exc)) from None
        for key, problem in boot.problems.items():
            log.warning("bootstrap: %s: %s", key, mask(problem))
        if not boot.has_token:
            if not opts.wait_for_token:
                raise AppError(_TXT["no_token"].format(path=opts.env_path))
            await self._write_template()
            try:  # the file may have been rewritten meanwhile: SECRET_KEY is (re)generated if it is gone
                boot = await wait_for_token(
                    opts.env_path, opts.environ, registry=self.registry, interval=opts.token_poll_interval
                )
            except BootstrapError as exc:
                raise AppError(str(exc)) from None
            if not boot.has_token:
                raise AppError(_TXT["no_token"].format(path=opts.env_path))
        if not boot.secret_key:
            raise AppError(_TXT["no_secret_key"].format(path=opts.env_path))
        if not boot.database_url:
            raise AppError(_TXT["no_db_url"].format(path=opts.env_path))
        self.boot = boot
        return boot

    async def _write_template(self) -> None:
        """Before the first start the owner gets the full, commented file to fill in (07 §3.2)."""
        from svbg.boot.envfile import write_atomic

        opts = self.options
        try:
            text = await asyncio.to_thread(
                render_env_offline, opts.env_path, opts.environ, registry=self.registry
            )
            current = await asyncio.to_thread(read_text, opts.env_path)
            if text != current:
                await asyncio.to_thread(write_atomic, opts.env_path, text)
        except (AppError, EnvFileError, OSError) as exc:
            log.warning("cannot write the .env template: %s", mask(str(exc)))

    async def _connect_db(self, boot: BootstrapConfig) -> DatabaseLike:
        assert boot.database_url is not None
        factory = self.options.database_factory or _default_database
        db = factory(boot.database_url)
        deadline = time.monotonic() + self.options.db_connect_timeout
        delay = 0.5
        while True:
            try:
                await db.start()
            except (OSError, sa.exc.SQLAlchemyError, ConnectionError) as exc:
                error = mask(f"{type(exc).__name__}: {exc}")[:300]
                if time.monotonic() + delay > deadline:
                    await _quiet_close(db)
                    raise AppError(_TXT["db_unreachable"].format(error=error)) from None
                log.warning("database is not reachable yet (%s), retrying in %.1fs", error, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, _DB_RETRY_MAX_DELAY)
            except Exception as exc:
                await _quiet_close(db)
                raise AppError(_TXT["db_unreachable"].format(error=type(exc).__name__)) from exc
            else:
                self.db = db
                return db

    async def _load_content(self, db: DatabaseLike, snap: SettingsSnapshot) -> ContentStore:
        from svbg.content.store import ContentStore

        assert self.boot is not None
        # media_root: the default banner (a placeholder picture on every screen until the owner removes it)
        content = ContentStore(
            db,
            default_lang=str(snap["DEFAULT_LANGUAGE"] or "ru"),
            media_root=self.boot.data_dir / "media",
            preview_available=lambda: bool(self._public_url()),
        )
        await content.load()
        return content

    async def _build_jobs(self, db: DatabaseLike, hub: ErrorHub) -> None:
        from svbg.jobs import JobQueue, JobWorker, Scheduler

        self.queue = queue = JobQueue(db)
        self.worker = JobWorker(
            db,
            queue,
            self.job_handlers,
            hub=hub,
            poll_interval=self.options.jobs_poll_interval,
            deps={"app": self},
        )
        self.scheduler = scheduler = Scheduler(db, hub)
        scheduler.every("jobs.purge", 3600, self._purge_jobs, jitter_s=60)
        scheduler.every("ui.short_tokens.purge", 86_400, self._purge_short_tokens, jitter_s=600)
        scheduler.every("settings.audit.purge", 86_400, self._purge_settings_audit, jitter_s=600)
        scheduler.every("attention.purge", 86_400, self._purge_attention, jitter_s=600)
        scheduler.every("components.health", 60, self._sync_health, jitter_s=5)

    # ------------------------------------------------------------------ stage 1: panel, admin chat

    async def _build_remnawave(
        self,
        stack: AsyncExitStack,
        db: DatabaseLike,
        hub: ErrorHub,
        settings: SettingsService,
        snap: SettingsSnapshot,
    ) -> None:
        """The panel component (RELOAD ``remnawave``), the writer's job handlers, reconciliation, the webhook
        inbox and the importer. Nothing here waits for the panel: an unreachable or misconfigured panel is a
        ``DOWN`` component, never a failed start."""
        assert self.scheduler is not None
        rw = RemnawaveComponent(bus=self.bus, **self.options.remnawave_kwargs)
        self.remnawave = rw
        self.components.register(rw)
        # Registered early, so it runs late: the worker and the inbox (users of the client) stop first.
        self._on_stop(stack, "remnawave", rw.aclose)
        try:
            await rw.reconfigure(snap)
        except RuntimeError as exc:  # invalid settings (e.g. URL without a token): reported, not fatal
            log.warning("%s", mask(_TXT["rw_bad_settings"].format(error=exc)))
            await hub.capture(exc, "app:remnawave", module="remnawave", handled="панель не подключена")

        def api() -> Any:
            return rw.client

        database: Any = db  # the stage-1 services are typed against svbg.db.engine.Database
        self.contributors = contributors = SquadContributors(self.attention)
        self.panel_writer = writer = PanelWriter(
            database, api, contributors=contributors, attention=self.attention, bus=self.bus
        )
        self.job_handlers.update({kind: self._writer_gate(fn) for kind, fn in writer.handlers().items()})
        self.reconciler = reconciler = Reconciler(
            database,
            api,
            contributors=contributors,
            attention=self.attention,
            bus=self.bus,
            webhooks_enabled=self._webhooks_enabled,
            full_with_webhooks=timedelta(minutes=int(snap["REMNAWAVE_SYNC_MINUTES"])),
            full_without_webhooks=timedelta(minutes=int(snap["REMNAWAVE_SYNC_NO_WEBHOOKS_MINUTES"])),
        )
        self.inbox = inbox = InboxProcessor(
            database,
            api,
            contributors=contributors,
            attention=self.attention,
            bus=self.bus,
            hub=hub,
            on_panel_started=self._on_panel_started,
            on_mass=lambda: reconciler.full_pass("webhooks"),
            poll_interval=self.options.inbox_poll_interval,
        )
        self.importer = PanelImporter(database, api, contributors=contributors)
        self.job_handlers[IMPORT_JOB] = self._import_job

        scheduler = self.scheduler
        scheduler.every("remnawave.sync", TICK_S, self._rw_sync_tick, jitter_s=15, run_at_start=True)
        scheduler.every("remnawave.refresh", _RW_REFRESH_S, self._rw_refresh, jitter_s=300)
        inbox.register(scheduler)

        self._webhook_secret = _opt_secret(snap["REMNAWAVE_WEBHOOK_SECRET"])
        settings.subscribe(["REMNAWAVE_WEBHOOK_SECRET"], self._on_webhook_secret)

    async def _build_admin_chat(
        self, stack: AsyncExitStack, db: DatabaseLike, users: UserDirectory, snap: SettingsSnapshot
    ) -> None:
        """The admin supergroup service (component ``admin_chat``). Its screens and the error-report sink are
        wired by ``svbg.tg.admin.connect_chat``; without that module the hub keeps reporting to owner DMs."""
        assert self.notifier is not None
        try:
            from svbg.services.admin_chat import AdminChatService

            service = AdminChatService(
                db,  # type: ignore[arg-type]
                self.notifier,
                self.holder,
                owners=users.owner_ids,
                **self.options.admin_chat_kwargs,
            )
            self.components.register(service)
            await service.start()
            await service.reconfigure(snap)
        except Exception as exc:  # noqa: BLE001 - isolation boundary: the bot works without the admin chat
            await self._module_failed("svbg.services.admin_chat", exc, "setup")
            return
        self.admin_chat = service
        # Safety net for a start that fails later; normally the service stops before the bot (see _start).
        self._on_stop(stack, "admin chat pump", lambda: service.stop(grace=0))
        PanelEventRelay(service.post).install(self.bus)

    async def _start_maintenance(self, stack: AsyncExitStack) -> None:
        """Auto «техработы» for purchases while the panel breaker is open > 3 min (``MAINTENANCE_MODE``)."""
        try:
            from svbg.services.maintenance import start_service

            service = await start_service(self, **self.options.maintenance_kwargs)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001 - isolation boundary: purchases simply never pause
            await self._module_failed("svbg.services.maintenance", exc, "setup")
            return
        self.maintenance = service
        self._on_stop(stack, "maintenance", service.stop)

    def _remnawave_routes(self, db: DatabaseLike) -> list[web.RouteDef]:
        inbox = self.inbox
        return remnawave_routes(
            db=db,  # type: ignore[arg-type]
            secrets=self.webhook_secrets,
            on_stored=None if inbox is None else inbox.wake,
            stats=self.webhook_stats,
        )

    def webhook_secrets(self) -> list[str]:
        """Accepted panel webhook secrets: current, the explicit previous one, the one replaced < 24 h ago.

        No current secret means webhooks are off: nothing is accepted (``401``), neither the explicit previous
        secret nor one still inside its rotation window.
        """
        if not self._webhook_secret:
            return []
        out: list[str] = [self._webhook_secret]
        if self.settings is not None:
            explicit = _opt_secret(self.settings.current()["REMNAWAVE_WEBHOOK_SECRET_PREVIOUS"])
            if explicit and explicit not in out:
                out.append(explicit)
        previous = self._webhook_previous
        if previous is not None:
            if time.monotonic() < previous[1]:
                if previous[0] not in out:
                    out.append(previous[0])
            else:
                self._webhook_previous = None
        return out

    async def _on_webhook_secret(self, snap: SettingsSnapshot, _keys: set[str]) -> None:
        """Track the current secret; only a replacement by another non-empty secret opens the 24 h window.

        Clearing the secret switches webhooks off at once (and forgets any window), so "clear, save, then set
        a new one" is also the way to rotate after a leak with no grace period for the old secret.
        """
        new = _opt_secret(snap["REMNAWAVE_WEBHOOK_SECRET"])
        old, self._webhook_secret = self._webhook_secret, new
        if new is None:
            self._webhook_previous = None
        elif old and old != new:  # rotation (mode A): the panel keeps the old secret until it is restarted
            self._webhook_previous = (old, time.monotonic() + _WEBHOOK_ROTATION_S)

    def revoke_previous_webhook_secret(self) -> bool:
        """Stop accepting the secret replaced < 24 h ago right now (rotation after a leak).

        Returns ``True`` when a rotation window was open. ``REMNAWAVE_WEBHOOK_SECRET_PREVIOUS`` is a setting
        and is cleared through the settings pipeline, not here.
        """
        had, self._webhook_previous = self._webhook_previous is not None, None
        if had:
            log.info("remnawave: the previous webhook secret was revoked before its window ended")
        return had

    def _webhooks_enabled(self) -> bool:
        """Webhooks are on when the panel says so and we can verify them (a secret is configured)."""
        rw = self.remnawave
        caps = None if rw is None else rw.capabilities
        return bool(self._webhook_secret) and caps is not None and caps.webhooks_enabled is True

    def _panel_up(self) -> bool:
        rw = self.remnawave
        return rw is not None and rw.configured and rw.breaker_state in (None, BreakerState.CLOSED)

    async def _rw_sync_tick(self) -> None:
        """Reconciliation tick; skipped while the panel is not connected or its breaker is open (the outage
        itself is reported by the component health and auto-maintenance, not by a failing task)."""
        if self.reconciler is None or not self._panel_up():
            return
        await self.reconciler.tick()

    async def _rw_refresh(self) -> None:
        if self.remnawave is None or not self._panel_up():
            return
        await self.remnawave.refresh()

    async def _on_panel_started(self) -> None:
        """``service.panel_started`` (02 §5.4/§6.2): the panel may have been updated or restored from a backup
        (its queued webhooks are then lost), so version and configuration are re-read and an unscheduled full
        reconciliation starts in the background. The inbox loop is not blocked by it; the reconciler's lease
        keeps it from overlapping a scheduled pass (a busy lease just skips this one)."""
        if self.remnawave is None or not self.remnawave.configured:
            return
        try:
            await self.remnawave.refresh()
        except RemnawaveError as err:
            log.warning("remnawave: refresh after a panel restart failed: %s", err.kind.value)
            return  # the panel is not reachable: the scheduled pass catches up once it is
        if self.catalog is not None:  # the panel may come back with other squads
            self._spawn(self._sync_locations_quietly(), "catalog-locations-after-restart")
        running = self._restart_pass
        if self.reconciler is None or (running is not None and not running.done()):
            return
        self._restart_pass = self._spawn(self._full_pass_after_restart(), "remnawave-full-after-restart")

    async def _full_pass_after_restart(self) -> None:
        reconciler = self.reconciler
        if reconciler is None:
            return
        try:
            report = await reconciler.full_pass(_PANEL_STARTED_TRIGGER)
        except RemnawaveError as err:
            log.warning("remnawave: full sync after a panel restart failed: %s", err.kind.value)
        except Exception as exc:  # isolation boundary: reported to the owner, the inbox loop goes on
            if self.hub is None:
                raise
            await self.hub.capture(exc, "remnawave:full_after_restart", module="svbg.remnawave.sync")
        else:
            log.info("remnawave: full sync after a panel restart: %s", report.status)

    async def webhook_seen(self) -> WebhookActivity | None:
        """Last stored panel webhook (``rw_inbox``) and the last rejected signature (setup wizard)."""
        from svbg.tg.setup.wizard import WebhookActivity

        if self.db is None:
            return None
        async with self.db.read() as conn:
            last_ok = await conn.scalar(sa.select(sa.func.max(rw_inbox.c.received_at)))
        bad_at = getattr(self.webhook_stats, "last_bad_signature_at", None)
        return WebhookActivity(last_ok_at=last_ok, last_bad_signature_at=bad_at)

    async def enqueue_import(
        self, mode: str = "dry_run", filters: Mapping[str, Any] | None = None
    ) -> int | None:
        """Queue a panel import (02 §7.1): ``dry_run`` counts, ``apply`` creates users and subscriptions.

        One import at a time (``dedup_key``); returns the job id or ``None`` when one is already queued.
        """
        if mode not in ("dry_run", "apply"):
            raise ValueError(_TXT["import_bad_mode"].format(mode=mode))
        assert self.queue is not None
        payload = {"mode": mode, "filters": dict(filters or {})}
        return await self.queue.enqueue(
            IMPORT_JOB, payload, queue="panel", lane="background", dedup_key=IMPORT_JOB, max_attempts=5
        )

    async def _import_job(self, job: Job, _ctx: JobContext) -> None:
        """Job ``remnawave.import``: runs the importer; a retry resumes the interrupted run (cursor)."""
        importer = self.importer
        assert importer is not None and self.db is not None
        mode = str(job.payload.get("mode") or "dry_run")
        if mode not in ("dry_run", "apply"):
            raise PermanentJobError(_TXT["import_bad_mode"].format(mode=mode))
        filters = job.payload.get("filters") or {}
        resume: int | None = None
        if job.attempts > 1:  # an earlier attempt of this job may have stopped midway
            async with self.db.read() as conn:
                resume = await conn.scalar(
                    sa.select(sa.func.max(import_runs.c.id)).where(
                        import_runs.c.status == "running",
                        import_runs.c.mode == mode,
                        import_runs.c.started_at >= job.created_at,
                    )
                )
        try:
            report = await importer.run(mode, filters=filters, resume_run_id=resume)
        except PanelNotConfiguredError as err:
            raise PermanentJobError(_TXT["import_no_panel"]) from err
        except RemnawaveError as err:
            if err.kind in (ErrorKind.AUTH, ErrorKind.FORBIDDEN_SCOPE, ErrorKind.VALIDATION):
                raise PermanentJobError(str(err)) from err
            raise
        except ValueError as err:  # unknown filters: retrying cannot help
            raise PermanentJobError(str(err)) from err
        summary = import_summary(mode, report)
        if self.admin_chat is not None:
            from svbg.services.admin_chat import K_PANEL

            await self.admin_chat.post_report(K_PANEL, summary)
        else:
            await self.notify_owners_report(summary)

    # ------------------------------------------------------------------ stage 2: sales

    async def _register_extra_payment_slugs(self, db: DatabaseLike) -> None:
        """``PAY_<SLUG>_*`` keys of instances created beyond the built-in ones (a second instance of a
        provider, 07 §4.3) must be in the registry before the settings are loaded. Best effort: on an empty
        or unmigrated database there is nothing to add."""
        from svbg.payments.providers import BUILTIN_PROVIDERS
        from svbg.payments.registry import instance_setting_defs, setting_key
        from svbg.payments.tables import payment_instances

        try:
            async with db.read() as conn:
                rows = (
                    await conn.execute(sa.select(payment_instances.c.slug, payment_instances.c.provider))
                ).all()
        except (sa.exc.SQLAlchemyError, OSError, asyncpg.PostgresError) as exc:
            log.warning("payment instances are not readable yet: %s", type(exc).__name__)
            return
        by_slug = {cls.manifest.slug: cls for cls in BUILTIN_PROVIDERS}
        for slug, provider in rows:
            cls = by_slug.get(str(provider))
            if cls is None or setting_key(str(slug), "ENABLED") in self.registry:
                continue
            for defn in instance_setting_defs(str(slug), cls, providers=list(by_slug)):
                self.registry.add(defn)

    def _settings_notes(self, sid: str) -> list[str]:
        """Lines under a «Платёжки» subsection: where the provider must send its webhooks."""
        from svbg.tg.admin.payments import webhook_hint

        prefix = f"{PAYMENTS_SECTION}."
        if not sid.startswith(prefix) or self.pay_instances is None:
            return []
        return webhook_hint(self.pay_instances, sid[len(prefix) :])[0]

    def payment_slugs(self) -> list[str]:
        """Slugs of the payment instances whose ``PAY_<SLUG>_*`` keys are registered (component per slug)."""
        out: list[str] = []
        for defn in self.registry.all():
            comp = defn.component or ""
            if comp.startswith("payments.") and comp[9:] not in out:
                out.append(comp[9:])
        return out

    async def _build_sales(
        self,
        stack: AsyncExitStack,
        db: DatabaseLike,
        hub: ErrorHub,
        settings: SettingsService,
        snap: SettingsSnapshot,
    ) -> None:
        """Catalog, payment instances (component ``payments.<slug>`` each, RELOAD of ``PAY_<SLUG>_*``), the
        payment core and its poller, billing, trial, required channel, receipts and the admin topic events.

        Nothing here talks to a provider or the panel at start: a payment instance with wrong keys is a
        ``DOWN`` component and an error report, never a failed start. A programming error in this block is
        isolated like an optional module (reported; the bot keeps working without sales)."""
        try:
            await self._build_sales_parts(stack, db, hub, settings, snap)
        except Exception as exc:  # noqa: BLE001 - isolation boundary: menus and the panel keep working
            await self._module_failed("svbg.sales", exc, "setup")
            self.catalog = self.payments = self.billing = self.poller = None
            self.receipts = self.channel = self.trial = self.actions = None

    async def _build_sales_parts(
        self,
        stack: AsyncExitStack,
        db: DatabaseLike,
        hub: ErrorHub,
        settings: SettingsService,
        snap: SettingsSnapshot,
    ) -> None:
        from svbg.billing.receipts import Receipts
        from svbg.billing.service import Billing
        from svbg.catalog.service import CatalogService
        from svbg.catalog.squads_job import ApplySquadsJob, NotifierProgress
        from svbg.payments import (
            VERIFY_JOB,
            InstanceRegistry,
            PaymentCore,
            PaymentInstanceComponent,
            Poller,
            ProviderCatalog,
        )
        from svbg.payments.providers import BUILTIN_PROVIDERS
        from svbg.services.admin_events import AdminEvents
        from svbg.subscriptions.channel import ChannelService
        from svbg.subscriptions.devices import SubscriptionActions
        from svbg.subscriptions.hooks import EventRelay
        from svbg.subscriptions.terms import CatalogTrialSource
        from svbg.subscriptions.trial import TrialService
        from svbg.tg.admin.receipts import AdminReceiptCards

        assert self.scheduler is not None and self.crypto is not None and self.notifier is not None
        database: Any = db  # the stage-2 services are typed against svbg.db.engine.Database
        current = settings.current

        self.catalog = catalog = CatalogService(database, currency=str(snap["CURRENCY"] or "RUB"))
        await catalog.load()

        self.pay_instances = instances = InstanceRegistry(
            database, self.crypto, ProviderCatalog(BUILTIN_PROVIDERS), public_url=self._public_url
        )
        await instances.load()
        self._on_stop(stack, "payment instances", instances.close)
        for slug in self.payment_slugs():
            component = PaymentInstanceComponent(instances, slug)
            self.components.register(component)
            try:
                await component.reconfigure(snap)
            except Exception as exc:  # noqa: BLE001 - wrong keys of one cash desk never stop the bot
                log.warning("payment instance %s is not applied: %s", slug, mask(str(exc))[:300])
                await hub.capture(
                    exc, f"app:payments.{slug}", module=f"payments.{slug}", handled="платёжка не подключена"
                )

        def skew_alert_count() -> int:
            try:
                return int(current()["PAY_CLOCK_SKEW_ALERT_COUNT"])
            except (KeyError, TypeError, ValueError, RuntimeError):
                return 5

        self.payments = core = PaymentCore(
            database,
            instances,
            attention=self.attention,
            admin_chat=self.admin_chat,
            bus=self.bus,
            has_domain=lambda: bool(self._public_url()),
            return_url=self._bot_link,
            clock_skew_alert_count=skew_alert_count,
        )
        self._on_stop(stack, "payment notifications", core.drain)
        self.poller = Poller(core, database)
        self.billing = billing = Billing(
            database, catalog=catalog, payments=core, config=current, attention=self.attention
        )
        billing.attach()  # on_paid / on_refunded + the X5 spending guard
        self.channel = ChannelService(database, config=current, lookup=self._member_lookup)
        from svbg.subscriptions.service import configure_username_prefix

        configure_username_prefix(lambda: current().get("PANEL_USERNAME_PREFIX"))  # live, new subs only
        stack.callback(configure_username_prefix, None)
        self.trial = TrialService(database, CatalogTrialSource(catalog), config=current, channel=self.channel)
        self.actions = SubscriptionActions(config=current)
        cards = None
        if self.admin_chat is not None:
            cards = AdminReceiptCards(database, self.admin_chat, self.notifier)
        self.receipts = Receipts(database, core, cards)

        self.job_handlers.update(EventRelay(self.bus).handlers())  # durable domain events → the bus
        self.job_handlers.update(billing.handlers())
        self.job_handlers[VERIFY_JOB] = core.verify_job
        self.job_handlers.update(
            ApplySquadsJob(database, progress=NotifierProgress(self.notifier)).handlers()
        )

        scheduler = self.scheduler
        scheduler.every("payments.events.purge", _DAY_S, self._purge_payment_events, jitter_s=600)
        scheduler.every("billing.sweep", _BILLING_SWEEP_S, billing.sweep, jitter_s=5)
        scheduler.every(
            "catalog.locations", _LOCATIONS_SYNC_S, self._sync_locations, jitter_s=30, run_at_start=True
        )
        # The first start applies REMNAWAVE_* from .env after the scheduler's run-at-start pass, and the
        # wizard connects the panel later still: sync the squads once the panel is (re)configured and once its
        # version is first detected, not 10 min later (a duplicate pass changes nothing).
        from svbg.remnawave.component import EVENT_VERSION

        def sync_soon() -> None:
            self._spawn(self._sync_locations_quietly(), "catalog-locations-on-connect")

        async def on_panel_settings(_snap: SettingsSnapshot, _keys: set[str]) -> None:
            sync_soon()

        async def on_panel_version(_event: Event) -> None:
            sync_soon()

        settings.subscribe(["REMNAWAVE_URL", "REMNAWAVE_TOKEN"], on_panel_settings)
        stack.callback(self.bus.subscribe(EVENT_VERSION, on_panel_version))

        if self.admin_chat is not None:
            self.admin_events = events = AdminEvents(
                database,
                self.admin_chat,
                instance_info=self._instance_info,
                timezone=lambda: str(current()["TIMEZONE"] or "Europe/Moscow"),
                group_ready=lambda: self.admin_chat is not None and self.admin_chat.configured,
            )
            events.install(self.bus)

    def _build_user_path(
        self,
        dp: Dispatcher,
        screens: ScreenRouter,
        users: UserDirectory,
        content: ContentStore,
        hub: ErrorHub,
    ) -> None:
        """The user path (07 §5 stage 2) on the screen router, its jobs, events and schedules, plus the
        aiogram routers of payments in the chat (Stars, receipts), the receipt cards in the admin chat and
        the required channel (``chat_member``). Without sales the stage-0 home screen stays."""
        if self.billing is None or self.payments is None or self.settings is None:
            return
        from aiogram.types import ChatMemberUpdated

        from svbg.core.errors import guard
        from svbg.tg.admin.receipts import receipts_router
        from svbg.tg.user.deps import UserPathDeps
        from svbg.tg.user.wiring import UserPath

        assert self.scheduler is not None
        deps = UserPathDeps(
            db=self.db,  # type: ignore[arg-type]
            config=self.settings.current,
            screens=screens,
            users=users,
            content=content,
            catalog=self.catalog,
            billing=self.billing,
            payments=self.payments,
            trial=self.trial,
            channel=self.channel,
            actions=self.actions,
            receipts=self.receipts,
            i_paid=self.poller.check_now if self.poller is not None else None,
            fetch_devices=self._fetch_devices,
        )
        self.user_path = path = UserPath(deps, notify_call=self._notify_call)
        self.billing.fulfiller.messenger = path.messenger  # the purchase message is edited by the user path
        path.register()
        self.job_handlers.update(path.handlers())
        path.install(self.bus)
        path.schedule(self.scheduler)
        # Stars + receipts (before the screen router): included by _wire_modules after the early routers
        self._user_routers.append(path.aiogram_router())
        if self.receipts is not None:
            self._user_routers.append(
                receipts_router(
                    self.receipts,
                    owner_ids=users.owner_ids,
                    db=self.db,  # type: ignore[arg-type]
                    hub=hub,
                    admin_chat_id=self._admin_chat_id,
                )
            )
        channel = self.channel
        if channel is not None:
            member_router = Router(name="svbg-channel")

            @member_router.chat_member()
            async def on_chat_member(update: ChatMemberUpdated) -> None:
                async with guard("tg:chat_member", hub=hub):
                    await channel.on_update(update)

            self._user_routers.append(member_router)

    async def _on_start(self, user: UserCtx, chat_id: int, link: DeepLink | None) -> tuple[str, Any] | None:
        """``/start`` gate (decision C9): a first-time user is announced in «👤 Новые пользователи»; the user
        path picks the first screen (required channel gate), then the consent page if one must be accepted.

        With deep links wired this is the gate of :meth:`DeeplinkService.start_hook`: it is called with
        ``link=None`` (the deep-link service keeps the intent and resumes it after onboarding)."""
        # behind the entry captcha the post waits for the right tap (_captcha_passed): bots are not announced
        captcha = self.user_path is not None and self.user_path.captcha.required(user)
        if user.is_new and self.admin_events is not None and not user.at_least("support") and not captcha:
            self._spawn(self.admin_events.new_user(user.user_id), "admin-new-user")
        picked = None if self.user_path is None else await self.user_path.on_start(user, chat_id, link)
        if picked is not None or not self._pages_wired or self.pages is None or user.at_least("support"):
            return picked
        try:
            page = await self.pages.needs_consent(user.user_id)  # 0 SQL while consent is off
        except Exception as exc:  # noqa: BLE001 - isolation: /start must work without the pages module
            if self.hub is not None:
                await self.hub.capture(exc, "app:start:consent", module="svbg.pages", handled="пропущено")
            return None
        return None if page is None else ("page", page.code)

    async def _captcha_passed(self, user: UserCtx) -> None:
        """A new user passed the entry captcha: the «👤 Новые пользователи» post and the referral welcome
        (with the days «за переход») that waited for it."""
        if self.admin_events is not None:
            self._spawn(self.admin_events.new_user(user.user_id), "admin-new-user")
        if self.referral is not None:
            await self.referral.captcha_passed(user.user_id)

    async def _after_onboarding(self, ctx: Any) -> Any:
        """Resume point after the channel gate / language choice (decision C9): the consent page first, then
        the deep-link intent kept at ``/start``; ``None`` → the step's own screen."""
        if self._pages_wired and self.pages is not None:
            page = await self.pages.needs_consent(ctx.user.user_id)
            if page is not None:
                from svbg.tg.ui.view import Redirect

                return Redirect("page", page.code)
        if self.deeplinks is None:
            return None
        from svbg.deeplinks.hook import resume_redirect

        return await resume_redirect(self.deeplinks, ctx)

    async def _notify_call(self, method: TelegramMethod[Any], chat_id: int) -> Any:
        """User notifications go through the notifier (per-chat pacing, blocked users marked)."""
        assert self.notifier is not None
        return await self.notifier.call(method, chat_id=chat_id, priority=Priority.NORMAL)

    async def _fetch_devices(self, panel_user_id: int) -> list[PanelDevice]:
        """The panel's device list of one user (``GET /hwid/devices/{id}``; runs in a background job)."""
        from svbg.tg.user.deps import PanelDevice

        if self.remnawave is None:
            raise PanelNotConfiguredError
        result = await self.remnawave.client.devices(panel_user_id, lane=Lane.BACKGROUND)
        return [
            PanelDevice(d.hwid, d.platform, d.os_version, d.device_model, str(d.created_at or ""))
            for d in result.devices
        ]

    async def _member_lookup(self, chat_id: int, telegram_id: int) -> Membership:
        """``getChatMember`` through the bot of the moment (the token may be hot-swapped)."""
        from svbg.subscriptions.channel import membership_of

        bot = self.holder.get()
        if bot is None:
            raise BotUnavailableError("бот не подключён к Telegram")
        member = await bot.get_chat_member(chat_id=chat_id, user_id=telegram_id)
        return membership_of(member.status, getattr(member, "is_member", None))

    def _bot_link(self) -> str | None:
        """Where a payment page sends the user back: the bot's chat."""
        me = self.holder.me
        return f"https://t.me/{me.username}" if me is not None and me.username else None

    def _instance_info(self, instance_id: int) -> tuple[str, str | None] | None:
        inst = self.pay_instances.get(instance_id) if self.pay_instances is not None else None
        if inst is None:
            return None
        return inst.title, inst.method_kinds[0] if inst.method_kinds else None

    def _payment_routes(self) -> list[web.RouteDef]:
        if self.payments is None:
            return []
        from svbg.web.routes.payments import payment_routes

        return payment_routes(self.payments)

    async def _sync_locations(self) -> None:
        """Panel squads → ``locations`` (catalog), skipped while the panel is not connected or its breaker is
        open; the snapshot is reloaded (and other processes notified) only when something changed."""
        if self.catalog is None or self.remnawave is None or self.db is None or not self._panel_up():
            return
        from svbg.catalog.locations import sync_locations

        database: Any = self.db
        result = await sync_locations(database, self.remnawave.client, attention=self.attention)
        if result.changed:
            await self.catalog.changed()

    async def _sync_locations_quietly(self) -> None:
        try:
            await self._sync_locations()
        except (RemnawaveError, TimeoutError) as err:
            log.warning("catalog: locations sync failed: %s", type(err).__name__)

    async def _payments_poll_loop(self, poller: Poller) -> None:
        """Presence polling and the reconciler of payments (D14): ``Poller.tick`` every 2 s. Without due
        payments a tick costs no SQL; the database scan for scheduled checks runs every 15 s inside."""
        while True:
            try:
                await poller.run_once()
            except Exception as exc:  # noqa: BLE001 - isolation boundary: the next tick retries
                log.warning("payments: poll tick failed: %s", type(exc).__name__)
                if self.hub is not None:
                    await self.hub.capture(exc, "payments:poll", module="svbg.payments.poller")
            await asyncio.sleep(_PAY_POLL_S)

    async def _purge_payment_events(self) -> None:
        if self.payments is not None:
            await self.payments.purge_events()

    # ------------------------------------------------------------------ stages 3–4: modules

    async def _build_modules(
        self, stack: AsyncExitStack, db: DatabaseLike, settings: SettingsService
    ) -> None:
        """Media and content transfer, promo, pages, ads, referral, the pay freeze of the cutover, the owner
        modules' host (topics, jobs, tasks, bus, squad contributors, order items), the Bedolaga shadow pass.

        Each part is an isolation boundary: a part that cannot be built (a missing table, a bug) is reported
        and left out (``None``); its screens and buttons are not shown and the bot keeps working.
        """
        steps: tuple[tuple[str, Callable[[AsyncExitStack, Any, SettingsService], Awaitable[None]]], ...] = (
            ("svbg.content.media", self._build_media),
            ("svbg.promo", self._build_promo),
            ("svbg.pages", self._build_pages),
            ("svbg.ads", self._build_ads),
            ("svbg.referral", self._build_referral),
            ("svbg.importers.cutover", self._install_pay_freeze),
            ("svbg.ext", self._attach_extensions),
            ("svbg.importers.shadow", self._build_shadow),
        )
        for name, build in steps:
            try:
                await build(stack, db, settings)
            except Exception as exc:  # noqa: BLE001 - isolation boundary: the bot works without the part
                await self._module_failed(name, exc, "setup")

    async def _build_media(self, _stack: AsyncExitStack, db: Any, settings: SettingsService) -> None:
        from svbg.content.export_import import ContentTransfer
        from svbg.content.media import MediaLibrary, PublicMedia

        assert self.boot is not None and self.content is not None
        current = settings.current
        root = self.boot.data_dir / "media"
        media = MediaLibrary(db, root, limits=lambda: app_modules.media_limits(current))
        public = PublicMedia(self.content, root, derive_key(_secret_key(self.boot), "media-public"))
        hooks: list[Callable[[], Awaitable[object]]] = [self.content.reload]
        if self.catalog is not None:
            hooks.append(self.catalog.changed)  # imported plans: reload + tell other processes

        def keep() -> int:
            try:
                return max(1, int(app_modules.cfg(current, "CONTENT_BACKUPS_KEEP", 10)))
            except (TypeError, ValueError):
                return 10

        transfer = ContentTransfer(
            db, root, self.boot.data_dir / "content-exports", on_applied=hooks, keep_backups=keep
        )
        self.media, self.public_media, self.content_transfer = media, public, transfer

    def _currency(self) -> str:
        current = self.settings.current if self.settings is not None else None
        return str(app_modules.cfg(current, "CURRENCY", "RUB") or "RUB")

    def _timezone(self) -> str:
        current = self.settings.current if self.settings is not None else None
        return str(app_modules.cfg(current, "TIMEZONE", "Europe/Moscow") or "Europe/Moscow")

    async def _build_promo(self, stack: AsyncExitStack, db: Any, settings: SettingsService) -> None:
        from svbg.core.money import exponent
        from svbg.promo.service import PromoService
        from svbg.services.roles import Limits

        assert self.users is not None and self.scheduler is not None
        catalog = self.catalog

        def max_plan_price(currency: str) -> int | None:
            snap = getattr(catalog, "snapshot", None)
            prices = [
                price.amount_minor
                for plan in getattr(snap, "plans", ())
                if not getattr(plan, "is_trial", False)
                for price in plan.prices_in(currency)
            ]
            return max(prices) if prices else None

        def limits() -> Limits:
            cur = self._currency()
            try:
                exp = exponent(cur)
            except (KeyError, ValueError):
                exp = 2
            return Limits.from_settings(
                settings.current(), currency_exponent=exp, max_plan_price_minor=max_plan_price(cur)
            )

        promo = PromoService(
            db,
            catalog=catalog,
            trial=self.trial,
            currency=self._currency,
            timezone=self._timezone,
            limits=limits,
            owner_ids=self.users.owner_ids,
        )
        await promo.load()
        stack.callback(promo.install(self.bus))  # fulfilled orders → promo uses
        self.scheduler.every("promo.sweep", 600, promo.sweep, jitter_s=60)
        self.promo = promo

    async def _build_pages(self, _stack: AsyncExitStack, db: Any, _settings: SettingsService) -> None:
        from svbg.pages.service import PageService

        pages = PageService(db)
        await pages.load()  # system pages are created here (FAQ, rules, offer, consent)
        self.pages = pages

    async def _build_ads(self, _stack: AsyncExitStack, db: Any, _settings: SettingsService) -> None:
        from svbg.ads.service import AdService

        ads = AdService(db)
        await ads.load()
        self.ads = ads

    async def _build_referral(self, _stack: AsyncExitStack, db: Any, settings: SettingsService) -> None:
        from svbg.referral.wiring import PARTNERS_TOPIC, build

        if self.admin_chat is not None:
            self.admin_chat.register_topic(PARTNERS_TOPIC)

        def bot_username() -> str | None:
            me = self.holder.me
            return me.username if me is not None else None

        self.referral = build(
            db,
            config=settings.current,
            bus=self.bus,
            scheduler=self.scheduler,
            job_handlers=self.job_handlers,
            poster=self.admin_chat,
            sender=self.notifier,
            bot_username=bot_username,
            timezone=self._timezone,
        )

    async def _install_pay_freeze(self, _stack: AsyncExitStack, _db: Any, _settings: SettingsService) -> None:
        """``svbg pay freeze`` (rollback, 06 §4.7): no new invoices while frozen (one indexed SELECT)."""
        if self.payments is None:
            return
        from svbg.importers.cutover import install_pay_freeze

        install_pay_freeze(self.payments)

    async def _attach_extensions(self, stack: AsyncExitStack, db: Any, settings: SettingsService) -> None:
        """Owner modules (05 §3): their contributions are installed once; the host gates them at run time
        (a disabled module runs nothing, a failing one is ``degraded``/``failed``, never the bot)."""
        ext = self.ext
        if ext is None:
            return
        assert self.scheduler is not None
        rw = self.remnawave

        def api() -> Any:
            if rw is None:
                raise PanelNotConfiguredError
            return rw.client

        ext.attach(
            hub=self.hub,
            deps={
                "db": db,
                "api": api,
                "queue": self.queue,
                "admin_chat": self.admin_chat,
                "attention": self.attention,
                "bus": self.bus,
                "notifier": self.notifier,
                "writer": self.panel_writer,
                "contributors": self.contributors,
                "settings": settings,
                "scheduler": self.scheduler,
                "holder": self.holder,
                "users": self.users,
                "catalog": self.catalog,
                "billing": self.billing,
            },
        )
        if self.admin_chat is not None:
            ext.install_topics(self.admin_chat)
        ext.install_jobs(self.job_handlers)
        ext.install_tasks(self.scheduler)
        stack.callback(ext.install_bus(self.bus))
        if self.contributors is not None:
            for off in ext.install_contributors(self.contributors):
                stack.callback(off)
        ext.install_components(self.components)
        if self.billing is not None:
            fulfiller = self.billing.fulfiller
            ext.install_order_items(fulfiller)
            for kind in ext.order_kinds():  # X4: ``addon_lte`` (gated: refused while the module is off)
                handler = ext.order_kind(kind)
                if handler is not None:
                    fulfiller.register_kind(kind, handler)

    async def _build_shadow(self, _stack: AsyncExitStack, db: Any, settings: SettingsService) -> None:
        """The daily shadow pass of the Bedolaga migration (06 §4.2): registered always, it runs only while
        ``IMPORT_SHADOW_ENABLED`` is on (no restart needed); never writes to the panel."""
        target = getattr(db, "pg_dsn", None)
        if not target or self.scheduler is None:
            return
        from svbg.importers.bedolaga import ApiPanelReader, ImportConfig, importer_port
        from svbg.importers.shadow import ShadowConfig, ShadowService, deeplink_resolver

        current = settings.current
        rw = self.remnawave

        def api() -> Any:
            return None if rw is None else rw.current

        def panel() -> Any:
            client = api()
            return None if client is None else ApiPanelReader(client)

        def config() -> ShadowConfig:
            links = self.deeplinks
            resolve = deeplink_resolver(links.find_ad) if links is not None else None
            return ShadowConfig(timezone=self._timezone(), resolve_link=resolve)

        def source() -> str | None:
            value = app_modules.cfg(current, app_modules.SHADOW_SOURCE_KEY, None)
            return _opt_secret(value)

        async def post(_kind: str, report: Any) -> None:  # a svbg.tg.report.Report
            if self.admin_chat is not None:
                from svbg.services.admin_chat import K_SYSTEM

                await self.admin_chat.post_report(K_SYSTEM, report)
            else:
                await self.notify_owners_report(report)

        def import_config() -> Any:
            make = self.import_config
            return make() if make is not None else ImportConfig()

        port = importer_port(db, panel=panel, config=import_config)

        async def importer(**kwargs: Any) -> Any:
            try:
                return await port(**kwargs)
            finally:
                await self._after_import()

        shadow = ShadowService(
            target_dsn=str(target),
            source_dsn=source,
            api=api,
            importer=importer,
            post=post,
            attention=self.attention,  # type: ignore[arg-type]
            config=config,
            stop_writer=self._stop_writer,
        )

        async def tick() -> None:
            if app_modules.cfg(current, app_modules.SHADOW_ENABLED_KEY, False) is True:
                await shadow.tick()

        self.scheduler.daily("importers.shadow", dtime(6, 0), self._timezone(), tick, timeout_s=3600)
        self.shadow = shadow

    async def _after_import(self) -> None:
        """The import wrote rows the running services keep in memory: campaign codes (deep links and С11),
        pages, promo codes and plans are re-read, so the stand answers like a fresh start would."""
        steps: list[tuple[str, Callable[[], Awaitable[Any]] | None]] = [
            ("ads", self.ads.load if self.ads is not None else None),
            ("pages", self.pages.load if self.pages is not None else None),
            ("promo", self.promo.load if self.promo is not None else None),
            ("catalog", self.catalog.changed if self.catalog is not None else None),
        ]
        for name, fn in steps:
            if fn is None:
                continue
            try:
                await fn()
            except Exception:
                log.exception("reload of %s after the import failed", name)
        if self.deeplinks is not None:
            self.deeplinks.forget_ads()

    def _writer_gate(self, handler: Handler) -> Handler:
        """Panel writer jobs wait (not fail) while the writer is stopped by the shadow probe."""

        async def run(job: Job, ctx: JobContext) -> Any:
            if self._writer_stopped is not None:
                raise RetryJob(300, f"запись в панель остановлена: {self._writer_stopped}")
            return await handler(job, ctx)

        return run

    async def _stop_writer(self, reason: str) -> None:
        """Kill switch of the panel writer (06 §4.1 p.3): a writable panel token stops the writes."""
        self._writer_stopped = (str(reason) or "shadow")[:200]
        log.error("panel writer stopped: %s", mask(self._writer_stopped))
        await self.notify_owners(
            f"⛔ Запись в панель остановлена: {self._writer_stopped}. "
            "Задания ждут; перезапуск бота снимает стоп.",
            priority=Priority.HIGH,
        )

    def _media_routes(self) -> list[web.RouteDef]:
        if self.public_media is None:
            return []
        from svbg.web.routes.media import media_routes

        return media_routes(self.public_media)

    def _module_flags(self) -> frozenset[str]:
        """``flag:<module>`` of the content buttons: the module is wired (and, for referral, switched on)."""
        flags: set[str] = set()
        if self.promo is not None:
            flags.add("promo")
        if self._pages_wired and self.pages is not None:
            flags.add("pages")
        settings = self.settings
        if (
            self.referral is not None
            and settings is not None
            and app_modules.cfg(settings.current, "REFERRAL_ENABLED", False) is True
        ):
            flags.add("referral")
        return frozenset(flags)

    async def _load_user(self, tg_user: Any) -> UserCtx | None:
        """``UserDirectory.load`` plus the module flags (in memory, no SQL)."""
        assert self.users is not None
        user = await self.users.load(tg_user)
        if user is None:
            return None
        flags = self._module_flags()
        return replace(user, flags=user.flags | flags) if flags and not flags <= user.flags else user

    async def _start_extensions(self, stack: AsyncExitStack, settings: SettingsService) -> None:
        """Start the enabled owner modules (off by default) and follow their switches without a restart."""
        ext = self.ext
        if ext is None:
            return
        try:
            ext.bind_settings(settings)
            await ext.start(settings.current)
        except Exception as exc:  # noqa: BLE001 - isolation boundary: the core works without the modules
            await self._module_failed("svbg.ext", exc, "start")
            return
        self._on_stop(stack, "owner modules", ext.stop)

    # ------------------------------------------------------------------ Telegram

    def _build_telegram(
        self,
        *,
        db: DatabaseLike,
        boot: BootstrapConfig,
        settings: SettingsService,
        hub: ErrorHub,
        users: UserDirectory,
        content: ContentStore,
    ) -> None:
        codec = CallbackCodec(db, key=derive_key(_secret_key(boot), "callback-codec"))
        self._codec = codec
        media_url = None if self.public_media is None else app_modules.public_media_url(self.public_media)
        # the default banner on every message the bot sends (screens, notifications, admin chat, backups)
        self.banner = banner = BannerPolicy(
            content, public_url=self._public_url, media_url=media_url, media_root=boot.data_dir / "media"
        )
        banner_mod.install(banner)
        self.screens = screens = ScreenRouter(
            transport=BotTransport(self.holder),
            user_loader=self._load_user,
            ui_state=UiStateStore(db),
            content=content,
            codec=codec,
            hub=hub,
            public_url=self._public_url,
            media_url=media_url,
            media_root=boot.data_dir / "media",
            on_denied=self._on_denied,
            banner=banner,
        )
        self.dispatcher = dp = Dispatcher(name="svbg")
        runner_kwargs = dict(self.options.runner_kwargs)
        # BotRunner.stop = confirm the polling offset (≤ 5 s) + drain handlers + shutdown hooks + close
        # sessions; all of it must fit into one stop step, or the holder and sessions are left open.
        runner_kwargs.setdefault(
            "drain_timeout", _inner_budget(self.options.stop_timeout, reserve=_RUNNER_STOP_RESERVE, share=0.3)
        )
        runner_kwargs.setdefault("request_middlewares", [BannerMiddleware(banner)])
        self.runner = BotRunner(
            settings, dp, hub, holder=self.holder, workflow_data={"app": self}, **runner_kwargs
        )
        assert self.notifier is not None and self.attention is not None and self.crypto is not None
        self.deps = AppDeps(
            db=db,
            settings=settings,
            registry=self.registry,
            components=self.components,
            hub=hub,
            notifier=self.notifier,
            holder=self.holder,
            users=users,
            content=content,
            screens=screens,
            attention=self.attention,
            bus=self.bus,
            crypto=self.crypto,
            env_path=self.options.env_path,
            mirror=lambda: self.mirror,
            runner=lambda: self.runner,
            on_stop=self._add_module_stop,
            remnawave=self.remnawave,
            admin_chat=self.admin_chat,
            maintenance=self.maintenance,
            queue=self.queue,
            scheduler=self.scheduler,
            started_at=self._began_at,
            webhook_seen=self.webhook_seen,
            importer=self.importer,
            reconciler=self.reconciler,
            catalog=self.catalog,
            payments=self.payments,
            billing=self.billing,
            poller=self.poller,
            receipts=self.receipts,
            public_url=self._public_url,
            media=self.media,
            public_media=self.public_media,
            content_transfer=self.content_transfer,
            content_export=self.content_transfer.export if self.content_transfer is not None else None,
            register_job=self.job_handlers.__setitem__,
            extensions=self.ext,
            promo=self.promo,
            pages=self.pages,
            ads=self.ads,
            referral=self.referral,
            settings_notes=self._settings_notes,
        )
        self._build_user_path(dp, screens, users, content, hub)

    def _add_module_stop(self, name: str, fn: Callable[[], Awaitable[Any]]) -> None:
        self._module_stops.append((name, fn))

    async def _wire_modules(self) -> None:
        """Stage 3–4 screens (deep links, promo, pages, ads, referral, owner modules), the optional UI modules
        ``setup(router, deps)``, then ``/start``.

        A module that is not installed yet, or whose import/setup fails, is skipped and reported (log, error
        hub, the owner's startup message): the rest of the bot must keep working without it.

        aiogram routers: the early ones (admin capture of messages: constructor, promo / pages / ads editors)
        → the user path's (Stars, receipts, ``chat_member``) → the other modules' → ``/start``; the screen
        router is included last by :meth:`_start`.
        """
        assert self.dispatcher is not None and self.screens is not None and self.deps is not None
        early: list[Router] = []
        late: list[Router] = []
        await self._wire_stage3(early, late)
        for name in dict.fromkeys(self.options.optional_modules):  # a module listed twice is wired once
            try:
                module = importlib.import_module(name)
            except ModuleNotFoundError as exc:
                if exc.name is not None and name.startswith(exc.name):
                    self.missing_modules[name] = "модуль ещё не установлен"
                    log.warning("optional module %s is not available yet: its screens are disabled", name)
                    continue
                await self._module_failed(name, exc, "import")
                continue
            except Exception as exc:  # noqa: BLE001 - isolation boundary, reported to the hub
                await self._module_failed(name, exc, "import")
                continue
            setup = getattr(module, "setup", None)
            if not callable(setup):
                self.missing_modules[name] = "нет функции setup(router, deps)"
                log.error("module %s has no setup(router, deps); not wired", name)
                continue
            try:
                result = setup(self.screens, self.deps)
                if inspect.isawaitable(result):
                    result = await result
                if isinstance(result, Router):
                    (early if name in EARLY_ROUTER_MODULES else late).append(result)
            except Exception as exc:  # noqa: BLE001 - isolation boundary, reported to the hub
                await self._module_failed(name, exc, "setup")
                continue
            self.wired_modules.append(name)
        for router in (*early, *self._user_routers, *late):
            self.dispatcher.include_router(router)
        self._user_routers = []
        on_start: Any = self._on_start
        if self.deeplinks is not None:  # ad tag, referral, promo and target first; then the gate
            on_start = self.deeplinks.start_hook(self._on_start)
        assert self.users is not None
        self._start_router = build_start_router(
            screens=self.screens,
            users=_FlaggedUsers(self.users, self._load_user),  # type: ignore[arg-type]
            hub=self.hub,
            on_start=on_start,
        )
        self.dispatcher.include_router(self._start_router)

    async def _wire_stage3(self, early: list[Router], late: list[Router]) -> None:
        """Deep links (+ the ports to promo, ads and referral), the user and admin screens of promo, pages and
        ads, «🤝 Пригласить», the owner modules' screens and IP Guard cards, and the shop hooks."""
        assert self.screens is not None and self.deps is not None and self.users is not None
        screens = self.screens

        async def step(name: str, fn: Callable[[], Awaitable[None] | None]) -> bool:
            try:
                res = fn()
                if res is not None:
                    await res
            except Exception as exc:  # noqa: BLE001 - isolation boundary, reported to the hub
                await self._module_failed(name, exc, "setup")
                return False
            self.wired_parts.append(name)
            return True

        def deeplinks() -> None:
            from svbg.deeplinks.hook import from_app

            promo = app_modules.PromoPortAdapter(self.promo) if self.promo is not None else None
            ads = app_modules.AdsPortAdapter(self.ads) if self.ads is not None else None
            service = from_app(self.deps, promo=promo, ads=ads, referral=self.referral)
            if self.scheduler is not None:
                service.schedule(self.scheduler)
            self.deeplinks = service

        if not await step("svbg.deeplinks", deeplinks):
            self.deeplinks = None

        if self.promo is not None:
            promo = self.promo

            def promo_screens() -> None:
                from svbg.promo.user import PromoUserScreens
                from svbg.tg.admin.promo import PromoAdminScreens

                PromoUserScreens(promo).register(screens)
                admin = PromoAdminScreens(screens, promo, catalog=self.catalog)
                admin.install()
                early.append(admin.aiogram_router())

            if not await step("svbg.tg.admin.promo", promo_screens):
                self.promo = None

        if self.pages is not None:
            pages = self.pages

            async def after_consent(ctx: Any) -> Any:
                from svbg.deeplinks.hook import resume_redirect

                return await resume_redirect(self.deeplinks, ctx)

            def pages_screens() -> None:
                from svbg.pages.user import PageUserScreens
                from svbg.tg.admin.pages import PageAdminScreens

                user_screens = PageUserScreens(pages, after_consent=after_consent)
                user_screens.register(screens)
                admin = PageAdminScreens(screens, pages, user_screens=user_screens, timezone=self._timezone)
                admin.install()
                early.append(admin.aiogram_router())

            self._pages_wired = await step("svbg.tg.admin.pages", pages_screens)

        if self.ads is not None:
            ads = self.ads

            def ads_screens() -> None:
                from svbg.ads.admin import AdAdminScreens

                admin = AdAdminScreens(screens, ads, currency=self._currency)
                admin.install()
                early.append(admin.aiogram_router())

            await step("svbg.ads.admin", ads_screens)

        if self.referral is not None:
            referral = self.referral
            if not await step(
                "svbg.referral.screens", lambda: app_modules.ReferralScreens(referral).register(screens)
            ):
                self.referral = None

        if self.ext is not None:
            ext = self.ext

            async def ext_ui() -> None:
                for module, error in (await ext.install_ui(screens)).items():
                    self.missing_modules[f"ext:{module}"] = f"экраны не подключены ({error})"

            def ip_guard_cards() -> None:
                from svbg.ext.ip_guard.runtime import RUNTIME
                from svbg.ext.ip_guard.tg import CardActions, cards_router

                assert self.db is not None and self.users is not None
                late.append(cards_router(CardActions(RUNTIME.service, self.db, self.users.owner_ids)))  # type: ignore[arg-type]

            await step("svbg.ext.ui", ext_ui)
            if "ip_guard" in ext.names:
                await step("svbg.ext.ip_guard.cards", ip_guard_cards)

        if self.user_path is not None:
            self.user_path.home.after_onboarding = self._after_onboarding
            if self._captcha_passed not in self.user_path.captcha.on_passed:
                self.user_path.captcha.on_passed.append(self._captcha_passed)
            shop = self.user_path.shop
            shop.promo = self.promo
            shop.link_code = self.deeplinks.granted_plan_code if self.deeplinks is not None else None
        self.deps = replace(
            self.deps,
            deeplinks=self.deeplinks,
            promo=self.promo,
            pages=self.pages if self._pages_wired else None,
            ads=self.ads,
            referral=self.referral,
        )

    async def _module_failed(self, name: str, exc: BaseException, stage: str) -> None:
        self.missing_modules[name] = f"ошибка при подключении ({type(exc).__name__})"
        log.error("optional module %s failed at %s: %s", name, stage, type(exc).__name__, exc_info=exc)
        if self.hub is not None:
            await self.hub.capture(exc, f"app:{stage}:{name}", module=name, handled="модуль не подключён")

    # ------------------------------------------------------------------ hooks

    def _public_url(self) -> str | None:
        settings = self.settings
        if settings is None:
            return None
        value = settings.current()["PUBLIC_URL"]
        return str(value) if value else None

    def _apply_log_level(self, snap: SettingsSnapshot) -> None:
        if self.options.configure_logging:
            try:
                set_level(str(snap["LOG_LEVEL"]))
            except ValueError:
                log.warning("unknown LOG_LEVEL %r, keeping INFO", snap["LOG_LEVEL"])

    async def _on_log_level(self, snap: SettingsSnapshot, _keys: set[str]) -> None:
        self._apply_log_level(snap)

    async def _on_owners_changed(self, _snap: SettingsSnapshot, _keys: set[str]) -> None:
        if self.users is not None:
            self.users.invalidate()

    def _admin_chat_id(self) -> int | None:
        """The connected admin group right now (``None``: cards go to the owners' DMs)."""
        return self.admin_chat.chat_id if self.admin_chat is not None else None

    async def _on_denied(self, user: UserCtx, place: str) -> None:
        """Audit a denied staff action (``admin_audit``), at most once a minute per staff member and place.

        ``callback_data`` can be forged by any client: auditing denials of ordinary users would let anyone
        grow the journal without bound and bury the real entries, so those are only logged by the router.
        """
        if self.db is None or not user.at_least("support"):
            return
        target = place[:200]
        if not self._denied_throttle.allow((user.user_id, target)):
            return
        stmt = sa.insert(admin_audit).values(
            actor_id=user.user_id, role=user.role, action="access_denied", target=target
        )
        try:
            async with self.db.tx() as conn:
                await conn.execute(stmt)
        except (sa.exc.SQLAlchemyError, OSError) as exc:
            log.warning("cannot audit an access denial: %s", type(exc).__name__)

    async def _on_breaker(self, module: str, old: BreakerState, new: BreakerState) -> None:
        log.warning("module %s circuit breaker: %s → %s", module, old.value, new.value)

    async def _on_health_error(self, name: str, exc: BaseException) -> None:
        if self.hub is not None:
            await self.hub.capture(
                exc, f"health:{name}", module=name, handled="компонент помечен как недоступный"
            )

    async def _on_bus_error(self, event: Any, _handler: Any, exc: BaseException) -> None:
        if self.hub is not None:
            name = getattr(event, "name", "?")
            await self.hub.capture(exc, f"bus:{name}", handled="обработчик события пропущен")

    async def _on_mirror_notice(self, notice: MirrorNotice) -> None:
        await self.notify_owners(notice.message, priority=Priority.HIGH)

    async def notify_owners(self, text: str, *, priority: Priority = Priority.NORMAL) -> int:
        """Plain-text message to every owner; returns how many got it. Never raises."""
        if self.users is None or self.notifier is None:
            return 0
        delivered = 0
        try:
            owners = await self.users.owner_ids()
        except (sa.exc.SQLAlchemyError, OSError) as exc:
            log.warning("cannot resolve owners: %s", type(exc).__name__)
            return 0
        for chat_id in sorted(owners):
            try:
                if await self.notifier.send(chat_id, text, priority=priority) is not None:
                    delivered += 1
            except (TelegramAPIError, NotifierError, BotUnavailableError, *TRANSPORT_ERRORS) as exc:
                log.warning("owner notification failed: %s", type(exc).__name__)
        return delivered

    async def notify_owners_report(self, report: Report, *, priority: Priority = Priority.NORMAL) -> int:
        """A :class:`~svbg.tg.report.Report` to every owner (rich, or its HTML text); never raises."""
        if self.users is None or self.notifier is None:
            return 0
        delivered = 0
        try:
            owners = await self.users.owner_ids()
        except (sa.exc.SQLAlchemyError, OSError) as exc:
            log.warning("cannot resolve owners: %s", type(exc).__name__)
            return 0
        for chat_id in sorted(owners):
            try:
                sent = await send_report(
                    self.notifier, chat_id, report, bot=self.holder.get(), priority=priority
                )
            except (TelegramAPIError, NotifierError, BotUnavailableError, *TRANSPORT_ERRORS) as exc:
                log.warning("owner report failed: %s", type(exc).__name__)
                continue
            delivered += sent is not None
        return delivered

    async def _startup_report(self) -> None:
        """One message to the owners: version, start time, bot state and what needs attention."""
        if self.runner is None:
            return
        try:
            async with asyncio.timeout(30):
                await self.holder.wait_ready()
        except TimeoutError:
            return  # no bot → nobody to talk to; the problem is in the logs and /ready
        lines = [_TXT["started"].format(version=svbg.__version__, seconds=self.startup_seconds or 0.0)]
        reports = await self.components.health_all()
        bot = reports.get("bot")
        if bot is not None and bot.summary:
            lines.append(_TXT["bot_ok"].format(summary=bot.summary))
        problems = [
            f"• {name}: {r.summary}"
            for name, r in reports.items()
            if r.status in (Health.DEGRADED, Health.DOWN)
        ]
        if problems:
            lines += ["", _TXT["problems"], *problems]
        if self.settings is not None and self.settings.restart_pending:
            lines.append(_TXT["restart"].format(keys=", ".join(sorted(self.settings.restart_pending))))
        if self.missing_modules:
            lines.append("Не подключено: " + ", ".join(sorted(self.missing_modules)))
        await self.notify_owners("\n".join(lines), priority=Priority.HIGH)

    # ------------------------------------------------------------------ periodic tasks

    async def _purge_jobs(self) -> None:
        assert self.queue is not None
        await self.queue.purge()

    async def _purge_short_tokens(self) -> None:
        assert self._codec is not None
        await self._codec.purge_expired()

    async def _purge_settings_audit(self) -> None:
        assert self.settings is not None
        await self.settings.store.purge_audit()

    async def _purge_attention(self) -> None:
        assert self.attention is not None
        await self.attention.purge_resolved()

    async def _sync_health(self) -> None:
        assert self.attention is not None
        await self.attention.sync_health(await self.components.health_all())

    # ------------------------------------------------------------------ helpers

    def _spawn(self, coro: Awaitable[Any], name: str) -> asyncio.Future[Any]:
        task = asyncio.ensure_future(coro)
        task.set_name(name)
        self._background.add(task)

        def done(t: asyncio.Task[Any]) -> None:
            self._background.discard(t)
            if not t.cancelled() and t.exception() is not None:
                log.error("background task %s failed", name, exc_info=t.exception())

        task.add_done_callback(done)
        return task


def _secret_key(boot: BootstrapConfig) -> str:
    """``SECRET_KEY`` checked by :meth:`App._bootstrap` (an explicit error, not an ``assert``)."""
    key = boot.secret_key
    if not key:
        raise AppError(_TXT["no_secret_key"].format(path=boot.env_path))
    return key


def _opt_secret(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


async def _quiet_close(db: DatabaseLike) -> None:
    try:
        await db.close()
    except Exception:  # best effort on a failed start
        log.debug("closing the database after a failed start raised", exc_info=True)


# --------------------------------------------------------------------------------------- run


def _install_stop_signals(stop: asyncio.Event) -> Callable[[], None]:
    """SIGINT/SIGTERM set ``stop`` (POSIX). On Windows Ctrl+C arrives as KeyboardInterrupt/cancellation of
    the main task (asyncio.run), and Ctrl+Break / SIGTERM are wired through ``signal.signal``."""
    loop = asyncio.get_running_loop()
    if sys.platform != "win32":
        installed: list[int] = []
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(sig, stop.set)
                installed.append(sig)

        def restore_posix() -> None:
            for sig in installed:
                with contextlib.suppress(NotImplementedError, RuntimeError):
                    loop.remove_signal_handler(sig)

        return restore_posix

    previous: dict[int, Any] = {}

    def handler(_signum: int, _frame: Any) -> None:
        loop.call_soon_threadsafe(stop.set)

    for name in ("SIGBREAK", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        with contextlib.suppress(ValueError, OSError):
            previous[sig] = signal.signal(sig, handler)

    def restore_windows() -> None:
        for sig, old in previous.items():
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, old)

    return restore_windows


async def run(options: AppOptions, *, stop: asyncio.Event | None = None) -> None:
    """Start the application, wait for a stop signal (or ``stop``), stop gracefully."""
    stop_event = stop or asyncio.Event()
    restore = _install_stop_signals(stop_event)
    app = App(options)
    try:
        await app.start()
        await stop_event.wait()
    finally:
        try:
            await app.stop()
        finally:
            restore()
