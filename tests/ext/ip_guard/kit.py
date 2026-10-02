"""Test kit for IP Guard: the fake panel with the Connections API, fake admin chat / notifier, the service
wired to a real PostgreSQL and a deterministic job drain (writer + hooks + the module's jobs)."""

from __future__ import annotations

import ipaddress
import itertools
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web

import svbg.ext.ip_guard.tables  # noqa: F401 - registers the module tables before create_schema
from svbg.ext.ip_guard.panel import DROP_KIND, DropIpsJob, PanelReader
from svbg.ext.ip_guard.service import CARD_KIND, NOTIFY_KIND, IpGuardService
from svbg.jobs.queue import Job, JobQueue
from svbg.jobs.worker import JobContext, PermanentJobError, RetryJob
from svbg.services.admin_chat import PostResult
from svbg.subscriptions.hooks import EventRelay
from tests.fakes.remnawave import FakeRemnawave
from tests.subscriptions.kit import SyncEnv, sync_env

__all__ = ["ConnPanel", "FakeChat", "FakeNotifier", "GuardEnv", "guard_env", "ips"]

WORKER = "ipg-test"


def ips(count: int, *, first_octet: int = 5, third: int = 0) -> list[str]:
    """``count`` public IPv4 addresses in ``count`` different /24 subnets."""
    return [f"{first_octet}.{i + 1}.{third % 250}.1" for i in range(count)]


