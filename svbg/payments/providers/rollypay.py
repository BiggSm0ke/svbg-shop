"""RollyPay — the owner's main cash desk (SBP), wave A (07 §4.2, 05 §2.4.3).

Specification: ``docs/providers/rollypay.md`` (reconstructed from the owner's own service; to be checked
against the merchant cabinet documentation — see ``docs/providers/PROVENANCE.md``). Imports only
:mod:`svbg.sdk`.

* Requests carry ``X-API-Key`` and a fresh ``X-Nonce`` (UUIDv4) each.
* ``POST /payments`` creates an invoice; ``order_id``, ``customer_id`` and ``metadata`` carry **only** the
  opaque payment id — never a Telegram id or a subscription link.
* ``GET /payments/{payment_id}`` reads one status (``fetch_status``; no batch API).
* Webhooks: ``X-Signature = hex(HMAC_SHA256(signing_secret, X-Timestamp + "." + raw_body))`` compared in
  constant time; ``X-Timestamp`` (seconds, milliseconds or ISO 8601) becomes ``signed_at`` and the core
  rejects **every** webhook outside ±300 s; ``X-Test-Mode: true`` (or ``"test": true``) marks a test event,
  which the core rejects on a live instance.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final
from urllib.parse import quote

from svbg.sdk import (
    Capabilities,
    Checkout,
    ConfigModel,
    HttpResponse,
    Manifest,
    MethodKind,
    PaymentIntent,
    PaymentProvider,
    PaymentState,
    Probe,
    ProviderError,
    ProviderEvent,
    ProviderStatus,
    WebhookAuth,
    WebhookIgnored,
    WebhookRejected,
    WebhookRequest,
    WebhookResponse,
    constant_time_equal,
    hmac_sha256_hex,
    parse_amount,
    parse_timestamp,
    secret,
    text,
    url,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "REPLAY_WINDOW_S",
    "STATUS_MAP",
    "RollyPay",
    "RollyPayConfig",
    "paid_at_from",
    "sign",
]

DEFAULT_BASE_URL: Final = "https://api.rollypay.io/api/v1"
REPLAY_WINDOW_S: Final = 300
#: Provider status → SDK state. ``cancelled`` and ``failed`` are tolerated spellings.
STATUS_MAP: Final[Mapping[str, PaymentState]] = {
    "created": PaymentState.CREATED,
    "processing": PaymentState.PROCESSING,
    "paid": PaymentState.PAID,
    "expired": PaymentState.EXPIRED,
    "canceled": PaymentState.CANCELED,
    "cancelled": PaymentState.CANCELED,
    "failed": PaymentState.FAILED,
    "chargeback": PaymentState.CHARGEBACK,
    "refunded": PaymentState.REFUNDED,
}
#: How far ``paid_at`` from a status answer may lie from now (owner's heuristic, 05 §2.4.3).
PAID_AT_SKEW: Final = timedelta(seconds=300)
PAID_AT_MAX_AGE: Final = timedelta(days=8)
_DESCRIPTION_MAX: Final = 255
_ID_MAX: Final = 200
_UUID_RE: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_TRUE: Final = frozenset({"1", "true", "yes", "on"})
_ISO_ZONE_RE: Final = re.compile(r"(Z|z|[+-]\d{2}:?\d{2})$")

_T: Final = {
    "bad_key": "касса отклонила API-ключ — проверьте его в кабинете RollyPay",
    "rejected": "касса отклонила запрос (HTTP {status})",
    "unavailable": "касса временно недоступна (HTTP {status})",
    "bad_answer": "касса вернула непонятный ответ",
    "probe_ok": "Ключ принят, касса доступна",
    "probe_not_rollypay": "по адресу API отвечает не RollyPay — проверьте «Адрес API»",
}


class RollyPayConfig(ConfigModel):
    api_key = secret(
        "API-ключ",
        "Ключ магазина для запросов к API RollyPay (заголовок X-API-Key).",
        where="кабинет RollyPay → раздел API кассы → «API-ключ»",
    )
    signing_secret = secret(
        "Секрет подписи вебхуков",
        "Им RollyPay подписывает уведомления об оплате (заголовок X-Signature).",
        where="кабинет RollyPay → раздел API кассы → вебхуки → «Секрет подписи»",
    )
    base_url = url(
        "Адрес API",
        "Базовый адрес API RollyPay. Меняйте, только если касса сообщила другой.",
        default=DEFAULT_BASE_URL,
        required=False,
        advanced=True,
    )
    payment_method = text(
        "Способ оплаты на стороне кассы",
        "Передаётся кассе как payment_method (например, sbp). Пусто — способ выбирает покупатель на "
        "странице RollyPay.",
        where="кабинет RollyPay → способы оплаты",
        required=False,
        advanced=True,
        pattern=r"[A-Za-z0-9_.-]{1,64}",
    )


def sign(signing_secret: str, timestamp: str, body: bytes) -> str:
    """``hex(HMAC_SHA256(signing_secret, timestamp + "." + body))``."""
    return hmac_sha256_hex(signing_secret, timestamp.encode("utf-8") + b"." + body)


def _our_payment_id(value: Any) -> str | None:
    """Our opaque payment id when ``value`` looks like one (a UUID); legacy ids (``tc_…``) → ``None``."""
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower()
    return candidate if _UUID_RE.fullmatch(candidate) else None


def _external_id(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if 0 < len(value) <= _ID_MAX else None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    return isinstance(value, str) and value.strip().lower() in _TRUE


def paid_at_from(
    data: Mapping[str, Any], now: datetime, created_at: datetime | None = None
) -> datetime | None:
    """Payment time from a RollyPay answer, or ``None`` when it cannot be trusted (owner's heuristic).

    ``paid_at`` (then ``updated_at``) may be Unix seconds, milliseconds or ISO 8601. An ISO value **without a
    zone** is dropped (MSK or UTC cannot be told apart), and the result must lie within ``[created_at (or
    now − 8 days) − 5 min, now + 5 min]``.
    """
    low = (created_at or now - PAID_AT_MAX_AGE) - PAID_AT_SKEW
    high = now + PAID_AT_SKEW
    for key in ("paid_at", "updated_at"):
        raw = data.get(key)
        if raw is None or isinstance(raw, bool) or raw == "":
            continue
        if (
            isinstance(raw, str)
            and not re.fullmatch(r"\s*\d+(\.\d+)?\s*", raw)
            and not _ISO_ZONE_RE.search(raw.strip())
        ):
            continue  # ISO without a zone: MSK or UTC cannot be told apart
        try:
            value = parse_timestamp(raw)
        except ValueError:
            continue
        if low <= value <= high:
            return value
    return None


class RollyPay(PaymentProvider):
    """See module docstring."""

    manifest = Manifest(
        slug="rollypay",
        title="RollyPay",
        method_kinds=(MethodKind.SBP,),
        currencies=("RUB",),
        config=RollyPayConfig,
        docs_url="https://docs.rollypay.io",
        min_minor=17_900,
    )
    capabilities = Capabilities(
        webhook_auth=WebhookAuth.SIGNATURE,
        replay_window_s=REPLAY_WINDOW_S,
        fetch_status=True,
        batch_status=False,
        refund=False,
        in_chat_invoice=False,
        redirect=True,
        webhook=True,
    )

    # ------------------------------------------------------------------------------------------- HTTP

    @property
    def _base(self) -> str:
        return str(self.config.base_url or DEFAULT_BASE_URL).rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {
            "X-API-Key": self.config.api_key,
            "X-Nonce": str(uuid.uuid4()),  # replay protection of our requests: unique per call
            "Accept": "application/json",
        }

    def _raise_for(self, resp: HttpResponse, what: str) -> None:
        if resp.status in (401, 403):
            raise ProviderError(_T["bad_key"], retryable=False, status=resp.status)
        if resp.status == 429 or resp.status >= 500:
            raise ProviderError(
                _T["unavailable"].format(status=resp.status), retryable=True, status=resp.status
            )
        self.ctx.log.warning("rollypay: %s answered HTTP %s", what, resp.status)
        raise ProviderError(_T["rejected"].format(status=resp.status), retryable=False, status=resp.status)

    @staticmethod
    def _json(resp: HttpResponse) -> dict[str, Any]:
        try:
            data = resp.json()
        except ValueError:
            raise ProviderError(_T["bad_answer"], retryable=True, status=resp.status) from None
        if not isinstance(data, dict):
            raise ProviderError(_T["bad_answer"], retryable=True, status=resp.status)
        return data

    # ----------------------------------------------------------------------------------------- create

    async def create(self, intent: PaymentIntent) -> Checkout:
        pid = intent.payment_id
        body: dict[str, Any] = {
            "amount": intent.amount_text(),
            "payment_currency": intent.currency,
            "order_id": pid,
            "description": (intent.description or "Оплата")[:_DESCRIPTION_MAX],
            "customer_id": pid,  # opaque: never a Telegram id or a subscription link (05 §2.4.3)
            "metadata": {"payment_id": pid},
        }
        if intent.return_url:
            body["success_redirect_url"] = intent.return_url
            body["fail_redirect_url"] = intent.return_url
        if self.config.payment_method:
            body["payment_method"] = self.config.payment_method
        if intent.is_test or self.ctx.is_test:
            body["test"] = True
        resp = await self.ctx.http.request(
            "POST", f"{self._base}/payments", headers=self._headers(), json=body
        )
        if resp.status not in (200, 201):
            self._raise_for(resp, "create")
        data = self._json(resp)
        external = _external_id(data.get("payment_id"))
        pay_url = data.get("pay_url")
        if external is None or not isinstance(pay_url, str) or not re.match(r"https?://", pay_url):
            raise ProviderError(_T["bad_answer"], retryable=False, status=resp.status)
        expires_at: datetime | None = None
        if data.get("expires_at") not in (None, ""):
            try:
                expires_at = parse_timestamp(data["expires_at"])
            except ValueError:
                expires_at = None
        return Checkout(kind="url", external_id=external, pay_url=pay_url, expires_at=expires_at)

    # ---------------------------------------------------------------------------------------- webhook

    async def parse_webhook(self, req: WebhookRequest) -> ProviderEvent:
        signature = (req.header("X-Signature") or "").strip().lower()
        stamp = (req.header("X-Timestamp") or "").strip()
        if not signature or not stamp:
            raise WebhookRejected("missing signature", status=401)
        if not constant_time_equal(sign(self.config.signing_secret, stamp, req.body), signature):
            raise WebhookRejected("bad signature", status=401)
        try:
            signed_at = parse_timestamp(stamp)
        except ValueError:
            raise WebhookRejected("bad timestamp", status=401) from None
        data = req.json()
        if not isinstance(data, dict):
            raise WebhookRejected("malformed", status=400)
        raw_status = str(data.get("status") or "").strip().lower()
        state = STATUS_MAP.get(raw_status)
        if state is None:
            # Authentic, but nothing we understand: answer 200 so the desk stops retrying.
            self.ctx.log.warning("rollypay: webhook with unknown status %r ignored", raw_status[:40])
            raise WebhookIgnored("unknown status", WebhookResponse.json({"ok": True}))
        status = self._status(data, state, signed_at=signed_at)
        is_test = _truthy(req.header("X-Test-Mode")) or _truthy(data.get("test"))
        return ProviderEvent(
            state=status.state,
            external_id=status.external_id,
            payment_id=status.payment_id,
            amount=status.amount,
            currency=status.currency,
            paid_at=status.paid_at,
            is_test=is_test,
            signed_at=signed_at,
            summary={
                "payment_id": status.external_id,
                "order_id": status.payment_id,
                "status": raw_status,
                "amount": None if status.amount is None else str(status.amount),
                "currency": status.currency,
                "test": is_test,
            },
        )

    def _status(
        self,
        data: Mapping[str, Any],
        state: PaymentState,
        *,
        external_id: str | None = None,
        signed_at: datetime | None = None,
        malformed: type[Exception] = WebhookRejected,
    ) -> ProviderStatus:
        external = _external_id(data.get("payment_id")) or external_id
        metadata = data.get("metadata")
        ours = _our_payment_id(data.get("order_id"))
        if ours is None and isinstance(metadata, Mapping):
            ours = _our_payment_id(metadata.get("payment_id"))
        if external is None and ours is None:
            raise _malformed(malformed, "no payment id")
        amount: Decimal | None = None
        if data.get("amount") not in (None, ""):
            try:
                amount = parse_amount(data["amount"])
            except (TypeError, ValueError):
                raise _malformed(malformed, "bad amount") from None
        currency = data.get("currency") or data.get("payment_currency") or "RUB"
        if not isinstance(currency, str) or not re.fullmatch(r"[A-Za-z]{3,8}", currency.strip()):
            raise _malformed(malformed, "bad currency")
        now = signed_at or datetime.now(UTC)
        return ProviderStatus(
            state=state,
            external_id=external,
            payment_id=ours,
            amount=amount,
            currency=currency.strip().upper(),
            paid_at=paid_at_from(data, now) if state is PaymentState.PAID else None,
            # A test invoice says so in its status answer too: the core then never credits it on a live
            # instance (the owner may have switched test mode off after creating it).
            is_test=_truthy(data.get("test")),
        )

    def ack(self, event: ProviderEvent | None) -> WebhookResponse:
        return WebhookResponse.json({"ok": True})

    # ----------------------------------------------------------------------------------------- status

    async def fetch_status(self, ids: Sequence[str]) -> list[ProviderStatus]:
        result: list[ProviderStatus] = []
        for external in dict.fromkeys(ids):
            if not external:
                continue
            resp = await self.ctx.http.request(
                "GET", f"{self._base}/payments/{quote(external, safe='')}", headers=self._headers()
            )
            if resp.status == 404:
                continue  # unknown to the desk: absent from the result
            if resp.status != 200:
                self._raise_for(resp, "status")
            data = self._json(resp)
            state = STATUS_MAP.get(str(data.get("status") or "").strip().lower())
            if state is None:
                continue
            try:
                result.append(self._status(data, state, external_id=external, malformed=ProviderError))
            except ProviderError:
                self.ctx.log.warning("rollypay: unreadable status of %s skipped", external[:40])
        return result

    # ------------------------------------------------------------------------------------------ probe

    async def test_credentials(self) -> Probe:
        """No side effects: reads a payment that cannot exist. 401/403 → bad key; JSON 404 → key accepted."""
        probe_id = f"svbg-probe-{uuid.uuid4().hex}"
        resp = await self.ctx.http.request(
            "GET", f"{self._base}/payments/{probe_id}", headers=self._headers()
        )
        if resp.status in (401, 403):
            return Probe(False, _T["bad_key"])
        if resp.status == 429 or resp.status >= 500:
            return Probe(False, _T["unavailable"].format(status=resp.status))
        if resp.status == 200:
            return Probe(True, _T["probe_ok"])
        try:
            resp.json()
        except ValueError:
            return Probe(False, _T["probe_not_rollypay"])
        return Probe(True, _T["probe_ok"], {"status": resp.status})


def _malformed(kind: type[Exception], reason: str) -> Exception:
    if kind is WebhookRejected:
        return WebhookRejected(reason, status=400)
    return ProviderError(_T["bad_answer"], retryable=False)
