"""User screens as content (seeds valid, fallback without content), Russian only, the channel gate,
``/start`` deep-link stub, the billing messenger, «Я оплатил», cancel."""

from __future__ import annotations

import re
from typing import Any

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandObject
from aiogram.methods import EditMessageText
from aiogram.types import Chat, Message

from svbg.billing.ports import Button, Notice, UiRef
from svbg.content.model import parse_action, parse_label, parse_text_blocks
from svbg.content.store import PLACEHOLDER_RE
from svbg.core.clock import now
from svbg.tg.ui.conditions import compile_condition
from svbg.tg.ui.context import UserCtx
from svbg.tg.ui.view import Redirect
from svbg.tg.user import seeds
from svbg.tg.user.account import device_fingerprint, device_name, qr_png
from svbg.tg.user.base import parse_ids
from svbg.tg.user.deeplink import parse_start_payload
from svbg.tg.user.messenger import UserMessenger, notice_button
from svbg.tg.user.render import plain_view
from svbg.tg.user.start import build_start_router
from svbg.tg.user.texts import fmt_left, plural_days, t
from tests.tg.user.kit import CHANNEL, DATE, build_user_env

_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")  # screens.code CHECK constraint
_GLOBAL = {"balance", "days_left"}


@pytest.mark.parametrize("seed", seeds.USER_SCREENS, ids=lambda s: s.code)
def test_seed_screens_are_valid_content(seed: Any) -> None:
    assert _CODE_RE.match(seed.code)
    blocks = parse_text_blocks({k: dict(v) for k, v in seed.body.items()})
    assert set(blocks) == {"ru"} and set(seed.title) == {"ru"}  # the bot speaks Russian only
    allowed = set(seeds.PLACEHOLDERS[seed.code]) | _GLOBAL
    for block in blocks.values():
        used = set(PLACEHOLDER_RE.findall(block.text))
        assert used <= allowed, (seed.code, used - allowed)
    for b in seed.buttons:
        assert set(b.label) == {"ru"}, (seed.code, b.system_key)
        parse_label(dict(b.label))
        parse_action(dict(b.action))
        if b.visible_if is not None:
            compile_condition(dict(b.visible_if))
        assert b.style in (None, "primary", "success", "danger")


def test_home_has_at_most_seven_buttons_for_any_state() -> None:
    home = seeds.SEEDS[seeds.HOME]
    for state in ("none", "trial", "active", "expired", "frozen"):
        for flags in (frozenset(), frozenset({"trial"})):
            user = UserCtx(1, sub_state=state, flags=flags)
            visible = [
                b for b in home.buttons if b.visible_if is None or compile_condition(dict(b.visible_if))(user)
            ]
            assert len(visible) + 1 <= 7  # + the support link


def test_helpers() -> None:
    assert parse_ids("12:30", 2) == [12, 30]
    assert parse_ids("12:-3", 2) is None and parse_ids(None, 1) is None and parse_ids("1:2", 1) is None
    assert plural_days(30) == "1 мес." and plural_days(360) == "1 год" and plural_days(7) == "7 дн."
    assert plural_days(90, "en") == "3 мес."  # an old language code is ignored
    assert fmt_left(3 * 86_400 + 5) == "4 дн." and fmt_left(5_400) == "2 ч" and fmt_left(10) == "1 мин"
    assert t("en", "btn_menu") == t("ru", "btn_menu") == t(None, "btn_menu") == "🏠 Меню"
    assert qr_png("https://sub.example/abc").startswith(b"\x89PNG")
    assert len(device_fingerprint("hw")) == 8
    assert (
        device_name({"model": "Pixel", "platform": "Android", "os_version": "14"}, "ru")
        == "Pixel · Android · 14"
    )
    assert device_name({}, "en") == "Устройство"


@pytest.mark.parametrize(
    ("payload", "kind", "value"),
    [
        ("s_buy", "screen", "buy"),
        ("p_standard", "plan", "standard"),
        ("pr_AUTUMN", "promo", "AUTUMN"),
        ("t_500", "topup", "500"),
        ("r_abc", "ref", "abc"),
        ("a_vk", "ad", "vk"),
        ("l_x1", "link", "x1"),
        ("refAbCd1234", "legacy_ref", "AbCd1234"),
        ("campaign2024", "legacy_code", "campaign2024"),
    ],
)
def test_deep_link_stub(payload: str, kind: str, value: str) -> None:
    link = parse_start_payload(payload)
    assert link is not None and (link.kind, link.value) == (kind, value)
    assert link.as_intent()["raw"] == payload


@pytest.mark.parametrize("payload", [None, "", "setup_abc", "t_x", "s_", "bad payload", "x" * 65, "a/b"])
def test_deep_link_stub_rejects(payload: str | None) -> None:
    assert parse_start_payload(payload) is None


