"""Report cards for the admin chat and the owners' DMs: one model, two renderings.

A :class:`Report` is a title with one emoji, key-value lines, sections, tables and a footer. It renders to

* a rich message (Bot API 10.3 ``sendRichMessage``): heading, compact tables with aligned cells, a footer,
  and the banner photo on top when the banner is on (:func:`banner_media`);
* a classic Telegram HTML text (≤ 4096): ``key: <b>value</b>`` lines and ``<pre>`` tables no wider than
  :data:`PRE_WIDTH` cells, measured with Cyrillic, emoji and combining marks in mind. It is sent where rich
  messages are not available (old server, ``BadRequest``), and it is also what digests and logs quote.

Cells and values are plain strings or inline spans (:func:`b`, :func:`i`, :func:`code`, :func:`link`); nothing
needs escaping by the caller. :class:`RichGate` remembers for a while that a chat rejected rich messages.
"""

from __future__ import annotations

import html as _html
import inspect
import logging
import time
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any, Final, Literal

from aiogram.exceptions import TelegramBadRequest, TelegramNotFound
from aiogram.methods import SendRichMessage
from aiogram.types import (
    InputMediaPhoto,
    InputRichBlockDetails,
    InputRichBlockFooter,
    InputRichBlockList,
    InputRichBlockListItem,
    InputRichBlockParagraph,
    InputRichBlockPhoto,
    InputRichBlockPreformatted,
    InputRichBlockSectionHeading,
    InputRichBlockTable,
    InputRichMessage,
    RichBlockTableCell,
    RichTextBold,
    RichTextCode,
    RichTextItalic,
    RichTextUrl,
)

from svbg.core.money import format_money

if TYPE_CHECKING:
    from aiogram import Bot

__all__ = [
    "GATE",
    "MAX_HTML",
    "PRE_WIDTH",
    "Inline",
    "Report",
    "RichGate",
    "Span",
    "b",
    "banner_media",
    "cell_width",
    "code",
    "from_html",
    "i",
    "link",
    "money",
    "num",
    "plain",
    "pre_table",
    "send_report",
]

log = logging.getLogger("svbg.tg.report")

MAX_HTML: Final = 4096  # classic text limit, UTF-16 code units
MAX_RICH_CHARS: Final = 30_000  # Telegram: 32768; kept below it
MAX_RICH_BLOCKS: Final = 480  # Telegram: 500 (table rows count)
PRE_WIDTH: Final = 32  # a <pre> line wider than this wraps on phones
THIN: Final = "\N{NARROW NO-BREAK SPACE}"
NBSP: Final = "\N{NO-BREAK SPACE}"

Align = Literal["left", "center", "right"]
_ALIGNS: Final[dict[str, Align]] = {"l": "left", "c": "center", "r": "right"}


@dataclass(frozen=True, slots=True)
class Span:
    style: Literal["b", "i", "code", "url"]
    text: str
    url: str | None = None


type Inline = str | int | Span | Sequence[Inline] | None


def b(text: Any) -> Span:
    return Span("b", str(text))


def i(text: Any) -> Span:
    return Span("i", str(text))


def code(text: Any) -> Span:
    return Span("code", str(text))


def link(text: Any, url: str) -> Span:
    return Span("url", str(text), url)


def num(value: int | float) -> str:
    """``12 345`` with a narrow no-break space between thousands."""
    if isinstance(value, float) and not value.is_integer():
        whole, frac = f"{value:.1f}".split(".")
        return _group(whole) + "," + frac
    return _group(str(int(value)))


def _group(digits: str) -> str:
    sign = "−" if digits.startswith("-") else ""
    digits = digits.lstrip("-")
    out = []
    while len(digits) > 3:
        out.insert(0, digits[-3:])
        digits = digits[:-3]
    out.insert(0, digits)
    return sign + THIN.join(out)


def money(amount_minor: int, currency: str) -> str:
    """``3 588 ₽``: thin spaces between thousands, a no-break space before the symbol."""
    try:
        text = format_money(int(amount_minor), str(currency), nbsp=True)
    except (TypeError, ValueError):
        return f"{amount_minor}{NBSP}{currency}"
    head, sep, tail = text.rpartition(NBSP)
    if not sep or not any(ch.isdigit() for ch in head):
        return text
    if any(ch.isdigit() for ch in tail):  # no symbol after the number
        return text.replace(NBSP, THIN)
    return head.replace(NBSP, THIN) + NBSP + tail


