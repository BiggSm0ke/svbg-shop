"""Live ``.env`` mirror: render on change, watch for manual edits, 3-way merge (03 §3.3–3.5, 07 §3.2–3.5).

* **Render.** The full file is generated from the registry and the current snapshot (``ENV_LAYOUT``,
  ``ENV_SECRETS``) with :func:`svbg.boot.envfile.render_full`, keeping the owner's own lines, and written
  atomically only when the text differs. The written text is the merge **base** (``config_meta.env_base``),
  stored **encrypted** with ``SECRET_KEY`` (the file holds bootstrap secrets, a database dump must not).
  A write is skipped (and the round repeated) if the file changed after it was read, so a manual edit saved
  during a slow apply is never overwritten.
* **Watch.** ``os.stat`` polling (works on bind mounts, Docker Desktop, NFS — no inotify). A change whose
  hash equals the base is our own write and is ignored.
* **Merge** per key — base (last written), ours (snapshot), theirs (file): changed only in the file →
  ``service.apply(source="env_file")``; changed in both differently → the database wins, the conflict is
  audited and reported; invalid value → not applied, the line is restored with a ``# ⚠`` note above it.
  A line removed from the file is restored; ``KEY=`` resets a key; unknown keys are kept. While the
  database is unavailable the edit stays in the file and the merge is retried with exponential backoff.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import zoneinfo
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

from svbg.boot.envfile import (
    EnvDocument,
    EnvFileError,
    EnvLine,
    RenderKey,
    quote,
    read_text,
    render_full,
    write_atomic,
)
from svbg.core.clock import now
from svbg.core.component import fix_setting
from svbg.core.crypto import CryptoError
from svbg.core.settings import values
from svbg.core.settings.bootstrap import file_values
from svbg.core.settings.envtext import COMPACT_NOTE, HEADER, comment_lines
from svbg.core.settings.registry import RETIRED_KEYS, Apply, Registry, SettingDef
from svbg.core.settings.service import DB_UNAVAILABLE, EXPLICIT_SOURCES, Change, StaleSnapshotError

if TYPE_CHECKING:
    from svbg.core.settings.service import ApplyResult, SettingsService
    from svbg.core.settings.snapshot import SettingsSnapshot

__all__ = ["EnvMirror", "MirrorNotice", "MirrorStatus", "NoticeKind"]

log = logging.getLogger("svbg.core.settings.mirror")

NoticeKind = Literal[
    "applied", "invalid", "conflict", "restart", "created", "restored", "unwritable", "unreadable"
]
NoticeHook = Callable[["MirrorNotice"], Awaitable[None] | None]

META_BASE: Final = "env_base"
_BASE_FORMAT: Final = 2  # {"v": 2, "enc": "enc:v1:..."}; v1 kept the text in plain JSON
_MERGE_ATTEMPTS: Final = 3
_SYNC_ATTEMPTS: Final = 3  # rounds when the file keeps changing under us
_BACKOFF_MIN: Final = 1.0  # first delay of a merge retried because the database is unavailable

# Owner-facing texts (Russian).
_T: Final = {
    "applied": "✏️ .env: {key} {old} → {new} ({how})",
    "applied_secret": "✏️ .env: {key} изменён (секрет, {how})",
    "how_hot": "применено",
    "how_reload": "применено, переподключено",
    "how_restart": "сохранено, нужен перезапуск",
    "invalid": "⚠️ .env: {key} = «{raw}» не применено: {reason}. Оставлено прежнее: {old}",
    "invalid_secret": "⚠️ .env: новое значение {key} не применено: {reason}. Оставлено прежнее.",
    "note_invalid": "{ts}: значение «{raw}» отклонено: {reason}. Применено прежнее: {old}",
    "note_invalid_secret": "{ts}: новое значение отклонено: {reason}. Оставлено прежнее (секрет)",
    "note_broken": "{ts}: строка не разобрана (проверьте кавычки). Применено прежнее: {old}",
    "broken": "⚠️ .env: строка {key} не разобрана (проверьте кавычки) — оставлено прежнее значение",
    "conflict": "⚠️ .env: {key} изменили и в боте, и в файле. Оставлено значение из бота: {ours}; "
    "из файла ({theirs}) не применено.",
    "note_conflict": "{ts}: значение «{theirs}» из файла не применено — одновременно изменено в боте",
    "restart": "♻️ .env: {keys} — сохранено, вступит в силу после перезапуска",
    "created": "📄 Создан файл настроек {path}",
    "restored": "📄 Файл {path} был удалён — восстановлен из БД",
    "unwritable": "❌ Файл {path} недоступен для записи ({error}). Бот работает, настройки хранятся в БД. "
    "Исправьте права: sudo chown 1000:1000 {path}",
    "unreadable": "❌ Файл {path} не читается ({error}). Правки из файла не применяются, "
    "файл не перезаписывается.",
    "secret_hidden": "секрет",
}


@dataclass(frozen=True)
class MirrorNotice:
    """Something the owner should know about the file (sent to the admin chat by the caller)."""

    kind: NoticeKind
    message: str  # Russian, secrets masked
    keys: tuple[str, ...] = ()
    fix_action: str | None = None


@dataclass
class MirrorStatus:
    path: Path
    writable: bool = True
    readable: bool = True
    error: str | None = None
    last_write_at: datetime | None = None
    last_external_at: datetime | None = None
    last_external_keys: list[str] = field(default_factory=list)
    invalid: dict[str, str] = field(default_factory=dict)  # key → reason of the last rejected file edit
    conflicts: list[str] = field(default_factory=list)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _stat_signature(path: Path) -> tuple[int, int, int, int] | None:
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino, st.st_ctime_ns)


class EnvMirror:
    def __init__(
        self,
        service: SettingsService,
        registry: Registry,
        env_path: Path,
        *,
        poll_interval: float = 2.0,
        debounce: float = 0.3,
        settle: float = 0.5,
        retry_interval: float = 30.0,
        on_notice: NoticeHook | None = None,
    ) -> None:
        self.service = service
        self.registry = registry
        self.path = Path(env_path)
        self._poll = poll_interval
        self._debounce = debounce
        self._settle = settle
        self._retry = retry_interval
        self._on_notice = on_notice
        self._lock = asyncio.Lock()
        self._base_text: str | None = None
        self._base_hash: str | None = None
        self._sig: tuple[int, int, int, int] | None = None
        self._write_requested = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._started = False
        self._retry_later = False  # the last round could not persist an edit (database unavailable)
        self._backoff = 0.0
        self._reported: set[str] = set()  # one-shot notices (unwritable/unreadable) until resolved
        self.status = MirrorStatus(self.path)
        service.subscribe("*", self._on_settings_changed)

    # ------------------------------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        """Load the merge base, merge edits made while the bot was down, then watch and write."""
        if self._started:
            return
        self._started = True
        await self._load_base()
        try:
            await self.sync()
        except Exception:
            # The bot must start even if the file cannot be synchronized; the loops keep retrying.
            log.exception("settings mirror: initial synchronization failed")
            self.request_write()
        self._tasks = [
            asyncio.create_task(self._watch_loop(), name="settings-env-watch"),
            asyncio.create_task(self._write_loop(), name="settings-env-write"),
        ]

    async def stop(self) -> None:
        """Stop watching; a pending write is flushed."""
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._write_requested.is_set():
            self._write_requested.clear()
            try:
                await self.sync()
            except Exception:
                log.exception("settings mirror: final write failed")
        self._started = False

    def request_write(self) -> None:
        """Schedule a render (debounced): many changes in a row produce one write."""
        self._write_requested.set()

    async def render_now(self) -> bool:
        """Merge pending file edits and write the file now; True if the file is in sync afterwards."""
        await self.sync()
        return self.status.writable and self.status.readable

    async def sync(self) -> None:
        """One synchronization round: read → (merge) → render → write if different."""
        async with self._lock:
            notices = await self._sync_locked()
        await self._emit(notices)

    async def _on_settings_changed(self, _snap: SettingsSnapshot, _keys: set[str]) -> None:
        self.request_write()

    # ------------------------------------------------------------------------------------------ loops

    async def _write_loop(self) -> None:
        while True:
            await self._write_requested.wait()
            await asyncio.sleep(self._debounce)
            self._write_requested.clear()
            self._retry_later = False
            try:
                await self.sync()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("settings mirror: write failed")
            if not (self.status.writable and self.status.readable):
                await asyncio.sleep(self._retry)  # e.g. permissions being fixed on the host
                self.request_write()
            elif self._retry_later:
                # Database unavailable (or settings kept changing): back off exponentially, 1 s … retry.
                self._backoff = min(max(self._backoff * 2, _BACKOFF_MIN), max(self._retry, _BACKOFF_MIN))
                await asyncio.sleep(self._backoff)
                self.request_write()
            else:
                self._backoff = 0.0

    async def _watch_loop(self) -> None:
        while True:
            await asyncio.sleep(self._poll)
            try:
                sig = await asyncio.to_thread(_stat_signature, self.path)
                if sig == self._sig:
                    continue
                if sig is not None and self._settle > 0:
                    # Editors save in several steps: wait until the file stops changing.
                    await asyncio.sleep(self._settle)
                    if await asyncio.to_thread(_stat_signature, self.path) != sig:
                        continue
                await self.sync()
            except asyncio.CancelledError:
                raise
            except OSError as exc:
                log.warning("settings mirror: cannot stat %s: %s", self.path, exc.strerror)
            except Exception:
                log.exception("settings mirror: watch iteration failed")

    # ------------------------------------------------------------------------------------------ sync

    async def _load_base(self) -> None:
        try:
            stored = await self.service.store.meta_get(META_BASE)
        except (OSError, ValueError) as exc:
            log.warning("settings mirror: cannot load the merge base: %s", exc)
            return
        except Exception:
            log.exception("settings mirror: cannot load the merge base")
            return
        if not isinstance(stored, Mapping):
            return
        crypto = self.service.crypto
        enc = stored.get("enc")
        legacy = stored.get("text")
        text: str | None = None
        if isinstance(enc, str) and crypto.is_encrypted(enc):
            try:
                text = crypto.decrypt(enc)
            except CryptoError:
                # SECRET_KEY changed: merge without a base (explicit bot values win, see _plan).
                log.warning("settings mirror: the stored merge base cannot be decrypted, ignoring it")
        elif isinstance(legacy, str) and stored.get("sha256") == _sha(legacy):
            text = legacy
        if text is not None:
            self._base_text, self._base_hash = text, _sha(text)
        if legacy is not None:
            # Older versions kept the whole file (bootstrap secrets included) in plain text: re-store it
            # encrypted, or wipe it, right away.
            await self._store_base()

    async def _store_base(self) -> None:
        text = self._base_text
        value: dict[str, object] = {"v": _BASE_FORMAT}
        if text is not None:
            value["enc"] = self.service.crypto.encrypt(text)
        try:
            await self.service.store.meta_set(META_BASE, value)
        except Exception:
            # The in-memory base keeps working; the next start merges against the older base safely.
            log.warning("settings mirror: cannot store the merge base", exc_info=True)

    async def _sync_locked(self) -> list[MirrorNotice]:
        notices: list[MirrorNotice] = []
        for _ in range(_SYNC_ATTEMPTS):
            try:
                await self._sync_round(notices)
            except _FileChangedError:
                # The owner saved the file while we were merging: merge the newer text, never overwrite it.
                log.info("settings mirror: %s changed during synchronization, merging again", self.path)
                continue
            return notices
        self._defer()
        return notices

    def _defer(self) -> None:
        """Try again later (with backoff) instead of spinning."""
        self._retry_later = True
        self.request_write()

    async def _sync_round(self, notices: list[MirrorNotice]) -> None:
        try:
            sig = await asyncio.to_thread(_stat_signature, self.path)
            text = await asyncio.to_thread(read_text, self.path)
        except (EnvFileError, OSError) as exc:
            self.status.readable = False
            self.status.error = _error_text(exc)
            self._notice_once(
                notices, "unreadable", _T["unreadable"].format(path=self.path, error=self.status.error)
            )
            return
        self.status.readable = True
        self._reported.discard("unreadable")
        if text is None:
            kind: NoticeKind = "created" if self._base_text is None else "restored"
            if await self._write(self._render(None, rejected=set()), expected=None):
                notices.append(MirrorNotice(kind, _T[kind].format(path=self.path)))
            return
        self._sig = sig
        rejected: set[str] = set()
        text_hash = _sha(text)
        if text_hash != self._base_hash:
            try:
                doc, rejected = await self._merge(text, notices)
            except _RetryLaterError:
                self._defer()  # the database is unavailable: keep the file as is, try again later
                return
        else:
            doc = EnvDocument.parse(text)
        rendered = self._render(doc, rejected=rejected)
        if rendered != text:
            if text_hash != self._base_hash:
                # The file's edits are merged: the text we merged is the new base (refused keys keep their
                # old base value, so they stay "edited"), so an edit that lands before the write
                # (``_FileChangedError``) is merged against it, not against the older base.
                await self._adopt_base(self._merged_base(text, rejected))
            await self._write(rendered, notices, expected=text_hash)
        elif self._base_hash != text_hash:
            await self._adopt_base(text)

    def _merged_base(self, text: str, rejected: set[str]) -> str:
        if not rejected:
            return text
        doc = EnvDocument.parse(text)
        old = EnvDocument.parse(self._base_text) if self._base_text is not None else None
        for key in rejected:
            value = old.get(key) if old is not None else None
            if value is None:
                doc.remove(key)
            else:
                doc.set(key, value)
        return doc.render()

    async def _merge(self, text: str, notices: list[MirrorNotice]) -> tuple[EnvDocument, set[str]]:
        """Apply manual edits. Returns the file document (annotated) and keys whose file value was refused."""
        for _ in range(_MERGE_ATTEMPTS):
            snap = self.service.current()
            theirs = EnvDocument.parse(text)
            plan = self._plan(theirs, snap)
            try:
                result = (
                    await self.service.apply(
                        plan.changes, source="env_file", actor_id=None, expected_version=snap.version
                    )
                    if plan.changes
                    else None
                )
            except StaleSnapshotError:
                continue  # the bot changed something meanwhile: recompute against the new snapshot
            return await self._finish_merge(theirs, snap, plan, result, notices)
        log.warning("settings mirror: settings kept changing during the merge, retrying later")
        self._defer()
        return EnvDocument.parse(text), set()

    def _plan(self, theirs: EnvDocument, snap: SettingsSnapshot) -> _Plan:
        base = EnvDocument.parse(self._base_text) if self._base_text is not None else None
        t_values = file_values(theirs, self.registry)
        b_values = file_values(base, self.registry)
        plan = _Plan()
        for defn in self.registry.all():
            if not defn.in_file:
                continue
            key = defn.key
            t = t_values.get(key)
            if t is None:
                if _has_broken_line(theirs, defn) and not _has_broken_line(base, defn):
                    plan.broken.append(key)
                continue
            if defn.is_secret and t.strip() == values.SECRET_PLACEHOLDER:
                continue
            b = b_values.get(key) if base is not None else None
            if b is not None and t == b:
                continue  # unchanged in the file
            ours = values.to_text(defn, snap[key])
            if key not in self.service.undecryptable and values.same_value(defn, t, ours):
                continue  # already equal (changed on both sides the same way, or a cosmetic difference)
            if base is None:
                ours_changed = self.service.row_source(key) in EXPLICIT_SOURCES
            elif b is None or (defn.is_secret and b.strip() == values.SECRET_PLACEHOLDER):
                ours_changed = False
            else:
                ours_changed = not values.same_value(defn, b, ours)
            if ours_changed and key not in self.service.undecryptable:
                plan.conflicts[key] = t
            else:
                plan.changes.append(Change(key, t))
                plan.raw[key] = t
        return plan

    async def _finish_merge(
        self,
        theirs: EnvDocument,
        snap: SettingsSnapshot,
        plan: _Plan,
        result: ApplyResult | None,
        notices: list[MirrorNotice],
    ) -> tuple[EnvDocument, set[str]]:
        _rename_aliases(theirs, self.registry)
        ts = self._stamp()
        refused: set[str] = set()
        current = self.service.current()
        if result is not None and DB_UNAVAILABLE in result.rejected.values():
            raise _RetryLaterError
        if result is not None:
            for key, reason in result.rejected.items():
                defn = self.registry.get(key)
                old = values.display(defn, current[key])
                raw = plan.raw.get(key, "")
                if defn.is_secret:
                    note = _T["note_invalid_secret"].format(ts=ts, reason=reason)
                    message = _T["invalid_secret"].format(key=key, reason=reason)
                else:
                    note = _T["note_invalid"].format(ts=ts, raw=_short(raw), reason=reason, old=old)
                    message = _T["invalid"].format(key=key, raw=_short(raw), reason=reason, old=old)
                _annotate(theirs, key, note)
                refused.add(key)
                self.status.invalid[key] = reason
                notices.append(MirrorNotice("invalid", message, (key,), fix_setting(key)))
            applied = list(result.applied)
            for key in applied:
                theirs.clear_annotations(key)
                self.status.invalid.pop(key, None)
            for key in result.unchanged:
                theirs.clear_annotations(key)
            if applied:
                self.status.last_external_at = now()
                self.status.last_external_keys = applied
                notices.extend(self._applied_notices(snap, current, applied))
            if result.restart_required:
                restart_keys = [k for k in applied if self.registry.get(k).apply is Apply.RESTART]
                notices.append(
                    MirrorNotice(
                        "restart", _T["restart"].format(keys=", ".join(restart_keys)), tuple(restart_keys)
                    )
                )
        for key, raw in plan.conflicts.items():
            defn = self.registry.get(key)
            await self.service.record_conflict(key, raw)
            theirs_shown = _T["secret_hidden"] if defn.is_secret else _short(raw)
            ours_shown = values.display(defn, current[key])
            _annotate(theirs, key, _T["note_conflict"].format(ts=ts, theirs=theirs_shown))
            refused.add(key)
            if key not in self.status.conflicts:
                self.status.conflicts.append(key)
            message = _T["conflict"].format(key=key, ours=ours_shown, theirs=theirs_shown)
            notices.append(MirrorNotice("conflict", message, (key,), fix_setting(key)))
        for key in plan.broken:
            defn = self.registry.get(key)
            _annotate(theirs, key, _T["note_broken"].format(ts=ts, old=values.display(defn, current[key])))
            refused.add(key)
            notices.append(MirrorNotice("invalid", _T["broken"].format(key=key), (key,), fix_setting(key)))
        return theirs, refused

    def _applied_notices(
        self, old: SettingsSnapshot, new: SettingsSnapshot, keys: list[str]
    ) -> list[MirrorNotice]:
        lines: list[str] = []
        for key in keys:
            defn = self.registry.get(key)
            how = {
                Apply.HOT: _T["how_hot"],
                Apply.RELOAD: _T["how_reload"],
                Apply.RESTART: _T["how_restart"],
            }[defn.apply]
            if defn.is_secret:
                lines.append(_T["applied_secret"].format(key=key, how=how))
            else:
                lines.append(
                    _T["applied"].format(
                        key=key,
                        old=values.display(defn, old[key]),
                        new=values.display(defn, new[key]),
                        how=how,
                    )
                )
        return [MirrorNotice("applied", "\n".join(lines), tuple(keys))]

    # ------------------------------------------------------------------------------------------ render

    def _render(self, doc: EnvDocument | None, *, rejected: set[str]) -> str:
        snap = self.service.current()
        compact = snap.get("ENV_LAYOUT") == "compact"
        omit = snap.get("ENV_SECRETS") == "omit"
        base = EnvDocument.parse(self._base_text) if self._base_text is not None and doc is not None else None
        b_values = file_values(base, self.registry)
        if doc is not None:
            _rename_aliases(doc, self.registry)
        keys: list[RenderKey] = []
        for defn in self.registry.all():
            if not defn.in_file:
                continue
            key = defn.key
            locked = key in self.service.locked
            value = snap[key]
            if compact and snap.source(key) == "default" and not defn.bootstrap and not locked:
                if doc is not None:
                    for name in defn.names:
                        doc.remove(name)
                continue
            omitted = omit and defn.is_secret and not defn.bootstrap and value not in (None, "")
            if omitted:
                text = values.SECRET_PLACEHOLDER
            elif key in self.service.undecryptable and doc is not None and doc.get(key) is not None:
                text = doc.get(key) or ""  # unknown in the DB: keep what the file has
            else:
                text = values.to_text(defn, value)
            if (
                doc is not None
                and key not in rejected
                and key in b_values
                and not values.same_value(defn, b_values[key], text)
            ):
                doc.clear_annotations(key)  # the value changed since the warning was written
            keys.append(
                RenderKey(key, text, comment_lines(defn, locked=locked, omitted=omitted), defn.section)
            )
        header = [*HEADER, COMPACT_NOTE] if compact else list(HEADER)
        return render_full(self.registry.env_sections, keys, header, existing=doc)

    # ------------------------------------------------------------------------------------------ io

    async def _write(
        self, text: str, notices: list[MirrorNotice] | None = None, *, expected: str | None
    ) -> bool:
        """Write ``text`` atomically if the file still has the content we read (sha256 ``expected``;
        ``None`` = the file did not exist). Raises :class:`_FileChangedError` otherwise."""
        try:
            await asyncio.to_thread(_write_file, self.path, text, expected)
        except OSError as exc:
            self.status.writable = False
            self.status.error = _error_text(exc)
            log.error("settings mirror: cannot write %s: %s", self.path, self.status.error)  # noqa: TRY400
            if notices is not None:
                self._notice_once(
                    notices, "unwritable", _T["unwritable"].format(path=self.path, error=self.status.error)
                )
            return False
        self.status.writable = True
        self.status.error = None
        self.status.last_write_at = now()
        self._reported.discard("unwritable")
        self._sig = await asyncio.to_thread(_stat_signature, self.path)
        await self._adopt_base(text)
        return True

    async def _adopt_base(self, text: str) -> None:
        self._base_text, self._base_hash = text, _sha(text)
        await self._store_base()

    def _notice_once(self, notices: list[MirrorNotice], kind: NoticeKind, message: str) -> None:
        if kind in self._reported:
            return
        self._reported.add(kind)
        notices.append(MirrorNotice(kind, message))

    async def _emit(self, notices: list[MirrorNotice]) -> None:
        hook = self._on_notice
        if hook is None:
            return
        for notice in notices:
            try:
                res = hook(notice)
                if res is not None:
                    await res
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("settings mirror: notice hook failed")

    def _stamp(self) -> str:
        moment = now()
        tz_name = self.service.current().get("TIMEZONE")
        try:
            moment = moment.astimezone(zoneinfo.ZoneInfo(str(tz_name)))
        except (zoneinfo.ZoneInfoNotFoundError, ValueError):
            return moment.strftime("%Y-%m-%d %H:%M UTC")
        return moment.strftime("%Y-%m-%d %H:%M")


class _RetryLaterError(Exception):
    """The edit could not be persisted for a transient reason; leave the file untouched."""


class _FileChangedError(Exception):
    """The file changed between reading and writing: our render is stale."""


@dataclass
class _Plan:
    changes: list[Change] = field(default_factory=list)
    raw: dict[str, str] = field(default_factory=dict)
    conflicts: dict[str, str] = field(default_factory=dict)
    broken: list[str] = field(default_factory=list)


def _write_file(path: Path, text: str, expected: str | None) -> None:
    """Compare-and-write in one worker-thread call (the window to the rename is a few milliseconds)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        current = read_text(path)
    except EnvFileError:
        current = ""  # undecodable now: certainly not what we read
    if (None if current is None else _sha(current)) != expected:
        raise _FileChangedError
    write_atomic(path, text)


