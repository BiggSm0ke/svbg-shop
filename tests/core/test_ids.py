from __future__ import annotations

import re
import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from svbg.core import ids

UUID7_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def test_uuid7_format_version_and_variant() -> None:
    value = ids.uuid7()
    assert UUID7_RE.match(value)
    parsed = uuid.UUID(value)
    assert parsed.version == 7
    assert parsed.variant == uuid.RFC_4122
    assert str(parsed) == value
    assert ids.is_uuid7(value)


def test_uuid7_timestamp_is_current() -> None:
    before = datetime.now(UTC) - timedelta(seconds=1)
    value = ids.uuid7()
    after = datetime.now(UTC) + timedelta(seconds=1)
    assert before <= ids.uuid7_time(value) <= after


def test_uuid7_strictly_increasing_in_a_burst() -> None:
    values = [ids.uuid7() for _ in range(20_000)]
    assert values == sorted(values)
    assert len(set(values)) == len(values)


def test_uuid7_monotonic_when_clock_goes_backwards(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ids, "_last_ms", -1)  # restored after the test
    times = iter([2_000_000_000_000_000_000, 1_000_000_000_000_000_000, 1_000_000_000_000_000_000])
    monkeypatch.setattr(ids.time, "time_ns", lambda: next(times))
    a, b, c = ids.uuid7(), ids.uuid7(), ids.uuid7()
    assert a < b < c
    # The timestamp did not jump back.
    assert ids.uuid7_time(b) == ids.uuid7_time(a)


def test_uuid7_counter_overflow_bumps_timestamp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ids, "_last_ms", -1)
    monkeypatch.setattr(ids.time, "time_ns", lambda: 1_700_000_000_000_000_000)
    first = ids.uuid7()
    monkeypatch.setattr(ids, "_last_rand", ids._RAND_MASK)
    second = ids.uuid7()
    assert second > first
    assert ids.uuid7_time(second) - ids.uuid7_time(first) == timedelta(milliseconds=1)


def test_uuid7_unique_across_threads() -> None:
    results: list[list[str]] = [[] for _ in range(8)]

    def work(i: int) -> None:
        results[i].extend(ids.uuid7() for _ in range(2000))

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    flat = [v for r in results for v in r]
    assert len(set(flat)) == len(flat)
    for r in results:
        assert r == sorted(r)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not-a-uuid",
        "4b04647f-12db-4900-bada-981cceba6449",  # a valid UUIDv4, not v7
        "0190F0B5-3C7A-7ABC-8DEF-0123456789AB",  # uppercase is not canonical
        "0190f0b53c7a7abc8def0123456789ab",
        "0190f0b5-3c7a-7abc-cdef-0123456789ab",  # wrong variant
        "0190f0b5-3c7a-7abc-8def-0123456789ag",
        None,
        42,
    ],
)
def test_is_uuid7_rejects(value: object) -> None:
    assert not ids.is_uuid7(value)


def test_uuid7_time_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="UUIDv7"):
        ids.uuid7_time("nope")


def test_short_token_length_and_alphabet() -> None:
    for n in (1, 2, 10, 11, 32, 512):
        token = ids.short_token(n)
        assert len(token) == n
        assert re.fullmatch(r"[A-Za-z0-9_-]+", token)
    assert len(ids.short_token()) == 10


def test_short_token_is_random() -> None:
    tokens = {ids.short_token(10) for _ in range(1000)}
    assert len(tokens) == 1000


@pytest.mark.parametrize("n", [0, -1, 513, True, 2.5, "10"])
def test_short_token_rejects_bad_length(n: object) -> None:
    with pytest.raises(ValueError):
        ids.short_token(n)  # type: ignore[arg-type]
