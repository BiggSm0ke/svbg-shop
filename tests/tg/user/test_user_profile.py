"""«👤 Профиль» (also the subscription section) and its «🎟 Промокоды»: the info and the buttons with and
without a subscription, the list of entered codes with the waiting discount, the home layout around them."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from svbg.content import defaults
from svbg.content.store import seed_system_screens
from svbg.core.clock import now
from svbg.promo.service import PromoService, UsedCode
from svbg.promo.user import HISTORY_LIMIT, PromoUserScreens, history_text
from svbg.tg.user import seeds
from svbg.tg.user.profile import ProfileScreens
from svbg.tg.user.status import SubInfo, UserStatus
from svbg.tg.user.texts import fmt_date
from tests.promo.kit import add_promo
from tests.tg.user.kit import UserEnv, build_user_env

_ALL_FLAGS = frozenset({"promo", "referral"})
NBSP = chr(0xA0)


def _with_flags(env: UserEnv, flags: frozenset[str] = _ALL_FLAGS) -> None:
    """The app adds the module flags (``flag:promo`` …) to every user context; the kit's loader does not."""
    load = env.directory.load

    async def loader(tg_user: Any) -> Any:
        user = await load(tg_user)
        return None if user is None else replace(user, flags=user.flags | flags)

    env.router.user_loader = loader


async def _promo(env: UserEnv) -> PromoService:
    service = PromoService(env.db, catalog=env.b.catalog, currency=lambda: "RUB", timezone=lambda: "UTC")
    await service.load()
    PromoUserScreens(service).register(env.router)
    return service


async def _paid(env: UserEnv) -> tuple[int, int]:
    uid, tg = await env.new_user(balance=20_000)
    await env.open(tg)
    await env.press(tg, "Профиль")
    await env.press(tg, "Купить подписку")
    await env.press(tg, "1 мес.")
    await env.press(tg, "Оплатить")
    await env.drain()
    return uid, tg


# ------------------------------------------------------------------------------------------------ pure


def test_profile_values_without_and_with_a_subscription() -> None:
    deps = SimpleNamespace(config=lambda: {"TIMEZONE": "UTC"}, catalog=None)
    screens = ProfileScreens(deps, None)  # type: ignore[arg-type]
    joined = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
    bare = UserStatus(1, 555, None, 0, False, False, False, False, None, created_at=joined)
    values = screens.values(bare)
    assert values["name"] == "без имени" and values["id"] == "555" and values["since"] == "01.09.2026"
    assert values["sub"] == "Подписки пока нет. Нажмите «Купить подписку», чтобы выбрать тариф и срок."
    assert values["username"] == "—" and values["plan"] == "—" and values["servers"] == ""
    assert screens.values(replace(bare, username="anya"))["name"] == "@anya"
    sub = SubInfo(
        id=1,
        plan_id=1,
        plan_snapshot={"code": "std", "name": {"ru": "Стандарт", "en": "Standard"}},
        is_trial=False,
        paid_until=now() + timedelta(days=12, hours=1),
        link_state="linked",
        subscription_url="https://sub.example/x",
        hold_kind=None,
        extra_devices=0,
        device_limit=3,
        traffic_bytes=50 * 1024**3,
        used_traffic=1610612736,
        panel_user_id=7,
    )
    full = screens.values(replace(bare, first_name="Аня", username="@anya", sub=sub), telegram_id=777)
    assert full["name"] == "Аня (@anya)" and full["id"] == "777" and full["username"] == "@anya"
    assert full["sub"] == (
        f"Тариф: Стандарт\nСтатус: 🟢 активна\nОсталось: 13 дн.\nДействует до: {full['until']}\n"
        "Устройства: до 3\nТрафик: 1,5 ГБ из 50 ГБ"
    )
    for code in (seeds.PROFILE, seeds.PROMOS):
        assert len(seeds.SEEDS[code].body["ru"]["text"]) < 200  # with every value filled, far below 1024


