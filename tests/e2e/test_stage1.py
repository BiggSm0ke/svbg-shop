"""Stage 1 end to end (07 §5 «Этап 1», criteria): the whole application on a real PostgreSQL 17, the fake
Telegram Bot API and the fake Remnawave 3.4.4 panel (no real server is contacted).

* the setup wizard from scratch: panel address + token (both messages deleted) → «Remnawave 3.4.4 ✅», the
  admin supergroup picked with ``request_chat`` → 9 topics created by the bot, webhooks step → «Готово»;
* a wrong panel token gives a clear Russian error and the previous connection keeps working;
* a signed panel webhook → ``rw_inbox`` → GET confirmation → projection into ``subscriptions``; a forged
  signature is rejected and stores nothing;
* an exception in a handler → one report in «🚨 Ошибки»; 50 identical ones → still one message, edited to
  «×50»;
* the panel goes down → auto-maintenance after the breaker stayed open for ``auto_after`` (3 min in
  production, shortened here) → the panel recovers → maintenance is lifted;
* importing the users of the fake panel (dry run → apply → apply again: idempotent).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from aiohttp import ClientSession

from svbg.app import App, PanelEventRelay
from svbg.boot.envfile import EnvDocument
from svbg.core.bus import Event
from svbg.core.component import Health
from svbg.core.errors import BreakerState
from svbg.core.settings.service import Change
from svbg.remnawave import RemnawaveError
from svbg.services.maintenance import ATT_AUTO, AUTO_AFTER
from svbg.tg.admin.connect_chat import REQUEST_ID
from svbg.tg.ui.codec import encode
from svbg.tg.ui.view import View
from tests.e2e.conftest import OWNER_ID, AppEnv, StartApp, counting_db
from tests.fakes.remnawave import FakeRemnawave
from tests.fakes.telegram import Call, FakeTelegram

pytestmark = pytest.mark.pg

GROUP = -1_002_000_000_777
SECRET = "S" * 20 + "ecret" + "0123456789abcdefghij" + "XYZ"  # ≥ 32 Latin letters and digits

#: Fast timings for tests; production defaults are asserted separately.
FAST: dict[str, Any] = {
    "remnawave_kwargs": {
        "probe_timeout": 5.0,
        "close_grace": 0.5,
        "transport_overrides": {"max_attempts": 1, "breaker_cooldown": 0.3, "backoff_base": 0.01},
    },
    "maintenance_kwargs": {"auto_after": 0.6, "interval": 0.1, "recovery_timeout": 2.0},
    "inbox_poll_interval": 0.2,
    "hub_kwargs": {"update_interval": 0.5, "flush_interval": 0.2},
}


# ------------------------------------------------------------------------------------------------ fixtures


@pytest.fixture
async def panel() -> AsyncIterator[FakeRemnawave]:
    async with FakeRemnawave(webhook_enabled=False) as fake:
        yield fake


def panel_env(app_env: AppEnv, panel: FakeRemnawave, **extra: str | None) -> str:
    """Write the panel connection into ``.env`` (as an owner would); returns the token."""
    token = panel.add_token()
    app_env.write(REMNAWAVE_URL=panel.url, REMNAWAVE_TOKEN=token, **extra)
    return token


def make_bot_admin(tg: FakeTelegram, token: str, chat_id: int = GROUP) -> None:
    tg.make_admin(
        chat_id,
        tg.bot_id(token),
        can_manage_topics=True,
        can_pin_messages=True,
        can_delete_messages=True,
        can_manage_chat=True,
    )


async def until(predicate: Callable[[], Any], timeout: float = 10.0, what: str = "condition") -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"{what}: not met within {timeout}s")
        await asyncio.sleep(0.03)


async def until_async(predicate: Callable[[], Any], timeout: float = 10.0, what: str = "condition") -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = await predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"{what}: not met within {timeout}s")
        await asyncio.sleep(0.05)


class Chat:
    """The owner's private chat as Telegram shows it: what the bot sent/edited, buttons to press."""

    def __init__(self, tg: FakeTelegram, user_id: int = OWNER_ID) -> None:
        self.tg = tg
        self.user_id = user_id
        self._msg_ids = iter(range(50_000, 60_000))

    def visible(self, start: int = 0) -> list[Call]:
        return [
            c
            for c in self.tg.calls[start:]
            if c.ok
            and c.method in ("sendMessage", "editMessageText")
            and c.params.get("chat_id") == self.user_id
        ]

    @property
    def text(self) -> str:
        shown = self.visible()
        return str(shown[-1].params["text"]) if shown else ""

    def all_text(self) -> str:
        return "\n".join(str(c.params.get("text")) for c in self.visible())

    @staticmethod
    def message_id(call: Call) -> int:
        if call.method == "sendMessage":
            return int(call.result["message_id"])
        return int(call.params["message_id"])

    @staticmethod
    def buttons(call: Call) -> list[tuple[str, str]]:
        markup = call.params.get("reply_markup") or {}
        rows = markup.get("inline_keyboard") or [] if isinstance(markup, dict) else []
        return [(b["text"], b.get("callback_data", "")) for row in rows for b in row]

    async def wait_text(self, needle: str, *, start: int = 0, timeout: float = 15.0) -> Call:
        return await until(
            lambda: next((c for c in reversed(self.visible(start)) if needle in str(c.params["text"])), None),
            timeout,
            f"text {needle!r} in the owner chat (last: {self.text[:300]!r})",
        )

    async def press(self, label: str, *, expect: str | None = None, timeout: float = 15.0) -> Call | None:
        """Press the newest button whose label contains ``label``; optionally wait for ``expect``."""
        for call in reversed(self.visible()):
            found = next((data for text, data in self.buttons(call) if label in text), None)
            if found:
                start = len(self.tg.calls)
                self.tg.push_callback(self.user_id, found, self.message_id(call))
                if expect is None:
                    await self.tg.wait_for("answerCallbackQuery", lambda _c: True, timeout, start=start)
                    return None
                return await self.wait_text(expect, start=start, timeout=timeout)
        raise AssertionError(f"no button {label!r}; last screen: {self.text[:400]!r}")

    async def say(self, text: str, *, expect: str | None = None, timeout: float = 15.0) -> Call | None:
        start = len(self.tg.calls)
        self.tg.push_message(self.user_id, text)
        if expect is None:
            return None
        return await self.wait_text(expect, start=start, timeout=timeout)

    def share_chat(self, chat_id: int) -> None:
        """The owner picked a group in the ``request_chat`` dialog (Telegram sends ``chat_shared``)."""
        self.tg.push_update(
            {
                "message": {
                    "message_id": next(self._msg_ids),
                    "date": int(time.time()),
                    "chat": {"id": self.user_id, "type": "private", "first_name": "Owner"},
                    "from": {"id": self.user_id, "is_bot": False, "first_name": "Owner"},
                    "chat_shared": {"request_id": REQUEST_ID, "chat_id": chat_id},
                }
            }
        )


