"""Import of an existing panel (02 §7.1): dry-run report → apply.

* streams ``users/stream`` pages (size 500) — memory is bounded by one page, never by the panel size;
* panel users with ``telegramId`` get a bot user (found or created by ``telegram_id``) and a linked
  subscription; several panel accounts of one Telegram user become several subscriptions (02 §6.7); a user
  created here already uses the VPN and does not get the entry captcha (``captcha_passed_at``);
* panel users without ``telegramId`` become **unclaimed** subscriptions (``user_id IS NULL``; claiming is
  stage 2);
* subscriptions are linked by panel ``id`` and keep ``shortUuid``/``username`` as they are; the panel's values
  become the desired ones (legacy, no plan), so nothing is written back — **the importer never writes to the
  panel**;
* idempotent: a repeat run inserts nothing (``ON CONFLICT DO NOTHING`` on every unique key), a run interrupted
  midway resumes from its stored cursor;
* the dry run counts the same things with reads only.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, Final

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from svbg.core.clock import now
from svbg.core.tables import users
from svbg.remnawave.contributors import SquadContributors, reverse
from svbg.remnawave.errors import ErrorKind, RemnawaveError
from svbg.remnawave.models import PanelUser
from svbg.remnawave.projection import snapshot_values
from svbg.remnawave.tables import import_runs
from svbg.remnawave.transport import Lane
from svbg.subscriptions.tables import subscription_events, subscriptions

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from svbg.db.engine import Database
    from svbg.remnawave.api import RemnawaveApi

__all__ = ["ImportReport", "PanelImporter", "same_panel"]

log = logging.getLogger("svbg.remnawave.importer")

PAGE_SIZE: Final = 500
SOURCE: Final = "panel"
_FILTERS: Final = frozenset(
    {"status", "telegram_id", "tag", "email", "traffic_limit_strategy", "external_squad_uuid"}
)


def same_panel(a: Any, b: Any) -> bool:
    """Do two API objects (before/after a hot swap) talk to the same panel address?"""
    if a is b:
        return True
    url_a = getattr(getattr(getattr(a, "transport", None), "config", None), "base_url", None)
    url_b = getattr(getattr(getattr(b, "transport", None), "config", None), "base_url", None)
    return url_a is not None and url_a == url_b


@dataclass(slots=True)
class ImportReport:
    run_id: int
    mode: str
    pages: int = 0
    total: int = 0
    with_telegram: int = 0
    without_telegram: int = 0
    users_created: int = 0
    subscriptions_created: int = 0
    already_linked: int = 0
    conflicts: int = 0
    finished: bool = False

    def as_json(self) -> dict[str, Any]:
        return asdict(self)


class PanelImporter:
    def __init__(
        self,
        db: Database,
        api: Callable[[], RemnawaveApi],
        *,
        contributors: SquadContributors | None = None,
        page_size: int = PAGE_SIZE,
    ) -> None:
        self._db = db
        self._api = api
        self._contributors = contributors or SquadContributors()
        self._page = page_size

    async def run(
        self,
        mode: str = "dry_run",
        *,
        filters: Mapping[str, Any] | None = None,
        resume_run_id: int | None = None,
    ) -> ImportReport:
        if mode not in ("dry_run", "apply"):
            raise ValueError("mode must be 'dry_run' or 'apply'")
        flt = {k: v for k, v in dict(filters or {}).items() if v is not None}
        unknown = set(flt) - _FILTERS
        if unknown:
            raise ValueError(f"unknown import filters: {sorted(unknown)}")
        run_id, cursor, report = await self._open(mode, flt, resume_run_id)
        first = self._api()
        async with self._db.read() as conn:
            twins = await self._contributors.twins(conn)
        while True:
            # Per page: a hot swap (e.g. a new token) closes the previous session between pages. Another
            # panel address mid-run stops the run (resumable from the saved cursor) instead of mixing panels.
            api = self._api()
            if not same_panel(api, first):
                raise RemnawaveError(
                    ErrorKind.TRANSIENT,
                    None,
                    "PANEL_CHANGED",
                    "адрес панели изменился во время импорта",
                    "Импорт остановлен, чтобы не смешать пользователей двух панелей. Проверьте адрес "
                    "панели и запустите импорт снова.",
                )
            page = await api.stream(cursor, self._page, lane=Lane.BACKGROUND, **flt)
            report.pages += 1
            if page.users:
                if mode == "apply":
                    await self._apply_page(page.users, twins, report, run_id)
                else:
                    await self._dry_page(page.users, report)
            nxt = page.cursor
            done = not page.has_more or nxt is None or nxt == cursor
            cursor = None if done else nxt
            report.finished = done
            await self._save(run_id, cursor, report, done=done)
            if done:
                break
        log.info("panel import %s #%s: %s", mode, run_id, report.as_json())
        return report

    # ------------------------------------------------------------------------------------------ bookkeeping

    async def _open(
        self, mode: str, flt: Mapping[str, Any], resume_run_id: int | None
    ) -> tuple[int, str | None, ImportReport]:
        async with self._db.tx() as conn:
            if resume_run_id is not None:
                row = (
                    (
                        await conn.execute(
                            sa.select(import_runs).where(import_runs.c.id == resume_run_id).with_for_update()
                        )
                    )
                    .mappings()
                    .first()
                )
                if row is None or row["mode"] != mode or row["status"] != "running":
                    raise ValueError(f"import run {resume_run_id} cannot be resumed")
                saved = dict(row["report"] or {})
                saved.pop("run_id", None)
                saved.pop("mode", None)
                report = ImportReport(
                    int(row["id"]), mode, **{k: v for k, v in saved.items() if k in _REPORT_KEYS}
                )
                return int(row["id"]), row["cursor"], report
            run_id = await conn.scalar(
                sa.insert(import_runs)
                .values(source=SOURCE, mode=mode, filters=dict(flt))
                .returning(import_runs.c.id)
            )
        return int(run_id), None, ImportReport(int(run_id), mode)

    async def _save(self, run_id: int, cursor: str | None, report: ImportReport, *, done: bool) -> None:
        values: dict[str, Any] = {"cursor": cursor, "report": report.as_json()}
        if done:
            values["status"] = "done"
            values["finished_at"] = now()
        async with self._db.tx() as conn:
            await conn.execute(sa.update(import_runs).where(import_runs.c.id == run_id).values(**values))

    # ----------------------------------------------------------------------------------------------- pages

    @staticmethod
    def _count(page: Sequence[PanelUser], report: ImportReport) -> list[int]:
        report.total += len(page)
        tg = [u.telegram_id for u in page if u.telegram_id is not None]
        report.with_telegram += len(tg)
        report.without_telegram += len(page) - len(tg)
        return sorted(set(tg))

    async def _dry_page(self, page: Sequence[PanelUser], report: ImportReport) -> None:
        tg = self._count(page, report)
        ids = [u.id for u in page]
        async with self._db.read() as conn:
            existing_tg = set(
                (
                    await conn.execute(sa.select(users.c.telegram_id).where(users.c.telegram_id.in_(tg)))
                ).scalars()
            )
            linked = set(
                (
                    await conn.execute(
                        sa.select(subscriptions.c.panel_user_id).where(subscriptions.c.panel_user_id.in_(ids))
                    )
                ).scalars()
            )
            taken = await _taken_keys(conn, [u for u in page if u.id not in linked])
        report.users_created += len([t for t in tg if t not in existing_tg])
        report.already_linked += len(linked)
        conflicts = sum(
            1 for u in page if u.id not in linked and (u.short_uuid in taken or u.username in taken)
        )
        report.conflicts += conflicts
        report.subscriptions_created += len(page) - len(linked) - conflicts

    async def _apply_page(
        self, page: Sequence[PanelUser], twins: Mapping[str, str], report: ImportReport, run_id: int
    ) -> None:
        tg = self._count(page, report)
        ids = [u.id for u in page]
        async with self._db.tx() as conn:
            if tg:
                created = (
                    await conn.execute(
                        pg_insert(users)
                        .values([{"telegram_id": t, "captcha_passed_at": sa.func.now()} for t in tg])
                        .on_conflict_do_nothing(index_elements=[users.c.telegram_id])
                        .returning(users.c.id)
                    )
                ).all()
                report.users_created += len(created)
                owner = {
                    int(t): int(i)
                    for i, t in (
                        await conn.execute(
                            sa.select(users.c.id, users.c.telegram_id).where(users.c.telegram_id.in_(tg))
                        )
                    ).all()
                }
            else:
                owner = {}
            linked = set(
                (
                    await conn.execute(
                        sa.select(subscriptions.c.panel_user_id).where(subscriptions.c.panel_user_id.in_(ids))
                    )
                ).scalars()
            )
            report.already_linked += len(linked)
            ts = now()
            rows = [
                _row(u, owner.get(u.telegram_id) if u.telegram_id is not None else None, twins, ts)
                for u in page
                if u.id not in linked
            ]
            if not rows:
                return
            inserted = [
                int(r[0])
                for r in (
                    await conn.execute(
                        pg_insert(subscriptions)
                        .values(rows)
                        .on_conflict_do_nothing()
                        .returning(subscriptions.c.id)
                    )
                ).all()
            ]
            report.subscriptions_created += len(inserted)
            report.conflicts += len(rows) - len(inserted)
            if inserted:
                await conn.execute(
                    sa.insert(subscription_events).from_select(
                        ["subscription_id", "kind", "source", "new_expire", "ref_type", "ref_id"],
                        sa.select(
                            subscriptions.c.id,
                            sa.literal("imported"),
                            sa.literal("import"),
                            subscriptions.c.paid_until,
                            sa.literal("import_run"),
                            sa.literal(str(run_id)),
                        ).where(subscriptions.c.id.in_(inserted)),
                    )
                )


_REPORT_KEYS: Final = frozenset(ImportReport.__slots__) - {"run_id", "mode"}  # type: ignore[attr-defined]


def _row(user: PanelUser, user_id: int | None, twins: Mapping[str, str], ts: Any) -> dict[str, Any]:
    disabled = user.status == "DISABLED"
    values = snapshot_values(user)
    values.update(
        user_id=user_id,
        plan_snapshot={"legacy": True, "source": SOURCE},
        link_state="linked",
        panel_user_id=user.id,
        panel_state_ts=ts,
        paid_until=user.expire_at,
        desired_expire_at=user.expire_at,
        desired_traffic_bytes=user.traffic_limit_bytes,
        desired_reset_strategy=user.traffic_limit_strategy,
        desired_device_limit=user.hwid_device_limit,
        desired_squads=reverse(user.squad_uuids, twins),
        desired_ext_squad=user.external_squad_uuid,
        desired_tag=user.tag,
        # Disabled in the panel = the panel admin's decision (02 §6.3), not the bot's: an override that the
        # projection keeps (and lifts when the user is enabled in the panel).
        desired_status="active",
        disabled_reason=None,
        overrides={"status": "DISABLED"} if disabled else {},
    )
    values.setdefault("panel_squads", [])
    values.setdefault("subscription_url", None)
    return values


async def _taken_keys(conn: AsyncConnection, page: Sequence[PanelUser]) -> set[str]:
    """shortUuids / usernames of ``page`` already used by other subscriptions (dry-run conflict count)."""
    if not page:
        return set()
    shorts = [u.short_uuid for u in page]
    names = [u.username for u in page]
    rows = (
        await conn.execute(
            sa.select(subscriptions.c.panel_short_uuid, subscriptions.c.panel_username).where(
                sa.or_(
                    subscriptions.c.panel_short_uuid.in_(shorts), subscriptions.c.panel_username.in_(names)
                )
            )
        )
    ).all()
    out: set[str] = set()
    for short, name in rows:
        if short:
            out.add(short)
        if name:
            out.add(name)
    return out