def test_history_text() -> None:
    at = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    assert history_text([], "UTC") == "Вы ещё не вводили промокоды."
    items = [
        UsedCode("SALE20", "−20 % на покупку", at, waiting=True),
        UsedCode("X" * 40, "+100 ₽ на баланс", at, waiting=False),
    ]
    assert history_text(items, "UTC") == (
        "Вы вводили:\nSALE20 · −20 % на покупку · ждёт оплаты\n"
        + "X" * 23
        + "… · +100 ₽ на баланс · 01.10.2026"
    )


# ------------------------------------------------------------------------------------------------ bot


def _rows(shown: Any) -> list[list[str]]:
    return [[b.text.replace(NBSP, " ") for b in row] for row in shown.markup.inline_keyboard]


async def test_home_has_profile_and_no_language_or_promo(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn) as env:
        _uid, tg = await env.new_user()
        home = await env.open(tg)
        assert _rows(home) == [
            ["👤 Профиль"],
            ["💰 Баланс: 0 ₽"],
            ["🎁 Попробовать бесплатно"],
            ["💬 Поддержка"],
        ]
        assert home.data("Профиль") == "v1:profile:o"


async def test_home_layout_with_modules_on(pg_dsn: str) -> None:
    """[Подключиться] / [Профиль · left] / [Баланс][Пригласить] / [Информация][Поддержка]."""
    async with build_user_env(pg_dsn) as env:
        async with env.db.tx() as conn:  # the kit seeds home without the module buttons: add them
            await seed_system_screens(conn, [s for s in defaults.SYSTEM_SCREENS if s.code == defaults.HOME])
        await env.content.load()
        _uid, tg = await _paid(env)
        _with_flags(env, frozenset({"referral", "pages"}))
        home = await env.click(tg, "v1:home:o")
        rows = _rows(home)
        assert rows[0] == ["🔗 Подключиться"] and rows[1][0].startswith("👤 Профиль · ")
        assert rows[2][0].startswith("💰 Баланс: ") and rows[2][1] == "🤝 Пригласить"
        assert rows[3:] == [["ℹ️ Информация", "💬 Поддержка"]], rows
        assert home.button("Профиль").style == "success"


async def test_profile_without_a_subscription(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn) as env:
        _uid, tg = await env.new_user(first_name="Аня")
        await env.open(tg)
        q0 = env.db.queries
        profile = await env.press(tg, "Профиль")
        assert env.db.queries - q0 <= 2  # the status read only
        text = profile.text
        assert text.startswith("👤 Профиль\n\nАня\n") and f"ID: {tg}" in text
        assert f"С нами с {fmt_date(now(), 'Europe/Moscow')}" in text and "Баланс: 0 ₽" in text
        assert "\n\nПодписки пока нет. Нажмите «Купить подписку»" in text
        assert text.endswith("🎁 Можно попробовать бесплатно: 3 дн.") and len(text) < 900, text
        # no module is on in this env: «Промокоды» and «Пригласить друзей» stay hidden
        assert _rows(profile) == [
            ["🛒 Купить подписку"],
            ["💳 Пополнить"],
            ["🎁 Попробовать бесплатно"],
            ["◀️ Назад"],
        ]
        balance = await env.press(tg, "Пополнить")
        assert balance.text.startswith("💰 Баланс")
        await env.press(tg, "Меню")
        await env.press(tg, "Профиль")
        home = await env.press(tg, "Назад")
        assert home.text.startswith("👋 Привет, Аня!")


