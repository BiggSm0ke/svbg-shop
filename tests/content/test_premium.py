"""Premium-emoji probe (07 §2.4.1): send → compare the echo → ok | stripped | unknown, stored, re-checked."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer

from svbg.content.premium import (
    META_KEY,
    PremiumService,
    PremiumState,
    ProbeEcho,
    evaluate,
    load_state,
    probe_message,
    reference_emoji,
    token_fingerprint,
)
from svbg.content.store import build_snapshot
from svbg.core import clock
from svbg.tg.admin.content.telegram import TransportProbeSender
from svbg.tg.ui.router import BotTransport
from tests.dbkit import CountingDatabase
from tests.fakes.telegram import FakeTelegram

EMOJI = "5368324170671202286"
OWNER = 4242


class FakeSender:
    """Echoes like Telegram; ``strip`` drops what a bot without Premium would lose."""

    def __init__(self, *, strip: str | None = None, fail: bool = False) -> None:
        self.strip = strip
        self.fail = fail
        self.sent: list[tuple[int, str, list[dict[str, Any]], str]] = []
        self.deleted: list[tuple[int, int]] = []

    async def send(self, chat_id: int, text: str, entities: list[dict[str, Any]], emoji_id: str) -> ProbeEcho:
        if self.fail:
            raise OSError("network down")
        self.sent.append((chat_id, text, entities, emoji_id))
        ents = [] if self.strip in ("entity", "both") else entities
        button: dict[str, Any] = {"text": "проверка", "callback_data": "x"}
        if self.strip not in ("icon", "both"):
            button["icon_custom_emoji_id"] = emoji_id
        return ProbeEcho(77, ents, {"inline_keyboard": [[button]]})

    async def delete(self, chat_id: int, message_id: int) -> None:
        self.deleted.append((chat_id, message_id))


class BotRef:
    def __init__(self, bot_id: int | None = 1, token: str | None = "1:AAA") -> None:
        self.bot_id = bot_id
        self.token = token

    def __call__(self) -> tuple[int | None, str | None]:
        return self.bot_id, self.token


@pytest.fixture
def frozen() -> Iterator[clock.FrozenClock]:
    fc = clock.FrozenClock()
    clock.set_clock(fc)
    try:
        yield fc
    finally:
        clock.reset_clock()


def service(
    db: CountingDatabase, sender: FakeSender | None, bot: BotRef | None = None, **kw: Any
) -> PremiumService:
    async def owners() -> list[int]:
        return [OWNER, 1]

    return PremiumService(db, lambda: sender, bot=bot or BotRef(), owners=owners, **kw)


def test_evaluate_needs_both_entity_and_icon() -> None:
    text, entities = probe_message(EMOJI)
    assert text.endswith("⭐")
    assert entities[0]["custom_emoji_id"] == EMOJI and entities[0]["length"] == 1
    icon = {"inline_keyboard": [[{"text": "x", "icon_custom_emoji_id": EMOJI}]]}
    assert evaluate(ProbeEcho(1, entities, icon), EMOJI) == "ok"
    assert evaluate(ProbeEcho(1, [], icon), EMOJI) == "stripped"
    assert evaluate(ProbeEcho(1, entities, {"inline_keyboard": [[{"text": "x"}]]}), EMOJI) == "stripped"
    other = [{**entities[0], "custom_emoji_id": "1"}]
    assert evaluate(ProbeEcho(1, other, icon), EMOJI) == "stripped"


async def test_probe_ok_is_stored_and_the_message_deleted(db: CountingDatabase) -> None:
    sender = FakeSender()
    svc = service(db, sender)
    state = await svc.probe(OWNER, EMOJI)
    assert state.status == "ok" and state.emoji_id == EMOJI
    assert sender.deleted == [(OWNER, 77)]
    rows = await db.raw("select value from config_meta where key = $1", META_KEY)
    assert rows[0]["value"]["status"] == "ok"
    assert rows[0]["value"]["token_fp"] == token_fingerprint("1:AAA")
    assert "1:AAA" not in str(rows[0]["value"])  # never the token itself
    assert (await load_state(db)).status == "ok"
    assert "работают" in state.label and state.warning is None


@pytest.mark.parametrize("strip", ["entity", "icon", "both"])
async def test_probe_stripped_warns_honestly(db: CountingDatabase, strip: str) -> None:
    svc = service(db, FakeSender(strip=strip))
    state = await svc.probe(OWNER, EMOJI)
    assert state.status == "stripped"
    assert state.warning is not None and "Premium" in state.warning
    assert svc.state.status == "stripped"


async def test_probe_unknown_on_failure_or_without_emoji(db: CountingDatabase) -> None:
    state = await service(db, FakeSender(fail=True)).probe(OWNER, EMOJI)
    assert state.status == "unknown" and "OSError" in (state.detail or "")
    state = await service(db, FakeSender()).probe(OWNER)
    assert state.status == "unknown" and "иконку" in (state.detail or "")
    state = await service(db, None).probe(OWNER, EMOJI)
    assert state.status == "unknown"


async def test_recheck_daily_and_on_token_change(db: CountingDatabase, frozen: clock.FrozenClock) -> None:
    sender = FakeSender()
    bot = BotRef()
    svc = service(db, sender, bot, reference=lambda: EMOJI)
    assert svc.due()  # never checked
    first = await svc.tick()
    assert first is not None and first.status == "ok"
    assert sender.sent[0][0] == OWNER  # the first owner gets the probe
    assert not svc.due()
    assert await svc.tick() is None
    frozen.advance(hours=23)
    assert not svc.due()
    frozen.advance(hours=1)
    assert svc.due()
    await svc.tick()
    assert len(sender.sent) == 2
    bot.token = "1:BBB"  # token rotated
    assert svc.due()
    bot.token, bot.bot_id = "2:CCC", 2  # another bot
    assert svc.due()
    # a fresh process reads the stored state
    other = service(db, sender, BotRef(2, "2:CCC"), reference=lambda: EMOJI)
    await other.load()
    assert other.due()


def test_state_json_round_trip_and_garbage() -> None:
    st = PremiumState("stripped", clock.now(), 5, "abc", EMOJI, "x")
    assert PremiumState.from_json(st.to_json()) == st
    assert PremiumState.from_json({"status": "weird", "checked_at": "nope"}).status == "unknown"
    assert PremiumState.from_json(None) == PremiumState()


def test_reference_emoji_from_content() -> None:
    screens = [
        {
            "id": 1,
            "code": "home",
            "kind": "system",
            "body": {
                "ru": {
                    "text": "🔥 x",
                    "entities": [
                        {"type": "custom_emoji", "offset": 0, "length": 2, "custom_emoji_id": "111"}
                    ],
                }
            },
        }
    ]
    snap = build_snapshot(screens, [], [], version=1)
    assert reference_emoji(snap) == "111"
    buttons = [
        {"id": 1, "screen_id": 1, "label": {"ru": "a"}, "action": "copy:x", "icon_custom_emoji_id": "222"}
    ]
    snap = build_snapshot(screens, buttons, [], version=2)
    assert reference_emoji(snap) == "222"
    assert reference_emoji(build_snapshot([], [], [], version=3)) is None


@pytest.fixture
async def fake_tg() -> AsyncIterator[FakeTelegram]:
    async with FakeTelegram() as fake:
        yield fake


async def test_probe_over_http_against_the_fake_bot_api(db: CountingDatabase, fake_tg: FakeTelegram) -> None:
    token = fake_tg.add_bot(username="svbg_bot")
    bot = Bot(token, session=AiohttpSession(api=TelegramAPIServer.from_base(fake_tg.url)))
    try:
        transport = BotTransport(SimpleNamespace(get=lambda: bot, me=None))
        sender = TransportProbeSender(transport)
        svc = PremiumService(db, lambda: sender, bot=lambda: (bot.id, token))
        state = await svc.probe(OWNER, EMOJI)
        assert state.status == "ok"  # the fake echoes entities and icon_custom_emoji_id like Premium does
        sent = fake_tg.calls_for("sendMessage")[-1]
        assert sent.params["entities"][0]["custom_emoji_id"] == EMOJI
        assert sent.params["reply_markup"]["inline_keyboard"][0][0]["icon_custom_emoji_id"] == EMOJI
        assert fake_tg.calls_for("deleteMessage")
    finally:
        await bot.session.close()


async def test_a_failed_probe_keeps_the_known_result_and_retries_in_an_hour(
    db: CountingDatabase, frozen: clock.FrozenClock
) -> None:
    sender = FakeSender()
    svc = service(db, sender, reference=lambda: EMOJI)
    ok = await svc.probe(OWNER, EMOJI)
    assert ok.status == "ok" and ok.checked_at is not None
    frozen.advance(days=1)
    sender.fail = True  # Telegram blinked
    failed = await svc.probe(OWNER, EMOJI)
    assert failed.status == "ok" and failed.checked_at == ok.checked_at  # nothing known is lost
    assert failed.failed_last and "OSError" in (failed.detail or "")
    assert (await load_state(db)).status == "ok"
    assert "работают" in failed.label
    frozen.advance(minutes=59)
    assert not svc.due()  # no hammering …
    frozen.advance(minutes=1)
    assert svc.due()  # … but not a day of «не проверены» either
    sender.fail = False
    again = await svc.tick()
    assert again is not None and again.status == "ok" and not again.failed_last and again.detail is None
    assert not svc.due()


async def test_tick_tries_every_owner_until_a_send_works(db: CountingDatabase) -> None:
    class Blocked(FakeSender):
        async def send(
            self, chat_id: int, text: str, entities: list[dict[str, Any]], emoji_id: str
        ) -> ProbeEcho:
            if chat_id == OWNER:  # this owner never started the bot: 403
                raise PermissionError("Forbidden: bot can't initiate conversation")
            return await super().send(chat_id, text, entities, emoji_id)

    sender = Blocked()
    svc = service(db, sender, reference=lambda: EMOJI)
    state = await svc.tick()
    assert state is not None and state.status == "ok"
    assert [s[0] for s in sender.sent] == [1]  # the second owner got it


async def test_a_failure_for_another_bot_does_not_reuse_the_old_result(db: CountingDatabase) -> None:
    sender = FakeSender()
    bot = BotRef()
    svc = service(db, sender, bot)
    assert (await svc.probe(OWNER, EMOJI)).status == "ok"
    bot.bot_id, bot.token = 2, "2:BBB"
    sender.fail = True
    state = await svc.probe(OWNER, EMOJI)
    assert state.status == "unknown" and state.checked_at is None and state.bot_id == 2
    assert not svc.due()  # retried in an hour, not on every tick
