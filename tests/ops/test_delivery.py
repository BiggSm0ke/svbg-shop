"""TelegramDelivery against the fake Bot API: topic «💾 Бэкапы», owners' DMs, 429 / 5xx retries."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramBadRequest

from svbg.ops.backup import BackupError, TelegramDelivery
from tests.fakes.telegram import FakeTelegram

GROUP = -1001234567890


@dataclass
class Topic:
    enabled: bool = True
    thread: int | None = 77

    def thread_in(self, chat_id: int) -> int | None:
        return self.thread if chat_id == GROUP else None


@dataclass
class FakeAdminChat:
    configured: bool = True
    chat_id: int | None = GROUP
    topics: dict[str, Topic] = field(default_factory=lambda: {"backups": Topic(), "system": Topic(thread=5)})
    ensured: int = 0

    def state(self, kind: str) -> Topic:
        return self.topics[kind]

    async def ensure_topics(self) -> None:
        self.ensured += 1
        self.topics["backups"].thread = 99


@pytest.fixture
async def tg() -> AsyncIterator[FakeTelegram]:
    async with FakeTelegram() as fake:
        yield fake


@pytest.fixture
async def bot(tg: FakeTelegram) -> AsyncIterator[Bot]:
    b = Bot(tg.add_bot(), session=AiohttpSession(api=TelegramAPIServer.from_base(tg.url)))
    try:
        yield b
    finally:
        await b.session.close()


@pytest.fixture
def files(tmp_path: Path) -> list[Path]:
    out = []
    for i in (1, 2):
        p = tmp_path / f"svbg-20261002-040000-daily.svbg.part{i}of2"
        p.write_bytes(b"x" * (1000 + i))
        out.append(p)
    return out


async def _no_owners() -> frozenset[int]:
    return frozenset()


async def _owners() -> frozenset[int]:
    return frozenset({1001, 1002})


async def _nap(_s: float) -> None:
    return None


async def test_documents_go_to_the_backups_topic(tg: FakeTelegram, bot: Bot, files: list[Path]) -> None:
    d = TelegramDelivery(lambda: bot, admin_chat=FakeAdminChat(), owners=_owners)
    assert await d.send(files, ["c1", "c2"]) == 1
    calls = tg.calls_for("sendDocument")
    assert [c.params["chat_id"] for c in calls] == [GROUP, GROUP]
    assert [c.params["message_thread_id"] for c in calls] == [77, 77]
    uploads = [next(v for v in c.params.values() if isinstance(v, dict) and "upload" in v) for c in calls]
    assert [u["upload"] for u in uploads] == [f.name for f in files]
    assert [u["size"] for u in uploads] == [1001, 1002]
    assert [c.params["caption"] for c in calls] == ["c1", "c2"]
    assert all(c.params["disable_notification"] for c in calls)


async def test_missing_thread_is_created_and_disabled_topic_falls_back(
    tg: FakeTelegram, bot: Bot, files: list[Path]
) -> None:
    chat = FakeAdminChat()
    chat.topics["backups"].thread = None
    d = TelegramDelivery(lambda: bot, admin_chat=chat, owners=_owners)
    assert await d.targets() == [(GROUP, 99)] and chat.ensured == 1
    chat.topics["backups"].enabled = False
    assert await d.targets() == [(GROUP, 5)]


async def test_owners_dms_without_a_group(tg: FakeTelegram, bot: Bot, files: list[Path]) -> None:
    d = TelegramDelivery(
        lambda: bot, admin_chat=FakeAdminChat(configured=False, chat_id=None), owners=_owners
    )
    assert await d.send(files[:1], ["c"]) == 2
    assert sorted(c.params["chat_id"] for c in tg.calls_for("sendDocument")) == [1001, 1002]
    assert all("message_thread_id" not in c.params for c in tg.calls_for("sendDocument"))
    with pytest.raises(BackupError, match="некуда"):
        await TelegramDelivery(lambda: bot, admin_chat=None, owners=_no_owners).send(files, ["a", "b"])
    with pytest.raises(BackupError, match="бот не запущен"):
        await TelegramDelivery(lambda: None, admin_chat=None, owners=_owners).send(files, ["a", "b"])


async def test_flood_and_server_errors_are_retried(tg: FakeTelegram, bot: Bot, files: list[Path]) -> None:
    waits: list[float] = []

    async def sleep(s: float) -> None:
        waits.append(s)

    tg.fail_next("429", method="sendDocument", retry_after=3)
    tg.fail_next("502", method="sendDocument")
    d = TelegramDelivery(lambda: bot, admin_chat=FakeAdminChat(), owners=_owners, sleep=sleep)
    assert await d.send(files[:1], ["c"]) == 1
    assert waits[0] == 3.0
    assert len([c for c in tg.calls_for("sendDocument") if c.ok]) == 1


async def test_bad_request_is_not_retried(tg: FakeTelegram, bot: Bot, files: list[Path]) -> None:
    tg.fail_next("400", method="sendDocument", description="Bad Request: file is too big")
    d = TelegramDelivery(lambda: bot, admin_chat=FakeAdminChat(), owners=_owners, sleep=_nap)
    with pytest.raises(TelegramBadRequest):
        await d.send(files[:1], ["c"])
    assert len(tg.calls_for("sendDocument")) == 1
