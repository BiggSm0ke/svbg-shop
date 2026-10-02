"""Pal24 / PayPalych (Pally) — card and SBP payment links (07 §4.2).

Specification: ``docs/providers/pal24.md`` (official documentation <https://pal24.pro/reference/api>, the same
page on <https://pally.info/reference/api>, checked 2026-10-02). Written from scratch by the specification;
imports only :mod:`svbg.sdk` and the standard library.

* Requests carry ``Authorization: Bearer <API token>``; bodies are forms, answers JSON. The host is a setting
  (``pal24.pro`` by default, ``pally.info`` answers the same).
* ``POST /api/v1/bill/create`` creates a one-time bill (``type=normal``: a ``multi`` bill never changes its
  status); ``order_id`` carries **only** our opaque payment id and comes back in the postback as ``InvId``;
  ``bill_id`` is our ``external_id``; the buyer goes to ``link_page_url``. Notification URLs are not sent:
  Result / Refund / Chargeback URL are shop settings in the cabinet.
* ``GET /api/v1/bill/status?id=`` reads a bill; no batch method by ids.
* **Postbacks** (form): payment ``SignatureValue = upper(md5(OutSum:InvId:apiToken))`` over the values exactly
  as received; refund ``md5(Amount:Currency:BillId:PaymentId:Id:token)``; chargeback
  ``md5(BillId:PaymentId:Id:token)``. The signatures do **not** cover ``Status`` (a ``FAIL`` and a ``SUCCESS``
  postback of one order carry the same signature) and carry no time, so the scheme is declared weak: a
  postback with a valid signature only makes the core re-read the bill (``bill/status``) or, for refunds and
  chargebacks, the payment (``payment/status?refunds=1&chargeback=1``) before anything changes (spec §4.2).
* ``UNDERPAID`` is reported as paid **without an amount** (the core turns it into ``mismatch``); ``OVERPAID``
  as paid with the bill amount (the surplus is logged).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final
from urllib.parse import parse_qsl

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
    choice,
    constant_time_equal,
    flag,
    integer,
    parse_amount,
    secret,
    text,
    url,
)

__all__ = [
    "BILL_STATUS",
    "CURRENCIES",
    "DEFAULT_BASE_URL",
    "PAYMENT_REF",
    "Pal24",
    "Pal24Config",
    "chargeback_signature",
    "payment_signature",
    "refund_signature",
    "transfer_signature",
]

DEFAULT_BASE_URL: Final = "https://pal24.pro"
CURRENCIES: Final = ("RUB", "USD", "EUR")
#: Bill / payment ``status`` → SDK state (spec §6). ``UNDERPAID`` and ``OVERPAID`` are handled separately.
BILL_STATUS: Final[Mapping[str, PaymentState]] = {
    "NEW": PaymentState.CREATED,
    "PROCESS": PaymentState.PROCESSING,
    "SUCCESS": PaymentState.PAID,
    "OVERPAID": PaymentState.PAID,
    "UNDERPAID": PaymentState.PAID,
    "FAIL": PaymentState.FAILED,
}
#: Prefix of a reference to a Pal24 *payment* (not a bill): refund and chargeback postbacks are verified with
#: ``payment/status`` by ``PaymentId``; the core hands this reference back to :meth:`Pal24.fetch_status`.
PAYMENT_REF: Final = "payment:"
_ID_MAX: Final = 200
_MAX_FIELDS: Final = 64
_DESCRIPTION_MAX: Final = 250
_UUID_RE: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_ERROR_KEY_RE: Final = re.compile(r"api:error\.[a-z_\-]+")
_NOT_FOUND: Final = frozenset({"api:error.bill_not_found", "api:error.payment_not_found"})

_T: Final = {
    "bad_token": "Pal24 отклонила API-токен — проверьте его в кабинете Pal24 («API интеграции»)",
    "ip": "Pal24 не пускает запросы с IP сервера — добавьте его в вайтлист API в кабинете Pal24",
    "shop": "Pal24 не нашла магазин или не даёт к нему доступ — проверьте «ID магазина»",
    "banned": "Pal24 заблокировала аккаунт мерчанта — обратитесь в поддержку Pal24",
    "amount": "Pal24 не принимает такую сумму (api:error.invalid_amount)",
    "rate": "Pal24: это направление оплаты сейчас недоступно (api:error.rate-not-found)",
    "rejected": "Pal24 отклонила запрос (HTTP {status}{key})",
    "invalid": "Pal24 отклонила параметры счёта (HTTP {status}) — проверьте ID магазина и настройки",
    "unavailable": "Pal24 временно недоступна (HTTP {status})",
    "bad_answer": "Pal24 вернула непонятный ответ",
    "currency": "Pal24 принимает только RUB, USD и EUR",
    "probe_ok": "Токен и магазин приняты, Pal24 доступна. Подпись postback проверится при первой оплате",
    "probe_not_pal": "по адресу API отвечает не Pal24 — проверьте «Адрес API»",
}


class Pal24Config(ConfigModel):
    api_token = secret(
        "API-токен",
        "Токен вида «число|строка». Им подписываются запросы к API (Authorization: Bearer) и Pal24 "
        "подписывает postback (SignatureValue).",
        where="кабинет Pal24 (pal24.pro / pally.info) → «API интеграции» → токен",
    )
    shop_id = text(
        "ID магазина",
        "Идентификатор магазина (shop_id), например LXZv3R7Q8B. Без него не работают Result / Success / "
        "Fail URL.",
        where="кабинет Pal24 → «Магазины» → ваш магазин → ID магазина",
        pattern=r"[A-Za-z0-9_\-]{1,64}",
    )
    payer_pays_commission = flag(
        "Комиссию платит покупатель",
        "Да — Pal24 добавит свою комиссию к сумме для покупателя (payer_pays_commission=1); нет — комиссия "
        "удерживается из суммы магазина. Зачисляется в любом случае сумма счёта.",
        default=False,
    )
    payment_method = choice(
        "Способ оплаты",
        ("any", "BANK_CARD", "SBP"),
        "any — покупатель выбирает на форме Pal24; BANK_CARD — только карта; SBP — только СБП.",
        default="any",
        required=False,
    )
    bill_ttl = integer(
        "Время жизни счёта, секунд",
        "Параметр ttl счёта. 0 — не передавать (срок по умолчанию Pal24).",
        default=0,
        required=False,
        min=0,
        max=2_592_000,
        advanced=True,
    )
    base_url = url(
        "Адрес API",
        "Хост API Pal24. Меняйте, только если Pal24 сообщила другой (например, https://pally.info).",
        where="документация Pal24 (pal24.pro/reference/api) → «Базовый пример»",
        default=DEFAULT_BASE_URL,
        required=False,
        advanced=True,
    )


# ------------------------------------------------------------------------------------------- signatures


def _md5_upper(raw: str) -> str:
    return hashlib.md5(raw.encode("utf-8"), usedforsecurity=True).hexdigest().upper()  # noqa: S324 - protocol


def payment_signature(out_sum: str, inv_id: str, api_token: str) -> str:
    """``strtoupper(md5(OutSum:InvId:apiToken))`` over the raw strings (spec §4.2)."""
    return _md5_upper(f"{out_sum}:{inv_id}:{api_token}")


def refund_signature(  # noqa: PLR0917 - the protocol's field list
    amount: str, currency: str, bill_id: str, payment_id: str, refund_id: str, api_token: str
) -> str:
    """``strtoupper(md5(Amount:Currency:BillId:PaymentId:Id:apiToken))`` (spec §4.3)."""
    return _md5_upper(f"{amount}:{currency}:{bill_id}:{payment_id}:{refund_id}:{api_token}")


def chargeback_signature(bill_id: str, payment_id: str, chargeback_id: str, api_token: str) -> str:
    """``strtoupper(md5(BillId:PaymentId:Id:apiToken))`` (spec §4.4)."""
    return _md5_upper(f"{bill_id}:{payment_id}:{chargeback_id}:{api_token}")


def transfer_signature(amount: str, trs_id: str, api_token: str) -> str:
    """``strtoupper(md5(Amount:TrsId:apiToken))`` — payout and P2P postbacks (spec §4.5)."""
    return _md5_upper(f"{amount}:{trs_id}:{api_token}")


# ----------------------------------------------------------------------------------------------- helpers


def _our_payment_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower()
    return candidate if _UUID_RE.fullmatch(candidate) else None


def _id(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if 0 < len(value) <= _ID_MAX else None


def _form(req: WebhookRequest) -> dict[str, str]:
    """Postback fields (``application/x-www-form-urlencoded``). A repeated field is ambiguous → ``400``."""
    try:
        body = req.body.decode("utf-8")
    except UnicodeDecodeError:
        raise WebhookRejected("malformed", status=400) from None
    if not body.strip():
        raise WebhookRejected("empty postback", status=400)
    try:
        pairs = parse_qsl(body, keep_blank_values=True, strict_parsing=True, max_num_fields=_MAX_FIELDS)
    except ValueError:
        raise WebhookRejected("malformed", status=400) from None
    fields: dict[str, str] = {}
    for key, value in pairs:
        if key in fields:
            raise WebhookRejected(f"repeated field {key[:20]}", status=400)
        fields[key] = value
    return fields


def _signature_ok(expected: str, given: str | None) -> bool:
    """Case-insensitive, constant-time; a missing value never matches."""
    return constant_time_equal(expected.upper(), (given or "").strip().upper())


def _error_key(resp: HttpResponse) -> str | None:
    """The ``api:error.*`` key of an error answer, wherever the JSON puts it."""
    found = _ERROR_KEY_RE.search(resp.text())
    return found.group(0) if found else None


def _decimal_json(resp: HttpResponse) -> Any:
    try:
        return json.loads(resp.body.decode("utf-8"), parse_float=Decimal)
    except (UnicodeDecodeError, ValueError):
        raise ProviderError(_T["bad_answer"], retryable=True, status=resp.status) from None


def _resource(data: Any) -> Mapping[str, Any] | None:
    """A resource answer: flat (``{"id": …, "success": true}``) or wrapped in ``data``."""
    if not isinstance(data, Mapping):
        return None
    inner = data.get("data")
    if isinstance(inner, Mapping) and "status" in inner:
        return inner
    return data


def _currency(value: Any) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z]{3,8}", value.strip()):
        return value.strip().upper()
    return None


def _upper(value: Any) -> str:
    return value.strip().upper() if isinstance(value, str) else ""


class Pal24(PaymentProvider):
    """See module docstring."""

    manifest = Manifest(
        slug="pal24",
        title="Pal24 (PayPalych)",
        method_kinds=(MethodKind.CARD, MethodKind.SBP),
        currencies=CURRENCIES,
        config=Pal24Config,
        docs_url="https://pal24.pro/reference/api",
    )
    capabilities = Capabilities(
        # MD5(OutSum:InvId:token) is a static per-order value: it covers neither Status nor TrsId and carries
        # no time — every postback is re-read with fetch_status before anything changes (spec §4.2).
        webhook_auth=WebhookAuth.SECRET_HEADER,
        replay_window_s=None,
        fetch_status=True,
        batch_status=False,  # bill/search filters by dates, not by ids
        refund=False,  # the refund API is enabled only on request through support (spec §7)
        recurring=False,  # no recurring payments, only reusable «multi» bills
        receipt_54fz=False,
        in_chat_invoice=False,
        redirect=True,
        webhook=True,
    )

    # ------------------------------------------------------------------------------------------- HTTP

    @property
    def _base(self) -> str:
        return str(self.config.base_url or DEFAULT_BASE_URL).rstrip("/") + "/api/v1"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.config.api_token}", "Accept": "application/json"}

    def _raise_for(self, resp: HttpResponse, what: str) -> None:
        key = _error_key(resp)
        status = resp.status
        if status == 401:
            raise ProviderError(_T["bad_token"], retryable=False, status=status)
        if status == 429 or status >= 500:
            raise ProviderError(_T["unavailable"].format(status=status), retryable=True, status=status)
        self.ctx.log.warning("pal24: %s answered HTTP %s %s", what, status, key or "")
        human = {
            "api:error.ip_access_denied": _T["ip"],
            "api:error.access_denied": _T["shop"],
            "api:error.merchant_not_found": _T["shop"],
            "api:error.shop_not_found": _T["shop"],
            "api:error.merchant_banned": _T["banned"],
            "api:error.invalid_amount": _T["amount"],
            "api:error.rate-not-found": _T["rate"],
        }.get(key or "")
        if human is None:
            human = (
                _T["invalid"].format(status=status)
                if status == 422
                else _T["rejected"].format(status=status, key=f", {key}" if key else "")
            )
        raise ProviderError(human, retryable=False, status=status)

    # ----------------------------------------------------------------------------------------- create

    async def create(self, intent: PaymentIntent) -> Checkout:
        currency = intent.currency.upper()
        if currency not in CURRENCIES:
            raise ProviderError(_T["currency"], retryable=False)
        form: dict[str, str] = {
            "amount": intent.amount_text(),  # "179.00": a decimal string, at most two decimals
            "shop_id": str(self.config.shop_id),
            "order_id": intent.payment_id,  # opaque: never a Telegram id or a subscription link
            "description": (intent.description or "Оплата")[:_DESCRIPTION_MAX],
            "type": "normal",
            "currency_in": currency,
            "locale": "ru",
            "payer_pays_commission": "1" if self.config.payer_pays_commission else "0",
        }
        if self.config.payment_method in ("BANK_CARD", "SBP"):
            form["payment_method"] = str(self.config.payment_method)
        ttl = int(self.config.bill_ttl or 0)
        if ttl > 0:
            form["ttl"] = str(ttl)
        resp = await self.ctx.http.request(
            "POST", f"{self._base}/bill/create", headers=self._headers(), data=form
        )
        if resp.status != 200:
            self._raise_for(resp, "bill/create")
        data = _decimal_json(resp)
        if not isinstance(data, Mapping):
            raise ProviderError(_T["bad_answer"], retryable=False, status=resp.status)
        success = data.get("success")
        pay_url = data.get("link_page_url")
        bill_id = _id(data.get("bill_id"))
        if (
            success not in (True, "true", 1, "1")
            or bill_id is None
            or not isinstance(pay_url, str)
            or not re.match(r"https?://", pay_url.strip())
        ):
            self.ctx.log.warning("pal24: bill/create answered without success/bill_id/link_page_url")
            raise ProviderError(_T["bad_answer"], retryable=False, status=resp.status)
        expires = datetime.now(UTC) + timedelta(seconds=ttl) if ttl > 0 else None
        return Checkout(kind="url", external_id=bill_id, pay_url=pay_url.strip(), expires_at=expires)

    # ---------------------------------------------------------------------------------------- webhook

    async def parse_webhook(self, req: WebhookRequest) -> ProviderEvent:
        fields = _form(req)
        token = str(self.config.api_token)
        given = fields.get("SignatureValue")
        if "OutSum" in fields:
            return self._payment_postback(fields, token, given)
        if "PaymentId" in fields and "BillId" in fields and "Id" in fields:
            return self._refund_or_chargeback(fields, token, given)
        if "TrsId" in fields and "Amount" in fields:
            # payout or P2P deal postback: authentic ones are acknowledged, nothing to do for a shop (§4.5)
            if not _signature_ok(transfer_signature(fields["Amount"], fields["TrsId"], token), given):
                raise WebhookRejected("bad signature", status=401)
            self.ctx.log.info("pal24: payout / P2P postback acknowledged without changes")
            raise WebhookIgnored("transfer postback")
        raise WebhookRejected("unknown postback", status=400)

    def _payment_postback(self, fields: Mapping[str, str], token: str, given: str | None) -> ProviderEvent:
        out_sum = fields["OutSum"]
        inv_id = fields.get("InvId", "")
        # The raw strings, as received: "18.54" and "18.540" are different signatures.
        if not _signature_ok(payment_signature(out_sum, inv_id, token), given):
            raise WebhookRejected("bad signature", status=401)
        bill_id = _id(fields.get("TrsId"))
        ours = _our_payment_id(inv_id)
        if bill_id is None and ours is None:
            raise WebhookRejected("no bill id", status=400)
        try:
            amount = parse_amount(out_sum)
        except (TypeError, ValueError):
            raise WebhookRejected("bad amount", status=400) from None
        status = _upper(fields.get("Status"))
        # Status is not signed: it is only a hint, the core re-reads the bill before any change.
        state = BILL_STATUS.get(status, PaymentState.PROCESSING)
        currency = _currency(fields.get("CurrencyIn"))
        return ProviderEvent(
            state=state,
            external_id=bill_id,
            payment_id=ours,
            amount=amount,
            currency=currency,
            is_test=self.ctx.is_test,
            signed_at=None,
            summary={
                "kind": "payment",
                "trs_id": bill_id,
                "inv_id": inv_id[:_ID_MAX] or None,
                "status": status[:20] or None,
                "out_sum": str(amount),
                "commission": fields.get("Commission", "")[:20] or None,
                "currency": currency,
                "account_type": fields.get("AccountType", "")[:20] or None,
                "error_code": fields.get("ErrorCode", "")[:20] or None,
            },
        )

    def _refund_or_chargeback(
        self, fields: Mapping[str, str], token: str, given: str | None
    ) -> ProviderEvent:
        bill_id, pay_id, item_id = fields["BillId"], fields["PaymentId"], fields["Id"]
        is_refund = "Amount" in fields
        if is_refund:
            expected = refund_signature(
                fields["Amount"], fields.get("Currency", ""), bill_id, pay_id, item_id, token
            )
        else:
            expected = chargeback_signature(bill_id, pay_id, item_id, token)
        if not _signature_ok(expected, given):
            raise WebhookRejected("bad signature", status=401)
        payment_ref = _id(pay_id)
        if payment_ref is None or len(PAYMENT_REF) + len(payment_ref) > _ID_MAX:
            raise WebhookRejected("no payment id", status=400)
        status = _upper(fields.get("Status"))
        claimed = PaymentState.REFUNDED if is_refund else PaymentState.CHARGEBACK
        return ProviderEvent(
            # Status is not signed either: whatever it says, the payment is re-read (payment/status).
            state=claimed if status == "SUCCESS" else PaymentState.PROCESSING,
            external_id=PAYMENT_REF + payment_ref,
            payment_id=_our_payment_id(fields.get("InvId")),
            is_test=self.ctx.is_test,
            signed_at=None,
            summary={
                "kind": "refund" if is_refund else "chargeback",
                "id": item_id[:60],
                "bill_id": bill_id[:60],
                "payment_id": payment_ref,
                "status": status[:20] or None,
                "amount": fields.get("Amount", "")[:20] or None,
                "currency": _currency(fields.get("Currency")),
            },
        )

    def ack(self, event: ProviderEvent | None) -> WebhookResponse:
        return WebhookResponse.ok("OK")

    # ----------------------------------------------------------------------------------------- status

    async def _get(self, path: str, params: Mapping[str, str], what: str) -> Mapping[str, Any] | None:
        """A GET resource; ``None`` when Pal24 does not know the id."""
        resp = await self.ctx.http.request(
            "GET", f"{self._base}{path}", headers=self._headers(), params=dict(params)
        )
        if resp.status == 404 or (resp.status in (400, 403) and _error_key(resp) in _NOT_FOUND):
            return None
        if resp.status != 200:
            self._raise_for(resp, what)
        resource = _resource(_decimal_json(resp))
        if resource is None:
            raise ProviderError(_T["bad_answer"], retryable=True, status=resp.status)
        return resource

    async def fetch_status(self, ids: Sequence[str]) -> list[ProviderStatus]:
        """``bill/status`` by bill id; a ``payment:<id>`` reference (from a refund / chargeback postback) is
        read with ``payment/status?refunds=1&chargeback=1``. Unknown ids are absent from the result."""
        result: list[ProviderStatus] = []
        for ref in dict.fromkeys(ids):
            if not ref:
                continue
            if ref.startswith(PAYMENT_REF):
                status = await self._payment_status(ref[len(PAYMENT_REF) :])
            else:
                status = await self._bill_status(ref)
            if status is not None:
                result.append(status)
        return result

    async def _bill_status(self, bill_id: str) -> ProviderStatus | None:
        bill = await self._get("/bill/status", {"id": bill_id}, "bill/status")
        if bill is None:
            return None
        reported = _id(bill.get("id"))
        if reported is not None and reported != bill_id:
            self.ctx.log.warning("pal24: status of %s answered for another bill", bill_id[:40])
            return None
        return self._status(
            bill,
            external_id=bill_id,
            order_id=bill.get("order_id"),
            amount_key="amount",
            ref=bill_id,
        )

    async def _payment_status(self, payment_ref: str) -> ProviderStatus | None:
        payment = await self._get(
            "/payment/status", {"id": payment_ref, "refunds": "1", "chargeback": "1"}, "payment/status"
        )
        if payment is None:
            return None
        bill_id = _id(payment.get("bill_id"))
        if bill_id is None:
            self.ctx.log.warning("pal24: payment %s has no bill id", payment_ref[:40])
            return None
        ours = _our_payment_id(payment.get("bill_order_id"))
        chargeback = payment.get("chargeback")
        chargebacks = chargeback if isinstance(chargeback, list) else [chargeback]
        if any(isinstance(c, Mapping) and _upper(c.get("status")) == "SUCCESS" for c in chargebacks):
            return ProviderStatus(
                state=PaymentState.CHARGEBACK, external_id=bill_id, payment_id=ours, is_test=self.ctx.is_test
            )
        refunds = payment.get("refunds")
        refunded = any(
            isinstance(r, Mapping) and _upper(r.get("status")) == "SUCCESS"
            for r in (refunds if isinstance(refunds, list) else [])
        )
        refunded_amount = _maybe_amount(payment.get("refunded_amount"))
        if refunded or (refunded_amount is not None and refunded_amount > 0):
            total = _maybe_amount(payment.get("amount"))
            if refunded_amount is not None and total is not None and refunded_amount < total:
                self.ctx.log.warning("pal24: payment %s is refunded partially", payment_ref[:40])
            return ProviderStatus(
                state=PaymentState.REFUNDED, external_id=bill_id, payment_id=ours, is_test=self.ctx.is_test
            )
        return self._status(
            payment,
            external_id=bill_id,
            order_id=payment.get("bill_order_id"),
            amount_key="bill_amount",
            ref=payment_ref,
        )

    def _status(
        self,
        data: Mapping[str, Any],
        *,
        external_id: str,
        order_id: Any,
        amount_key: str,
        ref: str,
    ) -> ProviderStatus | None:
        raw = _upper(data.get("status"))
        state = BILL_STATUS.get(raw)
        if state is None:
            self.ctx.log.warning("pal24: unknown status %r of %s", raw[:20], ref[:40])
            return None
        amount: Decimal | None = None
        if raw == "UNDERPAID":
            # less than the bill was paid: no amount → the core marks the payment «mismatch» for the owner
            self.ctx.log.warning("pal24: %s is underpaid", ref[:40])
        elif state is PaymentState.PAID:
            amount = _maybe_amount(data.get(amount_key))
            if amount is None:
                self.ctx.log.warning("pal24: unreadable amount of %s", ref[:40])
            if raw == "OVERPAID":
                self.ctx.log.warning("pal24: %s is overpaid; the bill amount is credited", ref[:40])
        return ProviderStatus(
            state=state,
            external_id=external_id,
            payment_id=_our_payment_id(order_id),
            amount=amount,
            currency=_currency(data.get("currency_in")),
            is_test=self.ctx.is_test,
        )

    # ------------------------------------------------------------------------------------------ probe

    async def test_credentials(self) -> Probe:
        """No side effects: ``GET bill/search`` of the shop, one item — checks the token, the IP whitelist
        and access to the shop. The postback signature uses the same token."""
        resp = await self.ctx.http.request(
            "GET",
            f"{self._base}/bill/search",
            headers=self._headers(),
            params={"shop_id": str(self.config.shop_id), "per_page": "1"},
        )
        if resp.status == 200:
            try:
                data = resp.json()
            except ValueError:
                return Probe(False, _T["probe_not_pal"])
            if not isinstance(data, Mapping) or "data" not in data:
                return Probe(False, _T["probe_not_pal"])
            return Probe(True, _T["probe_ok"])
        try:
            self._raise_for(resp, "bill/search")
        except ProviderError as exc:
            if resp.status == 404 and _error_key(resp) is None:
                return Probe(False, _T["probe_not_pal"])
            return Probe(False, exc.human)
        return Probe(False, _T["bad_answer"])  # pragma: no cover - _raise_for always raises


def _maybe_amount(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return parse_amount(value)
    except (TypeError, ValueError):
        return None
