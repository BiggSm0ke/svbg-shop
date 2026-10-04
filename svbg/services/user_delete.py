"""Delete a user completely, as if they never opened the bot («🗑 Удалить полностью» in the user card).

* :meth:`UserDeleter.preview` — what goes away (one SQL) for the confirmation screen.
* :meth:`UserDeleter.delete` — the presser's right ``users.delete`` is checked, then the panel users of the
  user's subscriptions are deleted through :func:`svbg.remnawave.writer.delete_panel_user` (outside any
  transaction; the first panel failure stops everything and nothing in the bot is touched), then **one**
  transaction re-checks the right, removes every row tied to the user and writes the ``admin_audit`` row.
  After the commit the in-memory caches are dropped (``forget`` hook) and a short card goes to the admin
  group.

What is removed:

* every row with a foreign key to ``users.id`` (:data:`FK_POLICY`; a test fails when a new table references
  ``users`` and is not listed there). Staff columns (``created_by``, ``closed_by``) are cleared instead;
* rows tied through the user's subscriptions, orders and payments (cascades, receipts, the webhook log,
  admin-group cards of receipts and IP Guard blocks);
* references without a foreign key: jobs about the user, ``trial_grants`` by Telegram id (the trial is
  available again), the channel membership cache, import id maps and imported transactions, error events,
  broadcast message records, webhook inbox rows and LTE counters of the deleted panel users;
* people this user invited lose their inviter (the referral pair goes away).

Kept: aggregate counters (deep-link daily stats, ad clicks, promo use counters) and the ``admin_audit``
journal of staff actions (ids are never reused, so the old entries do not attach to anybody).

Not allowed: deleting oneself, an owner (stored role or ``OWNER_IDS``) or any staff member, including a
member of a custom role without rights (take the role away first). While the panel writer is stopped (the
shadow probe found a writable token) the panel is not touched: only «Удалить только в боте» works.
"""

from __future__ import annotations

import html
import inspect
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Protocol

import sqlalchemy as sa

from svbg.core.tables import users
from svbg.db.meta import metadata
from svbg.remnawave.errors import (
    ErrorKind,
    PanelNotConfiguredError,
    PanelUnavailableError,
    RemnawaveError,
    WriteBlockedError,
)
from svbg.remnawave.writer import delete_panel_user
from svbg.services import roles
from svbg.services.roles import Act, Actor

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from svbg.db.engine import Database
    from svbg.remnawave.api import RemnawaveApi

__all__ = [
    "AUDIT_ACTION",
    "FK_POLICY",
    "DeleteResult",
    "Preview",
    "UserDeleter",
    "panel_reason",
    "user_fk_columns",
]

log = logging.getLogger("svbg.services.user_delete")

AUDIT_ACTION: Final = "user.delete"
ADMIN_TOPIC: Final = "new_users"  # «👤 Пользователи» topic of the admin group
DELETE: Final = "delete"
NULL: Final = "null"

#: Every foreign key to ``users.id``: delete the row or clear the column. Order matters for ``RESTRICT``
#: keys between the tables (receipts before payments). A new table referencing ``users`` must be added here
#: (``tests/services/test_user_delete.py`` checks the list against the metadata).
FK_POLICY: Final[Mapping[tuple[str, str], str]] = {
    ("manual_receipts", "user_id"): DELETE,
    ("payments", "user_id"): DELETE,
    ("wallet_ledger", "user_id"): DELETE,
    ("orders", "user_id"): DELETE,
    ("referral_rewards", "user_id"): DELETE,
    ("referrals", "referred_user_id"): DELETE,
    ("referrals", "referrer_id"): DELETE,  # the people this user invited lose their inviter
    ("referral_codes", "user_id"): DELETE,
    ("ip_guard_blocks", "user_id"): DELETE,
    ("notification_log", "user_id"): DELETE,
    ("trial_grants", "user_id"): DELETE,
    ("subscriptions", "user_id"): DELETE,
    ("promo_uses", "user_id"): DELETE,
    ("promo_pending", "user_id"): DELETE,
    ("ad_link_users", "user_id"): DELETE,
    ("deeplink_hits", "user_id"): DELETE,
    ("page_consents", "user_id"): DELETE,
    ("tickets", "user_id"): DELETE,
    ("ui_state", "user_id"): DELETE,
    ("user_identities", "user_id"): DELETE,
    ("tickets", "closed_by"): NULL,
    ("broadcasts", "created_by"): NULL,
    ("deeplinks", "created_by"): NULL,
}

