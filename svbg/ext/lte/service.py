"""LTE quotas: the runtime service, settings, module state and the manifest ``SPEC`` (05 §2.1, 07 §2.4.3).

:class:`LteService` glues the pure engines to the database, the panel and the core:

* **cycle** (``lte.cycle``, every 600 s): topology (hourly cache) → :class:`~.collector.Collector` (reads only
  the nodes of LTE groups) → unprocessed term events → period timers → ``decide`` with the safety fuses →
  :class:`~.enforce.Enforcer` (``lte_blocks`` + ``panel_squad_substitutions`` in one transaction, the core
  writer applies) → notifications. The DB steps of the cycle and the term hook share one ``asyncio.Lock``
  (panel reads are outside it; the hook retries instead of waiting long);
* **term hook** (``lte.term``): ``subscription_events`` of one subscription (X2) → the period machine →
  a decision for that subscription right away («день сброса сразу после оплаты»). A per-subscription cursor
  (``lte_event_cursor``) makes it exactly once; the pack item of a purchase calls it synchronously inside
  the fulfill transaction, so the pack lands in the right (possibly new) period;
* **switching off is never silent** (07 §2.4.3 p.4): ``LTE_ENFORCE=off`` releases blocks only with
  ``LTE_OFF_ACTION=release`` («Снять»); ``keep`` («Оставить», default) keeps them. Turning the module off
  (``LTE_ENABLED=false``) keeps them too: the core freezes the squads of those subscriptions and raises
  «Требует внимания». Re-enabling after a switch-off takes a fresh baseline of the counters (05 §2.1.8).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Final

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.dialects.postgresql import insert as pg_insert

from svbg.core.clock import now
from svbg.core.component import HealthReport
from svbg.core.settings.registry import Apply, SettingDef
from svbg.core.tables import admin_audit
from svbg.db.meta import JSONB, UtcDateTime, now_default
from svbg.ext.api import (
    BusSub,
    JobDef,
    ModuleSpec,
    Periodic,
    Perm,
    Slot,
    Topic,
    ViewLoader,
    enabled_setting,
    lazy,
)
from svbg.ext.lte import notify
from svbg.ext.lte.accounting import (
    AccountingParams,
    GroupState,
    hour_floor,
    retention_cutoffs,
    usage_between,
)
from svbg.ext.lte.collector import Collector, CollectOutcome, PanelReader, Topology
from svbg.ext.lte.decide import (
    EnforceSettings,
    GroupInput,
    LiveBlock,
    Override,
    SubjectInput,
    Twin,
    after_block_resend_due,
    check_foreign_nodes,
    check_twins,
    rights_of,
)
from svbg.ext.lte.enforce import AppliedPlan, Enforcer, enqueue_resend, sync_twins
from svbg.ext.lte.model import (
    ENFORCE_MODES,
    NON_MONEY_SOURCES,
    PAID_CORE_KINDS,
    engine_event_kind,
    group_limit_rows,
    msk_date,
)
from svbg.ext.lte.periods import (
    AnchorState,
    Event,
    HoldInterval,
    Outcome,
    PeriodParams,
    PeriodState,
    advance,
    effective_time,
    is_late,
    plan_recompute,
    simulate,
)
from svbg.ext.lte.planner import Plan, PlannerParams, decide
from svbg.ext.lte.tables import (
    lte_anchors,
    lte_blocks,
    lte_counters,
    lte_credits,
    lte_group_nodes,
    lte_groups,
    lte_metadata,
    lte_node_state,
    lte_overrides,
    lte_period_usage,
    lte_periods,
    lte_twins,
    lte_usage_daily,
    lte_usage_hourly,
)
from svbg.jobs.queue import enqueue
from svbg.jobs.worker import RetryJob
from svbg.subscriptions.tables import subscription_events, subscriptions

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from svbg.core.bus import Event as BusEvent
    from svbg.db.engine import Database
    from svbg.ext.api import ModuleContext
    from svbg.jobs.queue import Job
    from svbg.jobs.worker import JobContext
    from svbg.remnawave.api import RemnawaveApi

__all__ = [
    "MODULE",
    "RUNTIME",
    "SECTION",
    "SETTINGS",
    "SPEC",
    "LteConfig",
    "LteService",
    "Runtime",
    "SubjectRow",
    "create_runtime_tables",
    "lte_event_cursor",
    "lte_kv",
]

log = logging.getLogger("svbg.ext.lte")

MODULE: Final = "lte"
SECTION: Final = ("lte", "🌐 Трафик LTE")
TERM_KIND: Final = "lte.term"
CARD_KIND: Final = "lte.card"
CYCLE_S: Final = 600
CYCLE_TIMEOUT_S: Final = 480
TOPOLOGY_TTL: Final = timedelta(hours=1)
CATCH_UP_LIMIT: Final = 300
#: Rows of ``subscription_events`` read per cycle above the high-water mark (an index range on the PK).
CATCH_UP_SCAN: Final = 2000
#: Failed subscriptions retried per cycle (with backoff) — a poison event never starves the others.
CATCH_UP_RETRIES: Final = 50
CATCH_UP_KEY: Final = "catch_up"
#: The mark never passes events younger than this: a transaction may commit a lower id after a higher one.
HWM_LAG: Final = timedelta(minutes=10)
RETRY_BASE: Final = timedelta(minutes=10)
RETRY_MAX: Final = timedelta(hours=24)
FAILED_ALERT_AFTER: Final = 3
FAILED_KEEP: Final = 1000
#: Per-cycle time budgets of the DB steps (the term hook waits on the same lock).
CATCH_UP_BUDGET_S: Final = 60.0
TIMERS_BUDGET_S: Final = 60.0
TIMERS_LIMIT: Final = 500
#: ``lte.term`` waits this long for the cycle's DB step, then retries later (never a module error).
TERM_LOCK_WAIT_S: Final = 5.0
TERM_RETRY_S: Final = 30
#: A late event re-simulates the subscription's history; longer histories fall back to "apply now + review".
REPLAY_MAX_EVENTS: Final = 5000
FREEZE_CORE_KINDS: Final = ("frozen", "freeze")
UNFREEZE_CORE_KINDS: Final = ("unfrozen", "unfreeze")
HOURLY_KEPT: Final = timedelta(hours=71)
STALE_CYCLE: Final = timedelta(minutes=25)
AFTER_BLOCK_KEY: Final = "lte:after_block:"

# ------------------------------------------------------------------------------------------- settings

K_ENABLED: Final = "LTE_ENABLED"
K_ENFORCE: Final = "LTE_ENFORCE"
K_OFF_ACTION: Final = "LTE_OFF_ACTION"
K_ENFORCE_LIST: Final = "LTE_ENFORCE_LIST"
K_WARN: Final = "LTE_WARN_PERCENT"
K_QUIET: Final = "LTE_QUIET_HOURS"
K_NOTIFY: Final = "LTE_NOTIFY_USER"
K_TOPUP: Final = "LTE_TOPUP_ENABLED"
K_COVERAGE: Final = "LTE_TOPUP_MIN_COVERAGE_HOURS"
K_CARD_BLOCKS: Final = "LTE_ADMIN_NOTIFY_BLOCKS"
K_CARD_TOPUPS: Final = "LTE_ADMIN_NOTIFY_TOPUPS"
K_GB_MAX: Final = "LTE_ADMIN_GB_MAX"
K_GB_BYTES: Final = "LTE_GB_BYTES"
#: Advanced keys → the lower-case names the engines' ``from_values`` read.
ADVANCED: Final[Mapping[str, tuple[int, int, int, str, str]]] = {
    "LTE_RENEWAL_GRACE_HOURS": (
        24,
        0,
        168,
        "Льгота продления, ч",
        "Опоздание с оплатой не дольше — день сброса тот же.",
    ),
    "LTE_ROLLOVER_MIN_REMAINING_HOURS": (
        24,
        0,
        72,
        "Запас покрытия на границе, ч",
        "Меньше — период откладывается до продления.",
    ),
    "LTE_MAX_NEW_BLOCKS_PER_CYCLE": (
        30,
        1,
        1000,
        "Новых блоков за цикл",
        "Больше — ждут следующего цикла (сначала самые большие расходы).",
    ),
    "LTE_QUARANTINE_NEW_BLOCKS": (
        100,
        1,
        100_000,
        "Карантин: кандидатов за цикл",
        "Больше — новые блоки группы ждут подтверждения админа.",
    ),
    "LTE_MAX_BLOCKED_SHARE_PCT": (
        25,
        1,
        100,
        "Карантин: доля заблокированных, %",
        "Больше — новые блоки группы ждут подтверждения админа.",
    ),
    "LTE_SANITY_MAX_MBPS": (
        2000,
        100,
        100_000,
        "Правдоподобная скорость, Мбит/с",
        "Дельта выше — помечается, блок по ней откладывается на цикл.",
    ),
    "LTE_BOUNDARY_SNAP_S": (
        60,
        0,
        300,
        "Притяжение к границе, с",
        "Граница ближе к краю интервала — всё в одну сторону.",
    ),
    "LTE_WRITE_LAG_S": (120, 0, 300, "Лаг записи панели, с", "Сдвиг точки деления на границе периода."),
    "LTE_MAX_CATCHUP_DAYS": (35, 7, 70, "Догон после простоя, дней", "Глубже история не читается."),
}
_TAGS: Final = ("lte", "трафик", "квота", "белые списки")


def _quiet_validator(value: Any) -> None:
    notify.parse_quiet(value)


def _s(key: str, typ: Any, default: Any, title: str, desc: str, **kw: Any) -> SettingDef:
    return SettingDef(
        key, typ, default, SECTION[0], title, desc, apply=Apply.HOT, owner_only=True, tags=_TAGS, **kw
    )


SETTINGS: Final[tuple[SettingDef, ...]] = (
    enabled_setting(
        MODULE,
        "Трафик LTE",
        "Считает трафик пользователей на нодах LTE и при исчерпании лимита отключает только эти серверы. "
        "Выключение блоки не снимает: снять — кнопкой «Снять блоки» на экране «🌐 Трафик LTE».",
        section=SECTION[0],
    ),
    _s(
        K_ENFORCE,
        "enum",
        "shadow",
        "Применение",
        "on — блоки в панели; shadow — решения только в журнале; off — решений нет (блоки снимаются, "
        "только если «При выключении» = release).",
        choices=ENFORCE_MODES,
    ),
    _s(
        K_OFF_ACTION,
        "enum",
        "keep",
        "При выключении применения",
        "keep — оставить поставленные блоки; release — снять все блоки через панель.",
        choices=("keep", "release"),
    ),
    _s(
        K_ENFORCE_LIST,
        "list[int]",
        [],
        "Пилот (id в панели)",
        "Пусто — для всех. Иначе блоки только этим.",
        advanced=True,
    ),
    _s(K_WARN, int, 80, "Порог предупреждения, %", "Предупреждение, ⚠️ и кнопка докупки.", min=50, max=99),
    _s(
        K_QUIET,
        str,
        "00:00-09:00",
        "Тихие часы (МСК)",
        "Предупреждения переносятся на конец окна.",
        validator=_quiet_validator,
    ),
    _s(K_NOTIFY, bool, True, "Уведомлять пользователей", "Предупреждение, исчерпание и обновление лимита."),
    _s(K_TOPUP, bool, False, "Продажа пакетов", "Кнопка «⚡ Докупить трафик LTE» и пакеты в покупке."),
    _s(
        K_COVERAGE,
        int,
        24,
        "Запас подписки для докупки, ч",
        "Подписка должна жить дольше — иначе «сначала продлите».",
        min=0,
        max=168,
    ),
    _s(K_CARD_BLOCKS, bool, False, "Карточки блоков в тему", "Каждый блок — сообщение в «🌐 Трафик LTE»."),
    _s(
        K_CARD_TOPUPS, bool, False, "Карточки докупок в тему", "Каждая докупка — сообщение в «🌐 Трафик LTE»."
    ),
    _s(K_GB_MAX, int, 50, "«+ГБ» для админа, не больше", "Выше — только владелец.", min=1, max=10_000),
    _s(
        K_GB_BYTES,
        int,
        10**9,
        "Байт в «ГБ»",
        "10⁹ (как у владельца) или 2³⁰.",
        min=10**9,
        max=2**30,
        advanced=True,
    ),
    *(
        _s(key, int, d, title, desc, min=lo, max=hi, advanced=True)
        for key, (d, lo, hi, title, desc) in ADVANCED.items()
    ),
)


@dataclass(frozen=True, slots=True)
class LteConfig:
    """Typed view of the ``LTE_*`` snapshot (bad values fall back to defaults: the registry validates)."""

    enabled: bool = False
    mode: str = "shadow"
    off_action: str = "keep"
    pilot: frozenset[int] = frozenset()
    warn_percent: int = 80
    quiet: tuple[time, time] | None = (time(0), time(9))
    notify_user: bool = True
    topup_enabled: bool = False
    min_coverage_hours: int = 24
    card_blocks: bool = False
    card_topups: bool = False
    admin_gb_max: int = 50
    gb_bytes: int = 10**9
    advanced: Mapping[str, Any] = field(default_factory=dict)
    support_url: str | None = None

    @classmethod
    def from_snapshot(cls, snap: Mapping[str, Any]) -> LteConfig:
        def get(key: str, default: Any) -> Any:
            try:
                value = snap.get(key, default)
            except Exception:  # noqa: BLE001 - an odd snapshot means defaults
                return default
            return default if value is None else value

        def num(key: str, default: int, lo: int, hi: int) -> int:
            raw = get(key, default)
            if isinstance(raw, bool):
                return default
            try:
                return min(max(int(raw), lo), hi)
            except (TypeError, ValueError):
                return default

        mode = str(get(K_ENFORCE, "shadow"))
        off = str(get(K_OFF_ACTION, "keep"))
        pilot: set[int] = set()
        for item in get(K_ENFORCE_LIST, []) or []:
            try:
                pilot.add(int(item))
            except (TypeError, ValueError):
                continue
        try:
            quiet = notify.parse_quiet(get(K_QUIET, "00:00-09:00"))
        except ValueError:
            quiet = (time(0), time(9))
        advanced = {key[4:].lower(): get(key, d) for key, (d, *_rest) in ADVANCED.items()}
        support = get("SUPPORT_URL", None)
        return cls(
            enabled=get(K_ENABLED, False) is True,
            mode=mode if mode in ENFORCE_MODES else "shadow",
            off_action=off if off in ("keep", "release") else "keep",
            pilot=frozenset(pilot),
            warn_percent=num(K_WARN, 80, 50, 99),
            quiet=quiet,
            notify_user=get(K_NOTIFY, True) is not False,
            topup_enabled=get(K_TOPUP, False) is True,
            min_coverage_hours=num(K_COVERAGE, 24, 0, 168),
            card_blocks=get(K_CARD_BLOCKS, False) is True,
            card_topups=get(K_CARD_TOPUPS, False) is True,
            admin_gb_max=num(K_GB_MAX, 50, 1, 10_000),
            gb_bytes=num(K_GB_BYTES, 10**9, 10**9, 2**30),
            advanced=advanced,
            support_url=support
            if isinstance(support, str) and support.startswith(("https://", "tg://"))
            else None,
        )

    @property
    def enforce(self) -> EnforceSettings:
        return EnforceSettings(
            mode=self.mode,  # type: ignore[arg-type]
            pilot=self.pilot,
            release_when_off=self.off_action == "release",
            warn_percent=self.warn_percent,
        )

    @property
    def accounting(self) -> AccountingParams:
        return AccountingParams.from_values(self.advanced)

    @property
    def planner(self) -> PlannerParams:
        return PlannerParams.from_values(self.advanced)

    @property
    def periods(self) -> PeriodParams:
        return PeriodParams.from_values(self.advanced)

    @property
    def notify(self) -> notify.NotifyConfig:
        return notify.NotifyConfig(enabled=self.notify_user, quiet=self.quiet)


# ------------------------------------------------------------------------------------- runtime tables

#: Integration: these two tables follow ``svbg.ext.lte.tables.REGISTERED`` (same metadata).
lte_kv = sa.Table(
    "lte_kv",
    lte_metadata,
    sa.Column("key", sa.Text, primary_key=True),
    sa.Column("value", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column("updated_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.CheckConstraint("length(key) BETWEEN 1 AND 64", name="key_len"),
)

lte_event_cursor = sa.Table(
    "lte_event_cursor",
    lte_metadata,
    sa.Column(
        "subscription_id",
        sa.BigInteger,
        sa.ForeignKey("subscriptions.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("last_event_id", sa.BigInteger, nullable=False, server_default=sa.text("0")),
    sa.Column("updated_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.CheckConstraint("last_event_id >= 0", name="last_event_id"),
)

RUNTIME_TABLES: Final = (lte_kv, lte_event_cursor)


def create_runtime_tables(sync_conn: sa.Connection) -> None:
    """``run_sync(create_runtime_tables)`` — tests and the time before the migration exists."""
    lte_metadata.create_all(sync_conn, tables=list(RUNTIME_TABLES))


def _parse_at(value: Any) -> datetime:
    """An ISO moment from ``lte_kv``; garbage is "due now" (a retry is never lost)."""
    if isinstance(value, str):
        with contextlib.suppress(ValueError):
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is not None:
                return parsed
    return datetime.min.replace(tzinfo=UTC)


async def kv_get(conn: AsyncConnection, key: str) -> dict[str, Any]:
    value = await conn.scalar(sa.select(lte_kv.c.value).where(lte_kv.c.key == key))
    return dict(value) if isinstance(value, Mapping) else {}


async def kv_put(conn: AsyncConnection, key: str, value: Mapping[str, Any]) -> None:
    stmt = pg_insert(lte_kv).values(key=key, value=dict(value), updated_at=now())
    stmt = stmt.on_conflict_do_update(
        index_elements=[lte_kv.c.key],
        set_={"value": stmt.excluded.value, "updated_at": stmt.excluded.updated_at},
    )
    await conn.execute(stmt)


# ---------------------------------------------------------------------------------------- loaded data


@dataclass(frozen=True, slots=True)
class GroupRow:
    id: int
    slug: str
    name: Mapping[str, Any]
    state: str
    enforce: bool
    margin_bytes: int
    margin_pct: int
    limit_rows: Mapping[str, int | None]
    squad_uuid: str | None
    version: int

    def input(
        self, state: GroupState | None = None, *, quarantined: bool = False, cleared: bool = False
    ) -> GroupInput:
        return GroupInput(
            id=self.id,
            state=self.state,
            enforce=self.enforce,
            margin_bytes=self.margin_bytes,
            margin_pct=self.margin_pct,
            limit_rows=self.limit_rows,
            incomplete=state.incomplete if state is not None else (),
            anomaly=state.anomaly if state is not None else (),
            suspended=self.state == "suspended",
            quarantined=quarantined,
            quarantine_cleared=cleared,
        )


def group_from_row(r: Mapping[str, Any]) -> GroupRow:
    return GroupRow(
        id=int(r["id"]),
        slug=str(r["slug"]),
        name=dict(r["name"] or {}),
        state=str(r["state"]),
        enforce=bool(r["enforce"]),
        margin_bytes=int(r["margin_bytes"] or 0),
        margin_pct=int(r["margin_pct"] or 0),
        limit_rows=group_limit_rows(
            has_default=bool(r["has_default"]),
            limit_default=r["limit_default_bytes"],
            has_trial=bool(r["has_trial"]),
            limit_trial=r["limit_trial_bytes"],
        ),
        squad_uuid=r["squad_uuid"],
        version=int(r["version"]),
    )


@dataclass(frozen=True, slots=True)
class Model:
    """Groups, live node memberships and the twin map (small; loaded per cycle / per decision)."""

    groups: Mapping[int, GroupRow]
    group_nodes: Mapping[int, frozenset[str]]
    twins: Mapping[str, Twin]


async def load_model(conn: AsyncConnection, *, at: datetime) -> Model:
    groups = {
        g.id: g
        for g in (
            group_from_row(r)
            for r in (
                await conn.execute(sa.select(lte_groups).where(lte_groups.c.state != "draft"))
            ).mappings()
        )
    }
    gn = lte_group_nodes.c
    nodes: dict[int, set[str]] = {}
    for r in (
        await conn.execute(
            sa.select(gn.group_id, gn.node_uuid).where(
                gn.counted_from <= at, sa.or_(gn.counted_to.is_(None), gn.counted_to > at)
            )
        )
    ).all():
        nodes.setdefault(int(r.group_id), set()).add(str(r.node_uuid))
    twins = {
        str(r.base_squad_uuid): Twin(
            str(r.base_squad_uuid), int(r.group_id), str(r.twin_squad_uuid), r.problem
        )
        for r in (await conn.execute(sa.select(lte_twins))).all()
    }
    return Model(groups=groups, group_nodes={k: frozenset(v) for k, v in nodes.items()}, twins=twins)


@dataclass(frozen=True, slots=True)
class SubjectRow:
    """A subscription as loaded for decisions (+ what the enforcement and notifications need)."""

    subject: SubjectInput
    user_id: int | None
    desired_squads: tuple[str, ...]
    paid_until: datetime | None
    period_end: datetime | None


def rights_for(
    desired: Sequence[str],
    model: Model,
    topology: Topology | None,
) -> frozenset[int]:
    """Groups the subscription's base squads give rights to: the twin map (owner's declaration) plus, when
    the topology is known, ``tags(G) ⊆ inbounds(base)`` (a base without a twin then shows as «неприменим»)."""
    rights = {twin.group_id for base in desired if (twin := model.twins.get(base)) is not None}
    if topology is not None and topology.squad_inbounds:
        tags = topology.group_tags(model.group_nodes)
        rights |= rights_of(desired, squad_inbounds=topology.squad_inbounds, group_tags=tags)
    return frozenset(g for g in rights if g in model.groups)


async def load_subjects(
    conn: AsyncConnection,
    model: Model,
    *,
    at: datetime,
    topology: Topology | None = None,
    sids: Sequence[int] | None = None,
    collect: CollectOutcome | None = None,
) -> dict[int, SubjectRow]:
    """Subscriptions with a live period or a live block (7 batched queries for any number of them)."""
    s = subscriptions.c
    p = lte_periods
    live_block = sa.exists().where(
        lte_blocks.c.subscription_id == s.id, lte_blocks.c.status.in_(("active", "releasing"))
    )
    q = (
        sa.select(
            s.id,
            s.panel_user_id,
            s.user_id,
            s.desired_squads,
            s.hold_kind,
            s.paid_until,
            p.c.id.label("pid"),
            p.c.state.label("pstate"),
            p.c.is_trial,
            p.c.planned_end_at,
        )
        .select_from(subscriptions.outerjoin(p, sa.and_(p.c.subscription_id == s.id, p.c.state != "closed")))
        .where(sa.or_(p.c.id.is_not(None), live_block))
    )
    if sids is not None:
        q = q.where(s.id.in_(list(sids)))
    base = (await conn.execute(q)).all()
    if not base:
        return {}
    ids = [int(r.id) for r in base]
    pids = [int(r.pid) for r in base if r.pid is not None]
    used: dict[int, dict[int, int]] = {}
    if pids:
        u = lte_period_usage.c
        for r in (
            await conn.execute(sa.select(u.period_id, u.group_id, u.used_bytes).where(u.period_id.in_(pids)))
        ).all():
            used.setdefault(int(r.period_id), {})[int(r.group_id)] = int(r.used_bytes)
    credits: dict[int, dict[int, int]] = {}
    if pids:
        c = lte_credits.c
        for r in (
            await conn.execute(
                sa.select(c.period_id, c.group_id, sa.func.sum(c.bytes))
                .where(c.period_id.in_(pids), c.status == "active")
                .group_by(c.period_id, c.group_id)
            )
        ).all():
            credits.setdefault(int(r[0]), {})[int(r[1])] = int(r[2] or 0)
    overrides: dict[int, list[Override]] = {}
    o = lte_overrides.c
    for r in (
        await conn.execute(sa.select(lte_overrides).where(o.subscription_id.in_(ids), o.revoked_at.is_(None)))
    ).mappings():
        overrides.setdefault(int(r["subscription_id"]), []).append(
            Override(
                kind=r["kind"],
                group_id=r["group_id"],
                limit_bytes=r["limit_bytes"],
                applies_to=r["applies_to"],
                period_id=r["period_id"],
                valid_until=r["valid_until"],
                exempt_kind=r["exempt_kind"],
            )
        )
    blocks: dict[int, list[LiveBlock]] = {}
    b = lte_blocks.c
    for r in (
        await conn.execute(
            sa.select(lte_blocks).where(b.subscription_id.in_(ids), b.status.in_(("active", "releasing")))
        )
    ).mappings():
        blocks.setdefault(int(r["subscription_id"]), []).append(
            LiveBlock(
                id=int(r["id"]),
                group_id=int(r["group_id"]),
                mode=r["mode"],
                status=r["status"],
                reason=r["reason"],
                period_id=r["period_id"],
                release_reason=r["release_reason"],
            )
        )
    notified = await notify.load_notified(conn, ids, at=at)
    prev: dict[int, int] = {}
    for r in (
        await conn.execute(
            sa.select(p.c.subscription_id, p.c.id)
            .where(p.c.subscription_id.in_(ids), p.c.state == "closed")
            .order_by(p.c.subscription_id, p.c.starts_at.desc())
            .ext(distinct_on(p.c.subscription_id))
        )
    ).all():
        prev[int(r[0])] = int(r[1])
    gap = collect.gap_estimated if collect is not None else {}
    clamped = collect.clamped if collect is not None else frozenset()
    out: dict[int, SubjectRow] = {}
    for r in base:
        sid = int(r.id)
        pid = int(r.pid) if r.pid is not None else None
        desired = tuple(str(x) for x in (r.desired_squads or ()))
        sent = notified.get(sid, set())
        past = prev.get(sid)
        reset_groups = frozenset(
            g for (period, g, kind) in sent if period == past and kind in ("warn", "exhausted")
        )
        rights = rights_for(desired, model, topology)
        out[sid] = SubjectRow(
            subject=SubjectInput(
                subscription_id=sid,
                panel_user_id=int(r.panel_user_id or 0),
                period_id=pid,
                period_state=r.pstate or "open",
                period_is_trial=bool(r.is_trial),
                rights=rights,
                used=used.get(pid or -1, {}),
                credits=credits.get(pid or -1, {}),
                gap_estimated={g: v for (sub, g), v in gap.items() if sub == sid},
                clamped_sanity=frozenset(g for (sub, g) in clamped if sub == sid),
                overrides=tuple(overrides.get(sid, ())),
                live_blocks=tuple(blocks.get(sid, ())),
                notified=frozenset((g, kind) for (period, g, kind) in sent if period == pid),
                reset_notice_groups=reset_groups,
                frozen=r.hold_kind is not None,
            ),
            user_id=r.user_id,
            desired_squads=desired,
            paid_until=r.paid_until,
            period_end=r.planned_end_at,
        )
    return out


# ------------------------------------------------------------------------------------- term events


def engine_event(row: Mapping[str, Any]) -> Event | None:
    """A ``subscription_events`` row → an engine :class:`Event` (``None`` — not about the term)."""
    details = row["details"] if isinstance(row["details"], Mapping) else {}
    core = str(row["kind"])
    kind = engine_event_kind(core, source=str(row["source"] or ""), details=details)
    if kind == "unclassified" and row["new_expire"] is None and row["old_expire"] is None:
        return None  # devices, channel, reissue…: not a change of the term
    was = details.get("is_trial_before")
    after = details.get("is_trial_after", details.get("is_trial"))
    if core in ("trial_started", "trial"):
        after = True
    paid_at = row["ts"] if kind == "paid" else None
    raw_paid = details.get("paid_at")
    if isinstance(raw_paid, str):
        with contextlib.suppress(ValueError):
            paid_at = datetime.fromisoformat(raw_paid)
    return Event(
        occurred_at=row["ts"],
        kind=kind,  # type: ignore[arg-type]
        paid_at=paid_at,
        old_end=row["old_expire"],
        new_end=row["new_expire"],
        was_trial=was if isinstance(was, bool) else None,
        is_trial=after if isinstance(after, bool) else None,
        is_new_row=core in ("purchase_new", "trial_started", "import"),
        source=str(row["source"] or "") or None,
        event_id=int(row["id"]),
    )


def period_from_row(p: Mapping[str, Any]) -> PeriodState:
    return PeriodState(
        anchor_at=p["anchor_at"],
        idx=int(p["idx"]),
        starts_at=p["starts_at"],
        planned_end_at=p["planned_end_at"],
        state=p["state"],
        is_trial=bool(p["is_trial"]),
        series_first=int(p["idx"]) == 0,
        ended_at=p["ended_at"],
        end_cause=p["end_cause"],
        period_id=int(p["id"]),
    )


def hold_intervals(
    marks: Sequence[tuple[str, datetime]], *, frozen_since: datetime | None = None
) -> tuple[HoldInterval, ...]:
    """Freeze intervals from the ``frozen``/``unfrozen`` journal (05 §2.1.9 E8): past holds keep their end,
    so an unfreeze processed later never lets E8 fire inside the freeze. ``frozen_since`` — the live hold of
    the subscription row (covers a freeze without a journal row, e.g. an import)."""
    out: list[HoldInterval] = []
    opened: datetime | None = None
    for kind, at in marks:
        if kind in FREEZE_CORE_KINDS:
            if opened is None:
                opened = at
        elif kind in UNFREEZE_CORE_KINDS and opened is not None:
            out.append(HoldInterval(blocked_at=opened, unblocked_at=max(at, opened)))
            opened = None
    if opened is None and frozen_since is not None:
        opened = frozen_since
    if opened is not None:
        out.append(HoldInterval(blocked_at=opened))
    return tuple(out)


async def load_state(conn: AsyncConnection, sid: int) -> AnchorState:
    a = (
        (await conn.execute(sa.select(lte_anchors).where(lte_anchors.c.subscription_id == sid)))
        .mappings()
        .first()
    )
    p = (
        (
            await conn.execute(
                sa.select(lte_periods).where(
                    lte_periods.c.subscription_id == sid, lte_periods.c.state != "closed"
                )
            )
        )
        .mappings()
        .first()
    )
    period = period_from_row(p) if p is not None else None
    if a is None:
        return AnchorState(period=period)
    return AnchorState(
        anchor_at=a["anchor_at"],
        anchor_kind=a["anchor_kind"],
        anchor_source=a["anchor_source"] or "",
        series_open=bool(a["series_open"]),
        series_started_at=a["series_started_at"],
        series_closed_at=a["series_closed_at"],
        coverage_end=a["coverage_end"],
        is_trial=bool(a["is_trial"]),
        review_reason=a["review_reason"],
        period=period,
    )


@dataclass(slots=True)
class WriteResult:
    released: list[tuple[int, str]] = field(default_factory=list)
    reviews: list[str] = field(default_factory=list)
    revoked: int = 0
    opened: list[int] = field(default_factory=list)


async def write_outcome(
    conn: AsyncConnection, sid: int, out: Outcome, *, enforcer: Enforcer, at: datetime
) -> WriteResult:
    """Persist the machine's result: anchor, periods (closing before opening), actions."""
    res = WriteResult()
    st = out.state
    if st.anchor_at is not None and st.anchor_kind is not None:
        values = {
            "anchor_at": st.anchor_at,
            "anchor_kind": st.anchor_kind,
            "anchor_source": (st.anchor_source or "")[:64],
            "series_open": st.series_open,
            "series_started_at": st.series_started_at,
            "series_closed_at": st.series_closed_at if not st.series_open else None,
            "coverage_end": st.coverage_end,
            "is_trial": st.is_trial,
            "review_reason": (st.review_reason or None) and st.review_reason[:200],
            "updated_at": at,
        }
        if not st.series_open and values["series_closed_at"] is None:
            values["series_closed_at"] = at
        stmt = pg_insert(lte_anchors).values(subscription_id=sid, **values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[lte_anchors.c.subscription_id],
            set_={**values, "version": lte_anchors.c.version + 1},
        )
        await conn.execute(stmt)
    keymap: dict[tuple[datetime, int, datetime], int] = {}
    existing = [p for p in out.periods if p.period_id is not None]
    fresh = [p for p in out.periods if p.period_id is None]
    for p in sorted(existing, key=lambda x: x.state != "closed"):
        await conn.execute(
            sa.update(lte_periods)
            .where(lte_periods.c.id == p.period_id)
            .values(
                state=p.state,
                ended_at=p.ended_at if p.state == "closed" else None,
                end_cause=p.end_cause,
                planned_end_at=p.planned_end_at,
                is_trial=p.is_trial,
            )
        )
        keymap[p.key] = int(p.period_id or 0)
    for p in fresh:
        pid = (
            await conn.execute(
                sa.insert(lte_periods)
                .values(
                    subscription_id=sid,
                    anchor_at=p.anchor_at,
                    idx=p.idx,
                    starts_at=p.starts_at,
                    planned_end_at=max(p.planned_end_at, p.starts_at + timedelta(seconds=1)),
                    ended_at=p.ended_at if p.state == "closed" else None,
                    end_cause=p.end_cause,
                    state=p.state,
                    is_trial=p.is_trial,
                )
                .returning(lte_periods.c.id)
            )
        ).scalar_one()
        keymap[p.key] = int(pid)
        if p.state != "closed":
            res.opened.append(int(pid))
    for action in out.actions:
        if action.kind == "expire_credits" and action.period is not None:
            pid = action.period.period_id or keymap.get(action.period.key)
            if pid:
                await conn.execute(
                    sa.update(lte_credits)
                    .where(lte_credits.c.period_id == pid, lte_credits.c.status == "active")
                    .values(status="expired", expired_at=action.at)
                )
        elif action.kind == "release_blocks":
            res.released.extend(await _release_subscription(conn, sid, action.cause or "reset", enforcer, at))
        elif action.kind == "revoke_exemption":
            res.revoked += await _revoke_launch_trial(conn, sid, action.at)
        elif action.kind == "needs_review":
            res.reviews.append(action.cause)
    return res


