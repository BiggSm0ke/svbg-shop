"""Backup → restore round trip on the test PostgreSQL with the real ``pg_dump`` / ``pg_restore``."""

from __future__ import annotations

import gzip
import json
import tarfile
from pathlib import Path

import asyncpg
import pytest

from svbg.core.crypto import Crypto, fingerprint, generate_key
from svbg.ops import restore as restore_mod
from svbg.ops.backup import BACKUP_NAME_RE, BackupError, create_backup, list_backups, rotate, split_parts
from svbg.ops.pgtools import PgTools, conninfo
from svbg.ops.restore import EnvMode, RestoreError, resolve_inputs, restore
from tests.dbkit import CountingDatabase
from tests.ops.conftest import FAST_KDF, PASSWORD, seed

pytestmark = pytest.mark.pg


def _env_text(key: str, dsn: str = "postgresql://svbg:old@db:5432/svbg") -> str:
    return f"# test\nBOT_TOKEN=123456:AAAA\nSECRET_KEY={key}\nDATABASE_URL={dsn}\nTRIAL_DAYS=5\n"


async def _tables(dsn: str) -> list[str]:
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch("select tablename from pg_tables where schemaname = 'public' order by 1")
        return [r["tablename"] for r in rows]
    finally:
        await conn.close()


async def _scalar(dsn: str, sql: str) -> object:
    conn = await asyncpg.connect(dsn)
    try:
        return await conn.fetchval(sql)
    finally:
        await conn.close()


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    (d / "media" / "ab").mkdir(parents=True)
    (d / "media" / "ab" / "abcdef.jpg").write_bytes(b"\xff\xd8 fake jpeg")
    return d


async def _backup(
    db: CountingDatabase,
    data_dir: Path,
    key: str,
    tools: PgTools,
    *,
    password: str | None = PASSWORD,
    reason: str = "manual",
) -> Path:
    env = data_dir / ".env"
    env.write_text(_env_text(key), "utf-8")
    result = await create_backup(
        db.pg_dsn,
        data_dir / "backups",
        reason=reason,
        password=password,
        env_path=env,
        media_dir=data_dir / "media",
        tools=tools,
        kdf=FAST_KDF,
    )
    return result.path


async def test_round_trip_encrypted(
    db: CountingDatabase, target_dsn: str, data_dir: Path, tmp_path: Path, secret_key: str, pg_tools: PgTools
) -> None:
    await seed(db, secret_key, users=30)
    env = data_dir / ".env"
    env.write_text(_env_text(secret_key), "utf-8")
    result = await create_backup(
        db.pg_dsn,
        data_dir / "backups",
        reason="daily",
        password=PASSWORD,
        env_path=env,
        media_dir=data_dir / "media",
        tools=pg_tools,
        kdf=FAST_KDF,
    )
    assert BACKUP_NAME_RE.match(result.path.name) and result.path.suffix == ".svbg"
    assert result.encrypted and result.has_env and result.content == "media"
    assert result.revision == "test_rev_1"
    assert result.rows >= 30 + 1 + 1 + 1
    raw = result.path.read_bytes()
    assert b"SECRET_KEY" not in raw and b"123456:AAAA" not in raw and secret_key.encode() not in raw
    assert list(data_dir.joinpath("backups").iterdir()) == [result.path], "no temporary leftovers"

    # Another host: empty database, no .env yet → the .env of the backup is written.
    host = tmp_path / "host"
    report = await restore(
        [result.path],
        target_dsn,
        data_dir=host,
        env_path=host / ".env",
        password=PASSWORD,
        tools=pg_tools,
    )
    assert report.tables >= 10 and report.rows == result.rows
    assert report.secrets_ok == 3 and report.secrets_failed == 0
    assert report.env_written and report.env_backup is None
    assert report.media_restored == 1
    assert (host / "media" / "ab" / "abcdef.jpg").read_bytes() == b"\xff\xd8 fake jpeg"
    assert f"SECRET_KEY={secret_key}" in (host / ".env").read_text("utf-8")
    assert await _scalar(target_dsn, "select count(*) from users") == 30
    assert await _scalar(target_dsn, "select version_num from alembic_version") == "test_rev_1"
    stored = await _scalar(target_dsn, "select value #>> '{}' from settings where key = 'REMNAWAVE_TOKEN'")
    assert Crypto([secret_key]).decrypt(str(stored)) == "panel-token-123"
    assert not [p for p in host.iterdir() if p.name.startswith(".restore-")], "workdir removed"