#: ``legacy_id_map.entity`` → which collected ids its ``new_id`` points to.
_MAP_ENTITIES: Final[Mapping[str, str]] = {
    "user": "user",
    "subscription": "subs",
    "panel_user": "subs",
    "ip_guard_exempt": "subs",
    "payment": "payments",
    "order": "orders",
    "promo_use": "promo_uses",
    "lte_period": "lte_periods",
    "lte_block": "lte_blocks",
    "ip_guard_block": "ipg_blocks",
}
_NOTIFY_JOB: Final = "notify.user"  # svbg.services.notify_user.JOB_KIND

T: Final[dict[str, str]] = {
    "denied": roles.DENIED,
    "not_found": "Пользователь не найден. Возможно, его уже удалили.",
    "self": "Себя удалить нельзя.",
    "owner": "Владельца удалить нельзя.",
    "staff": "Это сотрудник. Сначала снимите с него роль.",
    "panel": "Панель не дала удалить пользователя: {reason}.",
    "stopped": "запись в панель остановлена, после перезапуска бота она снова заработает",
    "panel_partial": "Панель удалила {done} из {total}, дальше ошибка: {reason}.",
    "changed": "Пока вы подтверждали, у пользователя появилась новая подписка. Откройте удаление ещё раз.",
    "done": "Пользователь удалён.",
    "card": "🗑 <b>Пользователь удалён</b>\n{who}\nУдалил(а): {actor}\nПанель: {panel}",
    "card_panel_deleted": "удалён и там ({n})",
    "card_panel_kept": "оставлен (удалён только в боте)",
    "card_panel_none": "не было",
    "no_name": "без имени",
}

_PANEL_REASONS: Final[Mapping[ErrorKind, str]] = {
    ErrorKind.AUTH: "панель не принимает токен",
    ErrorKind.FORBIDDEN_SCOPE: "у токена нет права удалять пользователей",
    ErrorKind.TRANSIENT: "панель не отвечает",
    ErrorKind.PROXY_CHECK: "панель закрыла соединение",
    ErrorKind.SERVER: "внутренняя ошибка панели",
    ErrorKind.VALIDATION: "панель отклонила запрос",
    ErrorKind.CONFLICT: "конфликт в панели",
}


def panel_reason(err: RemnawaveError) -> str:
    """A short Russian reason for the admin (never the raw response)."""
    if isinstance(err, PanelNotConfiguredError):
        return "панель не подключена"
    if isinstance(err, WriteBlockedError):
        return "запись в панель сейчас запрещена (см. «Состояние»)"
    if isinstance(err, PanelUnavailableError):
        return "панель не отвечает"
    reason = _PANEL_REASONS.get(err.kind, "ошибка панели")
    tail = " ".join(str(x) for x in (err.status, err.code) if x)
    return f"{reason} ({tail})" if tail else reason


def user_fk_columns() -> list[tuple[str, str, str | None]]:
    """``(table, column, ondelete)`` of every foreign key to ``users.id`` in the full metadata."""
    from svbg.db.schema import load_all

    load_all()
    found: list[tuple[str, str, str | None]] = []
    for table in metadata.sorted_tables:
        for column in table.columns:
            for fk in column.foreign_keys:
                if fk.target_fullname == "users.id":
                    found.append((table.name, column.name, fk.ondelete))
    return found


