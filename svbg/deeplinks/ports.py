"""What the deep-link service needs from other modules (promo, ads, referral), as small protocols.

The promo/ads modules (stage 3, another owner) and the referral module (stage 4a) are wired by the app; the
service works without any of them (the corresponding part of a link is ignored and logged). Every call is
bounded by a timeout and isolated: a failing module never breaks ``/start``.

:func:`referral_port` adapts the stage-4a contract function ``attach_referrer(user_id, code)`` (whatever it
returns) to :class:`ReferralPort`.
"""

from __future__ import annotations

import enum
import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "AdRef",
    "AdsPort",
    "PromoInfo",
    "PromoOutcome",
    "PromoPort",
    "PromoStatus",
    "ReferralPort",
    "referral_port",
]


class PromoStatus(enum.StrEnum):
    APPLIED = "applied"  # days / wallet / gift granted right now
    PENDING = "pending"  # a discount bound to the user (``pending_promo``), applied at checkout
    REFUSED = "refused"  # unknown, expired, used up, not for this user …


@dataclass(frozen=True, slots=True)
class PromoOutcome:
    status: PromoStatus
    text: str | None = None  # a short Russian line for the user («Промокод AUTUMN −20% применён ✓»)


@dataclass(frozen=True, slots=True)
class PromoInfo:
    id: int
    code: str
    kind: str  # days | percent | fixed | wallet | trial | gift | wallet+days …
    active: bool = True
    summary: str | None = None  # «−20% на покупку», «+7 дней» (admin builder)


@runtime_checkable
class PromoPort(Protocol):
    async def lookup(self, code: str) -> PromoInfo | None:
        """The promo code as stored (case as in the database), for the link builder."""
        ...

    async def apply_from_link(self, user_id: int, code: str) -> PromoOutcome:
        """Days/wallet: apply now (limits, ``promo_uses``); discount: bind as ``pending_promo``."""
        ...


@dataclass(frozen=True, slots=True)
class AdRef:
    id: int
    code: str
    active: bool = True
    title: str | None = None


@runtime_checkable
class AdsPort(Protocol):
    async def find(self, code: str) -> AdRef | None:
        """Exact ``ad_links.code`` match (Bedolaga campaign codes are kept as is)."""
        ...

    async def attach(self, user_id: int, ad_link_id: int, *, is_new: bool) -> None:
        """First-touch attribution (``users.ad_link_id`` when empty); bonuses are the ads module's job."""
        ...


@runtime_checkable
class ReferralPort(Protocol):
    async def attach_referrer(self, user_id: int, code: str) -> bool:
        """Bind the referrer of ``code`` to a new user; ``False`` when the code is unknown or refused."""
        ...


def _truthy(result: Any) -> bool:
    for attr in ("attached", "ok", "success"):
        value = getattr(result, attr, None)
        if isinstance(value, bool):
            return value
    return bool(result)


class _CallableReferral:
    def __init__(self, fn: Callable[[int, str], Any]) -> None:
        self._fn = fn

    async def attach_referrer(self, user_id: int, code: str) -> bool:
        result = self._fn(user_id, code)
        if inspect.isawaitable(result):
            result = await result
        return _truthy(result)


def referral_port(source: Any) -> ReferralPort | None:
    """A :class:`ReferralPort` from a service object or a bare ``attach_referrer(user_id, code)`` function.

    The result of the call may be a bool, ``None`` (treated as "not attached") or an object with an
    ``attached``/``ok``/``success`` boolean attribute.
    """
    if source is None:
        return None
    fn = getattr(source, "attach_referrer", None)
    if fn is None and callable(source):
        fn = source
    if fn is None or not callable(fn):
        return None
    return _CallableReferral(fn)