async def _release_subscription(
    conn: AsyncConnection, sid: int, reason: str, enforcer: Enforcer, at: datetime
) -> list[tuple[int, str]]:
    from svbg.ext.lte.decide import Release

    rows = (
        await conn.execute(
            sa.select(lte_blocks.c.id, lte_blocks.c.group_id, lte_blocks.c.mode).where(
                lte_blocks.c.subscription_id == sid, lte_blocks.c.status == "active"
            )
        )
    ).all()
    done: list[tuple[int, str]] = []
    for r in rows:
        rel = Release(int(r.id), sid, 0, int(r.group_id), reason, r.mode == "shadow")
        if await enforcer.release(conn, rel, at=at):
            done.append((int(r.id), reason))
    return done


async def _revoke_launch_trial(conn: AsyncConnection, sid: int, at: datetime) -> int:
    o = lte_overrides.c
    revoked = (
        (
            await conn.execute(
                sa.update(lte_overrides)
                .where(
                    o.subscription_id == sid,
                    o.kind == "exempt",
                    o.exempt_kind == "launch_trial",
                    o.revoked_at.is_(None),
                )
                .values(revoked_at=at, revoke_reason="converted_to_paid")
                .returning(o.id)
            )
        )
        .scalars()
        .all()
    )
    for oid in revoked:
        await audit(
            conn,
            None,
            "lte.exemption_revoked",
            f"sub:{sid}",
            reason="converted_to_paid",
            details={"override_id": oid},
        )
    return len(revoked)