# ---------------------------------------------------------------------------------------------- inline


def plain(value: Inline) -> str:
    if value is None:
        return ""
    if isinstance(value, Span):
        return value.text
    if isinstance(value, str | int):
        return str(value)
    return "".join(plain(v) for v in value)


def _html_inline(value: Inline) -> str:
    if value is None:
        return ""
    if isinstance(value, Span):
        text = _html.escape(value.text, quote=False)
        if value.style == "url":
            return f'<a href="{_html.escape(value.url or "", quote=True)}">{text}</a>'
        return f"<{value.style}>{text}</{value.style}>"
    if isinstance(value, str | int):
        return _html.escape(str(value), quote=False)
    return "".join(_html_inline(v) for v in value)


def _rich_inline(value: Inline) -> Any:
    if value is None:
        return ""
    if isinstance(value, Span):
        if value.style == "b":
            return RichTextBold(text=value.text)
        if value.style == "i":
            return RichTextItalic(text=value.text)
        if value.style == "code":
            return RichTextCode(text=value.text)
        return RichTextUrl(text=value.text, url=value.url or "")
    if isinstance(value, str | int):
        return str(value)
    parts = [_rich_inline(v) for v in value]
    return parts[0] if len(parts) == 1 else parts


def _bold(value: Inline) -> Inline:
    """Plain text becomes bold; spans keep their own style."""
    if isinstance(value, str | int):
        return b(value)
    return value


_HTML_STYLES: Final[dict[str, Literal["b", "i", "code", "url"]]] = {
    "b": "b",
    "strong": "b",
    "i": "i",
    "em": "i",
    "code": "code",
    "pre": "code",
    "a": "url",
}


