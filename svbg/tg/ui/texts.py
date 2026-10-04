"""System strings of the UI engine (toasts, fallback screen, form controls) in one place.

Screen texts are content (``svbg.content``); these are only the engine's own messages. The bot speaks
Russian only.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

__all__ = ["DEFAULT_LANG", "TEXTS", "t"]

#: The only language of the bot. Kept for callers that still pass a language around.
DEFAULT_LANG: Final = "ru"

TEXTS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "stale": "Меню обновилось",
        "denied": "Нет прав",
        "error_toast": "Что-то пошло не так, мы уже знаем",
        "error_text": (
            "⚠️ Что-то пошло не так. Мы уже знаем об ошибке.\n\nПопробуйте ещё раз или вернитесь в меню."
        ),
        "menu": "🏠 Меню",
        "home_text": "Главное меню",
        "busy": "Подождите, выполняю предыдущее действие…",
        "flood": "Слишком часто, подождите пару секунд и повторите",
        "form_cancel": "✖️ Отмена",
        "form_skip": "Пропустить ➡️",
        "form_cancelled": "Отменено",
        "form_expired": "Время ввода истекло",
        "form_error": "⚠️ {error}",
        "form_bad_input": "Не понял ответ, попробуйте ещё раз.",
        "form_text_only": "Пришлите ответ текстом.",
        "form_secret_expired": (
            "🔐 Время ввода истекло: значение не сохранено, сообщение с ним удалено. "
            "Откройте форму заново и пришлите значение ещё раз."
        ),
        "stray_secret": (
            "🔐 Похоже на токен или пароль. Сообщение удалено и нигде не сохранено.\n"
            "Чтобы подключить панель, откройте /setup, нажмите кнопку нужного шага и уже потом "
            "пришлите токен."
        ),
    }
)


def t(_lang: str | None, key: str) -> str:
    """System string ``key``. The first argument (a language) is accepted for old callers and ignored."""
    return TEXTS[key]
