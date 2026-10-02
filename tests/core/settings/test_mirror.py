"""The live .env mirror on a real PostgreSQL and a temporary directory."""

from __future__ import annotations

import asyncio
import dataclasses
import errno
import hashlib
import json
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from svbg.boot import envfile
from svbg.boot.envfile import EnvDocument
from svbg.core.crypto import Crypto, generate_key
from svbg.core.settings.mirror import EnvMirror, MirrorNotice
from svbg.core.settings.registry import Registry, core_registry
from svbg.core.settings.service import Change, SettingsService
from svbg.core.settings.values import SECRET_PLACEHOLDER
from tests.core.settings.conftest import ServiceFactory, ShimDatabase, wait_until

TOKEN = "123456789:" + "A" * 35
RW_TOKEN = "rw-token-" + "y" * 30

MirrorFactory = Callable[..., Any]


def read(path: Path) -> str:
    # Windows: a read racing with the mirror's os.replace may hit a transient sharing violation.
    for _ in range(20):
        try:
            return path.read_text(encoding="utf-8")
        except PermissionError:
            time.sleep(0.01)
    return path.read_text(encoding="utf-8")


def file_value(path: Path, key: str) -> str | None:
    return EnvDocument.parse(read(path)).get(key)


def edit(path: Path, key: str, value: str) -> None:
    """Edit like a human with an editor: change one line, keep everything else."""
    doc = EnvDocument.parse(read(path))
    doc.set(key, value)
    path.write_text(doc.render(), encoding="utf-8", newline="")


def registry_with(**changes: dict[str, Any]) -> Registry:
    reg = Registry()
    for defn in core_registry().all():
        reg.add(dataclasses.replace(defn, **changes[defn.key]) if defn.key in changes else defn)
    return reg


@pytest.fixture
async def mirrors() -> AsyncIterator[list[EnvMirror]]:
    started: list[EnvMirror] = []
    yield started
    for mirror in started:
        await mirror.stop()


@pytest.fixture
def make_mirror(env_path: Path, mirrors: list[EnvMirror]) -> MirrorFactory:
    async def factory(
        service: SettingsService, *, start: bool = True, notices: list[MirrorNotice] | None = None, **kw: Any
    ) -> EnvMirror:
        kw.setdefault("poll_interval", 0.1)
        kw.setdefault("debounce", 0.05)
        kw.setdefault("settle", 0.05)
        mirror = EnvMirror(
            service,
            service.registry,
            env_path,
            on_notice=(notices.append if notices is not None else None),
            **kw,
        )
        if start:
            await mirror.start()
            mirrors.append(mirror)
        return mirror

    return factory


def audit_sources(rows: list[Any]) -> list[str]:
    return [r["source"] for r in rows]


# ------------------------------------------------------------------------------------------------ render


async def test_first_start_renders_full_file(
    make_service: ServiceFactory, make_mirror: MirrorFactory, env_path: Path, db: ShimDatabase
) -> None:
    svc = await make_service(environ={"BOT_TOKEN": TOKEN})
    notices: list[MirrorNotice] = []
    await make_mirror(svc, notices=notices)
    text = read(env_path)
    doc = EnvDocument.parse(text)
    for defn in svc.registry.all():
        if defn.in_file:
            assert defn.key in doc.as_dict(), defn.key
    assert doc.get("TRIAL_DAYS") == "3" and doc.get("BOT_TOKEN") == TOKEN
    assert "DATA_DIR" not in doc.as_dict()
    assert "SvBG Shop — все настройки" in text and "── Продажи и триал" in text
    assert "По умолчанию: 3" in text
    assert [n.kind for n in notices] == ["created"]
    base = await db.fetch("select value::text as v from config_meta where key='env_base'")
    stored = json.loads(base[0]["v"])
    assert set(stored) == {"v", "enc"}  # the merge base holds bootstrap secrets: encrypted, never plain
    assert TOKEN not in base[0]["v"] and "SECRET_KEY" not in base[0]["v"]
    assert svc.crypto.decrypt(stored["enc"]) == text
    # Defaults written to the file are not frozen: no rows were created.
    assert await db.fetch("select key from settings") == []


