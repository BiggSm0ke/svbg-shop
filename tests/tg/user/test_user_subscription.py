"""«👤 Профиль» as the subscription section: the home button (time left, colour by status and the hot
thresholds), the profile with and without a subscription, «Продлить», the old «Подписка» codes as aliases, the
settings and the new home layout for new and older installs."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa

from svbg.content import defaults
from svbg.content.editing import RELAYOUT_SUMMARY, relayout_system_buttons
from svbg.content.store import ContentStore
from svbg.content.tables import screen_buttons
from svbg.core.clock import now
from svbg.core.settings.registry import full_registry
from svbg.tg.user import seeds
from svbg.tg.user.profile import ProfileScreens
from svbg.tg.user.status import SubInfo, UserStatus
from svbg.tg.user.subscription import label_with_left, sub_button
from svbg.tg.user.texts import fmt_bytes, fmt_left_short
from tests.dbkit import open_db
from tests.tg.user.kit import UserEnv, build_user_env

AT = datetime(2026, 10, 2, 12, 0, tzinfo=now().tzinfo)
DAY = timedelta(days=1)


def _status(
    left: timedelta | None, *, trial: bool = False, hold: str | None = None, **sub: Any
) -> UserStatus:
    info = None
    if left is not None:
        info = SubInfo(
            id=1,
            plan_id=1,
            plan_snapshot={"code": "std", "name": {"ru": "Стандарт", "en": "Standard"}, "squads": ["a", "b"]},
            is_trial=trial,
            paid_until=AT + left,
            link_state=sub.pop("link_state", "linked"),
            subscription_url="https://sub.example/x",
            hold_kind=hold,
            extra_devices=0,
            device_limit=sub.pop("device_limit", 5),
            traffic_bytes=sub.pop("traffic_bytes", 100 * 1024**3),
            used_traffic=sub.pop("used_traffic", 1610612736),
            panel_user_id=7,
        )
    return UserStatus(1, 10, "Аня", 0, True, trial, info is not None, False, info)


# ------------------------------------------------------------------------------------------------ pure


@pytest.mark.parametrize(
    ("left", "trial", "text", "style"),
    [
        (20 * DAY, False, "20 дн.", "success"),
        (8 * DAY, False, "8 дн.", "primary"),
        (2 * DAY + timedelta(hours=5), False, "2 дн. 5 ч", "danger"),
        (timedelta(hours=5, minutes=30), False, "5 ч", "danger"),
        (-DAY, False, "закончилась", "danger"),
        (20 * DAY, True, "20 дн.", "danger"),  # a trial is always red
        (2 * DAY + timedelta(hours=5), True, "2 дн. 5 ч", "danger"),
    ],
)
def test_home_button_text_and_colour(left: timedelta, trial: bool, text: str, style: str) -> None:
    b = sub_button(_status(left, trial=trial), "ru", at=AT)
    assert (b.left, b.style) == (text, style)


def test_home_button_without_or_on_hold_and_thresholds() -> None:
    assert sub_button(None, "ru") == sub_button(_status(None), "ru", at=AT)
    none = sub_button(_status(None), "ru", at=AT)
    assert none.left == "" and none.style is None  # the button keeps its own look
    paused = sub_button(_status(20 * DAY, hold="admin"), "ru", at=AT)
    assert (paused.left, paused.style) == ("на паузе", "danger")
    # the blue edge follows what the button shows: «10 дн.» is still green, «9 дн.» is blue
    assert sub_button(_status(9 * DAY + timedelta(hours=1)), "ru", at=AT).style == "success"
    assert sub_button(_status(9 * DAY), "ru", at=AT).style == "primary"
    assert sub_button(_status(3 * DAY), "ru", at=AT).style == "primary"
    assert sub_button(_status(3 * DAY - timedelta(seconds=1)), "ru", at=AT).style == "danger"
    custom = {"blue_days": 25, "red_days": 10, "at": AT}
    assert sub_button(_status(20 * DAY), "ru", **custom).style == "primary"
    assert sub_button(_status(8 * DAY), "ru", **custom).style == "danger"
    assert sub_button(_status(30 * DAY), "en", **custom).left == "30 дн."  # an old language code: ignored


def test_left_formats_and_label() -> None:
    assert fmt_left_short(20 * 86_400 - 5) == "20 дн." and fmt_left_short(2 * 86_400) == "2 дн."
    assert fmt_left_short(2 * 86_400 + 5 * 3_600 + 50) == "2 дн. 5 ч" and fmt_left_short(300) == "5 мин"
    assert fmt_left_short(2 * 86_400 + 5 * 3_600, "en") == "2 дн. 5 ч"
    assert fmt_bytes(1610612736) == "1,5 ГБ" and fmt_bytes(1610612736, "en") == "1,5 ГБ"
    assert fmt_bytes(300 * 1024**2) == "300 МБ" and fmt_bytes(None) == "0 МБ"
    assert label_with_left("📱 Подписка · {left}", "12 дн.") == "📱 Подписка · 12 дн."
    assert label_with_left("📱 Подписка · {left}", "") == "📱 Подписка"
    assert label_with_left("{left} осталось", "") == "осталось"
    assert label_with_left("📱 Подписка", "12 дн.") == "📱 Подписка"
    assert label_with_left("👤 Профиль · {left}", "8 дн.") == "👤 Профиль · 8 дн."
    assert label_with_left("👤 Профиль · {left}", "") == "👤 Профиль"


def test_profile_subscription_values_and_servers() -> None:
    locations = {"a": SimpleNamespace(present=True, label=lambda lang: "🇳🇱 Амстердам")}
    snap = SimpleNamespace(location=locations.get)
    deps = SimpleNamespace(config=lambda: {"TIMEZONE": "UTC"}, catalog=SimpleNamespace(snapshot=snap))
    screens_ = ProfileScreens(deps, None)  # type: ignore[arg-type]
    values = screens_.values(_status(20 * DAY), at=AT)
    assert {k: values[k] for k in ("status", "plan", "left", "until", "traffic", "devices", "servers")} == {
        "status": "🟢 активна",
        "plan": "Стандарт",
        "left": "20 дн.",
        "until": "22.10.2026",
        "traffic": "1,5 ГБ из 100 ГБ",
        "devices": "до 5",
        "servers": "\nСерверы: 🇳🇱 Амстердам",
    }
    assert values["sub"].endswith("Трафик: 1,5 ГБ из 100 ГБ\nСерверы: 🇳🇱 Амстердам")
    unlimited = screens_.values(_status(-DAY, traffic_bytes=0, device_limit=0), at=AT)
    assert unlimited["status"] == "🔴 закончилась" and unlimited["left"] == "—"
    assert unlimited["traffic"] == "1,5 ГБ, без лимита" and unlimited["devices"] == "без ограничений"
    trial = screens_.values(_status(DAY, trial=True, link_state="pending"), at=AT)
    assert trial["status"] == "⏳ подключается" and trial["until"] == "03.10.2026 12:00"
    none = screens_.values(_status(None), trial="\n\n🎁 Можно попробовать бесплатно: 3 дн.", at=AT)
    assert none["sub"].startswith("Подписки пока нет.") and none["sub"].endswith("бесплатно: 3 дн.")
    assert none["servers"] == ""
    # the longest profile: every value at its widest is still far below the caption limit
    widest = {**values, "name": "Я" * 64 + " (@" + "u" * 32 + ")", "id": "9" * 12, "balance": "999 999,99 ₽"}
    text = seeds.SEEDS[seeds.PROFILE].body["ru"]["text"]
    for key, value in widest.items():
        text = text.replace("{" + key + "}", value)
    assert len(text) < 600 and "{" not in text, text


# ------------------------------------------------------------------------------------------------ settings


def test_settings_defaults_and_red_below_blue() -> None:
    reg = full_registry()
    blue, red = reg.get("SUB_BUTTON_BLUE_DAYS"), reg.get("SUB_BUTTON_RED_DAYS")
    assert (blue.default, red.default, blue.section, red.section) == (10, 3, "sales", "sales")
    assert (blue.min, blue.max, red.min, red.max) == (1, 365, 1, 365)
    assert "синеет" in blue.title and "краснеет" in red.title

    def errors(cfg: dict[str, int], changed: set[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for check in reg.checks:
            out.update(check(cfg, frozenset(changed)))
        return out

    assert errors({"SUB_BUTTON_BLUE_DAYS": 10, "SUB_BUTTON_RED_DAYS": 3}, set()) == {}
    bad = errors({"SUB_BUTTON_BLUE_DAYS": 5, "SUB_BUTTON_RED_DAYS": 5}, {"SUB_BUTTON_BLUE_DAYS"})
    assert list(bad) == ["SUB_BUTTON_BLUE_DAYS"] and "меньше синего" in bad["SUB_BUTTON_BLUE_DAYS"]


# ------------------------------------------------------------------------------------------------ bot


async def _paid(env: UserEnv) -> tuple[int, int, int]:
    uid, tg = await env.new_user(balance=20_000)
    await env.open(tg)
    await env.press(tg, "Профиль")
    shown = await env.press(tg, "Купить подписку")
    if "Выберите срок" not in shown.text:  # several plans: the list first
        await env.press(tg, "Тариф 1")
    await env.press(tg, "1 мес.")
    await env.press(tg, "Оплатить")
    await env.drain()
    sid = (await env.rows("select id from subscriptions where user_id = $1", uid))[0]["id"]
    return uid, tg, sid


async def test_home_button_follows_the_time_left_and_hot_thresholds(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn) as env:
        _uid, tg, sid = await _paid(env)

        async def home_button(left: timedelta) -> Any:
            await env.db.raw("update subscriptions set paid_until = $1 where id = $2", now() + left, sid)
            q0 = env.db.queries
            home = await env.click(tg, "v1:home:o")
            assert env.db.queries - q0 <= 2  # the status read only
            assert not any("Подписка" in label for label in home.labels()), home.labels()
            return home.button("Профиль")

        b = await home_button(20 * DAY - timedelta(minutes=5))
        assert (b.text, b.style, b.callback_data) == ("👤 Профиль · 20 дн.", "success", "v1:profile:o")
        b = await home_button(8 * DAY)
        assert (b.text, b.style) == ("👤 Профиль · 8 дн.", "primary")
        b = await home_button(2 * DAY + timedelta(hours=5, minutes=30))
        assert (b.text, b.style) == ("👤 Профиль · 2 дн. 5 ч", "danger")
        b = await home_button(-DAY)
        assert (b.text, b.style) == ("👤 Профиль · закончилась", "danger")
        env.config["SUB_BUTTON_BLUE_DAYS"] = 30  # hot: the next click already uses it
        env.config["SUB_BUTTON_RED_DAYS"] = 21
        assert (await home_button(25 * DAY)).style == "primary"
        assert (await home_button(20 * DAY)).style == "danger"


async def test_profile_with_a_subscription_and_renew(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn, plans=(1, 2)) as env:
        _uid, tg, _sid = await _paid(env)
        await env.click(tg, "v1:home:o")
        q0 = env.db.queries
        profile = await env.press(tg, "Профиль")
        assert env.db.queries - q0 <= 2
        text = profile.text
        assert text.startswith("👤 Профиль") and "Статус: 🟢 активна" in text and "Тариф: " in text
        assert "Осталось: 30 дн." in text and "Действует до: " in text and "Баланс: " in text
        assert "Трафик: 0 МБ, без лимита" in text and "Устройства: " in text and len(text) < 900
        rows = [[b.text for b in row] for row in profile.markup.inline_keyboard]  # type: ignore[union-attr]
        assert rows == [
            ["🔗 Подключиться"],
            ["🔄 Продлить", "📦 Сменить тариф"],
            ["📱 Устройства", "💳 Пополнить"],
            ["◀️ Назад"],
        ]
        periods = await env.press(tg, "Продлить")  # straight to the periods of the current plan
        assert "Выберите срок" in periods.text and periods.data("Назад") == "v1:buy:o"
        plans = await env.click(tg, "v1:buy:o")
        assert plans.data("Назад") == "v1:profile:o"


async def test_profile_without_a_subscription(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn, config={"TRIAL_DAYS": 0}) as env:
        _uid, tg = await env.new_user()
        home = await env.open(tg)
        assert "🎁 Попробовать бесплатно" not in home.labels()
        b = home.button("Профиль")
        assert (b.text, b.style) == ("👤 Профиль", None)  # no subscription: no suffix, its own look
        profile = await env.press(tg, "Профиль")
        assert "Подписки пока нет" in profile.text and "бесплатно" not in profile.text
        assert profile.labels() == ["🛒 Купить подписку", "💳 Пополнить", "◀️ Назад"]
        plans = await env.press(tg, "Купить")
        assert "Выберите срок" in plans.text or "Выберите тариф" in plans.text


async def test_old_subscription_codes_open_the_profile(pg_dsn: str) -> None:
    """Old «📱 Подписка» buttons, deep links (``system:sub``) and notifications open the profile."""
    async with build_user_env(pg_dsn) as env:
        _uid, tg = await env.new_user(first_name="Аня")
        await env.open(tg)
        for data in ("v1:sub:o", "v1:sub_none:o", "v1:sys:sub", "v1:sys:profile"):
            q0 = env.db.queries
            shown = await env.click(tg, data)
            assert shown.text.startswith("👤 Профиль\n\nАня"), (data, shown.text)
            assert env.db.queries - q0 <= 2, data


# ------------------------------------------------------------------------------------------------ layout


def _home_rows(store: ContentStore) -> list[tuple[str | None, int, int]]:
    entry = store.get_screen(defaults.HOME)
    assert entry is not None
    return sorted((b.system_key, b.row, b.sort) for b in entry.screen.buttons)


async def test_new_install_gets_the_profile_layout(pg_dsn: str) -> None:
    async with open_db(pg_dsn) as db:
        store = ContentStore(db)
        await store.load()
        rows = {key: (row, sort) for key, row, sort in _home_rows(store)}
        assert rows["connect"] == (0, 0) and rows["profile"] == (1, 0)
        assert rows["balance"] == (2, 0) and rows["referral"] == (2, 1) and rows["trial"] == (3, 0)
        assert rows["info"] == (4, 0) and rows["admin"][0] == 9
        assert not {"sub", "buy", "renew", "devices", "lang", "promo"} & set(rows)
        for code in (seeds.PROFILE, seeds.PROMOS):
            assert store.get_screen(code) is not None
        for code in (seeds.SUB, seeds.SUB_NONE):  # aliases of the profile now, no content of their own
            assert store.get_screen(code) is None
        labels = {
            r["system_key"]: r["label"]
            for r in await db.raw(
                "select system_key, label from screen_buttons where screen_id = $1",
                store.get_screen(defaults.HOME).id,
            )  # type: ignore[union-attr]
        }
        assert all(set(label) == {"ru"} for label in labels.values()), labels


async def _as_installed(db: Any, home_id: int, buttons: Any) -> None:
    """Make the rows of a screen look like an install seeded with ``buttons`` (old fingerprints)."""
    async with db.tx() as conn:
        for old in buttons:
            values = {
                "label": dict(old.label),
                "action": dict(old.action),
                "visible_if": dict(old.visible_if) if old.visible_if is not None else sa.null(),
                "row": old.row,
                "sort": old.sort,
                "style": old.style,
            }
            done = await conn.execute(
                sa.update(screen_buttons)
                .where(screen_buttons.c.screen_id == home_id, screen_buttons.c.system_key == old.system_key)
                .values(**values)
            )
            if not done.rowcount:
                await conn.execute(
                    sa.insert(screen_buttons).values(screen_id=home_id, system_key=old.system_key, **values)
                )


async def test_previous_install_moves_to_the_profile_layout(pg_dsn: str) -> None:
    """An install of the «Подписка» layout (v2): untouched rows move, «👤 Профиль» is added, «🌐 Язык» and
    «🎟 Промокод» leave home; rows the owner edited stay as they are."""
    async with open_db(pg_dsn) as db:
        store = ContentStore(db)
        await store.load()
        home = store.get_screen(defaults.HOME)
        assert home is not None
        modules = [
            old
            for code, old, _new in defaults.RELAYOUT_SYSTEM_BUTTONS
            if code == defaults.HOME and old.system_key in ("referral", "info") and "en" in old.label  # v2
        ]
        retired = [
            old for _code, old in defaults.RETIRED_SYSTEM_BUTTONS if old.system_key in ("lang", "promo")
        ]
        assert len(modules) == 2 and len(retired) == 2
        await _as_installed(db, home.id, [*seeds.HOME_V2, *modules, *retired])
        async with db.tx() as conn:
            await conn.execute(
                sa.delete(screen_buttons).where(
                    screen_buttons.c.screen_id == home.id, screen_buttons.c.system_key == "profile"
                )
            )
            # the owner moved «Пригласить» and renamed «Промокод»: both are theirs now
            await conn.execute(
                sa.update(screen_buttons)
                .where(screen_buttons.c.system_key == "referral", screen_buttons.c.screen_id == home.id)
                .values(row=6)
            )
            await conn.execute(
                sa.update(screen_buttons)
                .where(screen_buttons.c.system_key == "promo", screen_buttons.c.screen_id == home.id)
                .values(label={"ru": "🎟 Мой промокод"})
            )
        await store.load()  # the next start: seeding adds «Профиль», then the old rows go or move
        rows = {key: (row, sort) for key, row, sort in _home_rows(store)}
        assert rows["connect"] == (0, 0) and rows["profile"] == (1, 0)
        assert rows["balance"] == (2, 0) and rows["trial"] == (3, 0) and rows["info"] == (4, 0)
        assert rows["referral"] == (6, 1) and "promo" in rows  # the owner's rows stay as they are
        assert "lang" not in rows and "sub" not in rows  # untouched: retired
        labels = {
            r["system_key"]: r["label"]
            for r in await db.raw(
                "select system_key, label from screen_buttons where screen_id = $1", home.id
            )
        }
        assert labels["profile"] == {"ru": "👤 Профиль · {left}"}
        assert labels["promo"] == {"ru": "🎟 Мой промокод"}
        async with db.tx() as conn:
            assert await relayout_system_buttons(conn) == 0  # idempotent
        notes = await db.raw(
            "select 1 from content_audit where entity = 'note' and new->>'summary' = $1", RELAYOUT_SUMMARY
        )
        assert len(notes) == 1


async def test_first_install_moves_only_untouched_buttons(pg_dsn: str) -> None:
    """An install seeded before the «Подписка» section (v1): «Купить», «Продлить», «Устройства» and «Язык»
    leave home, «Подключиться» moves up; the owner's edits stay."""
    async with open_db(pg_dsn) as db:
        store = ContentStore(db)
        await store.load()
        home = store.get_screen(defaults.HOME)
        assert home is not None
        v1 = [
            old
            for old, new in seeds.HOME_RELAYOUT
            if old.system_key in ("buy", "renew", "devices", "connect")
        ]
        assert len(v1) == 4
        async with db.tx() as conn:
            await conn.execute(
                sa.delete(screen_buttons).where(
                    screen_buttons.c.screen_id == home.id,
                    screen_buttons.c.system_key.in_(("sub", "profile")),
                )
            )
        await _as_installed(db, home.id, v1)
        async with db.tx() as conn:  # the owner renamed «Продлить»: theirs now
            await conn.execute(
                sa.update(screen_buttons)
                .where(screen_buttons.c.system_key == "renew", screen_buttons.c.screen_id == home.id)
                .values(label={"ru": "🔄 Продлить скорее"})
            )
        await store.load()
        rows = {key: (row, sort) for key, row, sort in _home_rows(store)}
        assert rows["connect"] == (0, 0) and rows["profile"] == (1, 0) and "sub" not in rows
        assert "renew" in rows and "buy" not in rows and "devices" not in rows
        async with db.tx() as conn:
            assert await relayout_system_buttons(conn) == 0


