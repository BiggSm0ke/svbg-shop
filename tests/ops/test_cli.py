"""``svbg backup`` / ``svbg restore`` commands end to end (real pg_dump / pg_restore, another database)."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import asyncpg
import pytest

from svbg.ops.cli import add_commands
from svbg.ops.pgtools import PgTools
from tests.dbkit import CountingDatabase
from tests.ops.conftest import PASSWORD, seed

pytestmark = pytest.mark.pg


@dataclass
class Io:
    out: list[str] = field(default_factory=list)
    err: list[str] = field(default_factory=list)

    def say(self, text: str = "") -> None:
        self.out.append(text)

    def warn(self, text: str) -> None:
        self.err.append(text)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="svbg")
    add_commands(p.add_subparsers(dest="command", required=True))
    return p


async def run(argv: list[str], environ: dict[str, str]) -> tuple[int, Io]:
    io = Io()
    args = parser().parse_args(argv)
    code = await asyncio.to_thread(args.handler, args, environ, io)
    return int(code), io


@pytest.fixture
def environ(tmp_path: Path, pg_tools: PgTools, db: CountingDatabase, secret_key: str) -> dict[str, str]:
    data = tmp_path / "data"
    data.mkdir()
    (data / ".env").write_text(
        f"SECRET_KEY={secret_key}\nDATABASE_URL={db.pg_dsn}\nBACKUP_PASSWORD={PASSWORD}\n", "utf-8"
    )
    env = {"DATA_DIR": str(data)}
    if pg_tools.bindir is not None:
        env["SVBG_PG_BIN"] = str(pg_tools.bindir)
    return env


@pytest.fixture(autouse=True)
def _pg_bin(environ: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    if "SVBG_PG_BIN" in environ:
        monkeypatch.setenv("SVBG_PG_BIN", environ["SVBG_PG_BIN"])


async def test_backup_then_restore_into_another_database(
    db: CountingDatabase, target_dsn: str, environ: dict[str, str], tmp_path: Path, secret_key: str
) -> None:
    await seed(db, secret_key, users=12)
    code, io = await run(["backup", "--reason", "pre_update", "--keep", "3"], environ)
    assert code == 0, io.err
    backups = list((tmp_path / "data" / "backups").glob("svbg-*-pre_update.svbg"))
    assert len(backups) == 1 and str(backups[0]) in io.out[0]
    assert PASSWORD not in "\n".join(io.out + io.err)

    wrong = tmp_path / "wrong.txt"
    wrong.write_text("nope nope nope\n", "utf-8")
    code, io = await run(
        ["restore", str(backups[0]), "--password-file", str(wrong), "--dsn", target_dsn], environ
    )
    assert code == 1 and "неверный пароль" in io.err[0]

    code, io = await run(
        ["restore", str(backups[0]), "--dsn", target_dsn], {**environ, "SVBG_BACKUP_PASSWORD": PASSWORD}
    )
    assert code == 0, io.err
    assert any("База восстановлена и проверена" in line for line in io.out)
    assert any("Секреты: расшифровано 3 из 3" in line for line in io.out)
    conn = await asyncpg.connect(target_dsn)
    try:
        assert await conn.fetchval("select count(*) from users") == 12
    finally:
        await conn.close()

    code, io = await run(
        ["restore", str(backups[0]), "--dsn", target_dsn, "--wipe"],
        {**environ, "SVBG_BACKUP_PASSWORD": PASSWORD},
    )
    assert code == 2 and "--wipe требует подтверждения" in io.err[0]
    code, io = await run(
        ["restore", str(backups[0]), "--dsn", target_dsn, "--wipe", "--yes"],
        {**environ, "SVBG_BACKUP_PASSWORD": PASSWORD},
    )
    assert code == 0, io.err


async def test_encrypted_backup_without_a_password_is_a_usage_error(
    db: CountingDatabase, target_dsn: str, environ: dict[str, str], tmp_path: Path
) -> None:
    code, io = await run(["backup"], environ)
    assert code == 0, io.err
    path = next((tmp_path / "data" / "backups").glob("*.svbg"))
    code, io = await run(["restore", str(path), "--dsn", target_dsn], environ)
    assert code == 2 and "SVBG_BACKUP_PASSWORD" in io.err[0]


async def test_no_password_backup_and_config_errors(
    db: CountingDatabase, environ: dict[str, str], tmp_path: Path
) -> None:
    code, io = await run(["backup", "--no-password"], environ)
    assert code == 0 and any("Без пароля" in line for line in io.err)
    assert list((tmp_path / "data" / "backups").glob("*.tar.gz"))

    (tmp_path / "data" / ".env").write_text("TRIAL_DAYS=3\n", "utf-8")
    code, io = await run(["backup"], environ)
    assert code == 78 and "DATABASE_URL не задан" in io.err[0]
    code, io = await run(["restore", "x.svbg"], environ)
    assert code == 78

    empty = tmp_path / "empty.txt"
    empty.write_text("\n", "utf-8")
    code, io = await run(["backup", "--dsn", "postgresql://a@b/c", "--password-file", str(empty)], environ)
    assert code == 2 and "файл пароля пуст" in io.err[0]
    code, io = await run(["restore", str(tmp_path / "missing.svbg"), "--dsn", "postgresql://a@b/c"], environ)
    assert code == 1 and "не найден" in io.err[0]


async def test_backup_before_migrate(
    db: CountingDatabase, environ: dict[str, str], tmp_path: Path, secret_key: str
) -> None:
    from svbg.ops.cli import backup_before_migrate

    env_path = tmp_path / "data" / ".env"
    assert await backup_before_migrate(db.pg_dsn, env_path, environ, head="h2") is None, "no alembic_version"
    await seed(db, secret_key, users=1)  # alembic_version = test_rev_1
    assert await backup_before_migrate(db.pg_dsn, env_path, environ, head="test_rev_1") is None, "up to date"
    path = await backup_before_migrate(db.pg_dsn, env_path, environ, head="h2")
    assert path is not None and path.name.endswith("-pre_migrate.svbg"), "encrypted with BACKUP_PASSWORD"
