"""``GET /m/<token>`` — public media for the link-preview screen mode (07 §2.4.1, §2.3).

Telegram fetches the picture of a ``link_preview_options(url=…/m/<token>)`` message from here. The route:

* answers only tokens of the current content snapshot (:class:`svbg.content.media.PublicMedia`): HMAC ids,
  not sequential numbers, so files cannot be enumerated; there is no index or listing, and every miss is the
  same plain ``404`` (unknown token, a document, a file missing on disk, a wrong extension);
* needs no SQL (the snapshot is in memory) and streams the file with ``Range``/conditional request support;
* sends long-lived cache headers: the token is derived from the content hash, so the bytes behind a URL
  never change (``immutable``); the type comes from the sniffed ``mime``, never from the file name;
* the token is a path parameter named ``token``, so the access log masks it.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

from aiohttp import web

if TYPE_CHECKING:
    from svbg.content.media import PublicMedia

__all__ = ["CACHE_CONTROL", "PATH", "media_routes"]

PATH: Final = "/m/{token}"
CACHE_CONTROL: Final = "public, max-age=31536000, immutable"
_TOKEN_RE: Final = re.compile(r"^([A-Za-z0-9_-]{16,64})(?:\.([a-z0-9]{2,5}))?$")
_EXT_ALIASES: Final = {"jpeg": "jpg"}


def _not_found() -> web.Response:
    return web.Response(status=404, text="not found", content_type="text/plain")


def media_routes(public: PublicMedia) -> list[web.RouteDef]:
    """Routes to pass to ``build_web_app``."""

    async def handle(request: web.Request) -> web.StreamResponse:
        match = _TOKEN_RE.match(request.match_info.get("token", ""))
        if match is None:
            return _not_found()
        token, ext = match.group(1), match.group(2)
        found = public.resolve(token)
        if found is None:
            return _not_found()
        item, path = found
        if ext is not None:
            expected = public.path(item).rpartition(".")[2]
            if _EXT_ALIASES.get(ext, ext) != expected:
                return _not_found()
        headers = {
            "Content-Type": item.mime or "application/octet-stream",
            "Cache-Control": CACHE_CONTROL,
            "Content-Disposition": "inline",
            "Cross-Origin-Resource-Policy": "cross-origin",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        }
        return web.FileResponse(path, chunk_size=256 * 1024, headers=headers)

    return [web.get(PATH, handle, allow_head=True)]
