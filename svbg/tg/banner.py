"""The default banner on every message the bot sends, until the owner removes it.

**On/off.** The banner is on while at least one content screen shows it (the owner's «🖼 Заглушка: убрать
везде / вернуть» switches every screen, so it switches everything). It is computed once per content snapshot
version, in memory. With the banner off nothing here changes a single request.

**Three places dress a message:**

* **C1** the screen router (:meth:`BannerPolicy.dress` on every :class:`~svbg.tg.ui.view.View` it delivers);
* **C3** messages built from content screens outside a click (``plain_view`` → :func:`view_scope`): a screen
  with its own picture shows that picture, a screen whose picture the owner removed shows none;
* **C2** :class:`BannerMiddleware`, an aiogram request middleware on every ``Bot``: everything else
  (notifier, admin chat, owner DMs, ``message.answer``…). ``sendMessage`` → ``sendPhoto`` with the text as
  the caption when it fits 1024; a longer text, or one that will likely be edited later (a callback keyboard,
  or :func:`banner_scope` ``"text"``), gets the banner as a link preview when ``PUBLIC_URL`` is set.
  ``editMessageText`` of a message C2 turned into a photo becomes ``editMessageCaption``; ``sendDocument``
  with an uploaded file gets the banner as its thumbnail. Rich messages, copies, messages with their own
  media and service calls are never touched.

The banner never cuts a text and never costs a message: a text over 1024 without ``PUBLIC_URL`` goes as is,
a refused picture is sent again without it. :func:`banner_scope` marks requests as already decided
(``"decided"``) or opts out (``"off"``).

The small module-level API (:func:`is_banner_on`, :func:`banner_file_id`, :func:`banner_photo`,
:func:`remember_banner`, :func:`banner_preview_url`) serves code that dresses messages itself (rich messages).
"""

from __future__ import annotations

import asyncio
import functools
import io
import logging
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal

from aiogram.client.default import Default
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import (
    EditMessageCaption,
    EditMessageText,
    SendAnimation,
    SendDocument,
    SendMessage,
    SendPhoto,
    SendVideo,
    TelegramMethod,
)
from aiogram.types import (
    BufferedInputFile,
    FSInputFile,
    InlineKeyboardMarkup,
    InputFile,
    LinkPreviewOptions,
    Message,
)

from svbg.content.banner import CAPTION_LIMIT, asset_bytes, banner_sha256
from svbg.content.media import resolve_media_path
from svbg.tg.ui.renderer import visible_len
from svbg.tg.ui.view import MediaRef, View

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.client.session.middlewares.base import NextRequestMiddlewareType

    from svbg.content.model import Media
    from svbg.content.store import ContentSnapshot, ContentStore

__all__ = [
    "BannerMiddleware",
    "BannerPolicy",
    "banner_file_id",
    "banner_photo",
    "banner_preview_url",
    "banner_scope",
    "banner_upload_lock",
    "current",
    "install",
    "is_banner_on",
    "remember_banner",
    "uninstall",
    "view_scope",
    "visible_len",
]

log = logging.getLogger("svbg.tg.banner")

Mode = Literal["auto", "decided", "off", "text"]
LRU_SIZE: Final = 10_000
THUMB_SIDE: Final = 320
THUMB_MAX_BYTES: Final = 200_000
_PICTURE_KINDS: Final = frozenset({"photo", "animation", "video"})
_NO_TEXT: Final = "there is no text in the message to edit"
#: Errors of a picture itself (lowercase substrings): the message goes again without it.
_MEDIA_ERRORS: Final = (
    "wrong file identifier",
    "wrong remote file identifier",
    "wrong file_id",
    "failed to get http url content",
    "photo_invalid_dimensions",
    "image_process_failed",
    "wrong type of the web page content",
    "not enough rights to send photos",
    "not enough rights to send videos",
    "not enough rights to send animations",
    "file must be non-empty",
)


# ---------------------------------------------------------------- scope


@dataclass(frozen=True, slots=True)
class _Scope:
    mode: Mode = "auto"
    media_id: int | None = None


_AUTO: Final = _Scope()
_SCOPE: ContextVar[_Scope] = ContextVar("svbg_banner", default=_AUTO)


