"""«✏️ Конструктор» — screens, buttons and media edited right in the bot (07 §2.4.1, §3.7).

* :mod:`.screens` — the editor UI (режим «✏️», screen card, button wizard, own screens, «Как видит…», history
  and «↩️ Отменить»);
* :mod:`.telegram` — the premium-emoji probe sender, icon extraction/validation, file downloads;
* the write side is :mod:`svbg.content.editing`, the probe is :mod:`svbg.content.premium`, the in-place
  mode is :mod:`svbg.tg.ui.edit_mode`.

:func:`setup` is the module entry point for ``svbg.app`` (``setup(router, deps)``).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from aiogram import Router

from svbg.content.editing import ContentEditor
from svbg.content.media import MediaLibrary
from svbg.content.premium import PremiumService, reference_emoji
from svbg.tg.admin.content.screens import SCREEN_HOME, ContentScreens
from svbg.tg.admin.content.telegram import TransportProbeSender, bot_downloader
from svbg.tg.ui.edit_mode import EditMode

__all__ = ["SCREEN_HOME", "ContentScreens", "setup"]

log = logging.getLogger("svbg.tg.admin.content")

PROBE_TASK = "content.premium_probe"
PROBE_INTERVAL_S = 3600.0


async def setup(router: Any, deps: Any) -> Router:
    """Wire the constructor: editor, media library, premium probe (hourly tick, daily re-check), ``/edit``.

    Optional deps: ``media`` (a :class:`MediaLibrary`; else one is built over the router's media directory),
    ``scheduler``, ``holder`` (downloads, token fingerprint), ``users.owner_ids``.
    """
    store = deps.content

    def public_url() -> str | None:
        fn = getattr(router, "_public_url", None)
        return fn() if callable(fn) else None

    # without PUBLIC_URL «превью-ссылка» media goes as an attachment: caption limits apply on save
    editor = ContentEditor(deps.db, store, preview_available=lambda: bool(public_url()))
    library = getattr(deps, "media", None)
    if not isinstance(library, MediaLibrary):
        root = getattr(router, "_media_root", None)
        library = MediaLibrary(deps.db, Path(root)) if root is not None else None
    holder = getattr(deps, "holder", None)
    users = getattr(deps, "users", None)
    owner_ids = getattr(users, "owner_ids", None) if users is not None else None

    def bot_info() -> tuple[int | None, str | None]:
        bot = holder.get() if holder is not None else None
        return (None, None) if bot is None else (bot.id, bot.token)

    sender = TransportProbeSender(router.transport)
    premium = PremiumService(
        deps.db,
        lambda: sender if holder is None or holder.get() is not None else None,
        bot=bot_info,
        owners=owner_ids,
        reference=lambda: reference_emoji(store.snapshot),
    )
    try:
        await premium.load()
    except Exception:  # the editor works without a stored probe result
        log.exception("could not load the premium emoji state")
    screens = ContentScreens(
        router,
        store,
        editor,
        deps.db,
        edit_mode=EditMode(),
        library=library,
        premium=premium,
        owner_ids=owner_ids,
        download=bot_downloader(holder) if holder is not None else None,
        public_url=public_url,
    )
    screens.install()
    scheduler = getattr(deps, "scheduler", None)
    if scheduler is not None:
        try:
            scheduler.every(PROBE_TASK, PROBE_INTERVAL_S, premium.tick, jitter_s=120, run_at_start=True)
        except ValueError:  # already registered (a second setup in tests)
            log.debug("premium probe task is already scheduled")
    return screens.aiogram_router()
