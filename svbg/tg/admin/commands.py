"""The staff command menu: Telegram's «/» list shows each staff member the admin commands they may use.

Users keep the default list. For a staff member the bot sets ``setMyCommands`` with
``BotCommandScopeChat(chat_id=<their Telegram id>)``: owners and admins get the commands their rights allow
(``/admin``, ``/user``, ``/broadcast``, ``/plans``, ``/promos``, ``/status``, ``/settings``), support gets
``/admin`` and ``/user``; a member of a custom role gets the commands of the role's rights. The menus are set
once after the bot starts (in the background) and again for everyone a change of «👮 Команда» touched
(:func:`role_changed`); a former staff member gets ``deleteMyCommands`` for that chat.

A chat that never wrote to the bot answers «chat not found»: skipped quietly, the next role change or restart
tries again.
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from aiogram.exceptions import TelegramAPIError
from aiogram.methods import DeleteMyCommands, SetMyCommands
from aiogram.types import BotCommand, BotCommandScopeChat
from sqlalchemy.exc import SQLAlchemyError

from svbg.services import roles

if TYPE_CHECKING:
    from svbg.db.engine import Database

__all__ = ["COMMANDS", "StaffCommand", "StaffCommands", "commands_for", "for_router", "role_changed"]

log = logging.getLogger("svbg.tg.admin.commands")

SYNC_DELAY: Final = 2.0  # after the startup hook: let the bot settle first
CALL_TIMEOUT: Final = 10.0


@dataclass(frozen=True, slots=True)
class StaffCommand:
    command: str
    description: str
    role: str = "admin"
    perm: str | None = None


COMMANDS: Final[tuple[StaffCommand, ...]] = (
    StaffCommand("admin", "Админка", "support"),
    StaffCommand("user", "Найти пользователя", "support", perm="users.view"),
    StaffCommand("broadcast", "Рассылки", perm="broadcast"),
    StaffCommand("plans", "Тарифы", perm="plans"),
    StaffCommand("promos", "Промокоды", perm="promo"),
    StaffCommand("status", "Состояние бота", perm="system.view"),
    StaffCommand("settings", "Все настройки", perm="settings.business"),
)

_RANK: Final = {"user": 0, "support": 1, "admin": 2, "owner": 3}


def commands_for(role: str, perms: Iterable[str] = (), *, scoped: bool = False) -> list[BotCommand]:
    """The commands of one staff member (empty for a plain user); ``scoped`` — a member of a custom role."""
    actor = roles.Actor(None, None, role, frozenset(perms), scoped=scoped and role != "owner")
    return [
        BotCommand(command=c.command, description=c.description)
        for c in COMMANDS
        if _RANK.get(role, 0) >= _RANK[c.role]
        and _RANK.get(role, 0) >= 1
        and (c.perm is None or actor.has_perm(c.perm))
    ]


Call = Callable[[Any, int], Awaitable[Any]]

_BY_ROUTER: weakref.WeakKeyDictionary[Any, StaffCommands] = weakref.WeakKeyDictionary()


def for_router(router: Any) -> StaffCommands | None:
    return _BY_ROUTER.get(router)


def role_changed(
    router: Any, telegram_id: int | None, role: str, perms: Iterable[str] = (), *, scoped: bool = False
) -> None:
    """Refresh one person's menu after a role change (in the background; no-op when menus are not wired)."""
    staff = for_router(router)
    if staff is not None and telegram_id is not None:
        staff.spawn(staff.apply(telegram_id, role, tuple(perms), scoped=scoped))


class StaffCommands:
    """``call(method, chat_id)`` sends a Bot API method (the screen router's transport)."""

    def __init__(
        self,
        call: Call,
        *,
        db: Database | None = None,
        configured_owners: Callable[[], frozenset[int]] = frozenset,
    ) -> None:
        self.call = call
        self.db = db
        self.configured_owners = configured_owners
        self._tasks: set[asyncio.Task[Any]] = set()

    def attach(self, router: Any) -> StaffCommands:
        _BY_ROUTER[router] = self
        return self

    def spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._done)

    def _done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.warning("staff menus not synced: %s", type(task.exception()).__name__)

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def apply(
        self, telegram_id: int, role: str, perms: Iterable[str] = (), *, scoped: bool = False
    ) -> bool:
        """Set (or, for a plain user, delete) the menu of one chat. ``False``: Telegram refused."""
        commands = commands_for(role, perms, scoped=scoped)
        scope = BotCommandScopeChat(chat_id=telegram_id)
        method: Any = (
            SetMyCommands(commands=commands, scope=scope) if commands else DeleteMyCommands(scope=scope)
        )
        try:
            async with asyncio.timeout(CALL_TIMEOUT):
                await self.call(method, telegram_id)
        except (TelegramAPIError, OSError, TimeoutError) as e:
            log.debug("staff menu for %s not set: %s", telegram_id, getattr(e, "message", type(e).__name__))
            return False
        except Exception as e:  # noqa: BLE001 - a menu is a convenience: never stop the others (no bot yet …)
            log.warning("staff menu for %s not set: %s", telegram_id, type(e).__name__)
            return False
        return True

    async def sync_all(self) -> int:
        """Menus of every staff member (stored roles and owners from the settings); how many were set."""
        members: dict[int, tuple[str, tuple[str, ...], bool]] = {}
        if self.db is not None:
            try:
                async with self.db.read() as conn:
                    staff = await roles.staff_list(conn, limit=500)
            except (SQLAlchemyError, OSError) as e:
                log.warning("staff menus skipped: %s", type(e).__name__)
                staff = []
            for m in staff:
                if m.telegram_id is not None and m.banned_at is None:
                    members[m.telegram_id] = (m.role, m.perms, m.scoped)
        for tg in self.configured_owners():
            members[int(tg)] = ("owner", (), False)
        done = 0
        for tg, (role, perms, scoped) in members.items():
            done += await self.apply(tg, role, perms, scoped=scoped)
        return done

    async def sync_later(self, delay: float = SYNC_DELAY) -> None:
        await asyncio.sleep(delay)
        count = await self.sync_all()
        log.info("staff command menus set for %d people", count)
