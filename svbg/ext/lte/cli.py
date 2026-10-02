"""``svbg lte release`` — emergency release of LTE blocks from the host (05 §2.1.6), for when the bot is down.

::

    svbg lte release [--due-only | --all | --panel-only] [--dry-run] [--yes]
                     [--base-map TWIN=BASE …] [--twin-suffix TEXT] [--url URL --token-file FILE]

* ``--due-only`` (default) — blocks of periods whose reset has already come (the planned end passed or the
  period is closed);
* ``--all`` — every live block;

  both work on the database: the blocks go to ``releasing``, their substitution rows are deleted and the core
  writer is queued (it applies the base squads at the bot's start; the confirm job and the re-send in 150 s
  follow). The module's twin map rows no longer used are dropped too, so the base squads are written even if
  the module stays switched off.
* ``--panel-only`` — without the database: the "twin → base" map comes from the panel itself (a twin's
  inbounds are a proper subset of exactly one base; ``--twin-suffix`` marks twins by name, ``--base-map``
  pins pairs) and every panel user showing a twin gets the base back (never an empty squad set; ambiguous
  twins are skipped). Afterwards, before the bot starts: ``svbg set LTE_ENFORCE=off`` and ``svbg lte release
  --all`` once the database is back, otherwise the bot re-applies its blocks.

Exit codes follow ``svbg.__main__``: 0 ok, 1 failure, 2 usage, 78 configuration.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

import sqlalchemy as sa

if TYPE_CHECKING:
    from svbg.db.engine import Database
    from svbg.remnawave.api import RemnawaveApi

__all__ = [
    "PanelReport",
    "ReleaseReport",
    "add_commands",
    "cmd_release",
    "panel_desired",
    "panel_twin_bases",
    "release_db",
    "release_panel",
]

EXIT_OK: Final = 0
EXIT_FAIL: Final = 1
EXIT_USAGE: Final = 2
EXIT_CONFIG: Final = 78
CONFIRM_WORD: Final = "СНЯТЬ"
Mode = Literal["due", "all", "panel"]


class _Io(Protocol):
    def say(self, text: str = "") -> None: ...

    def warn(self, text: str) -> None: ...


class _Fail(Exception):
    def __init__(self, message: str, code: int = EXIT_FAIL) -> None:
        super().__init__(message)
        self.code = code


# -------------------------------------------------------------------------------------------- database


@dataclass(frozen=True, slots=True)
class ReleaseReport:
    mode: str
    blocks: int
    subscriptions: int
    dry_run: bool = False
    twins_dropped: int = 0


async def release_db(db: Database, *, mode: Literal["due", "all"], dry_run: bool = False) -> ReleaseReport:
    """Release live blocks in the database (the writer applies them); ``dry_run`` only counts."""
    from svbg.core.clock import now
    from svbg.ext.lte.admin import drop_unused_twin_map
    from svbg.ext.lte.enforce import Enforcer
    from svbg.ext.lte.service import audit, kv_put
    from svbg.ext.lte.tables import lte_blocks, lte_periods

    at = now()
    b, p = lte_blocks.c, lte_periods.c
    q = sa.select(sa.func.count(), sa.func.count(sa.distinct(b.subscription_id))).where(b.status == "active")
    if mode == "due":
        q = q.where(
            sa.exists().where(p.id == b.period_id, sa.or_(p.state == "closed", p.planned_end_at <= at))
        )
    async with db.read() as conn:
        blocks, subs = (await conn.execute(q)).one()
    if dry_run:
        return ReleaseReport(mode, int(blocks or 0), int(subs or 0), dry_run=True)
    released = await Enforcer(db).release_all("emergency", due_only=mode == "due", at=at)
    async with db.tx() as conn:
        dropped = await drop_unused_twin_map(conn)
        await kv_put(conn, "emergency", {"mode": mode, "at": at.isoformat(), "released": released})
        await audit(
            conn, None, "lte.cli_release", "lte", reason=f"svbg lte release --{mode}", details={"n": released}
        )
    return ReleaseReport(mode, released, int(subs or 0), twins_dropped=dropped)


# ---------------------------------------------------------------------------------------------- panel


def panel_twin_bases(
    squads: Mapping[str, tuple[str, frozenset[str]]],
    *,
    base_map: Mapping[str, str] | None = None,
    twin_suffix: str | None = None,
) -> tuple[dict[str, str], list[str]]:
    """``twin → base`` from the panel: ``squads`` is ``uuid → (name, inbound ids)``.

    A twin is a squad pinned in ``base_map`` or whose name contains ``twin_suffix``; its base is the only
    squad whose inbounds strictly contain the twin's. Ambiguous twins are reported and skipped.
    """
    explicit = {k.lower(): v.lower() for k, v in (base_map or {}).items()}
    known = {u.lower() for u in squads}
    bases: dict[str, str] = {}
    warnings: list[str] = []
    for twin, base in explicit.items():
        if twin not in known or base not in known:
            warnings.append(f"--base-map {twin}={base}: такого сквада нет в панели — пропуск")
        else:
            bases[twin] = base
    suffix = (twin_suffix or "").strip().lower()
    if not suffix:
        return bases, warnings
    for uuid, (name, inbounds) in squads.items():
        key = uuid.lower()
        if key in bases or suffix not in name.lower():
            continue
        candidates = [
            other.lower()
            for other, (other_name, other_in) in squads.items()
            if other.lower() != key and suffix not in other_name.lower() and inbounds < other_in
        ]
        if len(candidates) == 1:
            bases[key] = candidates[0]
        else:
            warnings.append(f"двойник «{name}»: база не определена ({len(candidates)} кандидатов) — пропуск")
    return bases, warnings


def panel_desired(actual: Sequence[str], bases: Mapping[str, str]) -> list[str] | None:
    """The user's squads with every twin replaced by its base; ``None`` — nothing to change (or it would be
    empty: an empty ``activeInternalSquads`` is never written)."""
    out: list[str] = []
    changed = False
    for item in actual:
        base = bases.get(item.lower())
        value = base if base is not None else item
        changed |= base is not None
        if value not in out:
            out.append(value)
    return out if changed and out else None


@dataclass(slots=True)
class PanelReport:
    users: int = 0
    patched: int = 0
    failed: int = 0
    warnings: list[str] = field(default_factory=list)
    bases: dict[str, str] = field(default_factory=dict)


#: ``write(api, panel_user_id, squads)`` — the out-of-band squads write of the panel-only mode.
SquadsWrite = Callable[["RemnawaveApi", int, list[str]], Awaitable[None]]


def emergency_writer() -> SquadsWrite | None:
    """The core writer's out-of-band squads write (``svbg.remnawave.writer.emergency_set_squads``): the only
    panel mutation allowed without the outbox, for this command when the database is down. ``None`` until the
    integration adds it — the command then only reports what it would do."""
    from svbg.remnawave import writer

    fn = getattr(writer, "emergency_set_squads", None)
    return fn if callable(fn) else None


async def release_panel(
    api: RemnawaveApi,
    *,
    base_map: Mapping[str, str] | None = None,
    twin_suffix: str | None = None,
    dry_run: bool = False,
    write: SquadsWrite | None = None,
) -> PanelReport:
    """Give every panel user showing a twin its base squad back (no database)."""
    import msgspec

    from svbg.ext.lte.collector import parse_squads
    from svbg.remnawave.errors import RemnawaveError
    from svbg.remnawave.transport import Lane

    raw = await api.transport.request(
        "GET", "/internal-squads", idempotent=True, scope="internal-squads:list", lane=Lane.BACKGROUND
    )
    data = msgspec.json.decode(raw.body) if raw.body else {}
    inbounds, names = parse_squads(data.get("response") if isinstance(data, dict) else None)
    squads = {uuid: (names.get(uuid, ""), ins) for uuid, ins in inbounds.items()}
    report = PanelReport()
    report.bases, report.warnings = panel_twin_bases(squads, base_map=base_map, twin_suffix=twin_suffix)
    if not report.bases:
        report.warnings.append("карта «двойник → база» пуста: укажите --twin-suffix или --base-map")
        return report
    write = write or emergency_writer()
    if write is None and not dry_run:
        report.warnings.append("запись в панель без бота ещё не подключена в этой сборке — только отчёт")
        dry_run = True
    async for page in api.iter_users(500):
        for user in page.users:
            target = panel_desired(user.squad_uuids, report.bases)
            if target is None:
                continue
            report.users += 1
            if dry_run or write is None:
                continue
            try:
                await write(api, user.id, target)
                report.patched += 1
            except RemnawaveError as err:
                report.failed += 1
                report.warnings.append(f"пользователь {user.id}: {err.kind.value}")
    return report


# ------------------------------------------------------------------------------------------------ cli


def _boot(environ: Mapping[str, str]) -> Any:
    from svbg.core.settings import BootstrapError, read_bootstrap
    from svbg.core.settings.bootstrap import default_env_path

    try:
        return read_bootstrap(default_env_path(environ), environ)
    except BootstrapError as exc:
        raise _Fail(str(exc), EXIT_CONFIG) from None


def _base_map(items: Sequence[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items or ():
        twin, sep, base = item.partition("=")
        if not sep or not twin.strip() or not base.strip():
            raise _Fail(f"--base-map ждёт ДВОЙНИК=БАЗА, а не {item!r}", EXIT_USAGE)
        out[twin.strip()] = base.strip()
    return out


def _panel_settings(args: argparse.Namespace, boot: Any) -> dict[str, Any]:
    from svbg.boot.envfile import EnvDocument, read_text
    from svbg.core.settings.values import SECRET_PLACEHOLDER

    doc = EnvDocument.parse(read_text(boot.env_path) or "")
    cfg: dict[str, Any] = {
        k: v
        for k in ("REMNAWAVE_URL", "REMNAWAVE_TOKEN", "REMNAWAVE_CADDY_TOKEN", "REMNAWAVE_COOKIE")
        if (v := doc.get(k)) and v != SECRET_PLACEHOLDER
    }
    if args.url:
        cfg["REMNAWAVE_URL"] = args.url
    if args.token_file:
        try:
            token = Path(args.token_file).read_text("utf-8").strip()
        except OSError as exc:
            raise _Fail(
                f"файл токена не читается: {exc.strerror or type(exc).__name__}", EXIT_USAGE
            ) from None
        if not token:
            raise _Fail("файл токена пуст", EXIT_USAGE)
        cfg["REMNAWAVE_TOKEN"] = token
    return cfg


def _dsn(args: argparse.Namespace, boot: Any) -> str:
    dsn = args.dsn or boot.database_url
    if not dsn:
        raise _Fail(f"DATABASE_URL не задан: впишите его в {boot.env_path}", EXIT_CONFIG)
    return str(dsn)


def _confirm(args: argparse.Namespace, io: _Io, what: str) -> None:
    if args.yes or args.dry_run:
        return
    answer = None
    if sys.stdin and sys.stdin.isatty():
        io.say(f"{what}. Чтобы продолжить, введите {CONFIRM_WORD}:")
        try:
            answer = input().strip().upper()
        except EOFError:
            answer = None
    if answer != CONFIRM_WORD:
        raise _Fail("отменено: нужно подтверждение (или --yes)", EXIT_USAGE)


async def _run_db(dsn: str, mode: Literal["due", "all"], dry_run: bool) -> ReleaseReport:
    from svbg.db.engine import Database

    db = Database(dsn)
    await db.start()
    try:
        return await release_db(db, mode=mode, dry_run=dry_run)
    finally:
        await db.close()


async def _run_panel(cfg: Mapping[str, Any], args: argparse.Namespace) -> PanelReport:
    from svbg.remnawave.api import RemnawaveApi
    from svbg.remnawave.transport import Transport, TransportConfig

    try:
        tcfg = TransportConfig.from_settings(cfg)
    except ValueError as exc:
        raise _Fail(str(exc), EXIT_CONFIG) from None
    if tcfg is None:
        raise _Fail("панель не настроена: нет REMNAWAVE_URL / REMNAWAVE_TOKEN (или --url и --token-file)", 78)
    transport = Transport(tcfg)
    try:
        return await release_panel(
            RemnawaveApi(transport),
            base_map=_base_map(args.base_map),
            twin_suffix=args.twin_suffix,
            dry_run=args.dry_run,
        )
    finally:
        await transport.aclose()


def cmd_release(args: argparse.Namespace, environ: Mapping[str, str], io: _Io) -> int:
    mode: Mode = args.mode
    try:
        boot = _boot(environ)
        if mode == "panel":
            cfg = _panel_settings(args, boot)
            _confirm(args, io, "Все пользователи панели с двойником LTE получат базовый сквад")
            panel = asyncio.run(_run_panel(cfg, args))
            for note in panel.warnings:
                io.warn(f"⚠️ {note}")
            verb = "нужно вернуть" if args.dry_run else "возвращено"
            io.say(f"✅ Двойников в карте: {len(panel.bases)}; {verb}: {panel.users}; ошибок: {panel.failed}")
            if not args.dry_run:
                io.warn(
                    "⚠️ До старта бота: svbg set LTE_ENFORCE=off, "
                    "а когда база доступна — svbg lte release --all"
                )
            return EXIT_FAIL if panel.failed else EXIT_OK
        dsn = _dsn(args, boot)
        _confirm(args, io, "Блоки LTE будут сняты" + (" все" if mode == "all" else " (сброс уже наступил)"))
        report = asyncio.run(_run_db(dsn, mode, bool(args.dry_run)))
    except _Fail as exc:
        io.warn(f"❌ {exc}")
        return exc.code
    except Exception as exc:  # noqa: BLE001 - the operator sees a short reason, never a traceback with data
        io.warn(f"❌ Не удалось: {type(exc).__name__}")
        return EXIT_FAIL
    if report.dry_run:
        io.say(f"Будет снято блоков: {report.blocks} (подписок: {report.subscriptions}). Ничего не изменено.")
        return EXIT_OK
    io.say(
        f"✅ Снято блоков: {report.blocks}. Бот применит это в панели через писатель (сразу или при старте)."
    )
    if mode == "all":
        io.warn("⚠️ Чтобы бот не поставил блоки снова: svbg set LTE_ENFORCE=shadow (или off)")
    return EXIT_OK


def add_commands(sub: Any) -> None:
    """Add ``lte release`` to the subparsers of ``svbg.__main__.build_parser``."""
    p_lte = sub.add_parser("lte", help="модуль «Трафик LTE»: аварийные команды")
    lsub = p_lte.add_subparsers(dest="lte_command", metavar="КОМАНДА")
    lsub.required = True
    rel = lsub.add_parser("release", help="снять блоки LTE (когда бот не работает)")
    mode = rel.add_mutually_exclusive_group()
    mode.add_argument("--due-only", dest="mode", action="store_const", const="due", help="сброс наступил")
    mode.add_argument("--all", dest="mode", action="store_const", const="all", help="все живые блоки")
    mode.add_argument("--panel-only", dest="mode", action="store_const", const="panel", help="без базы")
    rel.add_argument("--dry-run", action="store_true", help="только посчитать")
    rel.add_argument("--yes", action="store_true", help="не спрашивать подтверждение")
    rel.add_argument("--dsn", help=argparse.SUPPRESS)
    rel.add_argument("--base-map", action="append", metavar="TWIN=BASE", help="пара «двойник = база»")
    rel.add_argument("--twin-suffix", help="часть имени двойников в панели, например noLTE")
    rel.add_argument("--url", help="адрес панели (иначе REMNAWAVE_URL из data/.env)")
    rel.add_argument("--token-file", help="файл с API-токеном панели (иначе REMNAWAVE_TOKEN из data/.env)")
    rel.set_defaults(mode="due", handler=cmd_release)