def _plan() -> list[tuple[str, str, str]]:
    """The FK pass in order: :data:`FK_POLICY` first, then keys no one listed (logged; ``SET NULL`` keys are
    cleared, the rest deleted) so a table added later never blocks the deletion."""
    found = user_fk_columns()
    known = {(t, c) for t, c, _ in found}
    plan = [(t, c, how) for (t, c), how in FK_POLICY.items() if (t, c) in known]
    for t, c, ondelete in found:
        if (t, c) not in FK_POLICY:
            how = NULL if (ondelete or "").upper() == "SET NULL" else DELETE
            log.warning("user delete: %s.%s is not in FK_POLICY, using %s", t, c, how)
            plan.append((t, c, how))
    return plan


class AdminPoster(Protocol):
    async def post(self, kind: str, text: str, *, html: bool = ..., **kw: Any) -> Any: ...


@dataclass(frozen=True, slots=True)
class Preview:
    """What a deletion would remove (confirmation screen)."""

    user_id: int
    telegram_id: int | None
    first_name: str | None
    username: str | None
    role: str
    wallet_minor: int
    subscriptions: int
    panel_users: int
    payments: int
    orders: int
    invited: int
    invited_by: bool
    tickets: int
    promo_uses: int
    staff_role_id: int | None = None  # a custom role (``svbg.services.staff_roles``): staff, not deletable


@dataclass(frozen=True, slots=True)
class DeleteResult:
    ok: bool
    code: str  # done | denied | not_found | self | owner | staff | panel | changed
    text: str
    telegram_id: int | None = None
    name: str | None = None
    panel_deleted: int = 0
    panel_kept: int = 0  # panel users left in the panel («Удалить только в боте»)
    rows: Mapping[str, int] = field(default_factory=dict)

    @property
    def denied(self) -> bool:
        return self.code == "denied"


