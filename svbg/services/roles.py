"""Roles and rights of the bot's staff (04 §9.1 — the «роль × действие» matrix, enforced exactly).

Three roles: **Owner** (``OWNER_IDS`` or ``users.role='owner'``), **Admin** (``users.role='admin'`` plus a
set of rights in ``users.perms``; ``*`` = every Admin right), **Support**. Rights above a role are never
granted: only an owner assigns roles, and an admin's ``perms`` can only contain the Admin column of the
matrix.

* :func:`load_actor` re-reads the presser's role from the database **inside the caller's transaction** (money
  actions lock the row ``FOR SHARE``: a role revoked a millisecond earlier wins). The screen router checks the
  cached :class:`~svbg.tg.ui.context.UserCtx` on every callback; services check again with this fresh read,
  so a member of the admin group who is not staff of the bot is refused even with a forged button.
* :func:`authorize` — the matrix; :func:`check_limit` — ``ADMIN_GRANT_DAYS_MAX`` / ``ADMIN_WALLET_ADJUST_MAX``
  per operation and :func:`check_daily` — the same actor's total over a rolling 24 h (above: owner only);
  :func:`check_target` — an admin never moves money or paid time to themselves; :func:`require_reason` — a
  money action without a reason is refused.
* Rights: ``ADMIN_PERMS`` is the core Admin column (``*`` expands to it and **only** to it); X13 module rights
  (``ip_guard.*``, ``lte.*``, …) come from the extension registry and are granted one by one, never by ``*``.
* :func:`audit` writes ``admin_audit`` in the caller's transaction (``amount_minor`` → ``reason`` mandatory,
  the table has the same CHECK).
* :func:`set_role` — grant / change / revoke a role (owner only), audited in the same transaction.

Nothing here talks to Telegram; texts of :class:`RoleError` are short Russian sentences for the admin.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import sqlalchemy as sa

from svbg.core.tables import admin_audit, users

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncConnection

__all__ = [
    "ADMIN_PERMS",
    "DAY_FACTOR",
    "DEFAULT_GRANT_DAYS_MAX",
    "KEY_GRANT_DAYS_DAY_MAX",
    "KEY_GRANT_DAYS_MAX",
    "KEY_WALLET_ADJUST_DAY_MAX",
    "KEY_WALLET_ADJUST_MAX",
    "MATRIX",
    "PERM_LABELS",
    "ROLES",
    "ROLE_LABELS",
    "STAFF_ROLES",
    "Act",
    "Actor",
    "Limits",
    "RoleChange",
    "RoleError",
    "Rule",
    "StaffMember",
    "audit",
    "authorize",
    "check_daily",
    "check_limit",
    "check_target",
    "clean_perms",
    "is_module_perm",
    "load_actor",
    "require_reason",
    "set_role",
    "staff_list",
    "stored_perms",
]

ROLES: Final[tuple[str, ...]] = ("user", "support", "admin", "owner")
STAFF_ROLES: Final[tuple[str, ...]] = ("support", "admin", "owner")
_RANK: Final[Mapping[str, int]] = {r: i for i, r in enumerate(ROLES)}

#: The Admin column of 04 §9.1 (order = bit order of the role editor's checkboxes; append only).
ADMIN_PERMS: Final[tuple[str, ...]] = (
    "settings.business",
    "plans",
    "promo",
    "payments.confirm",
    "wallet.adjust",
    "subs.grant",
    "payments.refund",
    "broadcast",
    "users.ban",
    "stats",
    "system.view",
    "content.edit",
    "deeplinks",
    "tickets",
    "broadcast.send",
)
ALL_PERMS: Final = "*"

ROLE_LABELS: Final[Mapping[str, str]] = {
    "user": "пользователь",
    "support": "поддержка",
    "admin": "админ",
    "owner": "владелец",
}
PERM_LABELS: Final[Mapping[str, str]] = {
    "settings.business": "Бизнес-настройки и тексты",
    "plans": "Тарифы",
    "promo": "Промокоды",
    "payments.confirm": "Подтверждать ручные оплаты",
    "wallet.adjust": "Начислять и списывать баланс",
    "subs.grant": "Выдавать дни и тарифы",
    "payments.refund": "Возвраты",
    "broadcast": "Рассылки",
    "users.ban": "Блокировать пользователей",
    "stats": "Дашборд и выручка",
    "system.view": "«Состояние» и логи",
    "content.edit": "Редактор экранов и кнопок",
    "deeplinks": "Конструктор ссылок",
    "tickets": "Обращения",
    "broadcast.send": "Отправлять рассылки",
}
_MODULE_PERM_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,31}\.[a-z][a-z0-9_.]{0,31}$")

#: Setting keys (registered by the integration step; absent → defaults below).
KEY_GRANT_DAYS_MAX: Final = "ADMIN_GRANT_DAYS_MAX"
KEY_WALLET_ADJUST_MAX: Final = "ADMIN_WALLET_ADJUST_MAX"
KEY_GRANT_DAYS_DAY_MAX: Final = "ADMIN_GRANT_DAYS_DAY_MAX"
KEY_WALLET_ADJUST_DAY_MAX: Final = "ADMIN_WALLET_ADJUST_DAY_MAX"
DEFAULT_GRANT_DAYS_MAX: Final = 31
#: Without an explicit 24 h cap an admin may do this many maximal operations per rolling day.
DAY_FACTOR: Final = 3
#: ``pg_advisory_xact_lock(class, actor)`` namespace serializing one admin's money operations ("SvAL").
_DAILY_LOCK_CLASS: Final = 0x5376414C
REASON_MIN: Final = 3
REASON_MAX: Final = 500


class Act(enum.StrEnum):
    """Actions of the matrix this module guards (names double as ``admin_audit.action`` prefixes)."""

    USERS_VIEW = "users.view"  # card, search, history
    USERS_DEVICES = "users.devices"  # reset devices
    USERS_REISSUE = "users.reissue"  # reissue the subscription link
    USERS_MESSAGE = "users.message"  # write to the user
    TICKETS = "tickets"  # answer / close support tickets (07 §2.4.6)
    WALLET_ADJUST = "wallet.adjust"
    SUBS_GRANT = "subs.grant"  # ± days, give a plan
    USERS_BAN = "users.ban"
    STATS = "stats"
    SYSTEM_VIEW = "system.view"
    ROLES_MANAGE = "roles.manage"


@dataclass(frozen=True, slots=True)
class Rule:
    min_role: str  # lowest role allowed at all
    perm: str | None = None  # Admin right needed (owners have every right)
    owner_only: bool = False


#: 04 §9.1 rows this module enforces.
MATRIX: Final[Mapping[Act, Rule]] = {
    Act.USERS_VIEW: Rule("support"),
    Act.USERS_DEVICES: Rule("support"),
    Act.USERS_REISSUE: Rule("support"),
    Act.USERS_MESSAGE: Rule("support"),
    Act.TICKETS: Rule("support"),  # «ответы в поддержке»: Support and above
    Act.WALLET_ADJUST: Rule("admin", "wallet.adjust"),
    Act.SUBS_GRANT: Rule("admin", "subs.grant"),
    Act.USERS_BAN: Rule("admin", "users.ban"),
    Act.STATS: Rule("admin", "stats"),
    Act.SYSTEM_VIEW: Rule("admin", "system.view"),
    Act.ROLES_MANAGE: Rule("owner", owner_only=True),
}

#: Actions that move money or paid time: a reason is mandatory and ``amount_minor`` goes to the audit.
MONEY_ACTS: Final = frozenset({Act.WALLET_ADJUST, Act.SUBS_GRANT})


class RoleError(Exception):
    """A refused staff action; ``text`` is shown to the admin as is."""

    def __init__(self, code: str, text: str) -> None:
        super().__init__(f"{code}: {text}")
        self.code = code
        self.text = text


DENIED: Final = "Нет прав"


@dataclass(frozen=True, slots=True)
class Actor:
    """A staff member as the database sees them right now."""

    user_id: int | None
    telegram_id: int | None
    role: str
    perms: frozenset[str] = frozenset()
    banned: bool = False

    @property
    def is_owner(self) -> bool:
        return self.role == "owner" and not self.banned

    def has_perm(self, perm: str) -> bool:
        if self.banned:
            return False
        if self.role == "owner":
            return True
        if self.role != "admin":
            return False
        # ``*`` is the core Admin column only: module rights (IP Guard unblock, LTE config…) are explicit.
        return perm in self.perms or (ALL_PERMS in self.perms and perm in ADMIN_PERMS)

    def can(self, act: Act) -> bool:
        return authorize(self, act)


def _perms(raw: Any) -> frozenset[str]:
    if isinstance(raw, list | tuple | set | frozenset):
        return frozenset(p for p in raw if isinstance(p, str))
    return frozenset()


def authorize(actor: Actor | None, act: Act) -> bool:
    """The 04 §9.1 matrix: role at least ``min_role`` and (for admins) the right ``perm``."""
    if actor is None or actor.banned:
        return False
    rule = MATRIX[act]
    if _RANK.get(actor.role, -1) < _RANK[rule.min_role]:
        return False
    if actor.role == "owner":
        return True
    if rule.owner_only:
        return False
    return rule.perm is None or actor.has_perm(rule.perm)


async def load_actor(
    conn: AsyncConnection,
    *,
    telegram_id: int | None = None,
    user_id: int | None = None,
    owner_ids: Iterable[int] = (),
    lock: bool = False,
) -> Actor | None:
    """The actor's role and rights read now (``lock``: ``FOR SHARE`` until the transaction ends).

    ``owner_ids`` = ``OWNER_IDS``: such a Telegram id is an owner whatever the stored role says. ``None``
    when the person is not a user of the bot at all (and not in ``OWNER_IDS``).
    """
    if (telegram_id is None) == (user_id is None):
        raise ValueError("pass exactly one of telegram_id / user_id")
    cond = users.c.telegram_id == telegram_id if telegram_id is not None else users.c.id == user_id
    stmt = sa.select(users.c.id, users.c.telegram_id, users.c.role, users.c.perms, users.c.banned_at).where(
        cond
    )
    if lock:
        stmt = stmt.with_for_update(read=True)
    row = (await conn.execute(stmt)).first()
    owners = frozenset(owner_ids)
    if row is None:
        if telegram_id is not None and telegram_id in owners:
            return Actor(None, telegram_id, "owner")
        return None
    tg = row.telegram_id
    role = str(row.role) if row.role in _RANK else "user"
    if tg is not None and tg in owners:
        role = "owner"
    return Actor(
        int(row.id),
        int(tg) if tg is not None else None,
        role,
        _perms(row.perms) if role == "admin" else frozenset(),
        banned=row.banned_at is not None and tg not in owners,
    )


# ------------------------------------------------------------------------------------------------ limits


@dataclass(frozen=True, slots=True)
class Limits:
    """Limits for admins (owners are not limited). ``wallet_adjust_max_minor``: ``None`` = no admin may
    adjust the wallet (no plans to derive the default from) — only the owner. ``*_day_max*``: the total of one
    admin over a rolling 24 h; ``None`` = :data:`DAY_FACTOR` × the per-operation limit."""

    grant_days_max: int = DEFAULT_GRANT_DAYS_MAX
    wallet_adjust_max_minor: int | None = None
    grant_days_day_max: int | None = None
    wallet_adjust_day_max_minor: int | None = None

    @property
    def grant_days_per_day(self) -> int:
        if self.grant_days_day_max is not None:
            return self.grant_days_day_max
        return self.grant_days_max * DAY_FACTOR

    @property
    def wallet_adjust_per_day(self) -> int | None:
        if self.wallet_adjust_max_minor is None:
            return None
        if self.wallet_adjust_day_max_minor is not None:
            return self.wallet_adjust_day_max_minor
        return self.wallet_adjust_max_minor * DAY_FACTOR

    @classmethod
    def from_settings(
        cls, values: Mapping[str, Any], *, currency_exponent: int, max_plan_price_minor: int | None
    ) -> Limits:
        """``ADMIN_GRANT_DAYS_MAX`` (days) and ``ADMIN_WALLET_ADJUST_MAX`` (whole units of the shop currency;
        empty → the price of the most expensive plan, 04 §9.1)."""
        days = _setting_int(values, KEY_GRANT_DAYS_MAX)
        wallet = _setting_int(values, KEY_WALLET_ADJUST_MAX)
        days_day = _setting_int(values, KEY_GRANT_DAYS_DAY_MAX)
        wallet_day = _setting_int(values, KEY_WALLET_ADJUST_DAY_MAX)
        return cls(
            grant_days_max=days if days is not None and days >= 0 else DEFAULT_GRANT_DAYS_MAX,
            wallet_adjust_max_minor=(
                wallet * 10**currency_exponent if wallet is not None and wallet >= 0 else max_plan_price_minor
            ),
            grant_days_day_max=days_day if days_day is not None and days_day >= 0 else None,
            wallet_adjust_day_max_minor=(
                wallet_day * 10**currency_exponent if wallet_day is not None and wallet_day >= 0 else None
            ),
        )


def _setting_int(values: Mapping[str, Any], key: str) -> int | None:
    try:
        value = values[key]
    except (KeyError, RuntimeError):
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def check_limit(actor: Actor, act: Act, amount: int, limits: Limits) -> None:
    """Refuse an admin's operation above the limit (``amount``: days or minor units, sign ignored)."""
    if actor.is_owner:
        return
    size = abs(amount)
    if act is Act.SUBS_GRANT and size > limits.grant_days_max:
        raise RoleError("limit", f"Больше {limits.grant_days_max} дн. за раз может выдать только владелец.")
    if act is Act.WALLET_ADJUST:
        cap = limits.wallet_adjust_max_minor
        if cap is None or size > cap:
            raise RoleError("limit", "Такую сумму может провести только владелец.")


