"""Value parsing/validation, the registry and the snapshot (no database)."""

from __future__ import annotations

import dataclasses

import pytest

from svbg.core.settings import values
from svbg.core.settings.envtext import comment_lines
from svbg.core.settings.registry import SECTIONS, Apply, Registry, SettingDef, core_registry
from svbg.core.settings.snapshot import SettingsSnapshot
from svbg.core.settings.values import SettingValueError


def d(key: str = "X_KEY", type_: object = int, default: object = 1, **kw: object) -> SettingDef:
    return SettingDef(key, type_, default, kw.pop("section", "sales"), "Заголовок", "Описание.", **kw)  # type: ignore[arg-type]


# ------------------------------------------------------------------------------------------------ values


@pytest.mark.parametrize(
    ("type_", "raw", "expected"),
    [
        (int, " 42 ", 42),
        (int, "1 000", 1000),
        (int, "-5", -5),
        (float, "1,5", 1.5),
        (bool, "да", True),
        (bool, "OFF", False),
        (bool, "1", True),
        ("duration", "30d", 30 * 86400),
        ("duration", "1h30m", 5400),
        ("duration", "90", 90),
        ("duration", "2ч", 7200),
        ("list[str]", " a, b ,,c ", ["a", "b", "c"]),
        ("list[int]", "1, 2,3", [1, 2, 3]),
        ("url", "https://example.com/", "https://example.com"),
        ("url", "https://example.com/a/", "https://example.com/a"),
        ("secret", "abc", "abc"),
    ],
)
def test_parse_ok(type_: object, raw: str, expected: object) -> None:
    defn = d(type_=type_, default=expected)
    assert values.parse(defn, raw) == expected


@pytest.mark.parametrize(
    ("type_", "raw", "fragment"),
    [
        (int, "abc", "целое"),
        (int, "1.5", "целое"),
        (float, "nan", "число"),
        (bool, "maybe", "да/нет"),
        ("duration", "5 parsecs", "длительность"),
        ("duration", "h", "длительность"),
        ("list[int]", "1,x", "целых"),
        ("url", "example.com", "https://"),
        ("url", "ftp://example.com", "https://"),
        ("str", "a\nb", "переводов"),
    ],
)
def test_parse_errors_are_russian(type_: object, raw: str, fragment: str) -> None:
    defn = d(type_=type_, default=None, nullable=True)
    with pytest.raises(SettingValueError, match=fragment):
        values.parse(defn, raw)


def test_empty_value_nullable_and_required() -> None:
    assert values.parse(d(default=None, nullable=True), "  ") is None
    with pytest.raises(SettingValueError, match="обязательно"):
        values.parse(d(), "")
    assert values.parse_or_default(d(default=7), "") == 7
    assert values.parse_or_default(d(type_="list[int]", default=[]), "") == []


def test_range_choices_and_validator() -> None:
    defn = d(min=0, max=365)
    assert values.parse(defn, "365") == 365
    with pytest.raises(SettingValueError, match="минимума"):
        values.parse(defn, "-1")
    with pytest.raises(SettingValueError, match="максимума"):
        values.parse(defn, "366")
    enum = d(type_="enum", default="a", choices=("a", "b"))
    with pytest.raises(SettingValueError, match="a \\| b"):
        values.parse(enum, "c")

    def no_seven(v: object) -> None:
        if v == 7:
            raise ValueError("семь нельзя")

    with pytest.raises(SettingValueError, match="семь нельзя"):
        values.parse(d(validator=no_seven), "7")


def test_secret_validator_message_never_contains_value() -> None:
    def bad(v: object) -> None:
        raise ValueError(f"bad {v}")

    defn = d(type_="secret", default=None, nullable=True, validator=bad)
    with pytest.raises(SettingValueError) as info:
        values.parse(defn, "supersecretvalue")
    assert "supersecretvalue" not in str(info.value)


def test_coerce_typed_values() -> None:
    assert values.coerce(d(), 5) == 5
    with pytest.raises(SettingValueError):
        values.coerce(d(), True)  # bool is not an int here
    with pytest.raises(SettingValueError):
        values.coerce(d(type_=bool, default=False), 1)
    assert values.coerce(d(type_="list[int]", default=[]), (1, 2)) == [1, 2]
    with pytest.raises(SettingValueError):
        values.coerce(d(type_="list[str]", default=[]), ["a,b"])
    assert values.coerce(d(type_=float, default=0.0), 2) == 2.0
    assert values.coerce(d(), "8") == 8


