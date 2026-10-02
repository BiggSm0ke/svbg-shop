"""IP Guard runtime (05 §2.2.3): the collection pass, block / unblock / close, the anomaly fuse, cards,
user notifications, exempt list, CDN flags and health.

Transactions (each one atomic, panel calls never inside):

* **block** — ``ip_guard_blocks`` row (partial UNIQUE: one active block per subscription) + core
  :func:`~svbg.subscriptions.hold.freeze` with ``hold_kind='ip_guard'`` (which queues ``panel.disable``) +
  :data:`~svbg.ext.ip_guard.panel.DROP_KIND` (``connections/drop`` by IP on specific nodes, FIFO after the
  disable) + the card job + the user notification job;
* **unblock** (admin button only) — ``panel.revoke`` first when a new link is wanted (the account is still
  DISABLED), then core :func:`~svbg.subscriptions.hold.unfreeze` (``paid_until = now + frozen``, expire PATCH,
  enable — the writer skips the enable when the term is over) + the block becomes ``unblocked`` + card and
  notification jobs; afterwards the window and the counters of that user are reset and the grace starts;
* **close** — core :func:`~svbg.subscriptions.hold.zero_hold`: the account stays DISABLED, the term is zeroed.

Cards are ``ip_guard.card`` jobs (durable, deduplicated per card): the handler renders the card **from the
database** and posts it to the admin chat topic ``antiabuse`` as an editable card (``card_ref``).
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Literal

import sqlalchemy as sa
from aiogram.methods import PinChatMessage, UnpinChatMessage
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.dialects.postgresql import insert as pg_insert

from svbg.core.clock import now
from svbg.core.component import HealthReport
from svbg.core.tables import admin_audit, users
from svbg.ext.ip_guard import texts
from svbg.ext.ip_guard.config import Params
from svbg.ext.ip_guard.decide import (
    AnomalyPlan,
    AnomalyRecord,
    BlockOutcome,
    DbState,
    DeciderMemory,
    DismissInfo,
    GraceInfo,
    LastWarning,
    WarningDraft,
    decide,
    plan_warnings,
)
from svbg.ext.ip_guard.panel import PanelReader, append_event_tx, enqueue_drop
from svbg.ext.ip_guard.tables import (
    ACTIVE_BLOCK_PREDICATE,
    ip_guard_alerts,
    ip_guard_blocks,
    ip_guard_exempt,
    ip_guard_nodes,
)
from svbg.ext.ip_guard.window import (
    NodeInfo,
    NodePoll,
    PassResult,
    UserStats,
    Window,
    parse_ip,
    prepare_polls,
)
from svbg.jobs.queue import enqueue
from svbg.remnawave.errors import RemnawaveError
from svbg.remnawave.writer import K_DISABLE, K_REVOKE, enqueue_action
from svbg.subscriptions.hold import freeze, unfreeze, zero_hold
from svbg.subscriptions.lifecycle import SubscriptionError
from svbg.subscriptions.tables import subscriptions

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from svbg.core.attention import AttentionService
    from svbg.db.engine import Database
    from svbg.jobs.queue import Job
    from svbg.jobs.worker import JobContext
    from svbg.remnawave.api import RemnawaveApi
    from svbg.tg.report import Report

__all__ = [
    "CARD_KIND",
    "NOTIFY_KIND",
    "TOPIC",
    "ActionResult",
    "IpGuardService",
    "PassSummary",
]

log = logging.getLogger("svbg.ext.ip_guard")

TOPIC: Final = "antiabuse"
CARD_KIND: Final = "ip_guard.card"
NOTIFY_KIND: Final = "ip_guard.notify"
PURGE_KIND: Final = "ip_guard.purge"
EVIDENCE_TOP: Final = 50
DISMISS_FOR: Final = timedelta(hours=6)
BYPASS_COOLDOWN: Final = timedelta(minutes=30)
BYPASS_AFTER: Final = timedelta(minutes=2)
RESIDUAL_PASSES: Final = 3
REMIND_EVERY: Final = timedelta(minutes=30)
HEALTH_DOWN_MIN: Final = timedelta(minutes=10)
NODE_FAIL_ALERT: Final = 10
LIVE_STATES: Final = ("pending", "linked", "panel_missing")
ATT_PREFIX: Final = "ip_guard:"

OwnerIds = Callable[[], Awaitable[frozenset[int]]]


@dataclass(frozen=True, slots=True)
class ActionResult:
    """What an admin button did; ``text`` is the toast."""

    ok: bool
    text: str
    code: str = ""


@dataclass(slots=True)
class PassSummary:
    nodes_ok: int = 0
    nodes_failed: int = 0
    watched: int = 0
    blocks: list[int] = field(default_factory=list)
    alerts: int = 0
    anomaly: bool = False
    skipped: str | None = None


@dataclass(frozen=True, slots=True)
class _Sub:
    id: int
    user_id: int | None
    panel_user_id: int
    hold_kind: str | None


_TOASTS: Final = {
    "blocked": "Заблокировано",
    "already_blocked": "Уже заблокирован",
    "unblocked": "Разблокировано",
    "already": "Уже сделано",
    "gone": "Не найдено",
    "closed": "Блок закрыт",
    "acked": "Отмечено",
    "dismissed": "Отмечено как ложная тревога",
    "changed": "Список изменился — карточка обновлена, проверьте ещё раз",
    "no_sub": "Аккаунт панели не привязан к подписке бота",
    "failed": "Не получилось, попробуйте ещё раз",
    "no_reason": "Укажите причину",
}


async def _audit(
    conn: Any,
    action: str,
    subscription_id: int,
    *,
    actor_id: int | None,
    role: str | None,
    reason: str | None,
    details: Mapping[str, Any],
) -> None:
    """One ``admin_audit`` row in the caller's transaction (target ``sub:<id>``; NULL actor = system)."""
    await conn.execute(
        sa.insert(admin_audit).values(
            actor_id=actor_id,
            role=role,
            action=action,
            target=f"sub:{subscription_id}",
            reason=None if reason is None else reason[:500],
            details=dict(details),
        )
    )


def _json_metrics(st: UserStats, draft: WarningDraft | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "ip_count": st.ip_count,
        "live_ip_count": st.live_ip_count,
        "subnet_count": st.subnet_count,
        "complete": st.complete,
    }
    if draft is not None:
        if draft.missing_nodes:
            out["missing_nodes"] = list(draft.missing_nodes)[:10]
        if draft.sustained_left is not None:
            out["sustained_left"] = draft.sustained_left
        if draft.fail_reason:
            out["fail_reason"] = draft.fail_reason[:200]
        if draft.precondition_reason:
            out["precondition"] = draft.precondition_reason[:200]
        if draft.grace is not None:
            out["until"] = draft.grace.until.isoformat()
        if draft.dismissed is not None:
            out["until"] = draft.dismissed.until.isoformat()
    return out


