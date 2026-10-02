"""Masking of secrets and personal data for error reports, plus the default clock of the hub.

Secrets are masked by :func:`svbg.core.log.mask` (registered secrets + known token shapes); on top of that
reports drop personal data (e-mails, @usernames, phone numbers) and contexts lose sensitive keys.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from svbg.core.clock import now as _core_now
from svbg.core.log import mask as _core_mask

log = logging.getLogger("svbg.errors")


def mask(text: str) -> str:
    """Mask secrets in ``text`` (registered secrets and known token shapes, see ``svbg.core.log``)."""
    return _core_mask(text) if text else text


def default_clock() -> datetime:
    """Aware UTC now from ``svbg.core.clock`` (honours ``set_clock`` in tests)."""
    return _core_now()


# --- personal data -----------------------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_USERNAME_RE = re.compile(r"(?<![\w@./])@[A-Za-z][A-Za-z0-9_]{3,31}\b")
# Phone-like: optional "+", 10-15 digits possibly separated by spaces/dashes/parentheses.
_PHONE_RE = re.compile(r"(?<![\w.])\+?\d(?:[ \-()]{0,2}\d){9,14}(?![\w.])")


def scrub_pii(text: str) -> str:
    """Remove e-mails, @usernames and phone-like numbers."""
    if not text:
        return text
    text = _EMAIL_RE.sub("***@***", text)
    text = _USERNAME_RE.sub("@***", text)
    return _PHONE_RE.sub("***", text)


def clean(text: str) -> str:
    """Secrets and PII removed: safe to show in a report or store in the database."""
    return scrub_pii(mask(text))


def truncate(text: str, limit: int, *, keep: str = "head") -> str:
    """Cut ``text`` to ``limit`` characters adding an ellipsis marker."""
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    if keep == "tail":
        return "…" + text[-(limit - 1) :]
    return text[: limit - 1] + "…"


# --- context sanitization ----------------------------------------------------------------------------------

_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(token|secret|passw|pwd|api[_-]?key|cookie|authorization|auth|session|signature|card|cvv|phone"
    r"|email|username|first_name|last_name)"
)
_MAX_CONTEXT_KEYS = 30
_MAX_VALUE_LEN = 300
_MAX_DEPTH = 3


def _sanitize_value(value: Any, depth: int) -> Any:
    if value is None or isinstance(value, bool | int | float):
        if isinstance(value, float) and math.isnan(value):  # NaN is not valid JSON
            return None
        return value
    if isinstance(value, str):
        return truncate(clean(value), _MAX_VALUE_LEN)
    if depth >= _MAX_DEPTH:
        return truncate(clean(repr(value)), _MAX_VALUE_LEN)
    if isinstance(value, Mapping):
        return sanitize_context(value, _depth=depth + 1)
    if isinstance(value, list | tuple | set | frozenset):
        items = list(value)[:_MAX_CONTEXT_KEYS]
        return [_sanitize_value(v, depth + 1) for v in items]
    if isinstance(value, datetime):
        return value.isoformat()
    return truncate(clean(str(value)), _MAX_VALUE_LEN)


def sanitize_context(context: Mapping[Any, Any] | None, *, _depth: int = 0) -> dict[str, Any]:
    """JSON-safe, bounded, masked copy of a context mapping. Sensitive keys get ``"***"``."""
    if not context:
        return {}
    out: dict[str, Any] = {}
    for i, (key, value) in enumerate(context.items()):
        if i >= _MAX_CONTEXT_KEYS:
            out["…"] = f"+{len(context) - _MAX_CONTEXT_KEYS}"
            break
        k = truncate(str(key), 64)
        out[k] = "***" if _SENSITIVE_KEY_RE.search(k) else _sanitize_value(value, _depth)
    return out


def is_json_safe(value: Any) -> bool:
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return False
    return True


Clock = Callable[[], datetime]
