"""Pure rendering helpers: content → aiogram objects, and the choice of the Bot API operation.

* :func:`build_keyboard` — visibility (compiled conditions), row width ≤ 8, ``style``,
  ``icon_custom_emoji_id``, safe ``{placeholder}`` substitution. Static buttons are built once per snapshot
  and reused (memo on the template).
* :func:`plan_transition` — text → text: ``editMessageText``; same media → ``editMessageCaption``; other
  media → ``editMessageMedia``; text ↔ media: send a new message and delete the old one.
* Texts always go out with ``entities`` and ``parse_mode=None`` (the bot's default parse mode never applies
  to content), lengths are measured in UTF-16 code units (text 4096, caption 1024).

Nothing here performs I/O.
"""

from __future__ import annotations

import enum
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal
from urllib.parse import quote

from aiogram.methods import (
    EditMessageCaption,
    EditMessageMedia,
    EditMessageText,
    SendAnimation,
    SendDocument,
    SendMessage,
    SendPhoto,
    SendVideo,
    TelegramMethod,
)
from aiogram.types import (
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaAnimation,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    LinkPreviewOptions,
    Message,
    MessageEntity,
    WebAppInfo,
)

from svbg.content.model import (
    Button,
    CopyAction,
    DeeplinkAction,
    ModuleAction,
    ScreenAction,
    ShareAction,
    SystemAction,
    UrlAction,
    WebAppAction,
)
from svbg.content.store import PLACEHOLDER_RE, ButtonTemplate, KeyboardTemplate, ScreenEntry, compile_keyboard
from svbg.tg.ui import codec
from svbg.tg.ui.view import MediaRef, View

if TYPE_CHECKING:
    from svbg.tg.ui.context import UserCtx

__all__ = [
    "CAPTION_LIMIT",
    "MAX_BUTTONS",
    "MAX_ROW_WIDTH",
    "MODULE_SCREEN",
    "SYSTEM_SCREEN",
    "TEXT_LIMIT",
    "MessageShape",
    "Op",
    "TextTooLongError",
    "build_edit",
    "build_keyboard",
    "build_send",
    "content_view",
    "fit_text",
    "format_label",
    "format_text",
    "nav_button",
    "plan_transition",
    "to_entities",
    "utf16_len",
]

log = logging.getLogger("svbg.tg.ui.renderer")

TEXT_LIMIT: Final = 4096
CAPTION_LIMIT: Final = 1024
MAX_ROW_WIDTH: Final = 8
MAX_BUTTONS: Final = 100
ELLIPSIS: Final = "…"
SYSTEM_SCREEN: Final = "sys"  # callback screen for ``system:<name>`` actions
MODULE_SCREEN: Final = "mod"  # callback screen for ``module:<ext>.<action>``

_NO_PREVIEW: Final = LinkPreviewOptions(is_disabled=True)


class TextTooLongError(ValueError):
    def __init__(self, length: int, limit: int) -> None:
        self.length = length
        self.limit = limit
        super().__init__(f"text is {length} UTF-16 units long, the limit is {limit}")


# ---------------------------------------------------------------- text helpers


def utf16_len(text: str) -> int:
    """Length in UTF-16 code units, the unit Telegram uses for limits and entity offsets."""
    return len(text) + sum(1 for ch in text if ord(ch) > 0xFFFF)


def check_length(text: str, *, caption: bool) -> None:
    limit = CAPTION_LIMIT if caption else TEXT_LIMIT
    n = utf16_len(text)
    if n > limit:
        raise TextTooLongError(n, limit)


def to_entities(raw: Iterable[Mapping[str, Any] | MessageEntity] | None) -> list[MessageEntity] | None:
    if not raw:
        return None
    out = [e if isinstance(e, MessageEntity) else MessageEntity.model_validate(dict(e)) for e in raw]
    return out or None


def format_label(label: str, values: Mapping[str, str]) -> str:
    """Replace known ``{name}`` placeholders; unknown ones stay as typed. Never uses ``str.format``."""
    return PLACEHOLDER_RE.sub(lambda m: values.get(m.group(1), m.group(0)), label)