async def test_oldest_install_moves_balance_and_trial_too(pg_dsn: str) -> None:
    """A v1 row matches its own old seed as well as the v2 one: «Баланс» and «Попробовать» take the new rows
    (with the «{balance}» label) instead of staying where v1 put them."""
    async with open_db(pg_dsn) as db:
        store = ContentStore(db)
        await store.load()
        home = store.get_screen(defaults.HOME)
        assert home is not None
        wanted = {("balance", "💰 Баланс", 2), ("trial", "🎁 Попробовать бесплатно", 1)}  # v1 rows
        v1 = [
            old for old, _new in seeds.HOME_RELAYOUT if (old.system_key, old.label["ru"], old.row) in wanted
        ]
        assert len(v1) == 2
        await _as_installed(db, home.id, v1)
        await store.load()
        rows = {key: (row, sort) for key, row, sort in _home_rows(store)}
        assert rows["balance"] == (2, 0) and rows["trial"] == (3, 0)
        labels = {
            r["system_key"]: r["label"]
            for r in await db.raw(
                "select system_key, label from screen_buttons where screen_id = $1", home.id
            )
        }
        assert labels["balance"] == {"ru": "💰 Баланс: {balance}"}
        async with db.tx() as conn:
            assert await relayout_system_buttons(conn) == 0


