"""What a broadcast sends: the normalized message copy, its keyboard and the Bot API calls (no I/O here).

* :func:`normalize` turns the admin's message (sent or forwarded: text, photo, GIF, video, file, audio, voice)
  into plain JSON — type, text/caption, **all** entities (``custom_emoji``, spoilers, expandable quotes…),
  ``file_id``, «caption above media», media spoiler and link preview options — so the broadcast survives
  the deletion of the original;
* :func:`parse_buttons` reads the admin's button list (one line = one row, ``Текст | действие [| цвет]``;
  a Premium emoji at the start of a label becomes the button icon) into the constructor button model
  (:class:`svbg.content.model.Button` JSON), validated by the same rules as screen buttons;
* :func:`build_markup` renders those buttons for a language; :func:`copy_method` / :func:`send_method` build
  ``copyMessage`` (preferred: Telegram copies the original with its formatting) and the ``send*``
  fallback from the normalized copy (``entities=`` without ``parse_mode``).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from aiogram.methods import (
    CopyMessage,
    SendAnimation,
    SendAudio,
    SendDocument,
    SendMessage,
    SendPhoto,
    SendVideo,
    SendVoice,
    TelegramMethod,
)
from aiogram.types import InlineKeyboardMarkup, LinkPreviewOptions, MessageEntity

from svbg.content.model import BUTTON_STYLES, Button, ContentError
from svbg.tg.ui.context import UserCtx
from svbg.tg.ui.renderer import CAPTION_LIMIT, MAX_ROW_WIDTH, TEXT_LIMIT, build_keyboard, utf16_len

if TYPE_CHECKING:
    from aiogram.types import Message

__all__ = [
    "MAX_BUTTONS",
    "MEDIA_TYPES",
    "TYPES",
    "ComposeError",
    "build_markup",
    "copy_method",
    "describe",
    "has_custom_emoji",
    "normalize",
    "parse_buttons",
    "send_method",
]

MEDIA_TYPES: Final = ("photo", "animation", "video", "document", "audio", "voice")
TYPES: Final = ("text", *MEDIA_TYPES)
MAX_BUTTONS: Final = 24
MAX_ROWS: Final = 12
_STYLE_ALIASES: Final[Mapping[str, str]] = {
    "синяя": "primary",
    "синий": "primary",
    "blue": "primary",
    "зелёная": "success",
    "зеленая": "success",
    "зелёный": "success",
    "зеленый": "success",
    "green": "success",
    "красная": "danger",
    "красный": "danger",
    "red": "danger",
    **{s: s for s in BUTTON_STYLES},
}
_URL_RE: Final = re.compile(r"^(?:https?://|tg://)\S+$", re.IGNORECASE)
_BARE_TG_RE: Final = re.compile(r"^t\.me/\S+$", re.IGNORECASE)
_TYPE_LABELS: Final[Mapping[str, str]] = {
    "text": "📝 текст",
    "photo": "🖼 фото",
    "animation": "🎞 GIF",
    "video": "🎬 видео",
    "document": "📎 файл",
    "audio": "🎵 аудио",
    "voice": "🎙 голосовое",
}


class ComposeError(ValueError):
    """The admin's input cannot be used; ``str(error)`` is a short Russian explanation."""


# ------------------------------------------------------------------------------------------- the message


def _entities(raw: Sequence[MessageEntity] | None) -> list[dict[str, Any]]:
    return [e.model_dump(mode="json", exclude_none=True) for e in raw or ()]


def normalize(message: Message) -> dict[str, Any]:
    """The broadcast copy of ``message``. Raises :class:`ComposeError` for unsupported messages."""
    kind: str | None = None
    file_id: str | None = None
    if message.text is not None:
        kind = "text"
    elif message.photo:
        kind, file_id = "photo", message.photo[-1].file_id
    elif message.animation is not None:  # before document: a GIF also carries ``document``
        kind, file_id = "animation", message.animation.file_id
    else:
        for name in ("video", "document", "audio", "voice"):
            obj = getattr(message, name, None)
            if obj is not None:
                kind, file_id = name, obj.file_id
                break
    if kind is None:
        raise ComposeError(
            "Такое сообщение нельзя разослать. Пришлите текст, фото, GIF, видео, файл, аудио или голосовое."
        )
    if kind == "text":
        text, entities = message.text or "", _entities(message.entities)
    else:
        text, entities = message.caption or "", _entities(message.caption_entities)
    limit = TEXT_LIMIT if kind == "text" else CAPTION_LIMIT
    if utf16_len(text) > limit:
        raise ComposeError(f"Слишком длинно: максимум {limit} символов.")
    content: dict[str, Any] = {"type": kind, "text": text, "entities": entities}
    if file_id is not None:
        content["file_id"] = file_id
    if message.show_caption_above_media:
        content["above"] = True
    if message.has_media_spoiler:
        content["spoiler"] = True
    if kind == "text" and message.link_preview_options is not None:
        raw = message.link_preview_options.model_dump(exclude_none=True)
        # aiogram's ``Default(...)`` sentinels are not values: keep plain JSON scalars only
        content["preview"] = {k: v for k, v in raw.items() if isinstance(v, bool | int | str)}
    return content


