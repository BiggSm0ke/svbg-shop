"""Walk the whole admin as one staff member of a running shop and describe every screen (helpers, no tests).

:func:`crawl` opens ``adm`` and follows every button that *opens* an admin screen (``v1:<screen>:o``),
never an action (an action may change something). A screen is an admin screen when the router guards it by a
staff role. Screens with many arguments (a setting card, a user card) are sampled: :data:`SAMPLE` of each.

:func:`render_markdown` turns the result into ``admin-tree-dump.md`` (or ``SVBG_ADMIN_DUMP_PATH``);
``tests/e2e/test_admin_tree.py`` checks the layout rules on it.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import deque
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from typing import Any

from svbg.tg.ui.codec import decode, encode
from tests.e2e.test_stage3_kit import Chat

RENDERS = ("editMessageText", "editMessageCaption", "editMessageMedia", "sendMessage", "sendPhoto")
#: Screens shown for every argument (the rest: the first :data:`SAMPLE` arguments).
EVERY_ARG = frozenset({"set.v", "set.sec", "apay.c"})
SAMPLE = 2
MAX_SCREENS = 600
MAX_DEPTH = 7


@dataclass
class Button:
    label: str
    data: str | None = None
    url: str | None = None
    copy: str | None = None

    @property
    def screen(self) -> str | None:
        d = decode(self.data) if self.data else None
        return d.screen if d is not None else None

    @property
    def action(self) -> str | None:
        d = decode(self.data) if self.data else None
        return d.action if d is not None else None


@dataclass
class Screen:
    data: str
    code: str
    arg: Any
    depth: int
    via: str | None  # the label of the button that led here
    parent: str | None  # data of the screen that had that button
    text: str = ""
    rows: list[list[Button]] = field(default_factory=list)
    toast: str = ""
    rendered: bool = True

    @property
    def header(self) -> str:
        return _plain(self.text.split("\n", 1)[0]) if self.text else ""

    @property
    def buttons(self) -> list[Button]:
        return [b for row in self.rows for b in row]


def _plain(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _rows(message: Mapping[str, Any]) -> list[list[Button]]:
    markup = message.get("reply_markup")
    rows = markup.get("inline_keyboard") or [] if isinstance(markup, dict) else []
    out: list[list[Button]] = []
    for row in rows:
        line = []
        for b in row:
            copy = b.get("copy_text")
            line.append(
                Button(
                    str(b.get("text", "")),
                    data=b.get("callback_data"),
                    url=b.get("url") or (b.get("web_app") or {}).get("url"),
                    copy=copy.get("text") if isinstance(copy, dict) else None,
                )
            )
        out.append(line)
    return out


def staff_screens(router: Any) -> frozenset[str]:
    """Codes the router guards by a staff role (admin screens; a client's screens have no role)."""
    screens = getattr(router, "_screens", {}) or {}
    return frozenset(
        code
        for code, route in screens.items()
        if getattr(getattr(route, "access", None), "required_role", None) in ("support", "admin", "owner")
    )


async def _open(p: Chat, data: str, timeout: float = 6.0) -> tuple[str, bool]:
    """Press ``data`` on the current message; returns the toast and whether a screen was drawn."""
    p.follow()
    start = len(p.tg.calls)
    toast = await p.click(data)
    deadline = time.monotonic() + timeout
    drawn = False
    while time.monotonic() < deadline:
        if any(c.method in RENDERS and c.params.get("chat_id") == p.telegram_id for c in p.tg.calls[start:]):
            drawn = True
            break
        if toast and time.monotonic() > deadline - timeout + 1.0:
            break  # a toast without a screen
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.05)
    p.follow()
    return toast, drawn


async def crawl(p: Chat, router: Any, *, root: str = "adm") -> list[Screen]:
    """Breadth first from ``root``; see the module docstring."""
    staff = staff_screens(router) | {root}
    queue: deque[Screen] = deque([Screen(encode(root), root, None, 0, None, None)])
    seen: set[str] = {encode(root)}
    per_code: dict[str, int] = {}
    out: list[Screen] = []
    while queue and len(out) < MAX_SCREENS:
        node = queue.popleft()
        toast, drawn = await _open(p, node.data)
        node.toast, node.rendered = toast, drawn
        msg = p.message()
        node.text = str(msg.get("text") or msg.get("caption") or "").replace(" ", " ")
        node.rows = _rows(msg)
        out.append(node)
        if node.depth >= MAX_DEPTH:
            continue
        for b in node.buttons:
            if not b.data or b.data in seen:
                continue
            d = decode(b.data)
            if d is None or d.action != "o" or d.screen not in staff:
                continue
            seen.add(b.data)
            every = d.screen in EVERY_ARG and ":" not in str(d.arg or "")
            if d.arg not in (None, "") and not every:
                if per_code.get(d.screen, 0) >= SAMPLE:
                    continue
                per_code[d.screen] = per_code.get(d.screen, 0) + 1
            queue.append(Screen(b.data, d.screen, d.arg, node.depth + 1, b.label, node.data))
    return out


