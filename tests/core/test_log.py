from __future__ import annotations

import io
import json
import logging
import queue
import sys
import threading
import time
from collections.abc import Iterator

import pytest

from svbg.core import log
from svbg.core.log import (
    MASK,
    MAX_SECRET_LEN,
    MAX_SECRETS,
    RingBuffer,
    SecretMaskingFilter,
    SecretRegistry,
    mask,
)

TG_TOKEN = "1234567890:AAH9xT_k3yQ-Zp7wVb2nM4cR8sLdE6fGhJk"
SECRET = "Sup3r$ecret/Value+="


@pytest.fixture(autouse=True)
def _isolate() -> Iterator[None]:
    root = logging.getLogger()
    level = root.level
    SecretRegistry.clear()  # also resets the eviction counter and warning rate limit
    yield
    log.shutdown_logging()
    SecretRegistry.clear()
    root.setLevel(level)


# ---------------------------------------------------------------- registry


def test_register_and_mask() -> None:
    assert SecretRegistry.register(SECRET)
    assert mask(f"value={SECRET}!").count(SECRET) == 0
    assert mask(f"got {SECRET} here") == "got *** here"
    assert SecretRegistry.contains(SECRET)
    assert SecretRegistry.count() == 1


def test_registry_is_process_global_via_instances() -> None:
    SecretRegistry().register("instance-secret")
    assert mask("x instance-secret y") == "x *** y"
    assert log.register_secret("another-secret")
    assert "another-secret" not in mask("another-secret")


@pytest.mark.parametrize("value", [None, "", "short", 12345678])
def test_register_ignores_short_or_non_str(value: object) -> None:
    assert not SecretRegistry.register(value)  # type: ignore[arg-type]
    assert SecretRegistry.count() == 0
    assert mask("short text") == "short text"


def test_encoded_variants_are_masked() -> None:
    SecretRegistry.register("pa ss/wo'rd\\x")
    assert "wo" not in mask("url: https://h/?p=pa%20ss%2Fwo%27rd%5Cx")
    assert "wo" not in mask("form: p=pa+ss%2Fwo%27rd%5Cx")
    assert "wo" not in mask(repr({"p": "pa ss/wo'rd\\x"}))
    assert "wo" not in mask(json.dumps({"p": "pa ss/wo'rd\\x"}))


def test_dsn_password_is_registered_too() -> None:
    SecretRegistry.register("postgresql://svbg:veryS3cretPw@db:5432/svbg")
    assert mask("auth failed for password veryS3cretPw") == "auth failed for password ***"


def test_longest_secret_wins() -> None:
    SecretRegistry.register("abcdef")
    SecretRegistry.register("abcdefghij")
    assert mask("abcdefghij") == MASK


def test_unregister_and_clear() -> None:
    SecretRegistry.register("removable-1")
    SecretRegistry.register("removable-2")
    SecretRegistry.unregister("removable-1")
    SecretRegistry.unregister(None)
    assert mask("removable-1 removable-2") == "removable-1 ***"
    SecretRegistry.clear()
    assert mask("removable-2") == "removable-2"


