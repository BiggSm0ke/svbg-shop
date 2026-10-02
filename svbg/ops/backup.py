"""Backups (04 §10, 07 stage 3a): ``pg_dump`` → tar.gz → password encryption → Telegram + last N locally.

What one backup is — a single file ``data/backups/svbg-<YYYYmmdd>-<HHMMSS>-<reason>.svbg`` (encrypted) or
``….tar.gz`` (no password, local only)::

    gzip(tar(manifest.json, db.dump, content.zip, env.enc?))  [→ encrypted with scrypt + Fernet, see crypt]

* ``db.dump`` — ``pg_dump --format=custom --compress=0`` (gzip compresses the whole archive once) taken from
  an exported snapshot; the same snapshot counts the rows of every table, so ``svbg restore`` can prove the
  restored database is complete (``manifest.json → tables``).
* ``content.zip`` — the content export of the constructor (``content_export``) or, while that is not wired,
  the media files of ``data/media`` (the screens themselves live in the database).
* ``env.enc`` — ``data/.env`` encrypted with the same password (its own salt). The plaintext ``.env`` and
  ``SECRET_KEY`` are never in the archive; without a password ``.env`` is not included at all.

Telegram: only encrypted backups are sent (topic «💾 Бэкапы», or the owners' DMs while no admin group is
connected), in parts of ≤ 45 MB (``<file>.part1of3`` …; ``svbg restore`` joins them). ``pg_dump`` runs as an
asyncio subprocess (cancelling the backup stops it and removes the partial dump); gzip and encryption run in
worker threads; one backup at a time per process.
"""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import hashlib
import io
import json
import logging
import os
import re
import secrets
import shutil
import tarfile
import time
import zipfile
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol

import asyncpg

import svbg
from svbg.core import clock
from svbg.core.log import mask
from svbg.ops.crypt import EncryptingWriter, KdfParams, encrypt_bytes
from svbg.ops.pgtools import PgToolError, PgTools, conninfo, major_version

if TYPE_CHECKING:
    from aiogram import Bot

__all__ = [
    "BACKUP_NAME_RE",
    "FORMAT",
    "FORMAT_VERSION",
    "PART_SIZE",
    "BackupError",
    "BackupResult",
    "ContentExport",
    "TelegramDelivery",
    "create_backup",
    "list_backups",
    "media_zip",
    "rotate",
    "split_parts",
]

log = logging.getLogger("svbg.ops.backup")

FORMAT: Final = "svbg-backup"
FORMAT_VERSION: Final = 1
PART_SIZE: Final = 45 * 1024 * 1024
BACKUP_NAME_RE: Final = re.compile(r"^svbg-(\d{8})-(\d{6})-([a-z][a-z0-9_]{0,23})\.(svbg|tar\.gz)$")
_REASON_RE: Final = re.compile(r"[a-z][a-z0-9_]{0,23}")
_STALE_TMP_S: Final = 86_400.0
_COPY_BUF: Final = 1024 * 1024

#: Writes the constructor's ``content.zip`` to the given path (svbg.content.export_import).
ContentExport = Callable[[Path], Awaitable[None]]


class BackupError(Exception):
    """A backup could not be made. ``str()`` is owner-facing (Russian), secrets masked."""


@dataclass(frozen=True)
class BackupResult:
    path: Path
    size: int
    encrypted: bool
    created_at: datetime
    reason: str
    tables: int
    rows: int
    revision: str | None
    has_env: bool
    content: str  # export | media | none
    duration_s: float
    removed: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def as_state(self) -> dict[str, Any]:
        return {
            "file": self.path.name,
            "size": self.size,
            "encrypted": self.encrypted,
            "at": self.created_at.isoformat(),
            "reason": self.reason,
            "rows": self.rows,
            "tables": self.tables,
            "duration_s": round(self.duration_s, 1),
        }