def group_sends(tg: FakeTelegram, thread_id: int | None = None) -> list[Call]:
    return [
        c
        for c in tg.calls
        if c.ok
        and c.method == "sendMessage"
        and c.params.get("chat_id") == GROUP
        and (thread_id is None or c.params.get("message_thread_id") == thread_id)
    ]


async def topic_thread(app: App, kind: str) -> int:
    rows = await counting_db(app.db).raw("select thread_id from admin_topics where kind = $1", kind)
    assert rows and rows[0]["thread_id"], f"topic {kind} has no thread"
    return int(rows[0]["thread_id"])


# ------------------------------------------------------------------------------------------------ wiring


async def test_stage1_parts_are_wired_with_production_defaults(start_app: StartApp, app_env: AppEnv) -> None:
    app = await start_app()
    for name in ("svbg.tg.setup.wizard", "svbg.tg.admin.status", "svbg.tg.admin.connect_chat"):
        assert name in app.wired_modules, app.missing_modules
    assert {"remnawave", "admin_chat"} <= set(app.components.names())
    assert app.deps is not None and app.deps.remnawave is app.remnawave
    assert app.deps.admin_chat is app.admin_chat and app.deps.maintenance is app.maintenance
    assert app.maintenance is not None and app.maintenance.auto_after == AUTO_AFTER == 180.0
    assert {"panel.create", "panel.update", "panel.renew", "remnawave.import"} <= set(app.job_handlers)
    report = await app.components.health("remnawave")
    assert report.status is Health.DISABLED and report.fix_action == "setting:REMNAWAVE_URL"
    assert app.web is not None
    async with ClientSession() as session:  # webhooks are off until a secret is configured
        async with session.post(f"{app.web.url}/webhooks/remnawave", data=b"{}") as resp:
            assert resp.status == 401
        async with session.get(f"{app.web.url}/ready") as resp:
            assert resp.status == 200  # a not-connected panel does not make the bot «not ready»