class _Refused(Exception):
    def __init__(self, code: str, text: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.text = text if text is not None else T[code]


def _name(first_name: str | None, username: str | None) -> str:
    name = (first_name or "").strip()[:64]
    handle = f"@{username[:64]}" if username else ""
    return " ".join(x for x in (name, handle) if x) or T["no_name"]


def _strs(values: Iterable[Any]) -> list[str]:
    return [str(v) for v in values]


class UserDeleter:
    """See the module docstring. ``panel`` returns the current panel client (raises when not connected);
    ``forget(user_id, telegram_id)`` drops in-memory state after the commit; ``admin_chat`` gets the card;
    ``writes_stopped`` returns why panel writes are stopped (``None``: they are not)."""

    def __init__(
        self,
        db: Database,
        *,
        owner_ids: Callable[[], Awaitable[frozenset[int]]],
        panel: Callable[[], RemnawaveApi] | None = None,
        forget: Callable[[int, int | None], Any] | None = None,
        admin_chat: AdminPoster | Callable[[], AdminPoster | None] | None = None,
        writes_stopped: Callable[[], str | None] | None = None,
    ) -> None:
        from svbg.db.schema import load_all

        load_all()  # every table module, so the FK graph and the lookups below are complete
        self._db = db
        self._owner_ids = owner_ids
        self._panel = panel
        self._forget = forget
        self._admin_chat = admin_chat
        self._writes_stopped = writes_stopped

    # ------------------------------------------------------------------------------------------ preview

    async def preview(self, conn: AsyncConnection, user_id: int) -> Preview | None:
        """Counters of everything that goes away, in one statement."""
        t = metadata.tables

        def count(table: str, cond: Callable[[sa.Table], Any]) -> Any:
            tab = t.get(table)
            if tab is None:
                return sa.literal(0)
            return sa.select(sa.func.count()).select_from(tab).where(cond(tab)).scalar_subquery()

        uid = users.c.id
        subs = t["subscriptions"]
        panel = (
            sa.select(sa.func.count(sa.distinct(subs.c.panel_user_id)))
            .where(subs.c.user_id == uid, subs.c.panel_user_id.is_not(None))
            .scalar_subquery()
        )
        refs = t.get("referrals")
        invited_by: Any = (
            sa.false()
            if refs is None
            else sa.select(sa.literal(1)).where(refs.c.referred_user_id == uid).exists()
        )
        stmt = sa.select(
            users.c.id,
            users.c.telegram_id,
            users.c.first_name,
            users.c.username,
            users.c.role,
            users.c.staff_role_id,
            users.c.wallet_minor,
            count("subscriptions", lambda x: x.c.user_id == uid).label("subs"),
            panel.label("panel"),
            count("payments", lambda x: x.c.user_id == uid).label("payments"),
            count("orders", lambda x: x.c.user_id == uid).label("orders"),
            count("referrals", lambda x: x.c.referrer_id == uid).label("invited"),
            invited_by.label("invited_by"),
            count("tickets", lambda x: x.c.user_id == uid).label("tickets"),
            count("promo_uses", lambda x: x.c.user_id == uid).label("promo_uses"),
        ).where(uid == user_id)
        row = (await conn.execute(stmt)).mappings().first()
        if row is None:
            return None
        return Preview(
            user_id=int(row["id"]),
            telegram_id=row["telegram_id"],
            first_name=row["first_name"],
            username=row["username"],
            role=str(row["role"]),
            staff_role_id=int(row["staff_role_id"]) if row["staff_role_id"] is not None else None,
            wallet_minor=int(row["wallet_minor"] or 0),
            subscriptions=int(row["subs"] or 0),
            panel_users=int(row["panel"] or 0),
            payments=int(row["payments"] or 0),
            orders=int(row["orders"] or 0),
            invited=int(row["invited"] or 0),
            invited_by=bool(row["invited_by"]),
            tickets=int(row["tickets"] or 0),
            promo_uses=int(row["promo_uses"] or 0),
        )

    async def allowed(self, actor_tg: int, user_id: int) -> bool:
        """May ``actor_tg`` delete ``user_id`` right now (the card shows the button only then)."""
        owners = await self._owner_ids()
        async with self._db.read() as conn:
            actor = await roles.load_actor(conn, telegram_id=actor_tg, owner_ids=owners)
            if not roles.authorize(actor, Act.USERS_DELETE):
                return False
            try:
                await self._target(conn, actor, user_id, owners)
            except _Refused:
                return False
        return True

    # ------------------------------------------------------------------------------------------ delete

    async def delete(self, actor_tg: int, user_id: int, *, panel: bool = True) -> DeleteResult:
        """Delete ``user_id`` (``panel``: delete the panel users too; see the module docstring)."""
        owners = await self._owner_ids()
        try:
            async with self._db.read() as conn:
                actor = await self._actor(conn, actor_tg, owners)
                target = await self._target(conn, actor, user_id, owners)
                panel_ids = await self._panel_ids(conn, user_id)
        except _Refused as e:
            return DeleteResult(False, e.code, e.text)
        deleted: list[int] = []
        if panel and panel_ids:
            stopped = self._writes_stopped() if callable(self._writes_stopped) else None
            if isinstance(stopped, str) and stopped:
                log.warning("user %s: panel writes are stopped (%s)", user_id, stopped)
                return DeleteResult(False, "panel", T["panel"].format(reason=T["stopped"]))
            try:
                api = self._client()
                for pid in panel_ids:
                    await delete_panel_user(api, pid)
                    deleted.append(pid)
            except RemnawaveError as err:
                log.warning("user %s: panel delete failed: %s", user_id, err)
                reason = panel_reason(err)
                text = (
                    T["panel_partial"].format(done=len(deleted), total=len(panel_ids), reason=reason)
                    if deleted
                    else T["panel"].format(reason=reason)
                )
                return DeleteResult(False, "panel", text, panel_deleted=len(deleted))
        try:
            async with self._db.tx() as conn:
                actor = await self._actor(conn, actor_tg, owners, lock=True)
                target = await self._target(conn, actor, user_id, owners, lock=True)
                if panel:
                    await self._check_unchanged(conn, user_id, deleted)
                rows = await self._purge(conn, target)
                name = _name(target.first_name, target.username)
                await roles.audit(
                    conn,
                    actor,
                    AUDIT_ACTION,
                    target=f"user:{user_id}",
                    details={
                        "telegram_id": target.telegram_id,
                        "name": name,
                        "panel": "deleted" if deleted else ("kept" if panel_ids else "none"),
                        "panel_users": deleted if deleted else panel_ids,
                        "rows": rows,
                    },
                )
        except _Refused as e:
            return DeleteResult(False, e.code, e.text, panel_deleted=len(deleted))
        log.info("user %s deleted by %s (%d rows)", user_id, actor.user_id, sum(rows.values()))
        await self._after(user_id, target.telegram_id)
        await self._post(actor, target, deleted, panel_ids)
        return DeleteResult(
            True,
            "done",
            T["done"],
            telegram_id=target.telegram_id,
            name=name,
            panel_deleted=len(deleted),
            panel_kept=0 if panel else len(panel_ids),
            rows=rows,
        )

    # ------------------------------------------------------------------------------------------ checks

    async def _actor(
        self, conn: AsyncConnection, actor_tg: int, owners: frozenset[int], *, lock: bool = False
    ) -> Actor:
        actor = await roles.load_actor(conn, telegram_id=actor_tg, owner_ids=owners, lock=lock)
        if actor is None or not roles.authorize(actor, Act.USERS_DELETE):
            raise _Refused("denied")
        return actor

    @staticmethod
    async def _target(
        conn: AsyncConnection,
        actor: Actor | None,
        user_id: int,
        owners: frozenset[int],
        *,
        lock: bool = False,
    ) -> Any:
        stmt = sa.select(
            users.c.id,
            users.c.telegram_id,
            users.c.role,
            users.c.staff_role_id,
            users.c.first_name,
            users.c.username,
        ).where(users.c.id == user_id)
        if lock:
            stmt = stmt.with_for_update()
        row = (await conn.execute(stmt)).first()
        if row is None:
            raise _Refused("not_found")
        if actor is not None and actor.user_id is not None and int(row.id) == actor.user_id:
            raise _Refused("self")
        if row.role == "owner" or (row.telegram_id is not None and row.telegram_id in owners):
            raise _Refused("owner")
        if row.role != "user" or row.staff_role_id is not None:  # a custom role, even one without rights
            raise _Refused("staff")
        return row

    @staticmethod
    async def _panel_ids(conn: AsyncConnection, user_id: int) -> list[int]:
        subs = metadata.tables["subscriptions"]
        rows = await conn.execute(
            sa.select(subs.c.panel_user_id)
            .where(subs.c.user_id == user_id, subs.c.panel_user_id.is_not(None))
            .group_by(subs.c.panel_user_id)
            .order_by(subs.c.panel_user_id)
        )
        return [int(r[0]) for r in rows]

    async def _check_unchanged(self, conn: AsyncConnection, user_id: int, deleted: Sequence[int]) -> None:
        """A panel user that appeared after the panel step (a purchase meanwhile) would be orphaned."""
        if set(await self._panel_ids(conn, user_id)) - set(deleted):
            raise _Refused("changed")

    def _client(self) -> RemnawaveApi:
        if self._panel is None:
            raise PanelNotConfiguredError()
        return self._panel()

    # ------------------------------------------------------------------------------------------ purge

    async def _ids(self, conn: AsyncConnection, table: str, column: str, cond: Any) -> list[Any]:
        tab = metadata.tables.get(table)
        if tab is None:
            return []
        return [r[0] for r in await conn.execute(sa.select(tab.c[column]).where(cond(tab)))]

    async def _purge(self, conn: AsyncConnection, target: Any) -> dict[str, int]:
        """Every row tied to the user, in one transaction (the caller's). Returns rows per table."""
        uid = int(target.id)
        tg = target.telegram_id
        t = metadata.tables
        rows: dict[str, int] = {}

        async def run(name: str, stmt: Any) -> None:
            n = (await conn.execute(stmt)).rowcount or 0
            if n > 0:
                rows[name] = rows.get(name, 0) + int(n)

        ids: dict[str, list[Any]] = {"user": [uid]}
        ids["subs"] = await self._ids(conn, "subscriptions", "id", lambda x: x.c.user_id == uid)
        sids = ids["subs"]
        ids["orders"] = await self._ids(conn, "orders", "id", lambda x: x.c.user_id == uid)
        ids["payments"] = await self._ids(conn, "payments", "id", lambda x: x.c.user_id == uid)
        ids["promo_uses"] = await self._ids(conn, "promo_uses", "id", lambda x: x.c.user_id == uid)
        receipts = await self._ids(conn, "manual_receipts", "id", lambda x: x.c.user_id == uid)
        notices = await self._ids(conn, "notification_log", "id", lambda x: x.c.user_id == uid)
        panel_ids = await self._ids(
            conn,
            "subscriptions",
            "panel_user_id",
            lambda x: sa.and_(x.c.user_id == uid, x.c.panel_user_id.is_not(None)),
        )
        if sids:
            ids["lte_periods"] = await self._ids(
                conn, "lte_periods", "id", lambda x: x.c.subscription_id.in_(sids)
            )
            ids["lte_blocks"] = await self._ids(
                conn, "lte_blocks", "id", lambda x: x.c.subscription_id.in_(sids)
            )
            ids["ipg_blocks"] = await self._ids(
                conn,
                "ip_guard_blocks",
                "id",
                lambda x: sa.or_(x.c.subscription_id.in_(sids), x.c.user_id == uid),
            )
            ipg_alerts = await self._ids(
                conn, "ip_guard_alerts", "id", lambda x: x.c.subscription_id.in_(sids)
            )
        else:
            ipg_alerts = []

        # Jobs about the user, their subscriptions, orders, payments and notices (any status).
        jobs = t.get("jobs")
        if jobs is not None:
            p = jobs.c.payload
            uid_s = str(uid)
            conds: list[Any] = [
                p["user_id"].astext == uid_s,
                p["referred_user_id"].astext == uid_s,
                p["referrer_id"].astext == uid_s,
                jobs.c.caused_by == f"user:{uid}",
            ]
            if sids:
                conds += [
                    jobs.c.ordering_key.in_([f"sub:{s}" for s in sids]),
                    p["sub_id"].astext.in_(_strs(sids)),
                    p["subscription_id"].astext.in_(_strs(sids)),
                ]
            if ids["orders"]:
                conds += [
                    p["order_id"].astext.in_(_strs(ids["orders"])),
                    jobs.c.caused_by.in_([f"order:{o}" for o in ids["orders"]]),
                ]
            if ids["payments"]:
                conds.append(p["payment_id"].astext.in_(_strs(ids["payments"])))
            if notices:
                conds.append(sa.and_(jobs.c.kind == _NOTIFY_JOB, p["id"].astext.in_(_strs(notices))))
            refs = [f"block:{b}" for b in ids.get("ipg_blocks", [])] + [f"alert:{a}" for a in ipg_alerts]
            if refs:
                conds.append(p["ref"].astext.in_(refs))
            await run("jobs", sa.delete(jobs).where(sa.or_(*conds)))

        # Cards of the admin group about the user's receipts and IP Guard blocks (the messages stay).
        cards = t.get("admin_cards")
        card_refs = (
            [f"receipt:{r}" for r in receipts]
            + [f"block:{b}" for b in ids.get("ipg_blocks", [])]
            + [f"alert:{a}" for a in ipg_alerts]
        )
        if cards is not None and card_refs:
            await run("admin_cards", sa.delete(cards).where(cards.c.ref.in_(card_refs)))

        # Import id maps: the importer would treat the person as new again.
        id_map = t.get("legacy_id_map")
        if id_map is not None:
            map_conds = [
                sa.and_(id_map.c.entity == entity, id_map.c.new_id.in_(_strs(ids[key])))
                for entity, key in _MAP_ENTITIES.items()
                if ids.get(key)
            ]
            await run("legacy_id_map", sa.delete(id_map).where(sa.or_(*map_conds)))
        legacy_tx = t.get("legacy_transactions")
        if legacy_tx is not None:
            await run("legacy_transactions", sa.delete(legacy_tx).where(legacy_tx.c.user_id == uid))

        # The webhook log of the user's payments (text ids, no foreign key).
        events = t.get("payment_events")
        if events is not None and ids["payments"]:
            await run(
                "payment_events", sa.delete(events).where(events.c.payment_id.in_(_strs(ids["payments"])))
            )

        # Every foreign key to users.id (receipts before payments: RESTRICT keys).
        for table, column, how in _plan():
            tab = t[table]
            col = tab.c[column]
            if how == NULL:
                await run(f"{table}.{column}", sa.update(tab).where(col == uid).values({column: None}))
            else:
                await run(table, sa.delete(tab).where(col == uid))

        # References without a foreign key.
        trials = t.get("trial_grants")
        if trials is not None and tg is not None:
            await run("trial_grants", sa.delete(trials).where(trials.c.telegram_id == tg))
        members = t.get("channel_members")
        if members is not None and tg is not None:
            await run("channel_members", sa.delete(members).where(members.c.telegram_id == tg))
        for table in ("broadcast_msgs", "error_events"):
            tab = t.get(table)
            if tab is not None:
                await run(table, sa.delete(tab).where(tab.c.user_id == uid))
        if panel_ids:
            for table in ("rw_inbox", "lte_counters"):
                tab = t.get(table)
                if tab is not None:
                    await run(table, sa.delete(tab).where(tab.c.panel_user_id.in_(panel_ids)))

        await run("users", sa.delete(users).where(users.c.id == uid))
        return rows

    # ------------------------------------------------------------------------------------------ after

    async def _after(self, user_id: int, telegram_id: int | None) -> None:
        if self._forget is None:
            return
        try:
            res = self._forget(user_id, telegram_id)
            if inspect.isawaitable(res):
                await res
        except Exception:
            log.exception("user %s deleted, but dropping cached state failed", user_id)

    async def _post(
        self, actor: Actor, target: Any, deleted: Sequence[int], panel_ids: Sequence[int]
    ) -> None:
        chat = self._admin_chat
        if callable(chat) and not hasattr(chat, "post"):
            chat = chat()
        if chat is None:
            return
        who = html.escape(_name(target.first_name, target.username), quote=False)
        if target.telegram_id is not None:
            who += f" · <code>{int(target.telegram_id)}</code>"
        actor_name = await self._actor_name(actor)
        if deleted:
            panel = T["card_panel_deleted"].format(n=len(deleted))
        else:
            panel = T["card_panel_kept"] if panel_ids else T["card_panel_none"]
        text = T["card"].format(who=who, actor=html.escape(actor_name, quote=False), panel=panel)
        try:
            from svbg.tg.notifier import Priority

            await chat.post(ADMIN_TOPIC, text, html=True, priority=Priority.NORMAL)
        except Exception:
            log.exception("cannot post the deletion of user %s to the admin group", target.id)

    async def _actor_name(self, actor: Actor) -> str:
        label = roles.ROLE_LABELS.get(actor.role, actor.role)
        if actor.user_id is None:
            return f"{label} {actor.telegram_id}"
        try:
            async with self._db.read() as conn:
                row = (
                    await conn.execute(
                        sa.select(users.c.first_name, users.c.username).where(users.c.id == actor.user_id)
                    )
                ).first()
        except (sa.exc.SQLAlchemyError, OSError):
            row = None
        name = _name(row.first_name, row.username) if row is not None else str(actor.telegram_id)
        return f"{name} ({label})"