async def test_bot_change_reaches_file_within_a_second(
    make_service: ServiceFactory, make_mirror: MirrorFactory, env_path: Path
) -> None:
    svc = await make_service()
    await make_mirror(svc, debounce=0.3, poll_interval=2.0)
    started = time.monotonic()
    result = await svc.apply([Change("TRIAL_DAYS", "14")], source="bot", actor_id=1)
    assert result.ok
    await wait_until(lambda: file_value(env_path, "TRIAL_DAYS") == "14", timeout=1.0)
    assert time.monotonic() - started <= 1.0


async def test_manual_edit_is_applied_within_three_seconds(
    make_service: ServiceFactory, make_mirror: MirrorFactory, env_path: Path
) -> None:
    svc = await make_service()
    notices: list[MirrorNotice] = []
    await make_mirror(svc, notices=notices, poll_interval=1.0, settle=0.3, debounce=0.3)
    started = time.monotonic()
    edit(env_path, "TRIAL_DAYS", "21")
    await wait_until(lambda: svc.current()["TRIAL_DAYS"] == 21, timeout=3.0)
    assert time.monotonic() - started <= 3.0
    assert svc.current().source("TRIAL_DAYS") == "env_file"
    assert svc.row_source("TRIAL_DAYS") == "env_file"
    await wait_until(lambda: any(n.kind == "applied" for n in notices))
    applied = next(n for n in notices if n.kind == "applied")
    assert "TRIAL_DAYS 3 → 21" in applied.message


async def test_own_writes_do_not_trigger_reapply(
    make_service: ServiceFactory, make_mirror: MirrorFactory, env_path: Path, db: ShimDatabase
) -> None:
    svc = await make_service()
    calls: list[str] = []
    original = svc.apply

    async def spy(changes: list[Change], **kw: Any) -> Any:
        calls.append(kw["source"])
        return await original(changes, **kw)

    svc.apply = spy  # type: ignore[method-assign]
    await make_mirror(svc)
    for value in ("4", "5", "6"):
        await svc.apply([Change("TRIAL_DAYS", value)], source="bot", actor_id=1)
    await wait_until(lambda: file_value(env_path, "TRIAL_DAYS") == "6")
    await asyncio.sleep(0.6)  # several poll intervals
    assert calls == ["bot", "bot", "bot"]
    assert "env_file" not in audit_sources(await db.fetch("select source from settings_audit"))


async def test_owner_lines_comments_bom_and_crlf_survive(
    make_service: ServiceFactory, make_mirror: MirrorFactory, env_path: Path
) -> None:
    env_path.parent.mkdir(parents=True)
    original = f"﻿# мой заголовок\r\nBOT_TOKEN={TOKEN}\r\n# про триал\r\nTRIAL_DAYS=7 # неделя\r\nMY_OWN=1\r\n"
    env_path.write_bytes(original.encode("utf-8"))
    svc = await make_service()
    await make_mirror(svc)
    text = env_path.read_bytes().decode("utf-8")
    assert text.startswith("﻿") and "\r\n" in text and "\n" not in text.replace("\r\n", "")
    assert "# мой заголовок" in text and "# про триал" in text and "# неделя" in text
    assert "MY_OWN=1" in text and "Не распознано" in text
    assert svc.current()["TRIAL_DAYS"] == 7 and svc.current().source("TRIAL_DAYS") == "env_file"


# ------------------------------------------------------------------------------------------------ merge