async def test_status_screen_shows_panel_queue_and_maintenance(
    start_app: StartApp, app_env: AppEnv, panel: FakeRemnawave
) -> None:
    panel_env(app_env, panel)
    app = await start_app(**FAST)
    owner = Chat(app_env.tg)
    shown = await owner.say("/status", expect="Состояние")
    assert shown is not None
    text = str(shown.params["text"])
    assert "Компоненты" in text and "Панель 3.4.4" in text
    assert "Работает" in text  # uptime from AppDeps.started_at
    labels = [label for label, _ in Chat.buttons(shown)]
    assert any("Техработы" in label for label in labels)  # MAINTENANCE_MODE exists in the registry
    assert app.deps is not None and app.deps.started_at is not None
    # An ordinary user gets nothing from /status.
    stranger = Chat(app_env.tg, user_id=9_009)
    start = len(app_env.tg.calls)
    await stranger.say("/status")
    await asyncio.sleep(0.5)
    assert not [c for c in stranger.visible(start) if "Состояние" in str(c.params.get("text"))]


# ------------------------------------------------------------------------------------- wizard from scratch


async def test_wizard_from_scratch_panel_admin_group_webhooks_done(
    start_app: StartApp, app_env: AppEnv, panel: FakeRemnawave
) -> None:
    tg = app_env.tg
    app = await start_app(**FAST)
    owner = Chat(tg)
    token = panel.add_token()

    # Step 2: Remnawave — address and token; both messages are deleted, the token never shows up.
    await owner.say("/setup", expect="Мастер настройки")
    assert "Панель не подключена" in owner.text
    await owner.press("Ввести адрес", expect="Пришлите адрес панели")
    await owner.say(panel.url, expect="API-токен")
    await owner.say(token)
    done = await owner.wait_text("✅ Подключено", timeout=20)
    assert "Панель 3.4.4" in done.params["text"]
    deletes = tg.calls_for("deleteMessage")
    assert len({c.params["message_id"] for c in deletes if c.params.get("chat_id") == OWNER_ID}) >= 2
    assert token not in owner.all_text()
    assert app.remnawave is not None and app.remnawave.configured
    assert (await app.remnawave.client.metadata()).version == "3.4.4"
    assert app.settings is not None and app.settings.current()["REMNAWAVE_TOKEN"] == token
    health = await app.components.health("remnawave")
    assert health.status is Health.OK and "3.4.4" in health.summary

    # Step 3: the admin supergroup, picked with request_chat → the bot creates the core and module topics.
    make_bot_admin(tg, app_env.token)
    await owner.press("Дальше", expect="Админ-группа")
    await owner.press("Подключить группу", expect="Выбрать группу")
    await owner.press("Выбрать группу")
    owner.share_chat(GROUP)
    # 9 core topics + the module topics (stage 3–4: «Партнёры», «Трафик LTE», «Антиабуз»)
    assert app.admin_chat is not None
    total = sum(1 for d in app.admin_chat.topic_defs() if app.admin_chat.state(d.kind).enabled)
    assert total >= 9
    await owner.wait_text(f"Темы готовы: {total} из {total}", timeout=20)
    assert len(tg.topics(GROUP)) == total
    assert app.settings.current()["ADMIN_CHAT_ID"] == GROUP
    assert app.admin_chat is not None and app.admin_chat.chat_id == GROUP

    # Step 4: webhooks (mode A: the bot generates the secret, the panel gets a ready snippet).
    await owner.say("/setup", expect="Мастер настройки")
    start = len(tg.calls)
    tg.push_callback(OWNER_ID, encode("setup.wiz", "o", "wh"), owner.message_id(owner.visible()[-1]))
    await owner.wait_text("Вебхуки панели", start=start)
    await owner.press("Показать настройки", expect="WEBHOOK_SECRET_HEADER")
    secret = app.settings.current()["REMNAWAVE_WEBHOOK_SECRET"]
    assert isinstance(secret, str) and len(secret) >= 32
    assert "/webhooks/remnawave" in owner.text
    # The owner ran the snippet and restarted the panel: it now signs webhooks with that secret.
    panel.configuration["notifications"]["webhook"] = True
    assert app.web is not None
    status = await panel.send_webhook(
        f"{app.web.url}/webhooks/remnawave", "service", "service.panel_started", {"panelVersion": "3.4.4"},
        secret=secret,
    )  # fmt: skip
    assert status == 200
    # «service.panel_started» → inbox → the component re-reads the panel configuration (webhooks on).
    rw = app.remnawave
    await until(lambda: rw.capabilities is not None and rw.capabilities.webhooks_enabled, what="caps refresh")

    # Step 5: readiness checklist and «Готово».
    start = len(tg.calls)
    tg.push_callback(OWNER_ID, encode("setup.wiz", "o", "ready"), owner.message_id(owner.visible()[-1]))
    ready = await owner.wait_text("Готовность", start=start)
    text = str(ready.params["text"])
    assert "✅ Remnawave: Панель 3.4.4" in text
    assert "✅ Админ-группа" in text
    assert "✅ Первое событие вебхука получено" in text
    assert "✅ Владелец назначен" in text
    assert "Готовность: 6/6" in text
    await owner.press("Готово", expect="Настройка завершена")
    # The .env mirror got the new panel settings (owner-facing file, no restart needed).

    def mirrored() -> bool:
        doc = EnvDocument.parse(app_env.env_path.read_text(encoding="utf-8"))
        return doc.get("REMNAWAVE_URL") == panel.url and doc.get("ADMIN_CHAT_ID") == str(GROUP)

    await until(mirrored, what="panel and admin chat settings mirrored to .env")