def test_text_round_trip_and_same_value() -> None:
    cases = [
        (d(type_=bool, default=False), True),
        (d(type_="duration", default=0), 90061),
        (d(type_="list[int]", default=[]), [1, 2]),
        (d(type_=float, default=0.0), 0.25),
        (d(), 0),
    ]
    for defn, value in cases:
        assert values.parse(defn, values.to_text(defn, value)) == value
    defn = d()
    assert values.same_value(defn, "05", "5")
    assert not values.same_value(defn, "5", "6")
    assert values.same_value(defn, "abc", " abc ")  # unparsable → raw comparison
    assert not values.same_value(defn, None, "1")


def test_display_masks_secrets_and_placeholder_marker() -> None:
    secret = d(type_="secret", default=None, nullable=True)
    assert values.display(secret, "1234567890abcdef") == "\N{BULLET}" * 8 + "cdef"
    assert values.display(d(), None) == "—"
    assert values.is_unchanged_marker(secret, values.SECRET_PLACEHOLDER)
    assert values.is_unchanged_marker(secret, "\N{BULLET}" * 8 + "cdef")
    assert not values.is_unchanged_marker(d(), values.SECRET_PLACEHOLDER)


# ------------------------------------------------------------------------------------------------ registry


def test_core_registry_has_contract_keys() -> None:
    reg = core_registry()
    expected = {
        "BOT_TOKEN", "SECRET_KEY", "DATABASE_URL", "OWNER_IDS", "TELEGRAM_PROXY", "TELEGRAM_API_URL",
        "LOCKED_KEYS", "DATA_DIR", "BOT_MODE", "PUBLIC_URL", "WEBHOOK_SECRET", "DEFAULT_LANGUAGE", "TIMEZONE",
        "CURRENCY", "LOG_LEVEL", "ENV_LAYOUT", "ENV_SECRETS", "ADMIN_CHAT_ID", "SUPPORT_URL", "TRIAL_DAYS",
        "TRIAL_AUDIENCE", "REMNAWAVE_URL", "REMNAWAVE_TOKEN", "REMNAWAVE_CADDY_TOKEN", "REMNAWAVE_COOKIE",
        "REMNAWAVE_WEBHOOK_SECRET", "WALLET_AUTOCOMPLETE_MINUTES", "REQUIRED_CHANNEL_ID", "REPORT_DAILY_AT",
    }  # fmt: skip
    assert expected <= set(reg.keys())
    assert reg.get("TRIAL_DAYS").default == 3
    assert (reg.get("TRIAL_DAYS").min, reg.get("TRIAL_DAYS").max) == (0, 365)
    assert reg.get("DATABASE_URL").apply is Apply.RESTART
    assert reg.get("BOT_MODE").component == "bot"
    assert all(reg.get(k).component == "remnawave" for k in reg if k.startswith("REMNAWAVE_"))
    assert reg.get("ADMIN_CHAT_ID").component == "admin_chat"
    assert all(reg.get(k).bootstrap for k in ("BOT_TOKEN", "SECRET_KEY", "DATABASE_URL", "OWNER_IDS"))
    assert reg.get("BOT_TOKEN").is_secret and reg.get("REMNAWAVE_TOKEN").is_secret
    assert next(sid for sid, _ in SECTIONS) == "boot"
    assert not any("сайт" in title.lower() for _, title in SECTIONS)


def test_registry_rejects_bad_definitions() -> None:
    reg = Registry()
    reg.add(d("A_KEY", aliases=("OLD_A",)))
    with pytest.raises(ValueError, match="already used"):
        reg.add(d("A_KEY"))
    with pytest.raises(ValueError, match="already used"):
        reg.add(d("B_KEY", aliases=("OLD_A",)))
    with pytest.raises(ValueError, match="component"):
        reg.add(d("C_KEY", apply=Apply.RELOAD))
    with pytest.raises(ValueError, match="invalid default"):
        reg.add(d("D_KEY", default=500, max=10))
    with pytest.raises(ValueError, match="nullable"):
        reg.add(d("E_KEY", default=None))
    with pytest.raises(ValueError, match="choices"):
        reg.add(d("F_KEY", type_="enum", default="x"))
    with pytest.raises(ValueError, match="section"):
        reg.add(d("G_KEY", section="nope"))
    with pytest.raises(ValueError, match="invalid setting key"):
        reg.add(d("lower"))
    with pytest.raises(TypeError):
        reg.add(d("H_KEY", type_=dict, default={}))