async def test_restore_refuses_non_empty_db_and_wipe_replaces_it(
    db: CountingDatabase, target_dsn: str, data_dir: Path, tmp_path: Path, secret_key: str, pg_tools: PgTools
) -> None:
    await seed(db, secret_key, users=3)
    path = await _backup(db, data_dir, secret_key, pg_tools)
    host = tmp_path / "host"
    host.mkdir()
    (host / ".env").write_text(_env_text(secret_key, "postgresql://svbg:new@db/svbg"), "utf-8")
    conn = await asyncpg.connect(target_dsn)
    await conn.execute("create table junk (id int); insert into junk values (1)")
    await conn.close()
    with pytest.raises(RestoreError, match="база не пустая"):
        await restore(
            [path], target_dsn, data_dir=host, env_path=host / ".env", password=PASSWORD, tools=pg_tools
        )
    assert await _tables(target_dsn) == ["junk"], "refused before touching anything"

    report = await restore(
        [path],
        target_dsn,
        data_dir=host,
        env_path=host / ".env",
        password=PASSWORD,
        wipe=True,
        tools=pg_tools,
    )
    assert report.wiped and "junk" not in await _tables(target_dsn)
    assert await _scalar(target_dsn, "select count(*) from users") == 3
    assert not report.env_written, "same SECRET_KEY: the host's .env is kept"


