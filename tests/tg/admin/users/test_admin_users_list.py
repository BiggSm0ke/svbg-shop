"""«📋 Все пользователи»: pages of 10 newest first, filters with totals, the card returns to the same page."""

from __future__ import annotations

from svbg.tg.admin.users import queries
from svbg.tg.admin.users.screens import SCREEN_ALL, SCREEN_CARD
from svbg.tg.ui.codec import encode
from tests.tg.admin.users.kit import OWNER, SUPPORT, USER, UEnv, add_sub

MORE, PREV, TO_LIST = "Дальше ➡️", "⬅️ Назад", "⬅️ К списку"


def rows(env: UEnv) -> list[str]:
    """Labels of the user rows (they all carry « · »)."""
    return [lb for lb in env.labels() if " · " in lb]


async def people(env: UEnv, n: int, start: int = 7100) -> list[int]:
    return [await env.add(start + i, first_name=f"Клиент{i}") for i in range(n)]


async def test_pages_newest_first_and_back_from_the_card(env: UEnv) -> None:
    ids = await people(env, 12)  # + 5 from the kit = 17
    await env.click(SUPPORT, encode(SCREEN_ALL))
    text = env.text
    assert "<b>📋 Все пользователи</b>" in text and "Всего: 17" in text and "Страница 1." in text
    first = rows(env)
    assert len(first) == 10 and first[0] == "Клиент11 · без подписки"
    assert MORE in env.labels() and PREV not in env.labels() and "✅ Все" in env.labels()
    await env.press(SUPPORT, MORE)
    second = rows(env)
    assert "Страница 2." in env.text and len(second) == 7 and not set(first) & set(second)
    assert MORE not in env.labels() and PREV in env.labels()
    await env.press(SUPPORT, "Клиент1 ·")
    assert f"№ {ids[1]}" in env.text and TO_LIST in env.labels()
    await env.press(SUPPORT, TO_LIST)
    assert "Страница 2." in env.text and rows(env) == second
    await env.press(SUPPORT, PREV)
    assert "Страница 1." in env.text and rows(env) == first
    # the card opened from somewhere else has the usual back row
    await env.click(SUPPORT, encode(SCREEN_CARD, arg=str(env.ids[USER])))
    assert TO_LIST not in env.labels() and "⬅️ Пользователи" in env.labels()


async def test_filters_and_statuses(env: UEnv) -> None:
    active = await env.add(7201, first_name="Активный")
    trial = await env.add(7202, first_name="Пробник")
    expired = await env.add(7203, first_name="Бывший")
    banned = await env.add(7204, username="bad_guy")
    await add_sub(env.db, active, days=10)
    await add_sub(env.db, trial, days=3, is_trial=True)
    await add_sub(env.db, expired, days=-2)
    await env.db.raw("update users set banned_at = now() where id = $1", banned)
    await env.db.raw("update users set captcha_passed_at = now() where id <> $1", env.ids[USER])
    await env.click(OWNER, encode(SCREEN_ALL))
    await env.press(OWNER, "Активные")
    assert "Активные: 1 из 9" in env.text and "✅ Активные" in env.labels()
    assert rows(env) == ["Активный · 🟢 активна · 9 дн."]
    await env.press(OWNER, "Пробные")
    assert rows(env) == ["Пробник · 🎁 пробная · 2 дн."]
    await env.press(OWNER, "Истекли")
    assert rows(env) == ["Бывший · ⛔️ истекла"]
    await env.press(OWNER, "Заблокированы")
    assert rows(env) == ["@bad_guy · 🚫 блок"]
    await env.press(OWNER, "Без подписки")
    assert len(rows(env)) == 6 and "Активный · 🟢 активна · 9 дн." not in rows(env)
    await env.press(OWNER, "Без капчи")
    assert rows(env) == ["Иван · без подписки"]
    # a row keeps the filter: back from the card lands on «Без капчи»
    await env.press(OWNER, "Иван ·")
    await env.press(OWNER, TO_LIST)
    assert "✅ Без капчи" in env.labels() and rows(env) == ["Иван · без подписки"]


async def test_one_sql_per_page_and_cached_totals(env: UEnv) -> None:
    await people(env, 12)
    await env.click(SUPPORT, encode(SCREEN_ALL))  # warm caches (ui_state, totals)
    before = env.db.queries
    await env.press(SUPPORT, MORE)
    assert env.db.queries - before <= 2  # the page itself is one SQL; totals come from the cache
    async with env.db.read() as conn:
        counts = await queries.count_directory(conn)
    assert counts["all"] == 17 and counts["nosub"] == 17 and counts["act"] == 0


async def test_bad_arguments_show_the_first_page(env: UEnv) -> None:
    for arg in ("zzz:1:", "all:0:", "act:1:x5", "all:1:b" + "9" * 30):
        await env.click(SUPPORT, encode(SCREEN_ALL, arg=arg))
        assert "Страница 1." in env.text and "✅ Все" in env.labels()
    await env.click(SUPPORT, encode(SCREEN_CARD, arg=f"{env.ids[USER]}|junk"))
    assert "Telegram ID: <code>5005</code>" in env.text and TO_LIST not in env.labels()
