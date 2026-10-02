"""«🛠 Админка» — the admin home with the dashboard (04 §9, 01 §1.6 «Статистика»).

One screen ``adm`` for all staff:

* Support sees the entry points (search);
* owners and admins with ``stats`` also see the numbers for **today / 7 days / 30 days** in the owner's time
  zone (``TIMEZONE``): revenue per payment instance (only real paid payments — admin credits, test and
  imported payments are not revenue), new users, trials and how many of those trials were paid for, active
  paid and trial subscriptions, expired in the last 7 days, and «Требует внимания» (open attention items);
* buttons to the other admin sections the viewer may open (users, roles, plans, settings, «Состояние»).

The numbers are two SQL statements, cached in memory for :data:`CACHE_TTL` seconds («🔄 Обновить» re-reads):
a click normally costs no SQL at all. ``/admin`` opens the screen in a private chat.
"""

from __future__ import annotations

import asyncio
import html
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from typing import TYPE_CHECKING, Any, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import sqlalchemy as sa
from aiogram import Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, Message

from svbg.core.clock import now
from svbg.core.money import format_money
from svbg.services import roles
from svbg.services.roles import Act, Actor
from svbg.tg.admin.users import settings_reader
from svbg.tg.admin.users.screens import ADMIN_HOME, SCREEN_FIND
from svbg.tg.ui.renderer import nav_button
from svbg.tg.ui.view import View

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from svbg.db.engine import Database
    from svbg.tg.ui.context import UserCtx
    from svbg.tg.ui.router import ScreenCtx, ScreenRouter

__all__ = [
    "ACTIONS",
    "CACHE_TTL",
    "SCREEN",
    "Dashboard",
    "DashboardScreens",
    "Stats",
    "collect",
    "setup",
    "windows",
]

log = logging.getLogger("svbg.tg.admin.dashboard")

SCREEN: Final = ADMIN_HOME
ACTIONS: Final = "adma"
CACHE_TTL: Final = 60.0
COLLECT_TIMEOUT: Final = 10.0
ATTENTION_TOP: Final = 3
# Screens of other admin modules (linked only when the viewer may open them).
ROLES_SCREEN: Final = "roles"  # svbg.tg.admin.roles.SCREEN_LIST
PLANS_SCREEN: Final = "plans"  # svbg.tg.admin.plans.SCREEN_LIST
SETTINGS_SCREEN: Final = "settings_root"  # svbg.content.defaults.SETTINGS_ROOT
STATUS_SCREEN: Final = "status"  # svbg.tg.admin.status.SCREEN
ATTENTION_SCREEN: Final = "status.att"  # svbg.tg.admin.status.SCREEN_ATTENTION
HOME: Final = "home"

_T: Final[dict[str, str]] = {
    "title": "🛠 <b>Админка</b>",
    "support_hint": "Найдите пользователя по ID, @username, имени, ссылке подписки, shortUuid "
    "или номеру платежа.",
    "updated": "<i>Данные на {at} ({tz})</i>",
    "periods": "сегодня · 7 дн. · 30 дн.",
    "revenue": "💵 <b>Выручка</b> ({periods})",
    "revenue_line": "{title}: {d1} · {d7} · {d30}",
    "revenue_total": "<b>Итого {cur}</b>: {d1} · {d7} · {d30}",
    "revenue_none": "Оплат за 30 дней не было.",
    "new_users": "👥 Новые пользователи: {d1} · {d7} · {d30}",
    "trials": "🎁 Пробные: {d1} · {d7} · {d30}",
    "conversion": "📈 Оплатили после пробной (из взявших за 30 дн.): {conv} из {n}{pct}",
    "active": "📶 Активные подписки: {paid} платных, {trial} пробных",
    "churn": "📉 Истекли за 7 дн. и не продлены: {n}",
    "attention": "⚠️ <b>Требует внимания</b>: {n}",
    "attention_none": "✅ Всё в порядке: открытых проблем нет.",
    "unavailable": "⚠️ Статистика сейчас недоступна, попробуйте обновить через минуту.",
    "b_find": "🔍 Найти пользователя",
    "b_refresh": "🔄 Обновить",
    "b_attention": "⚠️ Требует внимания",
    "b_roles": "👥 Роли",
    "b_plans": "📦 Тарифы",
    "b_settings": "⚙️ Настройки",
    "b_status": "🩺 Состояние",
    "b_home": "🏠 Меню",
    "refreshed": "Обновлено",
}
_SEVERITY_ICON: Final[Mapping[str, str]] = {"error": "🔴", "warn": "🟡", "info": "🔵"}


