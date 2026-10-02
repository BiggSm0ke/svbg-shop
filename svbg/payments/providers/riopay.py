"""RioPay — pay-in cash desk (SBP, cards) through RioPay's payment page (07 §4.2, §4.4).

Specification: ``docs/providers/riopay.md`` (official documentation <https://docs.riopay.online/ru/docs>
and the merchant OpenAPI <https://api.riopay.online/swagger-merchants> v2.1.1, checked on 2026-10-02).
Written from scratch from that specification. Imports only :mod:`svbg.sdk`.

* Every request carries ``X-Api-Token`` (``Authorization`` is not accepted by RioPay).
* ``PUT /v1/orders`` creates an order **idempotently** by ``externalId`` (= our opaque payment id): a repeat
  after a timeout returns the same order instead of a duplicate. ``externalUserId`` is never sent.
* ``GET /v1/orders/{id}`` reads one status (``fetch_status``; no batch API; ``404`` → unknown).
* Webhooks: ``X-Signature = hex(HMAC_SHA512(api_token, raw_body))`` compared in constant time over the raw
  bytes (never over re-serialized JSON); no time is signed, so there is no freshness window — replays are
  absorbed by the core's deduplication. ``X-Type`` other than ``ORDER_UPDATE`` is answered ``200`` unchanged.
* ``FAILED``/``CANCELED``/``EXPIRED``/``BLOCKED`` are not strictly final: a later ``COMPLETED`` is a late
  payment. ``REFUND`` → refunded, ``CHARGEBACK`` → chargeback. ``isTest`` (test terminal) → ``is_test``.
* Refunds: full only, ``POST /v1/refunds`` with a deterministic ``externalId`` (a repeat → ``409``, never a
  second refund).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping, Sequence
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
    RefundResult,
    WebhookAuth,
    WebhookIgnored,
    WebhookRejected,
    WebhookRequest,
    constant_time_equal,
    integer,
    parse_amount,
    parse_timestamp,
    secret,
    url,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "STATUS_MAP",
    "RioPay",
    "RioPayConfig",
    "amount_text",
    "sign",
]

DEFAULT_BASE_URL: Final = "https://api.riopay.online"
#: RioPay order status → SDK state (spec §3.5).
STATUS_MAP: Final[Mapping[str, PaymentState]] = {
    "CREATED": PaymentState.CREATED,
    "PENDING": PaymentState.CREATED,
    "COMPLETED": PaymentState.PAID,
    "FAILED": PaymentState.FAILED,
    "CANCELED": PaymentState.CANCELED,
    "CANCELLED": PaymentState.CANCELED,
    "EXPIRED": PaymentState.EXPIRED,
    "BLOCKED": PaymentState.FAILED,
    "REFUND": PaymentState.REFUNDED,
    "CHARGEBACK": PaymentState.CHARGEBACK,
}
ORDER_UPDATE: Final = "ORDER_UPDATE"
_PURPOSE_MAX: Final = 255
_REASON_MAX: Final = 1000
_URL_MAX: Final = 2048
_ID_MAX: Final = 200
_UUID_RE: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_SIG_RE: Final = re.compile(r"[0-9a-f]{128}")

_T: Final = {
    "bad_key": "RioPay отклонил API-токен — проверьте его (выдаёт менеджер RioPay)",
    "no_merchant": "RioPay не нашёл мерчанта для этого токена — обратитесь к менеджеру RioPay",
    "no_service": "у RioPay нет активного магазина или сервиса{detail} — проверьте «ID сервиса»",
    "rejected": "RioPay отклонил запрос (HTTP {status}){detail}",
    "duplicate": "RioPay: заказ с таким externalId уже есть{detail}",
    "unavailable": "RioPay временно недоступен (HTTP {status})",
    "bad_answer": "RioPay вернул непонятный ответ",
    "currency": "RioPay в боте принимает только рубли",
    "refund_off": "возвраты для этого способа оплаты не включены — обратитесь к менеджеру RioPay",
    "refund_conflict": "RioPay не принял возврат{detail}",
    "refund_failed": "RioPay отклонил возврат (статус FAILED)",
    "probe_ok": "Токен принят. Сервисы RioPay: {services}",
    "probe_no_services": "Токен принят, но у мерчанта нет ни одного сервиса — обратитесь к менеджеру RioPay",
    "probe_service": "сервис {sid} не найден среди сервисов мерчанта ({services}) — проверьте «ID сервиса»",
    "probe_not_riopay": "по адресу API отвечает не RioPay — проверьте «Адрес API»",
}


class RioPayConfig(ConfigModel):
    api_token = secret(
        "API-токен",
        "Токен мерчанта для запросов к API (заголовок X-Api-Token). Тем же токеном RioPay подписывает "
        "уведомления (HMAC-SHA512, заголовок X-Signature).",
        where="выдаёт менеджер RioPay вместе с мерчант-аккаунтом",
    )
    service_id = integer(
        "ID сервиса",
        "Сервис (терминал), через который создаются заказы. Пусто — сервис по умолчанию; для новых аккаунтов "
        "RioPay требует указывать его явно.",
        where="«Проверить ключи» в мастере покажет список сервисов (GET /v1/orders/services → serviceId); "
        "или спросите менеджера RioPay",
        required=False,
        min=1,
        advanced=True,
    )
    base_url = url(
        "Адрес API",
        "Базовый адрес API RioPay. Меняйте, только если RioPay сообщил другой.",
        default=DEFAULT_BASE_URL,
        required=False,
        advanced=True,
    )
    return_url = url(
        "Куда вернуть покупателя",
        "Страница после оплаты (successUrl и failUrl). Пусто — ссылка на бота, которую передаёт ядро.",
        where="обычно ссылка на вашего бота: https://t.me/<имя_бота>",
        required=False,
        advanced=True,
    )


def sign(api_token: str, body: bytes) -> str:
    """``hex(HMAC_SHA512(api_token, body))`` — the webhook signature over the raw body."""
    return hmac.new(api_token.encode("utf-8"), body, hashlib.sha512).hexdigest()


def amount_text(amount: Decimal) -> str:
    """Decimal text without exponent or trailing zeros: ``179.00`` → ``"179"``, ``179.50`` → ``"179.5"``."""
    return format(amount.normalize(), "f")


def _decimal_json(body: bytes) -> Any:
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


def _currency(value: Any) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z]{3,8}", value.strip()):
        return value.strip().upper()
    return None


def _detail(resp: HttpResponse) -> str:
    """A short message of RioPay's ``HttpErrorResponse`` (``: …``), never the request itself."""
    try:
        data = json.loads(resp.body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return ""
    if isinstance(data, dict):
        message = data.get("message")
        if isinstance(message, list):
            message = "; ".join(str(m) for m in message[:3])
        if isinstance(message, str) and message.strip():
            return ": " + message.strip()[:160]
    return ""


def _fits_url(value: str | None) -> bool:
    return bool(value) and len(str(value)) <= _URL_MAX and bool(re.match(r"https?://", str(value)))


class RioPay(PaymentProvider):
    """See module docstring."""

    manifest = Manifest(
        slug="riopay",
        title="RioPay",
        method_kinds=(MethodKind.SBP, MethodKind.CARD),
        currencies=("RUB",),
        config=RioPayConfig,
        docs_url="https://docs.riopay.online/ru/docs",
    )
    capabilities = Capabilities(
        webhook_auth=WebhookAuth.SIGNATURE,
        replay_window_s=None,  # nothing time-related is signed
        fetch_status=True,
        batch_status=False,
        refund=True,
        recurring=False,
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
            "X-Api-Token": self.config.api_token,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _raise_for(self, resp: HttpResponse, what: str) -> None:
        detail = _detail(resp)
        if resp.status == 401:
            raise ProviderError(_T["bad_key"], retryable=False, status=resp.status)
        if resp.status == 403:
            raise ProviderError(_T["no_merchant"], retryable=False, status=resp.status)
        if resp.status in (408, 429) or resp.status >= 500:
            raise ProviderError(
                _T["unavailable"].format(status=resp.status), retryable=True, status=resp.status
            )
        self.ctx.log.warning("riopay: %s answered HTTP %s", what, resp.status)
        if resp.status == 404:
            raise ProviderError(_T["no_service"].format(detail=detail), retryable=False, status=404)
        if resp.status == 409:
            raise ProviderError(_T["duplicate"].format(detail=detail), retryable=False, status=409)
        raise ProviderError(
            _T["rejected"].format(status=resp.status, detail=detail), retryable=False, status=resp.status
        )

    @staticmethod
    def _json(resp: HttpResponse) -> Any:
        try:
            return _decimal_json(resp.body)
        except (UnicodeDecodeError, ValueError):
            raise ProviderError(_T["bad_answer"], retryable=True, status=resp.status) from None

    @classmethod
    def _order(cls, resp: HttpResponse) -> dict[str, Any]:
        data = cls._json(resp)
        if isinstance(data, dict) and "id" not in data and isinstance(data.get("data"), dict):
            data = data["data"]  # tolerate a {"data": {...}} envelope
        if not isinstance(data, dict):
            raise ProviderError(_T["bad_answer"], retryable=True, status=resp.status)
        return data

    # ----------------------------------------------------------------------------------------- create

    async def create(self, intent: PaymentIntent) -> Checkout:
        if intent.currency != "RUB":
            raise ProviderError(_T["currency"], retryable=False)
        pid = intent.payment_id
        body: dict[str, Any] = {
            "amount": amount_text(intent.amount),
            "currency": intent.currency,
            "externalId": pid,  # opaque: never a Telegram id or a subscription link
            "purpose": (intent.description or "Оплата")[:_PURPOSE_MAX],
        }
        if self.config.service_id is not None:
            body["serviceId"] = int(self.config.service_id)
        back = self.config.return_url or intent.return_url
        if _fits_url(back):
            body["successUrl"] = back
            body["failUrl"] = back
        if _fits_url(self.ctx.webhook_url):
            body["callbackUrl"] = self.ctx.webhook_url
        resp = await self.ctx.http.request(
            "PUT", f"{self._base}/v1/orders", headers=self._headers(), json=body
        )
        if resp.status not in (200, 201):
            self._raise_for(resp, "create")
        data = self._order(resp)
        external = _external_id(data.get("id"))
        pay_url = data.get("paymentLink")
        echoed = data.get("externalId")
        if external is None or not isinstance(pay_url, str) or not re.match(r"https?://", pay_url):
            raise ProviderError(_T["bad_answer"], retryable=False, status=resp.status)
        if echoed is not None and str(echoed) != pid:
            self.ctx.log.warning("riopay: create answered an order of another externalId")
            raise ProviderError(_T["bad_answer"], retryable=False, status=resp.status)
        # RioPay returns no lifetime: the order expires on its side (EXPIRED), expires_at stays unset.
        return Checkout(kind="url", external_id=external, pay_url=pay_url)

    # ---------------------------------------------------------------------------------------- webhook

    async def parse_webhook(self, req: WebhookRequest) -> ProviderEvent:
        signature = (req.header("X-Signature") or "").strip().lower()
        if not _SIG_RE.fullmatch(signature):
            raise WebhookRejected("missing signature", status=403)
        if not constant_time_equal(sign(self.config.api_token, req.body), signature):
            raise WebhookRejected("bad signature", status=403)
        kind = (req.header("X-Type") or ORDER_UPDATE).strip().upper()
        if kind != ORDER_UPDATE:
            # REFUND_UPDATE / PAYOUT_UPDATE: authentic, but not an order state. A completed refund also
            # arrives as ORDER_UPDATE with status REFUND — that one moves the payment.
            self.ctx.log.info("riopay: %s webhook acknowledged without changes", kind[:40])
            raise WebhookIgnored(kind.lower())
        try:
            data = _decimal_json(req.body)
        except (UnicodeDecodeError, ValueError):
            raise WebhookRejected("malformed", status=400) from None
        if not isinstance(data, dict):
            raise WebhookRejected("malformed", status=400)
        raw_status = str(data.get("status") or "").strip().upper()
        state = STATUS_MAP.get(raw_status)
        if state is None:
            self.ctx.log.warning("riopay: webhook with unknown status %r ignored", raw_status[:40])
            raise WebhookIgnored("unknown status")
        status = self._status(data, state, malformed=WebhookRejected)
        return ProviderEvent(
            state=status.state,
            external_id=status.external_id,
            payment_id=status.payment_id,
            amount=status.amount,
            currency=status.currency,
            paid_at=status.paid_at,
            is_test=status.is_test,
            signed_at=None,
            summary={
                "id": status.external_id,
                "externalId": status.payment_id,
                "status": raw_status,
                "amount": None if status.amount is None else str(status.amount),
                "currency": status.currency,
                "paymentType": _short(data.get("paymentType")),
                "commission": _short(data.get("commission")),
                "received": _short(data.get("received")),
                "isTest": status.is_test,
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
        external = _external_id(data.get("id")) or external_id
        ours = _our_payment_id(data.get("externalId"))
        if external is None and ours is None:
            raise _malformed(malformed, "no order id")
        amount: Decimal | None = None
        if data.get("amount") not in (None, ""):
            try:
                amount = parse_amount(data["amount"])
            except (TypeError, ValueError):
                raise _malformed(malformed, "bad amount") from None
        currency: str | None = None
        if data.get("currency") not in (None, ""):
            currency = _currency(data.get("currency"))
            if currency is None:
                raise _malformed(malformed, "bad currency")
        paid_at = None
        if state is PaymentState.PAID and data.get("payedAt") not in (None, ""):
            try:
                paid_at = parse_timestamp(data["payedAt"])
            except ValueError:
                paid_at = None
        return ProviderStatus(
            state=state,
            external_id=external,
            payment_id=ours,
            amount=amount,
            currency=currency,
            paid_at=paid_at,
            is_test=data.get("isTest") is True,
        )

    # ----------------------------------------------------------------------------------------- status

    async def fetch_status(self, ids: Sequence[str]) -> list[ProviderStatus]:
        result: list[ProviderStatus] = []
        for external in dict.fromkeys(ids):
            if not external:
                continue
            resp = await self.ctx.http.request(
                "GET", f"{self._base}/v1/orders/{quote(external, safe='')}", headers=self._headers()
            )
            if resp.status == 404:
                continue  # unknown to RioPay: absent from the result
            if resp.status != 200:
                self._raise_for(resp, "status")
            data = self._order(resp)
            state = STATUS_MAP.get(str(data.get("status") or "").strip().upper())
            if state is None:
                continue
            try:
                result.append(self._status(data, state, external_id=external, malformed=ProviderError))
            except ProviderError:
                self.ctx.log.warning("riopay: unreadable status of %s skipped", external[:40])
        return result

    # ----------------------------------------------------------------------------------------- refund

    async def refund(self, external_id: str, amount_minor: int, currency: str) -> RefundResult:
        """Full refund only: ``POST /v1/refunds``. ``amount`` is sent so that RioPay refuses anything that is
        not exactly the order amount; ``externalId`` is derived from the order id, so a repeat can never
        create a second refund (``409``)."""
        from_minor = Decimal(amount_minor).scaleb(-2)
        body: dict[str, Any] = {
            "orderId": external_id,
            "externalId": f"refund-{external_id}"[:_ID_MAX],
            "amount": amount_text(from_minor),
            "reason": "Возврат по запросу магазина"[:_REASON_MAX],
        }
        if _fits_url(self.ctx.webhook_url):
            body["callbackUrl"] = self.ctx.webhook_url
        try:
            resp = await self.ctx.http.request(
                "POST", f"{self._base}/v1/refunds", headers=self._headers(), json=body
            )
        except ProviderError as exc:
            return RefundResult(False, None, exc.human)
        if resp.status not in (200, 201):
            detail = _detail(resp)
            if resp.status == 403:
                return RefundResult(False, None, _T["refund_off"])
            if resp.status in (404, 409):
                return RefundResult(False, None, _T["refund_conflict"].format(detail=detail))
            try:
                self._raise_for(resp, "refund")
            except ProviderError as exc:
                return RefundResult(False, None, exc.human)
        try:
            data = self._order(resp)
        except ProviderError as exc:
            return RefundResult(False, None, exc.human)
        ref = _external_id(data.get("id"))
        if str(data.get("status") or "").strip().upper() == "FAILED":
            return RefundResult(False, ref, _T["refund_failed"])
        return RefundResult(True, ref, str(data.get("status") or "")[:20])

    # ------------------------------------------------------------------------------------------ probe

    async def test_credentials(self) -> Probe:
        """No side effects: ``GET /v1/orders/services`` (401 → bad token, 403 → no merchant)."""
        resp = await self.ctx.http.request("GET", f"{self._base}/v1/orders/services", headers=self._headers())
        if resp.status == 401:
            return Probe(False, _T["bad_key"])
        if resp.status == 403:
            return Probe(False, _T["no_merchant"])
        if resp.status in (408, 429) or resp.status >= 500:
            return Probe(False, _T["unavailable"].format(status=resp.status))
        if resp.status != 200:
            return Probe(False, _T["probe_not_riopay"])
        try:
            data = _decimal_json(resp.body)
        except (UnicodeDecodeError, ValueError):
            return Probe(False, _T["probe_not_riopay"])
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return Probe(False, _T["probe_not_riopay"])
        services = [_service(item) for item in items if isinstance(item, Mapping)]
        services = [s for s in services if s["serviceId"] is not None]
        if not services:
            return Probe(False, _T["probe_no_services"])
        listing = ", ".join(
            f"{s['serviceId']} «{s['displayName']}»" + (" (по умолчанию)" if s["isDefault"] else "")
            for s in services
        )
        wanted = self.config.service_id
        if wanted is not None and all(s["serviceId"] != int(wanted) for s in services):
            return Probe(False, _T["probe_service"].format(sid=int(wanted), services=listing))
        return Probe(True, _T["probe_ok"].format(services=listing), {"services": services})


def _service(item: Mapping[str, Any]) -> dict[str, Any]:
    """Safe fields of one ``OrderServiceInfoData`` with RUB limits (``currencyLimits.RUB`` wins)."""
    sid = item.get("serviceId")
    limits = item.get("currencyLimits")
    rub = limits.get("RUB") if isinstance(limits, Mapping) else None
    rub = rub if isinstance(rub, Mapping) else {}

    def limit(key: str) -> str | None:
        value = rub.get(key, item.get(key))
        try:
            return None if value in (None, "") else str(parse_amount(value))
        except (TypeError, ValueError):
            return None

    return {
        "serviceId": sid if isinstance(sid, int) and not isinstance(sid, bool) else None,
        "displayName": str(item.get("displayName") or "")[:60],
        "isDefault": item.get("isDefault") is True,
        "min": limit("minOrderAmount"),
        "max": limit("maxOrderAmount"),
    }


def _short(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    return str(value)[:40]


def _malformed(kind: type[Exception], reason: str) -> Exception:
    if kind is WebhookRejected:
        return WebhookRejected(reason, status=400)
    return ProviderError(_T["bad_answer"], retryable=False)
