"""What screen renderers and action handlers return."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    LinkPreviewOptions,
    MessageEntity,
)

__all__ = ["MediaKind", "MediaRef", "Redirect", "Toast", "View"]

MediaKind = Literal["photo", "animation", "video", "document"]


@dataclass(frozen=True, slots=True)
class MediaRef:
    """Media attached to a view.

    ``file`` is a Telegram ``file_id``, an HTTPS URL or an ``InputFile`` (upload). ``key`` identifies the
    media independently of how it is sent (``"m:12"`` for content media): when the old and the new message
    show the same key, only the caption is edited. ``media_id`` lets the router cache the ``file_id`` after
    an upload.
    """

    kind: MediaKind
    file: str | InputFile
    key: str | None = None
    media_id: int | None = None


@dataclass(slots=True)
class View:
    """A rendered screen: text (with ``entities`` *or* ``parse_mode``), optional media, keyboard, toast.

    ``mode="new"`` sends a fresh message (and deletes the previous main message) instead of editing it.
    ``preview`` sets link preview options for text messages (the "preview link" media mode).
    ``banner``: ``None`` lets the default banner policy (:mod:`svbg.tg.banner`) decide, ``False`` keeps the
    message without a picture (a content screen whose picture the owner removed). ``picture`` is the content
    media to show instead of the default banner when the view is sent outside the router (notifications).
    """

    text: str = ""
    entities: Sequence[MessageEntity] | None = None
    parse_mode: str | None = None
    media: MediaRef | None = None
    keyboard: Sequence[Sequence[InlineKeyboardButton]] | InlineKeyboardMarkup | None = None
    toast: str | None = None
    toast_alert: bool = False
    mode: Literal["edit", "new"] = "edit"
    preview: LinkPreviewOptions | None = None
    banner: bool | None = None
    picture: int | None = None

    def __post_init__(self) -> None:
        if self.entities and self.parse_mode:
            raise ValueError("a view uses either entities or parse_mode, never both")
        if self.mode not in ("edit", "new"):
            raise ValueError("mode must be 'edit' or 'new'")


@dataclass(frozen=True, slots=True)
class Toast:
    """Handler result: answer the callback with a toast and keep the current message as is."""

    text: str
    alert: bool = False


@dataclass(frozen=True, slots=True)
class Redirect:
    """Handler result: render another screen (optionally with a toast)."""

    screen: str
    arg: Any = None
    toast: str | None = None