ADMIN = "🛠 Админка"
FORBIDDEN = ("Нет прав", "Меню обновилось")
MAX_BUTTONS = 20
MAX_ROWS = 15
CAPTION = 1024
BACK = ("⬅️", "✅ Готово")  # «✅ Готово» closes the move mode of a button (its way back)
NO = ("Нет", "Отмена")  # a confirmation: «✅ Да» / «⬅️ Нет» is its own way back


def problems(
    screens: list[Screen],
    *,
    root: str = "adm",
    sections: Collection[str] = (),
    skip: Collection[str] = (),
    foreign: Collection[str] = (),
    long_ok: Collection[str] = (),
) -> list[str]:
    """Layout rules every admin screen keeps (one line per broken rule).

    * drawn, no «Нет прав» / «Меню обновилось»;
    * a section screen (``sections``: the codes of ``nav.TITLES``) starts with the breadcrumb
      «🛠 Админка › …» (``foreign``: screens of modules the admin does not draw); no screen has a title line
      and then the breadcrumb again;
    * the last row is ``[⬅️ <parent>] [🛠 Админка]`` (just ``[🛠 Админка]`` under the root); a form prompt
      (a «✖️ Отмена» button) and a confirmation («Нет») are exempt;
    * only the root leads to the user menu; no screen opens the same target twice; at most
      :data:`MAX_BUTTONS` buttons in :data:`MAX_ROWS` rows; the text fits a picture caption (``long_ok``:
      technical pages that may not).

    ``skip``: flows with their own navigation (the first-run wizard).
    """
    out: list[str] = []
    for s in screens:
        where = f"{s.code}({s.arg})" if s.arg not in (None, "") else s.code
        if not s.rendered or s.toast in FORBIDDEN:
            out.append(f"{where}: not drawn ({s.toast!r})")
            continue
        if s.code == root or s.code in skip:
            continue
        plain = _plain(s.text).split("\n")
        if (
            s.code in sections
            and s.code not in foreign
            and s.arg in (None, "")
            and not plain[0].startswith(f"{ADMIN} › ")
        ):
            out.append(f"{where}: header {plain[0]!r} is not a breadcrumb")
        if len(plain) > 1 and plain[1].startswith(f"{ADMIN} › "):
            out.append(f"{where}: two headers {plain[:2]!r}")
        if len(_plain(s.text)) > CAPTION and s.code not in long_ok:
            out.append(f"{where}: text {len(_plain(s.text))} > {CAPTION}")
        buttons = s.buttons
        if any(b.screen == "home" for b in buttons):
            out.append(f"{where}: leads to the user menu")
        if len(buttons) > MAX_BUTTONS or len(s.rows) > MAX_ROWS:
            out.append(f"{where}: {len(buttons)} buttons in {len(s.rows)} rows")
        targets = [b.data for b in buttons if b.data and b.action != "noop"]
        dup = {t for t in targets if targets.count(t) > 1}
        if dup:
            out.append(f"{where}: the same button twice {sorted(dup)}")
        if any(b.label.startswith("✖️") for b in buttons):
            continue
        last = s.rows[-1] if s.rows else []
        if any(any(word in b.label for word in NO) for b in last):
            continue
        if (
            not last
            or last[-1].label != ADMIN
            or last[-1].screen != root
            or len(last) > 2
            or (len(last) == 2 and not last[0].label.startswith(BACK))
        ):
            out.append(f"{where}: last row {[b.label for b in last]}")
    return out


def _button_md(b: Button) -> str:
    if b.data:
        d = decode(b.data)
        target = "?" if d is None else (d.screen if d.action == "o" else f"{d.screen}:{d.action}")
        if d is not None and d.arg not in (None, ""):
            target += f"({str(d.arg)[:24]})"
        return f"[{b.label} → {target}]"
    if b.copy:
        return f"[{b.label} ⧉]"
    if b.url:
        return f"[{b.label} ↗]"
    return f"[{b.label}]"


def render_markdown(screens: list[Screen], *, who: str) -> str:
    lines = [
        "# Дерево админки",
        "",
        f"Снято автоматически (`tests/e2e/test_admin_tree.py`, `SVBG_ADMIN_DUMP=1`) от лица: {who}.",
        "Обход в ширину от `adm`, только кнопки, которые открывают экраны админки. Экраны с аргументом",
        f"(карточка настройки, пользователя) показаны выборочно, по {SAMPLE} на код.",
        "",
        f"Экранов: {len(screens)}.",
        "",
        "## Оглавление",
        "",
    ]
    for s in screens:
        arg = f"({str(s.arg)[:30]})" if s.arg not in (None, "") else ""
        lines.append(f"- {'  ' * min(s.depth, 6)}`{s.code}{arg}` {s.header[:80]}")
    for s in screens:
        arg = f"({str(s.arg)[:40]})" if s.arg not in (None, "") else ""
        lines += ["", f"## `{s.code}{arg}`", ""]
        if s.via:
            lines.append(f"Кнопка «{s.via}» · глубина {s.depth}")
        if s.toast:
            lines.append(f"Тост: «{s.toast}»")
        if not s.rendered:
            lines.append("**Экран не перерисовался**")
        lines += ["", "```text", _plain(s.text), "```", ""]
        for row in s.rows:
            lines.append("    " + " ".join(_button_md(b) for b in row))
    return "\n".join(lines) + "\n"
