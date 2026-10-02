"""Command line: ``python -m svbg <command>`` (also installed as ``svbg``).

Commands::

    run [--no-migrate]      migrate the database, then run the bot until SIGINT/SIGTERM
    migrate [--sql]         apply Alembic migrations (``--sql``: print the SQL, touch nothing)
    env init                create (or complete) data/.env with every setting, by sections
    env render [--stdout]   rewrite data/.env in the canonical form (``--stdout``: print it, secrets masked)
    set KEY=VALUE           change one line of data/.env atomically (the running bot applies it in ~2 s)
    health [--ready]        exit 0 if the local web server answers /health (or /ready)
    owner-link              print a one-time link that makes whoever opens it the owner
    show-key                print SECRET_KEY (host only — the bot never sends it to Telegram)
    backup / restore        database backup into data/backups; restore a backup into an empty database
    import bedolaga         import a Bedolaga database (dry_run → shadow → apply), report С0–…
    import settings         map a Bedolaga .env (+ system_settings) onto our settings (diff or --apply)
    cutover …, pay …        migration runbook: gates, rollback export, webhook, panel probe; pay freeze
    lte release             emergency release of the LTE blocks (bot or database down)

``migrate`` (and ``run``) makes a ``pre_migrate`` backup first when the schema is at an older revision; a
failed backup aborts the migration.

Exit codes: 0 ok, 1 failure, 2 usage / invalid value, 78 configuration error, 130 interrupted.
Nothing here prints secrets except ``show-key`` (explicitly) and ``env render --stdout --show-secrets``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib
import logging
import os
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final, TextIO

__all__ = ["EXIT_CONFIG", "EXIT_FAIL", "EXIT_INTERRUPTED", "EXIT_OK", "EXIT_USAGE", "main"]

EXIT_OK: Final = 0
EXIT_FAIL: Final = 1
EXIT_USAGE: Final = 2
EXIT_CONFIG: Final = 78
EXIT_INTERRUPTED: Final = 130


log = logging.getLogger("svbg.cli")


class CliError(Exception):
    def __init__(self, message: str, code: int = EXIT_FAIL) -> None:
        super().__init__(message)
        self.code = code


class _Io:
    def __init__(self, out: TextIO, err: TextIO) -> None:
        self.out = out
        self.err = err

    def say(self, text: str = "") -> None:
        self.out.write(text + "\n")
        self.out.flush()

    def warn(self, text: str) -> None:
        self.err.write(text + "\n")
        self.err.flush()


# --------------------------------------------------------------------------------------- helpers


def _env_path(environ: Mapping[str, str]) -> Path:
    from svbg.core.settings.bootstrap import default_env_path

    return default_env_path(environ)


def _bootstrap(environ: Mapping[str, str], *, generate_key: bool) -> Any:
    from svbg.core.settings import BootstrapError, load_bootstrap, read_bootstrap

    path = _env_path(environ)
    try:
        if generate_key:
            return load_bootstrap(path, environ)
        return read_bootstrap(path, environ)
    except BootstrapError as exc:
        raise CliError(str(exc), EXIT_CONFIG) from None


def _require_dsn(boot: Any) -> str:
    if not boot.database_url:
        raise CliError(f"DATABASE_URL не задан: впишите его в {boot.env_path} или в окружение", EXIT_CONFIG)
    return str(boot.database_url)


def _write_env(path: Path, text: str) -> None:
    from svbg.boot.envfile import write_atomic

    path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(path, text)


# --------------------------------------------------------------------------------------- commands


def _loop_factory() -> Any:
    """uvloop on Linux (faster I/O for the bot and asyncpg); the default loop elsewhere."""
    if sys.platform == "win32":
        return None
    try:
        import uvloop
    except ImportError:
        return None
    return uvloop.new_event_loop


def cmd_run(args: argparse.Namespace, environ: Mapping[str, str], io: _Io) -> int:
    from svbg.app import AppError, AppOptions, run

    if not args.no_migrate:
        code = cmd_migrate(argparse.Namespace(sql=False), environ, io, quiet=True)
        if code != EXIT_OK:
            return code
    try:
        options = AppOptions.from_environ(environ)
        asyncio.run(run(options), loop_factory=_loop_factory())
    except AppError as exc:
        io.warn(f"❌ {exc}")
        return EXIT_CONFIG
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    return EXIT_OK


def _migration_errors() -> tuple[type[BaseException], ...]:
    import asyncpg
    from alembic.util import CommandError
    from sqlalchemy.exc import SQLAlchemyError

    # ImportError: the SQLAlchemy asyncio extension without greenlet; RuntimeError: no DATABASE_URL in env.py.
    return (SQLAlchemyError, CommandError, asyncpg.PostgresError, ImportError, RuntimeError, ValueError)


def cmd_migrate(args: argparse.Namespace, environ: Mapping[str, str], io: _Io, *, quiet: bool = False) -> int:
    from svbg.core.log import mask
    from svbg.db import migrations

    if args.sql:
        io.say(migrations.offline_sql())
        return EXIT_OK
    boot = _bootstrap(environ, generate_key=False)
    dsn = _require_dsn(boot)
    migration_errors = _migration_errors()
    code = _backup_before_migrate(dsn, boot.env_path, environ, io)
    if code != EXIT_OK:
        return code
    try:
        asyncio.run(migrations.upgrade(dsn))
    except (OSError, ConnectionError) as exc:
        io.warn(f"❌ База данных недоступна: {mask(str(exc))[:300]}")
        return EXIT_FAIL
    except migration_errors as exc:
        io.warn(f"❌ Миграция не выполнена: {type(exc).__name__}: {mask(str(exc))[:500]}")
        return EXIT_FAIL
    if not quiet:
        io.say(f"✅ Схема базы данных актуальна ({migrations.head_revision()})")
    return EXIT_OK


def _backup_before_migrate(dsn: str, env_path: Path, environ: Mapping[str, str], io: _Io) -> int:
    """``pre_migrate`` backup (04 §10) when the schema is at an older revision; a failure aborts migrating.
    An unreachable database is left to the migration itself (it reports it the same way as before)."""
    from svbg.core.log import mask
    from svbg.db import migrations

    try:
        from svbg.ops.backup import BackupError
        from svbg.ops.cli import backup_before_migrate
    except ImportError:  # a build without the ops module: migrate as before
        return EXIT_OK
    try:
        path = asyncio.run(backup_before_migrate(dsn, env_path, environ, head=migrations.head_revision()))
    except BackupError as exc:
        io.warn(f"❌ Бэкап перед миграцией не сделан, миграция отменена: {exc}")
        return EXIT_FAIL
    except (*_migration_errors(), OSError, ConnectionError) as exc:
        log.info("pre-migrate backup skipped: %s", mask(f"{type(exc).__name__}: {exc}")[:200])
        return EXIT_OK
    if path is not None:
        io.say(f"💾 Бэкап перед миграцией: {path}")
    return EXIT_OK


def cmd_env_init(_args: argparse.Namespace, environ: Mapping[str, str], io: _Io) -> int:
    from svbg.app import render_env_offline
    from svbg.boot.envfile import read_text

    boot = _bootstrap(environ, generate_key=True)  # generates SECRET_KEY into the file when absent
    path = boot.env_path
    existed = boot.file_exists and "SECRET_KEY" not in boot.generated
    text = render_env_offline(path, environ)
    if read_text(path) != text:
        _write_env(path, text)
    io.say(f"{'✅ Дополнен' if existed else '✅ Создан'} файл настроек {path}")
    if "SECRET_KEY" in boot.generated:
        io.say(
            "🔑 Сгенерирован SECRET_KEY. Сохраните копию: svbg show-key (без него секреты не расшифровать)."
        )
    if not boot.has_token:
        io.say("Впишите BOT_TOKEN=<токен от @BotFather> — бот подхватит его без перезапуска.")
    return EXIT_OK


# Names of keys that hold secrets even when the registry does not know them (the owner's own lines,
# e.g. POSTGRES_PASSWORD, SOME_API_KEY, WEBHOOK_SECRET, *_DSN).
_SECRET_NAME_RE: Final = re.compile(
    r"(?i)passw|secret|token|api[_\-.]?key|private[_\-.]?key|access[_\-.]?key|credential|(?:^|[_\-.])key$|"
    r"(?:^|[_\-.])(?:dsn|pwd|pass|auth|cookie|session)$"
)
# A commented-out assignment ("# POSTGRES_PASSWORD=..."): masked like a live line.
_COMMENTED_KV_RE: Final = re.compile(
    r"^(?P<head>\s*#+\s*(?:export\s+)?)(?P<key>[A-Za-z_][A-Za-z0-9_.\-]*)(?P<sep>\s*=\s*)(?P<val>.*)$"
)


def _mask_env_text(text: str) -> str:
    """The rendered ``.env`` for the terminal: every secret value replaced, layout and other values kept.

    Registry secrets → the "stored encrypted" placeholder; unknown keys with a secret-looking name → ``***``;
    every other value (and commented-out assignments) goes through :func:`svbg.core.log.mask`, which hides
    URL passwords, bot tokens and ``key=value`` secrets. Broken lines of a secret key are hidden entirely.
    """
    from svbg.app_modules import full_registry
    from svbg.boot.envfile import EnvDocument, EnvLine, quote
    from svbg.core.log import MASK, mask
    from svbg.core.settings.values import SECRET_PLACEHOLDER

    registry = full_registry()
    doc = EnvDocument.parse(text)

    def secret_replacement(key: str) -> str | None:
        defn = registry.find(key)
        if defn is not None:
            return SECRET_PLACEHOLDER if defn.is_secret else None
        return MASK if _SECRET_NAME_RE.search(key) else None

    for index, line in enumerate(doc.lines):
        if line.kind == "kv" and line.key is not None:
            value = line.value or ""
            if not value:
                continue
            masked = secret_replacement(line.key) or mask(value)
            if masked != value:
                doc.lines[index] = EnvLine("kv", f"{line.key}={quote(masked)}", line.key, masked, line.eol)
        elif line.kind == "invalid":
            if line.key is not None and secret_replacement(line.key) is not None:
                raw = f"{line.key}={MASK}"
            else:
                raw = mask(line.raw)
            doc.lines[index] = EnvLine("invalid", raw, line.key, None, line.eol)
        elif line.kind == "comment":
            m = _COMMENTED_KV_RE.match(line.raw)
            if m is None or not m["val"].strip():
                continue
            hidden = secret_replacement(m["key"])
            value = hidden if hidden is not None else mask(m["val"])
            if value != m["val"]:
                raw = f"{m['head']}{m['key']}{m['sep']}{value}"
                doc.lines[index] = EnvLine("comment", raw, line.key, line.value, line.eol)
    return doc.render()


def cmd_env_render(args: argparse.Namespace, environ: Mapping[str, str], io: _Io) -> int:
    from svbg.app import render_env_offline
    from svbg.boot.envfile import read_text

    path = _env_path(environ)
    text = render_env_offline(path, environ)
    if args.stdout:
        if not args.show_secrets:
            text = _mask_env_text(text)
        io.out.write(text)
        io.out.flush()
        return EXIT_OK
    if read_text(path) == text:
        io.say(f"Файл {path} уже в актуальном виде")
    else:
        _write_env(path, text)
        io.say(f"✅ Файл {path} переписан в актуальном виде (значения сохранены)")
    return EXIT_OK


def _split_assignment(items: Sequence[str]) -> tuple[str, str]:
    if len(items) == 1 and "=" in items[0]:
        key, _, value = items[0].partition("=")
    elif len(items) == 2:
        key, value = items
    else:
        raise CliError("Использование: svbg set KEY=VALUE  (или: svbg set KEY VALUE)", EXIT_USAGE)
    key = key.strip()
    if key.startswith("export "):
        key = key[len("export ") :].strip()
    return key, value


def cmd_set(args: argparse.Namespace, environ: Mapping[str, str], io: _Io) -> int:
    from svbg.app_modules import full_registry
    from svbg.boot.envfile import EnvDocument, EnvFileError, read_text
    from svbg.core.settings import values as setting_values
    from svbg.core.settings.bootstrap import read_bootstrap
    from svbg.core.settings.envtext import comment_lines

    key, raw = _split_assignment(args.assignment)
    registry = full_registry()
    defn = registry.find(key)
    if defn is None:
        hints = [d.key for d in registry.all() if key.upper() in d.key]
        tail = f" Похожие: {', '.join(hints[:5])}" if hints else ""
        raise CliError(f"Неизвестная настройка {key}.{tail}", EXIT_USAGE)
    if defn.readonly:
        raise CliError(
            f"{defn.key} не меняется правкой (только через окружение/процедуру ротации)", EXIT_USAGE
        )
    if not defn.in_file:
        raise CliError(f"{defn.key} не хранится в .env", EXIT_USAGE)
    if raw.strip():
        try:
            setting_values.parse_or_default(defn, raw)
        except setting_values.SettingValueError as exc:
            raise CliError(f"Значение для {defn.key} не подходит: {exc}", EXIT_USAGE) from None
    path = _env_path(environ)
    try:
        text = read_text(path)
    except (EnvFileError, OSError) as exc:
        raise CliError(f"Файл {path} не читается: {exc}") from None
    doc = EnvDocument.parse(text or "")
    for alias in defn.aliases:
        if doc.get(alias) is not None:
            doc.remove(alias)
    doc.set(defn.key, raw, comment=comment_lines(defn), section=registry.section_title(defn.section))
    try:
        _write_env(path, doc.render())
    except OSError as exc:
        raise CliError(f"Не удалось записать {path}: {exc.strerror or type(exc).__name__}") from None
    shown = "(секрет)" if defn.is_secret else (raw if raw.strip() else "по умолчанию")
    io.say(f"✅ {defn.key} = {shown} записано в {path}")
    boot = read_bootstrap(path, environ, registry=registry)
    if defn.key in boot.locked:
        io.warn(f"⚠️ {defn.key} задан окружением контейнера (LOCKED_KEYS): значение из файла не применится")
    elif defn.apply.value == "restart":
        io.say("♻️ Вступит в силу после перезапуска бота")
    else:
        io.say("Запущенный бот применит изменение за ~2 с")
    return EXIT_OK


def cmd_health(args: argparse.Namespace, environ: Mapping[str, str], io: _Io) -> int:
    from aiohttp import ClientError, ClientSession, ClientTimeout

    from svbg.app import AppError, AppOptions

    try:
        options = AppOptions.from_environ(environ)
    except AppError as exc:
        io.warn(str(exc))
        return EXIT_CONFIG
    path = "/ready" if args.ready else "/health"
    url = f"http://127.0.0.1:{options.web_port}{path}"

    async def probe() -> tuple[int, str]:
        async with (
            ClientSession(timeout=ClientTimeout(total=args.timeout)) as session,
            session.get(url) as resp,
        ):
            return resp.status, (await resp.text())[:500]

    try:
        status, body = asyncio.run(probe())
    except (ClientError, TimeoutError, OSError) as exc:
        io.warn(f"unhealthy: {type(exc).__name__}")
        return EXIT_FAIL
    if status != 200:
        io.warn(f"unhealthy: HTTP {status} {body}")
        return EXIT_FAIL
    io.say(body)
    return EXIT_OK


def _database(dsn: str) -> Any:
    """The application's database class (tests replace this factory)."""
    from svbg.app import _default_database

    return _default_database(dsn)