async def audit(
    conn: AsyncConnection,
    actor_id: int | None,
    action: str,
    target: str,
    *,
    reason: str | None = None,
    details: Mapping[str, Any] | None = None,
    role: str | None = None,
) -> None:
    """``admin_audit`` with ``details.domain = 'lte'`` (actor ``NULL`` — the system / CLI)."""
    await conn.execute(
        sa.insert(admin_audit).values(
            actor_id=actor_id,
            role=role if actor_id is not None else "system",
            action=action,
            target=target,
            reason=(reason or None) and reason[:500],
            details={"domain": "lte", **dict(details or {})},
        )
    )


async def resum_usage(
    conn: AsyncConnection, sid: int, period_id: int, *, start: datetime, end: datetime
) -> None:
    """Usage of a re-simulated period re-summed from the buckets (daily 70 days, hourly 72 h, 05 §2.1.9)."""
    if end <= start:
        return
    d, h = lte_usage_daily.c, lte_usage_hourly.c
    daily: dict[int, dict[Any, int]] = {}
    for gid, day, value in (
        await conn.execute(
            sa.select(d.group_id, d.msk_date, d.bytes).where(
                d.subscription_id == sid, d.msk_date >= msk_date(start)
            )
        )
    ).all():
        daily.setdefault(int(gid), {})[day] = int(value)
    hourly: dict[int, dict[Any, int]] = {}
    hourly_since = hour_floor(end) - HOURLY_KEPT  # older hours may be gone (retention): days split by time
    for gid, hour, value in (
        await conn.execute(
            sa.select(h.group_id, h.hour_utc, h.bytes).where(
                h.subscription_id == sid, h.hour_utc >= hour_floor(start)
            )
        )
    ).all():
        hourly.setdefault(int(gid), {})[hour] = int(value)
    for gid in sorted({*daily, *hourly}):
        used = usage_between(
            start, end, daily=daily.get(gid, {}), hourly=hourly.get(gid, {}), hourly_since=hourly_since
        )
        stmt = pg_insert(lte_period_usage).values(period_id=period_id, group_id=gid, used_bytes=used)
        await conn.execute(
            stmt.on_conflict_do_update(
                index_elements=[lte_period_usage.c.period_id, lte_period_usage.c.group_id],
                set_={"used_bytes": stmt.excluded.used_bytes},
            )
        )