def format_text(
    text: str, entities: Sequence[Mapping[str, Any]], values: Mapping[str, str]
) -> tuple[str, list[dict[str, Any]]]:
    """Substitute placeholders and move entity offsets accordingly (UTF-16 units).

    An entity that covers a placeholder grows or shrinks with it; an entity boundary inside a placeholder
    snaps to the edge of the substituted value; entities that collapse to zero length are dropped.
    """
    plain = [dict(e) for e in entities]
    if not values or "{" not in text:
        return text, plain
    pieces: list[str] = []
    repl: list[tuple[int, int, int]] = []  # (start16, end16, new_len16) in the original text
    pos = 0
    pos16 = 0
    for m in PLACEHOLDER_RE.finditer(text):
        value = values.get(m.group(1))
        if value is None:
            continue
        before = text[pos : m.start()]
        start16 = pos16 + utf16_len(before)
        end16 = start16 + utf16_len(m.group(0))
        pieces.append(before)
        pieces.append(value)
        repl.append((start16, end16, utf16_len(value)))
        pos, pos16 = m.end(), end16
    if not repl:
        return text, plain
    pieces.append(text[pos:])

    def move(x: int, *, is_end: bool) -> int:
        delta = 0
        for start, end, new_len in repl:
            if end <= x:
                delta += new_len - (end - start)
            elif start < x:  # boundary inside a placeholder
                return start + delta + (new_len if is_end else 0)
            else:
                break
        return x + delta

    out: list[dict[str, Any]] = []
    for e in plain:
        begin = move(int(e["offset"]), is_end=False)
        finish = move(int(e["offset"]) + int(e["length"]), is_end=True)
        if finish > begin:
            e["offset"], e["length"] = begin, finish - begin
            out.append(e)
    return "".join(pieces), out


def fit_text(
    text: str, entities: Sequence[MessageEntity] | None, limit: int
) -> tuple[str, list[MessageEntity] | None]:
    """Cut ``text`` to ``limit`` UTF-16 units (adding "…") and clip entities to the remaining text."""
    if utf16_len(text) <= limit:
        return text, list(entities) if entities else None
    budget = limit - 1  # room for the ellipsis
    used = 0
    cut = 0
    for i, ch in enumerate(text):
        w = 2 if ord(ch) > 0xFFFF else 1
        if used + w > budget:
            cut = i
            break
        used += w
    else:  # pragma: no cover - unreachable: the text is longer than the limit
        cut = len(text)
    clipped: list[MessageEntity] = []
    for e in entities or ():
        if e.offset >= used:
            continue
        length = min(e.length, used - e.offset)
        clipped.append(e if length == e.length else e.model_copy(update={"length": length}))
    return text[:cut] + ELLIPSIS, clipped or None


# ---------------------------------------------------------------- keyboards


def nav_button(
    text: str,
    screen: str,
    action: str = codec.ACTION_OPEN,
    arg: str | None = None,
    *,
    style: str | None = None,
    icon_custom_emoji_id: str | None = None,
) -> InlineKeyboardButton:
    """A callback button for code-defined keyboards (arg must fit into 64 bytes)."""
    return InlineKeyboardButton(
        text=text,
        callback_data=codec.encode(screen, action, arg),
        style=style,
        icon_custom_emoji_id=icon_custom_emoji_id,
    )


def _action_fields(button: Button, bot_username: str | None) -> dict[str, Any] | None:
    action = button.action
    try:
        match action:
            case ScreenAction(target=target):
                return {"callback_data": codec.encode(target, codec.ACTION_OPEN)}
            case SystemAction(name=name):
                return {"callback_data": codec.encode(SYSTEM_SCREEN, name)}
            case ModuleAction(ext=ext, action=act):
                return {"callback_data": codec.encode(MODULE_SCREEN, f"{ext}.{act}")}
            case UrlAction(url=url):
                return {"url": url}
            case WebAppAction(url=url):
                return {"web_app": WebAppInfo(url=url)}
            case DeeplinkAction(code=code):
                if not bot_username:
                    return None
                return {"url": f"https://t.me/{bot_username}?start={code}"}
            case CopyAction(text=text):
                return {"copy_text": CopyTextButton(text=text)}
            case ShareAction(text=text):
                return {"url": "https://t.me/share/url?url=" + quote(text, safe="")}
    except ValueError as e:  # a name that cannot be encoded (e.g. module action too long)
        log.warning("button %s skipped: %s", button.id, e)
        return None
    return None  # pragma: no cover - exhaustive match


def _make_button(t: ButtonTemplate, label: str, bot_username: str | None) -> InlineKeyboardButton | None:
    fields = _action_fields(t.button, bot_username)
    if fields is None:
        return None
    return InlineKeyboardButton(
        text=label,
        style=t.button.style,
        icon_custom_emoji_id=t.button.icon_custom_emoji_id,
        **fields,
    )


def _visible(t: ButtonTemplate, ctx: UserCtx) -> bool:
    if t.condition is None:
        return True
    try:
        return bool(t.condition(ctx))
    except (TypeError, AttributeError, ValueError):  # fail closed: a broken condition hides the button
        log.warning("visibility condition of button %s failed; hidden", t.button.id)
        return False


