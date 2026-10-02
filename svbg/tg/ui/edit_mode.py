"""Режим «✏️» (07 §2.4.1): content screens become editable in place for users with ``content.edit``.

* :class:`EditMode` keeps who has the mode on (in memory: ``/edit`` again or a restart turns it off) and
  re-checks the right on **every** render — a revoked right hides the editor UI at once.
* :meth:`EditMode.install` hooks the screen router's content rendering. For an editor with the mode on, the
  keyboard of a content screen is replaced by its **edit keyboard**: every button of the screen (also disabled
  ones — 🚫, and ones with a visibility condition — 🔒) opens its editor instead of acting; the screen's
  code-made rows stay as they are; the last row is the service row «✏️ Экран · ➕ Кнопка · 👁 Как видит…».
  Nobody else ever gets these buttons: for other users the hook is one set lookup.
* :meth:`EditMode.render_as` renders a screen exactly as another (synthetic) user would see it — the
  «Как видит…» preview — bypassing the hook.

Callback names live here so the renderer side and the admin side (``svbg.tg.admin.content``) agree; they all
contain a dot, so they never collide with content screen codes (``[a-z][a-z0-9_]*``).
"""

from __future__ import annotations

import itertools
import logging
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, Final

from aiogram.types import InlineKeyboardButton

from svbg.tg.ui import codec
from svbg.tg.ui.renderer import MAX_BUTTONS, MAX_ROW_WIDTH

if TYPE_CHECKING:
    from svbg.content.model import Button
    from svbg.content.store import ScreenEntry
    from svbg.tg.ui.context import UserCtx
    from svbg.tg.ui.router import ScreenCtx, ScreenRouter
    from svbg.tg.ui.view import View

__all__ = [
    "ACTIONS",
    "PERM",
    "SCREEN_BUTTON",
    "SCREEN_EDITOR",
    "SCREEN_PREVIEW",
    "EditMode",
    "button_rows",
    "can_edit",
    "edit_keyboard",
    "pages_of",
]

log = logging.getLogger("svbg.tg.ui.edit_mode")

PERM: Final = "content.edit"
SCREEN_EDITOR: Final = "ce.scr"  # arg: screen id
SCREEN_BUTTON: Final = "ce.btn"  # arg: button id
SCREEN_PREVIEW: Final = "ce.view"  # arg: "<screen id>" or "<screen id>.<state>"
ACTIONS: Final = "ce.a"  # actions of the constructor (``nb`` = new button, arg: screen id)

MARK_DISABLED: Final = "🚫 "
MARK_CONDITION: Final = "🔒 "

ContentViewFn = Callable[..., "View"]


def can_edit(user: UserCtx) -> bool:
    """Owner, or an admin with ``content.edit`` (``*`` included)."""
    return user.at_least("admin") and user.has_perm(PERM)


def _label(button: Button, lang: str, default_lang: str) -> str:
    text = button.label.get(lang) or button.label.get(default_lang)
    if not text:
        text = next((t for t in button.label.values() if t.strip()), "") or "…"
    return text


def _cb(screen: str, arg: str | None = None) -> str:
    return codec.encode(screen, codec.ACTION_OPEN, arg)


def service_row(entry: ScreenEntry, lang: str = "ru") -> list[InlineKeyboardButton]:
    sid = str(entry.id)
    en = lang == "en"
    return [
        InlineKeyboardButton(text="✏️ Screen" if en else "✏️ Экран", callback_data=_cb(SCREEN_EDITOR, sid)),
        InlineKeyboardButton(
            text="➕ Button" if en else "➕ Кнопка", callback_data=codec.encode(ACTIONS, "nb", sid)
        ),
        InlineKeyboardButton(
            text="👁 Seen as…" if en else "👁 Как видит…", callback_data=_cb(SCREEN_PREVIEW, sid)
        ),
    ]


def button_rows(entry: ScreenEntry, user: UserCtx) -> list[list[InlineKeyboardButton]]:
    """Every button of the screen as an editor button (also disabled 🚫 and conditional 🔒 ones), by rows."""
    ordered = sorted(entry.screen.buttons, key=lambda b: (b.row, b.sort, b.id if b.id is not None else 0))
    rows: list[list[InlineKeyboardButton]] = []
    for _row, group in itertools.groupby(ordered, key=lambda b: b.row):
        built: list[InlineKeyboardButton] = []
        for b in group:
            if b.id is None:
                continue
            mark = MARK_DISABLED if not b.enabled else (MARK_CONDITION if b.visible_if else "")
            built.append(
                InlineKeyboardButton(
                    text=mark + _label(b, user.lang, entry.default_lang),
                    callback_data=_cb(SCREEN_BUTTON, str(b.id)),
                    style=b.style,
                    icon_custom_emoji_id=b.icon_custom_emoji_id,
                )
            )
        rows.extend(built[i : i + MAX_ROW_WIDTH] for i in range(0, len(built), MAX_ROW_WIDTH))
    return rows


