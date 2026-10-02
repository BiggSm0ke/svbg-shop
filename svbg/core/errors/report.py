"""Human-readable Telegram HTML report for an error group.

Format (07 §2.4.3), in the look of the other admin chat reports (``key: <b>value</b>``)::

    🚨 <title>

    Где: <place>
    Модуль: <module>
    Сколько: ×7 за 10 мин (всего ×12)
    Кто: 3 пользователя, последний #42
    Что сделано: <handled>
    Что проверить: <hint>

    <blockquote expandable>Подробности: type, message, the last lines of the stack (masked)</blockquote>

    группа <fingerprint> · событие #id · версия

Everything variable is masked (secrets, PII) and HTML-escaped; the result is hard-capped at 4096 UTF-16
code units (the strictest reading of Telegram's limit), shrinking the stack first.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from svbg.core.errors.classify import Severity
from svbg.core.errors.sanitize import clean

MAX_LEN = 4096
_MAX_TITLE = 200
_MAX_PLACE = 200
_MAX_HANDLED = 300
_MAX_HINT = 600
_MAX_MESSAGE = 600

STACK_LINES = 15  # the bottom of the traceback: where it broke
STACK_BUDGET = 1800

_ICONS = {Severity.ERROR: "🚨", Severity.WARN: "⚠️", Severity.INFO: "ℹ️"}


@dataclass(slots=True)
class ErrorGroupView:
    """Snapshot of an error group handed to sinks and the report renderer.

    ``message`` and ``stack`` are expected to be masked already; the renderer masks again anyway.
    """

    fingerprint: str
    place: str
    title: str
    hint: str
    first_seen: datetime
    last_seen: datetime
    count: int = 1
    users_count: int = 0
    episode_count: int = 1
    episode_started_at: datetime | None = None
    module: str | None = None
    severity: Severity = Severity.ERROR
    handled: str = ""
    reopened: bool = False
    status: str = "open"
    muted_until: datetime | None = None
    last_user_id: int | None = None
    exc_type: str = ""
    message: str = ""
    stack: str = ""
    version: str = ""
    event_id: int | None = None
    chat_ref: Any = None
    extra: dict[str, Any] = field(default_factory=dict)


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


_ESCAPES = {"&": "&amp;", "<": "&lt;", ">": "&gt;"}


def _esc_cut(text: str, limit: int, *, cleaned: bool = False) -> str:
    """Mask, escape and cut so that the *escaped* result is at most ``limit`` UTF-16 units."""
    if not cleaned:
        text = clean(text)
    esc = _esc(text)
    if len(esc) * 2 <= limit or utf16_len(esc) <= limit:  # fast path (≤ 2 units per code point)
        return esc
    out: list[str] = []
    used = 0
    for ch in text:
        piece = _ESCAPES.get(ch, ch)
        cost = utf16_len(piece)
        if used + cost > limit - 1:
            break
        out.append(piece)
        used += cost
    out.append("…")
    return "".join(out)


def plural_ru(n: int, one: str, few: str, many: str) -> str:
    n100 = abs(n) % 100
    n10 = n100 % 10
    if 11 <= n100 <= 14:
        return many
    if n10 == 1:
        return one
    if 2 <= n10 <= 4:
        return few
    return many


def format_duration_ru(delta: timedelta) -> str:
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 60:
        return "меньше минуты"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} мин"
    hours = minutes // 60
    if hours < 48:
        rest = minutes % 60
        return f"{hours} ч {rest} мин" if rest else f"{hours} ч"
    return f"{hours // 24} {plural_ru(hours // 24, 'день', 'дня', 'дней')}"


def _times(view: ErrorGroupView) -> str:
    started = view.episode_started_at or view.first_seen
    if view.episode_count <= 1:
        times = "один раз"
    else:
        times = f"×{view.episode_count} за {format_duration_ru(view.last_seen - started)}"
    if view.count > view.episode_count:
        times += f" (всего ×{view.count})"
    return times


def _who(view: ErrorGroupView) -> str:
    if view.users_count <= 0:
        return "не связано с пользователем"
    users = (
        f"{view.users_count} {plural_ru(view.users_count, 'пользователь', 'пользователя', 'пользователей')}"
    )
    return users + (f", последний #{view.last_user_id}" if view.last_user_id is not None else "")


def _header(view: ErrorGroupView) -> str:
    """Title and ``key: <b>value</b>`` lines, the look of the other admin chat reports."""
    icon = _ICONS.get(Severity(view.severity), "🚨")
    title = _esc_cut(view.title or "Ошибка", _MAX_TITLE)
    prefix = "🔁 снова · " if view.reopened else ""
    lines = [
        f"{icon} {prefix}<b>{title}</b>",
        "",
        f"Где: <b>{_esc_cut(view.place, _MAX_PLACE)}</b>",
    ]
    if view.module:
        lines.append(f"Модуль: <b>{_esc_cut(view.module, 60)}</b>")
    lines.append(f"Сколько: <b>{_esc(_times(view))}</b>")
    lines.append(f"Кто: <b>{_esc(_who(view))}</b>")
    if view.handled:
        lines.append(f"Что сделано: {_esc_cut(view.handled, _MAX_HANDLED)}")
    if view.hint:
        lines.append(f"Что проверить: {_esc_cut(view.hint, _MAX_HINT)}")
    if view.muted_until is not None and view.status == "muted":
        lines.append(f"🔕 Заглушено до {view.muted_until:%d.%m %H:%M} UTC")
    return "\n".join(lines)


def _fit_stack(stack: str, budget: int, max_lines: int = STACK_LINES) -> str:
    """Escaped stack keeping the most recent (bottom) lines that fit ``budget`` UTF-16 units."""
    if budget <= 0 or not stack:
        return ""
    lines = clean(stack).rstrip("\n").split("\n")
    out: list[str] = []
    used = 0
    for n, line in enumerate(reversed(lines)):
        esc = _esc_cut(line, 300, cleaned=True)
        cost = utf16_len(esc) + 1
        if used + cost > budget or n >= max_lines:
            if used + 2 <= budget:
                out.append("…")
            break
        out.append(esc)
        used += cost
    return "\n".join(reversed(out))


def render_report(group: ErrorGroupView) -> str:
    """Telegram HTML (parse_mode=HTML) report, ≤ 4096 UTF-16 code units, valid and masked.

    The card stays a text message (it is edited on every repeat), so the banner comes as a link preview.
    """
    header = _header(group)
    tech_head = ["<b>Подробности</b>", f"Тип: <code>{_esc_cut(group.exc_type or '?', 200)}</code>"]
    if group.message:
        tech_head.append(f"Сообщение: {_esc_cut(group.message, _MAX_MESSAGE)}")
    foot_parts = [f"группа <code>{_esc(group.fingerprint[:12])}</code>"]
    if group.event_id is not None:
        foot_parts.append(f"событие #{group.event_id}")
    if group.version:
        foot_parts.append(f"версия {_esc_cut(group.version, 40)}")
    footer = " · ".join(foot_parts)

    open_tag, close_tag = "<blockquote expandable>", "</blockquote>"
    fixed = "\n".join([header, "", open_tag + "\n".join(tech_head), "Стек:", close_tag, "", footer])
    budget = min(MAX_LEN - utf16_len(fixed) - 1, STACK_BUDGET)
    stack = _fit_stack(group.stack, budget)

    body = [*tech_head]
    if stack:
        body += ["Стек:", stack]
    text = f"{header}\n\n{open_tag}" + "\n".join(body) + f"{close_tag}\n\n{footer}"
    if utf16_len(text) > MAX_LEN:  # unreachable with the field caps above; keep the guarantee anyway
        text = f"{header}\n\n{open_tag}" + "\n".join(tech_head) + f"{close_tag}\n\n{footer}"
    return text
