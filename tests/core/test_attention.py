from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from svbg.core import clock
from svbg.core.attention import AttentionService, RaiseOutcome
from svbg.core.bus import ALL, Event, EventBus
from svbg.core.component import Health, HealthReport
from svbg.core.tables import admin_audit, attention_items, config_meta, user_identities, users
from svbg.db.engine import Database
from svbg.db.schema import create_schema

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
UUID7_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
BOT_TOKEN = "123456789:AAH" + "x" * 32


@pytest.fixture
def fc() -> Iterator[clock.FrozenClock]:
    frozen = clock.FrozenClock(T0)
    clock.set_clock(frozen)
    yield frozen
    clock.reset_clock()


@pytest.fixture
async def db(pg_dsn: str) -> AsyncIterator[Database]:
    await create_schema(pg_dsn)
    database = Database(pg_dsn, pool_size=5)
    await database.start()
    yield database
    await database.close()


@pytest.fixture
def svc(db: Database, fc: clock.FrozenClock) -> AttentionService:
    return AttentionService(db)


async def _count(db: Database) -> int:
    async with db.read() as conn:
        return (await conn.execute(sa.select(sa.func.count()).select_from(attention_items))).scalar_one()


# -- raise / dedup / reopen ----------------------------------------------------------------------


async def test_raise_creates_then_updates_same_row(svc: AttentionService, db: Database, fc) -> None:
    r1 = await svc.raise_item(
        "component:remnawave", "warn", "Панель медленная", "p95 900 мс", "screen:status"
    )
    assert r1.outcome is RaiseOutcome.CREATED and r1.outcome.notify
    item = r1.item
    assert (item.severity, item.title, item.body, item.fix_action) == (
        "warn",
        "Панель медленная",
        "p95 900 мс",
        "screen:status",
    )
    assert item.created_at == item.updated_at == T0
    assert item.is_open and not item.is_snoozed()

    fc.advance(minutes=5)
    r2 = await svc.raise_item("component:remnawave", "warn", "Панель медленная", "p95 1200 мс")
    assert r2.outcome is RaiseOutcome.UPDATED and not r2.outcome.notify
    assert r2.item.id == item.id
    assert r2.item.body == "p95 1200 мс"
    assert r2.item.fix_action is None
    assert r2.item.created_at == T0
    assert r2.item.updated_at == T0 + timedelta(minutes=5)
    assert await _count(db) == 1


async def test_resolve_and_reopen(svc: AttentionService, fc) -> None:
    first = (await svc.raise_item("jobs:dead", "error", "Задачи упали")).item
    fc.advance(hours=1)
    assert await svc.resolve("jobs:dead") is True
    assert await svc.resolve("jobs:dead") is False
    assert await svc.resolve("never:raised") is False
    resolved = await svc.get("jobs:dead")
    assert resolved is not None and resolved.resolved_at == T0 + timedelta(hours=1)
    assert await svc.open_items() == []

    fc.advance(hours=1)
    r = await svc.raise_item("jobs:dead", "warn", "Задачи упали снова")
    assert r.outcome is RaiseOutcome.REOPENED
    assert r.item.id == first.id
    assert r.item.resolved_at is None
    assert r.item.created_at == T0 + timedelta(hours=2)  # a new episode
    assert [i.dedup_key for i in await svc.open_items()] == ["jobs:dead"]


async def test_escalation_cancels_snooze_same_severity_keeps_it(svc: AttentionService, fc) -> None:
    item = (await svc.raise_item("disk", "warn", "Мало места")).item
    assert await svc.snooze(item.id, T0 + timedelta(hours=24)) is True
    assert await svc.open_items() == []

    r = await svc.raise_item("disk", "warn", "Мало места", "9%")
    assert r.outcome is RaiseOutcome.UPDATED
    assert r.item.snoozed_until == T0 + timedelta(hours=24)
    r = await svc.raise_item("disk", "info", "Мало места")  # de-escalation keeps snooze too
    assert r.item.snoozed_until is not None
    assert await svc.open_items() == []

    r = await svc.raise_item("disk", "error", "Диск почти полон", "2%")
    assert r.outcome is RaiseOutcome.ESCALATED and r.outcome.notify
    assert r.item.snoozed_until is None
    assert [i.severity for i in await svc.open_items()] == ["error"]


