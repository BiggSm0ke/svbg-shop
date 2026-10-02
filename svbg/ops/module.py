"""Ops module entry point: ``setup(router, deps)`` (add ``"svbg.ops.module"`` to the app's optional modules).

Wires, from :class:`svbg.app.AppDeps`:

* scheduler tasks — ``ops.backup.tick`` and ``ops.report.tick`` every 60 s (the daily moments are read from
  the settings on every tick, so changes apply without a restart; 0 SQL between the moments) and
  ``ops.updates`` every 12 h;
* the owner screen ``ops`` «💾 Бэкапы и обновления»: last backup, schedule, password state, update status;
  buttons «Бэкап сейчас» (Owner), «Отчёт сейчас» (Owner / Admin with ``stats``), «Проверить обновления»
  (Owner) and the settings section;
* the «📊 Отчёт сейчас» button under every daily report (works in the admin group too: the router checks the
  presser's bot role on every callback — a group member without a role gets «Нет прав»).

Clicks never do heavy work: they answer with a toast and start a background task (≤ 1 per user / 30 s for
reports; one backup at a time). Background tasks are awaited (bounded) on shutdown.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from aiogram.types import InlineKeyboardButton

from svbg.ops.backup import BackupBusyError, BackupError, BackupService, TelegramDelivery, human_size
from svbg.ops.daily_report import DailyReport
from svbg.ops.settings import opt
from svbg.ops.state import K_BACKUP, MetaState
from svbg.ops.timing import zone_of
from svbg.ops.updates import INTERVAL_S, Fetcher, UpdateChecker, UpdateStatus, describe
from svbg.tg.admin import nav
from svbg.tg.report import Report, send_report
from svbg.tg.ui.renderer import nav_button
from svbg.tg.ui.view import Toast, View

if TYPE_CHECKING:
    from svbg.tg.ui.router import HandlerResult, ScreenCtx, ScreenRouter

__all__ = [
    "ACTIONS",
    "A_BACKUP",
    "A_REPORT",
    "A_UPDATES",
    "PERM_STATS",
    "SCREEN",
    "OpsModule",
    "setup",
]

log = logging.getLogger("svbg.ops")

SCREEN: Final = "ops"
ACTIONS: Final = "ops"
A_BACKUP: Final = "bk"
A_REPORT: Final = "rep"
A_UPDATES: Final = "upd"
PERM_STATS: Final = "stats"
K_REPORTS: Final = "reports"
K_BACKUPS: Final = "backups"
K_SYSTEM: Final = "system"
TICK_S: Final = 60.0
REPORT_THROTTLE_S: Final = 30.0
_STOP_GRACE_S: Final = 10.0
#: After cancelling: pg_dump gets SIGTERM, SIGKILL 5 s later (PgTools.arun); 10 + 12 s < stop_grace_period.
_CANCEL_GRACE_S: Final = 12.0

_T: Final = {
    "title": "💾 <b>Бэкапы и обновления</b>",
    "never": "Бэкап: ещё не делался",
    "last": "Последний бэкап: {when} · {size}{enc} · {sent}",
    "last_err": "🔴 Последняя попытка не удалась ({when}): {error}",
    "schedule_on": "Расписание: каждый день в {at} ({tz}), хранить {keep}",
    "schedule_off": "Ежедневный бэкап выключен (BACKUP_ENABLED)",
    "pwd_ok": "🔐 Пароль бэкапов задан",
    "pwd_no": "⚠️ Пароль бэкапов не задан: бэкапы не шифруются и не уходят в Telegram",
    "report_on": "Отчёт: каждый день в {at} ({tz}) в тему «📊 Отчёты»",
    "report_off": "Ежедневный отчёт выключен (REPORT_DAILY_ENABLED)",
    "updates": "Обновления: {text}",
    "running": "⏳ Сейчас идёт бэкап…",
    "b_backup": "💾 Бэкап сейчас",
    "b_report": "📊 Отчёт сейчас",
    "b_updates": "🔄 Проверить обновления",
    "b_settings": "⚙️ Настройки",
    "t_backup": "Бэкап запущен — файл придёт в «💾 Бэкапы»",
    "t_busy": "Бэкап уже выполняется",
    "t_report": "Готовлю отчёт…",
    "t_throttle": "Отчёт уже готовится, подождите полминуты",
    "t_updates": "Проверяю обновления…",
    "sent_yes": "отправлен в Telegram",
    "sent_no": "только на сервере",
    "report_failed": "Не удалось собрать отчёт ({error}). Попробуйте позже.",
    "updates_none": "✅ У вас последняя версия {version}",
    "updates_err": "Не удалось проверить обновления: {error}",
    "updates_repo": "Не задан репозиторий релизов (UPDATE_REPO)",
}


def _esc(text: Any) -> str:
    return html.escape(str(text), quote=False)


class OpsModule:
    """Holds the three services and the UI glue. Built by :func:`setup`; usable alone in tests."""

    def __init__(
        self,
        *,
        settings: Callable[[], Mapping[str, Any]],
        state: MetaState,
        backups: BackupService | None,
        report: DailyReport,
        updates: UpdateChecker,
        post: Callable[..., Awaitable[Any]],
        send: Callable[..., Awaitable[Any]],
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.state = state
        self.backups = backups
        self.report = report
        self.updates = updates
        self._post = post  # post(kind, text, html=True, buttons=…)
        self._send = send  # send(chat_id, text, parse_mode="HTML", reply_markup=…)
        self._monotonic = monotonic
        self._tasks: set[asyncio.Task[Any]] = set()
        self._report_at: dict[int, float] = {}

    # ------------------------------------------------------------------ registration

    def install(self, router: ScreenRouter) -> None:
        router.screen(SCREEN, required_role="owner")(self._screen)
        router.action(ACTIONS, A_BACKUP, required_role="owner")(self._backup_now)
        router.action(ACTIONS, A_REPORT, required_role="admin", perm=PERM_STATS)(self._report_now)
        router.action(ACTIONS, A_UPDATES, required_role="owner")(self._check_updates)

    def schedule(self, scheduler: Any) -> None:
        if self.backups is not None:
            scheduler.every("ops.backup.tick", TICK_S, self.backups.tick, jitter_s=2)
        scheduler.every("ops.report.tick", TICK_S, self.report.tick, jitter_s=2)
        scheduler.every("ops.updates", INTERVAL_S, self.updates.tick, jitter_s=1800)

    @staticmethod
    def report_buttons() -> list[list[InlineKeyboardButton]]:
        return [[nav_button(_T["b_report"], ACTIONS, A_REPORT)]]

    # ------------------------------------------------------------------ background work

    def spawn(self, coro: Awaitable[Any], name: str) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(coro)
        task.set_name(f"ops:{name}")
        self._tasks.add(task)

        def done(t: asyncio.Task[Any]) -> None:
            self._tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                exc = t.exception()
                log.warning("ops task %s failed: %s", name, type(exc).__name__, exc_info=exc)

        task.add_done_callback(done)
        return task

    async def drain(
        self,
        timeout: float = _STOP_GRACE_S,  # noqa: ASYNC109 - graceful deadline
        cancel_grace: float = _CANCEL_GRACE_S,
    ) -> None:
        """Wait for background tasks ≤ ``timeout``, cancel the rest and give them ≤ ``cancel_grace`` to stop
        (a cancelled backup stops pg_dump and removes its partial dump). Bounded: never past the container's
        ``stop_grace_period``."""
        tasks = list(self._tasks)
        if not tasks:
            return
        _done, pending = await asyncio.wait(tasks, timeout=timeout)
        for t in pending:
            t.cancel()
        if pending:
            _done, stuck = await asyncio.wait(pending, timeout=cancel_grace)
            for t in stuck:
                log.warning("ops task %s did not stop in time", t.get_name())

    # ------------------------------------------------------------------ screen

    async def _screen(self, _ctx: ScreenCtx, _arg: Any) -> View:
        return await self.render()

    async def render(self, *, toast: str | None = None) -> View:
        snap = self.settings()
        tz_name = str(opt(snap, "TIMEZONE"))
        zone = zone_of(tz_name)
        lines = [_T["title"], ""]
        state = await self.state.get(K_BACKUP)
        last = state.get("last") if isinstance(state.get("last"), dict) else None
        if last:
            lines.append(
                _T["last"].format(
                    when=_esc(_local(last.get("at"), zone)),
                    size=human_size(int(last.get("size") or 0)),
                    enc="" if last.get("encrypted") else ", без пароля",
                    sent=_T["sent_yes"] if last.get("sent") else _T["sent_no"],
                )
            )
        else:
            lines.append(_T["never"])
        error = state.get("last_error") if isinstance(state.get("last_error"), dict) else None
        if error and (not last or str(error.get("at", "")) > str(last.get("at", ""))):
            lines.append(
                _T["last_err"].format(
                    when=_esc(_local(error.get("at"), zone)), error=_esc(error.get("error", ""))
                )
            )
        if self.backups is not None and self.backups.running:
            lines.append(_T["running"])
        if opt(snap, "BACKUP_ENABLED"):
            lines.append(
                _T["schedule_on"].format(
                    at=_esc(opt(snap, "BACKUP_AT")), tz=_esc(tz_name), keep=int(opt(snap, "BACKUP_KEEP"))
                )
            )
        else:
            lines.append(_T["schedule_off"])
        lines.append(_T["pwd_ok"] if opt(snap, "BACKUP_PASSWORD") else _T["pwd_no"])
        if opt(snap, "REPORT_DAILY_ENABLED"):
            lines.append(_T["report_on"].format(at=_esc(opt(snap, "REPORT_DAILY_AT")), tz=_esc(tz_name)))
        else:
            lines.append(_T["report_off"])
        lines.append(_T["updates"].format(text=_esc(describe(self.updates.last))))
        keyboard = [
            [nav_button(_T["b_backup"], ACTIONS, A_BACKUP, style="primary")],
            [nav_button(_T["b_report"], ACTIONS, A_REPORT), nav_button(_T["b_updates"], ACTIONS, A_UPDATES)],
            [nav_button(_T["b_settings"], "set.v", arg="sys.backup")],
            nav.back_row(SCREEN),
        ]
        return View("\n".join(lines), parse_mode="HTML", keyboard=keyboard, toast=toast)

    # ------------------------------------------------------------------ actions

    async def _backup_now(self, _ctx: ScreenCtx, _arg: Any) -> HandlerResult:
        if self.backups is None:
            return Toast("Бэкапы недоступны: нет подключения к базе", alert=True)
        if self.backups.running:
            return Toast(_T["t_busy"])
        self.spawn(self._run_backup(), "backup")
        await asyncio.sleep(0)  # let the task take the lock, so a second click sees «busy»
        return Toast(_T["t_backup"])

    async def _run_backup(self) -> None:
        assert self.backups is not None
        with contextlib.suppress(BackupBusyError, BackupError):  # already alerted by the service
            await self.backups.run("manual")

    async def _report_now(self, ctx: ScreenCtx, _arg: Any) -> HandlerResult:
        now = self._monotonic()
        key = ctx.user.user_id
        if now - self._report_at.get(key, -1e9) < REPORT_THROTTLE_S:
            return Toast(_T["t_throttle"])
        self._report_at = {k: v for k, v in self._report_at.items() if now - v < REPORT_THROTTLE_S}
        self._report_at[key] = now
        self.spawn(self._deliver_report(ctx.chat_id), "report")
        return Toast(_T["t_report"])

    async def _deliver_report(self, chat_id: int) -> None:
        text: Report | str
        try:
            text = await self.report.build(partial=True)
        except Exception as exc:
            log.warning("report failed: %s", type(exc).__name__, exc_info=exc)
            text = _T["report_failed"].format(error=_esc(type(exc).__name__))
        buttons = self.report_buttons()
        if chat_id > 0:
            from aiogram.types import InlineKeyboardMarkup

            await self._send(
                chat_id, text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
            )
        else:
            await self._post(K_REPORTS, text, html=True, buttons=buttons)

    async def _check_updates(self, ctx: ScreenCtx, _arg: Any) -> HandlerResult:
        self.spawn(self._updates_reply(ctx.chat_id), "updates")
        return Toast(_T["t_updates"])

    async def _updates_reply(self, chat_id: int) -> None:
        status: UpdateStatus = await self.updates.check(force=True)
        if status.state == "available":
            return  # the release message went to «⚙️ Система»
        if status.state == "current":
            text = _T["updates_none"].format(version=_esc(status.current))
        elif status.state == "no_repo":
            text = _T["updates_repo"]
        else:
            text = _T["updates_err"].format(error=_esc(status.error or status.state))
        await self._send(chat_id, text, parse_mode="HTML")


def _local(value: Any, zone: Any) -> str:
    try:
        return datetime.fromisoformat(str(value)).astimezone(zone).strftime("%d.%m %H:%M")
    except (TypeError, ValueError):
        return "—"


# ------------------------------------------------------------------------------------------ setup


def build(
    deps: Any,
    *,
    fetcher: Fetcher | None = None,
    content_export: Callable[[Path], Awaitable[None]] | None = None,
) -> OpsModule:
    """The module from :class:`svbg.app.AppDeps` (duck-typed: only the attributes used are needed)."""
    db = deps.db
    settings_service = deps.settings
    state = MetaState(db)
    admin_chat = getattr(deps, "admin_chat", None)
    notifier = deps.notifier

    def settings() -> Mapping[str, Any]:
        return settings_service.current()

    def _bot() -> Any:
        holder = getattr(deps, "holder", None)
        return holder.get() if holder is not None else None

    async def post(kind: str, text: str | Report, **kw: Any) -> Any:
        if admin_chat is not None:
            if isinstance(text, Report):
                kw.pop("html", None)
                return await admin_chat.post_report(kind, text, **kw)
            return await admin_chat.post(kind, text, **kw)
        owners = await deps.owner_ids()
        from aiogram.types import InlineKeyboardMarkup

        buttons = kw.get("buttons")
        markup = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
        for owner in sorted(owners):
            with contextlib.suppress(Exception):
                if isinstance(text, Report):
                    await send_report(notifier, owner, text, reply_markup=markup, bot=_bot())
                    continue
                await notifier.send(
                    owner, text, parse_mode="HTML" if kw.get("html") else None, reply_markup=markup
                )
        return None

    async def send(chat_id: int, text: str | Report, **kw: Any) -> Any:
        if isinstance(text, Report):
            kw.pop("parse_mode", None)
            return await send_report(notifier, chat_id, text, bot=_bot(), **kw)
        return await notifier.send(chat_id, text, **kw)

    dsn = getattr(db, "pg_dsn", None)
    backups: BackupService | None = None
    if dsn:
        holder = deps.holder
        crypto = getattr(deps, "crypto", None)
        backups = BackupService(
            dsn=str(dsn),
            data_dir=Path(deps.env_path).parent,
            env_path=Path(deps.env_path),
            settings=settings,
            state=state,
            delivery=TelegramDelivery(
                holder.get, admin_chat=admin_chat, owners=deps.owner_ids, topic=K_BACKUPS
            ),
            notify=lambda text, high: post(K_BACKUPS, text, html=True, priority=_priority(high)),
            attention=getattr(deps, "attention", None),
            content_export=content_export,
            key_fingerprint=lambda: getattr(crypto, "primary_fingerprint", None),
        )
    report = DailyReport(
        db,
        settings=settings,
        post=lambda report, buttons: post(K_REPORTS, report, buttons=buttons),
        buttons=OpsModule.report_buttons,
        state=state,
        attention=getattr(deps, "attention", None),
    )
    updates = UpdateChecker(
        db,
        settings=settings,
        post=lambda text: post(K_SYSTEM, text, html=True),
        fetcher=fetcher,
        state=state,
    )
    return OpsModule(
        settings=settings, state=state, backups=backups, report=report, updates=updates, post=post, send=send
    )


def _priority(high: bool) -> Any:
    from svbg.tg.notifier import Priority

    return Priority.HIGH if high else Priority.NORMAL


def setup(router: ScreenRouter, deps: Any) -> None:
    """Module entry point for ``svbg.app`` (``setup(router, deps)``)."""
    module = build(deps, content_export=getattr(deps, "content_export", None))
    module.install(router)
    scheduler = getattr(deps, "scheduler", None)
    if scheduler is not None:
        module.schedule(scheduler)
    else:
        log.warning("ops: no scheduler — daily backups, reports and update checks are off")
    deps.on_stop("ops", module.drain)
