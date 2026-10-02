"""Settings service: load, the single apply pipeline, reset, undo, search (03 §3.6–3.7, §5.2).

Every change — from the bot, the ``.env`` file, the CLI, an import or the wizard — goes through
:meth:`SettingsService.apply`:

``parse → validate (field + cross-key) → probe RELOAD components → persist (one transaction: rows + audit)
→ swap snapshot → reconfigure components (failure → automatic rollback) → notify subscribers``.

The result is reported per key and is never "saved" when the value did not take effect.
Reads (:meth:`current`) are a single attribute access: no locks, no database.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

from sqlalchemy.exc import SQLAlchemyError

from svbg.core.component import Component, ComponentRegistry, ProbeError
from svbg.core.crypto import Crypto, CryptoError
from svbg.core.ids import uuid7
from svbg.core.log import register_secret
from svbg.core.settings import values
from svbg.core.settings.bootstrap import BootstrapError, environ_value, read_bootstrap
from svbg.core.settings.registry import Apply, Registry, SettingDef
from svbg.core.settings.snapshot import SettingsSnapshot, effective_source
from svbg.core.settings.store import AuditEntry, AuditWrite, DatabaseLike, RowWrite, SettingsStore, StoredRow

__all__ = [
    "DB_UNAVAILABLE",
    "EXPLICIT_SOURCES",
    "RESET",
    "ApplyResult",
    "ApplySource",
    "Change",
    "SettingsError",
    "SettingsService",
    "StaleSnapshotError",
]

log = logging.getLogger("svbg.core.settings")

ApplySource = Literal["bot", "env_file", "cli", "import", "wizard", "system"]
Subscriber = Callable[[SettingsSnapshot, set[str]], Awaitable[None] | None]

_DB_ERRORS: Final = (SQLAlchemyError, OSError)
_META_REGISTRY: Final = "registry_schema_version"
# Row sources that mean "the owner set this explicitly" (used by the .env merge without a base).
EXPLICIT_SOURCES: Final = frozenset({"bot", "cli", "import", "wizard", "system"})

# Owner-facing messages (Russian).
_M: Final = {
    "unknown": "неизвестная настройка",
    "locked": "задано окружением контейнера (LOCKED_KEYS) — меняется только там",
    "readonly_secret_key": "ключ шифрования меняется только командой ротации ключа, не правкой",
    "readonly": "эта настройка задаётся только в docker-compose/окружении",
    "file_only": "эта настройка меняется только в файле .env",
    "db": "не сохранено: база данных недоступна, прежнее значение продолжает работать",
    "probe_timeout": "{component}: проверка не завершилась за {seconds:g} с",
    "probe_failed": "{component}: проверка не прошла ({error})",
    "reconfigure": (
        "не применено: {component} не принял новые настройки ({error}). Возвращено прежнее значение"
    ),
    "undecryptable": "секрет не расшифровывается — вероятно, сменился SECRET_KEY; введите значение заново",
    "stored_invalid": "сохранённое значение больше не подходит ({error}); используется значение по умолчанию",
    "environ_invalid": "значение в окружении не подходит ({error}) и не используется",
    "not_found": "Изменение не найдено (возможно, история уже очищена)",
    "nothing_to_undo": "В этом изменении нечего отменять",
    "rolled_back": "откат: {error}",
    "conflict": "conflict_discarded",
}


#: Rejection reason when the database could not persist a change (transient, the caller may retry).
DB_UNAVAILABLE: Final = _M["db"]


class SettingsError(Exception):
    """An owner-facing error of a settings operation (Russian message)."""


class StaleSnapshotError(SettingsError):
    """``expected_version`` did not match: somebody changed settings meanwhile — recompute and retry."""


class _Reset:
    __slots__ = ()

    def __repr__(self) -> str:
        return "RESET"


#: ``Change(key, RESET)`` returns a key to its registry default (deletes the stored row).
RESET: Final = _Reset()


@dataclass
class Change:
    key: str
    raw: str | Any


@dataclass
class ApplyResult:
    batch_id: str
    applied: dict[str, Any]  # key → new value (secrets redacted)
    rejected: dict[str, str]  # key → owner-facing reason
    restart_required: bool
    unchanged: list[str] = field(default_factory=list)
    reloaded: list[str] = field(default_factory=list)  # components reconfigured

    @property
    def ok(self) -> bool:
        return not self.rejected


@dataclass(slots=True)
class _Op:
    defn: SettingDef
    value: Any
    reset: bool
    row_source: str


@dataclass(frozen=True, slots=True)
class _RowState:
    value: Any
    source: str


class SettingsService:
    def __init__(
        self,
        db: DatabaseLike,
        registry: Registry,
        crypto: Crypto,
        components: ComponentRegistry,
        *,
        environ: Mapping[str, str],
        env_path: Path,
        audit_actor_default: int | None = None,
        probe_timeout: float = 15.0,
        reconfigure_timeout: float = 30.0,
    ) -> None:
        self.registry = registry
        self.crypto = crypto
        self.components = components
        self.store = SettingsStore(db)
        self.environ: Mapping[str, str] = dict(environ)
        self.env_path = Path(env_path)
        self._actor_default = audit_actor_default
        self._probe_timeout = probe_timeout
        self._reconfigure_timeout = reconfigure_timeout
        self._snap: SettingsSnapshot | None = None
        self._rows: dict[str, _RowState] = {}
        self._subs: list[tuple[frozenset[str] | None, Subscriber]] = []
        self._lock = asyncio.Lock()
        self.locked: frozenset[str] = frozenset()
        self.restart_pending: set[str] = set()
        self.undecryptable: set[str] = set()
        self.problems: dict[str, str] = {}
        self._db_failures = 0  # consecutive failed writes: only the first one is logged with a traceback

    # ------------------------------------------------------------------------------------------ reading

    def current(self) -> SettingsSnapshot:
        snap = self._snap
        if snap is None:
            raise RuntimeError("settings are not loaded yet (call SettingsService.load())")
        return snap

    def row_source(self, key: str) -> str | None:
        """Source of the stored row for ``key`` (None = no row, i.e. default/file/environ value)."""
        row = self._rows.get(key)
        return None if row is None else row.source

    def subscribe(self, keys: Iterable[str] | Literal["*"], cb: Subscriber) -> None:
        """Call ``cb(snapshot, changed_keys)`` after a change of any of ``keys`` (``"*"`` = all)."""
        if keys == "*":
            self._subs.append((None, cb))
            return
        canonical = frozenset(self.registry.get(k).key for k in keys)
        self._subs.append((canonical, cb))

    def env_overrides(self) -> dict[str, str]:
        """Keys whose value in the container environment differs from the effective one (UI warning:
        «в окружении другое значение — оно игнорируется»). Values are not returned."""
        snap = self.current()
        out: dict[str, str] = {}
        for defn in self.registry.all():
            raw = environ_value(self.environ, defn)
            if raw is None or defn.key in self.locked or values.is_unchanged_marker(defn, raw):
                continue
            if not values.same_value(defn, raw, values.to_text(defn, snap[defn.key])):
                out[defn.key] = snap.source(defn.key)
        return out

    async def history(self, key: str | None = None, *, limit: int = 20) -> list[AuditEntry]:
        canonical = None if key is None else self.registry.get(key).key
        return await self.store.history(canonical, limit=limit)

    def search(self, query: str, *, limit: int = 8) -> list[SettingDef]:
        """Fuzzy-ish search by key, Russian title/description and tags."""
        q = query.strip().casefold()
        if not q:
            return []
        tokens = [t for t in q.replace(",", " ").split() if t]
        scored: list[tuple[float, int, SettingDef]] = []
        for index, defn in enumerate(self.registry.all()):
            score = _score(defn, q, tokens)
            if score > 0:
                scored.append((score, index, defn))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [defn for _, _, defn in scored[:limit]]

    # ------------------------------------------------------------------------------------------ load

    async def load(self) -> SettingsSnapshot:
        """Build the first snapshot.

        * bootstrap keys: LOCKED → ``.env`` → stored row (a change made in the bot) → environ → default;
        * runtime keys: LOCKED → stored row → environ (seeded into the database) → default.

        A stored row that cannot be read (wrong ``SECRET_KEY``, no longer valid) is never overwritten here:
        an environment value is then used in memory only, until the owner sets the key explicitly.
        """
        async with self._lock:
            try:
                boot = await asyncio.to_thread(
                    read_bootstrap, self.env_path, self.environ, registry=self.registry
                )
                boot_values, boot_sources, locked = boot.values, boot.sources, boot.locked
                self.problems.update(boot.problems)
            except BootstrapError as exc:
                boot_error: str | None = str(exc)
                boot_values, boot_sources, locked = {}, {}, frozenset()
            else:
                boot_error = None
            if boot_error is not None:
                log.error("settings: %s", boot_error)
            self.locked = locked
            stored = await self.store.load()
            vals: dict[str, Any] = {}
            sources: dict[str, str] = {}
            seeds: list[_Op] = []
            for defn in self.registry.all():
                key = defn.key
                row = None if defn.file_only else stored.get(key)
                resolved = self._resolve_locked(defn) if key in locked else None
                if resolved is None and defn.bootstrap:
                    resolved = self._resolve_bootstrap(defn, row, boot_values, boot_sources)
                elif resolved is None:
                    if row is not None:
                        resolved = self._decode_row(defn, row)
                    if resolved is None:
                        resolved = self._seed_from_environ(defn, seeds, persist=row is None)
                vals[key], sources[key] = resolved if resolved is not None else (defn.default, "default")
            if seeds:
                await self._persist_seeds(seeds)
            await self._record_registry_version()
            self._snap = SettingsSnapshot(
                vals,
                sources,
                version=1,
                aliases={a: d.key for d in self.registry.all() for a in d.aliases},
                secrets=frozenset(d.key for d in self.registry.all() if d.is_secret),
            )
            self._register_secrets(self._snap, self.registry.keys())
            log.info(
                "settings loaded: %d keys, %d stored, %d seeded from environment, %d problems",
                len(vals),
                len(self._rows),
                len(seeds),
                len(self.problems),
            )
            return self._snap

    def _resolve_locked(self, defn: SettingDef) -> tuple[Any, str] | None:
        raw = environ_value(self.environ, defn)
        if raw is None:
            return None
        try:
            return values.parse_or_default(defn, raw), "locked"
        except values.SettingValueError as exc:
            self.problems[defn.key] = _M["environ_invalid"].format(error=exc)
            return None

    def _resolve_bootstrap(
        self,
        defn: SettingDef,
        row: StoredRow | None,
        boot_values: Mapping[str, Any],
        boot_sources: Mapping[str, str],
    ) -> tuple[Any, str] | None:
        key = defn.key
        if key in boot_values and boot_sources.get(key) in ("env_file", "locked"):
            # The file is the truth for bootstrap keys (03 §3.2): an edit made while the bot was down wins
            # over the stored row, before any component (the bot runner) starts with a stale value.
            value = boot_values[key]
            if row is not None:
                self._adopt_matching_row(defn, row, value)
            return value, boot_sources[key]
        if row is not None:
            resolved = self._decode_row(defn, row)
            if resolved is not None:
                return resolved
        if key in boot_values:
            return boot_values[key], boot_sources.get(key, "default")
        return None

    def _adopt_matching_row(self, defn: SettingDef, row: StoredRow, value: Any) -> None:
        """Track the stored row of a bootstrap key only if it holds the value the file has; a stale row is
        ignored (and replaced by the next explicit change)."""
        try:
            stored_value = self._decode_value(defn, row.value)
        except (CryptoError, values.SettingValueError):
            return
        if stored_value == value:
            self._rows[defn.key] = _RowState(stored_value, row.source)
        else:
            log.info("settings: %s in .env differs from the stored value; the file wins", defn.key)

    def _decode_row(self, defn: SettingDef, row: StoredRow) -> tuple[Any, str] | None:
        try:
            value = self._decode_value(defn, row.value)
        except CryptoError:
            undecryptable = True
        except values.SettingValueError as exc:
            self.problems[defn.key] = _M["stored_invalid"].format(error=exc)
            log.warning("settings: stored value of %s is no longer valid: %s", defn.key, exc)
            return None
        else:
            undecryptable = False
        if undecryptable:
            self.undecryptable.add(defn.key)
            self.problems[defn.key] = _M["undecryptable"]
            log.error("settings: stored secret %s cannot be decrypted with the current SECRET_KEY", defn.key)
            return None
        self._rows[defn.key] = _RowState(value, row.source)
        return value, effective_source(row.source)

    def _seed_from_environ(
        self, defn: SettingDef, seeds: list[_Op], *, persist: bool = True
    ) -> tuple[Any, str] | None:
        """Value from ``os.environ``; seeded into the database only when no row exists (``persist``)."""
        raw = environ_value(self.environ, defn)
        if raw is None or values.is_unchanged_marker(defn, raw):
            return None
        try:
            value = values.parse_or_default(defn, raw)
        except values.SettingValueError as exc:
            self.problems[defn.key] = _M["environ_invalid"].format(error=exc)
            return None
        if value == defn.default:
            return None  # seeding the default would freeze it
        if persist:
            seeds.append(_Op(defn, value, reset=False, row_source="env_seed"))
        return value, "environ"

    async def _persist_seeds(self, seeds: list[_Op]) -> None:
        rows = [RowWrite(op.defn.key, self._encode_value(op.defn, op.value), "env_seed") for op in seeds]
        audit = [
            AuditWrite(op.defn.key, None, self._envelope(op.defn, op.value, "env_seed"), True) for op in seeds
        ]
        try:
            await self.store.write(batch_id=uuid7(), source="env_seed", actor_id=None, rows=rows, audit=audit)
        except _DB_ERRORS:
            log.exception("settings: cannot persist values seeded from the environment")
            return
        for op in seeds:
            self._rows[op.defn.key] = _RowState(op.value, "env_seed")

    async def _record_registry_version(self) -> None:
        meta = {"fingerprint": self.registry.fingerprint, "keys": len(self.registry)}
        try:
            if await self.store.meta_get(_META_REGISTRY) != meta:
                await self.store.meta_set(_META_REGISTRY, meta)
        except _DB_ERRORS:
            log.warning("settings: cannot record the registry version", exc_info=True)

    # ------------------------------------------------------------------------------------------ apply

    async def apply(
        self,
        changes: Sequence[Change],
        *,
        source: ApplySource,
        actor_id: int | None,
        expected_version: int | None = None,
    ) -> ApplyResult:
        """Validate, probe, persist, swap, reconfigure. See the module docstring."""
        actor = actor_id if actor_id is not None else self._actor_default
        async with self._lock:
            snap = self.current()
            if expected_version is not None and snap.version != expected_version:
                raise StaleSnapshotError("settings changed meanwhile")
            ops, rejected, unchanged, raws = self._resolve(changes, source)
            return await self._run(ops, rejected, unchanged, raws, source=source, actor_id=actor)

    async def reset(self, key: str, *, source: ApplySource, actor_id: int | None) -> ApplyResult:
        """Return ``key`` to its registry default (the stored row is deleted)."""
        return await self.apply([Change(key, RESET)], source=source, actor_id=actor_id)

    async def undo(self, batch_id: str, *, actor_id: int | None) -> ApplyResult:
        """Re-apply the values that were in effect before ``batch_id`` (through the same pipeline)."""
        actor = actor_id if actor_id is not None else self._actor_default
        everything = await self.store.batch(batch_id)
        if not everything:
            raise SettingsError(_M["not_found"])
        entries = [e for e in everything if e.applied]
        if not entries:
            raise SettingsError(_M["nothing_to_undo"])
        async with self._lock:
            ops: dict[str, _Op] = {}
            rejected: dict[str, str] = {}
            for entry in entries:
                defn = self.registry.find(entry.key)
                if defn is None:
                    continue
                if defn.key in self.locked:
                    rejected[defn.key] = _M["locked"]
                    continue
                try:
                    ops[defn.key] = self._op_from_envelope(defn, entry.old)
                except (CryptoError, values.SettingValueError) as exc:
                    rejected[defn.key] = (
                        str(exc) if isinstance(exc, values.SettingValueError) else _M["undecryptable"]
                    )
            ops = {k: op for k, op in ops.items() if not self._is_noop(op)}
            return await self._run(ops, rejected, [], {}, source="undo", actor_id=actor)

    def _op_from_envelope(self, defn: SettingDef, env: dict[str, Any] | None) -> _Op:
        if env is None:
            return _Op(defn, defn.default, reset=True, row_source="default")
        src = str(env.get("src") or "bot")
        if "enc" in env:
            value = values.from_json(defn, self.crypto.decrypt(env["enc"]))
        else:
            value = values.from_json(defn, env.get("v"))
        return _Op(defn, value, reset=False, row_source=src)

    def _resolve(
        self, changes: Sequence[Change], source: str
    ) -> tuple[dict[str, _Op], dict[str, str], list[str], dict[str, Any]]:
        ops: dict[str, _Op] = {}
        rejected: dict[str, str] = {}
        unchanged: list[str] = []
        raws: dict[str, Any] = {}
        for change in changes:
            defn = self.registry.find(change.key)
            if defn is None:
                rejected[change.key] = _M["unknown"]
                continue
            key = defn.key
            ops.pop(key, None)
            rejected.pop(key, None)
            error = self._permission_error(defn, source)
            if error is not None:
                rejected[key] = error
                raws[key] = change.raw
                continue
            if values.is_unchanged_marker(defn, change.raw):
                unchanged.append(key)
                continue
            raw = change.raw
            if defn.is_secret and isinstance(raw, str):
                register_secret(raw.strip())  # before probes/validation can mention it in a log line
            try:
                if raw is RESET or (_is_blank(raw) and not defn.nullable):
                    op = _Op(defn, defn.default, reset=True, row_source="default")
                else:
                    op = _Op(defn, values.coerce(defn, raw), reset=False, row_source=source)
            except values.SettingValueError as exc:
                rejected[key] = str(exc)
                raws[key] = raw
                continue
            if self._is_noop(op):
                unchanged.append(key)
                continue
            ops[key] = op
        return ops, rejected, unchanged, raws

    def _permission_error(self, defn: SettingDef, source: str) -> str | None:
        if defn.key in self.locked:
            return _M["locked"]
        if defn.readonly:
            return _M["readonly_secret_key"] if defn.key == "SECRET_KEY" else _M["readonly"]
        if defn.file_only and source != "env_file":
            return _M["file_only"]
        return None

    def _is_noop(self, op: _Op) -> bool:
        key = op.defn.key
        if op.defn.file_only:
            return self.current()[key] == op.value
        row = self._rows.get(key)
        if op.reset:
            return row is None and self.current().source(key) == "default"
        return row is not None and row.value == op.value and key not in self.undecryptable

    async def _run(
        self,
        ops: dict[str, _Op],
        rejected: dict[str, str],
        unchanged: list[str],
        raws: Mapping[str, Any],
        *,
        source: str,
        actor_id: int | None,
    ) -> ApplyResult:
        batch_id = uuid7()
        old = self.current()
        candidate = self._candidate(old, ops)

        # Cross-key checks on the full candidate snapshot.
        for check in self.registry.checks:
            for key, message in check(candidate, frozenset(ops)).items():
                if key in ops:
                    rejected[key] = message
                    del ops[key]
        candidate = self._candidate(old, ops)

        # Probe every affected RELOAD component with the candidate, before anything is persisted.
        groups = self._groups(ops)
        if groups:
            names = list(groups)
            outcomes = await asyncio.gather(*(self._probe(name, candidate) for name in names))
            failed = {name: error for name, error in zip(names, outcomes, strict=True) if error is not None}
            for name, error in failed.items():
                for key in groups.pop(name):
                    rejected[key] = error
                    ops.pop(key, None)
            if failed:
                candidate = self._candidate(old, ops)

        # Persist: rows + audit (applied and rejected) in one transaction.
        prev_rows = {key: self._rows.get(key) for key in ops}
        rows = [self._row_write(op, actor_id) for op in ops.values() if not op.defn.file_only]
        audit = [
            AuditWrite(key, self._row_envelope(op.defn), self._op_envelope(op), True)
            for key, op in ops.items()
        ]
        audit += [self._rejected_audit(key, message, raws.get(key)) for key, message in rejected.items()]
        audit = [a for a in audit if a is not None]
        if rows or audit:
            try:
                await self.store.write(
                    batch_id=batch_id, source=source, actor_id=actor_id, rows=rows, audit=audit
                )
            except _DB_ERRORS as exc:
                self._db_failures += 1
                if self._db_failures == 1:
                    log.exception("settings: cannot persist batch %s", batch_id)
                else:  # an outage: one traceback is enough
                    log.warning(
                        "settings: cannot persist batch %s, database unavailable (%s, %d in a row)",
                        batch_id,
                        type(exc).__name__,
                        self._db_failures,
                    )
                for key in ops:
                    rejected[key] = _M["db"]
                return ApplyResult(batch_id, {}, rejected, False, unchanged)
            self._db_failures = 0
        for key, op in ops.items():
            if op.defn.file_only:
                continue
            if op.reset:
                self._rows.pop(key, None)
            else:
                self._rows[key] = _RowState(op.value, op.row_source)
            self.undecryptable.discard(key)
            self.problems.pop(key, None)

        # Swap, then reconfigure components; a failure rolls that component's keys back.
        self._snap = candidate
        self._register_secrets(candidate, ops)
        reloaded: list[str] = []
        for component_name, keys in groups.items():
            component = self.components.find(component_name)
            if component is None:
                continue
            error = await self._reconfigure(component, self.current())
            if error is None:
                reloaded.append(component_name)
                continue
            await self._rollback(
                component,
                keys=keys,
                old=old,
                prev_rows=prev_rows,
                batch_id=batch_id,
                error=error,
                actor_id=actor_id,
            )
            for key in keys:
                rejected[key] = _M["reconfigure"].format(component=component_name, error=error)
                ops.pop(key, None)

        restart = [key for key, op in ops.items() if op.defn.apply is Apply.RESTART]
        self.restart_pending.update(restart)
        applied = {key: _public(op.defn, op.value) for key, op in ops.items()}
        if ops or rejected:
            log.info(
                "settings batch %s from %s: applied=%s rejected=%s",
                batch_id,
                source,
                sorted(applied),
                sorted(rejected),
            )
        if ops:
            await self._notify(set(ops))
        return ApplyResult(batch_id, applied, rejected, bool(restart), unchanged, reloaded)

    def _candidate(self, old: SettingsSnapshot, ops: Mapping[str, _Op]) -> SettingsSnapshot:
        if not ops:
            return old
        new_values = {key: op.value for key, op in ops.items()}
        new_sources = {
            key: ("default" if op.reset else effective_source(op.row_source)) for key, op in ops.items()
        }
        return old.with_changes(new_values, new_sources)

    def _groups(self, ops: Mapping[str, _Op]) -> dict[str, list[str]]:
        groups: dict[str, list[str]] = {}
        for key, op in ops.items():
            name = op.defn.component
            if op.defn.apply is Apply.RELOAD and name and name in self.components:
                groups.setdefault(name, []).append(key)
        return groups

    async def _probe(self, name: str, candidate: SettingsSnapshot) -> str | None:
        component = self.components.find(name)
        if component is None:
            return None
        try:
            async with asyncio.timeout(self._probe_timeout):
                await component.probe(candidate)
        except ProbeError as exc:
            return str(exc)
        except TimeoutError:
            return _M["probe_timeout"].format(component=name, seconds=self._probe_timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("settings: probe of %s failed unexpectedly", name)
            return _M["probe_failed"].format(component=name, error=type(exc).__name__)
        return None

    async def _reconfigure(self, component: Component, cfg: SettingsSnapshot) -> str | None:
        try:
            async with asyncio.timeout(self._reconfigure_timeout):
                await component.reconfigure(cfg)
        except ProbeError as exc:
            return str(exc)
        except TimeoutError:
            return f"нет ответа за {self._reconfigure_timeout:g} с"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("settings: reconfigure of %s failed", component.name)
            return type(exc).__name__
        return None

    async def _rollback(
        self,
        component: Component,
        *,
        keys: list[str],
        old: SettingsSnapshot,
        prev_rows: Mapping[str, _RowState | None],
        batch_id: str,
        error: str,
        actor_id: int | None,
    ) -> None:
        log.error("settings: %s rejected batch %s, rolling back %s", component.name, batch_id, sorted(keys))
        self._snap = self.current().with_changes({k: old[k] for k in keys}, {k: old.source(k) for k in keys})
        rows: list[RowWrite] = []
        audit: list[AuditWrite] = []
        for key in keys:
            defn = self.registry.get(key)
            prev = prev_rows.get(key)
            current_env = self._row_envelope(defn)
            if defn.file_only:
                pass
            elif prev is None:
                rows.append(RowWrite(key, delete=True))
                self._rows.pop(key, None)
            else:
                rows.append(RowWrite(key, self._encode_value(defn, prev.value), prev.source, actor_id))
                self._rows[key] = prev
            restored = None if prev is None else self._envelope(defn, prev.value, prev.source)
            audit.append(AuditWrite(key, current_env, restored, True, _M["rolled_back"].format(error=error)))
        try:
            await self.store.write(
                batch_id=uuid7(),
                source="rollback",
                actor_id=actor_id,
                rows=rows,
                audit=audit,
                mark_failed=[(batch_id, key, _M["rolled_back"].format(error=error)) for key in keys],
            )
        except _DB_ERRORS:
            log.critical("settings: rollback of batch %s could not be persisted", batch_id, exc_info=True)
            for key in keys:
                self.problems[key] = _M["db"]
        # Best effort: make sure the component runs with the restored configuration.
        if await self._reconfigure(component, self.current()) is not None:
            log.error("settings: %s did not accept the restored configuration either", component.name)

    async def _notify(self, changed: set[str]) -> None:
        snap = self.current()
        calls: list[Awaitable[None]] = []
        for keys, cb in list(self._subs):
            hit = changed if keys is None else changed & keys
            if hit:
                calls.append(_call_subscriber(cb, snap, set(hit)))
        if calls:
            await asyncio.gather(*calls)

    async def record_conflict(self, key: str, file_raw: str, *, actor_id: int | None = None) -> None:
        """Audit a value from ``.env`` discarded because the bot changed the same key (03 §3.3)."""
        defn = self.registry.get(key)
        entry = self._rejected_audit(defn.key, _M["conflict"], file_raw)
        if entry is None:
            return
        try:
            await self.store.write(batch_id=uuid7(), source="env_file", actor_id=actor_id, audit=[entry])
        except _DB_ERRORS:
            log.warning("settings: cannot audit the .env conflict on %s", defn.key, exc_info=True)

    # ------------------------------------------------------------------------------------------ encoding

    def _encode_value(self, defn: SettingDef, value: Any) -> Any:
        data = values.to_json(defn, value)
        if defn.is_secret and data is not None:
            return self.crypto.encrypt(str(data))
        return data

    def _decode_value(self, defn: SettingDef, data: Any) -> Any:
        if defn.is_secret and isinstance(data, str):
            data = self.crypto.decrypt(data)
        return values.from_json(defn, data)

    def _envelope(self, defn: SettingDef, value: Any, src: str) -> dict[str, Any]:
        if defn.is_secret and value not in (None, ""):
            text = str(value)
            return {"fp": self.crypto.value_fingerprint(text), "enc": self.crypto.encrypt(text), "src": src}
        return {"v": values.to_json(defn, value), "src": src}

    def _row_envelope(self, defn: SettingDef) -> dict[str, Any] | None:
        """Audit form of the current state: the stored row, or the effective value of a file-only key."""
        if defn.file_only:
            snap = self.current()
            return self._envelope(defn, snap[defn.key], snap.source(defn.key))
        row = self._rows.get(defn.key)
        return None if row is None else self._envelope(defn, row.value, row.source)

    def _op_envelope(self, op: _Op) -> dict[str, Any] | None:
        if op.reset and not op.defn.file_only:
            return None
        return self._envelope(op.defn, op.value, op.row_source)

    def _row_write(self, op: _Op, actor_id: int | None) -> RowWrite:
        if op.reset:
            return RowWrite(op.defn.key, delete=True)
        return RowWrite(op.defn.key, self._encode_value(op.defn, op.value), op.row_source, actor_id)

    def _rejected_audit(self, key: str, message: str, raw: Any) -> AuditWrite | None:
        defn = self.registry.find(key)
        if defn is None:
            return None  # unknown keys are not audited (arbitrary input)
        new: dict[str, Any] | None
        if raw is None or raw is RESET:
            new = None
        elif defn.is_secret:
            new = {"fp": self.crypto.value_fingerprint(str(raw))}
        else:
            new = {"raw": str(raw)[:500]}
        return AuditWrite(defn.key, self._row_envelope(defn), new, False, message)

    def _register_secrets(self, snap: SettingsSnapshot, keys: Iterable[str]) -> None:
        for key in keys:
            if snap.is_secret(key):
                value = snap[key]
                if isinstance(value, str):
                    register_secret(value)


# ---------------------------------------------------------------------------------------------- helpers


def _is_blank(raw: Any) -> bool:
    return raw is None or (isinstance(raw, str) and raw.strip() == "")


def _public(defn: SettingDef, value: Any) -> Any:
    return values.display(defn, value) if defn.is_secret else value


async def _call_subscriber(cb: Subscriber, snap: SettingsSnapshot, keys: set[str]) -> None:
    try:
        res = cb(snap, keys)
        if res is not None:
            await res
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("settings subscriber %s failed", getattr(cb, "__qualname__", cb))


def _score(defn: SettingDef, q: str, tokens: list[str]) -> float:
    key = defn.key.casefold()
    title = defn.title.casefold()
    desc = defn.description.casefold()
    tags = [t.casefold() for t in defn.tags]
    names = [n.casefold() for n in defn.names]
    if q in names:
        return 100.0
    score = 0.0
    if any(q in n for n in names):
        score += 60
    if q in title:
        score += 50
    if any(q in t or t in q for t in tags):
        score += 45
    if q in desc:
        score += 20
    for token in tokens:
        if len(token) < 2:
            continue
        if token in title or any(token in t for t in tags):
            score += 10
        elif token in desc or token in key:
            score += 5
    if score == 0:
        candidates = [key, title, *tags, *title.split()]
        best = max(difflib.SequenceMatcher(None, q, c).ratio() for c in candidates)
        if best >= 0.75:
            score = 30 * best
    return score
