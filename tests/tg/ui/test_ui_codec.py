from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from svbg.core.clock import FrozenClock, reset_clock, set_clock
from svbg.tg.ui import codec
from svbg.tg.ui.codec import CallbackCodec, CallbackTooLongError, Decoded, decode, encode, fits
from tests.dbkit import CountingDatabase, open_db


@pytest.fixture
async def db(pg_dsn: str) -> AsyncIterator[CountingDatabase]:
    async with open_db(pg_dsn) as database:
        yield database


@pytest.fixture
def clock() -> FrozenClock:
    c = FrozenClock(datetime(2026, 10, 1, 12, 0, tzinfo=UTC))
    set_clock(c)
    yield c
    reset_clock()


def test_roundtrip() -> None:
    assert encode("home") == "v1:home:o"
    assert encode("buy", "period", "3") == "v1:buy:period:3"
    assert decode("v1:buy:period:3") == Decoded("buy", "period", "3")
    assert decode("v1:home:o") == Decoded("home", "o")
    # the arg is the last field and may contain ':'
    data = encode("dev", "rm", "12:abc:3")
    assert decode(data) == Decoded("dev", "rm", "12:abc:3")
    # unicode args count in bytes
    data = encode("s", "a", "привет")
    assert decode(data) == Decoded("s", "a", "привет")
    assert decode("v1:s:a:") == Decoded("s", "a")


def test_limit_is_64_bytes() -> None:
    prefix = "v1:s:a:"
    ok = "x" * (64 - len(prefix))
    assert len(encode("s", "a", ok).encode()) == 64
    with pytest.raises(CallbackTooLongError):
        encode("s", "a", ok + "x")
    # Cyrillic letters are 2 bytes each
    assert not fits("s", "a", "я" * 29)
    assert fits("s", "a", "я" * 28)
    with pytest.raises(CallbackTooLongError):
        encode("s", "a", "~reserved")  # token marker is reserved


@pytest.mark.parametrize(
    "bad",
    [
        None,
        123,
        "",
        "home",
        "v2:home:o",
        "v1:home",
        "v1::o",
        "v1:home:",
        "v1:ho me:o",
        "v1:a:b:\x00",
        "x" * 65,
        "v1:s:a:" + "я" * 29,
        "v1:s:a:~short",
        "v1:s:a:~bad*token!",
        "s:a:b",
    ],
)
def test_decode_rejects_garbage_and_old_formats(bad: object) -> None:
    assert decode(bad) is None


@pytest.mark.parametrize(("screen", "action"), [("bad name", "o"), ("s", "a:b"), ("", "o"), ("s" * 33, "o")])
def test_encode_validates_names(screen: str, action: str) -> None:
    with pytest.raises(ValueError):
        encode(screen, action)


def test_encode_rejects_control_chars_and_non_str() -> None:
    with pytest.raises(ValueError, match="control"):
        encode("s", "a", "line\nbreak")
    with pytest.raises(TypeError):
        encode("s", "a", 5)  # type: ignore[arg-type]


async def test_long_args_use_short_tokens(db: CountingDatabase, clock: FrozenClock) -> None:
    c = CallbackCodec(db, key=b"k" * 32)
    long_arg = "order:" + "x" * 100
    data = await c.encode_long("pay", "check", long_arg)
    assert len(data.encode()) <= 64
    decoded = decode(data)
    assert decoded is not None and decoded.token is not None and decoded.arg is None
    resolved = await c.resolve(decoded)
    assert resolved == Decoded("pay", "check", long_arg)
    # structured args are fine too
    data2 = await c.encode_long("pay", "check", {"ids": list(range(30))})
    r2 = await c.resolve(decode(data2))  # type: ignore[arg-type]
    assert r2 is not None and r2.arg == {"ids": list(range(30))}
    # short args stay inline without SQL
    before = db.queries
    assert await c.encode_long("pay", "check", "7") == "v1:pay:check:7"
    assert db.queries == before


async def test_same_arg_reuses_token_and_skips_sql(db: CountingDatabase, clock: FrozenClock) -> None:
    c = CallbackCodec(db, key=b"k" * 32)
    arg = "y" * 80
    first = await c.encode_long("s", "a", arg)
    q = db.queries
    assert await c.encode_long("s", "a", arg) == first
    assert db.queries == q  # cached: no write on every render
    rows = await db.raw("select count(*) as n from short_tokens")
    assert rows[0]["n"] == 1
    # a restarted process with the same key produces the same token and resolves old buttons
    c2 = CallbackCodec(db, key=b"k" * 32)
    assert await c2.encode_long("s", "a", arg) == first
    assert (await c2.resolve(decode(first))) is not None  # type: ignore[arg-type]


async def test_token_is_bound_to_screen_and_action(db: CountingDatabase, clock: FrozenClock) -> None:
    c = CallbackCodec(db)
    data = await c.encode_long("admin", "refund", "z" * 80)
    token = data.rsplit("~", 1)[1]
    forged = decode(f"v1:home:refund:~{token}")
    assert forged is not None
    assert await c.resolve(forged) is None
    fresh = CallbackCodec(db)  # no cache: goes to the database
    assert await fresh.resolve(forged) is None
    assert await fresh.resolve(decode(data)) is not None  # type: ignore[arg-type]


async def test_expired_and_unknown_tokens(db: CountingDatabase, clock: FrozenClock) -> None:
    c = CallbackCodec(db, ttl=timedelta(days=1))
    data = await c.encode_long("s", "a", "q" * 80)
    unknown = decode("v1:s:a:~AAAAAAAAAAAA")
    assert unknown is not None and await c.resolve(unknown) is None
    clock.advance(timedelta(days=2))
    assert await c.resolve(decode(data)) is None  # type: ignore[arg-type]
    assert await CallbackCodec(db).resolve(decode(data)) is None  # type: ignore[arg-type]
    assert await c.purge_expired() == 1
    rows = await db.raw("select count(*) as n from short_tokens")
    assert rows[0]["n"] == 0


async def test_encode_long_validation(db: CountingDatabase) -> None:
    c = CallbackCodec(db)
    with pytest.raises(TypeError):
        await c.encode_long("s", "a", {1, 2})
    with pytest.raises(ValueError, match="too large"):
        await c.encode_long("s", "a", "x" * 10_000)
    with pytest.raises(CallbackTooLongError):
        await c.encode_long("s" * 32, "a" * 32, "x" * 100)
    with pytest.raises(ValueError):
        CallbackCodec(db, key=b"short")
    with pytest.raises(ValueError):
        CallbackCodec(db, ttl=timedelta(0))


async def test_resolve_passthrough_without_token(db: CountingDatabase) -> None:
    c = CallbackCodec(db)
    d = Decoded("s", "a", "1")
    assert await c.resolve(d) is d
    assert codec.ACTION_OPEN == "o"
