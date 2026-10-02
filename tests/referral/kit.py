"""Referral test kit: the referral tables attached to the shared metadata only while a referral test runs
(until integration registers ``svbg.referral.tables`` in ``TABLE_MODULES``), seeding helpers, fakes for the
admin chat and the user notifier, and a deterministic job runner."""

from __future__ import annotations

import itertools
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa

from svbg.core.bus import Event, EventBus
from svbg.db import schema
from svbg.db.meta import metadata
from svbg.jobs.queue import Job, JobQueue
from svbg.jobs.worker import JobContext, PermanentJobError
from svbg.referral import tables as referral_tables
from svbg.referral.service import ReferralService
from svbg.subscriptions.hooks import EventRelay
from tests.dbkit import CountingDatabase

SQUAD = "11111111-1111-4111-8111-111111111111"
TABLES: tuple[sa.Table, ...] = referral_tables.TABLES
_REGISTERED = "svbg.referral.tables" in schema.TABLE_MODULES
_PANEL_IDS = itertools.count(70_000)
_TG_IDS = itertools.count(5_000_000)
WORKER = "ref-test"

#: Owner's prod values, program on.
PROD: dict[str, Any] = {
    "REFERRAL_ENABLED": True,
    "REFERRAL_MODE": "days",
    "REFERRAL_INVITER_DAYS": 14,
    "REFERRAL_INVITEE_DAYS": 7,
    "REFERRAL_TRIGGER": "trial_or_paid",
    "REFERRAL_INVITER_CAP_30D": 20,
    "REFERRAL_INVITER_CAP_TOTAL": 0,
    "REFERRAL_PERCENT": 10,
    "CURRENCY": "RUB",
}


def detach_tables(tables: Sequence[sa.Table] = TABLES, *, registered: bool = _REGISTERED) -> None:
    if registered:
        return
    for table in reversed(tables):
        if table.name in metadata.tables:
            metadata.remove(table)


def attach_tables(tables: Sequence[sa.Table] = TABLES) -> None:
    for table in tables:
        if table.name not in metadata.tables:
            metadata._add_table(table.name, table.schema, table)


detach_tables()


# ------------------------------------------------------------------------------------------------ fakes


@dataclass
class FakePoster:
    posts: list[tuple[str, str, bool]] = field(default_factory=list)

    async def post(self, kind: str, text: str, *, html: bool = False) -> None:
        self.posts.append((kind, text, html))


@dataclass
class FakeSender:
    sent: list[tuple[int, str, str | None]] = field(default_factory=list)
    fail: BaseException | None = None

    async def send(self, chat_id: int, text: str, *, parse_mode: str | None = None) -> object:
        if self.fail is not None:
            raise self.fail
        self.sent.append((chat_id, text, parse_mode))
        return object()


# ------------------------------------------------------------------------------------------------ seeding


async def add_user(
    db: CountingDatabase,
    *,
    username: str | None = None,
    first_name: str | None = None,
    language: str | None = "ru",
    tg: int | None = None,
) -> int:
    rows = await db.raw(
        "insert into users (telegram_id, username, first_name, language) "
        "values ($1, $2, $3, $4) returning id",
        tg if tg is not None else next(_TG_IDS),
        username,
        first_name,
        language,
    )
    return int(rows[0]["id"])


async def add_sub(
    db: CountingDatabase,
    user_id: int,
    *,
    days_left: float = 10,
    is_trial: bool = False,
    link_state: str = "linked",
    hold: bool = False,
) -> int:
    until = datetime.now(UTC) + timedelta(days=days_left)
    rows = await db.raw(
        "insert into subscriptions (user_id, plan_id, plan_snapshot, link_state, paid_until, "
        "desired_expire_at, desired_squads, is_trial, panel_user_id, hold_kind, hold_since) "
        "values ($1, 1, $2::jsonb, $3, $4, $4, $5::jsonb, $6, $7, $8, $9) returning id",
        user_id,
        json.dumps({"plan_id": 1, "squads": [SQUAD], "device_limit": 5}),
        link_state,
        until,
        json.dumps([SQUAD]),
        is_trial,
        next(_PANEL_IDS) if link_state == "linked" else None,
        "admin" if hold else None,
        datetime.now(UTC) if hold else None,
    )
    return int(rows[0]["id"])


async def add_trial(db: CountingDatabase, user_id: int, *, days_left: float = 3) -> int:
    sid = await add_sub(db, user_id, days_left=days_left, is_trial=True)
    await db.raw("insert into trial_grants (user_id, subscription_id) values ($1, $2)", user_id, sid)
    return sid


