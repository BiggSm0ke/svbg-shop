"""Entry captcha (``svbg.tg.user.captcha``) through the real screen router and the ``/start`` router: new
users only, the deep link kept before it and resumed after it, wrong taps, stale and double taps, the pause
after five misses, the gate on every button, staff / old users / the switch skip it."""

from __future__ import annotations

import random
from typing import Any

import pytest
from aiogram.filters import CommandObject
from aiogram.types import Chat, Message

from svbg.core.settings import values
from svbg.core.settings.registry import CAPTCHA_EMOJIS_DEFAULT, full_registry
from svbg.tg.ui.view import Redirect
from svbg.tg.user import seeds
from svbg.tg.user.captcha import COOLDOWN_S, MAX_MISSES, _parse_tap, captcha_emojis
from svbg.tg.user.start import build_start_router
from tests.tg.user.kit import CHANNEL, DATE, Shown, UserEnv, build_user_env

ON: dict[str, Any] = {"CAPTCHA_ENABLED": True, "OWNER_IDS": [1]}  # someone else owns the bot


async def start(env: UserEnv, tg: int, args: str | None = None) -> Shown:
    router = build_start_router(screens=env.router, users=env.directory, hub=None, on_start=env.path.on_start)
    handler = router.message.handlers[0].callback
    msg = Message(
        message_id=1,
        date=DATE,
        chat=Chat(id=tg, type="private"),
        from_user=env.tg_user(tg),
        text="/start" if args is None else f"/start {args}",
    )
    await handler(msg, command=CommandObject(prefix="/", command="start", args=args))
    return env.tg.last(tg)


def tap_data(shown: Shown, emoji: str) -> str:
    for b in shown.buttons():
        if b.text == emoji:
            return str(b.callback_data)
    raise AssertionError(f"no button {emoji!r} in {shown.labels()}")


def target_of(env: UserEnv, uid: int) -> str:
    challenge = env.path.captcha.current(uid)
    assert challenge is not None
    return challenge.target


def wrong_of(env: UserEnv, uid: int) -> str:
    return next(e for e in CAPTCHA_EMOJIS_DEFAULT if e != target_of(env, uid))


async def passed_at(env: UserEnv, uid: int) -> Any:
    return (await env.rows("select captcha_passed_at from users where id = $1", uid))[0]["captcha_passed_at"]


# ------------------------------------------------------------------------------------------------ pure parts


def test_emoji_list_is_cleaned_and_falls_back_to_the_default() -> None:
    assert captcha_emojis(None) == CAPTCHA_EMOJIS_DEFAULT
    assert captcha_emojis(["🍎"]) == CAPTCHA_EMOJIS_DEFAULT  # one emoji is no question
    assert captcha_emojis(["🍎", " 🍎 ", "", "🍌"]) == ("🍎", "🍌")
    assert captcha_emojis("🍎, 🍌,🍓") == ("🍎", "🍌", "🍓")
    assert len(captcha_emojis([chr(0x1F400 + i) for i in range(20)])) == 12


def test_tap_argument() -> None:
    assert _parse_tap("0a1b2c3d.4") == ("0a1b2c3d", 4)
    assert _parse_tap("0a1b2c3d.x") == ("0a1b2c3d", None)
    assert _parse_tap("0a1b2c3d.²") == ("0a1b2c3d", None)
    assert _parse_tap("0a1b2c3d") == ("0a1b2c3d", None)
    assert _parse_tap(".1") == (None, None)
    assert _parse_tap(None) == (None, None)


def test_settings_are_declared() -> None:
    reg = full_registry()
    enabled, emojis = reg.get("CAPTCHA_ENABLED"), reg.get("CAPTCHA_EMOJIS")
    assert enabled.default is True and enabled.section == "sales"
    assert tuple(emojis.default) == CAPTCHA_EMOJIS_DEFAULT and emojis.section == "sales"
    assert values.parse(emojis, "🍎, 🍌") == ["🍎", "🍌"]
    for bad in ("🍎", "🍎, 🍎", "🍎, яблоко"):
        with pytest.raises(values.SettingValueError):
            values.parse(emojis, bad)


# ------------------------------------------------------------------------------------------------ flows


