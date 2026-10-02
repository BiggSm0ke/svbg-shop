"""Pure helpers of the setup wizard: webhook URL, panel ``.env`` snippet, WEBHOOK_URL line check, state."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import msgspec
import pytest

from svbg.remnawave.models import SystemConfig
from svbg.tg.setup.wizard import (
    NOTIFY_GROUPS,
    WizardState,
    build_env_snippet,
    check_webhook_url_line,
    generate_webhook_secret,
    is_panel_secret,
    missing_notify_groups,
    panel_features,
    webhook_target,
)

OUR = "http://svbg-shop:8080/webhooks/remnawave"


# ------------------------------------------------------------------------------------------ webhook target


def test_docker_network_target() -> None:
    t = webhook_target(
        hostname="svbg-shop", public_url=None, panel_url="http://remnawave:3000", resolves=True
    )
    assert t.url == OUR and t.source == "docker" and t.problem is None and t.notes == ()


def test_docker_target_wins_over_public_url_when_panel_is_local() -> None:
    t = webhook_target(
        hostname="svbg-shop", public_url="https://bot.example.com/", panel_url="http://remnawave:3000"
    )
    assert t.url == OUR


def test_public_url_for_remote_panel() -> None:
    t = webhook_target(
        hostname="svbg-shop", public_url="https://bot.example.com/", panel_url="https://panel.example.com"
    )
    assert t.url == "https://bot.example.com/webhooks/remnawave" and t.source == "public"


def test_custom_port_and_unresolvable_hostname() -> None:
    t = webhook_target(
        hostname="mybot", public_url=None, panel_url="http://remnawave:3000", port=9000, resolves=False
    )
    assert t.url == "http://mybot:9000/webhooks/remnawave"
    assert any("remnawave-network" in n for n in t.notes)


def test_remote_panel_without_public_url_warns() -> None:
    t = webhook_target(hostname="svbg-shop", public_url=None, panel_url="https://panel.example.com")
    assert t.url == OUR and any("той же docker-сети" in n for n in t.notes)


def test_container_id_hostname_needs_public_url() -> None:
    t = webhook_target(hostname="3f2a9c1b7d4e", public_url=None, panel_url="http://remnawave:3000")
    assert t.url is None and t.problem is not None and "PUBLIC_URL" in t.problem
    t = webhook_target(hostname="3f2a9c1b7d4e", public_url="https://bot.example.com", panel_url=None)
    assert t.url == "https://bot.example.com/webhooks/remnawave"


@pytest.mark.parametrize(
    "public", ["https://bot.example.com/a b", "https://bot.example.com/x?y=1", "https://b'x.com"]
)
def test_unsafe_public_url_is_refused(public: str) -> None:
    t = webhook_target(hostname="3f2a9c1b7d4e", public_url=public, panel_url=None)
    assert t.url is None and t.problem is not None


@pytest.mark.parametrize("hostname", ["svbg-shop", "3f2a9c1b7d4e"])
@pytest.mark.parametrize(
    "public", ["http://bot.example.com", "http://bot.example.com:8080/", "http://8.8.8.8:8080"]
)
def test_plain_http_public_url_over_internet_is_refused(hostname: str, public: str) -> None:
    # webhook bodies carry trojan/ss passwords, vless uuids and login passwords (02 §5.1)
    t = webhook_target(hostname=hostname, public_url=public, panel_url="https://panel.example.com")
    assert t.url is None and t.source is None
    assert t.problem is not None and "https://" in t.problem


def test_plain_http_public_url_ignored_when_panel_is_in_docker_network() -> None:
    t = webhook_target(
        hostname="svbg-shop", public_url="http://bot.example.com", panel_url="http://remnawave:3000"
    )
    assert t.url == OUR and t.source == "docker" and t.problem is None


@pytest.mark.parametrize(
    ("public", "expected"),
    [
        ("http://10.0.0.5:8080", "http://10.0.0.5:8080/webhooks/remnawave"),
        ("http://192.168.1.20:8080/", "http://192.168.1.20:8080/webhooks/remnawave"),
        ("http://127.0.0.1:8080", "http://127.0.0.1:8080/webhooks/remnawave"),
        ("http://svbg-shop:8080", "http://svbg-shop:8080/webhooks/remnawave"),
        ("https://bot.example.com", "https://bot.example.com/webhooks/remnawave"),
    ],
)
def test_http_public_url_allowed_inside_private_network(public: str, expected: str) -> None:
    t = webhook_target(hostname="3f2a9c1b7d4e", public_url=public, panel_url="http://10.0.0.2:3000")
    assert t.url == expected and t.source == "public" and t.problem is None


# ------------------------------------------------------------------------------------------ snippet


def test_snippet_shape_mode_a_and_b() -> None:
    secret = generate_webhook_secret()
    a = build_env_snippet(our_url=OUR, secret=secret, notify=["expiration"])
    assert a.startswith('cd /opt/remnawave && cp .env ".env.bak.$(date +%s)"')
    assert f"OUR_URL='{OUR}'" in a
    assert f"setkv WEBHOOK_SECRET_HEADER '{secret}'" in a
    assert "setkv EXPIRATION_NOTIFICATIONS '[-72,-24,24]'" in a
    assert ">> .env" in a and "cat >>" not in a  # lines are modified in place, never blindly appended
    assert a.rstrip().endswith("docker compose down && docker compose up -d")
    b = build_env_snippet(our_url=OUR)
    assert "WEBHOOK_SECRET_HEADER" not in b.replace("grep '^WEBHOOK_' .env", "")
    n = build_env_snippet(our_url=None, notify=list(NOTIFY_GROUPS))
    assert "WEBHOOK" not in n and n.count("setkv ") == 2 * len(NOTIFY_GROUPS)


@pytest.mark.parametrize(
    ("kw", "message"),
    [
        ({"our_url": "http://x/'; rm -rf /"}, "unsafe"),
        ({"our_url": OUR, "secret": "short"}, "secret"),
        ({"our_url": OUR, "secret": "a" * 31 + "-"}, "secret"),
        ({"our_url": None, "notify": ["nope"]}, "unknown"),
        ({"our_url": None, "env_dir": "/opt/x; reboot"}, "unsafe"),
    ],
)
def test_snippet_refuses_unsafe_values(kw: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        build_env_snippet(**kw)  # type: ignore[arg-type]


def _bash() -> str | None:
    found = shutil.which("bash")
    if found is None:
        return None
    try:
        subprocess.run([found, "-c", "sed --version"], check=True, capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return found


def _run(bash: str, snippet: str, env_dir: Path) -> None:
    script = snippet.replace("cd /opt/remnawave", 'cd "$1"').replace(
        "docker compose down && docker compose up -d", "true"
    )
    path = env_dir.parent / "snippet.sh"
    path.write_text(script, encoding="utf-8", newline="\n")
    subprocess.run([bash, path.as_posix(), env_dir.as_posix()], check=True, capture_output=True, timeout=30)


def _env(env_dir: Path) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for line in (env_dir / ".env").read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        out.setdefault(key, []).append(value)
    return out


def test_snippet_edits_env_in_place_and_is_idempotent(tmp_path: Path) -> None:
    bash = _bash()
    if bash is None:
        pytest.skip("bash with GNU sed is not available")
    env_dir = tmp_path / "remnawave"
    env_dir.mkdir()
    (env_dir / ".env").write_text(
        "APP_PORT=3000\n"
        "WEBHOOK_ENABLED=false\n"
        "WEBHOOK_URL=https://old.example.com/hook?a=1&b=2, https://x.example.com/z # old bot\n"
        "WEBHOOK_SECRET_HEADER=OldSecretOldSecretOldSecretOldSecret12\n",
        encoding="utf-8",
        newline="\n",
    )
    snippet = build_env_snippet(our_url=OUR, notify=["expiration", "bandwidth"])  # mode B: keep the secret
    _run(bash, snippet, env_dir)
    env = _env(env_dir)
    assert env["WEBHOOK_URL"] == [f"https://old.example.com/hook?a=1&b=2,https://x.example.com/z,{OUR}"]
    assert env["WEBHOOK_ENABLED"] == ["true"]
    assert env["WEBHOOK_SECRET_HEADER"] == ["OldSecretOldSecretOldSecretOldSecret12"]
    assert env["EXPIRATION_NOTIFICATIONS"] == ["[-72,-24,24]"]
    assert env["BANDWIDTH_USAGE_NOTIFICATIONS_THRESHOLD"] == ["[80,95]"]
    assert env["APP_PORT"] == ["3000"]
    assert check_webhook_url_line("WEBHOOK_URL=" + env["WEBHOOK_URL"][0], OUR) == []
    before = (env_dir / ".env").read_text(encoding="utf-8")
    _run(bash, snippet, env_dir)  # a repeated run changes nothing
    assert (env_dir / ".env").read_text(encoding="utf-8") == before
    assert len(list(env_dir.glob(".env.bak.*"))) >= 1


def test_snippet_mode_a_on_fresh_env(tmp_path: Path) -> None:
    bash = _bash()
    if bash is None:
        pytest.skip("bash with GNU sed is not available")
    env_dir = tmp_path / "rw"
    env_dir.mkdir()
    (env_dir / ".env").write_text("APP_PORT=3000\n", encoding="utf-8", newline="\n")
    secret = generate_webhook_secret()
    _run(bash, build_env_snippet(our_url=OUR, secret=secret), env_dir)
    env = _env(env_dir)
    assert env["WEBHOOK_URL"] == [OUR]
    assert env["WEBHOOK_SECRET_HEADER"] == [secret]
    assert env["WEBHOOK_ENABLED"] == ["true"]


# ------------------------------------------------------------------------------------------ line check


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (f"WEBHOOK_URL={OUR}", []),
        (f"WEBHOOK_URL=https://a.example.com/h,{OUR}", []),
        (OUR, []),
        (f"WEBHOOK_URL=https://a.example.com/h, {OUR}", ["пробелы", "не начинается"]),
        (f"WEBHOOK_URL={OUR} # бот", ["комментарий"]),
        (f'WEBHOOK_URL="{OUR}"', ["кавычки", "не начинается"]),
        (f"WEBHOOK_URL={OUR},", ["пусто"]),
        ("WEBHOOK_URL=https://a.example.com/h", ["нет адреса бота"]),
        (f"WEBHOOK_URL={OUR},{OUR}", ["дважды"]),
        ("URL=x", ["WEBHOOK_URL="]),
        ("", ["Пустая"]),
    ],
)
def test_webhook_url_line(line: str, expected: list[str]) -> None:
    problems = check_webhook_url_line(line, OUR)
    if not expected:
        assert problems == []
    for part in expected:
        assert any(part in p for p in problems), problems


# ------------------------------------------------------------------------------------------ secret, config


def test_generated_secret_matches_the_panel_rule() -> None:
    secrets_ = {generate_webhook_secret() for _ in range(20)}
    assert len(secrets_) == 20
    assert all(is_panel_secret(s) and len(s) == 64 for s in secrets_)
    assert not is_panel_secret("a" * 31)
    assert not is_panel_secret("a" * 40 + "_")
    assert not is_panel_secret(None)
    with pytest.raises(ValueError):
        generate_webhook_secret(16)


def test_panel_features_and_missing_groups() -> None:
    cfg = msgspec.json.decode(
        b'{"notifications": {"webhook": true, "bandwidthUsage": null, "notConnectedAfter": [2,24],'
        b' "expirationNotifications": null}, "misc": {"subPublicDomain": "sub.example.com"}}',
        type=SystemConfig,
    )
    features = {env: (enabled, shown) for _label, env, enabled, shown in panel_features(cfg)}
    assert features["WEBHOOK_ENABLED"] == (True, "включены")
    assert features["NOT_CONNECTED_USERS_NOTIFICATIONS_AFTER_HOURS"] == (True, "[2,24]")
    assert features["EXPIRATION_NOTIFICATIONS"] == (False, "выключено")
    assert missing_notify_groups(cfg) == ["expiration", "bandwidth"]


def test_wizard_state_roundtrip_and_garbage() -> None:
    state = WizardState(
        step="wh", skipped=["rw"], webhook_mode="B", panel_check={"ok": True, "text": "x", "at": "t"}
    )
    again = WizardState.from_json(state.to_json())
    assert again == state
    assert WizardState.from_json(None) == WizardState()
    assert WizardState.from_json({"v": 99, "step": "wh"}) == WizardState()
    junk = WizardState.from_json({"v": 1, "step": "evil", "skipped": ["rw", "x"], "webhook_mode": "C"})
    assert junk.step == "rw" and junk.skipped == ["rw"] and junk.webhook_mode is None
