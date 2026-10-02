"""`.env` parser and renderer that never damages the owner's file.

The model is a list of physical lines (a multi-line quoted value is one `EnvLine`). Every line keeps
its exact original text and line ending, so ``EnvDocument.parse(text).render() == text`` holds for
*any* input, including CRLF, a UTF-8 BOM, broken lines and mixed line endings. Edits (`set`, `remove`,
`annotate_invalid`) touch only the lines they are about and keep the owner's comments, quoting style
and inline comments.

Syntax (a compatible subset of docker compose / python-dotenv):

- ``KEY=value``, ``export KEY=value``, spaces around ``=`` are allowed;
- bare values end at an inline comment (whitespace followed by ``#``); ``a#b`` stays one value;
- ``"double"`` quotes with escapes ``\\\\ \\" \\' \\n \\r \\t \\$`` (others are kept literally) and
  ``'single'`` quotes (literal); both may span several lines;
- ``# comment`` lines, blank lines; anything else is an ``invalid`` line that is preserved as is.

`render_full` produces the canonical full file used by the settings mirror (07 §3.2) and, given the current
file, keeps the owner's own lines in place. `write_atomic` replaces the file atomically with a backup.

All file functions are synchronous and small; call them via `asyncio.to_thread` (or the ``*_async`` wrappers)
from async code.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import glob
import logging
import os
import re
import secrets
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, NamedTuple

__all__ = [
    "ANNOTATION_PREFIX",
    "BOM",
    "DUPLICATE_PREFIX",
    "INVALID_PREFIX",
    "MAX_FILE_BYTES",
    "UNRECOGNIZED_TITLE",
    "EnvDocument",
    "EnvFileError",
    "EnvLine",
    "LineKind",
    "RenderKey",
    "quote",
    "read_text",
    "read_text_async",
    "render_full",
    "render_section_header",
    "section_title",
    "unquote",
    "write_atomic",
    "write_atomic_async",
]

log = logging.getLogger("svbg.boot.envfile")

LineKind = Literal["blank", "comment", "kv", "invalid"]

BOM = "\ufeff"
DUPLICATE_PREFIX = "# (duplicate, ignored) "
INVALID_PREFIX = "# (invalid, ignored) "
ANNOTATION_PREFIX = "# ⚠ "
UNRECOGNIZED_TITLE = "Не распознано"
HEADER_WIDTH = 72
MAX_FILE_BYTES = 4 * 1024 * 1024
# A quoted value may continue on the following lines; an unterminated quote is searched for at most this far,
# which bounds the parser's work on broken files.
MAX_MULTILINE_CHARS = 64 * 1024
STALE_TEMP_SECONDS = 3600.0
_REPLACE_ATTEMPTS = 10
_REPLACE_RETRY_DELAY = 0.05

# Owner-facing messages (Russian), kept in one place.
_MSG_TOO_BIG = "Файл {path} слишком большой ({size} байт, максимум {limit})."
_MSG_NOT_UTF8 = "Файл {path} не в кодировке UTF-8 (ошибка в байте {offset}). Сохраните его в UTF-8."

_KEY_LINE_RE = re.compile(r"[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_.\-]*)[ \t]*=")
_WRITE_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_BARE_RE = re.compile(r"[A-Za-z0-9_./:@,+=\-]+")
_INLINE_COMMENT_RE = re.compile(r"[ \t]#")
_TRAILER_RE = re.compile(r"[ \t]*(?:#[^\n]*)?")
_DQ_SPECIAL_RE = re.compile(r'[\\"]')
_SECTION_RE = re.compile(r"[ \t]*#[ \t]*─{2,}[ \t]*(.+?)[ \t]*(?:─{2,}.*)?")
_ANNOTATION_RE = re.compile(r"[ \t]*#[ \t]*⚠")
_RULE_RE = re.compile(r"[ \t]*#[ \t]*[═─━=\-_*#~]{8,}[ \t]*")
_LINE_BREAK_RE = re.compile(r"\r\n|\r|\n")

_DQ_ENCODE = str.maketrans({"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "$": "\\$"})
_DQ_DECODE = {"\\": "\\", '"': '"', "'": "'", "n": "\n", "r": "\r", "t": "\t", "$": "$"}


class EnvFileError(ValueError):
    """The `.env` file cannot be used (too big, not UTF-8). The message is owner-facing (Russian)."""


@dataclass(slots=True)
class EnvLine:
    """One physical line, or a multi-line quoted value spanning several physical lines."""

    kind: LineKind
    raw: str  # exact original text without the final line ending
    key: str | None = None
    value: str | None = None  # decoded value (kv lines only)
    eol: str | None = None  # original line ending; None → the document's newline


# --------------------------------------------------------------------------------------------- quoting


def quote(value: str) -> str:
    """Render a value: bare when it is made of safe characters, otherwise double-quoted with escapes."""
    if value and _BARE_RE.fullmatch(value):
        return value
    return '"' + value.translate(_DQ_ENCODE) + '"'


def unquote(raw: str) -> str:
    """Decode the text after ``=``: ``"…"`` (escapes), ``'…'`` (literal) or bare (inline comment stripped).

    Raises `ValueError` for an unterminated quote or text after the closing quote that is not a comment.
    """
    start = len(raw) - len(raw.lstrip(" \t"))
    if raw[start : start + 1] in {'"', "'"}:
        scanned = _scan_quoted(raw, start, len(raw))
        if scanned is None:
            raise ValueError("unterminated quoted value")
        value, end = scanned
        if not _TRAILER_RE.fullmatch(raw, end):
            raise ValueError("unexpected text after the closing quote")
        return value
    return _bare_value(raw)


def _bare_value(text: str) -> str:
    match = _INLINE_COMMENT_RE.search(text)
    return (text[: match.start()] if match else text).strip(" \t")


def _scan_quoted(text: str, start: int, end_limit: int) -> tuple[str, int] | None:
    """Decode a quoted string whose opening quote is at ``text[start]``.

    Returns ``(value, index after the closing quote)`` or None if no closing quote before ``end_limit``.
    Literal CRLF inside a value is normalized to LF (as text-mode readers do).
    """
    quote_char = text[start]
    if quote_char == "'":
        close = text.find("'", start + 1, end_limit)
        if close < 0:
            return None
        return text[start + 1 : close].replace("\r\n", "\n"), close + 1
    parts: list[str] = []
    pos = start + 1
    while True:
        match = _DQ_SPECIAL_RE.search(text, pos, end_limit)
        if match is None:
            return None
        at = match.start()
        parts.append(text[pos:at].replace("\r\n", "\n"))
        if text[at] == '"':
            return "".join(parts), at + 1
        if at + 1 >= end_limit:
            return None
        escaped = text[at + 1]
        parts.append(_DQ_DECODE.get(escaped, "\\" + escaped))
        pos = at + 2


# --------------------------------------------------------------------------------------------- parsing


class _Bounds(NamedTuple):
    content_end: int
    eol: str
    next_pos: int


def _line_bounds(text: str, pos: int) -> _Bounds:
    newline_at = text.find("\n", pos)
    if newline_at < 0:
        return _Bounds(len(text), "", len(text))
    if newline_at > pos and text[newline_at - 1] == "\r":
        return _Bounds(newline_at - 1, "\r\n", newline_at + 1)
    return _Bounds(newline_at, "\n", newline_at + 1)


class _KV(NamedTuple):
    key: str
    value: str
    value_start: int  # absolute index where the value text (incl. quotes) starts
    value_end: int  # absolute index right after the value text
    style: str  # '"', "'" or "" (bare)
    bounds: _Bounds  # of the last physical line of the entry


def _scan_kv(text: str, pos: int, bounds: _Bounds) -> tuple[str | None, _KV | None]:
    """Try to read a ``KEY=value`` entry starting at ``pos``. Returns ``(key, entry)``.

    ``key`` is set whenever the line starts like an assignment (so broken assignments can be attributed);
    ``entry`` is None when the value is malformed.
    """
    match = _KEY_LINE_RE.match(text, pos, bounds.content_end)
    if match is None:
        return None, None
    key = match.group(1)
    eq_end = match.end()
    value_pos = eq_end
    while value_pos < bounds.content_end and text[value_pos] in " \t":
        value_pos += 1
    if value_pos < bounds.content_end and text[value_pos] in {'"', "'"}:
        limit = min(len(text), max(bounds.content_end, value_pos + MAX_MULTILINE_CHARS))
        scanned = _scan_quoted(text, value_pos, limit)
        if scanned is None:
            return key, None
        value, close_end = scanned
        last = bounds if close_end <= bounds.content_end else _line_bounds(text, close_end)
        if not _TRAILER_RE.fullmatch(text, close_end, last.content_end):
            return key, None
        return key, _KV(key, value, value_pos, close_end, text[value_pos], last)
    after = text[eq_end : bounds.content_end]
    comment = _INLINE_COMMENT_RE.search(after)
    head = after[: comment.start()] if comment else after
    unindented = head.lstrip(" \t")
    start = eq_end + len(head) - len(unindented)
    value_text = unindented.rstrip(" \t")
    return key, _KV(key, value_text, start, start + len(value_text), "", bounds)


def _classify(text: str, pos: int, bounds: _Bounds) -> tuple[EnvLine, _Bounds]:
    key, entry = _scan_kv(text, pos, bounds)
    if entry is not None:
        last = entry.bounds
        line = EnvLine("kv", text[pos : last.content_end], entry.key, entry.value, last.eol)
        return line, last
    raw = text[pos : bounds.content_end]
    if key is not None:
        return EnvLine("invalid", raw, key=key, eol=bounds.eol), bounds
    if not raw.strip():
        return EnvLine("blank", raw, eol=bounds.eol), bounds
    if raw.lstrip(" \t").startswith("#"):
        return EnvLine("comment", raw, eol=bounds.eol), bounds
    return EnvLine("invalid", raw, eol=bounds.eol), bounds


def _parse_entry(raw: str) -> _KV | None:
    """Re-read a kv line's own raw text (used for in-place value replacement)."""
    _, entry = _scan_kv(raw, 0, _line_bounds(raw, 0))
    return entry