async def test_new_user_start_with_a_deep_link_then_wrong_then_right(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn, config=ON) as env:
        uid, tg = await env.new_user()
        resumed: list[Any] = []

        async def resume(ctx: Any) -> Any:
            intent = await env.router.ui_state.pop_pending_intent(ctx.user.user_id)
            resumed.append(intent)
            return None if intent is None else Redirect(seeds.BUY, toast=f"link {intent['raw']}")

        env.path.home.after_onboarding = resume
        shown = await start(env, tg, "p_standard")
        # the deep link is kept before the captcha
        state = await env.router.ui_state.get(uid)
        assert state.pending_intent is not None and state.pending_intent["raw"] == "p_standard"
        first = env.path.captcha.current(uid)
        assert first is not None
        assert shown.text == f"Проверим, что вы не бот\n\nНажмите на {first.target}"
        assert [len(row) for row in shown.markup.inline_keyboard] == [3, 3]  # type: ignore[union-attr]
        assert sorted(shown.labels()) == sorted(CAPTCHA_EMOJIS_DEFAULT)
        assert first.target not in tap_data(shown, first.target)  # the button carries a position only

        # a wrong tap: a toast and a new challenge in the same message
        old_data = tap_data(shown, wrong_of(env, uid))
        again = await env.click(tg, old_data)
        second = env.path.captcha.current(uid)
        assert second is not None and env.tg.toasts()[-1] == "Не то. Попробуйте ещё раз"
        assert again.message_id == shown.message_id
        assert second.nonce != first.nonce and second.target != first.target
        assert second.options != first.options
        assert again.text.endswith(f"Нажмите на {second.target}")
        assert await passed_at(env, uid) is None and resumed == []

        # the old button again (a double tap): the current challenge stays, nothing counted
        await env.click(tg, old_data)
        assert env.path.captcha.current(uid) == second and second.misses == 1

        # the right one: passed, and the kept link goes on
        done = await env.click(tg, tap_data(env.tg.last(tg), second.target))
        assert await passed_at(env, uid) is not None
        assert resumed[0]["raw"] == "p_standard" and env.tg.toasts()[-1] == "link p_standard"
        assert "Выберите срок" in done.text and done.message_id == shown.message_id
        assert env.path.captcha.current(uid) is None
        # a second tap of the right button changes nothing
        await env.click(tg, tap_data(again, second.target))
        assert env.tg.toasts()[-1] == "Проверка уже пройдена"
        assert "Выберите срок" in env.tg.last(tg).text
        # from now on /start goes straight to the menu
        home = await start(env, tg)
        assert "Привет" in home.text


async def test_any_button_of_a_new_user_opens_the_captcha(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn, config=ON) as env:
        uid, tg = await env.new_user()
        await start(env, tg)
        target = target_of(env, uid)
        for data in ("v1:bal:o", "v1:sys:trial", "v1:home:o", "garbage", "v1:captcha:nope"):
            shown = await env.click(tg, data)  # e.g. a broadcast button
            assert shown.text.endswith(f"Нажмите на {target}"), data
        assert env.path.captcha.current(uid).misses == 0  # type: ignore[union-attr]
        assert await env.rows("select id from subscriptions where user_id = $1", uid) == []
        # a captcha that was never shown (the bot restarted): a fresh one, no miss counted
        env.path.captcha._challenges.clear()
        shown = await env.click(tg, "v1:captcha:tap:deadbeef.0")
        assert env.path.captcha.current(uid) is not None
        assert shown.text.endswith(f"Нажмите на {target_of(env, uid)}")
        await env.click(tg, tap_data(shown, target_of(env, uid)))
        balance = await env.click(tg, "v1:bal:o")
        assert "Баланс" in balance.text


async def test_old_users_staff_and_the_switch_skip_the_captcha(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn, config=ON) as env:
        old_uid, old_tg = await env.new_user()
        await env.db.raw("update users set captcha_passed_at = now() where id = $1", old_uid)
        assert "Привет" in (await start(env, old_tg)).text
        assert "Баланс" in (await env.click(old_tg, "v1:bal:o")).text

        staff_uid, staff_tg = await env.new_user()
        await env.db.raw("update users set role = 'support' where id = $1", staff_uid)
        env.directory.invalidate()
        assert "Привет" in (await start(env, staff_tg)).text
        assert "Баланс" in (await env.click(staff_tg, "v1:bal:o")).text

        uid, tg = await env.new_user()
        env.config["CAPTCHA_ENABLED"] = False  # hot: no restart
        assert "Привет" in (await start(env, tg)).text
        assert "Баланс" in (await env.click(tg, "v1:bal:o")).text
        assert await passed_at(env, uid) is None  # not marked: it was never solved
        env.config["CAPTCHA_ENABLED"] = True
        assert "Нажмите на" in (await env.click(tg, "v1:bal:o")).text
        # switched off while the captcha is on the screen: the next tap just lets the user in
        env.config["CAPTCHA_ENABLED"] = False
        shown = env.tg.last(tg)
        assert "Привет" in (await env.click(tg, tap_data(shown, shown.labels()[0]))).text
        assert env.path.captcha.current(old_uid) is None and env.path.captcha.current(staff_uid) is None


