"""Built-in HTTP server: Telegram webhook, provider webhooks, local health endpoints."""

from __future__ import annotations

from svbg.web.app import (
    MAX_BODY_BYTES,
    REQUEST_ID_KEY,
    ReadyCheck,
    WebServer,
    build_web_app,
    components_ready,
)

__all__ = [
    "MAX_BODY_BYTES",
    "REQUEST_ID_KEY",
    "ReadyCheck",
    "WebServer",
    "build_web_app",
    "components_ready",
]
