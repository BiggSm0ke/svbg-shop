"""What a payment plugin may touch at runtime (SDK 1.0-beta, D13): ``ctx.http``, ``ctx.log``, ``ctx.kv``.

A plugin never sees aiohttp, the database or settings. The core builds one :class:`PluginContext` per
instance:

* ``http`` — an HTTP client bound to the instance's outgoing proxy (``PAY_<SLUG>_PROXY_URL``: ``socks5://``,
  ``socks5h://``, ``http://``), with a default timeout, a request counter (D14 budgets) and transport errors
  mapped to :class:`~svbg.sdk.payments.ProviderError` (``retryable=True``);
* ``log`` — a logger whose records pass the core's secret masking;
* ``kv`` — a tiny per-instance key-value store (JSON values) for things like a cached exchange rate.
"""

from __future__ import annotations

import json as _json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

__all__ = ["HttpClient", "HttpResponse", "KeyValue", "PluginContext"]


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """A fully read HTTP response."""

    status: int
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        """Parsed JSON body; ``ValueError`` when the body is not JSON."""
        return _json.loads(self.body.decode("utf-8"))


@runtime_checkable
class HttpClient(Protocol):
    """Outgoing HTTP of one payment instance."""

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None,
        json: Any = None,
        data: bytes | str | Mapping[str, str] | None = None,
        timeout: float | None = None,  # noqa: ASYNC109 - per-request bound, not a cancellation scope
    ) -> HttpResponse:
        """Send a request and read the whole response (bounded size). Raises ``ProviderError`` on
        transport failures (timeout, connection, proxy); HTTP error statuses are returned, not raised."""
        ...


@runtime_checkable
class KeyValue(Protocol):
    """Per-instance persistent key-value store (small JSON values)."""

    async def get(self, key: str) -> Any | None: ...

    async def set(self, key: str, value: Any) -> None: ...

    async def delete(self, key: str) -> None: ...


@dataclass(frozen=True, slots=True)
class PluginContext:
    """Runtime context of one payment instance."""

    http: HttpClient
    log: logging.Logger | logging.LoggerAdapter[logging.Logger]
    kv: KeyValue
    instance_id: int
    slug: str
    is_test: bool = False
    webhook_url: str | None = None  # where the provider must send webhooks (None: no public address)