async def test_wrong_panel_token_gives_clear_error_and_old_connection_keeps_working(
    start_app: StartApp, app_env: AppEnv, panel: FakeRemnawave
) -> None:
    good = panel_env(app_env, panel)
    app = await start_app(**FAST)
    assert app.remnawave is not None and app.settings is not None
    assert (await app.remnawave.client.metadata()).version == "3.4.4"
    owner = Chat(app_env.tg)

    # Through the wizard: the self-test fails on a separate session, the running client is untouched.
    bad = "eyJhbGciOiJIUzI1NiJ9.eyJ1dWlkIjoid3JvbmcifQ.c2lnbmF0dXJlc2lnbmF0dXJl"
    await owner.say("/setup", expect="Мастер настройки")
    await owner.press("Ввести адрес", expect="Пришлите адрес панели")
    await owner.say(panel.url, expect="API-токен")
    await owner.say(bad)
    failed = await owner.wait_text("❌ Не подключено", timeout=20)
    assert "Прежнее подключение продолжает работать" in failed.params["text"]
    assert "токен" in failed.params["text"].lower()
    assert "eyJ1dWlkIjoid3JvbmcifQ" not in owner.all_text()

    # Through the settings pipeline (the «⚙️ Настройки» screen and the .env mirror use the same path).
    result = await app.settings.apply([Change("REMNAWAVE_TOKEN", bad)], source="bot", actor_id=None)
    assert not result.ok and "токен" in result.rejected["REMNAWAVE_TOKEN"].lower()
    assert app.settings.current()["REMNAWAVE_TOKEN"] == good
    assert (await app.remnawave.client.metadata()).version == "3.4.4"
    assert (await app.components.health("remnawave")).status is Health.OK


# ------------------------------------------------------------------------------------ webhook → projection


