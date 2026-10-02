"""Media files of the constructor (07 §2.4.1, §2.2): files on disk by sha256, ``file_id`` cache, public ids.

* **Storage.** A file lives at ``DATA_DIR/media/<sha[:2]>/<sha256>.<ext>`` (written to a temp file, then
  ``os.replace``: a crash never leaves a half-written file under the final name). The ``media`` row keeps the
  path relative to the media directory, so the directory can be moved with ``DATA_DIR``. The same bytes are
  stored once (``sha256 UNIQUE``); uploading them again returns the existing row.
* **Validation.** The type is sniffed from the bytes (magic numbers), never taken from the client: photos are
  JPEG/PNG/WebP, animations GIF/MP4, videos MP4/MOV, documents anything. Sizes are capped per kind
  (:class:`MediaLimits`, defaults = Bot API upload limits); :meth:`MediaLibrary.check_declared` refuses an
  oversized Telegram file *before* it is downloaded.
* **Photos** are kept byte for byte only when they are a small clean JPEG (no EXIF, XMP, IPTC, ICC,
  comments or bytes after the image: the file can be public via ``/m/``); otherwise they are re-encoded with
  Pillow: shrunk to ``photo_max_side``, orientation applied, alpha flattened on white, progressive JPEG
  without any metadata. Memory is bounded (the bot shares a 512 MB container): big JPEGs are decoded already
  scaled down by libjpeg, other formats are refused above ``photo_decode_max_pixels``, intermediates are
  freed at once and one photo is processed at a time per process. Pillow is imported lazily and runs in
  :func:`asyncio.to_thread`. Decompression bombs are refused.
* **GIF / MP4 / MOV** (animations and videos are public too) lose their metadata without re-encoding: GIF
  comments and foreign application extensions are dropped; MP4/MOV ``udta``/``meta``/XMP boxes (location,
  author, device) are overwritten as ``free`` padding of the same size, so chunk offsets stay valid.
  Documents are never public and are stored as sent.
* **``file_id`` cache.** ``media.file_ids`` is ``{bot_id: file_id}``; an upload received from Telegram can be
  stored with its ``file_id`` right away when the bytes were kept as is (a re-encoded photo is a different
  file). The renderer learns the rest through :meth:`svbg.content.store.ContentStore.remember_file_id`.
* **Public ids** for ``/m/<id>`` (link-preview mode): ``HMAC(key, sha256)`` — unguessable, not sequential,
  stable for the same content and key; :class:`PublicMedia` resolves them from the in-memory content
  snapshot (no SQL per request), serving only photos, animations and videos.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import io
import logging
import os
import re
import secrets
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final, Protocol

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from svbg.content.model import MEDIA_KINDS, Media
from svbg.content.tables import media

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from svbg.content.store import ContentSnapshot
    from svbg.db.engine import Database

__all__ = [
    "EXT_BY_MIME",
    "PUBLIC_KINDS",
    "MediaError",
    "MediaLibrary",
    "MediaLimits",
    "PreparedMedia",
    "PublicMedia",
    "Sniffed",
    "StoredMedia",
    "media_rel_path",
    "prepare_media",
    "public_token",
    "resolve_media_path",
    "sniff",
    "strip_gif_metadata",
    "strip_isobmff_metadata",
    "upsert_media_row",
    "write_media_file",
]

log = logging.getLogger("svbg.content.media")

_MB: Final = 1024 * 1024
_SHA_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_EXT_RE: Final = re.compile(r"^[a-z0-9]{1,5}$")
#: Kinds that ``/m/<id>`` serves (link previews show pictures and videos, never documents).
PUBLIC_KINDS: Final = frozenset({"photo", "animation", "video"})
#: Telegram refuses photos with an aspect ratio above 20.
_MAX_ASPECT: Final = 20.0

EXT_BY_MIME: Final[Mapping[str, str]] = MappingProxyType(
    {
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
        "image/gif": "gif",
        "video/mp4": "mp4",
        "video/quicktime": "mov",
        "video/webm": "webm",
        "application/pdf": "pdf",
        "application/zip": "zip",
        "application/octet-stream": "bin",
    }
)

_ALLOWED: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "photo": frozenset({"image/jpeg", "image/png", "image/webp"}),
        "animation": frozenset({"image/gif", "video/mp4"}),
        "video": frozenset({"video/mp4", "video/quicktime"}),
    }
)

_KIND_RU: Final[Mapping[str, str]] = MappingProxyType(
    {"photo": "фото", "animation": "GIF", "video": "видео", "document": "файл"}
)


class MediaError(ValueError):
    """A file the library refuses; ``message`` is a short Russian text for the admin."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class MediaLimits:
    """Size and picture limits. Defaults follow the Bot API upload limits (photo 10 MB, others 50 MB)."""

    photo_max_bytes: int = 10 * _MB
    animation_max_bytes: int = 50 * _MB
    video_max_bytes: int = 50 * _MB
    document_max_bytes: int = 50 * _MB
    photo_max_side: int = 2560  # longest side after re-encoding
    photo_max_pixels: int = 50_000_000  # input (header) size; bigger images are refused (bombs)
    #: Pixels actually decoded (after libjpeg downscaling): ~4 bytes each, so 16 MP is about 64 MB.
    photo_decode_max_pixels: int = 16_000_000
    photo_keep_bytes: int = 1536 * 1024  # a clean JPEG up to this size is stored as is
    jpeg_quality: int = 85

    def max_bytes(self, kind: str) -> int:
        return {
            "photo": self.photo_max_bytes,
            "animation": self.animation_max_bytes,
            "video": self.video_max_bytes,
            "document": self.document_max_bytes,
        }[kind]


