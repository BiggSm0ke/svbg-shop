"""QR code of an invitation link (PNG, ``segno``). Pure CPU, no I/O; results are cached per link."""

from __future__ import annotations

import io
from functools import lru_cache
from typing import Final

import segno

__all__ = ["MAX_LINK", "qr_png"]

MAX_LINK: Final = 512
_SCALE: Final = 10
_BORDER: Final = 2


@lru_cache(maxsize=512)
def _png(link: str) -> bytes:
    buf = io.BytesIO()
    segno.make(link, error="m", micro=False).save(buf, kind="png", scale=_SCALE, border=_BORDER)
    return buf.getvalue()


def qr_png(link: str) -> bytes:
    """PNG bytes of the QR code for ``link`` (``https://`` or ``tg://`` only, ≤ :data:`MAX_LINK` chars)."""
    if not isinstance(link, str) or not link.startswith(("https://", "tg://")) or len(link) > MAX_LINK:
        raise ValueError("QR-код строится только для ссылки https:// или tg:// длиной до 512 символов")
    return _png(link)
