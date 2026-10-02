"""Rendering of user screens: content (text, media, system buttons) + live values + code-built button rows.

Two entry points:

* :func:`screen_view` — inside a click (``ScreenCtx``): the router's own content rendering (media, link
  preview, visibility of system buttons for the enriched :class:`UserCtx`), then the screen's live
  ``{placeholders}`` and the dynamic rows (plans, periods, payment methods…);
* :func:`plain_view` — outside a click (billing's messages, notifications, background jobs): the same text and
  buttons; the media is not attached here, the view only says which picture the screen has (``picture``: the
  owner's own one, ``banner=False``: removed) and the banner middleware shows it.

When the content store has no enabled screen with the code, the default from :mod:`svbg.tg.user.seeds` is
used, so a screen is never empty. Placeholders are substituted safely (``{name}`` only, no ``str.format``)
and entity offsets move with them (UTF-16).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Final

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from svbg.content.banner import is_banner
from svbg.content.model import Button, ContentError, parse_action, parse_label
from svbg.content.store import KeyboardTemplate, compile_keyboard
from svbg.tg.ui.conditions import ConditionError
from svbg.tg.ui.renderer import ELLIPSIS, build_keyboard, format_text, to_entities
from svbg.tg.ui.view import View
from svbg.tg.user.seeds import SEEDS, seed_text

if TYPE_CHECKING:
    from svbg.content.store import ContentStore, ScreenEntry
    from svbg.tg.ui.context import UserCtx
    from svbg.tg.ui.router import ScreenCtx

__all__ = ["Rows", "plain_view", "screen_view", "seed_keyboard"]

Rows = Sequence[Sequence[InlineKeyboardButton]]

_SEED_KEYBOARDS: dict[tuple[str, str], KeyboardTemplate] = {}
_NO_ROWS: Final[tuple[tuple[InlineKeyboardButton, ...], ...]] = ()


def seed_keyboard(code: str, lang: str) -> KeyboardTemplate | None:
    """Compiled keyboard of a seed screen (cached per code and language)."""
    key = (code, lang)
    cached = _SEED_KEYBOARDS.get(key)
    if cached is not None:
        return cached
    seed = SEEDS.get(code)
    if seed is None:
        return None
    buttons: list[Button] = []
    for b in seed.buttons:
        try:
            buttons.append(
                Button(
                    label=parse_label(dict(b.label)),
                    action=parse_action(dict(b.action)),
                    style=b.style,  # type: ignore[arg-type]
                    row=b.row,
                    sort=b.sort,
                    visible_if=dict(b.visible_if) if b.visible_if is not None else None,
                    system_key=b.system_key,
                )
            )
        except ContentError:  # pragma: no cover - seeds are validated by tests
            continue
    try:
        template = compile_keyboard(buttons, lang)
    except ConditionError:  # pragma: no cover - seeds are validated by tests
        return None
    _SEED_KEYBOARDS[key] = template
    return template


def _rows_of(markup: Any) -> list[list[InlineKeyboardButton]]:
    if markup is None:
        return []
    if isinstance(markup, InlineKeyboardMarkup):
        return [list(r) for r in markup.inline_keyboard]
    return [list(r) for r in markup]


def _merge(top: Rows, middle: Any, bottom: Rows) -> list[list[InlineKeyboardButton]]:
    rows = [list(r) for r in top if r]
    rows += [r for r in _rows_of(middle) if r]
    rows += [list(r) for r in bottom if r]
    return rows


def _apply_values(view: View, values: Mapping[str, str]) -> View:
    """Substitute screen values into an already rendered view (entities move along)."""
    if not values or "{" not in view.text:
        return view
    raw = [e.model_dump(exclude_none=True) for e in view.entities or ()]
    text, entities = format_text(view.text, raw, values)
    view.text = text if text.strip() else ELLIPSIS
    view.entities = to_entities(entities)
    return view


def _entry(content: ContentStore | None, code: str) -> ScreenEntry | None:
    entry = None if content is None else content.get_screen(code)
    return entry if entry is not None and entry.screen.enabled else None


def screen_view(
    ctx: ScreenCtx,
    code: str,
    values: Mapping[str, str] | None = None,
    *,
    top: Rows = _NO_ROWS,
    bottom: Rows = _NO_ROWS,
) -> View:
    """Screen ``code`` for the click in ``ctx`` (``ctx.user`` should already be enriched)."""
    vals = dict(values or {})
    view = ctx.content_view(code)
    if view is None:
        return plain_view(ctx.user, None, code, vals, top=top, bottom=bottom, bot_username=_bot(ctx))
    view = _apply_values(view, vals)
    view.keyboard = _merge(top, view.keyboard, bottom)
    return view


def _bot(ctx: ScreenCtx) -> str | None:
    return ctx.router.transport.bot_username


def plain_view(
    user: UserCtx,
    content: ContentStore | None,
    code: str,
    values: Mapping[str, str] | None = None,
    *,
    top: Rows = _NO_ROWS,
    bottom: Rows = _NO_ROWS,
    bot_username: str | None = None,
    fallback_text: str | None = None,
) -> View:
    """Screen ``code`` without a click: content text and buttons, else the seed, else ``fallback_text``.

    The view carries the screen's picture choice (:class:`View` ``picture`` / ``banner``), not the media."""
    vals = {**user.placeholders(), **dict(values or {})}
    entry = _entry(content, code)
    if entry is not None:
        block = entry.text(user.lang)
        text, entities = format_text(block.text, block.entities, vals)
        keyboard = build_keyboard(
            entry.keyboard(user.lang),
            user,
            user.lang,
            default_lang=entry.default_lang,
            bot_username=bot_username,
        )
    else:
        raw_text, raw_entities = seed_text(code, user.lang)
        if not raw_text and fallback_text is not None:
            raw_text, raw_entities = fallback_text, []
        text, entities = format_text(raw_text, raw_entities, vals)
        template = seed_keyboard(code, user.lang)
        keyboard = (
            build_keyboard(template, user, user.lang, bot_username=bot_username)
            if template is not None
            else None
        )
    if not text.strip():
        text, entities = ELLIPSIS, []
    view = View(text=text, entities=to_entities(entities), keyboard=_merge(top, keyboard, bottom))
    if entry is not None:
        media_id = entry.screen.media_id
        if media_id is None:
            view.banner = False  # the owner removed this screen's picture
        elif content is not None and not is_banner(content.get_media(media_id)):
            view.picture = media_id  # the owner's own picture instead of the default banner
    return view