async def test_wrong_password_and_damage_never_touch_the_database(
    db: CountingDatabase, target_dsn: str, data_dir: Path, tmp_path: Path, secret_key: str, pg_tools: PgTools
) -> None:
    await seed(db, secret_key, users=2)
    path = await _backup(db, data_dir, secret_key, pg_tools)
    host = tmp_path / "host"
    kw = {"data_dir": host, "env_path": host / ".env", "tools": pg_tools}
    with pytest.raises(RestoreError, match="неверный пароль"):
        await restore([path], target_dsn, password="not the password", **kw)
    with pytest.raises(RestoreError, match="нужен пароль"):
        await restore([path], target_dsn, password=None, **kw)
    damaged = tmp_path / path.name
    blob = bytearray(path.read_bytes())
    blob[len(blob) // 2] ^= 0xFF
    damaged.write_bytes(bytes(blob))
    with pytest.raises(RestoreError, match="повреждён"):
        await restore([damaged], target_dsn, password=PASSWORD, **kw)
    truncated = tmp_path / ("t-" + path.name)
    truncated.write_bytes(path.read_bytes()[:-100])
    with pytest.raises(RestoreError, match="обрезан"):
        await restore([truncated], target_dsn, password=PASSWORD, **kw)
    foreign = tmp_path / "foreign.svbg"
    foreign.write_bytes(b"PK\x03\x04 not ours")
    with pytest.raises(RestoreError, match="не бэкап SvBG"):
        await restore([foreign], target_dsn, password=PASSWORD, **kw)
    assert await _tables(target_dsn) == []


async def test_parts_are_joined_and_a_missing_part_is_named(
    db: CountingDatabase, target_dsn: str, data_dir: Path, tmp_path: Path, secret_key: str, pg_tools: PgTools
) -> None:
    await seed(db, secret_key, users=50)
    path = await _backup(db, data_dir, secret_key, pg_tools)
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    parts = split_parts(path, max(1, path.stat().st_size // 3 + 1), downloads)
    assert [p.name for p in parts] == [f"{path.name}.part{i}of3" for i in (1, 2, 3)]
    assert b"".join(p.read_bytes() for p in parts) == path.read_bytes()
    assert resolve_inputs([parts[1]]) == parts, "any part finds its siblings"
    assert resolve_inputs([parts[2], parts[0], parts[1]]) == parts

    parts[1].unlink()
    with pytest.raises(RestoreError, match="нет части 2 из 3"):
        resolve_inputs([parts[0]])
    split_parts(path, max(1, path.stat().st_size // 3 + 1), downloads)
    host = tmp_path / "host"
    report = await restore(
        [parts[0]], target_dsn, data_dir=host, env_path=host / ".env", password=PASSWORD, tools=pg_tools
    )
    assert await _scalar(target_dsn, "select count(*) from users") == 50 and report.rows > 50


def test_resolve_inputs_errors(tmp_path: Path) -> None:
    whole = tmp_path / "svbg-20261002-040000-daily.svbg"
    whole.write_bytes(b"x")
    part = tmp_path / "other.svbg.part1of2"
    part.write_bytes(b"x")
    with pytest.raises(RestoreError, match="не найден"):
        resolve_inputs([tmp_path / "missing.svbg"])
    with pytest.raises(RestoreError, match="смешивать"):
        resolve_inputs([whole, part])
    with pytest.raises(RestoreError, match="один файл"):
        resolve_inputs([whole, whole])
    with pytest.raises(RestoreError, match="разным"):
        resolve_inputs([part, tmp_path / "third.svbg.part2of2"])
    with pytest.raises(RestoreError, match="не указан"):
        resolve_inputs([])


async def test_env_modes_with_a_different_secret_key(
    db: CountingDatabase, target_dsn: str, data_dir: Path, tmp_path: Path, secret_key: str, pg_tools: PgTools
) -> None:
    await seed(db, secret_key, users=1)
    path = await _backup(db, data_dir, secret_key, pg_tools)
    host = tmp_path / "host"
    host.mkdir()
    other = generate_key()
    host_env = _env_text(other, "postgresql://svbg:NEWPASS@db:5432/svbg")
    (host / ".env").write_text(host_env, "utf-8")
    kw = {"data_dir": host, "env_path": host / ".env", "password": PASSWORD, "tools": pg_tools}

    with pytest.raises(RestoreError, match="SECRET_KEY здесь другой"):
        await restore([path], target_dsn, **kw)
    assert await _tables(target_dsn) == [], "the .env decision comes before the database"

    report = await restore([path], target_dsn, env_mode=EnvMode.WRITE, **kw)
    assert report.env_written and report.env_backup is not None
    assert report.env_backup.read_text("utf-8") == host_env
    written = (host / ".env").read_text("utf-8")
    assert f"SECRET_KEY={secret_key}" in written
    assert "DATABASE_URL=postgresql://svbg:NEWPASS@db:5432/svbg" in written, "the host's DB stays"
    assert "svbg:old@" not in written
    assert report.secrets_ok == 3 and report.secrets_failed == 0


async def test_env_skip_reports_undecryptable_secrets(
    db: CountingDatabase, target_dsn: str, data_dir: Path, tmp_path: Path, secret_key: str, pg_tools: PgTools
) -> None:
    await seed(db, secret_key, users=1)
    path = await _backup(db, data_dir, secret_key, pg_tools)
    host = tmp_path / "host"
    host.mkdir()
    (host / ".env").write_text(_env_text(generate_key()), "utf-8")
    report = await restore(
        [path],
        target_dsn,
        data_dir=host,
        env_path=host / ".env",
        password=PASSWORD,
        env_mode=EnvMode.SKIP,
        tools=pg_tools,
    )
    assert not report.env_written
    assert report.secrets_ok == 0 and report.secrets_failed == 3
    assert any("SECRET_KEY" in w for w in report.warnings)


async def test_unencrypted_local_backup(
    db: CountingDatabase, target_dsn: str, data_dir: Path, tmp_path: Path, secret_key: str, pg_tools: PgTools
) -> None:
    await seed(db, secret_key, users=4)
    env = data_dir / ".env"
    env.write_text(_env_text(secret_key), "utf-8")
    result = await create_backup(
        db.pg_dsn,
        data_dir / "backups",
        password=None,
        env_path=env,
        media_dir=data_dir / "media",
        tools=pg_tools,
    )
    assert result.path.name.endswith(".tar.gz") and not result.encrypted and not result.has_env
    assert any(".env" in w for w in result.warnings)
    with gzip.open(result.path) as gz, tarfile.open(fileobj=gz, mode="r|") as tar:
        names = [m.name for m in tar]
    assert names == ["manifest.json", "db.dump", "content.zip"], "never a plaintext .env"
    host = tmp_path / "host"
    host.mkdir()
    (host / ".env").write_text(_env_text(secret_key), "utf-8")
    with pytest.raises(RestoreError, match=r"нет копии \.env"):
        await restore(
            [result.path], target_dsn, data_dir=host, env_path=host / ".env", env_mode=EnvMode.WRITE
        )
    report = await restore([result.path], target_dsn, data_dir=host, env_path=host / ".env", tools=pg_tools)
    assert report.secrets_ok == 3 and await _scalar(target_dsn, "select count(*) from users") == 4


async def test_manifest_records_key_fingerprint_and_counts(
    db: CountingDatabase, data_dir: Path, tmp_path: Path, secret_key: str, pg_tools: PgTools
) -> None:
    await seed(db, secret_key, users=7)
    path = await _backup(db, data_dir, secret_key, pg_tools, password=None)
    with gzip.open(path) as gz, tarfile.open(fileobj=gz, mode="r|") as tar:
        member = next(iter(tar))
        f = tar.extractfile(member)
        assert f is not None
        manifest = json.loads(f.read())
    assert manifest["format"] == "svbg-backup" and manifest["version"] == 1
    assert manifest["secret_key_fp"] == fingerprint(secret_key)
    assert manifest["tables"]["users"] == 7
    assert set(manifest["files"]) == {"db.dump", "content.zip"}
    assert secret_key not in json.dumps(manifest)


async def test_verification_failure_is_reported(
    db: CountingDatabase,
    target_dsn: str,
    data_dir: Path,
    tmp_path: Path,
    secret_key: str,
    pg_tools: PgTools,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed(db, secret_key, users=2)
    path = await _backup(db, data_dir, secret_key, pg_tools)
    real = restore_mod._count

    async def lying(conn: asyncpg.Connection, names: list[str]) -> dict[str, int]:
        counts = await real(conn, names)
        counts["users"] = counts.get("users", 0) + 1
        return counts

    monkeypatch.setattr(restore_mod, "_count", lying)
    host = tmp_path / "host"
    with pytest.raises(RestoreError, match=r"проверка не пройдена.*users: 3 ≠ 2"):
        await restore(
            [path], target_dsn, data_dir=host, env_path=host / ".env", password=PASSWORD, tools=pg_tools
        )


async def test_failures_of_the_dump_are_owner_readable(
    db: CountingDatabase, data_dir: Path, tmp_path: Path, pg_tools: PgTools
) -> None:
    empty_bin = tmp_path / "nobin"
    empty_bin.mkdir()
    with pytest.raises(BackupError, match="pg_dump не найден"):
        await create_backup(db.pg_dsn, data_dir / "backups", tools=PgTools(empty_bin))
    broken = "postgresql://svbg:svbg@127.0.0.1:1/nope"
    with pytest.raises(BackupError, match="база данных недоступна") as info:
        await create_backup(broken, data_dir / "backups", tools=pg_tools)
    assert "svbg:svbg" not in str(info.value)
    assert not [p for p in (data_dir / "backups").iterdir() if p.name.startswith(".tmp-")]


def test_conninfo_keeps_the_password_off_the_command_line() -> None:
    url, env = conninfo("postgresql+asyncpg://svbg:p%40ss@db:5432/svbg?sslmode=disable")
    assert url == "postgresql://svbg@db:5432/svbg?sslmode=disable"
    assert env == {"PGPASSWORD": "p@ss"}
    assert conninfo("postgres://db/svbg") == ("postgresql://db/svbg", {})


def test_rotation_keeps_the_newest(tmp_path: Path) -> None:
    names = [
        "svbg-20261001-040000-daily.svbg",
        "svbg-20261002-040000-daily.svbg",
        "svbg-20261003-040000-daily.svbg",
        "svbg-20261003-120000-manual.tar.gz",
        "notes.txt",
        "svbg-2026-bad.svbg",
    ]
    for n in names:
        (tmp_path / n).write_bytes(b"x")
    assert [p.name for p in list_backups(tmp_path)][:2] == [
        "svbg-20261003-120000-manual.tar.gz",
        "svbg-20261003-040000-daily.svbg",
    ]
    removed = rotate(tmp_path, 2)
    assert sorted(removed) == ["svbg-20261001-040000-daily.svbg", "svbg-20261002-040000-daily.svbg"]
    left = sorted(p.name for p in tmp_path.iterdir())
    assert left == ["notes.txt", "svbg-2026-bad.svbg", names[2], names[3]]
