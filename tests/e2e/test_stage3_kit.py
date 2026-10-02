"""Stage 3–4 end-to-end kit (helpers only, no tests).

* :class:`FakeTelegramX` — the fake Bot API plus what the constructor and broadcasts need: ``getFile`` and
  ``/file/bot<token>/<path>`` downloads, ``getCustomEmojiStickers``, raw user messages (photos, forwarded
  messages with Premium emoji / spoilers, stickers) stored like Telegram stores them;
* the ``tg`` fixture (import it into a test module to override the conftest one);
* :class:`Chat` — a :class:`~tests.e2e.test_stage2_kit.Person` that also sends messages and follows the
  newest bot message of the chat (screens sent with ``new=True``: editors, captures).
"""

from __future__ import annotations

import io
import itertools
import json
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any

import asyncpg
import pytest
from aiohttp import ClientSession, ClientTimeout, web

from tests.e2e.test_stage2_kit import Person, Shop, until
from tests.fakes.telegram import FakeTelegram, _ApiError, _Bot, _chat_json, _user_json
from tests.pgcluster import PgCluster

__all__ = [
    "EMOJI",
    "Chat",
    "FakeTelegramX",
    "chat",
    "create_database",
    "drop_database",
    "jpeg_bytes",
    "tg",
]

EMOJI = "5368324170671202286"


