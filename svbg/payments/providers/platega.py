"""Platega — SBP, cards, international cards and crypto through one merchant account, wave B (07 §4.2, §4.4).

Specification: ``docs/providers/platega.md`` (official API documentation <https://docs.platega.io/>, checked
on 2026-10-02). Ported from Remnashop (MIT, © 2024 snoups) ``src/infrastructure/payment_gateways/platega.py``,
reworked for the SDK — see ``THIRD_PARTY_NOTICES.md``. Imports only :mod:`svbg.sdk`.

* Every request carries ``X-MerchantId`` and ``X-Secret``.
* ``POST /transaction/process`` with ``paymentMethod`` when the user picked a method kind (SBP → 2, card → 11,
  international card → 12, crypto → 13); ``POST /v2/transaction/process`` without one (Platega's own method
  page). ``payload`` carries **only** the opaque payment id; no ``metadata`` with user data is sent.
* ``GET /transaction/{id}`` reads one status (``fetch_status``; no batch API; ``404`` → unknown).
* Callbacks carry the **same static** ``X-MerchantId`` / ``X-Secret`` pair (no body signature, no time):
  ``WebhookAuth.SECRET_HEADER`` is weak, so the core never changes a payment from a callback alone — it
  re-reads the transaction with ``fetch_status`` (``payments.verify``). Remnashop credited straight
  from the callback.
* Amounts are read from JSON as :class:`~decimal.Decimal` (``parse_float=Decimal``), never through ``float``.
"""

from __future__ import annotations

import json
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
    constant_time_equal,
    parse_amount,
    secret,
    text,
    url,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "METHOD_CODES",
    "STATUS_MAP",
    "Platega",
    "PlategaConfig",
    "parse_expires_in",
]

DEFAULT_BASE_URL: Final = "https://app.platega.io"
#: SDK method kind → Platega ``paymentMethod`` (``PaymentMethodInt``).
METHOD_CODES: Final[Mapping[MethodKind, int]] = {
    MethodKind.SBP: 2,
    MethodKind.CARD: 11,
    MethodKind.INTL_CARD: 12,
    MethodKind.CRYPTO: 13,
}
#: Platega ``PaymentStatus`` → SDK state.
STATUS_MAP: Final[Mapping[str, PaymentState]] = {
    "PENDING": PaymentState.CREATED,
    "CONFIRMED": PaymentState.PAID,
    "CANCELED": PaymentState.CANCELED,
    "CANCELLED": PaymentState.CANCELED,
    "CHARGEBACKED": PaymentState.CHARGEBACK,
}
_DESCRIPTION_MAX: Final = 255
_ID_MAX: Final = 200
_UUID_RE: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_EXPIRES_RE: Final = re.compile(r"(?:(\d+)\.)?(\d{1,3}):(\d{2}):(\d{2})(?:\.\d+)?")
_FALLBACK_RETURN: Final = "https://t.me"

_T: Final = {
    "bad_key": "Platega отклонила MerchantId или секрет — проверьте их в кабинете Platega",
    "rejected": "Platega отклонила запрос (HTTP {status}){detail}",
    "unavailable": "Platega временно недоступна (HTTP {status})",
    "bad_answer": "Platega вернула непонятный ответ",
    "currency": "Platega принимает только рубли",
    "probe_ok": "MerchantId и секрет приняты, Platega доступна",
    "probe_not_platega": "по адресу API отвечает не Platega — проверьте «Адрес API»",
}


class PlategaConfig(ConfigModel):
    merchant_id = text(
        "MerchantId",
        "Идентификатор мерчанта (UUID) — заголовок X-MerchantId в запросах и в уведомлениях Platega.",
        where="кабинет Platega → «Настройки» → MerchantId (выдаёт менеджер при подключении)",
        pattern=r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}",
    )
    api_secret = secret(
        "API-ключ (X-Secret)",
        "Секрет для запросов к API; тем же ключом Platega подписывает свои уведомления (заголовок X-Secret).",
        where="кабинет Platega → «Настройки» → API-ключ (X-Secret)",
    )
    base_url = url(
        "Адрес API",
        "Базовый адрес API Platega. Меняйте, только если Platega сообщила другой.",
        default=DEFAULT_BASE_URL,
        required=False,
        advanced=True,
    )
    return_url = url(
        "Куда вернуть покупателя",
        "Страница после оплаты (обязательна для Platega). Пусто — ссылка на бота, которую передаёт ядро.",
        where="обычно ссылка на вашего бота: https://t.me/<имя_бота>",
        required=False,
        advanced=True,
    )


