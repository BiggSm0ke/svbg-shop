"""Core building blocks shared by every SvBG package.

Kept import-light on purpose: submodules (``clock``, ``ids``, ``money``, ``log``, ``crypto``, ...) are
imported explicitly by their users, so importing ``svbg.core`` never pulls heavy dependencies or creates
import cycles between core modules.
"""

from __future__ import annotations
