"""System strings of the UI engine (toasts, fallback screen, form controls) in one place.

Screen texts are content (``svbg.content``); these are only the engine's own messages. They move to Fluent
together with the other system messages later.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

__all__ = ["DEFAULT_LANG", "t"]

DEFAULT_LANG: Final = "ru"

_TEXTS: Final[Mapping[str, Mapping[str, str]]] = MappingProxyType(
    {
        "ru": MappingProxyType(
            {
                "stale": "Меню обновилось",
                "denied": "Нет прав",
                "error_toast": "Что-то пошло не так, мы уже знаем",
                "error_text": (
                    "⚠️ Что-то пошло не так. Мы уже знаем об ошибке.\n\n"
                    "Попробуйте ещё раз или вернитесь в меню."
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
        ),
        "en": MappingProxyType(
            {
                "stale": "The menu has been updated",
                "denied": "Not allowed",
                "error_toast": "Something went wrong. We have been notified",
                "error_text": (
                    "⚠️ Something went wrong. We have been notified.\n\nTry again or go back to the menu."
                ),
                "menu": "🏠 Menu",
                "home_text": "Main menu",
                "busy": "Please wait, the previous action is still running…",
                "flood": "Too fast. Wait a couple of seconds and try again",
                "form_cancel": "✖️ Cancel",
                "form_skip": "Skip ➡️",
                "form_cancelled": "Cancelled",
                "form_expired": "Input timed out",
                "form_error": "⚠️ {error}",
                "form_bad_input": "Did not get that, please try again.",
                "form_text_only": "Please send the answer as text.",
                "form_secret_expired": (
                    "🔐 Time ran out, so nothing was saved, and the bot deleted your message. "
                    "Open the form again and send the value once more."
                ),
                "stray_secret": (
                    "🔐 This looks like a token or a password, so the bot deleted the message and saved "
                    "nothing.\nTo connect the panel, open /setup, tap the button for the step you need, "
                    "then send the token."
                ),
            }
        ),
    }
)


def t(lang: str | None, key: str) -> str:
    """System string ``key`` in ``lang`` (falls back to Russian)."""
    table = _TEXTS.get(lang or DEFAULT_LANG) or _TEXTS[DEFAULT_LANG]
    return table.get(key) or _TEXTS[DEFAULT_LANG][key]