def jpeg_bytes(size: tuple[int, int] = (64, 48), color: tuple[int, int, int] = (200, 30, 30)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG", quality=80)
    return buf.getvalue()


class FakeTelegramX(FakeTelegram):
    """:class:`FakeTelegram` + files, custom emoji stickers and raw user messages."""

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.files: dict[str, bytes] = {}
        self.custom_emoji: set[str] = {EMOJI}
        self._raw_ids = itertools.count(1)

    async def start(self) -> None:
        app = web.Application(client_max_size=50 * 1024 * 1024)
        app.router.add_route("GET", "/file/bot{token}/{path:.*}", self._download)
        app.router.add_route("*", "/bot{token}/{method}", self._handle)
        self._runner = web.AppRunner(app, handler_cancellation=True, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        server = site._server
        assert server is not None
        self._port = server.sockets[0].getsockname()[1]  # type: ignore[attr-defined]
        self._client = ClientSession(timeout=ClientTimeout(total=5))

    async def _download(self, request: web.Request) -> web.StreamResponse:
        if request.match_info["token"] not in self._tokens:
            return web.Response(status=401)
        data = self.files.get(request.match_info["path"].rsplit("/", 1)[-1])
        if data is None:
            return web.Response(status=404)
        return web.Response(body=data, content_type="application/octet-stream")

    async def _m_getfile(self, bot: _Bot, params: Mapping[str, Any]) -> Any:
        file_id = str(params.get("file_id"))
        if file_id not in self.files:
            raise _ApiError(400, "Bad Request: invalid file_id")
        return {
            "file_id": file_id,
            "file_unique_id": f"u-{file_id}",
            "file_size": len(self.files[file_id]),
            "file_path": f"photos/{file_id}",
        }

    async def _m_getcustomemojistickers(self, bot: _Bot, params: Mapping[str, Any]) -> Any:
        ids = params.get("custom_emoji_ids") or []
        if isinstance(ids, str):
            ids = json.loads(ids)
        return [
            {
                "file_id": f"s{e}",
                "file_unique_id": f"us{e}",
                "type": "custom_emoji",
                "width": 100,
                "height": 100,
                "is_animated": False,
                "is_video": False,
                "custom_emoji_id": e,
                "emoji": "🔥",
            }
            for e in ids
            if e in self.custom_emoji
        ]

    def push_raw(self, user_id: int, **fields: Any) -> dict[str, Any]:
        """A user message with arbitrary fields (``photo``, ``entities``, ``forward_origin`` …), kept so
        that ``copyMessage`` of it works like in Telegram."""
        bot = self._bot(None)
        msg: dict[str, Any] = {
            "message_id": bot.next_message_id(user_id),
            "date": int(time.time()),
            "chat": _chat_json(user_id),
            "from": _user_json(user_id),
            **fields,
        }
        bot.messages[(user_id, msg["message_id"])] = msg
        return self.push_update({"message": msg}, bot_id=bot.bot_id)

    def push_photo_file(self, user_id: int, data: bytes, *, caption: str | None = None) -> str:
        file_id = f"PH{next(self._raw_ids)}"
        self.files[file_id] = data
        photo = [
            {
                "file_id": file_id,
                "file_unique_id": f"u-{file_id}",
                "width": 64,
                "height": 48,
                "file_size": len(data),
            }
        ]
        extra: dict[str, Any] = {"caption": caption} if caption is not None else {}
        self.push_raw(user_id, photo=photo, **extra)
        return file_id

    def bot_messages(self, chat_id: int) -> list[dict[str, Any]]:
        """Current messages of the bot in a chat, oldest first."""
        bot = self._bot(None)
        return [
            m
            for (c, _mid), m in sorted(bot.messages.items(), key=lambda kv: kv[0][1])
            if c == chat_id and m.get("from", {}).get("is_bot")
        ]


@pytest.fixture
async def tg() -> AsyncIterator[FakeTelegramX]:
    async with FakeTelegramX(webhook_retry_delay=0.05) as fake:
        yield fake


@dataclass
class Chat(Person):
    """A person who also writes messages; :meth:`follow` moves ``main`` to the newest bot message."""

    def seen(self) -> str:
        """Text (or caption) and the button labels of the newest bot message."""
        self.follow()
        return " | ".join([self.text(), *(str(b.get("text", "")) for b in self.buttons())])

    def follow(self) -> int:
        msgs = self.tg.bot_messages(self.telegram_id)  # type: ignore[attr-defined]
        assert msgs, "the bot has not written yet"
        self.main = int(msgs[-1]["message_id"])
        return self.main

    async def _after(self, begin: int, expect: str | None, timeout: float) -> None:
        await until(lambda: self.last_sent(begin), timeout, f"a bot reply to {self.telegram_id}")
        if expect is not None:
            await until(
                lambda: expect in self.seen(),
                timeout,
                f"{expect!r} in the newest message (now: {self.seen()[:400]!r})",
            )
        self.follow()

    async def say(
        self, text: str, *, entities: list[dict[str, Any]] | None = None, expect: str | None = None
    ) -> None:
        begin = len(self.tg.calls)
        self.tg.push_message(self.telegram_id, text, entities=entities)
        await self._after(begin, expect, 15.0)

    async def send_raw(self, *, expect: str | None = None, **fields: Any) -> None:
        begin = len(self.tg.calls)
        self.tg.push_raw(self.telegram_id, **fields)  # type: ignore[attr-defined]
        await self._after(begin, expect, 15.0)

    async def send_photo(self, data: bytes, *, expect: str | None = None) -> str:
        begin = len(self.tg.calls)
        file_id = self.tg.push_photo_file(self.telegram_id, data)  # type: ignore[attr-defined]
        await self._after(begin, expect, 15.0)
        return file_id

    async def tap(self, label: str, *, expect: str | None = None, timeout: float = 15.0) -> str:
        """Press ``label`` on the newest bot message, then follow to the newest one again."""
        self.follow()
        toast = await self.press(label, timeout=timeout)
        if expect is not None:
            await until(
                lambda: expect in self.seen() or expect in toast,
                timeout,
                f"{expect!r} after {label!r} (now: {self.seen()[:400]!r}, toast {toast!r})",
            )
        self.follow()
        return toast


def chat(shop: Shop, telegram_id: int) -> Chat:
    return Chat(shop.tg, telegram_id)


async def create_database(pg_cluster: PgCluster, name: str) -> str:
    """An empty database on the test server (another host's database); returns its DSN."""
    admin = await asyncpg.connect(pg_cluster.dsn("postgres"))
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()
    return pg_cluster.dsn(name)


async def drop_database(pg_cluster: PgCluster, name: str) -> None:
    admin = await asyncpg.connect(pg_cluster.dsn("postgres"))
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        await admin.close()