async def test_invalid_manual_edit_is_not_applied_and_marked(
    make_service: ServiceFactory, make_mirror: MirrorFactory, env_path: Path
) -> None:
    svc = await make_service()
    notices: list[MirrorNotice] = []
    mirror = await make_mirror(svc, notices=notices)
    edit(env_path, "TRIAL_DAYS", "abc")
    await wait_until(lambda: file_value(env_path, "TRIAL_DAYS") == "3")
    text = read(env_path)
    assert "# ⚠" in text and "«abc» отклонено" in text and "Применено прежнее: 3" in text
    assert svc.current()["TRIAL_DAYS"] == 3 and svc.row_source("TRIAL_DAYS") is None
    await wait_until(lambda: any(n.kind == "invalid" for n in notices))
    invalid = next(n for n in notices if n.kind == "invalid")
    assert invalid.keys == ("TRIAL_DAYS",) and invalid.fix_action == "setting:TRIAL_DAYS"
    assert "TRIAL_DAYS" in mirror.status.invalid
    # Fixing the line applies it and removes the warning.
    edit(env_path, "TRIAL_DAYS", "8")
    await wait_until(lambda: svc.current()["TRIAL_DAYS"] == 8)
    await wait_until(lambda: "# ⚠" not in read(env_path))


async def test_conflict_database_wins_and_owner_is_notified(
    make_service: ServiceFactory, make_mirror: MirrorFactory, env_path: Path, db: ShimDatabase
) -> None:
    svc = await make_service()
    notices: list[MirrorNotice] = []
    mirror = await make_mirror(svc, notices=notices, start=False)
    await mirror.start()
    # Freeze background work, then change the key on both sides.
    for task in mirror._tasks:
        task.cancel()
    await svc.apply([Change("TRIAL_DAYS", "7")], source="bot", actor_id=1)
    edit(env_path, "TRIAL_DAYS", "5")
    await mirror.sync()
    assert svc.current()["TRIAL_DAYS"] == 7
    assert file_value(env_path, "TRIAL_DAYS") == "7"
    conflict = next(n for n in notices if n.kind == "conflict")
    assert "Оставлено значение из бота: 7" in conflict.message and "(5)" in conflict.message
    found = await db.fetch(
        "select error, applied, source from settings_audit where error='conflict_discarded'"
    )
    assert found and not found[0]["applied"] and found[0]["source"] == "env_file"
    assert "одновременно изменено в боте" in read(env_path)


async def test_same_change_on_both_sides_is_not_a_conflict(
    make_service: ServiceFactory, make_mirror: MirrorFactory, env_path: Path
) -> None:
    svc = await make_service()
    notices: list[MirrorNotice] = []
    mirror = await make_mirror(svc, notices=notices, start=False)
    await mirror.start()
    for task in mirror._tasks:
        task.cancel()
    await svc.apply([Change("TRIAL_DAYS", "7")], source="bot", actor_id=1)
    edit(env_path, "TRIAL_DAYS", "07")
    await mirror.sync()
    assert not [n for n in notices if n.kind in {"conflict", "invalid"}]
    assert file_value(env_path, "TRIAL_DAYS") == "7"


async def test_default_change_in_new_version_rewrites_untouched_lines_only(
    make_service: ServiceFactory, make_mirror: MirrorFactory, env_path: Path, mirrors: list[EnvMirror]
) -> None:
    svc = await make_service()
    await make_mirror(svc)
    await svc.apply([Change("TRIAL_AUDIENCE", "channel_members")], source="bot", actor_id=1)
    # Set in the file explicitly to a value equal to the default (the line already says 60: go via 61).
    edit(env_path, "WALLET_AUTOCOMPLETE_MINUTES", "61")
    await wait_until(lambda: svc.current()["WALLET_AUTOCOMPLETE_MINUTES"] == 61)
    edit(env_path, "WALLET_AUTOCOMPLETE_MINUTES", "60")
    await wait_until(lambda: svc.current()["WALLET_AUTOCOMPLETE_MINUTES"] == 60)
    assert svc.row_source("WALLET_AUTOCOMPLETE_MINUTES") == "env_file"
    await wait_until(lambda: file_value(env_path, "TRIAL_AUDIENCE") == "channel_members")
    for mirror in mirrors:
        await mirror.stop()
    mirrors.clear()

    v2 = registry_with(
        TRIAL_DAYS={"default": 5},
        TRIAL_AUDIENCE={"default": "all"},
        WALLET_AUTOCOMPLETE_MINUTES={"default": 90},
    )
    upgraded = await make_service(registry=v2)
    await make_mirror(upgraded)
    assert file_value(env_path, "TRIAL_DAYS") == "5"  # untouched default follows the new version
    assert upgraded.current()["TRIAL_DAYS"] == 5 and upgraded.row_source("TRIAL_DAYS") is None
    assert file_value(env_path, "TRIAL_AUDIENCE") == "channel_members"
    assert file_value(env_path, "WALLET_AUTOCOMPLETE_MINUTES") == "60"
    assert "По умолчанию: 5" in read(env_path)


