"""Tribute — payments inside Telegram (cards, SBP, Wallet Pay) through the Tribute **Shop API**.

Written from scratch from the specification ``docs/providers/tribute.md`` (official documentation
<https://wiki.tribute.tg/for-shops/api> and the OpenAPI <https://tribute.tg/api/v1/openapi/shop/en>,
checked on 2026-10-02). Imports only :mod:`svbg.sdk`.

* ``POST /shop/orders`` (header ``Api-Key``) creates a one-time order: ``amount`` in **minor units**,
  ``currency`` in lower case, ``customerId`` = **only** the opaque payment id. The order ``uuid`` is our
  ``external_id``; the buyer goes to ``webappPaymentUrl`` (Mini App in Telegram) or ``paymentUrl`` (browser).
  The API has no idempotency key, so a create is never retried by the plugin.
* Webhooks: ``trbt-signature`` = HMAC-SHA256 of the raw body keyed with the API key. The documentation does
  not say whether the value is hex or base64 (§5.2, §12 of the specification), so the header is compared in
  constant time with **both** encodings of the same MAC until the owner confirms one. No signed time header
  (``replay_window_s=None``): replays are stopped by the core's body deduplication.
* Events: ``shop_order`` → paid (the charged amount is ``firstPeriodAmount`` when present, else ``amount``; a
  trial activation carries no money and is ignored); ``shop_order_payment_received`` /
  ``shop_order_prepaid`` → processing; ``shop_order_refunded`` with ``status=completed`` → refunded (a bank
  chargeback arrives the same way); ``shop_order_payment_failed`` is not terminal (the order stays payable)
  and is acknowledged without a state. Recurring and Creator-API events (donations, channel subscriptions,
  digital products) carry no invoice of ours and are acknowledged with ``{"status": "ok"}``.
* ``fetch_status``: ``GET /shop/orders/{uuid}`` (one per order, no batch API). ``refund``: the whole
  ``sell`` transaction of a paid order (the API has no partial refunds).
* Tribute has no public test mode: every webhook is live.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
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
    WebhookResponse,
    choice,
    constant_time_equal,
    integer,
    parse_timestamp,
    secret,
    url,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "ORDER_STATUS",
    "SIGNATURE_HEADER",
    "Tribute",
    "TributeConfig",
    "signatures",
]

DEFAULT_BASE_URL: Final = "https://tribute.tg/api/v1"
SIGNATURE_HEADER: Final = "trbt-signature"
#: ``ShopOrder.status`` → SDK state (specification §6, §10).
ORDER_STATUS: Final[Mapping[str, PaymentState]] = {
    "pending": PaymentState.CREATED,
    "prepaid": PaymentState.PROCESSING,
    "paid": PaymentState.PAID,
    "failed": PaymentState.FAILED,
}
#: Shop API currencies; all three have two decimals (amounts are integers in minor units).
_CURRENCIES: Final = ("RUB", "EUR", "USD")
_EXPONENT: Final = 2
_TITLE_MAX: Final = 100  # UTF-16 code units
_DESCRIPTION_MAX: Final = 300
_ID_MAX: Final = 200
_UUID_RE: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_FRACTION_RE: Final = re.compile(r"(\.\d{6})\d+")
_OK: Final = {"status": "ok"}
#: Events that never change one of our invoices (recurring orders, Creator API, physical goods).
_IGNORED_EVENTS: Final = frozenset(
    {
        "shop_order_payment_failed",
        "shop_order_charge_success",
        "shop_order_charge_failed",
        "shop_order_cancelled",
        "shop_token_charge_success",
        "shop_token_charge_failed",
        "new_subscription",
        "renewed_subscription",
        "cancelled_subscription",
        "new_donation",
        "recurrent_donation",
        "cancelled_donation",
        "new_digital_product",
        "digital_product_refunded",
        "physical_order_created",
        "physical_order_shipped",
        "physical_order_canceled",
    }
)

_T: Final = {
    "bad_key": "Tribute отклонил API-ключ — проверьте его: Tribute → «⋯» → Настройки → API Keys",
    "forbidden": "Tribute запретил операцию: магазин принадлежит другому аккаунту — проверьте «ID магазина»",
    "not_found": "Tribute не нашёл магазин — проверьте «ID магазина» или подключите магазин в Tribute",
    "rejected": "Tribute отклонил запрос: {error}",
    "unavailable": "Tribute временно недоступен (HTTP {status})",
    "bad_answer": "Tribute вернул непонятный ответ",
    "currency": "Tribute принимает только RUB, EUR и USD",
    "probe_ok": "Ключ принят, магазин «{name}» (ID {id})",
    "probe_no_hook": " — внимание: в Tribute не указан Webhook URL (Настройки → API Keys)",
    "probe_no_shop": "Ключ верный, но у аккаунта нет магазина — Shop API недоступен. Подключите магазин "
    "через поддержку Tribute",
    "probe_shop_missing": "Ключ верный, но магазина с ID {id} у аккаунта нет",
    "refund_partial": "Tribute возвращает только всю сумму заказа, частичный возврат невозможен",
    "refund_not_paid": "заказ Tribute не оплачен — возвращать нечего",
    "refund_no_tx": "у заказа Tribute нет транзакции, которую можно вернуть",
    "refund_ok": "Tribute: возврат начат, итог придёт уведомлением",
}
#: ``error`` codes of ``POST /shop/orders`` → owner-facing text.
_ERRORS: Final[Mapping[str, str]] = {
    "error_shop_inactive": "магазин в Tribute не активен",
    "error_invalid_currency": "валюта не поддерживается магазином",
    "error_amount_too_small": "сумма меньше минимальной для Tribute",
    "error_amount_too_large": "сумма больше максимальной для Tribute",
    "error_title_required": "пустое название заказа",
    "error_title_too_long": "слишком длинное название заказа",
    "error_description_required": "пустое описание заказа",
    "error_description_too_long": "слишком длинное описание заказа",
    "error_invalid_url": "адрес возврата не принят (нужен https://)",
    "error_customer_id_too_long": "слишком длинный идентификатор платежа",
    "error_order_not_paid": "заказ не оплачен",
    "error_already_refunded": "уже возвращено",
    "error_not_refundable": "транзакцию нельзя вернуть",
    "error_transaction_mismatch": "транзакция не относится к заказу",
    "error_refund_failed": "возврат не прошёл",
}


class TributeConfig(ConfigModel):
    api_key = secret(
        "API-ключ",
        "Ключ для запросов к API Tribute (заголовок Api-Key). Этим же ключом Tribute подписывает "
        "уведомления.",
        where="Tribute (@tribute или web.tribute.tg) → «⋯» → Настройки → API Keys → «Generate API Key»",
    )
    shop_id = integer(
        "ID магазина",
        "Номер магазина в Shop API. Пусто — Tribute использует самый старый магазин аккаунта.",
        where="Tribute → «⋯» → Настройки → API Keys (список магазинов); проверка ключа покажет найденные",
        required=False,
        min=1,
        advanced=True,
    )
    pay_link = choice(
        "Куда вести покупателя",
        ("telegram", "web"),
        "telegram — оплата в Mini App Tribute внутри Telegram (webappPaymentUrl), web — страница оплаты в "
        "браузере (paymentUrl).",
        where="выберите сами; для бота обычно telegram",
        default="telegram",
    )
    base_url = url(
        "Адрес API",
        "Базовый адрес API Tribute. Меняйте, только если Tribute сообщил другой.",
        where="документация wiki.tribute.tg → For shops → API (обычно менять не нужно)",
        default=DEFAULT_BASE_URL,
        required=False,
        advanced=True,
    )


def signatures(api_key: str, body: bytes) -> tuple[str, str]:
    """Both accepted encodings of ``HMAC-SHA256(api_key, body)``: lower-case hex and standard base64."""
    digest = hmac.new(api_key.encode("utf-8"), body, hashlib.sha256).digest()
    return digest.hex(), base64.b64encode(digest).decode("ascii")


def _signature_ok(api_key: str, body: bytes, header: str) -> bool:
    hex_mac, b64_mac = signatures(api_key, body)
    as_hex = constant_time_equal(hex_mac, header.lower())
    as_b64 = constant_time_equal(b64_mac, header)
    return as_hex or as_b64


def _from_minor(value: Any) -> Decimal:
    """An integer amount in minor units → major units; ``ValueError`` otherwise."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"not an amount in minor units: {value!r}")
    return Decimal(value).scaleb(-_EXPONENT)


