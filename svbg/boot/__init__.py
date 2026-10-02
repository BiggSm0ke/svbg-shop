"""Bootstrap-level helpers that work before the database and settings are available.

Nothing here imports aiogram, SQLAlchemy or other heavy runtime pieces: these modules are used by the CLI
(`svbg env init`), by the settings bootstrap and by the `.env` mirror.
"""

from __future__ import annotations