def parse_expires_in(value: Any) -> timedelta | None:
    """Platega's ``expiresIn`` (``"00:15:00"``, ``"1.00:00:00"``) as a duration; ``None`` when unreadable."""
    if not isinstance(value, str):
        return None
    match = _EXPIRES_RE.fullmatch(value.strip())
    if match is None:
        return None
    days, hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
    if minutes > 59 or seconds > 59:
        return None
    delta = timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)
    return delta if delta > timedelta(0) else None


def _decimal_json(body: bytes) -> Any:
    """JSON with every non-integer number as :class:`Decimal` (``ValueError`` when not JSON)."""
    return json.loads(body.decode("utf-8"), parse_float=Decimal)


def _our_payment_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower()
    return candidate if _UUID_RE.fullmatch(candidate) else None


def _external_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if 0 < len(value) <= _ID_MAX else None


def _json_amount(amount: Decimal) -> int | float:
    """A JSON number for Platega (``number`` in their schema): integral amounts as integers."""
    return int(amount) if amount == amount.to_integral_value() else float(amount)


class Platega(PaymentProvider):
    """See module docstring."""

    manifest = Manifest(
        slug="platega",
        title="Platega",
        method_kinds=(MethodKind.SBP, MethodKind.CARD, MethodKind.INTL_CARD, MethodKind.CRYPTO),
        currencies=("RUB",),
        config=PlategaConfig,
        docs_url="https://docs.platega.io/",
    )
    capabilities = Capabilities(
        webhook_auth=WebhookAuth.SECRET_HEADER,
        replay_window_s=None,
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
            "X-MerchantId": self.config.merchant_id,
            "X-Secret": self.config.api_secret,
            "Accept": "application/json",
        }

    def _raise_for(self, resp: HttpResponse, what: str) -> None:
        if resp.status in (401, 403):
            raise ProviderError(_T["bad_key"], retryable=False, status=resp.status)
        if resp.status in (408, 429) or resp.status >= 500:
            raise ProviderError(
                _T["unavailable"].format(status=resp.status), retryable=True, status=resp.status
            )
        self.ctx.log.warning("platega: %s answered HTTP %s", what, resp.status)
        raise ProviderError(
            _T["rejected"].format(status=resp.status, detail=_detail(resp)),
            retryable=False,
            status=resp.status,
        )

    @staticmethod
    def _json(resp: HttpResponse) -> dict[str, Any]:
        try:
            data = _decimal_json(resp.body)
        except (UnicodeDecodeError, ValueError):
            raise ProviderError(_T["bad_answer"], retryable=True, status=resp.status) from None
        if not isinstance(data, dict):
            raise ProviderError(_T["bad_answer"], retryable=True, status=resp.status)
        return data

    # ----------------------------------------------------------------------------------------- create

    async def create(self, intent: PaymentIntent) -> Checkout:
        if intent.currency != "RUB":
            raise ProviderError(_T["currency"], retryable=False)
        pid = intent.payment_id
        back = self.config.return_url or intent.return_url or _FALLBACK_RETURN
        body: dict[str, Any] = {
            "paymentDetails": {"amount": _json_amount(intent.amount), "currency": intent.currency},
            "description": (intent.description or "Оплата")[:_DESCRIPTION_MAX],
            "return": back,
            "failedUrl": back,
            "payload": pid,  # opaque: never a Telegram id or a subscription link
        }
        method = METHOD_CODES.get(intent.method_hint) if intent.method_hint else None
        if method is not None:
            body["paymentMethod"] = method
            path = "/transaction/process"
        else:
            path = "/v2/transaction/process"
        resp = await self.ctx.http.request("POST", f"{self._base}{path}", headers=self._headers(), json=body)
        if resp.status not in (200, 201):
            self._raise_for(resp, "create")
        data = self._json(resp)
        external = _external_id(data.get("transactionId"))
        pay_url = data.get("url") or data.get("redirect")  # v2 answers «url», v1 «redirect»
        if external is None or not isinstance(pay_url, str) or not re.match(r"https?://", pay_url):
            raise ProviderError(_T["bad_answer"], retryable=False, status=resp.status)
        ttl = parse_expires_in(data.get("expiresIn"))
        expires_at = datetime.now(UTC) + ttl if ttl else None
        return Checkout(kind="url", external_id=external, pay_url=pay_url, expires_at=expires_at)

    # ---------------------------------------------------------------------------------------- webhook

    async def parse_webhook(self, req: WebhookRequest) -> ProviderEvent:
        merchant = (req.header("X-MerchantId") or "").strip()
        given = (req.header("X-Secret") or "").strip()
        if not merchant or not given:
            raise WebhookRejected("missing credentials", status=401)
        # Both compared in constant time; the merchant id is case-insensitive (a UUID).
        merchant_ok = constant_time_equal(merchant.lower(), str(self.config.merchant_id).lower())
        secret_ok = constant_time_equal(given, self.config.api_secret)
        if not (merchant_ok and secret_ok):
            raise WebhookRejected("bad credentials", status=401)
        try:
            data = _decimal_json(req.body)
        except (UnicodeDecodeError, ValueError):
            raise WebhookRejected("malformed", status=400) from None
        if not isinstance(data, dict):
            raise WebhookRejected("malformed", status=400)
        raw_status = str(data.get("status") or "").strip().upper()
        state = STATUS_MAP.get(raw_status)
        if state is None:
            self.ctx.log.warning("platega: callback with unknown status %r ignored", raw_status[:40])
            raise WebhookIgnored("unknown status")
        status = self._status(data, state, malformed=WebhookRejected)
        return ProviderEvent(
            state=status.state,
            external_id=status.external_id,
            payment_id=status.payment_id,
            amount=status.amount,
            currency=status.currency,
            signed_at=None,
            summary={
                "id": status.external_id,
                "payload": status.payment_id,
                "status": raw_status,
                "amount": None if status.amount is None else str(status.amount),
                "currency": status.currency,
                "paymentMethod": _method_name(data.get("paymentMethod")),
            },
        )

    def _status(
        self,
        data: Mapping[str, Any],
        state: PaymentState,
        *,
        external_id: str | None = None,
        malformed: type[Exception],
    ) -> ProviderStatus:
        external = _external_id(data.get("id")) or _external_id(data.get("transactionId")) or external_id
        ours = _our_payment_id(data.get("payload"))
        if external is None and ours is None:
            raise _malformed(malformed, "no transaction id")
        details = data.get("paymentDetails")
        source: Mapping[str, Any] = details if isinstance(details, Mapping) else data
        amount: Decimal | None = None
        if source.get("amount") not in (None, ""):
            try:
                amount = parse_amount(source["amount"])
            except (TypeError, ValueError):
                raise _malformed(malformed, "bad amount") from None
        currency = source.get("currency") or data.get("currency")
        if currency is not None and (
            not isinstance(currency, str) or not re.fullmatch(r"[A-Za-z]{3,8}", currency.strip())
        ):
            raise _malformed(malformed, "bad currency")
        return ProviderStatus(
            state=state,
            external_id=external,
            payment_id=ours,
            amount=amount,
            currency=currency.strip().upper() if isinstance(currency, str) else None,
        )

    # ----------------------------------------------------------------------------------------- status

    async def fetch_status(self, ids: Sequence[str]) -> list[ProviderStatus]:
        result: list[ProviderStatus] = []
        for external in dict.fromkeys(ids):
            if not external:
                continue
            resp = await self.ctx.http.request(
                "GET", f"{self._base}/transaction/{quote(external, safe='')}", headers=self._headers()
            )
            if resp.status == 404:
                continue  # unknown to Platega: absent from the result
            if resp.status != 200:
                self._raise_for(resp, "status")
            data = self._json(resp)
            state = STATUS_MAP.get(str(data.get("status") or "").strip().upper())
            if state is None:
                continue
            try:
                result.append(self._status(data, state, external_id=external, malformed=ProviderError))
            except ProviderError:
                self.ctx.log.warning("platega: unreadable status of %s skipped", external[:40])
        return result

    # ------------------------------------------------------------------------------------------ probe

    async def test_credentials(self) -> Probe:
        """No side effects: reads a missing transaction (401/403 → bad credentials; 404 → accepted)."""
        resp = await self.ctx.http.request(
            "GET", f"{self._base}/transaction/{uuid.uuid4()}", headers=self._headers()
        )
        if resp.status in (401, 403):
            return Probe(False, _T["bad_key"])
        if resp.status in (408, 429) or resp.status >= 500:
            return Probe(False, _T["unavailable"].format(status=resp.status))
        if resp.status in (200, 404, 400):
            ctype = str(resp.headers.get("Content-Type") or resp.headers.get("content-type") or "")
            if "html" in ctype.lower() or resp.body.lstrip()[:1] == b"<":
                return Probe(False, _T["probe_not_platega"])
            return Probe(True, _T["probe_ok"], {"status": resp.status})
        return Probe(False, _T["rejected"].format(status=resp.status, detail=""))


def _method_name(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    return str(value)[:20]


def _detail(resp: HttpResponse) -> str:
    """A short provider message for the owner (``: …``), never the request itself."""
    try:
        data = json.loads(resp.body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return ""
    if isinstance(data, dict):
        for key in ("message", "error", "title"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return ": " + value.strip()[:120]
    return ""


def _malformed(kind: type[Exception], reason: str) -> Exception:
    if kind is WebhookRejected:
        return WebhookRejected(reason, status=400)
    return ProviderError(_T["bad_answer"], retryable=False)