async def _create_owner_link(dsn: str, bot_username: str | None) -> Any:
    from svbg.tg.setup.owner import create_owner_link

    db = _database(dsn)
    await db.start()
    try:
        return await create_owner_link(db, bot_username=bot_username)
    finally:
        await db.close()


async def _bot_username(boot: Any) -> str | None:
    """``getMe`` with the configured token/proxy/API URL; ``None`` if Telegram cannot be reached."""
    from aiogram import Bot
    from aiogram.client.session.aiohttp import AiohttpSession
    from aiogram.client.telegram import PRODUCTION, TelegramAPIServer
    from aiogram.exceptions import TelegramAPIError

    from svbg.core.component import ProbeError
    from svbg.tg.notifier import TRANSPORT_ERRORS
    from svbg.tg.runner import BotConfig

    try:
        cfg = BotConfig.from_mapping(boot.values)
    except ProbeError:
        return None
    api = TelegramAPIServer.from_base(cfg.api_url) if cfg.api_url else PRODUCTION
    session = AiohttpSession(api=api, proxy=cfg.proxy) if cfg.proxy else AiohttpSession(api=api)
    try:
        bot = Bot(cfg.token, session=session)
        async with asyncio.timeout(10):
            me = await bot.get_me()
        return me.username
    except (TelegramAPIError, TimeoutError, ValueError, *TRANSPORT_ERRORS):
        return None
    finally:
        await session.close()


