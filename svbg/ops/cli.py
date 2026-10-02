"""CLI commands of the ops module, added to ``python -m svbg`` by :func:`add_commands`::

    backup [--reason R] [--password-file F] [--no-password]
    restore FILE [FILE…] [--password-file F] [--env auto|write|skip] [--wipe] [--yes] [--dsn URL]

Passwords are never taken from the command line (``ps`` shows it): ``--password-file``, the environment
variable ``SVBG_BACKUP_PASSWORD``, ``BACKUP_PASSWORD`` in ``data/.env`` (backup only) or a prompt.
Exit codes follow ``svbg.__main__``: 0 ok, 1 failure, 2 usage, 78 configuration.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final, Protocol

__all__ = ["add_commands", "backup_before_migrate", "cmd_backup", "cmd_restore"]

EXIT_OK: Final = 0
EXIT_FAIL: Final = 1
EXIT_USAGE: Final = 2
EXIT_CONFIG: Final = 78
_ENV_PASSWORD: Final = "SVBG_BACKUP_PASSWORD"  # noqa: S105 - the name of a variable, not a secret
_WIPE_WORD: Final = "СТЕРЕТЬ"


class _Io(Protocol):
    def say(self, text: str = "") -> None: ...

    def warn(self, text: str) -> None: ...


def _boot(environ: Mapping[str, str]) -> Any:
    from svbg.core.settings import BootstrapError, read_bootstrap
    from svbg.core.settings.bootstrap import default_env_path

    try:
        return read_bootstrap(default_env_path(environ), environ)
    except BootstrapError as exc:
        raise _Fail(str(exc), EXIT_CONFIG) from None


class _Fail(Exception):
    def __init__(self, message: str, code: int = EXIT_FAIL) -> None:
        super().__init__(message)
        self.code = code


def _read_password_file(path: str) -> str:
    try:
        text = Path(path).read_text("utf-8")
    except OSError as exc:
        raise _Fail(f"файл пароля не читается: {exc.strerror or type(exc).__name__}", EXIT_USAGE) from None
    value = text.strip("\r\n")
    if not value:
        raise _Fail("файл пароля пуст", EXIT_USAGE)
    return value


def _env_file_password(env_path: Path) -> str | None:
    from svbg.boot.envfile import EnvDocument, read_text
    from svbg.core.settings.values import SECRET_PLACEHOLDER

    try:
        text = read_text(env_path)
    except (OSError, ValueError):
        return None
    value = EnvDocument.parse(text or "").get("BACKUP_PASSWORD")
    if not value or value == SECRET_PLACEHOLDER:
        return None
    return value


def _ask(prompt: str) -> str | None:
    if not sys.stdin or not sys.stdin.isatty():
        return None
    try:
        return getpass.getpass(prompt) or None
    except (EOFError, KeyboardInterrupt):
        return None


def _dsn(args: argparse.Namespace, boot: Any) -> str:
    dsn = args.dsn or boot.database_url
    if not dsn:
        raise _Fail(f"DATABASE_URL не задан: впишите его в {boot.env_path}", EXIT_CONFIG)
    return str(dsn)


def _confirm_wipe(io: _Io) -> None:
    answer = None
    if sys.stdin and sys.stdin.isatty():
        io.say(f"Все данные базы будут удалены. Чтобы продолжить, введите {_WIPE_WORD}:")
        try:
            answer = input().strip()
        except EOFError:
            answer = None
    if answer != _WIPE_WORD:
        raise _Fail("отменено: --wipe требует подтверждения (или --yes)", EXIT_USAGE)


def _restore_password(args: argparse.Namespace, environ: Mapping[str, str], paths: list[Path]) -> str | None:
    if args.password_file:
        return _read_password_file(args.password_file)
    password = environ.get(_ENV_PASSWORD) or None
    if password is None and _looks_encrypted(paths):
        password = _ask("Пароль бэкапа (BACKUP_PASSWORD): ")
        if not password:
            raise _Fail(
                f"бэкап зашифрован: передайте пароль через --password-file или {_ENV_PASSWORD}", EXIT_USAGE
            )
    return password


def cmd_backup(args: argparse.Namespace, environ: Mapping[str, str], io: _Io) -> int:
    from svbg.ops.backup import BackupError, create_backup, human_size

    try:
        boot = _boot(environ)
        dsn = _dsn(args, boot)
        env_path: Path = boot.env_path
        password: str | None = None
        if not args.no_password:
            if args.password_file:
                password = _read_password_file(args.password_file)
            else:
                password = environ.get(_ENV_PASSWORD) or _env_file_password(env_path)
        data_dir = env_path.parent
        keep = args.keep
        result = asyncio.run(
            create_backup(
                dsn,
                data_dir / "backups",
                reason=args.reason,
                password=password,
                env_path=env_path,
                media_dir=data_dir / "media",
                keep=keep,
                secret_key=boot.secret_key,
            )
        )
    except _Fail as exc:
        io.warn(f"❌ {exc}")
        return exc.code
    except BackupError as exc:
        io.warn(f"❌ Бэкап не удался: {exc}")
        return EXIT_FAIL
    io.say(
        f"✅ Бэкап: {result.path} ({human_size(result.size)}, таблиц {result.tables}, строк {result.rows})"
    )
    if not result.encrypted:
        io.warn("⚠️ Без пароля: бэкап не зашифрован, копии .env в нём нет. Храните файл только на сервере.")
    for note in result.warnings:
        io.warn(f"⚠️ {note}")
    return EXIT_OK


def cmd_restore(args: argparse.Namespace, environ: Mapping[str, str], io: _Io) -> int:
    from svbg.ops.restore import EnvMode, RestoreError, restore

    try:
        boot = _boot(environ)
        dsn = _dsn(args, boot)
        if args.wipe and not args.yes:
            _confirm_wipe(io)
        paths = [Path(p) for p in args.files]
        password = _restore_password(args, environ, paths)
        env_path: Path = boot.env_path
        report = asyncio.run(
            restore(
                paths,
                dsn,
                data_dir=env_path.parent,
                env_path=env_path,
                password=password,
                env_mode=EnvMode(args.env),
                wipe=args.wipe,
                current_secret_key=boot.secret_key,
                progress=io.say,
            )
        )
    except _Fail as exc:
        io.warn(f"❌ {exc}")
        return exc.code
    except RestoreError as exc:
        io.warn(f"❌ Восстановление не выполнено: {exc}")
        return EXIT_FAIL
    io.say(f"✅ База восстановлена и проверена: таблиц {report.tables}, строк {report.rows}")
    if report.secrets_ok or report.secrets_failed:
        io.say(f"🔐 Секреты: расшифровано {report.secrets_ok} из {report.secrets_ok + report.secrets_failed}")
    if report.media_restored:
        io.say(f"🖼 Медиафайлов восстановлено: {report.media_restored}")
    if report.env_written:
        saved = f" (прежний сохранён: {report.env_backup.name})" if report.env_backup else ""
        io.say(f"📄 Файл .env взят из бэкапа{saved}; DATABASE_URL этого сервера сохранён")
    for note in report.warnings:
        io.warn(f"⚠️ {note}")
    io.say("Запустите бота: svbg up (или docker compose up -d)")
    return EXIT_OK


def _looks_encrypted(paths: list[Path]) -> bool:
    from svbg.ops.crypt import is_encrypted_head
    from svbg.ops.restore import RestoreError, resolve_inputs

    try:
        first = resolve_inputs(paths)[0]
        with first.open("rb") as f:
            return is_encrypted_head(f.read(16))
    except (RestoreError, OSError):
        return False


def add_commands(sub: Any) -> None:
    """Add ``backup`` and ``restore`` to the subparsers of ``svbg.__main__.build_parser``."""
    p_backup = sub.add_parser("backup", help="сделать бэкап базы в data/backups")
    p_backup.add_argument("--reason", default="manual", help="метка в имени файла (manual, pre_update…)")
    p_backup.add_argument("--password-file", help="файл с паролем шифрования (иначе BACKUP_PASSWORD)")
    p_backup.add_argument("--no-password", action="store_true", help="не шифровать (только для сервера)")
    p_backup.add_argument("--keep", type=int, default=None, help="оставить N последних бэкапов")
    p_backup.add_argument("--dsn", help=argparse.SUPPRESS)
    p_backup.set_defaults(handler=cmd_backup)

    p_restore = sub.add_parser("restore", help="восстановить бэкап в пустую базу")
    p_restore.add_argument("files", nargs="+", metavar="FILE", help="файл бэкапа или все его части .partNofM")
    p_restore.add_argument("--password-file", help="файл с паролем бэкапа")
    p_restore.add_argument(
        "--env",
        choices=["auto", "write", "skip"],
        default="auto",
        help="копия .env из бэкапа: auto (по умолчанию), write — записать, skip — не трогать",
    )
    p_restore.add_argument("--wipe", action="store_true", help="очистить непустую базу перед восстановлением")
    p_restore.add_argument("--yes", action="store_true", help="не спрашивать подтверждение --wipe")
    p_restore.add_argument("--dsn", help="другая база (по умолчанию DATABASE_URL)")
    p_restore.set_defaults(handler=cmd_restore)


async def backup_before_migrate(
    dsn: str, env_path: Path, environ: Mapping[str, str], *, head: str | None
) -> Path | None:
    """Automatic ``pre_migrate`` backup (04 §10) for ``svbg migrate`` / ``svbg run``.

    Made only when the database already has a schema at another revision than ``head`` (a fresh database
    or an up-to-date one is skipped). Encrypted when ``SVBG_BACKUP_PASSWORD`` or ``BACKUP_PASSWORD`` in
    ``data/.env`` is set. Raises :class:`svbg.ops.backup.BackupError`: the caller must not migrate then.
    """
    from svbg.db.migrations import current_revision
    from svbg.ops.backup import create_backup

    current = await current_revision(dsn)
    if current is None or current == head:
        return None
    password = environ.get(_ENV_PASSWORD) or _env_file_password(env_path)
    data_dir = env_path.parent
    result = await create_backup(
        dsn,
        data_dir / "backups",
        reason="pre_migrate",
        password=password,
        env_path=env_path,
        media_dir=data_dir / "media",
    )
    return result.path
