"""«📱 Подписка»: the home button (time left, colour by status and the hot thresholds), the section with and
without a subscription, «Продлить», the settings and the new home layout for new and older installs."""

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
from svbg.tg.user.status import SubInfo, UserStatus
from svbg.tg.user.subscription import SubscriptionScreens, label_with_left, sub_button
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
    assert sub_button(_status(30 * DAY), "en", **custom).left == "30 d"


def test_left_formats_and_label() -> None:
    assert fmt_left_short(20 * 86_400 - 5) == "20 дн." and fmt_left_short(2 * 86_400) == "2 дн."
    assert fmt_left_short(2 * 86_400 + 5 * 3_600 + 50) == "2 дн. 5 ч" and fmt_left_short(300) == "5 мин"
    assert fmt_left_short(2 * 86_400 + 5 * 3_600, "en") == "2 d 5 h"
    assert fmt_bytes(1610612736) == "1,5 ГБ" and fmt_bytes(1610612736, "en") == "1.5 GB"
    assert fmt_bytes(300 * 1024**2) == "300 МБ" and fmt_bytes(None) == "0 МБ"
    assert label_with_left("📱 Подписка · {left}", "12 дн.") == "📱 Подписка · 12 дн."
    assert label_with_left("📱 Подписка · {left}", "") == "📱 Подписка"
    assert label_with_left("{left} осталось", "") == "осталось"
    assert label_with_left("📱 Подписка", "12 дн.") == "📱 Подписка"


def test_section_values_and_servers() -> None:
    locations = {"a": SimpleNamespace(present=True, label=lambda lang: "🇳🇱 Амстердам")}
    snap = SimpleNamespace(location=locations.get)
    deps = SimpleNamespace(config=lambda: {"TIMEZONE": "UTC"}, catalog=SimpleNamespace(snapshot=snap))
    screens_ = SubscriptionScreens(deps, None)  # type: ignore[arg-type]
    values = screens_.values(_status(20 * DAY), "ru", at=AT)
    assert values == {
        "status": "🟢 активна",
        "plan": "Стандарт",
        "left": "20 дн.",
        "until": "22.10.2026",
        "traffic": "1,5 ГБ из 100 ГБ",
        "devices": "до 5",
        "servers": "\nСерверы: 🇳🇱 Амстердам",
    }
    unlimited = screens_.values(_status(-DAY, traffic_bytes=0, device_limit=0), "en", at=AT)
    assert unlimited["status"] == "🔴 expired" and unlimited["left"] == "—"
    assert unlimited["traffic"] == "1.5 GB, no limit" and unlimited["devices"] == "unlimited"
    trial = screens_.values(_status(DAY, trial=True, link_state="pending"), "ru", at=AT)
    assert trial["status"] == "⏳ подключается" and trial["until"] == "03.10.2026 12:00"
    for code in (seeds.SUB, seeds.SUB_NONE):
        for block in seeds.SEEDS[code].body.values():
            assert len(block["text"]) < 300  # far below the caption limit with every value filled


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
    await env.press(tg, "Подписка")
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
            return home.button("Подписка")

        b = await home_button(20 * DAY + timedelta(minutes=5))
        assert (b.text, b.style, b.callback_data) == ("📱 Подписка · 21 дн.", "success", "v1:sub:o")
        b = await home_button(8 * DAY)
        assert (b.text, b.style) == ("📱 Подписка · 8 дн.", "primary")
        b = await home_button(2 * DAY + timedelta(hours=5, minutes=30))
        assert (b.text, b.style) == ("📱 Подписка · 2 дн. 5 ч", "danger")
        b = await home_button(-DAY)
        assert (b.text, b.style) == ("📱 Подписка · закончилась", "danger")
        env.config["SUB_BUTTON_BLUE_DAYS"] = 30  # hot: the next click already uses it
        env.config["SUB_BUTTON_RED_DAYS"] = 21
        assert (await home_button(25 * DAY)).style == "primary"
        assert (await home_button(20 * DAY)).style == "danger"


async def test_section_with_a_subscription_and_renew(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn, plans=(1, 2)) as env:
        _uid, tg, _sid = await _paid(env)
        await env.click(tg, "v1:home:o")
        q0 = env.db.queries
        section = await env.press(tg, "Подписка")
        assert env.db.queries - q0 <= 2
        text = section.text
        assert text.startswith("📱 Подписка") and "Статус: 🟢 активна" in text and "Тариф: " in text
        assert "Осталось: 30 дн." in text and "Действует до: " in text
        assert "Трафик: 0 МБ, без лимита" in text and "Устройства: " in text and len(text) < 900
        labels = section.labels()
        for label in ("🔗 Подключиться", "🔄 Продлить", "📦 Сменить тариф", "📱 Устройства", "🏠 Меню"):
            assert label in labels, labels
        assert "🛒 Купить подписку" not in labels and "🎁 Попробовать бесплатно" not in labels
        periods = await env.press(tg, "Продлить")  # straight to the periods of the current plan
        assert "Выберите срок" in periods.text and periods.data("Назад") == "v1:buy:o"