def cmd_owner_link(args: argparse.Namespace, environ: Mapping[str, str], io: _Io) -> int:
    from sqlalchemy.exc import SQLAlchemyError

    from svbg.core.log import mask

    boot = _bootstrap(environ, generate_key=False)
    dsn = _require_dsn(boot)
    username = args.username or (asyncio.run(_bot_username(boot)) if boot.has_token else None)
    try:
        link = asyncio.run(_create_owner_link(dsn, username))
    except ImportError as exc:
        io.warn(f"❌ Ссылка владельца недоступна в этой сборке: {exc.name or exc}")
        return EXIT_FAIL
    except (OSError, ConnectionError, SQLAlchemyError) as exc:
        io.warn(f"❌ База данных недоступна или не создана (svbg migrate): {mask(str(exc))[:300]}")
        return EXIT_FAIL
    if link.url:
        io.say(link.url)
    else:
        io.say(f"https://t.me/<username_бота>?start={link.payload}")
        io.warn("Не удалось узнать имя бота: подставьте его вручную (или передайте --username).")
    io.warn(
        f"Ссылка одноразовая, действует до {link.expires_at:%Y-%m-%d %H:%M} UTC. "
        "Кто первым откроет её, станет владельцем. Новая ссылка отменяет прежнюю."
    )
    return EXIT_OK