async def test_alias_is_read_and_line_rewritten(
    make_service: ServiceFactory, make_mirror: MirrorFactory, env_path: Path
) -> None:
    reg = registry_with(TRIAL_DAYS={"aliases": ("TRIAL_PERIOD_DAYS",)})
    env_path.parent.mkdir(parents=True)
    env_path.write_text("# старое имя\nTRIAL_PERIOD_DAYS=12\n", encoding="utf-8")
    svc = await make_service(registry=reg)
    await make_mirror(svc)
    assert svc.current()["TRIAL_DAYS"] == 12 and svc.current()["TRIAL_PERIOD_DAYS"] == 12
    doc = EnvDocument.parse(read(env_path))
    assert doc.get("TRIAL_DAYS") == "12" and "TRIAL_PERIOD_DAYS" not in doc.as_dict()
    assert "Раньше называлось: TRIAL_PERIOD_DAYS" in read(env_path)


async def test_removed_line_restored_and_blank_resets(
    make_service: ServiceFactory, make_mirror: MirrorFactory, env_path: Path
) -> None:
    svc = await make_service()
    mirror = await make_mirror(svc)
    await svc.apply(
        [Change("TRIAL_DAYS", "9"), Change("SUPPORT_URL", "https://t.me/help")], source="bot", actor_id=1
    )
    await mirror.render_now()
    doc = EnvDocument.parse(read(env_path))
    doc.remove("CURRENCY")
    env_path.write_text(doc.render(), encoding="utf-8", newline="")
    await mirror.sync()
    assert file_value(env_path, "CURRENCY") == "RUB" and svc.current()["CURRENCY"] == "RUB"
    edit(env_path, "TRIAL_DAYS", "")
    edit(env_path, "SUPPORT_URL", "")
    await mirror.sync()
    assert svc.current()["TRIAL_DAYS"] == 3 and svc.row_source("TRIAL_DAYS") is None  # reset to default
    assert svc.current()["SUPPORT_URL"] is None and svc.row_source("SUPPORT_URL") == "env_file"
    assert file_value(env_path, "TRIAL_DAYS") == "3"


async def test_edits_made_while_down_are_merged_on_start(
    make_service: ServiceFactory, make_mirror: MirrorFactory, env_path: Path, mirrors: list[EnvMirror]
) -> None:
    svc = await make_service()
    await make_mirror(svc)
    for mirror in mirrors:
        await mirror.stop()
    mirrors.clear()
    edit(env_path, "CURRENCY", "EUR")
    again = await make_service()
    assert again.current()["CURRENCY"] == "RUB"
    await make_mirror(again)
    assert again.current()["CURRENCY"] == "EUR" and again.current().source("CURRENCY") == "env_file"


async def test_no_base_explicit_bot_value_wins(
    make_service: ServiceFactory, make_mirror: MirrorFactory, env_path: Path, db: ShimDatabase
) -> None:
    svc = await make_service()
    await svc.apply([Change("TRIAL_DAYS", "11")], source="bot", actor_id=1)
    env_path.parent.mkdir(parents=True)
    env_path.write_text("TRIAL_DAYS=4\nCURRENCY=USD\n", encoding="utf-8")
    notices: list[MirrorNotice] = []
    await make_mirror(svc, notices=notices)
    assert svc.current()["TRIAL_DAYS"] == 11 and file_value(env_path, "TRIAL_DAYS") == "11"
    assert svc.current()["CURRENCY"] == "USD"
    assert any(n.kind == "conflict" for n in notices)