def _eol_for(raw: str, newline: str) -> str:
    """Line ending to add after ``raw``: CRLF after a lone trailing CR (it would merge with an LF)."""
    return "\r\n" if raw.endswith("\r") else newline


def _detect_newline(text: str) -> str:
    newline_at = text.find("\n")
    if newline_at > 0 and text[newline_at - 1] == "\r":
        return "\r\n"
    return "\n"


# --------------------------------------------------------------------------------------------- helpers


def section_title(line: str) -> str | None:
    """Title of a section header comment ``# ── <title> ──…``, or None if the line is not one."""
    match = _SECTION_RE.fullmatch(line)
    if match is None:
        return None
    title = match.group(1).strip(" \t─")
    return title or None


def render_section_header(title: str) -> str:
    """``# ── <title> ───…`` padded to a fixed width. A title that already is a ``#`` line is kept."""
    title = _one_line(title).strip()
    if title.startswith("#"):
        return title
    head = f"# ── {title} "
    return head + "─" * max(3, HEADER_WIDTH - len(head))


def _section_match_key(section: str) -> str:
    title = section_title(section.strip())
    if title is None:
        title = section.split("──", maxsplit=1)[0].strip()
    return title.casefold()


def _one_line(text: str) -> str:
    return _LINE_BREAK_RE.sub(" ", text)