async def lock_cursor(conn: AsyncConnection, sid: int) -> int:
    """Serialize term processing of one subscription (row lock) and return the last processed event id."""
    await conn.execute(pg_insert(lte_event_cursor).values(subscription_id=sid).on_conflict_do_nothing())
    value = await conn.scalar(
        sa.select(lte_event_cursor.c.last_event_id)
        .where(lte_event_cursor.c.subscription_id == sid)
        .with_for_update()
    )
    return int(value or 0)


# ---------------------------------------------------------------------------------------- the service


@dataclass(frozen=True, slots=True)
class CycleReport:
    at: datetime
    ok: bool
    requests: int = 0
    failed: int = 0
    blocks: int = 0
    releases: int = 0
    unenforceable: int = 0
    quarantine: tuple[int, ...] = ()
    error: str | None = None


class LteService:
    """See the module docstring. Built once per process (``Runtime``); safe to call from jobs and tasks."""

    def __init__(
        self,
        db: Database,
        api: Callable[[], RemnawaveApi] | None,
        *,
        config: Callable[[], Mapping[str, Any]],
        attention: Any | None = None,
        admin_chat: Any | None = None,
        notifier: Any | None = None,
        settings: Any | None = None,
    ) -> None:
        self.db = db
        self._api = api
        self._config = config
        self.attention = attention
        self.admin_chat = admin_chat
        self.settings = settings
        self.enforcer = Enforcer(db, api, attention=attention)
        self.reader = PanelReader(api) if api is not None else None
        self.collector = Collector(db, self.reader) if self.reader is not None else None
        #: DB steps of the cycle and the term hook (short); the panel reads are outside it.
        self.lock = asyncio.Lock()
        #: One cycle at a time (scheduler, CLI, admin "run now").
        self.cycle_lock = asyncio.Lock()
        self.topology: Topology | None = None
        self.sender = notify.NotifySender(
            db,
            notifier=notifier,
            config=lambda: self.cfg().notify,
            gb_bytes=lambda: self.cfg().gb_bytes,
            topup_ok=self.topup_available,
        )
        self.last_report: CycleReport | None = None
        self.last_plan: Plan | None = None

    def cfg(self) -> LteConfig:
        try:
            return LteConfig.from_snapshot(self._config())
        except Exception:  # noqa: BLE001 - settings not loaded yet: defaults
            return LteConfig()

    # ------------------------------------------------------------------------------------ topology

    async def refresh_topology(self, *, force: bool = False, at: datetime | None = None) -> Topology | None:
        at = at or now()
        topo = self.topology
        if (
            not force
            and topo is not None
            and topo.loaded_at is not None
            and at - topo.loaded_at < TOPOLOGY_TTL
        ):
            return topo
        if self.reader is None:
            return topo
        try:
            self.topology = await self.reader.topology()
        except Exception as err:  # noqa: BLE001 - the cycle works on the twin map without it
            log.warning("lte: topology read failed: %s", type(err).__name__)
        return self.topology

    # ----------------------------------------------------------------------------------- term events

    async def apply_events(
        self, conn: AsyncConnection, sid: int, *, at: datetime | None = None
    ) -> WriteResult | None:
        """Process unprocessed term events of ``sid`` in the caller's transaction (exactly once), then the
        timers up to ``at``. ``None`` — nothing was pending (the caller may run the timers itself).

        A late event (timers effective after its ``t_eff`` were already applied, :func:`is_late`) re-simulates
        the subscription's history from its first event (05 §2.1.9): a renewal paid before E8 but delivered
        after it continues the series instead of opening a new one.
        """
        at = at or now()
        cursor = await lock_cursor(conn, sid)
        rows = (
            (
                await conn.execute(
                    sa.select(subscription_events)
                    .where(subscription_events.c.subscription_id == sid, subscription_events.c.id > cursor)
                    .order_by(subscription_events.c.id)
                )
            )
            .mappings()
            .all()
        )
        if not rows:
            return None
        params = self.cfg().periods
        state = await load_state(conn, sid)
        holds = await self._holds(conn, sid)
        events = [ev for ev in (engine_event(row) for row in rows) if ev is not None]
        late = [
            t_eff
            for t_eff in (effective_time(ev, params=params).at for ev in events)
            if is_late(state, t_eff)
        ]
        last_id = int(rows[-1]["id"])
        result: WriteResult | None = None
        if late:
            result = await self._replay(conn, sid, since=min(late), last_id=last_id, at=at, holds=holds)
            if result is None:
                # No replay possible (manual anchor, huge history): apply now and ask for a review.
                events = [
                    replace(ev, occurred_at=max(at, ev.occurred_at), paid_at=None)
                    if is_late(state, effective_time(ev, params=params).at)
                    else ev
                    for ev in events
                ]
                out = simulate(state, events, until=at, params=params, holds=holds)
                result = await write_outcome(conn, sid, out, enforcer=self.enforcer, at=at)
                result.reviews.append("событие срока пришло позже границы периода")
        else:
            out = simulate(state, events, until=at, params=params, holds=holds)
            result = WriteResult()
            if events or out.actions:
                result = await write_outcome(conn, sid, out, enforcer=self.enforcer, at=at)
        await conn.execute(
            sa.update(lte_event_cursor)
            .where(lte_event_cursor.c.subscription_id == sid)
            .values(last_event_id=last_id, updated_at=at)
        )
        return result

    async def _replay(
        self,
        conn: AsyncConnection,
        sid: int,
        *,
        since: datetime,
        last_id: int,
        at: datetime,
        holds: Sequence[HoldInterval],
    ) -> WriteResult | None:
        """Re-simulate every term event of ``sid`` (``id ≤ last_id``) from an empty state and rewrite the
        periods from ``since`` on: stale live periods are closed (``recomputed``), their credits and live
        blocks move to the new live period, whose usage is re-summed from the buckets. ``None`` — not
        possible (manual anchor, history over :data:`REPLAY_MAX_EVENTS`, corrupted input)."""
        params = self.cfg().periods
        current = await load_state(conn, sid)
        if current.anchor_kind == "manual":
            return None
        ev = subscription_events.c
        rows = (
            (
                await conn.execute(
                    sa.select(subscription_events)
                    .where(ev.subscription_id == sid, ev.id <= last_id)
                    .order_by(ev.id)
                    .limit(REPLAY_MAX_EVENTS + 1)
                )
            )
            .mappings()
            .all()
        )
        if len(rows) > REPLAY_MAX_EVENTS:
            return None
        events = [e for e in (engine_event(row) for row in rows) if e is not None]
        out = simulate(AnchorState(), events, until=at, params=params, holds=holds)
        if out.truncated:
            return None
        stored = [
            period_from_row(r)
            for r in (await conn.execute(sa.select(lte_periods).where(lte_periods.c.subscription_id == sid)))
            .mappings()
            .all()
        ]
        diff = plan_recompute(stored, out.periods, since=since)
        stale_ids = [int(x.period_id) for x in diff.stale if x.period_id is not None]
        for x in diff.stale:  # first: frees the "one live period" slot for the new rows
            await conn.execute(
                sa.update(lte_periods)
                .where(lte_periods.c.id == x.period_id)
                .values(
                    state="closed",
                    ended_at=x.ended_at if x.state == "closed" and x.ended_at is not None else x.starts_at,
                    end_cause="recomputed",
                )
            )
        changed = [new for old, new in diff.keep if old != new]
        rewritten = replace(
            out,
            periods=(*changed, *diff.insert),
            actions=tuple(a for a in out.actions if a.at >= since),
        )
        result = await write_outcome(conn, sid, rewritten, enforcer=self.enforcer, at=at)
        live = next((x for x in (*changed, *diff.insert) if x.live), None)
        live_id = await conn.scalar(
            sa.select(lte_periods.c.id).where(
                lte_periods.c.subscription_id == sid, lte_periods.c.state != "closed"
            )
        )
        if live_id is not None and stale_ids:
            await conn.execute(
                sa.update(lte_credits)
                .where(lte_credits.c.period_id.in_(stale_ids), lte_credits.c.status == "active")
                .values(period_id=live_id)
            )
            await conn.execute(
                sa.update(lte_blocks)
                .where(
                    lte_blocks.c.period_id.in_(stale_ids), lte_blocks.c.status.in_(("active", "releasing"))
                )
                .values(period_id=live_id)
            )
        if live is not None and live_id is not None:
            await resum_usage(conn, sid, int(live_id), start=live.starts_at, end=at)
        log.info("lte: subscription %s re-simulated from %s (late term event)", sid, since.isoformat())
        return result

    @staticmethod
    async def _holds(conn: AsyncConnection, sid: int) -> tuple[HoldInterval, ...]:
        """Every freeze of ``sid`` (past ones with their end) — the E8 timer is held inside each of them."""
        ev = subscription_events.c
        marks = (
            await conn.execute(
                sa.select(ev.kind, ev.ts)
                .where(ev.subscription_id == sid, ev.kind.in_((*FREEZE_CORE_KINDS, *UNFREEZE_CORE_KINDS)))
                .order_by(ev.id)
            )
        ).all()
        row = (
            await conn.execute(
                sa.select(subscriptions.c.hold_kind, subscriptions.c.hold_since).where(
                    subscriptions.c.id == sid
                )
            )
        ).first()
        since = row.hold_since if row is not None and row.hold_kind is not None else None
        return hold_intervals([(str(k), ts) for k, ts in marks], frozen_since=since)

    async def process_subscription(
        self, sid: int, *, at: datetime | None = None, wait: float | None = None
    ) -> AppliedPlan | None:
        """``lte.term``: events → periods → a decision for this subscription (one transaction).

        ``wait`` — how long to wait for the cycle's DB step; then :class:`RetryJob` (the job comes back
        later and is not counted as a module error)."""
        at = at or now()
        if wait is None:
            await self.lock.acquire()
        else:
            try:
                async with asyncio.timeout(wait):
                    await self.lock.acquire()
            except TimeoutError:
                raise RetryJob(TERM_RETRY_S, "LTE: идёт цикл, повтор позже") from None
        try:
            async with self.db.tx() as conn:
                written = await self.apply_events(conn, sid, at=at)
                applied = await self.decide_for(conn, [sid], at=at)
        finally:
            self.lock.release()
        await self._after(applied, written)
        return applied

    async def decide_for(
        self,
        conn: AsyncConnection,
        sids: Sequence[int] | None,
        *,
        at: datetime,
        collect: CollectOutcome | None = None,
        model: Model | None = None,
    ) -> AppliedPlan:
        """Load, decide (same planner as the cycle and the preview) and apply in the caller's transaction."""
        cfg = self.cfg()
        model = model or await load_model(conn, at=at)
        topology = self.topology
        subjects = await load_subjects(conn, model, at=at, topology=topology, sids=sids, collect=collect)
        if not subjects:
            return AppliedPlan()
        state = await kv_get(conn, "fuses")
        quarantined = {int(g) for g in state.get("quarantine", [])}
        cleared = {int(g) for g in state.get("cleared", [])}
        incidents = {int(k): tuple(v) for k, v in (state.get("incidents") or {}).items()}
        groups = []
        for g in model.groups.values():
            gs = collect.group_states.get(g.id) if collect is not None else None
            if g.id in incidents:
                gs = GroupState(
                    incomplete=gs.incomplete if gs else (),
                    anomaly=(*(gs.anomaly if gs else ()), *incidents[g.id]),
                )
            groups.append(
                g.input(gs, quarantined=g.id in quarantined and g.id not in cleared, cleared=g.id in cleared)
            )
        plan = decide(
            groups=groups,
            subjects=[row.subject for row in subjects.values()],
            settings=cfg.enforce,
            now=at,
            params=cfg.planner,
        )
        tags = topology.group_tags(model.group_nodes) if topology is not None else None
        applied = await self.enforcer.apply_plan(
            conn,
            plan,
            desired={sid: row.desired_squads for sid, row in subjects.items()},
            twins=model.twins,
            squad_inbounds=topology.squad_inbounds if topology is not None else None,
            group_tags=tags,
            at=at,
        )
        await self._record_notifications(conn, plan=plan, applied=applied, subjects=subjects, cfg=cfg, at=at)
        if sids is None:
            # The cycle owns the fuse state: a quarantine waits for the admin, a confirmation is used once.
            await kv_put(
                conn,
                "fuses",
                {
                    "quarantine": sorted((quarantined - cleared) | set(plan.quarantine)),
                    "cleared": [],
                    "incidents": {str(k): list(v) for k, v in incidents.items()},
                },
            )
        self.last_plan = plan
        return applied

    async def _record_notifications(
        self,
        conn: AsyncConnection,
        *,
        plan: Plan,
        applied: AppliedPlan,
        subjects: Mapping[int, SubjectRow],
        cfg: LteConfig,
        at: datetime,
    ) -> None:
        placed = {(b.subscription_id, b.group_id): b.block_id for b in applied.placed}
        for req in plan.notifications:
            row = subjects.get(req.subscription_id)
            if row is None:
                continue
            block_id = placed.get((req.subscription_id, req.group_id))
            if req.kind == "exhausted" and block_id is None:
                continue
            await notify.record(conn, req, user_id=row.user_id, block_id=block_id, cfg=cfg.notify, at=at)

    async def _after(self, applied: AppliedPlan | None, written: WriteResult | None = None) -> None:
        """Post-commit side effects (attention, cards); never raises."""
        try:
            if applied is not None and applied.unenforceable:
                await self.enforcer.raise_unenforceable(applied.unenforceable)
            cfg = self.cfg()
            if applied is not None and cfg.card_blocks and applied.placed:
                cards = [
                    notify.card_report(
                        "block",
                        sid=b.subscription_id,
                        used=notify.fmt_gb(b.used_bytes, cfg.gb_bytes),
                        limit=notify.fmt_gb(b.limit_bytes, cfg.gb_bytes),
                        reason=notify.REASONS.get(b.reason, b.reason),
                    )
                    for b in applied.placed
                    if b.mode == "enforce"
                ]
                await notify.post_cards(self.admin_chat, cards)
            if written is not None and written.reviews and self.attention is not None:
                await self.attention.raise_item(
                    "lte:review",
                    "info",
                    "LTE: проверьте серию подписки",
                    "Периоды LTE пересчитаны по событию с неполными данными: "
                    + "; ".join(written.reviews[:3]),
                )
        except Exception:
            log.exception("lte: post-commit step failed")

    # -------------------------------------------------------------------------------------- the cycle

    async def run_cycle(self, *, at: datetime | None = None) -> CycleReport:
        """One cycle (``lte.cycle``). Errors of a step are isolated: the next step still runs.

        Panel reads (topology, usage) run under :attr:`cycle_lock` only; :attr:`lock` — shared with the term
        hook — covers just the DB steps (catch-up, timers, decisions), each with its own time budget."""
        async with self.cycle_lock:
            at = at or now()
            cfg = self.cfg()
            await self.refresh_topology(at=at)
            async with self.db.tx() as conn:
                model = await load_model(conn, at=at)
                await sync_twins(conn, model.twins.values())
            collect = CollectOutcome(read_at=at, group_states={})
            error = None
            if self.collector is not None and model.group_nodes:
                try:
                    collect = await self.collector.collect(
                        group_nodes=model.group_nodes, params=cfg.accounting, topology=self.topology, at=at
                    )
                except Exception as err:  # noqa: BLE001 - incomplete cycle: decisions still run (releases)
                    error = type(err).__name__
                    log.warning("lte: accounting failed: %s", error)
                    collect = CollectOutcome(
                        read_at=at,
                        group_states={
                            g: GroupState(incomplete=("cycle_partial",)) for g in model.group_nodes
                        },
                        error=error,
                    )
            await self._remember_incidents(collect)
            async with self.lock:
                await self._catch_up(at)
                await self._timers(at)
                async with self.db.tx() as conn:
                    applied = await self.decide_for(conn, None, at=at, collect=collect, model=model)
            await self._after(applied)
            await self._after_block_check(at)
            report = CycleReport(
                at=at,
                ok=collect.ok and error is None,
                requests=collect.requests,
                failed=collect.failed_requests,
                blocks=len(applied.placed),
                releases=len(applied.released),
                unenforceable=len(applied.unenforceable),
                quarantine=tuple(sorted(self.last_plan.quarantine)) if self.last_plan is not None else (),
                error=error,
            )
            self.last_report = report
            async with self.db.tx() as conn:
                await kv_put(
                    conn,
                    "cycle",
                    {
                        "at": at.isoformat(),
                        "ok": report.ok,
                        "requests": report.requests,
                        "failed": report.failed,
                        "blocks": report.blocks,
                        "releases": report.releases,
                    },
                )
            if report.quarantine and self.attention is not None:
                try:
                    await self.attention.raise_item(
                        "lte:quarantine",
                        "warn",
                        "LTE: карантин новых блоков",
                        "Слишком много новых блоков за цикл — они не поставлены. Проверьте учёт "
                        "и подтвердите на экране «🌐 Трафик LTE».",
                    )
                except Exception:
                    log.exception("lte: quarantine attention failed")
            return report

    async def _remember_incidents(self, collect: CollectOutcome) -> None:
        """Rows vanished / history rollback are sticky until an admin clears them (05 §2.1.8)."""
        result = collect.result
        if result is None or not result.incidents:
            return
        groups: dict[int, set[str]] = {}
        async with self.db.read() as conn:
            model = await load_model(conn, at=collect.read_at)
        for gid, nodes in model.group_nodes.items():
            for node, reasons in result.incidents.items():
                if node in nodes:
                    groups.setdefault(gid, set()).update(reasons)
        if not groups:
            return
        async with self.db.tx() as conn:
            state = await kv_get(conn, "fuses")
            incidents = {str(k): set(v) for k, v in (state.get("incidents") or {}).items()}
            for gid, reasons in groups.items():
                incidents.setdefault(str(gid), set()).update(reasons)
            state["incidents"] = {k: sorted(v) for k, v in incidents.items()}
            await kv_put(conn, "fuses", state)
        if self.attention is not None:
            try:
                await self.attention.raise_item(
                    "lte:incident",
                    "error",
                    "LTE: история панели пропала или откатилась",
                    "Новые блоки LTE остановлены до проверки (разблокировки продолжаются). "
                    "Проверьте панель и снимите инцидент на экране «🌐 Трафик LTE».",
                )
            except Exception:
                log.exception("lte: incident attention failed")

    async def _catch_up(self, at: datetime) -> int:
        """Subscriptions with unprocessed term events (a missed hook, a restart) are processed here.

        The scan reads ``subscription_events`` above a global high-water mark (``lte_kv['catch_up']``) in id
        order — an index range, not the whole history. A subscription whose events keep failing is parked
        with a backoff (retried a few per cycle, «Требует внимания» after :data:`FAILED_ALERT_AFTER` tries),
        so it never takes the slots of the others; the mark moves past rows that need nothing more.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + CATCH_UP_BUDGET_S
        e, cur = subscription_events.c, lte_event_cursor.c
        async with self.db.read() as conn:
            state = await kv_get(conn, CATCH_UP_KEY)
            hwm = int(state.get("hwm") or 0)
            rows = (
                await conn.execute(
                    sa.select(
                        e.id,
                        e.subscription_id,
                        e.ts,
                        sa.func.coalesce(cur.last_event_id, 0).label("done"),
                    )
                    .select_from(
                        subscription_events.outerjoin(
                            lte_event_cursor, cur.subscription_id == e.subscription_id
                        )
                    )
                    .where(e.id > hwm)
                    .order_by(e.id)
                    .limit(CATCH_UP_SCAN)
                )
            ).all()
        failed: dict[str, dict[str, Any]] = {
            str(k): dict(v) for k, v in (state.get("failed") or {}).items() if isinstance(v, Mapping)
        }
        pending: list[int] = []
        seen: set[int] = set()
        for r in rows:
            sid = int(r.subscription_id)
            if r.id > r.done and sid not in seen:
                seen.add(sid)
                if str(sid) not in failed:
                    pending.append(sid)
        retries = sorted(
            (int(k) for k, v in failed.items() if _parse_at(v.get("next")) <= at),
            key=lambda sid: _parse_at(failed[str(sid)].get("next")),
        )[:CATCH_UP_RETRIES]
        ok: set[int] = set()
        bad: set[int] = set()
        for sid in (*retries, *pending[:CATCH_UP_LIMIT]):
            if loop.time() > deadline:
                break
            try:
                async with self.db.tx() as conn:
                    written = await self.apply_events(conn, sid, at=at)
                await self._after(None, written)
                ok.add(sid)
            except Exception:
                log.exception("lte: term events of subscription %s failed", sid)
                bad.add(sid)
        alerts = 0
        for sid in ok:
            failed.pop(str(sid), None)
        for sid in sorted(bad):
            if str(sid) not in failed and len(failed) >= FAILED_KEEP:
                continue  # beyond the cap it stays in the scan (the old behaviour) — something systemic
            n = int(failed.get(str(sid), {}).get("n") or 0) + 1
            delay = min(RETRY_BASE * 2 ** min(n - 1, 10), RETRY_MAX)
            failed[str(sid)] = {"n": n, "next": (at + delay).isoformat()}
            alerts += n == FAILED_ALERT_AFTER
        cutoff = at - HWM_LAG
        mark = hwm
        for r in rows:
            sid = int(r.subscription_id)
            settled = r.id <= r.done or sid in ok or str(sid) in failed
            if not settled or r.ts > cutoff:
                break
            mark = int(r.id)
        if mark != hwm or failed != (state.get("failed") or {}):
            async with self.db.tx() as conn:
                await kv_put(conn, CATCH_UP_KEY, {"hwm": mark, "failed": failed})
        if alerts and self.attention is not None:
            try:
                await self.attention.raise_item(
                    "lte:term_failed",
                    "warn",
                    "LTE: события срока не обрабатываются",
                    f"Периоды LTE не обновляются у {len(failed)} подписок: ошибка повторяется. "
                    "Повтор идёт с паузой; подробности — в журнале ошибок.",
                )
            except Exception:
                log.exception("lte: term-failed attention failed")
        return len(ok)

    async def _timers(self, at: datetime) -> int:
        """Boundaries, deferrals and series ends that are due (05 §2.1.9 timers).

        Pending term events of the subscription are applied first under the same cursor lock: an event
        committed after :meth:`_catch_up` (a renewal, an unfreeze) is never overtaken by the timers.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + TIMERS_BUDGET_S
        params = self.cfg().periods
        a, p, s = lte_anchors.c, lte_periods.c, subscriptions.c
        grace = params.grace
        async with self.db.read() as conn:
            parked = {int(k) for k in ((await kv_get(conn, CATCH_UP_KEY)).get("failed") or {})}
            sids = (
                (
                    await conn.execute(
                        sa.select(a.subscription_id)
                        .select_from(
                            lte_anchors.outerjoin(
                                lte_periods,
                                sa.and_(p.subscription_id == a.subscription_id, p.state != "closed"),
                            ).join(subscriptions, s.id == a.subscription_id)
                        )
                        .where(
                            sa.or_(
                                sa.and_(p.state == "open", p.planned_end_at <= at),
                                sa.and_(
                                    a.series_open,
                                    a.coverage_end.is_not(None),
                                    a.coverage_end + grace <= at,
                                    s.hold_kind.is_(None),  # E8 waits while frozen
                                ),
                            )
                        )
                        .order_by(a.subscription_id)
                        .limit(TIMERS_LIMIT)
                    )
                )
                .scalars()
                .all()
            )
        done = 0
        for sid in (int(x) for x in sids):
            if sid in parked:
                continue  # its events fail: the catch-up retries it with a backoff
            if loop.time() > deadline:
                break
            try:
                async with self.db.tx() as conn:
                    written = await self.apply_events(conn, sid, at=at)
                    if written is None:
                        state = await load_state(conn, sid)
                        out = advance(state, until=at, params=params, holds=await self._holds(conn, sid))
                        if out.actions:
                            written = await write_outcome(conn, sid, out, enforcer=self.enforcer, at=at)
                if written is not None:
                    await self._after(None, written)
                    done += 1
            except Exception:
                log.exception("lte: timers of subscription %s failed", sid)
        return done

    async def _after_block_check(self, at: datetime) -> int:
        """«Трафик после блока»: **new** deltas 20+ min after an applied block → re-send, at most hourly."""
        b, u = lte_blocks.c, lte_period_usage.c
        async with self.db.read() as conn:
            rows = (
                await conn.execute(
                    sa.select(b.id, b.subscription_id, b.applied_at, b.resend_done_at, u.last_delta_at)
                    .select_from(
                        lte_blocks.join(
                            lte_period_usage, sa.and_(u.period_id == b.period_id, u.group_id == b.group_id)
                        )
                    )
                    .where(b.status == "active", b.mode == "enforce", b.applied_at.is_not(None))
                )
            ).all()
        due = [
            r
            for r in rows
            if after_block_resend_due(
                applied_at=r.applied_at,
                last_delta_at=r.last_delta_at,
                resend_done_at=r.resend_done_at,
                now=at,
            )
        ]
        if not due:
            return 0
        async with self.db.tx() as conn:
            for r in due:
                await enqueue_resend(conn, int(r.subscription_id), int(r.id), at=at - timedelta(seconds=150))
                await conn.execute(
                    sa.update(lte_blocks).where(lte_blocks.c.id == r.id).values(resend_done_at=at)
                )
        if self.attention is not None:
            try:
                await self.attention.raise_item(
                    "lte:after_block",
                    "warn",
                    "LTE: трафик после блока",
                    f"У {len(due)} заблокированных подписок идёт новый трафик по LTE. "
                    "Бот переотправил сквады; если повторяется — проверьте ноды LTE.",
                )
            except Exception:
                log.exception("lte: after-block attention failed")
        return len(due)

    # ------------------------------------------------------------------------------- daily / retention

    async def daily(self, *, at: datetime | None = None) -> int:
        """Expired individual overrides and the ``launch_trial`` safety net (05 §2.1.9, 2.1.14)."""
        at = at or now()
        o = lte_overrides.c
        async with self.db.tx() as conn:
            expired = (
                (
                    await conn.execute(
                        sa.update(lte_overrides)
                        .where(o.revoked_at.is_(None), o.valid_until.is_not(None), o.valid_until <= at)
                        .values(revoked_at=at, revoke_reason="expired")
                        .returning(o.id)
                    )
                )
                .scalars()
                .all()
            )
            e, cur = subscription_events.c, lte_event_cursor.c
            paid = (
                sa.exists()
                .where(
                    e.subscription_id == o.subscription_id,
                    e.kind.in_(sorted(PAID_CORE_KINDS)),
                    e.source.not_in(sorted(NON_MONEY_SOURCES)),  # an admin plan or a gift is not money
                    cur.subscription_id == o.subscription_id,
                    e.id <= cur.last_event_id,  # a payment the period machine has already seen
                )
                .correlate(lte_overrides)
            )
            stuck = (
                (
                    await conn.execute(
                        sa.update(lte_overrides)
                        .where(
                            o.kind == "exempt", o.exempt_kind == "launch_trial", o.revoked_at.is_(None), paid
                        )
                        .values(revoked_at=at, revoke_reason="converted_to_paid")
                        .returning(o.subscription_id)
                    )
                )
                .scalars()
                .all()
            )
            for sid in stuck:
                await audit(conn, None, "lte.exemption_revoked", f"sub:{sid}", reason="converted_to_paid")
        if stuck and self.attention is not None:
            await self.attention.raise_item(
                "lte.launch_trial_stuck",
                "info",
                "LTE: исключение триала снято страховкой",
                f"У {len(stuck)} подписок исключение «триал на запуске» осталось после оплаты "
                "и снято сейчас.",
            )
        return len(expired) + len(stuck)

    async def retention(self, *, at: datetime | None = None) -> int:
        """03:40 MSK: counters 3 days (the last read state of a node stays), hourly 72 h, daily 70 days,
        closed periods 13 months."""
        at = at or now()
        cut = retention_cutoffs(at)
        total = 0
        async with self.db.tx() as conn:
            ns = lte_node_state.c
            for node, last_date in (await conn.execute(sa.select(ns.node_uuid, ns.last_ok_read_date))).all():
                from svbg.ext.lte.accounting import counters_retention_cutoff

                cutoff = counters_retention_cutoff(today_utc=at.date(), last_ok_read_date=last_date)
                r = await conn.execute(
                    sa.delete(lte_counters).where(
                        lte_counters.c.node_uuid == node, lte_counters.c.usage_date < cutoff
                    )
                )
                total += int(r.rowcount or 0)
            for stmt in (
                sa.delete(lte_usage_hourly).where(lte_usage_hourly.c.hour_utc < cut.hourly_before),
                sa.delete(lte_usage_daily).where(lte_usage_daily.c.msk_date < cut.daily_before),
                sa.delete(lte_periods).where(
                    lte_periods.c.state == "closed", lte_periods.c.ended_at < cut.closed_periods_before
                ),
            ):
                total += int((await conn.execute(stmt)).rowcount or 0)
        return total

    # ------------------------------------------------------------------------------------- invariants

    async def invariants(self, *, at: datetime | None = None) -> list[str]:
        """I2/I3/I4/I7/I11 by the panel topology; problems go to ``lte_twins.problem`` and attention."""
        at = at or now()
        topology = await self.refresh_topology(force=True, at=at)
        if topology is None:
            return []
        async with self.db.read() as conn:
            model = await load_model(conn, at=at)
        tags = topology.group_tags(model.group_nodes)
        problems = [
            *check_twins(model.twins.values(), squad_inbounds=topology.squad_inbounds, group_tags=tags),
            *check_foreign_nodes(
                node_inbounds=topology.node_inbounds(), group_nodes=model.group_nodes, group_tags=tags
            ),
        ]
        by_twin: dict[str, str] = {}
        for pr in problems:
            if pr.code in ("twin_missing", "twin_empty", "twin_mismatch", "twin_is_base"):
                by_twin[pr.subject] = f"{pr.code}: {pr.detail}".strip(": ")[:200]
        async with self.db.tx() as conn:
            for twin in model.twins.values():
                await conn.execute(
                    sa.update(lte_twins)
                    .where(lte_twins.c.base_squad_uuid == twin.base_squad_uuid)
                    .values(
                        checked_at=at,
                        problem=by_twin.get(twin.twin_squad_uuid) or by_twin.get(twin.base_squad_uuid),
                    )
                )
        keys = [f"lte:inv:{pr.code}:{pr.subject}"[:200] for pr in problems]
        if self.attention is not None:
            try:
                for pr, key in zip(problems, keys, strict=True):
                    await self.attention.raise_item(
                        key,
                        "warn",
                        _INV_TITLES.get(pr.code, "LTE: нарушен инвариант"),
                        pr.detail or pr.subject,
                    )
                await self.attention.auto_resolve("lte:inv:", keep=keys)
            except Exception:
                log.exception("lte: invariants attention failed")
        return keys

    # ----------------------------------------------------------------------------------- off / on

    async def release_all(self, reason: str, *, actor_id: int | None = None, due_only: bool = False) -> int:
        n = await self.enforcer.release_all(reason, due_only=due_only)
        async with self.db.tx() as conn:
            await audit(conn, actor_id, "lte.release_all", "lte", reason=reason, details={"released": n})
        await notify.post_cards(self.admin_chat, [notify.card_report("release_all", n=n, reason=reason)])
        return n

    async def on_setup(self) -> None:
        """Re-enabled after a switch-off: a fresh baseline (the bytes of the off time are not charged)."""
        async with self.db.tx() as conn:
            state = await kv_get(conn, "module")
            if state.get("stopped") and state.get("stopped_enabled") is False:
                await conn.execute(sa.delete(lte_counters))
                await conn.execute(sa.delete(lte_node_state))
            await kv_put(conn, "module", {"started": now().isoformat()})

    async def on_teardown(self) -> None:
        """Stop (switch-off or shutdown): remember whether the module was switched off."""
        async with self.db.tx() as conn:
            await kv_put(
                conn, "module", {"stopped": now().isoformat(), "stopped_enabled": self.cfg().enabled}
            )

    # ------------------------------------------------------------------------------------ pack check

    async def topup_available(self, sid: int, group_id: int) -> bool:
        from svbg.ext.lte import packs

        try:
            offer = await packs.offer_for(self, sid, group_id)
        except Exception:  # noqa: BLE001
            return False
        return offer is not None and offer.ok

    # ---------------------------------------------------------------------------------------- health

    async def health(self) -> HealthReport:
        async with self.db.read() as conn:
            state = await kv_get(conn, "cycle")
        at_raw = state.get("at")
        if not at_raw:
            return HealthReport.ok("Ждёт первого цикла учёта")
        last = datetime.fromisoformat(str(at_raw))
        age = now() - last
        if age > STALE_CYCLE:
            return HealthReport.degraded(
                f"Последний цикл учёта {int(age.total_seconds() // 60)} мин назад (норма ≤ 10)"
            )
        if not state.get("ok"):
            return HealthReport.degraded("Последний цикл учёта неполный: часть чтений панели не удалась")
        return HealthReport.ok("Учёт работает")

    async def status_lines(self) -> list[str]:
        cfg = self.cfg()
        async with self.db.read() as conn:
            state = await kv_get(conn, "cycle")
            blocked = await conn.scalar(
                sa.select(sa.func.count()).where(
                    lte_blocks.c.status == "active", lte_blocks.c.mode == "enforce"
                )
            )
        lines = [f"Применение: {cfg.mode}", f"Заблокировано: {int(blocked or 0)}"]
        if state.get("at"):
            last = datetime.fromisoformat(str(state["at"]))
            lines.append(f"Последний цикл: {int((now() - last).total_seconds() // 60)} мин назад")
        return lines


