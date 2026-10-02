"""The command line of the integrated build: module commands, ``import``, the pre-migrate backup step."""

from __future__ import annotations

import asyncio
import io
from collections.abc import Callable
from pathlib import Path

import pytest

import svbg.__main__ as cli
from svbg.app_modules import full_registry
from svbg.boot.envfile import EnvDocument, write_atomic
from svbg.core.crypto import generate_key
from svbg.db import migrations
from tests.e2e.conftest import apply_sql, schema_sql

Run = Callable[..., tuple[int, str, str]]


@pytest.fixture
def run_cli(tmp_path: Path) -> Run:
    def run(*argv: str, environ: dict[str, str] | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        env = {"DATA_DIR": str(tmp_path / "data")} if environ is None else environ
        code = cli.main(list(argv), environ=env, stdout=out, stderr=err, configure_logging=False)
        return code, out.getvalue(), err.getvalue()

    return run


def _commands(parser: object) -> set[str]:
    sub = next(a for a in parser._actions if a.dest == "command")  # type: ignore[attr-defined]
    return set(sub.choices)


def test_parser_has_every_module_command() -> None:
    names = _commands(cli.build_parser())
    assert {"backup", "restore", "cutover", "pay", "lte", "import"} <= names
    # a core command does not import the modules (fast healthcheck), the module commands still parse
    assert "lte" not in _commands(cli.build_parser(["health"]))
    assert "lte" in _commands(cli.build_parser(["lte", "release", "--dry-run"]))


def test_import_needs_a_source(tmp_path: Path, run_cli: Run) -> None:
    env = tmp_path / "data" / ".env"
    env.parent.mkdir(parents=True)
    write_atomic(env, f"DATABASE_URL=postgresql://u:p@127.0.0.1:1/x\nSECRET_KEY={generate_key()}\n")
    code, _, err = run_cli("import", "bedolaga")
    assert code == cli.EXIT_USAGE and "Bedolaga" in err


def test_env_init_writes_module_keys(tmp_path: Path, run_cli: Run) -> None:
    code, _, _ = run_cli("env", "init")
    assert code == 0
    doc = EnvDocument.parse((tmp_path / "data" / ".env").read_text(encoding="utf-8"))
    for key in (
        "BACKUP_ENABLED",
        "REFERRAL_ENABLED",
        "LTE_ENABLED",
        "IP_GUARD_ENABLED",
        "IMPORT_SHADOW_ENABLED",
    ):
        assert key in full_registry()
        assert doc.get(key) is not None, key
    code, out, _ = run_cli("set", "IP_GUARD_ENABLED=true")
    assert code == 0, out


@pytest.mark.pg
async def test_import_settings_dry_run_and_migrate_without_backup(
    pg_dsn: str, tmp_path: Path, run_cli: Run
) -> None:
    await apply_sql(pg_dsn, schema_sql())
    env = tmp_path / "data" / ".env"
    env.parent.mkdir(parents=True)
    write_atomic(env, f"DATABASE_URL={pg_dsn}\nSECRET_KEY={generate_key()}\n")

    # an up-to-date schema: no pre-migrate backup (and no pg_dump needed)
    code, out, err = await asyncio.to_thread(run_cli, "migrate")
    assert code == 0, err
    assert str(migrations.head_revision()) in out and "Бэкап" not in out
    assert not (tmp_path / "data" / "backups").exists()

    bedolaga_env = tmp_path / "bedolaga.env"
    bedolaga_env.write_text("BOT_TOKEN=123:abc\nTRIAL_DURATION_DAYS=5\nSUPPORT_USERNAME=@help\n", "utf-8")
    code, out, err = await asyncio.to_thread(run_cli, "import", "settings", "--env", str(bedolaga_env))
    assert code == 0, err
    assert "без записи" in out
