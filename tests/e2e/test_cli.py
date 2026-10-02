"""``python -m svbg``: env init/render, set, show-key, migrate, health, owner-link (with the running bot)."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
import socket
from collections.abc import Callable
from pathlib import Path

import asyncpg
import pytest

import svbg.__main__ as cli
from svbg.boot.envfile import EnvDocument, write_atomic
from svbg.core.crypto import generate_key
from svbg.core.settings import core_registry
from svbg.db import migrations
from tests.e2e.conftest import AppEnv, StartApp, make_database

Run = Callable[..., tuple[int, str, str]]


@pytest.fixture
def run_cli(tmp_path: Path) -> Run:
    def run(*argv: str, environ: dict[str, str] | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        env = {"DATA_DIR": str(tmp_path / "data")} if environ is None else environ
        code = cli.main(list(argv), environ=env, stdout=out, stderr=err, configure_logging=False)
        return code, out.getvalue(), err.getvalue()

    return run


def _env(tmp_path: Path) -> Path:
    return tmp_path / "data" / ".env"


def test_env_init_creates_full_file_with_secret_key(tmp_path: Path, run_cli: Run) -> None:
    code, out, _ = run_cli("env", "init")
    assert code == 0, out
    text = _env(tmp_path).read_text(encoding="utf-8")
    doc = EnvDocument.parse(text)
    for defn in core_registry().all():
        if defn.in_file:
            assert doc.get(defn.key) is not None, f"{defn.key} missing from the generated .env"
    assert doc.get("TRIAL_DAYS") == "3"
    assert len(doc.get("SECRET_KEY") or "") == 44
    assert "# ── Продажи и триал" in text
    assert "SECRET_KEY" in out and "show-key" in out

    # Idempotent: a second run keeps the file byte-for-byte (and the key).
    code, _, _ = run_cli("env", "init")
    assert code == 0
    assert _env(tmp_path).read_text(encoding="utf-8") == text


def test_env_init_keeps_owner_values_and_lines(tmp_path: Path, run_cli: Run) -> None:
    path = _env(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("# мой комментарий\nTRIAL_DAYS=10\nMY_OWN_KEY=hello\n", encoding="utf-8")
    assert run_cli("env", "init")[0] == 0
    text = path.read_text(encoding="utf-8")
    doc = EnvDocument.parse(text)
    assert doc.get("TRIAL_DAYS") == "10"
    assert doc.get("MY_OWN_KEY") == "hello"
    assert "# мой комментарий" in text


def test_env_render_stdout_masks_secrets(tmp_path: Path, run_cli: Run) -> None:
    assert run_cli("env", "init")[0] == 0
    key = EnvDocument.parse(_env(tmp_path).read_text(encoding="utf-8")).get("SECRET_KEY")
    assert key
    code, out, _ = run_cli("env", "render", "--stdout")
    assert code == 0
    assert key not in out
    assert "TRIAL_DAYS=3" in out
    code, out, _ = run_cli("env", "render", "--stdout", "--show-secrets")
    assert key in out


def test_env_render_stdout_masks_unknown_and_commented_secrets(tmp_path: Path, run_cli: Run) -> None:
    assert run_cli("env", "init")[0] == 0
    path = _env(tmp_path)
    secrets = {
        "POSTGRES_PASSWORD": "pg-Pa55word-xyz",
        "SOME_API_KEY": "api-k3y-value-123",
        "MY_WEBHOOK_SECRET": "whsec-abcdef-987",
        "LEGACY_DSN": "postgresql://svbg:dsnpass-42@db:5432/svbg",
    }
    extra = [f"{k}={v}" for k, v in secrets.items()]
    extra += [
        "OTHER_URL=postgresql://bot:urlpass-77@db/x",
        "# OLD_BOT_TOKEN=commented-tok-555",
        "PLAIN_SETTING=visible-value",
    ]
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write("\n" + "\n".join(extra) + "\n")
    code, out, _ = run_cli("env", "render", "--stdout")
    assert code == 0
    for leaked in (*secrets.values(), "dsnpass-42", "urlpass-77", "commented-tok-555"):
        assert leaked not in out
    assert 'POSTGRES_PASSWORD="***"' in out
    assert 'OTHER_URL="postgresql://bot:***@db/x"' in out
    assert "# OLD_BOT_TOKEN=***" in out
    assert "PLAIN_SETTING=visible-value" in out
    assert "TRIAL_DAYS=3" in out
    code, out, _ = run_cli("env", "render", "--stdout", "--show-secrets")
    assert code == 0
    for value in secrets.values():
        assert value in out
    assert "commented-tok-555" in out


def test_set_validates_and_writes_atomically(tmp_path: Path, run_cli: Run) -> None:
    assert run_cli("env", "init")[0] == 0
    path = _env(tmp_path)
    code, out, _ = run_cli("set", "TRIAL_DAYS=5")
    assert code == 0 and "TRIAL_DAYS" in out
    assert EnvDocument.parse(path.read_text(encoding="utf-8")).get("TRIAL_DAYS") == "5"

    before = path.read_text(encoding="utf-8")
    for argv, fragment in (
        (("set", "TRIAL_DAYS=abc"), "не подходит"),
        (("set", "TRIAL_DAYS=9999"), "не подходит"),
        (("set", "NO_SUCH_KEY=1"), "Неизвестная настройка"),
        (("set", "SECRET_KEY=x"), "не меняется"),
        (("set", "a", "b", "c"), "Использование"),
    ):
        code, _, err = run_cli(*argv)
        assert code == cli.EXIT_USAGE, argv
        assert fragment in err, (argv, err)
    assert path.read_text(encoding="utf-8") == before

    # "KEY VALUE" form, secrets are never echoed.
    code, out, _ = run_cli("set", "REMNAWAVE_TOKEN", "tok-very-secret-123")
    assert code == 0
    assert "tok-very-secret-123" not in out
    assert EnvDocument.parse(path.read_text(encoding="utf-8")).get("REMNAWAVE_TOKEN") == "tok-very-secret-123"


def test_show_key(tmp_path: Path, run_cli: Run) -> None:
    code, _, err = run_cli("show-key")
    assert code == cli.EXIT_FAIL and "env init" in err
    assert run_cli("env", "init")[0] == 0
    key = EnvDocument.parse(_env(tmp_path).read_text(encoding="utf-8")).get("SECRET_KEY")
    code, out, err = run_cli("show-key")
    assert code == 0
    assert out.strip() == key
    assert "менеджере паролей" in err


def test_migrate_sql_and_missing_dsn(run_cli: Run) -> None:
    code, out, _ = run_cli("migrate", "--sql")
    assert code == 0
    assert "CREATE TABLE users (" in out
    code, _, err = run_cli("migrate")
    assert code == cli.EXIT_CONFIG
    assert "DATABASE_URL" in err


def test_broken_secret_key_is_a_config_error(tmp_path: Path, run_cli: Run) -> None:
    path = _env(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("SECRET_KEY=not-a-fernet-key\n", encoding="utf-8")
    code, _, err = run_cli("env", "init")
    assert code == cli.EXIT_CONFIG
    assert "SECRET_KEY" in err
    assert path.read_text(encoding="utf-8") == "SECRET_KEY=not-a-fernet-key\n"  # never replaced silently


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.mark.pg
async def test_health_against_running_app(start_app: StartApp, run_cli: Run) -> None:
    app = await start_app()
    assert app.web is not None
    code, out, _ = await asyncio.to_thread(run_cli, "health", environ={"SVBG_WEB_PORT": str(app.web.port)})
    assert code == 0 and "ok" in out.lower()
    code, _, _ = await asyncio.to_thread(
        run_cli, "health", "--ready", environ={"SVBG_WEB_PORT": str(app.web.port)}
    )
    assert code == 0
    code, _, err = await asyncio.to_thread(
        run_cli, "health", "--timeout", "1", environ={"SVBG_WEB_PORT": str(_free_port())}
    )
    assert code == cli.EXIT_FAIL and "unhealthy" in err
    code, _, _ = await asyncio.to_thread(run_cli, "health", environ={"SVBG_WEB_PORT": "nope"})
    assert code == cli.EXIT_CONFIG


@pytest.mark.pg
async def test_migrate_online(pg_dsn: str, tmp_path: Path, run_cli: Run) -> None:
    path = _env(tmp_path)
    path.parent.mkdir(parents=True)
    write_atomic(path, f"DATABASE_URL={pg_dsn}\nSECRET_KEY={generate_key()}\n")
    code, out, err = await asyncio.to_thread(run_cli, "migrate")
    assert code == 0, err
    assert str(migrations.head_revision()) in out


@pytest.mark.pg
async def test_migrate_reports_unreachable_database_without_password(tmp_path: Path, run_cli: Run) -> None:
    path = _env(tmp_path)
    path.parent.mkdir(parents=True)
    write_atomic(
        path, f"DATABASE_URL=postgresql://svbg:Pw0rdLeak42@127.0.0.1:1/svbg\nSECRET_KEY={generate_key()}\n"
    )
    code, _, err = await asyncio.to_thread(run_cli, "migrate")
    assert code == cli.EXIT_FAIL
    assert "Pw0rdLeak42" not in err


@pytest.mark.pg
async def test_owner_link_end_to_end(
    start_app: StartApp, app_env: AppEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("svbg.tg.setup.owner")
    monkeypatch.setattr(cli, "_database", make_database)
    app_env.write(OWNER_IDS=None)
    tg = app_env.tg
    app = await start_app()

    newcomer = 8008
    tg.push_message(newcomer, "/start")
    gate = await tg.wait_for("sendMessage", lambda c: c.params.get("chat_id") == newcomer, timeout=10)
    assert "настраивается" in gate.params["text"]

    out, err = io.StringIO(), io.StringIO()
    environ = {"DATA_DIR": str(app_env.data_dir)}
    code = await asyncio.to_thread(
        cli.main, ["owner-link"], environ=environ, stdout=out, stderr=err, configure_logging=False
    )
    assert code == 0, err.getvalue()
    link = out.getvalue().strip()
    match = re.fullmatch(r"https://t\.me/svbg_e2e_bot\?start=setup_([A-Za-z0-9_-]+)", link)
    assert match, link  # the bot username came from getMe on the (fake) Telegram
    code_value = match.group(1)

    conn = await asyncpg.connect(app_env.dsn)
    try:
        raw = await conn.fetchval("select value::text from config_meta where key = 'owner_link'")
    finally:
        await conn.close()
    stored = json.loads(raw)
    assert stored["hash"] == hashlib.sha256(code_value.encode()).hexdigest()
    assert code_value not in raw  # only the hash is stored

    start = len(tg.calls)
    tg.push_message(newcomer, f"/start setup_{code_value}")
    welcome = await tg.wait_for(
        "sendMessage", lambda c: c.params.get("chat_id") == newcomer, timeout=10, start=start
    )
    assert "владелец" in welcome.params["text"].lower()
    assert app.users is not None
    assert newcomer in await app.users.owner_ids()

    # Single use: the same link does not work twice.
    start = len(tg.calls)
    tg.push_message(9009, f"/start setup_{code_value}")
    again = await tg.wait_for(
        "sendMessage", lambda c: c.params.get("chat_id") == 9009, timeout=10, start=start
    )
    assert "недействительна" in again.params["text"]
