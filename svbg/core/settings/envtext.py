"""Texts of the generated ``.env``: file header and the comment lines above every key (07 §3.2).

Comments are regenerated on every render, so they must be deterministic (no dates, no current values).
"""

from __future__ import annotations

import textwrap
from typing import Final

from svbg.core.settings import values
from svbg.core.settings.registry import Apply, SettingDef

__all__ = ["COMPACT_NOTE", "HEADER", "comment_lines"]

_RULE: Final = "═" * 75

HEADER: Final[list[str]] = [
    _RULE,
    "SvBG Shop — все настройки. Файл синхронизируется с ботом в обе стороны:",
    "  • изменили в боте — через ~1 с изменится здесь;",
    "  • изменили здесь — бот применит за ~2 с без перезапуска (или пометит ошибку строкой «⚠»);",
    "  • тарифы, тексты, экраны, кнопки и картинки — в боте (/content), не здесь.",
    "Права файла 0600: здесь могут быть секреты. Настраивать удобнее в боте: /settings",
    _RULE,
]

COMPACT_NOTE: Final = (
    "Компактный вид (ENV_LAYOUT=compact): ключи со значениями по умолчанию не показаны. "
    "Можно вписать любой ключ — бот его применит."
)

_APPLY_TEXT: Final = {
    Apply.HOT: "⚡ применяется сразу",
    Apply.RESTART: "♻️ нужен перезапуск",
}


_WIDTH: Final = 100


def _apply_text(defn: SettingDef) -> str:
    if defn.readonly:
        return "🔒 правкой не меняется"
    if defn.apply is Apply.RELOAD:
        return f"🔄 переподключит {defn.component}"
    return _APPLY_TEXT[defn.apply]


def _range_text(defn: SettingDef) -> str | None:
    if defn.min is not None and defn.max is not None:
        return f"Диапазон {_num(defn.min)}–{_num(defn.max)}."
    if defn.min is not None:
        return f"Не меньше {_num(defn.min)}."
    if defn.max is not None:
        return f"Не больше {_num(defn.max)}."
    return None


def _num(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def comment_lines(defn: SettingDef, *, locked: bool = False, omitted: bool = False) -> list[str]:
    """Comment lines (without ``#``) written above ``defn`` in ``.env``."""
    lines = [
        wrapped
        for part in defn.description.splitlines()
        if part.strip()
        for wrapped in textwrap.wrap(part.strip(), _WIDTH, break_long_words=False, break_on_hyphens=False)
    ]
    meta: list[str] = []
    if defn.choices:
        meta.append("Значения: " + " | ".join(defn.choices) + ".")
    rng = _range_text(defn)
    if rng:
        meta.append(rng)
    if defn.is_secret:
        meta.append("Секрет.")
    elif defn.default is None or defn.default == []:
        meta.append("По умолчанию: пусто.")
    else:
        meta.append(f"По умолчанию: {values.to_text(defn, defn.default)}.")
    meta.append(_apply_text(defn))
    lines.append(" ".join(meta))
    if defn.aliases:
        lines.append("Раньше называлось: " + ", ".join(defn.aliases) + ".")
    if locked:
        lines.append("🔒 Задано окружением контейнера (LOCKED_KEYS): правка здесь не применится.")
    if omitted:
        lines.append(
            "Значение хранится только в БД (ENV_SECRETS=omit). Чтобы сменить — впишите новое вместо заглушки."
        )
    return lines
