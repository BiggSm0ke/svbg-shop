"""SettingsService on a real PostgreSQL: load priorities, the apply pipeline, rollback, undo, secrets."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from svbg.core.component import ComponentRegistry, ProbeError
from svbg.core.crypto import Crypto, generate_key
from svbg.core.settings.service import (
    DB_UNAVAILABLE,
    RESET,
    Change,
    SettingsError,
    StaleSnapshotError,
)
from svbg.core.settings.values import SECRET_PLACEHOLDER
from tests.core.settings.conftest import FakeComponent, ServiceFactory, ShimDatabase

TOKEN = "123456789:" + "A" * 35
RW_TOKEN = "rw-token-" + "x" * 30


async def rows(db: ShimDatabase) -> dict[str, tuple[Any, str]]:
    found = await db.fetch("select key, value::text as v, source from settings")
    return {r["key"]: (json.loads(r["v"]), r["source"]) for r in found}


async def audit(db: ShimDatabase) -> list[dict[str, Any]]:
    found = await db.fetch(
        "select id, batch_id, key, old::text as old, new::text as new, source, applied, error "
        "from settings_audit order by id"
    )
    return [dict(r) for r in found]


def comp(components: ComponentRegistry, name: str) -> FakeComponent:
    return components.get(name)  # type: ignore[return-value]


# ------------------------------------------------------------------------------------------------ load


async def test_load_defaults_and_registry_version(make_service: ServiceFactory, db: ShimDatabase) -> None:
    svc = await make_service()
    snap = svc.current()
    assert snap["TRIAL_DAYS"] == 3 and snap.source("TRIAL_DAYS") == "default"
    assert snap.version == 1 and await rows(db) == {}
    meta = await db.fetch("select value::text as v from config_meta where key='registry_schema_version'")
    assert json.loads(meta[0]["v"])["fingerprint"] == svc.registry.fingerprint


async def test_current_before_load_raises(make_service: ServiceFactory) -> None:
    svc = await make_service(load=False)
    with pytest.raises(RuntimeError):
        svc.current()


async def test_environ_seeds_runtime_keys_once(make_service: ServiceFactory, db: ShimDatabase) -> None:
    svc = await make_service(environ={"TRIAL_DAYS": "10", "CURRENCY": "RUB", "SUPPORT_URL": "bad url"})
    snap = svc.current()
    assert snap["TRIAL_DAYS"] == 10 and snap.source("TRIAL_DAYS") == "environ"
    assert (await rows(db))["TRIAL_DAYS"] == (10, "env_seed")
    assert "CURRENCY" not in await rows(db)  # equal to the default: not frozen
    assert "SUPPORT_URL" in svc.problems and snap["SUPPORT_URL"] is None
    # The DB is the truth afterwards: a different environment value no longer wins.
    again = await make_service(environ={"TRIAL_DAYS": "20"})
    assert again.current()["TRIAL_DAYS"] == 10
    assert again.env_overrides() == {"TRIAL_DAYS": "environ"}


async def test_bootstrap_keys_come_from_file_then_environ(
    make_service: ServiceFactory, env_path: Path
) -> None:
    env_path.parent.mkdir(parents=True)
    env_path.write_text(f"BOT_TOKEN={TOKEN}\n", encoding="utf-8")
    svc = await make_service(environ={"BOT_TOKEN": "111111:" + "Z" * 35, "OWNER_IDS": "5"})
    snap = svc.current()
    assert snap["BOT_TOKEN"] == TOKEN and snap.source("BOT_TOKEN") == "env_file"
    assert snap["OWNER_IDS"] == [5] and snap.source("OWNER_IDS") == "environ"


async def test_db_row_beats_environ_and_file_for_runtime_keys(
    make_service: ServiceFactory, env_path: Path
) -> None:
    svc = await make_service()
    await svc.apply([Change("TRIAL_DAYS", "7")], source="bot", actor_id=1)
    env_path.parent.mkdir(parents=True)
    env_path.write_text("TRIAL_DAYS=9\n", encoding="utf-8")
    again = await make_service(environ={"TRIAL_DAYS": "8"})
    assert again.current()["TRIAL_DAYS"] == 7 and again.current().source("TRIAL_DAYS") == "bot"


# ------------------------------------------------------------------------------------------------ apply


async def test_apply_hot_change_persists_audits_and_notifies(
    make_service: ServiceFactory, db: ShimDatabase
) -> None:
    svc = await make_service()
    seen: list[tuple[int, set[str]]] = []

    async def on_change(snap: Any, keys: set[str]) -> None:
        seen.append((snap["TRIAL_DAYS"], keys))

    svc.subscribe(["TRIAL_DAYS"], on_change)
    svc.subscribe(["CURRENCY"], lambda *_: pytest.fail("not subscribed to this key"))
    result = await svc.apply([Change("TRIAL_DAYS", "14")], source="bot", actor_id=42)
    assert result.ok and result.applied == {"TRIAL_DAYS": 14} and not result.restart_required
    assert svc.current()["TRIAL_DAYS"] == 14 and svc.current().version == 2
    assert svc.current().source("TRIAL_DAYS") == "bot"
    assert (await rows(db))["TRIAL_DAYS"] == (14, "bot")
    [entry] = await audit(db)
    assert entry["applied"] and entry["source"] == "bot" and entry["batch_id"] == result.batch_id
    assert json.loads(entry["new"]) == {"v": 14, "src": "bot"} and entry["old"] is None
    assert seen == [(14, {"TRIAL_DAYS"})]
    reloaded = await make_service()
    assert reloaded.current()["TRIAL_DAYS"] == 14


async def test_invalid_value_is_rejected_and_nothing_changes(
    make_service: ServiceFactory, db: ShimDatabase
) -> None:
    svc = await make_service()
    result = await svc.apply([Change("TRIAL_DAYS", "abc"), Change("NOPE", "1")], source="bot", actor_id=1)
    assert result.applied == {} and set(result.rejected) == {"TRIAL_DAYS", "NOPE"}
    assert "целое" in result.rejected["TRIAL_DAYS"]
    assert svc.current().version == 1 and await rows(db) == {}
    [entry] = await audit(db)  # unknown keys are not audited
    assert not entry["applied"] and json.loads(entry["new"]) == {"raw": "abc"}


async def test_probe_failure_rejects_before_persisting(
    make_service: ServiceFactory, components: ComponentRegistry, db: ShimDatabase
) -> None:
    svc = await make_service()
    rw = comp(components, "remnawave")
    rw.probe_error = ProbeError("Панель ответила 401 — проверьте токен")
    result = await svc.apply(
        [Change("REMNAWAVE_URL", "https://panel.example.com"), Change("TRIAL_DAYS", "5")],
        source="bot",
        actor_id=1,
    )
    assert result.rejected == {"REMNAWAVE_URL": "Панель ответила 401 — проверьте токен"}
    assert result.applied == {"TRIAL_DAYS": 5}  # other groups are independent
    assert svc.current()["REMNAWAVE_URL"] is None
    assert rw.probes and rw.probes[0]["REMNAWAVE_URL"] == "https://panel.example.com"
    assert rw.reconfigs == []
    assert "REMNAWAVE_URL" not in await rows(db)


async def test_probe_timeout_and_unexpected_error(
    make_service: ServiceFactory, components: ComponentRegistry
) -> None:
    svc = await make_service(probe_timeout=0.05)
    comp(components, "remnawave").probe_delay = 1
    result = await svc.apply([Change("REMNAWAVE_URL", "https://p.example.com")], source="bot", actor_id=1)
    assert "не завершилась" in result.rejected["REMNAWAVE_URL"]
    comp(components, "remnawave").probe_delay = 0
    comp(components, "remnawave").probe_error = RuntimeError("boom")
    result = await svc.apply([Change("REMNAWAVE_URL", "https://p.example.com")], source="bot", actor_id=1)
    assert "RuntimeError" in result.rejected["REMNAWAVE_URL"]
    assert svc.current()["REMNAWAVE_URL"] is None


async def test_reconfigure_failure_rolls_back_automatically(
    make_service: ServiceFactory, components: ComponentRegistry, db: ShimDatabase
) -> None:
    svc = await make_service()
    await svc.apply([Change("REMNAWAVE_URL", "https://old.example.com")], source="bot", actor_id=1)
    rw = comp(components, "remnawave")
    rw.reconfigure_error = RuntimeError("socket closed")
    rw.fail_reconfigure_times = 1
    version = svc.current().version
    result = await svc.apply([Change("REMNAWAVE_URL", "https://new.example.com")], source="bot", actor_id=1)
    assert "REMNAWAVE_URL" in result.rejected and "Возвращено прежнее" in result.rejected["REMNAWAVE_URL"]
    assert result.applied == {}
    assert svc.current()["REMNAWAVE_URL"] == "https://old.example.com"
    assert svc.current().version > version
    assert (await rows(db))["REMNAWAVE_URL"] == ("https://old.example.com", "bot")
    entries = await audit(db)
    original = [e for e in entries if e["batch_id"] == result.batch_id]
    assert original and not original[0]["applied"] and "откат" in original[0]["error"]
    assert entries[-1]["source"] == "rollback" and entries[-1]["applied"]
    # The component was asked to go back to the restored configuration.
    assert rw.reconfigs[-1]["REMNAWAVE_URL"] == "https://old.example.com"


async def test_rollback_of_first_value_deletes_row(
    make_service: ServiceFactory, components: ComponentRegistry, db: ShimDatabase
) -> None:
    svc = await make_service()
    rw = comp(components, "remnawave")
    rw.reconfigure_error = ProbeError("не подключилось")
    rw.fail_reconfigure_times = 1
    result = await svc.apply([Change("REMNAWAVE_URL", "https://x.example.com")], source="bot", actor_id=1)
    assert "не подключилось" in result.rejected["REMNAWAVE_URL"]
    assert "REMNAWAVE_URL" not in await rows(db)
    assert svc.current()["REMNAWAVE_URL"] is None and svc.current().source("REMNAWAVE_URL") == "default"


async def test_restart_keys_set_restart_required(make_service: ServiceFactory) -> None:
    svc = await make_service()
    result = await svc.apply([Change("DATABASE_URL", "postgresql://u:p@db:5432/x")], source="bot", actor_id=1)
    assert result.ok and result.restart_required
    assert "DATABASE_URL" in svc.restart_pending
    assert result.applied["DATABASE_URL"].startswith("\N{BULLET}")  # secret: redacted in the result


async def test_locked_keys(make_service: ServiceFactory, env_path: Path, db: ShimDatabase) -> None:
    env_path.parent.mkdir(parents=True)
    env_path.write_text("LOCKED_KEYS=TRIAL_DAYS,BOT_MODE\n", encoding="utf-8")
    svc = await make_service(environ={"TRIAL_DAYS": "30"})
    assert svc.locked == frozenset({"TRIAL_DAYS"})  # BOT_MODE is not in the environment → not locked
    assert svc.current()["TRIAL_DAYS"] == 30 and svc.current().source("TRIAL_DAYS") == "locked"
    result = await svc.apply([Change("TRIAL_DAYS", "1")], source="bot", actor_id=1)
    assert "LOCKED_KEYS" in result.rejected["TRIAL_DAYS"] and svc.current()["TRIAL_DAYS"] == 30
    assert "TRIAL_DAYS" not in await rows(db)
    result = await svc.apply([Change("LOCKED_KEYS", "")], source="bot", actor_id=1)
    assert "только в файле" in result.rejected["LOCKED_KEYS"]


async def test_readonly_and_unchanged_markers(make_service: ServiceFactory) -> None:
    svc = await make_service()
    result = await svc.apply(
        [Change("SECRET_KEY", generate_key()), Change("DATA_DIR", "/x")], source="env_file", actor_id=None
    )
    assert "ротации" in result.rejected["SECRET_KEY"] and "DATA_DIR" in result.rejected
    await svc.apply([Change("REMNAWAVE_TOKEN", RW_TOKEN)], source="bot", actor_id=1)
    result = await svc.apply([Change("REMNAWAVE_TOKEN", SECRET_PLACEHOLDER)], source="bot", actor_id=1)
    assert result.unchanged == ["REMNAWAVE_TOKEN"] and svc.current()["REMNAWAVE_TOKEN"] == RW_TOKEN
    result = await svc.apply([Change("TRIAL_DAYS", "3")], source="bot", actor_id=1)
    assert result.applied == {"TRIAL_DAYS": 3}  # explicit value equal to the default creates a row
    result = await svc.apply([Change("TRIAL_DAYS", "3")], source="bot", actor_id=1)
    assert result.unchanged == ["TRIAL_DAYS"] and result.applied == {}


async def test_cross_key_check(make_service: ServiceFactory) -> None:
    svc = await make_service()
    result = await svc.apply([Change("BOT_MODE", "webhook")], source="bot", actor_id=1)
    assert "PUBLIC_URL" in result.rejected["BOT_MODE"]
    result = await svc.apply(
        [Change("BOT_MODE", "webhook"), Change("PUBLIC_URL", "https://bot.example.com")],
        source="bot",
        actor_id=1,
    )
    assert result.ok and svc.current()["BOT_MODE"] == "webhook"


async def test_reset_and_blank_values(make_service: ServiceFactory, db: ShimDatabase) -> None:
    svc = await make_service()
    await svc.apply(
        [Change("TRIAL_DAYS", "9"), Change("SUPPORT_URL", "https://t.me/x")], source="bot", actor_id=1
    )
    result = await svc.reset("TRIAL_DAYS", source="bot", actor_id=1)
    assert result.applied == {"TRIAL_DAYS": 3}
    assert svc.current().source("TRIAL_DAYS") == "default" and "TRIAL_DAYS" not in await rows(db)
    result = await svc.apply([Change("SUPPORT_URL", "")], source="env_file", actor_id=None)
    assert result.applied == {"SUPPORT_URL": None}  # nullable: explicit null
    assert (await rows(db))["SUPPORT_URL"] == (None, "env_file")
    result = await svc.apply([Change("TRIAL_DAYS", RESET)], source="bot", actor_id=1)
    assert result.unchanged == ["TRIAL_DAYS"]


async def test_undo_batch_restores_previous_state(make_service: ServiceFactory, db: ShimDatabase) -> None:
    svc = await make_service()
    await svc.apply([Change("TRIAL_DAYS", "5")], source="env_file", actor_id=None)
    batch = await svc.apply(
        [Change("TRIAL_DAYS", "6"), Change("CURRENCY", "USD"), Change("REMNAWAVE_TOKEN", RW_TOKEN)],
        source="import",
        actor_id=1,
    )
    assert batch.ok
    result = await svc.undo(batch.batch_id, actor_id=1)
    assert result.ok and set(result.applied) == {"TRIAL_DAYS", "CURRENCY", "REMNAWAVE_TOKEN"}
    snap = svc.current()
    assert snap["TRIAL_DAYS"] == 5 and snap.source("TRIAL_DAYS") == "env_file"
    assert snap["CURRENCY"] == "RUB" and snap.source("CURRENCY") == "default"
    assert snap["REMNAWAVE_TOKEN"] is None
    assert (await rows(db))["TRIAL_DAYS"] == (5, "env_file") and "CURRENCY" not in await rows(db)
    # Undo of the undo brings the batch back, including the secret (kept encrypted in the audit).
    redo = await svc.undo(result.batch_id, actor_id=1)
    assert redo.ok and svc.current()["REMNAWAVE_TOKEN"] == RW_TOKEN and svc.current()["TRIAL_DAYS"] == 6
    with pytest.raises(SettingsError):
        await svc.undo("no-such-batch", actor_id=1)


async def test_secrets_are_encrypted_and_never_logged_or_audited(
    make_service: ServiceFactory, db: ShimDatabase, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    svc = await make_service()
    await svc.apply([Change("REMNAWAVE_TOKEN", RW_TOKEN)], source="bot", actor_id=1)
    await svc.apply([Change("REMNAWAVE_TOKEN", RW_TOKEN + "2")], source="bot", actor_id=1)
    await svc.apply([Change("WEBHOOK_SECRET", "bad secret with spaces!")], source="bot", actor_id=1)
    stored = await db.fetch("select value::text as v from settings where key='REMNAWAVE_TOKEN'")
    assert json.loads(stored[0]["v"]).startswith("enc:v1:")
    dump = json.dumps([dict(r) for r in await db.fetch("select * from settings_audit")], default=str)
    dump += json.dumps([dict(r) for r in await db.fetch("select * from settings")], default=str)
    for secret in (RW_TOKEN, RW_TOKEN + "2", "bad secret with spaces!"):
        assert secret not in dump
        assert secret not in caplog.text
    entries = await audit(db)
    assert set(json.loads(entries[1]["new"])) == {"fp", "enc", "src"}
    assert set(json.loads(entries[2]["new"])) == {"fp"}  # rejected secret: fingerprint only
    assert RW_TOKEN not in repr(svc.current())


async def test_undecryptable_secret_is_loud_not_ciphertext(make_service: ServiceFactory) -> None:
    svc = await make_service()
    await svc.apply([Change("REMNAWAVE_TOKEN", RW_TOKEN)], source="bot", actor_id=1)
    other = await make_service(crypto_=Crypto([generate_key()]))
    assert other.current()["REMNAWAVE_TOKEN"] is None
    assert "REMNAWAVE_TOKEN" in other.undecryptable and "SECRET_KEY" in other.problems["REMNAWAVE_TOKEN"]
    result = await other.apply([Change("REMNAWAVE_TOKEN", RW_TOKEN)], source="bot", actor_id=1)
    assert result.ok and "REMNAWAVE_TOKEN" not in other.undecryptable


async def test_database_outage_rejects_and_keeps_snapshot(
    make_service: ServiceFactory, db: ShimDatabase
) -> None:
    svc = await make_service()
    db.fail_writes = True
    result = await svc.apply([Change("TRIAL_DAYS", "5")], source="bot", actor_id=1)
    assert result.rejected == {"TRIAL_DAYS": DB_UNAVAILABLE} and svc.current()["TRIAL_DAYS"] == 3
    assert svc.current().version == 1
    db.fail_writes = False
    assert (await svc.apply([Change("TRIAL_DAYS", "5")], source="bot", actor_id=1)).ok


async def test_failing_subscriber_is_isolated(make_service: ServiceFactory) -> None:
    svc = await make_service()
    calls: list[set[str]] = []

    async def broken(_snap: Any, _keys: set[str]) -> None:
        raise RuntimeError("subscriber bug")

    svc.subscribe("*", broken)
    svc.subscribe("*", lambda _s, keys: calls.append(keys))  # sync callbacks are fine too
    assert (await svc.apply([Change("TRIAL_DAYS", "5")], source="bot", actor_id=1)).ok
    assert calls == [{"TRIAL_DAYS"}]


async def test_concurrent_applies_are_serialized(make_service: ServiceFactory, db: ShimDatabase) -> None:
    svc = await make_service()
    results = await asyncio.gather(
        *(svc.apply([Change("TRIAL_DAYS", str(i))], source="bot", actor_id=1) for i in range(10, 30))
    )
    assert all(r.ok for r in results)
    assert svc.current().version == 21
    assert (await rows(db))["TRIAL_DAYS"][0] == svc.current()["TRIAL_DAYS"]


async def test_expected_version_guard(make_service: ServiceFactory) -> None:
    svc = await make_service()
    await svc.apply([Change("TRIAL_DAYS", "5")], source="bot", actor_id=1)
    with pytest.raises(StaleSnapshotError):
        await svc.apply([Change("TRIAL_DAYS", "6")], source="env_file", actor_id=None, expected_version=1)


async def test_reload_component_gets_one_reconfigure_per_batch(
    make_service: ServiceFactory, components: ComponentRegistry
) -> None:
    svc = await make_service()
    rw = comp(components, "remnawave")
    result = await svc.apply(
        [Change("REMNAWAVE_URL", "https://p.example.com"), Change("REMNAWAVE_TOKEN", RW_TOKEN)],
        source="wizard",
        actor_id=1,
    )
    assert result.ok and result.reloaded == ["remnawave"]
    assert len(rw.probes) == 1 and len(rw.reconfigs) == 1
    assert rw.reconfigs[0]["REMNAWAVE_TOKEN"] == RW_TOKEN


async def test_unregistered_component_does_not_block(make_service: ServiceFactory) -> None:
    svc = await make_service()
    result = await svc.apply([Change("ADMIN_CHAT_ID", "-1001234")], source="bot", actor_id=1)
    assert result.ok and result.reloaded == [] and svc.current()["ADMIN_CHAT_ID"] == -1001234


async def test_search(make_service: ServiceFactory) -> None:
    svc = await make_service()
    assert svc.search("TRIAL_DAYS")[0].key == "TRIAL_DAYS"
    assert svc.search("пробный")[0].key in {"TRIAL_DAYS", "TRIAL_AUDIENCE"}
    assert any(d.key == "REMNAWAVE_TOKEN" for d in svc.search("токен"))
    assert svc.search("TRAIL_DAYS")[0].key == "TRIAL_DAYS"  # typo
    assert svc.search("") == [] and svc.search("zzzzqqq") == []
    assert len(svc.search("а", limit=3)) <= 3


async def test_history_and_purge(make_service: ServiceFactory) -> None:
    svc = await make_service()
    for i in range(5):
        await svc.apply([Change("TRIAL_DAYS", str(i + 10))], source="bot", actor_id=7)
    history = await svc.history("TRIAL_DAYS", limit=3)
    assert [json.loads(json.dumps(h.new))["v"] for h in history] == [14, 13, 12]
    assert all(h.actor_id == 7 for h in history)
    removed = await svc.store.purge_audit(keep_last=2)
    assert removed == 3 and len(await svc.history()) == 2


async def test_actor_default_is_used(make_service: ServiceFactory, db: ShimDatabase) -> None:
    svc = await make_service(audit_actor_default=99)
    await svc.apply([Change("TRIAL_DAYS", "5")], source="cli", actor_id=None)
    found = await db.fetch("select actor_id from settings_audit")
    assert found[0]["actor_id"] == 99


async def test_snapshot_reads_do_not_touch_the_database(
    make_service: ServiceFactory, db: ShimDatabase
) -> None:
    svc = await make_service()
    db.fail_reads = db.fail_writes = True
    for _ in range(1000):
        assert svc.current()["TRIAL_DAYS"] == 3


# -------------------------------------------------------------------------------------------- review fixes


async def test_bootstrap_key_edited_in_file_while_down_beats_the_stored_row(
    make_service: ServiceFactory, env_path: Path
) -> None:
    environ = {"BOT_TOKEN": "111111:" + "Z" * 35}
    svc = await make_service()
    assert (await svc.apply([Change("BOT_TOKEN", TOKEN)], source="bot", actor_id=1)).ok
    new_token = "555555555:" + "N" * 35
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(f"BOT_TOKEN={new_token}\n", encoding="utf-8")
    again = await make_service(environ=environ)
    assert again.current()["BOT_TOKEN"] == new_token  # the bot starts with the token from the file
    assert again.current().source("BOT_TOKEN") == "env_file"
    assert again.row_source("BOT_TOKEN") is None  # the stale row is not tracked as "set in the bot"
    # No line in the file: the change made in the bot beats the container environment.
    env_path.write_text("", encoding="utf-8")
    third = await make_service(environ=environ)
    assert third.current()["BOT_TOKEN"] == TOKEN and third.current().source("BOT_TOKEN") == "bot"
    # The file has the same value as the row: the row is tracked.
    env_path.write_text(f"BOT_TOKEN={TOKEN}\n", encoding="utf-8")
    fourth = await make_service(environ=environ)
    assert fourth.current()["BOT_TOKEN"] == TOKEN and fourth.row_source("BOT_TOKEN") == "bot"


async def test_unreadable_row_is_never_overwritten_by_the_environment(
    make_service: ServiceFactory, db: ShimDatabase
) -> None:
    env_token = "env-token-" + "e" * 20
    svc = await make_service()
    await svc.apply([Change("REMNAWAVE_TOKEN", RW_TOKEN)], source="bot", actor_id=1)
    before = await rows(db)
    other = await make_service(crypto_=Crypto([generate_key()]), environ={"REMNAWAVE_TOKEN": env_token})
    assert other.current()["REMNAWAVE_TOKEN"] == env_token  # used in memory only
    assert other.current().source("REMNAWAVE_TOKEN") == "environ"
    assert "REMNAWAVE_TOKEN" in other.undecryptable
    assert await rows(db) == before  # the encrypted original survives
    back = await make_service()  # the right SECRET_KEY is back
    assert back.current()["REMNAWAVE_TOKEN"] == RW_TOKEN
    # The same for a stored value that is no longer valid.
    await db.fetch(
        "insert into settings(key, value, source, updated_at) "
        "values ('TRIAL_DAYS', '9999'::jsonb, 'bot', now())"
    )
    seeded = await make_service(environ={"TRIAL_DAYS": "5"})
    assert seeded.current()["TRIAL_DAYS"] == 5 and "TRIAL_DAYS" in seeded.problems
    assert (await rows(db))["TRIAL_DAYS"] == (9999, "bot")


async def test_repeated_database_failures_log_one_traceback(
    make_service: ServiceFactory, db: ShimDatabase, caplog: pytest.LogCaptureFixture
) -> None:
    svc = await make_service()
    caplog.set_level(logging.WARNING, logger="svbg.core.settings")
    db.fail_writes = True
    for value in ("4", "5", "6"):
        result = await svc.apply([Change("TRIAL_DAYS", value)], source="env_file", actor_id=None)
        assert result.rejected == {"TRIAL_DAYS": DB_UNAVAILABLE}
    records = [r for r in caplog.records if "cannot persist batch" in r.getMessage()]
    assert len(records) == 3
    assert [r.exc_info is not None for r in records] == [True, False, False]
    db.fail_writes = False
    assert (await svc.apply([Change("TRIAL_DAYS", "7")], source="bot", actor_id=1)).ok
    assert svc._db_failures == 0