class _HtmlInline(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[Inline] = []
        self.stack: list[tuple[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br":
            self.out.append("\n")
            return
        href = dict(attrs).get("href") if tag == "a" else None
        self.stack.append((tag, href))

    def handle_endtag(self, tag: str) -> None:
        for n in range(len(self.stack) - 1, -1, -1):
            if self.stack[n][0] == tag:
                del self.stack[n:]
                return

    def handle_data(self, data: str) -> None:
        if not data:
            return
        for tag, href in reversed(self.stack):
            style = _HTML_STYLES.get(tag)
            if style == "url":
                if href:
                    self.out.append(link(data, href))
                    return
                continue
            if style is not None:
                self.out.append(Span(style, data))
                return
        self.out.append(data)


def from_html(text: str) -> Inline:
    """A short Telegram HTML fragment (``<b>``, ``<i>``, ``<code>``, ``<a href>``; other tags are dropped,
    the innermost style wins) as inline spans, for texts written in HTML before reports existed."""
    if "<" not in text and "&" not in text:
        return text
    parser = _HtmlInline()
    parser.feed(text)
    parser.close()
    out = parser.out
    return out[0] if len(out) == 1 else out


# ---------------------------------------------------------------------------------------------- widths


_ZERO: Final = frozenset({0x200B, 0x200C, 0x2060, 0xFE0E, 0xFE0F})


def cell_width(text: str) -> int:
    """Width in monospace cells: Cyrillic / Latin / digits / ₽ = 1, emoji and CJK = 2, marks and ZWJ = 0."""
    width, after_zwj, ri = 0, False, 0
    for ch in text:
        o = ord(ch)
        if o == 0x200D:
            after_zwj = True
            continue
        if (
            after_zwj
            or o in _ZERO
            or 0x1F3FB <= o <= 0x1F3FF
            or unicodedata.category(ch) in ("Mn", "Me", "Cf")
        ):
            after_zwj = False
            continue
        if 0x1F1E6 <= o <= 0x1F1FF:  # a flag is a pair of regional indicators
            ri += 1
            width += 2 if ri % 2 else 0
            continue
        wide = (
            unicodedata.east_asian_width(ch) in ("W", "F") or 0x1F000 <= o <= 0x1FAFF or 0x2600 <= o <= 0x27BF
        )
        width += 2 if wide else 1
    return width


def _clip(text: str, width: int) -> str:
    if cell_width(text) <= width:
        return text
    out, used = [], 0
    for ch in text:
        w = cell_width(ch)
        if used + w > width - 1:
            break
        out.append(ch)
        used += w
    return "".join(out) + "…"


def _pad(text: str, width: int, align: Align) -> str:
    gap = max(0, width - cell_width(text))
    if align == "right":
        return " " * gap + text
    if align == "center":
        return " " * (gap // 2) + text + " " * (gap - gap // 2)
    return text + " " * gap


def _mono(text: str) -> str:
    """Spaces that some monospace fonts draw narrower become plain no-break spaces."""
    return text.replace(THIN, NBSP).replace("\N{THIN SPACE}", NBSP).replace("\n", " ")


def pre_table(
    rows: Sequence[Sequence[str]],
    aligns: Sequence[Align],
    *,
    max_width: int = PRE_WIDTH,
    shrink: int = 0,
    min_shrink: int = 6,
    rule_before: int | None = None,
) -> str | None:
    """``<pre>`` with the header row, a rule under it and padded columns; ``None`` if it can't fit."""
    if not rows:
        return None
    cols = len(aligns)
    grid = [[_mono(str(r[c]) if c < len(r) else "") for c in range(cols)] for r in rows]
    widths = [max(cell_width(r[c]) for r in grid) for c in range(cols)]
    over = sum(widths) + cols - 1 - max_width
    if over > 0:
        widths[shrink] = widths[shrink] - over
        if widths[shrink] < min_shrink:
            return None
    lines = [
        " ".join(_pad(_clip(r[c], widths[c]), widths[c], aligns[c]) for c in range(cols)).rstrip()
        for r in grid
    ]
    rule = " ".join("─" * w for w in widths)
    lines.insert(1, rule)
    if rule_before is not None:
        lines.insert(rule_before + 1, rule)
    return "<pre>" + _html.escape("\n".join(lines), quote=False) + "</pre>"


# ---------------------------------------------------------------------------------------------- model


@dataclass(slots=True)
class _Table:
    header: list[str]
    rows: list[list[Inline]]
    aligns: list[Align]
    total: list[Inline] | None
    max_rows: int
    shrink: int


@dataclass(slots=True)
class _Part:
    kind: Literal["kv", "text", "section", "table", "note", "bullets", "details"]
    key: str = ""
    value: Inline = None
    table: _Table | None = None
    items: list[Inline] = field(default_factory=list)
    mono: bool = False


@dataclass(slots=True)
class Report:
    """``Report("💰", "Пополнение 500 ₽").line("Клиент", who).table(...).footer("12:30")``."""

    emoji: str
    title: str
    subtitle: str | None = None
    parts: list[_Part] = field(default_factory=list)
    foot: str | None = None

    # ------------------------------------------------------------------ building

    def line(self, key: str, value: Inline) -> Report:
        """``key: value`` (plain values are shown bold); an empty value skips the line."""
        if value is None or value in ("", []):
            return self
        self.parts.append(_Part("kv", key=key, value=value))
        return self

    def text(self, value: Inline) -> Report:
        if plain(value).strip():
            self.parts.append(_Part("text", value=value))
        return self

    def note(self, value: Inline) -> Report:
        """A grey (italic) remark."""
        if plain(value).strip():
            self.parts.append(_Part("note", value=value))
        return self

    def bullets(self, items: Sequence[Inline]) -> Report:
        """A bulleted list (warnings, notes)."""
        kept = [x for x in items if plain(x).strip()]
        if kept:
            self.parts.append(_Part("bullets", items=kept))
        return self

    def details(self, summary: str, lines: Sequence[Inline], *, mono: bool = False) -> Report:
        """A folded block: ``summary`` on top, ``lines`` inside (``mono``: one preformatted text, e.g. a
        traceback). Rich: a details block; HTML: an expandable quote."""
        kept = [x for x in lines if plain(x).strip()]
        if kept:
            self.parts.append(_Part("details", key=summary, items=kept, mono=mono))
        return self

    def section(self, title: str) -> Report:
        self.parts.append(_Part("section", key=title))
        return self

    def table(
        self,
        header: Sequence[str],
        rows: Sequence[Sequence[Inline]],
        align: str | None = None,
        *,
        total: Sequence[Inline] | None = None,
        max_rows: int = 20,
        shrink: int = 0,
    ) -> Report:
        """``align``: one letter per column, ``l`` / ``c`` / ``r`` (default: first left, the rest right)."""
        cols = len(header)
        if cols < 1 or cols > 20:
            raise ValueError("a table needs 1..20 columns")
        letters = align or ("l" + "r" * (cols - 1))
        if len(letters) != cols or any(ch not in _ALIGNS for ch in letters):
            raise ValueError("align needs one of l/c/r per column")
        if any(len(r) != cols for r in rows) or (total is not None and len(total) != cols):
            raise ValueError("every row needs as many cells as the header")
        if rows:
            self.parts.append(
                _Part(
                    "table",
                    table=_Table(
                        list(header),
                        [list(r) for r in rows],
                        [_ALIGNS[ch] for ch in letters],
                        list(total) if total is not None else None,
                        max(1, max_rows),
                        shrink,
                    ),
                )
            )
        return self

    def footer(self, text: str | None) -> Report:
        self.foot = text or None
        return self

    def stamp(self, when: datetime, prefix: str = "") -> Report:
        """Footer with the time: ``собрано 02.10 14:05``."""
        return self.footer(f"{prefix}{when:%d.%m %H:%M}".strip())

    @property
    def headline(self) -> str:
        return f"{self.emoji} {self.title}".strip()

    # ------------------------------------------------------------------ rich

    def rich(self, *, banner: str | None = None, header: str | None = None) -> InputRichMessage:
        """Blocks for ``sendRichMessage``; raises ``ValueError`` past Telegram's limits."""
        blocks: list[Any] = []
        if banner:
            media = InputMediaPhoto(media=banner, parse_mode=None, show_caption_above_media=None)
            blocks.append(InputRichBlockPhoto(photo=media))
        if header:
            blocks.append(InputRichBlockParagraph(text=RichTextItalic(text=header)))
        blocks.append(InputRichBlockSectionHeading(text=self.headline, size=3))
        if self.subtitle:
            blocks.append(InputRichBlockParagraph(text=RichTextItalic(text=self.subtitle)))
        count = len(blocks)
        kv: list[list[RichBlockTableCell]] = []

        def flush() -> None:
            nonlocal count
            if kv:
                blocks.append(InputRichBlockTable(cells=list(kv), is_compact=True))
                count += 1 + len(kv)
                kv.clear()

        for part in self.parts:
            if part.kind == "kv":
                kv.append([_cell(part.key), _cell(_bold(part.value))])
                continue
            flush()
            if part.kind == "text":
                blocks.append(InputRichBlockParagraph(text=_rich_inline(part.value)))
            elif part.kind == "note":
                blocks.append(InputRichBlockParagraph(text=RichTextItalic(text=plain(part.value))))
            elif part.kind == "section":
                blocks.append(InputRichBlockSectionHeading(text=part.key, size=4))
            elif part.kind == "bullets":
                blocks.append(
                    InputRichBlockList(
                        items=[
                            InputRichBlockListItem(blocks=[InputRichBlockParagraph(text=_rich_inline(x))])
                            for x in part.items
                        ]
                    )
                )
                count += 2 * len(part.items)
            elif part.kind == "details":
                inner: list[Any] = (
                    [InputRichBlockPreformatted(text="\n".join(plain(x) for x in part.items))]
                    if part.mono
                    else [InputRichBlockParagraph(text=_rich_inline(x)) for x in part.items]
                )
                blocks.append(InputRichBlockDetails(summary=part.key, blocks=inner))
                count += len(inner)
            elif part.table is not None:
                cells, rows = _rich_table(part.table)
                blocks.append(InputRichBlockTable(cells=cells, is_compact=True, is_striped=True))
                count += rows
            count += 1
        flush()
        if self.foot:
            blocks.append(InputRichBlockFooter(text=self.foot))
            count += 1
        if count > MAX_RICH_BLOCKS or len(self.plain_text()) > MAX_RICH_CHARS:
            raise ValueError("report is too big for one rich message")
        return InputRichMessage(blocks=blocks)

    # ------------------------------------------------------------------ classic HTML

    def html(self, *, header: str | None = None, limit: int = MAX_HTML) -> str:
        """Telegram HTML ≤ ``limit`` UTF-16 units: long tables are cut first, then trailing parts."""
        for rows in (None, 8, 3):
            chunks = self._chunks(header, rows)
            text = "\n\n".join(chunks)
            if _utf16(text) <= limit:
                return text
        while len(chunks) > 1 and _utf16("\n\n".join([*chunks, "…"])) > limit:
            chunks.pop()
        text = "\n\n".join([*chunks, "…"])
        return text if _utf16(text) <= limit else _html.escape(self.headline[: limit // 2], quote=False)

    def plain_text(self) -> str:
        """Everything as plain text (length checks, logs)."""
        out = [self.headline, self.subtitle or ""]
        for part in self.parts:
            if part.kind == "kv":
                out.append(f"{part.key}: {plain(part.value)}")
            elif part.kind in ("text", "note"):
                out.append(plain(part.value))
            elif part.kind == "bullets":
                out += [plain(x) for x in part.items]
            elif part.kind == "details":
                out += [part.key, *(plain(x) for x in part.items)]
            elif part.kind == "section":
                out.append(part.key)
            elif part.table is not None:
                out.append(" ".join(part.table.header))
                out += [" ".join(plain(c) for c in r) for r in part.table.rows]
        out.append(self.foot or "")
        return "\n".join(x for x in out if x)

    def _chunks(self, header: str | None, max_rows: int | None) -> list[str]:
        e = _esc
        head = f"{e(self.emoji)} <b>{e(self.title)}</b>".strip()
        if header:
            head = f"<i>{e(header)}</i>\n{head}"
        if self.subtitle:
            head += f"\n<i>{e(self.subtitle)}</i>"
        chunks = [head]
        open_section = False  # the last chunk is a fresh section title: the next part joins it
        kv: list[str] = []

        def add(piece: str) -> None:
            nonlocal open_section
            if open_section:
                chunks[-1] += "\n" + piece
            else:
                chunks.append(piece)
            open_section = False

        def flush() -> None:
            if kv:
                add("\n".join(kv))
                kv.clear()

        for part in self.parts:
            if part.kind == "kv":
                kv.append(f"{e(part.key)}: {_html_inline(_bold(part.value))}")
                continue
            flush()
            if part.kind == "text":
                add(_html_inline(part.value))
            elif part.kind == "note":
                add(f"<i>{e(plain(part.value))}</i>")
            elif part.kind == "bullets":
                add("\n".join(f"• {_html_inline(x)}" for x in part.items))
            elif part.kind == "details":
                body = (e(plain(x)) if part.mono else _html_inline(x) for x in part.items)
                add(f"<blockquote expandable><b>{e(part.key)}</b>\n" + "\n".join(body) + "</blockquote>")
            elif part.kind == "section":
                if open_section:
                    chunks[-1] = f"<b>{e(part.key)}</b>"
                else:
                    chunks.append(f"<b>{e(part.key)}</b>")
                open_section = True
            elif part.table is not None:
                add(_html_table(part.table, max_rows))
        flush()
        if self.foot:
            chunks.append(f"<i>{e(self.foot)}</i>")
        return chunks


def _esc(text: str) -> str:
    return _html.escape(text, quote=False)


def _utf16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _cell(
    text: Inline, *, head: bool = False, align: Align = "left", colspan: int | None = None
) -> RichBlockTableCell:
    return RichBlockTableCell(
        text=_rich_inline(text),
        is_header=True if head else None,
        align=align,
        valign="middle",
        colspan=colspan if colspan and colspan > 1 else None,
    )


def _rich_table(t: _Table) -> tuple[list[list[RichBlockTableCell]], int]:
    cells = [[_cell(h, head=True, align=a) for h, a in zip(t.header, t.aligns, strict=True)]]
    for row in t.rows[: t.max_rows]:
        cells.append([_cell(v, align=a) for v, a in zip(row, t.aligns, strict=True)])
    rest = len(t.rows) - t.max_rows
    if rest > 0:
        cells.append([_cell(i(f"… и ещё {rest}"), colspan=len(t.header))])
    if t.total is not None:
        cells.append([_cell(_bold(v), align=a) for v, a in zip(t.total, t.aligns, strict=True)])
    return cells, len(cells)


def _html_table(t: _Table, max_rows: int | None) -> str:
    limit = min(t.max_rows, max_rows) if max_rows is not None else t.max_rows
    shown = t.rows[:limit]
    rest = len(t.rows) - len(shown)
    more = f"\n<i>… и ещё {rest}</i>" if rest > 0 else ""
    cols = len(t.header)
    if cols == 2:  # label → value: plain lines wrap better than monospace
        lines = [f"{_html_inline(r[0])}: {_html_inline(_bold(r[1]))}" for r in shown]
        if t.total is not None:
            lines.append(f"<b>{_esc(plain(t.total[0]))}: {_esc(plain(t.total[1]))}</b>")
        return "\n".join(lines) + more
    if cols > 1:
        grid = [t.header, *([plain(c) for c in r] for r in shown)]
        if t.total is not None:
            grid.append([plain(c) for c in t.total])
        pre = pre_table(
            grid,
            t.aligns,
            shrink=t.shrink,
            rule_before=len(shown) + 1 if t.total is not None else None,
        )
        if pre is not None:
            return pre + more
    # too wide for a phone: one line per row
    lines = []
    for row in [*shown, *([t.total] if t.total is not None else [])]:
        rest_cols = " · ".join(
            f"{_esc(h)} {_html_inline(v)}" for h, v in zip(t.header[1:], row[1:], strict=True)
        )
        lines.append(f"<b>{_esc(plain(row[0]))}</b>" + (f" · {rest_cols}" if rest_cols else ""))
    return "\n".join(lines) + more


# ---------------------------------------------------------------------------------------------- gate


class RichGate:
    """Per chat: may we try a rich message? A rejection turns rich off for that chat for ``ttl`` seconds.

    A server without ``sendRichMessage`` at all (an older local Bot API server) turns it off everywhere:
    there is no point in probing every chat, each probe costs a slot of that chat's rate limit."""

    def __init__(self, ttl: float = 6 * 3600.0, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl
        self._clock = clock
        self._off: dict[int, float] = {}
        self._no_banner: dict[int, float] = {}
        self._all_off_until = 0.0

    def ok(self, chat_id: int) -> bool:
        if self._all_off_until and self._clock() < self._all_off_until:
            return False
        return self._fresh(self._off, chat_id)

    def banner_ok(self, chat_id: int) -> bool:
        return self._fresh(self._no_banner, chat_id)

    def off(self, chat_id: int, reason: str = "") -> None:
        if chat_id not in self._off:
            log.info("rich messages are off for chat %s for %.0f s: %s", chat_id, self._ttl, reason[:120])
        self._off[chat_id] = self._clock() + self._ttl

    def off_everywhere(self, reason: str = "") -> None:
        if not (self._all_off_until and self._clock() < self._all_off_until):
            log.info("rich messages are off for all chats for %.0f s: %s", self._ttl, reason[:120])
        self._all_off_until = self._clock() + self._ttl

    def banner_off(self, chat_id: int) -> None:
        self._no_banner[chat_id] = self._clock() + self._ttl

    def _fresh(self, table: dict[int, float], chat_id: int) -> bool:
        until = table.get(chat_id)
        if until is None:
            return True
        if self._clock() >= until:
            del table[chat_id]
            return True
        return False


async def banner_media(bot: Bot | None) -> str | None:
    """The banner's ``file_id`` when the banner is on (``svbg.tg.banner``), else ``None``. Never raises."""
    if bot is None:
        return None
    try:
        from svbg.tg import banner as _banner  # optional: written alongside

        on = _banner.is_banner_on()
        if inspect.isawaitable(on):
            on = await on
        if not on:
            return None
        file_id = _banner.banner_file_id(bot)
        if inspect.isawaitable(file_id):
            file_id = await file_id
    except Exception as exc:  # noqa: BLE001 - no banner module yet, no settings, a failed upload: just no banner
        log.debug("no banner for a report: %s", type(exc).__name__)
        return None
    return file_id if isinstance(file_id, str) and file_id else None


#: Shared by direct sends (owners' DMs outside the admin chat service).
GATE: Final = RichGate()


async def send_report(
    notifier: Any,
    chat_id: int,
    report: Report,
    *,
    reply_markup: Any = None,
    bot: Bot | None = None,
    gate: RichGate = GATE,
    **kw: Any,
) -> Any:
    """Send ``report`` through the notifier: rich first, the HTML text when the chat refuses rich messages."""
    if gate.ok(chat_id):
        banner = await banner_media(bot) if gate.banner_ok(chat_id) else None
        try:
            rich = report.rich(banner=banner)
        except ValueError:
            rich = None
        if rich is not None:
            call_kw = {"priority": kw["priority"]} if "priority" in kw else {}
            method = SendRichMessage(
                chat_id=chat_id,
                rich_message=rich,
                reply_markup=reply_markup,
                disable_notification=kw.get("disable_notification"),
            )
            try:
                return await notifier.call(method, chat_id=chat_id, **call_kw)
            except TelegramNotFound as exc:  # no such method on this server
                gate.off_everywhere(getattr(exc, "message", "") or type(exc).__name__)
            except TelegramBadRequest as exc:
                if banner:
                    gate.banner_off(chat_id)
                gate.off(chat_id, getattr(exc, "message", "") or type(exc).__name__)
    return await notifier.send(chat_id, report.html(), parse_mode="HTML", reply_markup=reply_markup, **kw)