def check_target(actor: Actor, act: Act, target_user_id: int) -> None:
    """An admin never moves money or paid time to their own account (only an owner may)."""
    if act not in MONEY_ACTS or actor.is_owner:
        return
    if actor.user_id is not None and actor.user_id == target_user_id:
        raise RoleError("self", "Себе начислять нельзя — попросите владельца.")


async def check_daily(conn: AsyncConnection, actor: Actor, act: Act, amount: int, limits: Limits) -> None:
    """Refuse an admin's money operation that takes their 24 h total (``|days|`` or ``|minor units|``, both
    signs count) above the daily cap. One admin's operations are serialized by a transaction-level advisory
    lock, so two parallel submits cannot both pass the check. Owners are not limited."""
    if actor.is_owner or act not in MONEY_ACTS or actor.user_id is None:
        return
    cap = limits.grant_days_per_day if act is Act.SUBS_GRANT else limits.wallet_adjust_per_day
    if cap is None:
        raise RoleError("limit", "Такую сумму может провести только владелец.")
    await conn.execute(
        sa.select(
            sa.func.pg_advisory_xact_lock(
                sa.literal(_DAILY_LOCK_CLASS, sa.Integer), sa.literal(actor.user_id & 0x7FFFFFFF, sa.Integer)
            )
        )
    )
    size: Any
    if act is Act.SUBS_GRANT:
        days = admin_audit.c.details["days"]
        size = sa.case(
            (sa.func.jsonb_typeof(days) == "number", sa.func.abs(days.astext.cast(sa.Numeric))), else_=0
        )
        actions: tuple[str, ...] = ("subs.grant", "subs.give_plan")
    else:
        size = sa.func.abs(sa.func.coalesce(admin_audit.c.amount_minor, 0))
        actions = ("wallet.adjust",)
    used = await conn.scalar(
        sa.select(sa.func.coalesce(sa.func.sum(size), 0)).where(
            admin_audit.c.actor_id == actor.user_id,
            admin_audit.c.action.in_(actions),
            admin_audit.c.ts > sa.func.now() - sa.text("interval '24 hours'"),
        )
    )
    left = max(0, cap - int(used or 0))
    if abs(amount) > left:
        if act is Act.SUBS_GRANT:
            raise RoleError("day_limit", f"Лимит на сутки: осталось {left} дн. Больше — только владелец.")
        raise RoleError("day_limit", "Суточный лимит корректировок исчерпан. Больше — только владелец.")