async def test_install_with_both_buttons_gets_the_combined_profile(pg_dsn: str) -> None:
    """An install of v3 (both «📱 Подписка» and «👤 Профиль» on home): the untouched «Подписка» goes,
    «Профиль» takes its row with the time left, «Пригласить» joins the balance; the profile loses its own
    «Подписка» and gains the subscription buttons; an edited row stays."""
    async with open_db(pg_dsn) as db:
        store = ContentStore(db)
        await store.load()
        home, profile = store.get_screen(defaults.HOME), store.get_screen(seeds.PROFILE)
        assert home is not None and profile is not None
        modules = [
            old
            for code, old, _new in defaults.RELAYOUT_SYSTEM_BUTTONS
            if code == defaults.HOME and old.system_key in ("referral", "info") and set(old.label) == {"ru"}
        ]
        assert len(modules) == 2
        await _as_installed(db, home.id, [*seeds.HOME_V3, *modules])
        v3_profile = [old for old, _new in seeds.PROFILE_RELAYOUT]
        await _as_installed(db, profile.id, v3_profile)
        async with db.tx() as conn:  # v3 had none of these on the profile: seeding adds them back
            await conn.execute(
                sa.delete(screen_buttons).where(
                    screen_buttons.c.screen_id == profile.id,
                    screen_buttons.c.system_key.in_(("change", "buy", "devices", "trial")),
                )
            )
            # the owner recoloured «Пополнить» of the profile: theirs now
            await conn.execute(
                sa.update(screen_buttons)
                .where(screen_buttons.c.system_key == "topup", screen_buttons.c.screen_id == profile.id)
                .values(style="primary")
            )
        await store.load()
        rows = {key: (row, sort) for key, row, sort in _home_rows(store)}
        assert rows["connect"] == (0, 0) and rows["profile"] == (1, 0) and "sub" not in rows
        assert rows["balance"] == (2, 0) and rows["referral"] == (2, 1) and rows["info"] == (4, 0)
        labels = {
            r["system_key"]: r["label"]
            for r in await db.raw(
                "select system_key, label from screen_buttons where screen_id = $1", home.id
            )
        }
        assert labels["profile"] == {"ru": "👤 Профиль · {left}"}
        entry = store.get_screen(seeds.PROFILE)
        assert entry is not None
        got = {b.system_key: (b.row, b.sort) for b in entry.screen.buttons}
        assert "sub" not in got and got["connect"] == (0, 0) and got["renew"] == (1, 0)
        assert got["change"] == (1, 1) and got["devices"] == (2, 0) and got["trial"] == (3, 0)
        assert got["promos"] == (4, 0) and got["referral"] == (4, 1) and got["back"][0] == 9
        assert got["topup"] == (1, 1)  # edited: left where the owner had it
        async with db.tx() as conn:
            assert await relayout_system_buttons(conn) == 0  # idempotent
