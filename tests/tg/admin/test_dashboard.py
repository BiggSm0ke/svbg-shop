"""«📊 Статистика»: dashboard numbers (owner's time zone, revenue = real paid payments), access, cache,
refusal; the three lines of the same cached numbers on the admin root."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from svbg.core.clock import now
from svbg.tg.admin.dashboard import (
    ACTIONS,
    SCREEN,
    Dashboard,
    PlanSales,
    Revenue,
    Stats,
    collect,
    render_stats,
    windows,
)
from svbg.tg.report import PRE_WIDTH, cell_width
from svbg.tg.ui.codec import encode
from tests.dbkit import CountingDatabase, add_user
from tests.tg.admin.users.kit import (
    ADMIN,
    ADMIN_STATS,
    OWNER,
    SUPPORT,
    UEnv,
    add_instance,
    add_payment,
    add_sub,
    build_uenv,
)
from tests.tg.ui.ui_harness import text_message


@pytest.fixture
async def env(db: CountingDatabase) -> UEnv:
    return await build_uenv(db)


def test_windows_are_local_midnights() -> None:
    at = datetime(2026, 10, 2, 1, 30, tzinfo=UTC)  # 04:30 in Moscow
    w = windows(at, ZoneInfo("Europe/Moscow"))
    assert w.today == datetime(2026, 10, 1, 21, 0, tzinfo=UTC)
    assert w.week == w.today - timedelta(days=6)
    assert w.month == w.today - timedelta(days=29)
    late = windows(datetime(2026, 10, 1, 22, 30, tzinfo=UTC), ZoneInfo("Asia/Vladivostok"))
    assert late.today == datetime(2026, 10, 1, 14, 0, tzinfo=UTC)


async def seed(env: UEnv) -> None:
    db = env.db
    at = now()
    rolly = await add_instance(db, "RollyPay", "rollypay")
    stars = await add_instance(db, "Звёзды", "stars")
    u1 = await add_user(db, 9001)
    u2 = await add_user(db, 9002)
    await db.raw("update users set created_at = $1 where id = $2", at - timedelta(days=20), u2)
    await add_payment(db, rolly, u1, 17900, paid_at=at)
    await add_payment(db, rolly, u1, 49900, paid_at=at - timedelta(days=3))
    await add_payment(db, rolly, u1, 89900, paid_at=at - timedelta(days=20))
    await add_payment(db, rolly, u1, 100000, paid_at=at - timedelta(days=40))  # outside 30 days
    await add_payment(db, rolly, u1, 55500, is_test=True)  # test: not revenue
    await add_payment(db, rolly, u1, 66600, is_imported=True)  # imported: not revenue
    await add_payment(db, rolly, u1, 77700, status="pending")
    await add_payment(db, stars, u2, 250, currency="XTR")
    # admin credits are not revenue
    await db.raw("update users set wallet_minor = 100000 where id = $1", u1)
    # trials: two in the last 30 days, one of them converted
    s1 = await add_sub(db, u1, days=20)
    s2 = await add_sub(db, u2, days=2, is_trial=True)
    await add_sub(db, env.ids[ADMIN], days=-3)  # expired 3 days ago
    await db.raw(
        "insert into trial_grants (user_id, telegram_id, subscription_id) values ($1, 9001, $2)", u1, s1
    )
    await db.raw(
        "insert into trial_grants (user_id, telegram_id, subscription_id, granted_at) "
        "values ($1, 9002, $2, now() - interval '10 days')",
        u2,
        s2,
    )
    await db.raw(
        "insert into subscription_events (subscription_id, kind, source) "
        "values ($1, 'trial_converted', 'bot')",
        s1,
    )
    await db.raw(
        "insert into attention_items (dedup_key, severity, title) values "
        "('a', 'warn', 'Платёжка недоступна'), ('b', 'error', 'Панель недоступна'), "
        "('c', 'info', 'Решено'), ('d', 'info', 'Отложено')"
    )
    await db.raw("update attention_items set resolved_at = now() where dedup_key = 'c'")
    await db.raw("update attention_items set snoozed_until = now() + interval '1 day' where dedup_key = 'd'")
    # plan sales: paid orders of the last 30 days (a draft and an old one are not counted)
    month = await db.raw(
        "insert into plans (code, name) values ('m1', '{\"ru\": \"Месяц\"}'::jsonb) returning id"
    )
    for status, days in (("fulfilled", 1), ("paid", 5), ("draft", 1), ("fulfilled", 40)):
        await db.raw(
            "insert into orders (user_id, kind, status, currency, total_minor, plan_id, paid_at) "
            "values ($1, 'new', $2, 'RUB', 17900, $3, now() - make_interval(days => $4))",
            u1,
            status,
            month[0]["id"],
            days,
        )


async def test_collect(env: UEnv) -> None:
    await seed(env)
    async with env.db.read() as conn:
        stats = await collect(conn, windows(now(), ZoneInfo("Europe/Moscow")))
    revenue = {(r.title, r.currency): (r.d1, r.d7, r.d30) for r in stats.revenue}
    today_rolly = revenue[("RollyPay", "RUB")]
    assert today_rolly[1:] == (17900 + 49900, 17900 + 49900 + 89900)
    assert today_rolly[0] in (17900, 17900 + 49900) or today_rolly[0] == 17900
    assert revenue[("Звёзды", "XTR")][2] == 250
    assert stats.totals()["RUB"][2] == 157700
    assert stats.new_users[2] == 7  # 5 staff + u1 + u2 (u2 20 days ago)
    assert stats.new_users[1] == 6
    assert stats.trials == (1, 1, 2) and stats.trials_converted == 1
    assert stats.active_paid == 1 and stats.active_trial == 1 and stats.churn_7d == 1
    assert stats.attention_count == 2
    assert stats.attention_top[0] == ("error", "Панель недоступна")
    assert [(p.title, p.currency, p.count, p.amount) for p in stats.plans] == [("Месяц", "RUB", 2, 35800)]


async def test_dashboard_screen_for_admin_with_stats(env: UEnv) -> None:
    await seed(env)
    await env.click(ADMIN_STATS, encode(SCREEN))
    text = env.text
    assert "💵 <b>Выручка, ₽</b>\n<pre>Касса" in text and "💵 <b>Выручка, ⭐</b>" in text
    # cash desks × today / 7 / 30 days in whole units (one desk per currency here: no total row)
    assert re.search(r"RollyPay +(179|678) +678 +1\xa0577</pre>", text) and "Звёзды" in text
    assert re.search(r"Месяц +2 +358</pre>", text)
    assert re.search(r"Пробные +1 +1 +2</pre>", text) and "<b>1 из 2</b> (50%)" in text
    assert "<b>1</b> платных, <b>1</b> пробных" in text and "Истекли за 7 дн. и не продлены: <b>1</b>" in text
    assert "Требует внимания</b>: 2" in text and "🔴 Панель недоступна" in text
    labels = env.labels()
    assert "🔄 Обновить" in labels and "👥 Роли" not in labels and "📦 Тарифы" not in labels


async def test_support_sees_no_numbers(env: UEnv) -> None:
    await seed(env)
    await env.click(SUPPORT, encode("adm"))
    assert "Выручка" not in env.text and "пришлите сюда его ID" in env.text
    assert "🔍 Найти пользователя" in env.labels()
    assert env.button("Обновить") == encode("adm")  # no numbers to refresh: just the screen again
    await env.click(SUPPORT, encode(ACTIONS, "rf"))
    assert env.toasts[-1] == "Нет прав"
    await env.click(SUPPORT, encode(SCREEN))
    assert env.toasts[-1] == "Нет прав"


async def test_root_live_line_and_full_numbers(env: UEnv) -> None:
    await seed(env)
    await env.click(OWNER, encode("adm"))
    text = env.text
    assert "Выручка: сегодня" in text and "за 30 дней" in text
    assert "Активных подписок: 1, пробных: 1" in text and "Требует внимания: 2" in text
    assert "RollyPay" not in text  # the split by cash desk is on «📊 Статистика»
    await env.press(OWNER, "📊 Статистика")
    assert "RollyPay" in env.text and "Оплат за 30 дней не было" not in env.text
    assert env.labels()[-2:] == ["🛠 Админка"] or "🛠 Админка" in env.labels()


async def test_owner_stats_screen_is_calm_without_data(env: UEnv) -> None:
    await env.click(OWNER, encode(SCREEN))
    assert "Оплат за 30 дней не было" in env.text and "Всё в порядке" in env.text
    assert "🔄 Обновить" in env.labels() and "🛠 Админка" in env.labels()


async def test_numbers_are_cached_and_refreshed_on_demand(env: UEnv) -> None:
    await env.click(ADMIN, encode(SCREEN))
    assert re.search(r"Новые +5 +5 +5", env.text)
    await add_user(env.db, 9100)
    before = env.db.queries
    await env.click(ADMIN, encode(SCREEN))
    assert env.db.queries - before <= 1  # cached: no statistics SQL
    assert re.search(r"Новые +5 +5 +5", env.text)
    await env.press(ADMIN, "Обновить")
    assert re.search(r"Новые +6 +6 +6", env.text) and env.toasts[-1] == "Обновлено"


async def test_failure_keeps_the_screen_alive(env: UEnv) -> None:
    class Broken:
        def read(self) -> object:
            raise OSError("db down")

    dashboard = Dashboard(Broken(), timezone=lambda: "Nowhere/Bad")  # type: ignore[arg-type]
    assert await dashboard.get() is None
    assert dashboard.tz() == UTC
    env.dashboard.db = Broken()  # type: ignore[assignment]
    await env.click(ADMIN, encode(SCREEN))
    assert "Статистика сейчас недоступна" in env.text


async def test_admin_command(env: UEnv) -> None:
    from svbg.tg.admin.menu import AdminMenu

    screens = AdminMenu(env.router, env.dashboard)  # handle_command only (screens already installed)
    assert await screens.handle_command(text_message(SUPPORT, "/admin"))
    assert "Админка" in env.text
    assert not await screens.handle_command(text_message(5005, "/admin"))
    assert not await screens.handle_command(text_message(OWNER, "/admin", chat_type="group"))


def test_render_stats_tables() -> None:
    at = datetime(2026, 10, 2, 11, 0, tzinfo=UTC)
    stats = Stats(
        at=at,
        new_users=(3, 41, 1180),
        trials=(1, 12, 60),
        trials_converted=21,
        active_paid=340,
        active_trial=25,
        churn_7d=4,
        attention_count=0,
        attention_top=(),
        revenue=(
            Revenue("RollyPay", "RUB", 17900, 67800, 1577000),
            Revenue("ЮKassa основная касса", "RUB", 0, 49950, 1234500),
            Revenue("Третья", "RUB", 100, 100, 100),
        ),
        plans=(PlanSales("Год семейный безлимит", "RUB", 2, 898000),),
    )
    text = "\n".join(render_stats(stats, UTC, max_rows=2))
    tables = [t.replace("\xa0", "_").split("\n") for t in re.findall(r"<pre>(.*?)</pre>", text, re.S)]
    assert len(tables) == 3  # revenue, plans, people
    revenue = tables[0]
    assert revenue[0].split() == ["Касса", "Сегодня", "7", "дн", "30", "дн"]
    assert revenue[2].split() == ["RollyPay", "179", "678", "15_770"]
    assert revenue[3].startswith("ЮKassa осн…")  # clipped to keep the row on a phone screen
    assert revenue[4].startswith("─") and revenue[5].split() == ["Итого", "180", "1_178", "28_116"]
    assert "и ещё касс: 1 (они вошли в итог)" in text
    assert tables[1][0].split() == ["Тариф", "Шт", "Сумма,", "₽"]
    assert tables[1][2].split() == ["Год", "семейный", "безлим…", "2", "8_980"]
    assert tables[2][2].split() == ["Новые", "3", "41", "1_180"]
    assert all(cell_width(line) <= PRE_WIDTH for t in tables for line in t)
    assert "Всё в порядке" in text