_INV_TITLES: Final[Mapping[str, str]] = {
    "twin_missing": "LTE: двойника нет в панели",
    "twin_empty": "LTE: двойник пустой",
    "twin_mismatch": "LTE: двойник не равен база − инбаунды LTE",
    "twin_is_base": "LTE: двойник совпадает с базой",
    "base_without_twin": "LTE: у сквада с инбаундами LTE нет двойника",
    "foreign_node": "LTE: инбаунды LTE на чужой ноде",
}


# ------------------------------------------------------------------------------------------- runtime


class Runtime:
    """The module context is remembered at the first hook (jobs only get ``(job, job_ctx)``)."""

    def __init__(self) -> None:
        self.ctx: ModuleContext | None = None
        self._service: LteService | None = None

    def bind(self, ctx: ModuleContext) -> None:
        if self.ctx is not ctx:
            self.ctx = ctx
            self._service = None

    def service(self) -> LteService:
        if self._service is None:
            ctx = self.ctx
            if ctx is None:
                raise LookupError("lte: the module context is not bound yet")
            deps = ctx.deps
            self._service = LteService(
                ctx.dep("db"),
                deps.get("api"),
                config=ctx.config,
                attention=deps.get("attention"),
                admin_chat=deps.get("admin_chat"),
                notifier=deps.get("notifier"),
                settings=deps.get("settings"),
            )
        return self._service

    def set_service(self, service: LteService | None) -> None:
        """Tests and the integration may inject a ready service."""
        self._service = service