def build_keyboard(
    buttons: KeyboardTemplate | Sequence[Button],
    ctx: UserCtx,
    lang: str | None = None,
    extra_rows: Iterable[Sequence[InlineKeyboardButton]] | None = None,
    *,
    default_lang: str = "ru",
    bot_username: str | None = None,
) -> InlineKeyboardMarkup:
    """Inline keyboard for ``ctx``: visible buttons only, rows split to ≤ 8, at most 100 buttons."""
    template = (
        buttons
        if isinstance(buttons, KeyboardTemplate)
        else compile_keyboard(list(buttons), lang or ctx.lang, default_lang)
    )
    values: Mapping[str, str] | None = None
    rows: list[list[InlineKeyboardButton]] = []
    memo_key = f"btn:{bot_username or ''}"
    for row in template.rows:
        built: list[InlineKeyboardButton] = []
        for t in row:
            if not _visible(t, ctx):
                continue
            if t.needs_format:
                if values is None:
                    values = ctx.placeholders()
                label = format_label(t.label, values)
                btn = _make_button(t, label, bot_username) if label.strip() else None
            elif memo_key in t.memo:
                btn = t.memo[memo_key]
            else:
                btn = _make_button(t, t.label, bot_username)
                t.memo[memo_key] = btn
            if btn is not None:
                built.append(btn)
        rows.extend(built[i : i + MAX_ROW_WIDTH] for i in range(0, len(built), MAX_ROW_WIDTH))
    for extra in extra_rows or ():
        items = list(extra)
        rows.extend(items[i : i + MAX_ROW_WIDTH] for i in range(0, len(items), MAX_ROW_WIDTH))
    total = 0
    capped: list[list[InlineKeyboardButton]] = []
    for row in rows:
        if total + len(row) > MAX_BUTTONS:
            log.warning("keyboard truncated to %d buttons", MAX_BUTTONS)
            row = row[: MAX_BUTTONS - total]  # noqa: PLW2901
            if row:
                capped.append(row)
            break
        total += len(row)
        capped.append(row)
    return InlineKeyboardMarkup(inline_keyboard=capped)


def as_markup(
    keyboard: Sequence[Sequence[InlineKeyboardButton]] | InlineKeyboardMarkup | None,
) -> InlineKeyboardMarkup | None:
    """Normalize a view keyboard; an empty keyboard becomes ``None`` (no markup)."""
    if keyboard is None:
        return None
    if isinstance(keyboard, InlineKeyboardMarkup):
        return keyboard if keyboard.inline_keyboard else None
    rows: list[list[InlineKeyboardButton]] = []
    for row in keyboard:
        items = list(row)
        rows.extend(items[i : i + MAX_ROW_WIDTH] for i in range(0, len(items), MAX_ROW_WIDTH))
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


# ---------------------------------------------------------------- content → view


def content_view(
    entry: ScreenEntry,
    ctx: UserCtx,
    *,
    media: MediaRef | None = None,
    preview_url: str | None = None,
    extra_rows: Iterable[Sequence[InlineKeyboardButton]] | None = None,
    bot_username: str | None = None,
) -> View:
    """Render a content screen for ``ctx``: text with placeholders, keyboard, media or link preview.

    In ``preview`` media mode the image is shown as a link preview above the text (``preview_url``), so the
    message stays a text message; without a public URL the screen falls back to an attachment.
    """
    block = entry.text(ctx.lang)
    text, entities = format_text(block.text, block.entities, ctx.placeholders())
    if not text.strip():
        text, entities = ELLIPSIS, []
    keyboard = build_keyboard(
        entry.keyboard(ctx.lang),
        ctx,
        ctx.lang,
        extra_rows,
        default_lang=entry.default_lang,
        bot_username=bot_username,
    )
    view = View(text=text, entities=to_entities(entities), keyboard=keyboard)
    if media is not None:
        if entry.screen.media_mode == "preview" and preview_url:
            view.preview = LinkPreviewOptions(url=preview_url, show_above_text=True, prefer_large_media=True)
        else:
            view.media = media
    return view


# ---------------------------------------------------------------- transitions


class Op(enum.Enum):
    SEND_NEW = "send_new"  # there is no previous message
    EDIT_TEXT = "edit_text"
    EDIT_CAPTION = "edit_caption"
    EDIT_MEDIA = "edit_media"
    SEND_NEW_DELETE_OLD = "send_new_delete_old"


ShapeKind = Literal["text", "photo", "animation", "video", "document"]


