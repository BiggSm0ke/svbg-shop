"""``POST /webhooks/pay/{instance_id}/{token}`` — payment provider webhooks (07 §4.1, 04 §2.5).

The route only moves bytes: it caps the body at 256 KB (``413`` early by ``Content-Length`` and while reading
chunked bodies), hands the **raw** bytes and headers to :meth:`PaymentCore.handle_webhook` (token check in
constant time, plugin authentication, freshness window, dedup, CAS, crediting — all within one short
transaction, no provider HTTP) and returns the plugin's ``ack``. The token in the path is masked in the
access log (``svbg.web.app``: path parameters named ``*token*``). Bodies are never logged.

A new instance needs no restart: the path is dynamic and the instance is looked up in memory.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

from aiohttp import web

from svbg.core.clock import now
from svbg.sdk.payments import WebhookRequest

if TYPE_CHECKING:
    from svbg.payments.core import PaymentCore

__all__ = ["MAX_BODY", "PATH", "payment_routes"]

log = logging.getLogger("svbg.web.payments")

PATH: Final = "/webhooks/pay/{instance_id}/{token}"
MAX_BODY: Final = 256 * 1024
_MAX_TOKEN: Final = 256


def _reply(status: int, text: str) -> web.Response:
    return web.Response(status=status, text=text, content_type="text/plain")


def payment_routes(core: PaymentCore) -> list[web.RouteDef]:
    """Routes to pass to ``build_web_app``."""

    async def handle(request: web.Request) -> web.Response:
        raw_id = request.match_info.get("instance_id", "")
        token = request.match_info.get("token", "")
        if not raw_id.isdigit() or len(raw_id) > 18 or not token or len(token) > _MAX_TOKEN:
            return _reply(404, "not found")
        length = request.content_length
        if length is not None and length > MAX_BODY:
            return _reply(413, "too large")
        chunks: list[bytes] = []
        size = 0
        async for chunk in request.content.iter_chunked(64 * 1024):
            size += len(chunk)
            if size > MAX_BODY:
                return _reply(413, "too large")
            chunks.append(chunk)
        req = WebhookRequest(
            body=b"".join(chunks),
            headers=list(request.headers.items()),
            method=request.method,
            path=request.path,
            query={k: v for k, v in request.query.items()},
            remote=request.remote,
            received_at=now(),
        )
        resp = await core.handle_webhook(int(raw_id), token, req)
        return web.Response(status=resp.status, body=resp.body, content_type=resp.content_type)

    return [web.post(PATH, handle)]
