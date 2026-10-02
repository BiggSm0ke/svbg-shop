"""English coverage of everything the *user* sees (the admin interface stays Russian).

Russian is the source, English the second language; an unknown key or language falls back to Russian. So every
user-facing table must have the same keys in both languages, with the same ``{placeholders}`` and the same
HTML tags, and the screen / button seeds must carry an ``en`` variant of every field. A new Russian string
without its English twin fails here instead of reaching an English-speaking user as Russian.
"""

from __future__ import annotations

import ast
import importlib
import pkgutil
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

import svbg

_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_TAG = re.compile(r"</?[A-Za-z][^>]*>")
_CYRILLIC = re.compile("[А-Яа-яЁё]")
_ROOT = Path(svbg.__file__).resolve().parent

#: ``(module, attribute)`` of ``{"ru": {key: text}, "en": {key: text}}`` tables.
LANG_TABLES: tuple[tuple[str, str], ...] = (
    ("svbg.tg.user.texts", "_TEXTS"),
    ("svbg.tg.ui.texts", "_TEXTS"),
    ("svbg.billing.texts", "_TEXTS"),
    ("svbg.referral.texts", "USER"),
    ("svbg.referral.texts", "SCREEN"),
    ("svbg.promo.user", "TEXTS"),
    ("svbg.pages.user", "_T"),
    ("svbg.support.service", "TEXTS"),
    ("svbg.app_modules", "_INVITE_T"),
)

#: ``(module, russian attribute, english attribute)`` — two tables of the same keys.
PAIR_TABLES: tuple[tuple[str, str, str], ...] = (
    ("svbg.billing.texts", "ERRORS", "ERRORS_EN"),
    ("svbg.ext.lte.ui", "T", "T_EN"),
    ("svbg.ext.lte.notify", "T", "T_EN"),
    ("svbg.ext.lte.packs", "REFUSALS", "REFUSALS_EN"),
    ("svbg.promo.rules", "REFUSALS", "REFUSALS_EN"),
    ("svbg.deeplinks.service", "TEXTS", "TEXTS_EN"),
    ("svbg.tg.user.start", "TEXTS", "TEXTS_EN"),
    ("svbg.subscriptions.trial", "TRIAL_TEXTS", "TRIAL_TEXTS_EN"),
    ("svbg.subscriptions.devices", "ACTION_TEXTS", "ACTION_TEXTS_EN"),
    ("svbg.subscriptions.hold", "SPEND_TEXTS", "SPEND_TEXTS_EN"),
)

#: English values that are meant to be Cyrillic (the name of a language in its own language).
_CYRILLIC_OK: frozenset[tuple[str, str]] = frozenset({("svbg.tg.user.texts._TEXTS", "btn_lang_ru")})


def _attr(module: str, name: str) -> Any:
    return getattr(importlib.import_module(module), name)


def _placeholders(text: str | None) -> list[str]:
    return sorted(_PLACEHOLDER.findall(text or ""))


def _tags(text: str | None) -> list[str]:
    return [re.sub(r"\s+", " ", tag).strip().lower() for tag in _TAG.findall(text or "")]


def _same_texts(where: str, ru: Mapping[str, str | None], en: Mapping[str, str | None]) -> None:
    """Same keys, same placeholders, same HTML tags, no Russian left in the English text."""
    missing = sorted(set(ru) - set(en))
    extra = sorted(set(en) - set(ru))
    assert not missing, f"{where}: no English text for {missing}"
    assert not extra, f"{where}: English keys without a Russian source {extra}"
    for key, ru_text in ru.items():
        en_text = en[key]
        assert (ru_text is None) == (en_text is None), f"{where}[{key!r}]: one language has no text"
        if ru_text is None or en_text is None:
            continue
        assert en_text.strip(), f"{where}[{key!r}]: the English text is empty"
        assert _placeholders(ru_text) == _placeholders(en_text), f"{where}[{key!r}]: placeholders differ"
        assert _tags(ru_text) == _tags(en_text), f"{where}[{key!r}]: HTML tags differ"
        if (where, key) not in _CYRILLIC_OK:
            assert not _CYRILLIC.search(en_text), f"{where}[{key!r}]: Russian letters in the English text"


@pytest.mark.parametrize(("module", "name"), LANG_TABLES, ids=str)
def test_language_tables_have_the_same_keys(module: str, name: str) -> None:
    table = _attr(module, name)
    assert set(table) >= {"ru", "en"}, f"{module}.{name}: both languages are required"
    _same_texts(f"{module}.{name}", table["ru"], table["en"])


@pytest.mark.parametrize(("module", "ru_name", "en_name"), PAIR_TABLES, ids=str)
def test_paired_tables_have_the_same_keys(module: str, ru_name: str, en_name: str) -> None:
    _same_texts(f"{module}.{en_name}", _attr(module, ru_name), _attr(module, en_name))