async def test_locked_key_edit_is_refused(
    make_service: ServiceFactory, make_mirror: MirrorFactory, env_path: Path
) -> None:
    env_path.parent.mkdir(parents=True)
    env_path.write_text("LOCKED_KEYS=TRIAL_DAYS\n", encoding="utf-8")
    svc = await make_service(environ={"TRIAL_DAYS": "30"})
    mirror = await make_mirror(svc)
    assert file_value(env_path, "TRIAL_DAYS") == "30" and "LOCKED_KEYS)" in read(env_path)
    edit(env_path, "TRIAL_DAYS", "2")
    await mirror.sync()
    assert svc.current()["TRIAL_DAYS"] == 30 and file_value(env_path, "TRIAL_DAYS") == "30"
    assert "задано окружением" in read(env_path)


async def test_broken_line_for_known_key_is_marked(
    make_service: ServiceFactory, make_mirror: MirrorFactory, env_path: Path
) -> None:
    svc = await make_service()
    mirror = await make_mirror(svc)
    text = read(env_path).replace("TRIAL_DAYS=3", 'TRIAL_DAYS="5')
    env_path.write_text(text, encoding="utf-8", newline="")
    await mirror.sync()
    assert file_value(env_path, "TRIAL_DAYS") == "3"
    assert "строка не разобрана" in read(env_path)


async def test_restart_key_from_file(
    make_service: ServiceFactory, make_mirror: MirrorFactory, env_path: Path
) -> None:
    svc = await make_service()
    notices: list[MirrorNotice] = []
    mirror = await make_mirror(svc, notices=notices)
    edit(env_path, "DATABASE_URL", "postgresql://svbg:pw@newdb:5432/svbg")
    edit(env_path, "LOCKED_KEYS", "BOT_MODE")
    await mirror.sync()
    assert {"DATABASE_URL", "LOCKED_KEYS"} <= svc.restart_pending
    assert any(n.kind == "restart" for n in notices)
    assert svc.row_source("LOCKED_KEYS") is None  # file-only key: never stored in the DB


async def test_secret_key_edit_is_refused_and_restored(
    make_service: ServiceFactory, make_mirror: MirrorFactory, env_path: Path
) -> None:
    from svbg.core.crypto import generate_key

    svc = await make_service(environ={"SECRET_KEY": generate_key()})
    mirror = await make_mirror(svc)
    original = file_value(env_path, "SECRET_KEY")
    new_key = generate_key()
    edit(env_path, "SECRET_KEY", new_key)
    await mirror.sync()
    assert file_value(env_path, "SECRET_KEY") == original
    assert new_key not in read(env_path).replace(f"SECRET_KEY={original}", "")
    assert "ротации" in read(env_path)


# ---------------------------------------------------------------------------------------- secrets & layout


async def test_env_secrets_omit_leaves_placeholder(
    make_service: ServiceFactory, make_mirror: MirrorFactory, env_path: Path
) -> None:
    svc = await make_service(environ={"BOT_TOKEN": TOKEN})
    mirror = await make_mirror(svc)
    await svc.apply([Change("REMNAWAVE_TOKEN", RW_TOKEN)], source="bot", actor_id=1)
    await mirror.render_now()
    assert file_value(env_path, "REMNAWAVE_TOKEN") == RW_TOKEN  # plain by default
    await svc.apply([Change("ENV_SECRETS", "omit")], source="bot", actor_id=1)
    await mirror.render_now()
    text = read(env_path)
    assert RW_TOKEN not in text and file_value(env_path, "REMNAWAVE_TOKEN") == SECRET_PLACEHOLDER
    assert file_value(env_path, "BOT_TOKEN") == TOKEN  # bootstrap secrets stay: the file is their source
    assert "ENV_SECRETS=omit" in text
    # Writing a new value instead of the placeholder applies it and hides it again.
    edit(env_path, "REMNAWAVE_TOKEN", RW_TOKEN + "new")
    await mirror.sync()
    assert svc.current()["REMNAWAVE_TOKEN"] == RW_TOKEN + "new"
    assert RW_TOKEN not in read(env_path)