def test_alias_resolution_and_sections() -> None:
    reg = Registry()
    reg.add(d("A_KEY", aliases=("OLD_A",)))
    reg.add(d("B_KEY", section="system"))
    assert reg.resolve_alias("old_a") == "A_KEY"
    assert reg.get("OLD_A").key == "A_KEY"
    assert reg.find("NOPE") is None
    with pytest.raises(KeyError):
        reg.get("NOPE")
    grouped = reg.by_section()
    assert next(iter(grouped)) == "boot"
    assert [x.key for x in grouped["sales"]] == ["A_KEY"]
    assert "OLD_A" in reg and "nope" not in reg


def test_fingerprint_changes_with_defaults() -> None:
    one, two = Registry(), Registry()
    one.add(d("A_KEY", default=1))
    two.add(d("A_KEY", default=2))
    assert one.fingerprint != two.fingerprint
    before = one.fingerprint
    one.add(d("B_KEY"))
    assert one.fingerprint != before


@pytest.mark.parametrize(
    ("key", "good", "bad"),
    [
        ("BOT_TOKEN", "123456789:" + "A" * 35, "12345:short"),
        ("REPORT_DAILY_AT", "23:59", "24:00"),
        ("TELEGRAM_PROXY", "socks5://u:p@host:1080", "host:1080"),
        ("DATABASE_URL", "postgresql://u:p@h/db", "sqlite:///x.db"),
        ("WEBHOOK_SECRET", "a" * 32, "short"),
        ("LOCKED_KEYS", "TRIAL_DAYS,BOT_MODE", "trial days"),
    ],
)
def test_core_validators(key: str, good: str, bad: str) -> None:
    defn = core_registry().get(key)
    values.parse(defn, good)
    with pytest.raises(SettingValueError):
        values.parse(defn, bad)


def test_secret_key_validator_accepts_fernet_keys_only() -> None:
    from svbg.core.crypto import generate_key

    defn = core_registry().get("SECRET_KEY")
    values.parse(defn, generate_key())
    with pytest.raises(SettingValueError, match="повреждён"):
        values.parse(defn, "not-a-key-at-all")


def test_timezone_validator() -> None:
    defn = core_registry().get("TIMEZONE")
    with pytest.raises(SettingValueError):
        values.parse(defn, "Mars/Olympus Mons!")


def test_comment_lines_are_deterministic_and_informative() -> None:
    reg = core_registry()
    lines = comment_lines(reg.get("TRIAL_DAYS"))
    text = " ".join(lines)
    assert "Диапазон 0–365" in text and "По умолчанию: 3" in text and "⚡" in text
    assert comment_lines(reg.get("TRIAL_DAYS")) == lines
    rw = " ".join(comment_lines(reg.get("REMNAWAVE_TOKEN"), locked=True))
    assert "🔄" in rw and "Секрет" in rw and "LOCKED_KEYS" in rw
    assert "♻️" in " ".join(comment_lines(reg.get("DATABASE_URL")))
    aliased = dataclasses.replace(reg.get("TRIAL_DAYS"), aliases=("TRIAL_PERIOD",))
    assert any("TRIAL_PERIOD" in line for line in comment_lines(aliased))


# ------------------------------------------------------------------------------------------------ snapshot


def test_snapshot_is_immutable_and_masks_secrets() -> None:
    snap = SettingsSnapshot(
        {"A": 1, "S": "topsecretvalue", "L": [1, 2]},
        {"A": "default", "S": "bot", "L": "env_file"},
        aliases={"OLD_A": "A"},
        secrets=frozenset({"S"}),
    )
    assert snap["A"] == 1 and snap["OLD_A"] == 1 and "OLD_A" in snap
    assert snap.source("S") == "bot" and snap.is_secret("S")
    snap["L"].append(3)
    assert snap["L"] == [1, 2]
    with pytest.raises(TypeError):
        snap["A"] = 2  # type: ignore[index]
    assert "topsecretvalue" not in repr(snap)
    assert snap.as_dict(mask_secrets=False)["S"] == "topsecretvalue"
    newer = snap.with_changes({"A": 2}, {"A": "bot"})
    assert newer.version == snap.version + 1 and newer["A"] == 2 and snap["A"] == 1
    assert newer.source("A") == "bot"
    with pytest.raises(KeyError):
        snap.with_changes({"NOPE": 1}, {"NOPE": "bot"})
    with pytest.raises(ValueError, match="no source"):
        SettingsSnapshot({"A": 1}, {})
