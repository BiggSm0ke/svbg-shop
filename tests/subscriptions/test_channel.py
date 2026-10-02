"""Required channel: cache, ``chat_member`` updates, disable on leave / restore on return (06 M2)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.types import ChatMemberUpdated

from svbg.subscriptions.channel import ChannelService, Membership, bot_lookup, membership_of
from svbg.subscriptions.hold import freeze, unfreeze
from svbg.subscriptions.lifecycle import SubscriptionLifecycle
from svbg.subscriptions.service import Desired
from tests.fakes.telegram import FakeTelegram
from tests.subscriptions.kit import SyncEnv, plan_row, sync_env

CHANNEL = -100_777
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


class Lookup:
    def __init__(self, status: str = "member") -> None:
        self.status = status
        self.calls = 0
        self.fail = False

    async def __call__(self, chat_id: int, telegram_id: int) -> Membership:
        self.calls += 1
        if self.fail:
            raise TimeoutError
        return membership_of(self.status)


def service(env: SyncEnv, action: str = "trial", lookup: Lookup | None = None) -> tuple[ChannelService, dict]:
    cfg: dict[str, Any] = {"REQUIRED_CHANNEL_ID": CHANNEL, "CHANNEL_LEAVE_ACTION": action}

    def snapshot() -> Mapping[str, Any]:
        return cfg

    return ChannelService(env.db, config=snapshot, lookup=lookup), cfg


def update(user_id: int, old: str, new: str, at: datetime, chat_id: int = CHANNEL) -> ChatMemberUpdated:
    user = {"id": user_id, "is_bot": False, "first_name": "U"}

    def member(status: str) -> dict[str, Any]:
        return {"status": status, "user": user, **({"until_date": 0} if status == "kicked" else {})}

    return ChatMemberUpdated.model_validate(
        {
            "chat": {"id": chat_id, "type": "channel", "title": "News"},
            "from": user,
            "date": at,
            "old_chat_member": member(old),
            "new_chat_member": member(new),
        }
    )


async def trial_sub(env: SyncEnv, tg: int) -> int:
    uid = await env.add_user(tg)
    async with env.db.tx() as conn:
        sid = await SubscriptionLifecycle().service.create(
            conn,
            user_id=uid,
            telegram_id=tg,
            desired=Desired(expire_at=datetime.now(UTC) + timedelta(days=3), squads=[env.squad]),
            is_trial=True,
        )
    await env.drain()
    return sid


async def paid_sub(env: SyncEnv, tg: int) -> int:
    uid = await env.add_user(tg)
    async with env.db.tx() as conn:
        res = await SubscriptionLifecycle().purchase(
            conn, user_id=uid, terms=plan_row(env.squad), days=30, ref_id=f"o{tg}"
        )
    await env.drain()
    return res.subscription_id


def test_membership_of() -> None:
    assert membership_of("member").is_member and membership_of("creator").is_member
    assert membership_of("administrator").is_member
    assert not membership_of("left").is_member and not membership_of("kicked").is_member
    assert membership_of("restricted", True).is_member and not membership_of("restricted", False).is_member
    assert membership_of(None) == Membership("left", False)


async def test_leave_disables_trial_and_return_restores_it(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        channel, _ = service(env, "trial")
        sid = await trial_sub(env, 900)
        paid = await paid_sub(env, 901)
        assert not await channel.on_update(update(900, "left", "member", T0))  # join: nothing to restore
        assert await channel.on_update(update(900, "member", "left", T0 + timedelta(minutes=1)))
        assert not await channel.on_update(update(901, "member", "left", T0))  # paid: policy "trial"
        await env.drain()
        row = await env.sub(sid)
        assert row["disabled_reason"] == "channel_left"
        assert env.panel_user(row["panel_user_id"])["status"] == "DISABLED"
        assert env.panel_user((await env.sub(paid))["panel_user_id"])["status"] == "ACTIVE"
        # An update older than the known fact is ignored (out-of-order delivery).
        assert not await channel.on_update(update(900, "left", "member", T0))
        assert await channel.on_update(update(900, "left", "member", T0 + timedelta(minutes=2)))
        await env.drain()
        row = await env.sub(sid)
        assert row["disabled_reason"] is None
        assert env.panel_user(row["panel_user_id"])["status"] == "ACTIVE"
        kinds = [r["kind"] for r in await env.db.raw("select kind from subscription_events order by id")]
        assert "channel_left" in kinds and "channel_returned" in kinds
        names = {e.name for e in env.events}
        assert {"subscription.channel_left", "subscription.channel_returned"} <= names


async def test_policy_all_off_and_foreign_reasons(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        channel, cfg = service(env, "all")
        paid = await paid_sub(env, 902)
        assert await channel.on_update(update(902, "member", "kicked", T0))
        await env.drain()
        assert (await env.sub(paid))["disabled_reason"] == "channel_left"
        # An admin disabled it meanwhile: returning to the channel must not lift the admin's decision.
        await env.db.raw("update subscriptions set disabled_reason = 'admin' where id = $1", paid)
        assert not await channel.on_update(update(902, "kicked", "member", T0 + timedelta(minutes=1)))
        cfg["CHANNEL_LEAVE_ACTION"] = "off"
        other = await paid_sub(env, 903)
        assert not await channel.on_update(update(903, "member", "left", T0))
        await env.drain()
        assert (await env.sub(other))["disabled_reason"] is None
        # Other chats are not cached at all.
        assert not await channel.on_update(update(903, "member", "left", T0, chat_id=-5))
        assert len(await env.db.raw("select 1 from channel_members where chat_id = -5")) == 0


async def test_frozen_subscription_is_left_to_unfreeze(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        channel, _ = service(env, "trial")
        sid = await trial_sub(env, 904)
        async with env.db.tx() as conn:
            await freeze(conn, sid, "ip_guard", reason="тест")
        await env.drain()
        assert not await channel.on_update(update(904, "member", "left", T0))
        assert (await env.sub(sid))["disabled_reason"] == "ip_guard"
        async with env.db.tx() as conn:
            keep = await channel.keeps_disabled(conn, sid)
            assert keep == "channel_left"
            await unfreeze(conn, sid, keep_disabled=keep)
        await env.drain()
        assert (await env.sub(sid))["disabled_reason"] == "channel_left"
        assert await channel.on_update(update(904, "left", "member", T0 + timedelta(minutes=1)))
        await env.drain()
        row = await env.sub(sid)
        assert row["disabled_reason"] is None
        async with env.db.read() as conn:
            assert await channel.keeps_disabled(conn, sid) is None


async def test_is_member_cache_lookup_and_fallback(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        lookup = Lookup("member")
        channel, cfg = service(env, "trial", lookup)
        assert await channel.is_member(905) is True and lookup.calls == 1
        mark = env.db.queries
        assert await channel.is_member(905) is True and lookup.calls == 1
        assert env.db.queries - mark == 1  # a cache hit: one SQL, no Telegram call
        assert await channel.is_member(905, fresh=True) is True and lookup.calls == 2
        lookup.fail = True
        assert await channel.is_member(905, fresh=True) is True  # Telegram down: the cached answer
        assert await channel.is_member(906) is None  # nothing known
        channel.set_lookup(None)
        assert await channel.is_member(906) is None
        cfg["REQUIRED_CHANNEL_ID"] = None
        mark = env.db.queries
        assert await channel.is_member(906) is True and env.db.queries == mark


async def test_lookup_finds_a_missed_leave(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        lookup = Lookup("member")
        channel, _ = service(env, "trial", lookup)
        sid = await trial_sub(env, 907)
        assert await channel.is_member(907)
        lookup.status = "left"  # the chat_member update was lost (bot restarted, not an admin then…)
        assert await channel.is_member(907, fresh=True) is False
        await env.drain()
        assert (await env.sub(sid))["disabled_reason"] == "channel_left"


async def test_leave_update_in_the_same_second_as_a_lookup_is_applied(pg_dsn: str) -> None:
    """``ChatMemberUpdated.date`` has whole seconds; a ``getChatMember`` answer stored at 12:00:00.7 must not
    hide a leave dated 12:00:00 (it is the later fact: a transition delivered after the lookup)."""
    async with sync_env(pg_dsn) as env:
        channel, _ = service(env, "trial")
        sid = await trial_sub(env, 908)
        at = T0 + timedelta(milliseconds=700)
        assert not await channel.record(CHANNEL, 908, membership_of("member"), seen_at=at)
        assert await channel.on_update(update(908, "member", "left", T0))
        await env.drain()
        assert (await env.sub(sid))["disabled_reason"] == "channel_left"
        rows = await env.db.raw("select seen_at from channel_members where telegram_id = 908")
        assert rows[0]["seen_at"] == at  # the stamp never goes back
        # a fact older than the whole second is still ignored
        assert not await channel.on_update(update(908, "left", "member", T0 - timedelta(seconds=1)))


async def test_bot_lookup_against_fake_telegram() -> None:
    async with FakeTelegram() as tg:
        token = tg.add_bot()
        bot = Bot(token, session=AiohttpSession(api=TelegramAPIServer.from_base(tg.url)))
        try:
            lookup = bot_lookup(bot)
            assert (await lookup(CHANNEL, 42)) == Membership("member", True)
            tg.chat_members[(CHANNEL, 42)] = "left"
            assert (await lookup(CHANNEL, 42)) == Membership("left", False)
        finally:
            await bot.session.close()