async def test_section_without_a_subscription(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn, config={"TRIAL_DAYS": 0}) as env:
        _uid, tg = await env.new_user()
        home = await env.open(tg)
        assert "🎁 Попробовать бесплатно" not in home.labels()
        section = await env.press(tg, "Подписка")
        assert "Сейчас подписки нет" in section.text and "бесплатно" not in section.text
        assert (
            section.labels()[0] == "🛒 Купить подписку" and "🎁 Попробовать бесплатно" not in section.labels()
        )
        await env.click(tg, "v1:lang:set:en")
        section = await env.click(tg, "v1:sub:o")
        assert "No subscription right now" in section.text and section.labels()[0] == "🛒 Buy"
        plans = await env.press(tg, "Buy")
        assert "Choose a period" in plans.text or "Choose a plan" in plans.text


# ------------------------------------------------------------------------------------------------ layout


def _home_rows(store: ContentStore) -> list[tuple[str | None, int, int]]:
    entry = store.get_screen(defaults.HOME)
    assert entry is not None
    return sorted((b.system_key, b.row, b.sort) for b in entry.screen.buttons)


async def test_new_install_gets_the_bedolaga_layout(pg_dsn: str) -> None:
    async with open_db(pg_dsn) as db:
        store = ContentStore(db)
        await store.load()
        rows = {key: (row, sort) for key, row, sort in _home_rows(store)}
        assert rows["connect"] == (0, 0) and rows["balance"] == (1, 0) and rows["trial"] == (2, 0)
        assert rows["sub"] == (3, 0) and rows["promo"] == (4, 0) and rows["referral"] == (4, 1)
        assert rows["info"] == (5, 0) and rows["lang"] == (5, 1) and rows["admin"][0] == 9
        assert not {"buy", "renew", "devices"} & set(rows)
        for code in (seeds.SUB, seeds.SUB_NONE):
            assert store.get_screen(code) is not None


async def test_older_install_moves_only_untouched_buttons(pg_dsn: str) -> None:
    async with open_db(pg_dsn) as db:
        store = ContentStore(db)
        await store.load()
        home = store.get_screen(defaults.HOME)
        assert home is not None
        # make it look like an install seeded before the «Подписка» section
        async with db.tx() as conn:
            await conn.execute(
                sa.delete(screen_buttons).where(
                    screen_buttons.c.screen_id == home.id, screen_buttons.c.system_key == "sub"
                )
            )
            for old, _new in seeds.HOME_RELAYOUT:
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
                    .where(
                        screen_buttons.c.screen_id == home.id, screen_buttons.c.system_key == old.system_key
                    )
                    .values(**values)
                )
                if not done.rowcount:
                    await conn.execute(
                        sa.insert(screen_buttons).values(
                            screen_id=home.id, system_key=old.system_key, **values
                        )
                    )
            # the owner moved «Баланс» and renamed «Продлить»: both are theirs now
            await conn.execute(
                sa.update(screen_buttons)
                .where(screen_buttons.c.system_key.in_(("balance",)), screen_buttons.c.screen_id == home.id)
                .values(row=7)
            )
            await conn.execute(
                sa.update(screen_buttons)
                .where(screen_buttons.c.system_key == "renew", screen_buttons.c.screen_id == home.id)
                .values(label={"ru": "🔄 Продлить скорее"})
            )
        await store.load()  # the next start: seeding adds «Подписка», then the untouched rows move
        rows = {key: (row, sort) for key, row, sort in _home_rows(store)}
        assert rows["connect"] == (0, 0) and rows["trial"] == (2, 0) and rows["lang"] == (5, 1)
        assert rows["sub"] == (3, 0)
        assert rows["balance"] == (7, 1) and "renew" in rows  # the owner's rows stay as they are
        assert "buy" not in rows and "devices" not in rows  # untouched ones left home
        labels = {
            r["system_key"]: r["label"]["ru"]
            for r in await db.raw(
                "select system_key, label from screen_buttons where screen_id = $1", home.id
            )
        }
        assert labels["balance"] == "💰 Баланс" and labels["renew"] == "🔄 Продлить скорее"
        async with db.tx() as conn:
            assert await relayout_system_buttons(conn) == 0  # idempotent
        notes = await db.raw(
            "select 1 from content_audit where entity = 'note' and new->>'summary' = $1", RELAYOUT_SUMMARY
        )
        assert len(notes) == 1
        version = await db.raw("select version from screens where id = $1", home.id)
        assert version[0]["version"] >= 2
