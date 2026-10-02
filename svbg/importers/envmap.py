"""Generic helpers for importing another bot's configuration (06 §3.1): ``.env`` parsing, the effective
value of a key from several sources, typed conversion of foreign values and the import outcome records.

Parsing follows python-dotenv as the source bot read its file (06 §3.1):

* ``KEY=value`` and ``export KEY=value``; blank lines and ``# comment`` lines are skipped;
* an inline comment `` # …`` after an unquoted value is cut off;
* an unquoted value that *starts* with ``#`` is empty (python-dotenv reads it as a comment);
* single or double quotes are removed (``\\n``, ``\\"`` unescaped in double quotes);
  ``#`` inside quotes stays;
* a repeated key: the last line wins (python-dotenv semantics), the repetition is reported.

The effective value of a key is the first source that has it (06 §3.1: the environment wins over the
database row of the source bot, a missing key falls back to the bot's own default, i.e. it is not imported).

Nothing here knows a particular bot: the Bedolaga mapping lives in ``svbg.importers.bedolaga.settings_map``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

__all__ = [
    "EnvFile",
    "NotTransferred",
    "PlannedChange",
    "SourceValue",
    "ValueParseError",
    "effective_values",
    "is_masked",
    "parse_env",
    "read_env_file",
    "to_bool",
    "to_int",
    "to_int_list",
    "to_str_list",
    "unconsumed",
]

_KEY_RE: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")
_TRUE: Final = frozenset({"1", "true", "yes", "y", "on", "да", "вкл"})
_FALSE: Final = frozenset({"0", "false", "no", "n", "off", "нет", "выкл", ""})
_INT_RE: Final = re.compile(r"[+-]?\d{1,18}")
# Masked values in an owner's "secrets removed" dump: ***, ••••, <masked>, [REDACTED], xxxxx…
_MASK_RE: Final = re.compile(
    r"^(?:\*{3,}.*|.*\*{3,}|[•●]{3,}.*|<\s*(?:masked|hidden|secret|redacted)\s*>|\[(?:masked|redacted)\]"
    r"|(?:masked|redacted|hidden)|x{5,})$",
    re.IGNORECASE,
)


class ValueParseError(ValueError):
    """A foreign value cannot be converted (owner-facing Russian message)."""


@dataclass(frozen=True, slots=True)
class EnvFile:
    """A parsed ``.env``: ``values`` (last line wins), the line of every key, repeated keys, bad lines."""

    values: dict[str, str]
    lines: dict[str, int]
    duplicates: tuple[str, ...] = ()
    bad_lines: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class SourceValue:
    """The effective value of a foreign key and where it came from (``origin`` e.g. ``env:42``)."""

    key: str
    raw: str | None
    origin: str


@dataclass(frozen=True, slots=True)
class PlannedChange:
    """A value for one of our settings, computed from one or more foreign keys."""

    key: str  # our canonical key (``TRIAL_DAYS``)
    raw: str  # text form, parsed by our registry (``values.coerce``)
    sources: tuple[str, ...]  # foreign keys it was computed from
    secret: bool = False
    note: str = ""  # e.g. why the value differs from the foreign one, or when to apply a deferred change


@dataclass(frozen=True, slots=True)
class NotTransferred:
    """A foreign key that is not imported, with the owner-facing reason («не перенесено»)."""

    key: str
    reason: str
    origin: str = ""
    category: str = "skipped"  # skipped | dead | secret | data | deferred | invalid | unknown | conflict


@dataclass(slots=True)
class _Parser:
    values: dict[str, str] = field(default_factory=dict)
    lines: dict[str, int] = field(default_factory=dict)
    duplicates: list[str] = field(default_factory=list)
    bad: list[int] = field(default_factory=list)


def parse_env(text: str) -> EnvFile:
    """Parse ``.env`` text the way python-dotenv does for simple files (see the module docstring)."""
    out = _Parser()
    raw_lines = text.lstrip("﻿").splitlines()
    i = 0
    while i < len(raw_lines):
        lineno = i + 1
        line = raw_lines[i].strip()
        i += 1
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, rest = line.partition("=")
        key = key.strip()
        if not sep or not _KEY_RE.fullmatch(key):
            out.bad.append(lineno)
            continue
        rest = rest.strip()
        if rest[:1] in ("'", '"'):
            quote = rest[0]
            body = rest[1:]
            # A quoted value may continue on the next lines until the closing quote.
            while _closing_quote(body, quote) < 0 and i < len(raw_lines):
                body += "\n" + raw_lines[i]
                i += 1
            end = _closing_quote(body, quote)
            if end < 0:
                out.bad.append(lineno)
                continue
            value = body[:end]
            if quote == '"':
                value = _unescape(value)
        elif rest.startswith("#"):
            value = ""
        else:
            value = re.split(r"\s+#", rest, maxsplit=1)[0].rstrip()
        if key in out.values:
            out.duplicates.append(key)
        out.values[key] = value
        out.lines[key] = lineno
    return EnvFile(out.values, out.lines, tuple(dict.fromkeys(out.duplicates)), tuple(out.bad))


def _closing_quote(body: str, quote: str) -> int:
    j = 0
    while j < len(body):
        ch = body[j]
        if ch == "\\" and quote == '"':
            j += 2
            continue
        if ch == quote:
            return j
        j += 1
    return -1


def _unescape(value: str) -> str:
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t", "r": "\r"}.get(m.group(1), m.group(1)), value)


def read_env_file(path: str | Path) -> EnvFile:
    return parse_env(Path(path).read_text(encoding="utf-8-sig"))


def effective_values(
    *sources: tuple[str, Mapping[str, str | None], Mapping[str, int] | None],
) -> dict[str, SourceValue]:
    """Merge ``(origin, values, lines)`` sources; an earlier source wins for a key present in several.

    ``origin`` of a value is ``"<origin>:<line>"`` when ``lines`` knows the key, else ``"<origin>"``.
    """
    out: dict[str, SourceValue] = {}
    for prefix, mapping, lines in sources:
        for key, raw in mapping.items():
            if key in out:
                continue
            line = (lines or {}).get(key)
            out[key] = SourceValue(key, raw, f"{prefix}:{line}" if line else prefix)
    return out


def is_masked(raw: str | None) -> bool:
    """True for a placeholder that stands for a removed secret (the value must be entered again)."""
    return raw is not None and bool(_MASK_RE.match(raw.strip()))


def to_bool(raw: str | None) -> bool:
    text = (raw or "").strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ValueParseError(f"ожидалось true/false, получено «{_short(raw)}»")


def to_int(raw: str | None) -> int:
    text = (raw or "").strip()
    if not _INT_RE.fullmatch(text):
        raise ValueParseError(f"ожидалось целое число, получено «{_short(raw)}»")
    return int(text)


def to_str_list(raw: str | None) -> list[str]:
    """``a,b; c`` or a JSON-ish ``["a", "b"]`` → ``["a", "b", "c"]`` (empty items dropped, order kept)."""
    text = (raw or "").strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    items = [part.strip().strip("'\"").strip() for part in re.split(r"[,;\s]+", text)]
    return list(dict.fromkeys(item for item in items if item))


def to_int_list(raw: str | None) -> list[int]:
    return [to_int(item) for item in to_str_list(raw)]


def _short(raw: str | None) -> str:
    text = "" if raw is None else str(raw)
    return text if len(text) <= 40 else text[:37] + "…"


def unconsumed(keys: Iterable[str], consumed: Iterable[str]) -> list[str]:
    """Keys of ``keys`` not in ``consumed``, sorted (for the «не перенесено» list)."""
    done = set(consumed)
    return sorted(k for k in keys if k not in done)