async def test_reopen_clears_snooze(svc: AttentionService, fc) -> None:
    item = (await svc.raise_item("k", "warn", "t")).item
    await svc.snooze(item.id, T0 + timedelta(days=1))
    await svc.resolve("k")
    reopened = await svc.raise_item("k", "warn", "t")
    assert reopened.item.snoozed_until is None
    assert len(await svc.open_items()) == 1


async def test_concurrent_raises_create_one_row(svc: AttentionService, db: Database) -> None:
    results = await asyncio.gather(*(svc.raise_item("race:key", "warn", f"t{i}") for i in range(20)))
    assert await _count(db) == 1
    assert sum(r.outcome is RaiseOutcome.CREATED for r in results) == 1
    assert len({r.item.id for r in results}) == 1


# -- snooze --------------------------------------------------------------------------------------


async def test_snooze_hides_until_time(svc: AttentionService, fc) -> None:
    item = (await svc.raise_item("a", "warn", "A")).item
    assert await svc.snooze_for(item.id, timedelta(hours=1)) is True
    assert await svc.open_items() == []
    assert [i.id for i in await svc.open_items(include_snoozed=True)] == [item.id]
    fetched = await svc.get_by_id(item.id)
    assert fetched is not None and fetched.is_snoozed()
    fc.advance(hours=1)  # exactly at the boundary the item is visible again
    assert [i.id for i in await svc.open_items()] == [item.id]


async def test_unsnooze(svc: AttentionService) -> None:
    item = (await svc.raise_item("a", "warn", "A")).item
    assert await svc.unsnooze(item.id) is False  # not snoozed
    await svc.snooze(item.id, T0 + timedelta(days=1))
    assert await svc.unsnooze(item.id) is True
    assert len(await svc.open_items()) == 1


async def test_snooze_rejects_bad_input(svc: AttentionService) -> None:
    item = (await svc.raise_item("a", "warn", "A")).item
    with pytest.raises(ValueError, match="aware"):
        await svc.snooze(item.id, datetime(2026, 10, 2))
    with pytest.raises(ValueError):
        await svc.snooze_for(item.id, timedelta(0))
    assert await svc.snooze(999_999, T0 + timedelta(hours=1)) is False
    await svc.resolve("a")
    assert await svc.snooze(item.id, T0 + timedelta(hours=1)) is False  # resolved items can't be snoozed


# -- reads ---------------------------------------------------------------------------------------


async def test_open_items_order_limit_and_counts(svc: AttentionService, fc) -> None:
    await svc.raise_item("i1", "info", "info old")
    fc.advance(seconds=1)
    await svc.raise_item("e1", "error", "error old")
    fc.advance(seconds=1)
    await svc.raise_item("w1", "warn", "warn")
    fc.advance(seconds=1)
    await svc.raise_item("e2", "error", "error new")
    snoozed = (await svc.raise_item("s1", "error", "snoozed")).item
    await svc.snooze(snoozed.id, T0 + timedelta(days=1))

    assert [i.dedup_key for i in await svc.open_items()] == ["e2", "e1", "w1", "i1"]
    assert [i.dedup_key for i in await svc.open_items(limit=2)] == ["e2", "e1"]
    assert await svc.open_counts() == {"info": 1, "warn": 1, "error": 2}
    with pytest.raises(ValueError):
        await svc.open_items(limit=0)


async def test_get_missing(svc: AttentionService) -> None:
    assert await svc.get("missing") is None
    assert await svc.get_by_id(12345) is None


# -- validation & sanitizing ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "severity", "title", "fix"),
    [
        ("", "warn", "t", None),
        ("has space", "warn", "t", None),
        ("tab\tkey", "warn", "t", None),
        ("k" * 201, "warn", "t", None),
        ("k", "critical", "t", None),
        ("k", "warn", "   ", None),
        ("k", "warn", "t", "with space"),
        ("k", "warn", "t", ""),
        ("k", "warn", "t", "x" * 201),
    ],
)
async def test_raise_validation(svc: AttentionService, key: str, severity: str, title: str, fix) -> None:
    with pytest.raises(ValueError):
        await svc.raise_item(key, severity, title, fix_action=fix)  # type: ignore[arg-type]


async def test_long_text_is_clipped_and_secrets_masked(svc: AttentionService) -> None:
    item = (
        await svc.raise_item(
            "long",
            "error",
            "T" * 500,
            f"Бот не запустился: token {BOT_TOKEN} invalid\n" + "b" * 5000,
        )
    ).item
    assert len(item.title) == 200 and item.title.endswith("…")
    assert len(item.body) == 3500
    assert BOT_TOKEN not in item.body
    assert "***" in item.body