def _comment_lines(lines: Iterable[str]) -> list[str]:
    out: list[str] = []
    for text in lines:
        for part in _LINE_BREAK_RE.split(text):
            stripped = part.rstrip()
            if not stripped:
                out.append("#")
            elif stripped.startswith("#"):
                out.append(stripped)
            else:
                out.append("# " + stripped)
    return out


def _check_key(key: str) -> None:
    if not isinstance(key, str) or not _WRITE_KEY_RE.fullmatch(key):
        raise ValueError(f"invalid .env key: {key!r}")


def _check_value(value: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f".env values must be str, got {type(value).__name__}")


def _render_value(value: str, style: str) -> str:
    if style == '"':
        return '"' + value.translate(_DQ_ENCODE) + '"'
    if style == "'" and "'" not in value and "\n" not in value and "\r" not in value:
        return f"'{value}'"
    if style == "" and value == "":
        return ""
    return quote(value)


def _with_value(line: EnvLine, value: str) -> EnvLine:
    """The same kv line with a new value: prefix (``export``, spacing), quote style, inline comment kept."""
    if line.kind == "kv" and line.value == value:
        return line
    entry = _parse_entry(line.raw) if line.kind == "kv" else None
    if entry is None or line.key is None:
        key = line.key or ""
        return EnvLine("kv", f"{key}={quote(value)}", key, value, line.eol)
    raw = line.raw[: entry.value_start] + _render_value(value, entry.style) + line.raw[entry.value_end :]
    return EnvLine("kv", raw, line.key, value, line.eol)


def _duplicate_comment(line: EnvLine) -> EnvLine:
    if "\n" in line.raw or "\r" in line.raw:
        body = f"{line.key}={quote(line.value or '')}"
    else:
        body = line.raw.strip()
    return EnvLine("comment", DUPLICATE_PREFIX + body, eol=line.eol)


def _may_dangle(line: EnvLine) -> bool:
    """A broken assignment: its meaning may change when a closing quote appears later in the file."""
    return line.kind == "invalid" and line.key is not None


def _neutralized(line: EnvLine) -> EnvLine:
    return EnvLine("comment", INVALID_PREFIX + line.raw.strip(), eol=line.eol)


def _first_difference(expected: Sequence[str], actual: Sequence[str]) -> int | None:
    for index, (left, right) in enumerate(zip(expected, actual, strict=False)):
        if left != right:
            return index
    if len(expected) != len(actual):
        return min(len(expected), len(actual))
    return None


def _is_section_header(line: EnvLine) -> bool:
    return line.kind == "comment" and section_title(line.raw) is not None


def _is_annotation(line: EnvLine) -> bool:
    return line.kind == "comment" and _ANNOTATION_RE.match(line.raw) is not None


# --------------------------------------------------------------------------------------------- document