def test_plain_view_falls_back_to_the_seed() -> None:
    user = UserCtx(1, lang="en", balance_minor=5_000)  # an old stored language changes nothing
    view = plain_view(user, None, seeds.HOME, {"status": "S", "name": "Аня"})
    assert view.text.startswith("👋 Привет, Аня!") and "S" in view.text
    assert view.entities and view.entities[0].type == "bold"
    labels = [b.text.replace("\xa0", " ") for row in view.keyboard or () for b in row]
    assert "💰 Баланс: 50 ₽" in labels and not any("Подписка" in label for label in labels)
    assert "👤 Профиль · {left}" in labels  # {left} is filled by the home route, not by the seed view
    assert not {"🛒 Купить подписку", "⚙️ Настройки", "🌐 Язык", "🎟 Промокод"} & set(labels)
    unknown = plain_view(user, None, "nope", fallback_text="fallback")
    assert unknown.text == "fallback"


async def test_screens_work_without_content_rows(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn, seed_content=False) as env:
        _uid, tg = await env.new_user()
        home = await env.open(tg)
        assert "Подписки пока нет." in home.text and "🎁 Попробовать бесплатно" in home.labels()
        section = await env.press(tg, "Профиль")
        assert section.text.startswith("👤 Профиль") and "Подписки пока нет" in section.text
        periods = await env.press(tg, "Купить")
        assert "Выберите срок" in periods.text


async def test_no_language_choice_anywhere(pg_dsn: str) -> None:
    """The bot is Russian-only: no «Язык» on home, an old stored «en» changes nothing, old language buttons
    and messages lead home."""
    async with build_user_env(pg_dsn) as env:
        uid, tg = await env.new_user()
        await env.db.raw("update users set language = 'en' where id = $1", uid)
        home = await env.open(tg)
        assert home.text.startswith("👋 Привет, Аня!") and "Подписки пока нет." in home.text
        assert not any("Язык" in label or "Language" in label for label in home.labels())
        old = await env.click(tg, "v1:lang:o")  # a button of an old message
        assert old.text.startswith("👋 Привет") and "👤 Профиль" in old.labels()
        stale = await env.click(tg, "v1:lang:set:en")  # the old «English» choice is gone
        assert "Hi" not in stale.text and "English" not in " ".join(stale.labels())
        assert (await env.rows("select language from users where id = $1", uid))[0]["language"] == "en"


async def test_trial_for_channel_members_goes_through_the_gate(pg_dsn: str) -> None:
    async with build_user_env(
        pg_dsn,
        config={
            "TRIAL_AUDIENCE": "channel_members",
            "REQUIRED_CHANNEL_ID": CHANNEL,
            "REQUIRED_CHANNEL_URL": "https://t.me/svbg_news",
        },
    ) as env:
        uid, tg = await env.new_user()
        env.lookup.member = False
        await env.open(tg)
        gate = await env.press(tg, "Попробовать")
        assert "Подпишитесь на наш канал" in gate.text
        assert gate.button("Перейти в канал").url == "https://t.me/svbg_news"
        await env.press(tg, "Я подписался")
        assert "Подписка на канал пока не видна" in (env.tg.toasts()[-1] or "")
        assert await env.rows("select id from subscriptions where user_id = $1", uid) == []
        env.lookup.member = True
        started = await env.press(tg, "Я подписался")
        assert "Пробный период на 3 дн. активирован" in started.text
        assert len(await env.rows("select id from subscriptions where user_id = $1", uid)) == 1


async def test_start_hook_gate_for_everyone_and_deep_link_intent(pg_dsn: str) -> None:
    async with build_user_env(
        pg_dsn, config={"REQUIRED_CHANNEL_ID": CHANNEL, "CHANNEL_REQUIRED_FOR": "all"}
    ) as env:
        uid, tg = await env.new_user()
        user = await env.ctx(tg)
        env.lookup.member = False
        picked = await env.path.on_start(user, tg, parse_start_payload("p_standard"))
        assert picked == (seeds.CHANNEL, None)
        state = await env.router.ui_state.get(uid)
        assert state.pending_intent == {
            "kind": "deeplink",
            "v": 1,
            "type": "plan",
            "value": "standard",
            "raw": "p_standard",
        }
        gate = await env.open(tg, seeds.CHANNEL)
        assert "🏠 Меню" not in gate.labels()  # the gate for everyone has no way around it
        env.lookup.member = True
        home = await env.press(tg, "Я подписался")
        assert "Привет" in home.text
        assert await env.path.on_start(user, tg, None) is None  # the fresh check refreshed the cache
        # the start router shows the picked screen
        await env.db.raw("delete from channel_members")
        env.lookup.member = False
        env.config["OWNER_IDS"] = [1]  # someone else owns the bot (staff skip the gate)
        router = build_start_router(
            screens=env.router, users=env.directory, hub=None, on_start=env.path.on_start
        )
        handler = router.message.handlers[0].callback
        msg = Message(
            message_id=1,
            date=DATE,
            chat=Chat(id=tg, type="private"),
            from_user=env.tg_user(tg),
            text="/start",
        )
        env.directory.invalidate()
        await handler(msg, command=CommandObject(prefix="/", command="start", args="s_buy"))
        assert "Подпишитесь на наш канал" in env.tg.last(tg).text


