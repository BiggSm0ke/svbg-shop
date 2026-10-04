"""«👮 Команда» (admin → ⚙️ Система): who helps run the bot and what each role may do.

The owner (and anyone whose role has «Команда и роли») creates named roles with a set of rights, gives a role
to a person and takes it away (:mod:`svbg.services.staff_roles`: a manager never gives more than they have).

* ``roles`` — the team: everyone with a role (owners from ``OWNER_IDS`` in the text), «➕ Добавить человека»,
  «🎭 Роли», team limits;
* ``rl`` — one person: their role now, a button per role to give, «🚫 Убрать из команды», for the owner
  «👑 Сделать владельцем» (``rl.ok`` confirms). An old ``<user>:<role>:<mask>`` argument opens the same
  screen;
* ``rl.add`` — «пришлите ID, @username или перешлите сообщение»: while it is open, such a message (deleted
  right away) opens the person's screen;
* ``rls`` — the roles; ``rlr`` — one role: its rights by admin section, rename, delete; ``rlg`` — the rights
  of one section as switches (applied at once to every member); ``rld`` — «Удалить роль?».

Every change is audited by the service; afterwards the cached context of everyone it touched is dropped and
their «/» menu is refreshed (:mod:`svbg.tg.admin.commands`), so a new right works on the next click. The old
action ``rla:save`` (``<user>:<role>:<mask>``, owner only) still sets a classic role: «сделать владельцем»
goes through it.
"""

from __future__ import annotations

import html
import logging
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from aiogram import Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramAPIError
from aiogram.methods import DeleteMessage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    Message,
    MessageOriginHiddenUser,
    MessageOriginUser,
)

from svbg.core.perms import CORE_PERMS, ROLES_MANAGE
from svbg.services import roles, staff_roles
from svbg.services.roles import ADMIN_PERMS, PERM_LABELS, ROLE_LABELS, RoleError
from svbg.services.staff_roles import Outcome, StaffRole, Target
from svbg.tg.admin import commands as staff_commands
from svbg.tg.admin import nav
from svbg.tg.admin.users import settings_reader
from svbg.tg.admin.users.screens import ROLE_SCREEN, SCREEN_CARD
from svbg.tg.ui import codec
from svbg.tg.ui.forms import Field, Form, text
from svbg.tg.ui.renderer import nav_button
from svbg.tg.ui.router import Access
from svbg.tg.ui.view import Redirect, View

if TYPE_CHECKING:
    from svbg.db.engine import Database
    from svbg.tg.ui.context import UserCtx
    from svbg.tg.ui.router import HandlerResult, ScreenCtx, ScreenRouter

__all__ = [
    "ACTIONS",
    "FORM_NEW",
    "FORM_RENAME",
    "GROUPS",
    "SCREEN_ADD",
    "SCREEN_CONFIRM",
    "SCREEN_DELETE",
    "SCREEN_EDIT",
    "SCREEN_GROUP",
    "SCREEN_LIST",
    "SCREEN_ROLE",
    "SCREEN_ROLES",
    "RoleScreens",
    "setup",
]

log = logging.getLogger("svbg.tg.admin.roles")

SCREEN_LIST: Final = "roles"
SCREEN_EDIT: Final = ROLE_SCREEN
SCREEN_CONFIRM: Final = "rl.ok"
SCREEN_ADD: Final = "rl.add"
SCREEN_ROLES: Final = "rls"
SCREEN_ROLE: Final = "rlr"
SCREEN_GROUP: Final = "rlg"
SCREEN_DELETE: Final = "rld"
ACTIONS: Final = "rla"
FORM_NEW: Final = "staff.role.new"
FORM_RENAME: Final = "staff.role.ren"
#: Old editor: bit ``i`` of the mask is ``ADMIN_PERMS[i]`` (kept so old buttons still save).
FULL_MASK: Final = (1 << len(ADMIN_PERMS)) - 1
MODULE_BIT0: Final = 24
MODULE_MAX: Final = 24
ADD_TTL: Final = 15 * 60.0
ADD_CACHE: Final = 1024
MEMBERS_MAX: Final = 40
_ARG_RE: Final = re.compile(r"^(\d{1,18})(?::(user|support|admin|owner):(\d{1,15}))?$")
_ID_RE: Final = re.compile(r"^\d{1,18}$")
_TOGGLE_RE: Final = re.compile(r"^(\d{1,18}):([a-z][a-z0-9_.]{0,64})$")
_PAIR_RE: Final = re.compile(r"^(\d{1,18}):(\d{1,3})$")
_ICON: Final = {"owner": "👑", "admin": "🛡", "support": "🎧", "user": "👤"}

