"""``svbg restore``: a backup (or its Telegram parts) → an empty PostgreSQL database, verified.

Order, so that a bad file never touches the database:

1. the archive is decrypted and unpacked into a private temporary directory; every member is checked against
   ``manifest.json`` (size + SHA-256), the encryption's final block and the gzip CRC are verified;
2. the ``.env`` decision is made (see :class:`EnvMode`) — a backup whose ``SECRET_KEY`` differs from the
   current one is refused in ``auto`` mode, because its secrets would not decrypt;
3. the target database must be empty (``wipe=True`` drops the ``public`` schema first);
4. ``pg_restore --single-transaction --exit-on-error`` (all or nothing);
5. verification: the row count of every table equals the count taken in the dump's snapshot, the Alembic
   revision matches, the stored secrets decrypt with the key the bot will use;
6. media files from ``content.zip`` are put back into ``data/media``; the ``.env`` copy is written last
   (the current file is kept as ``.env.before-restore-<time>``; ``DATABASE_URL`` of this host is kept).
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import gzip
import hashlib
import io
import json
import re
import secrets
import shutil
import tarfile
import zipfile
import zlib
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import IO, Any, Final

import asyncpg

from svbg.core import clock
from svbg.core.log import mask
from svbg.ops.backup import FORMAT, FORMAT_VERSION
from svbg.ops.crypt import BackupCryptoError, DecryptingReader, decrypt_bytes, is_encrypted_head
from svbg.ops.pgtools import PgToolError, PgTools, conninfo, major_version

__all__ = [
    "EnvMode",
    "RestoreError",
    "RestoreReport",
    "check_secrets",
    "resolve_inputs",
    "restore",
    "unpack",
]

_PART_RE: Final = re.compile(r"^(?P<base>.+)\.part(?P<i>\d{1,3})of(?P<n>\d{1,3})$")
_MEMBERS: Final = frozenset({"manifest.json", "db.dump", "content.zip", "env.enc"})
_MEDIA_RE: Final = re.compile(r"^media/[A-Za-z0-9][A-Za-z0-9._\-/]{0,250}$")
#: A file of the constructor's export (``media/<sha256>.<ext>``, flat); on disk it lives in ``<sha[:2]>/``
#: (``svbg.content.media.media_rel_path``) — that is the path ``media.path`` of the restored rows points to.
_FLAT_MEDIA_RE: Final = re.compile(r"^([0-9a-f]{64})\.([a-z0-9]{1,10})$")
_MANIFEST_MAX: Final = 4 * 1024 * 1024
_COPY_BUF: Final = 1024 * 1024
#: Keys of this host kept when the ``.env`` from the backup is written.
_HOST_KEYS: Final = ("DATABASE_URL", "DATA_DIR", "LOCKED_KEYS")


class RestoreError(Exception):
    """Restore refused or failed. ``str()`` is owner-facing (Russian)."""


class EnvMode(enum.Enum):
    AUTO = "auto"  # write when there is no .env here; refuse when SECRET_KEY differs; else keep
    WRITE = "write"  # always write the .env from the backup (the current one is kept as a copy)
    SKIP = "skip"  # never touch .env (secrets decrypt only if SECRET_KEY is the same)


@dataclass
class RestoreReport:
    manifest: dict[str, Any]
    tables: int = 0
    rows: int = 0
    secrets_ok: int = 0
    secrets_failed: int = 0
    env_written: bool = False
    env_backup: Path | None = None
    media_restored: int = 0
    wiped: bool = False
    warnings: list[str] = field(default_factory=list)


# ------------------------------------------------------------------------------------------ inputs


def resolve_inputs(paths: Sequence[Path]) -> list[Path]:
    """The files of one backup in order: a whole file, or all ``.partIofN`` parts (siblings are found)."""
    if not paths:
        raise RestoreError("не указан файл бэкапа")
    parsed = [(_PART_RE.match(p.name), p) for p in paths]
    if all(m is None for m, _ in parsed):
        if len(paths) != 1:
            raise RestoreError("укажите один файл бэкапа (или его части .partNofM)")
        path = paths[0]
        if not path.is_file():
            raise RestoreError(f"файл не найден: {path}")
        return [path]
    if any(m is None for m, _ in parsed):
        raise RestoreError("нельзя смешивать целый файл и части .partNofM")
    bases = {(m["base"], int(m["n"])) for m, _ in parsed if m is not None}
    if len(bases) != 1:
        raise RestoreError("части принадлежат разным бэкапам")
    ((base, total),) = bases
    if not 1 <= total <= 999:
        raise RestoreError("неверное число частей")
    given = {int(m["i"]): p for m, p in parsed if m is not None}
    folder = paths[0].parent
    out: list[Path] = []
    for index in range(1, total + 1):
        path = given.get(index) or folder / f"{base}.part{index}of{total}"
        if not path.is_file():
            raise RestoreError(f"нет части {index} из {total}: {path.name} (скачайте все части в одну папку)")
        out.append(path)
    return out


class _Joined(io.RawIOBase):
    """The parts read back to back as one stream."""

    def __init__(self, paths: Sequence[Path]) -> None:
        super().__init__()
        self._paths = list(paths)
        self._file: IO[bytes] | None = None

    def readable(self) -> bool:
        return True

    def readinto(self, b: Any) -> int:
        view = memoryview(b).cast("B")
        while True:
            if self._file is None:
                if not self._paths:
                    return 0
                self._file = self._paths.pop(0).open("rb")
            n = self._file.readinto(view)
            if n:
                return n
            self._file.close()
            self._file = None

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
        super().close()


@contextlib.contextmanager
def _plain_stream(paths: Sequence[Path], password: str | None) -> Iterator[IO[bytes]]:
    joined = io.BufferedReader(_Joined(paths), buffer_size=_COPY_BUF)
    try:
        head = joined.peek(16)[:16]
        if is_encrypted_head(head):
            if not password:
                raise RestoreError("бэкап зашифрован: нужен пароль (BACKUP_PASSWORD)")
            try:
                yield io.BufferedReader(DecryptingReader(joined, password), buffer_size=_COPY_BUF)
            except BackupCryptoError as exc:
                raise RestoreError(str(exc)) from None
        elif head[:2] == b"\x1f\x8b":
            yield joined
        else:
            raise RestoreError("это не бэкап SvBG (неизвестный формат файла)")
    finally:
        joined.close()


def _safe_copy(src: IO[bytes], dest: Path, expected: int | None) -> int:
    size = 0
    with dest.open("wb") as out:
        while piece := src.read(_COPY_BUF):
            size += len(piece)
            if expected is not None and size > expected:
                raise RestoreError(f"бэкап повреждён: {dest.name} больше заявленного размера")
            out.write(piece)
    return size


def _extract(tar: tarfile.TarFile, member: tarfile.TarInfo, workdir: Path, found: set[str]) -> None:
    """Copy one archive member (only the known regular files, each once) into ``workdir``."""
    name = member.name
    if name not in _MEMBERS or not member.isfile() or name in found:
        raise RestoreError(f"бэкап содержит неожиданный элемент: {name[:80]!r}")
    if name == "manifest.json" and member.size > _MANIFEST_MAX:
        raise RestoreError("бэкап повреждён: manifest.json слишком большой")
    src = tar.extractfile(member)
    if src is None:
        raise RestoreError(f"бэкап повреждён: {name} не читается")
    _safe_copy(src, workdir / name, member.size)
    found.add(name)


def unpack(paths: Sequence[Path], password: str | None, workdir: Path) -> dict[str, Any]:
    """Decrypt + unpack into ``workdir``, verify against the manifest; returns the manifest."""
    found: set[str] = set()
    try:
        with _plain_stream(paths, password) as stream:
            gz = gzip.GzipFile(fileobj=stream, mode="rb")
            with tarfile.open(fileobj=gz, mode="r|") as tar:
                for member in tar:
                    _extract(tar, member, workdir, found)
            while gz.read(_COPY_BUF):  # the end of the gzip stream: CRC and length are checked here
                pass
            while stream.read(_COPY_BUF):  # the end of the encrypted stream: the final block is checked
                pass
    except RestoreError:
        raise
    except BackupCryptoError as exc:
        raise RestoreError(str(exc)) from None
    except (OSError, EOFError, zlib.error, tarfile.TarError) as exc:
        if isinstance(exc, FileNotFoundError):
            raise RestoreError(f"файл не найден: {exc.filename}") from None
        raise RestoreError(f"архив повреждён или обрезан ({type(exc).__name__})") from None
    if "manifest.json" not in found:
        raise RestoreError("в архиве нет manifest.json: это не бэкап SvBG")
    try:
        manifest = json.loads((workdir / "manifest.json").read_text("utf-8"))
    except ValueError:
        raise RestoreError("manifest.json повреждён") from None
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise RestoreError("это не бэкап SvBG (manifest.json чужого формата)")
    if int(manifest.get("version") or 0) > FORMAT_VERSION:
        raise RestoreError("бэкап сделан более новой версией бота: сначала обновите бота")
    files = manifest.get("files")
    if not isinstance(files, dict) or "db.dump" not in files:
        raise RestoreError("в бэкапе нет дампа базы")
    for name, meta in files.items():
        path = workdir / str(name)
        if name not in _MEMBERS or not path.is_file():
            raise RestoreError(f"в архиве нет файла {name}")
        if path.stat().st_size != int(meta.get("size", -1)) or _sha256(path) != meta.get("sha256"):
            raise RestoreError(f"контрольная сумма {name} не совпала: файл повреждён")
    return manifest


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while piece := f.read(_COPY_BUF):
            h.update(piece)
    return h.hexdigest()


# ------------------------------------------------------------------------------------------ env


def _env_key(text: str | None) -> str | None:
    if not text:
        return None
    from svbg.boot.envfile import EnvDocument

    value = EnvDocument.parse(text).get("SECRET_KEY")
    return value.strip() if value and value.strip() else None


def _merge_env(restored: str, current: str | None) -> str:
    """The ``.env`` of the backup with this host's ``DATABASE_URL`` (and DATA_DIR / LOCKED_KEYS) kept."""
    from svbg.boot.envfile import EnvDocument

    doc = EnvDocument.parse(restored)
    if current:
        here = EnvDocument.parse(current)
        for key in _HOST_KEYS:
            value = here.get(key)
            if value is not None:
                doc.set(key, value)
    return doc.render()