RUNTIME = Runtime()


async def setup(ctx: ModuleContext) -> None:
    RUNTIME.bind(ctx)
    ctx.dep("db")
    await RUNTIME.service().on_setup()


async def teardown(ctx: ModuleContext) -> None:
    RUNTIME.bind(ctx)
    await RUNTIME.service().on_teardown()


async def cycle(ctx: ModuleContext) -> None:
    RUNTIME.bind(ctx)
    await RUNTIME.service().run_cycle()


async def invariants(ctx: ModuleContext) -> None:
    RUNTIME.bind(ctx)
    await RUNTIME.service().invariants()


async def daily(ctx: ModuleContext) -> None:
    RUNTIME.bind(ctx)
    await RUNTIME.service().daily()


async def retention(ctx: ModuleContext) -> None:
    RUNTIME.bind(ctx)
    await RUNTIME.service().retention()


async def health(ctx: ModuleContext) -> HealthReport:
    RUNTIME.bind(ctx)
    return await RUNTIME.service().health()


async def status(ctx: ModuleContext) -> Sequence[str]:
    RUNTIME.bind(ctx)
    return await RUNTIME.service().status_lines()


async def term_job(job: Job, jctx: JobContext) -> None:
    del jctx
    await RUNTIME.service().process_subscription(int(job.payload["sub_id"]), wait=TERM_LOCK_WAIT_S)