def require_reason(reason: str | None) -> str:
    """The reason of a money action (04 §9.1): stripped, 3–500 characters."""
    text = " ".join((reason or "").split())
    if len(text) < REASON_MIN:
        raise RoleError("reason", "Укажите причину (хотя бы пару слов).")
    return text[:REASON_MAX]


async def audit(
    conn: AsyncConnection,
    actor: Actor | None,
    action: str,
    *,
    target: str | None = None,
    amount_minor: int | None = None,
    reason: str | None = None,
    details: Mapping[str, Any] | None = None,
    batch_id: str | None = None,
) -> None:
    """One ``admin_audit`` row in the caller's transaction."""
    if amount_minor is not None and not (reason and reason.strip()):
        raise RoleError("reason", "Укажите причину (хотя бы пару слов).")
    await conn.execute(
        sa.insert(admin_audit).values(
            actor_id=None if actor is None else actor.user_id,
            role=None if actor is None else actor.role,
            action=action[:100],
            target=None if target is None else target[:200],
            amount_minor=amount_minor,
            reason=None if reason is None else reason[:REASON_MAX],
            batch_id=batch_id,
            details=dict(details or {}),
        )
    )


# ------------------------------------------------------------------------------------------------ roles


def is_module_perm(perm: Any) -> bool:
    """An X13 module right ``<module>.<action>`` (not a core one)."""
    return isinstance(perm, str) and perm not in ADMIN_PERMS and _MODULE_PERM_RE.match(perm) is not None