async def test_signed_panel_webhook_is_projected_into_subscriptions(
    start_app: StartApp, app_env: AppEnv, panel: FakeRemnawave
) -> None:
    panel_env(app_env, panel, REMNAWAVE_WEBHOOK_SECRET=SECRET)
    squad = panel.add_internal_squad()
    user = panel.add_user(telegramId=4_242_001, username="sv_e2e", activeInternalSquads=[squad])
    app = await start_app(**FAST)
    db = counting_db(app.db)
    assert await app.enqueue_import("apply") is not None
    rows = await until_async(
        lambda: db.raw("select id, panel_expire_at from subscriptions where panel_user_id = $1", user["id"]),
        what="imported subscription",
    )
    sub_id = rows[0]["id"]

    # An admin extends the user in the panel; the panel sends a signed «user.modified».
    new_expire = datetime.now(UTC).replace(microsecond=0) + timedelta(days=90)
    user["expireAt"] = new_expire
    user["status"] = "ACTIVE"
    assert app.web is not None
    url = f"{app.web.url}/webhooks/remnawave"
    assert await panel.send_webhook(url, "user", "user.modified", panel.user_json(user), secret=SECRET) == 200

    async def projected() -> bool:
        got = await db.raw("select panel_expire_at from subscriptions where id = $1", sub_id)
        return bool(got) and got[0]["panel_expire_at"] == new_expire

    await until_async(projected, what="panel_expire_at projected from the webhook")
    inbox = await db.raw("select status, event from rw_inbox")
    assert [(r["status"], r["event"]) for r in inbox] == [("done", "user.modified")]
    assert panel.calls(f"/users/{user['id']}", "GET"), "the webhook was confirmed with GET /users/{id}"

    # The same body again is a duplicate; a forged signature is rejected and nothing is stored.
    raw, headers = panel.build_webhook("user", "user.modified", panel.user_json(user), secret=SECRET)
    forged_raw, forged = panel.build_webhook("user", "user.deleted", panel.user_json(user), secret="x" * 40)
    replies: list[tuple[int, str]] = []
    async with ClientSession() as session:
        async with session.post(url, data=forged_raw, headers=forged) as resp:
            assert resp.status == 401
        for _ in range(2):  # the panel retries the same bytes
            async with session.post(url, data=raw, headers=headers) as resp:
                replies.append((resp.status, (await resp.json())["status"]))
    assert replies == [(200, "ok"), (200, "duplicate")]
    assert len(await db.raw("select 1 from rw_inbox")) == 2  # two distinct bodies, no forged row
    assert app.webhook_stats.bad_signature == 1
    seen = await app.webhook_seen()
    assert seen is not None and seen.last_ok_at is not None and seen.last_bad_signature_at is not None


async def test_webhook_secret_rotation_accepts_the_previous_secret(
    start_app: StartApp, app_env: AppEnv, panel: FakeRemnawave
) -> None:
    panel_env(app_env, panel, REMNAWAVE_WEBHOOK_SECRET=SECRET)
    app = await start_app(**FAST)
    assert app.settings is not None and app.web is not None
    new = "N" * 40
    result = await app.settings.apply([Change("REMNAWAVE_WEBHOOK_SECRET", new)], source="bot", actor_id=None)
    assert result.ok and "remnawave" not in result.reloaded  # HOT: no panel self-test for the secret
    assert app.webhook_secrets() == [new, SECRET]
    url = f"{app.web.url}/webhooks/remnawave"
    assert await panel.send_webhook(url, "service", "service.panel_started", {}, secret=SECRET) == 200
    bad = await app.settings.apply([Change("REMNAWAVE_WEBHOOK_SECRET", "short")], source="bot", actor_id=None)
    assert "32" in bad.rejected["REMNAWAVE_WEBHOOK_SECRET"]


async def test_clearing_the_webhook_secret_rejects_webhooks_at_once(
    start_app: StartApp, app_env: AppEnv, panel: FakeRemnawave
) -> None:
    """«Пусто — вебхуки не принимаются»: no 24 h grace for the cleared secret nor an explicit previous one."""
    panel_env(app_env, panel, REMNAWAVE_WEBHOOK_SECRET=SECRET)
    app = await start_app(**FAST)
    assert app.settings is not None and app.web is not None
    url = f"{app.web.url}/webhooks/remnawave"
    new, other = "N" * 40, "Q" * 40

    async def apply(**values: str | None) -> None:
        changes = [Change(k, v) for k, v in values.items()]
        assert (await app.settings.apply(changes, source="bot", actor_id=None)).ok  # type: ignore[union-attr]

    await apply(REMNAWAVE_WEBHOOK_SECRET=new)
    assert app.webhook_secrets() == [new, SECRET]  # an ordinary rotation keeps the window
    await apply(REMNAWAVE_WEBHOOK_SECRET=None, REMNAWAVE_WEBHOOK_SECRET_PREVIOUS=SECRET)
    assert app.webhook_secrets() == []
    for secret in (SECRET, new):
        assert await panel.send_webhook(url, "node", "node.modified", {"uuid": "n1"}, secret=secret) == 401
    assert app.webhook_stats.no_secret == 2

    # Rotation after a leak: clear, then set a new one — the leaked secret got no window.
    await apply(REMNAWAVE_WEBHOOK_SECRET_PREVIOUS=None)
    await apply(REMNAWAVE_WEBHOOK_SECRET=other)
    assert app.webhook_secrets() == [other]
    assert await panel.send_webhook(url, "node", "node.modified", {"uuid": "n2"}, secret=new) == 401
    # Or revoke the window explicitly right after a replacement.
    await apply(REMNAWAVE_WEBHOOK_SECRET=new)
    assert app.webhook_secrets() == [new, other]
    assert app.revoke_previous_webhook_secret() is True
    assert app.webhook_secrets() == [new]
    assert app.revoke_previous_webhook_secret() is False
    assert await panel.send_webhook(url, "node", "node.modified", {"uuid": "n3"}, secret=other) == 401
    assert await panel.send_webhook(url, "node", "node.modified", {"uuid": "n4"}, secret=new) == 200