async def test_cancel_and_i_paid(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn) as env:
        uid, tg = await env.new_user()
        await env.open(tg)
        await env.press(tg, "Профиль")
        await env.press(tg, "Купить подписку")
        await env.press(tg, "1 мес.")
        invoice = await env.press(tg, "СБП")
        payment_id = (await env.rows("select id from payments where user_id = $1", uid))[0]["id"]
        await env.click(tg, invoice.data("Я оплатил"))
        assert "Оплата ещё не поступила" in (env.tg.toasts()[-1] or "")

        async def paid(pid: str, user_id: int) -> Any:
            assert (pid, user_id) == (payment_id, uid)
            return type("R", (), {"status": "paid"})()

        env.path.deps.i_paid = paid
        await env.click(tg, invoice.data("Я оплатил"))
        assert env.tg.toasts()[-1] == "✅ Оплата получена"
        short = await env.press(tg, "Другой способ")
        assert "Не хватает 179 ₽" in short.text
        home = await env.press(tg, "Отменить покупку")
        assert "Покупка отменена" in (env.tg.toasts()[-1] or "") and "Привет" in home.text
        orders = await env.rows("select status from orders where user_id = $1 and kind = 'new'", uid)
        assert orders == [{"status": "canceled"}]


async def test_messenger_edit_send_and_content_override(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn) as env:
        _uid, tg = await env.new_user()
        shown = await env.open(tg)
        m: UserMessenger = env.path.messenger
        notice = Notice(
            "billing.paid",
            "✅ Оплачено!",
            ((Button("🔗 Подключиться", web_app="https://sub.example/x"),), (Button("Меню", action="menu"),)),
            {"title": "Тариф 1"},
        )
        ref = UiRef(tg, shown.message_id, now())
        assert await m.edit(ref, notice)
        edited = env.tg.shown(tg, shown.message_id)
        assert (
            edited.text == "✅ Оплачено!"
            and edited.button("Подключиться").web_app.url == "https://sub.example/x"
        )
        assert edited.data("Меню") == "v1:home:o"
        assert not await m.edit(UiRef(tg, 999_999, now()), notice)  # gone → billing sends a new one
        sent = await m.send(tg, notice)
        assert sent is not None and env.tg.shown(tg, sent.message_id).text == "✅ Оплачено!"
        env.tg.blocked.add(tg)
        assert await m.send(tg, notice) is None


async def test_messenger_not_modified_counts_as_success() -> None:
    async def call(method: Any, chat_id: int) -> Any:
        raise TelegramBadRequest(
            method=EditMessageText(text="x"), message="Bad Request: message is not modified"
        )

    m = UserMessenger(call)
    assert await m.edit(UiRef(1, 2, now()), Notice("billing.paid", "x"))


def test_notice_buttons_map_to_user_callbacks() -> None:
    assert notice_button(Button("Купить", action="reorder", params={"order_id": 7})).callback_data == (
        "v1:buy:reorder:7"
    )
    assert notice_button(Button("Пополнить", action="topup", params={"order_id": 7})).callback_data == (
        "v1:pay_short:o:7"
    )
    assert notice_button(Button("Меню", action="menu")).callback_data == "v1:home:o"
    assert notice_button(Button("x", action="unknown")) is None
    assert notice_button(Button("x", action="reorder")) is None  # no order id
    assert notice_button(Button("Сайт", url="https://example.com")).url == "https://example.com"
    assert notice_button(Button("Страница", web_app="http://plain.example")).url == "http://plain.example"


async def test_after_onboarding_resumes_after_channel(pg_dsn: str) -> None:
    """Decision C9: after «Я подписался» the app's hook (consent page / kept deep-link intent) decides the
    next screen; a hook that says nothing or fails falls back to home."""
    async with build_user_env(
        pg_dsn, config={"REQUIRED_CHANNEL_ID": CHANNEL, "CHANNEL_REQUIRED_FOR": "all"}
    ) as env:
        _uid, tg = await env.new_user()
        calls: list[int] = []

        async def resume(ctx: Any) -> Any:
            calls.append(ctx.user.telegram_id)
            return Redirect(seeds.PROFILE) if len(calls) == 1 else None

        env.path.home.after_onboarding = resume
        env.lookup.member = False
        await env.open(tg, seeds.CHANNEL)
        env.lookup.member = True
        resumed = await env.press(tg, "Я подписался")
        assert calls == [tg] and resumed.text.startswith("👤 Профиль")  # the hook's redirect
        await env.open(tg, seeds.CHANNEL)
        home = await env.press(tg, "Я подписался")
        assert calls == [tg, tg] and "Подписки пока нет." in home.text  # hook said None → home

        async def broken(_ctx: Any) -> Any:
            raise RuntimeError("boom")

        env.path.home.after_onboarding = broken
        await env.open(tg, seeds.CHANNEL)
        home = await env.press(tg, "Я подписался")
        assert "Подписки пока нет." in home.text
