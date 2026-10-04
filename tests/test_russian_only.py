"""The bot speaks Russian only (the owner removed English).

What is checked:

* no English tables are left anywhere in ``svbg`` (``*_EN`` names, ``{"ru": …, "en": …}`` tables, language
  lists), and the seeded screens and buttons carry Russian only;
* every text lookup that still takes a language for old callers answers in Russian whatever it is given;
* every key the code looks up exists (a typo would otherwise reach a user as a ``KeyError``).

Old ``en`` texts may still sit in the database (screens, pages, plan names): they are kept but never shown.
"""

from __future__ import annotations

import ast
import importlib
import pkgutil
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import svbg

_ROOT = Path(svbg.__file__).resolve().parent
_CYRILLIC = re.compile("[А-Яа-яЁё]")
_EN_NAME = re.compile(r"(^|_)EN$")
_LANG_LISTS = frozenset({"LANGUAGES", "LANGS", "SUPPORTED_LANGS"})


def _modules() -> list[ModuleType]:
    out = []
    for info in pkgutil.walk_packages(svbg.__path__, "svbg."):
        if info.name.endswith("__main__") or ".migrations" in info.name:
            continue
        out.append(importlib.import_module(info.name))
    return out


def _is_language_table(value: Any) -> bool:
    """``{"ru": …, "en": …}`` itself, or a table whose values are such pairs (choice labels by key)."""
    if not isinstance(value, Mapping) or not all(isinstance(k, str) for k in value):
        return False
    if "en" in value and "ru" in value:
        return True
    return any(isinstance(v, Mapping) and "en" in v and "ru" in v for v in value.values())


def test_no_english_tables_are_left() -> None:
    found: list[str] = []
    for module in _modules():
        for name, value in vars(module).items():
            if getattr(value, "__module__", module.__name__) != module.__name__:
                continue  # imported from elsewhere: checked in its own module
            where = f"{module.__name__}.{name}"
            if _EN_NAME.search(name) or name in _LANG_LISTS or _is_language_table(value):
                found.append(where)
    assert not found, f"English left in: {found}"


def test_seed_screens_and_buttons_are_russian_only() -> None:
    from svbg.content import defaults

    problems: list[str] = []
    for seed in defaults.SYSTEM_SCREENS:
        where = f"screen {seed.code!r}"
        if set(seed.title) != {"ru"} or set(seed.body) != {"ru"}:
            problems.append(f"{where}: languages {sorted({*seed.title, *seed.body})}")
        text = str(seed.body.get("ru", {}).get("text") or "")
        if not _CYRILLIC.search(text):
            problems.append(f"{where}: the text is not Russian")
        for button in seed.buttons:
            if set(button.label) != {"ru"}:
                problems.append(f"{where} button {button.system_key!r}: languages {sorted(button.label)}")
    assert not problems, "\n".join(problems)


# ------------------------------------------------------------------------------------------- lookups


def _lookups() -> list[tuple[str, Callable[[str | None, str], str], list[str]]]:
    """``(name, lookup(lang, key), keys)``: text tables that keep a language argument for old callers."""
    from svbg.ext.ip_guard import texts as ipg
    from svbg.referral import texts as referral
    from svbg.support import service as support
    from svbg.tg.ui import texts as ui

    def ref_user(lang: str | None, key: str) -> str:
        return referral.render(key, lang, **_dummy(referral.USER[key]))

    def ref_screen(lang: str | None, key: str) -> str:
        return referral.render(key, lang, table=referral.SCREEN, **_dummy(referral.SCREEN[key]))

    return [
        ("svbg.tg.ui.texts.t", ui.t, sorted(ui.TEXTS)),
        ("svbg.support.service.t", support.t, sorted(support.TEXTS)),
        ("svbg.referral.texts.USER", ref_user, sorted(referral.USER)),
        ("svbg.referral.texts.SCREEN", ref_screen, sorted(referral.SCREEN)),
        ("svbg.ext.ip_guard.texts.user_t", lambda lang, key: ipg.user_t(key, lang), list(ipg.USER_KEYS)),
    ]


def _dummy(template: str) -> dict[str, str]:
    return {name: "1" for name in re.findall(r"\{(\w+)\}", template)}