# ------------------------------------------------------------------------------------------ helpers


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _quote_literal(name: str) -> str:
    return "'" + name.replace("'", "''") + "'"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while piece := f.read(_COPY_BUF):
            h.update(piece)
    return h.hexdigest()


def _chmod(path: Path, mode: int) -> None:
    with contextlib.suppress(OSError, NotImplementedError):
        os.chmod(path, mode)


def list_backups(directory: Path) -> list[Path]:
    """Backups made by this module in ``directory``, newest first."""
    try:
        entries = list(directory.iterdir())
    except OSError:
        return []
    found = [
        (m.group(1), m.group(2), p) for p in entries if (m := BACKUP_NAME_RE.match(p.name)) and p.is_file()
    ]
    found.sort(key=lambda item: (item[0], item[1], item[2].name), reverse=True)
    return [p for _d, _t, p in found]


def rotate(directory: Path, keep: int, *, protect: Path | None = None) -> list[str]:
    """Delete all but the ``keep`` newest backups (never ``protect``); also stale temporary leftovers."""
    removed: list[str] = []
    for path in list_backups(directory)[max(1, keep) :]:
        if protect is not None and path == protect:
            continue
        with contextlib.suppress(OSError):
            path.unlink()
            removed.append(path.name)
    now = time.time()
    with contextlib.suppress(OSError):
        for path in directory.iterdir():
            name = path.name
            if not (name.startswith(".tmp-") or name.endswith(".partial")):
                continue
            with contextlib.suppress(OSError):
                if now - path.stat().st_mtime > _STALE_TMP_S:
                    if path.is_dir():
                        shutil.rmtree(path, ignore_errors=True)
                    else:
                        path.unlink()
    return removed


def media_zip(media_dir: Path, dest: Path) -> int:
    """``content.zip`` with ``media/<relative path>`` of every regular file; returns the number of files."""
    count = 0
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("README.txt", "SvBG Shop: файлы медиа (data/media). Экраны и тексты — в базе данных.\n")
        if media_dir.is_dir():
            for path in sorted(media_dir.rglob("*")):
                if path.is_symlink() or not path.is_file():
                    continue
                zf.write(path, "media/" + path.relative_to(media_dir).as_posix())
                count += 1
    return count