# ------------------------------------------------------------------------------------------------ data


@dataclass(frozen=True, slots=True)
class Windows:
    today: datetime
    week: datetime
    month: datetime
    at: datetime


def windows(at: datetime, tz: tzinfo) -> Windows:
    """Starts of «сегодня», «7 дней», «30 дней» (local midnights of the owner's time zone, in UTC)."""
    local = at.astimezone(tz)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)

    def utc(days_back: int) -> datetime:
        day = (midnight - timedelta(days=days_back)).replace(tzinfo=None)
        return day.replace(tzinfo=tz).astimezone(UTC)  # re-attach: correct across DST changes

    return Windows(today=utc(0), week=utc(6), month=utc(29), at=at)


@dataclass(frozen=True, slots=True)
class Revenue:
    title: str
    currency: str
    d1: int
    d7: int
    d30: int


@dataclass(frozen=True, slots=True)
class Stats:
    at: datetime
    new_users: tuple[int, int, int]
    trials: tuple[int, int, int]
    trials_converted: int  # of the trials granted in the last 30 days
    active_paid: int
    active_trial: int
    churn_7d: int
    attention_count: int
    attention_top: tuple[tuple[str, str], ...]  # (severity, title)
    revenue: tuple[Revenue, ...] = field(default_factory=tuple)

    def totals(self) -> dict[str, tuple[int, int, int]]:
        out: dict[str, list[int]] = {}
        for r in self.revenue:
            acc = out.setdefault(r.currency, [0, 0, 0])
            acc[0] += r.d1
            acc[1] += r.d7
            acc[2] += r.d30
        return {cur: (v[0], v[1], v[2]) for cur, v in out.items()}


_COUNTS_SQL: Final = sa.text(
    """
WITH u AS (
    SELECT count(*) FILTER (WHERE created_at >= :d1) AS n1,
           count(*) FILTER (WHERE created_at >= :d7) AS n7,
           count(*) AS n30
    FROM users WHERE created_at >= :d30
),
t AS (
    SELECT count(*) FILTER (WHERE g.granted_at >= :d1) AS n1,
           count(*) FILTER (WHERE g.granted_at >= :d7) AS n7,
           count(*) AS n30,
           count(*) FILTER (WHERE EXISTS (
               SELECT 1 FROM subscription_events e
               WHERE e.subscription_id = g.subscription_id AND e.kind = 'trial_converted'
           )) AS conv
    FROM trial_grants g WHERE g.granted_at >= :d30
),
s AS (
    SELECT count(*) FILTER (WHERE NOT is_trial AND paid_until > :now) AS paid,
           count(*) FILTER (WHERE is_trial AND paid_until > :now) AS trial,
           count(*) FILTER (WHERE NOT is_trial AND paid_until <= :now AND paid_until > :d7_ago) AS churn
    FROM subscriptions WHERE link_state IN ('pending', 'linked')
),
a AS (
    SELECT count(*) AS n FROM attention_items
    WHERE resolved_at IS NULL AND (snoozed_until IS NULL OR snoozed_until <= :now)
),
top AS (
    SELECT coalesce(json_agg(json_build_array(severity, title)), '[]'::json) AS items FROM (
        SELECT severity, title FROM attention_items
        WHERE resolved_at IS NULL AND (snoozed_until IS NULL OR snoozed_until <= :now)
        ORDER BY CASE severity WHEN 'error' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END, updated_at DESC, id DESC
        LIMIT :top
    ) x
)
SELECT u.n1 AS u1, u.n7 AS u7, u.n30 AS u30, t.n1 AS t1, t.n7 AS t7, t.n30 AS t30, t.conv,
       s.paid, s.trial, s.churn, a.n AS att, top.items AS att_top
FROM u, t, s, a, top
"""
)

_REVENUE_SQL: Final = sa.text(
    """
SELECT coalesce(i.title, '—') AS title,
       coalesce(p.paid_currency, p.currency) AS currency,
       coalesce(sum(coalesce(p.paid_amount_minor, p.amount_minor)) FILTER (WHERE p.paid_at >= :d1), 0) AS r1,
       coalesce(sum(coalesce(p.paid_amount_minor, p.amount_minor)) FILTER (WHERE p.paid_at >= :d7), 0) AS r7,
       coalesce(sum(coalesce(p.paid_amount_minor, p.amount_minor)), 0) AS r30
FROM payments p LEFT JOIN payment_instances i ON i.id = p.instance_id
WHERE p.status = 'paid' AND NOT p.is_test AND NOT p.is_imported AND p.paid_at >= :d30
GROUP BY 1, 2
ORDER BY r30 DESC, title
LIMIT 20
"""
)


