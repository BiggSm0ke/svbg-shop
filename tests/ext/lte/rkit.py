"""Test kit of the LTE runtime (``tests/ext/lte/test_{collector,service,enforce,ui,notify,admin,packs,…}``).

``async with lte_env(pg_dsn) as env:`` — the rw-sync kit (real ``Database`` + fake panel + core writer) plus:

* the ``lte_*`` tables and the runtime tables (until the integration registers them in the schema) and the
  order kind ``addon_lte`` allowed in ``orders`` (the same ALTER the integration migration does);
* a panel topology: base squad ``NL`` = {main, lte} inbounds, twin ``NL noLTE`` = {main}, an LTE node with the
  ``lte`` inbound and a plain node; one active LTE group (10 GB) with the LTE node and the twin map;
* :class:`~svbg.ext.lte.service.LteService` on that panel: a settable config, fake notifier and admin chat;
* :meth:`LteEnv.drain` — the panel jobs **and** the module's jobs, run like the worker does.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa

from svbg.ext.lte import tables as lte_tables
from svbg.ext.lte.service import (
    K_ENABLED,
    K_ENFORCE,
    K_NOTIFY,
    K_QUIET,
    K_TOPUP,
    LteService,
    card_job,
    confirm_job,
    create_runtime_tables,
    notify_job,
    resend_job,
    term_job,
)
from svbg.jobs.queue import Job, JobQueue
from svbg.jobs.worker import JobContext, PermanentJobError, RetryJob
from svbg.subscriptions.hooks import EventRelay
from tests.subscriptions.hwid_jobs import DeviceJobs
from tests.subscriptions.kit import SyncEnv, sync_env

__all__ = ["GB", "FakeAdminChat", "FakeNotifier", "LteEnv", "lte_env"]

GB = 10**9
WORKER = "lte-test"


@dataclass
class Sent:
    chat_id: int
    text: str
    kwargs: dict[str, Any]


@dataclass
class FakeNotifier:
    sent: list[Sent] = field(default_factory=list)
    fail: BaseException | None = None
    blocked: bool = False

    async def send(self, chat_id: int, text: str, **kwargs: Any) -> object | None:
        if self.fail is not None:
            raise self.fail
        if self.blocked:
            return None
        self.sent.append(Sent(chat_id, text, kwargs))
        return object()


@dataclass
class FakeAdminChat:
    posts: list[tuple[str, str]] = field(default_factory=list)
    fail: bool = False

    async def post(self, kind: str, text: str, **kwargs: Any) -> None:
        del kwargs
        if self.fail:
            raise RuntimeError("admin chat down")
        self.posts.append((kind, text))


@dataclass
class LteEnv:
    rw: SyncEnv
    service: LteService
    config: dict[str, Any]
    notifier: FakeNotifier
    admin_chat: FakeAdminChat
    base: str
    twin: str
    lte_node: str
    plain_node: str
    group_id: int

    @property
    def db(self) -> Any:
        return self.rw.db

    @property
    def panel(self) -> Any:
        return self.rw.panel

    # ----------------------------------------------------------------------------------------- seeding

    async def linked_sub(self, telegram_id: int = 100, **desired: Any) -> int:
        return await self.rw.linked_sub(telegram_id, **desired)

    async def user_of(self, sid: int) -> int:
        rows = await self.db.raw("select user_id from subscriptions where id = $1", sid)
        return int(rows[0]["user_id"])

    async def panel_id(self, sid: int) -> int:
        rows = await self.db.raw("select panel_user_id from subscriptions where id = $1", sid)
        return int(rows[0]["panel_user_id"])

    async def open_period(
        self,
        sid: int,
        *,
        used: int = 0,
        is_trial: bool = False,
        anchor_kind: str = "paid",
        ends_in: timedelta = timedelta(days=29),
        started: timedelta = timedelta(days=1),
        state: str = "open",
    ) -> int:
        """A live period (+ anchor and usage) written directly, bypassing the period machine."""
        at = datetime.now(UTC).replace(microsecond=0)
        start = at - started
        await self.db.raw(
            "insert into lte_anchors (subscription_id, anchor_at, anchor_kind, anchor_source, series_open, "
            "series_started_at, coverage_end, is_trial) values ($1, $2, $3, 'test', true, $2, $4, $5) "
            "on conflict (subscription_id) do nothing",
            sid,
            start,
            anchor_kind,
            at + timedelta(days=30),
            is_trial,
        )
        rows = await self.db.raw(
            "insert into lte_periods (subscription_id, anchor_at, idx, starts_at, planned_end_at, state, "
            "is_trial) values ($1, $2, 0, $2, $3, $4, $5) returning id",
            sid,
            start,
            at + ends_in,
            state,
            is_trial,
        )
        pid = int(rows[0]["id"])
        await self.set_used(pid, used)
        await self.db.raw(
            "update subscriptions set paid_until = $2 where id = $1", sid, at + timedelta(days=30)
        )
        await self.db.raw(
            "insert into lte_event_cursor (subscription_id, last_event_id) "
            "select $1, coalesce(max(id), 0) from subscription_events where subscription_id = $1 "
            "on conflict (subscription_id) do update set last_event_id = excluded.last_event_id",
            sid,
        )
        return pid

    async def set_used(self, period_id: int, used: int) -> None:
        await self.db.raw(
            "insert into lte_period_usage (period_id, group_id, used_bytes) values ($1, $2, $3) "
            "on conflict (period_id, group_id) do update set used_bytes = excluded.used_bytes",
            period_id,
            self.group_id,
            used,
        )

    async def add_pack(self, gb: int, amount_minor: int, *, enabled: bool = True) -> int:
        rows = await self.db.raw(
            "insert into lte_packs (gb, amount_minor, enabled) values ($1, $2, $3) returning id",
            gb,
            amount_minor,
            enabled,
        )
        return int(rows[0]["id"])

    # ------------------------------------------------------------------------------------------- reads

    async def blocks(self, sid: int | None = None) -> list[dict[str, Any]]:
        sql = "select * from lte_blocks" + (" where subscription_id = $1" if sid else "") + " order by id"
        rows = await (self.db.raw(sql, sid) if sid else self.db.raw(sql))
        return [dict(r) for r in rows]

    async def substitutions(self, sid: int) -> list[dict[str, Any]]:
        rows = await self.db.raw("select * from panel_squad_substitutions where subscription_id = $1", sid)
        return [dict(r) for r in rows]

    async def panel_squads(self, sid: int) -> list[str]:
        user = self.panel.users[await self.panel_id(sid)]
        return sorted(s["uuid"] if isinstance(s, Mapping) else str(s) for s in user["activeInternalSquads"])

    async def job_kinds(self, status: str = "ready") -> list[str]:
        rows = await self.db.raw("select kind from jobs where status = $1 order by id", status)
        return [str(r["kind"]) for r in rows]

    # -------------------------------------------------------------------------------------------- jobs

    async def drain(self, *, rounds: int = 50, make_due: bool = False) -> list[tuple[Job, str]]:
        """Panel jobs + LTE jobs, sequentially (deterministic). Retries are pushed an hour away."""
        queue = JobQueue(self.db)
        ctx = JobContext(db=self.db, queue=queue, worker_id=WORKER)
        handlers = {
            **self.rw.writer.handlers(),
            **DeviceJobs(self.db, self.rw.current_api, attention=self.rw.attention).handlers(),
            **EventRelay(self.rw.bus).handlers(),
            "lte.confirm": confirm_job,
            "lte.resend": resend_job,
            "lte.notify": notify_job,
            "lte.term": term_job,
            "lte.card": card_job,
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
                handler = handlers.get(job.kind)
                try:
                    if handler is None:
                        raise PermanentJobError(f"no handler for {job.kind}")
                    await handler(job, ctx)
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


def default_config(**over: Any) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        K_ENABLED: True,
        K_ENFORCE: "on",
        K_NOTIFY: True,
        K_QUIET: "off",
        K_TOPUP: True,
        "SUPPORT_URL": "https://t.me/support_bot",
    }
    cfg.update(over)
    return cfg


async def prepare_schema(db: Any) -> None:
    """LTE tables + runtime tables + ``addon_lte`` order kind (what the integration migration adds)."""
    async with db.engine.begin() as conn:
        if not lte_tables.REGISTERED:
            await conn.run_sync(lte_tables.create_tables)
        await conn.run_sync(create_runtime_tables)
        kinds = await conn.scalar(
            sa.text(
                "select pg_get_constraintdef(oid) from pg_constraint where conname = 'ck_orders_kind' limit 1"
            )
        )
        if kinds is not None and "addon_lte" not in str(kinds):
            await conn.execute(sa.text("alter table orders drop constraint ck_orders_kind"))
            await conn.execute(
                sa.text(
                    "alter table orders add constraint ck_orders_kind check (kind in "
                    "('new', 'renew', 'change', 'addon_devices', 'addon_lte', 'topup'))"
                )
            )


@asynccontextmanager
async def lte_env(pg_dsn: str, **config: Any) -> AsyncIterator[LteEnv]:
    async with sync_env(pg_dsn) as rw:
        await prepare_schema(rw.db)
        panel = rw.panel
        base = rw.squad
        panel.internal_squads[base]["inbounds"] = [
            {"uuid": "ib-main", "tag": "MAIN"},
            {"uuid": "ib-lte", "tag": "LTE"},
        ]
        twin = panel.add_internal_squad("NL noLTE")
        panel.internal_squads[twin]["inbounds"] = [{"uuid": "ib-main", "tag": "MAIN"}]
        lte_node = panel.add_node("LTE-1")
        plain_node = panel.add_node("NL-1")
        panel.nodes[0]["configProfile"]["activeInbounds"] = [{"uuid": "ib-lte", "tag": "LTE"}]
        panel.nodes[1]["configProfile"]["activeInbounds"] = [{"uuid": "ib-main", "tag": "MAIN"}]
        rows = await rw.db.raw(
            "insert into lte_groups (slug, name, state, enforce, has_default, limit_default_bytes, "
            "has_trial, limit_trial_bytes) values ('lte', $1, 'active', true, true, $2, true, 0) "
            "returning id",
            {"ru": "LTE"},
            10 * GB,
        )
        group_id = int(rows[0]["id"])
        await rw.db.raw(
            "insert into lte_group_nodes (group_id, node_uuid, counted_from) "
            "values ($1, $2, now() - interval "
            "'2 days')",
            group_id,
            lte_node,
        )
        await rw.db.raw(
            "insert into lte_twins (base_squad_uuid, group_id, twin_squad_uuid) values ($1, $2, $3)",
            base,
            group_id,
            twin,
        )
        cfg = default_config(**config)
        notifier = FakeNotifier()
        admin_chat = FakeAdminChat()
        service = LteService(
            rw.db,
            rw.current_api,
            config=lambda: cfg,
            attention=rw.attention,
            admin_chat=admin_chat,
            notifier=notifier,
        )
        from svbg.ext.lte.service import RUNTIME

        RUNTIME.set_service(service)
        rw.register_module("lte", "ok")
        async with rw.db.tx() as conn:
            from svbg.ext.lte.enforce import sync_twins

            await sync_twins(conn)
        try:
            yield LteEnv(
                rw=rw,
                service=service,
                config=cfg,
                notifier=notifier,
                admin_chat=admin_chat,
                base=base,
                twin=twin,
                lte_node=lte_node,
                plain_node=plain_node,
                group_id=group_id,
            )
        finally:
            RUNTIME.set_service(None)