class IpGuardService:
    """One instance per process (the window and the decider memory live here)."""

    def __init__(
        self,
        db: Database,
        api: Callable[[], RemnawaveApi],
        *,
        config: Callable[[], Mapping[str, Any]],
        admin_chat: Any = None,
        notifier: Any = None,
        attention: AttentionService | None = None,
        reader: PanelReader | None = None,
        resolve: Callable[[str], Awaitable[frozenset[ipaddress.IPv4Address | ipaddress.IPv6Address]]]
        | None = None,
    ) -> None:
        self.db = db
        self._api = api
        self._config = config
        self.admin_chat = admin_chat
        self.notifier = notifier
        self.attention = attention
        self.reader = reader or PanelReader(api)
        self.window = Window()
        self.memory = DeciderMemory()
        self._dns = resolve or _resolve_host
        self._resolved: dict[str, tuple[float, frozenset[Any]]] = {}
        self._residual: dict[int, int] = {}
        self._bypass_at: dict[int, datetime] = {}
        self._reminded_at: datetime | None = None
        self._last_ok_at: datetime | None = None
        self._down_since: datetime | None = None
        self._down_alerted = False
        self._last_pass: PassSummary | None = None
        self._lock = asyncio.Lock()

    # --------------------------------------------------------------------------------------- settings

    def params(self, whitelist: frozenset[int] = frozenset()) -> Params:
        return Params.from_config(self._config(), whitelist=whitelist)

    def support_url(self) -> str | None:
        try:
            value = self._config().get("SUPPORT_URL")
        except Exception:  # noqa: BLE001 - an odd snapshot means "no button", never a crash
            return None
        return value if isinstance(value, str) and value.startswith(("https://", "tg://")) else None

    def reset(self) -> None:
        """Module switched off: forget the window and the counters (no stale confirmations later)."""
        self.window = Window()
        self.memory = DeciderMemory()
        self._residual.clear()

    # ------------------------------------------------------------------------------------- the pass

    async def run_pass(self) -> PassSummary:
        """One collection pass (scheduler, every 60 s). Serialized: a slow pass never overlaps the next."""
        if self._lock.locked():
            return PassSummary(skipped="busy")
        async with self._lock:
            summary = await self._pass()
            self._last_pass = summary
            return summary

    async def _pass(self) -> PassSummary:
        params = self.params()
        cp = params.collector
        at = now()
        try:
            nodes = await self.reader.nodes()
        except (RemnawaveError, ValueError) as err:
            reason = getattr(err, "kind", None)
            result = self.window.apply_pass(
                [], {}, frozenset(), cp, now=at, nodes_error=str(getattr(reason, "value", "error"))
            )
            await self._health(result, ())
            return PassSummary(skipped="nodes")
        excluded = await self._sync_nodes(nodes)
        infra = await self._infra_ips(nodes)
        candidates = [n for n in nodes if n.is_connected and not n.is_disabled and n.uuid not in excluded]
        sem = asyncio.Semaphore(max(1, cp.node_concurrency))

        async def poll(uuid: str) -> NodePoll:
            async with sem:
                return await self.reader.poll_node(uuid, budget_s=cp.job_timeout_s)

        polls = {p.uuid: p for p in await asyncio.gather(*(poll(n.uuid) for n in candidates))}
        # ipaddress parsing is the heavy part (≈ 1 s per 100k addresses): off the event loop. Pure — it only
        # reads this pass's answers; the window itself is changed below, on the loop (passes are serialized).
        prepared = await asyncio.to_thread(prepare_polls, list(polls.values()), infra, cp)
        result = self.window.apply_pass(
            nodes, polls, infra, cp, now=now(), excluded=frozenset(excluded), prepared=prepared
        )
        summary = PassSummary(
            nodes_ok=sum(1 for p in result.polls.values() if p.status == "ok"),
            nodes_failed=sum(1 for p in result.polls.values() if p.status == "failed"),
        )
        watched = {pid: st for pid, st in result.stats.items() if st.ip_count >= params.decision.warn_ips}
        summary.watched = len(watched)
        subs = await self._subs_by_panel_id(watched)
        state, whitelist = await self._db_state(params, subs, result.at)
        params = self.params(whitelist)
        plan = decide(watched, self.memory, state, params.decision, any_ok=result.any_ok)
        outcomes: dict[int, BlockOutcome] = {}
        for pid in plan.blocks:
            sub = subs.get(pid)
            if sub is None:
                outcomes[pid] = BlockOutcome("failed", "аккаунт панели не привязан к подписке бота")
                continue
            outcomes[pid] = await self.block(
                sub.id,
                reason="auto",
                stats=watched[pid],
                expected_panel_user=pid,
            )
            if outcomes[pid].status == "blocked":
                summary.blocks.append(sub.id)
        warnings = plan_warnings(plan, outcomes, state, params.decision)
        self.memory = warnings.memory
        summary.alerts = await self._write_alerts(warnings.individual, warnings.summary, subs)
        if plan.anomaly is not None:
            summary.anomaly = True
            await self._apply_anomaly(plan.anomaly, subs, watched)
        await self._remind_quarantine(state)
        await self._check_active_blocks(result)
        await self._health(result, warnings.failing_blocks)
        return summary

    # ------------------------------------------------------------------------------------ pass helpers

    async def _sync_nodes(self, nodes: Sequence[NodeInfo]) -> set[str]:
        """Known nodes + CDN flags (1 SQL); new nodes are inserted and raise «пометьте CDN»."""
        async with self.db.tx() as conn:
            rows = (await conn.execute(sa.select(ip_guard_nodes.c.node_uuid, ip_guard_nodes.c.cdn))).all()
            known = {r.node_uuid: bool(r.cdn) for r in rows}
            new = [n for n in nodes if n.uuid not in known]
            if new:
                await conn.execute(
                    pg_insert(ip_guard_nodes)
                    .values([{"node_uuid": n.uuid, "name": n.name, "address": n.address} for n in new])
                    .on_conflict_do_nothing()
                )
        if new and known and self.attention is not None:
            names = ", ".join(n.name or n.uuid[:8] for n in new[:10])
            await self._raise(
                "new_nodes",
                "info",
                "IP Guard: новые ноды",
                f"Появились ноды: {names}. Если это CDN — отметьте в «🛡 IP Guard → Ноды», иначе разрыв по IP "
                "заденет чужих людей.",
            )
        return {u for u, cdn in known.items() if cdn}

    async def _infra_ips(self, nodes: Sequence[NodeInfo]) -> frozenset[Any]:
        ips: set[Any] = set()
        for node in nodes:
            for raw in node.ips:
                ip = parse_ip(raw)
                if ip is not None:
                    ips.add(ip)
            address = node.address.strip()
            if not address:
                continue
            literal = parse_ip(address)
            if literal is not None:
                ips.add(literal)
                continue
            cached = self._resolved.get(address)
            mono = time.monotonic()
            if cached is not None and cached[0] > mono:
                ips.update(cached[1])
                continue
            try:
                found = await asyncio.wait_for(self._dns(address), 5)
                self._resolved[address] = (mono + 3600, found)
            except Exception:  # noqa: BLE001 - a resolver failure only means "no node IPs" for 5 min
                found = frozenset()
                self._resolved[address] = (mono + 300, found)
            ips.update(found)
        return frozenset(ips)

    async def _subs_by_panel_id(self, watched: Mapping[int, UserStats]) -> dict[int, _Sub]:
        if not watched:
            return {}
        async with self.db.read() as conn:
            rows = (
                await conn.execute(
                    sa.select(
                        subscriptions.c.id,
                        subscriptions.c.user_id,
                        subscriptions.c.panel_user_id,
                        subscriptions.c.hold_kind,
                    ).where(
                        subscriptions.c.panel_user_id.in_(sorted(watched)),
                        subscriptions.c.link_state.in_(LIVE_STATES),
                    )
                )
            ).all()
        return {
            int(r.panel_user_id): _Sub(int(r.id), r.user_id, int(r.panel_user_id), r.hold_kind) for r in rows
        }

    async def _db_state(
        self, params: Params, subs: Mapping[int, _Sub], at: datetime
    ) -> tuple[DbState, frozenset[int]]:
        """Everything the decider needs from the database (a handful of queries, only for W ≥ warn)."""
        dp = params.decision
        sid_to_pid = {s.id: pid for pid, s in subs.items()}
        sids = sorted(sid_to_pid)
        horizon = at - timedelta(minutes=max(dp.warn_cooldown_minutes, params.grace_minutes, 60))
        blocked: set[int] = set()
        grace: dict[int, GraceInfo] = {}
        last_unblocked: dict[int, datetime] = {}
        last_warn: dict[int, LastWarning] = {}
        last_failed: dict[int, datetime] = {}
        whitelist: set[int] = set()
        dismissed: dict[int, DismissInfo] = {}
        b = ip_guard_blocks.c
        a = ip_guard_alerts.c
        async with self.db.read() as conn:
            if sids:
                for r in await conn.execute(
                    sa.select(b.subscription_id, b.status, b.unblocked_at).where(
                        b.subscription_id.in_(sids),
                        sa.or_(b.status == "active", b.unblocked_at > horizon),
                    )
                ):
                    pid = sid_to_pid[int(r.subscription_id)]
                    if r.status == "active":
                        blocked.add(pid)
                    elif r.unblocked_at is not None:
                        prev = last_unblocked.get(pid)
                        if prev is None or r.unblocked_at > prev:
                            last_unblocked[pid] = r.unblocked_at
                for pid, when in last_unblocked.items():
                    until = when + timedelta(minutes=params.grace_minutes)
                    if until > at:
                        grace[pid] = GraceInfo(when, until)
                for r in await conn.execute(
                    sa.select(a.subscription_id, a.kind, a.reason, a.created_at, a.metrics)
                    .where(a.subscription_id.in_(sids), a.created_at > horizon, a.kind != "digest")
                    .order_by(a.created_at)
                ):
                    pid = sid_to_pid[int(r.subscription_id)]
                    if r.kind == "block_failed":
                        last_failed[pid] = r.created_at
                    elif r.kind in ("warn", "not_blocked"):
                        kind = "warn" if r.kind == "warn" else str(r.reason)
                        last_warn[pid] = LastWarning(
                            kind, r.created_at, int((r.metrics or {}).get("ip_count") or 0)
                        )
                for r in await conn.execute(
                    sa.select(
                        ip_guard_exempt.c.subscription_id, ip_guard_exempt.c.until, ip_guard_exempt.c.actor_id
                    ).where(ip_guard_exempt.c.subscription_id.in_(sids))
                ):
                    pid = sid_to_pid[int(r.subscription_id)]
                    if r.until is None:
                        whitelist.add(pid)
                    elif r.until > at:
                        dismissed[pid] = DismissInfo(r.until, r.actor_id)
            counts = (
                await conn.execute(
                    sa.select(
                        sa.func.count().filter(b.blocked_at > at - timedelta(hours=1)).label("hour"),
                        sa.func.count()
                        .filter(b.blocked_at > at - timedelta(minutes=dp.window_minutes))
                        .label("recent"),
                    ).where(b.reason == "auto", b.blocked_at > at - timedelta(hours=1))
                )
            ).one()
            quarantine = tuple(
                AnomalyRecord(
                    int(r.id),
                    r.created_at,
                    r.quarantine_until,
                    frozenset(int(k) for k in (r.members or {}) if str(k).isdigit()),
                )
                for r in await conn.execute(
                    sa.select(a.id, a.created_at, a.quarantine_until, a.members).where(
                        a.kind == "anomaly", a.quarantine_until > at
                    )
                )
            )
        ok, why = self._preconditions(params)
        state = DbState(
            now=at,
            blocked=frozenset(blocked),
            grace=grace,
            last_unblocked_at=last_unblocked,
            last_warnings=last_warn,
            last_block_failed_at=last_failed,
            blocks_last_hour=int(counts.hour or 0),
            confirmed_recent=int(counts.recent or 0),
            quarantine=quarantine,
            dismissed=dismissed,
            preconditions_ok=ok,
            precondition_reason=why,
        )
        return state, frozenset(whitelist)

    def _preconditions(self, params: Params) -> tuple[bool, str | None]:
        if not params.auto_block:
            return False, "автоблок выключен"
        chat = self.admin_chat
        if chat is None or getattr(chat, "down", False):
            return False, "админ-чат недоступен"
        return True, None

    async def _write_alerts(
        self, individual: Sequence[WarningDraft], summary: Sequence[WarningDraft], subs: Mapping[int, _Sub]
    ) -> int:
        if not individual and not summary:
            return 0
        n = 0
        async with self.db.tx() as conn:
            for draft in individual:
                pid = draft.panel_user_id
                sub = subs.get(pid)
                kind = draft.kind if draft.kind in ("warn", "block_failed") else "not_blocked"
                reason = None if kind != "not_blocked" else draft.kind
                alert_id = await conn.scalar(
                    sa.insert(ip_guard_alerts)
                    .values(
                        created_at=now(),
                        kind=kind,
                        reason=reason,
                        subscription_id=sub.id if sub else None,
                        panel_user_id=pid,
                        metrics=_json_metrics(draft.stats, draft),
                        evidence=self.window.evidence(pid, EVIDENCE_TOP),
                    )
                    .returning(ip_guard_alerts.c.id)
                )
                await self._card(conn, f"alert:{alert_id}")
                n += 1
            if summary:
                members = {
                    str(d.panel_user_id): {
                        "sub": subs[d.panel_user_id].id if d.panel_user_id in subs else 0,
                        "ip": d.stats.ip_count,
                        "kind": d.kind,
                        "metrics": _json_metrics(d.stats, d),
                    }
                    for d in summary
                }
                digest_id = await conn.scalar(
                    sa.insert(ip_guard_alerts)
                    .values(created_at=now(), kind="digest", members=members, metrics={"count": len(summary)})
                    .returning(ip_guard_alerts.c.id)
                )
                await self._card(conn, f"alert:{digest_id}")
                n += 1
        return n

    async def _apply_anomaly(
        self, plan: AnomalyPlan, subs: Mapping[int, _Sub], watched: Mapping[int, UserStats]
    ) -> None:
        new_members = {
            str(pid): {
                "sub": subs[pid].id if pid in subs else 0,
                "ip": watched[pid].ip_count if pid in watched else 0,
                "state": "pending",
            }
            for pid in plan.new_members
        }
        metrics = {
            "trigger": plan.trigger,
            "trigger_count": plan.trigger_count,
            "blocks_last_hour": plan.blocks_last_hour,
            "confirmed_recent": plan.confirmed_recent,
        }
        a = ip_guard_alerts.c
        async with self.db.tx() as conn:
            if plan.create:
                alert_id = await conn.scalar(
                    sa.insert(ip_guard_alerts)
                    .values(
                        created_at=now(),
                        kind="anomaly",
                        quarantine_until=plan.quarantine_until,
                        members=new_members,
                        metrics=metrics,
                    )
                    .returning(a.id)
                )
            else:
                alert_id = plan.anomaly_id
                if alert_id is None:
                    return
                values: dict[str, Any] = {"updated_at": sa.func.now()}
                if new_members:
                    # new keys only: members already acted on keep their state
                    values["members"] = sa.cast(new_members, ip_guard_alerts.c.members.type).op("||")(
                        a.members
                    )
                if plan.quarantine_until is not None:
                    values["quarantine_until"] = sa.func.greatest(a.quarantine_until, plan.quarantine_until)
                    values["metrics"] = a.metrics.op("||")(sa.cast(metrics, ip_guard_alerts.c.metrics.type))
                if len(values) == 1:
                    return
                await conn.execute(sa.update(ip_guard_alerts).where(a.id == alert_id).values(**values))
            await self._card(conn, f"alert:{alert_id}")
        if plan.create:
            await self._raise(
                "anomaly",
                "warn",
                "IP Guard: автоблоки остановлены",
                "Сработал предохранитель массовых блоков. Решите по карточке в теме «🛡 Антиабуз».",
            )

    async def _remind_quarantine(self, state: DbState) -> None:
        active = [r for r in state.quarantine if r.quarantine_until > state.now]
        if not active or self.admin_chat is None:
            return
        if self._reminded_at is not None and state.now - self._reminded_at < REMIND_EVERY:
            return
        if self._reminded_at is None:
            self._reminded_at = state.now  # the card itself was the first message
            return
        self._reminded_at = state.now
        until = max(r.quarantine_until for r in active)
        try:
            await self.admin_chat.post(
                TOPIC, f"🛑 Карантин IP Guard ещё идёт (до {texts.fmt_time(until)} МСК). Решите по карточке."
            )
        except Exception:  # noqa: BLE001 - a reminder is best effort
            log.warning("ip guard: quarantine reminder was not queued")

    async def _check_active_blocks(self, result: PassResult) -> None:
        """Bypass (enabled in the panel while frozen) and connections that survived the drop."""
        b = ip_guard_blocks.c
        s = subscriptions.c
        async with self.db.read() as conn:
            rows = (
                await conn.execute(
                    sa.select(
                        b.id, b.subscription_id, b.panel_user_id, b.blocked_at, s.panel_status, s.hold_kind
                    )
                    .select_from(ip_guard_blocks.join(subscriptions, s.id == b.subscription_id))
                    .where(b.status == "active")
                )
            ).all()
        at = result.at
        live = result.live
        for r in rows:
            block_id = int(r.id)
            pid = r.panel_user_id
            events: list[dict[str, Any]] = []
            if (
                r.hold_kind == "ip_guard"
                and r.panel_status == "ACTIVE"
                and at - r.blocked_at > BYPASS_AFTER
                and at - self._bypass_at.get(block_id, datetime.min.replace(tzinfo=at.tzinfo))
                > BYPASS_COOLDOWN
            ):
                self._bypass_at[block_id] = at
                events.append({"kind": "bypass"})
                async with self.db.tx() as conn:
                    await enqueue_action(
                        conn, int(r.subscription_id), K_DISABLE, {"reason": "ip_guard"}, lane="background"
                    )
                await self._raise(
                    f"bypass:{int(r.subscription_id)}",
                    "warn",
                    "IP Guard: блок сняли в обход кнопки",
                    f"Подписку №{int(r.subscription_id)} включили в панели. Бот отключил её снова. "
                    "Снимайте блок "
                    "кнопкой «🔓 Разблокировать», иначе замороженные дни не вернутся.",
                )
            if result.any_ok and pid is not None and live.get(int(pid)):
                streak = self._residual.get(block_id, 0) + 1
                self._residual[block_id] = streak
                if streak == RESIDUAL_PASSES:
                    events.append({"kind": "residual"})
            else:
                self._residual.pop(block_id, None)
            if events:
                async with self.db.tx() as conn:
                    for event in events:
                        await append_event_tx(conn, block_id, event)
                    await self._card(conn, f"block:{block_id}")
        active_ids = {int(r.id) for r in rows}
        for stale in [k for k in self._residual if k not in active_ids]:
            del self._residual[stale]

    async def _health(self, result: PassResult, failing: Iterable[tuple[int, str, int]]) -> None:
        at = result.at
        if self.attention is None:
            return
        if result.any_ok:
            self._last_ok_at = at
            if self._down_alerted:
                await self._resolve("collect_down")
                if self.admin_chat is not None:
                    await self._post_quiet("✅ IP Guard: сбор IP восстановился.")
            self._down_since, self._down_alerted = None, False
        else:
            self._down_since = self._down_since or at
            ref = self._last_ok_at or self._down_since
            if not self._down_alerted and at - ref > max(HEALTH_DOWN_MIN, timedelta(minutes=5)):
                self._down_alerted = True
                reasons = ", ".join(f"{k[:8]}: {v}" for k, v in list(result.node_reasons().items())[:5])
                await self._raise(
                    "collect_down",
                    "error",
                    "IP Guard: сбор IP не работает",
                    f"Ни одна нода не отвечает больше 10 минут ({reasons or 'нет нод'}). Проверьте панель.",
                )
        if result.forbidden:
            await self._raise(
                "scopes",
                "error",
                "IP Guard: нет прав у токена",
                "Панель ответила 401/403. Выдайте токену скоупы connections:by-node, "
                "connections:by-node-result "
                "и connections:drop.",
            )
        elif result.any_ok:
            await self._resolve("scopes")
        for uuid in result.long_failed_nodes:
            await self._raise(
                f"node:{uuid[:36]}",
                "warn",
                "IP Guard: нода не отвечает",
                f"Нода {uuid[:8]} не отдаёт подключения {NODE_FAIL_ALERT}+ проходов подряд.",
            )
        if result.window_overflow:
            await self._raise(
                "overflow", "warn", "IP Guard: окно переполнено", "Слишком много IP в окне: часть не учтена."
            )
        for pid, reason, streak in failing:
            await self._raise(
                f"block_failed:{pid}",
                "error",
                "IP Guard: автоблок не выполнен",
                f"Аккаунт панели {pid}: блок не прошёл {streak} раз подряд ({reason[:200]}).",
            )

    async def _raise(
        self, key: str, severity: Literal["info", "warn", "error"], title: str, body: str
    ) -> None:
        if self.attention is None:
            return
        try:
            await self.attention.raise_item(ATT_PREFIX + key, severity, title, body)
        except Exception:  # noqa: BLE001 - alerts are best effort, the pass goes on
            log.warning("ip guard: attention item %s was not raised", key)

    async def _resolve(self, key: str) -> None:
        if self.attention is None:
            return
        try:
            await self.attention.resolve(ATT_PREFIX + key)
        except Exception:  # noqa: BLE001
            log.warning("ip guard: attention item %s was not resolved", key)

    async def _post_quiet(self, text: str) -> None:
        try:
            await self.admin_chat.post(TOPIC, text)
        except Exception:  # noqa: BLE001
            log.warning("ip guard: admin chat post failed")

    # ---------------------------------------------------------------------------------------- actions

    async def block(
        self,
        subscription_id: int,
        *,
        reason: Literal["auto", "manual", "anomaly"],
        actor_id: int | None = None,
        stats: UserStats | None = None,
        metrics: Mapping[str, Any] | None = None,
        evidence: Mapping[str, Any] | None = None,
        expected_panel_user: int | None = None,
        actor_role: str | None = None,
    ) -> BlockOutcome:
        """Freeze + disable + drop in one transaction. Idempotent (one active block per subscription)."""
        params = self.params()
        at = now()
        s = subscriptions.c
        try:
            async with self.db.tx() as conn:
                sub = (
                    await conn.execute(
                        sa.select(s.id, s.user_id, s.panel_user_id, s.link_state)
                        .where(s.id == subscription_id)
                        .with_for_update()
                    )
                ).first()
                if sub is None or sub.link_state not in LIVE_STATES:
                    return BlockOutcome("failed", "подписка не найдена или закрыта")
                if expected_panel_user is not None and sub.panel_user_id != expected_panel_user:
                    return BlockOutcome("failed", "аккаунт панели сменился")
                pid = int(sub.panel_user_id) if sub.panel_user_id is not None else None
                values: dict[str, Any] = dict(metrics or {})
                if stats is not None:
                    values = _json_metrics(stats)
                ev = dict(evidence) if evidence is not None else (self.window.evidence(pid) if pid else {})
                block_id = await conn.scalar(
                    pg_insert(ip_guard_blocks)
                    .values(
                        subscription_id=subscription_id,
                        panel_user_id=pid,
                        user_id=sub.user_id,
                        reason=reason,
                        blocked_at=at,
                        blocked_by=actor_id,
                        ip_count=int(values.get("ip_count") or 0),
                        live_ip_count=int(values.get("live_ip_count") or 0),
                        subnet_count=int(values.get("subnet_count") or 0),
                        evidence=ev,
                        evidence_purge_at=at + timedelta(days=params.evidence_ttl_days),
                    )
                    .on_conflict_do_nothing(
                        index_elements=["subscription_id"], index_where=sa.text(ACTIVE_BLOCK_PREDICATE)
                    )
                    .returning(ip_guard_blocks.c.id)
                )
                if block_id is None:
                    return BlockOutcome("already_blocked")
                frozen = await freeze(
                    conn,
                    subscription_id,
                    "ip_guard",
                    reason=f"IP Guard: {int(values.get('ip_count') or 0)} IP за окно",
                    actor_id=actor_id,
                    source="admin" if actor_id is not None else "system",
                    caused_by=f"ip_guard:block:{block_id}",
                )
                await conn.execute(
                    sa.update(ip_guard_blocks)
                    .where(ip_guard_blocks.c.id == block_id)
                    .values(frozen_seconds=frozen.frozen_seconds)
                )
                if (
                    reason != "auto"
                ):  # an admin's decision: in the common audit trail (auto blocks: own cards)
                    await _audit(
                        conn,
                        "ip_guard.block",
                        subscription_id,
                        actor_id=actor_id,
                        role=actor_role,
                        reason=f"IP Guard: {reason}",
                        details={"block_id": int(block_id), "frozen_seconds": frozen.frozen_seconds},
                    )
                targets = self.window.drop_map(pid) if pid is not None else {}
                if not targets:
                    targets = _targets_from_evidence(ev)
                job = await enqueue_drop(conn, subscription_id, int(block_id), targets) if pid else None
                if job is None:
                    await append_event_tx(conn, int(block_id), {"kind": "drop_none"})
                await self._card(conn, f"block:{block_id}")
                if params.notify_user and sub.user_id is not None:
                    await self._notify(conn, int(block_id), "blocked")
                else:
                    await append_event_tx(conn, int(block_id), {"kind": "notify", "state": "off"})
        except SubscriptionError as err:
            return BlockOutcome("failed", err.text)
        except Exception as err:  # noqa: BLE001 - a failed block is reported as block_failed, never raised
            log.warning("ip guard: block of subscription %s failed: %s", subscription_id, type(err).__name__)
            return BlockOutcome("failed", f"ошибка базы ({type(err).__name__})")
        log.info("ip guard: subscription %s blocked (%s)", subscription_id, reason)
        return BlockOutcome("blocked")

    async def unblock(
        self,
        block_id: int,
        *,
        actor_id: int | None,
        revoke: bool = False,
        actor_role: str | None = None,
        reason: str | None = None,
    ) -> ActionResult:
        """Unfreeze (the frozen days come back) and enable; ``revoke``: a new link first. Audited in the same
        transaction (``ip_guard.unblock``: frozen seconds, outcome, new date)."""
        b = ip_guard_blocks.c
        s = subscriptions.c
        async with self.db.tx() as conn:
            blk = (
                (await conn.execute(sa.select(ip_guard_blocks).where(b.id == block_id).with_for_update()))
                .mappings()
                .first()
            )
            if blk is None:
                return ActionResult(False, _TOASTS["gone"], "gone")
            if blk["status"] == "unblocked":
                return ActionResult(False, _TOASTS["already"], "already")
            sid = int(blk["subscription_id"])
            sub = (
                await conn.execute(
                    sa.select(s.hold_kind, s.hold_frozen_seconds, s.link_state)
                    .where(s.id == sid)
                    .with_for_update()
                )
            ).first()
            at = now()
            outcome = "gone"
            paid_until: datetime | None = None
            seconds = 0
            if sub is not None and sub.link_state in LIVE_STATES:
                if revoke and sub.link_state == "linked":
                    await enqueue_action(conn, sid, K_REVOKE, {}, caused_by=f"ip_guard:unblock:{block_id}")
                if sub.hold_kind == "ip_guard":
                    res = await unfreeze(
                        conn, sid, actor_id=actor_id, source="admin", caused_by=f"ip_guard:unblock:{block_id}"
                    )
                    outcome = res.outcome or "active"
                    paid_until = res.paid_until
                    seconds = int(sub.hold_frozen_seconds)
                else:
                    outcome = "active" if sub.hold_kind is None else "held"
            await conn.execute(
                sa.update(ip_guard_blocks)
                .where(b.id == block_id)
                .values(
                    status="unblocked",
                    unblocked_by=actor_id,
                    unblocked_at=at,
                    unblock_mode="revoke" if revoke else "plain",
                    outcome=outcome,
                    new_paid_until=paid_until,
                    frozen_seconds=seconds,
                )
            )
            await append_event_tx(conn, block_id, {"kind": "unblocked", "outcome": outcome})
            auto = "IP Guard: разблокировка с новой ссылкой" if revoke else "IP Guard: разблокировка"
            await _audit(
                conn,
                "ip_guard.unblock",
                sid,
                actor_id=actor_id,
                role=actor_role,
                reason=reason or auto,
                details={
                    "block_id": block_id,
                    "mode": "revoke" if revoke else "plain",
                    "outcome": outcome,
                    "frozen_seconds": seconds,
                    "new_paid_until": paid_until.isoformat() if paid_until else None,
                },
            )
            await self._card(conn, f"block:{block_id}")
            if self.params().notify_user and blk["user_id"] is not None and outcome in ("active", "expired"):
                await self._notify(conn, block_id, "unblocked")
        pid = blk["panel_user_id"]
        if pid is not None:
            self.window.forget_user(int(pid))
            self.memory.forget(int(pid))
        self._residual.pop(block_id, None)
        await self._resolve(f"bypass:{sid}")
        return ActionResult(True, _TOASTS["unblocked"], outcome)

    async def close(
        self, block_id: int, *, actor_id: int | None, reason: str, actor_role: str | None = None
    ) -> ActionResult:
        """«🗑 Закрыть блок»: the term is zeroed, the account stays disabled (a later unblock gives 0 days).

        Takes paid time away, so a ``reason`` is required; ``admin_audit`` (``ip_guard.close``, zeroed time)
        is written in the same transaction."""
        why = " ".join((reason or "").split())[:500]
        if len(why) < 3:
            return ActionResult(False, _TOASTS["no_reason"], "no_reason")
        b = ip_guard_blocks.c
        async with self.db.tx() as conn:
            blk = (
                await conn.execute(
                    sa.select(b.status, b.subscription_id).where(b.id == block_id).with_for_update()
                )
            ).first()
            if blk is None:
                return ActionResult(False, _TOASTS["gone"], "gone")
            if blk.status != "active":
                return ActionResult(False, _TOASTS["already"], "already")
            sid = int(blk.subscription_id)
            hold = (
                await conn.execute(
                    sa.select(subscriptions.c.hold_kind, subscriptions.c.hold_frozen_seconds).where(
                        subscriptions.c.id == sid
                    )
                )
            ).first()
            zeroed = 0
            if hold is not None and hold.hold_kind == "ip_guard":
                with contextlib.suppress(SubscriptionError):  # closed already: nothing to zero
                    await zero_hold(conn, sid, reason=f"IP Guard: {why}", actor_id=actor_id, source="admin")
                    zeroed = int(hold.hold_frozen_seconds)
            await conn.execute(
                sa.update(ip_guard_blocks)
                .where(b.id == block_id)
                .values(status="closed", closed_by=actor_id, closed_at=now(), zeroed=True, frozen_seconds=0)
            )
            await append_event_tx(conn, block_id, {"kind": "closed"})
            await _audit(
                conn,
                "ip_guard.close",
                sid,
                actor_id=actor_id,
                role=actor_role,
                reason=why,
                details={"block_id": block_id, "zeroed_seconds": zeroed},
            )
            await self._card(conn, f"block:{block_id}")
        return ActionResult(True, _TOASTS["closed"], "closed")

    async def confirm(self, ref: str, *, actor_id: int | None) -> ActionResult:
        """«✅ Верно / Проверено — открепить»: marks the card and unpins it."""
        kind, ident = _split_ref(ref)
        if ident is None:
            return ActionResult(False, _TOASTS["gone"], "gone")
        async with self.db.tx() as conn:
            if kind == "block":
                done = await conn.scalar(
                    sa.update(ip_guard_blocks)
                    .where(ip_guard_blocks.c.id == ident, ip_guard_blocks.c.confirmed_at.is_(None))
                    .values(confirmed_by=actor_id, confirmed_at=now())
                    .returning(ip_guard_blocks.c.id)
                )
            else:
                done = await conn.scalar(
                    sa.update(ip_guard_alerts)
                    .where(ip_guard_alerts.c.id == ident, ip_guard_alerts.c.acked_at.is_(None))
                    .values(acked_by=actor_id, acked_at=now(), updated_at=sa.func.now())
                    .returning(ip_guard_alerts.c.id)
                )
            if done is None:
                return ActionResult(False, _TOASTS["already"], "already")
            await self._card(conn, ref)
        return ActionResult(True, _TOASTS["acked"], "acked")

    async def block_from_alert(
        self, alert_id: int, *, actor_id: int | None, actor_role: str | None = None
    ) -> ActionResult:
        """«🚫 Заблокировать» on a warning card (manual block; the numbers come from the card)."""
        async with self.db.read() as conn:
            alert = (
                (await conn.execute(sa.select(ip_guard_alerts).where(ip_guard_alerts.c.id == alert_id)))
                .mappings()
                .first()
            )
        if alert is None or alert["kind"] in ("anomaly", "digest"):
            return ActionResult(False, _TOASTS["gone"], "gone")
        if alert["subscription_id"] is None:
            return ActionResult(False, _TOASTS["no_sub"], "no_sub")
        outcome = await self.block(
            int(alert["subscription_id"]),
            reason="manual",
            actor_id=actor_id,
            metrics=alert["metrics"],
            evidence=alert["evidence"],
            actor_role=actor_role,
        )
        await self.confirm(f"alert:{alert_id}", actor_id=actor_id)
        return _outcome_result(outcome)

    async def manual_block(
        self, subscription_id: int, *, actor_id: int | None, actor_role: str | None = None
    ) -> ActionResult:
        """Admin user card: block a subscription by hand (current window numbers if any)."""
        async with self.db.read() as conn:
            pid = await conn.scalar(
                sa.select(subscriptions.c.panel_user_id).where(subscriptions.c.id == subscription_id)
            )
        metrics: dict[str, Any] = {}
        if pid is not None and int(pid) in self.window.keys:
            keys = self.window.keys[int(pid)]
            metrics = {"ip_count": len(keys)}
        return _outcome_result(
            await self.block(
                subscription_id, reason="manual", actor_id=actor_id, metrics=metrics, actor_role=actor_role
            )
        )

    async def anomaly_members(self, alert_id: int) -> dict[str, Any] | None:
        async with self.db.read() as conn:
            row = (
                await conn.execute(
                    sa.select(ip_guard_alerts.c.members, ip_guard_alerts.c.kind).where(
                        ip_guard_alerts.c.id == alert_id
                    )
                )
            ).first()
        if row is None or row.kind != "anomaly":
            return None
        return dict(row.members or {})

    async def anomaly_block(
        self, alert_id: int, *, actor_id: int | None, expected: int, actor_role: str | None = None
    ) -> ActionResult:
        """«🚫 Заблокировать перечисленных (N)»: confirmed by the number the admin saw."""
        members = await self.anomaly_members(alert_id)
        if members is None:
            return ActionResult(False, _TOASTS["gone"], "gone")
        pending = {
            pid: info for pid, info in members.items() if (info or {}).get("state", "pending") == "pending"
        }
        if len(pending) != expected:
            async with self.db.tx() as conn:
                await self._card(conn, f"alert:{alert_id}")
            return ActionResult(False, _TOASTS["changed"], "changed")
        states: dict[str, str] = {}
        for pid, info in pending.items():
            sid = int((info or {}).get("sub") or 0)
            if sid <= 0:
                states[pid] = "skipped"
                continue
            outcome = await self.block(
                sid,
                reason="anomaly",
                actor_id=actor_id,
                metrics={"ip_count": int((info or {}).get("ip") or 0)},
                actor_role=actor_role,
            )
            states[pid] = "blocked" if outcome.status in ("blocked", "already_blocked") else "skipped"
        await self._set_member_states(alert_id, states, actor_id)
        n = sum(1 for v in states.values() if v == "blocked")
        return ActionResult(True, f"Заблокировано: {n}", "blocked")

    async def anomaly_dismiss(self, alert_id: int, *, actor_id: int | None) -> ActionResult:
        """«✅ Ложная тревога»: the pending members are protected for 6 h."""
        members = await self.anomaly_members(alert_id)
        if members is None:
            return ActionResult(False, _TOASTS["gone"], "gone")
        until = now() + DISMISS_FOR
        states: dict[str, str] = {}
        async with self.db.tx() as conn:
            for pid, info in members.items():
                if (info or {}).get("state", "pending") != "pending":
                    continue
                states[pid] = "dismissed"
                sid = int((info or {}).get("sub") or 0)
                if sid > 0:
                    await conn.execute(
                        pg_insert(ip_guard_exempt)
                        .values(subscription_id=sid, reason="ложная тревога", actor_id=actor_id, until=until)
                        .on_conflict_do_update(
                            index_elements=["subscription_id"],
                            set_={"until": until, "actor_id": actor_id, "reason": "ложная тревога"},
                            where=ip_guard_exempt.c.until.is_not(None),
                        )
                    )
        await self._set_member_states(alert_id, states, actor_id)
        return ActionResult(True, _TOASTS["dismissed"], "dismissed")

    async def _set_member_states(
        self, alert_id: int, states: Mapping[str, str], actor_id: int | None
    ) -> None:
        a = ip_guard_alerts.c
        async with self.db.tx() as conn:
            row = (await conn.execute(sa.select(a.members).where(a.id == alert_id).with_for_update())).first()
            if row is None:
                return
            members = dict(row.members or {})
            for pid, state in states.items():
                info = dict(members.get(pid) or {})
                info["state"] = state
                members[pid] = info
            await conn.execute(
                sa.update(ip_guard_alerts)
                .where(a.id == alert_id)
                .values(members=members, acked_by=actor_id, acked_at=now(), updated_at=sa.func.now())
            )
            await self._card(conn, f"alert:{alert_id}")

    async def digest_member(self, alert_id: int, pid: str) -> ActionResult:
        """A member button of a digest: its own warning card."""
        a = ip_guard_alerts.c
        async with self.db.tx() as conn:
            row = (await conn.execute(sa.select(a.members, a.kind).where(a.id == alert_id))).first()
            if row is None or row.kind != "digest" or pid not in (row.members or {}):
                return ActionResult(False, _TOASTS["gone"], "gone")
            info = row.members[pid] or {}
            kind = str(info.get("kind") or "warn")
            alert_kind = kind if kind in ("warn", "block_failed") else "not_blocked"
            new_id = await conn.scalar(
                sa.insert(ip_guard_alerts)
                .values(
                    created_at=now(),
                    kind=alert_kind,
                    reason=kind if alert_kind == "not_blocked" else None,
                    subscription_id=int(info.get("sub") or 0) or None,
                    panel_user_id=int(pid),
                    metrics=dict(info.get("metrics") or {"ip_count": info.get("ip", 0)}),
                    evidence=self.window.evidence(int(pid)),
                )
                .returning(a.id)
            )
            await self._card(conn, f"alert:{new_id}")
        return ActionResult(True, "Карточка отправлена", "card")

    async def set_exempt(
        self, subscription_id: int, *, on: bool, actor_id: int | None, reason: str = ""
    ) -> ActionResult:
        async with self.db.tx() as conn:
            if on:
                text = " ".join((reason or "белый список").split())[:200] or "белый список"
                await conn.execute(
                    pg_insert(ip_guard_exempt)
                    .values(subscription_id=subscription_id, reason=text, actor_id=actor_id, until=None)
                    .on_conflict_do_update(
                        index_elements=["subscription_id"],
                        set_={"until": None, "reason": text, "actor_id": actor_id},
                    )
                )
            else:
                await conn.execute(
                    sa.delete(ip_guard_exempt).where(ip_guard_exempt.c.subscription_id == subscription_id)
                )
        return ActionResult(True, "Добавлено в белый список" if on else "Убрано из белого списка", "exempt")

    async def set_cdn(self, node_uuid: str, *, cdn: bool) -> ActionResult:
        async with self.db.tx() as conn:
            done = await conn.scalar(
                sa.update(ip_guard_nodes)
                .where(ip_guard_nodes.c.node_uuid == node_uuid)
                .values(cdn=cdn, updated_at=sa.func.now())
                .returning(ip_guard_nodes.c.node_uuid)
            )
        if done is None:
            return ActionResult(False, _TOASTS["gone"], "gone")
        return ActionResult(True, "Отмечено как CDN" if cdn else "Снята отметка CDN", "cdn")

    async def resend_card(self, ref: str) -> ActionResult:
        _, ident = _split_ref(ref)
        if ident is None:
            return ActionResult(False, _TOASTS["gone"], "gone")
        async with self.db.tx() as conn:
            await self._card(conn, ref, resend=True)
        return ActionResult(True, "Карточка отправлена снова", "card")

    # ------------------------------------------------------------------------------------------- jobs

    @staticmethod
    async def _card(conn: AsyncConnection, ref: str, *, resend: bool = False) -> None:
        await enqueue(
            conn,
            CARD_KIND,
            {"ref": ref, "resend": resend},
            queue="tg_send",
            lane="background",
            dedup_key=f"ipg:card:{ref}",
            max_attempts=5,
        )

    @staticmethod
    async def _notify(conn: AsyncConnection, block_id: int, event: str) -> None:
        await enqueue(
            conn,
            NOTIFY_KIND,
            {"block_id": block_id, "event": event},
            queue="tg_send",
            lane="interactive",
            dedup_key=f"ipg:notify:{block_id}:{event}",
            max_attempts=1,  # a send that timed out is not repeated (lesson 7: duplicates)
        )

    async def card_job(self, job: Job, ctx: JobContext) -> None:
        """Render a card from the database and post/edit it in the topic «🛡 Антиабуз»."""
        del ctx
        ref = str(job.payload.get("ref") or "")
        if self.admin_chat is None:
            log.info("ip guard card %s: no admin chat, skipped", ref)
            return
        rendered = await self.render_card(ref)
        if rendered is None:
            return
        card, keyboard, pin = rendered
        if hasattr(self.admin_chat, "post_report"):  # a rich message where the chat takes them
            result = await self.admin_chat.post_report(
                TOPIC, card, buttons=keyboard, card_ref=ref, wait=True
            )
        else:
            result = await self.admin_chat.post(
                TOPIC, card.html(), html=True, buttons=keyboard, card_ref=ref, wait=True
            )
        if result is None or result.message_id is None or result.chat_id is None:
            return
        await self._store_card(ref, int(result.chat_id), int(result.message_id), pin)

    async def _store_card(self, ref: str, chat_id: int, msg_id: int, pin: bool) -> None:
        kind, ident = _split_ref(ref)
        table = ip_guard_blocks if kind == "block" else ip_guard_alerts
        async with self.db.tx() as conn:
            prev = (
                await conn.execute(
                    sa.select(table.c.pinned, table.c.card_chat_id, table.c.card_msg_id).where(
                        table.c.id == ident
                    )
                )
            ).first()
            if prev is None:
                return
            await conn.execute(
                sa.update(table).where(table.c.id == ident).values(card_chat_id=chat_id, card_msg_id=msg_id)
            )
        want = pin and self.params().pin
        if self.notifier is None or (want == bool(prev.pinned) and prev.card_msg_id == msg_id):
            return
        try:
            if want:
                await self.notifier.call(
                    PinChatMessage(chat_id=chat_id, message_id=msg_id, disable_notification=True),
                    chat_id=chat_id,
                )
            elif prev.pinned and prev.card_msg_id is not None:
                # always with message_id: without it Telegram unpins the latest pinned message (lesson 8)
                await self.notifier.call(
                    UnpinChatMessage(
                        chat_id=int(prev.card_chat_id or chat_id), message_id=int(prev.card_msg_id)
                    ),
                    chat_id=chat_id,
                )
        except Exception as err:  # noqa: BLE001 - pin is cosmetic (no rights to pin → card still works)
            log.warning("ip guard: pin/unpin failed: %s", type(err).__name__)
            return
        async with self.db.tx() as conn:
            await conn.execute(sa.update(table).where(table.c.id == ident).values(pinned=want))

    async def render_card(
        self, ref: str
    ) -> tuple[Report, list[list[InlineKeyboardButton]], bool] | None:
        """``(report, keyboard, pinned?)`` of a card, from the database."""
        kind, ident = _split_ref(ref)
        if ident is None:
            return None
        params = self.params()
        dp = params.decision
        names = await self._node_names()
        if kind == "block":
            async with self.db.read() as conn:
                row = (
                    (
                        await conn.execute(
                            sa.select(
                                ip_guard_blocks,
                                subscriptions.c.panel_status,
                                subscriptions.c.hold_frozen_seconds,
                                users.c.first_name,
                                users.c.username,
                                users.c.telegram_id,
                            )
                            .select_from(
                                ip_guard_blocks.join(
                                    subscriptions, subscriptions.c.id == ip_guard_blocks.c.subscription_id
                                ).outerjoin(users, users.c.id == ip_guard_blocks.c.user_id)
                            )
                            .where(ip_guard_blocks.c.id == ident)
                        )
                    )
                    .mappings()
                    .first()
                )
                if row is None:
                    return None
                by = await _names(
                    conn,
                    {
                        "confirmed": row["confirmed_by"],
                        "unblocked": row["unblocked_by"],
                        "closed": row["closed_by"],
                    },
                )
            text = texts.block_card(
                row,
                person=texts.who(row),
                window=dp.window_minutes,
                frozen_left=int(row["hold_frozen_seconds"] or 0),
                panel_disabled=row["panel_status"] == "DISABLED",
                by_names=by,
                node_names=names,
            )
            return (
                text,
                block_keyboard(int(ident), str(row["status"])),
                row["status"] == "active" and row["confirmed_at"] is None,
            )
        async with self.db.read() as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(ip_guard_alerts, users.c.first_name, users.c.username, users.c.telegram_id)
                        .select_from(
                            ip_guard_alerts.outerjoin(
                                subscriptions, subscriptions.c.id == ip_guard_alerts.c.subscription_id
                            ).outerjoin(users, users.c.id == subscriptions.c.user_id)
                        )
                        .where(ip_guard_alerts.c.id == ident)
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            if row["kind"] == "anomaly":
                people = await _people(conn, row["members"] or {})
            else:
                people = {}
            acked = (await _names(conn, {"acked": row["acked_by"]})).get("acked")
        if row["kind"] == "anomaly":
            text = texts.anomaly_card(
                row,
                now=now(),
                people=people,
                params={
                    "max_run": dp.max_blocks_per_run,
                    "window": dp.window_minutes,
                    "max_hour": dp.max_blocks_per_hour,
                },
            )
            pending = sum(
                1 for v in (row["members"] or {}).values() if (v or {}).get("state", "pending") == "pending"
            )
            return text, anomaly_keyboard(int(ident), pending), pending > 0
        if row["kind"] == "digest":
            return texts.digest_card(row), digest_keyboard(int(ident), row["members"] or {}), False
        person = (
            texts.who(row)
            if row["telegram_id"] is not None or row["first_name"]
            else f"аккаунт панели {row['panel_user_id']}"
        )
        text = texts.warning_card(
            row,
            person=person,
            window=dp.window_minutes,
            thresholds={
                "warn": dp.warn_ips,
                "block": dp.block_ips,
                "subnets": dp.min_subnets,
                "live": dp.confirm_live_ips,
            },
            acked_by=acked,
            node_names=names,
        )
        return (
            text,
            alert_keyboard(
                int(ident), acked=row["acked_at"] is not None, has_sub=row["subscription_id"] is not None
            ),
            False,
        )

    async def _node_names(self) -> dict[str, str]:
        async with self.db.read() as conn:
            rows = (await conn.execute(sa.select(ip_guard_nodes.c.node_uuid, ip_guard_nodes.c.name))).all()
        return {r.node_uuid: r.name or r.node_uuid[:8] for r in rows}

    async def notify_job(self, job: Job, ctx: JobContext) -> None:
        """Tell the user about the block / the unblock. Never retried; the outcome goes to the card."""
        del ctx
        block_id = int(job.payload["block_id"])
        event = str(job.payload.get("event") or "blocked")
        b = ip_guard_blocks.c
        async with self.db.read() as conn:
            row = (
                await conn.execute(
                    sa.select(
                        b.status,
                        b.ip_count,
                        b.outcome,
                        b.frozen_seconds,
                        b.new_paid_until,
                        b.unblock_mode,
                        users.c.telegram_id,
                        users.c.language,
                    )
                    .select_from(ip_guard_blocks.outerjoin(users, users.c.id == b.user_id))
                    .where(b.id == block_id)
                )
            ).first()
        if row is None:
            return
        state = "failed"
        if row.telegram_id is None or self.notifier is None:
            state = "unreachable"
        elif event == "blocked" and row.status != "active":
            return  # unblocked before the message went out: nothing to say
        else:
            text, keyboard = self.user_message(event, row)
            try:
                sent = await self.notifier.send(
                    int(row.telegram_id), text, parse_mode="HTML", reply_markup=keyboard
                )
                state = "sent" if sent is not None else "unreachable"
            except Exception as err:  # noqa: BLE001 - unknown outcome: never re-sent (lesson 7)
                log.warning(
                    "ip guard: user notification for block %s failed: %s", block_id, type(err).__name__
                )
        if event == "blocked":
            async with self.db.tx() as conn:
                await append_event_tx(conn, block_id, {"kind": "notify", "state": state})
                await self._card(conn, f"block:{block_id}")

    def user_message(self, event: str, row: Any) -> tuple[str, InlineKeyboardMarkup]:
        from svbg.billing.texts import lang_of
        from svbg.tg.ui.renderer import nav_button

        lang = lang_of(getattr(row, "language", None))
        support = self.support_url()
        rows: list[list[InlineKeyboardButton]] = []
        if event == "blocked":
            text = texts.user_t("user_blocked", lang).format(n=int(row.ip_count or 0))
            if support:
                rows.append([InlineKeyboardButton(text=texts.user_t("btn_support", lang), url=support)])
            rows.append(
                [InlineKeyboardButton(text=texts.user_t("btn_close", lang), callback_data="ipg:close")]
            )
        else:
            text = texts.unblock_user_text(
                str(row.outcome or "active"),
                left=int(row.frozen_seconds or 0),
                until=row.new_paid_until,
                revoked=row.unblock_mode == "revoke",
                lang=lang,
            )
            if row.outcome == "active":
                rows.append([nav_button(texts.user_t("btn_my_sub", lang), "home")])
            else:
                rows.append([nav_button(texts.user_t("btn_renew", lang), "buy", style="success")])
        return text, InlineKeyboardMarkup(inline_keyboard=rows)

    async def purge(self) -> int:
        """Evidence TTL: the IP list of old blocks is erased, counters stay (personal data, 05 §2.2.5)."""
        async with self.db.tx() as conn:
            res = await conn.execute(
                sa.update(ip_guard_blocks)
                .where(ip_guard_blocks.c.evidence_purge_at < now())
                .values(evidence={"purged": True}, evidence_purge_at=None)
            )
            await conn.execute(
                sa.delete(ip_guard_alerts).where(
                    ip_guard_alerts.c.created_at < now() - timedelta(days=self.params().evidence_ttl_days)
                )
            )
            await conn.execute(sa.delete(ip_guard_exempt).where(ip_guard_exempt.c.until < now()))
        return int(res.rowcount or 0)

    async def ips_document(self, ref: str) -> BufferedInputFile | None:
        """«📄 Все IP»: the window right now, or the stored top-50 after a restart."""
        kind, ident = _split_ref(ref)
        if ident is None:
            return None
        table = ip_guard_blocks if kind == "block" else ip_guard_alerts
        async with self.db.read() as conn:
            row = (
                await conn.execute(
                    sa.select(table.c.subscription_id, table.c.panel_user_id, table.c.evidence).where(
                        table.c.id == ident
                    )
                )
            ).first()
        if row is None:
            return None
        pid = int(row.panel_user_id) if row.panel_user_id is not None else None
        full = self.window.all_keys(pid) if pid is not None else []
        body = texts.ips_file(
            sid=int(row.subscription_id or 0),
            at=now(),
            window=self.params().decision.window_minutes,
            full=full,
            evidence=row.evidence or {},
            node_names=await self._node_names(),
        )
        return BufferedInputFile(body.encode(), filename=f"ip_guard_{kind}_{ident}.txt")

    # ----------------------------------------------------------------------------------------- status

    async def health(self) -> HealthReport:
        if self._down_alerted:
            return HealthReport.down("Сбор IP не работает: ни одна нода не отвечает")
        last = self._last_pass
        if last is None:
            return HealthReport.ok("Ждёт первого прохода")
        if last.nodes_ok == 0 and last.nodes_failed:
            return HealthReport.degraded("Ноды не отвечают на запрос подключений")
        return HealthReport.ok(f"Нод отвечает: {last.nodes_ok}")

    async def status_lines(self) -> list[str]:
        async with self.db.read() as conn:
            row = (
                await conn.execute(
                    sa.select(
                        sa.func.count().filter(ip_guard_blocks.c.status == "active").label("active"),
                        sa.func.count()
                        .filter(ip_guard_blocks.c.blocked_at > now() - timedelta(days=1))
                        .label("day"),
                    )
                )
            ).one()
        auto = "включён" if self.params().auto_block else "выключен (только предупреждения)"
        lines = [f"Автоблок: {auto}", f"Активных блоков: {int(row.active)}, за сутки: {int(row.day)}"]
        if self._last_pass is not None:
            lines.append(
                f"Последний проход: нод ок {self._last_pass.nodes_ok}, следим за {self._last_pass.watched}"
            )
        return lines

    async def report_lines(self) -> list[str]:
        day = now() - timedelta(days=1)
        async with self.db.read() as conn:
            blocks = await conn.scalar(sa.select(sa.func.count()).where(ip_guard_blocks.c.blocked_at > day))
            warns = await conn.scalar(sa.select(sa.func.count()).where(ip_guard_alerts.c.created_at > day))
        if not blocks and not warns:
            return []
        return [f"Блоков за сутки: {int(blocks or 0)}", f"Предупреждений и карточек: {int(warns or 0)}"]