async def collect(conn: AsyncConnection, win: Windows) -> Stats:
    """The dashboard numbers: two statements."""
    params = {
        "d1": win.today,
        "d7": win.week,
        "d30": win.month,
        "now": win.at,
        "d7_ago": win.at - timedelta(days=7),
        "top": ATTENTION_TOP,
    }
    row = (await conn.execute(_COUNTS_SQL, params)).mappings().one()
    revenue = (await conn.execute(_REVENUE_SQL, params)).mappings().all()
    top_raw = row["att_top"] if isinstance(row["att_top"], list) else []
    top = tuple((str(item[0]), str(item[1])) for item in top_raw if isinstance(item, list) and len(item) == 2)
    return Stats(
        at=win.at,
        new_users=(int(row["u1"]), int(row["u7"]), int(row["u30"])),
        trials=(int(row["t1"]), int(row["t7"]), int(row["t30"])),
        trials_converted=int(row["conv"]),
        active_paid=int(row["paid"]),
        active_trial=int(row["trial"]),
        churn_7d=int(row["churn"]),
        attention_count=int(row["att"]),
        attention_top=top,
        revenue=tuple(
            Revenue(str(r["title"]), str(r["currency"]), int(r["r1"]), int(r["r7"]), int(r["r30"]))
            for r in revenue
        ),
    )


class Dashboard:
    """Cached :func:`collect` (one refresh at a time; a failed refresh keeps the previous numbers)."""

    def __init__(
        self,
        db: Database,
        *,
        timezone: Callable[[], str] = lambda: "Europe/Moscow",
        ttl: float = CACHE_TTL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.db = db
        self.timezone = timezone
        self.ttl = ttl
        self._clock = clock
        self._stats: Stats | None = None
        self._at: float | None = None
        self._lock = asyncio.Lock()

    def tz(self) -> tzinfo:
        try:
            return ZoneInfo(self.timezone())
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            return UTC

    def invalidate(self) -> None:
        self._at = None

    async def get(self, *, refresh: bool = False) -> Stats | None:
        if not refresh and self._fresh():
            return self._stats
        async with self._lock:
            if not refresh and self._fresh():
                return self._stats
            try:
                async with asyncio.timeout(COLLECT_TIMEOUT), self.db.read() as conn:
                    self._stats = await collect(conn, windows(now(), self.tz()))
                self._at = self._clock()
            except (sa.exc.SQLAlchemyError, OSError, TimeoutError) as exc:
                log.warning("dashboard statistics failed: %s", type(exc).__name__)
        return self._stats

    def _fresh(self) -> bool:
        return self._stats is not None and self._at is not None and self._clock() - self._at < self.ttl


# ------------------------------------------------------------------------------------------------ screen


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=False)


def _money(amount: int, currency: str) -> str:
    try:
        return format_money(amount, currency, "ru")
    except (ValueError, KeyError):
        return f"{amount} {currency}"


def _actor(user: UserCtx) -> Actor:
    return Actor(user.user_id, user.telegram_id, user.role, user.perms)


def render_stats(stats: Stats, tz: tzinfo) -> list[str]:
    lines = [_T["updated"].format(at=stats.at.astimezone(tz).strftime("%d.%m %H:%M"), tz=_esc(str(tz))), ""]
    lines.append(_T["revenue"].format(periods=_T["periods"]))
    if stats.revenue:
        for r in stats.revenue:
            lines.append(
                _T["revenue_line"].format(
                    title=_esc(r.title[:32]),
                    d1=_esc(_money(r.d1, r.currency)),
                    d7=_esc(_money(r.d7, r.currency)),
                    d30=_esc(_money(r.d30, r.currency)),
                )
            )
        totals = stats.totals()
        if len(stats.revenue) > 1:
            for cur, (d1, d7, d30) in totals.items():
                lines.append(
                    _T["revenue_total"].format(
                        cur=_esc(cur),
                        d1=_esc(_money(d1, cur)),
                        d7=_esc(_money(d7, cur)),
                        d30=_esc(_money(d30, cur)),
                    )
                )
    else:
        lines.append(_T["revenue_none"])
    lines.append("")
    lines.append(_T["new_users"].format(d1=stats.new_users[0], d7=stats.new_users[1], d30=stats.new_users[2]))
    lines.append(_T["trials"].format(d1=stats.trials[0], d7=stats.trials[1], d30=stats.trials[2]))
    n = stats.trials[2]
    if n:
        pct = f" ({round(100 * stats.trials_converted / n)}%)"
        lines.append(_T["conversion"].format(conv=stats.trials_converted, n=n, pct=pct))
    lines.append(_T["active"].format(paid=stats.active_paid, trial=stats.active_trial))
    lines.append(_T["churn"].format(n=stats.churn_7d))
    lines.append("")
    if stats.attention_count:
        lines.append(_T["attention"].format(n=stats.attention_count))
        lines.extend(
            f"{_SEVERITY_ICON.get(sev, '•')} {_esc(title[:120])}" for sev, title in stats.attention_top
        )
    else:
        lines.append(_T["attention_none"])
    return lines


