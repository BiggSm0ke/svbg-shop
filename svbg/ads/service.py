"""Ad links (01 §1.4, 06 §2.7): the deep-link router's first lookup, first-touch attribution, a one-SQL
funnel.

* :meth:`AdService.by_code` — exact match on the whole ``/start`` parameter, from memory (**0 SQL**); only
  switched-on links. Bedolaga campaign codes work as they are (no prefix).
* :meth:`AdService.record_start` — one statement: ``clicks + 1`` and, for a new user, the first-touch row in
  ``ad_link_users`` (a user who already came through a link keeps it).
* :meth:`AdService.stats` — registrations (all / 30 days), trials, payers and revenue of the link's users in
  one statement.

New codes may not start with a deep-link prefix (``s_``, ``p_``, ``pr_``, ``t_``, ``r_``, ``a_``, ``l_``,
``setup_``, ``ref``…): an exact ad code is matched before the prefixes and would shadow them.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from svbg.ads.tables import ad_link_users, ad_links
from svbg.billing.tables import orders
from svbg.core.clock import now
from svbg.core.tables import admin_audit
from svbg.promo.rules import generate_code
from svbg.subscriptions.tables import trial_grants

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from svbg.db.engine import Database

__all__ = ["RESERVED_PREFIXES", "AdError", "AdLink", "AdService", "AdStats", "from_bedolaga_campaign"]

log = logging.getLogger("svbg.ads")

CODE_RE: Final = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
NEW_CODE_RE: Final = re.compile(r"^[A-Za-z0-9_-]{3,32}$")
RESERVED_PREFIXES: Final = (
    "s_",
    "p_",
    "pr_",
    "t_",
    "r_",
    "a_",
    "l_",
    "setup_",
    "ref",
    "plan_",
    "promo_",
    "ad_",
)
MAX_TITLE: Final = 128


class AdError(ValueError):
    """A refused change; ``str(error)`` is a short Russian message for the owner."""


@dataclass(frozen=True, slots=True)
class AdLink:
    id: int
    code: str
    title: str
    bonus: Mapping[str, Any]
    enabled: bool
    clicks: int
    source: str
    created_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> AdLink:
        return cls(
            id=int(row["id"]),
            code=str(row["code"]),
            title=str(row["title"]),
            bonus=dict(row["bonus"] or {}),
            enabled=bool(row["enabled"]),
            clicks=int(row["clicks"] or 0),
            source=str(row["source"]),
            created_at=row.get("created_at"),
        )

    def url(self, bot_username: str | None) -> str | None:
        return f"https://t.me/{bot_username}?start={self.code}" if bot_username else None

    @property
    def has_bonus(self) -> bool:
        return str(self.bonus.get("type") or "none") != "none"


@dataclass(frozen=True, slots=True)
class AdStats:
    users: int
    users_30d: int
    trials: int
    payers: int
    revenue_minor: int


def check_new_code(code: Any) -> str:
    value = code.strip() if isinstance(code, str) else ""
    if not NEW_CODE_RE.fullmatch(value):
        raise AdError("Код ссылки: 3–32 символа — латиница, цифры, «_» и «-»")
    if value.lower().startswith(RESERVED_PREFIXES):
        raise AdError(
            "Код не может начинаться с s_, p_, pr_, t_, r_, a_, l_, setup_, ref, plan_, promo_, ad_"
        )
    return value


def from_bedolaga_campaign(row: Mapping[str, Any]) -> dict[str, Any]:
    """``advertising_campaigns`` row → ``ad_links`` values (06 §2.7). The bonus is kept for reference only."""
    code = str(row.get("start_parameter") or "").strip()
    if not CODE_RE.fullmatch(code):
        raise AdError(f"код кампании «{code}» не подходит для ссылки")
    bonus: dict[str, Any] = {"type": str(row.get("bonus_type") or "none")}
    for key in ("balance_bonus_kopeks", "subscription_duration_days", "device_limit", "squads"):
        if row.get(key) is not None:
            bonus[key] = row[key]
    created = row.get("created_at")
    values: dict[str, Any] = {
        "code": code,
        "title": (str(row.get("name") or code).strip() or code)[:MAX_TITLE],
        "enabled": bool(row.get("is_active", True)),
        "bonus": bonus,
        "owner_user_id": row.get("partner_user_id"),
        "source": "import",
        "legacy_id": None if row.get("id") is None else str(row["id"]),
    }
    if isinstance(created, datetime):
        values["created_at"] = created if created.tzinfo else created.replace(tzinfo=UTC)
    return values


class AdService:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._by_code: dict[str, AdLink] = {}
        self._by_id: dict[int, AdLink] = {}

    # ------------------------------------------------------------------------------------------ memory

    async def load(self) -> int:
        async with self.db.read() as conn:
            rows = (await conn.execute(sa.select(ad_links))).mappings().all()
        links = [AdLink.from_row(r) for r in rows]
        self._by_code = {link.code: link for link in links}
        self._by_id = {link.id: link for link in links}
        return len(links)

    def _remember(self, link: AdLink) -> AdLink:
        old = self._by_id.get(link.id)
        if old is not None and old.code != link.code:
            self._by_code.pop(old.code, None)
        self._by_code[link.code] = link
        self._by_id[link.id] = link
        return link

    def by_code(self, code: Any) -> AdLink | None:
        """A switched-on link whose code is exactly ``code`` (the deep-link router's first check)."""
        link = self._by_code.get(code) if isinstance(code, str) else None
        return link if link is not None and link.enabled else None

    def get(self, link_id: int) -> AdLink | None:
        return self._by_id.get(link_id)

    def all(self) -> list[AdLink]:
        return sorted(self._by_id.values(), key=lambda link: link.id, reverse=True)

    # ------------------------------------------------------------------------------------------ users

    async def record_start(self, link: AdLink, user_id: int, *, is_new: bool) -> bool:
        """``/start`` through ``link`` (one statement). Returns ``True`` when the user got attached to it."""
        bump = (
            sa.update(ad_links)
            .where(ad_links.c.id == link.id)
            .values(clicks=ad_links.c.clicks + 1)
            .returning(ad_links.c.id)
            .cte("bump")
        )
        attach = (
            pg_insert(ad_link_users)
            .from_select(
                ["user_id", "ad_link_id"],
                sa.select(sa.literal(user_id, sa.BigInteger), bump.c.id).where(sa.literal(is_new)),
            )
            .on_conflict_do_nothing(index_elements=[ad_link_users.c.user_id])
            .returning(ad_link_users.c.user_id)
            .add_cte(bump)
        )
        async with self.db.tx() as conn:
            attached = (await conn.execute(attach)).first() is not None
        return attached

    async def link_of(self, user_id: int) -> AdLink | None:
        async with self.db.read() as conn:
            link_id = await conn.scalar(
                sa.select(ad_link_users.c.ad_link_id).where(ad_link_users.c.user_id == user_id)
            )
        return self._by_id.get(int(link_id)) if link_id is not None else None

    async def stats(self, link_id: int) -> AdStats:
        members = sa.select(ad_link_users.c.user_id).where(ad_link_users.c.ad_link_id == link_id)
        month = now() - timedelta(days=30)
        bought = sa.and_(
            orders.c.user_id.in_(members), orders.c.kind != "topup", orders.c.status == "fulfilled"
        )
        stmt = sa.select(
            sa.select(sa.func.count()).where(ad_link_users.c.ad_link_id == link_id).scalar_subquery(),
            sa.select(sa.func.count())
            .where(ad_link_users.c.ad_link_id == link_id, ad_link_users.c.attached_at >= month)
            .scalar_subquery(),
            sa.select(sa.func.count(sa.distinct(trial_grants.c.user_id)))
            .where(trial_grants.c.user_id.in_(members))
            .scalar_subquery(),
            sa.select(sa.func.count(sa.distinct(orders.c.user_id))).where(bought).scalar_subquery(),
            sa.select(sa.func.coalesce(sa.func.sum(orders.c.total_minor), 0)).where(bought).scalar_subquery(),
        )
        async with self.db.read() as conn:
            row = (await conn.execute(stmt)).one()
        return AdStats(*(int(v or 0) for v in row))

    # ------------------------------------------------------------------------------------------ owner

    async def _audit(
        self,
        conn: AsyncConnection,
        actor: tuple[int | None, str | None],
        action: str,
        link_id: int,
        details: Any,
    ) -> None:
        await conn.execute(
            sa.insert(admin_audit).values(
                actor_id=actor[0], role=actor[1], action=action, target=f"ad_link:{link_id}", details=details
            )
        )

    async def create(
        self, actor: tuple[int | None, str | None], *, title: str, code: str | None = None
    ) -> AdLink:
        name = title.strip() if isinstance(title, str) else ""
        if not name or len(name) > MAX_TITLE:
            raise AdError(f"Название: от 1 до {MAX_TITLE} символов")
        for attempt in range(5):
            value = check_new_code(code) if code is not None else generate_code(8).lower()
            try:
                async with self.db.tx() as conn:
                    row = (
                        (
                            await conn.execute(
                                sa.insert(ad_links)
                                .values(code=value, title=name, created_by=actor[0])
                                .returning(ad_links)
                            )
                        )
                        .mappings()
                        .one()
                    )
                    await self._audit(
                        conn, actor, "ad_link.create", int(row["id"]), {"code": value, "title": name}
                    )
                return self._remember(AdLink.from_row(row))
            except IntegrityError:
                if code is not None or attempt == 4:
                    raise AdError("Ссылка с таким кодом уже есть") from None
        raise AdError("Ссылка с таким кодом уже есть")  # pragma: no cover

    async def update(self, link_id: int, actor: tuple[int | None, str | None], **changes: Any) -> AdLink:
        allowed = {"title", "enabled", "code"}
        if set(changes) - allowed:
            raise AdError("Неизвестные поля")
        link = self._by_id.get(link_id)
        if link is None:
            raise AdError("Ссылка не найдена")
        values = dict(changes)
        if "title" in values:
            name = str(values["title"] or "").strip()
            if not name or len(name) > MAX_TITLE:
                raise AdError(f"Название: от 1 до {MAX_TITLE} символов")
            values["title"] = name
        if "code" in values:
            values["code"] = check_new_code(values["code"])
            if values["code"] != link.code and (link.clicks or (await self.stats(link_id)).users):
                raise AdError("По ссылке уже переходили — код менять нельзя, создайте новую")
        try:
            async with self.db.tx() as conn:
                row = (
                    (
                        await conn.execute(
                            sa.update(ad_links)
                            .where(ad_links.c.id == link_id)
                            .values(**values, updated_at=sa.func.now())
                            .returning(ad_links)
                        )
                    )
                    .mappings()
                    .first()
                )
                if row is None:
                    raise AdError("Ссылка не найдена")
                await self._audit(conn, actor, "ad_link.update", link_id, values)
        except IntegrityError:
            raise AdError("Ссылка с таким кодом уже есть") from None
        return self._remember(AdLink.from_row(row))

    async def delete(self, link_id: int, actor: tuple[int | None, str | None]) -> None:
        link = self._by_id.get(link_id)
        if link is None:
            raise AdError("Ссылка не найдена")
        async with self.db.tx() as conn:
            users = await conn.scalar(
                sa.select(sa.func.count())
                .select_from(ad_link_users)
                .where(ad_link_users.c.ad_link_id == link_id)
            )
            if users:
                raise AdError("По ссылке уже пришли пользователи — её можно только выключить")
            await conn.execute(sa.delete(ad_links).where(ad_links.c.id == link_id))
            await self._audit(conn, actor, "ad_link.delete", link_id, {"code": link.code})
        self._by_id.pop(link_id, None)
        self._by_code.pop(link.code, None)

    # ------------------------------------------------------------------------------------------ import

    @staticmethod
    async def import_campaign(conn: AsyncConnection, row: Mapping[str, Any]) -> int:
        """Insert or refresh a Bedolaga campaign (keyed by ``(source='import', legacy_id)``); returns its
        id."""
        values = from_bedolaga_campaign(row)
        stmt = pg_insert(ad_links).values(**values)
        if values["legacy_id"] is not None:
            update = {k: v for k, v in values.items() if k not in ("source", "legacy_id", "created_at")}
            stmt = stmt.on_conflict_do_update(
                index_elements=[ad_links.c.source, ad_links.c.legacy_id],
                index_where=ad_links.c.legacy_id.is_not(None),
                set_={**update, "updated_at": sa.func.now()},
            )
        return int((await conn.execute(stmt.returning(ad_links.c.id))).scalar_one())

    @staticmethod
    async def import_registration(
        conn: AsyncConnection, *, user_id: int, ad_link_id: int, attached_at: datetime | None = None
    ) -> bool:
        """A Bedolaga registration (first touch wins: call in ``attached_at`` order). No bonus is given
        again."""
        values: dict[str, Any] = {"user_id": user_id, "ad_link_id": ad_link_id, "source": "import"}
        if attached_at is not None:
            values["attached_at"] = attached_at
        stmt = (
            pg_insert(ad_link_users)
            .values(**values)
            .on_conflict_do_nothing(index_elements=[ad_link_users.c.user_id])
            .returning(ad_link_users.c.user_id)
        )
        return (await conn.execute(stmt)).first() is not None
