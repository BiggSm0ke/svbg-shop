"""Custom staff roles: the owner names a role («Модератор», «Поддержка» …), picks its rights and gives it to
people (``staff_roles``, ``users.staff_role_id``).

* A member has **exactly** the role's rights: they are copied into ``users.perms``, ``users.role`` is set to
  the rank the rights need (:func:`svbg.core.perms.tier_of`: ``support`` for the Support column only, else
  ``admin``; a role without rights gives no access at all). Every reader of the role (``load_actor``, the
  cached ``UserCtx``, the «/» menus) keeps working unchanged; editing a role rewrites its members in the same
  transaction.
* Who may manage: the owner, and anyone whose role has «Команда и роли» (``roles.manage``). A manager never
  gives more than they have: a role they create or edit, a role they hand out and the role of the person they
  change must all fit into their own rights. They never touch their own role, an owner or a role wider than
  theirs. Only owners deal with owners; owners from ``OWNER_IDS`` are changed only in the settings.
* Every change is written to ``admin_audit`` in the caller's transaction (``role.create``, ``role.rename``,
  ``role.perms``, ``role.delete``, ``role.assign``, ``role.unassign``). The caller drops the cached context of
  everyone in :attr:`Outcome.affected` and refreshes their «/» menus.

Texts of :class:`~svbg.services.roles.RoleError` are short Russian sentences for the admin.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

import sqlalchemy as sa

from svbg.core.perms import ADMIN_PERMS, ALL_PERMS, CORE_PERMS, SUPPORT_PERMS, tier_of
from svbg.core.tables import staff_roles, users
from svbg.services import roles
from svbg.services.roles import DENIED, Act, Actor, RoleError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

__all__ = [
    "MAX_ROLES",
    "NAME_MAX",
    "Member",
    "Outcome",
    "StaffRole",
    "Target",
    "assign",
    "can_edit",
    "clean_name",
    "create_role",
    "delete_role",
    "effective_perms",
    "find_user",
    "get_role",
    "grantable",
    "list_roles",
    "members_of",
    "rename_role",
    "role_of_user",
    "set_perms",
    "toggle_perm",
    "unassign",
]

NAME_MAX: Final = 40
MAX_ROLES: Final = 30
_SPACES: Final = re.compile(r"\s+")
_USERNAME: Final = re.compile(r"^@?([A-Za-z][A-Za-z0-9_]{3,31})$")
_LINK: Final = re.compile(r"^(?:https?://)?t\.me/([A-Za-z][A-Za-z0-9_]{3,31})/?$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class StaffRole:
    id: int
    name: str
    perms: tuple[str, ...]
    members: int = 0

    @property
    def tier(self) -> str:
        return tier_of(self.perms)


@dataclass(frozen=True, slots=True)
class Member:
    """Someone whose cached context and «/» menu must be refreshed after a change."""

    user_id: int
    telegram_id: int | None
    role: str
    perms: tuple[str, ...]
    scoped: bool


@dataclass(slots=True)
class Outcome:
    role: StaffRole | None = None
    affected: list[Member] = field(default_factory=list)
    changed: bool = True


@dataclass(frozen=True, slots=True)
class Target:
    """A person as the role screens see them."""

    user_id: int
    telegram_id: int | None
    username: str | None
    first_name: str | None
    role: str
    perms: tuple[str, ...]
    staff_role_id: int | None
    banned: bool


# ------------------------------------------------------------------------------------------------ rules


def clean_name(name: Any) -> str:
    text = _SPACES.sub(" ", str(name or "")).strip()
    if not text:
        raise RoleError("name", "Напишите название роли.")
    if len(text) > NAME_MAX:
        raise RoleError("name", f"Название длиннее {NAME_MAX} символов, сократите.")
    return text


def _require_manager(actor: Actor | None) -> Actor:
    if actor is None or not roles.authorize(actor, Act.ROLES_MANAGE):
        raise RoleError("denied", DENIED)
    return actor


def grantable(actor: Actor | None, catalogue: Iterable[str]) -> frozenset[str]:
    """Rights of ``catalogue`` that ``actor`` may give: all of them for the owner, their own for a manager."""
    if actor is None or actor.banned:
        return frozenset()
    if actor.is_owner:
        return frozenset(catalogue)
    return frozenset(p for p in catalogue if actor.has_perm(p) and p != ALL_PERMS)


def effective_perms(role: str, perms: Iterable[str], scoped: bool) -> frozenset[str]:
    """What a person can do now (for «may a manager change them»): the role's rights of a member, the Support
    column plus the stored rights of classic staff (``*`` → the Admin column)."""
    stored = frozenset(perms)
    if role == "user":
        return frozenset() if not scoped else stored
    if scoped:
        return stored
    base = set(SUPPORT_PERMS)
    if role == "admin":
        base |= stored - {ALL_PERMS}
        if ALL_PERMS in stored:
            base |= set(ADMIN_PERMS)
    return frozenset(base)


def _fits(actor: Actor, perms: Iterable[str]) -> bool:
    return actor.is_owner or all(actor.has_perm(p) for p in perms)


def can_edit(actor: Actor | None, role: StaffRole, actor_role_id: int | None) -> bool:
    """The owner edits any role; a manager — a role that is not theirs and fits into their rights."""
    if actor is None or not roles.authorize(actor, Act.ROLES_MANAGE):
        return False
    if actor.is_owner:
        return True
    return role.id != actor_role_id and _fits(actor, role.perms)


def _ordered(perms: Iterable[str]) -> tuple[str, ...]:
    return roles.stored_perms(list(perms))


# ------------------------------------------------------------------------------------------------ reads


def _role(row: Any, members: int = 0) -> StaffRole:
    return StaffRole(int(row.id), str(row.name), _ordered(row.perms or []), members)


async def list_roles(conn: AsyncConnection) -> list[StaffRole]:
    """Every role with its number of members, by name."""
    count = (
        sa.select(sa.func.count())
        .where(users.c.staff_role_id == staff_roles.c.id, users.c.role != "owner")
        .scalar_subquery()
    )
    rows = (
        await conn.execute(
            sa.select(staff_roles.c.id, staff_roles.c.name, staff_roles.c.perms, count.label("members"))
        )
    ).all()
    found = [_role(r, int(r.members or 0)) for r in rows]
    return sorted(found, key=lambda r: (r.name.casefold(), r.id))


async def get_role(conn: AsyncConnection, role_id: int, *, lock: bool = False) -> StaffRole | None:
    stmt = sa.select(staff_roles.c.id, staff_roles.c.name, staff_roles.c.perms).where(
        staff_roles.c.id == role_id
    )
    if lock:
        stmt = stmt.with_for_update()
    row = (await conn.execute(stmt)).first()
    if row is None:
        return None
    members = await conn.scalar(
        sa.select(sa.func.count()).where(users.c.staff_role_id == role_id, users.c.role != "owner")
    )
    return _role(row, int(members or 0))


async def members_of(conn: AsyncConnection, role_id: int, *, limit: int = 50) -> list[Target]:
    rows = (
        await conn.execute(
            _target_select()
            .where(users.c.staff_role_id == role_id, users.c.role != "owner")
            .order_by(users.c.id)
            .limit(limit)
        )
    ).all()
    return [_target(r) for r in rows]


async def role_of_user(conn: AsyncConnection, user_id: int | None) -> int | None:
    if user_id is None:
        return None
    value = await conn.scalar(sa.select(users.c.staff_role_id).where(users.c.id == user_id))
    return int(value) if value is not None else None


def _target_select() -> sa.Select[Any]:
    return sa.select(
        users.c.id,
        users.c.telegram_id,
        users.c.username,
        users.c.first_name,
        users.c.role,
        users.c.perms,
        users.c.staff_role_id,
        users.c.banned_at,
    )


def _target(row: Any) -> Target:
    return Target(
        int(row.id),
        int(row.telegram_id) if row.telegram_id is not None else None,
        row.username,
        row.first_name,
        str(row.role),
        roles.stored_perms(row.perms),
        int(row.staff_role_id) if row.staff_role_id is not None else None,
        row.banned_at is not None,
    )


async def get_target(conn: AsyncConnection, user_id: int, *, lock: bool = False) -> Target | None:
    stmt = _target_select().where(users.c.id == user_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await conn.execute(stmt)).first()
    return _target(row) if row is not None else None


async def find_user(conn: AsyncConnection, query: str) -> Target | None:
    """A person by Telegram ID, ``№`` (users.id with ``#``/``№``), @username or a ``t.me/<name>`` link."""
    text = (query or "").strip()
    if not text:
        return None
    if text[:1] in "#№" and text[1:].strip().isdigit():
        return await get_target(conn, int(text[1:].strip()))
    if text.lstrip("-").isdigit() and len(text) <= 19:
        row = (await conn.execute(_target_select().where(users.c.telegram_id == int(text)))).first()
        return _target(row) if row is not None else None
    m = _USERNAME.match(text) or _LINK.match(text)
    if m is None:
        return None
    row = (
        await conn.execute(
            _target_select()
            .where(sa.func.lower(users.c.username) == m[1].lower())
            .order_by(users.c.last_seen_at.desc().nulls_last(), users.c.id.desc())
            .limit(1)
        )
    ).first()
    return _target(row) if row is not None else None


# ------------------------------------------------------------------------------------------------ roles


async def _name_free(conn: AsyncConnection, name: str, *, skip: int | None = None) -> None:
    """Names are unique regardless of case (compared here: ``lower()`` of the database may not know Cyrillic
    under the C locale; there are at most :data:`MAX_ROLES` roles)."""
    rows = (await conn.execute(sa.select(staff_roles.c.id, staff_roles.c.name))).all()
    wanted = name.casefold()
    if any(str(r.name).casefold() == wanted and r.id != skip for r in rows):
        raise RoleError("name_taken", "Роль с таким названием уже есть.")


def _check_perms(
    actor: Actor, perms: Iterable[str], catalogue: Iterable[str], keep: Iterable[str] = ()
) -> tuple[str, ...]:
    wanted = set(perms) - {ALL_PERMS}
    known = set(catalogue) | set(CORE_PERMS) | set(keep)
    unknown = wanted - known
    if unknown:
        raise RoleError("perms", "Неизвестные права: " + ", ".join(sorted(unknown)))
    if not _fits(actor, wanted):
        raise RoleError("escalation", "Можно выдать только те права, которые есть у вас.")
    return _ordered(wanted)


async def create_role(
    conn: AsyncConnection,
    actor: Actor | None,
    name: str,
    perms: Iterable[str] = (),
    *,
    catalogue: Iterable[str] = CORE_PERMS,
) -> Outcome:
    actor = _require_manager(actor)
    clean = clean_name(name)
    rights = _check_perms(actor, perms, catalogue)
    total = await conn.scalar(sa.select(sa.func.count()).select_from(staff_roles))
    if int(total or 0) >= MAX_ROLES:
        raise RoleError("too_many", f"Ролей уже {MAX_ROLES}. Удалите лишние.")
    await _name_free(conn, clean)
    rid = await conn.scalar(
        sa.insert(staff_roles).values(name=clean, perms=list(rights)).returning(staff_roles.c.id)
    )
    role = StaffRole(int(rid), clean, rights, 0)
    await roles.audit(
        conn,
        actor,
        "role.create",
        target=f"staff_role:{role.id}",
        details={"name": clean, "perms": list(rights)},
    )
    return Outcome(role)


async def _editable(conn: AsyncConnection, actor: Actor | None, role_id: int) -> tuple[Actor, StaffRole]:
    actor = _require_manager(actor)
    role = await get_role(conn, role_id, lock=True)
    if role is None:
        raise RoleError("not_found", "Роли уже нет.")
    if not actor.is_owner:
        if role.id == await role_of_user(conn, actor.user_id):
            raise RoleError("own_role", "Свою роль может поменять только владелец.")
        if not _fits(actor, role.perms):
            raise RoleError("escalation", "У этой роли есть права, которых нет у вас.")
    return actor, role


async def rename_role(conn: AsyncConnection, actor: Actor | None, role_id: int, name: str) -> Outcome:
    actor, role = await _editable(conn, actor, role_id)
    clean = clean_name(name)
    if clean == role.name:
        return Outcome(role, changed=False)
    await _name_free(conn, clean, skip=role.id)
    await conn.execute(
        sa.update(staff_roles).where(staff_roles.c.id == role.id).values(name=clean, updated_at=sa.func.now())
    )
    await roles.audit(
        conn, actor, "role.rename", target=f"staff_role:{role.id}", details={"old": role.name, "new": clean}
    )
    return Outcome(StaffRole(role.id, clean, role.perms, role.members))


async def _rewrite_members(conn: AsyncConnection, role_id: int, perms: tuple[str, ...]) -> list[Member]:
    tier = tier_of(perms)
    rows = (
        await conn.execute(
            sa.update(users)
            .where(users.c.staff_role_id == role_id, users.c.role != "owner")
            .values(perms=list(perms), role=tier)
            .returning(users.c.id, users.c.telegram_id)
        )
    ).all()
    return [
        Member(int(r.id), int(r.telegram_id) if r.telegram_id is not None else None, tier, perms, True)
        for r in rows
    ]


async def set_perms(
    conn: AsyncConnection,
    actor: Actor | None,
    role_id: int,
    perms: Iterable[str],
    *,
    catalogue: Iterable[str] = CORE_PERMS,
) -> Outcome:
    """Replace the rights of a role; its members get them at once. Rights of a module that is off now stay
    as they are (they are not in ``catalogue``, nobody could switch them back on otherwise)."""
    actor, role = await _editable(conn, actor, role_id)
    rights = _check_perms(actor, perms, catalogue, keep=role.perms)
    if set(rights) == set(role.perms):
        return Outcome(role, changed=False)
    await conn.execute(
        sa.update(staff_roles)
        .where(staff_roles.c.id == role.id)
        .values(perms=list(rights), updated_at=sa.func.now())
    )
    affected = await _rewrite_members(conn, role.id, rights)
    await roles.audit(
        conn,
        actor,
        "role.perms",
        target=f"staff_role:{role.id}",
        details={
            "name": role.name,
            "added": sorted(set(rights) - set(role.perms)),
            "removed": sorted(set(role.perms) - set(rights)),
            "members": len(affected),
        },
    )
    return Outcome(StaffRole(role.id, role.name, rights, len(affected)), affected)


async def toggle_perm(
    conn: AsyncConnection,
    actor: Actor | None,
    role_id: int,
    perm: str,
    *,
    catalogue: Iterable[str] = CORE_PERMS,
) -> Outcome:
    role = await get_role(conn, role_id)
    if role is None:
        raise RoleError("not_found", "Роли уже нет.")
    wanted = set(role.perms) ^ {perm}
    return await set_perms(conn, actor, role_id, wanted, catalogue=catalogue)


async def delete_role(conn: AsyncConnection, actor: Actor | None, role_id: int) -> Outcome:
    """Delete a role; its members lose access (they stay users of the bot)."""
    actor, role = await _editable(conn, actor, role_id)
    rows = (
        await conn.execute(
            sa.update(users)
            .where(users.c.staff_role_id == role.id, users.c.role != "owner")
            .values(perms=[], role="user", staff_role_id=None)
            .returning(users.c.id, users.c.telegram_id)
        )
    ).all()
    affected = [
        Member(int(r.id), int(r.telegram_id) if r.telegram_id is not None else None, "user", (), False)
        for r in rows
    ]
    await conn.execute(sa.delete(staff_roles).where(staff_roles.c.id == role.id))
    await roles.audit(
        conn,
        actor,
        "role.delete",
        target=f"staff_role:{role.id}",
        details={"name": role.name, "perms": list(role.perms), "members": [m.user_id for m in affected]},
    )
    return Outcome(None, affected)


# ------------------------------------------------------------------------------------------------ people


async def _changeable(
    conn: AsyncConnection, actor: Actor, target_user_id: int, owner_ids: Iterable[int]
) -> Target:
    target = await get_target(conn, target_user_id, lock=True)
    if target is None:
        raise RoleError("not_found", "Пользователь не найден.")
    if actor.user_id is not None and target.user_id == actor.user_id:
        raise RoleError("self", "Свою роль менять нельзя.")
    if target.telegram_id is not None and target.telegram_id in frozenset(owner_ids):
        raise RoleError("configured_owner", "Этот владелец задан в OWNER_IDS, меняется только в настройках.")
    if target.role == "owner" and not actor.is_owner:
        raise RoleError("owner", "Владельца может менять только владелец.")
    if not actor.is_owner:
        scoped = target.staff_role_id is not None
        if not _fits(actor, effective_perms(target.role, target.perms, scoped)):
            raise RoleError("escalation", "У этого человека есть права, которых нет у вас.")
    return target


async def assign(
    conn: AsyncConnection,
    actor: Actor | None,
    target_user_id: int,
    role_id: int,
    *,
    owner_ids: Iterable[int] = (),
) -> Outcome:
    """Give a person a role (instead of whatever they had)."""
    actor = _require_manager(actor)
    role = await get_role(conn, role_id)
    if role is None:
        raise RoleError("not_found", "Роли уже нет.")
    target = await _changeable(conn, actor, target_user_id, owner_ids)
    if not _fits(actor, role.perms):
        raise RoleError("escalation", "У этой роли есть права, которых нет у вас.")
    if target.banned:
        raise RoleError("banned", "Пользователь заблокирован, сначала разблокируйте.")
    if target.staff_role_id == role.id and target.role != "owner":
        return Outcome(role, changed=False)
    tier = role.tier
    await conn.execute(
        sa.update(users)
        .where(users.c.id == target.user_id)
        .values(staff_role_id=role.id, perms=list(role.perms), role=tier)
    )
    await roles.audit(
        conn,
        actor,
        "role.assign",
        target=f"user:{target.user_id}",
        details={
            "role_id": role.id,
            "role": role.name,
            "old_role": target.role,
            "old_staff_role_id": target.staff_role_id,
            "perms": list(role.perms),
        },
    )
    member = Member(target.user_id, target.telegram_id, tier, role.perms, True)
    return Outcome(StaffRole(role.id, role.name, role.perms, role.members + 1), [member])


async def unassign(
    conn: AsyncConnection, actor: Actor | None, target_user_id: int, *, owner_ids: Iterable[int] = ()
) -> Outcome:
    """Take a person out of the team: no role, no rights (they stay a user of the bot)."""
    actor = _require_manager(actor)
    target = await _changeable(conn, actor, target_user_id, owner_ids)
    if target.role == "user" and target.staff_role_id is None:
        return Outcome(None, changed=False)
    await conn.execute(
        sa.update(users).where(users.c.id == target.user_id).values(staff_role_id=None, perms=[], role="user")
    )
    await roles.audit(
        conn,
        actor,
        "role.unassign",
        target=f"user:{target.user_id}",
        details={
            "old_role": target.role,
            "old_staff_role_id": target.staff_role_id,
            "old_perms": list(target.perms),
        },
    )
    return Outcome(None, [Member(target.user_id, target.telegram_id, "user", (), False)])
