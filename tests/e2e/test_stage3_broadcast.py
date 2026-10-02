"""Stage 3 end to end — a broadcast of a forwarded message (07 §2.4.5, stage3-contracts «Acceptance»).

The owner forwards a channel post with a Premium emoji and a spoiler to ``/broadcast`` → the bot copies it to
every recipient with all entities → the process restarts mid-run and the broadcast goes on from its cursor →
«⏹ Остановить» stops it within 5 s; nobody gets it twice beyond one batch.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from typing import Any

import pytest

from tests.e2e.conftest import OWNER_ID, AppEnv, StartApp
from tests.e2e.test_stage2_kit import FAST, open_shop, until
from tests.e2e.test_stage3_kit import EMOJI, chat, tg  # noqa: F401 - the ``tg`` fixture

pytestmark = pytest.mark.pg

RECIPIENTS = 400
BASE = 8_100_000
TEXT = "🔥 Новость: скидки до мая"
ENTITIES = [
    {"type": "custom_emoji", "offset": 0, "length": 2, "custom_emoji_id": EMOJI},
    {"type": "bold", "offset": 3, "length": 7},
    {"type": "spoiler", "offset": 12, "length": 12},
]


def copies(fake: Any, start: int = 0) -> list[Any]:
    return [
        c
        for c in fake.calls[start:]
        if c.ok and c.method == "copyMessage" and BASE < int(c.params.get("chat_id", 0)) <= BASE + RECIPIENTS
    ]


async def test_forwarded_broadcast_keeps_entities_survives_restart_and_stops_fast(
    start_app: StartApp, app_env: AppEnv
) -> None:
    async with open_shop(start_app, app_env, admin_chat=False) as shop:
        await shop.db.raw(
            "insert into users (telegram_id, first_name, language)"
            " select $1::bigint + g, 'U' || g, 'ru' from generate_series(1, $2::int) g",
            BASE,
            RECIPIENTS,
        )
        shop.tg.method_latency["copyMessage"] = 0.03  # a slow Telegram: the run lasts long enough to restart
        owner = chat(shop, OWNER_ID)
        await owner.start()
        await owner.say("/broadcast", expect="Рассылки")
        await owner.tap("➕ Новая рассылка", expect="ерешлите")
        origin = {
            "type": "channel",
            "chat": {"id": -1_001_234, "type": "channel", "title": "Канал"},
            "message_id": 5,
            "date": int(time.time()),
        }
        await owner.send_raw(text=TEXT, entities=ENTITIES, forward_origin=origin, expect="🚀 Отправить")
        await owner.tap("🚀 Отправить", expect="Да, отправить")
        await owner.tap("Да, отправить")

        await until(lambda: len(copies(shop.tg)) >= 30, timeout=30, what="the first recipients got it")
        await shop.app.stop()  # restart in the middle of the run
        before = len(copies(shop.tg))
        assert before < RECIPIENTS
        second = await start_app(**FAST)
        mark = len(shop.tg.calls)
        await until(
            lambda: len(copies(shop.tg, mark)) >= 30, timeout=30, what="the run goes on after the restart"
        )

        # ⏹ «Остановить» on the progress message of the new process (or the card)
        stop_on = next(
            m
            for m in reversed(shop.tg.bot_messages(OWNER_ID))
            if any(
                "Остановить" in str(b.get("text"))
                for row in (m.get("reply_markup") or {}).get("inline_keyboard", [])
                for b in row
            )
        )
        owner.main = int(stop_on["message_id"])
        pressed = time.monotonic()
        await owner.press("Остановить")
        while True:  # quiet for a second = stopped
            n = len(copies(shop.tg))
            await asyncio.sleep(1.0)
            if len(copies(shop.tg)) == n:
                break
            assert time.monotonic() - pressed < 10, "the broadcast does not stop"
        sent = copies(shop.tg)
        last = sent[-1].ts
        assert last - pressed <= 5.0, f"the broadcast went on {last - pressed:.1f}s after «Остановить»"
        db: Any = second.db
        rows = await db.raw("select status, sent from broadcasts")
        assert [r["status"] for r in rows] == ["canceled"]  # «Остановить» = canceled
        per_chat = Counter(int(c.params["chat_id"]) for c in sent)
        assert len(per_chat) < RECIPIENTS  # stopped before the end
        assert sum(n - 1 for n in per_chat.values()) <= 50  # at most one batch twice (the crash)

        # every copy is the admin's original with all its formatting
        for c in sent[:5] + sent[-5:]:
            msg = shop.tg.message(int(c.params["chat_id"]), int(c.result["message_id"]))
            assert msg is not None and msg["text"] == TEXT
            assert msg["entities"] == ENTITIES
            assert int(c.params["from_chat_id"]) == OWNER_ID