@dataclass(frozen=True, slots=True)
class Sniffed:
    mime: str
    ext: str


def sniff(data: bytes) -> Sniffed:
    """File type by magic numbers; unknown bytes are ``application/octet-stream``."""
    head = data[:32]
    mime = "application/octet-stream"
    if head.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif head.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        mime = "image/webp"
    elif head[:6] in (b"GIF87a", b"GIF89a"):
        mime = "image/gif"
    elif head[4:8] == b"ftyp":
        mime = "video/quicktime" if head[8:12] == b"qt  " else "video/mp4"
    elif head.startswith(b"\x1aE\xdf\xa3"):
        mime = "video/webm"
    elif head.startswith(b"%PDF-"):
        mime = "application/pdf"
    elif head.startswith(b"PK\x03\x04"):
        mime = "application/zip"
    return Sniffed(mime, EXT_BY_MIME[mime])


@dataclass(frozen=True, slots=True)
class PreparedMedia:
    """Validated (and for photos possibly re-encoded) bytes, ready to be stored."""

    kind: str
    data: bytes
    sha256: str
    mime: str
    ext: str
    width: int | None = None
    height: int | None = None
    duration: int | None = None
    reencoded: bool = False

    @property
    def size(self) -> int:
        return len(self.data)


def _positive(value: int | None) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _too_big(kind: str, limit: int) -> MediaError:
    return MediaError("too_large", f"Слишком большой {_KIND_RU[kind]}: максимум {max(limit // _MB, 1)} МБ.")


def prepare_media(
    data: bytes,
    kind: str,
    limits: MediaLimits | None = None,
    *,
    width: int | None = None,
    height: int | None = None,
    duration: int | None = None,
) -> PreparedMedia:
    """Validate ``data`` as ``kind`` (CPU-bound; call it through :func:`asyncio.to_thread`)."""
    limits = limits or MediaLimits()
    if kind not in MEDIA_KINDS:
        raise MediaError("bad_kind", "Неизвестный тип медиа.")
    if not data:
        raise MediaError("empty", "Файл пустой.")
    if len(data) > limits.max_bytes(kind):
        raise _too_big(kind, limits.max_bytes(kind))
    found = sniff(data)
    allowed = _ALLOWED.get(kind)
    if allowed is not None and found.mime not in allowed:
        raise MediaError("bad_type", _bad_type_text(kind))
    if kind == "photo":
        return _prepare_photo(data, found, limits)
    cleaned = data
    if kind in PUBLIC_KINDS and found.mime in ("video/mp4", "video/quicktime"):
        cleaned = strip_isobmff_metadata(data)
    elif kind in PUBLIC_KINDS and found.mime == "image/gif":
        cleaned = strip_gif_metadata(data)
    return PreparedMedia(
        kind=kind,
        data=cleaned,
        sha256=hashlib.sha256(cleaned).hexdigest(),
        mime=found.mime,
        ext=found.ext,
        width=_positive(width),
        height=_positive(height),
        duration=_positive(duration),
        reencoded=cleaned is not data,
    )


