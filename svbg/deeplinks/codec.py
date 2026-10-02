"""The ``start`` parameter codec (07 §2.4.4): pure, synchronous, no I/O.

A payload is ≤ 64 characters ``[A-Za-z0-9_-]`` (Telegram's limit for ``?start=``). Direct prefixes:

====================  ===============================================  =======================
prefix (alias)        meaning                                          value
====================  ===============================================  =======================
``s_``                open a screen                                    screen code
``p_`` (``plan_``)    open a plan                                      plan code or plan id
``pr_`` (``promo_``)  apply a promo code                               promo code
``t_``                top up the balance                               amount in major units
``r_`` (``ref_``)     referral code                                    referral code
``a_`` (``ad_``)      ad tag (campaign)                                ``ad_links.code``
``l_``                short link: a row of ``deeplinks``               link code
``setup_``            owner setup (consumed by an earlier router)      —
====================  ===============================================  =======================

The long aliases are the prefixes of 04 §2.5 that are already printed by the plan editor
(``plan_<code>``); they stay valid forever (public surface, 04 D16).

Legacy Bedolaga forms: ``ref<code>`` (referral, no underscore) and a bare campaign code (exact match with
``ad_links.code`` — that lookup needs the database and lives in :mod:`svbg.deeplinks.service`).
"""

from __future__ import annotations

import enum
import re
import secrets
import string
from dataclasses import dataclass
from typing import Final

__all__ = [
    "LINK_CODE_LEN",
    "PAYLOAD_MAX",
    "PREFIXES",
    "TOPUP_MAX",
    "Kind",
    "Parsed",
    "build",
    "is_payload",
    "new_link_code",
    "parse",
    "start_url",
    "valid_value",
]

PAYLOAD_MAX: Final = 64
LINK_CODE_LEN: Final = 8
#: The largest top-up amount a link may carry, in major units (the shop's own limits apply on top).
TOPUP_MAX: Final = 10_000_000

_PAYLOAD_RE: Final = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_LEGACY_REF_RE: Final = re.compile(r"^ref([A-Za-z0-9]{4,32})$")
_SCREEN_RE: Final = re.compile(r"^[A-Za-z0-9_-]{1,32}$")  # callback screen names without '.'
_PLAN_RE: Final = re.compile(r"^[a-z0-9][a-z0-9_]{0,31}$")  # plan code (or a numeric plan id)
_CODE_RE: Final = re.compile(r"^[A-Za-z0-9_-]{1,62}$")
_TOPUP_RE: Final = re.compile(r"^[1-9][0-9]{0,7}$")
_ALPHABET: Final = string.ascii_letters + string.digits


class Kind(enum.StrEnum):
    SCREEN = "screen"
    PLAN = "plan"
    PROMO = "promo"
    TOPUP = "topup"
    REF = "ref"
    AD = "ad"
    LINK = "link"
    SETUP = "setup"
    LEGACY_REF = "legacy_ref"
    BARE = "bare"  # no known prefix: maybe an old Bedolaga campaign code


#: Canonical prefix of every kind a link can be built for.
CANONICAL: Final[dict[Kind, str]] = {
    Kind.SCREEN: "s_",
    Kind.PLAN: "p_",
    Kind.PROMO: "pr_",
    Kind.TOPUP: "t_",
    Kind.REF: "r_",
    Kind.AD: "a_",
    Kind.LINK: "l_",
}

#: Every accepted prefix (no prefix is a prefix of another, so the order does not matter).
PREFIXES: Final[tuple[tuple[str, Kind], ...]] = (
    ("setup_", Kind.SETUP),
    ("promo_", Kind.PROMO),
    ("plan_", Kind.PLAN),
    ("ref_", Kind.REF),
    ("ad_", Kind.AD),
    ("pr_", Kind.PROMO),
    ("s_", Kind.SCREEN),
    ("p_", Kind.PLAN),
    ("t_", Kind.TOPUP),
    ("r_", Kind.REF),
    ("a_", Kind.AD),
    ("l_", Kind.LINK),
)

_VALUE_RE: Final[dict[Kind, re.Pattern[str]]] = {
    Kind.SCREEN: _SCREEN_RE,
    Kind.PLAN: _PLAN_RE,
    Kind.PROMO: _CODE_RE,
    Kind.TOPUP: _TOPUP_RE,
    Kind.REF: _CODE_RE,
    Kind.AD: _CODE_RE,
    Kind.LINK: _CODE_RE,
}


@dataclass(frozen=True, slots=True)
class Parsed:
    kind: Kind
    value: str
    raw: str


def is_payload(raw: str | None) -> bool:
    """True for a syntactically valid ``start`` parameter."""
    return bool(raw) and _PAYLOAD_RE.match(raw or "") is not None


def valid_value(kind: Kind, value: str) -> bool:
    """True when ``value`` is acceptable after the prefix of ``kind``."""
    pattern = _VALUE_RE.get(kind)
    if pattern is None or not pattern.match(value):
        return False
    return kind is not Kind.TOPUP or int(value) <= TOPUP_MAX


def parse(raw: str | None) -> Parsed | None:
    """Classify a payload without the database: ``setup_`` → ``ref…`` legacy → prefixes → bare.

    ``None`` for an empty or malformed payload and for a known prefix with an invalid value (``t_abc``).
    The caller checks the exact ``ad_links.code`` match *before* acting on the result (07 §2.4.4).
    """
    if raw is None:
        return None
    raw = raw.strip()
    if not is_payload(raw):
        return None
    if raw.startswith("setup_"):
        return Parsed(Kind.SETUP, raw[len("setup_") :], raw)
    legacy = _LEGACY_REF_RE.match(raw)
    if legacy is not None:
        return Parsed(Kind.LEGACY_REF, legacy.group(1), raw)
    for prefix, kind in PREFIXES:
        if raw.startswith(prefix):
            value = raw[len(prefix) :]
            return Parsed(kind, value, raw) if valid_value(kind, value) else None
    return Parsed(Kind.BARE, raw, raw)


def build(kind: Kind, value: str | int) -> str:
    """The payload for ``kind``/``value`` (canonical prefix); ``ValueError`` with a Russian reason."""
    prefix = CANONICAL.get(kind)
    if prefix is None:
        raise ValueError("Для этой цели ссылку не собрать")
    text = str(value)
    if not valid_value(kind, text):
        raise ValueError("Недопустимые символы или длина: нужны латиница, цифры, «_» и «-»")
    payload = prefix + text
    if len(payload) > PAYLOAD_MAX:  # pragma: no cover - value patterns keep it within the limit
        raise ValueError(f"Слишком длинно: максимум {PAYLOAD_MAX} символа")
    return payload


def start_url(bot_username: str | None, payload: str) -> str | None:
    """``https://t.me/<bot>?start=<payload>`` or ``None`` while the bot username is unknown."""
    if not bot_username or not is_payload(payload):
        return None
    return f"https://t.me/{bot_username.lstrip('@')}?start={payload}"


def new_link_code(length: int = LINK_CODE_LEN) -> str:
    """A random short-link code (letters and digits; 62⁸ ≈ 2·10¹⁴ values, not guessable in practice)."""
    if not 4 <= length <= 62:
        raise ValueError("length must be 4..62")
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))
