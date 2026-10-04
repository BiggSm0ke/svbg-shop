"""The dashboard numbers (04 §9, 01 §1.6 «Статистика») and the screen «📊 Статистика» (``adm.s``).

For owners and admins with ``stats``: the numbers for **today / 7 days / 30 days** in the owner's time zone
(``TIMEZONE``): revenue per payment instance (only real paid payments — admin credits, test and imported
payments are not revenue), new users, trials and how many of those trials were paid for, active paid and trial
subscriptions, expired in the last 7 days, «Требует внимания» (open attention items) and manual receipts
waiting for a decision.

The numbers are two SQL statements, cached in memory for :data:`CACHE_TTL` seconds («🔄 Обновить» re-reads):
a click normally costs no SQL at all. The admin root (``svbg.tg.admin.menu``) shows three lines of the same
cached numbers (:func:`render_live`).
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
from aiogram.types import InlineKeyboardButton

from svbg.core.clock import now
from svbg.core.money import CURRENCY_SYMBOL, exponent, format_money
from svbg.services import roles
from svbg.services.roles import Act, Actor
from svbg.tg.admin import nav
from svbg.tg.report import num, pre_table
from svbg.tg.ui.renderer import nav_button
from svbg.tg.ui.view import Redirect, View

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
    "render_live",
    "render_stats",
    "setup",
    "windows",
]

log = logging.getLogger("svbg.tg.admin.dashboard")

SCREEN: Final = nav.STATS
ACTIONS: Final = "adma"
CACHE_TTL: Final = 60.0
COLLECT_TIMEOUT: Final = 10.0
ATTENTION_TOP: Final = 3
REVENUE_ROWS: Final = 5  # kassas listed one by one on «📊 Статистика» (the rest are in the totals)
PLAN_ROWS: Final = 5  # plans in the sales table of «📊 Статистика»
# Screens of other admin modules (linked only when registered and allowed).
OPS_SCREEN: Final = "ops"  # svbg.ops.module.SCREEN (its action ``ops:rep`` = «📊 Отчёт сейчас»)
ADS_SCREEN: Final = "ads"  # svbg.ads.admin.SCREEN_LIST
SLICE_SCREEN: Final = "set.v"  # svbg.tg.admin.slices.SCREEN

_T: Final[dict[str, str]] = {
    "title": "📊 <b>Статистика</b>",
    "updated": "<i>Данные на {at} ({tz})</i>",
    "periods": "сегодня · 7 дн. · 30 дн.",
    "revenue": "💵 <b>Выручка, {symbol}</b>",
    "revenue_line": "{title}: {d1} · {d7} · {d30}",
    "revenue_total": "<b>Итого {cur}</b>: {d1} · {d7} · {d30}",
    "revenue_none": "Оплат за 30 дней не было.",
    "col_desk": "Касса",
    "col_d1": "Сегодня",
    "col_d7": "7 дн",
    "col_d30": "30 дн",
    "total": "Итого",
    "people": "👥 <b>Пользователи</b>",
    "row_new": "Новые",
    "row_trials": "Пробные",
    "plans": "🛒 <b>Тарифы за 30 дней</b>",
    "col_plan": "Тариф",
    "col_count": "Шт",
    "col_sum": "Сумма, {symbol}",
    "plans_more": "и ещё тарифов: {n}",
    "new_users": "👥 Новые пользователи: {d1} · {d7} · {d30}",
    "trials": "🎁 Пробные: {d1} · {d7} · {d30}",
    "conversion": "📈 Купили после пробной: <b>{conv} из {n}</b>{pct}",
    "active": "📶 Активные подписки: <b>{paid}</b> платных, <b>{trial}</b> пробных",
    "churn": "📉 Истекли за 7 дн. и не продлены: <b>{n}</b>",
    "attention": "⚠️ <b>Требует внимания</b>: {n}",
    "attention_none": "✅ Всё в порядке: открытых проблем нет.",
    "revenue_more": "и ещё касс: {n} (они вошли в итог)",
    "unavailable": "⚠️ Статистика сейчас недоступна, попробуйте обновить через минуту.",
    "live_revenue": "Выручка: сегодня {d1}, за 7 дней {d7}, за 30 дней {d30}",
    "live_users": "Новых сегодня: {new}. Активных подписок: {paid}, пробных: {trial}",
    "live_attention": "Требует внимания: {n}",
    "b_refresh": "🔄 Обновить",
    "b_report": "📊 Отчёт сейчас",
    "b_daily": "📰 Ежедневный отчёт",
    "b_ads": "📢 Воронка рекламы",
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
class PlanSales:
    """Plan purchases paid in the last 30 days (from the wallet, whatever topped it up)."""

    title: str
    currency: str
    count: int
    amount: int


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
    receipts_pending: int = 0  # manual payments waiting for a decision (``manual_receipts.submitted``)
    plans: tuple[PlanSales, ...] = field(default_factory=tuple)

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
rc AS (
    SELECT count(*) AS n FROM manual_receipts WHERE status = 'submitted'
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
       s.paid, s.trial, s.churn, a.n AS att, top.items AS att_top, rc.n AS receipts
FROM u, t, s, a, rc, top
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

_PLANS_SQL: Final = sa.text(
    """