#: Sections of the role editor (the admin's sections), each with its rights in this order. Module rights
#: (IP Guard, LTE …) are the last section; a core right missing here lands in «Система».
GROUPS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    (
        "👥 Пользователи",
        ("users.view", "users.help", "users.ban", "subs.grant", "wallet.adjust", "users.delete"),
    ),
    ("📦 Тарифы", ("plans",)),
    ("💳 Оплата", ("payments.confirm", "payments.refund")),
    ("🎯 Маркетинг", ("promo", "deeplinks")),
    ("📣 Связь", ("broadcast", "broadcast.send", "tickets")),
    ("🎨 Оформление", ("content.edit",)),
    ("📊 Статистика", ("stats",)),
    ("⚙️ Система", ("settings.business", "system.view", ROLES_MANAGE)),
)
MODULES_GROUP: Final = "🧩 Модули"

#: Labels of the switches (the admin's words, not codes).
LABELS: Final[dict[str, str]] = {
    "users.view": "Искать клиентов, смотреть карточки",
    "users.help": "Устройства, новая ссылка, написать клиенту",
    "users.ban": "Блокировать",
    "subs.grant": "Дарить дни и тарифы",
    "wallet.adjust": "Менять баланс",
    "users.delete": "Удалять клиента полностью",
    "plans": "Тарифы и цены",
    "payments.confirm": "Подтверждать ручные оплаты",
    "payments.refund": "Возвраты",
    "promo": "Промокоды и реклама",
    "deeplinks": "Ссылки на разделы бота",
    "broadcast": "Готовить рассылки",
    "broadcast.send": "Отправлять рассылки",
    "tickets": "Отвечать в поддержке",
    "content.edit": "Конструктор экранов",
    "stats": "Статистика и выручка",
    "settings.business": "Настройки, тексты, страницы",
    "system.view": "Состояние бота и логи",
    ROLES_MANAGE: "Команда и роли",
}

_T: Final[dict[str, str]] = {
    "list_hint": "Нажмите на человека, чтобы сменить или убрать роль. Что можно каждой роли, настраивается "
    "в «🎭 Роли».",
    "configured": "Владельцы из настроек: {ids}",
    "staff_empty": "Кроме владельца в команде пока никого.",
    "more": "И ещё {n}.",
    "b_add": "➕ Добавить человека",
    "b_roles": "🎭 Роли",
    "limits": "⚙️ Лимиты команды",
    "add_title": "➕ <b>Добавить в команду</b>",
    "add_hint": "Пришлите сюда Telegram ID человека, его @username или перешлите его сообщение. Человек "
    "должен хотя бы раз написать боту.",
    "add_nf": "Не нашёл «{q}». Проверьте ID или попросите человека написать боту.",
    "add_hidden": "Человек скрыл аккаунт при пересылке. Пришлите его ID или @username.",
    "edit_title": "🎖 <b>{who}</b>",
    "now": "Сейчас: {role}",
    "pick": "Выберите роль:",
    "no_roles": "Ролей пока нет, создайте первую в «🎭 Роли».",
    "self": "Это вы. Свою роль здесь поменять нельзя.",
    "is_owner": "Это владелец, его роль меняет только владелец.",
    "configured_owner": "Владелец из настроек («⚙️ Лимиты команды»), меняется только там.",
    "banned": "Заблокирован. Чтобы дать роль, сначала разблокируйте.",
    "wider": "У человека есть права, которых нет у вас, поэтому менять его роль нельзя.",
    "b_remove": "🚫 Убрать из команды",
    "b_owner": "👑 Сделать владельцем",
    "removed": "✅ Убран из команды.",
    "assigned": "✅ Роль «{name}» выдана.",
    "saved": "✅ Сохранено.",
    "unchanged": "Ничего не изменилось.",
    "no_role": "без роли",
    "role_n": "{name}, прав: {n}",
    "classic": "{role} (старая роль)",
    "confirm_owner": "👑 Сделать {who} владельцем?\n\nВладелец может всё, включая кассы, команду и системные "
    "настройки. Снять его сможет только другой владелец.",
    "yes": "✅ Да, сделать владельцем",
    "no": "⬅️ Нет",
    "to_card": "👤 Карточка",
    "to_list": "⬅️ Команда",
    "not_found": "Пользователь не найден",
    "no_name": "без имени",
    "roles_hint": "У человека одна роль. Права роли меняются сразу у всех, у кого она есть.",
    "roles_empty": "Ролей пока нет.",
    "b_new": "➕ Новая роль",
    "to_roles": "⬅️ Роли",
    "role_title": "🎭 <b>{name}</b>",
    "role_stats": "Прав: {n} · людей с ролью: {m}",
    "role_who": "У кого: {names}",
    "role_hint": "Откройте раздел и включите нужное. Меняется сразу.",
    "role_ro": "Эту роль менять нельзя: в ней есть права, которых нет у вас, или это ваша роль.",
    "role_none": "Прав нет: с этой ролью админка не откроется.",
    "b_rename": "✏️ Переименовать",
    "b_delete": "🗑 Удалить",
    "group_title": "🎭 <b>{name}</b> › {group}",
    "group_hint": "Нажмите, чтобы включить или выключить. Меняется сразу у всех с этой ролью.",
    "group_empty": "Здесь нечего выдать.",
    "gone": "Роли уже нет",
    "del_title": "🗑 Удалить роль «{name}»?",
    "del_members": "Она пропадёт у {n} чел., они останутся обычными пользователями.",
    "del_nobody": "Ни у кого её нет.",
    "del_yes": "🗑 Да, удалить",
    "deleted": "Роль удалена",
    "created": "✅ Роль создана. Теперь отметьте, что ей можно.",
    "renamed": "✅ Переименовано.",
    "ask_name": "Как назвать роль? Например: Модератор. До 40 символов.",
    "ask_rename": "Новое название роли (до 40 символов):",
}


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=False)