def cmd_show_key(_args: argparse.Namespace, environ: Mapping[str, str], io: _Io) -> int:
    boot = _bootstrap(environ, generate_key=False)
    key = boot.secret_key
    if not key:
        io.warn(f"SECRET_KEY ещё не создан: выполните svbg env init (файл {boot.env_path})")
        return EXIT_FAIL
    io.say(str(key))
    io.warn(
        "Храните ключ в менеджере паролей. Без него секреты из бэкапа не расшифровать. Никому не пересылайте."
    )
    return EXIT_OK


# --------------------------------------------------------------------------------------- import

_ENV_IMPORT_SOURCE: Final = "SVBG_IMPORT_SOURCE_DSN"


def _source_dsn(args: argparse.Namespace, environ: Mapping[str, str], *, required: bool = True) -> str | None:
    """The Bedolaga database: ``--source-dsn-file`` (preferred: not visible in the process list), the
    ``SVBG_IMPORT_SOURCE_DSN`` environment variable or ``--source-dsn``."""
    path = getattr(args, "source_dsn_file", None)
    if path:
        try:
            value = Path(path).read_text("utf-8").strip()
        except OSError as exc:
            raise CliError(
                f"--source-dsn-file: файл не читается ({exc.strerror or exc})", EXIT_USAGE
            ) from None
        if not value:
            raise CliError("--source-dsn-file: файл пуст", EXIT_USAGE)
        return value
    value = environ.get(_ENV_IMPORT_SOURCE) or getattr(args, "source_dsn", None)
    if not value and required:
        raise CliError(
            f"нужен адрес копии БД Bedolaga: --source-dsn-file, {_ENV_IMPORT_SOURCE} или --source-dsn",
            EXIT_USAGE,
        )
    return str(value) if value else None


