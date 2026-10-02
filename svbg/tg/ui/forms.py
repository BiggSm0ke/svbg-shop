"""Multi-step text input (wizards) persisted in ``ui_state.awaiting`` so it survives restarts.

A :class:`Form` is a list of :class:`Field` s with validators. The router starts a form, routes the user's
text messages into it step by step, re-asks with a human error on invalid input, supports "Отмена" (button or
``/cancel``) and "Пропустить" for optional fields, and calls ``on_done(ctx, data)`` at the end.

State in the database is plain JSON (``{"kind": "form", "v": 1, "form", "step", "data", "exp"}``); a state
whose form is no longer registered or whose ``exp`` passed is discarded. Secret fields (tokens, keys) must be
the last field: their value is handed to ``on_done`` directly and never written to ``ui_state``; the router
also deletes the user's message containing it.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from svbg.core import clock
from svbg.tg.ui import texts
from svbg.tg.ui.codec import encode
from svbg.tg.ui.context import ROLE_RANK
from svbg.tg.ui.renderer import nav_button
from svbg.tg.ui.view import View

if TYPE_CHECKING:
    from aiogram.types import InlineKeyboardButton

    from svbg.tg.ui.router import HandlerResult, ScreenCtx

__all__ = [
    "FORM_SCREEN",
    "Field",
    "Form",
    "FormState",
    "StepResult",
    "ValidationError",
    "advance",
    "integer",
    "prompt_view",
    "skip",
    "start_state",
    "text",
]

FORM_SCREEN: Final = "form"  # callback screen: v1:form:cancel / v1:form:skip
STATE_VERSION: Final = 1
MAX_INPUT: Final = 4096
_NAME_RE: Final = re.compile(r"^[a-z][a-z0-9_.]{0,47}$")


class ValidationError(ValueError):
    """Raised by validators; ``str(error)`` is shown to the user as is (keep it short, Russian); ``en`` is the
    same in English (shown to English-speaking users; ``None`` — the Russian text)."""

    def __init__(self, text: str = "", en: str | None = None) -> None:
        super().__init__(text)
        self.en = en


Validator = Callable[[str], Any]


def text(*, min_len: int = 1, max_len: int = 1024, strip: bool = True) -> Validator:
    """Plain text validator (length in characters)."""

    def check(value: str) -> str:
        v = value.strip() if strip else value
        if len(v) < min_len:
            raise ValidationError(
                "Слишком коротко" if min_len > 1 else "Пустой ответ",
                "Too short" if min_len > 1 else "Empty answer",
            )
        if len(v) > max_len:
            raise ValidationError(
                f"Слишком длинно: максимум {max_len} символов", f"Too long: at most {max_len} characters"
            )
        return v

    return check


def integer(*, min_value: int | None = None, max_value: int | None = None) -> Validator:
    def check(value: str) -> int:
        raw = value.strip().replace(" ", "").replace(" ", "")
        if not re.fullmatch(r"[+-]?\d{1,18}", raw):
            raise ValidationError("Нужно целое число", "A whole number is needed")
        n = int(raw)
        if min_value is not None and n < min_value:
            raise ValidationError(f"Минимум {min_value}", f"Minimum {min_value}")
        if max_value is not None and n > max_value:
            raise ValidationError(f"Максимум {max_value}", f"Maximum {max_value}")
        return n

    return check


@dataclass(frozen=True, slots=True)
class Field:
    name: str
    prompt: Mapping[str, str] | str  # by language, or one string
    validator: Validator | None = None  # default: non-empty text
    optional: bool = False
    secret: bool = False

    def prompt_for(self, lang: str) -> str:
        if isinstance(self.prompt, str):
            return self.prompt
        return (
            self.prompt.get(lang) or self.prompt.get(texts.DEFAULT_LANG) or next(iter(self.prompt.values()))
        )


FormDone = Callable[["ScreenCtx", dict[str, Any]], Awaitable["HandlerResult"]]
FormCancel = Callable[["ScreenCtx"], Awaitable["HandlerResult"]]

_DEFAULT_VALIDATOR: Final = text()


@dataclass(frozen=True, slots=True)
class Form:
    name: str
    fields: tuple[Field, ...]
    on_done: FormDone
    on_cancel: FormCancel | None = None
    required_role: str | None = None
    perm: str | None = None
    ttl: timedelta = timedelta(hours=1)

    def __post_init__(self) -> None:
        if not _NAME_RE.match(self.name):
            raise ValueError(f"invalid form name {self.name!r}")
        if not self.fields:
            raise ValueError("a form needs at least one field")
        names = [f.name for f in self.fields]
        if len(set(names)) != len(names):
            raise ValueError("field names must be unique")
        for i, f in enumerate(self.fields):
            if f.secret and i != len(self.fields) - 1:
                raise ValueError("a secret field must be the last one (it is never persisted)")
        if self.required_role is not None and self.required_role not in ROLE_RANK:
            raise ValueError(f"unknown role {self.required_role!r}")
        if self.ttl <= timedelta(0):
            raise ValueError("ttl must be positive")


@dataclass(slots=True)
class FormState:
    form: str
    step: int
    data: dict[str, Any] = field(default_factory=dict)
    expires_at: datetime | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": "form",
            "v": STATE_VERSION,
            "form": self.form,
            "step": self.step,
            "data": self.data,
            "exp": self.expires_at.isoformat() if self.expires_at else None,
        }

    @classmethod
    def from_json(cls, value: Any) -> FormState | None:
        if not isinstance(value, Mapping) or value.get("kind") != "form" or value.get("v") != STATE_VERSION:
            return None
        form, step, data = value.get("form"), value.get("step"), value.get("data")
        if not isinstance(form, str) or not isinstance(step, int) or isinstance(step, bool) or step < 0:
            return None
        if not isinstance(data, dict):
            return None
        exp_raw = value.get("exp")
        expires_at: datetime | None = None
        if isinstance(exp_raw, str):
            try:
                expires_at = datetime.fromisoformat(exp_raw)
            except ValueError:
                return None
            if expires_at.tzinfo is None:
                return None
        return cls(form, step, dict(data), expires_at)

    def expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= clock.now()


@dataclass(frozen=True, slots=True)
class StepResult:
    state: FormState  # updated state (step advanced unless ``error``)
    done: bool = False
    error: str | None = None
    secret: dict[str, Any] | None = None  # secret values for on_done only (never persisted)
    error_en: str | None = None  # ``error`` in English (``None``: no translation)

    def error_for(self, lang: str | None) -> str | None:
        """``error`` in the user's language (Russian fallback)."""
        return (self.error_en or self.error) if lang == "en" and self.error is not None else self.error