@pytest.mark.parametrize("lang", ["en", "ru", None, "de"])
def test_lookups_answer_in_russian_whatever_language_is_passed(lang: str | None) -> None:
    for name, lookup, keys in _lookups():
        assert keys, name
        for key in keys:
            text = lookup(lang, key)
            assert text == lookup("ru", key), f"{name}[{key!r}] depends on the language"
            bare = re.sub(r"[{]\w+[}]|<[^>]+>", "", text)  # placeholders and HTML tags are not words
            assert _CYRILLIC.search(bare) or not re.search("[A-Za-z]{3}", bare), f"{name}[{key!r}]: {text!r}"


def test_old_language_arguments_are_accepted_and_ignored() -> None:
    """Compat shims kept for callers that still pass a language (or an English twin)."""
    from svbg.billing.checkout import BillingError
    from svbg.catalog.model import pick_lang
    from svbg.content.model import Screen
    from svbg.core.money import format_money
    from svbg.domain.pricing import PricingError
    from svbg.ext.lte import notify as lte_notify
    from svbg.ext.lte.packs import REFUSALS, Availability
    from svbg.payments.core import CheckoutError
    from svbg.payments.providers.manual import receipt_problem
    from svbg.subscriptions.devices import ActionResult
    from svbg.subscriptions.trial import TRIAL_TEXTS, TrialResult, trial_text
    from svbg.tg.ui.context import UserCtx
    from svbg.tg.ui.forms import ValidationError

    assert BillingError("order_gone").localized("en") == BillingError("order_gone").text
    assert _CYRILLIC.search(BillingError("order_gone").text)
    assert BillingError("pricing", "Неверный срок", "Invalid period").localized("en") == "Неверный срок"
    assert str(PricingError("Неверный срок", "Invalid period")) == "Неверный срок"
    assert CheckoutError("Касса недоступна", human_en="Down").localized("en") == "Касса недоступна"
    assert str(ValidationError("Нужно целое число", "A whole number is needed")) == "Нужно целое число"
    assert ActionResult(False, "cooldown", 600).localized("en") == "Слишком часто. Попробуйте через 10 мин."
    assert TrialResult(False, "used").localized("en") == TRIAL_TEXTS["used"] == trial_text("used", "en")
    assert receipt_problem("document", mime_type="text/plain", lang="en") == receipt_problem("document")
    for code, text in REFUSALS.items():
        refusal = Availability.refuse(code, date="07.10")
        assert refusal.localized("en") == refusal.text == (text.format(date="07.10") if text else None)
    assert format_money(169950, "RUB", "en") == "1 699,50 ₽"
    assert lte_notify.fmt_gb(12_400_000_000, 10**9, "en") == "12,4"
    assert lte_notify.user_texts("en") is lte_notify.T
    assert pick_lang({"ru": "Стандарт", "en": "Standard"}, "en") == "Стандарт"
    screen = Screen.from_row({"id": 1, "code": "x", "body": {"ru": "привет", "en": "hello"}})
    assert screen.text("en").text == "привет"
    assert UserCtx(1, lang="en").lang == "ru"  # a stored English language never reaches a screen


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


def test_every_text_key_used_in_code_exists() -> None:
    from svbg.support import service as support
    from svbg.tg.ui import texts as ui
    from svbg.tg.user import texts as user_texts

    def user_lookup(key: str) -> object:
        return user_texts.t(None, key)

    #: ``(files, names of the lookup function, lookup by key)``
    lookups: tuple[tuple[list[Path], set[str], Callable[[str], object]], ...] = (
        (sorted((_ROOT / "tg" / "user").glob("*.py")), {"t"}, user_lookup),
        ([_ROOT / "support" / "ui.py"], {"user_t"}, user_lookup),
        ([_ROOT / "support" / "ui.py", _ROOT / "support" / "service.py"], {"t"}, support.TEXTS.__getitem__),
        (
            [*sorted((_ROOT / "tg" / "ui").glob("*.py")), _ROOT / "tg" / "admin" / "connect_chat.py"],
            {"texts.t", "ui_texts.t"},
            ui.TEXTS.__getitem__,
        ),
    )
    problems: list[str] = []
    for files, names, lookup in lookups:
        for path in files:
            for line, key in _called_keys(path, names):
                try:
                    lookup(key)
                except KeyError:
                    problems.append(f"{path.name}:{line} lookup of {key!r} has no text")
    assert not problems, "\n".join(problems)
