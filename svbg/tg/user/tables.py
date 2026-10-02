"""User path tables: the cache of a subscription's HWID devices.

The devices screen must cost no HTTP to the panel on a click (07 §2.6): the list is read from this cache and
refreshed in the background (job ``user.devices_refresh``: one ``GET /hwid/devices/{id}``). The panel's
``user_hwid_devices.added|deleted`` webhooks drop the cached row, so the next opening refreshes it. Only what
the screen shows is stored (no IP addresses, no user agents).
"""

from __future__ import annotations

import sqlalchemy as sa

import svbg.subscriptions.tables  # noqa: F401 - ``subscriptions`` must be on the metadata for the foreign key
from svbg.db.meta import JSONB, UtcDateTime, metadata, now_default

__all__ = ["user_devices"]

user_devices = sa.Table(
    "user_devices",
    metadata,
    sa.Column(
        "subscription_id",
        sa.BigInteger,
        sa.ForeignKey("subscriptions.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    # [{"hwid", "platform", "os_version", "model", "created_at"}] in the panel's order.
    sa.Column("devices", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    sa.Column("fetched_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.CheckConstraint("jsonb_typeof(devices) = 'array'", name="devices_array"),
)