def _import_errors() -> tuple[type[BaseException], ...]:
    return (*_migration_errors(), OSError, ConnectionError)


def cmd_import_bedolaga(args: argparse.Namespace, environ: Mapping[str, str], io: _Io) -> int:
    """``svbg import bedolaga``: ``dry_run`` (default, counts only) → ``shadow`` → ``apply`` (06 §4)."""
    import json

    from svbg.core.crypto import Crypto
    from svbg.core.log import mask
    from svbg.importers.bedolaga import (
        ApiPanelReader,
        BedolagaImporter,
        ImportConfig,
        config_from_env_file,
        render_text,
    )
    from svbg.importers.cutover import _panel_api

    boot = _bootstrap(environ, generate_key=False)
    dsn = _require_dsn(boot)
    source = _source_dsn(args, environ)
    assert source is not None
    crypto = Crypto([boot.secret_key]) if boot.secret_key else None
    if args.env:
        config = config_from_env_file(args.env, crypto=crypto)
    else:
        config = ImportConfig(crypto=crypto)
        io.warn("⚠️ Без --env (файл .env Bedolaga) часть настроек импорта берётся по умолчанию")

    async def run() -> Any:
        db = _database(dsn)
        await db.start()
        try:
            if args.mode == "dry_run":
                return await BedolagaImporter(db, source, config=config).run("dry_run")
            async with _panel_api(args, environ) as api:
                return await BedolagaImporter(db, source, panel=ApiPanelReader(api), config=config).run(
                    args.mode
                )
        finally:
            await db.close()

    try:
        report = asyncio.run(run())
    except _import_errors() as exc:
        io.warn(f"❌ Импорт не выполнен: {type(exc).__name__}: {mask(str(exc))[:500]}")
        return EXIT_FAIL
    io.say(render_text(report))
    if args.json:
        Path(args.json).write_text(
            json.dumps(report.as_json(), ensure_ascii=False, indent=2, default=str), "utf-8"
        )
        io.say(f"Отчёт JSON: {args.json}")
    if args.mode == "apply":
        io.say("Страницы (FAQ, правила, оферта) бот перечитает при старте.")
    return EXIT_OK if report.green else EXIT_FAIL


