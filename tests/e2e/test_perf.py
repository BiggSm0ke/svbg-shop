"""Performance smoke (07 §2.6): p95 of click processing ≤ 60 ms with 10 000 users in the database,
clicks arriving at ~50 updates/s.

Measured inside the process: an outer dispatcher middleware times each callback update; the time spent
waiting for the (fake) Telegram Bot API is measured by a session middleware and subtracted — what remains is
our own work (user lookup, permissions, rendering, SQL), i.e. the latency without network round trips.
The user cache is cold for every first click (as right after a restart).

Every screen shows the default banner (a picture uploaded once, then sent by ``file_id``), so a click between
two screens edits only the caption — the test screen ``perf`` carries the banner too, like a content screen.
"""

from __future__ import annotations

import asyncio
import contextvars
import random
import statistics
import time
from collections.abc import Awaitable, Callable
from typing import Any

import asyncpg
import pytest
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.client.telegram import TelegramAPIServer
from aiogram.types import TelegramObject

from svbg.app import App
from svbg.tg.runner import BotConfig
from svbg.tg.ui.codec import encode
from svbg.tg.ui.view import View
from tests.e2e.conftest import AppEnv, StartApp, counting_db

pytestmark = [pytest.mark.pg, pytest.mark.slow]

SEEDED_USERS = 10_000
CLICKS = 400
RATE = 50.0  # updates per second
P95_BUDGET_MS = 60.0

_tg_time: contextvars.ContextVar[list[float] | None] = contextvars.ContextVar("tg_time", default=None)


class _TelegramTimer(BaseRequestMiddleware):
    async def __call__(self, make_request: Any, bot: Bot, method: Any) -> Any:
        began = time.perf_counter()
        try:
            return await make_request(bot, method)
        finally:
            acc = _tg_time.get()
            if acc is not None:
                acc[0] += time.perf_counter() - began


def _timed_bot_factory(cfg: BotConfig) -> Bot:
    assert cfg.api_url is not None
    session = AiohttpSession(api=TelegramAPIServer.from_base(cfg.api_url))
    session.middleware(_TelegramTimer())
    return Bot(cfg.token, session=session)


class Samples:
    def __init__(self) -> None:
        self.total: list[float] = []
        self.own: list[float] = []

    async def middleware(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        acc = [0.0]
        token = _tg_time.set(acc)
        began = time.perf_counter()
        try:
            return await handler(event, data)
        finally:
            total = time.perf_counter() - began
            _tg_time.reset(token)
            self.total.append(total * 1000)
            self.own.append(max(total - acc[0], 0.0) * 1000)


def _uploads_a_file(call: Any) -> bool:
    """The call sends a picture's bytes (``attach://…``), not a cached ``file_id``."""
    media = call.params.get("media")
    inner = media.get("media") if isinstance(media, dict) else None
    return any(str(v or "").startswith("attach://") for v in (call.params.get("photo"), inner))


def _p95(values: list[float]) -> float:
    return statistics.quantiles(values, n=100, method="inclusive")[94]


async def test_click_p95_with_10k_users(start_app: StartApp, app_env: AppEnv) -> None:
    conn = await asyncpg.connect(app_env.dsn)
    try:
        await conn.execute(
            "insert into users (telegram_id, username, first_name, created_at) "
            "select 900000000 + g, 'user' || g, 'User ' || g, now() - (g || ' minutes')::interval "
            "from generate_series(1, $1::int) g",
            SEEDED_USERS,
        )
        await conn.execute("analyze users")
    finally:
        await conn.close()

    samples = Samples()

    def hook(app: App) -> None:
        assert app.screens is not None and app.dispatcher is not None

        @app.screens.screen("perf")
        async def perf(ctx: Any, arg: Any) -> View:
            home = ctx.content_view("home")  # the default banner, as on every content screen
            return View(text=f"Экран {arg}", media=home.media if home is not None else None)

        app.dispatcher.callback_query.outer_middleware(samples.middleware)

    # Background pollers (job claims, the panel webhook inbox) are not part of a click: slowed down so
    # the SQL counter measures the clicks only (stage 2 made the home card cost one more SQL on a cold click).
    app = await start_app(
        setup_hooks=[hook],
        runner_kwargs={"bot_factory": _timed_bot_factory},
        jobs_poll_interval=600,
        inbox_poll_interval=600,
    )
    tg = app_env.tg
    rng = random.Random(20261001)
    clickers = rng.sample(range(1, SEEDED_USERS + 1), 300)
    telegram_ids = [900_000_000 + i for i in clickers]

    # Each user first gets a main message (/start), then the caches are dropped: first clicks are cold.
    start = len(tg.calls)
    for uid in telegram_ids:
        tg.push_message(uid, "/start")
    main: dict[int, int] = {}
    deadline = time.monotonic() + 60
    while len(main) < len(telegram_ids):
        assert time.monotonic() < deadline
        for call in tg.calls[start:]:
            sent = call.method in ("sendMessage", "sendPhoto") and call.ok  # home shows the banner: a photo
            if sent and call.params.get("chat_id") in telegram_ids:
                main[call.params["chat_id"]] = call.result["message_id"]
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.3)  # let the last ui_state writes finish
    assert app.users is not None and app.screens is not None
    app.users.invalidate()
    db = counting_db(app.db)
    ids = await db.raw("select id from users where telegram_id = any($1::bigint[])", telegram_ids)
    assert len(ids) == len(telegram_ids)
    for row in ids:
        app.screens.ui_state.forget(int(row["id"]))
    # Periodic background work (billing sweep, health sync, the payments reconciler's 15 s scan) would land
    # in the counter at random and make SQL/click flaky (2.01): stop it for the measured window.
    assert app.scheduler is not None
    await app.scheduler.stop()
    for task in asyncio.all_tasks():
        if task.get_name() == "payments-poll":
            task.cancel()
    queries_before = db.queries

    start = len(tg.calls)
    pushed: list[str] = []
    interval = 1.0 / RATE
    next_at = time.perf_counter()
    for n in range(CLICKS):
        uid = telegram_ids[n % len(telegram_ids)]
        data = encode("perf", arg=str(n)) if n % 2 == 0 else encode("home")
        update = tg.push_callback(uid, data, main[uid])
        pushed.append(update["callback_query"]["id"])
        next_at += interval
        await asyncio.sleep(max(0.0, next_at - time.perf_counter()))

    deadline = time.monotonic() + 30
    while True:
        answered = {
            c.params["callback_query_id"] for c in tg.calls[start:] if c.method == "answerCallbackQuery"
        }
        if set(pushed) <= answered and len(samples.own) >= CLICKS:
            break
        assert time.monotonic() < deadline, "clicks were not answered in time"
        await asyncio.sleep(0.05)

    own_p50, own_p95 = statistics.median(samples.own), _p95(samples.own)
    total_p50, total_p95 = statistics.median(samples.total), _p95(samples.total)
    sql_per_click = round((db.queries - queries_before) / CLICKS, 2)
    # the banner went up once (at the first /start) and is reused by file_id: no upload during the clicks
    assert not [c for c in tg.calls[start:] if _uploads_a_file(c)]
    print(
        f"\nperf: {CLICKS} clicks @ {RATE:.0f}/s, {SEEDED_USERS} users: "
        f"own p50={own_p50:.1f} ms p95={own_p95:.1f} ms; with fake-Telegram RTT p50={total_p50:.1f} ms "
        f"p95={total_p95:.1f} ms; SQL/click={sql_per_click}"
    )
    assert own_p95 <= P95_BUDGET_MS
    assert sql_per_click <= 2.0  # 07 §2: at most 2 SQL per click on the hot path