def split_parts(path: Path, part_size: int, workdir: Path) -> list[Path]:
    """``[path]`` when it fits, else ``<name>.partIofN`` files of ≤ ``part_size`` bytes in ``workdir``."""
    size = path.stat().st_size
    if size <= part_size:
        return [path]
    total = -(-size // part_size)
    parts: list[Path] = []
    with path.open("rb") as src:
        for index in range(1, total + 1):
            part = workdir / f"{path.name}.part{index}of{total}"
            left = part_size
            with part.open("wb") as out:
                while left > 0 and (piece := src.read(min(_COPY_BUF, left))):
                    out.write(piece)
                    left -= len(piece)
            parts.append(part)
    return parts


@dataclass
class _Snapshot:
    snapshot: str
    tables: dict[str, int] = field(default_factory=dict)
    revision: str | None = None
    server_major: int | None = None


async def _open_snapshot(conn: asyncpg.Connection) -> _Snapshot:
    snap = _Snapshot(str(await conn.fetchval("select pg_export_snapshot()")))
    snap.server_major = int(await conn.fetchval("select current_setting('server_version_num')::int")) // 10000
    names = [
        r["relname"]
        for r in await conn.fetch(
            "select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace "
            "where n.nspname = 'public' and c.relkind in ('r', 'p') order by c.relname"
        )
    ]
    if names:
        sql = " union all ".join(
            f"select {_quote_literal(n)} as t, count(*)::bigint as c from public.{_quote_ident(n)}"
            for n in names
        )
        snap.tables = {r["t"]: int(r["c"]) for r in await conn.fetch(sql)}
    if "alembic_version" in snap.tables:
        snap.revision = await conn.fetchval("select version_num from public.alembic_version limit 1")
    return snap


def _pack(
    dest: Path,
    manifest: Mapping[str, Any],
    members: Sequence[tuple[str, Path]],
    password: str | None,
    kdf: KdfParams | None,
) -> int:
    partial = dest.with_name(dest.name + ".partial")
    mtime = int(time.time())
    try:
        with partial.open("wb") as raw:
            sink: Any = EncryptingWriter(raw, password, kdf=kdf) if password else raw
            with (
                gzip.GzipFile(fileobj=sink, mode="wb", compresslevel=6, mtime=0) as gz,
                tarfile.open(fileobj=gz, mode="w|", format=tarfile.PAX_FORMAT) as tar,
            ):
                data = json.dumps(manifest, ensure_ascii=False, indent=1).encode("utf-8")
                info = tarfile.TarInfo("manifest.json")
                info.size, info.mode, info.mtime = len(data), 0o600, mtime
                tar.addfile(info, io.BytesIO(data))
                for arcname, path in members:
                    info = tarfile.TarInfo(arcname)
                    info.size, info.mode, info.mtime = path.stat().st_size, 0o600, mtime
                    with path.open("rb") as f:
                        tar.addfile(info, f)
            if password:
                sink.close()
            raw.flush()
            os.fsync(raw.fileno())
        _chmod(partial, 0o600)
        os.replace(partial, dest)
    finally:
        with contextlib.suppress(OSError):
            partial.unlink()
    return dest.stat().st_size


def _env_secret_key(env_text: str) -> str | None:
    from svbg.boot.envfile import EnvDocument

    value = EnvDocument.parse(env_text).get("SECRET_KEY")
    return value.strip() if value and value.strip() else None


# ------------------------------------------------------------------------------------------ backup


async def create_backup(
    dsn: str,
    out_dir: Path,
    *,
    reason: str = "manual",
    password: str | None = None,
    env_path: Path | None = None,
    media_dir: Path | None = None,
    content_export: ContentExport | None = None,
    keep: int | None = None,
    tools: PgTools | None = None,
    kdf: KdfParams | None = None,
    dump_timeout: float = 1800.0,
    secret_key: str | None = None,
    key_fingerprint: str | None = None,
) -> BackupResult:
    """Make one backup file in ``out_dir`` (see the module docstring). Raises :class:`BackupError`."""
    if not _REASON_RE.fullmatch(reason):
        raise ValueError(f"invalid backup reason {reason!r}")
    password = password or None
    started = time.monotonic()
    created = clock.now()
    tools = tools or PgTools.locate()
    warnings: list[str] = []
    try:
        await asyncio.to_thread(out_dir.mkdir, parents=True, exist_ok=True)
    except OSError as exc:
        raise BackupError(
            f"не удалось создать каталог {out_dir}: {exc.strerror or type(exc).__name__}"
        ) from None
    _chmod(out_dir, 0o700)
    tmp = out_dir / f".tmp-{created:%Y%m%d%H%M%S}-{secrets.token_hex(4)}"
    try:
        tmp.mkdir(mode=0o700)
        try:
            url, dsn_env = conninfo(dsn)
            dump_major = major_version(await tools.aversion("pg_dump"))
        except PgToolError as exc:
            raise BackupError(str(exc)) from None
        snap = await _dump(
            dsn, url, dsn_env, tools=tools, out=tmp / "db.dump", dump_major=dump_major, limit_s=dump_timeout
        )

        content = "none"
        content_path = tmp / "content.zip"
        if content_export is not None:
            try:
                async with asyncio.timeout(600):
                    await content_export(content_path)
                content = "export"
            except Exception as exc:
                log.warning("content export failed: %s", type(exc).__name__, exc_info=exc)
                warnings.append(f"экспорт контента не удался ({type(exc).__name__}), сохранены только медиа")
                content_path.unlink(missing_ok=True)
        if content == "none" and media_dir is not None:
            await asyncio.to_thread(media_zip, media_dir, content_path)
            content = "media"

        has_env = False
        key = secret_key
        if env_path is not None:
            env_bytes = await asyncio.to_thread(_read_optional, env_path)
            if env_bytes is not None:
                key = key or _env_secret_key(env_bytes.decode("utf-8", "replace"))
                if password:
                    blob = await asyncio.to_thread(encrypt_bytes, env_bytes, password, kdf=kdf)
                    (tmp / "env.enc").write_bytes(blob)
                    has_env = True
                else:
                    warnings.append("без пароля копия .env в бэкап не попала")

        members = [("db.dump", tmp / "db.dump")]
        if content_path.exists():
            members.append(("content.zip", content_path))
        if has_env:
            members.append(("env.enc", tmp / "env.enc"))
        files: dict[str, Any] = {}
        for arcname, path in members:
            files[arcname] = {"size": path.stat().st_size, "sha256": await asyncio.to_thread(_sha256, path)}

        from svbg.core.crypto import fingerprint

        manifest = {
            "format": FORMAT,
            "version": FORMAT_VERSION,
            "created_at": created.isoformat(),
            "reason": reason,
            "app_version": svbg.__version__,
            "alembic_revision": snap.revision,
            "server_major": snap.server_major,
            "pg_dump_major": dump_major,
            "tables": snap.tables,
            "files": files,
            "content": content,
            "has_env": has_env,
            "secret_key_fp": key_fingerprint or (fingerprint(key) if key else None),
        }
        suffix = "svbg" if password else "tar.gz"
        stamp = created
        while (dest := out_dir / f"svbg-{stamp:%Y%m%d-%H%M%S}-{reason}.{suffix}").exists():
            stamp += timedelta(seconds=1)  # two backups within one second keep both files
        try:
            size = await asyncio.to_thread(_pack, dest, manifest, members, password, kdf)
        except OSError as exc:
            raise BackupError(f"не удалось записать бэкап: {exc.strerror or type(exc).__name__}") from None
    finally:
        # Shielded: a cancelled backup (shutdown, ``svbg update``) still removes its partial dump.
        await _shielded(asyncio.to_thread(shutil.rmtree, tmp, True))
    removed = await asyncio.to_thread(rotate, out_dir, keep, protect=dest) if keep else []
    return BackupResult(
        path=dest,
        size=size,
        encrypted=bool(password),
        created_at=created,
        reason=reason,
        tables=len(snap.tables),
        rows=sum(snap.tables.values()),
        revision=snap.revision,
        has_env=has_env,
        content=content,
        duration_s=time.monotonic() - started,
        removed=tuple(removed),
        warnings=tuple(warnings),
    )


async def _shielded(aw: Awaitable[Any]) -> None:
    """Finish ``aw`` even if the caller is cancelled meanwhile (the cancellation still propagates)."""
    task = asyncio.ensure_future(aw)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.shield(task), 30)
        raise