async def test_compact_layout_hides_defaults(
    make_service: ServiceFactory, make_mirror: MirrorFactory, env_path: Path
) -> None:
    svc = await make_service()
    mirror = await make_mirror(svc)
    await svc.apply([Change("ENV_LAYOUT", "compact"), Change("TRIAL_DAYS", "5")], source="bot", actor_id=1)
    await mirror.render_now()
    doc = EnvDocument.parse(read(env_path))
    assert doc.get("TRIAL_DAYS") == "5" and doc.get("ENV_LAYOUT") == "compact"
    assert "CURRENCY" not in doc.as_dict() and "BOT_TOKEN" in doc.as_dict()
    assert "Компактный вид" in read(env_path)
    # A key added by hand is still understood.
    edit(env_path, "CURRENCY", "EUR")
    await mirror.sync()
    assert svc.current()["CURRENCY"] == "EUR" and file_value(env_path, "CURRENCY") == "EUR"
    await svc.apply([Change("ENV_LAYOUT", "full")], source="bot", actor_id=1)
    await mirror.render_now()
    assert "WALLET_AUTOCOMPLETE_MINUTES" in EnvDocument.parse(read(env_path)).as_dict()


# ------------------------------------------------------------------------------------------------ failures


async def test_crash_during_write_never_damages_the_file(
    make_service: ServiceFactory,
    make_mirror: MirrorFactory,
    env_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc = await make_service()
    notices: list[MirrorNotice] = []
    mirror = await make_mirror(svc, notices=notices)
    before = read(env_path)

    def killed(_source: Path, _target: Path) -> bool:
        raise OSError(errno.EIO, "process killed during write")

    monkeypatch.setattr(envfile, "_replace", killed)
    await svc.apply([Change("TRIAL_DAYS", "13")], source="bot", actor_id=1)
    await mirror.render_now()
    assert read(env_path) == before  # old content intact
    assert EnvDocument.parse(read(env_path)).get("TRIAL_DAYS") == "3"
    assert not [p for p in env_path.parent.iterdir() if p.name.endswith(".tmp")]
    assert not mirror.status.writable and any(n.kind == "unwritable" for n in notices)
    assert svc.current()["TRIAL_DAYS"] == 13  # the bot keeps working from the DB
    monkeypatch.undo()
    await mirror.render_now()
    assert file_value(env_path, "TRIAL_DAYS") == "13" and mirror.status.writable


async def test_deleted_file_is_restored(
    make_service: ServiceFactory, make_mirror: MirrorFactory, env_path: Path
) -> None:
    svc = await make_service()
    notices: list[MirrorNotice] = []
    await make_mirror(svc, notices=notices)
    await svc.apply([Change("TRIAL_DAYS", "6")], source="bot", actor_id=1)
    await wait_until(lambda: file_value(env_path, "TRIAL_DAYS") == "6")
    env_path.unlink()
    await wait_until(env_path.exists)
    assert file_value(env_path, "TRIAL_DAYS") == "6"
    await wait_until(lambda: any(n.kind == "restored" for n in notices))


async def test_unreadable_file_is_left_alone(
    make_service: ServiceFactory, make_mirror: MirrorFactory, env_path: Path
) -> None:
    svc = await make_service()
    notices: list[MirrorNotice] = []
    mirror = await make_mirror(svc, notices=notices)
    env_path.write_bytes(b"TRIAL_DAYS=\xff\n")
    await mirror.sync()
    assert env_path.read_bytes() == b"TRIAL_DAYS=\xff\n"
    assert not mirror.status.readable and [n.kind for n in notices].count("unreadable") == 1
    await mirror.sync()
    assert [n.kind for n in notices].count("unreadable") == 1  # reported once


async def test_database_outage_during_merge_keeps_the_edit(
    make_service: ServiceFactory, make_mirror: MirrorFactory, env_path: Path, db: ShimDatabase
) -> None:
    svc = await make_service()
    mirror = await make_mirror(svc)
    for task in mirror._tasks:
        task.cancel()
    edit(env_path, "TRIAL_DAYS", "17")
    db.fail_writes = True
    await mirror.sync()
    assert file_value(env_path, "TRIAL_DAYS") == "17" and "# ⚠" not in read(env_path)
    db.fail_writes = False
    await mirror.sync()
    assert svc.current()["TRIAL_DAYS"] == 17


async def test_failing_notice_hook_is_isolated(
    make_service: ServiceFactory, env_path: Path, mirrors: list[EnvMirror]
) -> None:
    svc = await make_service()

    def broken(_notice: MirrorNotice) -> None:
        raise RuntimeError("telegram down")

    mirror = EnvMirror(svc, svc.registry, env_path, on_notice=broken, poll_interval=0.1, debounce=0.05)
    await mirror.start()
    mirrors.append(mirror)
    assert env_path.exists()


async def test_failed_write_is_retried_automatically(
    make_service: ServiceFactory,
    make_mirror: MirrorFactory,
    env_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc = await make_service()
    mirror = await make_mirror(svc, retry_interval=0.2)

    def busy(_source: Path, _target: Path) -> bool:
        raise PermissionError(errno.EACCES, "permission denied")

    monkeypatch.setattr(envfile, "_replace", busy)
    await svc.apply([Change("TRIAL_DAYS", "19")], source="bot", actor_id=1)
    await wait_until(lambda: not mirror.status.writable)
    monkeypatch.undo()  # the owner fixed the permissions
    await wait_until(lambda: file_value(env_path, "TRIAL_DAYS") == "19", timeout=3)
    assert mirror.status.writable


async def test_stop_flushes_pending_write(
    make_service: ServiceFactory, make_mirror: MirrorFactory, env_path: Path, mirrors: list[EnvMirror]
) -> None:
    svc = await make_service()
    mirror = await make_mirror(svc, debounce=5.0)
    await svc.apply([Change("TRIAL_DAYS", "23")], source="bot", actor_id=1)
    assert file_value(env_path, "TRIAL_DAYS") == "3"
    await mirror.stop()
    mirrors.clear()
    assert file_value(env_path, "TRIAL_DAYS") == "23"


async def test_secrets_never_reach_logs_or_notices(
    make_service: ServiceFactory,
    make_mirror: MirrorFactory,
    env_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    caplog.set_level(logging.DEBUG)
    svc = await make_service()
    notices: list[MirrorNotice] = []
    mirror = await make_mirror(svc, notices=notices)
    edit(env_path, "REMNAWAVE_TOKEN", RW_TOKEN)
    edit(env_path, "WEBHOOK_SECRET", "not valid secret!!")
    await mirror.sync()
    assert svc.current()["REMNAWAVE_TOKEN"] == RW_TOKEN
    everything = caplog.text + "\n".join(n.message for n in notices)
    assert RW_TOKEN not in everything and "not valid secret!!" not in everything
    assert any(n.kind == "invalid" and n.keys == ("WEBHOOK_SECRET",) for n in notices)


# -------------------------------------------------------------------------------------------- review fixes


async def test_legacy_plaintext_base_is_reencrypted_on_start(
    make_service: ServiceFactory, make_mirror: MirrorFactory, env_path: Path, db: ShimDatabase
) -> None:
    svc = await make_service(environ={"BOT_TOKEN": TOKEN})
    first = await make_mirror(svc, start=False)
    await first.start()
    await first.stop()
    text = read(env_path)
    legacy = json.dumps({"text": text, "sha256": hashlib.sha256(text.encode()).hexdigest()})
    await db.fetch("update config_meta set value=$1::jsonb where key='env_base'", legacy)
    notices: list[MirrorNotice] = []
    mirror = await make_mirror(svc, notices=notices)
    assert mirror._base_text == text  # the old base is still used for the merge
    raw = (await db.fetch("select value::text as v from config_meta where key='env_base'"))[0]["v"]
    assert TOKEN not in raw and "text" not in json.loads(raw)
    assert svc.crypto.decrypt(json.loads(raw)["enc"]) == text
    assert notices == []


async def test_base_encrypted_with_another_key_is_ignored(
    make_service: ServiceFactory, make_mirror: MirrorFactory, env_path: Path, db: ShimDatabase
) -> None:
    svc = await make_service()
    first = await make_mirror(svc, start=False)
    await first.start()
    await first.stop()
    other = await make_service(crypto_=Crypto([generate_key()]))
    mirror = await make_mirror(other)
    assert mirror._base_text is not None  # re-adopted after the first round, encrypted with the new key
    raw = json.loads(
        (await db.fetch("select value::text as v from config_meta where key='env_base'"))[0]["v"]
    )
    assert other.crypto.decrypt(raw["enc"]) == read(env_path)


async def test_edit_saved_during_a_slow_apply_is_not_lost(
    make_service: ServiceFactory, make_mirror: MirrorFactory, env_path: Path
) -> None:
    svc = await make_service()
    mirror = await make_mirror(svc)
    for task in mirror._tasks:
        task.cancel()
    original = svc.apply
    edited: list[bool] = []

    async def slow_apply(changes: list[Change], **kw: Any) -> Any:
        result = await original(changes, **kw)
        if kw.get("source") == "env_file" and not edited:
            edited.append(True)
            edit(env_path, "WALLET_AUTOCOMPLETE_MINUTES", "90")  # the owner saves again meanwhile
        return result

    svc.apply = slow_apply  # type: ignore[method-assign]
    edit(env_path, "TRIAL_DAYS", "17")
    edit(env_path, "LOG_LEVEL", "LOUD")  # invalid: the file gets a "# ⚠" note, i.e. it is rewritten
    await mirror.sync()
    assert svc.current()["TRIAL_DAYS"] == 17
    assert svc.current()["WALLET_AUTOCOMPLETE_MINUTES"] == 90  # merged in the next round, not overwritten
    assert file_value(env_path, "WALLET_AUTOCOMPLETE_MINUTES") == "90"
    assert file_value(env_path, "TRIAL_DAYS") == "17"
    assert "# ⚠" in read(env_path) and svc.current()["LOG_LEVEL"] == "INFO"


async def test_database_outage_backs_off_instead_of_spinning(
    make_service: ServiceFactory, make_mirror: MirrorFactory, env_path: Path, db: ShimDatabase
) -> None:
    svc = await make_service()
    calls: list[float] = []
    original = svc.apply

    async def spy(changes: list[Change], **kw: Any) -> Any:
        if kw.get("source") == "env_file":
            calls.append(time.monotonic())
        return await original(changes, **kw)

    svc.apply = spy  # type: ignore[method-assign]
    mirror = await make_mirror(svc, retry_interval=30.0)
    db.fail_writes = True
    edit(env_path, "TRIAL_DAYS", "17")
    await asyncio.sleep(1.6)
    assert 1 <= len(calls) <= 4, len(calls)  # without backoff: one attempt every debounce (~30)
    assert file_value(env_path, "TRIAL_DAYS") == "17" and "# ⚠" not in read(env_path)
    db.fail_writes = False
    await wait_until(lambda: svc.current()["TRIAL_DAYS"] == 17, timeout=6)
    await wait_until(lambda: mirror._backoff == 0.0, timeout=3)
