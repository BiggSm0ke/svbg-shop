"""Error isolation end to end (07 §5 stage 0): an exception in a screen handler gives the user the fallback
screen, the owner a report in DM, and the bot keeps answering.

Chaos: 1000 callback updates from 250 users, 10 % of them raise → every update is answered, nothing hangs.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from svbg.app import App
from svbg.tg.ui.codec import encode
from svbg.tg.ui.view import View
from tests.e2e.conftest import OWNER_ID, AppEnv, StartApp
from tests.fakes.telegram import Call

pytestmark = pytest.mark.pg

USERS = 250
ROUNDS = 4  # 250 × 4 = 1000 updates
#: Bot API calls that draw a screen. Content screens show the default banner (a photo with the text as its
#: caption), code screens are text: a screen is a text, a caption, or a replaced picture with a caption.
SCREEN_METHODS = ("sendMessage", "sendPhoto", "editMessageText", "editMessageCaption", "editMessageMedia")


class ChaosMonkey(RuntimeError):
    pass


def install_chaos_screen(app: App) -> None:
    assert app.screens is not None

    @app.screens.screen("chaos")
    async def chaos(_ctx: Any, arg: Any) -> View:
        if arg == "fail":
            raise ChaosMonkey("screen handler exploded on purpose")
        return View(text=f"chaos ok {arg}")


def _screen_text(call: Call) -> str:
    media = call.params.get("media")
    caption = media.get("caption") if isinstance(media, dict) else None
    return str(call.params.get("text") or call.params.get("caption") or caption or "")


async def _wait_screen(tg: Any, chat_id: int, start: int, timeout: float = 10.0) -> Call:
    """The first screen drawn in ``chat_id`` after call ``start`` (whatever the Bot API method)."""
    deadline = time.monotonic() + timeout
    while True:
        for c in tg.calls[start:]:
            if c.method in SCREEN_METHODS and c.ok and c.params.get("chat_id") == chat_id:
                return c
        assert time.monotonic() < deadline, f"no screen in chat {chat_id} within {timeout}s"
        await asyncio.sleep(0.03)


def _answered(tg: Any, start: int) -> dict[str, Call]:
    return {
        str(c.params["callback_query_id"]): c for c in tg.calls[start:] if c.method == "answerCallbackQuery"
    }


async def _open_main_messages(app_env: AppEnv, users: list[int]) -> dict[int, int]:
    """/start for every user; returns user → main message id."""
    tg = app_env.tg
    start = len(tg.calls)
    for uid in users:
        tg.push_message(uid, "/start")
    pending = set(users)
    deadline = time.monotonic() + 60
    main: dict[int, int] = {}
    while pending:
        assert time.monotonic() < deadline, f"/start not answered for {len(pending)} users"
        for call in tg.calls[start:]:
            sent = call.method in ("sendMessage", "sendPhoto") and call.ok  # home shows the banner: a photo
            if sent and call.params.get("chat_id") in pending:
                uid = call.params["chat_id"]
                main[uid] = call.result["message_id"]
                pending.discard(uid)
        await asyncio.sleep(0.05)
    return main


async def test_exception_in_screen_shows_fallback_and_reports_to_owner(
    start_app: StartApp, app_env: AppEnv
) -> None:
    tg = app_env.tg
    app = await start_app(setup_hooks=[install_chaos_screen])
    user = 5005
    main = await _open_main_messages(app_env, [user])

    start = len(tg.calls)
    tg.push_callback(user, encode("chaos", arg="fail"), main[user])
    # the fallback screen is drawn without media (the fewest moving parts): after the home picture (the
    # default banner) it comes as a new text message and the picture is deleted
    edit = await _wait_screen(tg, user, start)
    assert edit.method == "sendMessage"
    assert "Что-то пошло не так" in _screen_text(edit)
    main[user] = edit.result["message_id"]
    buttons = [b for row in edit.params["reply_markup"]["inline_keyboard"] for b in row]
    assert any("Меню" in b["text"] for b in buttons), "the fallback screen leads back to the menu"
    answer = await tg.wait_for("answerCallbackQuery", lambda c: True, timeout=10, start=start)
    assert answer.ok

    report = await tg.wait_for(
        "sendMessage",
        lambda c: c.params.get("chat_id") == OWNER_ID and c.params.get("parse_mode") == "HTML",
        timeout=15,
    )
    text = report.params["text"]
    assert "ChaosMonkey" in text
    assert "<blockquote expandable>" in text
    assert app_env.token.split(":")[1] not in text  # no secrets in the report

    # The bot keeps answering after the error.
    start = len(tg.calls)
    tg.push_callback(user, encode("chaos", arg="ok"), main[user])
    ok = await _wait_screen(tg, user, start)
    assert ok.method == "editMessageText" and _screen_text(ok) == "chaos ok ok"
    assert app.runner is not None


async def test_chaos_1000_updates_10_percent_failing(start_app: StartApp, app_env: AppEnv) -> None:
    tg = app_env.tg
    app = await start_app(setup_hooks=[install_chaos_screen])
    users = [100_000 + i for i in range(USERS)]
    main = await _open_main_messages(app_env, users)

    began = time.monotonic()
    start = len(tg.calls)
    expected: dict[str, bool] = {}  # callback id → should fail
    n = 0
    for _round in range(ROUNDS):
        round_ids: list[str] = []
        for uid in users:
            n += 1
            fail = n % 10 == 0
            update = tg.push_callback(uid, encode("chaos", arg="fail" if fail else f"r{n}"), main[uid])
            cid = update["callback_query"]["id"]
            expected[cid] = fail
            round_ids.append(cid)
        # Users click again only after their previous click was answered (like real people).
        deadline = time.monotonic() + 60
        while not set(round_ids) <= set(_answered(tg, start)):
            assert time.monotonic() < deadline, "callbacks hang: not answered within 60 s"
            await asyncio.sleep(0.05)
    elapsed = time.monotonic() - began

    answered = _answered(tg, start)
    assert len(expected) == 1000
    assert set(expected) <= set(answered), "every update got an answer"
    assert all(answered[cid].ok for cid in expected)

    # Every handler finished: nothing is stuck in the runner.
    for _ in range(100):
        health = await app.components.health("bot")
        if health.details.get("handlers_in_flight") == 0:
            break
        await asyncio.sleep(0.05)
    assert health.details.get("handlers_in_flight") == 0

    failures = sum(expected.values())
    fallback_screens = [
        c
        for c in tg.calls[start:]
        if c.method in SCREEN_METHODS
        and c.params.get("chat_id") in main
        and "Что-то пошло не так" in _screen_text(c)
    ]
    # Consecutive failures of the same user may hit an identical fallback screen ("message is not modified").
    assert len(fallback_screens) >= failures * 0.9
    assert failures == 100

    # The owner gets ONE report for the 100 identical errors (grouped by fingerprint), not a flood.
    assert app.hub is not None
    await app.hub.drain(5)
    reports = [
        c
        for c in tg.calls
        if c.method == "sendMessage"
        and c.params.get("chat_id") == OWNER_ID
        and "ChaosMonkey" in c.params["text"]
    ]
    assert len(reports) == 1
    groups = await app.hub.open_groups()
    chaos = [g for g in groups if g.exc_type.endswith("ChaosMonkey")]
    assert chaos and chaos[0].count >= failures

    # Still alive afterwards.
    after = len(tg.calls)
    tg.push_message(users[0], "/start")
    await tg.wait_for("sendPhoto", lambda c: c.params.get("chat_id") == users[0], timeout=10, start=after)
    print(f"chaos: 1000 updates in {elapsed:.1f}s ({1000 / elapsed:.0f}/s)")