def test_registry_thread_safety() -> None:
    per_thread = MAX_SECRETS // 4

    def work(i: int) -> None:
        for j in range(per_thread):
            SecretRegistry.register(f"secret-{i}-{j:04d}")
            mask("noise secret-0-0000 noise")

    threads = [threading.Thread(target=work, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert SecretRegistry.count() == 4 * per_thread
    assert mask(f"secret-3-{per_thread - 1:04d}") == MASK


def test_registry_thread_safety_under_eviction() -> None:
    def work(i: int) -> None:
        for j in range(MAX_SECRETS):
            SecretRegistry.register(f"flood-{i}-{j:05d}")
            mask("noise flood-0-00000 noise")

    threads = [threading.Thread(target=work, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert SecretRegistry.count() == MAX_SECRETS
    assert SecretRegistry.evicted() == 3 * MAX_SECRETS
    # the forms tuple matches the surviving values exactly (no torn rebuild)
    assert SecretRegistry.forms_count() == MAX_SECRETS  # these values have a single textual form


def test_register_many() -> None:
    assert SecretRegistry.register_many(["first-secret", None, "tiny", "second-secret"]) == 2
    assert mask("first-secret / second-secret / tiny") == "*** / *** / tiny"


def test_registry_mask_is_fast_with_many_secrets() -> None:
    SecretRegistry.register_many(f"secret-value-{i:05d}-xyz" for i in range(200))
    line = "ordinary log line with user 12345 and some context " * 20
    started = time.perf_counter()
    for _ in range(1000):
        mask(line)
    assert time.perf_counter() - started < 2.0


# ---------------------------------------------------------------- registry limits


def _awkward(i: int) -> str:
    """A secret with the most textual forms (URL/JSON/repr escapes all differ)."""
    return f"s3cr/t+'{i:05d} \\x=&\u00fc\u043a"


def test_too_long_secret_is_ignored() -> None:
    assert not SecretRegistry.register("x" * (MAX_SECRET_LEN + 1))
    assert SecretRegistry.register_many(["y" * (MAX_SECRET_LEN + 1), "z" * MAX_SECRET_LEN]) == 1
    assert SecretRegistry.count() == 1
    assert mask("z" * MAX_SECRET_LEN) == MASK


def test_registry_is_bounded_and_evicts_the_oldest(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="svbg.log")
    for i in range(MAX_SECRETS + 10):
        assert SecretRegistry.register(f"bounded-secret-{i:05d}")
    assert SecretRegistry.count() == MAX_SECRETS
    assert SecretRegistry.evicted() == 10
    assert mask("bounded-secret-00000") == "bounded-secret-00000"  # the oldest went first
    assert mask("bounded-secret-00009") == "bounded-secret-00009"
    assert mask("bounded-secret-00010") == MASK
    assert mask(f"bounded-secret-{MAX_SECRETS + 9:05d}") == MASK
    # one warning (rate-limited), in Russian, without any secret value
    warnings = [r for r in caplog.records if r.name == "svbg.log"]
    assert len(warnings) == 1
    text = warnings[0].getMessage()
    assert "Реестр секретов переполнен" in text and str(MAX_SECRETS) in text
    assert "bounded-secret" not in text


def test_re_registering_refreshes_a_secret() -> None:
    SecretRegistry.register("keep-me-alive")
    for i in range(MAX_SECRETS - 1):
        SecretRegistry.register(f"filler-secret-{i:05d}")
    assert SecretRegistry.register("keep-me-alive")  # refresh: moves to the newest end, no rebuild
    SecretRegistry.register("one-more-secret")  # evicts filler 0, not the refreshed value
    assert SecretRegistry.count() == MAX_SECRETS
    assert mask("keep-me-alive") == MASK
    assert mask("filler-secret-00000") == "filler-secret-00000"
    assert mask("filler-secret-00001") == MASK


def test_register_many_over_capacity_keeps_the_newest() -> None:
    values = [f"many-secret-{i:05d}" for i in range(MAX_SECRETS * 2)]
    assert SecretRegistry.register_many(values) == len(values)
    assert SecretRegistry.count() == MAX_SECRETS
    assert mask(values[MAX_SECRETS - 1]) == values[MAX_SECRETS - 1]
    assert mask(values[MAX_SECRETS]) == MASK and mask(values[-1]) == MASK
    assert SecretRegistry.register_many(values[-5:]) == 5  # known values: refreshed only
    assert SecretRegistry.evicted() == MAX_SECRETS


def test_mask_stays_fast_with_a_full_registry_of_awkward_secrets() -> None:
    """The bound is what keeps ``mask`` cheap: even a flood leaves at most MAX_SECRETS x 8 forms."""
    SecretRegistry.register_many(_awkward(i) for i in range(MAX_SECRETS * 4))
    assert SecretRegistry.count() == MAX_SECRETS
    assert SecretRegistry.forms_count() <= MAX_SECRETS * 8
    assert mask(f"x {_awkward(MAX_SECRETS * 4 - 1)} y") == f"x {MASK} y"
    line = "ordinary log line with user 12345 and some context " * 20  # ~1 KB
    started = time.perf_counter()
    for _ in range(1000):
        mask(line)
    assert time.perf_counter() - started < 1.5  # ~0.2 s on a laptop; generous for CI


def test_mask_is_lock_free_during_registration() -> None:
    """``mask`` reads one immutable tuple: a registration holding the lock never blocks it."""
    SecretRegistry.register("visible-secret")
    with SecretRegistry._lock:
        assert mask("a visible-secret b") == f"a {MASK} b"


# ---------------------------------------------------------------- shapes


@pytest.mark.parametrize(
    ("text", "leak"),
    [
        (f"token {TG_TOKEN} failed", TG_TOKEN),
        (f"GET https://api.telegram.org/bot{TG_TOKEN}/getMe", TG_TOKEN),
        ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig-_x", "eyJhbGci"),
        ("header bearer abc.def~ghi/jkl+==", "abc.def"),
        ("postgresql://svbg:p4ssw0rd@db:5432/svbg", "p4ssw0rd"),
        ("postgresql+asyncpg://svbg:p4ssw0rd@db/svbg", "p4ssw0rd"),
        ("socks5://user:pr0xy-pass@1.2.3.4:1080", "pr0xy-pass"),
        ("redis://:onlypass@redis:6379/0", "onlypass"),
        ("BOT_TOKEN=whatever-value-here", "whatever-value-here"),
        ("REMNAWAVE_TOKEN: abc123def456", "abc123def456"),
        ("https://pay.example/api?api_key=k3y-v4lue&x=1", "k3y-v4lue"),
        ('{"password": "hunter22", "user": "bob"}', "hunter22"),
        ("{'client_secret': 'zzz-yyy'}", "zzz-yyy"),
        ("Settings(webhook_secret='wh-s3cret')", "wh-s3cret"),
        ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
        ("{'Authorization': 'Token abc-def'}", "abc-def"),
        ("Cookie: session=abc; csrftoken=def", "session=abc"),
        ("X-Api-Key: live_key_123", "live_key_123"),
    ],
)
def test_mask_known_shapes(text: str, leak: str) -> None:
    masked = mask(text)
    assert leak not in masked
    assert MASK in masked
    assert mask(masked) == masked  # idempotent


def test_mask_keeps_useful_context() -> None:
    assert mask("postgresql://svbg:p4ssw0rd@db:5432/svbg") == "postgresql://svbg:***@db:5432/svbg"
    assert mask("BOT_TOKEN=abc123xyz") == "BOT_TOKEN=***"
    assert mask(f"bot{TG_TOKEN}/getMe") == "bot***/getMe"


@pytest.mark.parametrize(
    "text",
    [
        "TRIAL_DAYS=3",
        "user 1234567 bought plan 30d for 179 ₽",
        "https://example.com:8443/path?x=1",
        "    token = load_token(path)",  # source line in a traceback stays readable
        "plain message without secrets",
        "",
    ],
)
def test_mask_leaves_ordinary_text(text: str) -> None:
    assert mask(text) == text


def test_mask_non_str() -> None:
    assert mask(12345) == "12345"  # type: ignore[arg-type]


def test_mask_is_fast_on_large_input() -> None:
    blob = ("A" * 5000 + " ") * 200 + "x" * 100_000 + "token" * 20_000
    started = time.perf_counter()
    mask(blob)
    assert time.perf_counter() - started < 2.0


def test_redact() -> None:
    assert log.redact("1234567890abcdefXYZW") == "••••••••XYZW"
    assert log.redact("short") == "••••••••"
    assert log.redact("1234567890abcdefXYZW", keep_last=0) == "••••••••"
    assert log.redact("") == ""
    assert log.redact(None) == ""


# ---------------------------------------------------------------- filter


def _record(msg: str, *args: object, exc: bool = False) -> logging.LogRecord:
    exc_info = None
    if exc:
        try:
            raise RuntimeError(f"cannot connect with {TG_TOKEN}")
        except RuntimeError:
            exc_info = sys.exc_info()
    return logging.LogRecord("svbg.test", logging.ERROR, __file__, 1, msg, args or None, exc_info)


def test_filter_masks_message_args_and_traceback() -> None:
    SecretRegistry.register(SECRET)
    rec = _record("connect %s with %s", "db", SECRET, exc=True)
    assert SecretMaskingFilter().filter(rec) is True
    assert rec.getMessage() == "connect db with ***"
    assert rec.args is None
    assert rec.exc_text is not None
    assert "RuntimeError" in rec.exc_text
    assert TG_TOKEN not in rec.exc_text
    formatted = logging.Formatter().format(rec)
    assert TG_TOKEN not in formatted and SECRET not in formatted


def test_filter_handles_bad_args() -> None:
    rec = _record("value %d %d", "not-int", SECRET)
    SecretRegistry.register(SECRET)
    SecretMaskingFilter().filter(rec)
    assert "bad log args" in rec.getMessage()
    assert SECRET not in rec.getMessage()


def test_filter_masks_stack_info() -> None:
    rec = _record("x")
    rec.stack_info = f"Stack: BOT_TOKEN={TG_TOKEN}"
    SecretMaskingFilter().filter(rec)
    assert TG_TOKEN not in rec.stack_info


def test_mask_record_is_idempotent() -> None:
    rec = _record("hello %s", "world")
    log.mask_record(rec)
    rec.msg = "changed"  # second pass must not re-format anything
    log.mask_record(rec)
    assert rec.msg == "changed"


# ---------------------------------------------------------------- ring buffer


def test_ring_buffer_capacity_and_queries() -> None:
    ring = RingBuffer(capacity=3)
    logger = logging.getLogger("svbg.test.ring")
    logger.addHandler(ring)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    try:
        for i in range(5):
            logger.info("message %d", i)
        logger.warning("warn with %s", TG_TOKEN)
    finally:
        logger.removeHandler(ring)
        logger.propagate = True
    assert ring.capacity == 3
    assert len(ring) == 3
    texts = ring.tail(10)
    assert texts[0].endswith("message 3")
    assert texts[-1].endswith("warn with ***")
    assert [r.level for r in ring.records(min_level=logging.WARNING)] == [logging.WARNING]
    assert len(ring.records(limit=1)) == 1
    assert ring.records(limit=0) == []
    ring.clear()
    assert ring.tail() == []


def test_ring_buffer_rejects_bad_capacity() -> None:
    with pytest.raises(ValueError):
        RingBuffer(capacity=0)


# ---------------------------------------------------------------- setup


def _our_handlers() -> list[logging.Handler]:
    return [h for h in logging.getLogger().handlers if isinstance(h, log._QueueHandler | RingBuffer)]


def test_setup_logging_text_queue_masks_and_fills_ring() -> None:
    stream = io.StringIO()
    ring = log.setup_logging("info", stream=stream)
    SecretRegistry.register(SECRET)
    logger = logging.getLogger("svbg.test.setup")
    logger.debug("hidden debug")
    logger.info("hello %s", SECRET)
    try:
        raise ValueError(f"DATABASE_URL=postgresql://u:pw123456@h/db token {TG_TOKEN}")
    except ValueError:
        logger.exception("failed")
    log.flush_logging()

    out = stream.getvalue()
    assert "hello ***" in out
    assert "hidden debug" not in out
    assert "Traceback" in out and "ValueError" in out
    for leak in (SECRET, TG_TOKEN, "pw123456"):
        assert leak not in out
    assert any("hello ***" in t for t in ring.tail())
    assert "INFO" in out and "svbg.test.setup" in out


def test_setup_logging_json() -> None:
    stream = io.StringIO()
    log.setup_logging("DEBUG", json=True, stream=stream, use_queue=False)
    logger = logging.getLogger("svbg.test.json")
    try:
        raise KeyError(TG_TOKEN)
    except KeyError:
        logger.exception("boom %s", "here", extra={"user_id": 42, "detail": f"Bearer {SECRET}"})
    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert len(lines) == 1
    entry = lines[0]
    assert entry["msg"] == "boom here"
    assert entry["level"] == "ERROR"
    assert entry["logger"] == "svbg.test.json"
    assert entry["user_id"] == 42
    assert SECRET not in entry["detail"]
    assert "KeyError" in entry["exc"] and TG_TOKEN not in entry["exc"]
    assert entry["ts"].endswith("+00:00")


def test_setup_logging_is_idempotent_and_shutdown_removes_handlers() -> None:
    ring1 = log.setup_logging("INFO", stream=io.StringIO())
    logging.getLogger("svbg.test").info("before reconfigure")
    log.flush_logging()
    ring2 = log.setup_logging("WARNING", stream=io.StringIO())
    assert ring1 is ring2  # history survives reconfiguration
    assert any("before reconfigure" in t for t in ring2.tail())
    assert len(_our_handlers()) == 1
    assert logging.getLogger().level == logging.WARNING
    log.shutdown_logging()
    assert _our_handlers() == []
    log.flush_logging()  # no-op without setup


def test_ring_capacity_change_keeps_recent_history() -> None:
    log.setup_logging("INFO", stream=io.StringIO(), use_queue=False)
    for i in range(5):
        logging.getLogger("svbg.test").info("line %d", i)
    ring = log.setup_logging("INFO", stream=io.StringIO(), use_queue=False, ring_capacity=2)
    assert ring.capacity == 2
    assert [t[-6:] for t in ring.tail()] == ["line 3", "line 4"]
    log.setup_logging("INFO", stream=io.StringIO(), use_queue=False, ring_capacity=2000)


def test_set_level_and_parse_level() -> None:
    log.set_level("debug")
    assert logging.getLogger().level == logging.DEBUG
    assert logging.getLogger("aiogram.event").level == logging.DEBUG
    log.set_level("info")
    assert logging.getLogger("aiogram.event").level == logging.WARNING
    assert log.parse_level("WARN") == logging.WARNING
    assert log.parse_level(" error ") == logging.ERROR
    assert log.parse_level("15") == 15
    assert log.parse_level(30) == 30
    for bad in ("loud", "", True, None):
        with pytest.raises(ValueError, match="invalid log level"):
            log.parse_level(bad)  # type: ignore[arg-type]


def test_queue_full_drops_instead_of_blocking() -> None:
    q: queue.Queue[logging.LogRecord | None] = queue.Queue(1)
    handler = log._QueueHandler(q)
    handler.handle(_record("one"))
    started = time.perf_counter()
    handler.handle(_record("two"))
    assert time.perf_counter() - started < 0.5
    assert handler.dropped == 1
    prepared = q.get_nowait()
    assert prepared is not None and prepared.exc_info is None


def test_queue_handler_keeps_masked_traceback_text() -> None:
    q: queue.Queue[logging.LogRecord | None] = queue.Queue(10)
    handler = log._QueueHandler(q)
    handler.handle(_record("err", exc=True))
    prepared = q.get_nowait()
    assert prepared is not None
    assert prepared.exc_info is None
    assert prepared.exc_text and "RuntimeError" in prepared.exc_text
    assert TG_TOKEN not in prepared.exc_text
    assert log.dropped_records() == 0
