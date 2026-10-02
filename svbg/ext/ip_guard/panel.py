"""IP Guard calls to the panel (05 §2.2.7).

* :class:`PanelReader` — **reads** only: ``GET /nodes`` (with ``ips``, which the core ``Node`` model does not
  carry), ``connections/by-node`` and ``connections/by-user`` jobs. Reads are allowed outside the writer.
* :class:`DropIpsJob` — the one panel mutation of the module, ``POST /connections/drop`` by IP addresses on
  **specific** nodes (never ``allNodes``; shared IPs never get here). It runs as a ``queue='panel'`` job with
  ``ordering_key='sub:<id>'`` right after the core ``panel.disable`` of the block (FIFO per subscription) and
  follows the writer's error policy. It is written to move into :mod:`svbg.remnawave.writer` as
  ``panel.drop_ips`` unchanged (integration request); until then it is the module's job ``ip_guard.drop_ips``.

Connection errors ≥ 400 are logged at ``warning`` (never ``error``: an error log would be routed back into the
admin chat and loop, lesson 9).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import quote

import msgspec
import sqlalchemy as sa

from svbg.core.clock import now
from svbg.db.meta import JSONB
from svbg.ext.ip_guard.tables import ip_guard_blocks
from svbg.ext.ip_guard.window import NodeInfo, NodePoll, normalize_ip, parse_job_status
from svbg.jobs.queue import enqueue
from svbg.jobs.worker import PermanentJobError, RetryJob
from svbg.remnawave.errors import (
    ErrorKind,
    PanelNotConfiguredError,
    PanelUnavailableError,
    RemnawaveError,
    WriteBlockedError,
)
from svbg.remnawave.transport import Lane
from svbg.subscriptions.tables import subscriptions

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from svbg.core.attention import AttentionService
    from svbg.db.engine import Database
    from svbg.jobs.queue import Job
    from svbg.jobs.worker import JobContext
    from svbg.remnawave.api import RemnawaveApi

__all__ = ["DROP_KIND", "DropIpsJob", "PanelReader", "enqueue_drop"]

log = logging.getLogger("svbg.ext.ip_guard.panel")

DROP_KIND: Final = "ip_guard.drop_ips"
POLL_INTERVAL_S: Final = 1.0
DROP_CHUNK: Final = 200
MAX_EVENTS: Final = 100
UNREACHABLE_RETRY_S: Final = 60.0
_UUID_MAX: Final = 64


def _decode(body: bytes) -> Any:
    if not body:
        return None
    try:
        data = msgspec.json.decode(body)
    except msgspec.DecodeError:
        return None
    return data.get("response") if isinstance(data, dict) else None


def _node(raw: Any) -> NodeInfo | None:
    if not isinstance(raw, Mapping) or not isinstance(raw.get("uuid"), str):
        return None
    ips: list[str] = []
    for item in raw.get("ips") or []:
        value = item.get("ip") if isinstance(item, Mapping) else item
        if isinstance(value, str) and value:
            ips.append(value.split("/")[0])
    return NodeInfo(
        uuid=str(raw["uuid"])[:_UUID_MAX],
        name=str(raw.get("name") or "")[:100],
        address=str(raw.get("address") or "")[:255],
        is_connected=raw.get("isConnected") is True,
        is_disabled=raw.get("isDisabled") is True,
        ips=tuple(ips),
    )


def _path_part(value: str) -> str:
    if not value or len(value) > 128 or "/" in value or value in (".", ".."):
        raise ValueError("bad path part")
    return quote(value, safe="")


class PanelReader:
    """Reads for the collector; ``api`` returns the current client (raises when the panel is not set up)."""

    def __init__(self, api: Callable[[], RemnawaveApi], *, poll_interval: float = POLL_INTERVAL_S) -> None:
        self._api = api
        self._poll_interval = poll_interval

    async def _get(self, path: str, scope: str, *, lane: Lane = Lane.BACKGROUND) -> Any:
        raw = await self._api().transport.request("GET", path, idempotent=True, scope=scope, lane=lane)
        return _decode(raw.body)

    async def _post(self, path: str, scope: str, *, lane: Lane = Lane.BACKGROUND) -> Any:
        # Creating a read job is not a state change of the panel: safe to retry.
        raw = await self._api().transport.request("POST", path, idempotent=True, scope=scope, lane=lane)
        return _decode(raw.body)

    async def nodes(self) -> list[NodeInfo]:
        data = await self._get("/nodes", "nodes:list")
        if not isinstance(data, list):
            raise RemnawaveError(ErrorKind.SERVER, None, "BAD_RESPONSE", "список нод не по контракту")
        return [n for n in (_node(x) for x in data) if n is not None]

    async def poll_node(self, node_uuid: str, *, budget_s: float) -> NodePoll:
        """``POST by-node`` → ``GET`` once per second until done; one overall timeout. Never raises."""
        try:
            async with asyncio.timeout(budget_s):
                created = await self._post(
                    f"/connections/by-node/{_path_part(node_uuid)}", "connections:by-node"
                )
                job_id = created.get("jobId") if isinstance(created, Mapping) else None
                if not job_id:
                    return NodePoll(node_uuid, "failed", "no_job")
                path = f"/connections/by-node/{_path_part(str(job_id))}"
                while True:
                    try:
                        payload = await self._get(path, "connections:by-node-result")
                    except RemnawaveError as err:
                        if err.kind is ErrorKind.NOT_FOUND:  # A218: the job is gone
                            return NodePoll(node_uuid, "failed", "job_missing")
                        raise
                    data = payload if isinstance(payload, Mapping) else None
                    if data is not None and data.get("isCompleted") and not data.get("isFailed"):
                        # a finished answer may list thousands of users: parsed off the event loop
                        result = await asyncio.to_thread(parse_job_status, node_uuid, data)
                    else:
                        result = parse_job_status(node_uuid, data)
                    if result is not None:
                        return result
                    await asyncio.sleep(self._poll_interval)
        except TimeoutError:
            return NodePoll(node_uuid, "failed", "timeout")
        except RemnawaveError as err:
            log.warning("ip guard: node %s poll failed: %s", node_uuid, err.kind.value)
            reason = f"http_{err.status}" if err.status else err.kind.value
            return NodePoll(node_uuid, "failed", reason, http_status=err.status)
        except ValueError:
            return NodePoll(node_uuid, "failed", "bad_uuid")

    async def by_user(self, panel_user_id: int, *, budget_s: float = 30.0) -> dict[str, list[str]]:
        """«Показать IP сейчас»: one job over all nodes; node → IPs. Raises on panel errors / timeout."""
        if isinstance(panel_user_id, bool) or panel_user_id <= 0:
            raise ValueError("bad panel user id")
        async with asyncio.timeout(budget_s):
            created = await self._post(
                f"/connections/by-user/{panel_user_id}", "connections:by-user", lane=Lane.INTERACTIVE
            )
            job_id = created.get("jobId") if isinstance(created, Mapping) else None
            if not job_id:
                return {}
            path = f"/connections/by-user/{_path_part(str(job_id))}"
            while True:
                payload = await self._get(path, "connections:by-user-result", lane=Lane.INTERACTIVE)
                if isinstance(payload, Mapping) and payload.get("isCompleted"):
                    return _by_user_result(payload.get("result"))
                if isinstance(payload, Mapping) and payload.get("isFailed"):
                    return {}
                await asyncio.sleep(self._poll_interval)


def _by_user_result(result: Any) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    if not isinstance(result, Mapping):
        return out
    for node in result.get("nodes") or []:
        if not isinstance(node, Mapping) or not isinstance(node.get("nodeUuid"), str):
            continue
        ips = [
            str(e.get("ip") if isinstance(e, Mapping) else e)
            for e in node.get("ips") or []
            if isinstance(e, str | Mapping)
        ]
        out[node["nodeUuid"]] = sorted({i for i in ips if i and i != "None"})
    return out


# -------------------------------------------------------------------------------------------------- drop


async def enqueue_drop(
    conn: AsyncConnection,
    subscription_id: int,
    block_id: int,
    targets: Mapping[str, Sequence[str]],
    *,
    caused_by: str | None = None,
) -> int | None:
    """Queue the drop of ``targets`` (node → IPs) after the disable of the same subscription (FIFO)."""
    clean = {
        str(node)[:_UUID_MAX]: sorted({str(ip) for ip in ips if normalize_ip(str(ip)) is not None})
        for node, ips in targets.items()
    }
    clean = {node: ips for node, ips in clean.items() if ips}
    if not clean:
        return None
    return await enqueue(
        conn,
        DROP_KIND,
        {"sub_id": int(subscription_id), "block_id": int(block_id), "targets": clean},
        queue="panel",
        lane="background",
        ordering_key=f"sub:{int(subscription_id)}",
        max_attempts=20,
        caused_by=caused_by,
    )


def _check_gate(gate: Any) -> None:
    if not gate.writes_allowed:
        raise WriteBlockedError(gate.version)


class DropIpsJob:
    """Handler of :data:`DROP_KIND` (writer semantics: unreachable panel → wait without using attempts)."""

    def __init__(
        self,
        db: Database,
        api: Callable[[], RemnawaveApi],
        *,
        attention: AttentionService | None = None,
    ) -> None:
        self._db = db
        self._api = api
        self._attention = attention

    async def __call__(self, job: Job, ctx: JobContext) -> None:
        block_id = int(job.payload["block_id"])
        targets = job.payload.get("targets") or {}
        async with self._db.read() as conn:
            row = (
                await conn.execute(
                    sa.select(subscriptions.c.hold_kind, ip_guard_blocks.c.status)
                    .select_from(
                        ip_guard_blocks.join(
                            subscriptions, subscriptions.c.id == ip_guard_blocks.c.subscription_id
                        )
                    )
                    .where(ip_guard_blocks.c.id == block_id)
                )
            ).first()
        if row is None or row.status != "active" or row.hold_kind is None:
            log.info("ip guard drop for block %s skipped: block is no longer active", block_id)
            return
        dropped: list[str] = []
        offline: list[str] = []
        try:
            api = self._api()
            gate = await api.ensure_gate(lane=Lane.BACKGROUND)
            _check_gate(gate)
            for node, ips in sorted(targets.items()):
                if not isinstance(ips, list) or not ips:
                    continue
                try:
                    for i in range(0, len(ips), DROP_CHUNK):
                        await self._drop(api, node, [str(x) for x in ips[i : i + DROP_CHUNK]])
                except RemnawaveError as err:
                    if err.kind is ErrorKind.NOT_FOUND:  # A219: the node is not connected now
                        offline.append(node)
                        continue
                    raise
                dropped.append(node)
        except PanelNotConfiguredError as err:
            raise RetryJob(UNREACHABLE_RETRY_S, "панель не подключена") from err
        except (PanelUnavailableError, WriteBlockedError) as err:
            raise RetryJob(max(err.retry_after or 0.0, 5.0), str(err)) from err
        except RemnawaveError as err:
            log.warning("ip guard drop for block %s failed: %s", block_id, err.kind.value)
            if err.kind in (ErrorKind.AUTH, ErrorKind.FORBIDDEN_SCOPE, ErrorKind.VALIDATION):
                await self._note(block_id, {"kind": "drop_failed", "error": err.kind.value})
                if self._attention is not None:
                    await self._attention.raise_item(
                        "ip_guard:scopes",
                        "error",
                        "IP Guard: нет прав на разрыв соединений",
                        "Панель отклонила connections/drop. Выдайте токену скоуп connections:drop.",
                    )
                raise PermanentJobError(str(err)) from err
            if err.retry_after:
                raise RetryJob(err.retry_after, str(err)) from err
            raise
        await self._note(block_id, {"kind": "dropped", "nodes": len(dropped), "offline": len(offline)})

    @staticmethod
    async def _drop(api: RemnawaveApi, node: str, ips: list[str]) -> None:
        body = {
            "dropBy": {"by": "ipAddresses", "ipAddresses": ips},
            "targetNodes": {"target": "specificNodes", "nodeUuids": [node]},
        }
        await api.transport.request(
            "POST",
            "/connections/drop",
            json_body=msgspec.json.encode(body),
            idempotent=True,
            scope="connections:drop",
            lane=Lane.BACKGROUND,
        )

    async def _note(self, block_id: int, event: Mapping[str, Any]) -> None:
        await append_event(self._db, block_id, event)


async def append_event(db: Database, block_id: int, event: Mapping[str, Any]) -> None:
    """Append to ``ip_guard_blocks.events`` (bounded) in its own transaction."""
    async with db.tx() as conn:
        await append_event_tx(conn, block_id, event)


async def append_event_tx(conn: AsyncConnection, block_id: int, event: Mapping[str, Any]) -> None:
    """One more entry in the block's short log (the oldest one goes when there are :data:`MAX_EVENTS`)."""
    entry = sa.cast(sa.literal(json.dumps([{"at": now().isoformat(), **event}], default=str)), JSONB)
    events = ip_guard_blocks.c.events
    await conn.execute(
        sa.update(ip_guard_blocks)
        .where(ip_guard_blocks.c.id == block_id)
        .values(
            events=sa.case(
                (
                    sa.func.jsonb_array_length(events) >= MAX_EVENTS,
                    events.op("-")(sa.literal_column("0")).op("||")(entry),
                ),
                else_=events.op("||")(entry),
            )
        )
    )
