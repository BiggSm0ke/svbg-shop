"""Support (07 §2.4.6): ``SUPPORT_MODE = link | tickets | both``.

* ``link`` (default) — «💬 Поддержка» is a plain ``SUPPORT_URL`` link; nothing of this module runs.
* ``tickets`` / ``both`` — tickets over forum topics of the admin group (or ``SUPPORT_CHAT_ID``):
  :mod:`.service` (topics, copies both ways, close / reopen), :mod:`.ui` (screen, handlers, card buttons),
  :mod:`.tables` (``tickets``, ``ticket_messages``).

:func:`setup` is the module entry point for ``svbg.app`` (``setup(router, deps)``); the settings are hot.
Imports are lazy: :mod:`svbg.db.schema` loads :mod:`.tables` without the bot side.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from aiogram import Router

    from svbg.support.ui import SupportUi

__all__ = ["build", "setup"]


def build(router: Any, deps: Any) -> SupportUi:
    """The service and the screens over ``deps`` (``db``, ``notifier``, ``settings``, ``users``,
    ``admin_chat``, ``hub``)."""
    from svbg.support.service import TicketService
    from svbg.support.ui import SupportUi
    from svbg.tg.admin.users import settings_reader

    directory = getattr(deps, "users", None)

    async def owner_ids() -> frozenset[int]:
        """``OWNER_IDS``: stored owners are read fresh by every role check."""
        return frozenset(directory.configured_owner_ids()) if directory is not None else frozenset()

    service = TicketService(
        deps.db,
        deps.notifier,
        config=settings_reader(getattr(deps, "settings", None)),
        owner_ids=owner_ids,
        admin_chat=getattr(deps, "admin_chat", None),
    )
    ui = SupportUi(service, router, hub=getattr(deps, "hub", None))
    ui.install()
    return ui


def setup(router: Any, deps: Any) -> Router:
    return build(router, deps).aiogram_router()