@contextmanager
def banner_scope(mode: Mode = "off", *, media_id: int | None = None) -> Iterator[None]:
    """Requests made inside: ``off`` — never dress; ``decided`` — already dressed (the router);
    ``text`` — prefer the link preview (the message will be edited and may grow); ``auto`` with
    ``media_id`` — show that content picture instead of the default banner."""
    token = _SCOPE.set(_Scope(mode, media_id))
    try:
        yield
    finally:
        _SCOPE.reset(token)


def view_scope(view: View) -> AbstractContextManager[None]:
    """The scope a view sent outside the router asks for (``banner=False`` / its own ``picture``)."""
    if view.banner is False:
        return banner_scope("off")
    if view.picture is not None:
        return banner_scope("auto", media_id=view.picture)
    return nullcontext()


# ---------------------------------------------------------------- helpers


def _preview(url: str) -> LinkPreviewOptions:
    return LinkPreviewOptions(url=url, prefer_large_media=True, show_above_text=True)


def _free_preview(value: Any) -> bool:
    """The caller did not ask for a preview of its own (an explicit one wins)."""
    if value is None or isinstance(value, Default):
        return True
    return isinstance(value, LinkPreviewOptions) and bool(value.is_disabled) and not value.url


def _parse_mode(bot: Bot, value: Any) -> str | None:
    if isinstance(value, Default):
        try:
            value = bot.default[value.name]
        except (KeyError, AttributeError):
            return None
    return value if isinstance(value, str) else None


def _has_callbacks(markup: Any) -> bool:
    if not isinstance(markup, InlineKeyboardMarkup):
        return False
    return any(b.callback_data for row in markup.inline_keyboard for b in row)


def _file_id_of(message: Message, kind: str) -> str | None:
    if kind == "photo" and message.photo:
        return message.photo[-1].file_id
    file_id = getattr(getattr(message, kind, None), "file_id", None)
    return file_id if isinstance(file_id, str) else None


def is_media_error(exc: TelegramBadRequest) -> bool:
    desc = (exc.message or "").lower()
    return any(s in desc for s in _MEDIA_ERRORS)


def _bot_id(bot: Bot | int) -> int:
    return bot if isinstance(bot, int) else bot.id


@functools.cache
def thumbnail_bytes() -> bytes | None:
    """The banner as a document thumbnail: JPEG ≤ 320 px per side, well under 200 kB (once per process)."""
    data = asset_bytes()
    if not data:
        return None
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as img:
            rgb = img.convert("RGB")
            rgb.thumbnail((THUMB_SIDE, THUMB_SIDE))
            out = io.BytesIO()
            rgb.save(out, "JPEG", quality=85)
    except Exception:
        log.warning("banner thumbnail could not be made", exc_info=True)
        return None
    raw = out.getvalue()
    return raw if len(raw) < THUMB_MAX_BYTES else None


def _find_banner(snap: ContentSnapshot) -> int | None:
    """Media id of the banner when some screen (enabled or not) shows it."""
    sha = banner_sha256()
    if sha is None:
        return None
    ids = {m.id for m in snap.media.values() if m.sha256 == sha}
    if not ids:
        return None
    for entry in snap.by_id.values():
        if entry.screen.media_id in ids:
            return entry.screen.media_id
    return None


# ---------------------------------------------------------------- policy


