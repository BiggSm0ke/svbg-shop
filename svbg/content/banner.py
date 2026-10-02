"""Default banner: the built-in placeholder picture of every screen, until the owner takes it off.

* **Asset.** ``svbg/content/assets/default_banner.jpg`` ships inside the package (wheel and Docker image).
  It is a small clean JPEG, so :class:`~svbg.content.media.MediaLibrary` stores it byte for byte and the
  ``media`` row's ``sha256`` is the sha256 of the asset: the banner is told apart from the owner's pictures
  by that hash (:func:`is_banner`), never by id. :func:`ensure_banner_media` writes the file to
  ``DATA_DIR/media`` and upserts the row on start (nothing to do when both are there).
* **Where it goes.** System screens created by seeding get it in the same INSERT, every system screen without
  a picture of its own gets it once per installation (``config_meta['content.default_banner_v1']``), and a new
  own screen of the constructor gets it — while the banner is :func:`active <banner_active>`: not migrated yet
  or still shown somewhere (the owner who took it off every screen does not get it back on new ones).
  The write side (migration, «убрать со всех» / «вернуть», audit, undo) is :mod:`svbg.content.editing`.
* **Mode** (:func:`banner_mode`): an attachment when the text fits a caption in every language (≤ 1024 UTF-16
  units), else a link preview when ``PUBLIC_URL`` is set, else the screen stays without the banner.
* The owner's pictures are never touched: the banner only fills ``media_id IS NULL`` and only the banner's
  own ``media_id`` is ever cleared.
"""

from __future__ import annotations

import functools
import hashlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal

import sqlalchemy as sa

from svbg.content.media import MediaLibrary, resolve_media_path
from svbg.content.tables import media, screens
from svbg.core.tables import config_meta

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from svbg.content.model import Media
    from svbg.db.engine import Database

__all__ = [
    "ASSET",
    "CAPTION_LIMIT",
    "META_KEY",
    "BannerSeed",
    "active_banner_id",
    "asset_bytes",
    "banner_active",
    "banner_id",
    "banner_mode",
    "banner_sha256",
    "ensure_banner_media",
    "is_banner",
]

log = logging.getLogger("svbg.content.banner")

ASSET: Final = Path(__file__).resolve().parent / "assets" / "default_banner.jpg"
#: ``config_meta`` flag of the one-time migration of existing installations (value: what it did).
META_KEY: Final = "content.default_banner_v1"
#: Caption limit of a media message (Bot API), in UTF-16 code units.
CAPTION_LIMIT: Final = 1024


@dataclass(frozen=True, slots=True)
class BannerSeed:
    """The banner for :func:`svbg.content.store.seed_system_screens`: put it on the screens it creates."""

    media_id: int
    preview_ok: bool  # PUBLIC_URL is set: a text over the caption limit can show it as a link preview


@functools.cache
def asset_bytes() -> bytes | None:
    """The packaged picture; ``None`` when the installation lacks it (then there is no banner at all)."""
    try:
        return ASSET.read_bytes()
    except OSError:
        log.warning("default banner asset %s is missing", ASSET)
        return None


@functools.cache
def banner_sha256() -> str | None:
    data = asset_bytes()
    return hashlib.sha256(data).hexdigest() if data else None


def is_banner(item: Media | None) -> bool:
    """``item`` is the default banner (by content hash), not a picture of the owner."""
    sha = banner_sha256()
    return item is not None and sha is not None and item.sha256 == sha


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _text_of(block: Any) -> str:
    if isinstance(block, Mapping):
        return str(block.get("text") or "")
    if isinstance(block, str):
        return block
    return str(getattr(block, "text", "") or "")


def banner_mode(body: Mapping[str, Any] | None, preview_ok: bool) -> Literal["attach", "preview"] | None:
    """Media mode for the banner on a screen with ``body`` (``{lang: {text, …} | TextBlock | str}``):
    ``attach`` when every text fits a caption, else ``preview`` with ``PUBLIC_URL``, else ``None``."""
    if all(_utf16_len(_text_of(block)) <= CAPTION_LIMIT for block in (body or {}).values()):
        return "attach"
    return "preview" if preview_ok else None


async def banner_id(conn: AsyncConnection) -> int | None:
    """Id of the banner's ``media`` row (``None``: not installed)."""
    sha = banner_sha256()
    if sha is None:
        return None
    found = (await conn.execute(sa.select(media.c.id).where(media.c.sha256 == sha))).scalar_one_or_none()
    return None if found is None else int(found)


async def banner_active(conn: AsyncConnection, media_id: int) -> bool:
    """New screens get the banner: the one-time migration has not run yet, or some screen still shows it."""
    migrated = (
        await conn.execute(sa.select(config_meta.c.key).where(config_meta.c.key == META_KEY))
    ).first() is not None
    if not migrated:
        return True
    used = (
        await conn.execute(sa.select(screens.c.id).where(screens.c.media_id == media_id).limit(1))
    ).first()
    return used is not None


async def active_banner_id(conn: AsyncConnection) -> int | None:
    """The banner's media id when new screens should get it (installed and :func:`banner_active`)."""
    mid = await banner_id(conn)
    if mid is None or not await banner_active(conn, mid):
        return None
    return mid


async def ensure_banner_media(db: Database, root: Path) -> int | None:
    """Install the banner (file in ``root`` + ``media`` row) if needed; returns its media id.

    Fast path: the row exists and its file is on disk — one SELECT. Otherwise the asset goes through
    :class:`MediaLibrary` with the default limits (the owner's smaller limits must not re-encode it: a
    different hash would no longer be recognised as the banner). Cached ``file_id`` values are kept.
    """
    data, sha = asset_bytes(), banner_sha256()
    if data is None or sha is None:
        return None
    async with db.read() as conn:
        row = (await conn.execute(sa.select(media.c.id, media.c.path).where(media.c.sha256 == sha))).first()
    if row is not None:
        path = resolve_media_path(root, row.path)
        if path is not None and path.is_file():
            return int(row.id)
    stored = await MediaLibrary(db, root).add(data, "photo")
    if stored.media.sha256 != sha:  # pragma: no cover - the asset is a clean JPEG (tested)
        log.error("default banner was re-encoded on store; it would not be recognised — skipped")
        return None
    log.info("default banner installed as media %s", stored.media.id)
    return stored.media.id