def has_custom_emoji(content: Mapping[str, Any]) -> bool:
    return any(
        e.get("type") == "custom_emoji" for e in content.get("entities") or () if isinstance(e, Mapping)
    )


def describe(content: Mapping[str, Any]) -> str:
    """«🖼 фото · 120 символов» for cards."""
    kind = str(content.get("type"))
    label = _TYPE_LABELS.get(kind, kind)
    text = str(content.get("text") or "")
    if kind != "text" and not text:
        return f"{label} без подписи"
    return f"{label} · {len(text)} симв."


def _entity_objects(raw: Any) -> list[MessageEntity] | None:
    if not isinstance(raw, list) or not raw:
        return None
    return [MessageEntity.model_validate(e) for e in raw if isinstance(e, Mapping)]


def send_method(
    content: Mapping[str, Any],
    chat_id: int,
    markup: InlineKeyboardMarkup | None,
    *,
    silent: bool = False,
) -> TelegramMethod[Any]:
    """``send*`` from the normalized copy: entities, never ``parse_mode``."""
    kind = content.get("type")
    text = str(content.get("text") or "")
    entities = _entity_objects(content.get("entities"))
    common: dict[str, Any] = {
        "chat_id": chat_id,
        "reply_markup": markup,
        "disable_notification": silent or None,
        "parse_mode": None,
    }
    if kind == "text":
        raw_preview = content.get("preview")
        preview = LinkPreviewOptions.model_validate(raw_preview) if isinstance(raw_preview, Mapping) else None
        return SendMessage(text=text, entities=entities, link_preview_options=preview, **common)
    file_id = content.get("file_id")
    if not isinstance(file_id, str) or kind not in MEDIA_TYPES:
        raise ComposeError("В рассылке нет файла: пришлите сообщение заново.")
    caption: dict[str, Any] = {"caption": text or None, "caption_entities": entities}
    above = True if content.get("above") else None
    spoiler = True if content.get("spoiler") else None
    if kind == "photo":
        return SendPhoto(
            photo=file_id, show_caption_above_media=above, has_spoiler=spoiler, **caption, **common
        )
    if kind == "animation":
        return SendAnimation(
            animation=file_id, show_caption_above_media=above, has_spoiler=spoiler, **caption, **common
        )
    if kind == "video":
        return SendVideo(
            video=file_id, show_caption_above_media=above, has_spoiler=spoiler, **caption, **common
        )
    if kind == "document":
        return SendDocument(document=file_id, **caption, **common)
    if kind == "audio":
        return SendAudio(audio=file_id, **caption, **common)
    return SendVoice(voice=file_id, **caption, **common)


def copy_method(
    from_chat_id: int,
    message_id: int,
    chat_id: int,
    markup: InlineKeyboardMarkup | None,
    *,
    silent: bool = False,
) -> CopyMessage:
    """``copyMessage`` of the original: Telegram keeps every entity and the media as they are."""
    return CopyMessage(
        chat_id=chat_id,
        from_chat_id=from_chat_id,
        message_id=message_id,
        reply_markup=markup,
        disable_notification=silent or None,
    )


# ------------------------------------------------------------------------------------------- buttons


@dataclass(frozen=True, slots=True)
class _Piece:
    start: int  # index in the whole text
    text: str


def _utf16_starts(text: str) -> dict[int, int]:
    """UTF-16 offset → Python index for every character start."""
    out: dict[int, int] = {}
    pos = 0
    for i, ch in enumerate(text):
        out[pos] = i
        pos += 2 if ord(ch) > 0xFFFF else 1
    out[pos] = len(text)
    return out


def _icons(text: str, entities: Sequence[Mapping[str, Any]] | None) -> dict[int, tuple[int, str]]:
    """Python index of a custom emoji → (end index, custom_emoji_id)."""
    starts = _utf16_starts(text)
    out: dict[int, tuple[int, str]] = {}
    for e in entities or ():
        if e.get("type") != "custom_emoji" or not isinstance(e.get("custom_emoji_id"), str):
            continue
        a, b = (
            starts.get(int(e.get("offset", -1))),
            starts.get(int(e.get("offset", 0)) + int(e.get("length", 0))),
        )
        if a is not None and b is not None:
            out[a] = (b, str(e["custom_emoji_id"]))
    return out


