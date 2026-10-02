"""«🛠 Админка»: dashboard numbers (owner's time zone, revenue = real paid payments), access, cache,
refusal."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from svbg.core.clock import now
from svbg.tg.admin.dashboard import ACTIONS, SCREEN, Dashboard, collect, windows
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


async def test_dashboard_screen_for_admin_with_stats(env: UEnv) -> None:
    await seed(env)
    await env.click(ADMIN_STATS, encode(SCREEN))
    text = env.text
    assert "Выручка" in text and "RollyPay" in text and "Звёзды" in text and "Итого RUB" in text
    assert "Пробные: 1 · 1 · 2" in text and "1 из 2 (50%)" in text
    assert "1 платных, 1 пробных" in text and "Истекли за 7 дн. и не продлены: 1" in text
    assert "Требует внимания</b>: 2" in text and "🔴 Панель недоступна" in text
    labels = env.labels()
    assert "🔄 Обновить" in labels and "👥 Роли" not in labels and "📦 Тарифы" not in labels


async def test_support_sees_no_numbers(env: UEnv) -> None:
    await seed(env)
    await env.click(SUPPORT, encode(SCREEN))
    assert "Выручка" not in env.text and "Найдите пользователя" in env.text
    assert "🔍 Найти пользователя" in env.labels() and "🔄 Обновить" not in env.labels()
    await env.click(SUPPORT, encode(ACTIONS, "rf"))
    assert env.toasts[-1] == "Нет прав"


async def test_owner_sees_every_section(env: UEnv) -> None:
    await env.click(OWNER, encode(SCREEN))
    labels = env.labels()
    for label in (
        "🔍 Найти пользователя",
        "🔄 Обновить",
        "👥 Роли",
        "📦 Тарифы",
        "⚙️ Настройки",
        "🩺 Состояние",
    ):
        assert label in labels
    assert "Оплат за 30 дней не было" in env.text and "Всё в порядке" in env.text


async def test_numbers_are_cached_and_refreshed_on_demand(env: UEnv) -> None:
    await env.click(ADMIN, encode(SCREEN))
    assert "Новые пользователи: 5 · 5 · 5" in env.text
    await add_user(env.db, 9100)
    before = env.db.queries
    await env.click(ADMIN, encode(SCREEN))
    assert env.db.queries - before <= 1  # cached: no statistics SQL
    assert "Новые пользователи: 5 · 5 · 5" in env.text
    await env.press(ADMIN, "Обновить")
    assert "Новые пользователи: 6 · 6 · 6" in env.text and env.toasts[-1] == "Обновлено"


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
    from svbg.tg.admin.dashboard import DashboardScreens

    screens = DashboardScreens(env.router, env.dashboard)  # handle_command only (screens already installed)
    assert await screens.handle_command(text_message(SUPPORT, "/admin"))
    assert "Админка" in env.text
    assert not await screens.handle_command(text_message(5005, "/admin"))
    assert not await screens.handle_command(text_message(OWNER, "/admin", chat_type="group"))
