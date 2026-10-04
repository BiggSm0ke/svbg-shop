"""Staff rights shared by the UI context (:mod:`svbg.tg.ui.context`) and the roles service
(:mod:`svbg.services.roles`): no SQL, no aiogram.

* :data:`ADMIN_PERMS` — the Admin column of 04 §9.1 (``*`` of a classic admin expands to it and only to it).
* :data:`SUPPORT_PERMS` — the Support column: client cards and search, help with devices and links, tickets.
  Before custom roles every staff member had it by rank; staff **without** a custom role still do
  (:func:`implicit`), together with the «view» rights of modules (``ip_guard.view``, ``lte.view``).
* :data:`ROLES_MANAGE` — «Команда и роли»: never part of ``*``, granted only through a custom role.
* :data:`CORE_PERMS` — every core right a custom role may hold, in the order of the role editor.

A member of a custom role (``users.staff_role_id``) has exactly the rights of the role: nothing implicit.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

__all__ = [
    "ADMIN_PERMS",
    "ALL_PERMS",
    "CORE_PERMS",
    "ROLES_MANAGE",
    "SUPPORT_PERMS",
    "implicit",
    "support_level",
    "tier_of",
]

ALL_PERMS: Final = "*"

#: The Admin column of 04 §9.1 (order = bit order of the old role editor; append only).
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
    "users.delete",
)

USERS_VIEW: Final = "users.view"
USERS_HELP: Final = "users.help"
ROLES_MANAGE: Final = "roles.manage"

#: The Support column (what a classic «поддержка» could do).
SUPPORT_PERMS: Final[tuple[str, ...]] = (USERS_VIEW, USERS_HELP, "tickets")

#: Every core right of a custom role.
CORE_PERMS: Final[tuple[str, ...]] = (*ADMIN_PERMS, USERS_VIEW, USERS_HELP, ROLES_MANAGE)
_CORE: Final = frozenset(CORE_PERMS)


def _module_view(perm: str) -> bool:
    return perm not in _CORE and "." in perm and perm.endswith(".view")


def implicit(perm: str) -> bool:
    """A right every staff member without a custom role has by rank (the Support column, module views)."""
    return perm in SUPPORT_PERMS or _module_view(perm)


def support_level(perm: str) -> bool:
    """A right that does not lift a custom role above «поддержка» (no sums, no money, no settings)."""
    return implicit(perm)


def tier_of(perms: Iterable[str]) -> str:
    """The stored rank (``users.role``) of a custom role's member: ``user`` for a role without rights,
    ``support`` when every right is support-level, else ``admin``."""
    wanted = [p for p in perms if p != ALL_PERMS]
    if not wanted:
        return "user"
    return "support" if all(support_level(p) for p in wanted) else "admin"
