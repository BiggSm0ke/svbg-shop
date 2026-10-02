"""BackupService: daily moment (hot settings, catch-up, restart), Telegram parts, alerts, one at a time."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from svbg.core import clock
from svbg.core.attention import AttentionService
from svbg.ops.backup import BackupBusyError, BackupError, BackupService
from svbg.ops.pgtools import PgTools
from svbg.ops.state import K_BACKUP, MetaState
from tests.dbkit import CountingDatabase
from tests.ops.conftest import FAST_KDF, PASSWORD, seed

pytestmark = pytest.mark.pg


@dataclass
class FakeDelivery:
    sent: list[tuple[list[str], list[str], list[int]]] = field(default_factory=list)
    fail: Exception | None = None

    async def send(self, files: Sequence[Path], captions: Sequence[str]) -> int:
        if self.fail is not None:
            raise self.fail
        self.sent.append(([f.name for f in files], list(captions), [f.stat().st_size for f in files]))
        return 1


@dataclass
class Notes:
    items: list[tuple[str, bool]] = field(default_factory=list)

    async def __call__(self, report: Any, high: bool) -> None:
        self.items.append((report.html(), high))


@pytest.fixture
def frozen() -> Iterator[clock.FrozenClock]:
    fc = clock.FrozenClock(datetime(2026, 10, 2, 0, 0, tzinfo=UTC))
    clock.set_clock(fc)
    try:
        yield fc
    finally:
        clock.reset_clock()


def _service(
    db: CountingDatabase,
    tmp_path: Path,
    tools: PgTools,
    settings: dict[str, Any],
    *,
    delivery: FakeDelivery | None = None,
    notes: Notes | None = None,
    part_size: int = 45 * 1024 * 1024,
    dsn: str | None = None,
) -> BackupService:
    env = tmp_path / "data" / ".env"
    env.parent.mkdir(parents=True, exist_ok=True)
    if not env.exists():
        env.write_text("SECRET_KEY=x\n", "utf-8")
    return BackupService(
        dsn=dsn or db.pg_dsn,
        data_dir=tmp_path / "data",
        env_path=env,
        settings=lambda: settings,
        state=MetaState(db),
        delivery=delivery,
        notify=notes,
        attention=AttentionService(db),
        tools=tools,
        kdf=FAST_KDF,
        part_size=part_size,
        key_fingerprint=lambda: "abcd1234",
    )


async def test_run_sends_parts_and_records_state(
    db: CountingDatabase, tmp_path: Path, pg_tools: PgTools, secret_key: str
) -> None:
    await seed(db, secret_key, users=40)
    delivery, notes = FakeDelivery(), Notes()
    settings = {"BACKUP_PASSWORD": PASSWORD, "BACKUP_KEEP": 2, "TIMEZONE": "Europe/Moscow"}
    svc = _service(db, tmp_path, pg_tools, settings, delivery=delivery, notes=notes, part_size=20_000)
    result = await svc.run("manual")
    names, captions, sizes = delivery.sent[0]
    assert len(names) >= 2 and all(n.startswith(result.path.name + ".part") for n in names)
    assert all(s <= 20_000 for s in sizes) and sum(sizes) == result.size
    assert captions[0].startswith("💾 Бэкап") and f"часть 1/{len(names)}" in captions[0]
    assert "svbg restore" in captions[-1]
    assert not notes.items, "a delivered backup needs no extra message"
    state = await svc.status()
    assert state["last"]["file"] == result.path.name and state["last"]["sent"] == 1
    assert not [p for p in result.path.parent.iterdir() if p.name.startswith(".tmp-")], "parts removed"

    for _ in range(2):
        await svc.run("manual")
    assert len(list((tmp_path / "data" / "backups").glob("svbg-*"))) <= 2, "BACKUP_KEEP"


async def test_without_password_nothing_goes_to_telegram(
    db: CountingDatabase, tmp_path: Path, pg_tools: PgTools
) -> None:
    delivery, notes = FakeDelivery(), Notes()
    svc = _service(db, tmp_path, pg_tools, {}, delivery=delivery, notes=notes)
    result = await svc.run()
    assert not result.encrypted and delivery.sent == []
    ((text, high),) = notes.items
    assert "BACKUP_PASSWORD" in text and "без шифрования" in text and high


async def test_upload_failure_keeps_the_local_file(
    db: CountingDatabase, tmp_path: Path, pg_tools: PgTools
) -> None:
    token = "123456789:" + "ABCDEFGHIJ" * 3 + "abcde"
    delivery, notes = FakeDelivery(fail=RuntimeError(f"network down, bot {token}")), Notes()
    svc = _service(db, tmp_path, pg_tools, {"BACKUP_PASSWORD": PASSWORD}, delivery=delivery, notes=notes)
    result = await svc.run()
    assert result.path.exists()
    ((text, high),) = notes.items
    assert "не удалось отправить в Telegram" in text and high
    assert "ABCDEFGHIJ" not in text


async def test_failure_raises_attention_and_success_resolves_it(
    db: CountingDatabase, tmp_path: Path, pg_tools: PgTools
) -> None:
    notes = Notes()
    broken = _service(
        db, tmp_path, pg_tools, {}, notes=notes, dsn="postgresql://svbg:topsecret@127.0.0.1:1/nope"
    )
    with pytest.raises(BackupError):
        await broken.run()
    items = await AttentionService(db).open_items()
    assert [i.dedup_key for i in items] == ["ops:backup"] and items[0].fix_action == "screen:ops"
    ((text, high),) = notes.items
    assert text.startswith("🔴 <b>Бэкап не удался</b>") and high and "topsecret" not in text
    assert (await MetaState(db).get(K_BACKUP))["last_error"]["error"]

    ok = _service(db, tmp_path, pg_tools, {})
    await ok.run()
    assert await AttentionService(db).open_items() == []


async def test_one_backup_at_a_time(db: CountingDatabase, tmp_path: Path, pg_tools: PgTools) -> None:
    svc = _service(db, tmp_path, pg_tools, {})
    first = asyncio.create_task(svc.run())
    await asyncio.sleep(0)
    assert svc.running
    with pytest.raises(BackupBusyError):
        await svc.run()
    await first
    assert not svc.running


async def test_daily_tick_hot_settings_catch_up_and_restart(
    db: CountingDatabase, tmp_path: Path, pg_tools: PgTools, frozen: clock.FrozenClock
) -> None:
    settings: dict[str, Any] = {"BACKUP_AT": "04:00", "TIMEZONE": "Europe/Moscow", "BACKUP_ENABLED": True}
    svc = _service(db, tmp_path, pg_tools, settings)
    frozen.set(datetime(2026, 10, 2, 0, 59, tzinfo=UTC))  # 03:59 Moscow
    mark = db.queries
    assert await svc.tick() is False
    assert db.queries == mark, "no SQL before the moment"
    frozen.set(datetime(2026, 10, 2, 1, 0, 30, tzinfo=UTC))  # 04:00:30 Moscow
    assert await svc.tick() is True
    mark = db.queries
    assert await svc.tick() is False
    assert db.queries == mark, "no SQL for the rest of the day"

    restarted = _service(db, tmp_path, pg_tools, settings)
    assert await restarted.tick() is False, "a restart the same day does not repeat the backup"

    # Next day the owner moves the backup to 06:30 and the zone to UTC: applies at once.
    settings.update(BACKUP_AT="06:30", TIMEZONE="UTC")
    frozen.set(datetime(2026, 10, 3, 6, 29, tzinfo=UTC))
    assert await restarted.tick() is False
    frozen.set(datetime(2026, 10, 3, 6, 31, tzinfo=UTC))
    assert await restarted.tick() is True

    # Down at the moment and back 5 h later: the day is skipped (marked), not run late.
    frozen.set(datetime(2026, 10, 4, 11, 31, tzinfo=UTC))
    late = _service(db, tmp_path, pg_tools, settings)
    assert await late.tick() is False
    assert (await MetaState(db).get(K_BACKUP))["day"] == "2026-10-04"

    settings["BACKUP_ENABLED"] = False
    frozen.set(datetime(2026, 10, 5, 7, 0, tzinfo=UTC))
    assert await late.tick() is False
    backups = list((tmp_path / "data" / "backups").glob("svbg-*-daily.*"))
    assert len(backups) == 2