class BannerPolicy:
    """What the banner is right now and how a message gets it (see module docstring)."""

    def __init__(
        self,
        content: ContentStore,
        *,
        public_url: Callable[[], str | None] | None = None,
        media_url: Callable[[str, Any], str | None] | None = None,
        media_root: Path | None = None,
    ) -> None:
        self.content = content
        self._public_url = public_url
        #: ``(base, media) → public link`` of the ``/m/<token>`` route; ``None`` → no link previews.
        self.media_url = media_url
        self._media_root = media_root.resolve() if media_root is not None else None
        self._memo: tuple[int, int | None] = (-1, None)
        #: One first upload per media at a time, shared with the screen router.
        self.locks: dict[int, asyncio.Lock] = {}
        self._photos: OrderedDict[tuple[int, Any, int], None] = OrderedDict()
        self.stats: dict[str, int] = {"skipped_long": 0, "media_errors": 0, "uploads": 0}

    # ------------------------------------------------------------ what

    def banner(self) -> Media | None:
        snap = self.content.snapshot
        version, media_id = self._memo
        if version != snap.version:
            media_id = _find_banner(snap)
            self._memo = (snap.version, media_id)
        return snap.get_media(media_id)

    def is_on(self) -> bool:
        return self.banner() is not None

    def picture(self, media_id: int | None = None) -> Media | None:
        """The picture to show: the content media ``media_id`` (a screen's own) or the default banner."""
        if media_id is not None:
            media = self.content.get_media(media_id)
            if media is not None and media.kind in _PICTURE_KINDS:
                return media
        return self.banner()

    def preview_url(self, media: Media) -> str | None:
        if self._public_url is None or self.media_url is None:
            return None
        try:
            base = self._public_url()
            return self.media_url(base, media) if base else None
        except Exception:
            log.warning("public link of media %s is not available", media.id, exc_info=True)
            return None

    def file_id(self, bot_id: int | None, media: Media) -> str | None:
        return None if bot_id is None else self.content.file_id(media.id, bot_id)

    def upload_file(self, media: Media) -> InputFile | None:
        if self._media_root is not None:
            path = resolve_media_path(self._media_root, media.path)
            if path is not None and path.is_file():
                return FSInputFile(path)
        if media.sha256 == banner_sha256():
            data = asset_bytes()
            if data:
                return BufferedInputFile(data, filename="banner.jpg")
        return None

    def attachment(self, bot_id: int | None, media: Media, *, allow_upload: bool = True) -> MediaRef | None:
        key = f"m:{media.id}"
        file_id = self.file_id(bot_id, media)
        if file_id:
            return MediaRef(media.kind, file_id, key, media.id)  # type: ignore[arg-type]
        if not allow_upload:
            return None
        upload = self.upload_file(media)
        return None if upload is None else MediaRef(media.kind, upload, key, media.id)  # type: ignore[arg-type]

    def lock(self, media_id: int) -> asyncio.Lock:
        return self.locks.setdefault(media_id, asyncio.Lock())

    # ------------------------------------------------------------ C1 / C3

    def dress(
        self, view: View, *, bot_id: int | None, allow_upload: bool = True, group: bool = False
    ) -> View:
        """``view`` with the picture: a photo when the text fits a caption, else a link preview (with
        ``PUBLIC_URL``), else unchanged. Views with their own media or preview, or ``banner=False``, stay."""
        if view.media is not None or view.preview is not None or view.banner is False:
            return view
        media = self.picture(view.picture)
        if media is None:
            return view
        n = visible_len(view.text, view.parse_mode)
        if n <= CAPTION_LIMIT and not group:
            ref = self.attachment(bot_id, media, allow_upload=allow_upload)
            if ref is not None:
                return replace(view, media=ref)
        url = self.preview_url(media)
        if url:
            return replace(view, preview=_preview(url))
        if n > CAPTION_LIMIT:
            self.stats["skipped_long"] += 1
        return view

    # ------------------------------------------------------------ bookkeeping

    async def learn(self, bot_id: int, media: Media, sent: Any) -> None:
        """Cache the ``file_id`` Telegram gave the uploaded picture (one UPDATE per bot, ever)."""
        if not isinstance(sent, Message):
            return
        file_id = _file_id_of(sent, media.kind)
        if not file_id:
            return
        self.stats["uploads"] += 1
        try:
            await self.content.remember_file_id(media.id, bot_id, file_id)
        except Exception as e:  # noqa: BLE001 - the message is out; the next one uploads again
            log.warning("could not cache file_id of media %s: %s", media.id, type(e).__name__)

    async def forget(self, bot_id: int, media: Media) -> None:
        """A cached ``file_id`` Telegram refused: drop it, the next message uploads the file again."""
        forget = getattr(self.content, "forget_file_id", None)
        if forget is None:
            return
        try:
            await forget(media.id, bot_id)
        except Exception as e:  # noqa: BLE001
            log.warning("could not drop file_id of media %s: %s", media.id, type(e).__name__)

    def mark_photo(self, bot_id: int, chat_id: Any, message_id: int) -> None:
        key = (bot_id, chat_id, message_id)
        self._photos[key] = None
        self._photos.move_to_end(key)
        while len(self._photos) > LRU_SIZE:
            self._photos.popitem(last=False)

    def is_photo(self, bot_id: int, chat_id: Any, message_id: Any) -> bool:
        return (bot_id, chat_id, message_id) in self._photos

    def with_thumbnail(self, method: SendDocument) -> SendDocument:
        if method.thumbnail is not None or not isinstance(method.document, InputFile) or not self.is_on():
            return method
        thumb = thumbnail_bytes()
        if thumb is None:
            return method
        return method.model_copy(update={"thumbnail": BufferedInputFile(thumb, filename="banner.jpg")})