async def test_noise_on_the_webhook_url_does_not_report_a_secret_mismatch(
    start_app: StartApp, app_env: AppEnv, panel: FakeRemnawave
) -> None:
    panel_env(app_env, panel, REMNAWAVE_WEBHOOK_SECRET=SECRET)
    app = await start_app(**FAST)
    assert app.web is not None
    url = f"{app.web.url}/webhooks/remnawave"
    async with ClientSession() as session:
        for body, headers in (
            (b"garbage", {}),
            (b'{"scope":"user"}', {"User-Agent": "Remnawave", "X-Remnawave-Signature": "0" * 64}),
        ):
            async with session.post(url, data=body, headers=headers) as resp:
                assert resp.status == 401
    assert app.webhook_stats.bad_signature_foreign == 2 and app.webhook_stats.bad_signature == 0
    seen = await app.webhook_seen()
    assert seen is not None and seen.last_bad_signature_at is None  # the wizard keeps saying «ждём»
    # The panel itself with another secret is still recognised.
    assert await panel.send_webhook(url, "node", "node.modified", {"uuid": "n1"}, secret="W" * 40) == 401
    seen = await app.webhook_seen()
    assert seen is not None and seen.last_bad_signature_at is not None


async def test_panel_restart_webhook_starts_a_full_reconciliation(
    start_app: StartApp, app_env: AppEnv, panel: FakeRemnawave
) -> None:
    panel_env(app_env, panel, REMNAWAVE_WEBHOOK_SECRET=SECRET)
    app = await start_app(**FAST)
    assert app.web is not None and app.reconciler is not None
    reconciler = app.reconciler
    url = f"{app.web.url}/webhooks/remnawave"
    payload = {"panelVersion": "2.6.0"}
    assert await panel.send_webhook(url, "service", "service.panel_started", payload, secret=SECRET) == 200
    report = await until(
        lambda: (r := reconciler.last.get("full")) is not None and r.trigger == "panel_started" and r,
        what="a full sync triggered by service.panel_started",
    )
    assert report.status == "ok"


async def test_panel_restart_full_pass_runs_in_background_once_and_not_when_panel_is_down(
    start_app: StartApp, app_env: AppEnv, panel: FakeRemnawave, monkeypatch: pytest.MonkeyPatch
) -> None:
    panel_env(app_env, panel)
    app = await start_app(**FAST)
    assert app.reconciler is not None and app.remnawave is not None
    gate = asyncio.Event()
    calls: list[str] = []

    async def slow_full_pass(trigger: str = "manual") -> Any:
        calls.append(trigger)
        await gate.wait()

    monkeypatch.setattr(app.reconciler, "full_pass", slow_full_pass)
    # The inbox handler returns right away (the pass runs in the background); a second restart while the
    # first pass is still running does not start another one.
    await asyncio.wait_for(app._on_panel_started(), 5)
    await asyncio.wait_for(app._on_panel_started(), 5)
    await until(lambda: calls, what="background full pass")
    assert calls == ["panel_started"]
    gate.set()
    await until(lambda: app._restart_pass is not None and app._restart_pass.done(), what="pass finished")

    async def refresh_fails() -> Any:
        raise RemnawaveError("transient", message="connection refused")

    calls.clear()
    monkeypatch.setattr(app.remnawave, "refresh", refresh_fails)
    await app._on_panel_started()
    await asyncio.sleep(0.1)
    assert calls == [], "the panel is unreachable: the scheduled pass catches up later"


# ------------------------------------------------------------------------------- errors → «🚨 Ошибки»


class Boom(RuntimeError):
    pass


