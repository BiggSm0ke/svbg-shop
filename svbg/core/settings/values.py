"""Typed setting values: parsing owner input, validation, text/JSON forms.

One place converts between the three representations of a setting value:

* **text** — what the owner types in the bot or writes in ``.env`` (``"30d"``, ``"да"``, ``"1,2,3"``);
* **typed** — the canonical Python value kept in the snapshot (``int`` seconds, ``bool``, ``list[int]`` …);
* **JSON** — what is stored in the ``settings`` table (secrets are encrypted by the store, not here).

Every error is a :class:`SettingValueError` with an owner-facing Russian message; it never contains the
offending value of a secret.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlsplit

from svbg.core.log import redact

if TYPE_CHECKING:
    from svbg.core.settings.registry import SettingDef

__all__ = [
    "KINDS",
    "SECRET_PLACEHOLDER",
    "SettingValueError",
    "coerce",
    "display",
    "from_json",
    "is_unchanged_marker",
    "kind_of",
    "parse",
    "parse_or_default",
    "same_value",
    "to_json",
    "to_text",
    "validate",
]

#: Supported value kinds (``SettingDef.type`` may be a Python type or one of these names).
KINDS: Final = frozenset(
    {"int", "bool", "str", "float", "secret", "list[str]", "list[int]", "enum", "url", "duration"}
)
_TYPE_NAMES: Final[dict[object, str]] = {int: "int", bool: "bool", str: "str", float: "float"}

#: Written instead of a secret in ``.env`` when ``ENV_SECRETS=omit``; reading it back means "unchanged".
SECRET_PLACEHOLDER: Final = "<хранится зашифрованно в БД>"  # noqa: S105 - a marker, not a password
_MASK_DOTS: Final = "\N{BULLET}" * 8

_TRUE: Final = frozenset({"1", "true", "yes", "y", "on", "да", "д", "вкл", "включено", "+"})
_FALSE: Final = frozenset({"0", "false", "no", "n", "off", "нет", "н", "выкл", "выключено", "-"})
_INT_RE: Final = re.compile(r"[+-]?\d{1,18}")
_DURATION_PART_RE: Final = re.compile(r"(\d{1,9})\s*(w|d|h|m|s|н|д|ч|м|с)", re.IGNORECASE)
_DURATION_UNITS: Final = {
    "w": 604800,
    "н": 604800,
    "d": 86400,
    "д": 86400,
    "h": 3600,
    "ч": 3600,
    "m": 60,
    "м": 60,
    "s": 1,
    "с": 1,
}
_MAX_TEXT: Final = 4096
_MAX_LIST: Final = 200

# Owner-facing messages (Russian).
_M = {
    "int": "ожидалось целое число",
    "float": "ожидалось число",
    "bool": "ожидалось да/нет (true/false, on/off, 1/0)",
    "required": "значение обязательно",
    "too_long": "слишком длинное значение (максимум {n} символов)",
    "newline": "значение не должно содержать переводов строки",
    "choice": "допустимые значения: {choices}",
    "min": "значение меньше минимума ({min})",
    "max": "значение больше максимума ({max})",
    "url": "ожидался адрес вида https://example.com",
    "duration": "ожидалась длительность вида 30d, 12h, 1h30m или число секунд",
    "list_int": "ожидался список целых чисел через запятую",
    "list_item": "пустой элемент списка",
    "list_len": "слишком много элементов (максимум {n})",
    "type": "неверный тип значения",
    "invalid": "недопустимое значение",
}


class SettingValueError(ValueError):
    """A value cannot be used for a setting. ``str(exc)`` is owner-facing (Russian, no secret values)."""


def kind_of(defn: SettingDef) -> str:
    """Normalized kind name of a definition (``int``, ``list[str]``, ``secret`` …)."""
    raw = defn.type
    kind = _TYPE_NAMES.get(raw) if not isinstance(raw, str) else raw
    if kind is None or kind not in KINDS:
        raise TypeError(f"{defn.key}: unsupported setting type {raw!r}")
    return kind


def is_unchanged_marker(defn: SettingDef, raw: object) -> bool:
    """True for a secret placeholder sent back from the UI/file: it means "keep the current value"."""
    if not defn.is_secret or not isinstance(raw, str):
        return False
    text = raw.strip()
    return text == SECRET_PLACEHOLDER or text.startswith(_MASK_DOTS)


# --------------------------------------------------------------------------------------------- parsing


def parse(defn: SettingDef, raw: str) -> Any:
    """Parse owner text into a typed value and validate it. Empty text → ``None`` (only if nullable)."""
    if not isinstance(raw, str):
        raise SettingValueError(_M["type"])
    text = raw.strip()
    if len(text) > _MAX_TEXT:
        raise SettingValueError(_M["too_long"].format(n=_MAX_TEXT))
    if text == "":
        if defn.nullable:
            return None
        raise SettingValueError(_M["required"])
    value = _parse_kind(kind_of(defn), text)
    validate(defn, value)
    return value


def parse_or_default(defn: SettingDef, raw: str) -> Any:
    """Like :func:`parse`, but blank text of a non-nullable key means "the default" (``KEY=`` in a file)."""
    if isinstance(raw, str) and raw.strip() == "" and not defn.nullable:
        return defn.default
    return parse(defn, raw)


def _parse_kind(kind: str, text: str) -> Any:
    if kind == "int":
        return _parse_int(text)
    if kind == "float":
        return _parse_float(text)
    if kind == "bool":
        return _parse_bool(text)
    if kind == "duration":
        return _parse_duration(text)
    if kind == "list[str]":
        return _split_list(text)
    if kind == "list[int]":
        try:
            return [_parse_int(item) for item in _split_list(text)]
        except SettingValueError:
            raise SettingValueError(_M["list_int"]) from None
    if kind == "url":
        return text.rstrip("/") if text.count("/") > 2 else text  # keep "https://host" as is
    return text  # str, secret, enum


def _parse_int(text: str) -> int:
    cleaned = text.replace("_", "").replace(" ", "").replace(" ", "")
    if not _INT_RE.fullmatch(cleaned):
        raise SettingValueError(_M["int"])
    return int(cleaned)


def _parse_float(text: str) -> float:
    try:
        value = float(text.replace(",", ".").replace(" ", ""))
    except ValueError:
        raise SettingValueError(_M["float"]) from None
    if not math.isfinite(value):
        raise SettingValueError(_M["float"])
    return value


def _parse_bool(text: str) -> bool:
    low = text.casefold()
    if low in _TRUE:
        return True
    if low in _FALSE:
        return False
    raise SettingValueError(_M["bool"])


def _parse_duration(text: str) -> int:
    compact = text.replace(" ", "")
    if _INT_RE.fullmatch(compact):
        return int(compact)
    total = 0
    pos = 0
    for match in _DURATION_PART_RE.finditer(compact):
        if match.start() != pos:
            raise SettingValueError(_M["duration"])
        total += int(match.group(1)) * _DURATION_UNITS[match.group(2).lower()]
        pos = match.end()
    if pos != len(compact) or pos == 0:
        raise SettingValueError(_M["duration"])
    return total


def _split_list(text: str) -> list[str]:
    items = [item.strip() for item in text.split(",")]
    items = [item for item in items if item]
    if len(items) > _MAX_LIST:
        raise SettingValueError(_M["list_len"].format(n=_MAX_LIST))
    return items


# --------------------------------------------------------------------------------------------- typed input


def coerce(defn: SettingDef, value: Any) -> Any:
    """Accept a typed value (from code, import, the wizard); text is parsed. Returns the canonical value."""
    if isinstance(value, str):
        return parse(defn, value)
    if value is None:
        if defn.nullable:
            return None
        raise SettingValueError(_M["required"])
    kind = kind_of(defn)
    result: Any
    if kind in {"int", "duration"}:
        if isinstance(value, bool) or not isinstance(value, int):
            raise SettingValueError(_M["int"] if kind == "int" else _M["duration"])
        result = value
    elif kind == "float":
        if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
            raise SettingValueError(_M["float"])
        result = float(value)
    elif kind == "bool":
        if not isinstance(value, bool):
            raise SettingValueError(_M["bool"])
        result = value
    elif kind in {"list[str]", "list[int]"}:
        if not isinstance(value, list | tuple):
            raise SettingValueError(_M["type"])
        result = _coerce_list(kind, value)
    else:
        raise SettingValueError(_M["type"])
    validate(defn, result)
    return result


def _coerce_list(kind: str, items: Sequence[Any]) -> list[Any]:
    if len(items) > _MAX_LIST:
        raise SettingValueError(_M["list_len"].format(n=_MAX_LIST))
    if kind == "list[int]":
        if any(isinstance(i, bool) or not isinstance(i, int) for i in items):
            raise SettingValueError(_M["list_int"])
        return list(items)
    if any(not isinstance(i, str) for i in items):
        raise SettingValueError(_M["type"])
    out = [i.strip() for i in items]
    if any(not i or "," in i for i in out):
        raise SettingValueError(_M["list_item"])
    return out


# --------------------------------------------------------------------------------------------- validation


def validate(defn: SettingDef, value: Any) -> None:
    """Range, choices, format and the definition's own validator. ``None`` is valid only if nullable."""
    if value is None:
        if not defn.nullable:
            raise SettingValueError(_M["required"])
        return
    kind = kind_of(defn)
    if kind in {"str", "secret", "enum", "url"}:
        if not isinstance(value, str):
            raise SettingValueError(_M["type"])
        if "\n" in value or "\r" in value:
            raise SettingValueError(_M["newline"])
        if len(value) > _MAX_TEXT:
            raise SettingValueError(_M["too_long"].format(n=_MAX_TEXT))
        if kind == "url":
            _check_url(value)
    if defn.choices is not None:
        items = value if isinstance(value, list) else [value]
        bad = [item for item in items if str(item) not in defn.choices]
        if bad:
            raise SettingValueError(_M["choice"].format(choices=" | ".join(defn.choices)))
    if isinstance(value, int | float) and not isinstance(value, bool):
        if defn.min is not None and value < defn.min:
            raise SettingValueError(_M["min"].format(min=_num(defn.min)))
        if defn.max is not None and value > defn.max:
            raise SettingValueError(_M["max"].format(max=_num(defn.max)))
    if defn.validator is not None:
        try:
            defn.validator(value)
        except SettingValueError:
            raise
        except (ValueError, TypeError) as exc:
            message = str(exc) or _M["invalid"]
            if defn.is_secret and str(value) in message:
                message = _M["invalid"]  # never echo a secret back
            raise SettingValueError(message) from None


