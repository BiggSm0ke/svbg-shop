"""Payment plugin contract, SDK 1.0-beta (07 §4.1, 04 D12–D13). Frozen after 10 providers.

A plugin only speaks the provider's language: it creates an invoice, authenticates and parses a webhook from
the raw bytes, answers the provider (``ack``) and optionally reads statuses (``fetch_status``) or refunds.
Everything about money — CAS, «late payment wins», amount reconciliation, deduplication, the freshness
window, test-mode checks, polling budgets — is done by the core (:mod:`svbg.payments.core`); a plugin never
touches the database and imports only :mod:`svbg.sdk`.

Amounts cross this boundary **as the provider reports them**: :class:`~decimal.Decimal` in major units plus
the currency code (``Decimal("179")`` and ``Decimal("179.00")`` are the same amount). The core converts them
to minor units and compares exactly. The only identifier sent to a provider is the opaque payment id
(:attr:`PaymentIntent.payment_id`, a UUIDv7) — never a Telegram id or a subscription link.
"""

from __future__ import annotations

import abc
import enum
import hashlib
import hmac
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, ClassVar, Final, Literal

from svbg.sdk.config import ConfigModel
from svbg.sdk.context import PluginContext

__all__ = [
    "SDK_VERSION",
    "Capabilities",
    "Checkout",
    "Manifest",
    "MethodKind",
    "PaymentIntent",
    "PaymentProvider",
    "PaymentState",
    "Probe",
    "ProviderError",
    "ProviderEvent",
    "ProviderStatus",
    "RefundResult",
    "WebhookAuth",
    "WebhookIgnored",
    "WebhookRejected",
    "WebhookRequest",
    "WebhookResponse",
    "constant_time_equal",
    "hmac_sha256_hex",
    "parse_amount",
    "parse_timestamp",
]

SDK_VERSION: Final = "1.0-beta"
_SLUG_RE: Final = re.compile(r"[a-z][a-z0-9_]{1,31}")
_CURRENCY_RE: Final = re.compile(r"[A-Z]{3,8}")
_MAX_ID_LEN: Final = 200


class MethodKind(enum.StrEnum):
    """How the user pays; buttons are grouped by kind, the provider's name is hidden (04 §8)."""

    SBP = "sbp"
    CARD = "card"
    INTL_CARD = "intl_card"
    CRYPTO = "crypto"
    STARS = "stars"
    WALLET = "wallet"
    MANUAL = "manual"


class WebhookAuth(enum.StrEnum):
    """How the provider authenticates webhooks (07 §4.2)."""

    SIGNATURE = "signature"  # HMAC / RSA over the raw body (optionally with a timestamp)
    SECRET_HEADER = "secret_header"  # noqa: S105 - a static secret in a header — weak
    IP_ONLY = "ip_only"  # source address only — weak
    NONE = "none"  # nothing — weak

    @property
    def is_weak(self) -> bool:
        """Weak schemes force the core to re-read the status from the provider (``fetch_status``)."""
        return self is not WebhookAuth.SIGNATURE


class PaymentState(enum.StrEnum):
    """Provider-side state of an invoice as the plugin understood it."""

    CREATED = "created"
    PROCESSING = "processing"
    PAID = "paid"
    EXPIRED = "expired"
    CANCELED = "canceled"
    FAILED = "failed"
    CHARGEBACK = "chargeback"
    REFUNDED = "refunded"


@dataclass(frozen=True, slots=True, kw_only=True)
class Manifest:
    """Static description of a plugin."""

    slug: str
    title: str
    method_kinds: tuple[MethodKind, ...]
    currencies: tuple[str, ...]
    config: type[ConfigModel]
    docs_url: str
    min_minor: int | None = None
    max_minor: int | None = None
    sdk: str = SDK_VERSION

    def __post_init__(self) -> None:
        if not _SLUG_RE.fullmatch(self.slug):
            raise ValueError(f"invalid provider slug {self.slug!r} (a-z, 0-9, _; 2–32 chars)")
        if not self.title.strip():
            raise ValueError("manifest title must not be empty")
        if not self.method_kinds:
            raise ValueError("manifest needs at least one method kind")
        object.__setattr__(self, "method_kinds", tuple(MethodKind(k) for k in self.method_kinds))
        if not self.currencies or any(not _CURRENCY_RE.fullmatch(c) for c in self.currencies):
            raise ValueError("manifest currencies must be upper-case codes, e.g. ('RUB',)")
        if not (isinstance(self.config, type) and issubclass(self.config, ConfigModel)):
            raise TypeError("manifest config must be a ConfigModel subclass")
        for bound in (self.min_minor, self.max_minor):
            if bound is not None and (isinstance(bound, bool) or not isinstance(bound, int) or bound <= 0):
                raise ValueError("min_minor / max_minor must be positive integers")
        if self.min_minor is not None and self.max_minor is not None and self.min_minor > self.max_minor:
            raise ValueError("min_minor must not exceed max_minor")