# ------------------------------------------------------------------------------------------------ old editor


#: ``(bit, code, label)`` of one checkbox of the old editor (kept for old ``rla:save`` buttons).
Item = tuple[int, str, str]
_CORE_ITEMS: Final[tuple[Item, ...]] = tuple((i, p, PERM_LABELS.get(p, p)) for i, p in enumerate(ADMIN_PERMS))
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


# ------------------------------------------------------------------------------------------------ helpers


def _who(first_name: str | None, username: str | None, user_id: int) -> str:
    name = (first_name or "").strip()[:48] or _T["no_name"]
    return _esc(name + (f" @{username[:48]}" if username else "")) + f" (№ {user_id})"


def _short(first_name: str | None, username: str | None) -> str:
    name = (first_name or "").strip()[:24] or _T["no_name"]
    return name + (f" @{username[:24]}" if username else "")


@dataclass(frozen=True, slots=True)
class _Note:
    """Argument of a screen shown with a line on top (never encoded into a button)."""

    text: str
    arg: str | None = None


def _split(arg: Any) -> tuple[str | None, str | None]:
    if isinstance(arg, _Note):
        return arg.arg, arg.text
    return (arg if isinstance(arg, str) else None), None


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
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.router = router
        self.db = db
        self.owner_ids = owner_ids
        self.configured_owners = configured_owners
        self.invalidate = invalidate
        self.module_perms = module_perms
        self._clock = clock
        self._armed: OrderedDict[int, float] = OrderedDict()  # telegram id → when «добавить» was shown
        self._installed = False

    # ------------------------------------------------------------ catalogue

    def items(self) -> list[Item]:
        """The old editor's checkboxes: core + the X13 catalogue (a broken provider gives the core only)."""
        try:
            return catalogue(self.module_perms())
        except Exception:
            log.exception("module permissions catalogue failed")
            return list(_CORE_ITEMS)

    def modules(self) -> list[tuple[str, str]]:
        return [(code, label) for bit, code, label in self.items() if bit >= MODULE_BIT0]

    def groups(self) -> list[tuple[str, list[tuple[str, str]]]]:
        """``(section, [(code, label)])`` of the role editor."""
        listed = {p for _, perms in GROUPS for p in perms}
        out: list[tuple[str, list[tuple[str, str]]]] = []
        for title, perms in GROUPS:
            rows = [(p, LABELS.get(p) or PERM_LABELS.get(p, p)) for p in perms if p in CORE_PERMS]
            if title.startswith("⚙️"):
                rows += [(p, LABELS.get(p) or PERM_LABELS.get(p, p)) for p in CORE_PERMS if p not in listed]
            out.append((title, rows))
        modules = self.modules()
        if modules:
            out.append((MODULES_GROUP, modules))
        return out

    def codes(self) -> list[str]:
        return [*CORE_PERMS, *(code for code, _ in self.modules())]

    def label(self, code: str) -> str:
        for _, rows in self.groups():
            for c, label in rows:
                if c == code:
                    return label
        return LABELS.get(code) or PERM_LABELS.get(code, code)

    # ------------------------------------------------------------ registration

    def install(self) -> None:
        if self._installed:
            return
        self._installed = True
        r = self.router
        team = {"required_role": "support", "perm": ROLES_MANAGE}
        r.screen(SCREEN_LIST, **team)(self._list_screen)
        r.screen(SCREEN_EDIT, **team)(self._person_screen)
        r.screen(SCREEN_ADD, **team)(self._add_screen)
        r.screen(SCREEN_ROLES, **team)(self._roles_screen)
        r.screen(SCREEN_ROLE, **team)(self._role_screen)
        r.screen(SCREEN_GROUP, **team)(self._group_screen)
        r.screen(SCREEN_DELETE, **team)(self._delete_screen)
        r.screen(SCREEN_CONFIRM, required_role="owner")(self._confirm_screen)
        r.action(ACTIONS, "save", **team)(self._save)
        r.action(ACTIONS, "as", **team)(self._assign)
        r.action(ACTIONS, "rm", **team)(self._remove)
        r.action(ACTIONS, "tg", **team)(self._toggle)
        r.action(ACTIONS, "del", **team)(self._delete)
        r.action(ACTIONS, "new", **team)(self._new)
        r.action(ACTIONS, "ren", **team)(self._rename)
        name = (Field("name", _T["ask_name"], text(max_len=staff_roles.NAME_MAX)),)
        r.form(Form(FORM_NEW, name, self._new_done, self._form_cancel, **team))
        rename = (Field("name", _T["ask_rename"], text(max_len=staff_roles.NAME_MAX)),)
        r.form(Form(FORM_RENAME, rename, self._rename_done, self._form_cancel, **team))

    # ------------------------------------------------------------ writes

    async def _write(
        self, ctx: ScreenCtx, op: Callable[[Any, roles.Actor | None], Awaitable[Outcome]]
    ) -> Outcome | str:
        """Run ``op(conn, actor)`` in one transaction with the presser re-read now; a refusal → its text."""
        tg = ctx.user.telegram_id
        owners = await self.owner_ids()
        try:
            async with self.db.tx() as conn:
                actor = (
                    await roles.load_actor(conn, telegram_id=tg, owner_ids=owners, lock=True) if tg else None
                )
                out = await op(conn, actor)
        except RoleError as e:
            return e.text
        self.refresh(out.affected)
        return out

    def refresh(self, affected: Iterable[staff_roles.Member]) -> None:
        """Drop the cached context and refresh the «/» menu of everyone a change touched."""
        for m in affected:
            if m.telegram_id is None:
                continue
            if self.invalidate is not None:
                try:
                    self.invalidate(m.telegram_id)
                except Exception:
                    log.exception("user cache invalidation failed")
            staff_commands.role_changed(self.router, m.telegram_id, m.role, m.perms, scoped=m.scoped)

    # ------------------------------------------------------------ team

    async def _list_screen(self, ctx: ScreenCtx, arg: Any) -> View:
        _, note = _split(arg)
        async with self.db.read() as conn:
            staff = await roles.staff_list(conn)
            names = {r.id: r.name for r in await staff_roles.list_roles(conn)}
        lines = [nav.header(SCREEN_LIST), ""]
        if note:
            lines += [note, ""]
        configured = sorted(self.configured_owners())
        if configured:
            lines += [_T["configured"].format(ids=", ".join(f"<code>{i}</code>" for i in configured)), ""]
        rows: list[list[InlineKeyboardButton]] = [
            [nav_button(_T["b_add"], SCREEN_ADD), nav_button(_T["b_roles"], SCREEN_ROLES)]
        ]
        for m in staff[:MEMBERS_MAX]:
            label = f"{_ICON.get(m.role, '•')} {_short(m.first_name, m.username)}"
            if m.scoped and m.staff_role_id is not None:
                label += f" · {names.get(m.staff_role_id, '?')}"
            else:
                label += f" · {ROLE_LABELS.get(m.role, m.role)}"
            if m.banned_at is not None:
                label = "⛔️ " + label
            rows.append([nav_button(label[:64], SCREEN_EDIT, arg=str(m.user_id))])
        if len(staff) > MEMBERS_MAX:
            lines.append(_T["more"].format(n=len(staff) - MEMBERS_MAX))
        if not staff:
            lines.append(_T["staff_empty"])
        lines.append(_T["list_hint"])
        if ctx.user.role == "owner" and nav.has_screen(self.router, "set.v"):
            rows.append([nav_button(_T["limits"], "set.v", arg="sys.team")])
        rows.append(nav.back_row(SCREEN_LIST))
        return View(text="\n".join(lines), parse_mode="HTML", keyboard=rows)

    # ------------------------------------------------------------ add a person

    async def _add_screen(self, ctx: ScreenCtx, arg: Any) -> View:
        _, note = _split(arg)
        lines = [_T["add_title"], ""]
        if note:
            lines += [_esc(note), ""]
        lines.append(_T["add_hint"])
        self.arm(ctx.user)
        return View(
            text="\n".join(lines),
            parse_mode="HTML",
            keyboard=[nav.with_admin([nav_button(_T["to_list"], SCREEN_LIST)])],
        )

    def arm(self, user: UserCtx) -> None:
        tg = user.telegram_id
        if tg is None:
            return
        self._armed[tg] = self._clock()
        self._armed.move_to_end(tg)
        while len(self._armed) > ADD_CACHE:
            self._armed.popitem(last=False)

    def armed(self, telegram_id: int) -> bool:
        at = self._armed.get(telegram_id)
        return at is not None and self._clock() - at <= ADD_TTL

    def seen_callback(self, telegram_id: int, data: str | None) -> None:
        """Any other button ends the «добавить» mode (the screen arms it again when it renders)."""
        if telegram_id not in self._armed:
            return
        decoded = codec.decode(data)
        if decoded is None or decoded.screen != SCREEN_ADD or decoded.action != codec.ACTION_OPEN:
            self._armed.pop(telegram_id, None)

    @staticmethod
    def query_of(message: Message) -> str | None:
        """What to look for: the text, the sender of a forwarded message (``""`` — they hid their account)."""
        origin = message.forward_origin
        if isinstance(origin, MessageOriginUser):
            return str(origin.sender_user.id)
        if isinstance(origin, MessageOriginHiddenUser):
            return ""
        if origin is not None:
            return None
        value = (message.text or "").strip()
        if not value or value.startswith("/"):
            return None
        return value[:128]

    def wants(self, message: Message) -> bool:
        if message.chat.type != "private" or message.from_user is None:
            return False
        if (message.text or "").lstrip().startswith("/"):
            self._armed.pop(message.from_user.id, None)
            return False
        return self.armed(message.from_user.id) and self.query_of(message) is not None

    async def handle_add(self, message: Message) -> bool:
        """``True`` when the message named a person (answered); ``False`` lets other handlers see it."""
        if message.from_user is None or not self.armed(message.from_user.id):
            return False
        query = self.query_of(message)
        if query is None:
            return False
        try:
            user = await self.router.user_loader(message.from_user)
        except Exception:
            log.exception("user loader failed for the team search")
            return False
        if user is None or not Access("support", ROLES_MANAGE).allows(user):
            return False
        if await self.router.is_awaiting(message):
            return False
        target: Target | None = None
        if query:
            async with self.db.read() as conn:
                target = await staff_roles.find_user(conn, query)
        deleted = await self._delete_message(message)
        chat = message.chat.id
        if target is not None:
            self._armed.pop(message.from_user.id, None)
            await self.router.show(user, chat, SCREEN_EDIT, str(target.user_id), new=not deleted)
        else:
            note = _T["add_hidden"] if not query else _T["add_nf"].format(q=query[:64])
            await self.router.show(user, chat, SCREEN_ADD, _Note(note), new=not deleted)
        return True

    async def _delete_message(self, message: Message) -> bool:
        try:
            ok = await self.router.transport.call(
                DeleteMessage(chat_id=message.chat.id, message_id=message.message_id), chat_id=message.chat.id
            )
        except (TelegramAPIError, OSError, TimeoutError) as e:
            log.debug("could not delete a team query: %s", type(e).__name__)
            return False
        return ok is not False and ok is not None

    def aiogram_router(self, name: str = "svbg-admin-team") -> Router:
        router = Router(name=name)

        async def add_filter(message: Message) -> bool:
            return self.wants(message)

        async def on_add(message: Message) -> None:
            if not await self.handle_add(message):
                raise SkipHandler

        async def observe(query: CallbackQuery) -> bool:
            self.seen_callback(query.from_user.id, query.data)
            return False  # only looks: the screen router answers every button

        async def never(_query: CallbackQuery) -> None:  # pragma: no cover - the filter never passes
            raise SkipHandler

        router.message.register(on_add, add_filter)
        router.callback_query.register(never, observe)
        return router

    # ------------------------------------------------------------ one person

    def _parse_person(self, arg: Any) -> int | None:
        if not isinstance(arg, str):
            return None
        m = _ARG_RE.match(arg)
        return int(m[1]) if m is not None else None

    async def _person_screen(self, ctx: ScreenCtx, arg: Any) -> View | Redirect:
        raw, note = _split(arg)
        uid = self._parse_person(raw)
        if uid is None:
            return Redirect(SCREEN_LIST, toast=_T["not_found"])
        return await self.person_view(ctx, uid, note=note)

    async def person_view(self, ctx: ScreenCtx, uid: int, *, note: str | None = None) -> View:
        viewer = roles.actor_of(ctx.user)
        async with self.db.read() as conn:
            target = await staff_roles.get_target(conn, uid)
            all_roles = await staff_roles.list_roles(conn)
        back = nav.with_admin([nav_button(_T["to_list"], SCREEN_LIST)])
        if target is None:
            return View(text=_T["not_found"], keyboard=[back])
        card: list[InlineKeyboardButton] = []
        if nav.has_screen(self.router, SCREEN_CARD) and nav.can_open(self.router, ctx.user, SCREEN_CARD):
            card.append(nav_button(_T["to_card"], SCREEN_CARD, arg=str(uid)))
        configured = target.telegram_id is not None and target.telegram_id in self.configured_owners()
        role_name = {r.id: r.name for r in all_roles}
        lines = [_T["edit_title"].format(who=_who(target.first_name, target.username, target.user_id)), ""]
        if note:
            lines = [note, "", *lines]
        lines.append(_T["now"].format(role=_esc(self._role_text(target, role_name, configured))))
        rows: list[list[InlineKeyboardButton]] = []
        reason = self._locked(viewer, target, configured)
        if reason is not None:
            lines += ["", reason]
        else:
            if target.banned:
                lines += ["", _T["banned"]]
            usable = [r for r in all_roles if viewer.is_owner or all(viewer.has_perm(p) for p in r.perms)]
            if usable and not target.banned:
                lines += ["", _T["pick"]]
                current = target.staff_role_id if target.role != "owner" else None
                buttons = [
                    nav_button(
                        (("• " if r.id == current else "") + r.name)[:64], ACTIONS, "as", f"{uid}:{r.id}"
                    )
                    for r in usable
                ]
                rows.extend(buttons[i : i + 2] for i in range(0, len(buttons), 2))
            elif not all_roles:
                lines += ["", _T["no_roles"]]
            if target.role != "user" or target.staff_role_id is not None:
                rows.append([nav_button(_T["b_remove"], ACTIONS, "rm", str(uid))])
            if viewer.is_owner and target.role != "owner" and not target.banned:
                rows.append([nav_button(_T["b_owner"], SCREEN_CONFIRM, arg=str(uid))])
        if card:
            rows.append(card)
        rows.append(back)
        return View(text="\n".join(lines), parse_mode="HTML", keyboard=rows)

    @staticmethod
    def _role_text(target: Target, names: dict[int, str], configured: bool) -> str:
        if configured or target.role == "owner":
            return ROLE_LABELS["owner"]
        if target.staff_role_id is not None:
            name = names.get(target.staff_role_id, "?")
            return _T["role_n"].format(name=name, n=len(target.perms))
        if target.role == "user":
            return _T["no_role"]
        return _T["classic"].format(role=ROLE_LABELS.get(target.role, target.role))

    def _locked(self, viewer: roles.Actor, target: Target, configured: bool) -> str | None:
        """Why the viewer may not change this person (``None`` — they may)."""
        if viewer.user_id is not None and viewer.user_id == target.user_id:
            return _T["self"]
        if configured:
            return _T["configured_owner"]
        if target.role == "owner" and not viewer.is_owner:
            return _T["is_owner"]
        if not viewer.is_owner:
            perms = staff_roles.effective_perms(target.role, target.perms, target.staff_role_id is not None)
            if not all(viewer.has_perm(p) for p in perms):
                return _T["wider"]
        return None

    async def _assign(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        m = _PAIR_RE.match(arg) if isinstance(arg, str) else None
        if m is None:
            return Redirect(SCREEN_LIST, toast=_T["not_found"])
        uid, rid = int(m[1]), int(m[2])
        owners = self.configured_owners()

        async def op(conn: Any, actor: roles.Actor | None) -> Outcome:
            return await staff_roles.assign(conn, actor, uid, rid, owner_ids=owners)

        out = await self._write(ctx, op)
        if isinstance(out, str):
            return await self.person_view(ctx, uid, note=f"⚠️ {_esc(out)}")
        if not out.changed:
            return await self.person_view(ctx, uid, note=_T["unchanged"])
        name = out.role.name if out.role is not None else ""
        return await self.person_view(ctx, uid, note=_T["assigned"].format(name=_esc(name)))

    async def _remove(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        if not isinstance(arg, str) or not _ID_RE.match(arg):
            return Redirect(SCREEN_LIST, toast=_T["not_found"])
        uid = int(arg)
        owners = self.configured_owners()

        async def op(conn: Any, actor: roles.Actor | None) -> Outcome:
            return await staff_roles.unassign(conn, actor, uid, owner_ids=owners)

        out = await self._write(ctx, op)
        if isinstance(out, str):
            return await self.person_view(ctx, uid, note=f"⚠️ {_esc(out)}")
        return await self.person_view(ctx, uid, note=_T["removed"] if out.changed else _T["unchanged"])

    # ------------------------------------------------------------ «сделать владельцем» and old buttons

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

    async def _confirm_screen(self, ctx: ScreenCtx, arg: Any) -> View | Redirect:
        parsed = self._parse(arg)
        if parsed is None or parsed[1] not in (None, "owner"):
            return Redirect(SCREEN_LIST, toast=_T["not_found"])
        async with self.db.read() as conn:
            target = await staff_roles.get_target(conn, parsed[0])
        if target is None:
            return Redirect(SCREEN_LIST, toast=_T["not_found"])
        who = _who(target.first_name, target.username, target.user_id)
        return View(
            text=_T["confirm_owner"].format(who=who),
            parse_mode="HTML",
            keyboard=[
                [nav_button(_T["yes"], ACTIONS, "save", f"{parsed[0]}:owner:0", style="danger")],
                [nav_button(_T["no"], SCREEN_EDIT, arg=str(parsed[0]))],
            ],
        )

    async def _save(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        """Old ``<user>:<role>:<mask>`` buttons: a classic role (owner only), also «сделать владельцем»."""
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
            return await self.person_view(ctx, uid, note=f"⚠️ {_esc(e.text)}")
        self.refresh(
            [staff_roles.Member(change.user_id, change.telegram_id, change.new_role, change.new_perms, False)]
        )
        return await self.person_view(ctx, uid, note=_T["saved"] if change.changed else _T["unchanged"])

    # ------------------------------------------------------------ roles

    async def _roles_screen(self, ctx: ScreenCtx, arg: Any) -> View:
        _, note = _split(arg)
        async with self.db.read() as conn:
            all_roles = await staff_roles.list_roles(conn)
        lines = [nav.header(SCREEN_ROLES), ""]
        if note:
            lines += [note, ""]
        lines.append(_T["roles_hint"] if all_roles else _T["roles_empty"])
        viewer = roles.actor_of(ctx.user)
        rows: list[list[InlineKeyboardButton]] = []
        for r in all_roles:
            lock = "" if staff_roles.can_edit(viewer, r, ctx.user.staff_role) else "🔒 "
            label = f"{lock}{r.name} · прав: {len(r.perms)} · людей: {r.members}"
            rows.append([nav_button(label[:64], SCREEN_ROLE, arg=str(r.id))])
        if len(all_roles) < staff_roles.MAX_ROLES:
            rows.append([nav_button(_T["b_new"], ACTIONS, "new")])
        rows.append(nav.with_admin([nav_button(_T["to_list"], SCREEN_LIST)]))
        return View(text="\n".join(lines), parse_mode="HTML", keyboard=rows)

    async def _load_role(self, rid: int) -> tuple[StaffRole | None, list[Target]]:
        async with self.db.read() as conn:
            role = await staff_roles.get_role(conn, rid)
            members = await staff_roles.members_of(conn, rid, limit=6) if role is not None else []
        return role, members

    async def _role_screen(self, ctx: ScreenCtx, arg: Any) -> View | Redirect:
        raw, note = _split(arg)
        if raw is None or not _ID_RE.match(raw):
            return Redirect(SCREEN_ROLES, toast=_T["gone"])
        return await self.role_view(ctx, int(raw), note=note)

    async def role_view(self, ctx: ScreenCtx, rid: int, *, note: str | None = None) -> View | Redirect:
        role, members = await self._load_role(rid)
        if role is None:
            return Redirect(SCREEN_ROLES, toast=_T["gone"])
        editable = staff_roles.can_edit(roles.actor_of(ctx.user), role, ctx.user.staff_role)
        lines = [_T["role_title"].format(name=_esc(role.name)), ""]
        if note:
            lines = [note, "", *lines]
        lines.append(_T["role_stats"].format(n=len(role.perms), m=role.members))
        if members:
            names = ", ".join(_esc(_short(m.first_name, m.username)) for m in members[:5])
            if role.members > 5:
                names += " …"
            lines.append(_T["role_who"].format(names=names))
        lines.append("")
        if not role.perms:
            lines.append(_T["role_none"])
        lines.append(_T["role_hint"] if editable else _T["role_ro"])
        rows: list[list[InlineKeyboardButton]] = []
        have = set(role.perms)
        buttons = []
        for i, (title, perms) in enumerate(self.groups()):
            on = sum(1 for code, _ in perms if code in have)
            if not editable:
                if on:
                    lines.append(f"{title}: " + ", ".join(_esc(lb) for code, lb in perms if code in have))
                continue
            buttons.append(nav_button(f"{title} · {on}/{len(perms)}", SCREEN_GROUP, arg=f"{rid}:{i}"))
        rows.extend(buttons[i : i + 2] for i in range(0, len(buttons), 2))
        if editable:
            rows.append(
                [
                    nav_button(_T["b_rename"], ACTIONS, "ren", str(rid)),
                    nav_button(_T["b_delete"], SCREEN_DELETE, arg=str(rid)),
                ]
            )
        rows.append(nav.with_admin([nav_button(_T["to_roles"], SCREEN_ROLES)]))
        return View(text="\n".join(lines), parse_mode="HTML", keyboard=rows)

    async def _group_screen(self, ctx: ScreenCtx, arg: Any) -> View | Redirect:
        raw, note = _split(arg)
        m = _PAIR_RE.match(raw) if raw is not None else None
        if m is None:
            return Redirect(SCREEN_ROLES, toast=_T["gone"])
        return await self.group_view(ctx, int(m[1]), int(m[2]), note=note)

    async def group_view(
        self, ctx: ScreenCtx, rid: int, index: int, *, note: str | None = None
    ) -> View | Redirect:
        groups = self.groups()
        role, _ = await self._load_role(rid)
        if role is None:
            return Redirect(SCREEN_ROLES, toast=_T["gone"])
        viewer = roles.actor_of(ctx.user)
        if index >= len(groups) or not staff_roles.can_edit(viewer, role, ctx.user.staff_role):
            return Redirect(SCREEN_ROLE, arg=str(rid))
        title, perms = groups[index]
        lines = [_T["group_title"].format(name=_esc(role.name), group=_esc(title)), ""]
        if note:
            lines = [note, "", *lines]
        lines.append(_T["group_hint"])
        have = set(role.perms)
        rows: list[list[InlineKeyboardButton]] = []
        for code, label in perms:
            if not (viewer.is_owner or viewer.has_perm(code)):
                continue
            arg = f"{rid}:{code}"
            if not codec.fits(ACTIONS, "tg", arg):
                continue
            mark = "✅ " if code in have else "▫️ "
            rows.append([nav_button((mark + label)[:64], ACTIONS, "tg", arg)])
        if not rows:
            lines += ["", _T["group_empty"]]
        rows.append(nav.with_admin([nav_button(f"⬅️ {role.name}"[:48], SCREEN_ROLE, arg=str(rid))]))
        return View(text="\n".join(lines), parse_mode="HTML", keyboard=rows)

    def _group_of(self, code: str) -> int:
        for i, (_, perms) in enumerate(self.groups()):
            if any(c == code for c, _ in perms):
                return i
        return 0

    async def _toggle(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        m = _TOGGLE_RE.match(arg) if isinstance(arg, str) else None
        if m is None:
            return Redirect(SCREEN_ROLES, toast=_T["gone"])
        rid, code = int(m[1]), m[2]
        codes = self.codes()

        async def op(conn: Any, actor: roles.Actor | None) -> Outcome:
            return await staff_roles.toggle_perm(conn, actor, rid, code, catalogue=codes)

        out = await self._write(ctx, op)
        if isinstance(out, str):
            return await self.role_view(ctx, rid, note=f"⚠️ {_esc(out)}")
        return await self.group_view(ctx, rid, self._group_of(code))

    async def _delete_screen(self, ctx: ScreenCtx, arg: Any) -> View | Redirect:
        if not isinstance(arg, str) or not _ID_RE.match(arg):
            return Redirect(SCREEN_ROLES, toast=_T["gone"])
        role, _ = await self._load_role(int(arg))
        if role is None:
            return Redirect(SCREEN_ROLES, toast=_T["gone"])
        lines = [
            _T["del_title"].format(name=_esc(role.name)),
            "",
            _T["del_members"].format(n=role.members) if role.members else _T["del_nobody"],
        ]
        return View(
            text="\n".join(lines),
            parse_mode="HTML",
            keyboard=[
                [nav_button(_T["del_yes"], ACTIONS, "del", str(role.id), style="danger")],
                [nav_button(_T["no"], SCREEN_ROLE, arg=str(role.id))],
            ],
        )

    async def _delete(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        if not isinstance(arg, str) or not _ID_RE.match(arg):
            return Redirect(SCREEN_ROLES, toast=_T["gone"])
        rid = int(arg)

        async def op(conn: Any, actor: roles.Actor | None) -> Outcome:
            return await staff_roles.delete_role(conn, actor, rid)

        out = await self._write(ctx, op)
        if isinstance(out, str):
            return await self.role_view(ctx, rid, note=f"⚠️ {_esc(out)}")
        return Redirect(SCREEN_ROLES, toast=_T["deleted"])

    # ------------------------------------------------------------ create / rename

    async def _new(self, ctx: ScreenCtx, _arg: Any) -> HandlerResult:
        return await ctx.start_form(FORM_NEW)

    async def _rename(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        if not isinstance(arg, str) or not _ID_RE.match(arg):
            return Redirect(SCREEN_ROLES, toast=_T["gone"])
        return await ctx.start_form(FORM_RENAME, {"rid": int(arg)})

    async def _form_cancel(self, ctx: ScreenCtx) -> HandlerResult:
        return await self._roles_screen(ctx, None)

    async def _new_done(self, ctx: ScreenCtx, data: dict[str, Any]) -> HandlerResult:
        name = str(data.get("name") or "")

        async def op(conn: Any, actor: roles.Actor | None) -> Outcome:
            return await staff_roles.create_role(conn, actor, name, catalogue=self.codes())

        out = await self._write(ctx, op)
        if isinstance(out, str) or out.role is None:
            return await self._roles_screen(ctx, _Note(f"⚠️ {_esc(out if isinstance(out, str) else '')}"))
        return await self.role_view(ctx, out.role.id, note=_T["created"])

    async def _rename_done(self, ctx: ScreenCtx, data: dict[str, Any]) -> HandlerResult:
        rid = data.get("rid")
        if not isinstance(rid, int):
            return await self._roles_screen(ctx, None)
        name = str(data.get("name") or "")

        async def op(conn: Any, actor: roles.Actor | None) -> Outcome:
            return await staff_roles.rename_role(conn, actor, rid, name)

        out = await self._write(ctx, op)
        if isinstance(out, str):
            return await self.role_view(ctx, rid, note=f"⚠️ {_esc(out)}")
        return await self.role_view(ctx, rid, note=_T["renamed"] if out.changed else _T["unchanged"])


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

    screens = RoleScreens(
        router,
        deps.db,
        owner_ids=owner_ids,
        configured_owners=configured,
        invalidate=invalidate,
        module_perms=module_perms,
    )
    screens.install()
    return screens.aiogram_router()
