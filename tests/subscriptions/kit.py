"""Shared test kit for the rw-sync tests: real ``Database`` + fake panel + writer, without SQL shims.

Used as ``async with sync_env(pg_dsn) as env:`` (a context manager instead of fixtures, so the tests in
``tests/remnawave`` and ``tests/subscriptions`` share it without a shared conftest).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa

import svbg.remnawave.tables
import svbg.subscriptions.tables  # noqa: F401 - registers the tables in the shared metadata
from svbg.core.attention import AttentionService
from svbg.core.bus import Event, EventBus
from svbg.core.clock import FrozenClock, reset_clock, set_clock
from svbg.jobs.queue import Job, JobQueue
from svbg.jobs.worker import JobContext, PermanentJobError, RetryJob
from svbg.remnawave.api import RemnawaveApi
from svbg.remnawave.contributors import SquadContributors
from svbg.remnawave.transport import Transport, TransportConfig
from svbg.remnawave.writer import PanelWriter
from svbg.subscriptions.hooks import EventRelay
from svbg.subscriptions.service import Desired, SubscriptionService
from svbg.subscriptions.tables import subscriptions
from tests.dbkit import CountingDatabase, open_db
from tests.fakes.remnawave import FakeRemnawave
from tests.subscriptions.hwid_jobs import DeviceJobs

__all__ = ["FakeCatalog", "SyncEnv", "frozen_clock", "plan_row", "sync_env"]

WORKER = "test-worker"


@dataclass
class SyncEnv:
    db: CountingDatabase
    panel: FakeRemnawave
    api: RemnawaveApi
    attention: AttentionService
    bus: EventBus
    contributors: SquadContributors
    squad: str
    writer: PanelWriter = field(init=False)
    service: SubscriptionService = field(default_factory=SubscriptionService)
    events: list[Event] = field(default_factory=list)
    module_states: dict[str, Any] = field(default_factory=dict)

    def current_api(self) -> RemnawaveApi:
        """Writer/inbox/sync take the API through this, so a test may swap ``env.api`` (fault wrappers)."""
        return self.api

    # ----------------------------------------------------------------------------------------- seeding

    async def add_user(self, telegram_id: int) -> int:
        rows = await self.db.raw("insert into users (telegram_id) values ($1) returning id", telegram_id)
        return int(rows[0]["id"])

    async def new_sub(
        self,
        telegram_id: int | None = 100,
        *,
        days: float = 30,
        squads: Sequence[str] | None = None,
        **desired: Any,
    ) -> int:
        """A pending subscription + ``panel.create`` (through the service)."""
        user_id = await self.add_user(telegram_id) if telegram_id is not None else None
        spec = Desired(
            expire_at=desired.pop("expire_at", datetime.now(UTC) + timedelta(days=days)),
            squads=list(squads) if squads is not None else [self.squad],
            **desired,
        )
        async with self.db.tx() as conn:
            return await self.service.create(conn, user_id=user_id, telegram_id=telegram_id, desired=spec)

    async def linked_sub(self, telegram_id: int | None = 100, **desired: Any) -> int:
        sid = await self.new_sub(telegram_id, **desired)
        await self.drain()
        row = await self.sub(sid)
        assert row["link_state"] == "linked", row
        return sid

    def register_module(self, name: str, state: Any = "ok") -> None:
        """A module whose liveness the test controls via ``env.module_states[name]``."""
        self.module_states[name] = state

        def status() -> Any:
            value = self.module_states[name]
            if isinstance(value, BaseException):
                raise value
            return value

        self.contributors.register(name, status)

    async def substitute(self, sid: int, base: str, twin: str, module: str = "lte") -> None:
        await self.db.raw(
            "insert into panel_squad_substitutions (subscription_id, base_squad_uuid, substitute_squad_uuid, "
            "owner_module) values ($1, $2, $3, $4)",
            sid,
            base,
            twin,
            module,
        )
        await self.db.raw(
            "insert into panel_squad_twins (substitute_squad_uuid, base_squad_uuid, owner_module) "
            "values ($1, $2, $3) "
            "on conflict do nothing",
            twin,
            base,
            module,
        )

    # --------------------------------------------------------------------------------------------- reads

    async def sub(self, sid: int) -> Mapping[str, Any]:
        async with self.db.read() as conn:
            row = (
                (await conn.execute(sa.select(subscriptions).where(subscriptions.c.id == sid)))
                .mappings()
                .one()
            )
        return dict(row)

    async def jobs(self, kind: str | None = None) -> list[dict[str, Any]]:
        sql = "select * from jobs where queue = 'panel'" + (" and kind = $1" if kind else "") + " order by id"
        rows = await (self.db.raw(sql, kind) if kind else self.db.raw(sql))
        return [dict(r) for r in rows]

    async def attention_keys(self) -> set[str]:
        return {
            r["dedup_key"]
            for r in await self.db.raw("select dedup_key from attention_items where resolved_at is null")
        }

    def panel_user(self, panel_id: int) -> dict[str, Any]:
        return self.panel.users[panel_id]

    # ---------------------------------------------------------------------------------------------- jobs

    async def drain(self, *, rounds: int = 50, make_due: bool = False) -> list[tuple[Job, str]]:
        """Run panel jobs like the worker does (sequentially, deterministic). Returns ``(job, outcome)``."""
        queue = JobQueue(self.db)
        ctx = JobContext(db=self.db, queue=queue, worker_id=WORKER)
        handlers = {
            **self.writer.handlers(),
            **DeviceJobs(self.db, self.current_api, attention=self.attention).handlers(),
            **EventRelay(self.bus).handlers(),
        }
        outcomes: list[tuple[Job, str]] = []
        if make_due:
            await self.db.raw("update jobs set next_run_at = now() where status = 'ready'")
        for _ in range(rounds):
            claimed = await queue.claim("interactive", WORKER, 10) + await queue.claim(
                "background", WORKER, 10
            )
            if not claimed:
                break
            for job in claimed:
                fence = {"worker_id": WORKER, "attempt": job.attempts}
                try:
                    await handlers[job.kind](job, ctx)
                except RetryJob as r:
                    await queue.fail(job.id, str(r), retry_in=max(r.delay, 3600), **fence)
                    outcomes.append((job, "retry"))
                except PermanentJobError as e:
                    await queue.fail(job.id, str(e), permanent=True, **fence)
                    outcomes.append((job, "dead"))
                except Exception as e:
                    await queue.fail(job.id, f"{type(e).__name__}: {e}", retry_in=3600, **fence)
                    outcomes.append((job, "failed"))
                else:
                    await queue.complete(job.id, **fence)
                    outcomes.append((job, "done"))
        return outcomes

    async def make_due(self) -> None:
        await self.db.raw("update jobs set next_run_at = now() where status = 'ready'")


@asynccontextmanager
async def sync_env(pg_dsn: str, *, panel: FakeRemnawave | None = None) -> AsyncIterator[SyncEnv]:
    fake = panel or FakeRemnawave()
    async with open_db(pg_dsn, pool_size=8) as db, fake:
        transport = Transport(
            TransportConfig(
                base_url=fake.url,
                token=fake.add_token(),
                interactive_rps=10_000,
                background_rps=10_000,
                backoff_base=0.01,
            )
        )
        try:
            api = RemnawaveApi(transport)
            bus = EventBus()
            attention = AttentionService(db, bus=bus)
            contributors = SquadContributors(attention)
            env = SyncEnv(
                db=db,
                panel=fake,
                api=api,
                attention=attention,
                bus=bus,
                contributors=contributors,
                squad=fake.add_internal_squad("NL"),
            )
            env.writer = PanelWriter(
                db, env.current_api, contributors=contributors, attention=attention, bus=bus
            )

            async def collect(event: Event) -> None:
                env.events.append(event)

            bus.subscribe("*", collect)
            yield env
            await bus.aclose()
        finally:
            await transport.aclose()


def body_of(panel: FakeRemnawave, path: str, method: str) -> list[dict[str, Any]]:
    return [r.body for r in panel.calls(path, method)]


# ------------------------------------------------------------------------------------- stage 2 helpers


@contextmanager
def frozen_clock(start: datetime | None = None) -> Iterator[FrozenClock]:
    """Freeze :func:`svbg.core.clock.now` (whole seconds, so term arithmetic is exact) for one test."""
    clock = FrozenClock((start or datetime.now(UTC)).replace(microsecond=0))
    set_clock(clock)
    try:
        yield clock
    finally:
        reset_clock()


def plan_row(squad: str, plan_id: int = 1, **overrides: Any) -> dict[str, Any]:
    """A catalog plan row as ``svbg.catalog`` hands it out (06 M1: 5 devices, up to 15, unlimited)."""
    row: dict[str, Any] = {
        "id": plan_id,
        "code": f"plan{plan_id}",
        "name": {"ru": f"Тариф {plan_id}"},
        "squads": [squad],
        "traffic_bytes": 0,
        "reset_strategy": "NO_RESET",
        "device_limit": 5,
        "device_addon": {"enabled": True, "max_devices": 15},
        "is_trial": False,
    }
    row.update(overrides)
    return row


@dataclass
class FakeCatalog:
    """:class:`svbg.subscriptions.terms.TrialPlanSource` with a settable trial plan; counts its reads."""

    trial: Mapping[str, Any] | None = None
    reads: int = 0

    async def trial_plan(self, conn: Any) -> Mapping[str, Any] | None:
        self.reads += 1
        return self.trial