def _check_url(value: str) -> None:
    try:
        parts = urlsplit(value)
    except ValueError:
        raise SettingValueError(_M["url"]) from None
    if parts.scheme not in {"http", "https"} or not parts.hostname or " " in value:
        raise SettingValueError(_M["url"])


def _num(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


# --------------------------------------------------------------------------------------------- output forms


def to_text(defn: SettingDef, value: Any) -> str:
    """Text form for ``.env`` and edit prompts (secrets in clear — callers decide about masking)."""
    if value is None:
        return ""
    kind = kind_of(defn)
    if kind == "bool":
        return "true" if value else "false"
    if kind == "duration":
        return _format_duration(int(value))
    if kind in {"list[str]", "list[int]"}:
        return ",".join(str(item) for item in value)
    if kind == "float":
        return repr(float(value))
    return str(value)


def _format_duration(seconds: int) -> str:
    if seconds == 0:
        return "0"
    out: list[str] = []
    rest = seconds
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        count, rest = divmod(rest, size)
        if count:
            out.append(f"{count}{unit}")
    return "".join(out)


def display(defn: SettingDef, value: Any) -> str:
    """UI-safe short form: secrets as ``••••••••a1B9``, empty as ``—``."""
    if value in (None, "") or value == []:
        return "—"
    if defn.is_secret:
        return redact(str(value))
    return to_text(defn, value)


def to_json(defn: SettingDef, value: Any) -> Any:
    """JSON-compatible form for the database (typed values are already JSON-friendly)."""
    if value is None:
        return None
    kind = kind_of(defn)
    if kind in {"list[str]", "list[int]"}:
        return list(value)
    return value


def from_json(defn: SettingDef, data: Any) -> Any:
    """Typed value from its stored JSON form; raises :class:`SettingValueError` if it no longer fits
    (e.g. the type or range changed in a newer version)."""
    if data is None:
        if defn.nullable:
            return None
        raise SettingValueError(_M["required"])
    return coerce(defn, data) if not isinstance(data, str) else _from_json_str(defn, data)


def _from_json_str(defn: SettingDef, data: str) -> Any:
    kind = kind_of(defn)
    if kind in {"str", "secret", "enum", "url"}:
        validate(defn, data)
        return data
    return parse(defn, data)


def same_value(defn: SettingDef, left: str | None, right: str | None) -> bool:
    """Whether two text forms mean the same value (``"05"`` == ``"5"``); unparsable text compares raw."""
    if left == right:
        return True
    if left is None or right is None:
        return False
    try:
        return _canonical(defn, left) == _canonical(defn, right)
    except SettingValueError:
        return left.strip() == right.strip()


def _canonical(defn: SettingDef, text: str) -> Any:
    stripped = text.strip()
    if stripped == "":
        return None
    return _parse_kind(kind_of(defn), stripped)
