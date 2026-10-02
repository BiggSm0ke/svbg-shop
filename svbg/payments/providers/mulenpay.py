"""MulenPay — card / SBP acquiring with 54-FZ receipts (07 §4.2).

Specification: ``docs/providers/mulenpay.md`` (official documentation <https://docs.mulenpay.com/> and the
vendor's OpenAPI ``api.yml`` in github.com/platem9/mulenpay.ru, checked 2026-10-02). Ported from
Remnashop (MIT, © 2024 snoups) ``payment_gateways/mulen_pay.py`` and corrected (see
``THIRD_PARTY_NOTICES.md``). Imports only :mod:`svbg.sdk` and the
standard library.

* Requests carry ``Authorization: Bearer <API key>``; base ``https://mulenpay.ru/api``.
* ``POST /v2/payments`` creates a payment; ``sign = sha1(currency + amount + shopId + secret_key)`` with the
  amount as the exact string sent (``"179.00"``); ``uuid`` carries **only** our opaque payment id; ``items``
  carry one receipt line with ``vat_code`` / ``payment_subject`` / ``payment_mode`` from the settings.
* ``GET /v2/payments/{id}`` reads the status (``0`` created … ``3`` processed); no batch API.
* Callbacks (``{id, amount, currency, uuid, payment_status}``) are **not signed** by the documented
  protocol. A ``sign`` field, when present, is checked (wrong → 401), but no callback is trusted either way:
  the scheme is declared weak (``none``) and the core re-reads every reported payment with
  ``fetch_status`` before anything changes (07 §4.2). The callback URL itself carries the instance token.
"""

from __future__ import annotations

import hashlib
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
    WebhookAuth,
    WebhookIgnored,
    WebhookRejected,
    WebhookRequest,
    WebhookResponse,
    constant_time_equal,
    integer,
    parse_amount,
    secret,
    url,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "STATUS_CODES",
    "MulenPay",
    "MulenPayConfig",
    "sign",
]

DEFAULT_BASE_URL: Final = "https://mulenpay.ru/api"
#: ``payment.status`` of ``GET /v2/payments/{id}`` → SDK state. 5/6 are «Холд» (only with holdTime, which we
#: never send): the money is not captured yet.
STATUS_CODES: Final[Mapping[int, PaymentState]] = {
    0: PaymentState.CREATED,
    1: PaymentState.PROCESSING,
    2: PaymentState.CANCELED,
    3: PaymentState.PAID,
    4: PaymentState.FAILED,
    5: PaymentState.PROCESSING,
    6: PaymentState.PROCESSING,
}
#: ``payment_status`` of a callback.
CALLBACK_STATUS: Final[Mapping[str, PaymentState]] = {
    "success": PaymentState.PAID,
    "cancel": PaymentState.CANCELED,
}
_DESCRIPTION_MAX: Final = 255
_ID_MAX: Final = 200
_UUID_RE: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

_T: Final = {
    "bad_key": "MulenPay отклонила API-ключ — проверьте его в кабинете MulenPay",
    "bad_shop": "MulenPay не нашла магазин с этим ID у владельца ключа — проверьте «ID магазина»",
    "rejected": "MulenPay отклонила запрос (HTTP {status})",
    "invalid": "MulenPay отклонила параметры платежа (HTTP {status}) — проверьте ID магазина и "
    "секретный ключ",
    "unavailable": "MulenPay временно недоступна (HTTP {status})",
    "bad_answer": "MulenPay вернула непонятный ответ",
    "currency": "MulenPay принимает только рубли",
    "probe_ok": "Ключ и магазин приняты. Секретный ключ подписи проверится при первом платеже",
    "probe_not_mulen": "по адресу API отвечает не MulenPay — проверьте «Адрес API»",
}