SELECT coalesce(pl.name->>'ru', pl.name->>'en', pl.code, '—') AS title,
       o.currency,
       count(*) AS n,
       coalesce(sum(o.total_minor), 0) AS amount
FROM orders o LEFT JOIN plans pl ON pl.id = o.plan_id
WHERE o.kind IN ('new', 'renew', 'change') AND o.status IN ('paid', 'fulfilled') AND o.paid_at >= :d30
GROUP BY 1, 2
ORDER BY amount DESC, n DESC, title
LIMIT 20
"""
)


async def collect(conn: AsyncConnection, win: Windows) -> Stats:
    """The dashboard numbers: three statements."""
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
    plans = (await conn.execute(_PLANS_SQL, params)).mappings().all()
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
        receipts_pending=int(row["receipts"]),
        revenue=tuple(
            Revenue(str(r["title"]), str(r["currency"]), int(r["r1"]), int(r["r7"]), int(r["r30"]))
            for r in revenue
        ),
        plans=tuple(
            PlanSales(str(r["title"]), str(r["currency"]), int(r["n"]), int(r["amount"])) for r in plans
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
    return roles.actor_of(user)


def _sum_text(amounts: dict[str, int]) -> str:
    return " + ".join(_money(v, cur) for cur, v in amounts.items()) if amounts else _money(0, "RUB")


def render_live(stats: Stats, *, currency: str = "RUB") -> list[str]:
    """The root's three lines from the cached numbers: revenue totals, new users and active subscriptions,
    open problems (only when there are any)."""
    totals = stats.totals() or {currency: (0, 0, 0)}
    periods = [{cur: v[i] for cur, v in totals.items()} for i in range(3)]
    lines = [
        _esc(
            _T["live_revenue"].format(
                d1=_sum_text(periods[0]), d7=_sum_text(periods[1]), d30=_sum_text(periods[2])
            )
        ),
        _T["live_users"].format(new=stats.new_users[0], paid=stats.active_paid, trial=stats.active_trial),
    ]
    if stats.attention_count:
        lines.append(_T["live_attention"].format(n=stats.attention_count))
    return lines


def _units(amount_minor: int, currency: str) -> str:
    """Whole units for a table cell (the currency is in the column header): ``12 346``."""
    try:
        exp = exponent(currency)
    except (KeyError, ValueError):
        exp = 2
    return num(round(amount_minor / 10**exp))


def _symbol(currency: str) -> str:
    return CURRENCY_SYMBOL.get(currency.upper(), currency)


_PERIODS: Final = ("right", "right", "right")


def _revenue_tables(stats: Stats, max_rows: int | None) -> list[str] | None:
    """One ``<pre>`` table per currency: cash desks × today / 7 / 30 days, with a total row; ``None`` if a
    table does not fit a phone (the caller writes lines instead)."""
    listed = stats.revenue if max_rows is None else stats.revenue[:max_rows]
    totals = stats.totals()
    head = [_T["col_desk"], _T["col_d1"], _T["col_d7"], _T["col_d30"]]
    out: list[str] = []
    for cur in dict.fromkeys(r.currency for r in stats.revenue):
        rows = [
            [r.title[:32], *(_units(v, cur) for v in (r.d1, r.d7, r.d30))]
            for r in listed
            if r.currency == cur
        ]
        many = sum(1 for r in stats.revenue if r.currency == cur) > 1
        if many:
            rows.append([_T["total"], *(_units(v, cur) for v in totals[cur])])
        pre = pre_table([head, *rows], ("left", *_PERIODS), rule_before=len(rows) if many else None)
        if pre is None:
            return None
        out += [_T["revenue"].format(symbol=_esc(_symbol(cur))), pre]
    hidden = len(stats.revenue) - len(listed)
    if hidden > 0:
        out.append(_T["revenue_more"].format(n=hidden))
    return out


def _plans_table(stats: Stats) -> list[str]:
    if not stats.plans:
        return []
    shown = stats.plans[:PLAN_ROWS]
    one = len({p.currency for p in stats.plans}) == 1
    symbol = _symbol(stats.plans[0].currency) if one else ""
    rows = [
        [
            p.title[:40],
            num(p.count),
            _units(p.amount, p.currency) + ("" if one else f" {_symbol(p.currency)}"),
        ]
        for p in shown
    ]
    head = [_T["col_plan"], _T["col_count"], _T["col_sum"].format(symbol=symbol) if one else "Сумма"]
    pre = pre_table([head, *rows], ("left", "right", "right"))
    if pre is None:
        return []
    out = ["", _T["plans"], pre]
    if len(stats.plans) > len(shown):
        out.append(_T["plans_more"].format(n=len(stats.plans) - len(shown)))
    return out


def render_stats(stats: Stats, tz: tzinfo, *, max_rows: int | None = None) -> list[str]:
    """«📊 Статистика»: money and people as small monospace tables (today / 7 / 30 days), then lines."""
    lines = [_T["updated"].format(at=stats.at.astimezone(tz).strftime("%d.%m %H:%M"), tz=_esc(str(tz))), ""]
    tables = _revenue_tables(stats, max_rows) if stats.revenue else None
    if tables is not None:
        lines += tables
    elif stats.revenue:
        lines.append(_T["revenue"].format(symbol=_esc(_T["periods"])))
        listed = stats.revenue if max_rows is None else stats.revenue[:max_rows]
        for r in listed:
            lines.append(
                _T["revenue_line"].format(
                    title=_esc(r.title[:32]),
                    d1=_esc(_money(r.d1, r.currency)),
                    d7=_esc(_money(r.d7, r.currency)),
                    d30=_esc(_money(r.d30, r.currency)),
                )
            )
        if len(listed) < len(stats.revenue):
            lines.append(_T["revenue_more"].format(n=len(stats.revenue) - len(listed)))
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
        lines += [_T["revenue"].format(symbol=_esc(_symbol("RUB"))), _T["revenue_none"]]
    lines += _plans_table(stats)
    lines.append("")
    people = pre_table(
        [
            ["", _T["col_d1"], _T["col_d7"], _T["col_d30"]],
            [_T["row_new"], *(num(v) for v in stats.new_users)],
            [_T["row_trials"], *(num(v) for v in stats.trials)],
        ],
        ("left", *_PERIODS),
    )
    if people is not None:
        lines += [_T["people"], people]
    else:
        lines.append(
            _T["new_users"].format(d1=stats.new_users[0], d7=stats.new_users[1], d30=stats.new_users[2])
        )
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
    """Registers «📊 Статистика» (``adm.s``) and «🔄 Обновить» (``adma:rf``; arg ``s`` — back to the stats,
    none — the admin root, as in the old messages of the former dashboard home)."""

    def __init__(self, router: ScreenRouter, dashboard: Dashboard) -> None:
        self.router = router
        self.dashboard = dashboard
        self._installed = False

    def install(self) -> None:
        if self._installed:
            return
        self._installed = True
        self.router.screen(SCREEN, required_role="admin", perm="stats")(self._screen)
        self.router.action(ACTIONS, "rf", required_role="admin", perm="stats")(self._refresh)

    async def _screen(self, ctx: ScreenCtx, _arg: Any) -> View:
        return await self.view(ctx.user)

    async def _refresh(self, ctx: ScreenCtx, arg: Any) -> Redirect:
        await self.dashboard.get(refresh=True)
        return Redirect(SCREEN if arg == "s" else nav.ROOT, toast=_T["refreshed"])

    async def view(self, user: UserCtx, *, refresh: bool = False) -> View:
        stats = await self.dashboard.get(refresh=refresh)
        lines = [nav.header(SCREEN), ""]
        if stats is None:
            lines.append(_T["unavailable"])
        else:
            lines.extend(render_stats(stats, self.dashboard.tz(), max_rows=REVENUE_ROWS))
        return View(text="\n".join(lines), parse_mode="HTML", keyboard=self._keyboard(user))

    def _keyboard(self, user: UserCtx) -> list[list[InlineKeyboardButton]]:
        actor = _actor(user)
        rows: list[list[InlineKeyboardButton]] = []
        top = [nav_button(_T["b_refresh"], ACTIONS, "rf", "s")]
        if nav.has_screen(self.router, OPS_SCREEN) and roles.authorize(actor, Act.STATS):
            top.append(nav_button(_T["b_report"], OPS_SCREEN, "rep"))
        rows.append(top)
        more: list[InlineKeyboardButton] = []
        if nav.has_screen(self.router, SLICE_SCREEN) and user.has_perm("settings.business"):
            more.append(nav_button(_T["b_daily"], SLICE_SCREEN, arg="st.report"))
        if nav.has_screen(self.router, ADS_SCREEN) and user.has_perm("promo"):
            more.append(nav_button(_T["b_ads"], ADS_SCREEN))
        if more:
            rows.append(more)
        rows.append(nav.back_row(SCREEN))
        return rows


def setup(router: Any, deps: Any) -> Any:
    """Kept for configurations that still list this module: the admin home and its sections now come from
    :mod:`svbg.tg.admin.menu` (which also registers «📊 Статистика»)."""
    from svbg.tg.admin import menu

    return menu.setup(router, deps)
