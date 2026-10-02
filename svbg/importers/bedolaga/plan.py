"""The Bedolaga importer: modes, run bookkeeping, stage order (06 §2, §4.2; 02 §7.3–7.4).

Modes (``import_runs.mode``):

* ``dry_run`` — the whole import runs inside **one** transaction that is rolled back: the report (counts, the
  owner's lists of §4.2 п.1, checks С1–С5/С10/С12) is exactly what ``apply`` would do, and nothing stays in
  the
  database except the ``import_runs`` row. The panel is optional (without it every subscription is
  ``unverified``);
* ``shadow`` — the same import committed (one transaction, all or nothing) into the stand database, repeated
  daily against a fresh source: every write is an upsert by an external key, a row the bot changed since the
  previous run is left alone and reported, the wallet follows the source balance by ``import_adjust`` deltas.
  The panel is **read** (``users/stream``) and never written: the importer has no writer, enqueues no jobs;
* ``apply`` — the final import at T0 from the frozen snapshot, identical to ``shadow``; marks
  ``config_meta['import.bedolaga.applied']``.

Stages run in order in one target transaction: catalog → users → subscriptions → [lte, ip_guard] → wallet →
payments → promo → ads → referral → [referral_days] → misc, then every stage's ``check``. The bracketed
owner-module stages (``svbg.importers.bedolaga.{lte,ip_guard,referral_days}``, a separate owner) are picked
up when their module exists: ``async def run(ctx: Ctx)`` and an optional ``async def check(ctx: Ctx)``; a
missing one is listed in the report (``counts.modules``) and **blocks the cut-over** (``module_missing``)
as soon as the source has state only that module carries over (active LTE blocks / open periods / sent LTE
notices; active IP Guard blocks or a whitelist; referral-days markers) — without it the writer would touch
frozen terms, LTE users would lose their quota state and rewarded pairs could be rewarded again.

Owner decisions (``import_overrides``, 06 §4.2 п.2) live in ``config_meta['import.bedolaga.overrides']``:
``{"skip_panel_user_ids": [..], "link": {"<sub id>": <panel id>}, "skip_subscription_ids": [..],
"ack_restricted_user_ids": [..]}`` and are applied on every run (the last one: the owner has applied the
user's Bedolaga restriction by hand — the bot has no column for it yet, see ``users.py``).
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from svbg.core.clock import now
from svbg.core.tables import config_meta
from svbg.importers import legacy_id_map
from svbg.importers.bedolaga.report import Report
from svbg.importers.bedolaga.source import BedolagaSource, SourceSettings, open_source
from svbg.remnawave.tables import import_runs

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from svbg.core.crypto import Crypto
    from svbg.db.engine import Database
    from svbg.importers.bedolaga.subscriptions import PanelIndex, PanelReader

    ImporterPort = Callable[..., Awaitable[Mapping[str, Any]]]

__all__ = [
    "APPLIED_KEY",
    "MODES",
    "MODULE_STAGES",
    "OVERRIDES_KEY",
    "SOURCE",
    "BedolagaImporter",
    "Ctx",
    "ImportConfig",
    "Overrides",
    "config_from_env_file",
    "id_floor",
    "importer_port",
    "reserve_ids",
]

log = logging.getLogger("svbg.importers.bedolaga")

SOURCE: Final = "bedolaga"
MODES: Final = ("dry_run", "shadow", "apply")
OVERRIDES_KEY: Final = "import.bedolaga.overrides"
APPLIED_KEY: Final = "import.bedolaga.applied"

SourceFactory = Callable[[], AbstractAsyncContextManager[BedolagaSource]]


@dataclass(frozen=True, slots=True)
class ImportConfig:
    currency: str = "RUB"
    #: Parsed Bedolaga ``.env`` (only values are used; secrets are never copied by this importer).
    env: Mapping[str, str | None] = field(default_factory=dict)
    #: The snapshot moment (T0); ``None`` = now.
    t0: datetime | None = None
    #: Encrypts the config of placeholder payment instances created for history (``enabled=false``).
    crypto: Crypto | None = None
    overrides: Mapping[str, Any] | None = None
    rollypay_live_hours: int = 48
    cryptobot_live_hours: int = 24
    notify_window_days: int = 3


def config_from_env_file(path: str | Path, **kwargs: Any) -> ImportConfig:
    """``ImportConfig`` with the Bedolaga ``.env`` parsed like python-dotenv (06 §3.1, R15)."""
    from svbg.importers.envmap import read_env_file

    return ImportConfig(env=read_env_file(path).values, **kwargs)


@dataclass(slots=True)
class Overrides:
    skip_panel_user_ids: set[int] = field(default_factory=set)
    link: dict[int, int] = field(default_factory=dict)
    skip_subscription_ids: set[int] = field(default_factory=set)
    ack_restricted_user_ids: set[int] = field(default_factory=set)

    @classmethod
    def parse(cls, *sources: Mapping[str, Any] | None) -> Overrides:
        out = cls()
        for src in sources:
            if not src:
                continue
            out.skip_panel_user_ids.update(int(x) for x in src.get("skip_panel_user_ids") or ())
            out.skip_subscription_ids.update(int(x) for x in src.get("skip_subscription_ids") or ())
            out.link.update({int(k): int(v) for k, v in (src.get("link") or {}).items()})
            out.ack_restricted_user_ids.update(int(x) for x in src.get("ack_restricted_user_ids") or ())
        return out


@dataclass(slots=True)
class PlanInfo:
    id: int
    device_limit: int | None
    squads: list[str]


@dataclass(slots=True)
class Ctx:
    """Everything a stage needs. ``conn`` is the target transaction (rolled back in ``dry_run``)."""

    conn: AsyncConnection
    src: BedolagaSource
    settings: SourceSettings
    cfg: ImportConfig
    mode: str
    run_id: int
    t0: datetime
    report: Report
    panel: PanelIndex | None = None
    overrides: Overrides = field(default_factory=Overrides)
    #: Bedolaga user id → its source row, for users present in the target after the users stage.
    users: dict[int, dict[str, Any]] = field(default_factory=dict)
    #: Bedolaga subscription id → target subscription id (the same number) after the subscriptions stage.
    subs: dict[int, int] = field(default_factory=dict)
    #: Source subscriptions by user (all of them, for trial_used_at and notifications).
    source_subs: dict[int, dict[str, Any]] = field(default_factory=dict)
    #: Every source user row (imported or not): a skipped subscription still resolves its panel account.
    source_users: dict[int, dict[str, Any]] = field(default_factory=dict)
    #: ``MAX(id)`` per source table (users / subscriptions), skipped rows included.
    source_max: dict[str, int] = field(default_factory=dict)
    squad_by_server: dict[int, str] = field(default_factory=dict)
    twins: dict[str, str] = field(default_factory=dict)
    plan: PlanInfo | None = None
    instances: dict[str, int] = field(default_factory=dict)
    promos: dict[int, int] = field(default_factory=dict)
    ad_links: dict[int, int] = field(default_factory=dict)
    _maps: dict[str, dict[str, tuple[str, dict[str, Any]]]] = field(default_factory=dict)

    @property
    def dry(self) -> bool:
        return self.mode == "dry_run"

    async def mapped(self, entity: str) -> dict[str, tuple[str, dict[str, Any]]]:
        """``old_id → (new_id, data)`` of earlier runs (cached for the run)."""
        if entity not in self._maps:
            m = legacy_id_map
            rows = (
                await self.conn.execute(
                    sa.select(m.c.old_id, m.c.new_id, m.c.data).where(
                        m.c.source == SOURCE, m.c.entity == entity
                    )
                )
            ).all()
            self._maps[entity] = {str(r[0]): (str(r[1]), dict(r[2] or {})) for r in rows}
        return self._maps[entity]

    async def remember(self, entity: str, rows: Sequence[tuple[Any, Any, Mapping[str, Any]]]) -> None:
        """Upsert ``(old_id, new_id, data)`` into ``legacy_id_map``."""
        if not rows:
            return
        cache = await self.mapped(entity)
        values = [
            {
                "source": SOURCE,
                "entity": entity,
                "old_id": str(old),
                "new_id": str(new),
                "run_id": self.run_id,
                "data": dict(data),
            }
            for old, new, data in rows
        ]
        for chunk in _chunks(values, 1000):
            stmt = pg_insert(legacy_id_map).values(chunk)
            await self.conn.execute(
                stmt.on_conflict_do_update(
                    constraint="pk_legacy_id_map",
                    set_={
                        "new_id": stmt.excluded.new_id,
                        "run_id": stmt.excluded.run_id,
                        "data": stmt.excluded.data,
                        "updated_at": sa.func.now(),
                    },
                )
            )
        for v in values:
            cache[v["old_id"]] = (v["new_id"], v["data"])


def _chunks(seq: Sequence[Any], size: int) -> list[Sequence[Any]]:
    return [seq[i : i + size] for i in range(0, len(seq), size)]


chunks = _chunks


def uniform(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """A multi-row ``INSERT … VALUES`` needs the same keys in every row: missing ones become ``NULL``."""
    keys: dict[str, None] = {}
    for r in rows:
        keys.update(dict.fromkeys(r))
    return [{k: r.get(k) for k in keys} for r in rows]


class _DryRunRollback(Exception):
    """Raised at the end of a dry run to roll the transaction back."""


Stage = Callable[[Ctx], Any]

#: Owner-module stages (another owner's files) and the core stage each one runs after.
MODULE_STAGES: Final = (
    ("lte", "subscriptions"),
    ("ip_guard", "subscriptions"),
    ("referral_days", "referral"),
)
StageList = list[tuple[str, Stage, Stage | None]]


def _module(name: str) -> tuple[Stage, Stage | None] | None:
    qualified = f"svbg.importers.bedolaga.{name}"
    try:
        mod = importlib.import_module(qualified)
    except ModuleNotFoundError as exc:
        if exc.name != qualified:
            raise  # the module exists but one of its own imports is broken: fail loudly
        return None
    run = getattr(mod, "run", None)
    if not callable(run):
        return None
    check = getattr(mod, "check", None)
    return run, (check if callable(check) else None)


def _with_modules(core: StageList, report: Report) -> StageList:
    """Insert the owner-module stages that exist right after their anchor (in ``MODULE_STAGES`` order)."""
    out = list(core)
    for name, after in MODULE_STAGES:
        found = _module(name)
        report.set("modules", name, 1 if found else 0)
        if found is None:
            continue
        group = {after} | {m for m, a in MODULE_STAGES if a == after}
        at = max((i for i, (n, *_rest) in enumerate(out) if n in group), default=len(out) - 1)
        out.insert(at + 1, (name, *found))
    return out


#: Source state that only an owner module carries over: ``module → [(table, WHERE, what)]``.
_MODULE_STATE: Final = {
    "lte": (
        ("wlq_blocks", "status IN ('pending_apply', 'active') AND mode = 'enforce'", "активные блоки LTE"),
        ("wlq_periods", "state IN ('open', 'deferred')", "открытые периоды LTE"),
        ("wlq_notifications", "state = 'sent'", "отправленные уведомления LTE"),
        # The twin -> base map (panel_squad_substitutions) is the module's: without it the writer would
        # "repair" a twin squad in the panel back to the base one (С6/С7).
        ("wlq_squads", "kind = 'twin'", "двойники сквадов LTE"),
    ),
    "ip_guard": (
        ("ip_guard_blocks", "status = 'active'", "активные блоки IP Guard"),
        (
            "ip_guard_blocks",
            "status = 'unblocked' AND panel_restored IS NOT TRUE",
            "разблокированные, но не восстановленные в панели",
        ),
    ),
    "referral_days": (("referral_earnings", r"reason LIKE 'referral\_days\_%'", "маркеры реферальных дней"),),
}


async def _require_modules(ctx: Ctx) -> None:
    """``module_missing`` (blocking) for every absent owner module whose state the source has."""
    for name, probes in _MODULE_STATE.items():
        if ctx.report.get("modules", name):
            continue
        found: list[str] = []
        for table, where, what in probes:
            if not await ctx.src.has(table):
                continue
            cols = await ctx.src.columns(table)
            needed = {c for c in ("status", "mode", "state", "reason", "panel_restored") if c in where}
            if not needed <= cols:
                continue
            n = int(await ctx.src.scalar(f"SELECT count(*) FROM public.{table} WHERE {where}") or 0)
            if n:
                found.append(f"{what}: {n}")
        if name == "ip_guard" and ctx.settings.get("IP_GUARD_WHITELIST_PANEL_USER_IDS"):
            found.append("белый список IP_GUARD_WHITELIST_PANEL_USER_IDS")
        if found:
            ctx.report.issue("module_missing", module=name, state=found)


def _stages() -> list[tuple[str, Stage, Stage | None]]:
    # Imported lazily: the stage modules import this one.
    from svbg.importers.bedolaga import (
        ads,
        catalog,
        misc,
        payments,
        promo,
        referral,
        subscriptions,
        users,
        wallet,
    )

    return [
        ("catalog", catalog.run, None),
        ("users", users.run, users.check),
        ("subscriptions", subscriptions.run, subscriptions.check),
        ("wallet", wallet.run, wallet.check),
        ("payments", payments.run, payments.check),
        ("promo", promo.run, promo.check),
        ("ads", ads.run, ads.check),
        ("referral", referral.run, referral.check),
        ("misc", misc.run, misc.check),
    ]


class BedolagaImporter:
    def __init__(
        self,
        db: Database,
        source: str | SourceFactory,
        *,
        panel: PanelReader | None = None,
        config: ImportConfig | None = None,
    ) -> None:
        self._db = db
        self._source: SourceFactory = (lambda: open_source(source)) if isinstance(source, str) else source
        self._panel = panel
        self._cfg = config or ImportConfig()

    async def run(self, mode: str = "dry_run") -> Report:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        if mode != "dry_run" and self._panel is None:
            raise ValueError("shadow / apply need the panel (a read-only token is enough)")
        t0 = self._cfg.t0 or now()
        run_id = await self._open(mode, t0)
        report = Report(run_id, mode, t0)
        try:
            await self._run(report, t0)
        except Exception as exc:
            report.error = f"{type(exc).__name__}: {exc}"[:500]
            await self._close(run_id, report, status="failed")
            log.exception("bedolaga import #%s (%s) failed", run_id, mode)
            raise
        report.finished = True
        await self._close(run_id, report, status="done")
        log.info("bedolaga import #%s (%s): green=%s", run_id, mode, report.green)
        return report

    async def _run(self, report: Report, t0: datetime) -> None:
        from svbg.importers.bedolaga.subscriptions import PanelIndex

        panel = await PanelIndex.load(self._panel) if self._panel is not None else None
        async with self._source() as src:
            report.alembic_version = await src.alembic_version()
            settings = SourceSettings(self._cfg.env, await src.system_settings())
            try:
                async with self._db.tx() as conn:
                    stored = await conn.scalar(
                        sa.select(config_meta.c.value).where(config_meta.c.key == OVERRIDES_KEY)
                    )
                    ctx = Ctx(
                        conn=conn,
                        src=src,
                        settings=settings,
                        cfg=self._cfg,
                        mode=report.mode,
                        run_id=report.run_id,
                        t0=t0,
                        report=report,
                        panel=panel,
                        overrides=Overrides.parse(
                            stored if isinstance(stored, Mapping) else None, self._cfg.overrides
                        ),
                    )
                    checks: list[Stage] = []
                    stages = _with_modules(_stages(), report)
                    await _require_modules(ctx)
                    for name, stage, check in stages:
                        report.stage = name
                        await stage(ctx)
                        if check is not None:
                            checks.append(check)
                    report.stage = "checks"
                    for check in checks:
                        await check(ctx)
                    if ctx.dry:
                        raise _DryRunRollback  # noqa: TRY301 - the rollback is the point of a dry run
                    await _bump_sequences(ctx)
                    if report.mode == "apply":
                        value = {"run_id": report.run_id, "at": t0.isoformat()}
                        stmt = pg_insert(config_meta).values(key=APPLIED_KEY, value=value)
                        await conn.execute(
                            stmt.on_conflict_do_update(
                                index_elements=[config_meta.c.key],
                                set_={"value": stmt.excluded.value, "updated_at": sa.func.now()},
                            )
                        )
            except _DryRunRollback:
                pass
        report.stage = None

    async def _open(self, mode: str, t0: datetime) -> int:
        async with self._db.tx() as conn:
            run_id = await conn.scalar(
                sa.insert(import_runs)
                .values(source=SOURCE, mode=mode, filters={"t0": t0.isoformat()})
                .returning(import_runs.c.id)
            )
        return int(run_id)

    async def _close(self, run_id: int, report: Report, *, status: str) -> None:
        async with self._db.tx() as conn:
            await conn.execute(
                sa.update(import_runs)
                .where(import_runs.c.id == run_id)
                .values(status=status, finished_at=now(), cursor=report.stage, report=report.as_json())
            )


#: Room left above the source's ids while shadow runs daily: Bedolaga keeps registering users (and
#: subscriptions) with the next ids, rows the stand creates meanwhile must never take them (06 §2.1).
ID_GAP: Final = 100_000
_ID_TABLES: Final = ("users", "subscriptions")


def id_floor(ctx: Ctx, table: str) -> int:
    """The lowest id the target's own counter may hand out after this run."""
    top = ctx.source_max.get(table, 0)
    return top + ID_GAP if ctx.mode == "shadow" else top