def _module_catalogue(module_perms: Iterable[str]) -> list[str]:
    seen: list[str] = []
    for p in module_perms:
        if is_module_perm(p) and p not in seen:
            seen.append(p)
    return seen


def clean_perms(role: str, perms: Iterable[str], module_perms: Iterable[str] = ()) -> list[str]:
    """``perms`` valid for ``role``: the Admin column plus the X13 ``module_perms`` catalogue, only for admins
    (core in matrix order, then modules in catalogue order). ``*`` expands to the core column only."""
    if role != "admin":
        return []
    modules = _module_catalogue(module_perms)
    wanted = set(perms)
    unknown = wanted - set(ADMIN_PERMS) - set(modules) - {ALL_PERMS}
    if unknown:
        raise RoleError("perms", "Неизвестные права: " + ", ".join(sorted(unknown)))
    core = list(ADMIN_PERMS) if ALL_PERMS in wanted else [p for p in ADMIN_PERMS if p in wanted]
    return core + [p for p in modules if p in wanted]


def stored_perms(raw: Any) -> tuple[str, ...]:
    """Rights as stored in ``users.perms``: core in matrix order (``*`` → the whole core column), then the
    module rights in sorted order. Junk is dropped."""
    perms = _perms(raw)
    core = ADMIN_PERMS if ALL_PERMS in perms else tuple(p for p in ADMIN_PERMS if p in perms)
    return core + tuple(sorted(p for p in perms if is_module_perm(p)))