class DashboardScreens:
    """Registers ``adm`` (+ «🔄 Обновить») and ``/admin``."""

    def __init__(self, router: ScreenRouter, dashboard: Dashboard) -> None:
        self.router = router
        self.dashboard = dashboard
        self._installed = False

    def install(self) -> None:
        if self._installed:
            return
        self._installed = True
        self.router.screen(SCREEN, required_role="support")(self._screen)
        self.router.action(ACTIONS, "rf", required_role="admin", perm="stats")(self._refresh)

    async def _screen(self, ctx: ScreenCtx, _arg: Any) -> View:
        return await self.view(ctx.user)

    async def _refresh(self, ctx: ScreenCtx, _arg: Any) -> View:
        view = await self.view(ctx.user, refresh=True)
        view.toast = _T["refreshed"]
        return view

    async def view(self, user: UserCtx, *, refresh: bool = False) -> View:
        actor = _actor(user)
        lines = [_T["title"], ""]
        stats_allowed = roles.authorize(actor, Act.STATS)
        if stats_allowed:
            stats = await self.dashboard.get(refresh=refresh)
            lines.extend(
                render_stats(stats, self.dashboard.tz()) if stats is not None else [_T["unavailable"]]
            )
        else:
            lines.append(_T["support_hint"])
        return View(
            text="\n".join(lines), parse_mode="HTML", keyboard=self._keyboard(user, actor, stats_allowed)
        )

    @staticmethod
    def _keyboard(user: UserCtx, actor: Actor, stats_allowed: bool) -> list[list[InlineKeyboardButton]]:
        rows: list[list[InlineKeyboardButton]] = [[nav_button(_T["b_find"], SCREEN_FIND)]]
        if stats_allowed:
            row = [nav_button(_T["b_refresh"], ACTIONS, "rf")]
            if roles.authorize(actor, Act.SYSTEM_VIEW):
                row.append(nav_button(_T["b_attention"], ATTENTION_SCREEN))
            rows.append(row)
        sections: list[InlineKeyboardButton] = []
        if roles.authorize(actor, Act.ROLES_MANAGE):
            sections.append(nav_button(_T["b_roles"], ROLES_SCREEN))
        if actor.has_perm("plans"):
            sections.append(nav_button(_T["b_plans"], PLANS_SCREEN))
        if actor.has_perm("settings.business"):
            sections.append(nav_button(_T["b_settings"], SETTINGS_SCREEN))
        if roles.authorize(actor, Act.SYSTEM_VIEW):
            sections.append(nav_button(_T["b_status"], STATUS_SCREEN))
        rows.extend(sections[i : i + 2] for i in range(0, len(sections), 2))
        rows.append([nav_button(_T["b_home"], HOME)])
        return rows

    def aiogram_router(self, name: str = "svbg-admin-dashboard") -> Router:
        """``/admin`` in a private chat (any staff role)."""
        router = Router(name=name)

        async def on_admin(message: Message) -> None:
            if not await self.handle_command(message):
                raise SkipHandler

        router.message.register(on_admin, Command("admin"))
        return router

    async def handle_command(self, message: Message) -> bool:
        if message.from_user is None or message.chat.type != "private":
            return False
        try:
            user = await self.router.user_loader(message.from_user)
        except Exception:
            log.exception("user loader failed for /admin")
            return False
        if user is None or not user.at_least("support"):
            return False
        await self.router.show(user, message.chat.id, SCREEN, new=True)
        return True


def setup(router: Any, deps: Any) -> Router:
    """Module entry point for ``svbg.app`` (``setup(router, deps)``)."""
    read = settings_reader(getattr(deps, "settings", None))

    def timezone() -> str:
        try:
            value = read()["TIMEZONE"]
        except KeyError:
            return "Europe/Moscow"
        return str(value or "Europe/Moscow")

    screens = DashboardScreens(router, Dashboard(deps.db, timezone=timezone))
    screens.install()
    return screens.aiogram_router()
