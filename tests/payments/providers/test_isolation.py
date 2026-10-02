"""Wave A plugins as a set: SDK-only imports, catalog registration, settings keys, provenance and notices."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from svbg.payments.providers import BUILTIN_PROVIDERS
from svbg.payments.registry import ProviderCatalog, instance_setting_defs
from svbg.payments.testkit import check_static
from svbg.sdk import SDK_VERSION, PaymentProvider

ROOT = Path(__file__).resolve().parents[3]
PLUGIN_DIR = ROOT / "svbg" / "payments" / "providers"
PLUGIN_FILES = sorted(p for p in PLUGIN_DIR.glob("*.py") if p.name != "__init__.py")


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, f"{path.name}: relative import"
            names.add(node.module or "")
    return names


@pytest.mark.parametrize("path", PLUGIN_FILES, ids=[p.stem for p in PLUGIN_FILES])
def test_plugins_import_only_the_sdk_and_stdlib(path: Path) -> None:
    for name in _imports(path):
        top = name.split(".")[0]
        if top == "svbg":
            assert name == "svbg.sdk" or name.startswith("svbg.sdk."), f"{path.name} imports {name}"
        else:
            assert top in sys.stdlib_module_names or top == "__future__", f"{path.name} imports {name}"


def test_every_plugin_file_is_a_builtin_provider() -> None:
    modules = {cls.__module__.rsplit(".", 1)[-1] for cls in BUILTIN_PROVIDERS}
    assert modules == {p.stem for p in PLUGIN_FILES}


def test_catalog_accepts_all_builtin_providers() -> None:
    catalog = ProviderCatalog(BUILTIN_PROVIDERS)
    assert catalog.slugs() == [
        "rollypay",
        "stars",
        "cryptobot",
        "manual",
        "yookassa",
        "platega",
        "freekassa",
        "yoomoney",
        "robokassa",
        "cryptomus",
        "heleket",
        "wata",
        "mulenpay",
        "pal24",
        "lava",
        "tribute",
        "cloudpayments",
        "riopay",
        "severpay",
        "paypear",
        "overpay",
        "aurapay",
        "etoplatezhi",
        "antilopay",
        "cispay",
        "tabpay",
        "paritypay",
    ]
    for cls in BUILTIN_PROVIDERS:
        check_static(cls)
        assert cls.manifest.sdk == SDK_VERSION


@pytest.mark.parametrize("cls", BUILTIN_PROVIDERS, ids=[c.manifest.slug for c in BUILTIN_PROVIDERS])
def test_settings_keys(cls: type[PaymentProvider]) -> None:
    slug = cls.manifest.slug
    defs = {d.key: d for d in instance_setting_defs(slug, cls)}
    for field_def in cls.manifest.config.fields().values():
        key = f"PAY_{slug.upper()}_{field_def.env_suffix}"
        assert key in defs and defs[key].is_secret is field_def.is_secret
        assert defs[key].description and (field_def.where is None or field_def.where in defs[key].description)
    assert {f"PAY_{slug.upper()}_{s}" for s in ("ENABLED", "TEST_MODE", "PROXY_URL")} <= set(defs)


def test_expected_keys_exist() -> None:
    keys = {d.key for cls in BUILTIN_PROVIDERS for d in instance_setting_defs(cls.manifest.slug, cls)}
    assert {
        "PAY_ROLLYPAY_API_KEY",
        "PAY_ROLLYPAY_SIGNING_SECRET",
        "PAY_ROLLYPAY_BASE_URL",
        "PAY_STARS_RATE",
        "PAY_CRYPTOBOT_API_TOKEN",
        "PAY_MANUAL_DETAILS",
    } <= keys


def test_secret_fields_are_secrets() -> None:
    secret_fields = {
        (cls.manifest.slug, name)
        for cls in BUILTIN_PROVIDERS
        for name, fld in cls.manifest.config.fields().items()
        if fld.is_secret
    }
    assert secret_fields == {
        ("rollypay", "api_key"),
        ("rollypay", "signing_secret"),
        ("cryptobot", "api_token"),
        # wave B
        ("yookassa", "secret_key"),
        ("platega", "api_secret"),
        ("freekassa", "secret_word"),
        ("freekassa", "secret_word_2"),
        ("freekassa", "api_key"),
        ("yoomoney", "notification_secret"),
        ("robokassa", "password1"),
        ("robokassa", "password2"),
        ("cryptomus", "api_key"),
        ("heleket", "api_key"),
        ("wata", "api_key"),
        ("mulenpay", "api_key"),
        ("mulenpay", "secret_key"),
        # waves C-D
        ("pal24", "api_token"),
        ("lava", "secret_key"),
        ("lava", "additional_key"),
        ("tribute", "api_key"),
        ("cloudpayments", "api_secret"),
        ("riopay", "api_token"),
        ("severpay", "token"),
        ("paypear", "secret_key"),
        ("overpay", "secret_key"),
        ("aurapay", "api_key"),
        ("aurapay", "webhook_secret"),
        ("etoplatezhi", "secret_key"),
        ("antilopay", "private_key"),
        ("cispay", "api_key"),
        ("tabpay", "api_key"),
        ("tabpay", "webhook_secret"),
        ("paritypay", "api_key"),
        ("paritypay", "webhook_key"),
    }


def test_every_provider_has_a_setup_page() -> None:
    for cls in BUILTIN_PROVIDERS:
        slug = cls.manifest.slug
        assert (ROOT / "docs" / "providers" / f"{slug}.md").is_file(), f"no setup page for {slug}"


def test_provenance_and_specs() -> None:
    specs = ROOT / "docs" / "spec" / "providers"
    if not (specs / "PROVENANCE.md").is_file():
        pytest.skip("internal provider specs are not part of this checkout")
    provenance = (specs / "PROVENANCE.md").read_text(encoding="utf-8")
    for cls in BUILTIN_PROVIDERS:
        slug = cls.manifest.slug
        assert f"| `{slug}` |" in provenance, f"no provenance row for {slug}"
        assert (specs / f"{slug}.md").is_file(), f"no specification for {slug}"
    assert "Требует сверки владельцем" in provenance


def test_third_party_notices_cover_the_ports() -> None:
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    assert "Copyright (c) 2024 snoups" in notices and "MIT" in notices
    for module in ("stars.py", "cryptobot.py"):
        assert f"svbg/payments/providers/{module}" in notices
        source = (PLUGIN_DIR / module).read_text(encoding="utf-8")
        assert "Remnashop (MIT" in source and "THIRD_PARTY_NOTICES.md" in source
