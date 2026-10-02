"""CloudPayments — cards and SBP through the CloudPayments payment page (orders) or an SBP link.

Written from scratch from the specification ``docs/providers/cloudpayments.md`` (official documentation
<https://developers.cloudpayments.ru/>, checked on 2026-10-02). Imports only :mod:`svbg.sdk`.

* API: ``https://api.cloudpayments.ru``, every method is ``POST`` with HTTP Basic (``Public ID:API Secret``);
  ``X-Request-ID`` makes a create idempotent for an hour (a retry never creates a second invoice).
* Checkout: ``orders/create`` → ``Model.Url`` (the CloudPayments payment page); the SBP button goes to
  ``payments/qr/sbp/link`` → ``Model.QrUrl`` (RUB only). ``InvoiceId`` carries **only** the opaque payment id
  and is also our ``external_id``: the invoice ``Id`` has no status method, the payment is found by
  ``InvoiceId`` (``payments/find``). ``AccountId`` is not sent (no saved cards, no subscriptions).
* Notifications (POST, UTF-8, CloudPayments format, URL-encoded form): ``Content-HMAC`` =
  base64(HMAC-SHA256(API Secret, raw body)); fallback ``X-Content-HMAC`` over the URL-decoded body. Both are
  compared in constant time; there is no signed time (``replay_window_s=None``), replays are stopped by the
  core's body deduplication. ``TestMode=1`` marks a test site. The answer is always ``{"code":0}``.
* Notification kinds (one URL for all kinds; the kind is recognized from the fields): Pay/Confirm
  (``Status=Completed`` with ``GatewayName``) → paid, ``Status=Authorized`` → processing (hold); Fail →
  logged without a state change (the buyer may pay again on the same page); Refund (``OperationType=Refund``)
  → refunded; Cancel (hold released) → canceled; Check (``?type=check`` in the URL, or no ``GatewayName``) and
  Recurrent are acknowledged without a state.
* Limits of the protocol: the core has no partial-refund state, so **any** Refund notification marks the
  payment refunded; chargebacks are only visible by polling ``chargebacks/list`` (no notification) and are not
  reported; minimum and maximum amounts are not published (they depend on the merchant's contract).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final
from urllib.parse import parse_qsl, unquote, unquote_plus

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
    WebhookResponse,
    choice,
    constant_time_equal,
    flag,
    parse_amount,
    parse_timestamp,
    secret,
    text,
    url,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "SENDER_NETWORKS",
    "TRANSACTION_STATUS",
    "CloudPayments",
    "CloudPaymentsConfig",
    "sign",
]

DEFAULT_BASE_URL: Final = "https://api.cloudpayments.ru"
#: Official notification sources (specification §5.2) — informational: authenticity comes from the HMAC.
SENDER_NETWORKS: Final = (
    "185.98.81.0/28",
    "87.251.91.160/27",
    "46.46.175.96/27",
    "46.46.168.160/27",
    "162.55.174.97/32",
    "91.216.178.243/32",
)
#: Transaction ``Status`` → SDK state. ``Declined`` is not terminal for an invoice (a new attempt is
#: possible), so it maps to «created».
TRANSACTION_STATUS: Final[Mapping[str, PaymentState]] = {
    "awaitingauthentication": PaymentState.PROCESSING,
    "authorized": PaymentState.PROCESSING,
    "completed": PaymentState.PAID,
    "cancelled": PaymentState.CANCELED,
    "declined": PaymentState.CREATED,
}
_CURRENCIES: Final = ("RUB", "USD", "EUR", "GBP")
_DESCRIPTION_MAX: Final = 250
_ID_MAX: Final = 200
_UUID_RE: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_ACK: Final = {"code": 0}
_PAID_AT_SKEW: Final = timedelta(minutes=5)
_PAID_AT_MAX_AGE: Final = timedelta(days=30)

_T: Final = {
    "bad_key": "CloudPayments отклонил Public ID или API Secret — проверьте их в личном кабинете",
    "rejected": "CloudPayments отклонил запрос: {error}",
    "unavailable": "CloudPayments временно недоступен (HTTP {status})",
    "bad_answer": "CloudPayments вернул непонятный ответ",
    "currency": "CloudPayments принимает здесь только RUB, USD, EUR и GBP",
    "probe_ok": "Ключи приняты, API CloudPayments доступен (тестовый или боевой режим сайта покажет первый "
    "платёж)",
    "refund_unknown": "CloudPayments не нашёл оплату этого счёта",
    "refund_not_paid": "счёт CloudPayments не оплачен — возвращать нечего",
    "refund_ok": "CloudPayments: возврат проведён",
}


class CloudPaymentsConfig(ConfigModel):
    public_id = text(
        "Public ID",
        "Идентификатор сайта (логин для API, начинается с pk_).",
        where="личный кабинет merchant.cloudpayments.ru → Сайты → настройки сайта → «Public ID»",
        pattern=r"pk_[A-Za-z0-9]{1,64}",
    )
    api_secret = secret(
        "API Secret",
        "Пароль для API. Им же CloudPayments подписывает уведомления (Content-HMAC).",
        where="личный кабинет merchant.cloudpayments.ru → Сайты → настройки сайта → «Пароль для API»",
    )
    sbp_link = flag(
        "СБП сразу в банк",
        "Кнопка «СБП» ведёт сразу на ссылку СБП (payments/qr/sbp/link), минуя страницу выбора способа. "
        "Выключите, если СБП не подключена у вашего сайта в CloudPayments.",
        where="личный кабинет CloudPayments → Сайты → способы оплаты (подключение СБП)",
        default=True,
    )
    culture_name = choice(
        "Язык страницы оплаты",
        ("ru-RU", "en-US"),
        "Язык платёжной страницы CloudPayments (CultureName).",
        where="выберите сами",
        default="ru-RU",
        advanced=True,
    )
    base_url = url(
        "Адрес API",
        "Базовый адрес API CloudPayments. Меняйте, только если CloudPayments сообщил другой.",
        where="developers.cloudpayments.ru → API (обычно менять не нужно)",
        default=DEFAULT_BASE_URL,
        required=False,
        advanced=True,
    )


def sign(api_secret: str, message: bytes) -> str:
    """``base64(HMAC_SHA256(api_secret, message))`` — the value of ``Content-HMAC`` / ``X-Content-HMAC``."""
    digest = hmac.new(api_secret.encode("utf-8"), message, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def _authentic(api_secret: str, body: bytes, content_hmac: str, x_content_hmac: str) -> bool:
    """``Content-HMAC`` over the raw body, else ``X-Content-HMAC`` over the URL-decoded body (``+`` is tried
    both as a plus and as a space: the documentation does not say which decoding is meant)."""
    if content_hmac and constant_time_equal(sign(api_secret, body), content_hmac):
        return True
    if not x_content_hmac:
        return False
    try:
        raw = body.decode("utf-8")
    except UnicodeDecodeError:
        return False
    variants = dict.fromkeys((unquote(raw), unquote_plus(raw)))
    matched = False
    for decoded in variants:
        matched |= constant_time_equal(sign(api_secret, decoded.encode("utf-8")), x_content_hmac)
    return matched


def _our_payment_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower()
    return candidate if _UUID_RE.fullmatch(candidate) else None


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    return isinstance(value, str) and value.strip().lower() in ("1", "true")


def _currency(value: Any) -> str | None:
    code = _text(value).upper() if isinstance(value, str) else ""
    return code if re.fullmatch(r"[A-Z]{3}", code) else None


def _paid_at(value: Any, now: datetime) -> datetime | None:
    """``yyyy-MM-dd HH:mm:ss`` (UTC) or ISO without a zone (UTC); dropped when implausible."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        at = parse_timestamp(value.strip())
    except ValueError:
        return None
    return at if now - _PAID_AT_MAX_AGE <= at <= now + _PAID_AT_SKEW else None


