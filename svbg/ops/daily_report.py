"""Daily report (04 §9.1, 07 stage 3a): yesterday's money and growth into the topic «📊 Отчёты».

Sent once a day at ``REPORT_DAILY_AT`` in ``TIMEZONE`` (both hot: read on every minute tick); a restart up
to 3 h after the moment still sends it, a later one skips that day. «📊 Отчёт сейчас» (Owner / Admin with
``stats``) builds the same report for today so far.

Contents: revenue per payment instance (paid, non-test payments, by currency), purchases (new / renewals),
new users, trials and purchases right after a trial, active subscriptions, and the digest of what admins
gave by hand (``admin_audit``): money (rows with an amount, net sum), days and plans (``subs.grant`` /
``subs.give_plan``, net days), LTE gigabytes (``lte.add_gb``) and created promo codes — per admin.

Three read statements per report, all in the background (never on a click). Delivery: the day is marked
before sending (a crash never sends it twice); a failed build / post rolls the mark back and is retried on the
next ticks (≤ ``MAX_ATTEMPTS``), the last failure raises the «Требует внимания» item ``ops:report``.
"""

from __future__ import annotations

import contextlib
import html
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import sqlalchemy as sa

from svbg.core import clock
from svbg.core.errors.report import format_duration_ru
from svbg.core.money import format_money
from svbg.ops.settings import opt
from svbg.ops.state import K_REPORT, MetaState
from svbg.ops.timing import day_window, due_today, today_window, zone_of

if TYPE_CHECKING:
    from svbg.db.engine import Database

__all__ = [
    "GIFT_ACTIONS",
    "MAX_ATTEMPTS",
    "AdminMoney",
    "DailyReport",
    "ReportData",
    "RevenueLine",
    "collect",
    "render",
]

log = logging.getLogger("svbg.ops.daily_report")

_MONTHS: Final = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)  # fmt: skip
_MAX_LINES: Final = 15
#: Delivery attempts of one day's report (one per minute tick) before it is given up with an attention item.
MAX_ATTEMPTS: Final = 5
ATTENTION_KEY: Final = "ops:report"
#: Audited hand-outs without an amount that the owner must still see (04 §9.1 / stage-3 detection channel).
GIFT_ACTIONS: Final = ("subs.grant", "subs.give_plan", "lte.add_gb", "promo.create")