async def confirm_job(job: Job, jctx: JobContext) -> None:
    await RUNTIME.service().enforcer.confirm_job(job, jctx)


async def resend_job(job: Job, jctx: JobContext) -> None:
    await RUNTIME.service().enforcer.resend_job(job, jctx)


async def notify_job(job: Job, jctx: JobContext) -> None:
    await RUNTIME.service().sender.send_job(job, jctx)


async def card_job(job: Job, jctx: JobContext) -> None:
    """``lte.card``: a card for the topic «🌐 Трафик LTE», queued in a business transaction."""
    del jctx
    card = notify.card_from_payload(job.payload)
    if card is not None:
        await notify.post_cards(RUNTIME.service().admin_chat, [card])


async def enqueue_term(conn: AsyncConnection, sid: int, *, caused_by: str | None = None) -> int | None:
    """``lte.term`` for one subscription (one pending per subscription)."""
    return await enqueue(
        conn,
        TERM_KIND,
        {"sub_id": int(sid)},
        queue="hook",
        lane="interactive",
        dedup_key=f"lte:sub:{int(sid)}",
        max_attempts=10,
        caused_by=caused_by,
    )


async def on_subscription_event(event: BusEvent) -> None:
    """X2/X3: a term change → the module's own durable job (the bus is best effort)."""
    payload = event.payload if isinstance(event.payload, Mapping) else {}
    sid = payload.get("subscription_id")
    if isinstance(sid, bool) or not isinstance(sid, int):
        return
    service = RUNTIME.service()
    async with service.db.tx() as conn:
        await enqueue_term(conn, sid, caused_by=f"event:{event.name}")


