"""Operations: backups and restore, update check, daily report (04 §10, 07 stage 3a).

* :mod:`svbg.ops.backup` — ``pg_dump`` → tar.gz → scrypt + Fernet → Telegram «💾 Бэкапы» + last N locally;
* :mod:`svbg.ops.restore` — ``svbg restore``: verified restore into an empty database;
* :mod:`svbg.ops.updates` — GitHub Releases check every 12 h → «⚙️ Система»;
* :mod:`svbg.ops.daily_report` — the daily report in the owner's time zone + «Отчёт сейчас»;
* :mod:`svbg.ops.module` — ``setup(router, deps)`` for the app; :mod:`svbg.ops.cli` — CLI commands;
* :mod:`svbg.ops.settings` — the setting definitions (``OPS_SETTINGS``) the app registers.
"""
