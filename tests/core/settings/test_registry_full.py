"""The full registry (core + ops + referral + extension modules + every payment plugin): the ``.env`` it
renders, uniqueness of names, aliases, texts, sections and the keys asked for by the stage 3–4 integration
requests."""

from __future__ import annotations

import ast
import dataclasses
import re
from pathlib import Path
from typing import Any

import pytest

from svbg.app import render_env_offline
from svbg.boot.envfile import EnvDocument, render_full, section_title
from svbg.core.settings import values
from svbg.core.settings.envtext import comment_lines
from svbg.core.settings.registry import (
    EXTENSION_MODULES,
    MODULES_SECTION,
    PAYMENTS_SECTION,
    Apply,
    Registry,
    SettingDef,
    add_module_settings,
    add_payment_instance,
    core_registry,
    full_registry,
    payment_section,
)

PROVIDERS_DIR = Path(__file__).resolve().parents[3] / "svbg" / "payments" / "providers"
_CYRILLIC = re.compile(r"[А-Яа-яЁё]")
_KEY_RE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")


@pytest.fixture(scope="module")
def reg() -> Registry:
    return full_registry()


def plugin_slugs() -> list[str]:
    """``slug="…"`` of every ``Manifest`` in ``svbg/payments/providers/*.py`` (read from the source, so a
    plugin file that is not registered in ``BUILTIN_PROVIDERS`` is still found)."""
    slugs: list[str] = []
    for path in sorted(PROVIDERS_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Manifest":
                for kw in node.keywords:
                    if kw.arg == "slug" and isinstance(kw.value, ast.Constant):
                        slugs.append(kw.value.value)
    return slugs


# ------------------------------------------------------------------------------------------------ the file


def test_full_env_renders_every_key_once_and_is_a_fixed_point(reg: Registry, tmp_path: Path) -> None:
    env = tmp_path / ".env"
    text = render_env_offline(env, {}, registry=reg)
    doc = EnvDocument.parse(text)
    keys = [line.key for line in doc.lines if line.kind == "kv"]
    assert len(keys) == len(set(keys))
    assert set(keys) == {d.key for d in reg.all() if d.in_file}
    env.write_text(text, encoding="utf-8", newline="")
    assert render_env_offline(env, {}, registry=reg) == text
    headers = [t for line in text.splitlines() if (t := section_title(line))]
    # 07 §3.2 order, payment instances inside «Платёжки», module sections between «Модули» and «Система».
    assert headers.index("Платёжки") < headers.index("Платёжка «RollyPay» (инстанс rollypay)")
    assert headers.index("Платёжка «RollyPay» (инстанс rollypay)") < headers.index("Рефералка")
    assert headers.index("Логи и ошибки") < headers.index("Модули › 🌐 Трафик LTE") < headers.index("Система")
    assert headers.index("Логи и ошибки") < headers.index("Модули › 🛡 IP Guard") < headers.index("Система")
    assert "Модули" not in headers  # no keys of its own: the renderer omits it, the subsections follow


def test_env_sections_show_modules_as_subsections(reg: Registry) -> None:
    keys = [
        _render_key(d)
        for d in reg.all()
        if d.in_file and d.section in {"lte", "ip_guard", "system", payment_section("stars")}
    ]
    text = render_full(reg.env_sections, keys, ["# шапка"])
    headers = [t for line in text.splitlines() if (t := section_title(line))]
    assert headers == [
        "Платёжка «Telegram Stars» (инстанс stars)",
        "Модули › 🌐 Трафик LTE",
        "Модули › 🛡 IP Guard",
        "Система",
    ]
    assert dict(reg.sections)["lte"] == "🌐 Трафик LTE"  # own title kept for the settings screen


def _render_key(defn: SettingDef) -> Any:
    from svbg.boot.envfile import RenderKey

    return RenderKey(defn.key, values.to_text(defn, defn.default), comment_lines(defn), defn.section)


# ------------------------------------------------------------------------------------------------ names


def test_names_are_unique_and_aliases_resolve(reg: Registry) -> None:
    names = [n for d in reg.all() for n in d.names]
    assert len(names) == len(set(names)) == len({n.upper() for n in names})
    for defn in reg.all():
        assert _KEY_RE.fullmatch(defn.key), defn.key
        for alias in defn.aliases:
            assert _KEY_RE.fullmatch(alias) and alias != defn.key
            assert reg.resolve_alias(alias) == defn.key
            assert reg.resolve_alias(alias.lower()) == defn.key
            assert reg.get(alias) is defn
        assert reg.resolve_alias(defn.key) == defn.key


def test_every_key_has_russian_title_and_description(reg: Registry) -> None:
    for defn in reg.all():
        assert defn.title.strip(), defn.key  # may be a brand name («Cloudflare Access: Client ID»)
        assert defn.description.strip() and _CYRILLIC.search(defn.description), defn.key
        assert defn.section in dict(reg.sections), defn.key
        if defn.apply is Apply.RELOAD:
            assert defn.component, defn.key


def test_defaults_survive_the_text_round_trip(reg: Registry) -> None:
    """What the file says by default is read back as the same default (no key starts with a warning)."""
    for defn in reg.all():
        text = values.to_text(defn, defn.default)
        plain = dataclasses.replace(defn, validator=None)
        # URL defaults are compared normalized (a plugin's «https://x/» is read back as «https://x»).
        assert values.parse_or_default(defn, text) == values.coerce(plain, defn.default), defn.key


def test_snapshot_of_defaults_passes_the_cross_key_checks(reg: Registry) -> None:
    snap = {d.key: d.default for d in reg.all()}
    for check in reg.checks:
        assert check(snap, frozenset(snap)) == {}


def test_fingerprint_is_stable_between_builds(reg: Registry) -> None:
    assert full_registry().fingerprint == reg.fingerprint
    assert core_registry().fingerprint != reg.fingerprint


# ------------------------------------------------------------------------------------------------ payments


def test_every_payment_plugin_has_its_pay_block(reg: Registry) -> None:
    slugs = plugin_slugs()
    assert len(slugs) == 27 and len(set(slugs)) == 27
    for slug in slugs:
        sid = payment_section(slug)
        assert reg.parent(sid) == PAYMENTS_SECTION
        up = slug.upper()
        for suffix in ("ENABLED", "PROVIDER", "TEST_MODE", "PROXY_URL"):
            defn = reg.get(f"PAY_{up}_{suffix}")
            assert defn.section == sid
            assert defn.apply is Apply.RELOAD and defn.component == f"payments.{slug}"
            assert defn.owner_only
        assert reg.get(f"PAY_{up}_ENABLED").default is False  # the catalog: off until filled in
        assert reg.get(f"PAY_{up}_PROXY_URL").is_secret
        assert set(slugs) <= set(reg.get(f"PAY_{up}_PROVIDER").choices or ())
    assert reg.subsections(PAYMENTS_SECTION) == [payment_section(s) for s in _builtin_order()]
    assert [d.key for d in reg.by_section()[PAYMENTS_SECTION]] == ["PAY_CLOCK_SKEW_ALERT_COUNT"]


def _builtin_order() -> list[str]:
    from svbg.payments.providers import BUILTIN_PROVIDERS

    return [cls.manifest.slug for cls in BUILTIN_PROVIDERS]


def test_extra_instance_gets_its_own_subsection() -> None:
    from svbg.payments.providers import BUILTIN_PROVIDERS

    reg = core_registry()
    rolly = next(cls for cls in BUILTIN_PROVIDERS if cls.manifest.slug == "rollypay")
    defs = add_payment_instance(reg, "rollypay2", rolly, providers=_builtin_order())
    assert {d.section for d in defs} == {"payments.rollypay2"}
    assert dict(reg.sections)["payments.rollypay2"] == "Платёжка «RollyPay» (инстанс rollypay2)"
    assert reg.subsections(PAYMENTS_SECTION)[-1] == "payments.rollypay2"
    assert reg.get("PAY_ROLLYPAY2_PROVIDER").default == "rollypay"


# ------------------------------------------------------------------------------------------------ modules


def test_module_sections_are_subsections_of_modules(reg: Registry) -> None:
    assert reg.subsections(MODULES_SECTION) == ["lte", "ip_guard"]
    order = [sid for sid, _ in reg.sections]
    assert order.index(MODULES_SECTION) + 1 == order.index("lte")
    assert order.index("ip_guard") + 1 == order.index("system")
    assert [sid for sid, _ in reg.top_sections()][-2:] == [MODULES_SECTION, "system"]
    for sid, enabled in (("lte", "LTE_ENABLED"), ("ip_guard", "IP_GUARD_ENABLED")):
        first = reg.by_section()[sid][0]
        assert first.key == enabled and first.default is False  # «*_ENABLED=false» on top of the section
    assert len(reg.by_section()["lte"]) == 22 and len(reg.by_section()["ip_guard"]) == 22
    assert EXTENSION_MODULES == ("svbg.ext.lte.service", "svbg.ext.ip_guard")


def test_add_module_settings_is_idempotent_and_takes_the_apps_host() -> None:
    from svbg.ext.api import ExtensionHost, load_specs
    from svbg.ops.settings import OPS_SETTINGS

    reg = core_registry()
    for defn in OPS_SETTINGS:  # the app may register ops itself (decision C2)
        reg.add(defn)
    specs, errors = load_specs(EXTENSION_MODULES)
    assert errors == {}
    host = ExtensionHost(specs)
    added = add_module_settings(reg, host)
    assert added == 8 + 22 + 22  # referral + LTE + IP Guard; ops already there
    assert add_module_settings(reg, host) == 0
    assert len(reg) == len(full_registry())
    with pytest.raises(ValueError, match="already used"):
        reg.add(dataclasses.replace(reg.get("BACKUP_KEEP")))  # a different definition under a known name


def test_ops_and_referral_keep_their_classes(reg: Registry) -> None:
    for key in ("BACKUP_ENABLED", "BACKUP_AT", "BACKUP_KEEP", "BACKUP_PASSWORD", "BACKUP_TO_TELEGRAM"):
        assert reg.get(key).section == "reports" and reg.get(key).owner_only
    assert reg.get("BACKUP_PASSWORD").is_secret
    assert reg.get("REPORT_DAILY_ENABLED").section == "reports"
    assert {reg.get(k).section for k in ("UPDATE_CHECK", "UPDATE_REPO", "UPDATE_PRERELEASES")} == {"system"}
    referral = reg.by_section()["referral"]
    assert referral[0].key == "REFERRAL_ENABLED"
    assert len(referral) == 8
    assert all(d.owner_only for d in referral if d.key != "REFERRAL_ENABLED")
    assert not reg.get("REFERRAL_ENABLED").owner_only


# ------------------------------------------------------------------------------------------------ requested


#: key → (kind, default, section, apply, min, max, flags) from the integration requests.
REQUESTED: dict[str, tuple[str, Any, str, Apply, float | None, float | None, set[str]]] = {
    "MEDIA_PHOTO_MAX_SIDE": ("int", 2560, "content", Apply.HOT, 320, 4096, {"advanced"}),
    "MEDIA_PHOTO_JPEG_QUALITY": ("int", 85, "content", Apply.HOT, 60, 95, {"advanced"}),
    "CONTENT_BACKUPS_KEEP": ("int", 10, "content", Apply.HOT, 1, 100, {"advanced"}),
    "ADMIN_GRANT_DAYS_MAX": ("int", 31, "system", Apply.HOT, 0, 3650, {"owner_only"}),
    "ADMIN_GRANT_DAYS_DAY_MAX": ("int", None, "system", Apply.HOT, 0, None, {"owner_only", "nullable"}),
    "ADMIN_WALLET_ADJUST_MAX": ("int", None, "wallet", Apply.HOT, 0, None, {"owner_only", "nullable"}),
    "ADMIN_WALLET_ADJUST_DAY_MAX": ("int", None, "wallet", Apply.HOT, 0, None, {"owner_only", "nullable"}),
    "DEEPLINK_INTENT_TTL_HOURS": ("int", 24, "promo", Apply.HOT, 1, 720, set()),
    "IMPORT_SOURCE_DSN": ("secret", None, "system", Apply.HOT, None, None, {"owner_only", "nullable"}),
    "PANEL_USERNAME_PREFIX": ("str", "sv_", "remnawave", Apply.HOT, None, None, {"owner_only"}),
    "PANEL_DESCRIPTION_TEMPLATE": (
        "str",
        None,
        "remnawave",
        Apply.HOT,
        None,
        None,
        {"owner_only", "nullable"},
    ),
    "NOTIFY_ADMIN_NODES": ("bool", True, "admin_chat", Apply.HOT, None, None, {"owner_only"}),
    "TRIAL_CARRY_OVER": ("bool", False, "sales", Apply.HOT, None, None, set()),
    "PRICING_ROUNDING": ("bool", False, "sales", Apply.HOT, None, None, set()),
    "ONBOARDING_ASK_REFERRAL_CODE": ("bool", False, "sales", Apply.HOT, None, None, set()),
    "ONBOARDING_RULES": ("enum", "off", "sales", Apply.HOT, None, None, set()),
    "I18N_AVAILABLE": ("list[str]", ["ru", "en"], "system", Apply.HOT, None, None, set()),
    "I18N_ASK_ON_START": ("bool", False, "system", Apply.HOT, None, None, set()),
    "MAINTENANCE_MESSAGE": ("str", None, "system", Apply.HOT, None, None, {"nullable"}),
    "REMNAWAVE_ALLOW_PLAIN_HTTP": ("bool", False, "remnawave", Apply.RELOAD, None, None, {"owner_only"}),
    "CATALOG_LOCATIONS_SYNC_MINUTES": ("int", 10, "remnawave", Apply.RESTART, 1, 1440, {"owner_only"}),
    # Asked earlier (stage 2), checked here so the list stays complete.
    "CHANNEL_LEAVE_ACTION": ("enum", "trial", "sales", Apply.HOT, None, None, set()),
    "REISSUE_COOLDOWN_MINUTES": ("int", 10, "sales", Apply.HOT, 0, 1440, set()),
    "DEVICES_RESET_COOLDOWN_MINUTES": ("int", 5, "sales", Apply.HOT, 0, 1440, set()),
    "PAY_CLOCK_SKEW_ALERT_COUNT": ("int", 5, "payments", Apply.HOT, 1, 100, {"owner_only"}),
}


@pytest.mark.parametrize("key", sorted(REQUESTED))
def test_requested_key(reg: Registry, key: str) -> None:
    kind, default, section, apply, lo, hi, flags = REQUESTED[key]
    defn = reg.get(key)
    assert (defn.kind, defn.default, defn.section, defn.apply) == (kind, default, section, apply)
    if lo is not None:
        assert defn.min == lo
    if hi is not None:
        assert defn.max == hi
    for flag in flags:
        assert getattr(defn, flag), f"{key}: {flag}"
    assert defn.is_secret == (kind == "secret")


def test_requested_module_keys_come_from_their_manifests(reg: Registry) -> None:
    """LTE / IP Guard keys are declared by the modules (decision C2), not duplicated in the core."""
    core = core_registry()
    for prefix in ("LTE_", "IP_GUARD_", "REFERRAL_", "BACKUP_", "UPDATE_"):
        assert not [k for k in core if k.startswith(prefix)], prefix
        assert [k for k in reg if k.startswith(prefix)], prefix
    assert reg.get("LTE_OFF_ACTION").kind == "enum"  # C3: not LTE_OFF_RELEASE_BLOCKS
    assert "LTE_OFF_RELEASE_BLOCKS" not in reg


def test_no_site_payment_section(reg: Registry) -> None:
    titles = " ".join(t for _, t in reg.sections).lower()
    assert "сайт" not in titles
    assert not [k for k in reg if k.startswith(("SITEPAY_", "SITE_PAY"))]


# ------------------------------------------------------------------------------------------------ validators


@pytest.mark.parametrize(
    ("key", "good", "bad"),
    [
        ("PANEL_USERNAME_PREFIX", "user_", "слишком_длинный_префикс"),
        ("PANEL_USERNAME_PREFIX", "a-b", "a b"),
        ("PANEL_DESCRIPTION_TEMPLATE", "{full_name} @{tg_username} ({telegram_id})", "{username}"),
        ("PANEL_DESCRIPTION_TEMPLATE", "sv:{public_id}", "x" * 201),
        ("I18N_AVAILABLE", "ru, en", "ru, de"),
        ("IMPORT_SOURCE_DSN", "postgresql://ro:p@old:5432/bedolaga", "mysql://x"),
        ("ONBOARDING_RULES", "on", "maybe"),
    ],
)
def test_validators(reg: Registry, key: str, good: str, bad: str) -> None:
    defn = reg.get(key)
    values.parse(defn, good)
    with pytest.raises(values.SettingValueError):
        values.parse(defn, bad)


def test_default_language_must_be_available(reg: Registry) -> None:
    snap = {d.key: d.default for d in reg.all()}
    checks = reg.checks
    bad = {**snap, "I18N_AVAILABLE": ["en"], "DEFAULT_LANGUAGE": "ru"}
    errors = {k: v for c in checks for k, v in c(bad, frozenset({"I18N_AVAILABLE"})).items()}
    assert set(errors) == {"I18N_AVAILABLE"} and "язык по умолчанию" in errors["I18N_AVAILABLE"]
    errors = {k: v for c in checks for k, v in c(bad, frozenset({"DEFAULT_LANGUAGE"})).items()}
    assert set(errors) == {"DEFAULT_LANGUAGE"}
    empty = {**snap, "I18N_AVAILABLE": [], "DEFAULT_LANGUAGE": "en"}
    assert not any(c(empty, frozenset({"I18N_AVAILABLE"})) for c in checks)


def test_secret_keys(reg: Registry) -> None:
    secrets = {d.key for d in reg.all() if d.is_secret}
    assert {
        "IMPORT_SOURCE_DSN",
        "BACKUP_PASSWORD",
        "PAY_YOOKASSA_SECRET_KEY",
        "PAY_LAVA_SECRET_KEY",
    } <= secrets
    assert not reg.get("PAY_OVERPAY_PUBLIC_KEY").is_secret  # public keys are plain on purpose


# ------------------------------------------------------------------------------------------------ sections


def test_sections_one_level_deep_and_copied_subsections() -> None:
    reg = Registry()
    reg.add_section("demo", "Демо")  # default parent «Модули»
    assert reg.parent("demo") == MODULES_SECTION
    reg.add_section("top", "Верх", parent=None)
    assert reg.parent("top") is None and [s for s, _ in reg.sections][-1] == "top"
    with pytest.raises(ValueError, match="unknown parent"):
        reg.add_section("x1", "X", parent="nope")
    with pytest.raises(ValueError, match="subsection itself"):
        reg.add_section("x2", "X", parent="demo")
    with pytest.raises(KeyError):
        reg.parent("nope")
    custom = Registry([("a", "A")])
    custom.add_section("b", "B")  # no «Модули» in a custom list: top-level
    assert custom.parent("b") is None

    # Copying the core keys into a fresh registry (as tests and tools do) recreates payment subsections.
    copy = Registry()
    for defn in core_registry().all():
        copy.add(defn)
    assert copy.parent(payment_section("rollypay")) == PAYMENTS_SECTION
    assert copy.keys() == core_registry().keys()
    # A broken key never leaves a half-made section behind.
    with pytest.raises(ValueError, match="invalid setting key"):
        copy.add(SettingDef("bad", int, 1, "payments.zzz", "Т", "О"))
    assert "payments.zzz" not in dict(copy.sections)