def test_ip_guard_user_messages_have_english() -> None:
    from svbg.ext.ip_guard import texts

    assert set(texts.USER_KEYS) <= set(texts.T)
    _same_texts(
        "svbg.ext.ip_guard.texts.USER_T_EN", {k: texts.T[k] for k in texts.USER_KEYS}, texts.USER_T_EN
    )
    for key in texts.USER_KEYS:
        assert texts.user_t(key, "en") == texts.USER_T_EN[key]
        assert texts.user_t(key, "ru") == texts.T[key]
        assert texts.user_t(key, "de") == texts.T[key]  # an unknown language falls back to Russian


def test_payment_user_messages_have_english() -> None:
    from svbg.payments import core
    from svbg.payments.providers import manual, stars

    user_keys = (
        "no_instance",
        "currency",
        "too_small",
        "too_large",
        "frozen",
        "create_failed",
        "create_timeout",
    )
    assert set(user_keys) <= set(core._T_EN)
    for key in user_keys:
        assert _placeholders(core._T[key]) == _placeholders(core._T_EN[key]), key
    for key, text in core._T_EN.items():
        assert not _CYRILLIC.search(text), key
    buyer_keys = set(stars.TEXTS) - {"probe_ok"}
    _same_texts(
        "svbg.payments.providers.stars.TEXTS_EN", {k: stars.TEXTS[k] for k in buyer_keys}, stars.TEXTS_EN
    )
    assert set(manual._T_EN) == {"too_big", "bad_kind"}
    _same_texts("svbg.payments.providers.manual._T_EN", {k: manual._T[k] for k in manual._T_EN}, manual._T_EN)


def test_method_icons_name_both_languages() -> None:
    from svbg.tg.user.texts import METHOD_ICONS

    for kind, row in METHOD_ICONS.items():
        icon, ru, en = row
        assert icon and ru and en, kind
        assert not _CYRILLIC.search(en), kind


def test_user_refusal_localizers_fall_back_to_russian() -> None:
    """Every ``localized(lang)`` returns the English text for ``en`` and the Russian one otherwise."""
    from svbg.billing.checkout import BillingError
    from svbg.billing.texts import ERRORS_EN, localize_reason
    from svbg.domain.pricing import PricingError
    from svbg.ext.lte.packs import REFUSALS, REFUSALS_EN, Availability
    from svbg.payments.core import CheckoutError
    from svbg.subscriptions.devices import ActionResult
    from svbg.subscriptions.hold import SPEND_TEXTS, SPEND_TEXTS_EN, localize_spend
    from svbg.subscriptions.trial import TrialResult, trial_text

    for code, en in ERRORS_EN.items():
        assert BillingError(code).localized("en") == en
        assert BillingError(code).localized("ru") == BillingError(code).text
    custom = BillingError("pricing", "Неверный срок", "Invalid period")
    assert custom.localized("en") == "Invalid period"
    assert custom.localized("ru") == "Неверный срок"
    assert BillingError("pricing", "Неверный срок").localized("en") == "Неверный срок"  # no translation given
    assert PricingError("Неверный срок", "Invalid period").en == "Invalid period"
    assert PricingError("Неверный срок").en == "Неверный срок"
    assert CheckoutError("ru", human_en="en").localized("en") == "en"
    assert CheckoutError("ru", human_en="en").localized("ru") == "ru"
    assert CheckoutError("ru").localized("en") == "ru"
    for key, text in SPEND_TEXTS.items():
        assert localize_spend(text, "en") == SPEND_TEXTS_EN[key]
        assert localize_spend(text, "ru") == text
    assert localize_spend("something else", "en") == "something else"
    assert trial_text("used", "en") != trial_text("used", "ru")
    assert trial_text("nope", "en") == ""
    assert TrialResult(False, "used").localized("en") == trial_text("used", "en")
    assert ActionResult(False, "cooldown", 600).localized("en") == "Too often. Please try again in 10 min."
    assert ActionResult(False, "cooldown", 600).localized("ru") == "Слишком часто. Попробуйте через 10 мин."
    for code, text in REFUSALS.items():
        refusal = Availability.refuse(code, date="07.10")
        assert refusal.localized("ru") == (text.format(date="07.10") if text else None)
        en = REFUSALS_EN[code]
        assert refusal.localized("en") == (en.format(date="07.10") if en else None)
    assert localize_reason("Заказ повреждён.", "en") == "The order is corrupted."
    assert localize_reason("Можно не больше 5 устройств на подписку.", "en").startswith("No more than 5")
    assert localize_reason("Заказ повреждён.", "ru") == "Заказ повреждён."


