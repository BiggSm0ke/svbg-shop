"""User notifications: dedup by ``notification_log(target, kind, anchor)`` across the panel's webhooks and the
fallback scanner, owner toggles, skip rules at send time, rendering with the next step's button."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from svbg.core.bus import Event
from svbg.core.clock import now
from svbg.services.notify_user import base_kind, epoch_anchor
from tests.tg.user.kit import UserEnv, build_user_env


async def _paid_sub(env: UserEnv) -> tuple[int, int, int]:
    uid, tg = await env.new_user(balance=20_000)
    await env.open(tg)
    await env.press(tg, "Подписка")
    await env.press(tg, "Купить подписку")
    await env.press(tg, "1 мес.")
    await env.press(tg, "Оплатить")
    await env.drain()
    sid = (await env.rows("select id from subscriptions where user_id = $1", uid))[0]["id"]
    return uid, tg, sid


async def _set_until(env: UserEnv, sid: int, delta: timedelta, *, trial: bool | None = None) -> None:
    await env.db.raw("update subscriptions set paid_until = $1 where id = $2", now() + delta, sid)
    if trial is not None:
        await env.db.raw("update subscriptions set is_trial = $1 where id = $2", trial, sid)


async def _log(env: UserEnv) -> list[dict[str, Any]]:
    return await env.rows("select kind, anchor, status, reason from notification_log order by id")


def _texts(env: UserEnv, tg: int) -> list[str]:
    return [m.text for (c, _), m in sorted(env.tg.messages.items()) if c == tg]


async def test_scanner_reminds_once_and_the_panel_event_does_not_double_it(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn) as env:
        _uid, tg, sid = await _paid_sub(env)
        await _set_until(env, sid, timedelta(hours=20))
        assert await env.path.notifications.scan() == 1
        assert await env.path.notifications.scan() == 0
        await env.path.notifications.on_event(
            Event("remnawave.user.expiration", {"subscription_id": sid, "meta": {"expiration": -24}})
        )
        await env.drain()
        log = await _log(env)
        assert [(r["kind"], r["status"]) for r in log] == [("expiring_24h", "sent")]
        msg = env.tg.last(tg)
        assert msg.text.startswith("⏳ Подписка закончится через 20 ч")
        assert msg.data("Продлить") == "v1:buy:o"


async def test_renewal_starts_a_fresh_set_and_stale_rows_are_skipped(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn) as env:
        _uid, _tg, sid = await _paid_sub(env)
        await _set_until(env, sid, timedelta(hours=60))
        assert await env.path.notifications.scan() == 1  # expiring_72h
        await _set_until(env, sid, timedelta(days=40))  # renewed before the job ran
        await env.drain()
        assert [(r["kind"], r["status"], r["reason"]) for r in await _log(env)] == [
            ("expiring_72h", "skipped", "renewed")
        ]
        await _set_until(env, sid, timedelta(hours=10))
        assert await env.path.notifications.scan() == 1  # a new paid_until → a new anchor
        await env.drain()
        assert [r["kind"] for r in await _log(env)] == ["expiring_72h", "expiring_24h"]


async def test_trial_gets_only_the_two_hour_warning(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn) as env:
        _uid, tg = await env.new_user()
        await env.open(tg)
        await env.press(tg, "Попробовать")
        await env.drain()
        sid = (await env.rows("select id from subscriptions"))[0]["id"]
        assert await env.path.notifications.scan() == 0  # 3 days left: no «expiring» for a trial
        await _set_until(env, sid, timedelta(minutes=90))
        assert await env.path.notifications.scan() == 1
        await env.drain()
        assert [r["kind"] for r in await _log(env)] == ["trial_ending"]
        msg = env.tg.last(tg)
        assert "Пробный период закончится через 2 ч" in msg.text
        assert msg.data("Купить подписку") == "v1:buy:o"


async def test_expired_within_a_day_only_and_never_when_renewed(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn) as env:
        _uid, tg, sid = await _paid_sub(env)
        await _set_until(env, sid, -timedelta(days=3))
        assert await env.path.notifications.scan() == 0
        await _set_until(env, sid, -timedelta(hours=2))
        assert await env.path.notifications.scan() == 1
        # the panel says «expired» for the same fact: no second message
        await env.path.notifications.on_event(Event("remnawave.user.expired", {"subscription_id": sid}))
        await env.drain()
        assert [(r["kind"], r["status"]) for r in await _log(env)] == [("expired", "sent")]
        assert "Подписка закончилась" in env.tg.last(tg).text


async def test_toggles_switch_kinds_off(pg_dsn: str) -> None:
    async with build_user_env(
        pg_dsn, config={"NOTIFY_USER_EXPIRING": False, "NOTIFY_USER_FIRST_CONNECTED": False}
    ) as env:
        _uid, _tg, sid = await _paid_sub(env)
        await _set_until(env, sid, timedelta(hours=20))
        assert await env.path.notifications.scan() == 0
        await env.path.notifications.on_event(
            Event("remnawave.user.first_connected", {"subscription_id": sid})
        )
        assert await _log(env) == []


async def test_panel_events_first_connected_device_revoked_traffic(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn) as env:
        _uid, tg, sid = await _paid_sub(env)
        n = env.path.notifications
        for _ in range(2):
            await n.on_event(Event("remnawave.user.first_connected", {"subscription_id": sid}))
        device = {"hwid": "hw-1", "deviceModel": "iPhone 15", "platform": "iOS", "createdAt": "2026-10-01"}
        await n.on_event(
            Event("remnawave.user_hwid_devices.added", {"subscription_id": sid, "device": device})
        )
        await n.on_event(Event("remnawave.user.revoked", {"subscription_id": sid, "echo": True, "ts": "a"}))
        await n.on_event(Event("remnawave.user.revoked", {"subscription_id": sid, "echo": False, "ts": "b"}))
        await env.db.raw(
            "update subscriptions set panel_used_traffic = 85 * 1073741824::bigint, "
            "panel_traffic_limit = 100 * 1073741824::bigint where id = $1",
            sid,
        )
        await n.on_event(Event("remnawave.user.bandwidth_usage_threshold_reached", {"subscription_id": sid}))
        await n.on_event(Event("remnawave.unknown_event", {"subscription_id": sid}))
        await env.drain()
        log = await _log(env)
        assert [r["kind"] for r in log] == ["first_connected", "device_added", "revoked", "traffic"]
        assert all(r["status"] == "sent" for r in log)
        texts = _texts(env, tg)
        assert any(t.startswith("✅ Подключение работает") for t in texts)
        assert any("Новое устройство: iPhone 15 · iOS" in t for t in texts)
        assert any("Использовано 80% трафика" in t and "85 GB из 100 GB" in t for t in texts)
        revoked = next(m for m in env.tg.messages.values() if "Ссылка подписки обновлена" in m.text)
        assert revoked.button("Подключиться").web_app is not None
        device_msg = next(m for m in env.tg.messages.values() if "Новое устройство" in m.text)
        assert device_msg.data("Это не я") == "v1:dev:o"


async def test_blocked_and_banned_users_are_skipped(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn) as env:
        uid, tg, sid = await _paid_sub(env)
        env.tg.blocked.add(tg)
        await env.path.notifications.on_event(
            Event("remnawave.user.first_connected", {"subscription_id": sid})
        )
        await env.drain()
        assert [(r["status"], r["reason"]) for r in await _log(env)] == [("skipped", "not delivered")]
        await env.db.raw("update users set banned_at = now() where id = $1", uid)
        device = {"hwid": "hw-2", "createdAt": "x"}
        await env.path.notifications.on_event(
            Event("remnawave.user_hwid_devices.added", {"subscription_id": sid, "device": device})
        )
        await env.drain()
        assert (await _log(env))[-1]["reason"] == "banned"


async def test_garbage_events_and_helpers(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn) as env:
        n = env.path.notifications
        await n.on_event(Event("remnawave.user.expired", {"subscription_id": "1"}))
        await n.on_event(Event("remnawave.user.expired", {"subscription_id": 999_999}))
        await n.on_event(Event("remnawave.user.expiration", {"subscription_id": 999_999, "meta": {}}))
        assert await _log(env) == []
        assert base_kind("expiring_72h") == "expiring" and base_kind("revoked") == "revoked"
        assert epoch_anchor(None) == "0"
        assert await n.purge() == 0


async def test_expiration_event_respects_a_later_local_expiry(pg_dsn: str) -> None:
    async with build_user_env(pg_dsn) as env:
        _uid, _tg, sid = await _paid_sub(env)  # paid_until ≈ now + 30 days
        await env.path.notifications.on_event(
            Event("remnawave.user.expiration", {"subscription_id": sid, "meta": {"expiration": -24}})
        )
        assert await _log(env) == []  # the bot knows the subscription was renewed: no reminder