def install_failing_screen(app: App) -> None:
    assert app.screens is not None

    @app.screens.screen("stage1boom")
    async def boom(_ctx: Any, arg: Any) -> View:
        if arg == "fail":
            raise Boom("handler exploded on purpose")
        return View(text=f"ok {arg}")


async def test_handler_errors_go_to_the_errors_topic_once_with_a_counter(
    start_app: StartApp, app_env: AppEnv
) -> None:
    tg = app_env.tg
    make_bot_admin(tg, app_env.token)
    app_env.write(ADMIN_CHAT_ID=str(GROUP))
    app = await start_app(setup_hooks=[install_failing_screen], **FAST)
    user = 6_006
    tg.push_message(user, "/start")
    # the home screen shows the default banner: a photo message
    main = await tg.wait_for("sendPhoto", lambda c: c.params.get("chat_id") == user and c.ok, timeout=10)
    msg_id = main.result["message_id"]

    start = len(tg.calls)
    tg.push_callback(user, encode("stage1boom", arg="fail"), msg_id)
    report = await tg.wait_for(
        "sendMessage",
        lambda c: c.ok and c.params.get("chat_id") == GROUP and "Boom" in str(c.params.get("text")),
        timeout=20,
        start=start,
    )
    errors_thread = await topic_thread(app, "errors")
    assert report.params.get("message_thread_id") == errors_thread
    assert "<blockquote expandable>" in report.params["text"]
    assert app_env.token.split(":")[1] not in report.params["text"]
    labels = [b["text"] for row in report.params["reply_markup"]["inline_keyboard"] for b in row]
    assert any("Заглушить" in t for t in labels) and any("Состояние" in t for t in labels)
    assert not [c for c in tg.calls if c.params.get("chat_id") == OWNER_ID and "Boom" in str(c.params)]

    for _ in range(49):  # 49 more identical exceptions (one click at a time, like a person)
        cid = tg.push_callback(user, encode("stage1boom", arg="fail"), msg_id)["callback_query"]["id"]
        await tg.wait_for(
            "answerCallbackQuery", lambda c, cid=cid: c.params.get("callback_query_id") == cid, timeout=10
        )
    report_id = report.result["message_id"]

    def counted() -> bool:
        return any(
            c.ok and c.params.get("chat_id") == GROUP and c.params.get("message_id") == report_id
            and "×50" in str(c.params.get("text"))
            for c in tg.calls_for("editMessageText")
        )  # fmt: skip

    await until(counted, timeout=30, what="report edited to ×50")
    boom_sends = [c for c in group_sends(tg) if "Boom" in str(c.params.get("text"))]
    assert len(boom_sends) == 1, "50 identical exceptions give ONE message"


# ------------------------------------------------------------------------------------ auto-maintenance


async def test_panel_down_turns_on_auto_maintenance_and_recovery_lifts_it(
    start_app: StartApp, app_env: AppEnv, panel: FakeRemnawave
) -> None:
    panel_env(app_env, panel)
    app = await start_app(**FAST)
    rw, maint = app.remnawave, app.maintenance
    assert rw is not None and maint is not None and app.attention is not None
    assert not maint.active
    events: list[str] = []

    async def on_event(event: Event) -> None:
        events.append(event.name)

    app.bus.subscribe("maintenance.*", on_event)

    panel.inject("503", times=None)  # the panel container is down behind its proxy
    for _ in range(6):
        with pytest.raises(RemnawaveError):
            await rw.client.metadata()
    assert rw.breaker_state is BreakerState.OPEN
    began = time.monotonic()
    await until(lambda: maint.active, timeout=10, what="auto-maintenance")
    assert time.monotonic() - began >= 0.3, "not before the breaker stayed open for auto_after"
    assert maint.state.reason == "auto"
    attention = app.attention
    await until_async(lambda: attention.get(ATT_AUTO), what="«Требует внимания»: техработы")
    assert (await app.components.health("remnawave")).status is Health.DOWN

    panel.clear_faults()  # the panel is back; the breaker's HALF_OPEN trial closes it
    await until(lambda: not maint.active, timeout=15, what="maintenance lifted after recovery")
    assert rw.breaker_state is BreakerState.CLOSED
    await until(lambda: events[-1:] == ["maintenance.off"], what="maintenance.off event")
    assert "maintenance.on" in events

    async def resolved() -> bool:
        item = await attention.get(ATT_AUTO)
        return item is not None and not item.is_open

    await until_async(resolved, what="«Требует внимания» resolved")


