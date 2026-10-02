"""BotRunner against the fake Bot API: polling, hot token/proxy/API swap, polling <-> webhook."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator, Mapping
from typing import Any

import pytest
from aiogram import Dispatcher, F, Router
from aiogram.types import Message, User
from aiohttp import ClientSession

from svbg.core.component import Component, Health, ProbeError
from svbg.tg.runner import SECRET_HEADER, BotConfig, BotRunner, BotUnavailableError, webhook_path_secret
from svbg.web import WebServer, build_web_app
from tests.fakes.telegram import FakeTelegram


class FakeSettings:
    def __init__(self, **cfg: Any) -> None:
        self.cfg: dict[str, Any] = cfg

    def current(self) -> Mapping[str, Any]:
        return dict(self.cfg)


class FakeHub:
    def __init__(self) -> None:
        self.captured: list[tuple[str, BaseException, str]] = []

    async def capture(self, exc: BaseException, place: str, *, handled: str = "", **kw: Any) -> str | None:
        self.captured.append((place, exc, handled))
        return "fp"


def make_dispatcher(events: list[str]) -> Dispatcher:
    dp = Dispatcher()
    router = Router()

    @router.message(F.text == "boom")
    async def boom(message: Message) -> None:
        raise RuntimeError("handler exploded")

    @router.message()
    async def echo(message: Message) -> None:
        await message.answer(f"echo:{message.text}")

    dp.include_router(router)

    @dp.startup()
    async def on_startup() -> None:
        events.append("startup")

    @dp.shutdown()
    async def on_shutdown() -> None:
        events.append("shutdown")

    return dp


@pytest.fixture
async def tg() -> AsyncIterator[FakeTelegram]:
    async with FakeTelegram(webhook_retry_delay=0.05) as fake:
        yield fake


class Env:
    def __init__(self, tg: FakeTelegram) -> None:
        self.tg = tg
        self.token = tg.add_bot(username="svbg_bot")
        self.events: list[str] = []
        self.hub = FakeHub()
        self.settings = FakeSettings(BOT_TOKEN=self.token, TELEGRAM_API_URL=tg.url, BOT_MODE="polling")
        self.dp = make_dispatcher(self.events)
        self.runner = BotRunner(
            self.settings,
            self.dp,
            self.hub,
            polling_timeout=2,
            request_timeout=5,
            drain_timeout=2,
            session_grace=0.1,
            allow_http_webhook=True,
        )
        self.web = WebServer("127.0.0.1", 0, build_web_app(self.runner.web_routes()))

    def cfg(self, **over: Any) -> dict[str, Any]:
        return {**self.settings.cfg, **over}

    async def echo(
        self, text: str, *, user_id: int = 10, bot_id: int | None = None, timeout: float = 5
    ) -> Any:
        start = len(self.tg.calls)
        self.tg.push_message(user_id, text, bot_id=bot_id)
        return await self.tg.wait_for(
            "sendMessage",
            lambda c: c.params.get("text") == f"echo:{text}",
            timeout,
            start=start,
            ok_only=True,
        )


@pytest.fixture
async def env(tg: FakeTelegram) -> AsyncIterator[Env]:
    e = Env(tg)
    await e.web.start()
    try:
        yield e
    finally:
        await e.runner.stop()
        await e.web.stop()


def webhook_cfg(env: Env, **over: Any) -> dict[str, Any]:
    base = {"BOT_MODE": "webhook", "PUBLIC_URL": env.web.url, "WEBHOOK_SECRET": "s3cret_" + "x" * 20}
    return env.cfg(**{**base, **over})


# ---------------------------------------------------------------------------------------------- basics


async def test_is_a_component(env: Env) -> None:
    assert isinstance(env.runner, Component)
    assert env.runner.name == "bot"


async def test_polling_receives_updates(env: Env) -> None:
    await env.runner.start()
    assert env.runner.mode == "polling"
    call = await env.echo("/start")
    assert call.token == env.token
    assert call.params["chat_id"] == 10
    report = await env.runner.health()
    assert report.status is Health.OK
    assert report.details["username"] == "svbg_bot"
    assert env.runner.holder.current.token == env.token
    assert env.events == ["startup"]


async def test_each_update_processed_once(env: Env) -> None:
    await env.runner.start()
    for i in range(5):
        await env.echo(f"m{i}")
    await asyncio.sleep(0.3)
    texts = [c.params["text"] for c in env.tg.calls_for("sendMessage")]
    assert texts == [f"echo:m{i}" for i in range(5)]


async def test_start_without_token_reports_down(env: Env) -> None:
    env.settings.cfg.pop("BOT_TOKEN")
    await env.runner.start()
    report = await env.runner.health()
    assert report.status is Health.DOWN
    assert report.fix_action == "setting:BOT_TOKEN"
    with pytest.raises(BotUnavailableError):
        _ = env.runner.holder.current
    await env.runner.reconfigure(env.cfg(BOT_TOKEN=env.token))
    await env.echo("hello")


async def test_start_with_rejected_token_reports_down(env: Env) -> None:
    env.settings.cfg["BOT_TOKEN"] = "123456789:" + "A" * 35
    await env.runner.start()
    report = await env.runner.health()
    assert report.status is Health.DOWN
    assert "BotFather" in report.summary


async def test_handler_error_is_captured_and_polling_continues(env: Env) -> None:
    await env.runner.start()
    env.tg.push_message(10, "boom")
    await env.echo("after")
    assert [p for p, *_ in env.hub.captured] == ["tg:update"]
    assert isinstance(env.hub.captured[0][1], RuntimeError)


async def test_reconfigure_same_config_is_noop(env: Env) -> None:
    await env.runner.start()
    bot = env.runner.holder.current
    get_me = len(env.tg.calls_for("getMe"))
    await env.runner.reconfigure(env.cfg())
    await env.runner.reconfigure(env.cfg())
    assert env.runner.holder.current is bot
    assert len(env.tg.calls_for("getMe")) == get_me


# ---------------------------------------------------------------------------------------------- tokens


async def test_hot_token_rotation_same_bot(env: Env) -> None:
    await env.runner.start()
    await env.echo("before")
    old_token = env.token
    new_token = env.tg.rotate_token(old_token)
    version = env.runner.holder.version
    await env.runner.probe(env.cfg(BOT_TOKEN=new_token))
    await env.runner.reconfigure(env.cfg(BOT_TOKEN=new_token))
    assert env.runner.holder.version > version
    assert env.runner.holder.current.token == new_token
    mark = len(env.tg.calls)
    call = await env.echo("after")
    assert call.token == new_token
    await asyncio.sleep(0.3)
    assert not [c for c in env.tg.calls[mark:] if c.token == old_token], "old polling must be stopped"
    texts = [c.params["text"] for c in env.tg.calls_for("sendMessage")]
    assert texts == ["echo:before", "echo:after"]  # nothing re-delivered after the swap
    assert env.events == ["startup"]  # hooks are not re-run on swap


async def test_invalid_token_rejected_old_keeps_working(env: Env) -> None:
    await env.runner.start()
    bad = env.cfg(BOT_TOKEN="987654321:" + "B" * 35)
    with pytest.raises(ProbeError) as probe_err:
        await env.runner.probe(bad)
    assert "BotFather" in probe_err.value.human
    with pytest.raises(ProbeError):
        await env.runner.reconfigure(bad)
    assert env.runner.holder.current.token == env.token
    call = await env.echo("still alive")
    assert call.token == env.token
    assert (await env.runner.health()).status is Health.OK


@pytest.mark.parametrize("token", ["not-a-token", "abc:def", "12345 6:xyz"])
async def test_malformed_token(env: Env, token: str) -> None:
    with pytest.raises(ProbeError) as err:
        await env.runner.probe(env.cfg(BOT_TOKEN=token))
    assert err.value.fix_action == "setting:BOT_TOKEN"


async def test_secrets_not_in_repr_or_errors(env: Env) -> None:
    cfg = BotConfig.from_mapping(webhook_cfg(env))
    text = repr(cfg)
    assert env.token not in text and "s3cret_" not in text
    with pytest.raises(ProbeError) as err:
        await env.runner.probe(env.cfg(BOT_TOKEN="987654321:" + "C" * 35))
    assert "C" * 35 not in str(err.value)


async def test_switch_to_other_bot_requires_confirmation(env: Env) -> None:
    await env.runner.start()
    other = env.tg.add_bot(username="other_bot")
    seen: list[tuple[str | None, str | None]] = []

    async def deny(old: User, new: User) -> bool:
        seen.append((old.username, new.username))
        return False

    env.runner.confirm_bot_change = deny
    with pytest.raises(ProbeError, match="другого бота"):
        await env.runner.probe(env.cfg(BOT_TOKEN=other))
    assert seen == [("svbg_bot", "other_bot")]

    async def allow(old: User, new: User) -> bool:
        return True

    env.runner.confirm_bot_change = allow
    await env.runner.probe(env.cfg(BOT_TOKEN=other))
    await env.runner.reconfigure(env.cfg(BOT_TOKEN=other))
    assert env.runner.me is not None and env.runner.me.username == "other_bot"
    call = await env.echo("hi new", bot_id=env.tg.bot_id(other))
    assert call.token == other
    mark = len(env.tg.calls)
    await asyncio.sleep(0.3)
    assert not [c for c in env.tg.calls[mark:] if c.token == env.token]


async def test_api_url_hot_swap(env: Env) -> None:
    await env.runner.start()
    async with FakeTelegram() as second:
        second.add_bot(token=env.token, username="svbg_bot")
        await env.runner.reconfigure(env.cfg(TELEGRAM_API_URL=second.url))
        start = len(second.calls)
        second.push_message(10, "via second")
        call = await second.wait_for(
            "sendMessage", lambda c: c.params["text"] == "echo:via second", start=start
        )
        assert call.ok
        mark = len(env.tg.calls)
        await asyncio.sleep(0.3)
        assert not [c for c in env.tg.calls[mark:] if c.method == "getUpdates"]
        await env.runner.stop()


async def test_broken_proxy_rejected_old_keeps_working(env: Env) -> None:
    await env.runner.start()
    with pytest.raises(ProbeError):
        await env.runner.reconfigure(env.cfg(TELEGRAM_PROXY="socks5://127.0.0.1:1"))
    with pytest.raises(ProbeError, match="прокси"):
        await env.runner.probe(env.cfg(TELEGRAM_PROXY="ftp://nope"))
    await env.echo("ok")


async def test_bad_api_url_and_mode(env: Env) -> None:
    with pytest.raises(ProbeError, match="TELEGRAM_API_URL"):
        await env.runner.probe(env.cfg(TELEGRAM_API_URL="localhost:8081"))
    with pytest.raises(ProbeError, match="BOT_MODE"):
        await env.runner.probe(env.cfg(BOT_MODE="longpoll"))


# ---------------------------------------------------------------------------------------------- modes


async def test_polling_to_webhook_and_back(env: Env) -> None:
    await env.runner.start()
    await env.echo("polling 1")
    cfg = webhook_cfg(env)
    await env.runner.probe(cfg)
    await env.runner.reconfigure(cfg)
    assert env.runner.mode == "webhook"
    expected = f"{env.web.url}/tg/{webhook_path_secret(cfg['WEBHOOK_SECRET'])}"
    assert env.tg.webhook_url() == expected
    set_call = env.tg.calls_for("setWebhook")[-1]
    assert set_call.params["secret_token"] == cfg["WEBHOOK_SECRET"]
    assert not set_call.params.get("drop_pending_updates")
    await env.echo("via webhook")
    assert (await env.runner.health()).status is Health.OK

    await env.runner.reconfigure(env.cfg(BOT_MODE="polling"))
    assert env.runner.mode == "polling"
    assert env.tg.webhook_url() == ""
    delete = env.tg.calls_for("deleteWebhook")[-1]
    assert not delete.params.get("drop_pending_updates")
    await env.echo("polling 2")
    texts = [c.params["text"] for c in env.tg.calls_for("sendMessage")]
    assert texts == ["echo:polling 1", "echo:via webhook", "echo:polling 2"]


async def test_updates_queued_during_switch_are_not_lost(env: Env) -> None:
    await env.runner.start()
    await env.runner.reconfigure(webhook_cfg(env))
    await env.web.stop()  # webhook temporarily unreachable: Telegram keeps the updates
    env.tg.push_message(10, "queued")
    await asyncio.sleep(0.2)
    assert env.tg.pending_updates()
    await env.web.start()
    await env.runner.reconfigure(env.cfg(BOT_MODE="polling"))
    await env.tg.wait_for("sendMessage", lambda c: c.params["text"] == "echo:queued")


async def test_foreign_webhook_deleted_when_starting_polling(env: Env) -> None:
    from aiogram import Bot
    from aiogram.client.session.aiohttp import AiohttpSession
    from aiogram.client.telegram import TelegramAPIServer

    foreign = Bot(env.token, session=AiohttpSession(api=TelegramAPIServer.from_base(env.tg.url)))
    await foreign.set_webhook("http://127.0.0.1:1/other-app")
    await foreign.session.close()
    env.tg.push_message(10, "waiting")
    await env.runner.start()
    assert env.tg.webhook_url() == ""
    assert env.tg.calls_for("deleteWebhook")[-1].params.get("drop_pending_updates") in (None, False)
    await env.tg.wait_for("sendMessage", lambda c: c.params["text"] == "echo:waiting")


async def test_webhook_failure_restores_polling(env: Env) -> None:
    await env.runner.start()
    env.tg.fail_next("500", method="setWebhook")
    with pytest.raises(ProbeError, match="прежняя"):
        await env.runner.reconfigure(webhook_cfg(env))
    assert env.runner.mode == "polling"
    await env.echo("still polling")


async def test_webhook_probe_checks_config_and_reachability(env: Env) -> None:
    with pytest.raises(ProbeError, match="PUBLIC_URL"):
        await env.runner.probe(env.cfg(BOT_MODE="webhook", WEBHOOK_SECRET="x" * 20))
    with pytest.raises(ProbeError, match="WEBHOOK_SECRET"):
        await env.runner.probe(env.cfg(BOT_MODE="webhook", PUBLIC_URL=env.web.url, WEBHOOK_SECRET="short"))
    with pytest.raises(ProbeError, match="WEBHOOK_SECRET"):
        await env.runner.probe(webhook_cfg(env, WEBHOOK_SECRET="has spaces and !" * 2))
    with pytest.raises(ProbeError, match="недоступен") as err:
        await env.runner.probe(webhook_cfg(env, PUBLIC_URL="http://127.0.0.1:1"))
    assert err.value.fix_action == "setting:PUBLIC_URL"
    env.runner.allow_http_webhook = False
    with pytest.raises(ProbeError, match="https"):
        await env.runner.probe(webhook_cfg(env))


async def test_webhook_endpoint_security(env: Env) -> None:
    cfg = webhook_cfg(env)
    path = webhook_path_secret(cfg["WEBHOOK_SECRET"])
    update = {
        "update_id": 999,
        "message": {
            "message_id": 1,
            "date": 0,
            "chat": {"id": 10, "type": "private"},
            "from": {"id": 10, "is_bot": False, "first_name": "U"},
            "text": "direct",
        },
    }
    async with ClientSession() as http:
        # polling mode (not started): the route does not exist
        async with http.post(f"{env.web.url}/tg/{path}", json=update) as r:
            assert r.status == 404
        await env.runner.start()
        await env.runner.reconfigure(cfg)
        good = {SECRET_HEADER: cfg["WEBHOOK_SECRET"]}
        async with http.post(f"{env.web.url}/tg/wrong", json=update, headers=good) as r:
            assert r.status == 404
        async with http.post(f"{env.web.url}/tg/{path}", json=update, headers={SECRET_HEADER: "nope"}) as r:
            assert r.status == 401
        async with http.post(f"{env.web.url}/tg/{path}", json=update) as r:
            assert r.status == 401
        async with http.post(f"{env.web.url}/tg/{path}", data=b"x" * (300 * 1024), headers=good) as r:
            assert r.status == 413
        async with http.post(f"{env.web.url}/tg/{path}", data=b"{not json", headers=good) as r:
            assert r.status == 200  # acknowledged, so Telegram does not redeliver garbage forever
        async with http.post(f"{env.web.url}/tg/{path}", json=update, headers=good) as r:
            assert r.status == 200
        await env.tg.wait_for("sendMessage", lambda c: c.params["text"] == "echo:direct")
        async with http.get(f"{env.web.url}/tg/ping/{secrets.token_urlsafe(8)}") as r:
            assert r.status == 404  # unknown nonce


async def test_stop_drains_and_runs_shutdown_once(env: Env) -> None:
    await env.runner.start()
    await env.runner.reconfigure(env.cfg(BOT_TOKEN=env.tg.rotate_token(env.token)))
    await env.runner.stop()
    await env.runner.stop()
    assert env.events == ["startup", "shutdown"]
    assert (await env.runner.health()).status is Health.UNKNOWN
    with pytest.raises(BotUnavailableError):
        _ = env.runner.holder.current


async def test_polling_conflict_degrades_health(env: Env) -> None:
    await env.runner.start()
    env.tg.fail_next("409", method="getUpdates", count=50)
    for _ in range(100):
        if (await env.runner.health()).status is Health.DEGRADED:
            break
        await asyncio.sleep(0.1)
    report = await env.runner.health()
    assert report.status is Health.DEGRADED
    assert "Конфликт" in report.summary
    env.tg.clear_faults()


# ------------------------------------------------------------------------------------------- webhook health


def _info_calls(env: Env) -> int:
    return len(env.tg.calls_for("getWebhookInfo"))


async def test_webhook_health_is_cached(env: Env) -> None:
    await env.runner.start()
    await env.runner.reconfigure(webhook_cfg(env))
    before = _info_calls(env)
    for _ in range(5):  # /ready, docker healthchecks, settings screen…
        assert (await env.runner.health()).status is Health.OK
    assert _info_calls(env) == before  # setWebhook just succeeded: no need to ask Telegram

    env.runner.webhook_health_ttl = 0.0  # expired cache: concurrent callers share one request
    reports = await asyncio.gather(*(env.runner.health() for _ in range(10)))
    assert all(r.status is Health.OK for r in reports)
    assert _info_calls(env) == before + 1
    assert reports[0].details["pending_update_count"] == 0

    env.runner.webhook_health_ttl = 30.0
    await env.runner.health()
    assert _info_calls(env) == before + 1


async def test_webhook_health_detects_problems_after_ttl(env: Env) -> None:
    await env.runner.start()
    await env.runner.reconfigure(webhook_cfg(env))
    env.runner.webhook_health_ttl = 0.0
    env.tg.fail_next("500", method="getWebhookInfo")
    report = await env.runner.health()
    assert report.status is Health.DEGRADED
    assert "Telegram" in report.summary
    assert (await env.runner.health()).status is Health.OK


async def test_slow_telegram_does_not_block_webhook_health(env: Env) -> None:
    await env.runner.start()
    await env.runner.reconfigure(webhook_cfg(env))
    env.runner.webhook_health_ttl = 0.0
    env.runner.webhook_health_wait = 0.05
    env.tg.method_latency["getWebhookInfo"] = 0.5
    before = _info_calls(env)
    loop = asyncio.get_running_loop()
    started = loop.time()
    reports = await asyncio.gather(*(env.runner.health() for _ in range(5)))
    assert loop.time() - started < 0.4  # stale answer instead of waiting for Telegram
    assert all(r.status is Health.OK for r in reports)
    await asyncio.sleep(0.6)  # the single background refresh completes and refreshes the cache
    assert _info_calls(env) == before + 1


async def test_cold_webhook_health_survives_caller_timeout(env: Env) -> None:
    from svbg.core.component import ComponentRegistry

    await env.runner.start()
    await env.runner.reconfigure(webhook_cfg(env))
    env.runner._wh_check = None  # no answer cached yet (e.g. after a restore)
    env.tg.method_latency["getWebhookInfo"] = 0.3
    before = _info_calls(env)
    registry = ComponentRegistry()
    registry.register(env.runner)
    report = await registry.health("bot", limit_s=0.05)  # /ready gives up first…
    assert report.status is Health.UNKNOWN
    await asyncio.sleep(0.5)  # …but the shared request is not cancelled and fills the cache
    env.tg.method_latency.clear()
    assert (await env.runner.health()).status is Health.OK
    assert _info_calls(env) == before + 1


async def test_stop_cancels_webhook_health_refresh(env: Env) -> None:
    await env.runner.start()
    await env.runner.reconfigure(webhook_cfg(env))
    env.runner._wh_check = None
    env.tg.fail_next("timeout", method="getWebhookInfo")
    waiter = asyncio.ensure_future(env.runner.health())
    await asyncio.sleep(0.2)  # the refresh is now stuck in Telegram
    assert env.runner._wh_refresh is not None and not env.runner._wh_refresh.done()
    await asyncio.wait_for(env.runner.stop(), 5)
    report = await asyncio.wait_for(waiter, 1)
    assert report.status is not Health.OK
    assert env.runner._wh_refresh is None or env.runner._wh_refresh.done()
    env.tg.clear_faults()


async def test_holder_wait_ready(env: Env) -> None:
    waiter = asyncio.ensure_future(env.runner.holder.wait_ready())
    await asyncio.sleep(0)
    assert not waiter.done()
    await env.runner.start()
    bot = await asyncio.wait_for(waiter, 5)
    assert bot.token == env.token


async def test_start_retries_while_telegram_unreachable(env: Env) -> None:
    env.runner.start_retry_delay = 0.05
    env.tg.fail_next("502", method="getMe", count=2)
    await env.runner.start()
    report = await env.runner.health()
    assert report.status is Health.DOWN
    assert "Telegram" in report.summary
    for _ in range(100):
        if env.runner.mode == "polling":
            break
        await asyncio.sleep(0.05)
    await env.echo("recovered")
    assert (await env.runner.health()).status is Health.OK


async def test_failed_reconfigure_without_active_bot_is_reported(env: Env) -> None:
    env.settings.cfg.pop("BOT_TOKEN")
    await env.runner.start()
    with pytest.raises(ProbeError):
        await env.runner.reconfigure(env.cfg(BOT_TOKEN="123456789:" + "D" * 35))
    report = await env.runner.health()
    assert report.status is Health.DOWN
    assert "BotFather" in report.summary


async def test_startup_hook_failure_does_not_break_bot(env: Env) -> None:
    @env.dp.startup()
    async def broken() -> None:
        raise RuntimeError("startup failed")

    await env.runner.start()
    await env.echo("works")
    assert "tg:startup" in [p for p, *_ in env.hub.captured]


async def test_concurrent_reconfigure_is_serialized(env: Env) -> None:
    await env.runner.start()
    secret = webhook_cfg(env)
    await asyncio.gather(
        env.runner.reconfigure(secret),
        env.runner.reconfigure(env.cfg(BOT_MODE="polling")),
        env.runner.reconfigure(secret),
    )
    assert env.runner.mode == "webhook"
    assert env.tg.webhook_url().startswith(env.web.url)
    await env.echo("final")


async def test_notifier_follows_token_swap(env: Env) -> None:
    from svbg.tg.notifier import Notifier

    notifier = Notifier(env.runner.holder)
    await env.runner.start()
    await notifier.send(77, "before")
    new_token = env.tg.rotate_token(env.token)
    await env.runner.reconfigure(env.cfg(BOT_TOKEN=new_token))
    await notifier.send(77, "after")
    tokens = {c.params["text"]: c.token for c in env.tg.calls_for("sendMessage")}
    assert tokens == {"before": env.token, "after": new_token}
    await notifier.close()


async def test_concurrent_handlers_are_bounded(tg: FakeTelegram) -> None:
    release = asyncio.Event()
    active: list[int] = []
    peak = 0
    dp = Dispatcher()

    @dp.message()
    async def slow(message: Message) -> None:
        nonlocal peak
        active.append(message.message_id)
        peak = max(peak, len(active))
        await release.wait()
        active.remove(message.message_id)

    token = tg.add_bot()
    runner = BotRunner(
        FakeSettings(BOT_TOKEN=token, TELEGRAM_API_URL=tg.url),
        dp,
        polling_timeout=1,
        max_concurrent_updates=2,
        drain_timeout=1,
    )
    await runner.start()
    try:
        for i in range(6):
            tg.push_message(100 + i, f"m{i}")
        await asyncio.sleep(0.5)
        assert peak == 2
        assert len(tg.pending_updates()) >= 3  # not consumed until a slot frees up
        release.set()
        for _ in range(50):
            if not tg.pending_updates() and not active:
                break
            await asyncio.sleep(0.05)
        assert not active and peak == 2
    finally:
        await runner.stop()