# ------------------------------------------------------------------------------------------ database


async def _connect(dsn: str) -> asyncpg.Connection:
    from svbg.db.engine import normalize_dsn

    try:
        return await asyncpg.connect(normalize_dsn(dsn)[1], timeout=15)
    except (OSError, asyncpg.PostgresError, TimeoutError, ValueError) as exc:
        raise RestoreError(f"база данных недоступна: {mask(str(exc))[:300]}") from None


async def _public_tables(conn: asyncpg.Connection) -> list[str]:
    rows = await conn.fetch(
        "select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace "
        "where n.nspname = 'public' and c.relkind in ('r', 'p', 'v', 'm', 'S', 'f') order by 1"
    )
    return [r["relname"] for r in rows]


async def _count(conn: asyncpg.Connection, names: Sequence[str]) -> dict[str, int]:
    if not names:
        return {}
    q = " union all ".join(
        "select '{lit}' as t, count(*)::bigint as c from public.\"{ident}\"".format(
            lit=n.replace("'", "''"), ident=n.replace('"', '""')
        )
        for n in names
    )
    return {r["t"]: int(r["c"]) for r in await conn.fetch(q)}


async def check_secrets(conn: asyncpg.Connection, key: str | None) -> tuple[int, int]:
    """``(ok, failed)``: encrypted values in ``settings`` and ``payment_instances`` tried with ``key``."""
    from svbg.core.crypto import PREFIX, Crypto, CryptoError

    values: list[str] = []
    tables = set(await _public_tables(conn))
    if "settings" in tables:
        for r in await conn.fetch("select value::text as v from settings"):
            with contextlib.suppress(ValueError, TypeError):
                v = json.loads(r["v"])
                if isinstance(v, str) and v.startswith(PREFIX):
                    values.append(v)
    if "payment_instances" in tables:
        for r in await conn.fetch("select config, webhook_token, proxy_url from payment_instances"):
            values += [v for v in r.values() if isinstance(v, str) and v.startswith(PREFIX)]
    if not values:
        return 0, 0
    if not key:
        return 0, len(values)
    try:
        crypto = Crypto([key])
    except CryptoError:
        return 0, len(values)
    ok = 0
    for v in values:
        with contextlib.suppress(CryptoError):
            crypto.decrypt(v)
            ok += 1
    return ok, len(values) - ok