async def test_pause_after_five_misses_in_a_row(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn, config=ON) as env:
        clock = [1000.0]
        env.path.captcha._clock = lambda: clock[0]
        env.path.captcha._rng = random.Random(7)
        uid, tg = await env.new_user()
        await start(env, tg)
        for n in range(1, MAX_MISSES + 1):
            await env.click(tg, tap_data(env.tg.last(tg), wrong_of(env, uid)))
            alert = env.tg.answers[-1]
            if n < MAX_MISSES:
                assert alert.text == "Не то. Попробуйте ещё раз" and not alert.show_alert
        assert env.tg.answers[-1].text == "Слишком много ошибок подряд. Подождите минуту и попробуйте снова"
        assert env.tg.answers[-1].show_alert
        target = target_of(env, uid)
        before = env.tg.last(tg).text
        clock[0] += 15
        await env.click(tg, tap_data(env.tg.last(tg), target))  # even the right one waits
        assert env.tg.toasts()[-1] == f"Подождите ещё {int(COOLDOWN_S) - 15} сек."
        assert await passed_at(env, uid) is None and env.tg.last(tg).text == before
        clock[0] += COOLDOWN_S
        await env.click(tg, tap_data(env.tg.last(tg), wrong_of(env, uid)))
        assert env.tg.toasts()[-1] == "Не то. Попробуйте ещё раз"  # the count started over
        await env.click(tg, tap_data(env.tg.last(tg), target_of(env, uid)))
        assert await passed_at(env, uid) is not None
        assert "Привет" in env.tg.last(tg).text


async def test_channel_gate_and_own_emojis_after_the_captcha(pg_dsn: str) -> None:
    config = {**ON, "REQUIRED_CHANNEL_ID": CHANNEL, "CHANNEL_REQUIRED_FOR": "all"}
    async with build_user_env(pg_dsn, config=config) as env:
        env.config["CAPTCHA_EMOJIS"] = ["🐱", "🐶"]
        env.lookup.member = False
        uid, tg = await env.new_user(first_name="Ann")
        await env.db.raw("update users set language = 'en' where id = $1", uid)  # old data: ignored
        shown = await start(env, tg)
        assert sorted(shown.labels()) == ["🐱", "🐶"] and len(shown.markup.inline_keyboard) == 1  # type: ignore[union-attr]
        assert shown.text == f"Проверим, что вы не бот\n\nНажмите на {target_of(env, uid)}"
        await env.click(tg, tap_data(shown, wrong_of_two(env, uid)))
        assert env.tg.toasts()[-1] == "Не то. Попробуйте ещё раз"
        gate = await env.click(tg, tap_data(env.tg.last(tg), target_of(env, uid)))
        assert "Подпишитесь на наш канал" in gate.text  # what /start shows next


def wrong_of_two(env: UserEnv, uid: int) -> str:
    return "🐶" if target_of(env, uid) == "🐱" else "🐱"


async def test_owner_text_without_the_placeholder_still_names_the_emoji(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn, config=ON) as env:
        entry = env.content.get_screen(seeds.CAPTCHA)
        assert entry is not None and entry.screen.kind == "system"
        await env.db.raw(
            "update screens set body = jsonb_build_object('ru', jsonb_build_object('text', 'Вы человек?'))"
            " where code = $1",
            seeds.CAPTCHA,
        )
        await env.content.load()
        uid, tg = await env.new_user()
        shown = await start(env, tg)
        assert shown.text == f"Вы человек?\n\n{target_of(env, uid)}"


async def test_pass_hooks_run_once_and_never_block_the_user(pg_dsn: str) -> None:
    """The app hangs the «new user» post and the waiting referral welcome on the pass: once per user, and a
    broken hook still lets the user in."""
    async with build_user_env(pg_dsn, config=ON) as env:
        passed: list[int] = []

        async def broken(_user: Any) -> None:
            raise RuntimeError("boom")

        env.path.captcha.on_passed += [broken, lambda user: passed.append(user.user_id)]
        uid, tg = await env.new_user()
        shown = await start(env, tg)
        right = tap_data(shown, target_of(env, uid))
        await env.click(tg, tap_data(shown, wrong_of(env, uid)))
        assert passed == []  # a wrong tap is not a pass
        done = await env.click(tg, tap_data(env.tg.last(tg), target_of(env, uid)))
        assert passed == [uid] and "Привет" in done.text
        await env.click(tg, right)  # an old button of the first question: nothing runs again
        assert passed == [uid]
        # stored by someone else meanwhile (a second process, an import): no second run either
        uid2, tg2 = await env.new_user()
        shown = await start(env, tg2)
        await env.db.raw("update users set captcha_passed_at = now() where id = $1", uid2)
        await env.click(tg2, tap_data(shown, target_of(env, uid2)))
        assert passed == [uid]
