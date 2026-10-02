"""Webhook signature and envelope (02 §5.1–5.2, §8.2 A «HMAC», C «Ротация секрета»)."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import pytest

from svbg.remnawave.webhooks import (
    MAX_BODY,
    WebhookParseError,
    body_hash,
    parse_envelope,
    sign,
    strip_secrets,
    timestamp_acceptable,
    verify_signature,
)
from tests.fakes.remnawave import FakeRemnawave

SECRET = "A" * 32 + "b1C2d3E4f5G6h7I8j9K0l1M2n3O4p5Q6"
OLD = "Z9" * 32


def test_known_vector_matches_node_create_hmac() -> None:
    # crypto.createHmac('sha256', 'key').update('The quick brown fox jumps over the lazy dog').digest('hex')
    raw = b"The quick brown fox jumps over the lazy dog"
    expected = "f7bc83f430538424b13298e6aa6fb143ef4d59a14946175997479dbc2d1a3cd8"
    assert sign(raw, "key") == expected
    assert verify_signature(raw, expected, ["key"])


def test_correct_wrong_and_empty_signatures() -> None:
    raw = b'{"scope":"user","event":"user.created"}'
    good = sign(raw, SECRET)
    assert verify_signature(raw, good, [SECRET])
    assert verify_signature(raw, f"  {good.upper()} ", [SECRET])  # header normalization
    assert not verify_signature(raw, sign(raw, "other-secret"), [SECRET])
    assert not verify_signature(raw + b" ", good, [SECRET])  # one byte changed
    assert not verify_signature(raw, None, [SECRET])
    assert not verify_signature(raw, "", [SECRET])
    assert not verify_signature(raw, "   ", [SECRET])
    assert not verify_signature(raw, good[:-1], [SECRET])  # wrong length
    assert not verify_signature(raw, "g" * 64, [SECRET])  # not hex
    assert not verify_signature(raw, good, [])
    assert not verify_signature(raw, good, ["", None])
    # An empty secret must never "match" an HMAC made with an empty key.
    assert not verify_signature(raw, sign(raw, ""), [""])


def test_rotation_accepts_current_and_previous() -> None:
    raw = b'{"x":1}'
    assert verify_signature(raw, sign(raw, OLD), [SECRET, OLD])
    assert verify_signature(raw, sign(raw, SECRET), [SECRET, OLD])
    # after the rotation window only the new one
    assert not verify_signature(raw, sign(raw, OLD), [SECRET])


def test_cyrillic_and_emoji_body_raw_bytes() -> None:
    panel = FakeRemnawave()
    data = {"id": 5, "username": "sv_5", "description": "Привет 👋 — «тест»  ", "telegramId": 5}
    raw, headers = panel.build_webhook("user", "user.modified", data, secret=SECRET)
    assert "Привет 👋".encode() in raw  # sent as UTF-8, not \u escapes
    assert verify_signature(raw, headers["X-Remnawave-Signature"], [SECRET])
    # Regression: re-serializing with json.dumps (ASCII escapes, spaces) breaks the signature.
    reserialized = json.dumps(json.loads(raw)).encode()
    assert reserialized != raw
    assert not verify_signature(reserialized, headers["X-Remnawave-Signature"], [SECRET])
    env = parse_envelope(raw)
    assert env.data["description"] == data["description"]
    assert headers["X-Remnawave-Timestamp"] == env.timestamp.strftime("%Y-%m-%dT%H:%M:%S.") + (
        f"{env.timestamp.microsecond // 1000:03d}Z"
    )


def test_signature_check_is_hmac_compare_digest_for_every_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[bytes, bytes]] = []
    real = hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(hmac, "compare_digest", spy)
    raw = b"{}"
    assert verify_signature(raw, sign(raw, SECRET), [SECRET, OLD, "third"])
    assert len(calls) == 3  # no early exit: timing does not reveal which secret matched


def test_parse_envelope_tolerant_and_strips_secrets() -> None:
    payload = {
        "scope": "user",
        "event": "user.some_future_event",
        "timestamp": "2026-09-30T10:00:00.000Z",
        "data": {
            "id": 42,
            "username": "sv_42",
            "trojanPassword": "tp",
            "ssPassword": "sp",
            "vlessUuid": "vu",
            "userTraffic": {"usedTrafficBytes": 1},
        },
        "meta": None,
        "somethingNew": True,
    }
    env = parse_envelope(json.dumps(payload).encode())
    assert env.scope == "user" and env.event == "user.some_future_event"
    assert env.timestamp == datetime(2026, 9, 30, 10, tzinfo=UTC)
    assert env.meta is None
    assert env.panel_user_id == 42
    assert "trojanPassword" not in env.data and "ssPassword" not in env.data and "vlessUuid" not in env.data
    assert env.data["userTraffic"] == {"usedTrafficBytes": 1}


def test_parse_envelope_meta_and_nested_user_ids() -> None:
    hwid = {
        "scope": "user_hwid_devices",
        "event": "user_hwid_devices.added",
        "timestamp": "2026-09-30T10:00:00+03:00",
        "data": {"user": {"id": 9, "vlessUuid": "x"}, "hwidUserDevice": {"hwid": "h"}},
    }
    env = parse_envelope(json.dumps(hwid).encode())
    assert env.panel_user_id == 9
    assert env.data["user"] == {"id": 9}
    assert env.meta is None
    login = {
        "scope": "service",
        "event": "service.login_attempt_failed",
        "timestamp": "2026-09-30T10:00:00Z",
        "data": {"loginAttempt": {"username": "admin", "password": "hunter2", "ip": "1.2.3.4"}},
    }
    env2 = parse_envelope(json.dumps(login).encode())
    assert "password" not in env2.data["loginAttempt"]
    assert env2.panel_user_id is None
    expiring = {
        "scope": "user",
        "event": "user.expiration",
        "timestamp": "2026-09-30T10:00:00Z",
        "data": {"id": "not-an-int"},
        "meta": {"expiration": -24},
    }
    env3 = parse_envelope(json.dumps(expiring).encode())
    assert env3.meta == {"expiration": -24}
    assert env3.panel_user_id is None


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"[]",
        b'{"scope":"user","event":"user.created"}',
        b'{"scope":"user","timestamp":"2026-09-30T10:00:00Z"}',
        b'{"scope":"","event":"x","timestamp":"2026-09-30T10:00:00Z"}',
        b'{"scope":"user","event":"x","timestamp":"yesterday"}',
        b'{"scope":"user","event":"x","timestamp":"2026-09-30T10:00:00"}',
    ],
)
def test_parse_envelope_rejects_garbage(raw: bytes) -> None:
    with pytest.raises(WebhookParseError):
        parse_envelope(raw)


def test_parse_envelope_size_limit() -> None:
    with pytest.raises(WebhookParseError):
        parse_envelope(b" " * (MAX_BODY + 1))


def test_timestamp_window() -> None:
    at = datetime(2026, 10, 1, 12, tzinfo=UTC)
    assert timestamp_acceptable(at, at)
    assert timestamp_acceptable(at - timedelta(days=6, hours=23), at)  # late BullMQ delivery is fine
    assert not timestamp_acceptable(at - timedelta(days=7, seconds=1), at)
    assert timestamp_acceptable(at + timedelta(minutes=4), at)
    assert not timestamp_acceptable(at + timedelta(minutes=6), at)


def test_body_hash_and_strip_secrets() -> None:
    assert body_hash(b"abc") == hashlib.sha256(b"abc").hexdigest()
    nested = {"a": [{"ssPassword": 1, "keep": 2}], "password": 3, "b": {"vlessUuid": 4}}
    assert strip_secrets(nested) == {"a": [{"keep": 2}], "b": {}}


async def test_fake_panel_sends_signed_webhooks_to_a_receiver() -> None:
    from aiohttp import web

    received: list[tuple[bytes, str | None]] = []

    async def handler(request: web.Request) -> web.Response:
        received.append((await request.read(), request.headers.get("X-Remnawave-Signature")))
        ok = verify_signature(received[-1][0], received[-1][1], [SECRET])
        return web.Response(status=200 if ok else 401)

    app = web.Application()
    app.router.add_post("/webhooks/remnawave", handler)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    try:
        panel = FakeRemnawave()
        url = f"http://127.0.0.1:{port}/webhooks/remnawave"
        assert await panel.send_webhook(url, "user", "user.created", {"id": 1}, secret=SECRET) == 200
        assert await panel.send_webhook(url, "user", "user.created", {"id": 1}, secret=OLD) == 401
    finally:
        await runner.cleanup()
    assert len(received) == 2
