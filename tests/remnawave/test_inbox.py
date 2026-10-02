"""Webhook intake and inbox processing (02 §5.2–5.6, §8.2 A/C)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import statistics
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp
import pytest

from svbg.core import clock
from svbg.remnawave.inbox import InboxProcessor
from svbg.web.app import WebServer, build_web_app
from svbg.web.routes import remnawave as remnawave_route
from svbg.web.routes.remnawave import PATH, WebhookStats, looks_like_panel, remnawave_routes
from tests.fakes.remnawave import webhook_body, webhook_signature
from tests.subscriptions.kit import SyncEnv, sync_env

pytestmark = pytest.mark.timeout(90)

SECRET = "S3cr3tS3cr3tS3cr3tS3cr3tS3cr3tS3cr3tS3cr3tS3cr3tS3cr3tS3cr3tABCD"
OLD_SECRET = "0ldSecret0ldSecret0ldSecret0ldSecret0ldSecret0ldSecret0ldSecretX"


@dataclass
class Hook:
    env: SyncEnv
    url: str
    stats: WebhookStats
    processor: InboxProcessor
    session: aiohttp.ClientSession
    secrets: list[str]
    started: list[int]

    async def post(self, raw: bytes, *, secret: str = SECRET, headers: dict[str, str] | None = None) -> int:
        hdrs = {"Content-Type": "application/json", "X-Remnawave-Signature": webhook_signature(raw, secret)}
        hdrs.update(headers or {})
        async with self.session.post(self.url, data=raw, headers=hdrs) as resp:
            return resp.status

    async def send(
        self,
        scope: str,
        event: str,
        data: dict[str, Any],
        *,
        ts: datetime | None = None,
        secret: str = SECRET,
    ) -> tuple[int, bytes]:
        stamp = (ts or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        payload = {"scope": scope, "event": event, "timestamp": stamp, "data": data}
        raw = webhook_body(payload)
        return await self.post(raw, secret=secret, headers=panel_headers(stamp)), raw

    async def inbox(self) -> list[dict[str, Any]]:
        return [dict(r) for r in await self.env.db.raw("select * from rw_inbox order by ts, hash")]


@asynccontextmanager
async def hook_env(pg_dsn: str) -> AsyncIterator[Hook]:
    async with sync_env(pg_dsn) as env:
        stats = WebhookStats()
        started: list[int] = []

        async def on_started() -> None:
            started.append(1)

        processor = InboxProcessor(
            env.db,
            env.current_api,
            contributors=env.contributors,
            attention=env.attention,
            bus=env.bus,
            on_panel_started=on_started,
        )
        secrets = [SECRET, OLD_SECRET]
        app = build_web_app(
            remnawave_routes(db=env.db, secrets=lambda: secrets, on_stored=processor.wake, stats=stats)
        )
        server = WebServer("127.0.0.1", 0, app)
        await server.start()
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                yield Hook(env, server.url + PATH, stats, processor, session, secrets, started)
        finally:
            await server.stop()


def panel_headers(stamp: str) -> dict[str, str]:
    """What Remnawave adds to every delivery besides the signature (02 §5.1)."""
    return {"User-Agent": "Remnawave", "X-Remnawave-Timestamp": stamp}


def user_data(env: SyncEnv, pid: int) -> dict[str, Any]:
    return env.panel.user_json(env.panel.users[pid])


# ------------------------------------------------------------------------------------------------ intake


async def test_signature_on_raw_bytes_and_rotation(pg_dsn: str) -> None:
    async with hook_env(pg_dsn) as h:
        status, raw = await h.send(
            "node", "node.connection_lost", {"uuid": "n1", "name": "Нода «Амстердам» 🇳🇱"}
        )
        assert status == 200 and len(await h.inbox()) == 1
        # Re-serializing (spaces, ASCII escapes) changes the bytes → the panel's signature no longer fits.
        reencoded = json.dumps(json.loads(raw)).encode()
        assert (
            await h.post(reencoded, headers={"X-Remnawave-Signature": webhook_signature(raw, SECRET)}) == 401
        )
        assert len(await h.inbox()) == 1
        assert h.stats.bad_signature_foreign == 1 and h.stats.bad_signature == 0  # no panel headers


async def test_bad_secret_wrong_header_and_no_secret(pg_dsn: str) -> None:
    async with hook_env(pg_dsn) as h:
        status, _ = await h.send("node", "node.connection_restored", {"uuid": "n1"}, secret="x" * 64)
        assert status == 401 and h.stats.bad_signature == 1
        status, _ = await h.send("node", "node.connection_restored", {"uuid": "n2"}, secret=OLD_SECRET)
        assert status == 200, "предыдущий секрет принимается в окне ротации"
        assert await h.post(b'{"scope":"node"}', headers={"X-Remnawave-Signature": "zz"}) == 401
        h.secrets.clear()
        status, _ = await h.send("node", "node.connection_restored", {"uuid": "n3"})
        assert status == 401 and h.stats.no_secret == 1
        assert len(await h.inbox()) == 1


async def test_only_panel_like_rejections_count_as_a_secret_mismatch(pg_dsn: str) -> None:
    """Internet noise must not make the wizard say «секрет не совпадает» (it would push the owner to change a
    working secret): only a request that looks like Remnawave's own counts as ``bad_signature``."""
    async with hook_env(pg_dsn) as h:
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        raw = webhook_body({"scope": "node", "event": "node.modified", "timestamp": stamp, "data": {}})
        old = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        stale = webhook_body({"scope": "node", "event": "node.modified", "timestamp": old, "data": {}})
        foreign: list[tuple[bytes, dict[str, str]]] = [
            (raw, {}),  # no panel headers at all
            (raw, {"User-Agent": "curl/8.0", "X-Remnawave-Timestamp": stamp}),
            (raw, {"User-Agent": "Remnawave"}),  # no timestamp header
            (raw, panel_headers("2026-01-01T00:00:00.000Z")),  # header ≠ body timestamp
            (raw, panel_headers("not a date")),
            (b"garbage", panel_headers(stamp)),  # not an envelope
            (stale, panel_headers(old)),  # outside the acceptance window
        ]
        for body, headers in foreign:
            assert await h.post(body, secret="x" * 64, headers=headers) == 401
        assert h.stats.bad_signature == 0 and h.stats.bad_signature_foreign == len(foreign)
        assert await h.post(raw, secret="x" * 64, headers=panel_headers(stamp)) == 401
        assert h.stats.bad_signature == 1
        assert await h.inbox() == []


