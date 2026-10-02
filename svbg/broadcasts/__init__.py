"""Broadcasts (07 §2.4.5): compose from a ready message, segments (DSL → SQL), persistent delivery job.

* :mod:`svbg.broadcasts.tables` — ``broadcasts``, ``broadcast_msgs``;
* :mod:`svbg.broadcasts.message` — normalized copy, buttons, Bot API calls;
* :mod:`svbg.broadcasts.segments` — presets and the recipients ``WHERE``;
* :mod:`svbg.broadcasts.repo` — rows, drafts, transitions, the audience query;
* :mod:`svbg.broadcasts.sender` — the ``broadcast.run`` / ``broadcast.cleanup`` jobs;
* :mod:`svbg.broadcasts.service` — operations for the admin UI (:mod:`svbg.tg.admin.broadcasts`).

Kept import-free: ``svbg.db.schema`` imports the tables module without Telegram dependencies.
"""

from __future__ import annotations
