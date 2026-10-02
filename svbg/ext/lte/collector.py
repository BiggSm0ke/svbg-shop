"""LTE quotas: reading the panel and storing one accounting cycle (05 §2.1.8).

Reads (allowed outside the writer, nothing here changes the panel):

* ``GET /nodes`` and ``GET /internal-squads`` (raw JSON: the core models do not carry ``createdAt``,
  ``xrayUptime``, ``configProfile.activeInbounds`` or the squads' inbounds) — :class:`PanelReader.topology`;
* ``POST /bandwidth-stats/nodes/usage`` **only for the nodes of LTE groups**, one call per UTC date for every
  node that needs it (:meth:`svbg.remnawave.api.RemnawaveApi.node_usage`).

:class:`Collector` turns the reads into the inputs of the pure engine
(:func:`svbg.ext.lte.accounting.account_cycle`) and writes the result in **one transaction**: UPSERT of the
changed counters only, increments of ``lte_period_usage`` / ``lte_usage_hourly`` / ``lte_usage_daily`` and
the read marks of ``lte_node_state`` (moved only when every date of a node was read). A failed read of a date
is incompleteness (new blocks still allowed); anomalies (regress, rows vanished) hold new blocks of the group.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import msgspec
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from svbg.core.clock import now
from svbg.ext.lte.accounting import (
    CYCLE_INTERVAL_SECONDS,
    AccountingParams,
    CounterKey,
    CounterState,
    CycleResult,
    GroupState,
    NodeCycleInput,
    NodeMembership,
    NodeReadContext,
    PeriodSpan,
    SubjectSpans,
    UsageRow,
    account_cycle,
    batch_requests,
    build_group_states,
    node_read_plan,
    parse_usage_response,
)
from svbg.ext.lte.tables import (
    lte_blocks,
    lte_counters,
    lte_group_nodes,
    lte_node_state,
    lte_period_usage,
    lte_periods,
    lte_usage_daily,
    lte_usage_hourly,
)
from svbg.remnawave.errors import ErrorKind, RemnawaveError
from svbg.remnawave.transport import Lane
from svbg.subscriptions.tables import subscriptions

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from svbg.db.engine import Database
    from svbg.remnawave.api import RemnawaveApi

__all__ = [
    "CollectOutcome",
    "Collector",
    "NodeFacts",
    "PanelReader",
    "Topology",
    "load_memberships",
]

log = logging.getLogger("svbg.ext.lte.collector")

#: A node without a good read for this long is treated as a bot-side gap (the start of the delta is pinned).
PANEL_GAP: Final = timedelta(seconds=2 * CYCLE_INTERVAL_SECONDS + 60)
#: Periods that ended this long ago still receive late deltas (allocation of a delta across a boundary).
SPAN_LOOKBACK: Final = timedelta(days=3)
READ_TIMEOUT_S: Final = 60.0
_UUID_MAX: Final = 64


# ----------------------------------------------------------------------------------------- topology


@dataclass(frozen=True, slots=True)
class NodeFacts:
    uuid: str
    name: str = ""
    connected: bool = True
    disabled: bool = False
    created_at: datetime | None = None
    xray_uptime_s: int | None = None
    inbounds: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class Topology:
    """Nodes with their active inbounds and squads with theirs (inbound uuids)."""

    nodes: Mapping[str, NodeFacts]
    squad_inbounds: Mapping[str, frozenset[str]]
    squad_names: Mapping[str, str] = field(default_factory=dict)
    loaded_at: datetime | None = None

    def group_tags(self, group_nodes: Mapping[int, Collection[str]]) -> dict[int, frozenset[str]]:
        """tags(G): active inbounds of the group's nodes."""
        out: dict[int, frozenset[str]] = {}
        for group_id, nodes in group_nodes.items():
            tags: set[str] = set()
            for node in nodes:
                facts = self.nodes.get(str(node).lower())
                if facts is not None:
                    tags |= facts.inbounds
            out[group_id] = frozenset(tags)
        return out

    def node_inbounds(self) -> dict[str, frozenset[str]]:
        return {uuid: facts.inbounds for uuid, facts in self.nodes.items()}


def _decode(body: bytes) -> Any:
    if not body:
        return None
    try:
        data = msgspec.json.decode(body)
    except msgspec.DecodeError:
        return None
    return data.get("response") if isinstance(data, dict) else None