@dataclass(frozen=True, slots=True, kw_only=True)
class Capabilities:
    """What a plugin can do. ``webhook=False`` means the provider never calls our URL (Telegram Stars,
    manual payments): payments are confirmed through the core API instead."""

    webhook_auth: WebhookAuth
    replay_window_s: int | None = None  # freshness window of event.signed_at; None = provider has no time
    fetch_status: bool = False
    batch_status: bool = False  # fetch_status accepts many ids in one request
    batch_limit: int = 100
    refund: bool = False
    recurring: bool = False
    receipt_54fz: bool = False
    in_chat_invoice: bool = False
    redirect: bool = True
    webhook: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "webhook_auth", WebhookAuth(self.webhook_auth))
        if self.replay_window_s is not None and not 1 <= self.replay_window_s <= 86_400:
            raise ValueError("replay_window_s must be within 1..86400")
        if self.batch_status and not self.fetch_status:
            raise ValueError("batch_status requires fetch_status")
        if not 1 <= self.batch_limit <= 1000:
            raise ValueError("batch_limit must be within 1..1000")


@dataclass(frozen=True, slots=True, kw_only=True)
class PaymentIntent:
    """What to charge. ``payment_id`` is the only identifier that may leave the bot."""

    payment_id: str
    amount_minor: int
    currency: str
    description: str
    customer_ref: str  # opaque, stable per (instance, user) hash — never a Telegram id
    return_url: str | None = None
    method_hint: MethodKind | None = None
    is_test: bool = False

    @property
    def amount(self) -> Decimal:
        """Amount in major units (``17950`` RUB → ``Decimal("179.50")``)."""
        from svbg.core.money import to_decimal

        return to_decimal(self.amount_minor, self.currency)

    def amount_text(self) -> str:
        """Amount with exactly the currency's decimals: ``"179.00"`` (``"100"`` for XTR)."""
        from svbg.core.money import exponent

        exp = exponent(self.currency)
        return f"{self.amount:.{exp}f}"


@dataclass(frozen=True, slots=True, kw_only=True)
class Checkout:
    """How the user pays: a link (``url``), a Telegram invoice (``invoice``) or bank details (``details``)."""

    kind: Literal["url", "invoice", "details"]
    external_id: str | None = None
    pay_url: str | None = None
    invoice: Mapping[str, Any] | None = None
    details: str | None = None
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.kind == "url" and not (self.pay_url and re.match(r"https?://", self.pay_url)):
            raise ValueError("url checkout needs an http(s) pay_url")
        if self.kind == "invoice" and not self.invoice:
            raise ValueError("invoice checkout needs invoice parameters")
        if self.kind == "details" and not (self.details and self.details.strip()):
            raise ValueError("details checkout needs details text")
        _check_id(self.external_id)
        if self.expires_at is not None and self.expires_at.tzinfo is None:
            raise ValueError("expires_at must be timezone-aware")

    def as_json(self) -> dict[str, Any]:
        """Plain data stored with the payment (``payments.checkout``)."""
        return {
            "kind": self.kind,
            "pay_url": self.pay_url,
            "invoice": dict(self.invoice) if self.invoice else None,
            "details": self.details,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }


def _check_id(value: str | None) -> None:
    if value is not None and (not isinstance(value, str) or not value or len(value) > _MAX_ID_LEN):
        raise ValueError(f"identifiers must be 1..{_MAX_ID_LEN} characters")


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderStatus:
    """A provider's report about one invoice (from ``fetch_status`` or inside a webhook).

    ``external_id`` and/or ``payment_id`` (our opaque id echoed back by the provider) identify it.
    """

    state: PaymentState
    external_id: str | None = None
    payment_id: str | None = None
    amount: Decimal | None = None
    currency: str | None = None
    paid_at: datetime | None = None
    is_test: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", PaymentState(self.state))
        _check_id(self.external_id)
        _check_id(self.payment_id)
        if self.external_id is None and self.payment_id is None:
            raise ValueError("a status needs external_id or payment_id")
        if self.amount is not None and not isinstance(self.amount, Decimal):
            raise TypeError("amount must be a Decimal (use parse_amount)")
        if self.currency is not None:
            object.__setattr__(self, "currency", self.currency.strip().upper())


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderEvent(ProviderStatus):
    """An authenticated webhook. ``signed_at`` is the time the provider signed (for the freshness window);
    ``summary`` holds a few safe fields for the event log (no personal data, no secrets)."""

    signed_at: datetime | None = None
    summary: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        ProviderStatus.__post_init__(self)
        if self.signed_at is not None and self.signed_at.tzinfo is None:
            raise ValueError("signed_at must be timezone-aware")
        object.__setattr__(self, "summary", MappingProxyType(dict(self.summary)))


class _Headers(Mapping[str, str]):
    """Case-insensitive read-only header mapping."""

    __slots__ = ("_items",)

    def __init__(self, items: Mapping[str, str] | Sequence[tuple[str, str]]) -> None:
        pairs = items.items() if isinstance(items, Mapping) else items
        self._items: dict[str, str] = {}
        for k, v in pairs:
            self._items.setdefault(k.lower(), v)

    def __getitem__(self, key: str) -> str:
        return self._items[key.lower()]

    def __iter__(self) -> Any:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and key.lower() in self._items


@dataclass(frozen=True, slots=True)
class WebhookRequest:
    """A raw incoming webhook (the plugin authenticates ``body`` exactly as received)."""

    body: bytes
    headers: Mapping[str, str] | Sequence[tuple[str, str]] = field(default_factory=dict)
    method: str = "POST"
    path: str = ""
    query: Mapping[str, str] = field(default_factory=dict)
    remote: str | None = None
    received_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not isinstance(self.headers, _Headers):
            object.__setattr__(self, "headers", _Headers(self.headers))

    def header(self, name: str) -> str | None:
        headers: Mapping[str, str] = self.headers  # type: ignore[assignment] - normalized in __post_init__
        return headers.get(name)

    def json(self) -> Any:
        """The body as JSON; a non-JSON body is rejected with ``400``."""
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise WebhookRejected("malformed", status=400) from None