def _fit(
    rows: Sequence[Sequence[InlineKeyboardButton]], budget: int
) -> tuple[list[list[InlineKeyboardButton]], int]:
    """At most ``budget`` buttons of ``rows`` (the last row cut if needed) and how many were left out."""
    out: list[list[InlineKeyboardButton]] = []
    left = max(budget, 0)
    dropped = 0
    for row in rows:
        items = list(row)
        take = items[:left]
        dropped += len(items) - len(take)
        left -= len(take)
        if take:
            out.append(take)
    return out, dropped


def pages_of(
    rows: Sequence[Sequence[InlineKeyboardButton]], per_page: int
) -> list[list[list[InlineKeyboardButton]]]:
    """Split rows into pages of at most ``per_page`` buttons (rows are kept whole; ≥ 1 page)."""
    pages: list[list[list[InlineKeyboardButton]]] = [[]]
    count = 0
    for row in rows:
        items = list(row)
        if pages[-1] and count + len(items) > per_page:
            pages.append([])
            count = 0
        pages[-1].append(items)
        count += len(items)
    return pages


def edit_keyboard(
    entry: ScreenEntry,
    user: UserCtx,
    extra_rows: Sequence[Sequence[InlineKeyboardButton]] | None = None,
) -> list[list[InlineKeyboardButton]]:
    """Every button of the screen (opens its editor), the code-made rows, then the service row.

    Never more than Telegram's 100 buttons: the service row always stays, the screen's own buttons come
    next (the rest is reachable in «🔘 Кнопки», page by page), the code-made rows get what is left.
    """
    service = service_row(entry, user.lang)
    extras: list[list[InlineKeyboardButton]] = []
    for extra in extra_rows or ():
        items = list(extra)
        extras.extend(items[i : i + MAX_ROW_WIDTH] for i in range(0, len(items), MAX_ROW_WIDTH))
    budget = MAX_BUTTONS - len(service)
    rows, hidden = _fit(button_rows(entry, user), budget)
    budget -= sum(len(r) for r in rows)
    extras, hidden_extra = _fit(extras, budget)
    if hidden or hidden_extra:
        log.warning(
            "edit keyboard of screen %s cut to %d buttons (%d hidden)",
            entry.id,
            MAX_BUTTONS,
            hidden + hidden_extra,
        )
    return [*rows, *extras, service]


class EditMode:
    """Who edits in place, and the router hook that shows them the edit keyboard."""

    def __init__(self) -> None:
        self._on: set[int] = set()
        self._original: ContentViewFn | None = None
        self._router: ScreenRouter | None = None

    # ------------------------------------------------------------ state

    def is_on(self, user: UserCtx) -> bool:
        return user.user_id in self._on

    def active(self, user: UserCtx) -> bool:
        """The mode is on and the right is still there (checked on every render)."""
        if user.user_id not in self._on:
            return False
        if not can_edit(user):
            self._on.discard(user.user_id)
            return False
        return True

    def enable(self, user: UserCtx) -> bool:
        if not can_edit(user):
            return False
        self._on.add(user.user_id)
        return True

    def disable(self, user: UserCtx) -> None:
        self._on.discard(user.user_id)

    def toggle(self, user: UserCtx) -> bool | None:
        """New state, or ``None`` when the user may not edit."""
        if user.user_id in self._on:
            self._on.discard(user.user_id)
            return False
        return True if self.enable(user) else None

    # ------------------------------------------------------------ router hook

    def install(self, router: ScreenRouter) -> None:
        """Wrap the router's content rendering (idempotent)."""
        if self._router is router:
            return
        if self._router is not None:
            raise RuntimeError("edit mode is already installed on another router")
        original: ContentViewFn = router._content_view
        self._original = original
        self._router = router

        def content_view(
            ctx: ScreenCtx,
            entry: ScreenEntry,
            extra_rows: Sequence[Sequence[Any]] | None,
            *,
            with_media: bool = True,
        ) -> View:
            view = original(ctx, entry, extra_rows, with_media=with_media)
            if with_media and ctx.user.user_id in self._on and self.active(ctx.user):
                view.keyboard = edit_keyboard(entry, ctx.user, extra_rows)
            return view

        router._content_view = content_view  # type: ignore[method-assign]

    def render_as(self, ctx: ScreenCtx, entry: ScreenEntry, user: UserCtx) -> View:
        """The screen as ``user`` sees it (no edit keyboard), media included."""
        if self._router is None or self._original is None:
            raise RuntimeError("edit mode is not installed")
        from svbg.tg.ui.router import ScreenCtx as _Ctx

        as_user = _Ctx(self._router, user, ctx.chat_id)
        return self._original(as_user, entry, None, with_media=True)
