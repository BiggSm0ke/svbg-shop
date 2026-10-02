"""ParityPay — SBP and Russian cards through one cash desk (07 §4.2, §4.4).

Written from scratch from our specification ``docs/providers/paritypay.md`` (official documentation
<https://docs.paritypay.net/>, OpenAPI 2.1.0, checked on 2026-10-02). Imports only :mod:`svbg.sdk`.

* Every request carries ``X-ShopId`` (cash desk UUID) and ``X-SecretKey`` (secret key **No. 1**).
* ``POST /v2/invoice/create``: ``order_id`` is **only** the opaque payment id (unique per desk — it doubles as
  the idempotency key: ``422 "Order id is not unique"`` after a lost answer → the invoice is read back with
  ``GET /v2/invoice/status?order_id=…``); ``service`` = ``sbp`` / ``card`` when the user picked a method kind,
  otherwise the payer chooses on the form; ``callback_url`` = the instance webhook address.
* Webhook: ``X-SIGNATURE = hex(HMAC_SHA256(secret key No. 2, values of the top-level body fields sorted by
  key, concatenated without separators; null → ""))``. Values are taken from the **raw** JSON text (numbers as
  their literals, ``parse_float=str``), never through ``float`` — PHP and Python print floats differently.
  The signature has no time and the concatenation has no separators, so by default (``confirm_via_api``) a
  paid notification carries no currency: the core re-reads the invoice with ``fetch_status`` and checks the
  amount before crediting; a refund notification is confirmed inline by ``GET /v2/invoice/status``.
* ``fetch_status``: one id → ``GET /v2/invoice/status?id=…``; several → ``GET /v2/invoice/list`` (newest
  first, 100 per page, at most a few pages), leftovers beyond the scanned pages → single status requests.
* No test mode, no refund method, no published webhook IPs (spec §8–§9). Subscriptions (SBP only, need the
  manager's approval) are not used: subscription and payoff notifications are acknowledged and ignored.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

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
    flag,
    hmac_sha256_hex,
    integer,
    parse_amount,
    parse_timestamp,
    secret,
    text,
    url,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "SERVICES",
    "STATUS_MAP",
    "ParityPay",
    "ParityPayConfig",
    "signature_string",
    "webhook_signature",
]

DEFAULT_BASE_URL: Final = "https://api.paritypay.net"
#: SDK method kind → ParityPay ``service``.
SERVICES: Final[Mapping[MethodKind, str]] = {MethodKind.SBP: "sbp", MethodKind.CARD: "card"}
#: ParityPay ``InvoiceStatus`` → SDK state (spec §5.1).
STATUS_MAP: Final[Mapping[str, PaymentState]] = {
    "NEW": PaymentState.CREATED,
    "PAID": PaymentState.PAID,
    "EXPIRED": PaymentState.EXPIRED,
    "ERROR": PaymentState.FAILED,
    "REFUNDED": PaymentState.REFUNDED,
}
PER_PAGE: Final = 100
_MAX_LIST_PAGES: Final = 3
_MAX_SINGLE_LOOKUPS: Final = 10
_ID_MAX: Final = 200
_TEXT_MAX: Final = 255
_URL_MAX: Final = 500
_UUID: Final = r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
_UUID_RE: Final = re.compile(_UUID)
_AUTH_ERRORS: Final = ("shopid is incorrect", "secretkey is incorrect", "shop is inactive")
_NOT_UNIQUE: Final = "order id is not unique"

_T: Final = {
    "bad_key": "ParityPay отклонила UUID кассы или секретный ключ №1{detail} — проверьте их в настройках "
    "кассы",
    "rejected": "ParityPay отклонила запрос (HTTP {status}){detail}",
    "unavailable": "ParityPay временно недоступна (HTTP {status})",
    "bad_answer": "ParityPay вернула непонятный ответ",
    "currency": "ParityPay (рублёвая касса) принимает только рубли",
    "probe_ok": "UUID кассы и ключ №1 приняты, касса в рублях",
    "probe_currency": "касса ParityPay ведёт баланс в {cur}; плагин поддерживает только рублёвые кассы",
    "probe_not_pp": "по адресу API отвечает не ParityPay — проверьте «Адрес API»",
}


class ParityPayConfig(ConfigModel):
    shop_id = text(
        "UUID кассы (X-ShopId)",
        "Идентификатор кассы ParityPay — заголовок X-ShopId в запросах и поле shop_id в уведомлениях.",
        where="личный кабинет ParityPay → настройки кассы → UUID кассы",
        pattern=_UUID,
    )
    api_key = secret(
        "Секретный ключ №1",
        "Ключ доступа к API (заголовок X-SecretKey). Не путать с ключом №2.",
        where="личный кабинет ParityPay → настройки кассы → «Секретный ключ №1»",
    )
    webhook_key = secret(
        "Секретный ключ №2",
        "Им ParityPay подписывает HTTP-уведомления (заголовок X-SIGNATURE). Это другой ключ, не №1.",
        where="личный кабинет ParityPay → настройки кассы → «Секретный ключ №2»",
    )
    confirm_via_api = flag(
        "Перепроверять оплату через API",
        "Да (рекомендуется): подписанное уведомление только запускает проверку счёта через API ParityPay, "
        "деньги зачисляются по ответу API (в подписи нет времени). Нет: зачисление прямо по уведомлению.",
        default=True,
        advanced=True,
    )
    invoice_minutes = integer(
        "Срок жизни счёта, минут",
        "Сколько минут счёт можно оплатить (ParityPay: от 1 до 43200). Поздняя оплата всё равно зачтётся.",
        default=60,
        min=1,
        max=43_200,
        advanced=True,
    )
    base_url = url(
        "Адрес API",
        "Базовый адрес API ParityPay (без /v2). Меняйте, только если ParityPay сообщила другой.",
        default=DEFAULT_BASE_URL,
        required=False,
        advanced=True,
    )


# ------------------------------------------------------------------------------------------- signature


def _no_constants(name: str) -> Any:
    raise ValueError(f"non-JSON constant {name}")


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key {key[:20]!r}")
        result[key] = value
    return result


def _literal_json(body: bytes) -> Any:
    """JSON with every number kept as its source literal (``1209.01`` → ``"1209.01"``); duplicate keys and
    ``NaN``/``Infinity`` are errors (``ValueError``)."""
    return json.loads(
        body.decode("utf-8"),
        parse_float=str,
        parse_int=str,
        parse_constant=_no_constants,
        object_pairs_hook=_unique_pairs,
    )


def signature_string(fields: Mapping[str, Any]) -> str:
    """Values of the top-level fields sorted by key and joined without separators (spec §6.2);
    ``null`` → ``""``. Numbers must already be their JSON literals (strings). ``ValueError`` for booleans,
    objects and arrays: the signature of those is not defined by ParityPay."""
    parts: list[str] = []
    for key in sorted(fields):
        value = fields[key]
        if value is None:
            parts.append("")
        elif isinstance(value, str):
            parts.append(value)
        else:
            raise ValueError(f"field {key[:20]!r} has no defined signature form")
    return "".join(parts)


def webhook_signature(webhook_key: str, fields: Mapping[str, Any]) -> str:
    """``hex(HMAC_SHA256(key No. 2, signature_string(fields)))`` in lower case."""
    return hmac_sha256_hex(webhook_key, signature_string(fields).encode("utf-8"))


# --------------------------------------------------------------------------------------------- helpers


def _decimal_json(body: bytes) -> Any:
    """API answers: non-integer numbers as :class:`Decimal` (``ValueError`` when not JSON)."""
    return json.loads(body.decode("utf-8"), parse_float=Decimal)


def _text(value: Any, limit: int = _ID_MAX) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if 0 < len(value) <= limit else None


def _our_payment_id(value: Any) -> str | None:
    candidate = _text(value)
    if candidate is None or not _UUID_RE.fullmatch(candidate):
        return None
    return candidate.lower()


def _json_amount(amount: Decimal) -> int | float:
    """A JSON number for ParityPay (``number`` in its schema): integral amounts as integers."""
    return int(amount) if amount == amount.to_integral_value() else float(amount)


def _when(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        return parse_timestamp(value)
    except ValueError:
        return None


def _error_text(resp: HttpResponse) -> str:
    try:
        data = json.loads(resp.body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return ""
    if isinstance(data, dict):
        value = data.get("error") or data.get("message")
        if isinstance(value, str):
            return value.strip()[:120]
    return ""


def _looks_like_html(resp: HttpResponse) -> bool:
    ctype = str(resp.headers.get("Content-Type") or resp.headers.get("content-type") or "")
    return "html" in ctype.lower() or resp.body.lstrip()[:1] == b"<"


class ParityPay(PaymentProvider):
    """See module docstring."""

    manifest = Manifest(
        slug="paritypay",
        title="ParityPay",
        method_kinds=(MethodKind.SBP, MethodKind.CARD),
        currencies=("RUB",),
        config=ParityPayConfig,
        docs_url="https://docs.paritypay.net/",
    )
    capabilities = Capabilities(
        webhook_auth=WebhookAuth.SIGNATURE,
        replay_window_s=None,  # ParityPay signs no time
        fetch_status=True,
        batch_status=True,
        batch_limit=PER_PAGE,
        refund=False,  # no refund method in the API
        recurring=False,  # subscriptions need the manager's approval and are not implemented
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
            "X-ShopId": str(self.config.shop_id),
            "X-SecretKey": str(self.config.api_key),
            "Accept": "application/json",
        }

    async def _get(self, path: str, params: Mapping[str, str]) -> HttpResponse:
        return await self.ctx.http.request(
            "GET", f"{self._base}{path}", headers=self._headers(), params=dict(params)
        )

    def _error(self, resp: HttpResponse, what: str) -> ProviderError:
        detail = _error_text(resp)
        if resp.status in (401, 403) or detail.lower() in _AUTH_ERRORS:
            shown = f" ({detail})" if detail else ""
            return ProviderError(_T["bad_key"].format(detail=shown), retryable=False, status=resp.status)
        if resp.status in (408, 429) or resp.status >= 500:
            return ProviderError(
                _T["unavailable"].format(status=resp.status), retryable=True, status=resp.status
            )
        self.ctx.log.warning("paritypay: %s answered HTTP %s", what, resp.status)
        return ProviderError(
            _T["rejected"].format(status=resp.status, detail=f": {detail}" if detail else ""),
            retryable=False,
            status=resp.status,
        )

    @staticmethod
    def _object(resp: HttpResponse) -> dict[str, Any]:
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
        minutes = int(self.config.invoice_minutes)
        body: dict[str, Any] = {
            "order_id": intent.payment_id,  # opaque: never a Telegram id or a subscription link
            "amount": _json_amount(intent.amount),
            "comment": (intent.description or "Оплата")[:_TEXT_MAX],
            "expire": minutes,
        }
        service = SERVICES.get(intent.method_hint) if intent.method_hint else None
        if service is not None:
            body["service"] = service
        if intent.return_url and len(intent.return_url) <= _URL_MAX:
            body["success_url"] = intent.return_url
            body["fail_url"] = intent.return_url
        if self.ctx.webhook_url and len(self.ctx.webhook_url) <= _URL_MAX:
            body["callback_url"] = self.ctx.webhook_url
        resp = await self.ctx.http.request(
            "POST", f"{self._base}/v2/invoice/create", headers=self._headers(), json=body
        )
        if resp.status == 422 and _error_text(resp).lower() == _NOT_UNIQUE:
            data = await self._existing(intent)  # created before, the answer was lost
        elif resp.status != 200:
            raise self._error(resp, "create")
        else:
            data = self._object(resp)
        external = _text(data.get("id"))
        link = _text(data.get("link"), 2048)
        if external is None or link is None or not re.match(r"https?://", link):
            raise ProviderError(_T["bad_answer"], retryable=False, status=resp.status)
        # «expires» has no time zone (the desk's own): count the lifetime ourselves.
        expires_at = datetime.now(UTC) + timedelta(minutes=minutes)
        return Checkout(kind="url", external_id=external, pay_url=link, expires_at=expires_at)

    async def _existing(self, intent: PaymentIntent) -> dict[str, Any]:
        resp = await self._get("/v2/invoice/status", {"order_id": intent.payment_id})
        if resp.status != 200:
            raise self._error(resp, "status by order_id")
        data = self._object(resp)
        try:
            same_amount = parse_amount(data.get("amount")) == intent.amount
        except (TypeError, ValueError):
            same_amount = False
        if not same_amount or str(data.get("status") or "").upper() != "NEW":
            raise ProviderError(_T["rejected"].format(status=422, detail=": Order id is not unique"))
        return data

    # ---------------------------------------------------------------------------------------- webhook

    async def parse_webhook(self, req: WebhookRequest) -> ProviderEvent:
        given = (req.header("X-SIGNATURE") or "").strip().lower()
        if not given:
            raise WebhookRejected("missing signature", status=401)
        try:
            data = _literal_json(req.body)
        except (UnicodeDecodeError, ValueError):
            raise WebhookRejected("malformed", status=400) from None
        if not isinstance(data, dict) or not data:
            raise WebhookRejected("malformed", status=400)
        try:
            expected = webhook_signature(self.config.webhook_key, data)
        except ValueError:
            raise WebhookRejected("field without a defined signature form", status=401) from None
        if not constant_time_equal(expected, given):
            raise WebhookRejected("bad signature", status=401)
        if str(data.get("shop_id") or "").strip().lower() != str(self.config.shop_id).lower():
            raise WebhookRejected("another shop", status=401)
        if "shop_subscription_id" in data or "amount_to_payoff" in data:
            self.ctx.log.info("paritypay: subscription/payoff notification acknowledged and ignored")
            raise WebhookIgnored("not an invoice")
        raw_status = str(data.get("status") or "").strip().upper()
        state = STATUS_MAP.get(raw_status)
        if state is None:
            self.ctx.log.warning("paritypay: notification with unknown status %r ignored", raw_status[:40])
            raise WebhookIgnored("unknown status")
        external = _text(data.get("id"))
        if external is None:
            raise WebhookRejected("no invoice id", status=400)
        try:
            amount = parse_amount(data.get("amount"))
        except (TypeError, ValueError):
            raise WebhookRejected("bad amount", status=400) from None
        confirm = bool(self.config.confirm_via_api)
        if confirm and state is PaymentState.REFUNDED:
            await self._confirm_refund(external)
        return ProviderEvent(
            state=state,
            external_id=external,
            payment_id=_our_payment_id(data.get("order_id")),
            amount=amount,
            # no currency → the core re-reads the invoice (fetch_status) before crediting
            currency=None if confirm else "RUB",
            signed_at=None,
            summary={
                "id": external,
                "order_id": _text(data.get("order_id")),
                "status": raw_status,
                "amount": str(amount),
                "credited": _text(data.get("credited"), 40),
                "service": _text(data.get("service"), 20),
                "subscription_id": _text(data.get("subscription_id"), 64),
            },
        )

    async def _confirm_refund(self, external: str) -> None:
        try:
            statuses = await self._status_one(external)
        except ProviderError as exc:
            raise WebhookRejected(f"refund not confirmed yet: {exc.human}", status=503) from None
        if not statuses or statuses[0].state is not PaymentState.REFUNDED:
            self.ctx.log.warning(
                "paritypay: refund notification for %s not confirmed by the API", external[:40]
            )
            raise WebhookIgnored("refund not confirmed")

    # ----------------------------------------------------------------------------------------- status

    def _status(self, item: Mapping[str, Any]) -> ProviderStatus | None:
        state = STATUS_MAP.get(str(item.get("status") or "").strip().upper())
        external = _text(item.get("id"))
        if state is None or external is None:
            return None
        shop = item.get("shop_id")
        if shop is not None and str(shop).strip().lower() != str(self.config.shop_id).lower():
            return None
        try:
            amount = parse_amount(item.get("amount"))
        except (TypeError, ValueError):
            self.ctx.log.warning("paritypay: unreadable amount of %s skipped", external[:40])
            return None
        return ProviderStatus(
            state=state,
            external_id=external,
            payment_id=_our_payment_id(item.get("order_id")),
            amount=amount,
            currency="RUB",
            paid_at=_when(item.get("paid_at")),
        )

    async def _status_one(self, external: str) -> list[ProviderStatus]:
        resp = await self._get("/v2/invoice/status", {"id": external})
        if resp.status == 404:
            return []  # unknown to ParityPay
        if resp.status != 200:
            raise self._error(resp, "status")
        status = self._status(self._object(resp))
        return [status] if status is not None and status.external_id == external else []

    async def fetch_status(self, ids: Sequence[str]) -> list[ProviderStatus]:
        wanted = [i for i in dict.fromkeys(ids) if _text(i) == i]
        if not wanted:
            return []
        if len(wanted) == 1:
            return await self._status_one(wanted[0])
        found: dict[str, ProviderStatus] = {}
        missing = set(wanted)
        exhausted = False
        page = 1
        while missing and page <= _MAX_LIST_PAGES:
            resp = await self._get("/v2/invoice/list", {"page": str(page), "per_page": str(PER_PAGE)})
            if resp.status != 200:
                raise self._error(resp, "list")
            data = self._object(resp)
            items = data.get("invoices")
            if not isinstance(items, list):
                raise ProviderError(_T["bad_answer"], retryable=True, status=resp.status)
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                status = self._status(item)
                ext = status.external_id if status is not None else None
                if status is not None and ext is not None and ext in missing:
                    found[ext] = status
                    missing.discard(ext)
            meta = data.get("meta")
            last = meta.get("last_page") if isinstance(meta, Mapping) else None
            if not items or not isinstance(last, int) or isinstance(last, bool) or page >= last:
                exhausted = True  # every invoice of the desk was seen: the rest do not exist
                break
            page += 1
        if missing and not exhausted:
            for external in [i for i in wanted if i in missing][:_MAX_SINGLE_LOOKUPS]:
                for status in await self._status_one(external):
                    found[external] = status
        return [found[i] for i in wanted if i in found]

    # ------------------------------------------------------------------------------------------ probe

    async def test_credentials(self) -> Probe:
        """No side effects: ``GET /v2/shop/balance`` (checks the desk UUID and key No. 1, reads the desk
        currency). Key No. 2 can only be checked by a real notification."""
        resp = await self._get("/v2/shop/balance", {})
        if resp.status == 200 and not _looks_like_html(resp):
            try:
                data = self._object(resp)
            except ProviderError:
                return Probe(False, _T["probe_not_pp"])
            currency = str(data.get("currency") or "").strip().upper()
            if currency and currency != "RUB":
                return Probe(False, _T["probe_currency"].format(cur=currency[:8]))
            return Probe(True, _T["probe_ok"], {"currency": currency or None})
        if _looks_like_html(resp) or resp.status in (404, 405):
            return Probe(False, _T["probe_not_pp"])
        return Probe(False, self._error(resp, "balance").human)