class ConnPanel(FakeRemnawave):
    """The fake panel plus the ``connections/by-node`` and ``by-user`` jobs (not in the shared fake)."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.by_node: dict[str, dict[int, list[str]]] = {}
        self.pending_rounds = 0
        self.node_errors: dict[str, int] = {}  # node uuid → HTTP status of POST by-node
        self.drop_status: int | None = None
        self._jobs: dict[str, tuple[str, int]] = {}
        self._job_ids = itertools.count(1)

    def _routes(self, app: web.Application) -> None:
        super()._routes(app)
        r = app.router
        r.add_route(
            "POST",
            "/api/connections/by-node/{uuid}",
            self._guard(self._by_node, "connections", "by-node", "read"),
        )
        r.add_route(
            "GET",
            "/api/connections/by-node/{job}",
            self._guard(self._by_node_result, "connections", "by-node-result", "read"),
        )
        r.add_route(
            "POST",
            "/api/connections/by-user/{id}",
            self._guard(self._by_user, "connections", "by-user", "read"),
        )
        r.add_route(
            "GET",
            "/api/connections/by-user/{job}",
            self._guard(self._by_user_result, "connections", "by-user-result", "read"),
        )

    async def _by_node(self, request: web.Request) -> web.Response:
        uuid = request.match_info["uuid"]
        status = self.node_errors.get(uuid)
        if status is not None:
            raise self._error("A217", "Fault", status, request.path)
        job = str(next(self._job_ids))
        self._jobs[job] = (uuid, self.pending_rounds)
        return self._ok({"jobId": job}, 201)

    async def _by_node_result(self, request: web.Request) -> web.Response:
        job = request.match_info["job"]
        if job not in self._jobs:
            raise self._error("A218", "Job not found", 404, request.path)
        uuid, left = self._jobs[job]
        if left > 0:
            self._jobs[job] = (uuid, left - 1)
            return self._ok({"isCompleted": False, "isFailed": False, "result": None})
        users = [
            {"userId": uid, "ips": [{"ip": ip, "lastSeen": "2026-09-17T11:00:00.000Z"} for ip in addrs]}
            for uid, addrs in self.by_node.get(uuid, {}).items()
        ]
        return self._ok(
            {
                "isCompleted": True,
                "isFailed": False,
                "result": {"success": True, "nodeUuid": uuid, "users": users},
            }
        )

    async def _by_user(self, request: web.Request) -> web.Response:
        job = f"u{next(self._job_ids)}"
        self._jobs[job] = (request.match_info["id"], 0)
        return self._ok({"jobId": job}, 201)

    async def _by_user_result(self, request: web.Request) -> web.Response:
        uid = int(self._jobs[request.match_info["job"]][0])
        nodes = [
            {"nodeUuid": node, "ips": [{"ip": ip} for ip in users.get(uid, [])]}
            for node, users in self.by_node.items()
            if uid in users
        ]
        return self._ok({"isCompleted": True, "isFailed": False, "result": {"nodes": nodes}})

    async def _drop(self, request: web.Request) -> web.Response:
        if self.drop_status is not None:
            raise self._error("A219", "Node not connected", self.drop_status, request.path)
        return await super()._drop(request)


@dataclass
class FakeChat:
    posts: list[dict[str, Any]] = field(default_factory=list)
    down: bool = False
    _ids: Any = field(default_factory=lambda: itertools.count(100))
    cards: dict[str, int] = field(default_factory=dict)

    async def post(self, kind: str, text: str, **kwargs: Any) -> PostResult:
        ref = kwargs.get("card_ref")
        msg = self.cards.get(ref) if ref else None
        if msg is None:
            msg = next(self._ids)
            if ref:
                self.cards[ref] = msg
        self.posts.append({"kind": kind, "text": text, **kwargs, "message_id": msg})
        return PostResult(kind, chat_id=-1001, message_id=msg, thread_id=7)

    def last(self, ref: str) -> dict[str, Any]:
        return next(p for p in reversed(self.posts) if p.get("card_ref") == ref)


@dataclass
class FakeNotifier:
    sent: list[dict[str, Any]] = field(default_factory=list)
    calls: list[Any] = field(default_factory=list)
    fail: BaseException | None = None
    blocked: bool = False

    async def send(self, chat_id: int, text: str, **kwargs: Any) -> Any:
        if self.fail is not None:
            raise self.fail
        if self.blocked:
            return None
        self.sent.append({"chat_id": chat_id, "text": text, **kwargs})
        return object()

    async def call(self, method: Any, *, chat_id: int, **kwargs: Any) -> Any:
        del kwargs
        self.calls.append((type(method).__name__, chat_id, getattr(method, "message_id", None)))
        return True


@dataclass
class GuardEnv:
    env: SyncEnv
    panel: ConnPanel
    service: IpGuardService
    chat: FakeChat
    notifier: FakeNotifier
    cfg: dict[str, Any]
    node: str

    @property
    def db(self) -> Any:
        return self.env.db

    async def blocked_sub(self, telegram_id: int, n_ips: int = 30) -> tuple[int, int]:
        """A linked subscription whose panel user is connected from ``n_ips`` public IPs on the node."""
        sid = await self.env.linked_sub(telegram_id)
        pid = int((await self.env.sub(sid))["panel_user_id"])
        self.panel.by_node.setdefault(self.node, {})[pid] = ips(n_ips, third=telegram_id)
        return sid, pid

    async def drain(self, rounds: int = 50) -> list[tuple[Job, str]]:
        queue = JobQueue(self.db)
        ctx = JobContext(db=self.db, queue=queue, worker_id=WORKER)
        handlers = {
            **self.env.writer.handlers(),
            **EventRelay(self.env.bus).handlers(),
            CARD_KIND: self.service.card_job,
            NOTIFY_KIND: self.service.notify_job,
            DROP_KIND: DropIpsJob(self.db, self.env.current_api, attention=self.env.attention),
        }
        out: list[tuple[Job, str]] = []
        for _ in range(rounds):
            claimed = await queue.claim("interactive", WORKER, 20) + await queue.claim(
                "background", WORKER, 20
            )
            if not claimed:
                break
            for job in claimed:
                fence = {"worker_id": WORKER, "attempt": job.attempts}
                try:
                    await handlers[job.kind](job, ctx)
                except RetryJob as r:
                    await queue.fail(job.id, str(r), retry_in=max(r.delay, 3600), **fence)
                    out.append((job, "retry"))
                except PermanentJobError as e:
                    await queue.fail(job.id, str(e), permanent=True, **fence)
                    out.append((job, "dead"))
                except Exception as e:
                    await queue.fail(job.id, f"{type(e).__name__}: {e}", retry_in=3600, **fence)
                    out.append((job, "failed"))
                else:
                    await queue.complete(job.id, **fence)
                    out.append((job, "done"))
        return out

    async def one(self, sql: str, *args: Any) -> Mapping[str, Any]:
        rows = await self.db.raw(sql, *args)
        assert rows, sql
        return dict(rows[0])


async def _no_dns(host: str) -> frozenset[Any]:
    return frozenset({ipaddress.ip_address("198.51.100.250")}) if host else frozenset()


@asynccontextmanager
async def guard_env(pg_dsn: str, **cfg: Any) -> AsyncIterator[GuardEnv]:
    panel = ConnPanel()
    async with sync_env(pg_dsn, panel=panel) as env:
        node = panel.add_node("NL-1")
        config: dict[str, Any] = {"IP_GUARD_ENABLED": True, "SUPPORT_URL": "https://t.me/support", **cfg}
        chat = FakeChat()
        notifier = FakeNotifier()
        service = IpGuardService(
            env.db,
            env.current_api,
            config=lambda: config,
            admin_chat=chat,
            notifier=notifier,
            attention=env.attention,
            reader=PanelReader(env.current_api, poll_interval=0.0),
            resolve=_no_dns,
        )
        yield GuardEnv(env, panel, service, chat, notifier, config, node)