def _has_broken_line(doc: EnvDocument | None, defn: SettingDef) -> bool:
    if doc is None:
        return False
    names = set(defn.names)
    has_kv = any(line.kind == "kv" and line.key in names for line in doc.lines)
    return not has_kv and any(line.kind == "invalid" and line.key in names for line in doc.lines)


def _rename_aliases(doc: EnvDocument, registry: Registry) -> None:
    """Rewrite lines that use an old key name to the canonical name in place (or drop them if the canonical
    line exists too); lines of :data:`RETIRED_KEYS` (keys the bot no longer has) are dropped."""
    present = {line.key for line in doc.lines if line.kind in ("kv", "invalid")}
    for key in RETIRED_KEYS & present:
        if registry.find(key) is None:
            doc.remove(key)
    present = {line.key for line in doc.lines if line.kind == "kv"}
    for defn in registry.all():
        for alias in defn.aliases:
            if alias not in present:
                continue
            if defn.key in present:
                doc.remove(alias)
                continue
            for index, line in enumerate(doc.lines):
                if line.kind == "kv" and line.key == alias:
                    value = line.value or ""
                    doc.lines[index] = EnvLine("kv", f"{defn.key}={quote(value)}", defn.key, value, line.eol)
            present.add(defn.key)


def _annotate(doc: EnvDocument, key: str, note: str) -> None:
    try:
        doc.annotate_invalid(key, note)
    except KeyError:
        log.debug("settings mirror: no line for %s to annotate", key)


def _short(raw: str, limit: int = 60) -> str:
    text = " ".join(raw.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _error_text(exc: BaseException) -> str:
    if isinstance(exc, EnvFileError):
        return str(exc)
    if isinstance(exc, OSError) and exc.strerror:
        return exc.strerror
    return type(exc).__name__