def _dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _inbound_ids(items: Any) -> frozenset[str]:
    out: set[str] = set()
    for item in items or ():
        if isinstance(item, Mapping):
            value = item.get("uuid") or item.get("tag")
            if isinstance(value, str) and value:
                out.add(value[:_UUID_MAX])
        elif isinstance(item, str) and item:
            out.add(item[:_UUID_MAX])
    return frozenset(out)


def parse_nodes(data: Any) -> dict[str, NodeFacts]:
    out: dict[str, NodeFacts] = {}
    for raw in data if isinstance(data, list) else ():
        if not isinstance(raw, Mapping) or not isinstance(raw.get("uuid"), str):
            continue
        uuid = str(raw["uuid"]).lower()[:_UUID_MAX]
        profile = raw.get("configProfile") if isinstance(raw.get("configProfile"), Mapping) else {}
        uptime = raw.get("xrayUptime")
        out[uuid] = NodeFacts(
            uuid=uuid,
            name=str(raw.get("name") or "")[:100],
            connected=raw.get("isConnected") is True,
            disabled=raw.get("isDisabled") is True,
            created_at=_dt(raw.get("createdAt")),
            xray_uptime_s=int(uptime)
            if isinstance(uptime, int | float) and not isinstance(uptime, bool)
            else None,
            inbounds=_inbound_ids(profile.get("activeInbounds")),
        )
    return out


def parse_squads(data: Any) -> tuple[dict[str, frozenset[str]], dict[str, str]]:
    items = data.get("internalSquads") if isinstance(data, Mapping) else data
    inbounds: dict[str, frozenset[str]] = {}
    names: dict[str, str] = {}
    for raw in items if isinstance(items, list) else ():
        if not isinstance(raw, Mapping) or not isinstance(raw.get("uuid"), str):
            continue
        uuid = str(raw["uuid"])[:_UUID_MAX]
        inbounds[uuid] = _inbound_ids(raw.get("inbounds"))
        names[uuid] = str(raw.get("name") or "")[:100]
    return inbounds, names


class PanelReader:
    """Reads for the module; ``api`` returns the current client (raises when the panel is not set up)."""

    def __init__(self, api: Callable[[], RemnawaveApi], *, timeout_s: float = READ_TIMEOUT_S) -> None:
        self._api = api
        self._timeout = timeout_s

    async def _get(self, path: str, scope: str) -> Any:
        async with asyncio.timeout(self._timeout):
            raw = await self._api().transport.request(
                "GET", path, idempotent=True, scope=scope, lane=Lane.BACKGROUND
            )
        return _decode(raw.body)

    async def topology(self) -> Topology:
        nodes = await self._get("/nodes", "nodes:list")
        if not isinstance(nodes, list):
            raise RemnawaveError(ErrorKind.SERVER, None, "BAD_RESPONSE", "список нод не по контракту")
        squads = await self._get("/internal-squads", "internal-squads:list")
        inbounds, names = parse_squads(squads)
        return Topology(nodes=parse_nodes(nodes), squad_inbounds=inbounds, squad_names=names, loaded_at=now())

    async def usage(
        self, usage_date: date, node_uuids: Sequence[str]
    ) -> tuple[dict[str, tuple[UsageRow, ...]], datetime | None]:
        async with asyncio.timeout(self._timeout):
            answer = await self._api().node_usage(usage_date, node_uuids)
        return parse_usage_response(answer.payload), answer.date


# ------------------------------------------------------------------------------------------ memberships


async def load_memberships(
    conn: AsyncConnection, *, since: datetime | None = None
) -> dict[str, tuple[NodeMembership, ...]]:
    """Membership spans per node (only spans that are open or ended after ``since``)."""
    t = lte_group_nodes
    q = sa.select(t.c.node_uuid, t.c.group_id, t.c.counted_from, t.c.counted_to).where(
        sa.or_(t.c.counted_to.is_(None), t.c.counted_to > t.c.counted_from)
    )
    if since is not None:
        q = q.where(sa.or_(t.c.counted_to.is_(None), t.c.counted_to > since))
    out: dict[str, list[NodeMembership]] = {}
    for r in (await conn.execute(q.order_by(t.c.node_uuid, t.c.counted_from))).all():
        out.setdefault(str(r.node_uuid), []).append(
            NodeMembership(int(r.group_id), r.counted_from, r.counted_to)
        )
    return {node: tuple(items) for node, items in out.items()}


# ------------------------------------------------------------------------------------------ the cycle