# ---------------------------------------------------------------------------------------------- helpers


def _split_ref(ref: str) -> tuple[str, int | None]:
    kind, _, raw = ref.partition(":")
    if kind not in ("block", "alert") or not raw.isdigit() or len(raw) > 18:
        return kind, None
    return kind, int(raw)


def _targets_from_evidence(evidence: Mapping[str, Any]) -> dict[str, list[str]]:
    out: dict[str, set[str]] = {}
    for item in evidence.get("top") or [] if isinstance(evidence, Mapping) else []:
        if not isinstance(item, Mapping):
            continue
        for node in item.get("nodes") or []:
            out.setdefault(str(node), set()).update(str(i) for i in item.get("ips") or [])
    return {k: sorted(v) for k, v in out.items() if v}


def _outcome_result(outcome: BlockOutcome) -> ActionResult:
    if outcome.status == "blocked":
        return ActionResult(True, _TOASTS["blocked"], "blocked")
    if outcome.status == "already_blocked":
        return ActionResult(False, _TOASTS["already_blocked"], "already")
    return ActionResult(False, (outcome.reason or _TOASTS["failed"])[:190], "failed")


async def _names(conn: AsyncConnection, ids: Mapping[str, int | None]) -> dict[str, str | None]:
    wanted = {int(v) for v in ids.values() if v is not None}
    if not wanted:
        return dict.fromkeys(ids)
    rows = (
        await conn.execute(
            sa.select(users.c.id, users.c.username, users.c.first_name).where(users.c.id.in_(wanted))
        )
    ).all()
    found = {int(r.id): (f"@{r.username}" if r.username else (r.first_name or f"id {r.id}")) for r in rows}
    return {k: (found.get(int(v), f"id {v}") if v is not None else None) for k, v in ids.items()}