# ------------------------------------------------------------------------------------------------ import


async def test_import_users_of_the_panel_dry_run_apply_and_again(
    start_app: StartApp, app_env: AppEnv, panel: FakeRemnawave
) -> None:
    panel_env(app_env, panel)
    squad = panel.add_internal_squad()
    for i in range(7):
        panel.add_user(telegramId=7_000_000 + i, username=f"old_{i}", activeInternalSquads=[squad])
    panel.add_user(telegramId=7_000_000, username="old_0_second", activeInternalSquads=[squad])
    for i in range(3):
        panel.add_user(username=f"nobody_{i}", activeInternalSquads=[squad])  # no telegramId → unclaimed
    app = await start_app(**FAST)
    db = counting_db(app.db)
    tg = app_env.tg

    async def run(mode: str) -> dict[str, Any]:
        before = len(await db.raw("select 1 from import_runs where status = 'done'"))
        assert await app.enqueue_import(mode) is not None
        await until_async(lambda: _done_runs(db, before), timeout=30, what=f"import {mode} finished")
        rows = await db.raw("select report from import_runs order by id desc limit 1")
        return dict(rows[0]["report"])

    dry = await run("dry_run")
    assert dry["total"] == 11 and dry["with_telegram"] == 8 and dry["without_telegram"] == 3
    assert await db.raw("select 1 from subscriptions") == []

    applied = await run("apply")
    assert applied["subscriptions_created"] == 11 and applied["users_created"] == 7
    subs = await db.raw("select user_id, link_state from subscriptions")
    assert len(subs) == 11 and all(s["link_state"] == "linked" for s in subs)
    assert sum(s["user_id"] is None for s in subs) == 3
    two = await db.raw(
        "select count(*) as n from subscriptions s join users u on u.id = s.user_id where u.telegram_id = $1",
        7_000_000,
    )
    assert two[0]["n"] == 2  # two panel accounts of one Telegram user → two subscriptions
    writes = [r for r in panel.requests if r.method != "GET" and r.path != "/users/resolve"]  # resolve = read
    assert writes == [], "the importer never writes to the panel"

    again = await run("apply")
    assert again["subscriptions_created"] == 0 and again["users_created"] == 0
    assert len(await db.raw("select 1 from subscriptions")) == 11
    # No admin chat: the owner gets the summary in DM.
    await tg.wait_for(
        "sendMessage",
        lambda c: c.params.get("chat_id") == OWNER_ID and "Импорт из панели" in str(c.params.get("text")),
        timeout=10,
    )


async def _done_runs(db: Any, before: int) -> bool:
    return len(await db.raw("select 1 from import_runs where status = 'done'")) > before


async def test_import_without_panel_is_a_permanent_job_failure(start_app: StartApp) -> None:
    app = await start_app(**FAST)
    assert app.queue is not None
    job_id = await app.enqueue_import("apply")
    assert job_id is not None
    status = await until_async(lambda: _status_if_final(app, job_id), timeout=15, what="import job dead")
    assert status == "dead"
    job = await app.queue.get(job_id)
    assert job is not None and "панель не подключена" in (job.last_error or "")
    with pytest.raises(ValueError, match="dry_run"):
        await app.enqueue_import("everything")


async def _status_if_final(app: App, job_id: int) -> str | None:
    assert app.queue is not None
    status = await app.queue.status_of(job_id)
    return status if status in ("dead", "done") else None


# ------------------------------------------------------------------------------------- panel events relay


async def test_owner_panel_events_are_posted_to_the_panel_topic() -> None:
    posted: list[tuple[str, str, dict[str, Any]]] = []

    async def post(kind: str, text: str, **kw: Any) -> None:
        posted.append((kind, text, kw))

    relay = PanelEventRelay(post)
    await relay.on_event(Event("remnawave.node.connection_lost", {"owner": True, "node": {"name": "NL <1>"}}))
    await relay.on_event(Event("remnawave.user.modified", {"owner": False}))
    await relay.on_event(Event("remnawave.version.detected", {"support": "full", "message": "ok"}))
    await relay.on_event(Event("remnawave.token.expiring", {"message": "Токен истекает через 3 дня"}))
    assert [k for k, _, _ in posted] == ["panel", "panel"]
    assert "NL &lt;1&gt;" in posted[0][1] and posted[0][2]["html"] is True
    assert "Токен истекает" in posted[1][1]
