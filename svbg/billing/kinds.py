"""Job kinds of billing and their producers (always inside the caller's business transaction)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

from svbg.jobs.queue import enqueue

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

__all__ = [
    "ATTENTION_JOB",
    "FULFILL_JOB",
    "JOB_QUEUE",
    "NOTICE_JOB",
    "UI_JOB",
    "UI_PROGRESS_JOB",
    "enqueue_attention",
    "enqueue_fulfill",
    "enqueue_notice",
    "fulfill_dedup_key",
]

JOB_QUEUE: Final = "billing"
FULFILL_JOB: Final = "billing.fulfill"
UI_JOB: Final = "billing.ui"
UI_PROGRESS_JOB: Final = "billing.ui_progress"
NOTICE_JOB: Final = (
    "billing.notice"  # «tg_send» of 07 §4.5: a user notice (edit the message or send a new one)
)
ATTENTION_JOB: Final = "billing.attention"

FULFILL_MAX_ATTEMPTS: Final = 50


def fulfill_dedup_key(order_id: int) -> str:
    return f"fulfill:order:{int(order_id)}"


async def enqueue_fulfill(conn: AsyncConnection, order_id: int, *, allow_frozen: bool = False) -> int | None:
    """Exactly-once fulfill: ``dedup_key = fulfill:order:<id>`` (+ the order's own status CAS)."""
    payload: dict[str, Any] = {"order_id": int(order_id)}
    if allow_frozen:
        payload["allow_frozen"] = True
    return await enqueue(
        conn,
        FULFILL_JOB,
        payload,
        queue=JOB_QUEUE,
        lane="interactive",
        dedup_key=fulfill_dedup_key(order_id),
        max_attempts=FULFILL_MAX_ATTEMPTS,
        caused_by=f"order:{int(order_id)}",
    )


async def enqueue_notice(
    conn: AsyncConnection, payload: Mapping[str, Any], *, dedup_key: str, run_at: datetime | None = None
) -> int | None:
    return await enqueue(
        conn,
        NOTICE_JOB,
        payload,
        queue=JOB_QUEUE,
        lane="interactive",
        dedup_key=dedup_key,
        max_attempts=20,
        run_at=run_at,
    )


async def enqueue_attention(
    conn: AsyncConnection, key: str, severity: str, title: str, body: str, *, fix_action: str | None = None
) -> int | None:
    """«Требует внимания» after the commit (the attention service writes in its own transaction)."""
    return await enqueue(
        conn,
        ATTENTION_JOB,
        {"key": key, "severity": severity, "title": title, "body": body, "fix_action": fix_action},
        queue=JOB_QUEUE,
        lane="background",
        dedup_key=f"attention:{key}"[:400],
        max_attempts=20,
    )
