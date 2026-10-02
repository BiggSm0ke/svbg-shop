"""Daily report: the numbers on real tables, rendering, the 09:00-in-the-owner's-zone moment and restarts."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from svbg.core import clock
from svbg.ops.daily_report import (
    AdminMoney,
    DailyReport,
    ReportData,
    RevenueLine,
    build_report,
    collect,
    render,
)
from svbg.ops.state import K_REPORT, MetaState
from svbg.ops.timing import day_window, due_today, today_window, zone_of
from tests.dbkit import CountingDatabase

MSK = zone_of("Europe/Moscow")
NOW = datetime(2026, 10, 2, 6, 0, 30, tzinfo=UTC)  # 09:00:30 in Moscow
IN = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)  # yesterday in Moscow


def _t(hours: float) -> datetime:
    return IN + timedelta(hours=hours)


@dataclass
class Posts:
    items: list[tuple[str, Any]] = field(default_factory=list)

    async def __call__(self, report: Any, buttons: Sequence[Sequence[Any]] | None) -> None:
        self.items.append((report.html(), buttons))


@pytest.fixture
def frozen() -> Iterator[clock.FrozenClock]:
    fc = clock.FrozenClock(NOW)
    clock.set_clock(fc)
    try:
        yield fc
    finally:
        clock.reset_clock()


async def _user(
    db: CountingDatabase, tg: int, created: datetime, username: str | None = None, role: str = "user"
) -> int:
    rows = await db.raw(
        "insert into users (telegram_id, username, role, created_at) values ($1, $2, $3, $4) returning id",
        tg,
        username,
        role,
        created,
    )
    return int(rows[0]["id"])


async def _order(db: CountingDatabase, uid: int, kind: str, status: str, paid: datetime | None) -> None:
    await db.raw(
        "insert into orders (user_id, kind, status, currency, total_minor, paid_at) "
        "values ($1, $2, $3, 'RUB', 19900, $4)",
        uid,
        kind,
        status,
        paid,
    )


async def _instance(db: CountingDatabase, slug: str, title: str, sort: int) -> int:
    rows = await db.raw(
        "insert into payment_instances (provider, slug, title, sort, config, webhook_token) "
        "values ($1, $1, $2, $3, 'enc:v1:x', 'enc:v1:y') returning id",
        slug,
        title,
        sort,
    )
    return int(rows[0]["id"])


async def _payment(
    db: CountingDatabase, inst: int, uid: int, amount: int, currency: str, status: str, paid: datetime | None,
    *, test: bool = False,
) -> None:  # fmt: skip
    await db.raw(
        "insert into payments (instance_id, user_id, status, amount_minor, currency, paid_at, is_test) "
        "values ($1, $2, $3, $4, $5, $6, $7)",
        inst,
        uid,
        status,
        amount,
        currency,
        paid,
        test,
    )


async def seed_day(db: CountingDatabase) -> None:
    admin = await _user(db, 1, datetime(2026, 9, 1, tzinfo=UTC), "boss", "admin")
    a = await _user(db, 11, _t(0), "alice")
    b = await _user(db, 12, datetime(2026, 9, 19, tzinfo=UTC), "bob")
    c = await _user(db, 13, _t(1))
    await _user(db, 14, _t(2), "<script>")
    await _user(db, 15, datetime(2026, 10, 1, 22, 0, tzinfo=UTC))  # today in Moscow: not counted
    await db.raw("insert into trial_grants (user_id, telegram_id, granted_at) values ($1, 11, $2)", a, _t(-2))
    await db.raw(
        "insert into trial_grants (user_id, telegram_id, granted_at) values ($1, 12, $2)",
        b,
        IN - timedelta(days=12),
    )
    await _order(db, a, "new", "fulfilled", _t(2))  # first purchase after a trial
    await _order(db, b, "new", "fulfilled", IN - timedelta(days=11))
    await _order(db, b, "renew", "fulfilled", _t(3))  # not the first purchase
    await _order(db, c, "new", "fulfilled", _t(4))  # no trial
    await _order(db, c, "change", "fulfilled", _t(5))
    await _order(db, c, "new", "canceled", None)
    await db.raw(
        "insert into orders (user_id, kind, status, currency, total_minor, paid_at) "
        "values ($1, 'topup', 'credited', 'RUB', 50000, $2)",
        a,
        _t(1),
    )
    rolly = await _instance(db, "rollypay", "RollyPay", 10)
    stars = await _instance(db, "stars", "Telegram <Stars>", 20)
    await _payment(db, rolly, a, 19900, "RUB", "paid", _t(1))
    await _payment(db, rolly, c, 19900, "RUB", "paid", _t(4))
    await _payment(db, rolly, c, 99900, "RUB", "paid", datetime(2026, 9, 30, 20, 0, tzinfo=UTC))  # day before
    await _payment(db, rolly, c, 99900, "RUB", "pending", None)
    await _payment(db, rolly, c, 50000, "RUB", "paid", _t(5), test=True)
    await _payment(db, stars, b, 250, "XTR", "paid", _t(3))
    await db.raw(
        "insert into admin_audit (actor_id, role, action, amount_minor, reason, ts) values "
        "($1, 'admin', 'wallet.adjust', 50000, 'компенсация', $2), "
        "($1, 'admin', 'wallet.adjust', -20000, 'ошибка', $2), "
        "($1, 'admin', 'user.ban', null, null, $2), "
        "(null, null, 'wallet.adjust', 1000, 'cli', $2)",
        admin,
        _t(1),
    )
    await db.raw(
        "insert into subscriptions (user_id, desired_expire_at, desired_status) values "
        "($1, $3, 'active'), ($2, $3, 'active'), ($2, $4, 'active'), ($1, $3, 'disabled')",
        a,
        c,
        NOW + timedelta(days=10),
        NOW - timedelta(days=1),
    )


def test_windows_and_due() -> None:
    start, end, day = day_window(NOW, MSK)
    assert (start, end, day) == (
        datetime(2026, 9, 30, 21, 0, tzinfo=UTC),
        datetime(2026, 10, 1, 21, 0, tzinfo=UTC),
        date(2026, 10, 1),
    )
    start, end, day = today_window(NOW, MSK)
    assert (start, end, day) == (datetime(2026, 10, 1, 21, 0, tzinfo=UTC), NOW, date(2026, 10, 2))
    assert due_today(NOW, "09:01", MSK, None) is None
    due = due_today(NOW, "09:00", MSK, None)
    assert due is not None and due.day == "2026-10-02" and due.run
    assert due_today(NOW, "09:00", MSK, "2026-10-02") is None
    late = due_today(NOW + timedelta(hours=4), "09:00", MSK, None)
    assert late is not None and not late.run
    assert due_today(NOW, "25:00", MSK, None) is None
    assert zone_of("Nowhere/City") is UTC


@pytest.mark.pg
async def test_collect_counts_the_right_rows(db: CountingDatabase) -> None:
    await seed_day(db)
    start, end, day = day_window(NOW, MSK)
    mark = db.queries
    data = await collect(db, start, end, day, partial=False)
    assert db.queries - mark == 3
    assert data.new_users == 3
    assert data.trials == 1
    assert (data.purchases_new, data.purchases_renew, data.purchases_other) == (2, 1, 1)
    assert data.after_trial == 1
    assert data.active_subs == 2
    assert data.revenue == [
        RevenueLine("RollyPay", "RUB", 39800, 2),
        RevenueLine("Telegram <Stars>", "XTR", 250, 1),
    ]
    assert data.admin_money == [
        AdminMoney("@boss", 2, 30000, money_count=2),
        AdminMoney("система", 1, 1000, money_count=1),
    ]


@pytest.mark.pg
async def test_render(db: CountingDatabase) -> None:
    await seed_day(db)
    start, end, day = day_window(NOW, MSK)
    text = render(await collect(db, start, end, day, partial=False), currency="RUB", tz_name="Europe/Moscow")
    nb = "\N{NO-BREAK SPACE}"
    assert text == (
        "📊 <b>Отчёт за 1 октября</b>\n\n"
        f"Выручка: <b>398{nb}₽ + 250{nb}⭐</b>\n"
        "Оплат: <b>3</b>\n"
        "Покупок: <b>4</b> (новых 2, продлений 1, других 1)\n"
        "Новых пользователей: <b>3</b>\n"
        "Триалов: <b>1</b>, купили после триала 1\n"
        "Активных подписок: <b>2</b>\n\n"
        "<b>По кассам</b>\n"
        "<pre>Касса            Оплат  Сумма\n"
        "──────────────── ───── ──────\n"
        f"RollyPay             2  398{nb}₽\n"
        f"Telegram &lt;Stars&gt;     1 250{nb}⭐</pre>\n\n"
        "<b>Выдано вручную: 3</b>\n"
        "<pre>Админ   Шт Итог\n"
        "─────── ── ──────\n"
        f"@boss    2 +300{nb}₽\n"
        f"система  1 +10{nb}₽</pre>\n\n"
        "<i>по времени Europe/Moscow</i>"
    ), "titles are escaped, tables fit a phone"


def test_render_empty_and_partial() -> None:
    data = ReportData(NOW, NOW, date(2026, 10, 2), partial=True, admin_money=[AdminMoney("@x", 1, -500)])
    text = render(data, currency="RUB", tz_name="Europe/Moscow")
    assert text.startswith("📊 <b>Отчёт за сегодня, 2 октября</b>")
    assert text.endswith("<i>данные до 09:00, Europe/Moscow</i>")
    assert "Выручка: <b>оплат не было</b>" in text and "Покупок: <b>0</b>\n" in text
    assert "@x     1 −5\N{NO-BREAK SPACE}₽" in text


def test_rich_report_has_tables() -> None:
    data = ReportData(
        NOW,
        NOW,
        date(2026, 10, 1),
        partial=False,
        revenue=[RevenueLine("RollyPay", "RUB", 1234500, 2), RevenueLine("Карта", "RUB", 100, 1)],
    )
    rich = build_report(data, currency="RUB", tz_name="Europe/Moscow").rich()
    blocks = rich.model_dump(mode="json", exclude_none=True)["blocks"]
    assert [b["type"] for b in blocks] == ["heading", "table", "heading", "table", "footer"]
    kassa = blocks[3]
    assert kassa["is_compact"] is True and kassa["is_striped"] is True
    head, first, _, total = kassa["cells"]
    assert [c["text"] for c in head] == ["Касса", "Оплат", "Сумма"] and all(c["is_header"] for c in head)
    assert [c["align"] for c in first] == ["left", "right", "right"]
    assert all(c["valign"] == "middle" for c in first)
    assert first[2]["text"] == "12\N{NARROW NO-BREAK SPACE}345\N{NO-BREAK SPACE}₽"
    assert total[0]["text"] == {"type": "bold", "text": "Итого"}


@pytest.mark.pg
async def test_tick_sends_once_at_the_owners_time(db: CountingDatabase, frozen: clock.FrozenClock) -> None:
    await seed_day(db)
    settings: dict[str, Any] = {"REPORT_DAILY_AT": "09:00", "TIMEZONE": "Europe/Moscow", "CURRENCY": "RUB"}
    posts = Posts()
    buttons = [["btn"]]
    report = DailyReport(db, settings=lambda: settings, post=posts, buttons=lambda: buttons)

    frozen.set(NOW - timedelta(minutes=1))
    mark = db.queries
    assert await report.tick() is False
    assert db.queries == mark, "no SQL before the moment"
    frozen.set(NOW)
    assert await report.tick() is True
    ((text, sent_buttons),) = posts.items
    assert "Отчёт за 1 октября" in text and sent_buttons is buttons
    mark = db.queries
    assert await report.tick() is False and db.queries == mark

    restarted = DailyReport(db, settings=lambda: settings, post=posts)
    assert await restarted.tick() is False and len(posts.items) == 1

    # The owner moves the report to 08:00 Vladivostok (UTC+10) for the next day: applies at once.
    settings.update(REPORT_DAILY_AT="08:00", TIMEZONE="Asia/Vladivostok")
    frozen.set(datetime(2026, 10, 2, 21, 59, tzinfo=UTC))  # 07:59 on Oct 3 in Vladivostok
    assert await restarted.tick() is False
    frozen.set(datetime(2026, 10, 2, 22, 0, 5, tzinfo=UTC))
    assert await restarted.tick() is True
    assert "Отчёт за 2 октября" in posts.items[-1][0] and "Asia/Vladivostok" in posts.items[-1][0]


@pytest.mark.pg
async def test_missed_moment_and_disabled(db: CountingDatabase, frozen: clock.FrozenClock) -> None:
    settings: dict[str, Any] = {"REPORT_DAILY_AT": "09:00", "TIMEZONE": "Europe/Moscow"}
    posts = Posts()
    frozen.set(NOW + timedelta(hours=5))
    report = DailyReport(db, settings=lambda: settings, post=posts)
    assert await report.tick() is False and posts.items == []
    assert (await MetaState(db).get(K_REPORT))["day"] == "2026-10-02"
    settings["REPORT_DAILY_ENABLED"] = False
    frozen.set(NOW + timedelta(days=1))
    mark = db.queries
    assert await DailyReport(db, settings=lambda: settings, post=posts).tick() is False
    assert db.queries == mark and posts.items == []


@pytest.mark.pg
async def test_build_today_so_far(db: CountingDatabase, frozen: clock.FrozenClock) -> None:
    await seed_day(db)
    report = DailyReport(db, settings=lambda: {}, post=Posts())
    text = (await report.build(partial=True)).html()
    assert "Отчёт за сегодня, 2 октября" in text and "данные до 09:00" in text
    assert "Новых пользователей: <b>1</b>" in text, "the user created after midnight in Moscow"


@pytest.mark.pg
async def test_admin_gifts_without_an_amount_are_reported(db: CountingDatabase) -> None:
    admin = await _user(db, 1, datetime(2026, 9, 1, tzinfo=UTC), "boss", "admin")
    other = await _user(db, 2, datetime(2026, 9, 1, tzinfo=UTC), "helper", "admin")
    await db.raw(
        "insert into admin_audit (actor_id, role, action, amount_minor, reason, details, ts) values "
        "($1, 'admin', 'subs.grant', null, 'бонус', '{\"days\": 30}', $3), "
        "($1, 'admin', 'subs.grant', null, 'ошибка', '{\"days\": -5}', $3), "
        "($1, 'admin', 'subs.give_plan', null, 'партнёр', '{\"days\": 7, \"plan_id\": 3}', $3), "
        "($1, 'admin', 'subs.grant', null, 'кривой', '{\"days\": \"много\"}', $3), "
        "($1, 'admin', 'wallet.adjust', 10000, 'компенсация', '{}', $3), "
        "($2, 'admin', 'lte.add_gb', null, '+ГБ', '{\"gb\": 10}', $3), "
        "($2, 'admin', 'promo.create', null, null, '{\"kind\": \"days\"}', $3), "
        "($2, 'admin', 'user.ban', null, null, '{}', $3), "
        "($2, 'admin', 'lte.unblock', null, null, '{}', $3), "
        "($1, 'admin', 'subs.grant', null, 'вчера', '{\"days\": 99}', $4)",
        admin,
        other,
        _t(1),
        IN - timedelta(days=2),
    )
    start, end, day = day_window(NOW, MSK)
    mark = db.queries
    data = await collect(db, start, end, day, partial=False)
    assert db.queries - mark == 3, "still three statements"
    assert data.admin_money == [
        AdminMoney("@boss", 5, 10000, money_count=1, days=32, day_actions=4),
        AdminMoney("@helper", 2, 0, money_count=0, gb=10, promos=1),
    ]
    text = render(data, currency="RUB", tz_name="Europe/Moscow")
    nb = "\N{NO-BREAK SPACE}"
    assert "<b>Выдано вручную: 7</b>" in text
    assert f"@boss    5 +100{nb}₽ · +32{nb}дн." in text
    assert f"@helper  2 +10{nb}ГБ · промокодов 1" in text and "₽ · +10" not in text


@dataclass
class Attention:
    raised: list[tuple[str, str, str]] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)

    async def raise_item(self, key: str, severity: str, title: str, body: str = "", **_: Any) -> None:
        self.raised.append((key, severity, body))

    async def resolve(self, key: str) -> bool:
        self.resolved.append(key)
        return True


@dataclass
class FlakyPost(Posts):
    failures: int = 0

    async def __call__(self, report: Any, buttons: Sequence[Sequence[Any]] | None) -> None:
        if self.failures > 0:
            self.failures -= 1
            raise ConnectionError("telegram 502")
        await super().__call__(report, buttons)


@pytest.mark.pg
async def test_failed_delivery_is_retried_then_resolved(
    db: CountingDatabase, frozen: clock.FrozenClock
) -> None:
    settings: dict[str, Any] = {"REPORT_DAILY_AT": "09:00", "TIMEZONE": "Europe/Moscow"}
    posts = FlakyPost(failures=2)
    attention = Attention()
    report = DailyReport(db, settings=lambda: settings, post=posts, attention=attention)
    assert await report.tick() is False and posts.items == []
    saved = await MetaState(db).get(K_REPORT)
    assert saved["day"] != "2026-10-02" and saved["fail"]["n"] == 1, "the mark is rolled back"

    # A restart in between keeps the attempt count; the next tick retries.
    report = DailyReport(db, settings=lambda: settings, post=posts, attention=attention)
    frozen.set(NOW + timedelta(minutes=1))
    assert await report.tick() is False
    assert (await MetaState(db).get(K_REPORT))["fail"]["n"] == 2
    frozen.set(NOW + timedelta(minutes=2))
    assert await report.tick() is True and len(posts.items) == 1
    saved = await MetaState(db).get(K_REPORT)
    assert saved["day"] == "2026-10-02" and saved["fail"] is None
    assert attention.raised == [] and attention.resolved == ["ops:report"]
    frozen.set(NOW + timedelta(minutes=3))
    assert await report.tick() is False and len(posts.items) == 1, "sent once"


@pytest.mark.pg
async def test_delivery_gives_up_with_an_attention_item(
    db: CountingDatabase, frozen: clock.FrozenClock
) -> None:
    settings: dict[str, Any] = {"REPORT_DAILY_AT": "09:00", "TIMEZONE": "Europe/Moscow"}
    posts = FlakyPost(failures=100)
    attention = Attention()
    report = DailyReport(db, settings=lambda: settings, post=posts, attention=attention, max_attempts=3)
    for minute in range(3):
        frozen.set(NOW + timedelta(minutes=minute))
        assert await report.tick() is False
    assert posts.failures == 97
    ((key, severity, body),) = attention.raised
    assert key == "ops:report" and severity == "warn" and "2026-10-02" in body and "ConnectionError" in body
    assert (await MetaState(db).get(K_REPORT))["day"] == "2026-10-02", "given up: no more attempts"
    frozen.set(NOW + timedelta(minutes=5))
    assert await report.tick() is False and posts.failures == 97
    restarted = DailyReport(db, settings=lambda: settings, post=posts, attention=attention, max_attempts=3)
    assert await restarted.tick() is False and posts.failures == 97


@pytest.mark.pg
async def test_retries_cut_by_the_catch_up_window_raise_an_item(
    db: CountingDatabase, frozen: clock.FrozenClock
) -> None:
    settings: dict[str, Any] = {"REPORT_DAILY_AT": "09:00", "TIMEZONE": "Europe/Moscow"}
    posts = FlakyPost(failures=1)
    attention = Attention()
    report = DailyReport(db, settings=lambda: settings, post=posts, attention=attention)
    assert await report.tick() is False
    frozen.set(NOW + timedelta(hours=4))  # the scheduler was stuck: the moment is gone
    assert await report.tick() is False and posts.items == []
    ((key, _severity, body),) = attention.raised
    assert key == "ops:report" and "время отправки прошло" in body
