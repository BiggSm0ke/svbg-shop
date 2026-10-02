from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import time
from collections.abc import AsyncIterator
from pathlib import Path
from types import MappingProxyType

import pytest
from aiohttp import ClientSession
from PIL import Image

from svbg.content.media import PublicMedia, write_media_file
from svbg.content.model import Media
from svbg.content.store import ContentSnapshot
from svbg.web import WebServer, build_web_app
from svbg.web.routes.media import CACHE_CONTROL, media_routes

KEY = b"m" * 32


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (200, 10, 10)).save(buf, "JPEG")
    return buf.getvalue()


class _Source:
    def __init__(self, items: dict[int, Media]) -> None:
        self.snapshot = ContentSnapshot(
            1, MappingProxyType({}), MappingProxyType({}), MappingProxyType(items)
        )


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[tuple[WebServer, PublicMedia, dict[str, Media], bytes]]:
    data = _jpeg()
    sha = hashlib.sha256(data).hexdigest()
    rel = write_media_file(tmp_path, sha, "jpg", data)
    items = {
        "photo": Media(1, "photo", sha, path=rel, mime="image/jpeg", size=len(data)),
        "doc": Media(2, "document", "d" * 64, path=rel, mime="application/pdf"),
        "gone": Media(3, "animation", "e" * 64, path="ee/nope.gif", mime="image/gif"),
    }
    public = PublicMedia(_Source({m.id: m for m in items.values()}), tmp_path, KEY)
    srv = WebServer("127.0.0.1", 0, build_web_app(media_routes(public)))
    await srv.start()
    try:
        yield srv, public, items, data
    finally:
        await srv.stop()


async def test_serves_photo_with_cache_headers(
    env: tuple[WebServer, PublicMedia, dict[str, Media], bytes],
) -> None:
    srv, public, items, data = env
    async with ClientSession() as s:
        for path in (public.path(items["photo"]), f"/m/{public.token(items['photo'])}"):
            async with s.get(srv.url + path) as r:
                assert r.status == 200
                assert await r.read() == data
                assert r.headers["Content-Type"] == "image/jpeg"
                assert r.headers["Cache-Control"] == CACHE_CONTROL
                assert r.headers["Cross-Origin-Resource-Policy"] == "cross-origin"
                assert r.headers["X-Content-Type-Options"] == "nosniff"
                assert "ETag" in r.headers
        async with s.head(srv.url + public.path(items["photo"])) as r:
            assert r.status == 200
            assert int(r.headers["Content-Length"]) == len(data)


async def test_conditional_and_range_requests(
    env: tuple[WebServer, PublicMedia, dict[str, Media], bytes],
) -> None:
    srv, public, items, data = env
    url = srv.url + public.path(items["photo"])
    async with ClientSession() as s:
        async with s.get(url) as r:
            etag = r.headers["ETag"]
        async with s.get(url, headers={"If-None-Match": etag}) as r:
            assert r.status == 304
        async with s.get(url, headers={"Range": "bytes=0-9"}) as r:
            assert r.status == 206
            assert await r.read() == data[:10]


@pytest.mark.parametrize(
    "path",
    [
        "/m/1",  # sequential ids are not accepted
        "/m/AAAAAAAAAAAAAAAAAAAAAA",  # well-formed, unknown
        "/m/..%2F..%2Fetc%2Fpasswd",
        "/m/",
        "/m",
    ],
)
async def test_unknown_paths_are_plain_404(
    env: tuple[WebServer, PublicMedia, dict[str, Media], bytes], path: str
) -> None:
    srv, _public, _items, _data = env
    async with ClientSession() as s, s.get(srv.url + path) as r:
        assert r.status == 404
        assert "jpeg" not in (await r.text()).lower()


async def test_documents_missing_files_and_wrong_ext_are_404(
    env: tuple[WebServer, PublicMedia, dict[str, Media], bytes],
) -> None:
    srv, public, items, _data = env
    token = public.token(items["photo"])
    async with ClientSession() as s:
        for path in (
            f"/m/{public.token(items['doc'])}",
            public.path(items["gone"]),
            f"/m/{token}.png",
            f"/m/{token}.jpg.exe",
        ):
            async with s.get(srv.url + path) as r:
                assert r.status == 404, path
        async with s.get(f"{srv.url}/m/{token}.jpeg") as r:
            assert r.status == 200
        async with s.post(srv.url + public.path(items["photo"])) as r:
            assert r.status == 405


async def test_token_is_masked_in_access_log(
    env: tuple[WebServer, PublicMedia, dict[str, Media], bytes], caplog: pytest.LogCaptureFixture
) -> None:
    srv, public, items, _data = env
    token = public.token(items["photo"])
    with caplog.at_level(logging.INFO, logger="svbg.web.access"):
        async with ClientSession() as s, s.get(srv.url + public.path(items["photo"])) as r:
            assert r.status == 200
            await r.read()

        def access_lines() -> list[str]:
            return [r.getMessage() for r in caplog.records if r.name == "svbg.web.access"]

        # the access log is written after the response is sent: wait for it, with a deadline
        deadline = time.monotonic() + 2.0
        while not access_lines() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
    lines = access_lines()
    assert lines, "no access log line"
    assert any("/m/***" in line for line in lines)
    assert all(token not in line for line in lines)