def start_state(form: Form, initial: Mapping[str, Any] | None = None) -> FormState:
    return FormState(form.name, 0, dict(initial or {}), clock.now() + form.ttl)


def _next(form: Form, state: FormState, secret: dict[str, Any] | None = None) -> StepResult:
    step = state.step + 1
    new = FormState(state.form, step, state.data, clock.now() + form.ttl)
    return StepResult(new, done=step >= len(form.fields), secret=secret)


def advance(form: Form, state: FormState, value: str) -> StepResult:
    """Validate ``value`` for the current field and move to the next one."""
    if state.step >= len(form.fields):
        return StepResult(state, done=True)
    f = form.fields[state.step]
    if len(value) > MAX_INPUT:
        return StepResult(
            state,
            error=f"Слишком длинно: максимум {MAX_INPUT} символов",
            error_en=f"Too long: at most {MAX_INPUT} characters",
        )
    validator = f.validator or _DEFAULT_VALIDATOR
    try:
        parsed = validator(value)
    except ValidationError as e:
        return StepResult(state, error=str(e) or None, error_en=e.en)
    except (ValueError, TypeError):
        return StepResult(state, error="")  # router shows the generic "не понял" message
    if f.secret:
        return _next(form, state, {f.name: parsed})
    state.data[f.name] = parsed
    return _next(form, state)


def skip(form: Form, state: FormState) -> StepResult:
    """Skip an optional field (stores ``None``); a required field returns an error result."""
    if state.step >= len(form.fields):
        return StepResult(state, done=True)
    f = form.fields[state.step]
    if not f.optional:
        return StepResult(state, error="Это поле обязательно", error_en="This field is required")
    if not f.secret:
        state.data[f.name] = None
    return _next(form, state)


def prompt_view(form: Form, state: FormState, lang: str, *, error: str | None = None) -> View:
    """Prompt for the current field with "Отмена" (and "Пропустить" for optional fields)."""
    f = form.fields[min(state.step, len(form.fields) - 1)]
    body = f.prompt_for(lang)
    if len(form.fields) > 1:
        body = f"({state.step + 1}/{len(form.fields)}) {body}"
    if error is not None:
        message = error or texts.t(lang, "form_bad_input")
        body = texts.t(lang, "form_error").replace("{error}", message) + "\n\n" + body
    row: list[InlineKeyboardButton] = []
    if f.optional:
        row.append(nav_button(texts.t(lang, "form_skip"), FORM_SCREEN, "skip"))
    row.append(nav_button(texts.t(lang, "form_cancel"), FORM_SCREEN, "cancel"))
    return View(text=body, keyboard=[row])


CANCEL_DATA: Final = encode(FORM_SCREEN, "cancel")
SKIP_DATA: Final = encode(FORM_SCREEN, "skip")
