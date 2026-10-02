"""Error isolation and human reports (07 §2.4.3).

- :func:`guard` / :func:`timeout_guard` — boundaries around updates, screens, slots, jobs;
- :class:`ErrorHub` — fingerprints, grouping, throttled delivery, module circuit breakers;
- :func:`classify` / :func:`register_classifier` — "what happened / what to check" rules;
- :func:`render_report` — Telegram HTML report (≤ 4096, masked).
"""

from __future__ import annotations

from svbg.core.errors.boundary import Capturer, guard, timeout_guard, was_captured
from svbg.core.errors.breaker import BreakerState, CircuitBreaker
from svbg.core.errors.classify import Classification, Severity, classify
from svbg.core.errors.classify import register as register_classifier
from svbg.core.errors.fingerprint import fingerprint
from svbg.core.errors.hub import DEFAULT_HANDLED, Delivery, ErrorHub, ErrorSink
from svbg.core.errors.report import ErrorGroupView, render_report

__all__ = [
    "DEFAULT_HANDLED",
    "BreakerState",
    "Capturer",
    "CircuitBreaker",
    "Classification",
    "Delivery",
    "ErrorGroupView",
    "ErrorHub",
    "ErrorSink",
    "Severity",
    "classify",
    "fingerprint",
    "guard",
    "register_classifier",
    "render_report",
    "timeout_guard",
    "was_captured",
]