def _bad_type_text(kind: str) -> str:
    return {
        "photo": "Это не картинка: пришлите JPEG, PNG или WebP.",
        "animation": "Это не GIF: пришлите GIF или MP4 без звука.",
        "video": "Это не видео: пришлите MP4.",
    }.get(kind, "Неподдерживаемый тип файла.")


_TOO_MANY_PIXELS: Final = "Картинка слишком большая по пикселям — уменьшите её."


def _check_geometry(width: int, height: int, limits: MediaLimits) -> None:
    if width <= 0 or height <= 0 or width * height > limits.photo_max_pixels:
        raise MediaError("too_many_pixels", _TOO_MANY_PIXELS)
    if max(width, height) / max(min(width, height), 1) > _MAX_ASPECT:
        raise MediaError("bad_aspect", "Картинка слишком вытянутая: Telegram её не примет.")


#: One photo is decoded at a time in the whole process (uploads and content imports): the bot runs in a
#: small container and two parallel decodes would double the peak memory.
_PHOTO_LOCK: Final = threading.Lock()
#: JPEG markers a "clean" kept-as-is photo may carry: JFIF/JFXX (APP0) and Adobe colour transform (APP14).
_CLEAN_APP_PREFIXES: Final[Mapping[str, tuple[bytes, ...]]] = MappingProxyType(
    {"APP0": (b"JFIF\x00", b"JFXX\x00"), "APP14": (b"Adobe",)}
)
#: Info keys that mean metadata (EXIF, XMP, IPTC/Photoshop, ICC, comment).
_META_INFO_KEYS: Final = ("exif", "xmp", "comment", "icc_profile", "photoshop")
_EXIF_ORIENTATION: Final = 0x0112
#: Modes Pillow resizes directly; anything else (palette, 16-bit, LA, ...) is converted first.
_RESIZABLE_MODES: Final = frozenset({"RGB", "RGBA", "L", "CMYK"})


def _swap(old: Any, new: Any) -> Any:
    """Free the pixels of ``old`` as soon as ``new`` replaces it (peak memory, not wait for the GC)."""
    if new is not old:
        old.close()
    return new


def _upright(orientation: object) -> Any:
    """``Image.Transpose`` that makes a picture with this EXIF orientation upright (``None``: as is)."""
    from PIL.Image import Transpose

    fix = {
        2: Transpose.FLIP_LEFT_RIGHT,
        3: Transpose.ROTATE_180,
        4: Transpose.FLIP_TOP_BOTTOM,
        5: Transpose.TRANSPOSE,
        6: Transpose.ROTATE_270,
        7: Transpose.TRANSVERSE,
        8: Transpose.ROTATE_90,
    }
    return fix.get(orientation) if isinstance(orientation, int) else None


def _check_decoded(width: int, height: int, fmt: str, limits: MediaLimits) -> None:
    """Refuse a bitmap too big to decode; WebP gets half (its decoder keeps two extra full copies)."""
    budget = limits.photo_decode_max_pixels // (2 if fmt == "WEBP" else 1)
    if width * height > budget:
        raise MediaError("too_many_pixels", _TOO_MANY_PIXELS)


def _jpeg_is_clean(probe: Any, data: bytes) -> bool:
    """No EXIF, XMP, IPTC (APP13), ICC, comments or bytes after EOI: nothing that may identify the author."""
    applist = getattr(probe, "applist", None)
    if not isinstance(applist, list) or not data.endswith(b"\xff\xd9"):
        return False
    if any(key in probe.info for key in _META_INFO_KEYS):
        return False
    for marker, payload in applist:
        prefixes = _CLEAN_APP_PREFIXES.get(marker)
        if prefixes is None or not bytes(payload).startswith(prefixes):
            return False
    return True


