"""Plan terms parsing, durable subscription events and the stage 2 table constraints."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import asyncpg
import pytest

from svbg.core.bus import Event
from svbg.jobs.queue import Job, JobQueue
from svbg.jobs.worker import JobContext, PermanentJobError
from svbg.subscriptions.hooks import HOOK_KIND, HOOK_QUEUE, EventRelay, emit
from svbg.subscriptions.terms import PlanTerms, TrialPlanSource
from tests.subscriptions.kit import FakeCatalog, plan_row, sync_env


def test_plan_terms_from_catalog_row() -> None:
    terms = PlanTerms.from_snapshot(plan_row("sq", 4, panel_tag="VIP", squads=["sq", "sq", "x"]))
    assert terms.plan_id == 4 and terms.squads == ("sq", "x") and terms.max_devices == 15
    assert terms.addon_available and terms.device_limit_with(3) == 8
    snap = terms.to_snapshot()
    assert snap["name"] == {"ru": "Тариф 4"} and snap["panel_tag"] == "VIP"  # catalog keys survive
    assert PlanTerms.from_snapshot(snap) == terms
    assert PlanTerms.from_snapshot(terms) is terms
    unlimited = PlanTerms.from_snapshot(plan_row("sq", device_limit=0))
    assert not unlimited.addon_available and unlimited.device_limit_with(5) == 0
    fallback = PlanTerms.from_snapshot(plan_row("sq", device_limit=None, device_addon=None))
    assert fallback.device_limit_with(0) is None and not fallback.addon_available
    off = PlanTerms.from_snapshot(plan_row("sq", device_addon={"enabled": False, "max_devices": 15}))
    assert off.max_devices is None and not off.addon_available
    uncapped = PlanTerms.from_snapshot(plan_row("sq", device_addon={"price_minor": 1900, "per_days": 30}))
    assert uncapped.addon_available and uncapped.max_devices is None
    below = PlanTerms.from_snapshot(plan_row("sq", device_addon={"price_minor": 1900, "max_devices": 3}))
    assert not below.addon_available  # a cap under the included limit: nothing to sell, never a crash


@pytest.mark.parametrize(
    ("override", "error"),
    [
        ({"squads": []}, ValueError),
        ({"squads": "sq"}, TypeError),
        ({"traffic_bytes": -1}, ValueError),
        ({"reset_strategy": "YEAR"}, ValueError),
        ({"device_limit": -1}, ValueError),
        ({"device_limit": "5"}, TypeError),
        ({"device_addon": {"max_devices": -3}}, ValueError),
        ({"panel_tag": "X" * 17}, ValueError),
        ({"id": True}, TypeError),
    ],
)
def test_plan_terms_reject_broken_plans(override: dict[str, object], error: type[Exception]) -> None:
    with pytest.raises(error):
        PlanTerms.from_snapshot(plan_row("sq", **override))


def test_plan_terms_misc() -> None:
    with pytest.raises(TypeError):
        PlanTerms.from_snapshot(["not", "a", "mapping"])  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        PlanTerms.from_snapshot(plan_row("sq")).device_limit_with(-1)
    assert isinstance(FakeCatalog(), TrialPlanSource)


async def test_events_are_durable_and_transactional(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        with pytest.raises(RuntimeError):
            async with env.db.tx() as conn:
                await emit(conn, "subscription.term_changed", {"subscription_id": 1})
                raise RuntimeError("business change failed")
        assert not await env.db.raw("select 1 from jobs where queue = $1", HOOK_QUEUE)
        async with env.db.tx() as conn:
            with pytest.raises(ValueError):
                await emit(conn, "Bad Name", {})
            await emit(conn, "trial.activated", {"subscription_id": 7, "at": None})
        rows = await env.db.raw("select kind, lane from jobs where queue = $1", HOOK_QUEUE)
        assert [(r["kind"], r["lane"]) for r in rows] == [(HOOK_KIND, "background")]
        await env.drain()
        assert [e.payload["subscription_id"] for e in env.events if e.name == "trial.activated"] == [7]


async def test_relay_rejects_a_corrupted_job(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        relay = EventRelay(env.bus).handlers()[HOOK_KIND]
        ctx = JobContext(db=env.db, queue=JobQueue(env.db), worker_id="w")
        job = Job(
            id=1,
            queue=HOOK_QUEUE,
            lane="background",
            kind=HOOK_KIND,
            payload={"event": 5},
            attempts=1,
            max_attempts=5,
            ordering_key=None,
            dedup_key=None,
            caused_by=None,
            locked_by="w",
            locked_until=None,
            next_run_at=datetime.now(UTC),
            created_at=datetime.now(UTC),
        )
        with pytest.raises(PermanentJobError):
            await relay(job, ctx)
        seen: list[Event] = []

        async def boom(event: Event) -> None:
            seen.append(event)
            raise RuntimeError("subscriber bug")

        env.bus.subscribe("trial.activated", boom)
        ok = dataclasses.replace(job, payload={"event": "trial.activated", "payload": {"x": 1}})
        await relay(ok, ctx)  # a failing subscriber never fails the relay
        assert seen and seen[0].payload["x"] == 1


async def test_stage2_constraints(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        bad = (
            "insert into subscriptions (hold_kind, hold_since) values ('vacation', now())",
            "insert into subscriptions (hold_kind) values ('admin')",  # hold needs hold_since
            "insert into subscriptions (extra_devices) values (-1)",
            "insert into subscriptions (cooldowns) values ('[]'::jsonb)",
            "insert into subscriptions (disabled_reason) values ('whatever')",
            "insert into trial_grants (source) values ('bot')",  # neither user nor telegram id
        )
        for sql in bad:
            with pytest.raises(asyncpg.CheckViolationError):
                await env.db.raw(sql)
        await env.db.raw("insert into subscriptions (disabled_reason) values ('hold')")
        await env.db.raw("insert into trial_grants (telegram_id) values (42)")
        with pytest.raises(asyncpg.UniqueViolationError):
            await env.db.raw("insert into trial_grants (telegram_id) values (42)")
        uid = await env.add_user(42)
        await env.db.raw("insert into trial_grants (user_id) values ($1)", uid)
        with pytest.raises(asyncpg.UniqueViolationError):
            await env.db.raw("insert into trial_grants (user_id) values ($1)", uid)


async def test_catalog_plan_and_trial_source_interop() -> None:
    from types import SimpleNamespace

    from svbg.catalog.model import DeviceAddon, Plan
    from svbg.subscriptions.terms import CatalogTrialSource

    plan = Plan(
        id=3,
        code="m1",
        name={"ru": "Месяц"},
        enabled=True,
        device_limit=5,
        squads=("sq",),
        device_addon=DeviceAddon(1900, 30, 15),
    )
    terms = PlanTerms.from_snapshot(plan)
    assert terms.plan_id == 3 and terms.addon_available and terms.max_devices == 15
    assert PlanTerms.from_snapshot(plan.snapshot()) == terms
    assert PlanTerms.from_snapshot(terms.to_snapshot()) == terms
    trial = Plan(id=4, code="trial", name={"ru": "Пробный"}, is_trial=True, enabled=True, squads=("sq",))
    source = CatalogTrialSource(SimpleNamespace(snapshot=SimpleNamespace(trial=trial)))
    assert isinstance(source, TrialPlanSource)
    got = await source.trial_plan(None)  # type: ignore[arg-type]
    assert got is not None and PlanTerms.from_snapshot(got).is_trial
    assert (
        await CatalogTrialSource(SimpleNamespace(snapshot=SimpleNamespace(trial=None))).trial_plan(None)
        is None
    )  # type: ignore[arg-type]
