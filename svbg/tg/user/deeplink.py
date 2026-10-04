"""``/start <payload>`` parsing — the stub of 07 §2.4.4 (full handling arrives in stage 3b).

The codec of the ``start`` parameter (≤ 64 characters ``[A-Za-z0-9_-]``): direct prefixes ``s_<screen>``,
``p_<plan>``, ``pr_<promo>``, ``t_<amount>`` (top-up), ``r_<ref code>``, ``a_<ad tag>``, the short link
``l_<code>`` and legacy Bedolaga forms (``ref<code>``, a bare campaign code). ``setup_…`` belongs to the owner
setup and is consumed by an earlier router.

Stage 2 only *parses* the payload and keeps it as ``ui_state.pending_intent`` (the intent survives onboarding:
captcha, channel check), so stage 3b can act on it without changing ``/start``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Final

__all__ = ["DeepLink", "parse_start_payload"]

_PAYLOAD_RE: Final = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
#: Longest prefixes first (``pr_`` before ``p_``).
PREFIXES: Final[tuple[tuple[str, str], ...]] = (
    ("pr_", "promo"),
    ("s_", "screen"),
    ("p_", "plan"),
    ("t_", "topup"),
    ("r_", "ref"),
    ("a_", "ad"),
    ("l_", "link"),
)
_LEGACY_REF_RE: Final = re.compile(r"^ref([A-Za-z0-9]{4,32})$")


@dataclass(frozen=True, slots=True)
class DeepLink:
    kind: str  # screen | plan | promo | topup | ref | ad | link | legacy_ref | legacy_code
    value: str
    raw: str

    def as_intent(self) -> dict[str, Any]:
        return {"kind": "deeplink", "v": 1, "type": self.kind, "value": self.value, "raw": self.raw}


def parse_start_payload(payload: str | None) -> DeepLink | None:
    """The deep link of ``/start <payload>``; ``None`` for no / invalid payload and for ``setup_…``."""
    if not payload:
        return None
    raw = payload.strip()
    if not _PAYLOAD_RE.match(raw) or raw.startswith("setup_"):
        return None
    for prefix, kind in PREFIXES:
        if raw.startswith(prefix):
            value = raw[len(prefix) :]
            if not value:
                return None
            if kind == "topup" and not value.isdigit():
                return None
            return DeepLink(kind, value, raw)
    legacy = _LEGACY_REF_RE.match(raw)
    if legacy is not None:
        return DeepLink("legacy_ref", legacy.group(1), raw)
    return DeepLink("legacy_code", raw, raw)