@dataclass
class EnvDocument:
    """A parsed `.env` file. ``parse(text).render() == text`` holds for any text."""

    newline: str = "\n"
    bom: bool = False
    lines: list[EnvLine] = field(default_factory=list)

    @classmethod
    def parse(cls, text: str) -> EnvDocument:
        bom = text.startswith(BOM)
        if bom:
            text = text[1:]
        lines: list[EnvLine] = []
        pos = 0
        while pos < len(text):
            line, last = _classify(text, pos, _line_bounds(text, pos))
            lines.append(line)
            pos = last.next_pos
        return cls(newline=_detect_newline(text), bom=bom, lines=lines)

    # ---- reading

    def get(self, key: str) -> str | None:
        """Value of ``key``; the last occurrence wins (like docker / python-dotenv)."""
        for line in reversed(self.lines):
            if line.kind == "kv" and line.key == key:
                return line.value
        return None

    def keys(self) -> list[str]:
        """Distinct keys in order of first appearance."""
        return list(dict.fromkeys(line.key for line in self.lines if line.kind == "kv" and line.key))

    def as_dict(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for line in self.lines:
            if line.kind == "kv" and line.key is not None and line.value is not None:
                result.pop(line.key, None)  # last occurrence wins and defines the order
                result[line.key] = line.value
        return result

    def invalid_lines(self) -> list[EnvLine]:
        return [line for line in self.lines if line.kind == "invalid"]

    # ---- editing

    def set(
        self, key: str, value: str, *, comment: list[str] | None = None, section: str | None = None
    ) -> None:
        """Set ``key`` to ``value``.

        An existing entry is changed in place (position, ``export``, quote style and inline
        comment kept); earlier duplicates become ``# (duplicate, ignored) …`` comments. A broken
        line for the key is replaced. A missing key is appended (with ``comment`` lines above it)
        to the end of ``section`` (the block that starts at the header comment
        ``# ── <section> ──``), which is created at the end of the file if absent; without
        ``section`` the key goes to the end of the file. ``comment`` is ignored for existing keys.
        """
        _check_key(key)
        _check_value(value)
        self._set(key, value, comment, section)
        self._stabilize()

    def _set(self, key: str, value: str, comment: list[str] | None, section: str | None) -> None:
        positions = [i for i, line in enumerate(self.lines) if line.kind == "kv" and line.key == key]
        if positions:
            for index in positions[:-1]:
                self.lines[index] = _duplicate_comment(self.lines[index])
            self.lines[positions[-1]] = _with_value(self.lines[positions[-1]], value)
            return
        broken = [i for i, line in enumerate(self.lines) if line.kind == "invalid" and line.key == key]
        if broken:
            old = self.lines[broken[-1]]
            self.lines[broken[-1]] = EnvLine("kv", f"{key}={quote(value)}", key, value, old.eol)
            return
        new_lines = [EnvLine("comment", text) for text in _comment_lines(comment or [])]
        new_lines.append(EnvLine("kv", f"{key}={quote(value)}", key, value))
        if section is None:
            self._insert(self._content_end(0, len(self.lines)), new_lines)
            return
        span = self._section_span(section)
        if span is not None:
            self._insert(self._content_end(span[0] + 1, span[1]), new_lines)
            return
        at = self._content_end(0, len(self.lines))
        head = [EnvLine("blank", "")] if at > 0 else []
        head.append(EnvLine("comment", render_section_header(section)))
        self._insert(at, head + new_lines)

    def remove(self, key: str) -> None:
        """Remove every entry (valid or broken) for ``key`` together with the warnings right above it."""
        drop = self._annotation_indices(key)
        drop.update(
            i for i, line in enumerate(self.lines) if line.key == key and line.kind in {"kv", "invalid"}
        )
        self.lines = [line for i, line in enumerate(self.lines) if i not in drop]
        self._stabilize()

    def annotate_invalid(self, key: str, message: str) -> None:
        """Put ``# ⚠ <message>`` right above the key's (last) line; one warning per key, idempotent.

        Raises `KeyError` if the key is not in the file.
        """
        index = self._last_index(key)
        if index is None:
            raise KeyError(key)
        text = ANNOTATION_PREFIX + _one_line(message).strip()
        start = index
        while start > 0 and _is_annotation(self.lines[start - 1]):
            start -= 1
        current = self.lines[start:index]
        if len(current) == 1 and current[0].raw.rstrip() == text:
            return
        self.lines[start:index] = [EnvLine("comment", text)]
        self._stabilize()

    def clear_annotations(self, key: str) -> None:
        """Remove ``# ⚠`` warnings right above the key's lines (e.g. after the owner fixed the value)."""
        drop = self._annotation_indices(key)
        if drop:
            self.lines = [line for i, line in enumerate(self.lines) if i not in drop]
            self._stabilize()

    def render(self) -> str:
        parts: list[str] = [BOM] if self.bom else []
        last = len(self.lines) - 1
        for index, line in enumerate(self.lines):
            parts.append(line.raw)
            eol = line.eol
            if eol is None or (not eol and index != last):
                eol = _eol_for(line.raw, self.newline)
            parts.append(eol)
        return "".join(parts)

    # ---- internals

    def _stabilize(self) -> None:
        """Keep the rendered text meaning exactly what the model says.

        A broken line with an unterminated quote (``A="oops``) is ignored, but text added or removed after
        it could supply a closing quote and silently turn following lines into its value. If that would
        happen, such a line is turned into a ``# (invalid, ignored) …`` comment (its text is kept).
        """
        if not any(_may_dangle(line) for line in self.lines):
            return
        while True:
            parsed = EnvDocument.parse(self.render()).lines
            index = _first_difference([line.raw for line in self.lines], [line.raw for line in parsed])
            if index is None or index >= len(self.lines) or not _may_dangle(self.lines[index]):
                return
            self.lines[index] = _neutralized(self.lines[index])

    def _last_index(self, key: str) -> int | None:
        fallback: int | None = None
        for index in range(len(self.lines) - 1, -1, -1):
            line = self.lines[index]
            if line.key == key and line.kind == "kv":
                return index
            if fallback is None and line.key == key and line.kind == "invalid":
                fallback = index
        return fallback

    def _annotation_indices(self, key: str) -> set[int]:
        found: set[int] = set()
        for index, line in enumerate(self.lines):
            if line.key == key and line.kind in {"kv", "invalid"}:
                above = index
                while above > 0 and _is_annotation(self.lines[above - 1]):
                    above -= 1
                    found.add(above)
        return found

    def _section_span(self, section: str) -> tuple[int, int] | None:
        wanted = _section_match_key(section)
        start: int | None = None
        for index, line in enumerate(self.lines):
            if not _is_section_header(line):
                continue
            if start is not None:
                return start, index
            if (section_title(line.raw) or "").casefold() == wanted:
                start = index
        return None if start is None else (start, len(self.lines))

    def _content_end(self, start: int, end: int) -> int:
        """Index right after the last non-blank line in ``[start, end)`` (``start`` if none)."""
        while end > start and self.lines[end - 1].kind == "blank":
            end -= 1
        return end

    def _insert(self, at: int, new_lines: list[EnvLine]) -> None:
        self.lines[at:at] = new_lines


# --------------------------------------------------------------------------------------------- full render


@dataclass
class RenderKey:
    key: str
    value: str
    comment_lines: list[str]
    section: str


_TOP = "top"
_UNREC = "unrecognized"


@dataclass
class _Layout:
    """Where the owner's lines of an existing file go in the regenerated file."""

    top: list[str] = field(default_factory=list)
    unrecognized: list[str] = field(default_factory=list)
    groups: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    annotations: dict[str, list[str]] = field(default_factory=dict)
    key_lines: dict[str, EnvLine] = field(default_factory=dict)


def render_full(
    sections: list[tuple[str, str]],
    keys: list[RenderKey],
    header: list[str],
    existing: EnvDocument | None = None,
) -> str:
    """Render the canonical full `.env`: header, then per section ``# ── <title> ──…`` and every key with its
    comment lines above it. Sections without keys (and without the owner's lines) are omitted; a key whose
    section is not listed gets a section titled by its id at the end.

    With ``existing`` (the current file): values are replaced in place on the owner's lines (``export``, quote
    style, inline comments kept); our comment lines above known keys are regenerated; ``# ⚠`` warnings stay
    above their key; the owner's lines (comments, unknown keys, broken lines) stay right after the known key
    or section header they followed; unknown keys found outside known sections go to a trailing
    ``# ── Не распознано ──`` section. Newline style and BOM of ``existing`` are kept. The output is a fixed
    point: rendering again with the parsed output as ``existing`` gives the same text.
    """
    by_key: dict[str, RenderKey] = {}
    for item in keys:
        _check_key(item.key)
        _check_value(item.value)
        if item.key in by_key:
            raise ValueError(f"duplicate key in render_full: {item.key}")
        by_key[item.key] = item

    order = list(sections)
    known = {sid for sid, _ in order}
    for item in keys:
        if item.section not in known:
            order.append((item.section, item.section))
            known.add(item.section)

    section_lines: dict[str, str] = {}
    title_to_sid: dict[str, str] = {}
    for sid, title in order:
        line = render_section_header(title)
        parsed_title = section_title(line)
        if parsed_title is None:
            raise ValueError(f"section title must render as a '# ── … ──' header: {title!r}")
        section_lines[sid] = line
        title_to_sid.setdefault(parsed_title.casefold(), sid)

    comments = {item.key: _comment_lines(item.comment_lines) for item in keys}
    header_lines = _comment_lines(header)
    layout = (
        _Layout() if existing is None else _analyze(existing, by_key, title_to_sid, header_lines, comments)
    )

    out: list[str] = list(header_lines)
    if out:
        out.append("")
    top = _normalize(layout.top, strip_edges=True)
    if top:
        out.extend(top)
        out.append("")
    for sid, _ in order:
        section_keys = [item for item in keys if item.section == sid]
        lead = _normalize(layout.groups.get(("sec", sid), []))
        if not section_keys and not any(lead):
            continue
        out.append(section_lines[sid])
        out.extend(lead)
        for item in section_keys:
            out.extend(comments[item.key])
            out.extend(layout.annotations.get(item.key, []))
            out.append(_key_line(item, layout.key_lines.get(item.key)))
            out.extend(_normalize(layout.groups.get(("key", item.key), [])))
        _trim_blank_tail(out)
        out.append("")
    unrecognized = _normalize(layout.unrecognized, strip_edges=True)
    if unrecognized:
        out.append(render_section_header(UNRECOGNIZED_TITLE))
        out.extend(unrecognized)
    _trim_blank_tail(out)

    newline = existing.newline if existing is not None else "\n"
    bom = BOM if existing is not None and existing.bom else ""
    return bom + _stable_text(out, newline)


def _stable_text(out: list[str], newline: str) -> str:
    """Join lines; neutralize broken owner lines whose unterminated quote would swallow lines after them."""
    while True:
        text = "".join(raw + _eol_for(raw, newline) for raw in out)
        parsed = EnvDocument.parse(text).lines
        index = _first_difference(out, [line.raw for line in parsed])
        if index is None or index >= len(out):
            return text
        line = EnvDocument.parse(out[index]).lines
        if len(line) != 1 or not _may_dangle(line[0]):
            return text
        out[index] = _neutralized(line[0]).raw


def _key_line(item: RenderKey, existing: EnvLine | None) -> str:
    if existing is not None and existing.kind == "kv":
        return _with_value(existing, item.value).raw
    return f"{item.key}={quote(item.value)}"


def _our_header_indices(lines: Sequence[EnvLine], header_lines: list[str]) -> list[int]:
    """Lines of our previous header among the comment blocks before the first section.

    The current header found in a block (in order, possibly with the owner's lines in between) → exactly
    those lines. Otherwise the first comment block is our header from an older version if it starts with a
    rule line (``# ════…``) → the whole block.
    """
    index = 0
    first_block = True
    while index < len(lines) and not _is_section_header(lines[index]):
        if lines[index].kind != "comment":
            index += 1
            continue
        end = index
        while end < len(lines) and lines[end].kind == "comment" and not _is_section_header(lines[end]):
            end += 1
        matched = _match_in_order(lines, range(index, end), header_lines) if header_lines else None
        if matched is not None:
            return matched
        if first_block and _RULE_RE.fullmatch(lines[index].raw.rstrip()):
            return list(range(index, end))
        first_block = False
        index = end
    return []


def _match_in_order(lines: Sequence[EnvLine], block: range, wanted: list[str]) -> list[int] | None:
    """Indices in ``block`` whose text equals ``wanted`` in order (a subsequence), or None."""
    matched: list[int] = []
    for index in block:
        if len(matched) < len(wanted) and lines[index].raw.rstrip() == wanted[len(matched)]:
            matched.append(index)
    return matched if len(matched) == len(wanted) else None


def _match_from_end(texts: list[str], wanted: list[str]) -> list[int] | None:
    """Positions of ``wanted`` in ``texts`` as a subsequence matched from the end (closest to the key)."""
    positions: list[int] = []
    pending = len(wanted) - 1
    for position in range(len(texts) - 1, -1, -1):
        if pending < 0:
            break
        if texts[position] == wanted[pending]:
            positions.append(position)
            pending -= 1
    return positions[::-1] if pending < 0 else None


def _analyze(
    existing: EnvDocument,
    by_key: dict[str, RenderKey],
    title_to_sid: dict[str, str],
    header_lines: list[str],
    comments: dict[str, list[str]],
) -> _Layout:
    """Classify the lines of the current file: ours (regenerated) vs the owner's (kept, anchored)."""
    lines = existing.lines
    layout = _Layout()
    dropped: set[int] = set(_our_header_indices(lines, header_lines))

    # Which section each line physically sits in; known section headers are regenerated (dropped).
    regions: list[str] = []
    region = _TOP
    for index, line in enumerate(lines):
        title = section_title(line.raw) if line.kind == "comment" else None
        if title is not None:
            folded = title.casefold()
            if folded == UNRECOGNIZED_TITLE.casefold():
                region = _UNREC
                dropped.add(index)
            elif folded in title_to_sid:
                region = title_to_sid[folded]
                dropped.add(index)
        regions.append(region)

    # Anchor line of each known key: its last valid entry, or (if none) its last broken line.
    occurrences: dict[str, list[int]] = {}
    last_broken: dict[str, int] = {}
    for index, line in enumerate(lines):
        if line.key in by_key:
            if line.kind == "kv":
                occurrences.setdefault(line.key, []).append(index)
            elif line.kind == "invalid":
                last_broken[line.key] = index
    for key, index in last_broken.items():
        occurrences.setdefault(key, [index])
    anchor_at = {indices[-1]: key for key, indices in occurrences.items()}

    # The comment block right above every occurrence: our comment is dropped, warnings travel with the key.
    known_sections = set(title_to_sid.values())
    known_lines = {index for indices in occurrences.values() for index in indices}
    for key, indices in occurrences.items():
        for index in indices:
            ours, notes = _our_comment_block(
                lines,
                index,
                dropped,
                comments[key],
                in_known_section=regions[index] in known_sections,
                known_lines=known_lines,
            )
            dropped.update(ours)
            if index in anchor_at:
                dropped.update(notes)
                layout.annotations[key] = [lines[i].raw.rstrip() for i in notes]

    anchor: tuple[str, str] = (_TOP, "")
    comment_run = 0  # owner comment lines just appended to `top`, contiguous in the source
    for index, line in enumerate(lines):
        region = regions[index]
        if index in dropped:
            if _is_section_header(line):
                anchor = (_UNREC, "") if region == _UNREC else ("sec", region)
            comment_run = 0
            continue
        if index in anchor_at:
            key = anchor_at[index]
            anchor = ("key", key)
            layout.key_lines[key] = line
            comment_run = 0
            continue
        effective = _duplicate_comment(line) if line.kind == "kv" and line.key in by_key else line
        if region == _UNREC:
            layout.unrecognized.append(effective.raw)
        elif region == _TOP:
            if effective.kind == "kv":  # an unknown key outside known sections
                moved = layout.top[len(layout.top) - comment_run :] if comment_run else []
                del layout.top[len(layout.top) - len(moved) :]
                layout.unrecognized.extend([*moved, effective.raw])
                comment_run = 0
            else:
                layout.top.append(effective.raw)
                comment_run = comment_run + 1 if effective.kind == "comment" else 0
        else:
            layout.groups.setdefault(anchor, []).append(effective.raw)
    return layout


def _our_comment_block(
    lines: Sequence[EnvLine],
    index: int,
    dropped: set[int],
    wanted: list[str],
    *,
    in_known_section: bool,
    known_lines: set[int],
) -> tuple[list[int], list[int]]:
    """Split the comment lines right above ``lines[index]`` into (our comment, ``# ⚠`` warnings).

    Our current comment found (in order, the owner may have added lines in between) → only it is ours, the
    owner's lines stay. Otherwise the
    block is our stale comment (the description changed in a new version) only where we would have put it:
    inside a known section, right after a known key or a known section header. Anywhere else (a hand-written
    file, after the owner's own lines) nothing is ours.
    """
    start = index
    while (
        start > 0
        and start - 1 not in dropped
        and lines[start - 1].kind == "comment"
        and not _is_section_header(lines[start - 1])
    ):
        start -= 1
    block = range(start, index)
    notes = [i for i in block if _is_annotation(lines[i])]
    plain = [
        i
        for i in block
        if i not in notes and not lines[i].raw.lstrip(" \t").startswith(DUPLICATE_PREFIX.strip())
    ]
    if not wanted:
        return [], notes
    found = _match_from_end([lines[i].raw.rstrip() for i in plain], wanted)
    if found is not None:
        return [plain[position] for position in found], notes
    above = start - 1
    ours_position = in_known_section and (
        above in known_lines or (above >= 0 and above in dropped and _is_section_header(lines[above]))
    )
    return (plain if ours_position else []), notes


def _normalize(group: list[str], *, strip_edges: bool = False) -> list[str]:
    """Owner lines: blank runs collapsed (blank-only groups vanish), stale empty foreign headers dropped."""
    compact: list[str] = []
    for raw in group:
        if raw.strip():
            compact.append(raw)
        elif not compact or compact[-1]:
            compact.append("")
    result: list[str] = []
    for index, raw in enumerate(compact):
        if section_title(raw) is not None:
            following = index + 1
            while following < len(compact) and not compact[following]:
                following += 1
            if following == len(compact) or section_title(compact[following]) is not None:
                continue
        if not raw and result and not result[-1]:
            continue
        result.append(raw)
    if strip_edges or not any(result):
        while result and not result[0]:
            result.pop(0)
        _trim_blank_tail(result)
    return result


def _trim_blank_tail(lines: list[str]) -> None:
    while lines and not lines[-1]:
        lines.pop()


# --------------------------------------------------------------------------------------------- files


def read_text(path: Path, *, max_bytes: int = MAX_FILE_BYTES) -> str | None:
    """Read the file as UTF-8; None if it does not exist.

    A UTF-8 BOM is kept as a leading ``"\\ufeff"`` so that `EnvDocument.parse` can preserve it on write-back.
    Raises `EnvFileError` (owner-facing message) for files that are too big or not UTF-8; other `OSError`s
    (permissions, a directory in place of the file) propagate.
    """
    try:
        with open(path, "rb") as handle:
            data = handle.read(max_bytes + 1)
    except FileNotFoundError:
        return None
    if len(data) > max_bytes:
        raise EnvFileError(_MSG_TOO_BIG.format(path=path, size=f"> {max_bytes}", limit=max_bytes))
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EnvFileError(_MSG_NOT_UTF8.format(path=path, offset=exc.start)) from exc


def write_atomic(
    path: Path,
    text: str,
    *,
    keep_backup: bool = True,
    mode: int = 0o600,
    inplace_fallback: bool = False,
) -> None:
    """Atomically replace ``path`` with ``text`` (UTF-8).

    Writes a temp file in the same directory (created with ``mode``; owner/group of the old file kept when
    allowed), flushes and fsyncs it, saves the current content to ``<name>.bak`` (also ``mode``, also atomic;
    skipped when the content does not change), then ``os.replace`` and fsync of the directory. On any error
    the temp file is removed and the original file is untouched. A symlink is followed: its target is
    replaced.

    ``inplace_fallback``: when the file is a single-file bind mount (``os.replace`` fails with EBUSY/EXDEV),
    rewrite it in place instead — not atomic, so it is opt-in.
    """
    data = text.encode("utf-8")
    target = Path(os.path.realpath(path)) if Path(path).is_symlink() else Path(path)
    directory = target.parent
    _cleanup_stale_temps(directory, target.name)
    try:
        current: bytes | None = target.read_bytes()
        owner: os.stat_result | None = target.stat()
    except FileNotFoundError:
        current, owner = None, None
    temp = _write_temp(directory, target.name, data, mode, owner)
    try:
        if keep_backup and current is not None and current != data:
            _write_backup(target, current, mode, owner)
        replaced = _replace(temp, target)
        if not replaced:
            if not inplace_fallback:
                raise OSError(errno.EBUSY, "cannot replace a bind-mounted file atomically", str(target))
            log.warning("atomic replace is not possible for %s, rewriting in place", target)
            _write_in_place(target, data)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp)
    _fsync_directory(directory)