@dataclass(frozen=True, slots=True)
class RoleChange:
    user_id: int
    telegram_id: int | None
    old_role: str
    new_role: str
    old_perms: tuple[str, ...]
    new_perms: tuple[str, ...]

    @property
    def changed(self) -> bool:
        return self.old_role != self.new_role or self.old_perms != self.new_perms


async def set_role(
    conn: AsyncConnection,
    actor: Actor | None,
    target_user_id: int,
    role: str,
    perms: Iterable[str] = (),
    *,
    owner_ids: Iterable[int] = (),
    module_perms: Iterable[str] = (),
) -> RoleChange:
    """Grant / change / revoke (``role='user'``) a role. Owner only; never one's own role; ``OWNER_IDS``
    members are owners by configuration and are changed only there. Audited (``role.set``) in this
    transaction. ``module_perms`` — the X13 catalogue of active modules. The caller drops the target's cached
    context afterwards."""
    if actor is None or not authorize(actor, Act.ROLES_MANAGE):
        raise RoleError("denied", DENIED)
    if role not in _RANK:
        raise RoleError("role", "Неизвестная роль.")
    new_perms = tuple(clean_perms(role, perms, module_perms))
    row = (
        await conn.execute(
            sa.select(users.c.id, users.c.telegram_id, users.c.role, users.c.perms, users.c.banned_at)
            .where(users.c.id == target_user_id)
            .with_for_update()
        )
    ).first()
    if row is None:
        raise RoleError("not_found", "Пользователь не найден.")
    if actor.user_id is not None and int(row.id) == actor.user_id:
        raise RoleError("self", "Свою роль менять нельзя — попросите другого владельца.")
    if row.telegram_id is not None and row.telegram_id in frozenset(owner_ids):
        raise RoleError("configured_owner", "Этот владелец задан в OWNER_IDS — меняется только в настройках.")
    if row.banned_at is not None and role != "user":
        raise RoleError("banned", "Пользователь заблокирован — сначала разблокируйте.")
    old_role = str(row.role)
    old_perms = stored_perms(row.perms) if old_role == "admin" else ()
    change = RoleChange(
        int(row.id),
        int(row.telegram_id) if row.telegram_id is not None else None,
        old_role,
        role,
        old_perms,
        new_perms,
    )
    if not change.changed:
        return change
    await conn.execute(
        sa.update(users).where(users.c.id == target_user_id).values(role=role, perms=list(new_perms))
    )
    await audit(
        conn,
        actor,
        "role.set",
        target=f"user:{target_user_id}",
        details={
            "old_role": old_role,
            "new_role": role,
            "old_perms": list(old_perms),
            "new_perms": list(new_perms),
        },
    )
    return change


@dataclass(frozen=True, slots=True)
class StaffMember:
    user_id: int
    telegram_id: int | None
    username: str | None
    first_name: str | None
    role: str
    perms: tuple[str, ...]
    banned_at: datetime | None


async def staff_list(conn: AsyncConnection, *, limit: int = 100) -> list[StaffMember]:
    """Everyone with a stored staff role (partial index ``ix_users_staff_role``), owners first."""
    rank = sa.case({"owner": 0, "admin": 1, "support": 2}, value=users.c.role, else_=3)
    rows = (
        await conn.execute(
            sa.select(
                users.c.id,
                users.c.telegram_id,
                users.c.username,
                users.c.first_name,
                users.c.role,
                users.c.perms,
                users.c.banned_at,
            )
            .where(users.c.role != "user")
            .order_by(rank, users.c.id)
            .limit(limit)
        )
    ).all()
    return [
        StaffMember(
            int(r.id),
            int(r.telegram_id) if r.telegram_id is not None else None,
            r.username,
            r.first_name,
            str(r.role),
            stored_perms(r.perms),
            r.banned_at,
        )
        for r in rows
    ]