@dataclass(frozen=True, slots=True)
class CollectOutcome:
    """One accounting cycle: what was read and stored, and the flags of every group for ``decide``."""

    read_at: datetime
    group_states: Mapping[int, GroupState]
    result: CycleResult | None = None
    requests: int = 0
    failed_requests: int = 0
    gap_estimated: Mapping[tuple[int, int], int] = field(default_factory=dict)  # (sub, group) → bytes
    clamped: frozenset[tuple[int, int]] = frozenset()  # (sub, group) with an implausible delta
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.failed_requests == 0


class Collector:
    def __init__(self, db: Database, reader: PanelReader) -> None:
        self._db = db
        self._reader = reader

    async def collect(
        self,
        *,
        group_nodes: Mapping[int, Collection[str]],
        params: AccountingParams,
        topology: Topology | None = None,
        at: datetime | None = None,
    ) -> CollectOutcome:
        """Read the history of the nodes of ``group_nodes`` and store one cycle."""
        at = at or now()
        nodes = sorted({str(n).lower() for ns in group_nodes.values() for n in ns})
        if not nodes:
            return CollectOutcome(read_at=at, group_states={})
        async with self._db.read() as conn:
            memberships = await load_memberships(conn, since=at - timedelta(days=params.max_catchup_days))
            states = {
                str(r.node_uuid): r
                for r in (
                    await conn.execute(sa.select(lte_node_state).where(lte_node_state.c.node_uuid.in_(nodes)))
                ).all()
            }
        first_module_cycle = not states
        facts = topology.nodes if topology is not None else {}
        plans = []
        inputs: list[NodeCycleInput] = []
        disconnected: list[str] = []
        mark_updates: dict[str, dict[str, Any]] = {}
        for node in nodes:
            st = states.get(node)
            last_ok = st.last_ok_read_at if st is not None else None
            plan = node_read_plan(
                node_uuid=node,
                panel_now=at,
                last_ok_read_at=last_ok,
                max_catchup_days=params.max_catchup_days,
            )
            plans.append(plan)
            info = facts.get(node)
            connected = info.connected if info is not None else True
            gap_anchor = st.gap_anchor_at if st is not None else None
            gap_tail = int(st.gap_tail or 0) if st is not None else 0
            since = st.disconnected_since if st is not None else None
            reconnected = False
            update: dict[str, Any] = {}
            if not connected:
                disconnected.append(node)
                if since is None:
                    update.update(disconnected_since=at, gap_anchor_at=last_ok)
                    gap_anchor = last_ok
            elif since is not None:
                reconnected = True
                gap_tail = params.gap_tail_cycles
                update.update(disconnected_since=None, gap_tail=gap_tail)
            elif gap_tail > 0:
                reconnected = True
            panel_gap = last_ok is not None and at - last_ok > PANEL_GAP
            if panel_gap and gap_anchor is None:
                gap_anchor = last_ok
            if info is not None and info.xray_uptime_s is not None:
                update["xray_uptime_s"] = info.xray_uptime_s
            mark_updates[node] = {**update, "_tail": gap_tail, "_reconnected": reconnected}
            inputs.append(
                NodeCycleInput(
                    plan=plan,
                    context=NodeReadContext(
                        node_uuid=node,
                        first_read_at=st.first_read_at if st is not None else None,
                        node_created_at=info.created_at if info is not None else None,
                        prev_cycle_read_at=last_ok,
                        is_first_module_cycle=first_module_cycle,
                    ),
                    last_ok_read_at=last_ok,
                    gap_anchor_at=gap_anchor,
                    node_reconnected=reconnected,
                    panel_gap=panel_gap,
                    memberships=memberships.get(node, ()),
                )
            )
        readings: dict[tuple[str, date], Sequence[UsageRow] | None] = {}
        requests = failed = 0
        panel_date: datetime | None = None
        for req in batch_requests(plans):
            requests += 1
            try:
                rows, stamp = await self._reader.usage(req.usage_date, req.node_uuids)
            except (RemnawaveError, TimeoutError, OSError) as err:
                failed += 1
                log.warning("lte: nodes/usage for %s failed: %s", req.usage_date, type(err).__name__)
                for node in req.node_uuids:
                    readings[(node, req.usage_date)] = None
                continue
            panel_date = panel_date or stamp
            for node in req.node_uuids:
                readings[(node, req.usage_date)] = rows.get(node, ())
        read_at = (
            panel_date if panel_date is not None and abs((panel_date - at).total_seconds()) < 600 else at
        )
        users = {row.panel_user_id for rows in readings.values() if rows for row in rows}
        async with self._db.read() as conn:
            counters = await self._counters(conn, readings)
            subjects, known = await self._subjects(conn, users, read_at)
        result = account_cycle(
            read_at=read_at,
            nodes=inputs,
            readings=readings,
            counters=counters,
            subjects=subjects,
            params=params,
            known_users=known,
        )
        async with self._db.tx() as conn:
            await self._store(conn, result, mark_updates, read_at)
        states_by_group = build_group_states(
            group_nodes=group_nodes,
            coverage=result.coverage,
            disconnected_node_uuids=disconnected,
            catchup_limited_node_uuids=result.catchup_limited,
            node_anomalies=result.node_anomalies,
        )
        gap: dict[tuple[int, int], int] = {}
        period_owner = {p.period_id: s.subscription_id for s in subjects.values() for p in s.periods}
        for (period_id, group_id), value in result.usage.gap_estimated.items():
            sid = period_owner.get(period_id)
            if sid is not None:
                gap[(sid, group_id)] = gap.get((sid, group_id), 0) + value
        return CollectOutcome(
            read_at=read_at,
            group_states=states_by_group,
            result=result,
            requests=requests,
            failed_requests=failed,
            gap_estimated=gap,
            clamped=result.clamped,
        )

    # ---------------------------------------------------------------------------------------- loads

    @staticmethod
    async def _counters(
        conn: AsyncConnection, readings: Mapping[tuple[str, date], Any]
    ) -> dict[CounterKey, CounterState]:
        nodes = sorted({node for node, _ in readings})
        dates = sorted({day for _, day in readings})
        if not nodes:
            return {}
        t = lte_counters
        rows = (
            await conn.execute(sa.select(t).where(t.c.node_uuid.in_(nodes), t.c.usage_date.in_(dates)))
        ).mappings()
        out: dict[CounterKey, CounterState] = {}
        for r in rows:
            out[CounterKey(r["node_uuid"], r["usage_date"], int(r["panel_user_id"]))] = CounterState(
                total_bytes=int(r["total"]),
                baseline_bytes=int(r["baseline"]),
                accounted_bytes=int(r["accounted"]),
                carry_bytes=int(r["carry"]),
                vanished_at=r["vanished_at"],
                exists=True,
            )
        return out

    @staticmethod
    async def _subjects(
        conn: AsyncConnection, users: Iterable[int], at: datetime
    ) -> tuple[dict[int, SubjectSpans], frozenset[int]]:
        ids = sorted(users)
        if not ids:
            return {}, frozenset()
        s = subscriptions.c
        subs = {
            int(r.panel_user_id): int(r.id)
            for r in (
                await conn.execute(sa.select(s.id, s.panel_user_id).where(s.panel_user_id.in_(ids)))
            ).all()
        }
        sids = sorted(subs.values())
        periods: dict[int, list[PeriodSpan]] = {}
        blocked: dict[int, dict[int, datetime]] = {}
        if sids:
            p = lte_periods.c
            rows = (
                await conn.execute(
                    sa.select(lte_periods)
                    .where(
                        p.subscription_id.in_(sids),
                        sa.or_(p.state != "closed", p.ended_at > at - SPAN_LOOKBACK),
                    )
                    .order_by(p.subscription_id, p.starts_at)
                )
            ).mappings()
            for r in rows:
                periods.setdefault(int(r["subscription_id"]), []).append(
                    PeriodSpan(
                        period_id=int(r["id"]),
                        starts_at=r["starts_at"],
                        planned_end_at=r["planned_end_at"],
                        ended_at=r["ended_at"],
                        state=r["state"],
                        series_first=r["idx"] == 0 and r["starts_at"] == r["anchor_at"],
                        anchor_at=r["anchor_at"],
                        is_trial=bool(r["is_trial"]),
                    )
                )
            b = lte_blocks.c
            for r in (
                await conn.execute(
                    sa.select(b.subscription_id, b.group_id, b.applied_at).where(
                        b.subscription_id.in_(sids),
                        b.status == "active",
                        b.mode == "enforce",
                        b.applied_at.is_not(None),
                    )
                )
            ).all():
                blocked.setdefault(int(r.subscription_id), {})[int(r.group_id)] = r.applied_at
        out = {
            pid: SubjectSpans(
                subscription_id=sid, periods=tuple(periods.get(sid, ())), blocked_since=blocked.get(sid, {})
            )
            for pid, sid in subs.items()
        }
        return out, frozenset(subs)

    # ---------------------------------------------------------------------------------------- store

    @staticmethod
    async def _store(
        conn: AsyncConnection, result: CycleResult, marks: Mapping[str, Mapping[str, Any]], read_at: datetime
    ) -> None:
        if result.counters:
            t = lte_counters
            rows = [
                {
                    "node_uuid": w.key.node_uuid,
                    "usage_date": w.key.usage_date,
                    "panel_user_id": w.key.panel_user_id,
                    "total": w.state.total_bytes,
                    "accounted": w.state.accounted_bytes,
                    "baseline": w.state.baseline_bytes,
                    "carry": w.state.carry_bytes,
                    "vanished_at": w.state.vanished_at,
                    "seen_at": read_at,
                }
                for w in result.counters
            ]
            stmt = pg_insert(t)
            stmt = stmt.on_conflict_do_update(
                index_elements=[t.c.node_uuid, t.c.usage_date, t.c.panel_user_id],
                set_={
                    c: stmt.excluded[c]
                    for c in ("total", "accounted", "baseline", "carry", "vanished_at", "seen_at")
                },
            )
            await conn.execute(stmt, rows)
        usage = result.usage
        if usage.period_usage:
            t = lte_period_usage
            stmt = pg_insert(t)
            stmt = stmt.on_conflict_do_update(
                index_elements=[t.c.period_id, t.c.group_id],
                set_={
                    "used_bytes": t.c.used_bytes + stmt.excluded.used_bytes,
                    "after_block_bytes": t.c.after_block_bytes + stmt.excluded.after_block_bytes,
                    "estimated_bytes": t.c.estimated_bytes + stmt.excluded.estimated_bytes,
                    "last_delta_at": sa.func.greatest(t.c.last_delta_at, stmt.excluded.last_delta_at),
                },
            )
            await conn.execute(
                stmt,
                [
                    {
                        "period_id": pid,
                        "group_id": gid,
                        "used_bytes": d.used_bytes,
                        "after_block_bytes": d.after_block_bytes,
                        "estimated_bytes": d.estimated_bytes,
                        "last_delta_at": d.last_delta_at,
                    }
                    for (pid, gid), d in sorted(usage.period_usage.items())
                ],
            )
        if usage.hourly:
            t = lte_usage_hourly
            stmt = pg_insert(t)
            stmt = stmt.on_conflict_do_update(
                index_elements=[t.c.subscription_id, t.c.group_id, t.c.hour_utc],
                set_={"bytes": t.c.bytes + stmt.excluded.bytes},
            )
            await conn.execute(
                stmt,
                [
                    {"subscription_id": sid, "group_id": gid, "hour_utc": hour, "bytes": value}
                    for (sid, gid, hour), value in sorted(usage.hourly.items())
                ],
            )
        if usage.daily:
            t = lte_usage_daily
            stmt = pg_insert(t)
            stmt = stmt.on_conflict_do_update(
                index_elements=[t.c.subscription_id, t.c.group_id, t.c.msk_date],
                set_={
                    "bytes": t.c.bytes + stmt.excluded.bytes,
                    "after_block_bytes": t.c.after_block_bytes + stmt.excluded.after_block_bytes,
                },
            )
            await conn.execute(
                stmt,
                [
                    {
                        "subscription_id": sid,
                        "group_id": gid,
                        "msk_date": day,
                        "bytes": d.bytes_value,
                        "after_block_bytes": d.after_block_bytes,
                    }
                    for (sid, gid, day), d in sorted(usage.daily.items())
                ],
            )
        read_ok = {m.node_uuid: m for m in result.marks}
        t = lte_node_state
        for node, extra in sorted(marks.items()):
            values = {k: v for k, v in extra.items() if not k.startswith("_")}
            mark = read_ok.get(node)
            tail = int(extra.get("_tail") or 0)
            if mark is not None:
                values.update(
                    first_read_at=mark.first_read_at,
                    last_ok_read_at=mark.last_ok_read_at,
                    last_ok_read_date=mark.last_ok_read_date,
                )
                if extra.get("_reconnected") and "disconnected_since" not in values:
                    tail = max(0, tail - 1)
                    values["gap_tail"] = tail
                    if tail == 0:
                        values["gap_anchor_at"] = None
                elif "disconnected_since" not in values and values.get("gap_anchor_at") is None:
                    values["gap_anchor_at"] = None
            if not values:
                continue
            stmt = pg_insert(t).values(node_uuid=node, **values)
            stmt = stmt.on_conflict_do_update(index_elements=[t.c.node_uuid], set_=values)
            await conn.execute(stmt)