async def read_text_async(path: Path, *, max_bytes: int = MAX_FILE_BYTES) -> str | None:
    return await asyncio.to_thread(read_text, path, max_bytes=max_bytes)


async def write_atomic_async(
    path: Path, text: str, *, keep_backup: bool = True, mode: int = 0o600, inplace_fallback: bool = False
) -> None:
    await asyncio.to_thread(
        write_atomic, path, text, keep_backup=keep_backup, mode=mode, inplace_fallback=inplace_fallback
    )


def _temp_name(name: str) -> str:
    return f".{name}.{secrets.token_hex(6)}.tmp"


def _write_temp(directory: Path, name: str, data: bytes, mode: int, owner: os.stat_result | None) -> Path:
    temp = directory / _temp_name(name)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temp, flags, mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            if hasattr(os, "fchmod"):
                os.fchmod(handle.fileno(), mode)  # the umask may have narrowed or the mode widened
            if owner is not None and hasattr(os, "fchown"):
                try:
                    os.fchown(handle.fileno(), owner.st_uid, owner.st_gid)
                except PermissionError:
                    log.debug("cannot keep owner of %s (not permitted)", temp)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp)
        raise
    return temp


def _write_backup(target: Path, current: bytes, mode: int, owner: os.stat_result | None) -> None:
    backup = target.with_name(target.name + ".bak")
    try:
        temp = _write_temp(target.parent, backup.name, current, mode, owner)
        try:
            if not _replace(temp, backup):
                log.warning("cannot replace backup %s atomically, backup not updated", backup)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temp)
    except OSError as exc:
        # The backup is a convenience; failing to update it must not block writing the settings.
        log.warning("cannot update backup %s: %s", backup, exc.strerror or type(exc).__name__)