class MulenPayConfig(ConfigModel):
    api_key = secret(
        "API-ключ",
        "Ключ для запросов к API MulenPay (заголовок Authorization: Bearer).",
        where="кабинет MulenPay (mulenpay.ru) → Магазины → ваш магазин → Настройки → «API-ключ»",
    )
    secret_key = secret(
        "Секретный ключ",
        "Им подписывается создание платежа: sign = sha1(валюта + сумма + ID магазина + секретный ключ).",
        where="кабинет MulenPay → Магазины → ваш магазин → Настройки → «Секретный ключ»",
    )
    shop_id = integer(
        "ID магазина",
        "Числовой идентификатор магазина в MulenPay.",
        where="кабинет MulenPay → Магазины → столбец «ID» (или адресная строка страницы магазина)",
        min=1,
    )
    vat_code = integer(
        "Код НДС в чеке",
        "vat_code строки чека по 54-ФЗ: 0 — без НДС, 1 — 0%, 2 — 10%, 6 — 20%, 4/7 — расчётные 10/110 и "
        "20/120 (коды 3 и 5 — устаревшие 18%).",
        where="уточните у бухгалтера; для УСН и самозанятых обычно 0 (без НДС)",
        default=0,
        required=False,
        min=0,
        max=7,
    )
    payment_subject = integer(
        "Предмет расчёта",
        "payment_subject строки чека: 4 — услуга (подписка на VPN), 1 — товар, 10 — платёж.",
        where="уточните у бухгалтера; для подписки обычно 4 (услуга)",
        default=4,
        required=False,
        min=1,
        max=26,
        advanced=True,
    )
    payment_mode = integer(
        "Способ расчёта",
        "payment_mode строки чека: 4 — полный расчёт, 1 — полная предоплата.",
        where="уточните у бухгалтера; обычно 4 (полный расчёт)",
        default=4,
        required=False,
        min=1,
        max=7,
        advanced=True,
    )
    base_url = url(
        "Адрес API",
        "Базовый адрес API MulenPay. Меняйте, только если MulenPay сообщила другой.",
        where="документация MulenPay (docs.mulenpay.com) → servers",
        default=DEFAULT_BASE_URL,
        required=False,
        advanced=True,
    )


def sign(currency: str, amount: str, shop_id: int | str, secret_key: str) -> str:
    """``hex(sha1(currency + amount + shopId + secret_key))`` — the documented signature of a payment."""
    return hashlib.sha1(f"{currency}{amount}{shop_id}{secret_key}".encode()).hexdigest()  # noqa: S324


