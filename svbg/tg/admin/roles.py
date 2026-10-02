"""«👥 Роли» — grant and revoke staff roles (04 §9.1: «назначение ролей» is Owner only).

* ``roles`` — the staff list (owners from ``OWNER_IDS`` and everyone with a stored role) with a button per
  person;
* ``rl`` — the role editor of one user: role (пользователь / поддержка / админ / владелец) and, for an admin,
  the rights of the Admin column as checkboxes (new admins start with all of them, 04 §9.1) followed by the
  X13 module rights (``module_perms``: IP Guard, LTE…; bits from :data:`MODULE_BIT0`, never on by default —
  they are granted one by one). The draft lives in the callback argument (``<user>:<role>:<bitmask>``),
  nothing is written until «💾 Сохранить». A stored module right whose module is not loaded is not shown and
  is dropped on the next save;
* ``rl.ok`` — «Сделать владельцем?» confirmation.

Saving goes through :func:`svbg.services.roles.set_role` (owner re-checked in the database, ``admin_audit``
in the same transaction), then the target's cached context is dropped so the new rights apply on the next
click, and their «/» command menu is refreshed (:mod:`svbg.tg.admin.commands`). The editor is opened from the
user card («🎖 Роль») or from this list (admin → «⚙️ Система» → «👮 Команда»).
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, Any, Final

import sqlalchemy as sa
from aiogram import Router
from aiogram.types import InlineKeyboardButton

from svbg.core.tables import users
from svbg.services import roles
from svbg.services.roles import ADMIN_PERMS, PERM_LABELS, ROLE_LABELS, RoleError
from svbg.tg.admin import commands as staff_commands
from svbg.tg.admin import nav
from svbg.tg.admin.users import settings_reader
from svbg.tg.admin.users.screens import ROLE_SCREEN, SCREEN_CARD, SCREEN_FIND
from svbg.tg.ui.renderer import nav_button
from svbg.tg.ui.view import Redirect, View

if TYPE_CHECKING:
    from svbg.db.engine import Database
    from svbg.tg.ui.router import HandlerResult, ScreenCtx, ScreenRouter

__all__ = ["ACTIONS", "SCREEN_CONFIRM", "SCREEN_EDIT", "SCREEN_LIST", "RoleScreens", "setup"]

log = logging.getLogger("svbg.tg.admin.roles")

SCREEN_LIST: Final = "roles"
SCREEN_EDIT: Final = ROLE_SCREEN
SCREEN_CONFIRM: Final = "rl.ok"
ACTIONS: Final = "rla"
FULL_MASK: Final = (1 << len(ADMIN_PERMS)) - 1
#: Module rights start at this bit (core rights may grow up to it without shifting module bits).
MODULE_BIT0: Final = 24
MODULE_MAX: Final = 24
_ARG_RE: Final = re.compile(r"^(\d{1,18})(?::(user|support|admin|owner):(\d{1,15}))?$")
_ICON: Final = {"owner": "👑", "admin": "🛡", "support": "🎧", "user": "👤"}
_ROLE_ORDER: Final = ("user", "support", "admin", "owner")

_T: Final[dict[str, str]] = {
    "list_title": "👮 <b>Команда</b>",
    "list_hint": "Чтобы выдать роль, найдите человека и нажмите «🎖 Роль» в его карточке. "
    "Человек должен хотя бы раз написать боту.",
    "configured": "Владельцы из настроек (OWNER_IDS): {ids}",
    "staff_empty": "Других сотрудников пока нет.",
    "edit_title": "🎖 <b>Роль</b> · {who}",
    "current": "Сейчас: {role}",
    "draft": "Будет: {role}",
    "perms_hint": "Права админа (нажмите, чтобы включить или выключить):",
    "modules_hint": "🧩 — права модулей: «все права» их не включают, выдаются по одному.",
    "role_help": "Поддержка видит карточки и помогает, но не видит сумм и не меняет деньги и сроки.",
    "save": "💾 Сохранить",
    "saved": "✅ Сохранено.",
    "unchanged": "Ничего не изменилось.",
    "confirm_owner": "👑 Сделать {who} владельцем?\n\nВладелец получает все права, включая назначение ролей "
    "и системные настройки. Снять роль владельца сможет только другой владелец.",
    "yes": "✅ Да, сделать владельцем",
    "no": "⬅️ Нет",
    "to_card": "⬅️ К карточке",
    "to_list": "⬅️ Роли",
    "admin": "⬅️ Админка",
    "find": "🔍 Найти пользователя",
    "limits": "⚙️ Лимиты команды",
    "not_found": "Пользователь не найден",
    "no_name": "без имени",
}


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=False)


#: ``(bit, code, label)`` of one checkbox of the editor.
Item = tuple[int, str, str]
_CORE_ITEMS: Final[tuple[Item, ...]] = tuple((i, p, PERM_LABELS[p]) for i, p in enumerate(ADMIN_PERMS))
if len(ADMIN_PERMS) > MODULE_BIT0:  # pragma: no cover - a guard for whoever extends the core column
    raise RuntimeError("core rights overlap module bits: raise MODULE_BIT0")


def catalogue(module_perms: Iterable[tuple[str, str]] = ()) -> list[Item]:
    """Core rights at bits ``0..``, then valid, distinct module rights ``(code, title)`` from
    :data:`MODULE_BIT0` (at most :data:`MODULE_MAX`)."""
    items = list(_CORE_ITEMS)
    seen: set[str] = set()
    for code, title in module_perms:
        if len(seen) >= MODULE_MAX:
            break
        if not roles.is_module_perm(code) or code in seen:
            continue
        label = str(title or code).strip()[:56] or code
        items.append((MODULE_BIT0 + len(seen), code, label))
        seen.add(code)
    return items


def mask_of(perms: Iterable[str], items: Iterable[Item] = _CORE_ITEMS) -> int:
    wanted = set(perms)
    return sum(1 << bit for bit, code, _ in items if code in wanted)


def perms_of(mask: int, items: Iterable[Item] = _CORE_ITEMS) -> list[str]:
    return [code for bit, code, _ in items if mask & (1 << bit)]


def full_mask(items: Iterable[Item]) -> int:
    return sum(1 << bit for bit, _, _ in items)


def _who(first_name: str | None, username: str | None, user_id: int) -> str:
    name = (first_name or "").strip()[:48] or _T["no_name"]
    return _esc(name + (f" @{username[:48]}" if username else "")) + f" (№ {user_id})"


class RoleScreens:
    """See the module docstring."""

    def __init__(
        self,
        router: ScreenRouter,
        db: Database,
        *,
        owner_ids: Callable[[], Awaitable[frozenset[int]]],
        configured_owners: Callable[[], frozenset[int]] = frozenset,
        invalidate: Callable[[int | None], None] | None = None,
        module_perms: Callable[[], Iterable[tuple[str, str]]] = tuple,
    ) -> None:
        self.router = router
        self.db = db
        self.owner_ids = owner_ids
        self.configured_owners = configured_owners
        self.invalidate = invalidate
        self.module_perms = module_perms
        self._installed = False

    def items(self) -> list[Item]:
        """The checkboxes now: core + the X13 catalogue (a broken provider gives the core only)."""
        try:
            return catalogue(self.module_perms())
        except Exception:
            log.exception("module permissions catalogue failed")
            return list(_CORE_ITEMS)

    def install(self) -> None:
        if self._installed:
            return
        self._installed = True
        r = self.router
        r.screen(SCREEN_LIST, required_role="owner")(self._list_screen)
        r.screen(SCREEN_EDIT, required_role="owner")(self._edit_screen)
        r.screen(SCREEN_CONFIRM, required_role="owner")(self._confirm_screen)
        r.action(ACTIONS, "save", required_role="owner")(self._save)

    # ------------------------------------------------------------ list

    async def _list_screen(self, ctx: ScreenCtx, _arg: Any) -> View:
        async with self.db.read() as conn:
            staff = await roles.staff_list(conn)
        lines = [_T["list_title"], ""]
        configured = sorted(self.configured_owners())
        if configured:
            lines.append(_T["configured"].format(ids=", ".join(f"<code>{i}</code>" for i in configured)))
            lines.append("")
        rows: list[list[InlineKeyboardButton]] = []
        for m in staff:
            label = f"{_ICON.get(m.role, '•')} {(m.first_name or '').strip()[:24] or _T['no_name']}"
            if m.username:
                label += f" @{m.username[:24]}"
            label += f" · {ROLE_LABELS.get(m.role, m.role)}"
            if m.role == "admin":
                label += f" ({len(m.perms)})"
            if m.banned_at is not None:
                label = "⛔️ " + label
            rows.append([nav_button(label[:64], SCREEN_EDIT, arg=str(m.user_id))])
        if not staff:
            lines.append(_T["staff_empty"])
        lines.append(_T["list_hint"])
        rows.append([nav_button(_T["find"], SCREEN_FIND)])
        if nav.has_screen(self.router, "set.v"):
            rows.append([nav_button(_T["limits"], "set.v", arg="sys.team")])
        rows.append(nav.back_row(SCREEN_LIST))
        return View(text="\n".join(lines), parse_mode="HTML", keyboard=rows)

    # ------------------------------------------------------------ editor

    def _parse(self, arg: Any) -> tuple[int, str | None, int] | None:
        if not isinstance(arg, str):
            return None
        m = _ARG_RE.match(arg)
        if m is None:
            return None
        mask = int(m[3]) if m[3] is not None else 0
        if mask & ~full_mask(self.items()):
            return None
        return int(m[1]), m[2], mask

    async def _target(self, uid: int) -> Any:
        async with self.db.read() as conn:
            return (
                await conn.execute(
                    sa.select(
                        users.c.id,
                        users.c.telegram_id,
                        users.c.first_name,
                        users.c.username,
                        users.c.role,
                        users.c.perms,
                    ).where(users.c.id == uid)
                )
            ).first()

    async def _edit_screen(self, ctx: ScreenCtx, arg: Any) -> View | Redirect:
        parsed = self._parse(arg)
        if parsed is None:
            return Redirect(SCREEN_LIST, toast=_T["not_found"])
        return await self.editor(*parsed)

    async def editor(
        self, uid: int, draft_role: str | None, draft_mask: int, *, note: str | None = None
    ) -> View:
        row = await self._target(uid)
        if row is None:
            return View(text=_T["not_found"], keyboard=[[nav_button(_T["to_list"], SCREEN_LIST)]])
        items = self.items()
        current_role = str(row.role)
        current_mask = mask_of(roles.stored_perms(row.perms), items) if current_role == "admin" else 0
        current_count = len(perms_of(current_mask, items))
        if row.telegram_id is not None and row.telegram_id in self.configured_owners():
            current_role = "owner"
        role = draft_role or current_role
        if draft_role is None:
            mask = current_mask
        elif draft_role == "admin" and current_role != "admin" and draft_mask == 0:
            mask = FULL_MASK  # 04 §9.1: a new admin gets every Admin right by default
        else:
            mask = draft_mask if role == "admin" else 0
        who = _who(row.first_name, row.username, int(row.id))
        lines = [_T["edit_title"].format(who=who), ""]
        if note:
            lines = [note, "", *lines]
        cur_text = ROLE_LABELS.get(current_role, current_role)
        if current_role == "admin":
            cur_text += f" ({current_count} прав)"
        lines.append(_T["current"].format(role=cur_text))
        if (role, mask) != (current_role, current_mask if current_role == "admin" else 0):
            new_text = ROLE_LABELS.get(role, role) + (
                f" ({len(perms_of(mask, items))} прав)" if role == "admin" else ""
            )
            lines.append(_T["draft"].format(role=new_text))
        if role == "support":
            lines.append(_T["role_help"])
        rows: list[list[InlineKeyboardButton]] = []
        role_row = []
        for r in _ROLE_ORDER:
            label = ("• " if r == role else "") + ROLE_LABELS[r]
            role_row.append(nav_button(label, SCREEN_EDIT, arg=f"{uid}:{r}:{mask if r == 'admin' else 0}"))
        rows.append(role_row[:2])
        rows.append(role_row[2:])
        if role == "admin":
            lines.append("")
            lines.append(_T["perms_hint"])
            if len(items) > len(_CORE_ITEMS):
                lines.append(_T["modules_hint"])
            for bit, _code, title in items:
                on = bool(mask & (1 << bit))
                label = ("✅ " if on else "▫️ ") + ("🧩 " if bit >= MODULE_BIT0 else "") + title
                rows.append([nav_button(label[:64], SCREEN_EDIT, arg=f"{uid}:admin:{mask ^ (1 << bit)}")])
        if (role, mask) != (current_role, current_mask if current_role == "admin" else 0):
            target = SCREEN_CONFIRM if role == "owner" else None
            arg = f"{uid}:{role}:{mask}"
            if target is not None:
                rows.append([nav_button(_T["save"], target, arg=arg, style="primary")])
            else:
                rows.append([nav_button(_T["save"], ACTIONS, "save", arg, style="primary")])
        rows.append(
            [nav_button(_T["to_card"], SCREEN_CARD, arg=str(uid)), nav_button(_T["to_list"], SCREEN_LIST)]
        )
        return View(text="\n".join(lines), parse_mode="HTML", keyboard=rows)

    async def _confirm_screen(self, ctx: ScreenCtx, arg: Any) -> View | Redirect:
        parsed = self._parse(arg)
        if parsed is None or parsed[1] != "owner":
            return Redirect(SCREEN_LIST, toast=_T["not_found"])
        row = await self._target(parsed[0])
        if row is None:
            return Redirect(SCREEN_LIST, toast=_T["not_found"])
        who = _who(row.first_name, row.username, int(row.id))
        return View(
            text=_T["confirm_owner"].format(who=who),
            parse_mode="HTML",
            keyboard=[
                [nav_button(_T["yes"], ACTIONS, "save", f"{parsed[0]}:owner:0", style="danger")],
                [nav_button(_T["no"], SCREEN_EDIT, arg=str(parsed[0]))],
            ],
        )

    async def _save(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        parsed = self._parse(arg)
        if parsed is None or parsed[1] is None:
            return Redirect(SCREEN_LIST, toast=_T["not_found"])
        uid, role, mask = parsed
        items = self.items()
        modules = [code for bit, code, _ in items if bit >= MODULE_BIT0]
        tg = ctx.user.telegram_id
        owners = await self.owner_ids()
        try:
            async with self.db.tx() as conn:
                actor = (
                    await roles.load_actor(conn, telegram_id=tg, owner_ids=owners, lock=True) if tg else None
                )
                change = await roles.set_role(
                    conn,
                    actor,
                    uid,
                    role,
                    perms_of(mask, items),
                    owner_ids=self.configured_owners(),
                    module_perms=modules,
                )
        except RoleError as e:
            return await self.editor(uid, None, 0, note=f"⚠️ {_esc(e.text)}")
        if change.changed and self.invalidate is not None and change.telegram_id is not None:
            try:
                self.invalidate(change.telegram_id)
            except Exception:
                log.exception("user cache invalidation failed")
        if change.changed:
            staff_commands.role_changed(self.router, change.telegram_id, change.new_role, change.new_perms)
        return await self.editor(uid, None, 0, note=_T["saved"] if change.changed else _T["unchanged"])


def setup(router: Any, deps: Any) -> Router | None:
    """Module entry point for ``svbg.app`` (``setup(router, deps)``)."""
    directory = getattr(deps, "users", None)
    read = settings_reader(getattr(deps, "settings", None))

    def configured() -> frozenset[int]:
        if directory is not None:
            return frozenset(directory.configured_owner_ids())
        raw = read().get("OWNER_IDS") or []
        return frozenset(int(x) for x in raw if isinstance(x, int) and not isinstance(x, bool))

    def invalidate(telegram_id: int | None) -> None:
        if directory is not None and telegram_id is not None:
            directory.invalidate(telegram_id)

    async def owner_ids() -> frozenset[int]:  # OWNER_IDS; stored owners are re-read by every check
        return configured()

    def module_perms() -> list[tuple[str, str]]:
        """X13 catalogue of ``deps.extensions`` (:class:`svbg.ext.api.ExtensionRegistry`), if wired."""
        registry = getattr(deps, "extensions", None)
        listing = getattr(registry, "permissions", None)
        if not callable(listing):
            return []
        return [(perm.code, perm.title) for _module, perm in listing()]

    RoleScreens(
        router,
        deps.db,
        owner_ids=owner_ids,
        configured_owners=configured,
        invalidate=invalidate,
        module_perms=module_perms,
    ).install()
    return None