#: ``post(text, buttons)`` into the reports topic; ``reply(chat_id, text, buttons)`` into a private chat.
Post = Callable[[str, Sequence[Sequence[Any]] | None], Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class RevenueLine:
    title: str
    currency: str
    amount_minor: int
    count: int


@dataclass(frozen=True, slots=True)
class AdminMoney:
    """One admin's hand-outs: ``count`` actions in all; money (``money_count`` rows, ``net_minor``),
    ``days`` net from grants / given plans (``day_actions``), LTE ``gb``, created ``promos``."""

    who: str
    count: int
    net_minor: int
    money_count: int | None = None  # None: all ``count`` actions are money
    days: int = 0
    day_actions: int = 0
    gb: int = 0
    promos: int = 0


@dataclass
class ReportData:
    start: datetime
    end: datetime
    day: date
    partial: bool  # "today so far"
    revenue: list[RevenueLine] = field(default_factory=list)
    new_users: int = 0
    trials: int = 0
    after_trial: int = 0
    purchases_new: int = 0
    purchases_renew: int = 0
    purchases_other: int = 0
    active_subs: int = 0
    admin_money: list[AdminMoney] = field(default_factory=list)
    # support tickets (07 §2.4.6): opened, first answers given, median seconds to the first answer
    tickets_opened: int = 0
    tickets_answered: int = 0
    first_reply_s: float | None = None


_SCALARS: Final = sa.text(
    """
    select
      (select count(*) from users where created_at >= :s and created_at < :e) as new_users,
      (select count(*) from trial_grants
        where granted_at >= :s and granted_at < :e and source = 'bot') as trials,
      (select count(*) filter (where kind = 'new') from orders
         where kind <> 'topup' and paid_at >= :s and paid_at < :e) as p_new,
      (select count(*) filter (where kind = 'renew') from orders
         where kind <> 'topup' and paid_at >= :s and paid_at < :e) as p_renew,
      (select count(*) filter (where kind not in ('new', 'renew')) from orders
         where kind <> 'topup' and paid_at >= :s and paid_at < :e) as p_other,
      (select count(distinct o.user_id) from orders o
         join trial_grants t on t.user_id = o.user_id and t.granted_at <= o.paid_at
        where o.kind <> 'topup' and o.paid_at >= :s and o.paid_at < :e
          and not exists (select 1 from orders p where p.user_id = o.user_id and p.kind <> 'topup'
                           and p.paid_at < o.paid_at)) as after_trial,
      (select count(*) from subscriptions
        where desired_status = 'active' and desired_expire_at > :e) as active_subs,
      (select count(*) from tickets where opened_at >= :s and opened_at < :e) as t_opened,
      (select count(*) from tickets where first_reply_at >= :s and first_reply_at < :e) as t_answered,
      (select percentile_cont(0.5) within group (order by extract(epoch from first_reply_at - opened_at))
         from tickets where first_reply_at >= :s and first_reply_at < :e) as t_reply_s
    """
)
_REVENUE: Final = sa.text(
    """
    select pi.title as title, p.currency as currency, sum(p.amount_minor)::bigint as amount, count(*) as n
      from payments p join payment_instances pi on pi.id = p.instance_id
     where p.status = 'paid' and not p.is_test and p.paid_at >= :s and p.paid_at < :e
     group by pi.title, pi.sort, p.currency
     order by sum(p.amount_minor) desc, pi.sort, pi.title
    """
)
_ADMIN_MONEY: Final = sa.text(
    """
    select a.actor_id, u.username, u.first_name, u.telegram_id,
           count(*) as n,
           count(a.amount_minor) as n_money,
           coalesce(sum(a.amount_minor), 0)::bigint as net,
           count(*) filter (where a.action in ('subs.grant', 'subs.give_plan')) as n_days,
           coalesce(sum(case when a.action in ('subs.grant', 'subs.give_plan')
                              and jsonb_typeof(a.details -> 'days') = 'number'
                             then trunc((a.details ->> 'days')::numeric) end), 0)::bigint as days,
           coalesce(sum(case when a.action = 'lte.add_gb' and jsonb_typeof(a.details -> 'gb') = 'number'
                             then trunc((a.details ->> 'gb')::numeric) end), 0)::bigint as gb,
           count(*) filter (where a.action = 'promo.create') as n_promo
      from admin_audit a left join users u on u.id = a.actor_id
     where (a.amount_minor is not null
            or a.action in ('subs.grant', 'subs.give_plan', 'lte.add_gb', 'promo.create'))
       and a.ts >= :s and a.ts < :e
     group by a.actor_id, u.username, u.first_name, u.telegram_id
     order by count(*) desc
    """
)


def _who(row: Mapping[str, Any]) -> str:
    if row.get("username"):
        return "@" + str(row["username"])
    if row.get("first_name"):
        return str(row["first_name"])
    if row.get("telegram_id"):
        return f"id {row['telegram_id']}"
    return "система" if row.get("actor_id") is None else f"#{row['actor_id']}"


async def collect(db: Database, start: datetime, end: datetime, day: date, *, partial: bool) -> ReportData:
    data = ReportData(start, end, day, partial)
    params = {"s": start, "e": end}
    async with db.read() as conn:
        row = (await conn.execute(_SCALARS, params)).mappings().one()
        revenue = (await conn.execute(_REVENUE, params)).mappings().all()
        admins = (await conn.execute(_ADMIN_MONEY, params)).mappings().all()
    data.new_users, data.trials, data.after_trial = (
        int(row["new_users"]),
        int(row["trials"]),
        int(row["after_trial"]),
    )
    data.purchases_new, data.purchases_renew = int(row["p_new"]), int(row["p_renew"])
    data.purchases_other, data.active_subs = int(row["p_other"]), int(row["active_subs"])
    data.tickets_opened, data.tickets_answered = int(row["t_opened"]), int(row["t_answered"])
    data.first_reply_s = None if row["t_reply_s"] is None else float(row["t_reply_s"])
    data.revenue = [
        RevenueLine(str(r["title"]), str(r["currency"]), int(r["amount"]), int(r["n"])) for r in revenue
    ]
    data.admin_money = [
        AdminMoney(
            _who(r),
            int(r["n"]),
            int(r["net"]),
            money_count=int(r["n_money"]),
            days=int(r["days"]),
            day_actions=int(r["n_days"]),
            gb=int(r["gb"]),
            promos=int(r["n_promo"]),
        )
        for r in admins
    ]
    return data


def _money(amount: int, currency: str) -> str:
    try:
        return format_money(amount, currency, nbsp=True)
    except ValueError:
        return f"{amount} {currency}"


def _signed(amount: int, currency: str) -> str:
    text = _money(abs(amount), currency)
    return ("+" if amount > 0 else "−" if amount < 0 else "") + text


def _signed_int(value: int, unit: str) -> str:
    return ("+" if value > 0 else "−" if value < 0 else "±") + f"{abs(value)}\N{NO-BREAK SPACE}{unit}"


def _admin_line(a: AdminMoney, currency: str) -> str:
    money = a.count if a.money_count is None else a.money_count
    parts: list[str] = []
    if money:
        parts.append(_signed(a.net_minor, currency))
    if a.day_actions:
        parts.append(_signed_int(a.days, "дн."))
    if a.gb:
        parts.append(_signed_int(a.gb, "ГБ"))
    if a.promos:
        parts.append(f"промокодов {a.promos}")
    return f"   {html.escape(a.who)} — {a.count}" + "".join(f" · {html.escape(p)}" for p in parts)


def render(data: ReportData, *, currency: str, tz_name: str) -> str:
    """Telegram HTML; every dynamic piece is escaped."""
    e = html.escape
    when = f"{data.day.day} {_MONTHS[data.day.month - 1]}"
    if data.partial:
        title = f"📊 <b>Отчёт за сегодня, {when}</b> (до {data.end.astimezone(zone_of(tz_name)):%H:%M})"
    else:
        title = f"📊 <b>Отчёт за {when}</b>"
    lines = [title, f"<i>{e(tz_name)}</i>", ""]
    totals: dict[str, int] = {}
    payments = 0
    for line in data.revenue:
        totals[line.currency] = totals.get(line.currency, 0) + line.amount_minor
        payments += line.count
    if totals:
        total = " + ".join(_money(v, c) for c, v in totals.items())
        lines.append(f"💰 Выручка: <b>{e(total)}</b> · оплат: {payments}")
        for line in data.revenue[:_MAX_LINES]:
            lines.append(f"   {e(line.title)} — {e(_money(line.amount_minor, line.currency))} · {line.count}")
        if len(data.revenue) > _MAX_LINES:
            lines.append(f"   … и ещё {len(data.revenue) - _MAX_LINES}")
    else:
        lines.append("💰 Выручка: оплат не было")
    purchases = data.purchases_new + data.purchases_renew + data.purchases_other
    detail = f"новых {data.purchases_new}, продлений {data.purchases_renew}"
    if data.purchases_other:
        detail += f", других {data.purchases_other}"
    lines.append(f"🛒 Покупок: {purchases}" + (f" ({detail})" if purchases else ""))
    lines.append(f"👤 Новых пользователей: {data.new_users}")
    lines.append(f"🎁 Триалов: {data.trials} · купили после триала: {data.after_trial}")
    lines.append(f"📦 Активных подписок: {data.active_subs}")
    if data.tickets_opened or data.tickets_answered:
        reply = ""
        if data.first_reply_s is not None:
            reply = f" · первый ответ (медиана): {format_duration_ru(timedelta(seconds=data.first_reply_s))}"
        lines.append(f"🎫 Обращений: {data.tickets_opened} · с ответом: {data.tickets_answered}{reply}")
    if data.admin_money:
        actions = sum(a.count for a in data.admin_money)
        lines += ["", f"🛠 Вручную (админы): {actions}"]
        lines += [_admin_line(a, currency) for a in data.admin_money[:_MAX_LINES]]
        if len(data.admin_money) > _MAX_LINES:
            lines.append(f"   … и ещё {len(data.admin_money) - _MAX_LINES}")
    return "\n".join(lines)


class DailyReport:
    """Minute tick + «отчёт сейчас». ``post`` puts the text into the reports topic."""

    def __init__(
        self,
        db: Database,
        *,
        settings: Callable[[], Mapping[str, Any]],
        post: Post,
        buttons: Callable[[], Sequence[Sequence[Any]] | None] = lambda: None,
        state: MetaState | None = None,
        catch_up: timedelta = timedelta(hours=3),
        attention: Any = None,  # AttentionService
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        self._db = db
        self._settings = settings
        self._post = post
        self._buttons = buttons
        self._state = state or MetaState(db)
        self._catch_up = catch_up
        self._attention = attention
        self._max_attempts = max(1, max_attempts)
        self._last_day: str | None = None
        self._fails: tuple[str, int] = ("", 0)  # (day, failed attempts) of the report being delivered

    async def build(self, *, partial: bool, now: datetime | None = None) -> str:
        snap = self._settings()
        tz_name = str(opt(snap, "TIMEZONE"))
        zone = zone_of(tz_name)
        now = now or clock.now()
        start, end, day = today_window(now, zone) if partial else day_window(now, zone)
        data = await collect(self._db, start, end, day, partial=partial)
        return render(data, currency=str(opt(snap, "CURRENCY")), tz_name=tz_name)

    async def tick(self) -> bool:
        """Send yesterday's report when it is due; ``True`` if sent. ~0 SQL on the minutes in between."""
        snap = self._settings()
        if not opt(snap, "REPORT_DAILY_ENABLED"):
            return False
        now = clock.now()
        zone = zone_of(opt(snap, "TIMEZONE"))
        due = due_today(now, opt(snap, "REPORT_DAILY_AT"), zone, self._last_day, catch_up=self._catch_up)
        if due is None:
            return False
        if self._last_day is None:  # first due tick after a start: the durable mark decides
            saved = await self._state.get(K_REPORT)
            self._last_day = str(saved.get("day") or "")
            fail = saved.get("fail") if isinstance(saved.get("fail"), dict) else {}
            if fail.get("day") == due.day and str(fail.get("n", "")).isdigit():
                self._fails = (due.day, int(fail["n"]))
            if self._last_day == due.day:
                return False
        previous = self._last_day
        # Marked before sending: a crash in between loses one report rather than sending it twice.
        await self._state.merge(K_REPORT, {"day": due.day, "sent": due.run, "at": now.isoformat()})
        self._last_day = due.day
        failed = self._fails[1] if self._fails[0] == due.day else 0
        if not due.run:
            log.info("daily report for %s skipped: the moment was missed by more than the catch-up", due.day)
            if failed:
                await self._alert(due.day, failed, "время отправки прошло")
            return False
        try:
            text = await self.build(partial=False, now=now)
            await self._post(text, self._buttons())
        except Exception as exc:
            failed += 1
            self._fails = (due.day, failed)
            error = type(exc).__name__
            if failed >= self._max_attempts:
                log.error("daily report for %s failed %d times, given up: %s", due.day, failed, error)  # noqa: TRY400
                with contextlib.suppress(Exception):
                    await self._state.merge(K_REPORT, {"fail": {"day": due.day, "n": failed, "error": error}})
                await self._alert(due.day, failed, error)
                return False
            log.warning("daily report for %s failed (attempt %d): %s", due.day, failed, error, exc_info=exc)
            self._last_day = previous  # the next tick retries
            with contextlib.suppress(Exception):
                await self._state.merge(
                    K_REPORT,
                    {"day": previous, "sent": False, "fail": {"day": due.day, "n": failed, "error": error}},
                )
            return False
        if failed:
            self._fails = ("", 0)
            with contextlib.suppress(Exception):
                await self._state.merge(K_REPORT, {"fail": None})
        if self._attention is not None:  # a delivered report closes an earlier day's «not sent» item too
            with contextlib.suppress(Exception):
                await self._attention.resolve(ATTENTION_KEY)
        return True

    async def _alert(self, day: str, attempts: int, error: str) -> None:
        if self._attention is None:
            return
        with contextlib.suppress(Exception):
            await self._attention.raise_item(
                ATTENTION_KEY,
                "warn",
                "Ежедневный отчёт не отправлен",
                f"Отчёт за {day} не ушёл после {attempts} попыток ({error}). "
                "Соберите его кнопкой «📊 Отчёт сейчас».",
                fix_action="screen:ops",
            )