async def test_db_enforces_severity_check(db: Database) -> None:
    with pytest.raises(IntegrityError):
        async with db.tx() as conn:
            await conn.execute(
                sa.insert(attention_items).values(dedup_key="x", severity="critical", title="t")
            )


# -- auto resolve / health sync ------------------------------------------------------------------


async def test_auto_resolve_by_prefix(svc: AttentionService) -> None:
    for key in ("lte:node1", "lte:node2", "lte:node3", "lte_other:1", "ip:1"):
        await svc.raise_item(key, "warn", key)
    assert await svc.auto_resolve("lte:", keep=["lte:node2"]) == 2
    assert {i.dedup_key for i in await svc.open_items()} == {"lte:node2", "lte_other:1", "ip:1"}
    assert await svc.auto_resolve("lte:") == 1
    with pytest.raises(ValueError):
        await svc.auto_resolve("")


async def test_auto_resolve_escapes_like_wildcards(svc: AttentionService) -> None:
    await svc.raise_item("a_b:1", "warn", "t")
    await svc.raise_item("aXb:1", "warn", "t")
    await svc.raise_item("100%:x", "warn", "t")
    await svc.raise_item("1000:x", "warn", "t")
    assert await svc.auto_resolve("a_b:") == 1
    assert await svc.auto_resolve("100%") == 1
    assert {i.dedup_key for i in await svc.open_items()} == {"aXb:1", "1000:x"}


async def test_sync_health(svc: AttentionService, fc) -> None:
    await svc.raise_item("component:bot", "error", "Бот не работает")
    await svc.raise_item("component:admin_chat", "warn", "Чат недоступен")
    reports = {
        "remnawave": HealthReport.down("401 от панели", fix_action="setting:REMNAWAVE_TOKEN"),
        "payments.rollypay": HealthReport.degraded("Касса отвечает медленно"),
        "bot": HealthReport.ok("Работает"),
        "admin_chat": HealthReport(Health.UNKNOWN, "Не ответил"),
        "sitepay": HealthReport.disabled(),
    }
    raised = await svc.sync_health(reports)
    assert {r.item.dedup_key: r.item.severity for r in raised} == {
        "component:remnawave": "error",
        "component:payments.rollypay": "warn",
    }
    rw = await svc.get("component:remnawave")
    assert rw is not None
    assert rw.fix_action == "setting:REMNAWAVE_TOKEN"
    assert "remnawave" in rw.title and rw.body == "401 от панели"
    open_keys = {i.dedup_key for i in await svc.open_items()}
    assert open_keys == {"component:remnawave", "component:payments.rollypay", "component:admin_chat"}

    # Second pass: everything recovered → items resolve; repeat raise is a silent update.
    raised = await svc.sync_health({"remnawave": HealthReport.down("401 от панели")})
    assert raised[0].outcome is RaiseOutcome.UPDATED
    await svc.sync_health({"remnawave": HealthReport.ok(), "payments.rollypay": HealthReport.ok()})
    assert {i.dedup_key for i in await svc.open_items()} == {"component:admin_chat"}


# -- bus integration -----------------------------------------------------------------------------


async def test_events_published(db: Database, fc) -> None:
    bus = EventBus()
    events: list[Event] = []

    async def collect(event: Event) -> None:
        events.append(event)

    bus.subscribe(ALL, collect)
    svc = AttentionService(db, bus=bus)
    await svc.raise_item("k", "warn", "t")
    await svc.raise_item("k", "warn", "t")  # silent update: no event
    await svc.raise_item("k", "error", "t")  # escalation
    await svc.resolve("k")
    await svc.resolve("k")  # nothing to resolve: no event
    await svc.raise_item("k", "warn", "t")  # reopen
    assert [(e.name, e.payload.get("outcome")) for e in events] == [
        ("attention.raised", "created"),
        ("attention.raised", "escalated"),
        ("attention.resolved", None),
        ("attention.raised", "reopened"),
    ]
    assert events[0].payload["dedup_key"] == "k"
    assert "title" not in events[0].payload  # keep payload free of free text


async def test_failing_subscriber_does_not_break_service(db: Database, fc) -> None:
    bus = EventBus()

    async def boom(event: Event) -> None:
        raise RuntimeError("subscriber broke")

    bus.subscribe("attention.*", boom)
    svc = AttentionService(db, bus=bus)
    assert (await svc.raise_item("k", "warn", "t")).outcome is RaiseOutcome.CREATED
    assert await svc.resolve("k") is True


