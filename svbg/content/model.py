"""Content model: screens, buttons, actions and media as plain immutable data (07 §2.4.1).

Nothing here knows about Telegram: the same model can be rendered by ``svbg.tg.ui.renderer`` and, later, by
a web renderer. Parsing from database/JSON values is strict (:class:`ContentError` with a field path), so bad
content is rejected when it is saved or imported rather than when a user opens the screen.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any, Final, Literal, cast
from urllib.parse import urlsplit

__all__ = [
    "BUTTON_STYLES",
    "MEDIA_KINDS",
    "MEDIA_MODES",
    "SCREEN_KINDS",
    "Action",
    "Button",
    "ContentError",
    "CopyAction",
    "DeeplinkAction",
    "Media",
    "ModuleAction",
    "Screen",
    "ScreenAction",
    "ShareAction",
    "SystemAction",
    "TextBlock",
    "UrlAction",
    "WebAppAction",
    "parse_action",
    "parse_label",
    "parse_text_blocks",
    "parse_title",
]

SCREEN_KINDS: Final = ("system", "custom")
MEDIA_KINDS: Final = ("photo", "animation", "video", "document")
MEDIA_MODES: Final = ("attach", "preview")
BUTTON_STYLES: Final = ("primary", "success", "danger")

MediaKind = Literal["photo", "animation", "video", "document"]
MediaMode = Literal["attach", "preview"]
ButtonStyle = Literal["primary", "success", "danger"]

_NAME_RE: Final = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,31}$")
_SCREEN_REF_RE: Final = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,31}$")
_DEEPLINK_RE: Final = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
_LANG_RE: Final = re.compile(r"^[a-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})?$")
_EMOJI_ID_RE: Final = re.compile(r"^\d{1,32}$")
_URL_SCHEMES: Final = ("http", "https", "tg")
MAX_LABEL: Final = 128
MAX_COPY_TEXT: Final = 256  # Bot API CopyTextButton limit
MAX_TEXT: Final = 4096
MAX_URL: Final = 2048
MAX_ENTITIES: Final = 100

_ENTITY_TYPES: Final = frozenset(
    {
        "mention",
        "hashtag",
        "cashtag",
        "bot_command",
        "url",
        "email",
        "phone_number",
        "bold",
        "italic",
        "underline",
        "strikethrough",
        "spoiler",
        "blockquote",
        "expandable_blockquote",
        "code",
        "pre",
        "text_link",
        "text_mention",
        "custom_emoji",
        "date_time",
    }
)
_ENTITY_KEYS: Final = frozenset(
    {
        "type",
        "offset",
        "length",
        "url",
        "user",
        "language",
        "custom_emoji_id",
        "unix_time",
        "date_time_format",
    }
)


class ContentError(ValueError):
    """Invalid content value; ``path`` names the field (``buttons[2].action.url``)."""

    def __init__(self, path: str, message: str) -> None:
        self.path = path
        self.message = message
        super().__init__(f"{path}: {message}")


# ---------------------------------------------------------------- actions


@dataclass(frozen=True, slots=True)
class ScreenAction:
    target: str  # screen code or numeric id as text
    kind: Literal["screen"] = "screen"

    def to_json(self) -> dict[str, Any]:
        return {"type": "screen", "target": self.target}


@dataclass(frozen=True, slots=True)
class SystemAction:
    name: str  # buy, renew, topup, connect, devices, invite, promo, support, lang, ...
    kind: Literal["system"] = "system"

    def to_json(self) -> dict[str, Any]:
        return {"type": "system", "name": self.name}


@dataclass(frozen=True, slots=True)
class UrlAction:
    url: str
    kind: Literal["url"] = "url"

    def to_json(self) -> dict[str, Any]:
        return {"type": "url", "url": self.url}


@dataclass(frozen=True, slots=True)
class WebAppAction:
    url: str
    kind: Literal["webapp"] = "webapp"

    def to_json(self) -> dict[str, Any]:
        return {"type": "webapp", "url": self.url}


@dataclass(frozen=True, slots=True)
class DeeplinkAction:
    code: str  # value of the bot's ``?start=`` parameter
    kind: Literal["deeplink"] = "deeplink"

    def to_json(self) -> dict[str, Any]:
        return {"type": "deeplink", "code": self.code}


@dataclass(frozen=True, slots=True)
class CopyAction:
    text: str
    kind: Literal["copy"] = "copy"

    def to_json(self) -> dict[str, Any]:
        return {"type": "copy", "text": self.text}


@dataclass(frozen=True, slots=True)
class ShareAction:
    text: str
    kind: Literal["share"] = "share"

    def to_json(self) -> dict[str, Any]:
        return {"type": "share", "text": self.text}


@dataclass(frozen=True, slots=True)
class ModuleAction:
    ext: str
    action: str
    kind: Literal["module"] = "module"

    def to_json(self) -> dict[str, Any]:
        return {"type": "module", "ext": self.ext, "action": self.action}


Action = (
    ScreenAction
    | SystemAction
    | UrlAction
    | WebAppAction
    | DeeplinkAction
    | CopyAction
    | ShareAction
    | ModuleAction
)


def _name(value: Any, path: str) -> str:
    if not isinstance(value, str) or not _NAME_RE.match(value):
        raise ContentError(path, "expected a short name [A-Za-z0-9_.-], up to 32 chars")
    return value


def _url(value: Any, path: str, *, https_only: bool = False) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_URL or any(c.isspace() for c in value):
        raise ContentError(path, f"expected a URL without spaces, up to {MAX_URL} chars")
    parts = urlsplit(value)
    if https_only:
        if parts.scheme != "https" or not parts.netloc:
            raise ContentError(path, "Web App URL must start with https://")
    elif parts.scheme not in _URL_SCHEMES or (parts.scheme != "tg" and not parts.netloc):
        raise ContentError(path, "URL must start with https://, http:// or tg://")
    return value


def _text(value: Any, path: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ContentError(path, f"expected non-empty text up to {limit} chars")
    return value


def parse_action(value: Any, path: str = "action") -> Action:
    """Parse ``{"type": ..., ...}`` or the compact form ``"screen:home"`` / ``"url:https://…"``."""
    if isinstance(value, str):
        kind, sep, rest = value.partition(":")
        if not sep:
            raise ContentError(path, "expected '<type>:<value>'")
        if kind == "module":
            ext, dot, act = rest.partition(".")
            if not dot:
                raise ContentError(path, "module action must be 'module:<ext>.<action>'")
            value = {"type": "module", "ext": ext, "action": act}
        else:
            key = {
                "screen": "target",
                "system": "name",
                "url": "url",
                "webapp": "url",
                "deeplink": "code",
                "copy": "text",
                "share": "text",
            }.get(kind)
            if key is None:
                raise ContentError(path, f"unknown action type '{kind}'")
            value = {"type": kind, key: rest}
    if not isinstance(value, Mapping):
        raise ContentError(path, "expected an object")
    kind = value.get("type")
    if kind == "screen":
        target = value.get("target")
        if isinstance(target, int) and not isinstance(target, bool) and target > 0:
            target = str(target)
        if not isinstance(target, str) or not _SCREEN_REF_RE.match(target):
            raise ContentError(f"{path}.target", "expected a screen code or id")
        return ScreenAction(target)
    if kind == "system":
        return SystemAction(_name(value.get("name"), f"{path}.name"))
    if kind == "url":
        return UrlAction(_url(value.get("url"), f"{path}.url"))
    if kind == "webapp":
        return WebAppAction(_url(value.get("url"), f"{path}.url", https_only=True))
    if kind == "deeplink":
        code = value.get("code")
        if not isinstance(code, str) or not _DEEPLINK_RE.match(code):
            raise ContentError(f"{path}.code", "expected [A-Za-z0-9_-], up to 64 chars")
        return DeeplinkAction(code)
    if kind == "copy":
        return CopyAction(_text(value.get("text"), f"{path}.text", MAX_COPY_TEXT))
    if kind == "share":
        return ShareAction(_text(value.get("text"), f"{path}.text", MAX_TEXT))
    if kind == "module":
        return ModuleAction(
            _name(value.get("ext"), f"{path}.ext"), _name(value.get("action"), f"{path}.action")
        )
    raise ContentError(f"{path}.type", f"unknown action type {kind!r}")


# ---------------------------------------------------------------- texts


@dataclass(frozen=True, slots=True)
class TextBlock:
    """Message text with Bot API entities (JSON dicts, offsets in UTF-16 code units)."""

    text: str
    entities: tuple[Mapping[str, Any], ...] = ()


def _lang(value: Any, path: str) -> str:
    if not isinstance(value, str) or not _LANG_RE.match(value):
        raise ContentError(path, "expected a language code like 'ru' or 'en'")
    return value


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _entities(value: Any, path: str, text_len16: int) -> tuple[Mapping[str, Any], ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > MAX_ENTITIES:
        raise ContentError(path, f"expected a list of up to {MAX_ENTITIES} entities")
    out: list[Mapping[str, Any]] = []
    for i, raw in enumerate(value):
        p = f"{path}[{i}]"
        if not isinstance(raw, Mapping):
            raise ContentError(p, "expected an object")
        if raw.get("type") not in _ENTITY_TYPES:
            raise ContentError(f"{p}.type", f"unknown entity type {raw.get('type')!r}")
        offset, length = raw.get("offset"), raw.get("length")
        if (
            isinstance(offset, bool)
            or isinstance(length, bool)
            or not isinstance(offset, int)
            or not isinstance(length, int)
            or offset < 0
            or length <= 0
            or offset + length > text_len16
        ):
            raise ContentError(p, "entity offset/length is outside the text")
        clean = {k: v for k, v in raw.items() if k in _ENTITY_KEYS}
        if clean["type"] == "text_link":
            clean["url"] = _url(raw.get("url"), f"{p}.url")
        if clean["type"] == "custom_emoji" and not (
            isinstance(raw.get("custom_emoji_id"), str) and _EMOJI_ID_RE.match(raw["custom_emoji_id"])
        ):
            raise ContentError(f"{p}.custom_emoji_id", "expected a numeric custom emoji id")
        out.append(MappingProxyType(clean))
    return tuple(out)


def parse_text_blocks(value: Any, path: str = "body") -> Mapping[str, TextBlock]:
    """``{lang: {"text": str, "entities": [...]}}`` (a bare string per language is accepted too)."""
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise ContentError(path, "expected an object {lang: {text, entities}}")
    out: dict[str, TextBlock] = {}
    for lang, block in value.items():
        p = f"{path}.{lang}"
        _lang(lang, p)
        item: Any = {"text": block} if isinstance(block, str) else block
        if not isinstance(item, Mapping):
            raise ContentError(p, "expected {text, entities}")
        text = item.get("text", "")
        if not isinstance(text, str) or len(text) > MAX_TEXT:
            raise ContentError(f"{p}.text", f"expected text up to {MAX_TEXT} chars")
        out[lang] = TextBlock(text, _entities(item.get("entities"), f"{p}.entities", _utf16_len(text)))
    return MappingProxyType(out)


def _str_by_lang(value: Any, path: str, limit: int) -> Mapping[str, str]:
    if value is None:
        return MappingProxyType({})
    if isinstance(value, str):  # a single string means the default language
        value = {"ru": value}
    if not isinstance(value, Mapping):
        raise ContentError(path, "expected an object {lang: text}")
    out: dict[str, str] = {}
    for lang, text in value.items():
        _lang(lang, f"{path}.{lang}")
        if not isinstance(text, str) or len(text) > limit:
            raise ContentError(f"{path}.{lang}", f"expected text up to {limit} chars")
        out[lang] = text
    return MappingProxyType(out)


def parse_label(value: Any, path: str = "label") -> Mapping[str, str]:
    labels = _str_by_lang(value, path, MAX_LABEL)
    if not any(t.strip() for t in labels.values()):
        raise ContentError(path, "button needs a label in at least one language")
    return labels


def parse_title(value: Any, path: str = "title") -> Mapping[str, str]:
    return _str_by_lang(value, path, 256)


# ---------------------------------------------------------------- entities


@dataclass(frozen=True, slots=True)
class Media:
    id: int
    kind: MediaKind
    sha256: str
    path: str | None = None
    mime: str | None = None
    size: int | None = None
    width: int | None = None
    height: int | None = None
    duration: int | None = None
    file_ids: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True, slots=True)
class Button:
    label: Mapping[str, str]
    action: Action
    id: int | None = None
    icon_custom_emoji_id: str | None = None
    style: ButtonStyle | None = None
    row: int = 0
    sort: int = 0
    visible_if: Mapping[str, Any] | None = None  # raw DSL; compiled by the content store
    enabled: bool = True
    system_key: str | None = None

    def __post_init__(self) -> None:
        if self.style is not None and self.style not in BUTTON_STYLES:
            raise ContentError("style", f"expected one of {', '.join(BUTTON_STYLES)}")
        if self.icon_custom_emoji_id is not None and not _EMOJI_ID_RE.match(self.icon_custom_emoji_id):
            raise ContentError("icon_custom_emoji_id", "expected a numeric custom emoji id")

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Button:
        style = row.get("style")
        return cls(
            id=row.get("id"),
            label=parse_label(row.get("label")),
            action=parse_action(row.get("action")),
            icon_custom_emoji_id=row.get("icon_custom_emoji_id"),
            style=cast("ButtonStyle | None", style),
            row=int(row.get("row") or 0),
            sort=int(row.get("sort") or 0),
            visible_if=row.get("visible_if"),
            enabled=bool(row.get("enabled", True)),
            system_key=row.get("system_key"),
        )


@dataclass(frozen=True, slots=True)
class Screen:
    id: int
    code: str | None
    kind: Literal["system", "custom"] = "custom"
    title: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    body: Mapping[str, TextBlock] = field(default_factory=lambda: MappingProxyType({}))
    media_id: int | None = None
    media_mode: MediaMode = "attach"
    enabled: bool = True
    version: int = 1
    updated_by: int | None = None
    updated_at: datetime | None = None
    buttons: tuple[Button, ...] = ()

    def text(self, lang: str, default_lang: str = "ru") -> TextBlock:
        """Body in ``lang`` → default language → any language → empty."""
        block = self.body.get(lang) or self.body.get(default_lang)
        if block is None and self.body:
            block = next(iter(self.body.values()))
        return block or TextBlock("")

    @classmethod
    def from_row(cls, row: Mapping[str, Any], buttons: Sequence[Button] = ()) -> Screen:
        kind = row.get("kind") or "custom"
        mode = row.get("media_mode") or "attach"
        if kind not in SCREEN_KINDS:
            raise ContentError("kind", f"expected one of {', '.join(SCREEN_KINDS)}")
        if mode not in MEDIA_MODES:
            raise ContentError("media_mode", f"expected one of {', '.join(MEDIA_MODES)}")
        return cls(
            id=int(row["id"]),
            code=row.get("code"),
            kind=kind,
            title=parse_title(row.get("title")),
            body=parse_text_blocks(row.get("body")),
            media_id=row.get("media_id"),
            media_mode=mode,
            enabled=bool(row.get("enabled", True)),
            version=int(row.get("version") or 1),
            updated_by=row.get("updated_by"),
            updated_at=row.get("updated_at"),
            buttons=tuple(buttons),
        )
