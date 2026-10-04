"""«⚙️ Состояние», «Требует внимания» and «Что включено в панели» (07 §2.4.3, 02 §5.7, 04 §9.1).

Screens (code-defined, on the :class:`~svbg.tg.ui.router.ScreenRouter`):

* ``status`` — components from :meth:`ComponentRegistry.health_all` (bounded, isolated), auto-maintenance,
  the ``jobs`` queue per lane (ready / running / dead), periodic tasks with problems, open error groups
  (top 10), the ``.env`` mirror, version, uptime and RSS, the «Требует внимания» counter;
* ``status.att`` — open «Требует внимания» items, worst first; the owner gets the ``fix_action`` button of
  each item («🛠 Исправить» → the setting or the screen it names) and «🔕 24 ч» (snooze);
* ``status.panel`` — «Что включено в панели»: reads ``GET /system/configuration`` and shows which panel
  ``.env`` lines are missing with a ready modify-in-place snippet (02 §5.7).

Access (04 §9.1): Owner, and Admin with ``system.view`` — read only. Actions that change something (snooze)
are owner-only; every callback is re-checked by the router. ``/status`` opens the screen in a private chat.

Every part of the screen is collected concurrently with its own timeout; a failing part becomes one line
«не удалось получить …» and never breaks the screen. No secrets are shown: component details are not
rendered, summaries and titles are masked.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import sys
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Protocol, TypeVar

from aiogram import Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, Message

import svbg
from svbg.core import clock
from svbg.core.component import Health, HealthReport
from svbg.core.log import mask
from svbg.remnawave.component import RemnawaveComponent
from svbg.remnawave.errors import RemnawaveError
from svbg.tg.admin import nav
from svbg.tg.report import num, pre_table
from svbg.tg.setup.wizard import (
    SCREEN as WIZARD_SCREEN,
)
from svbg.tg.setup.wizard import (
    build_env_snippet,
    missing_notify_groups,
    panel_features,
    snippet_block,
)
from svbg.tg.ui import codec as codec_mod
from svbg.tg.ui.renderer import nav_button
from svbg.tg.ui.view import Toast, View

if TYPE_CHECKING:
    from svbg.core.attention import AttentionItem
    from svbg.core.component import ComponentRegistry
    from svbg.core.errors import ErrorGroupView
    from svbg.tg.ui.router import HandlerResult, ScreenCtx, ScreenRouter

log = logging.getLogger("svbg.tg.admin.status")

__all__ = [
    "ACTIONS",
    "PERM_VIEW",
    "SCREEN",
    "SCREEN_ATTENTION",
    "SCREEN_PANEL",
    "StatusData",
    "StatusScreens",
    "fix_button_target",
    "rss_bytes",
    "setup",
]

T = TypeVar("T")

SCREEN: Final = "status"  # = svbg.tg.admin_chat_sink.STATUS_SCREEN
SCREEN_ATTENTION: Final = "status.att"
SCREEN_PANEL: Final = "status.panel"
ACTIONS: Final = "stat"
A_SNOOZE: Final = "snz"
PERM_VIEW: Final = "system.view"
SETTINGS_KEY_SCREEN: Final = "set.key"  # svbg.tg.admin.settings.SCREEN_KEY
SLICE_SCREEN: Final = "set.v"  # svbg.tg.admin.slices.SCREEN
HOME: Final = "home"

ATTENTION_LIMIT: Final = 20
ERRORS_LIMIT: Final = 10
SNOOZE_FOR: Final = timedelta(hours=24)
MAX_TEXT: Final = 4000  # leave room under Telegram's 4096
_PART_TIMEOUT: Final = 3.0

_COMPONENT_NAMES: Final[Mapping[str, str]] = {
    "bot": "Telegram-бот",
    "database": "PostgreSQL",
    "remnawave": "Remnawave",
    "admin_chat": "Админ-чат",
    "module:lte": "Модуль «Трафик LTE»",
    "module:ip_guard": "Модуль «IP Guard»",
}


def _component_name(name: str, registry: Any = None) -> str:
    """``payments.rollypay`` → «Касса RollyPay» (the title of its settings section), ``module:lte`` →
    «Модуль lte»; known names in Russian."""
    if name in _COMPONENT_NAMES:
        return _COMPONENT_NAMES[name]
    if name.startswith("payments."):
        title = ""
        try:
            title = str(registry.section_title(name)) if registry is not None else ""
        except (KeyError, AttributeError):
            title = ""
        quoted = re.search(r"«(.+?)»", title)  # «Платёжка «RollyPay» (инстанс rollypay)» → RollyPay
        return f"Касса {quoted.group(1) if quoted else name.partition('.')[2]}"
    if name.startswith("module:"):
        return f"Модуль {name.partition(':')[2]}"
    return name


_ICONS: Final[Mapping[Health, str]] = {
    Health.OK: "✅",
    Health.DEGRADED: "⚠️",
    Health.DOWN: "🔴",
    Health.DISABLED: "⚪",
    Health.UNKNOWN: "❔",
}
_SEVERITY_ICON: Final[Mapping[str, str]] = {"error": "🔴", "warn": "🟠", "info": "ℹ️"}

# Owner-facing texts (Russian) in one place.
_T: Final[dict[str, str]] = {
    "uptime": "SvBG Shop {version} · работает <b>{uptime}</b> · память <b>{rss}</b>",
    "disabled_desks": "⚪ Выключено касс: {n}",
    "components": "<b>Компоненты</b>",
    "no_components": "Компоненты не зарегистрированы.",
    "jobs": "<b>Очередь задач</b>",
    "jobs_empty": "Задач нет.",
    "jobs_line": "{icon} {queue}: ждут {ready} · в работе {running} · ошибок {dead}",
    "jobs_head": "Очередь",
    "jobs_ready": "Ждут",
    "jobs_running": "Идут",
    "jobs_dead": "Ошибки",
    "tasks": "<b>Периодические задачи с проблемами</b>",
    "task_line": "⚠️ {name}: {problem}",
    "task_failed": "ошибка «{error}», последний успех {ok}",
    "task_never": "ещё ни разу не выполнилась успешно",
    "errors": "<b>Ошибки (открытые группы)</b>",
    "errors_none": "Открытых ошибок нет.",
    "error_line": "{icon} <b>×{count}</b> {title} — {place}, {at}",
    "env": "<b>Настройки и .env</b>",
    "env_ok": "✅ .env синхронизирован{at}",
    "env_bad": "⚠️ .env: {error}",
    "env_ro": "⚠️ .env недоступен для записи",
    "env_invalid": "⚠️ В .env отклонены строки: {keys}",
    "env_conflicts": "⚠️ Конфликты правок .env: {keys}",
    "env_restart": "♻️ Ждут перезапуска: {keys}",
    "attention": "⚠️ <b>Требует внимания: {n}</b> ({parts})",
    "attention_none": "✅ Ничего не требует внимания.",
    "failed": "❔ Не удалось получить: {what}",
    "updated": "Обновлено {at}",
    "refresh": "🔄 Обновить",
    "att_button": "⚠️ Требует внимания ({n})",
    "panel_button": "🖥 Что включено в панели",
    "wizard": "🧭 Мастер настройки",
    "maintenance": "🛠 Техработы",
    "menu": "🏠 Меню",
    "back": "⬅️ Состояние",
    "att_empty": "Всё в порядке — открытых пунктов нет.",
    "att_more": "…и ещё {n}",
    "att_snoozed": "Скрыто на 24 ч",
    "att_gone": "Этот пункт уже решён",
    "fix": "🛠 {n}. Исправить",
    "snooze": "🔕 {n}. 24 ч",
    "panel_off": "Панель не подключена — подключите её в мастере настройки.",
    "panel_err": "❌ Не удалось прочитать настройки панели: {error}",
    "panel_all": "✅ Всё, что полезно боту, в панели включено.",
    "panel_missing": "<b>Чтобы включить недостающее</b>, выполните на сервере панели:",
    "panel_hooks": (
        "Вебхуки включаются в мастере настройки (шаг «Вебхуки»): там бот покажет адрес и секрет для панели."
    ),
    "panel_without": "Без этих настроек бот работает сам: напоминания и пороги считает его планировщик.",
    "panel_domain": "Домен подписок: <code>{domain}</code>",
    "warn_commas": "⚠️ В значениях-списках и в WEBHOOK_URL — без пробелов после запятых.",
    "warn_comments": "⚠️ Никаких комментариев (<code># …</code>) в изменённых строках.",
}

Part = Callable[[], Awaitable[T]]


# ================================================================================ helpers


def rss_bytes() -> int | None:
    """Resident memory (Linux ``/proc``; elsewhere the peak from ``resource``), or ``None``."""
    try:
        with open("/proc/self/statm", encoding="ascii") as fh:
            pages = int(fh.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError, AttributeError):
        pass
    try:
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except (ImportError, OSError, AttributeError):
        return None
    return int(peak) if sys.platform == "darwin" else int(peak) * 1024


def _human_bytes(value: int | None) -> str:
    if value is None:
        return "—"
    mb = value / (1024 * 1024)
    return f"{mb:.0f} МБ" if mb < 1024 else f"{mb / 1024:.1f} ГБ"


def _human_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days} д {hours} ч"
    if hours:
        return f"{hours} ч {minutes} мин"
    return f"{minutes} мин"


def _hm(value: datetime | None) -> str:
    return "—" if value is None else value.strftime("%d.%m %H:%M UTC")


def _esc(text: Any, limit: int = 300) -> str:
    raw = mask(str(text))
    if len(raw) > limit:
        raw = raw[: limit - 1] + "…"
    return html.escape(raw, quote=False)


def fix_button_target(fix_action: str | None) -> tuple[str, str | None] | None:
    """``fix_action`` → ``(screen, arg)`` to open, or ``None`` for an unknown / unsafe value.

    ``setting:<KEY>`` opens the settings card of the key; ``screen:<code>[:<arg>]`` opens the screen.
    """
    if not fix_action:
        return None
    kind, _, rest = fix_action.partition(":")
    if kind == "setting" and rest and rest.replace("_", "").isalnum() and rest.upper() == rest:
        return SETTINGS_KEY_SCREEN, rest
    if kind == "screen" and rest:
        code, _, arg = rest.partition(":")
        try:
            codec_mod.encode(code)
        except (ValueError, TypeError):
            return None
        return code, arg or None
    return None


# ================================================================================ data


@dataclass(slots=True)
class StatusData:
    """Everything the «Состояние» screen shows; ``failed`` lists parts that could not be collected."""

    health: dict[str, HealthReport] = field(default_factory=dict)
    jobs: dict[str, dict[str, int]] | None = None
    tasks: list[tuple[str, str]] = field(default_factory=list)
    errors: list[ErrorGroupView] = field(default_factory=list)
    attention: dict[str, int] = field(default_factory=dict)
    env: list[str] = field(default_factory=list)
    maintenance: tuple[bool, str] | None = None  # (active, text)
    uptime_s: float | None = None
    rss: int | None = None
    failed: list[str] = field(default_factory=list)
    at: datetime = field(default_factory=clock.now)


class _Attention(Protocol):
    async def open_items(self, *, include_snoozed: bool = False, limit: int = 100) -> list[AttentionItem]: ...

    async def open_counts(self) -> Mapping[str, int]: ...

    async def snooze_for(self, id: int, delta: timedelta) -> bool: ...

    async def get_by_id(self, id: int) -> AttentionItem | None: ...


class _Hub(Protocol):
    async def open_groups(
        self, limit: int = 10, *, since: timedelta | None = None
    ) -> list[ErrorGroupView]: ...


class _Queue(Protocol):
    async def stats(self) -> dict[str, dict[str, int]]: ...


class _Scheduler(Protocol):
    def tasks(self) -> Mapping[str, Any]: ...


class _Settings(Protocol):
    @property
    def restart_pending(self) -> set[str]: ...


class _Maintenance(Protocol):
    @property
    def state(self) -> Any: ...


# ================================================================================ screens


class StatusScreens:
    """Registers «Состояние» screens on a router and serves ``/status``.

    Everything except ``components`` and ``attention`` is optional (``None`` → the part is not shown).
    ``mirror`` is a callable returning the current ``EnvMirror`` (it is created after the modules are wired).
    ``started_at`` — ``time.monotonic()`` of the process start (default: when this object was created).
    """

    def __init__(
        self,
        router: ScreenRouter,
        *,
        components: ComponentRegistry,
        attention: _Attention,
        hub: _Hub | None = None,
        queue: _Queue | None = None,
        scheduler: _Scheduler | None = None,
        settings: _Settings | None = None,
        mirror: Callable[[], Any] | None = None,
        maintenance: _Maintenance | None = None,
        started_at: float | None = None,
        health_timeout: float = 4.0,
        part_timeout: float = _PART_TIMEOUT,
        panel_timeout: float = 8.0,
        version: str = svbg.__version__,
    ) -> None:
        self.router = router
        self.components = components
        self.attention = attention
        self.hub = hub
        self.queue = queue
        self.scheduler = scheduler
        self.settings = settings
        self.mirror = mirror
        self.maintenance = maintenance
        self.started_at = started_at if started_at is not None else time.monotonic()
        self.health_timeout = health_timeout
        self.part_timeout = part_timeout
        self.panel_timeout = panel_timeout
        self.version = version
        self._installed = False

    def install(self) -> None:
        if self._installed:
            return
        self._installed = True
        view = {"required_role": "admin", "perm": PERM_VIEW}
        r = self.router
        r.screen(SCREEN, **view)(self._status_screen)
        r.screen(SCREEN_ATTENTION, **view)(self._attention_screen)
        r.screen(SCREEN_PANEL, **view)(self._panel_screen)
        r.action(ACTIONS, A_SNOOZE, required_role="owner")(self._snooze)

    def aiogram_router(self, name: str = "svbg-status") -> Router:
        """``/status`` in a private chat (owner, admin with ``system.view``)."""
        router = Router(name=name)

        async def on_status(message: Message) -> None:
            if not await self.handle_status(message):
                raise SkipHandler

        router.message.register(on_status, Command("status"))
        return router

    async def handle_status(self, message: Message) -> bool:
        if message.from_user is None or message.chat.type != "private":
            return False
        try:
            user = await self.router.user_loader(message.from_user)
        except Exception:
            log.exception("user loader failed for /status")
            return False
        if user is None or not (user.at_least("admin") and user.has_perm(PERM_VIEW)):
            return False
        await self.router.show(user, message.chat.id, SCREEN, new=True)
        return True

    # ------------------------------------------------------------ collecting

    async def _part(self, what: str, fn: Part[T], data: StatusData, limit: float | None = None) -> T | None:
        try:
            async with asyncio.timeout(limit or self.part_timeout):
                return await fn()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one broken part must not break the screen
            log.warning("status: %s failed: %s", what, type(exc).__name__)
            data.failed.append(what)
            return None

    async def collect(self) -> StatusData:
        """Gather every part concurrently; each is bounded and isolated."""
        data = StatusData()

        async def health() -> None:
            res = await self._part(
                "компоненты",
                lambda: self.components.health_all(limit_s=self.health_timeout),
                data,
                self.health_timeout + 1.0,
            )
            data.health = res or {}

        async def jobs() -> None:
            if self.queue is not None:
                data.jobs = await self._part("очередь задач", self.queue.stats, data)

        async def errors() -> None:
            hub = self.hub
            if hub is not None:
                data.errors = await self._part("ошибки", lambda: hub.open_groups(ERRORS_LIMIT), data) or []

        async def attention() -> None:
            counts = await self._part("«Требует внимания»", self.attention.open_counts, data)
            data.attention = dict(counts or {})

        await asyncio.gather(health(), jobs(), errors(), attention())
        data.tasks = self._task_problems()
        data.env = self._env_lines()
        maint = self.maintenance
        if maint is not None:
            try:
                state = maint.state
                data.maintenance = (bool(state.active), state.text())
            except Exception:  # noqa: BLE001 - informational
                data.failed.append("техработы")
        data.uptime_s = time.monotonic() - self.started_at
        data.rss = rss_bytes()
        return data

    def _task_problems(self) -> list[tuple[str, str]]:
        sched = self.scheduler
        if sched is None:
            return []
        out: list[tuple[str, str]] = []
        try:
            tasks = sched.tasks()
        except Exception:  # noqa: BLE001 - informational
            return [("scheduler", "не удалось получить состояние")]
        for name, st in tasks.items():
            error = getattr(st, "last_error", None)
            last_ok = getattr(st, "last_ok_at", None)
            runs = getattr(st, "runs", 0)
            if error:
                out.append((name, _T["task_failed"].format(error=_esc(error, 120), ok=_hm(last_ok))))
            elif runs and last_ok is None:
                out.append((name, _T["task_never"]))
        return out

    def _env_lines(self) -> list[str]:
        lines: list[str] = []
        mirror = None
        if self.mirror is not None:
            try:
                mirror = self.mirror()
            except Exception:  # noqa: BLE001 - informational
                mirror = None
        status = getattr(mirror, "status", None)
        if status is not None:
            if getattr(status, "error", None):
                lines.append(_T["env_bad"].format(error=_esc(status.error, 200)))
            elif not getattr(status, "writable", True):
                lines.append(_T["env_ro"])
            else:
                at = getattr(status, "last_write_at", None)
                lines.append(_T["env_ok"].format(at=f", запись {_hm(at)}" if at else ""))
            invalid = getattr(status, "invalid", None) or {}
            if invalid:
                lines.append(_T["env_invalid"].format(keys=_esc(", ".join(sorted(invalid)), 200)))
            conflicts = getattr(status, "conflicts", None) or []
            if conflicts:
                lines.append(_T["env_conflicts"].format(keys=_esc(", ".join(sorted(conflicts)), 200)))
        pending = getattr(self.settings, "restart_pending", None) if self.settings is not None else None
        if pending:
            lines.append(_T["env_restart"].format(keys=_esc(", ".join(sorted(pending)), 200)))
        return lines

    # ------------------------------------------------------------ rendering

    @staticmethod
    def _jobs_table(jobs: Mapping[str, Mapping[str, int]]) -> list[str]:
        """Queues × waiting / running / failed as a small monospace table (lines if it does not fit)."""
        items = sorted(jobs.items())
        head = [_T["jobs_head"], _T["jobs_ready"], _T["jobs_running"], _T["jobs_dead"]]
        rows = [
            [queue[:24], *(num(counts.get(k, 0)) for k in ("ready", "running", "dead"))]
            for queue, counts in items
        ]
        pre = pre_table([head, *rows], ("left", "right", "right", "right"))
        if pre is not None:
            return [pre]
        return [
            _T["jobs_line"].format(
                icon="❌" if counts.get("dead", 0) else "▫️",
                queue=_esc(queue, 40),
                ready=counts.get("ready", 0),
                running=counts.get("running", 0),
                dead=counts.get("dead", 0),
            )
            for queue, counts in items
        ]

    def render(self, data: StatusData, *, owner: bool) -> View:
        lines = [nav.header(SCREEN), ""]
        lines.append(
            _T["uptime"].format(
                version=_esc(self.version, 40),
                uptime=_human_duration(data.uptime_s or 0.0),
                rss=_human_bytes(data.rss),
            )
        )
        if data.maintenance is not None:
            active, text = data.maintenance
            lines.append(f"{'🛠' if active else '▫️'} {_esc(text)}")
        lines += ["", _T["components"]]
        if data.health:
            off_desks = 0
            registry = getattr(self.settings, "registry", None)
            for name, report in data.health.items():
                if report.status is Health.DISABLED and name.startswith("payments."):
                    off_desks += 1  # 23 switched-off cash desks are one line, not 23
                    continue
                summary = f" — {_esc(report.summary, 200)}" if report.summary else ""
                label = _component_name(name, registry)
                lines.append(f"{_ICONS[report.status]} {_esc(label, 60)}{summary}")
            if off_desks:
                lines.append(_T["disabled_desks"].format(n=off_desks))
        else:
            lines.append(_T["no_components"])
        if data.jobs is not None:
            lines += ["", _T["jobs"]]
            if not data.jobs:
                lines.append(_T["jobs_empty"])
            else:
                lines += self._jobs_table(data.jobs)
        if data.tasks:
            lines += ["", _T["tasks"]]
            lines += [_T["task_line"].format(name=_esc(n, 60), problem=p) for n, p in data.tasks[:10]]
        if self.hub is not None:
            lines += ["", _T["errors"]]
            if not data.errors:
                lines.append(_T["errors_none"])
            for g in data.errors[:ERRORS_LIMIT]:
                icon = "🔕" if g.status == "muted" else "🚨"
                lines.append(
                    _T["error_line"].format(
                        icon=icon,
                        count=g.count,
                        title=_esc(g.title, 120),
                        place=_esc(g.place, 80),
                        at=_hm(g.last_seen),
                    )
                )
        if data.env:
            lines += ["", _T["env"], *data.env]
        lines.append("")
        total = sum(data.attention.values())
        if total:
            parts = " · ".join(
                f"{_SEVERITY_ICON[s]} {data.attention[s]}"
                for s in ("error", "warn", "info")
                if data.attention.get(s)
            )
            lines.append(_T["attention"].format(n=total, parts=parts))
        elif "«Требует внимания»" not in data.failed:
            lines.append(_T["attention_none"])
        if data.failed:
            lines.append(_T["failed"].format(what=", ".join(data.failed)))
        lines.append(_T["updated"].format(at=data.at.strftime("%H:%M:%S UTC")))
        text = "\n".join(lines)
        if len(text) > MAX_TEXT:
            text = text[: MAX_TEXT - 1] + "…"
        keyboard: list[list[InlineKeyboardButton]] = []
        if total:
            keyboard.append([nav_button(_T["att_button"].format(n=total), SCREEN_ATTENTION)])
        keyboard.append([nav_button(_T["panel_button"], SCREEN_PANEL), nav_button(_T["refresh"], SCREEN)])
        if owner:  # the setup wizard is in «🔌 Панель Remnawave»
            keyboard.append([nav_button(_T["maintenance"], SLICE_SCREEN, arg="sys.maint")])
        keyboard.append(nav.back_row(SCREEN))
        return View(text=text, parse_mode="HTML", keyboard=keyboard)

    async def _status_screen(self, ctx: ScreenCtx, _arg: Any) -> View:
        data = await self.collect()
        return self.render(data, owner=ctx.user.role == "owner")

    # ------------------------------------------------------------ «Требует внимания»

    async def _attention_view(self, ctx: ScreenCtx, *, toast: str | None = None) -> View:
        owner = ctx.user.role == "owner"
        items = await self.attention.open_items(limit=ATTENTION_LIMIT + 1)
        lines = [nav.header(SCREEN_ATTENTION), ""]
        keyboard: list[list[InlineKeyboardButton]] = []
        if not items:
            lines.append(_T["att_empty"])
        for n, item in enumerate(items[:ATTENTION_LIMIT], start=1):
            icon = _SEVERITY_ICON.get(item.severity, "•")
            lines.append(f"{n}. {icon} <b>{_esc(item.title, 200)}</b>")
            if item.body:
                lines.append(_esc(item.body, 400))
            lines.append("")
            if owner:
                row: list[InlineKeyboardButton] = []
                target = fix_button_target(item.fix_action)
                if target is not None:
                    data = await ctx.callback(target[0], arg=target[1])
                    row.append(InlineKeyboardButton(text=_T["fix"].format(n=n), callback_data=data))
                row.append(nav_button(_T["snooze"].format(n=n), ACTIONS, A_SNOOZE, str(item.id)))
                keyboard.append(row)
        if len(items) > ATTENTION_LIMIT:
            lines.append(_T["att_more"].format(n=len(items) - ATTENTION_LIMIT))
        text = "\n".join(lines).strip()
        if len(text) > MAX_TEXT:
            text = text[: MAX_TEXT - 1] + "…"
        keyboard.append(nav.back_row(SCREEN_ATTENTION))
        return View(text=text, parse_mode="HTML", keyboard=keyboard, toast=toast)

    async def _attention_screen(self, ctx: ScreenCtx, _arg: Any) -> View:
        return await self._attention_view(ctx)

    async def _snooze(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        if not isinstance(arg, str) or not arg.isdigit() or len(arg) > 18:
            return Toast(_T["att_gone"])
        ok = await self.attention.snooze_for(int(arg), SNOOZE_FOR)
        if not ok:
            return await self._attention_view(ctx, toast=_T["att_gone"])
        log.info("attention item %s snoozed for 24 h by user %s", arg, ctx.user.user_id)
        return await self._attention_view(ctx, toast=_T["att_snoozed"])

    # ------------------------------------------------------------ «Что включено в панели»

    async def panel_report(self) -> str:
        comp = self.components.find("remnawave")
        if not isinstance(comp, RemnawaveComponent) or not comp.configured:
            return _T["panel_off"]
        try:
            async with asyncio.timeout(self.panel_timeout):
                config = await comp.client.configuration()
        except TimeoutError:
            return _T["panel_err"].format(error="панель не ответила вовремя")
        except RemnawaveError as err:
            reason = err.message or err.kind.value
            if err.hint_ru:
                reason = f"{reason}. {err.hint_ru}"
            return _T["panel_err"].format(error=_esc(reason, 400))
        lines: list[str] = []
        for label, env, enabled, shown in panel_features(config):
            icon = "✅" if enabled else "⚪"
            lines.append(f"{icon} {html.escape(label)} (<code>{env}</code>): {html.escape(shown)}")
        if config.misc.sub_public_domain:
            lines.append(_T["panel_domain"].format(domain=_esc(config.misc.sub_public_domain, 200)))
        missing = missing_notify_groups(config)
        if not config.notifications.webhook:
            lines += ["", _T["panel_hooks"]]
        if missing:
            snippet = build_env_snippet(our_url=None, notify=missing)
            lines += [
                "",
                _T["panel_missing"],
                snippet_block(snippet),
                _T["warn_commas"],
                _T["warn_comments"],
                _T["panel_without"],
            ]
        elif config.notifications.webhook:
            lines += ["", _T["panel_all"]]
        return "\n".join(lines)

    async def _panel_screen(self, ctx: ScreenCtx, _arg: Any) -> View:
        body = await self.panel_report()
        keyboard: list[list[InlineKeyboardButton]] = [
            [nav_button(_T["refresh"], SCREEN_PANEL)],
        ]
        if ctx.user.role == "owner":
            keyboard.append([nav_button(_T["wizard"], WIZARD_SCREEN, arg="wh")])
        keyboard.append(nav.back_row(SCREEN_PANEL))
        return View(text=f"{nav.header(SCREEN_PANEL)}\n\n{body}", parse_mode="HTML", keyboard=keyboard)


# ================================================================================ entry point


class _Deps(Protocol):
    @property
    def components(self) -> ComponentRegistry: ...

    @property
    def attention(self) -> Any: ...


def setup(router: ScreenRouter, deps: _Deps) -> Router:
    """Module entry point for ``svbg.app`` (``setup(router, deps)``).

    Optional attributes of ``deps`` used when present: ``hub``, ``queue``, ``scheduler``, ``settings``,
    ``mirror`` (callable), ``maintenance``, ``started_at`` (``time.monotonic()`` of the start).
    """
    screens = StatusScreens(
        router,
        components=deps.components,
        attention=deps.attention,
        hub=getattr(deps, "hub", None),
        queue=getattr(deps, "queue", None),
        scheduler=getattr(deps, "scheduler", None),
        settings=getattr(deps, "settings", None),
        mirror=getattr(deps, "mirror", None),
        maintenance=getattr(deps, "maintenance", None),
        started_at=getattr(deps, "started_at", None),
    )
    screens.install()
    return screens.aiogram_router()