def _replace(source: Path, target: Path) -> bool:
    """``os.replace`` with retries for transient Windows sharing violations.

    Returns False when the target cannot be replaced at all because it is a mount point (EBUSY/EXDEV).
    """
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(source, target)
        except PermissionError:
            if os.name != "nt" or attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_RETRY_DELAY)  # antivirus/editor holds the file for a moment
        except OSError as exc:
            if exc.errno in {errno.EBUSY, errno.EXDEV}:
                return False
            raise
        else:
            return True
    return False  # pragma: no cover - the loop either returns or raises


def _write_in_place(target: Path, data: bytes) -> None:
    with open(target, "r+b") as handle:
        handle.write(data)
        handle.truncate()
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return  # directories cannot be opened for fsync on Windows; NTFS journals the rename
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError as exc:
        log.debug("cannot open %s for fsync: %s", directory, exc.strerror)
        return
    try:
        os.fsync(fd)
    except OSError as exc:
        log.debug("fsync of %s is not supported: %s", directory, exc.strerror)
    finally:
        os.close(fd)


def _cleanup_stale_temps(directory: Path, name: str) -> None:
    """Remove temp files left by a writer killed mid-write (they may contain secrets)."""
    deadline = time.time() - STALE_TEMP_SECONDS
    for stale in directory.glob(f".{glob.escape(name)}.*.tmp"):  # also matches the backup's temp files
        try:
            if stale.stat().st_mtime < deadline:
                stale.unlink()
        except OSError as exc:
            log.debug("cannot remove stale temp file %s: %s", stale, exc.strerror)
