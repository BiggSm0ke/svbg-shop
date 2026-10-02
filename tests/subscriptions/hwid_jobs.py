"""``panel.hwid_delete`` / ``panel.hwid_reset`` handlers now live in the single panel writer
(:class:`svbg.remnawave.writer.DeviceJobs`, registered by :meth:`PanelWriter.handlers`); this module keeps the
old import path of the tests.
"""

from __future__ import annotations

from svbg.remnawave.writer import DeviceJobs

__all__ = ["DeviceJobs"]