async def _people(conn: AsyncConnection, members: Mapping[str, Any]) -> dict[str, str]:
    sids = {int((v or {}).get("sub") or 0): pid for pid, v in members.items()}
    sids.pop(0, None)
    if not sids:
        return {}
    rows = (
        (
            await conn.execute(
                sa.select(subscriptions.c.id, users.c.first_name, users.c.username, users.c.telegram_id)
                .select_from(subscriptions.outerjoin(users, users.c.id == subscriptions.c.user_id))
                .where(subscriptions.c.id.in_(sorted(sids)))
            )
        )
        .mappings()
        .all()
    )
    return {sids[int(r["id"])]: f"{texts.who(r)} · №{int(r['id'])}" for r in rows}


async def _resolve_host(host: str) -> frozenset[Any]:
    import socket

    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return frozenset(ip for ip in (parse_ip(info[4][0]) for info in infos) if ip is not None)


# ---------------------------------------------------------------------------------------------- keyboards

CB: Final = "ipg"


def _btn(text: str, data: str, style: str | None = None) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data, style=style)


def block_keyboard(block_id: int, status: str) -> list[list[InlineKeyboardButton]]:
    T = texts.T
    if status == "active":
        return [
            [_btn(T["b_unblock"], f"{CB}:u:{block_id}", "success")],
            [
                _btn(T["b_confirmed"], f"{CB}:ok:block:{block_id}"),
                _btn(T["b_ips"], f"{CB}:ip:block:{block_id}"),
            ],
            [_btn(T["b_close"], f"{CB}:x:{block_id}", "danger")],
        ]
    if status == "closed":
        return [[_btn(T["b_unblock"], f"{CB}:u:{block_id}", "success")]]
    return []


