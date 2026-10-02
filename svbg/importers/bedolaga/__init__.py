"""Bedolaga → SvBG importer (06 §2, §4.2; 02 §7.3–7.4): ``dry_run`` → ``shadow`` → ``apply``.

Entry point::

    report = await BedolagaImporter(db, source_dsn, panel=ApiPanelReader(api),
    config=ImportConfig(...)).run(mode)

The source is read through its own read-only connection; the panel is only read (``users/stream``); nothing
is ever written to either. See :mod:`svbg.importers.bedolaga.plan` for the modes.
"""

from __future__ import annotations

from svbg.importers.bedolaga.plan import (
    MODES,
    BedolagaImporter,
    ImportConfig,
    Overrides,
    config_from_env_file,
    importer_port,
)
from svbg.importers.bedolaga.report import Report, render_text
from svbg.importers.bedolaga.subscriptions import ApiPanelReader, PanelReader, StaticPanelReader

__all__ = [
    "MODES",
    "ApiPanelReader",
    "BedolagaImporter",
    "ImportConfig",
    "Overrides",
    "PanelReader",
    "Report",
    "StaticPanelReader",
    "config_from_env_file",
    "importer_port",
    "render_text",
]