def cmd_import_settings(args: argparse.Namespace, environ: Mapping[str, str], io: _Io) -> int:
    """``svbg import settings``: the Bedolaga ``.env`` (+ ``system_settings`` of its database) → our settings.

    Without ``--apply``: what WOULD change (secrets: only «отличается»). With ``--apply``: one settings batch
    (``source=import``, undo = one button in the bot), then optionally the catalog, admin topics, CDN nodes.
    Run it with the bot stopped (T0 runbook, 06 §4.4): the bot reads the result at its next start.
    """
    from svbg.app_modules import full_registry
    from svbg.core.component import ComponentRegistry
    from svbg.core.crypto import Crypto
    from svbg.core.log import mask
    from svbg.core.settings import SettingsService
    from svbg.importers.bedolaga.settings_map import (
        BedolagaSettingsSource,
        apply_catalog,
        apply_cdn_nodes,
        apply_settings,
        apply_topics,
        build_plan,
        diff_against,
        read_source_tables,
    )
    from svbg.importers.shadow import open_readonly

    boot = _bootstrap(environ, generate_key=False)
    dsn = _require_dsn(boot)
    if not boot.secret_key:
        raise CliError("SECRET_KEY не задан: выполните svbg env init", EXIT_CONFIG)
    source = _source_dsn(args, environ, required=False)
    deferred: bool | list[str] = list(args.deferred) if args.deferred else False

    async def run() -> int:
        tables: dict[str, Any] = {}
        if source:
            conn = await open_readonly(source)
            try:
                tables = await read_source_tables(conn)
            finally:
                await conn.close()
        try:
            plan = build_plan(BedolagaSettingsSource.from_env_file(args.env, **tables))
        except OSError as exc:
            raise CliError(f"--env: файл не читается ({exc.strerror or exc})", EXIT_USAGE) from None
        db = _database(dsn)
        await db.start()
        try:
            settings = SettingsService(
                db,
                full_registry(),
                Crypto([boot.secret_key]),
                ComponentRegistry(),
                environ=environ,
                env_path=boot.env_path,
            )
            await settings.load()
            if not args.apply:
                for row in diff_against(settings, plan):
                    if row["status"] == "same":
                        continue
                    shown = f"{row.get('old', '…')} → {row.get('new', '…')}" if "new" in row else ""
                    io.say(f"{row['status']:>11}  {row['key']} {shown}".rstrip())
                text = plan.render_not_transferred()
                if text:
                    io.say(text)
                for warning in plan.warnings:
                    io.warn(f"⚠️ {warning}")
                io.say("Проверка без записи. Применить: svbg import settings --env … --apply")
                return EXIT_OK
            result = await apply_settings(settings, plan, include_deferred=deferred)
            io.say(f"✅ Применено: {len(result.applied)}, без изменений: {len(result.unchanged)}")
            for item in result.not_transferred:
                io.say(f"  не перенесено: {item.key} — {item.reason}")
            if result.restart_required:
                io.say("♻️ Часть настроек вступит в силу после перезапуска бота")
            if args.catalog:
                currency = str(settings.current()["CURRENCY"] or "RUB")
                async with db.tx() as conn:
                    cat = await apply_catalog(conn, plan.catalog, currency=currency)
                io.say(f"Каталог: {cat.skipped or ('изменён' if cat.changed else 'без изменений')}")
            if args.topics:
                async with db.tx() as conn:
                    topics = await apply_topics(conn, plan.topics)
                io.say(f"Темы админ-чата: {', '.join(topics) or 'без изменений'}")
            if args.cdn_nodes:
                async with db.tx() as conn:
                    nodes = await apply_cdn_nodes(conn, plan.cdn_nodes)
                io.say(f"CDN-ноды IP Guard: {nodes if nodes is not None else 'модуль не установлен'}")
            return EXIT_OK
        finally:
            await db.close()

    try:
        return asyncio.run(run())
    except _import_errors() as exc:
        io.warn(f"❌ Импорт настроек не выполнен: {type(exc).__name__}: {mask(str(exc))[:500]}")
        return EXIT_FAIL