def _split(piece: _Piece, sep: str) -> list[_Piece]:
    parts: list[_Piece] = []
    pos = 0
    for chunk in piece.text.split(sep):
        parts.append(_Piece(piece.start + pos, chunk))
        pos += len(chunk) + len(sep)
    return parts


def _strip(piece: _Piece) -> _Piece:
    lead = len(piece.text) - len(piece.text.lstrip())
    return _Piece(piece.start + lead, piece.text.strip())


def _action(raw: str) -> dict[str, Any]:
    value = raw.strip()
    if _BARE_TG_RE.match(value):
        value = "https://" + value
    if _URL_RE.match(value):
        return {"type": "url", "url": value}
    kind, sep, rest = value.partition(":")
    kind = kind.strip().lower()
    if not sep or not rest.strip():
        raise ComposeError(
            f"Не понял действие «{value[:40]}»: нужна ссылка или screen:/system:/deeplink:/copy:"
        )
    rest = rest.strip()
    mapping = {
        "screen": ("screen", "target"),
        "экран": ("screen", "target"),
        "system": ("system", "name"),
        "deeplink": ("deeplink", "code"),
        "start": ("deeplink", "code"),
        "copy": ("copy", "text"),
        "share": ("share", "text"),
        "webapp": ("webapp", "url"),
    }
    if kind not in mapping:
        raise ComposeError(
            f"Неизвестное действие «{kind}»: ссылка, screen:, system:, deeplink:, copy:, share:"
        )
    typ, key = mapping[kind]
    return {"type": typ, key: rest}


def parse_buttons(text: str, entities: Sequence[Mapping[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Admin's button list → constructor button JSON (label, action, style, icon, row, sort).

    Format: one line = one row; buttons in a row are separated by ``;;``; a button is
    ``Текст | действие`` or ``Текст | действие | цвет`` (синяя/зелёная/красная). Action: a link
    (``https://…``, ``t.me/…``), ``screen:<код>``, ``system:<имя>``, ``deeplink:<код>``, ``copy:<текст>``,
    ``share:<текст>``, ``webapp:https://…``. A Premium emoji at the very start of the text becomes the icon.
    """
    icons = _icons(text, entities)
    out: list[dict[str, Any]] = []
    row = 0
    for line in _split(_Piece(0, text), "\n"):
        if not line.text.strip():
            continue
        cells = [_strip(c) for c in _split(line, ";;")]
        if len(cells) > MAX_ROW_WIDTH:
            raise ComposeError(f"В одном ряду не больше {MAX_ROW_WIDTH} кнопок.")
        for sort, cell in enumerate(cells):
            parts = _split(cell, "|")
            if len(parts) not in (2, 3):
                raise ComposeError(f"Строка «{cell.text[:40]}»: нужно «Текст | действие».")
            label_piece = _strip(parts[0])
            icon: str | None = None
            hit = icons.get(label_piece.start)
            label = label_piece.text
            if hit is not None:
                end, icon = hit
                label = text[end : label_piece.start + len(label_piece.text)].strip()
            if not label:
                raise ComposeError("У кнопки нет текста: после значка нужен текст.")
            item: dict[str, Any] = {
                "label": {"ru": label},
                "action": _action(parts[1].text),
                "row": row,
                "sort": sort,
            }
            if len(parts) == 3 and parts[2].text.strip():
                style = _STYLE_ALIASES.get(parts[2].text.strip().lower())
                if style is None:
                    raise ComposeError("Цвет кнопки: синяя, зелёная или красная.")
                item["style"] = style
            if icon is not None:
                item["icon_custom_emoji_id"] = icon
            try:
                Button.from_row(item)
            except ContentError as e:
                raise ComposeError(f"Кнопка «{label[:40]}»: {e.message}") from None
            out.append(item)
        row += 1
        if row > MAX_ROWS:
            raise ComposeError(f"Не больше {MAX_ROWS} рядов кнопок.")
    if not out:
        raise ComposeError("Не нашёл ни одной кнопки. Формат: «Текст | ссылка».")
    if len(out) > MAX_BUTTONS:
        raise ComposeError(f"Не больше {MAX_BUTTONS} кнопок.")
    return out


def build_markup(
    buttons: Sequence[Mapping[str, Any]], lang: str, *, bot_username: str | None = None
) -> InlineKeyboardMarkup | None:
    """Keyboard of a broadcast for ``lang``; buttons that cannot be built (e.g. a deep link while the bot
    username is unknown) are left out."""
    if not buttons:
        return None
    parsed: list[Button] = []
    for raw in buttons:
        try:
            parsed.append(Button.from_row(raw))
        except (ContentError, TypeError, ValueError):
            continue
    markup = build_keyboard(parsed, UserCtx(0, lang=lang), lang, bot_username=bot_username)
    return markup if markup.inline_keyboard else None