def _currency(value: Any) -> str:
    if not isinstance(value, str) or value.strip().upper() not in _CURRENCIES:
        raise ValueError(f"unknown currency {value!r}")
    return value.strip().upper()


def _uuid(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower()
    return candidate if _UUID_RE.fullmatch(candidate) else None


def _external_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if 0 < len(value) <= _ID_MAX else None


def _time(value: Any) -> datetime | None:
    """RFC 3339 with up to 9 fractional digits (truncated to microseconds); ``None`` when unreadable."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return parse_timestamp(_FRACTION_RE.sub(r"\1", value.strip()))
    except ValueError:
        return None


def _clip(value: str, limit: int) -> str:
    """Whitespace-normalized text of at most ``limit`` UTF-16 code units."""
    value = " ".join(value.split())
    if len(value.encode("utf-16-le")) // 2 <= limit:
        return value
    out = ""
    for ch in value:
        if len((out + ch + "…").encode("utf-16-le")) // 2 > limit:
            break
        out += ch
    return out.rstrip() + "…"


def _charged(order: Mapping[str, Any]) -> Decimal:
    """What the buyer paid for the order: ``firstPeriodAmount`` when present, else ``amount``."""
    first = order.get("firstPeriodAmount")
    return _from_minor(first if first is not None else order.get("amount"))


class Tribute(PaymentProvider):
    """See module docstring."""

    manifest = Manifest(
        slug="tribute",
        title="Tribute",
        method_kinds=(MethodKind.CARD,),
        currencies=_CURRENCIES,
        config=TributeConfig,
        docs_url="https://wiki.tribute.tg/for-shops/api",
        # The API error text says «minimum amount is 100 (in smallest currency units)»; the limits page says
        # ₽100 (open question §12 of the specification) — Tribute's own error_amount_too_small decides.
        min_minor=100,
    )
    capabilities = Capabilities(
        webhook_auth=WebhookAuth.SIGNATURE,
        replay_window_s=None,  # no signed time header; deliveries are retried for ~24 h and can be resent
        fetch_status=True,
        batch_status=False,  # no «statuses by uuid list» method
        refund=True,
        recurring=False,  # only one-time orders are created
        receipt_54fz=False,
        in_chat_invoice=False,
        redirect=True,
        webhook=True,
    )

    # ------------------------------------------------------------------------------------------- HTTP

    @property
    def _base(self) -> str:
        return str(self.config.base_url or DEFAULT_BASE_URL).rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {"Api-Key": self.config.api_key, "Accept": "application/json"}

    @staticmethod
    def _body(resp: HttpResponse) -> Any:
        try:
            return resp.json()
        except ValueError:
            raise ProviderError(_T["bad_answer"], retryable=True, status=resp.status) from None

    def _error(self, resp: HttpResponse, what: str) -> ProviderError:
        if resp.status == 401:
            return ProviderError(_T["bad_key"], retryable=False, status=401)
        if resp.status == 429 or resp.status >= 500:
            code = _error_code(resp)
            if code == "error_refund_failed":
                return ProviderError(_T["rejected"].format(error=_ERRORS[code]), status=resp.status)
            return ProviderError(
                _T["unavailable"].format(status=resp.status), retryable=True, status=resp.status
            )
        if resp.status == 403:
            return ProviderError(_T["forbidden"], retryable=False, status=403)
        code = _error_code(resp)
        if resp.status == 404 and code is None:
            return ProviderError(_T["not_found"], retryable=False, status=404)
        detail = _ERRORS.get(code or "", code or f"HTTP {resp.status}")
        self.ctx.log.warning("tribute: %s answered HTTP %s (%s)", what, resp.status, (code or "")[:60])
        return ProviderError(_T["rejected"].format(error=detail), retryable=False, status=resp.status)

    async def _get(self, path: str) -> HttpResponse:
        return await self.ctx.http.request("GET", f"{self._base}{path}", headers=self._headers())

    # ----------------------------------------------------------------------------------------- create

    async def create(self, intent: PaymentIntent) -> Checkout:
        if intent.currency not in _CURRENCIES:
            raise ProviderError(_T["currency"], retryable=False)
        text = intent.description or "Оплата"
        body: dict[str, Any] = {
            "amount": intent.amount_minor,
            "currency": intent.currency.lower(),
            "title": _clip(text, _TITLE_MAX),
            "description": _clip(text, _DESCRIPTION_MAX),
            "customerId": intent.payment_id,  # opaque: never a Telegram id
            "period": "onetime",
        }
        if self.config.shop_id:
            body["shopId"] = int(self.config.shop_id)
        if intent.return_url and intent.return_url.startswith("https://"):
            body["successUrl"] = intent.return_url
            body["failUrl"] = intent.return_url
        # No idempotency key in the API: a retry would create a second order, so none is made here.
        resp = await self.ctx.http.request(
            "POST", f"{self._base}/shop/orders", headers=self._headers(), json=body
        )
        if resp.status not in (200, 201):
            raise self._error(resp, "create")
        data = self._body(resp)
        if not isinstance(data, dict):
            raise ProviderError(_T["bad_answer"], retryable=False, status=resp.status)
        external = _external_id(data.get("uuid"))
        web, app = data.get("paymentUrl"), data.get("webappPaymentUrl")
        links = (app, web) if self.config.pay_link == "telegram" else (web, app)
        pay_url = next((u for u in links if isinstance(u, str) and re.match(r"https?://", u)), None)
        if external is None or pay_url is None:
            raise ProviderError(_T["bad_answer"], retryable=False, status=resp.status)
        return Checkout(kind="url", external_id=external, pay_url=pay_url)

    # ---------------------------------------------------------------------------------------- webhook

    async def parse_webhook(self, req: WebhookRequest) -> ProviderEvent:
        header = (req.header(SIGNATURE_HEADER) or "").strip()
        if not header:
            raise WebhookRejected("missing signature", status=401)
        if not _signature_ok(self.config.api_key, req.body, header):
            raise WebhookRejected("bad signature", status=401)
        data = req.json()
        if not isinstance(data, dict) or not isinstance(data.get("payload"), dict):
            raise WebhookRejected("malformed", status=400)
        name = data.get("name")
        if not isinstance(name, str) or not name:
            raise WebhookRejected("malformed", status=400)
        payload: dict[str, Any] = data["payload"]
        sent_at = _time(data.get("sent_at"))
        ignored = WebhookIgnored(f"event {name[:40]}", WebhookResponse.json(_OK))
        if name == "shop_order":
            if payload.get("isTrial") is True:
                raise WebhookIgnored("trial activation", WebhookResponse.json(_OK))
            state = PaymentState.PAID
        elif name in ("shop_order_payment_received", "shop_order_prepaid"):
            state = PaymentState.PROCESSING
        elif name == "shop_order_refunded":
            if str(payload.get("status") or "").strip().lower() != "completed":
                raise ignored  # «initiated»: the money is still with the buyer's bank
            state = PaymentState.REFUNDED
        else:
            if name not in _IGNORED_EVENTS:
                self.ctx.log.warning("tribute: unknown event %r acknowledged", name[:40])
            raise ignored
        external = _external_id(payload.get("uuid"))
        ours = _uuid(payload.get("customerId"))
        if external is None and ours is None:
            raise WebhookRejected("no order id", status=400)
        amount: Decimal | None = None
        currency: str | None = None
        try:
            if state is PaymentState.PAID:
                amount = _charged(payload)
            elif payload.get("amount") is not None:
                amount = _from_minor(payload.get("amount"))
            if payload.get("currency") is not None or state is PaymentState.PAID:
                currency = _currency(payload.get("currency"))
        except ValueError:
            raise WebhookRejected("bad amount", status=400) from None
        # The event's creation time (retries keep it), not the delivery time.
        paid_at = _time(data.get("created_at")) if state is PaymentState.PAID else None
        return ProviderEvent(
            state=state,
            external_id=external,
            payment_id=ours,
            amount=amount,
            currency=currency,
            paid_at=paid_at,
            is_test=False,  # Tribute has no test mode
            signed_at=sent_at,  # inside the signed body; informational (no replay window)
            summary={
                "event": name[:40],
                "uuid": external,
                "customer_id": ours,
                "amount": None if amount is None else str(amount),
                "currency": currency,
                "refund_status": payload.get("status") if name == "shop_order_refunded" else None,
                "transaction_id": payload.get("transactionId"),
            },
        )

    def ack(self, event: ProviderEvent | None) -> WebhookResponse:
        return WebhookResponse.json(_OK)

    # ----------------------------------------------------------------------------------------- status

    async def _order(self, external: str) -> dict[str, Any] | None:
        resp = await self._get(f"/shop/orders/{quote(external, safe='')}")
        if resp.status in (403, 404):
            return None  # another shop's order or unknown: absent from the result
        if resp.status != 200:
            raise self._error(resp, "status")
        data = self._body(resp)
        if not isinstance(data, dict):
            raise ProviderError(_T["bad_answer"], retryable=True, status=resp.status)
        return data

    async def fetch_status(self, ids: Sequence[str]) -> list[ProviderStatus]:
        result: list[ProviderStatus] = []
        for external in dict.fromkeys(ids):
            if not external:
                continue
            order = await self._order(external)
            if order is None:
                continue
            state = ORDER_STATUS.get(str(order.get("status") or "").strip().lower())
            if state is None:
                continue
            amount: Decimal | None = None
            currency: str | None = None
            try:
                amount, currency = _charged(order), _currency(order.get("currency"))
            except ValueError:
                self.ctx.log.warning("tribute: unreadable amount of order %s", external[:40])
            result.append(
                ProviderStatus(
                    state=state,
                    external_id=_external_id(order.get("uuid")) or external,
                    amount=amount,
                    currency=currency,
                    paid_at=_time(order.get("lastPaidTransactionAt")) if state is PaymentState.PAID else None,
                )
            )
        return result

    # ----------------------------------------------------------------------------------------- refund

    async def refund(self, external_id: str, amount_minor: int, currency: str) -> RefundResult:
        """Refund the whole ``sell`` transaction of a paid order (the result arrives as a webhook)."""
        try:
            order = await self._order(external_id)
            if order is None or str(order.get("status") or "").lower() != "paid":
                return RefundResult(False, message=_T["refund_not_paid"])
            try:
                charged = _charged(order)
                order_currency = _currency(order.get("currency"))
            except ValueError:
                return RefundResult(False, message=_T["bad_answer"])
            if order_currency != currency.upper() or charged != Decimal(amount_minor).scaleb(-_EXPONENT):
                return RefundResult(False, message=_T["refund_partial"])
            tx_id = await self._sell_transaction(external_id)
            if tx_id is None:
                return RefundResult(False, message=_T["refund_no_tx"])
            path = f"/shop/orders/{quote(external_id, safe='')}/transactions/{tx_id}/refund"
            resp = await self.ctx.http.request("POST", f"{self._base}{path}", headers=self._headers())
            if resp.status != 200:
                raise self._error(resp, "refund")
            data = self._body(resp)
        except ProviderError as exc:
            return RefundResult(False, message=exc.human)
        ok = isinstance(data, dict) and data.get("success") is True
        return RefundResult(ok, str(tx_id), _T["refund_ok"] if ok else _T["bad_answer"])

    async def _sell_transaction(self, external_id: str) -> int | None:
        start: str | None = None
        for _ in range(20):  # pages; an order has a handful of transactions
            path = f"/shop/orders/{quote(external_id, safe='')}/transactions"
            if start:
                path += f"?startFrom={quote(start, safe='')}"
            resp = await self._get(path)
            if resp.status != 200:
                raise self._error(resp, "transactions")
            data = self._body(resp)
            items = data.get("transactions") if isinstance(data, dict) else None
            for tx in items if isinstance(items, list) else []:
                if not isinstance(tx, dict):
                    continue
                tx_id = tx.get("id")
                kind = str(tx.get("type") or "")
                if (
                    isinstance(tx_id, int)
                    and not isinstance(tx_id, bool)
                    and kind.endswith("sell")
                    and tx.get("isRefunded") is not True
                    and tx.get("isRefundable") is not False
                ):
                    return tx_id
            start = data.get("nextFrom") if isinstance(data, dict) else None
            if not isinstance(start, str) or not start:
                return None
        return None

    # ------------------------------------------------------------------------------------------ probe

    async def test_credentials(self) -> Probe:
        """``GET /shops`` — reads the account's shops, creates nothing."""
        try:
            resp = await self._get("/shops")
        except ProviderError as exc:
            return Probe(False, exc.human)
        if resp.status in (401, 403):
            return Probe(False, _T["bad_key"])
        if resp.status != 200:
            return Probe(False, self._error(resp, "probe").human)
        try:
            shops = self._body(resp)
        except ProviderError as exc:
            return Probe(False, exc.human)
        if not isinstance(shops, list):
            return Probe(False, _T["bad_answer"])
        shops = [s for s in shops if isinstance(s, dict)]
        if not shops:
            return Probe(False, _T["probe_no_shop"])
        wanted = self.config.shop_id
        shop = (
            next((s for s in shops if s.get("id") == wanted), None)
            if wanted
            else min(shops, key=lambda s: s.get("id") if isinstance(s.get("id"), int) else 1 << 62)
        )
        if shop is None:
            return Probe(False, _T["probe_shop_missing"].format(id=wanted))
        message = _T["probe_ok"].format(name=str(shop.get("name") or "?")[:60], id=shop.get("id"))
        if not shop.get("callbackUrl"):
            message += _T["probe_no_hook"]
        return Probe(
            True,
            message,
            {
                "shops": [s.get("id") for s in shops],
                "recurrent": bool(shop.get("recurrent")),
                "only_stars": bool(shop.get("onlyStars")),
                "callback_url_set": bool(shop.get("callbackUrl")),
            },
        )


def _error_code(resp: HttpResponse) -> str | None:
    try:
        body = resp.json()
    except ValueError:
        return None
    code = body.get("error") if isinstance(body, dict) else None
    return code[:60] if isinstance(code, str) and code else None