@dataclass(frozen=True, slots=True)
class WebhookResponse:
    """What the core answers to the provider."""

    status: int = 200
    body: bytes = b"OK"
    content_type: str = "text/plain"

    @classmethod
    def ok(cls, text: str = "OK") -> WebhookResponse:
        return cls(200, text.encode("utf-8"), "text/plain")

    @classmethod
    def json(cls, data: Any, status: int = 200) -> WebhookResponse:
        return cls(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json")


class WebhookRejected(Exception):
    """The webhook is not authentic or unreadable. ``status`` is returned to the caller (401 by default)."""

    def __init__(self, reason: str, *, status: int = 401) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


class WebhookIgnored(Exception):
    """An authentic request without a payment state (a ping, a test call): answered with ``response``."""

    def __init__(self, reason: str = "ignored", response: WebhookResponse | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.response = response or WebhookResponse.ok()


class ProviderError(Exception):
    """A provider call failed. ``human`` is shown to the owner (Russian); ``retryable`` for transient ones."""

    def __init__(self, human: str, *, retryable: bool = False, status: int | None = None) -> None:
        super().__init__(human)
        self.human = human
        self.retryable = retryable
        self.status = status


@dataclass(frozen=True, slots=True)
class Probe:
    """Result of ``test_credentials``: ``message`` is shown to the owner (Russian)."""

    ok: bool
    message: str = ""
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RefundResult:
    ok: bool
    external_id: str | None = None
    message: str = ""


class PaymentProvider(abc.ABC):
    """Base class of a payment plugin. Subclasses set ``manifest`` and ``capabilities``."""

    manifest: ClassVar[Manifest]
    capabilities: ClassVar[Capabilities]

    def __init__(self, config: ConfigModel, ctx: PluginContext) -> None:
        if not isinstance(config, self.manifest.config):
            raise TypeError(f"{self.manifest.slug}: config must be {self.manifest.config.__name__}")
        self.config = config
        self.ctx = ctx

    @abc.abstractmethod
    async def create(self, intent: PaymentIntent) -> Checkout:
        """Create an invoice. Raise :class:`ProviderError` on failure."""

    async def parse_webhook(self, req: WebhookRequest) -> ProviderEvent:
        """Authenticate (raw bytes, constant time) and parse a webhook."""
        raise WebhookRejected("webhooks_not_supported", status=404)

    def ack(self, event: ProviderEvent | None) -> WebhookResponse:
        """Answer expected by the provider after the core accepted the event."""
        return WebhookResponse.ok()

    async def fetch_status(self, ids: Sequence[str]) -> list[ProviderStatus]:
        """Statuses of invoices by ``external_id`` (only if ``capabilities.fetch_status``). Unknown ids are
        simply absent from the result."""
        raise NotImplementedError

    async def refund(self, external_id: str, amount_minor: int, currency: str) -> RefundResult:
        """Refund (only if ``capabilities.refund``)."""
        raise NotImplementedError

    @abc.abstractmethod
    async def test_credentials(self) -> Probe:
        """Check the configuration against the provider without side effects (no invoice is created)."""


# ------------------------------------------------------------------------------------------------ helpers


def constant_time_equal(a: str | bytes | None, b: str | bytes | None) -> bool:
    """Constant-time comparison; ``None`` or empty values never match."""
    if not a or not b:
        return False
    left = a.encode("utf-8") if isinstance(a, str) else a
    right = b.encode("utf-8") if isinstance(b, str) else b
    return hmac.compare_digest(left, right)


def hmac_sha256_hex(secret: str | bytes, message: bytes) -> str:
    key = secret.encode("utf-8") if isinstance(secret, str) else secret
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def parse_amount(value: Any) -> Decimal:
    """A provider amount as an exact :class:`Decimal`: ``"179"``, ``"179.00"``, ``179``, ``179.0`` all
    give the same value. Floats are converted through ``str`` (no binary noise). ``ValueError`` otherwise."""
    if isinstance(value, bool) or value is None:
        raise ValueError("amount is missing")
    if isinstance(value, Decimal):
        dec = value
    elif isinstance(value, int | float):
        dec = Decimal(str(value))
    elif isinstance(value, str):
        text = (
            value.strip().replace(",", ".") if value.count(",") == 1 and "." not in value else value.strip()
        )
        try:
            dec = Decimal(text)
        except InvalidOperation:
            raise ValueError(f"not an amount: {value!r}") from None
    else:
        raise TypeError(f"not an amount: {value!r}")
    if not dec.is_finite() or dec < 0:
        raise ValueError(f"not an amount: {value!r}")
    return dec


_MS_THRESHOLD: Final = 10**11  # numbers above this are milliseconds (year 5138 in seconds)


def parse_timestamp(value: Any) -> datetime:
    """Provider time in any common form → aware UTC: Unix seconds or milliseconds (number or digits),
    ISO 8601 with or without a zone (no zone = UTC). ``ValueError`` otherwise."""
    if isinstance(value, bool) or value is None:
        raise ValueError("timestamp is missing")
    if isinstance(value, int | float | Decimal) or (
        isinstance(value, str) and re.fullmatch(r"\s*\d{1,16}(\.\d+)?\s*", value)
    ):
        number_value = float(value)
    elif isinstance(value, str):
        text = value.strip().replace("Z", "+00:00").replace("z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            raise ValueError(f"not a timestamp: {value!r}") from None
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    else:
        raise ValueError(f"not a timestamp: {value!r}")
    if number_value >= _MS_THRESHOLD:
        number_value /= 1000.0
    try:
        return datetime.fromtimestamp(number_value, tz=UTC)
    except (OverflowError, OSError, ValueError):
        raise ValueError(f"not a timestamp: {value!r}") from None