async def add_purchase(db: CountingDatabase, sid: int, ref: str = "1") -> None:
    await db.raw(
        "insert into subscription_events (subscription_id, kind, source, ref_type, ref_id) "
        "values ($1, 'purchase_new', 'bot', 'order', $2)",
        sid,
        ref,
    )


async def set_code(db: CountingDatabase, user_id: int, code: str) -> None:
    await db.raw("insert into referral_codes (user_id, code) values ($1, $2)", user_id, code)


async def bind(db: CountingDatabase, referred: int, referrer: int, *, ago: timedelta | None = None) -> None:
    at = datetime.now(UTC) - (ago or timedelta())
    await db.raw(
        "insert into referrals (referred_user_id, referrer_id, attached_at) values ($1, $2, $3)",
        referred,
        referrer,
        at,
    )


async def grant_rows(
    db: CountingDatabase, referrer: int, count: int, *, ago: timedelta | None = None
) -> None:
    """``count`` already rewarded friends of ``referrer`` (granted inviter sides ``ago`` back)."""
    at = datetime.now(UTC) - (ago or timedelta(days=1))
    for _ in range(count):
        friend = await add_user(db)
        await bind(db, friend, referrer)
        await db.raw(
            "insert into referral_rewards (referred_user_id, user_id, side, kind, status, days, granted_at) "
            "values ($1, $2, 'inviter', 'days', 'granted', 14, $3)",
            friend,
            referrer,
            at,
        )


async def rewards(db: CountingDatabase, referred: int) -> dict[str, dict[str, Any]]:
    rows = await db.raw(
        "select * from referral_rewards where referred_user_id = $1 and kind = 'days'", referred
    )
    return {r["side"]: dict(r) for r in rows}


async def paid_until(db: CountingDatabase, sid: int) -> datetime:
    rows = await db.raw("select paid_until from subscriptions where id = $1", sid)
    return rows[0]["paid_until"]


async def jobs(db: CountingDatabase, kind: str, status: str = "ready") -> list[dict[str, Any]]:
    rows = await db.raw("select * from jobs where kind = $1 and status = $2 order by id", kind, status)
    return [dict(r) for r in rows]


# ------------------------------------------------------------------------------------------------ runner


@dataclass
class Env:
    db: CountingDatabase
    svc: ReferralService
    cfg: dict[str, Any]
    poster: FakePoster
    sender: FakeSender
    bus: EventBus
    events: list[Event] = field(default_factory=list)

    async def drain(self, *, rounds: int = 30) -> list[tuple[str, str]]:
        """Run ready jobs like the worker (sequentially): referral jobs and the subscription event relay;
        panel jobs are completed without a panel. Returns ``(kind, outcome)``."""
        queue = JobQueue(self.db)
        ctx = JobContext(db=self.db, queue=queue, worker_id=WORKER)
        handlers: dict[str, Any] = {**self.svc.handlers(), **EventRelay(self.bus).handlers()}
        done: list[tuple[str, str]] = []
        await self.db.raw("update jobs set next_run_at = now() where status = 'ready'")
        for _ in range(rounds):
            claimed: list[Job] = []
            for lane in ("interactive", "background"):
                claimed += await queue.claim(lane, WORKER, 20)
            if not claimed:
                break
            for job in claimed:
                fence = {"worker_id": WORKER, "attempt": job.attempts}
                handler = handlers.get(job.kind)
                try:
                    if handler is not None:
                        await handler(job, ctx)
                except PermanentJobError as e:
                    await queue.fail(job.id, str(e), permanent=True, **fence)
                    done.append((job.kind, "dead"))
                except Exception as e:
                    await queue.fail(job.id, f"{type(e).__name__}: {e}", retry_in=3600, **fence)
                    done.append((job.kind, "failed"))
                else:
                    await queue.complete(job.id, **fence)
                    done.append((job.kind, "done" if handler is not None else "skipped"))
        return done


def job(kind: str, payload: Mapping[str, Any]) -> Job:
    now = datetime.now(UTC)
    return Job(
        id=0,
        queue="hook",
        lane="background",
        kind=kind,
        payload=dict(payload),
        attempts=1,
        max_attempts=5,
        ordering_key=None,
        dedup_key=None,
        caused_by=None,
        locked_by=None,
        locked_until=None,
        next_run_at=now,
        created_at=now,
    )
