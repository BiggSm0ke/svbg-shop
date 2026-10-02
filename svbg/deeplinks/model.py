"""What a deep link asks for: :class:`Intent` (pure data, JSON round-trip, no I/O).

An intent has at most one *target* (``screen`` | ``plan`` | ``topup``) plus optional extras: a promo code, an
ad tag and a referral code. Short links (``l_<code>``) store the same shape in ``deeplinks.intent`` (the
"spec": no ``link_id``/``source``/``expires_at``).

The pending form (``ui_state.pending_intent``) is ``{"kind": "deeplink", "v": 2, …}`` with an expiry. The
stage-2 stub wrote ``{"kind": "deeplink", "v": 1, "type", "value", "raw"}``: :meth:`Intent.from_pending`
still reads it (the raw payload is re-parsed, side effects are not repeated).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Final

from svbg.deeplinks import codec
from svbg.deeplinks.codec import Kind

__all__ = ["PENDING_KIND", "PENDING_VERSION", "Intent", "IntentError"]

PENDING_KIND: Final = "deeplink"
PENDING_VERSION: Final = 2
_TARGETS: Final = ("screen", "plan", "topup")


class IntentError(ValueError):
    """A spec that cannot be a link (Russian message for the admin)."""


@dataclass(frozen=True, slots=True)
class Intent:
    screen: str | None = None
    plan: str | None = None  # plan code, or a numeric plan id as text
    topup: int | None = None  # major units of the shop currency
    promo: str | None = None
    ad: str | None = None
    ref: str | None = None
    link_id: int | None = None  # the ``deeplinks`` row of an ``l_`` link
    source: str | None = None  # the raw ``start`` payload
    expires_at: datetime | None = None  # only for the pending form

    def __post_init__(self) -> None:
        if sum(getattr(self, t) is not None for t in _TARGETS) > 1:
            raise IntentError("У ссылки может быть только одна цель")

    # ------------------------------------------------------------------ properties

    @property
    def target(self) -> str | None:
        return next((t for t in _TARGETS if getattr(self, t) is not None), None)

    @property
    def actionable(self) -> bool:
        """Something to do after onboarding (a target or a promo); ad and referral act at ``/start``."""
        return self.target is not None or self.promo is not None

    @property
    def empty(self) -> bool:
        return not self.actionable and self.ad is None and self.ref is None

    def expired(self, now: datetime) -> bool:
        return self.expires_at is not None and self.expires_at <= now

    def with_meta(self, **kw: Any) -> Intent:
        return replace(self, **kw)

    def direct_payload(self) -> str | None:
        """The direct (prefix) payload equivalent to this spec, when there is one: a single part only."""
        parts = [
            (Kind.SCREEN, self.screen),
            (Kind.PLAN, self.plan),
            (Kind.TOPUP, self.topup),
            (Kind.PROMO, self.promo),
            (Kind.AD, self.ad),
            (Kind.REF, self.ref),
        ]
        present = [(k, v) for k, v in parts if v is not None]
        if len(present) != 1:
            return None
        kind, value = present[0]
        try:
            return codec.build(kind, str(value))
        except ValueError:
            return None

    # ------------------------------------------------------------------ spec (deeplinks.intent)

    def to_spec(self) -> dict[str, Any]:
        out: dict[str, Any] = {"v": 1}
        for name in ("screen", "plan", "topup", "promo", "ad", "ref"):
            value = getattr(self, name)
            if value is not None:
                out[name] = value
        return out

    @classmethod
    def from_spec(cls, raw: Any) -> Intent:
        """Validate a spec (from the database or the link builder); :class:`IntentError` when broken."""
        if not isinstance(raw, Mapping):
            raise IntentError("Повреждённое описание ссылки")
        values: dict[str, Any] = {}
        for name, kind in (
            ("screen", Kind.SCREEN),
            ("plan", Kind.PLAN),
            ("promo", Kind.PROMO),
            ("ad", Kind.AD),
            ("ref", Kind.REF),
        ):
            value = raw.get(name)
            if value is None:
                continue
            if not isinstance(value, str) or not codec.valid_value(kind, value):
                raise IntentError(f"Недопустимое значение поля {name}")
            values[name] = value
        topup = raw.get("topup")
        if topup is not None:
            if isinstance(topup, bool) or not isinstance(topup, int) or not 0 < topup <= codec.TOPUP_MAX:
                raise IntentError("Сумма пополнения должна быть от 1 до 10 000 000")
            values["topup"] = topup
        return cls(**values)

    # ------------------------------------------------------------------ pending (ui_state.pending_intent)

    def to_pending(self) -> dict[str, Any]:
        out = self.to_spec()
        out.update(
            kind=PENDING_KIND,
            v=PENDING_VERSION,
            link_id=self.link_id,
            source=self.source,
            exp=self.expires_at.isoformat() if self.expires_at is not None else None,
        )
        return out

    @classmethod
    def from_pending(cls, raw: Any) -> Intent | None:
        """The intent kept in ``ui_state``; ``None`` for anything unknown or broken (never raises)."""
        if not isinstance(raw, Mapping) or raw.get("kind") != PENDING_KIND:
            return None
        version = raw.get("v")
        if version == 1:  # the stage-2 stub: re-parse the raw payload
            return cls.from_parsed(codec.parse(raw.get("raw") if isinstance(raw.get("raw"), str) else None))
        if version != PENDING_VERSION:
            return None
        try:
            spec = cls.from_spec(raw)
        except IntentError:
            return None
        link_id = raw.get("link_id")
        source = raw.get("source")
        expires_at: datetime | None = None
        exp = raw.get("exp")
        if isinstance(exp, str):
            try:
                expires_at = datetime.fromisoformat(exp)
            except ValueError:
                return None
            if expires_at.tzinfo is None:
                return None
        return replace(
            spec,
            link_id=link_id if isinstance(link_id, int) and not isinstance(link_id, bool) else None,
            source=source if isinstance(source, str) and codec.is_payload(source) else None,
            expires_at=expires_at,
        )

    @classmethod
    def from_parsed(cls, parsed: codec.Parsed | None) -> Intent | None:
        """The intent of a direct payload (no database): ``None`` for ``setup_``, ``l_`` and bare codes."""
        if parsed is None:
            return None
        source = parsed.raw
        match parsed.kind:
            case Kind.SCREEN:
                return cls(screen=parsed.value, source=source)
            case Kind.PLAN:
                return cls(plan=parsed.value, source=source)
            case Kind.TOPUP:
                return cls(topup=int(parsed.value), source=source)
            case Kind.PROMO:
                return cls(promo=parsed.value, source=source)
            case Kind.AD:
                return cls(ad=parsed.value, source=source)
            case Kind.REF | Kind.LEGACY_REF:
                return cls(ref=parsed.value if parsed.kind is Kind.REF else parsed.raw, source=source)
            case _:
                return None