def _read_optional(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


async def _dump(
    dsn: str,
    url: str,
    dsn_env: Mapping[str, str],
    *,
    tools: PgTools,
    out: Path,
    dump_major: int | None,
    limit_s: float,
) -> _Snapshot:
    """``pg_dump`` of an exported snapshot; the snapshot's transaction stays open until the dump is done."""
    from svbg.db.engine import normalize_dsn

    try:
        conn = await asyncpg.connect(
            normalize_dsn(dsn)[1],
            timeout=15,
            server_settings={
                "application_name": "svbg-backup",
                "idle_in_transaction_session_timeout": "0",
                "statement_timeout": "0",
            },
        )
    except (OSError, asyncpg.PostgresError, TimeoutError) as exc:
        raise BackupError(f"база данных недоступна: {mask(str(exc))[:300]}") from None
    try:
        tx = conn.transaction(isolation="repeatable_read", readonly=True)
        await tx.start()
        try:
            snap = await _open_snapshot(conn)
            if dump_major is not None and snap.server_major is not None and dump_major < snap.server_major:
                raise BackupError(
                    f"pg_dump {dump_major} старше сервера PostgreSQL {snap.server_major}: "
                    f"обновите образ бота (нужен postgresql-client-{snap.server_major})"
                )
            args = [
                "--format=custom",
                "--compress=0",
                f"--snapshot={snap.snapshot}",
                "--no-password",
                f"--file={out}",
                f"--dbname={url}",
            ]
            try:
                await tools.arun("pg_dump", args, timeout=limit_s, dsn_env=dsn_env)
            except PgToolError as exc:
                raise BackupError(str(exc)) from None
        finally:
            with contextlib.suppress(asyncpg.PostgresError, OSError):
                await tx.rollback()
    finally:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(conn.close(), 5)
    return snap


# ------------------------------------------------------------------------------------------ delivery


class _TopicState(Protocol):
    enabled: bool

    def thread_in(self, chat_id: int) -> int | None: ...


class AdminChatLike(Protocol):
    @property
    def configured(self) -> bool: ...

    @property
    def chat_id(self) -> int | None: ...

    def state(self, kind: str) -> _TopicState: ...

    async def ensure_topics(self) -> Any: ...


class TelegramDelivery:
    """Sends backup files as documents: the admin group topic «💾 Бэкапы», else every owner's DM.

    Documents go straight to the Bot API with a long upload timeout (a 45 MB part on a slow link takes
    minutes; the notifier's default request timeout is for messages). 429 is waited out (≤ 60 s), network
    errors are retried once.
    """

    def __init__(
        self,
        bot: Callable[[], Bot | None],
        *,
        admin_chat: AdminChatLike | None,
        owners: Callable[[], Awaitable[frozenset[int]]],
        topic: str = "backups",
        upload_timeout: float = 900.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._bot = bot
        self._admin_chat = admin_chat
        self._owners = owners
        self._topic = topic
        self._timeout = upload_timeout
        self._sleep = sleep

    async def targets(self) -> list[tuple[int, int | None]]:
        chat = self._admin_chat
        if chat is not None and chat.configured and chat.chat_id is not None:
            chat_id = chat.chat_id
            state = chat.state(self._topic)
            if not state.enabled:
                return [(chat_id, chat.state("system").thread_in(chat_id))]
            thread = state.thread_in(chat_id)
            if thread is None:
                with contextlib.suppress(Exception):
                    await chat.ensure_topics()
                thread = chat.state(self._topic).thread_in(chat_id)
            return [(chat_id, thread)]
        return [(owner, None) for owner in sorted(await self._owners())]

    async def send(self, files: Sequence[Path], captions: Sequence[str]) -> int:
        """Send every file to every target; returns how many targets got all of them."""
        from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter, TelegramServerError
        from aiogram.methods import SendDocument
        from aiogram.types import FSInputFile

        bot = self._bot()
        if bot is None:
            raise BackupError("бот не запущен: файл бэкапа не отправлен")
        targets = await self.targets()
        if not targets:
            raise BackupError("некуда отправить бэкап: нет админ-группы и владельцев")
        delivered = 0
        for chat_id, thread in targets:
            for path, caption in zip(files, captions, strict=True):
                method = SendDocument(
                    chat_id=chat_id,
                    document=FSInputFile(path, filename=path.name),
                    caption=caption,
                    message_thread_id=thread,
                    disable_notification=True,
                    disable_content_type_detection=True,
                )
                for attempt in range(3):
                    try:
                        await bot(method, request_timeout=int(self._timeout))
                        break
                    except TelegramRetryAfter as exc:
                        if attempt == 2 or exc.retry_after > 60:
                            raise
                        await self._sleep(float(exc.retry_after))
                    except (TelegramNetworkError, TelegramServerError, TimeoutError):
                        if attempt == 2:
                            raise
                        await self._sleep(5.0)
            delivered += 1
        return delivered


def human_size(size: int) -> str:
    if size < 1024 * 1024:
        return f"{max(1, round(size / 1024))} КБ"
    return f"{size / (1024 * 1024):.1f} МБ".replace(".", ",")


def captions_for(result: BackupResult, parts: Sequence[Path], tz_name: str) -> list[str]:
    from zoneinfo import ZoneInfo

    try:
        local = result.created_at.astimezone(ZoneInfo(tz_name))
    except (ValueError, KeyError):
        local = result.created_at.astimezone(UTC)
    head = f"💾 Бэкап {local:%d.%m.%Y %H:%M} · {human_size(result.size)}"
    tail = "Восстановление: svbg restore <файл>. Пароль — BACKUP_PASSWORD."
    if len(parts) == 1:
        return [f"{head}\n{tail}"]
    return [f"{head} · часть {i}/{len(parts)}\n{tail}" for i in range(1, len(parts) + 1)]


# ------------------------------------------------------------------------------------------ service


class Delivery(Protocol):
    async def send(self, files: Sequence[Path], captions: Sequence[str]) -> int: ...


#: ``notify(text, high)`` into the backups topic (Telegram HTML).
Notify = Callable[[str, bool], Awaitable[Any]]


class BackupBusyError(BackupError):
    """Another backup is running in this process."""


class BackupService:
    """Scheduled and manual backups: settings, the daily moment, Telegram delivery, status and alerts.

    ``tick()`` runs every minute (0 SQL until the moment ``BACKUP_AT``); ``run()`` does one backup now.
    A failure raises the «Требует внимания» item ``ops:backup`` and posts into «💾 Бэкапы»; the next success
    resolves it.
    """

    def __init__(
        self,
        *,
        dsn: str,
        data_dir: Path,
        env_path: Path | None,
        settings: Callable[[], Mapping[str, Any]],
        state: Any,  # MetaState
        delivery: Delivery | None = None,
        notify: Notify | None = None,
        attention: Any = None,  # AttentionService
        content_export: ContentExport | None = None,
        key_fingerprint: Callable[[], str | None] = lambda: None,
        tools: PgTools | None = None,
        kdf: KdfParams | None = None,
        part_size: int = PART_SIZE,
    ) -> None:
        self.dsn = dsn
        self.data_dir = data_dir
        self.backup_dir = data_dir / "backups"
        self._env_path = env_path
        self._settings = settings
        self._state = state
        self._delivery = delivery
        self._notify = notify
        self._attention = attention
        self._content_export = content_export
        self._key_fingerprint = key_fingerprint
        self._tools = tools
        self._kdf = kdf
        self._part_size = part_size
        self._lock = asyncio.Lock()
        self._last_day: str | None = None
        self.last: dict[str, Any] | None = None

    @property
    def running(self) -> bool:
        return self._lock.locked()

    async def tick(self) -> bool:
        """Daily backup at ``BACKUP_AT`` in ``TIMEZONE``; ``True`` if one was made by this call."""
        from svbg.ops.settings import opt
        from svbg.ops.state import K_BACKUP
        from svbg.ops.timing import due_today, zone_of

        snap = self._settings()
        if not opt(snap, "BACKUP_ENABLED"):
            return False
        now = clock.now()
        due = due_today(now, opt(snap, "BACKUP_AT"), zone_of(opt(snap, "TIMEZONE")), self._last_day)
        if due is None:
            return False
        if self._last_day is None:
            self._last_day = str((await self._state.get(K_BACKUP)).get("day") or "")
            if self._last_day == due.day:
                return False
        await self._state.merge(K_BACKUP, {"day": due.day})
        self._last_day = due.day
        if not due.run:
            return False
        try:
            await self.run("daily")
        except BackupBusyError:
            return False
        return True

    async def run(self, reason: str = "manual") -> BackupResult:
        """One backup now (+ Telegram when enabled). Raises :class:`BackupError` after alerting."""
        if self._lock.locked():
            raise BackupBusyError("бэкап уже выполняется")
        async with self._lock:
            return await self._run(reason)

    async def _run(self, reason: str) -> BackupResult:
        from svbg.ops.settings import opt
        from svbg.ops.state import K_BACKUP

        snap = self._settings()
        password = opt(snap, "BACKUP_PASSWORD") or None
        try:
            result = await create_backup(
                self.dsn,
                self.backup_dir,
                reason=reason,
                password=password,
                env_path=self._env_path,
                media_dir=self.data_dir / "media",
                content_export=self._content_export,
                keep=int(opt(snap, "BACKUP_KEEP")),
                tools=self._tools,
                kdf=self._kdf,
                key_fingerprint=self._key_fingerprint(),
            )
        except BackupError as exc:
            await self._failed(str(exc))
            raise
        except Exception as exc:
            await self._failed(f"непредвиденная ошибка ({type(exc).__name__})")
            raise BackupError(f"непредвиденная ошибка ({type(exc).__name__})") from exc

        sent = 0
        notes = list(result.warnings)
        if opt(snap, "BACKUP_TO_TELEGRAM") and self._delivery is not None:
            if not result.encrypted:
                notes.append(
                    "в Telegram не отправлен: задайте BACKUP_PASSWORD (без пароля бэкап только на сервере)"
                )
            else:
                try:
                    sent = await self._send(result, str(opt(snap, "TIMEZONE")))
                except Exception as exc:
                    log.warning("backup upload failed: %s", type(exc).__name__, exc_info=exc)
                    notes.append(
                        f"не удалось отправить в Telegram: {mask(str(exc))[:200] or type(exc).__name__}"
                    )
        state = {**result.as_state(), "sent": sent, "error": None, "notes": notes}
        self.last = state
        await self._state.merge(K_BACKUP, {"last": state, "last_ok_at": result.created_at.isoformat()})
        if self._attention is not None:
            with contextlib.suppress(Exception):
                await self._attention.resolve("ops:backup")
        if self._notify is not None and (notes or not sent):
            text = f"💾 Бэкап готов: {result.path.name} · {human_size(result.size)}" + (
                "" if result.encrypted else " · без пароля"
            )
            if notes:
                text += "\n" + "\n".join(f"⚠️ {n}" for n in notes)
            with contextlib.suppress(Exception):
                await self._notify(_escape(text), bool(notes))
        return result

    async def _send(self, result: BackupResult, tz_name: str) -> int:
        assert self._delivery is not None
        workdir = self.backup_dir / f".tmp-send-{secrets.token_hex(4)}"
        await asyncio.to_thread(workdir.mkdir, mode=0o700)
        try:
            parts = await asyncio.to_thread(split_parts, result.path, self._part_size, workdir)
            return await self._delivery.send(parts, captions_for(result, parts, tz_name))
        finally:
            await asyncio.to_thread(shutil.rmtree, workdir, True)

    async def _failed(self, reason: str) -> None:
        from svbg.ops.state import K_BACKUP

        log.error("backup failed: %s", reason)
        self.last = {"error": reason, "at": clock.now().isoformat()}
        with contextlib.suppress(Exception):
            await self._state.merge(K_BACKUP, {"last_error": self.last})
        if self._attention is not None:
            with contextlib.suppress(Exception):
                await self._attention.raise_item(
                    "ops:backup", "error", "Бэкап не удался", reason[:500], fix_action="screen:ops"
                )
        if self._notify is not None:
            with contextlib.suppress(Exception):
                await self._notify(_escape(f"🔴 Бэкап не удался: {reason}"), True)

    async def status(self) -> dict[str, Any]:
        """Durable status for the ops screen (one statement)."""
        from svbg.ops.state import K_BACKUP

        return await self._state.get(K_BACKUP)


def _escape(text: str) -> str:
    import html

    return html.escape(text, quote=False)