async def reserve_ids(conn: AsyncConnection, table: str, floor: int) -> None:
    """Move the id counter of ``table`` to at least ``floor`` and above ``MAX(id)`` — never backwards.
    ``setval`` is not transactional, so a dry run never calls it."""
    if table not in _ID_TABLES:
        raise ValueError(table)
    seq = f"pg_get_serial_sequence('{table}', 'id')"
    await conn.exec_driver_sql(
        f"SELECT setval({seq}, GREATEST((SELECT COALESCE(MAX(id), 0) FROM {table}), {int(floor)}, "
        f"COALESCE(pg_sequence_last_value({seq}::regclass), 0), 1))"
    )


async def _bump_sequences(ctx: Ctx) -> None:
    """Explicit ids were inserted (users / subscriptions keep Bedolaga ids): the counters go above them."""
    for table in _ID_TABLES:
        await reserve_ids(ctx.conn, table, id_floor(ctx, table))


def importer_port(
    db: Database,
    *,
    panel: Callable[[], PanelReader | None],
    config: Callable[[], ImportConfig] | ImportConfig | None = None,
) -> ImporterPort:
    """The port :class:`svbg.importers.shadow.ShadowService` calls: ``await port(mode="shadow",
    source_dsn=dsn)`` → the report JSON (with ``skipped`` for С1). ``panel`` is called per run (a reader
    over the read-only token's client); ``config`` may be a factory so a daily run picks up fresh overrides.
    """

    async def port(*, mode: str = "shadow", source_dsn: str) -> Mapping[str, Any]:
        cfg = config() if callable(config) else config
        report = await BedolagaImporter(db, source_dsn, panel=panel(), config=cfg).run(mode)
        return report.as_json()

    return port
