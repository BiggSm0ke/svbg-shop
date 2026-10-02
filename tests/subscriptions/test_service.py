"""Minimal subscription service and table constraints (stage 1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from svbg.remnawave.models import FOREVER
from svbg.subscriptions.service import Desired, SubscriptionService, make_username
from tests.subscriptions.kit import sync_env

SOON = datetime.now(UTC) + timedelta(days=3)


def test_make_username() -> None:
    assert make_username("sv_", 123456789) == "sv_123456789"
    assert make_username("rs_", 42, 3) == "rs_42_3"
    assert make_username("sv_", None, fallback="a1b2c3") == "sv_a1b2c3"
    assert len(make_username("sv_", 99_999_999_999, 99)) <= 36
    for bad in ("", "with space", "x" * 13):
        with pytest.raises(ValueError):
            make_username(bad, 1)
    with pytest.raises(ValueError):
        make_username("sv_", None)
    with pytest.raises(ValueError):
        SubscriptionService(username_prefix="bad prefix")


def test_prefix_follows_the_setting() -> None:
    """``PANEL_USERNAME_PREFIX`` is live: a service without an explicit prefix reads it on every create."""
    from svbg.subscriptions.service import configure_username_prefix

    cfg = {"PANEL_USERNAME_PREFIX": "user_"}
    service, pinned = SubscriptionService(), SubscriptionService(username_prefix="rs_")
    try:
        configure_username_prefix(lambda: cfg["PANEL_USERNAME_PREFIX"])
        assert (service.prefix, pinned.prefix) == ("user_", "rs_")
        cfg["PANEL_USERNAME_PREFIX"] = "bad prefix"  # never valid in the registry; ignored here too
        assert service.prefix == "sv_"
    finally:
        configure_username_prefix(None)
    assert service.prefix == "sv_"


def test_desired_validation() -> None:
    with pytest.raises(ValueError, match="сквад"):
        Desired(expire_at=SOON, squads=[])
    with pytest.raises(ValueError):
        Desired(expire_at=datetime(2030, 1, 1), squads=["a"])
    with pytest.raises(ValueError):
        Desired(expire_at=SOON, squads=["a"], device_limit=-1)
    cols = Desired(expire_at=datetime(2200, 1, 1, tzinfo=UTC), squads=["a", "a", "b"]).columns()
    assert cols["desired_expire_at"] == FOREVER and cols["desired_squads"] == ["a", "b"]


async def test_create_change_and_job_contract(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        first = await env.new_sub(321)
        async with env.db.tx() as conn:
            second = await env.service.create(
                conn, user_id=None, telegram_id=321, desired=Desired(expire_at=SOON, squads=[env.squad])
            )
            unclaimed = await env.service.create(
                conn, user_id=None, telegram_id=None, desired=Desired(expire_at=SOON, squads=[env.squad])
            )
        assert (await env.sub(first))["panel_username"] == "sv_321"
        assert (await env.sub(second))["panel_username"] == "sv_321_2"
        assert (await env.sub(unclaimed))["panel_username"].startswith("sv_")
        jobs = await env.jobs()
        assert {(j["kind"], j["ordering_key"], j["lane"]) for j in jobs} == {
            ("panel.create", f"sub:{first}", "interactive"),
            ("panel.create", f"sub:{second}", "interactive"),
            ("panel.create", f"sub:{unclaimed}", "interactive"),
        }
        assert all(j["max_attempts"] >= 50 for j in jobs)
        async with env.db.tx() as conn:
            with pytest.raises(ValueError):
                await env.service.change(conn, first, desired_squads=[])
            with pytest.raises(ValueError):
                await env.service.change(conn, first, panel_status="ACTIVE")
            await env.service.change(conn, first, desired_traffic_bytes=1, desired_expire_at=SOON)
        update = (await env.jobs("panel.update"))[0]
        assert update["payload"] == {"sub_id": first, "fields": ["expire", "traffic"]}
        row = await env.sub(first)
        assert row["paid_until"] == row["desired_expire_at"]


async def test_rolled_back_business_change_leaves_no_job(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        sid = await env.new_sub(1)
        with pytest.raises(RuntimeError):
            async with env.db.tx() as conn:
                await env.service.renew(conn, sid, 30)
                raise RuntimeError("платёж откатился")
        assert [j["kind"] for j in await env.jobs()] == ["panel.create"]


async def test_close_without_panel_delete(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        sid = await env.linked_sub(2)
        async with env.db.tx() as conn:
            await env.service.close(conn, sid)
        await env.drain()
        row = await env.sub(sid)
        assert row["disabled_reason"] == "closed"
        assert env.panel_user(row["panel_user_id"])["status"] == "DISABLED"


async def test_table_constraints(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        bad = (
            "insert into subscriptions (link_state) values ('weird')",
            "insert into subscriptions (link_state) values ('linked')",  # linked needs panel_user_id
            "insert into subscriptions (desired_squads) values ('{}'::jsonb)",
            "insert into subscriptions (desired_device_limit) values (-1)",
            "insert into subscriptions (panel_user_id) values (0)",
        )
        for sql in bad:
            with pytest.raises(asyncpg.CheckViolationError):
                await env.db.raw(sql)
        await env.db.raw("insert into subscriptions (panel_user_id, panel_short_uuid) values (5, 'abc')")
        with pytest.raises(asyncpg.UniqueViolationError):
            await env.db.raw("insert into subscriptions (panel_user_id) values (5)")
        with pytest.raises(asyncpg.UniqueViolationError):
            await env.db.raw("insert into subscriptions (panel_short_uuid) values ('abc')")
        sid = (await env.db.raw("select id from subscriptions"))[0]["id"]
        await env.db.raw(
            "insert into panel_squad_substitutions "
            "(subscription_id, base_squad_uuid, substitute_squad_uuid, "
            "owner_module) values ($1, 'b', 't', 'lte')",
            sid,
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            await env.db.raw(
                "insert into panel_squad_substitutions "
                "(subscription_id, base_squad_uuid, substitute_squad_uuid, "
                "owner_module) values ($1, 'b', 't2', 'lte')",
                sid,
            )
        public = await env.db.raw("select public_id from subscriptions")
        assert len(public[0]["public_id"]) == 36
