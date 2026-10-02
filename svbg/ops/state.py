"""Small durable state of the ops module in ``config_meta`` (key → JSON), one statement per call.

Keys: ``ops.backup`` (last run, last scheduled day), ``ops.report`` (last scheduled day),
``ops.updates`` (last check, last notified release).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from svbg.core import clock
from svbg.core.tables import config_meta

if TYPE_CHECKING:
    from svbg.db.engine import Database

__all__ = ["K_BACKUP", "K_REPORT", "K_UPDATES", "MetaState"]

K_BACKUP: Final = "ops.backup"
K_REPORT: Final = "ops.report"
K_UPDATES: Final = "ops.updates"


class MetaState:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def get(self, key: str) -> dict[str, Any]:
        async with self._db.read() as conn:
            value = (
                await conn.execute(sa.select(config_meta.c.value).where(config_meta.c.key == key))
            ).scalar_one_or_none()
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                return {}
        return dict(value) if isinstance(value, Mapping) else {}

    async def merge(self, key: str, values: Mapping[str, Any]) -> None:
        """``value = value || values`` (insert when missing)."""
        payload = json.loads(json.dumps(dict(values), default=str))  # datetimes → ISO text
        stmt = pg_insert(config_meta).values(key=key, value=payload, updated_at=clock.now())
        stmt = stmt.on_conflict_do_update(
            index_elements=[config_meta.c.key],
            set_={
                "value": sa.func.coalesce(config_meta.c.value, sa.text("'{}'::jsonb")).op("||")(
                    stmt.excluded.value
                ),
                "updated_at": stmt.excluded.updated_at,
            },
        )
        async with self._db.tx() as conn:
            await conn.execute(stmt)