def unblock_confirm_keyboard(block_id: int) -> list[list[InlineKeyboardButton]]:
    T = texts.T
    return [
        [
            _btn(T["b_unblock_plain"], f"{CB}:uy:{block_id}", "success"),
            _btn(T["b_unblock_revoke"], f"{CB}:ur:{block_id}", "primary"),
        ],
        [_btn(T["b_cancel"], f"{CB}:c:block:{block_id}")],
    ]


def close_confirm_keyboard(block_id: int) -> list[list[InlineKeyboardButton]]:
    """One button per reason (the term is zeroed, so the reason goes to ``admin_audit``) and «Отмена»."""
    T = texts.T
    rows = [
        [_btn(T["b_close_yes"].format(reason=reason), f"{CB}:xy:{block_id}:{i}", "danger")]
        for i, reason in enumerate(texts.CLOSE_REASONS)
    ]
    rows.append([_btn(T["b_cancel"], f"{CB}:c:block:{block_id}")])
    return rows


def alert_keyboard(alert_id: int, *, acked: bool, has_sub: bool) -> list[list[InlineKeyboardButton]]:
    T = texts.T
    rows: list[list[InlineKeyboardButton]] = []
    first: list[InlineKeyboardButton] = []
    if not acked:
        first.append(_btn(T["b_ack"], f"{CB}:ok:alert:{alert_id}", "success"))
    if has_sub:
        first.append(_btn(T["b_block"], f"{CB}:b:{alert_id}", "danger"))
    if first:
        rows.append(first)
    rows.append([_btn(T["b_ips"], f"{CB}:ip:alert:{alert_id}")])
    return rows


