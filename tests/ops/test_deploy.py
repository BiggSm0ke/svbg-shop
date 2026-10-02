"""Deploy files: Dockerfile/compose invariants, script syntax, installer helpers (bash, sourced)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"
SH = shutil.which("dash") or shutil.which("sh")


def _find_bash() -> str | None:
    # On Windows prefer Git's bash next to sh: System32\bash.exe is the WSL launcher.
    if SH is not None:
        sibling = Path(SH).with_name("bash.exe" if sys.platform == "win32" else "bash")
        if sibling.exists():
            return str(sibling)
    found = shutil.which("bash")
    if found and "system32" not in found.lower():
        return found
    return None


BASH = _find_bash()
needs_sh = pytest.mark.skipif(SH is None, reason="no POSIX shell")
needs_bash = pytest.mark.skipif(BASH is None, reason="no bash")


def _posix(path: Path) -> str:
    """A path the shell understands (msys on Windows: /c/...)."""
    if sys.platform != "win32":
        return str(path)
    drive, rest = os.path.splitdrive(str(path.resolve()))
    return f"/{drive.rstrip(':').lower()}{rest.replace(os.sep, '/')}"


def test_dockerfile_invariants() -> None:
    text = (ROOT / "Dockerfile").read_text("utf-8")
    assert "python:3.13-slim" in text
    assert "astral-sh/uv" in text and "uv sync --locked --no-dev" in text, "uv multi-stage, locked deps"
    assert re.search(r"^FROM .* AS build$", text, re.M) and text.count("\nFROM ") == 2
    assert "tini" in text and 'ENTRYPOINT ["/usr/bin/tini", "--"]' in text
    # The panel's PostgreSQL is 18 and pg_dump refuses newer servers: the client must be >= 18.
    m = re.search(r"^ARG PG_CLIENT=(\d+)$", text, re.M)
    assert m and int(m.group(1)) >= 18
    assert "postgresql-client-${PG_CLIENT}" in text and "/usr/lib/postgresql/${PG_CLIENT}/bin" in text
    assert "\nUSER 1000:1000\n" in text, "non-root"
    assert "HEALTHCHECK" in text and '"python", "-m", "svbg", "health"' in text
    assert "DATA_DIR=/app/data" in text
    ignore = (ROOT / ".dockerignore").read_text("utf-8").split()
    assert {".env", "data", ".git", ".venv", "tests"} <= set(ignore), "secrets and junk stay out of the image"


def test_compose_example_invariants() -> None:
    text = (DEPLOY / "docker-compose.yml").read_text("utf-8")
    assert "name: svbg" in text and "container_name: svbg-shop" in text and "hostname: svbg-shop" in text
    assert "image: svbg-shop:local" in text, "built locally, there is no public image"
    assert "./data:/app/data" in text and "env_file:" not in text.replace("# env_file:", "")
    assert '"127.0.0.1:8080:8080"' in text, "only loopback"
    assert "postgres:17-alpine" in text and "POSTGRES_PASSWORD_FILE: /run/secrets/pg" in text
    assert "condition: service_healthy" in text and "pg_isready" in text
    assert "file: ./data/.pg_password" in text
    tagged = [line for line in text.splitlines() if "svbg:remnawave-network" in line]
    assert len(tagged) == 4, "every remnawave-network line is marked"
    assert "\t" not in text


def test_host_cli_has_no_bashisms() -> None:
    text = (DEPLOY / "svbg").read_text("utf-8")
    assert text.startswith("#!/bin/sh\n") and "set -eu" in text
    for bashism in ("[[", "function ", "local ", "source ", "$'", "<<<", "echo -e", "${!"):
        assert bashism not in text, f"svbg: {bashism!r}"
    assert "\r\n" not in text
    assert "/opt/svbg" in text and "install.conf" in text and "--update" in text


def test_installer_header_and_hygiene() -> None:
    text = (DEPLOY / "install.sh").read_text("utf-8")
    assert text.startswith("#!/usr/bin/env bash\n") and "set -Eeuo pipefail" in text
    assert "\r\n" not in text
    assert "set -x" not in text, "tracing would print secrets"
    assert "/var/log/svbg-install.log" in text
    assert 'docker build -t "$IMAGE"' in text and "IMAGE=svbg-shop:local" in text
    # Secrets never go through `run` (it logs its argv) and never into argv of curl/jq/awk.
    for secret in (
        "BOT_TOKEN",
        "RW_TOKEN",
        "ADMIN_PASS",
        "RW_AUTH",
        "HOOK_SECRET",
        "PG_PASS",
        "DATABASE_URL",
    ):
        assert not re.search(rf"^\s*run .*\${{?{secret}\b", text, re.M), f"{secret} passed to run"
    # Remnawave contract: endpoints and forwarded headers for the direct http://127.0.0.1:3000 access.
    for route in ("/api/auth/status", "/api/auth/register", "/api/auth/login", "/api/tokens"):
        assert route in text
    assert "X-Forwarded-Proto: https" in text and "X-Forwarded-For:" in text


@needs_sh
def test_host_cli_parses() -> None:
    assert SH is not None
    res = subprocess.run([SH, "-n", str(DEPLOY / "svbg")], capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stderr


@needs_bash
def test_installer_parses() -> None:
    assert BASH is not None
    res = subprocess.run(
        [BASH, "-n", _posix(DEPLOY / "install.sh")], capture_output=True, text=True, check=False
    )
    assert res.returncode == 0, res.stderr


def _bash(tmp_path: Path, script: str, *, extra_path: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Source install.sh (main does not run) and execute ``script`` with its functions."""
    assert BASH is not None
    env = {
        **os.environ,
        "SVBG_DIR": _posix(tmp_path / "svbg"),
        "RW_DIR": _posix(tmp_path / "rw"),
        "CADDY_DIR": _posix(tmp_path / "caddy"),
        "SVBG_LOG": _posix(tmp_path / "install.log"),
    }
    if extra_path is not None:
        env["PATH"] = str(extra_path) + os.pathsep + env.get("PATH", "")
    prelude = f'source "{_posix(DEPLOY / "install.sh")}"\n'
    script_file = tmp_path / "t.sh"
    script_file.write_text(prelude + script, "utf-8", newline="\n")
    return subprocess.run(
        [BASH, _posix(script_file)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
        timeout=60,
    )


@needs_bash
def test_env_set_and_get(tmp_path: Path) -> None:
    env_file = tmp_path / "panel.env"
    env_file.write_text(
        'A=1\nDATABASE_URL="postgresql://u:p@db/x"\n# B=commented\nC=3\n', "utf-8", newline="\n"
    )
    res = _bash(
        tmp_path,
        f"""f="{_posix(env_file)}"
env_set "$f" A 'a&b/c\\d'
env_set "$f" B new
env_set "$f" B newer
echo "A=[$(env_get "$f" A)] DB=[$(env_get "$f" DATABASE_URL)] B=[$(env_get "$f" B)] Z=[$(env_get "$f" Z)]"
""",
    )
    assert res.returncode == 0, res.stderr
    assert "A=[a&b/c\\d] DB=[postgresql://u:p@db/x] B=[newer] Z=[]" in res.stdout
    lines = env_file.read_text("utf-8").splitlines()
    assert lines == ["A=a&b/c\\d", 'DATABASE_URL="postgresql://u:p@db/x"', "# B=commented", "C=3", "B=newer"]


@needs_bash
def test_validators(tmp_path: Path) -> None:
    res = _bash(
        tmp_path,
        """lower=abcdefghijklmnopqrstuvwxyz12
for p in Short1a "$lower" Abcdefghijklmnopqrstuvwxyz12 ABCDEFGHIJKLMNOPQRSTUVWXYZ; do
    if password_ok "$p"; then echo "pw:$p:ok"; else echo "pw:$p:no"; fi
done
for d in panel.example.com bot.my-shop.ru example 'a b.com' -bad.com xn--80ak6aa92e.xn--p1ai; do
    if valid_domain "$d"; then echo "dom:$d:ok"; else echo "dom:$d:no"; fi
done
""",
    )
    assert res.returncode == 0, res.stderr
    out = res.stdout
    assert "pw:Short1a:no" in out and "pw:abcdefghijklmnopqrstuvwxyz12:no" in out
    assert "pw:Abcdefghijklmnopqrstuvwxyz12:ok" in out and "pw:ABCDEFGHIJKLMNOPQRSTUVWXYZ:no" in out
    assert "dom:panel.example.com:ok" in out and "dom:bot.my-shop.ru:ok" in out
    assert "dom:example:no" in out and "dom:a b.com:no" in out and "dom:-bad.com:no" in out
    assert "dom:xn--80ak6aa92e.xn--p1ai:ok" in out


@needs_bash
def test_render_compose(tmp_path: Path) -> None:
    res = _bash(
        tmp_path,
        """NET=remnawave-network DB_MODE=panel render_compose
cp "$SVBG_DIR/docker-compose.yml" "$SVBG_DIR/panel.yml"
NET=svbg-network DB_MODE=own render_compose
""",
    )
    assert res.returncode == 0, res.stderr
    panel = (tmp_path / "svbg" / "panel.yml").read_text("utf-8")
    own = (tmp_path / "svbg" / "docker-compose.yml").read_text("utf-8")
    for text in (panel, own):
        assert "image: svbg-shop:local" in text and "container_name: svbg-shop" in text
        assert "./data:/app/data" in text and "env_file" not in text and "mem_limit: 512m" in text
        assert '"127.0.0.1:8080:8080"' in text and "external: true" in text and "\t" not in text
    assert "name: remnawave-network" in panel and "depends_on" not in panel and "postgres:" not in panel
    assert "name: svbg-network" in own and "condition: service_healthy" in own
    assert "postgres:17-alpine" in own and "file: ./data/.pg_password" in own


@needs_bash
def test_caddy_blocks_are_idempotent(tmp_path: Path) -> None:
    caddyfile = tmp_path / "Caddyfile"
    caddyfile.write_text("https://other.example.com {\n    respond 200\n}\n", "utf-8", newline="\n")
    res = _bash(
        tmp_path,
        f"""CADDYFILE="{_posix(caddyfile)}"
BOT_DOMAIN=bot.example.com PANEL_DOMAIN=panel.example.com
caddy_put bot "$(caddy_block bot)"
caddy_put panel "$(caddy_block panel)"
caddy_put bot "$(caddy_block bot)"
if caddy_has_foreign other.example.com; then echo foreign-other; fi
if caddy_has_foreign bot.example.com; then echo foreign-bot; fi
""",
    )
    assert res.returncode == 0, res.stderr
    assert "foreign-other" in res.stdout and "foreign-bot" not in res.stdout
    text = caddyfile.read_text("utf-8")
    assert text.startswith("https://other.example.com {")
    assert text.count("# >>> svbg:bot") == 1 and text.count("# <<< svbg:bot") == 1
    assert "@public path /webhooks/* /tg/* /m/* /r/*" in text and "reverse_proxy svbg-shop:8080" in text
    assert "respond 404" in text and "reverse_proxy * http://remnawave:3000" in text


@needs_bash
def test_panel_webhooks_env(tmp_path: Path) -> None:
    rw = tmp_path / "rw"
    rw.mkdir()
    sample = "vsmu67Kmg6R8FjIOF1WUY8LWBHie4scdEqrfsKmyf4IAf8dY3nFS0wwYHkhh6ZvQ"
    (rw / ".env").write_text(
        f"WEBHOOK_ENABLED=false\nWEBHOOK_URL=https://your-webhook-url.com/endpoint\nWEBHOOK_SECRET_HEADER={sample}\n",
        "utf-8",
        newline="\n",
    )
    res = _bash(
        tmp_path, 'panel_hooks_env; echo "secret=$HOOK_SECRET"; panel_hooks_env; echo "again=$HOOK_SECRET"\n'
    )
    assert res.returncode == 0, res.stderr
    env = (rw / ".env").read_text("utf-8")
    secret = re.search(r"^secret=(\w+)$", res.stdout, re.M)
    assert secret and re.fullmatch(r"[0-9a-f]{64}", secret.group(1)), "the public sample secret is replaced"
    assert f"again={secret.group(1)}" in res.stdout, "a second run keeps the secret"
    assert "WEBHOOK_ENABLED=true" in env and "WEBHOOK_URL=http://svbg-shop:8080/webhooks/remnawave\n" in env

    # Webhooks already go to another bot: our address is appended once, the shared secret is kept.
    other = "A" * 40
    (rw / ".env").write_text(
        f"WEBHOOK_ENABLED=true\nWEBHOOK_URL=http://bedolaga:8080/hook\nWEBHOOK_SECRET_HEADER={other}\n",
        "utf-8",
        newline="\n",
    )
    res = _bash(tmp_path, 'panel_hooks_env; panel_hooks_env; echo "secret=$HOOK_SECRET"\n')
    assert res.returncode == 0, res.stderr
    env = (rw / ".env").read_text("utf-8")
    assert "WEBHOOK_URL=http://bedolaga:8080/hook,http://svbg-shop:8080/webhooks/remnawave\n" in env
    assert f"secret={other}" in res.stdout


@needs_bash
def test_state_roundtrip_and_settings_lines(tmp_path: Path) -> None:
    res = _bash(
        tmp_path,
        """MODE=full NET=remnawave-network DB_MODE=panel BOT_DOMAIN=bot.example.com BOT_NAME="it's_me"
save_state
MODE="" BOT_NAME="" BOT_DOMAIN=""
load_state
echo "loaded=$MODE/$BOT_NAME/$BOT_DOMAIN/$SVBG_SRC"
BOT_TOKEN=123:abc DATABASE_URL=postgresql://svbg:pw@remnawave-db:5432/svbg RW_URL=http://remnawave:3000
RW_TOKEN="" HOOK_SECRET=hs OWNER_ID="" ADMIN_CHAT=-1001 FIRST_INSTALL=1
settings_lines
""",
    )
    assert res.returncode == 0, res.stderr
    assert "loaded=full/it's_me/bot.example.com/" in res.stdout
    state = (tmp_path / "svbg" / "install.conf").read_text("utf-8")
    assert "TOKEN" not in state.replace("RW_TOKEN_SET", ""), "no secrets in install.conf"
    lines = [line for line in res.stdout.splitlines() if "=" in line and not line.startswith("loaded=")]
    keys = [line.split("=", 1)[0] for line in lines]
    assert keys == [
        "BOT_TOKEN",
        "DATABASE_URL",
        "BOT_MODE",
        "PUBLIC_URL",
        "REMNAWAVE_URL",
        "REMNAWAVE_WEBHOOK_SECRET",
        "ADMIN_CHAT_ID",
    ], "empty answers do not overwrite existing settings"
    assert "PUBLIC_URL=https://bot.example.com" in lines and "BOT_MODE=polling" in lines


def _stub(bindir: Path, name: str, body: str) -> None:
    path = bindir / name
    path.write_text("#!/bin/sh\n" + body + "\n", "utf-8", newline="\n")
    path.chmod(0o755)


@needs_bash
def test_token_check_keeps_the_token_out_of_argv(tmp_path: Path) -> None:
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    argv, stdin = _posix(tmp_path / "curl.argv"), _posix(tmp_path / "curl.stdin")
    _stub(stubs, "curl", f'echo "$*" >> "{argv}"\ncat >> "{stdin}"\necho \'{{"ok":true}}\'')
    _stub(stubs, "jq", "cat >/dev/null\necho testbot")
    token = "123456789:" + "B" * 35
    res = _bash(tmp_path, f'BOT_TOKEN="{token}"\nname=$(tg_username)\necho "name=$name"\n', extra_path=stubs)
    assert res.returncode == 0, res.stderr
    assert "name=testbot" in res.stdout
    assert token not in (tmp_path / "curl.argv").read_text("utf-8") and "-K -" in (
        tmp_path / "curl.argv"
    ).read_text("utf-8")
    assert f'url = "https://api.telegram.org/bot{token}/getMe"' in (tmp_path / "curl.stdin").read_text(
        "utf-8"
    )