def _add_import_commands(sub: Any) -> None:
    p_imp = sub.add_parser("import", help="переезд с Bedolaga: данные и настройки")
    imp = p_imp.add_subparsers(dest="import_command", required=True, metavar="what")

    p = imp.add_parser("bedolaga", help="импорт базы Bedolaga: dry_run → shadow → apply")
    p.add_argument("--mode", choices=["dry_run", "shadow", "apply"], default="dry_run")
    p.add_argument("--source-dsn-file", help=f"файл с DSN копии БД Bedolaga (или {_ENV_IMPORT_SOURCE})")
    p.add_argument("--source-dsn", help="DSN копии БД Bedolaga (виден в списке процессов: лучше файл)")
    p.add_argument("--env", help="файл .env Bedolaga (настройки импорта)")
    p.add_argument("--json", help="записать отчёт в JSON-файл")
    p.add_argument("--panel-url", help="адрес панели (иначе REMNAWAVE_URL из data/.env)")
    p.add_argument("--token-file", help="файл с токеном панели (shadow: только чтение)")
    p.set_defaults(handler=cmd_import_bedolaga)

    p = imp.add_parser("settings", help="настройки из .env Bedolaga (проверка или --apply)")
    p.add_argument("--env", required=True, help="файл .env Bedolaga")
    p.add_argument("--source-dsn-file", help=f"файл с DSN копии БД Bedolaga (или {_ENV_IMPORT_SOURCE})")
    p.add_argument("--source-dsn", help="DSN копии БД Bedolaga (system_settings, каналы)")
    p.add_argument("--apply", action="store_true", help="записать (иначе только показать разницу)")
    p.add_argument("--deferred", nargs="*", metavar="KEY", help="включить отложенные ключи (шаги 8, 9, 12)")
    p.add_argument("--catalog", action="store_true", help="с --apply: тариф и цены Bedolaga")
    p.add_argument("--topics", action="store_true", help="с --apply: темы админ-чата")
    p.add_argument("--cdn-nodes", action="store_true", help="с --apply: CDN-ноды IP Guard")
    p.set_defaults(handler=cmd_import_settings)