def test_language_tables_found_in_the_code_are_complete() -> None:
    """Safety net: any module-level ``{"ru": …, "en": …}`` table anywhere in ``svbg`` has both languages with
    the same keys — so a new table is covered even if nobody adds it to the lists above."""
    found = 0
    for info in pkgutil.walk_packages(svbg.__path__, "svbg."):
        if info.name.endswith("__main__") or ".migrations" in info.name:
            continue
        module = importlib.import_module(info.name)
        for name, value in vars(module).items():
            if not isinstance(value, Mapping) or "ru" not in value:
                continue
            ru = value["ru"]
            if not isinstance(ru, (Mapping, str)) or not all(isinstance(k, str) for k in value):
                continue
            where = f"{info.name}.{name}"
            assert "en" in value, f"{where}: has «ru» but no «en»"
            if isinstance(ru, Mapping) and all(isinstance(v, str) for v in ru.values()):
                en = value["en"]
                assert isinstance(en, Mapping), f"{where}: «en» is not a table like «ru»"
                _same_texts(where, ru, en)
            found += 1
    assert found >= len(LANG_TABLES)


# ------------------------------------------------------------------------------------------- code → tables


def _called_keys(path: Path, names: set[str]) -> list[tuple[int, str]]:
    """``(line, key)`` of every ``name(lang, "key", …)`` call (``t``, ``user_t``, ``ui_texts.t``…)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or len(node.args) < 2:
            continue
        func = node.func
        label = (
            func.id
            if isinstance(func, ast.Name)
            else (
                f"{func.value.id}.{func.attr}"
                if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                else ""
            )
        )
        key = node.args[1]
        if label in names and isinstance(key, ast.Constant) and isinstance(key.value, str):
            found.append((node.lineno, key.value))
    return found


def test_every_user_text_key_used_in_code_exists_in_both_languages() -> None:
    from svbg.support.service import TEXTS as SUPPORT_TEXTS
    from svbg.tg.ui import texts as ui_texts
    from svbg.tg.user import texts as user_texts

    #: ``(files, names of the lookup function, the table it reads)``
    lookups = (
        (sorted((_ROOT / "tg" / "user").glob("*.py")), {"t"}, user_texts._TEXTS),
        ([_ROOT / "support" / "ui.py"], {"user_t"}, user_texts._TEXTS),
        ([_ROOT / "support" / "ui.py", _ROOT / "support" / "service.py"], {"t"}, SUPPORT_TEXTS),
        (
            [*sorted((_ROOT / "tg" / "ui").glob("*.py")), _ROOT / "tg" / "admin" / "connect_chat.py"],
            {"texts.t", "ui_texts.t"},
            ui_texts._TEXTS,
        ),
    )
    problems: list[str] = []
    for files, names, table in lookups:
        for path in files:
            for line, key in _called_keys(path, names):
                problems += [
                    f"{path.name}:{line} lookup of {key!r} has no {lang} text"
                    for lang in ("ru", "en")
                    if key not in table[lang]
                ]
    assert not problems, "\n".join(problems)


# ------------------------------------------------------------------------------------------- seeds


def _seed_screens() -> list[Any]:
    from svbg.content import defaults

    return list(defaults.SYSTEM_SCREENS)


def _entity_types(block: Mapping[str, Any]) -> list[str]:
    return [str(e.get("type")) for e in block.get("entities") or ()]


@pytest.mark.parametrize("seed", _seed_screens(), ids=lambda s: s.code)
def test_seed_screens_have_english_for_every_field(seed: Any) -> None:
    where = f"screen {seed.code!r}"
    assert {"ru", "en"} <= set(seed.title), f"{where}: title needs ru and en"
    assert {"ru", "en"} <= set(seed.body), f"{where}: body needs ru and en"
    for field, table in (("title", seed.title),):
        assert str(table["en"]).strip(), f"{where}: empty English {field}"
        assert not _CYRILLIC.search(str(table["en"])), f"{where}: Russian letters in the English {field}"
    ru_body, en_body = seed.body["ru"], seed.body["en"]
    assert str(en_body.get("text") or "").strip(), f"{where}: empty English body"
    assert not _CYRILLIC.search(str(en_body["text"])), f"{where}: Russian letters in the English body"
    assert _placeholders(ru_body.get("text")) == _placeholders(en_body.get("text")), f"{where}: placeholders"
    assert _entity_types(ru_body) == _entity_types(en_body), f"{where}: formatting entities differ"
    for button in seed.buttons:
        label = f"{where} button {button.system_key!r}"
        assert {"ru", "en"} <= set(button.label), f"{label}: label needs ru and en"
        assert str(button.label["en"]).strip(), f"{label}: empty English label"
        assert not _CYRILLIC.search(str(button.label["en"])), f"{label}: Russian letters in the English label"
        assert _placeholders(button.label["ru"]) == _placeholders(button.label["en"]), (
            f"{label}: placeholders"
        )


def test_user_seed_screens_cover_every_notification() -> None:
    from svbg.tg.user import seeds

    for code, names in seeds.PLACEHOLDERS.items():
        seed = seeds.SEEDS.get(code)
        if seed is None:
            continue
        for lang in ("ru", "en"):
            text, _entities = seeds.seed_text(code, lang)
            assert text, f"{code}: no {lang} text"
            assert set(_placeholders(text)) <= {*names, "balance", "days_left"}, (
                f"{code}/{lang}: unknown placeholder"
            )