def _clip(value: str, limit: int = _DESCRIPTION_MAX) -> str:
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def _json_amount(amount: Decimal) -> int | float:
    """A JSON number with at most two decimals (exact: the shortest repr of ``179.99`` is ``179.99``)."""
    return int(amount) if amount == amount.to_integral_value() else float(amount)


class CloudPayments(PaymentProvider):
    """See module docstring."""

    manifest = Manifest(
        slug="cloudpayments",
        title="CloudPayments",
        method_kinds=(MethodKind.CARD, MethodKind.SBP),
        currencies=_CURRENCIES,
        config=CloudPaymentsConfig,
        docs_url="https://developers.cloudpayments.ru/",
        # min/max are not published: they depend on the merchant's contract and terminal settings
    )
    capabilities = Capabilities(
        webhook_auth=WebhookAuth.SIGNATURE,
        replay_window_s=None,  # DateTime is the payment time, not a signed sending time
        fetch_status=True,
        batch_status=False,  # payments/find takes one InvoiceId; v2/payments/list is by period, not by ids
        refund=True,
        recurring=False,  # subscriptions need AccountId and a card token; not used by this plugin
        receipt_54fz=False,
        in_chat_invoice=False,
        redirect=True,
        webhook=True,
    )

    # ------------------------------------------------------------------------------------------- HTTP

    @property
    def _base(self) -> str:
        return str(self.config.base_url or DEFAULT_BASE_URL).rstrip("/")

    def _headers(self, request_id: str | None = None) -> dict[str, str]:
        token = base64.b64encode(f"{self.config.public_id}:{self.config.api_secret}".encode()).decode("ascii")
        headers = {"Authorization": f"Basic {token}", "Accept": "application/json"}
        if request_id is not None:
            headers["X-Request-ID"] = request_id
        return headers

    async def _call(
        self, method: str, body: Mapping[str, Any], what: str, *, request_id: str | None = None
    ) -> dict[str, Any]:
        """``POST`` and the ``{"Success", "Message", "Model"}`` envelope; HTTP errors →
        :class:`ProviderError`. ``Success=false`` is returned to the caller (it is not always an error:
        «Not found»)."""
        resp = await self.ctx.http.request(
            "POST", f"{self._base}/{method}", headers=self._headers(request_id), json=dict(body)
        )
        if resp.status != 200:
            raise self._error(resp, what)
        try:
            data = resp.json()
        except ValueError:
            raise ProviderError(_T["bad_answer"], retryable=True, status=resp.status) from None
        if not isinstance(data, dict) or not isinstance(data.get("Success"), bool):
            raise ProviderError(_T["bad_answer"], retryable=True, status=resp.status)
        return data

    def _error(self, resp: HttpResponse, what: str) -> ProviderError:
        if resp.status in (401, 403):
            return ProviderError(_T["bad_key"], retryable=False, status=resp.status)
        if resp.status == 429 or resp.status >= 500:
            return ProviderError(
                _T["unavailable"].format(status=resp.status), retryable=True, status=resp.status
            )
        self.ctx.log.warning("cloudpayments: %s answered HTTP %s", what, resp.status)
        return ProviderError(
            _T["rejected"].format(error=f"HTTP {resp.status}"), retryable=False, status=resp.status
        )

    def _refused(self, data: Mapping[str, Any], what: str) -> ProviderError:
        message = _clip(_text(data.get("Message")) or "Success=false", 200)
        self.ctx.log.warning("cloudpayments: %s refused (%s)", what, message)
        return ProviderError(_T["rejected"].format(error=message), retryable=False)

    # ----------------------------------------------------------------------------------------- create

    async def create(self, intent: PaymentIntent) -> Checkout:
        if intent.currency not in _CURRENCIES:
            raise ProviderError(_T["currency"], retryable=False)
        pid = intent.payment_id
        amount = _json_amount(intent.amount)
        description = _clip(intent.description or "Оплата")
        if intent.method_hint is MethodKind.SBP and intent.currency == "RUB" and self.config.sbp_link:
            body: dict[str, Any] = {
                "PublicId": self.config.public_id,
                "Amount": amount,
                "Currency": "RUB",
                "Scheme": "charge",
                "Description": description,
                "InvoiceId": pid,  # opaque: never a Telegram id
            }
            if intent.return_url:
                body["SuccessRedirectUrl"] = intent.return_url
            data = await self._call("payments/qr/sbp/link", body, "sbp", request_id=f"svbg-sbp-{pid}")
            link_key = "QrUrl"
        else:
            body = {
                "Amount": amount,
                "Currency": intent.currency,
                "Description": description,
                "InvoiceId": pid,  # opaque: never a Telegram id
                "RequireConfirmation": False,
                "SendEmail": False,
                "CultureName": self.config.culture_name,
            }
            if intent.return_url:
                body["SuccessRedirectUrl"] = intent.return_url
                body["FailRedirectUrl"] = intent.return_url
            data = await self._call("orders/create", body, "create", request_id=f"svbg-order-{pid}")
            link_key = "Url"
        if not data["Success"]:
            raise self._refused(data, "create")
        model = data.get("Model")
        pay_url = model.get(link_key) if isinstance(model, Mapping) else None
        if not isinstance(pay_url, str) or not re.match(r"https?://", pay_url):
            raise ProviderError(_T["bad_answer"], retryable=False)
        return Checkout(kind="url", external_id=pid, pay_url=pay_url)

    # ---------------------------------------------------------------------------------------- webhook

    async def parse_webhook(self, req: WebhookRequest) -> ProviderEvent:
        if req.method.upper() != "POST":
            raise WebhookRejected("notifications must be sent with POST", status=400)
        content_hmac = (req.header("Content-HMAC") or "").strip()
        x_content_hmac = (req.header("X-Content-HMAC") or "").strip()
        if not content_hmac and not x_content_hmac:
            raise WebhookRejected("missing signature", status=401)
        if not _authentic(self.config.api_secret, req.body, content_hmac, x_content_hmac):
            raise WebhookRejected("bad signature", status=401)
        try:
            pairs = parse_qsl(req.body.decode("utf-8"), keep_blank_values=True, strict_parsing=bool(req.body))
        except (UnicodeDecodeError, ValueError):
            raise WebhookRejected("malformed", status=400) from None
        fields = dict(pairs)
        ack = WebhookResponse.json(_ACK)
        kind = _text(req.query.get("type")).lower()
        if kind in ("check", "recurrent") or "TransactionId" not in fields:
            raise WebhookIgnored(f"{kind or 'recurrent'} notification", ack)
        state, label = self._classify(fields)
        if state is None:
            raise WebhookIgnored(f"{label} notification", ack)
        ours = _our_payment_id(fields.get("InvoiceId"))
        if ours is None:
            # Authentic, but not an invoice of this bot (a payment from the cabinet, a subscription charge).
            raise WebhookIgnored("no invoice of ours", ack)
        try:
            amount = parse_amount(fields.get("Amount"))
        except (TypeError, ValueError):
            raise WebhookRejected("bad amount", status=400) from None
        # Refund and Cancel notifications may come without Currency; a payment must name it.
        currency = _currency(fields.get("Currency"))
        if currency is None and (state is PaymentState.PAID or "Currency" in fields):
            raise WebhookRejected("bad currency", status=400)
        is_test = _truthy(fields.get("TestMode"))
        now = req.received_at
        return ProviderEvent(
            state=state,
            payment_id=ours,
            amount=amount,
            currency=currency,
            paid_at=_paid_at(fields.get("DateTime"), now) if state is PaymentState.PAID else None,
            is_test=is_test,
            summary={
                "kind": label,
                "transaction_id": _text(fields.get("TransactionId"))[:40],
                "payment_transaction_id": _text(fields.get("PaymentTransactionId"))[:40] or None,
                "status": _text(fields.get("Status"))[:40] or None,
                "reason_code": _text(fields.get("ReasonCode"))[:20] or None,
                "amount": str(amount),
                "currency": currency,
                "gateway": _text(fields.get("GatewayName"))[:40] or None,
                "test": is_test,
            },
        )

    def _classify(self, fields: Mapping[str, str]) -> tuple[PaymentState | None, str]:
        """The notification kind from its fields (one URL serves Pay, Fail, Confirm, Refund and Cancel)."""
        operation = _text(fields.get("OperationType")).lower()
        status = _text(fields.get("Status")).lower()
        if operation == "refund" or fields.get("PaymentTransactionId"):
            return PaymentState.REFUNDED, "refund"
        if operation == "cardpayout":
            return None, "payout"
        if fields.get("ReasonCode") or fields.get("Reason"):
            # Fail: the attempt was declined, the invoice stays payable (logged without a state change).
            return PaymentState.CREATED, "fail"
        if status:
            if not fields.get("GatewayName"):
                return None, "check"  # Check has no GatewayName: the payment is not authorized yet
            state = TRANSACTION_STATUS.get(status)
            return (state, "pay") if state is not None else (None, f"status {status[:20]}")
        return PaymentState.CANCELED, "cancel"

    def ack(self, event: ProviderEvent | None) -> WebhookResponse:
        return WebhookResponse.json(_ACK)

    # ----------------------------------------------------------------------------------------- status

    async def _find(self, invoice_id: str) -> dict[str, Any] | None:
        """The last payment operation of our invoice (``payments/find``: refunds are not returned)."""
        data = await self._call("payments/find", {"InvoiceId": invoice_id}, "status")
        model = data.get("Model")
        if not data["Success"] or not isinstance(model, dict):
            return None  # «Not found»: no payment attempt yet
        return model

    async def fetch_status(self, ids: Sequence[str]) -> list[ProviderStatus]:
        result: list[ProviderStatus] = []
        now = datetime.now(UTC)
        for external in dict.fromkeys(ids):
            if not external:
                continue
            model = await self._find(external)
            if model is None:
                continue
            state = TRANSACTION_STATUS.get(_text(model.get("Status")).lower())
            if state is None:
                continue
            if state is PaymentState.PAID and _truthy(model.get("Refunded")):
                state = PaymentState.REFUNDED
            amount: Decimal | None = None
            try:
                amount = parse_amount(model.get("Amount"))
            except (TypeError, ValueError):
                self.ctx.log.warning("cloudpayments: unreadable amount of %s", external[:40])
            result.append(
                ProviderStatus(
                    state=state,
                    external_id=external[:_ID_MAX],
                    payment_id=_our_payment_id(model.get("InvoiceId")),
                    amount=amount,
                    currency=_currency(model.get("Currency")),
                    paid_at=_paid_at(model.get("ConfirmDateIso") or model.get("AuthDateIso"), now)
                    if state is PaymentState.PAID
                    else None,
                    is_test=_truthy(model.get("TestMode")),
                )
            )
        return result

    # ----------------------------------------------------------------------------------------- refund

    async def refund(self, external_id: str, amount_minor: int, currency: str) -> RefundResult:
        """``payments/refund`` of the paid transaction of our invoice (the API allows partial amounts)."""
        try:
            model = await self._find(external_id)
            if model is None:
                return RefundResult(False, message=_T["refund_unknown"])
            if _text(model.get("Status")).lower() != "completed":
                return RefundResult(False, message=_T["refund_not_paid"])
            tx_id = model.get("TransactionId")
            if isinstance(tx_id, bool) or not isinstance(tx_id, int):
                return RefundResult(False, message=_T["bad_answer"])
            amount = Decimal(amount_minor).scaleb(-2)
            data = await self._call(
                "payments/refund",
                {"TransactionId": tx_id, "Amount": _json_amount(amount)},
                "refund",
                request_id=f"svbg-refund-{tx_id}-{amount_minor}",
            )
            if not data["Success"]:
                raise self._refused(data, "refund")
        except ProviderError as exc:
            return RefundResult(False, message=exc.human)
        model = data.get("Model")
        refund_id = model.get("TransactionId") if isinstance(model, Mapping) else None
        return RefundResult(True, None if refund_id is None else str(refund_id)[:_ID_MAX], _T["refund_ok"])

    # ------------------------------------------------------------------------------------------ probe

    async def test_credentials(self) -> Probe:
        """``POST /test`` with Basic auth — checks the key pair, creates nothing."""
        try:
            data = await self._call("test", {}, "probe")
        except ProviderError as exc:
            return Probe(False, exc.human)
        if not data["Success"]:
            return Probe(False, self._refused(data, "probe").human)
        return Probe(True, _T["probe_ok"])