def _jpeg_draft_scale(width: int, height: int, limits: MediaLimits) -> int:
    """DCT scale (1/2/4/8) to decode a big JPEG with: no coarser than the output needs, unless RAM demands."""
    scale = 1
    for s in (2, 4, 8):
        if s > min(width, height):
            break
        lossless = max(width, height) // s >= limits.photo_max_side
        fits = -(-width // scale) * -(-height // scale) <= limits.photo_decode_max_pixels
        if lossless or not fits:
            scale = s
    return scale


def _prepare_photo(data: bytes, found: Sniffed, limits: MediaLimits) -> PreparedMedia:
    """Keep a clean small JPEG; otherwise decode (bounded memory), shrink, orient, flatten, re-encode.

    Memory: a JPEG is decoded already scaled down by libjpeg (``draft``); other formats are refused above
    ``photo_decode_max_pixels`` (WebP: half). Alpha is flattened into a fresh RGB bitmap and the source
    freed at once; then the image is shrunk in place *before* rotation and the final colour conversion. So
    at most two full-size bitmaps exist briefly, and one photo is processed at a time.
    """
    from PIL import Image, UnidentifiedImageError  # lazy: only the admin upload path needs it

    try:
        with _PHOTO_LOCK, Image.open(io.BytesIO(data)) as probe:
            w0, h0 = probe.size
            _check_geometry(w0, h0, limits)
            fmt = (probe.format or "").upper()
            keep = (
                fmt == "JPEG"
                and found.mime == "image/jpeg"
                and probe.mode in ("RGB", "L")
                and max(w0, h0) <= limits.photo_max_side
                and len(data) <= limits.photo_keep_bytes
                and _jpeg_is_clean(probe, data)
                and len(probe.getexif()) == 0
            )
            if keep:
                probe.load()  # full decode: a truncated or broken JPEG is refused here, not by Telegram
                return PreparedMedia(
                    "photo", data, hashlib.sha256(data).hexdigest(), "image/jpeg", "jpg", w0, h0
                )
            if fmt == "JPEG":
                scale = _jpeg_draft_scale(w0, h0, limits)
                if scale > 1:
                    probe.draft(None, (w0 // scale, h0 // scale))
            _check_decoded(*probe.size, fmt, limits)
            probe.load()
            orientation = probe.getexif().get(_EXIF_ORIENTATION, 1)  # read before conversions drop it
            image: Image.Image = probe
            if image.mode not in _RESIZABLE_MODES:
                alpha = image.mode in ("LA", "PA", "La", "RGBa") or "transparency" in image.info
                image = _swap(image, image.convert("RGBA" if alpha else "RGB"))
            if image.mode == "RGBA":  # flatten at full size: resizing RGBA would premultiply into a copy
                flat = Image.new("RGB", image.size, (255, 255, 255))
                flat.paste(image, (0, 0), image)
                image = _swap(image, flat)
            if max(image.size) > limits.photo_max_side:
                image.thumbnail((limits.photo_max_side, limits.photo_max_side), Image.Resampling.LANCZOS)
            if image.mode != "RGB":
                image = _swap(image, image.convert("RGB"))
            method = _upright(orientation)
            if method is not None:
                image = _swap(image, image.transpose(method))
            image.info.clear()  # Pillow would carry a JPEG comment or XMP of the source into the output
            out = io.BytesIO()
            image.save(out, "JPEG", quality=limits.jpeg_quality, optimize=True, progressive=True)
            width, height = image.size
            del image
    except MediaError:
        raise
    except Image.DecompressionBombError:
        raise MediaError("too_many_pixels", _TOO_MANY_PIXELS) from None
    except MemoryError:
        raise MediaError("too_many_pixels", _TOO_MANY_PIXELS) from None
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        raise MediaError("broken", "Не удалось прочитать картинку: файл повреждён.") from None
    encoded = out.getvalue()
    return PreparedMedia(
        "photo",
        encoded,
        hashlib.sha256(encoded).hexdigest(),
        "image/jpeg",
        "jpg",
        width,
        height,
        reencoded=True,
    )


# ---------------------------------------------------------------- video / GIF metadata

_BROKEN_MEDIA: Final = "Не удалось прочитать файл: он повреждён."
#: ISO-BMFF (MP4/MOV) boxes that hold user metadata: ©xyz/loci location, author, device, XMP.
_ISO_META_BOXES: Final = frozenset({b"udta", b"meta"})
#: Containers whose children are searched for metadata boxes (the movie and its tracks).
_ISO_SEARCH: Final = frozenset({b"moov", b"trak"})
#: ``uuid`` box of Adobe XMP.
_XMP_UUID: Final = bytes.fromhex("be7acfcb97a942e89c71999491e3afac")
#: GIF application extensions needed for playback (loop count); every other one (XMP, ...) is dropped.
_GIF_KEEP_APPS: Final = (b"NETSCAPE2.0", b"ANIMEXTS1.0")


def _iso_boxes(data: bytes, start: int, end: int) -> list[tuple[int, int, bytes, int]]:
    """``(offset, header length, type, size)`` of the boxes in ``data[start:end]``; strict."""
    out: list[tuple[int, int, bytes, int]] = []
    pos = start
    while pos < end:
        if end - pos < 8:
            raise MediaError("broken", _BROKEN_MEDIA)
        size = int.from_bytes(data[pos : pos + 4], "big")
        kind = data[pos + 4 : pos + 8]
        header = 8
        if size == 1:
            if end - pos < 16:
                raise MediaError("broken", _BROKEN_MEDIA)
            size = int.from_bytes(data[pos + 8 : pos + 16], "big")
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            raise MediaError("broken", _BROKEN_MEDIA)
        out.append((pos, header, kind, size))
        pos += size
    return out


def _replace_spans(data: bytes, spans: list[tuple[int, int, bytes]]) -> bytes:
    """``data`` with non-overlapping ``(start, end, replacement)`` spans substituted (one output copy)."""
    view = memoryview(data)
    parts: list[bytes | memoryview] = []
    pos = 0
    for start, end, repl in sorted(spans, key=lambda span: span[0]):
        parts += [view[pos:start], repl]
        pos = end
    parts.append(view[pos:])
    return b"".join(parts)


def strip_isobmff_metadata(data: bytes) -> bytes:
    """MP4/MOV without user metadata: ``udta``/``meta`` boxes (and XMP ``uuid``) become ``free`` padding.

    Sizes and offsets stay the same (``stco`` chunk offsets remain valid), so nothing is re-muxed. A clean
    file is returned as the same object.
    """
    spans: list[tuple[int, int, bytes]] = []

    def blank(pos: int, header: int, size: int) -> None:
        head = data[pos : pos + 4] + b"free" + data[pos + 8 : pos + header]
        spans.append((pos, pos + size, head + bytes(size - header)))

    for pos, header, kind, size in _iso_boxes(data, 0, len(data)):
        xmp = kind == b"uuid" and data[pos + header : pos + header + 16] == _XMP_UUID
        if kind in _ISO_META_BOXES or xmp:
            blank(pos, header, size)
        elif kind == b"moov":
            for cpos, cheader, ckind, csize in _iso_boxes(data, pos + header, pos + size):
                if ckind in _ISO_META_BOXES:
                    blank(cpos, cheader, csize)
                elif ckind in _ISO_SEARCH:
                    for tpos, theader, tkind, tsize in _iso_boxes(data, cpos + cheader, cpos + csize):
                        if tkind in _ISO_META_BOXES:
                            blank(tpos, theader, tsize)
    return _replace_spans(data, spans) if spans else data


def _gif_sub_blocks(data: bytes, pos: int) -> tuple[int, bytes]:
    """End of the data sub-blocks starting at ``pos`` and the first block's payload."""
    first: bytes | None = None
    n = len(data)
    while True:
        if pos >= n:
            raise MediaError("broken", _BROKEN_MEDIA)
        length = data[pos]
        if pos + 1 + length > n:
            raise MediaError("broken", _BROKEN_MEDIA)
        if first is None:
            first = data[pos + 1 : pos + 1 + length]
        pos += 1 + length
        if length == 0:
            return pos, first


def strip_gif_metadata(data: bytes) -> bytes:
    """GIF without comments, foreign application extensions (XMP, ...) and bytes after the trailer."""
    n = len(data)
    if n < 13:
        raise MediaError("broken", _BROKEN_MEDIA)
    pos = 13
    if data[10] & 0x80:
        pos += 3 * (2 << (data[10] & 7))
    spans: list[tuple[int, int, bytes]] = []
    while True:
        if pos >= n:
            raise MediaError("broken", _BROKEN_MEDIA)
        block = data[pos]
        if block == 0x3B:  # trailer
            if pos + 1 < n:
                spans.append((pos + 1, n, b""))
            break
        if block == 0x21:  # extension: label + sub-blocks
            if pos + 2 > n:
                raise MediaError("broken", _BROKEN_MEDIA)
            label = data[pos + 1]
            end, first = _gif_sub_blocks(data, pos + 2)
            if label == 0xFE or (label == 0xFF and first[:11] not in _GIF_KEEP_APPS):
                spans.append((pos, end, b""))
            pos = end
        elif block == 0x2C:  # image descriptor (+ local colour table) + LZW code size + data
            if pos + 10 > n:
                raise MediaError("broken", _BROKEN_MEDIA)
            flags = data[pos + 9]
            pos += 10 + (3 * (2 << (flags & 7)) if flags & 0x80 else 0) + 1
            pos, _ = _gif_sub_blocks(data, pos)
        else:
            raise MediaError("broken", _BROKEN_MEDIA)
    return _replace_spans(data, spans) if spans else data


# ---------------------------------------------------------------- disk


def media_rel_path(sha256: str, ext: str) -> str:
    """``ab/abcdef….jpg`` — relative to the media directory."""
    if not _SHA_RE.match(sha256) or not _EXT_RE.match(ext):
        raise MediaError("bad_name", "Некорректное имя медиафайла.")
    return f"{sha256[:2]}/{sha256}.{ext}"


def resolve_media_path(root: Path, rel: str | None) -> Path | None:
    """Absolute path of ``rel`` inside ``root``; ``None`` if empty or escaping the directory."""
    if not rel:
        return None
    base = root.resolve()
    path = (base / rel).resolve()
    return path if path.is_relative_to(base) and path != base else None


def write_media_file(root: Path, sha256: str, ext: str, data: bytes) -> str:
    """Store ``data`` (checked against ``sha256``) atomically; returns the relative path (blocking I/O)."""
    if hashlib.sha256(data).hexdigest() != sha256:
        raise MediaError("hash_mismatch", "Файл повреждён: контрольная сумма не совпала.")
    rel = media_rel_path(sha256, ext)
    target = root / rel
    if target.is_file() and target.stat().st_size == len(data):
        return rel
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{secrets.token_hex(6)}.tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
    return rel


async def upsert_media_row(
    conn: AsyncConnection,
    *,
    kind: str,
    sha256: str,
    path: str,
    mime: str | None,
    size: int,
    width: int | None = None,
    height: int | None = None,
    duration: int | None = None,
    file_ids: Mapping[str, str] | None = None,
) -> tuple[int, bool]:
    """Insert or refresh the row of ``sha256``; returns ``(id, created)``. Learned ``file_ids`` are merged."""
    extra = dict(file_ids or {})
    stmt = pg_insert(media).values(
        kind=kind,
        sha256=sha256,
        path=path,
        mime=mime,
        size=size,
        width=width,
        height=height,
        duration=duration,
        file_ids=extra,
    )
    ex = stmt.excluded
    stmt = stmt.on_conflict_do_update(
        index_elements=[media.c.sha256],
        set_={
            "path": ex.path,
            "mime": sa.func.coalesce(ex.mime, media.c.mime),
            "size": ex.size,
            "width": sa.func.coalesce(media.c.width, ex.width),
            "height": sa.func.coalesce(media.c.height, ex.height),
            "duration": sa.func.coalesce(media.c.duration, ex.duration),
            "file_ids": media.c.file_ids.op("||")(ex.file_ids),
        },
    ).returning(media.c.id, sa.literal_column("(xmax = 0)").label("created"))
    row = (await conn.execute(stmt)).one()
    return int(row.id), bool(row.created)


@dataclass(frozen=True, slots=True)
class StoredMedia:
    media: Media
    created: bool
    reencoded: bool


class MediaLibrary:
    """Upload path of the constructor: validate → (re-encode) → disk → ``media`` row."""

    def __init__(
        self,
        db: Database,
        root: Path,
        *,
        limits: MediaLimits | Callable[[], MediaLimits] | None = None,
    ) -> None:
        self._db = db
        self._root = root
        self._limits = limits or MediaLimits()

    @property
    def root(self) -> Path:
        return self._root

    def limits(self) -> MediaLimits:
        lim = self._limits
        return lim if isinstance(lim, MediaLimits) else lim()

    def check_declared(self, kind: str, size: int | None) -> None:
        """Refuse a Telegram file by its declared ``file_size`` before downloading it."""
        if kind not in MEDIA_KINDS:
            raise MediaError("bad_kind", "Неизвестный тип медиа.")
        limit = self.limits().max_bytes(kind)
        if size is not None and size > limit:
            raise _too_big(kind, limit)

    def path_of(self, item: Media) -> Path | None:
        """The file of ``item`` on disk, if it is there."""
        path = resolve_media_path(self._root, item.path)
        return path if path is not None and path.is_file() else None

    async def add(
        self,
        data: bytes,
        kind: str,
        *,
        bot_id: int | str | None = None,
        file_id: str | None = None,
        width: int | None = None,
        height: int | None = None,
        duration: int | None = None,
    ) -> StoredMedia:
        """Store an upload. ``file_id`` (with ``bot_id``) is cached only when the bytes were kept as is."""
        prepared = await asyncio.to_thread(
            prepare_media, data, kind, self.limits(), width=width, height=height, duration=duration
        )
        rel = await asyncio.to_thread(
            write_media_file, self._root, prepared.sha256, prepared.ext, prepared.data
        )
        file_ids: dict[str, str] = {}
        if file_id and bot_id is not None and not prepared.reencoded:
            file_ids[str(bot_id)] = file_id
        async with self._db.tx() as conn:
            media_id, created = await upsert_media_row(
                conn,
                kind=prepared.kind,
                sha256=prepared.sha256,
                path=rel,
                mime=prepared.mime,
                size=prepared.size,
                width=prepared.width,
                height=prepared.height,
                duration=prepared.duration,
                file_ids=file_ids,
            )
        item = Media(
            id=media_id,
            kind=prepared.kind,  # type: ignore[arg-type]  # validated by prepare_media
            sha256=prepared.sha256,
            path=rel,
            mime=prepared.mime,
            size=prepared.size,
            width=prepared.width,
            height=prepared.height,
            duration=prepared.duration,
            file_ids=MappingProxyType(file_ids),
        )
        log.info("media %s stored (%s, %d bytes, new=%s)", media_id, prepared.kind, prepared.size, created)
        return StoredMedia(item, created, prepared.reencoded)


# ---------------------------------------------------------------- public ids


def public_token(key: bytes, sha256: str) -> str:
    """Unguessable public id of a file: 22 url-safe chars of ``HMAC-SHA256(key, sha256)``."""
    digest = hmac.new(key, b"svbg:media:v1:" + sha256.encode("ascii"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest[:16]).decode("ascii").rstrip("=")


class _SnapshotSource(Protocol):
    @property
    def snapshot(self) -> ContentSnapshot: ...


@dataclass(frozen=True, slots=True)
class _Index:
    version: int
    by_token: Mapping[str, Media]


class PublicMedia:
    """``/m/<token>`` ids for the content snapshot; the index is rebuilt once per snapshot version."""

    def __init__(self, content: _SnapshotSource, root: Path, key: bytes) -> None:
        if len(key) < 16:
            raise ValueError("public media key must be at least 16 bytes")
        self._content = content
        self._root = root
        self._key = key
        self._index = _Index(-1, MappingProxyType({}))

    def token(self, item: Media) -> str:
        return public_token(self._key, item.sha256)

    def path(self, item: Media) -> str:
        """``/m/<token>.<ext>`` (the extension helps clients that look at it; the route accepts both)."""
        ext = EXT_BY_MIME.get(item.mime or "", "")
        return f"/m/{self.token(item)}" + (f".{ext}" if ext and ext != "bin" else "")

    def url(self, base: str, item: Media) -> str:
        return base.rstrip("/") + self.path(item)

    def _current(self) -> Mapping[str, Media]:
        snap = self._content.snapshot
        index = self._index
        if index.version != snap.version:
            by_token = {self.token(m): m for m in snap.media.values() if m.kind in PUBLIC_KINDS}
            index = _Index(snap.version, MappingProxyType(by_token))
            self._index = index  # one reference assignment, like the snapshot itself
        return index.by_token

    def resolve(self, token: str) -> tuple[Media, Path] | None:
        """The media and its file for ``token``; ``None`` for unknown tokens, documents and missing files."""
        item = self._current().get(token)
        if item is None:
            return None
        path = resolve_media_path(self._root, item.path)
        if path is None or not path.is_file():
            return None
        return item, path