def block_confirm_keyboard(alert_id: int) -> list[list[InlineKeyboardButton]]:
    T = texts.T
    return [
        [
            _btn(T["b_block_yes"], f"{CB}:by:{alert_id}", "danger"),
            _btn(T["b_cancel"], f"{CB}:c:alert:{alert_id}"),
        ]
    ]


def anomaly_keyboard(alert_id: int, pending: int) -> list[list[InlineKeyboardButton]]:
    T = texts.T
    if pending <= 0:
        return []
    return [
        [_btn(T["b_anomaly_block"].format(n=pending), f"{CB}:ab:{alert_id}:{pending}", "danger")],
        [_btn(T["b_anomaly_dismiss"], f"{CB}:ad:{alert_id}", "success")],
    ]


def anomaly_confirm_keyboard(alert_id: int, pending: int) -> list[list[InlineKeyboardButton]]:
    T = texts.T
    return [
        [
            _btn(T["b_anomaly_block_yes"].format(n=pending), f"{CB}:aby:{alert_id}:{pending}", "danger"),
            _btn(T["b_cancel"], f"{CB}:c:alert:{alert_id}"),
        ]
    ]


def digest_keyboard(alert_id: int, members: Mapping[str, Any]) -> list[list[InlineKeyboardButton]]:
    T = texts.T
    rows = []
    for pid, raw in list(members.items())[:20]:
        info = raw or {}
        rows.append(
            [
                _btn(
                    T["b_digest_member"].format(sid=int(info.get("sub") or 0), w=int(info.get("ip") or 0)),
                    f"{CB}:dg:{alert_id}:{pid}",
                )
            ]
        )
    return rows