# ---------------------------------------------------------------- C2


_HANDLED: Final = frozenset({SendMessage, EditMessageText, SendDocument})
_SEND_CLS: Final[dict[str, tuple[type[TelegramMethod[Message]], str]]] = {
    "photo": (SendPhoto, "photo"),
    "animation": (SendAnimation, "animation"),
    "video": (SendVideo, "video"),
}
_NOT_COPIED: Final = frozenset(
    {"text", "entities", "parse_mode", "link_preview_options", "disable_web_page_preview"}
)


def _as_media(method: SendMessage, kind: str, file: str | InputFile) -> TelegramMethod[Message]:
    cls, field = _SEND_CLS[kind]
    target = cls.model_fields
    fields: dict[str, Any] = {
        name: value
        for name in type(method).model_fields
        if name not in _NOT_COPIED and name in target and (value := getattr(method, name)) is not None
    }
    return cls(
        **fields,
        **{field: file},
        caption=method.text,
        caption_entities=method.entities,
        parse_mode=method.parse_mode,
    )


def _as_caption(method: EditMessageText) -> EditMessageCaption:
    return EditMessageCaption(
        business_connection_id=method.business_connection_id,
        chat_id=method.chat_id,
        message_id=method.message_id,
        caption=method.text,
        caption_entities=method.entities,
        parse_mode=method.parse_mode,
        reply_markup=method.reply_markup,
    )