@dataclass(frozen=True, slots=True)
class MessageShape:
    kind: ShapeKind = "text"
    media_key: str | None = None

    @property
    def is_media(self) -> bool:
        return self.kind != "text"

    @classmethod
    def of_view(cls, view: View) -> MessageShape:
        if view.media is None:
            return cls("text")
        return cls(view.media.kind, view.media.key)

    @classmethod
    def of_message(cls, message: Message) -> MessageShape | None:
        """Shape of an existing message, ``None`` if the bot cannot turn it into a screen."""
        if message.animation is not None:  # GIFs also carry ``document``; check animation first
            return cls("animation")
        if message.photo:
            return cls("photo")
        if message.video is not None:
            return cls("video")
        if message.document is not None:
            return cls("document")
        if message.text is not None:
            return cls("text")
        return None

    def to_json(self) -> dict[str, Any]:
        return {"k": self.kind, "m": self.media_key}

    @classmethod
    def from_json(cls, value: Any) -> MessageShape | None:
        if not isinstance(value, Mapping):
            return None
        kind = value.get("k")
        if kind not in ("text", "photo", "animation", "video", "document"):
            return None
        key = value.get("m")
        return cls(kind, key if isinstance(key, str) else None)


def plan_transition(prev: MessageShape | None, new: MessageShape, *, force_new: bool = False) -> Op:
    """Pick the cheapest Bot API operation that turns ``prev`` into ``new``."""
    if prev is None:
        return Op.SEND_NEW
    if force_new:
        return Op.SEND_NEW_DELETE_OLD
    if not prev.is_media and not new.is_media:
        return Op.EDIT_TEXT
    if prev.is_media and new.is_media:
        if prev.kind == new.kind and prev.media_key is not None and prev.media_key == new.media_key:
            return Op.EDIT_CAPTION
        return Op.EDIT_MEDIA
    return Op.SEND_NEW_DELETE_OLD


def _prepared_text(view: View, *, caption: bool) -> tuple[str, list[MessageEntity] | None]:
    entities = list(view.entities) if view.entities else None
    if view.parse_mode is not None:  # formatted by Telegram: cannot be cut safely, reject instead
        check_length(view.text, caption=caption)
        return view.text, None
    return fit_text(view.text, entities, CAPTION_LIMIT if caption else TEXT_LIMIT)


_INPUT_MEDIA: Final = {
    "photo": InputMediaPhoto,
    "animation": InputMediaAnimation,
    "video": InputMediaVideo,
    "document": InputMediaDocument,
}


def build_send(view: View, chat_id: int, markup: InlineKeyboardMarkup | None) -> TelegramMethod[Message]:
    """``sendMessage`` / ``sendPhoto`` / ``sendAnimation`` / ``sendVideo`` / ``sendDocument`` for a view."""
    media = view.media
    if media is None:
        text, entities = _prepared_text(view, caption=False)
        if not text.strip():
            raise ValueError("a text view needs non-empty text")
        return SendMessage(
            chat_id=chat_id,
            text=text,
            entities=entities,
            parse_mode=view.parse_mode,
            link_preview_options=view.preview or _NO_PREVIEW,
            reply_markup=markup,
        )
    caption, entities = _prepared_text(view, caption=True)
    common: dict[str, Any] = {
        "chat_id": chat_id,
        "caption": caption or None,
        "caption_entities": entities,
        "parse_mode": view.parse_mode,
        "reply_markup": markup,
    }
    if media.kind == "photo":
        return SendPhoto(photo=media.file, **common)
    if media.kind == "animation":
        return SendAnimation(animation=media.file, **common)
    if media.kind == "video":
        return SendVideo(video=media.file, **common)
    return SendDocument(document=media.file, **common)


def build_edit(
    op: Op, view: View, chat_id: int, message_id: int, markup: InlineKeyboardMarkup | None
) -> TelegramMethod[Any]:
    """Edit method for ``op`` (one of EDIT_TEXT, EDIT_CAPTION, EDIT_MEDIA)."""
    if op is Op.EDIT_TEXT:
        text, entities = _prepared_text(view, caption=False)
        if not text.strip():
            raise ValueError("a text view needs non-empty text")
        return EditMessageText(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            entities=entities,
            parse_mode=view.parse_mode,
            link_preview_options=view.preview or _NO_PREVIEW,
            reply_markup=markup,
        )
    if view.media is None:
        raise ValueError(f"{op.name} needs a view with media")
    caption, entities = _prepared_text(view, caption=True)
    if op is Op.EDIT_CAPTION:
        return EditMessageCaption(
            chat_id=chat_id,
            message_id=message_id,
            caption=caption or None,
            caption_entities=entities,
            parse_mode=view.parse_mode,
            reply_markup=markup,
        )
    if op is Op.EDIT_MEDIA:
        media_cls = _INPUT_MEDIA[view.media.kind]
        return EditMessageMedia(
            chat_id=chat_id,
            message_id=message_id,
            media=media_cls(
                media=view.media.file,
                caption=caption or None,
                caption_entities=entities,
                parse_mode=view.parse_mode,
            ),
            reply_markup=markup,
        )
    raise ValueError(f"{op.name} is not an edit operation")