def _slot(module: str, name: str) -> Callable[[Any], Any]:
    def call(c: Any) -> Any:
        import importlib

        return getattr(importlib.import_module(f"svbg.ext.lte.{module}"), name)(c)

    call.__qualname__ = f"lte.{name}"
    return call


class _LazyItem:
    """Order item / kind handlers resolved on first use (the manifest stays import-light)."""

    def __init__(self, attr: str) -> None:
        self._attr = attr

    async def fulfill(self, conn: AsyncConnection, order: Mapping[str, Any], item: Mapping[str, Any]) -> None:
        from svbg.ext.lte import packs

        await getattr(packs, self._attr)().fulfill(conn, order, item)

    async def apply(
        self, conn: AsyncConnection, order: Mapping[str, Any], items: Sequence[Mapping[str, Any]]
    ) -> int:
        from svbg.ext.lte import packs

        return int(await getattr(packs, self._attr)().apply(conn, order, items))


async def _ui(router: Any, ctx: ModuleContext) -> None:
    RUNTIME.bind(ctx)
    if not (hasattr(router, "screen") and hasattr(router, "action")):
        return
    from svbg.ext.lte import admin, ui

    ui.install(router, RUNTIME.service)
    admin.install(router, RUNTIME.service)


SPEC = ModuleSpec(
    name=MODULE,
    title="Трафик LTE",
    enabled_key=K_ENABLED,
    settings=SETTINGS,
    section=SECTION,
    topics=(Topic("lte", "Трафик LTE", "🌐", priority="normal", noun=("карточка", "карточки", "карточек")),),
    perms=(
        Perm("lte.view", "LTE: просмотр", "Строки квоты в карточке пользователя"),
        Perm("lte.users", "LTE: пользователи", "Исключения, +ГБ, блок и разблок, индивидуальный лимит"),
        Perm("lte.config", "LTE: настройка", "Лимиты, применение, ноды, двойники, аварийное снятие"),
    ),
    tasks=(
        Periodic(
            "cycle",
            lazy("svbg.ext.lte.service:cycle"),
            every_s=CYCLE_S,
            jitter_s=5,
            timeout_s=CYCLE_TIMEOUT_S,
        ),
        Periodic(
            "invariants", lazy("svbg.ext.lte.service:invariants"), every_s=3600, jitter_s=30, timeout_s=120
        ),
        Periodic("daily", lazy("svbg.ext.lte.service:daily"), daily_at=time(3, 30), optional=False),
        Periodic("retention", lazy("svbg.ext.lte.service:retention"), daily_at=time(3, 40), optional=False),
    ),
    jobs=(
        JobDef(TERM_KIND, lazy("svbg.ext.lte.service:term_job"), when_disabled="skip", timeout_s=60),
        JobDef("lte.confirm", lazy("svbg.ext.lte.service:confirm_job"), when_disabled="run", timeout_s=30),
        JobDef("lte.resend", lazy("svbg.ext.lte.service:resend_job"), when_disabled="run", timeout_s=30),
        JobDef(notify.JOB_KIND, lazy("svbg.ext.lte.service:notify_job"), when_disabled="skip", timeout_s=30),
        JobDef(CARD_KIND, lazy("svbg.ext.lte.service:card_job"), when_disabled="run", timeout_s=30),
    ),
    events=(
        BusSub("subscription.*", lazy("svbg.ext.lte.service:on_subscription_event")),
        BusSub("trial.activated", lazy("svbg.ext.lte.service:on_subscription_event")),
    ),
    slots=(
        Slot("home", "status_lines", _slot("ui", "render_status_line"), order=20),
        Slot("subscription", "blocks", _slot("ui", "render_blocks"), order=20),
        Slot("subscription", "buttons", _slot("ui", "render_buttons"), order=20),
        Slot("admin.user_card", "sections", _slot("admin", "render_card"), perm="lte.view"),
        Slot("admin.home", "entries", _slot("admin", "render_home_entry"), order=50),
    ),
    views=(
        ViewLoader("home", lazy("svbg.ext.lte.ui:load_view")),
        ViewLoader("subscription", lazy("svbg.ext.lte.ui:load_view")),
        ViewLoader("admin.user_card", lazy("svbg.ext.lte.admin:load_card")),
    ),
    order_items={"lte_pack": _LazyItem("pack_item")},
    order_kinds={"addon_lte": _LazyItem("addon_kind")},
    owns_substitutions=True,
    setup=lazy("svbg.ext.lte.service:setup"),
    teardown=lazy("svbg.ext.lte.service:teardown"),
    health=lazy("svbg.ext.lte.service:health"),
    status=lazy("svbg.ext.lte.service:status"),
    report=lazy("svbg.ext.lte.service:status"),
    ui=_ui,
)
