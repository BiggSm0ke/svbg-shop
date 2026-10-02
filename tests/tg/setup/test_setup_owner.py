"""One-time owner link: creation, single use, expiry, rate limit, welcome screen (real PostgreSQL)."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import Any

import pytest
from aiogram.methods import SendMessage

from svbg.core.clock import FrozenClock, reset_clock, set_clock
from svbg.tg.setup.owner import (
    DEEP_LINK_PREFIX,
    META_KEY,
    SCREEN_WELCOME,
    OwnerLinkError,
    OwnerSetup,
    code_from_payload,
    create_owner_link,
    owner_link_url,
    redeem_owner_code,
)
from svbg.tg.ui.context import UserCtx
from tests.dbkit import CountingDatabase, add_user
from tests.tg.admin.settings_harness import SEnv
from tests.tg.ui.ui_harness import text_message


@pytest.fixture(autouse=True)
def _real_clock() -> Any:
    yield
    reset_clock()


async def meta_rows(db: CountingDatabase) -> list[Any]:
    return await db.raw("select key, value::text as value from config_meta where key=$1", META_KEY)


# ---------------------------------------------------------------- link creation


async def test_link_stores_only_a_hash(db: CountingDatabase) -> None:
    link = await create_owner_link(db, bot_username="svbg_bot")
    assert link.payload == DEEP_LINK_PREFIX + link.code
    assert len(link.payload) <= 64
    assert link.url == f"https://t.me/svbg_bot?start={link.payload}"
    assert link.code not in repr(link)
    rows = await meta_rows(db)
    assert len(rows) == 1
    stored = json.loads(rows[0]["value"])
    assert set(stored) == {"hash", "created_at", "expires_at"}
    assert link.code not in rows[0]["value"]
    assert len(stored["hash"]) == 64


async def test_new_link_replaces_the_old_one(db: CountingDatabase) -> None:
    old = await create_owner_link(db)
    new = await create_owner_link(db)
    assert old.code != new.code
    assert old.url is None
    with pytest.raises(OwnerLinkError) as e:
        await redeem_owner_code(db, old.code, telegram_id=1)
    assert e.value.reason == "invalid"
    claim = await redeem_owner_code(db, new.code, telegram_id=1)
    assert claim.previous_role is None


def test_payload_and_url_parsing() -> None:
    assert code_from_payload("setup_" + "a" * 32) == "a" * 32
    for bad in (None, "", "setup_", "setup_short", "promo_" + "a" * 32, "setup_" + "a" * 40 + "!", "x"):
        assert code_from_payload(bad) is None
    assert owner_link_url("@svbg_bot", "c") == "https://t.me/svbg_bot?start=setup_c"
    assert owner_link_url("bad name", "c") is None
    assert owner_link_url(None, "c") is None


# ---------------------------------------------------------------- redeem


async def test_redeem_creates_owner_with_audit_and_is_single_use(db: CountingDatabase) -> None:
    link = await create_owner_link(db)
    claim = await redeem_owner_code(
        db, link.code, telegram_id=42, username="artem", first_name="Артём", language="ru"
    )
    user = (await db.raw("select id, role, username, first_name from users where telegram_id=42"))[0]
    assert user["role"] == "owner" and user["id"] == claim.user_id and user["first_name"] == "Артём"
    audit = await db.raw("select actor_id, role, action, target, details::text as d from admin_audit")
    assert len(audit) == 1
    assert audit[0]["action"] == "owner.claim"
    assert audit[0]["actor_id"] == claim.user_id
    assert json.loads(audit[0]["d"]) == {"telegram_id": 42, "previous_role": None}
    assert await meta_rows(db) == []
    with pytest.raises(OwnerLinkError):
        await redeem_owner_code(db, link.code, telegram_id=43)
    assert (await db.raw("select count(*) as n from users where role='owner'"))[0]["n"] == 1


async def test_existing_user_is_promoted(db: CountingDatabase) -> None:
    uid = await add_user(db, 77, "admin")
    link = await create_owner_link(db)
    claim = await redeem_owner_code(db, link.code, telegram_id=77)
    assert claim.user_id == uid and claim.previous_role == "admin"
    assert (await db.raw("select role from users where id=$1", uid))[0]["role"] == "owner"


async def test_wrong_code_does_not_consume_the_link(db: CountingDatabase) -> None:
    link = await create_owner_link(db)
    for bad in ("x" * 32, link.code[:-1] + ("A" if link.code[-1] != "A" else "B"), "short", "bad code!"):
        with pytest.raises(OwnerLinkError) as e:
            await redeem_owner_code(db, bad, telegram_id=5)
        assert e.value.reason == "invalid"
    assert len(await meta_rows(db)) == 1
    assert (await db.raw("select count(*) as n from users"))[0]["n"] == 0
    await redeem_owner_code(db, link.code, telegram_id=5)


async def test_expired_link_is_removed(db: CountingDatabase) -> None:
    clock = FrozenClock()
    set_clock(clock)
    link = await create_owner_link(db)
    clock.advance(timedelta(hours=1, seconds=1))
    with pytest.raises(OwnerLinkError) as e:
        await redeem_owner_code(db, link.code, telegram_id=5)
    assert e.value.reason == "expired"
    assert await meta_rows(db) == []
    assert (await db.raw("select count(*) as n from users"))[0]["n"] == 0


async def test_concurrent_redeem_has_one_winner(db: CountingDatabase) -> None:
    link = await create_owner_link(db)
    results = await asyncio.gather(
        *(redeem_owner_code(db, link.code, telegram_id=100 + i) for i in range(4)), return_exceptions=True
    )
    winners = [r for r in results if not isinstance(r, BaseException)]
    assert len(winners) == 1
    assert all(isinstance(r, OwnerLinkError) for r in results if r not in winners)
    assert (await db.raw("select count(*) as n from users where role='owner'"))[0]["n"] == 1


async def test_corrupted_meta_is_rejected(db: CountingDatabase) -> None:
    await db.raw(
        "insert into config_meta (key, value) values ($1, $2::jsonb)", META_KEY, json.dumps({"hash": 5})
    )
    with pytest.raises(OwnerLinkError):
        await redeem_owner_code(db, "a" * 32, telegram_id=5)


# ---------------------------------------------------------------- bot flow


def _setup(senv: SEnv, **kw: Any) -> tuple[OwnerSetup, list[int]]:
    invalidated: list[int] = []

    async def invalidate(tg_id: int) -> None:
        invalidated.append(tg_id)
        row = (await senv.db.raw("select id, role from users where telegram_id=$1", tg_id))[0]
        senv.users.by_tg[tg_id] = UserCtx(row["id"], telegram_id=tg_id, role=row["role"])

    setup = OwnerSetup(senv.db, senv.router, settings=senv.service, invalidate_user=invalidate, **kw)
    setup.install()
    setup.install()  # idempotent
    return setup, invalidated


async def test_start_with_link_makes_owner_and_greets(senv: SEnv) -> None:
    setup, invalidated = _setup(senv)
    link = await create_owner_link(senv.db, bot_username="svbg_bot")
    assert await setup.handle_start(text_message(321, f"/start {link.payload}"))
    assert invalidated == [321]
    sent = senv.transport.of(SendMessage)[-1]
    assert "владелец" in sent.text
    labels = [b.text for row in sent.reply_markup.inline_keyboard for b in row]  # type: ignore[union-attr]
    assert "⚙️ Настройки" in labels
    assert senv.service.current()["OWNER_IDS"] == [321]
    assert link.code not in senv.all_text()
    # the welcome button leads to the settings root, which the new owner may open
    await senv.click(321, sent.reply_markup.inline_keyboard[0][0].callback_data)  # type: ignore[union-attr]
    assert "Все настройки" in senv.text and "Запуск" in " ".join(senv.labels())
    # used link: a clear message, no second owner
    assert await setup.handle_start(text_message(654, f"/start {link.payload}"))
    assert "недействительна" in senv.transport.of(SendMessage)[-1].text
    assert senv.service.current()["OWNER_IDS"] == [321]


async def test_greeting_works_with_a_stale_user_cache(senv: SEnv) -> None:
    setup = OwnerSetup(senv.db, senv.router)
    setup.install()
    await senv.add(500, "user")  # cached as a plain user, nobody invalidates
    link = await create_owner_link(senv.db)
    assert await setup.handle_start(text_message(500, f"/start {link.payload}"))
    assert "владелец" in senv.transport.of(SendMessage)[-1].text
    assert (await senv.db.raw("select role from users where telegram_id=500"))[0]["role"] == "owner"


async def test_existing_owner_ids_are_kept(senv: SEnv) -> None:
    from svbg.core.settings.service import Change

    await senv.service.apply([Change("OWNER_IDS", "11,12")], source="cli", actor_id=None)
    setup, _ = _setup(senv)
    link = await create_owner_link(senv.db)
    assert await setup.handle_start(text_message(13, f"/start {link.payload}"))
    assert senv.service.current()["OWNER_IDS"] == [11, 12, 13]


async def test_not_our_start_payloads(senv: SEnv) -> None:
    setup, _ = _setup(senv)
    assert not await setup.handle_start(text_message(1, "/start"))
    assert not await setup.handle_start(text_message(1, "/start promo_abc"))
    assert not await setup.handle_start(text_message(1, "/start setup_" + "a" * 32, chat_type="group"))
    assert senv.transport.calls == []
    assert await setup.handle_start(text_message(1, "/start setup_bad!"))
    assert "недействительна" in senv.transport.of(SendMessage)[-1].text


async def test_attempts_are_rate_limited(senv: SEnv) -> None:
    setup, _ = _setup(senv, max_attempts=3)
    link = await create_owner_link(senv.db)
    for i in range(3):
        assert await setup.handle_start(text_message(9, "/start setup_" + "z" * 32, message_id=10 + i))
    assert await setup.handle_start(text_message(9, f"/start {link.payload}", message_id=20))
    assert "Слишком много попыток" in senv.transport.of(SendMessage)[-1].text
    assert len(await meta_rows(senv.db)) == 1  # the real link survived the flood
    assert await setup.handle_start(text_message(10, f"/start {link.payload}", message_id=21))
    assert "владелец" in senv.transport.of(SendMessage)[-1].text


def test_rate_limiter_memory_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    from svbg.tg.setup import owner as owner_mod

    t = [1000.0]
    monkeypatch.setattr(owner_mod.clock, "monotonic", lambda: t[0])
    monkeypatch.setattr(owner_mod, "MAX_TRACKED_USERS", 50)
    setup = OwnerSetup(Any, Any, max_attempts=2, attempt_window_s=60.0)  # type: ignore[arg-type]
    for uid in range(40):
        assert setup._allow_attempt(uid)
    t[0] += 61  # every window expired: stale entries are evicted on the next attempt
    assert setup._allow_attempt(10_000)
    assert list(setup._attempts) == [10_000]
    for uid in range(200):  # a flood of distinct fresh accounts never exceeds the hard limit
        assert setup._allow_attempt(20_000 + uid)
        assert len(setup._attempts) <= 50
    assert setup._allow_attempt(99)
    assert setup._allow_attempt(99)
    assert not setup._allow_attempt(99)  # the limit still works for the current user
    assert len(setup._attempts) <= 50


async def test_database_error_is_reported(senv: SEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    from sqlalchemy.exc import OperationalError

    import svbg.tg.setup.owner as owner_mod

    async def down(*a: Any, **kw: Any) -> Any:
        raise OperationalError("select", None, ConnectionError("down"))

    monkeypatch.setattr(owner_mod, "redeem_owner_code", down)
    setup, _ = _setup(senv)
    assert await setup.handle_start(text_message(1, "/start setup_" + "a" * 32))
    assert "база данных не отвечает" in senv.transport.of(SendMessage)[-1].text


async def test_welcome_screen_is_owner_only(senv: SEnv) -> None:
    _setup(senv)
    await senv.add(77, "admin", frozenset({"settings.business"}))
    await senv.click(77, f"v1:{SCREEN_WELCOME}:o")
    assert senv.toasts[-1] == "Нет прав"


async def test_aiogram_router(senv: SEnv) -> None:
    setup, _ = _setup(senv)
    router: Any = setup.aiogram_router()
    assert len(router.message.handlers) == 1


async def test_module_setup_entry_point(senv: SEnv) -> None:
    from types import SimpleNamespace

    from svbg.tg.setup.owner import setup

    invalidated: list[int] = []
    deps = SimpleNamespace(
        db=senv.db, settings=senv.service, users=SimpleNamespace(invalidate=invalidated.append)
    )
    router: Any = setup(senv.router, deps)
    assert len(router.message.handlers) == 1
    link = await create_owner_link(senv.db)
    owner = OwnerSetup(senv.db, senv.router, invalidate_user=deps.users.invalidate)
    assert await owner.handle_start(text_message(31, f"/start {link.payload}"))
    assert invalidated == [31]