def _restore_media(content_zip: Path, media_dir: Path) -> int:
    restored = 0
    root = media_dir.resolve()
    with zipfile.ZipFile(content_zip) as zf:
        for info in zf.infolist():
            name = info.filename
            if info.is_dir() or not _MEDIA_RE.match(name) or ".." in PurePosixPath(name).parts:
                continue
            rel = PurePosixPath(name).relative_to("media")
            flat = _FLAT_MEDIA_RE.match(rel.as_posix())
            if flat is not None:
                rel = PurePosixPath(flat[1][:2], f"{flat[1]}.{flat[2]}")
            target = (media_dir / rel).resolve()
            if not target.is_relative_to(root) or target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out, _COPY_BUF)
            restored += 1
    return restored


# ------------------------------------------------------------------------------------------ restore


async def restore(
    paths: Sequence[Path],
    dsn: str,
    *,
    data_dir: Path,
    env_path: Path,
    password: str | None = None,
    env_mode: EnvMode = EnvMode.AUTO,
    wipe: bool = False,
    current_secret_key: str | None = None,
    tools: PgTools | None = None,
    restore_timeout: float = 3600.0,
    progress: Callable[[str], None] = lambda _msg: None,
) -> RestoreReport:
    """Restore a backup into the database ``dsn`` (see the module docstring). Raises :class:`RestoreError`."""
    files = await asyncio.to_thread(resolve_inputs, list(paths))
    tools = tools or PgTools.locate()
    workdir = data_dir / f".restore-{secrets.token_hex(6)}"
    await asyncio.to_thread(_make_workdir, data_dir, workdir)
    try:
        progress("Проверяю архив…")
        manifest = await asyncio.to_thread(unpack, files, password, workdir)
        report = RestoreReport(manifest)

        # ---- .env decision (before the database is touched)
        current_env = await asyncio.to_thread(_read_text, env_path)
        here_key = current_secret_key or _env_key(current_env)
        backup_fp = manifest.get("secret_key_fp")
        env_blob = (workdir / "env.enc").read_bytes() if (workdir / "env.enc").is_file() else None
        restored_env: str | None = None
        if env_blob is not None and env_mode is not EnvMode.SKIP:
            assert password is not None  # env.enc exists only in encrypted backups
            try:
                restored_env = decrypt_bytes(env_blob, password).decode("utf-8")
            except (BackupCryptoError, UnicodeError):
                raise RestoreError("копия .env в бэкапе повреждена") from None
        from svbg.core.crypto import fingerprint

        same_key = backup_fp is None or (here_key is not None and fingerprint(here_key) == backup_fp)
        write_env = False
        if env_mode is EnvMode.WRITE:
            if restored_env is None:
                raise RestoreError("в бэкапе нет копии .env (бэкап без пароля?) — запустите с --env skip")
            write_env = True
        elif env_mode is EnvMode.AUTO and not same_key:
            if restored_env is not None and current_env is None:
                write_env = True
            elif restored_env is not None:
                raise RestoreError(
                    "SECRET_KEY здесь другой, чем в бэкапе: секреты из бэкапа не расшифруются. "
                    "Запустите с --env write (файл .env будет взят из бэкапа, текущий сохранится рядом, "
                    "DATABASE_URL этого сервера останется) или верните прежний SECRET_KEY и --env skip"
                )
            else:
                report.warnings.append(
                    "SECRET_KEY отличается от бэкапа, а копии .env в бэкапе нет: верните прежний SECRET_KEY"
                )
        elif not same_key:
            report.warnings.append("SECRET_KEY отличается от бэкапа: секреты не расшифруются")
        key_after = _env_key(restored_env) if write_env else here_key

        # ---- database
        try:
            url, dsn_env = conninfo(dsn)
            restore_major = major_version(await tools.aversion("pg_restore"))
        except PgToolError as exc:
            raise RestoreError(str(exc)) from None
        conn = await _connect(dsn)
        try:
            existing = await _public_tables(conn)
            if existing and not wipe:
                raise RestoreError(
                    f"база не пустая ({len(existing)} таблиц): восстановление — только в пустую базу. "
                    "Остановите бота и запустите с --wipe (все данные этой базы будут удалены)"
                )
            server_major = (
                int(await conn.fetchval("select current_setting('server_version_num')::int")) // 10000
            )
            if restore_major is not None and restore_major < server_major:
                raise RestoreError(f"pg_restore {restore_major} старше сервера PostgreSQL {server_major}")
            if existing:
                progress("Очищаю базу…")
                await conn.execute("drop schema public cascade; create schema public")
                report.wiped = True
        finally:
            await conn.close()

        progress("Восстанавливаю базу…")
        args = [
            "--no-owner",
            "--no-privileges",
            "--exit-on-error",
            "--single-transaction",
            "--no-password",
            f"--dbname={url}",
            str(workdir / "db.dump"),
        ]
        try:
            await tools.arun("pg_restore", args, timeout=restore_timeout, dsn_env=dsn_env)
        except PgToolError as exc:
            raise RestoreError(str(exc)) from None

        progress("Проверяю восстановленные данные…")
        expected = {str(k): int(v) for k, v in dict(manifest.get("tables") or {}).items()}
        conn = await _connect(dsn)
        try:
            actual = await _count(conn, [t for t in await _public_tables(conn) if t in expected])
            missing = sorted(set(expected) - set(actual))
            wrong = sorted(t for t, n in expected.items() if t in actual and actual[t] != n)
            if missing or wrong:
                detail = ", ".join(
                    [
                        *(f"{t}: нет" for t in missing[:5]),
                        *(f"{t}: {actual[t]} ≠ {expected[t]}" for t in wrong[:5]),
                    ]
                )
                raise RestoreError(f"проверка не пройдена — данные не совпадают с бэкапом ({detail})")
            revision = manifest.get("alembic_revision")
            if revision is not None:
                got = await conn.fetchval("select version_num from alembic_version limit 1")
                if got != revision:
                    raise RestoreError(f"проверка не пройдена: версия схемы {got} ≠ {revision}")
            report.tables, report.rows = len(actual), sum(actual.values())
            report.secrets_ok, report.secrets_failed = await check_secrets(conn, key_after)
        finally:
            await conn.close()
        if report.secrets_failed:
            report.warnings.append(
                f"секретов не расшифровано: {report.secrets_failed} (другой SECRET_KEY) — введите заново"
            )

        content_zip = workdir / "content.zip"
        if content_zip.is_file():
            try:
                report.media_restored = await asyncio.to_thread(
                    _restore_media, content_zip, data_dir / "media"
                )
            except (OSError, zipfile.BadZipFile) as exc:
                report.warnings.append(f"медиа не восстановлены ({type(exc).__name__})")

        if write_env and restored_env is not None:
            report.env_backup = await asyncio.to_thread(_write_env, env_path, restored_env, current_env)
            report.env_written = True
        return report
    finally:
        await asyncio.to_thread(shutil.rmtree, workdir, True)


def _make_workdir(data_dir: Path, workdir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    workdir.mkdir(mode=0o700)


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text("utf-8")
    except FileNotFoundError:
        return None


def _write_env(env_path: Path, restored: str, current: str | None) -> Path | None:
    from svbg.boot.envfile import write_atomic

    saved: Path | None = None
    if current is not None:
        saved = env_path.with_name(f"{env_path.name}.before-restore-{clock.now():%Y%m%d-%H%M%S}")
        saved.write_text(current, "utf-8")
        with contextlib.suppress(OSError, NotImplementedError):
            saved.chmod(0o600)
    env_path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(env_path, _merge_env(restored, current), keep_backup=False)
    return saved