class BannerMiddleware(BaseRequestMiddleware):
    """C2: dresses requests no other place decided (see module docstring)."""

    def __init__(self, policy: BannerPolicy) -> None:
        self.policy = policy

    async def __call__(
        self, make_request: NextRequestMiddlewareType[Any], bot: Bot, method: TelegramMethod[Any]
    ) -> Any:
        kind = type(method)
        if kind not in _HANDLED:
            return await make_request(bot, method)
        scope = _SCOPE.get()
        if scope.mode in ("decided", "off"):
            return await make_request(bot, method)
        if kind is SendDocument:
            return await make_request(bot, self.policy.with_thumbnail(method))  # type: ignore[arg-type]
        media = self.policy.picture(scope.media_id)
        if media is None:
            return await make_request(bot, method)
        if kind is SendMessage:
            return await self._send(make_request, bot, method, media, scope)  # type: ignore[arg-type]
        return await self._edit(make_request, bot, method, media)  # type: ignore[arg-type]

    async def _send(
        self,
        make_request: NextRequestMiddlewareType[Any],
        bot: Bot,
        method: SendMessage,
        media: Media,
        scope: _Scope,
    ) -> Any:
        if not _free_preview(method.link_preview_options):
            return await make_request(bot, method)
        n = visible_len(method.text, _parse_mode(bot, method.parse_mode))
        edited_later = scope.mode == "text" or _has_callbacks(method.reply_markup)
        if edited_later or n > CAPTION_LIMIT:
            url = self.policy.preview_url(media)
            if url:
                return await make_request(
                    bot, method.model_copy(update={"link_preview_options": _preview(url)})
                )
        if n > CAPTION_LIMIT:
            self.policy.stats["skipped_long"] += 1
            return await make_request(bot, method)
        file_id = self.policy.file_id(bot.id, media)
        if file_id:
            return await self._send_as(make_request, bot, method, media, file_id)
        async with self.policy.lock(media.id):  # one upload; the others wait and reuse the file_id
            file_id = self.policy.file_id(bot.id, media)
            if not file_id:
                upload = self.policy.upload_file(media)
                if upload is None:
                    return await make_request(bot, method)
                return await self._send_as(make_request, bot, method, media, upload)
        return await self._send_as(make_request, bot, method, media, file_id)

    async def _send_as(
        self,
        make_request: NextRequestMiddlewareType[Any],
        bot: Bot,
        method: SendMessage,
        media: Media,
        file: str | InputFile,
    ) -> Any:
        try:
            sent = await make_request(bot, _as_media(method, media.kind, file))
        except TelegramBadRequest as e:
            if not is_media_error(e):
                raise
            self.policy.stats["media_errors"] += 1
            log.warning("banner refused (%s); the message goes without it", e.message)
            if isinstance(file, str):
                await self.policy.forget(bot.id, media)
            return await make_request(bot, method)
        if isinstance(sent, Message):
            self.policy.mark_photo(bot.id, sent.chat.id, sent.message_id)
            if not isinstance(file, str):
                await self.policy.learn(bot.id, media, sent)
        return sent

    async def _edit(
        self, make_request: NextRequestMiddlewareType[Any], bot: Bot, method: EditMessageText, media: Media
    ) -> Any:
        if method.rich_message is not None or method.inline_message_id is not None or method.text is None:
            return await make_request(bot, method)
        fits = visible_len(method.text, _parse_mode(bot, method.parse_mode)) <= CAPTION_LIMIT
        if self.policy.is_photo(bot.id, method.chat_id, method.message_id):
            return await make_request(bot, _as_caption(method) if fits else method)
        edit: EditMessageText = method
        if _free_preview(method.link_preview_options):
            url = self.policy.preview_url(media)
            if url:
                edit = method.model_copy(update={"link_preview_options": _preview(url)})
        try:
            return await make_request(bot, edit)
        except TelegramBadRequest as e:
            if not fits or _NO_TEXT not in (e.message or "").lower():
                raise
        # a picture message (sent before a restart): edit its caption instead
        self.policy.mark_photo(bot.id, method.chat_id, method.message_id)
        return await make_request(bot, _as_caption(method))


# ---------------------------------------------------------------- module API (rich messages, tools)

_current: BannerPolicy | None = None


def install(policy: BannerPolicy) -> None:
    global _current  # noqa: PLW0603 - one app per process
    _current = policy


def uninstall(policy: BannerPolicy | None = None) -> None:
    global _current  # noqa: PLW0603
    if policy is None or _current is policy:
        _current = None


def current() -> BannerPolicy | None:
    return _current


def is_banner_on() -> bool:
    """The default banner is shown (some screen has it and the app installed the policy)."""
    return _current is not None and _current.is_on()


def banner_file_id(bot: Bot | int) -> str | None:
    """Telegram ``file_id`` of the banner for this bot, when it was uploaded before."""
    policy = _current
    media = None if policy is None else policy.banner()
    return None if policy is None or media is None else policy.file_id(_bot_id(bot), media)


def banner_photo(bot: Bot | int) -> str | InputFile | None:
    """What to put as the banner picture: the cached ``file_id`` or the file to upload (then call
    :func:`remember_banner` with the sent message, under :func:`banner_upload_lock`). ``None``: banner off."""
    policy = _current
    media = None if policy is None else policy.banner()
    if policy is None or media is None:
        return None
    return policy.file_id(_bot_id(bot), media) or policy.upload_file(media)


def banner_upload_lock() -> asyncio.Lock | None:
    policy = _current
    media = None if policy is None else policy.banner()
    return None if policy is None or media is None else policy.lock(media.id)


async def remember_banner(bot: Bot | int, sent: Any) -> None:
    """After a message uploaded the banner: cache its ``file_id`` so it is never uploaded again."""
    policy = _current
    media = None if policy is None else policy.banner()
    if policy is not None and media is not None:
        await policy.learn(_bot_id(bot), media, sent)


def banner_preview_url() -> str | None:
    """The banner's public link (``PUBLIC_URL/m/<token>``) for a link preview; ``None`` without it."""
    policy = _current
    media = None if policy is None else policy.banner()
    return None if policy is None or media is None else policy.preview_url(media)