def _id(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if 0 < len(value) <= _ID_MAX else None


def _our_payment_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower()
    return candidate if _UUID_RE.fullmatch(candidate) else None


def _status_code(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


class MulenPay(PaymentProvider):
    """See module docstring."""

    manifest = Manifest(
        slug="mulenpay",
        title="MulenPay",
        method_kinds=(MethodKind.CARD, MethodKind.SBP),
        currencies=("RUB",),
        config=MulenPayConfig,
        docs_url="https://docs.mulenpay.com/",
    )
    capabilities = Capabilities(
        webhook_auth=WebhookAuth.NONE,  # callbacks are unsigned: every one is re-read with fetch_status
        replay_window_s=None,
        fetch_status=True,
        batch_status=False,
        refund=False,
        receipt_54fz=True,
        in_chat_invoice=False,
        redirect=True,
        webhook=True,
    )

    # ------------------------------------------------------------------------------------------- HTTP

    @property
    def _base(self) -> str:
        return str(self.config.base_url or DEFAULT_BASE_URL).rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.config.api_key}", "Accept": "application/json"}

    def _raise_for(self, resp: HttpResponse, what: str) -> None:
        if resp.status == 401:
            raise ProviderError(_T["bad_key"], retryable=False, status=resp.status)
        if resp.status == 403:
            raise ProviderError(_T["bad_shop"], retryable=False, status=resp.status)
        if resp.status == 429 or resp.status >= 500:
            raise ProviderError(
                _T["unavailable"].format(status=resp.status), retryable=True, status=resp.status
            )
        self.ctx.log.warning("mulenpay: %s answered HTTP %s", what, resp.status)
        key = "invalid" if resp.status in (400, 422) else "rejected"
        raise ProviderError(_T[key].format(status=resp.status), retryable=False, status=resp.status)

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
        if intent.currency.upper() != "RUB":
            raise ProviderError(_T["currency"], retryable=False)
        currency = "rub"
        amount = intent.amount_text()  # "179.00": the exact string that is signed
        shop_id = int(self.config.shop_id)
        description = (intent.description or "Оплата")[:_DESCRIPTION_MAX]
        body: dict[str, Any] = {
            "currency": currency,
            "amount": amount,
            "uuid": intent.payment_id,  # opaque: never a Telegram id or a subscription link
            "shopId": shop_id,
            "description": description,
            "language": "ru",
            "items": [
                {
                    "description": description,
                    "quantity": 1,
                    "price": float(amount),  # a JSON number, as in the OpenAPI schema
                    "vat_code": int(self.config.vat_code),
                    "payment_subject": int(self.config.payment_subject),
                    "payment_mode": int(self.config.payment_mode),
                    "measurement_unit": 0,
                }
            ],
            "sign": sign(currency, amount, shop_id, self.config.secret_key),
        }
        resp = await self.ctx.http.request(
            "POST", f"{self._base}/v2/payments", headers=self._headers(), json=body
        )
        if resp.status not in (200, 201):
            self._raise_for(resp, "create")
        data = self._json(resp)
        pay_url = data.get("paymentUrl")
        external = _id(data.get("id"))
        if (
            data.get("success") is not True
            or external is None
            or not isinstance(pay_url, str)
            or not re.match(r"https?://", pay_url)
        ):
            self.ctx.log.warning("mulenpay: create answered without success/id/paymentUrl")
            raise ProviderError(_T["bad_answer"], retryable=False, status=resp.status)
        return Checkout(kind="url", external_id=external, pay_url=pay_url)

    # ---------------------------------------------------------------------------------------- webhook

    def _sign_matches(self, data: Mapping[str, Any], given: str) -> bool:
        """The callback ``sign`` against the payment formula in every spelling MulenPay could have used for
        currency (``rub`` / ``RUB``) and amount (``100.5`` / ``100.50``)."""
        try:
            amount = parse_amount(data.get("amount"))
        except (TypeError, ValueError):
            return False
        raw = data.get("amount")
        amounts = {f"{amount:.2f}", str(raw) if isinstance(raw, str | int) else str(amount)}
        if isinstance(raw, float):
            amounts.add(repr(raw))
        cur = str(data.get("currency") or "rub").strip()
        currencies = {cur, cur.lower(), cur.upper()}
        shop_id = int(self.config.shop_id)
        matched = False
        for c in sorted(currencies):
            for a in sorted(amounts):
                matched |= constant_time_equal(sign(c, a, shop_id, self.config.secret_key), given)
        return matched

    async def parse_webhook(self, req: WebhookRequest) -> ProviderEvent:
        data = req.json()
        if not isinstance(data, dict):
            raise WebhookRejected("malformed", status=400)
        given = data.get("sign")
        signed = isinstance(given, str) and bool(given.strip())
        if given not in (None, "") and not (signed and self._sign_matches(data, str(given).strip().lower())):
            raise WebhookRejected("bad signature", status=401)
        raw_status = str(data.get("payment_status") or "").strip().lower()
        state = CALLBACK_STATUS.get(raw_status)
        if state is None:
            self.ctx.log.warning("mulenpay: callback with unknown status %r ignored", raw_status[:40])
            raise WebhookIgnored("unknown status")
        status = self._status(data, state, malformed=WebhookRejected)
        return ProviderEvent(
            state=status.state,
            external_id=status.external_id,
            payment_id=status.payment_id,
            amount=status.amount,
            currency=status.currency,
            is_test=self.ctx.is_test,
            signed_at=None,
            summary={
                "id": status.external_id,
                "uuid": status.payment_id,
                "status": raw_status,
                "amount": None if status.amount is None else str(status.amount),
                "currency": status.currency,
                "signed": signed,
            },
        )

    def _status(
        self,
        data: Mapping[str, Any],
        state: PaymentState,
        *,
        malformed: type[Exception],
        external_id: str | None = None,
    ) -> ProviderStatus:
        external = _id(data.get("id")) or external_id
        ours = _our_payment_id(data.get("uuid"))
        if external is None and ours is None:
            raise _malformed(malformed, "no payment id")
        amount: Decimal | None = None
        if data.get("amount") not in (None, ""):
            try:
                amount = parse_amount(data["amount"])
            except (TypeError, ValueError):
                raise _malformed(malformed, "bad amount") from None
        currency = data.get("currency") or "RUB"
        if not isinstance(currency, str) or not re.fullmatch(r"[A-Za-z]{3}", currency.strip()):
            raise _malformed(malformed, "bad currency")
        return ProviderStatus(
            state=state,
            external_id=external,
            payment_id=ours,
            amount=amount,
            currency=currency.strip().upper(),
            is_test=self.ctx.is_test,
        )

    def ack(self, event: ProviderEvent | None) -> WebhookResponse:
        return WebhookResponse.json({"success": True})

    # ----------------------------------------------------------------------------------------- status

    async def fetch_status(self, ids: Sequence[str]) -> list[ProviderStatus]:
        result: list[ProviderStatus] = []
        for external in dict.fromkeys(ids):
            if not external:
                continue
            resp = await self.ctx.http.request(
                "GET", f"{self._base}/v2/payments/{quote(external, safe='')}", headers=self._headers()
            )
            if resp.status in (404, 422):
                continue  # unknown to MulenPay: absent from the result
            if resp.status != 200:
                self._raise_for(resp, "status")
            payment = self._json(resp).get("payment")
            if not isinstance(payment, dict):
                continue
            code = _status_code(payment.get("status"))
            state = STATUS_CODES.get(code) if code is not None else None
            if state is None:
                self.ctx.log.warning("mulenpay: unknown status code %r of %s", code, external[:40])
                continue
            reported = _id(payment.get("id"))
            if reported is not None and reported != external:
                self.ctx.log.warning("mulenpay: status of %s answered for another id", external[:40])
                continue
            try:
                result.append(self._status(payment, state, malformed=ProviderError, external_id=external))
            except ProviderError:
                self.ctx.log.warning("mulenpay: unreadable status of %s skipped", external[:40])
        return result

    # ------------------------------------------------------------------------------------------ probe

    async def test_credentials(self) -> Probe:
        """No side effects: reads the shop's balances — checks the API key and that the shop belongs to it.
        The secret key can only be checked by creating a payment, which a probe never does."""
        resp = await self.ctx.http.request(
            "GET", f"{self._base}/v2/shops/{int(self.config.shop_id)}/balances", headers=self._headers()
        )
        if resp.status == 401:
            return Probe(False, _T["bad_key"])
        if resp.status in (403, 404):
            return Probe(False, _T["bad_shop"])
        if resp.status == 429 or resp.status >= 500:
            return Probe(False, _T["unavailable"].format(status=resp.status))
        try:
            data = resp.json()
        except ValueError:
            return Probe(False, _T["probe_not_mulen"])
        if resp.status != 200 or not isinstance(data, dict) or data.get("success") is False:
            return Probe(False, _T["rejected"].format(status=resp.status))
        return Probe(True, _T["probe_ok"])


def _malformed(kind: type[Exception], reason: str) -> Exception:
    if kind is WebhookRejected:
        return WebhookRejected(reason, status=400)
    return ProviderError(_T["bad_answer"], retryable=False)