def _add_module_commands(sub: Any) -> None:
    """Commands of the ops, cutover and LTE modules; a module missing from the build is skipped."""
    for name in ("svbg.ops.cli", "svbg.importers.cutover", "svbg.ext.lte.cli"):
        try:
            module = importlib.import_module(name)
        except ImportError as exc:
            log.warning("commands of %s are not available: %s", name, exc)
            continue
        module.add_commands(sub)


# --------------------------------------------------------------------------------------- parser


#: Commands of modules (heavier imports: loaded only when one of them is called, or for the help).
MODULE_COMMANDS: Final = frozenset({"backup", "restore", "cutover", "pay", "lte"})


def build_parser(argv: Sequence[str] | None = None) -> argparse.ArgumentParser:
    """The CLI. With ``argv`` whose command is a core one, the module commands are not imported (a fast
    ``svbg health`` for the Docker healthcheck)."""
    parser = argparse.ArgumentParser(prog="svbg", description="SvBG Shop — Telegram-бот продажи VPN-подписок")
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    p_run = sub.add_parser("run", help="запустить бота (с миграциями)")
    p_run.add_argument("--no-migrate", action="store_true", help="не применять миграции перед стартом")
    p_run.set_defaults(handler=cmd_run)

    p_mig = sub.add_parser("migrate", help="применить миграции базы данных")
    p_mig.add_argument("--sql", action="store_true", help="только напечатать SQL")
    p_mig.set_defaults(handler=cmd_migrate)

    p_env = sub.add_parser("env", help="файл настроек data/.env")
    env_sub = p_env.add_subparsers(dest="env_command", required=True, metavar="action")
    p_init = env_sub.add_parser("init", help="создать/дополнить полный .env")
    p_init.set_defaults(handler=cmd_env_init)
    p_render = env_sub.add_parser("render", help="переписать .env в актуальном виде")
    p_render.add_argument("--stdout", action="store_true", help="напечатать вместо записи")
    p_render.add_argument("--show-secrets", action="store_true", help="с --stdout: не скрывать секреты")
    p_render.set_defaults(handler=cmd_env_render)

    p_set = sub.add_parser("set", help="изменить одну настройку в .env: KEY=VALUE")
    p_set.add_argument("assignment", nargs="+", metavar="KEY=VALUE")
    p_set.set_defaults(handler=cmd_set)

    p_health = sub.add_parser("health", help="проверка живости для Docker healthcheck")
    p_health.add_argument("--ready", action="store_true", help="проверять /ready вместо /health")
    p_health.add_argument("--timeout", type=float, default=5.0)
    p_health.set_defaults(handler=cmd_health)

    p_owner = sub.add_parser("owner-link", help="одноразовая ссылка владельца")
    p_owner.add_argument("--username", help="имя бота без @ (если Telegram недоступен)")
    p_owner.set_defaults(handler=cmd_owner_link)

    p_key = sub.add_parser("show-key", help="показать SECRET_KEY (только на хосте)")
    p_key.set_defaults(handler=cmd_show_key)

    first = next(iter(argv), None) if argv is not None else None
    if first is None or first.startswith("-") or first in MODULE_COMMANDS:
        _add_module_commands(sub)  # backup, restore, cutover, pay, lte
    _add_import_commands(sub)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    configure_logging: bool = True,
) -> int:
    io = _Io(stdout or sys.stdout, stderr or sys.stderr)
    for stream in (io.out, io.err):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]  # Windows consoles: cp1251/cp866
    parser = build_parser(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(argv)
    env = dict(os.environ if environ is None else environ)
    if configure_logging and args.command != "run":
        from svbg.core.log import setup_logging

        setup_logging(env.get("LOG_LEVEL", "WARNING") or "WARNING", use_queue=False, stream=io.err)
    try:
        return int(args.handler(args, env, io))
    except CliError as exc:
        io.warn(f"❌ {exc}")
        return exc.code
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    sys.exit(main())