async def test_profile_with_a_subscription_and_modules_on(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn) as env:
        uid, tg = await _paid(env)
        await env.db.raw("update users set username = 'anya' where id = $1", uid)
        _with_flags(env)
        await env.click(tg, "v1:home:o")
        profile = await env.press(tg, "Профиль")
        text = profile.text
        assert "Аня (@anya)" in text and f"ID: {tg}" in text
        assert "Статус: 🟢 активна" in text and "Тариф: " in text and "Осталось: 30 дн." in text
        assert "Действует до: " in text and "Устройства: " in text and "Трафик: 0 МБ, без лимита" in text
        assert "Баланс: " in text and len(text) < 900
        assert _rows(profile) == [
            ["🔗 Подключиться"],
            ["🔄 Продлить", "📦 Сменить тариф"],
            ["📱 Устройства", "💳 Пополнить"],
            ["🎟 Промокоды", "🤝 Пригласить друзей"],
            ["◀️ Назад"],
        ]
        assert (
            profile.data("Промокоды") == "v1:promos:o" and profile.button("Подключиться").style == "primary"
        )
        old = await env.click(tg, "v1:sub:o")  # an old «📱 Подписка» button: the same profile
        assert old.text == profile.text
        periods = await env.press(tg, "Продлить")
        assert "Выберите срок" in periods.text


async def test_promo_codes_in_the_profile(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn) as env:
        service = await _promo(env)
        uid, tg = await env.new_user()
        _with_flags(env, frozenset({"promo"}))
        await env.open(tg)
        await env.press(tg, "Профиль")
        empty = await env.press(tg, "Промокоды")
        assert empty.text == "🎟 Промокоды\n\nВы ещё не вводили промокоды."
        assert empty.labels() == ["✏️ Ввести промокод", "◀️ Назад"] and empty.data("Назад") == "v1:profile:o"
        await add_promo(env.db, "GIFT100", "wallet", amount_minor=10_000, currency="RUB")
        await add_promo(env.db, "SALE20", "percent", percent=20)
        assert (await service.activate(uid, "gift100")).outcome == "applied"
        assert (await service.activate(uid, "SALE20")).outcome == "pending"
        q0 = env.db.queries
        shown = await env.click(tg, "v1:promos:o")
        assert env.db.queries - q0 <= 2
        today = now().astimezone(UTC).strftime("%d.%m.%Y")
        assert shown.text.startswith(
            "🎟 Промокоды\n\nСкидка ждёт оплаты: SALE20, −20 % на покупку. Действует до "
        )
        assert "Вы вводили:\nSALE20 · −20 % на покупку · ждёт оплаты\n" in shown.text
        assert shown.text.endswith(f"GIFT100 · +100 ₽ на баланс · {today}")
        entry = await env.press(tg, "Ввести промокод")  # the old entry form
        assert "Уже ждёт оплаты промокод SALE20" in entry.text and "Отправьте промокод" in entry.text
        again = await env.click(tg, "v1:sys:promo")  # old «🎟 Промокод» buttons and deep links: the same form
        assert "Отправьте промокод" in again.text


async def test_promo_list_shows_the_last_ten(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn) as env:
        service = await _promo(env)
        uid, tg = await env.new_user()
        for i in range(HISTORY_LIMIT + 2):
            await add_promo(env.db, f"BONUS{i:02d}", "wallet", amount_minor=100, currency="RUB")
            assert (await service.activate(uid, f"BONUS{i:02d}")).outcome == "applied"
        sale = await add_promo(env.db, "BIGSALE", "percent", percent=10)
        await env.db.raw(  # a discount that went into a paid order: what it really took off
            "insert into promo_uses (promo_id, user_id, order_id, source, effect, used_at) values"
            " ($1, $2, 777, 'checkout', '{\"discount_minor\": 3580}'::jsonb, now() + interval '1 minute')",
            sale,
            uid,
        )
        items = await service.history(uid)
        assert len(items) == HISTORY_LIMIT
        assert (items[0].code, items[0].what, items[0].waiting) == ("BIGSALE", "−35,80 ₽ на покупку", False)
        assert items[1].code == f"BONUS{HISTORY_LIMIT + 1:02d}" and items[-1].code == "BONUS03"
        shown = await env.open(tg, seeds.PROMOS)
        assert shown.text.count(" · ") == 2 * HISTORY_LIMIT and "BONUS00" not in shown.text
        assert len(shown.text) < 900