# -- purge ---------------------------------------------------------------------------------------


async def test_purge_resolved(svc: AttentionService, db: Database, fc) -> None:
    await svc.raise_item("old", "info", "t")
    await svc.raise_item("open", "info", "t")
    await svc.resolve("old")
    fc.advance(days=10)
    await svc.raise_item("recent", "info", "t")
    await svc.resolve("recent")
    fc.advance(days=25)
    assert await svc.purge_resolved(older_than_days=30) == 1
    assert await svc.get("old") is None
    assert await svc.get("recent") is not None
    assert await svc.get("open") is not None
    with pytest.raises(ValueError):
        await svc.purge_resolved(-1)


# -- core tables ---------------------------------------------------------------------------------


async def test_users_defaults_and_constraints(db: Database) -> None:
    async with db.tx() as conn:
        row = (
            (await conn.execute(sa.insert(users).values(telegram_id=1001).returning(users))).mappings().one()
        )
        other = (await conn.execute(sa.insert(users).values().returning(users.c.public_id))).scalar_one()
    assert row["role"] == "user"
    assert row["perms"] == []
    assert UUID7_RE.match(row["public_id"]), row["public_id"]
    assert UUID7_RE.match(other) and other != row["public_id"]
    assert row["created_at"].tzinfo is not None

    async with db.tx() as conn:
        await conn.execute(
            sa.insert(users).values(telegram_id=1002, role="admin", perms=["payments.confirm"])
        )

    bad_rows = [
        {"telegram_id": 1003, "role": "superuser"},
        {"telegram_id": 1004, "perms": {"a": 1}},
        {"telegram_id": 1001},  # duplicate telegram_id
        {"public_id": row["public_id"]},  # duplicate public_id
    ]
    for values in bad_rows:
        with pytest.raises(IntegrityError):
            async with db.tx() as conn:
                await conn.execute(sa.insert(users).values(**values))


async def test_user_identities(db: Database) -> None:
    async with db.tx() as conn:
        uid = (await conn.execute(sa.insert(users).values(telegram_id=1).returning(users.c.id))).scalar_one()
        await conn.execute(sa.insert(user_identities).values(user_id=uid, provider="email", subject="a@b.c"))
    with pytest.raises(IntegrityError):
        async with db.tx() as conn:
            await conn.execute(
                sa.insert(user_identities).values(user_id=uid, provider="email", subject="a@b.c")
            )
    with pytest.raises(IntegrityError):
        async with db.tx() as conn:
            await conn.execute(sa.insert(user_identities).values(user_id=999, provider="x", subject="y"))
    async with db.tx() as conn:
        await conn.execute(sa.delete(users).where(users.c.id == uid))
    async with db.read() as conn:
        left = (await conn.execute(sa.select(sa.func.count()).select_from(user_identities))).scalar_one()
    assert left == 0  # cascade


async def test_admin_audit_money_requires_reason(db: Database) -> None:
    async with db.tx() as conn:
        row = (
            (
                await conn.execute(
                    sa.insert(admin_audit)
                    .values(
                        actor_id=1,
                        role="admin",
                        action="wallet.credit",
                        target="user:5",
                        amount_minor=10_000,
                        reason="компенсация",
                        batch_id="b1",
                    )
                    .returning(admin_audit)
                )
            )
            .mappings()
            .one()
        )
        await conn.execute(sa.insert(admin_audit).values(action="settings.apply"))  # system actor
    assert row["details"] == {} and row["ts"].tzinfo is not None
    for reason in (None, "   "):
        with pytest.raises(IntegrityError):
            async with db.tx() as conn:
                await conn.execute(
                    sa.insert(admin_audit).values(action="wallet.debit", amount_minor=100, reason=reason)
                )


async def test_config_meta_upsert(db: Database) -> None:
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    for value in ({"text": "A=1\n"}, {"text": "A=2\n"}):
        stmt = pg_insert(config_meta).values(key="env_base", value=value)
        stmt = stmt.on_conflict_do_update(index_elements=["key"], set_={"value": stmt.excluded.value})
        async with db.tx() as conn:
            await conn.execute(stmt)
    async with db.read() as conn:
        rows = (await conn.execute(sa.select(config_meta))).mappings().all()
    assert len(rows) == 1 and rows[0]["value"] == {"text": "A=2\n"}
