"""Shared SQLAlchemy metadata.

Every package declares its tables in its own ``tables.py`` against this ``metadata``.
``svbg.db.schema`` imports all table modules so that ``metadata`` is complete.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = sa.MetaData(naming_convention=NAMING)

# Column type helpers used across table modules.
UtcDateTime = sa.DateTime(timezone=True)
JSONB = pg.JSONB


def now_default() -> sa.TextClause:
    """Server-side default: current UTC timestamp."""
    return sa.text("now()")