def test_looks_like_panel_is_safe_on_any_input() -> None:
    stamp = "2026-10-01T10:00:00.000Z"
    raw = webhook_body({"scope": "user", "event": "user.expired", "timestamp": stamp, "data": {}})
    clock.set_clock(datetime(2026, 10, 1, 10, 1, tzinfo=UTC))
    try:
        assert looks_like_panel(raw, panel_headers(stamp))
        assert looks_like_panel(raw, {"User-Agent": " remnawave ", "X-Remnawave-Timestamp": stamp})
        for body, headers in (
            (raw, {}),
            (b"", panel_headers(stamp)),
            (bytes([0xFF, 0xFE]), panel_headers(stamp)),
            (b"[1, 2]", panel_headers(stamp)),
            (raw, panel_headers("")),
            (raw, panel_headers("2026-10-01T10:00:00.001Z")),
        ):
            assert not looks_like_panel(body, headers)
    finally:
        clock.reset_clock()


def test_bad_signature_warnings_are_throttled(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    moment = [1000.0]
    monkeypatch.setattr(remnawave_route.time, "monotonic", lambda: moment[0])
    throttled = remnawave_route._Throttled(60.0)
    with caplog.at_level(logging.WARNING, logger="svbg.web.remnawave"):
        for _ in range(500):  # a flood within one minute: one line
            throttled.warning("rejected")
            moment[0] += 0.01
        moment[0] += 60
        throttled.warning("rejected")
    lines = [r.getMessage() for r in caplog.records]
    assert lines == ["rejected", "rejected (+499 more since the last message)"]


async def test_body_limits_malformed_and_stale(pg_dsn: str) -> None:
    async with hook_env(pg_dsn) as h:
        big = (
            b'{"scope":"node","event":"node.modified","timestamp":"2026-10-01T00:00:00Z","data":"'
            + b"x" * 300_000
            + b'"}'
        )
        assert await h.post(big) == 413
        assert await h.post(b"not json at all") == 400
        status, _ = await h.send(
            "node", "node.modified", {"uuid": "n"}, ts=datetime.now(UTC) - timedelta(days=8)
        )
        assert status == 200 and h.stats.stale == 1
        status, _ = await h.send(
            "node", "node.modified", {"uuid": "n"}, ts=datetime.now(UTC) + timedelta(minutes=10)
        )
        assert status == 200 and h.stats.stale == 2
        assert await h.inbox() == []


async def test_duplicate_bodies_are_processed_once(pg_dsn: str) -> None:
    async with hook_env(pg_dsn) as h:
        sid = await h.env.linked_sub(10)
        pid = (await h.env.sub(sid))["panel_user_id"]
        status, raw = await h.send("user", "user.modified", user_data(h.env, pid))
        assert status == 200
        for _ in range(2):  # the panel's retries: the very same bytes
            assert await h.post(raw) == 200
        assert len(await h.inbox()) == 1 and h.stats.duplicates == 2
        before = len(h.env.panel.calls(f"/users/{pid}", "GET"))
        assert await h.processor.run_once() == 1
        assert await h.processor.run_once() == 0
        assert len(h.env.panel.calls(f"/users/{pid}", "GET")) == before + 1
        assert (await h.inbox())[0]["status"] == "done"


async def test_intake_is_one_insert_and_never_calls_the_panel(pg_dsn: str) -> None:
    """The structural part of «< 50 ms» (02 §5.3): one INSERT, no GET/confirmation on the receive path."""
    async with hook_env(pg_dsn) as h:
        sid = await h.env.linked_sub(12)
        pid = (await h.env.sub(sid))["panel_user_id"]
        data = user_data(h.env, pid)
        h.env.panel.requests.clear()
        mark = h.env.db.queries
        status, _ = await h.send("user", "user.modified", data)
        assert status == 200
        assert h.env.db.queries - mark == 1, "приём вебхука — ровно один INSERT"
        assert h.env.panel.requests == [], "на приёме панель не вызывается"


@pytest.mark.slow
async def test_intake_answers_under_50_ms(pg_dsn: str) -> None:
    """Wall-clock check. Strict limits (median < 50 ms, p90 < 100 ms) only with ``SVBG_STRICT_TIMING=1`` on a
    quiet machine; by default only a gross regression fails (a loaded CI runner must not flake)."""
    strict = os.environ.get("SVBG_STRICT_TIMING") == "1"
    async with hook_env(pg_dsn) as h:
        await h.send("node", "node.modified", {"uuid": "warmup"})
        timings: list[float] = []
        for i in range(30):
            start = time.perf_counter()
            status, _ = await h.send("node", "node.modified", {"uuid": f"n{i}"})
            timings.append(time.perf_counter() - start)
            assert status == 200
        assert statistics.median(timings) < (0.05 if strict else 0.5), timings
        if strict:
            assert sorted(timings)[int(len(timings) * 0.9)] < 0.1, timings


async def test_secrets_never_reach_the_inbox(pg_dsn: str) -> None:
    async with hook_env(pg_dsn) as h:
        sid = await h.env.linked_sub(11)
        pid = (await h.env.sub(sid))["panel_user_id"]
        data = user_data(h.env, pid)
        assert data["trojanPassword"] and data["vlessUuid"]
        data["email"] = "person@example.com"
        await h.send("user", "user.modified", data)
        await h.send(
            "service",
            "service.login_attempt_failed",
            {"loginAttempt": {"username": "admin", "password": "hunter2", "ip": "1.2.3.4"}},
        )
        stored = json.dumps([r["slim"] for r in await h.inbox()])
        for secret in (
            data["trojanPassword"],
            data["vlessUuid"],
            data["ssPassword"],
            "hunter2",
            "person@example.com",
            "1.2.3.4",
        ):
            assert secret not in stored


# -------------------------------------------------------------------------------------------- processing


async def test_state_is_confirmed_by_get_before_projection(pg_dsn: str) -> None:
    async with hook_env(pg_dsn) as h:
        sid = await h.env.linked_sub(20)
        pid = (await h.env.sub(sid))["panel_user_id"]
        fake = user_data(h.env, pid)
        fake["status"] = "LIMITED"  # the webhook claims LIMITED, but the panel (truth) says ACTIVE
        await h.send("user", "user.limited", fake)
        await h.processor.run_once()
        row = await h.env.sub(sid)
        assert row["panel_status"] == "ACTIVE"
        assert h.processor.stats.confirmed == 1
        names = [e.name for e in h.env.events]
        assert "remnawave.user.limited" in names


async def test_panel_down_uses_newer_webhook_data_only(pg_dsn: str) -> None:
    async with hook_env(pg_dsn) as h:
        sid = await h.env.linked_sub(21)
        pid = (await h.env.sub(sid))["panel_user_id"]
        h.env.panel.inject("503", path=f"/users/{pid}", method="GET", times=None)
        data = user_data(h.env, pid)
        data["status"] = "LIMITED"
        # Two clocks (bot snapshot vs panel payload): an event within FALLBACK_SKEW of the snapshot is not
        # "newer" — only a clearly later one replaces the snapshot while the panel is down.
        await h.send("user", "user.limited", data)
        await h.processor.run_once()
        assert (await h.env.sub(sid))["panel_status"] == "ACTIVE"
        assert h.processor.stats.fallback == 0
        data["usedTrafficBytes"] = 1  # another body
        await h.send("user", "user.limited", data, ts=datetime.now(UTC) + timedelta(seconds=30))
        await h.processor.run_once()
        assert (await h.env.sub(sid))["panel_status"] == "LIMITED"
        assert h.processor.stats.fallback == 1
        old = dict(data, status="ACTIVE")
        await h.send("user", "user.enabled", old, ts=datetime.now(UTC) - timedelta(hours=1))
        await h.processor.run_once()
        assert (await h.env.sub(sid))["panel_status"] == "LIMITED", "старое событие не перетирает новое"


async def test_unknown_event_is_stored_and_skipped(pg_dsn: str) -> None:
    async with hook_env(pg_dsn) as h:
        status, _ = await h.send("future", "future.something_new", {"x": 1})
        assert status == 200
        await h.processor.run_once()
        rows = await h.inbox()
        assert [(r["event"], r["status"]) for r in rows] == [("future.something_new", "skipped")]


async def test_unknown_panel_user_is_skipped_and_announced(pg_dsn: str) -> None:
    async with hook_env(pg_dsn) as h:
        user = h.env.panel.add_user(username="manual_1", telegramId=4242)
        await h.send("user", "user.created", h.env.panel.user_json(user))
        await h.processor.run_once()
        assert (await h.inbox())[0]["status"] == "skipped"
        events = [e for e in h.env.events if e.name == "remnawave.unknown_user"]
        assert events and events[0].payload["telegram_id"] == 4242


async def test_deleted_in_panel_vs_our_delete_echo(pg_dsn: str) -> None:
    async with hook_env(pg_dsn) as h:
        sid = await h.env.linked_sub(30)
        pid = (await h.env.sub(sid))["panel_user_id"]
        data = user_data(h.env, pid)
        del h.env.panel.users[pid]  # an admin deleted it in the panel
        await h.send("user", "user.deleted", data)
        await h.processor.run_once()
        assert (await h.env.sub(sid))["link_state"] == "panel_missing"
        assert f"rw:panel_missing:{sid}" in await h.env.attention_keys()

        sid2 = await h.env.linked_sub(31)
        pid2 = (await h.env.sub(sid2))["panel_user_id"]
        data2 = user_data(h.env, pid2)
        async with h.env.db.tx() as conn:
            await h.env.service.close(conn, sid2, delete_in_panel=True)
        await h.env.drain()
        await h.send("user", "user.deleted", data2)
        await h.processor.run_once()
        assert (await h.env.sub(sid2))["link_state"] == "closed"
        assert f"rw:panel_missing:{sid2}" not in await h.env.attention_keys()


async def test_echo_of_our_write_is_flagged(pg_dsn: str) -> None:
    async with hook_env(pg_dsn) as h:
        sid = await h.env.linked_sub(32)
        pid = (await h.env.sub(sid))["panel_user_id"]
        async with h.env.db.tx() as conn:
            await h.env.service.renew(conn, sid, 30)
        await h.env.drain()
        await h.send("user", "user.modified", user_data(h.env, pid))
        await h.processor.run_once()
        event = next(e for e in h.env.events if e.name == "remnawave.user.modified")
        assert event.payload["echo"] is True
        row = await h.env.sub(sid)
        assert row["overrides"] == {}, "эхо своей записи — не ручная правка"


async def test_manual_edit_by_webhook_becomes_override(pg_dsn: str) -> None:
    async with hook_env(pg_dsn) as h:
        sid = await h.env.linked_sub(33, traffic_bytes=10)
        pid = (await h.env.sub(sid))["panel_user_id"]
        h.env.panel_user(pid)["hwidDeviceLimit"] = 9
        await h.send("user", "user.modified", user_data(h.env, pid))
        await h.processor.run_once()
        assert (await h.env.sub(sid))["overrides"] == {"device_limit": 9}
        event = next(e for e in h.env.events if e.name == "remnawave.user.modified")
        assert event.payload["echo"] is False


async def test_owner_events_go_to_the_bus_and_panel_start_triggers_sync(pg_dsn: str) -> None:
    async with hook_env(pg_dsn) as h:
        await h.send(
            "node",
            "node.connection_lost",
            {"uuid": "n1", "name": "NL-1", "isConnected": False, "address": "10.0.0.1"},
        )
        await h.send("service", "service.panel_started", {"panelVersion": "3.4.4"})
        await h.send(
            "service",
            "service.api_token_deleted",
            {"apiToken": {"uuid": "t1", "tokenName": "bot", "token": "SECRET"}},
        )
        await h.processor.run_once()
        await h.env.bus.drain(1)
        by_name = {e.name: e.payload for e in h.env.events}
        lost = by_name["remnawave.node.connection_lost"]
        assert lost["owner"] is True and lost["node"]["name"] == "NL-1" and "address" not in lost["node"]
        assert by_name["remnawave.service.api_token_deleted"]["apiToken"] == {
            "uuid": "t1",
            "tokenName": "bot",
        }
        assert h.started == [1]


async def test_failing_item_is_isolated_and_retried(pg_dsn: str) -> None:
    async with hook_env(pg_dsn) as h:
        sid = await h.env.linked_sub(40)
        pid = (await h.env.sub(sid))["panel_user_id"]
        h.env.panel.inject("500", path=f"/users/{pid}", method="GET", times=None)
        bad = user_data(h.env, pid)
        bad.pop("username")  # webhook fallback impossible → the item fails
        await h.send("user", "user.modified", bad)
        await h.send("node", "node.modified", {"uuid": "n1"})
        assert await h.processor.run_once() == 2
        rows = {r["event"]: r for r in await h.inbox()}
        assert rows["node.modified"]["status"] == "done"
        assert rows["user.modified"]["status"] == "new" and rows["user.modified"]["attempts"] == 1
        h.env.panel.clear_faults()
        await h.env.db.raw("update rw_inbox set locked_until = null")
        await h.processor.run_once()
        assert {r["event"]: r["status"] for r in await h.inbox()}["user.modified"] == "done"


async def test_background_loop_and_purge(pg_dsn: str) -> None:
    async with hook_env(pg_dsn) as h:
        await h.processor.start()
        try:
            await h.send("node", "node.created", {"uuid": "n9"})
            for _ in range(100):
                rows = await h.inbox()
                if rows and rows[0]["status"] == "done":
                    break
                await asyncio.sleep(0.05)
            assert (await h.inbox())[0]["status"] == "done"
        finally:
            await h.processor.stop()
        # After 72 h only the hash is kept (slim emptied); the row outlives the 7-day acceptance window.
        await h.env.db.raw("update rw_inbox set received_at = now() - interval '73 hours'")
        assert await h.processor.purge() == 0
        rows = await h.inbox()
        assert len(rows) == 1 and rows[0]["slim"] == {}
        await h.env.db.raw("update rw_inbox set received_at = now() - interval '8 days 6 hours'")
        assert await h.processor.purge() == 1


async def test_chunked_oversized_body_is_rejected(pg_dsn: str) -> None:
    async with hook_env(pg_dsn) as h:

        async def chunks() -> AsyncIterator[bytes]:
            for _ in range(5):
                yield b"x" * 65_536

        async with h.session.post(h.url, data=chunks(), headers={"X-Remnawave-Signature": "0" * 64}) as resp:
            assert resp.status == 413
        assert h.stats.too_large == 1 and await h.inbox() == []


def test_purge_and_sync_are_registered_with_the_scheduler() -> None:
    from svbg.remnawave.contributors import SquadContributors
    from svbg.remnawave.sync import Reconciler

    calls: list[tuple[str, float]] = []

    class FakeScheduler:
        def every(self, name: str, interval_s: float, fn: Any, **kwargs: Any) -> None:
            calls.append((name, interval_s))

    InboxProcessor(None, lambda: None, contributors=SquadContributors()).register(FakeScheduler())  # type: ignore[arg-type]
    Reconciler(None, lambda: None, contributors=SquadContributors()).register(FakeScheduler())  # type: ignore[arg-type]
    assert calls == [("remnawave.inbox.purge", 86_400), ("remnawave.sync", 300.0)]


async def test_mass_burst_is_replaced_by_one_reconciliation(pg_dsn: str) -> None:
    async with hook_env(pg_dsn) as h:
        sid = await h.env.linked_sub(60)
        pid = (await h.env.sub(sid))["panel_user_id"]
        passes: list[int] = []

        async def full_pass() -> None:
            passes.append(1)

        processor = InboxProcessor(
            h.env.db, h.env.current_api, contributors=h.env.contributors, on_mass=full_pass, mass_threshold=2
        )
        for i in range(3):
            data = user_data(h.env, pid)
            data["usedTrafficBytes"] = i  # distinct bodies
            await h.send("user", "user.modified", data, ts=datetime.now(UTC) + timedelta(milliseconds=i))
        gets = len(h.env.panel.calls(f"/users/{pid}", "GET"))
        await processor.run_once()
        assert passes == [1]
        assert len(h.env.panel.calls(f"/users/{pid}", "GET")) == gets, "вместо N GET — одна сверка"
        assert {r["status"] for r in await h.inbox()} == {"done"}


# ------------------------------------------------------------------------------------- review fixes


async def test_login_attempt_is_masked_and_never_reaches_the_admin_group(pg_dsn: str) -> None:
    """02 §5.4: an optional security alert, off by default. Admins type passwords into the login field."""
    from svbg.app import PanelEventRelay

    async with hook_env(pg_dsn) as h:
        await h.send(
            "service",
            "service.login_attempt_failed",
            {"loginAttempt": {"username": "P@ssw0rd123", "ip": "1.2.3.4", "userAgent": "x", "password": "y"}},
        )
        await h.processor.run_once()
        await h.env.bus.drain(1)
        stored = json.dumps([r["slim"] for r in await h.inbox()], ensure_ascii=False)
        assert "P@ssw0rd123" not in stored and "P@…(11)" in stored
        event = next(e for e in h.env.events if e.name == "remnawave.service.login_attempt_failed")
        assert event.payload["owner"] is False and event.payload["security"] is True
        assert "P@ssw0rd123" not in repr(event.payload)
        posted: list[Any] = []

        async def post(*args: Any, **kwargs: Any) -> None:
            posted.append(args)

        relay = PanelEventRelay(post)
        assert relay.render(event) is None
        await relay.on_event(event)
        assert posted == [], "в админ-группу не уходит"


def test_mask_login() -> None:
    from svbg.remnawave.inbox import mask_login

    assert mask_login("administrator") == "ad…(13)"
    assert mask_login("abc") == "…(3)"  # too short to show anything
    assert mask_login("") is None and mask_login(None) is None


async def test_replayed_body_inside_the_acceptance_window_stays_a_duplicate(pg_dsn: str) -> None:
    from svbg.remnawave.inbox import RETENTION
    from svbg.remnawave.webhooks import MAX_AGE, MAX_FUTURE

    assert RETENTION > MAX_AGE + MAX_FUTURE, "хэш живёт дольше окна приёма вебхуков"
    async with hook_env(pg_dsn) as h:
        sent_at = datetime.now(UTC) - timedelta(days=6)
        status, raw = await h.send("node", "node.connection_lost", {"uuid": "n1", "name": "NL"}, ts=sent_at)
        assert status == 200
        await h.processor.run_once()
        await h.env.db.raw("update rw_inbox set received_at = now() - interval '6 days'")
        await h.processor.purge()
        assert await h.post(raw) == 200  # an intercepted body replayed on day 6: still within 7 days
        assert h.stats.duplicates == 1
        rows = await h.inbox()
        assert len(rows) == 1 and rows[0]["status"] == "done"


async def test_mass_mode_counts_only_events_a_pass_replaces(pg_dsn: str) -> None:
    async with hook_env(pg_dsn) as h:
        sid = await h.env.linked_sub(61)
        pid = (await h.env.sub(sid))["panel_user_id"]
        passes: list[int] = []

        async def full_pass() -> None:
            passes.append(1)

        processor = InboxProcessor(
            h.env.db,
            h.env.current_api,
            contributors=h.env.contributors,
            on_mass=full_pass,
            mass_threshold=2,
            batch=1,
        )
        for i in range(6):
            data = user_data(h.env, pid)
            data["usedTrafficBytes"] = i
            await h.send("user", "user.expiration", data, ts=datetime.now(UTC) + timedelta(milliseconds=i))
        while await processor.run_once():
            pass
        assert passes == [], "уведомления user.expiration не запускают полную сверку"
        assert {r["status"] for r in await h.inbox()} == {"done"}


@pytest.mark.parametrize("outcome", ["busy", "aborted", "raises"])
async def test_mass_pass_that_did_not_finish_ok_replaces_nothing(pg_dsn: str, outcome: str) -> None:
    from svbg.remnawave.sync import SyncReport

    async with hook_env(pg_dsn) as h:
        sid = await h.env.linked_sub(62)
        pid = (await h.env.sub(sid))["panel_user_id"]
        passes: list[int] = []

        async def full_pass() -> SyncReport:
            passes.append(1)
            if outcome == "raises":
                raise RuntimeError("панель недоступна (тест)")
            return SyncReport("full", "webhooks", status=outcome)

        processor = InboxProcessor(
            h.env.db,
            h.env.current_api,
            contributors=h.env.contributors,
            bus=h.env.bus,
            on_mass=full_pass,
            mass_threshold=2,
        )
        for i in range(3):
            data = user_data(h.env, pid)
            data["usedTrafficBytes"] = i
            await h.send("user", "user.modified", data, ts=datetime.now(UTC) + timedelta(milliseconds=i))
        await h.send("node", "node.connection_lost", {"uuid": "n1", "name": "NL-1"})
        assert await processor.run_once() == 4, "события обработаны обычным путём, владелец не ждёт"
        assert passes == [1]
        rows = await h.inbox()
        assert {r["status"] for r in rows} == {"done"}
        assert all(r["note"] != "обработано сверкой" for r in rows)
        assert any(e.name == "remnawave.node.connection_lost" for e in h.env.events)
        assert len(h.env.panel.calls(f"/users/{pid}", "GET")) >= 1, "подтверждено GET, а не выброшено"
        # Mass mode pauses after an attempt: a new burst right away does not hammer the reconciler.
        for i in range(3):
            data = user_data(h.env, pid)
            data["usedTrafficBytes"] = 10 + i
            await h.send("user", "user.modified", data)
        await processor.run_once()
        assert passes == [1]


async def test_mass_pass_marks_only_events_received_before_it(pg_dsn: str) -> None:
    async with hook_env(pg_dsn) as h:
        sid = await h.env.linked_sub(63)
        pid = (await h.env.sub(sid))["panel_user_id"]
        late: list[bytes] = []

        async def full_pass() -> None:
            data = user_data(h.env, pid)
            data["usedTrafficBytes"] = 999  # arrives while the stream is already past this user
            _, raw = await h.send("user", "user.limited", data)
            late.append(raw)

        processor = InboxProcessor(
            h.env.db, h.env.current_api, contributors=h.env.contributors, on_mass=full_pass, mass_threshold=2
        )
        for i in range(3):
            data = user_data(h.env, pid)
            data["usedTrafficBytes"] = i
            await h.send("user", "user.modified", data, ts=datetime.now(UTC) - timedelta(seconds=10 - i))
        gets = len(h.env.panel.calls(f"/users/{pid}", "GET"))
        await processor.run_once()
        rows = {r["event"]: r for r in await h.inbox()}
        assert rows["user.modified"]["note"] == "обработано сверкой"
        assert (
            rows["user.limited"]["status"] == "done" and rows["user.limited"]["note"] != "обработано сверкой"
        )
        assert len(h.env.panel.calls(f"/users/{pid}", "GET")) == gets + 1, "позднее событие подтверждено GET"


async def test_our_api_token_deleted_opens_the_breaker(pg_dsn: str) -> None:
    from svbg.remnawave.capabilities import jwt_claims
    from svbg.remnawave.inbox import ATTENTION_API_GONE
    from svbg.remnawave.transport import BreakerState

    async with hook_env(pg_dsn) as h:
        breaker = h.env.api.transport.breaker
        ours = (jwt_claims(h.env.api.transport.config.token) or {})["uuid"]
        foreign = "11111111-2222-4333-8444-555555555555"
        await h.send("service", "service.api_token_deleted", {"apiToken": {"uuid": foreign, "name": "other"}})
        await h.processor.run_once()
        assert breaker.state is BreakerState.CLOSED
        assert ATTENTION_API_GONE not in await h.env.attention_keys()
        await h.send("service", "service.api_token_deleted", {"apiToken": {"uuid": ours, "name": "svbg-bot"}})
        await h.processor.run_once()
        await h.env.bus.drain(1)
        assert breaker.state is BreakerState.OPEN, "запросы остановлены сразу, а не по первому 401"
        item = await h.env.attention.get(ATTENTION_API_GONE)
        assert item is not None and item.severity == "error" and item.fix_action == "setting:REMNAWAVE_TOKEN"
        assert "svbg-bot" in item.body
        flags = [e.payload["ours"] for e in h.env.events if e.name == "remnawave.service.api_token_deleted"]
        assert flags == [False, True]
        breaker.reset()


async def test_events_of_a_closed_subscription_are_skipped(pg_dsn: str) -> None:
    async with hook_env(pg_dsn) as h:
        sid = await h.env.linked_sub(64)
        async with h.env.db.tx() as conn:
            await h.env.service.close(conn, sid)
        await h.env.drain()
        pid = (await h.env.sub(sid))["panel_user_id"]
        gets = len(h.env.panel.calls(f"/users/{pid}", "GET"))
        await h.send("user", "user.expired", user_data(h.env, pid))
        await h.processor.run_once()
        assert (await h.inbox())[0]["status"] == "skipped"
        assert len(h.env.panel.calls(f"/users/{pid}", "GET")) == gets
        assert not [e for e in h.env.events if e.name == "remnawave.user.expired"]
