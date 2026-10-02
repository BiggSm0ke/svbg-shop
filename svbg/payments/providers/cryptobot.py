"""CryptoBot (Crypto Pay API) — crypto payments for fiat-priced invoices, wave A (07 §4.2, 04 §8).

Ported from Remnashop (MIT, © 2024 snoups) ``src/infrastructure/payment_gateways/cryptopay.py`` and
rewritten for the SvBG SDK — see ``THIRD_PARTY_NOTICES.md``; specification: ``docs/providers/cryptobot.md``.
Imports only :mod:`svbg.sdk`.

* ``createInvoice`` with ``currency_type=fiat``: the price is fixed in the shop currency (``179.00 RUB``), the
  buyer chooses the asset in @CryptoBot; ``payload`` is the opaque payment id only.
* Webhook ``invoice_paid``: ``Crypto-Pay-API-Signature = hex(HMAC_SHA256(key=sha256(token), raw_body))``,
  compared in constant time. No timestamp header: the signed ``request_date`` is reported as ``signed_at``;
  the replay window is not enforced (a replayed body is deduplicated and the payment state is monotonic).
* ``getInvoices`` with ``invoice_ids`` — one request for all pending invoices of the instance (D14 batch).
* Test mode of the instance = **testnet** (``testnet-pay.crypt.bot``, tokens from @CryptoTestnetBot).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
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
    hmac_sha256_hex,
    integer,
    parse_amount,
    parse_timestamp,
    secret,
    text,
    url,
)

__all__ = [
    "MAINNET_URL",
    "TESTNET_URL",
    "CryptoBot",
    "CryptoBotConfig",
    "sign",
]

MAINNET_URL: Final = "https://pay.crypt.bot/api"
TESTNET_URL: Final = "https://testnet-pay.crypt.bot/api"
BATCH_LIMIT: Final = 100
STATUS_MAP: Final[Mapping[str, PaymentState]] = {
    "active": PaymentState.CREATED,
    "paid": PaymentState.PAID,
    "expired": PaymentState.EXPIRED,
}
#: Fiat currencies of Crypto Pay that the shop can price in (intersection with the core's currency table).
FIAT: Final = ("RUB", "USD", "EUR", "GBP", "KZT", "UAH", "BYN", "UZS", "GEL", "AZN", "TRY", "CNY", "AMD")
_DESCRIPTION_MAX: Final = 1024
_ASSETS_RE: Final = r"[A-Za-z0-9]{2,10}(\s*,\s*[A-Za-z0-9]{2,10})*"
_UUID_RE: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

_T: Final = {
    "bad_token": "CryptoBot отклонил API-токен (для тестового режима нужен токен из @CryptoTestnetBot)",
    "rejected": "CryptoBot отклонил запрос: {error}",
    "unavailable": "CryptoBot временно недоступен (HTTP {status})",
    "bad_answer": "CryptoBot вернул непонятный ответ",
    "probe_ok": "Токен принят: приложение «{name}»{net}",
}


class CryptoBotConfig(ConfigModel):
    api_token = secret(
        "API-токен Crypto Pay",
        "Токен приложения Crypto Pay. Им же проверяются подписи вебхуков.",
        where="@CryptoBot → Crypto Pay → Мои приложения → приложение → API-токен (в тестовом режиме — "
        "@CryptoTestnetBot)",
    )
    accepted_assets = text(
        "Принимаемые монеты",
        "Через запятую, например USDT,TON,BTC. Пусто — все монеты, которые поддерживает CryptoBot.",
        required=False,
        advanced=True,
        pattern=_ASSETS_RE,
    )
    invoice_hours = integer(
        "Срок жизни счёта, часов",
        "Сколько часов счёт можно оплатить. После этого он истекает (поздняя оплата всё равно зачтётся).",
        default=24,
        min=1,
        max=744,
        advanced=True,
    )
    base_url = url(
        "Адрес API",
        "Пусто — основная сеть или testnet по тестовому режиму инстанса.",
        required=False,
        advanced=True,
    )


def sign(api_token: str, body: bytes) -> str:
    """``hex(HMAC_SHA256(key=sha256(api_token), body))`` — the Crypto Pay webhook signature."""
    return hmac_sha256_hex(hashlib.sha256(api_token.encode("utf-8")).digest(), body)


def _text(value: Any, limit: int = 200) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if 0 < len(value) <= limit else None


def _when(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        return parse_timestamp(value)
    except ValueError:
        return None


class CryptoBot(PaymentProvider):
    """See module docstring."""

    manifest = Manifest(
        slug="cryptobot",
        title="CryptoBot",
        method_kinds=(MethodKind.CRYPTO,),
        currencies=FIAT,
        config=CryptoBotConfig,
        docs_url="https://help.send.tg/en/articles/10279948-crypto-pay-api",
    )
    capabilities = Capabilities(
        webhook_auth=WebhookAuth.SIGNATURE,
        replay_window_s=None,
        fetch_status=True,
        batch_status=True,
        batch_limit=BATCH_LIMIT,
        refund=False,
        in_chat_invoice=False,
        redirect=True,
        webhook=True,
    )

    # ------------------------------------------------------------------------------------------- HTTP

    @property
    def _base(self) -> str:
        explicit = self.config.base_url
        return str(explicit or (TESTNET_URL if self.ctx.is_test else MAINNET_URL)).rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {"Crypto-Pay-API-Token": self.config.api_token, "Accept": "application/json"}

    async def _call(
        self, method: str, name: str, *, json: Any = None, params: Mapping[str, str] | None = None
    ) -> Any:
        resp = await self.ctx.http.request(
            method, f"{self._base}/{name}", headers=self._headers(), json=json, params=params
        )
        return self._result(resp, name)

    def _result(self, resp: HttpResponse, name: str) -> Any:
        if resp.status == 429 or resp.status >= 500:
            raise ProviderError(
                _T["unavailable"].format(status=resp.status), retryable=True, status=resp.status
            )
        try:
            data = resp.json()
        except ValueError:
            raise ProviderError(_T["bad_answer"], retryable=resp.status >= 500, status=resp.status) from None
        if not isinstance(data, dict):
            raise ProviderError(_T["bad_answer"], status=resp.status)
        if data.get("ok") is True and resp.ok:
            return data.get("result")
        error = data.get("error")
        name_text = ""
        if isinstance(error, Mapping):
            name_text = str(error.get("name") or error.get("code") or "")
        elif isinstance(error, str):
            name_text = error
        if resp.status in (401, 403) or "UNAUTHORIZED" in name_text.upper():
            raise ProviderError(_T["bad_token"], retryable=False, status=resp.status)
        self.ctx.log.warning("cryptobot: %s failed: HTTP %s %s", name, resp.status, name_text[:80])
        raise ProviderError(
            _T["rejected"].format(error=name_text[:80] or f"HTTP {resp.status}"),
            retryable=False,
            status=resp.status,
        )

    # ----------------------------------------------------------------------------------------- create

    async def create(self, intent: PaymentIntent) -> Checkout:
        body: dict[str, Any] = {
            "currency_type": "fiat",
            "fiat": intent.currency,
            "amount": intent.amount_text(),
            "description": (intent.description or "Оплата")[:_DESCRIPTION_MAX],
            "payload": intent.payment_id,  # opaque id only
            "expires_in": int(self.config.invoice_hours) * 3600,
            "allow_comments": False,
        }
        if self.config.accepted_assets:
            body["accepted_assets"] = ",".join(
                a.strip().upper() for a in str(self.config.accepted_assets).split(",") if a.strip()
            )
        if intent.return_url and re.match(r"https://", intent.return_url):
            body["paid_btn_name"] = "callback"
            body["paid_btn_url"] = intent.return_url
        invoice = await self._call("POST", "createInvoice", json=body)
        if not isinstance(invoice, Mapping):
            raise ProviderError(_T["bad_answer"])
        external = _text(invoice.get("invoice_id"))
        pay_url = invoice.get("bot_invoice_url") or invoice.get("pay_url")
        if external is None or not isinstance(pay_url, str) or not re.match(r"https?://", pay_url):
            raise ProviderError(_T["bad_answer"])
        return Checkout(
            kind="url",
            external_id=external,
            pay_url=pay_url,
            expires_at=_when(invoice.get("expiration_date")),
        )

    # ---------------------------------------------------------------------------------------- webhook

    async def parse_webhook(self, req: WebhookRequest) -> ProviderEvent:
        signature = (req.header("Crypto-Pay-API-Signature") or "").strip().lower()
        if not signature:
            raise WebhookRejected("missing signature", status=401)
        if not constant_time_equal(sign(self.config.api_token, req.body), signature):
            raise WebhookRejected("bad signature", status=401)
        data = req.json()
        if not isinstance(data, dict):
            raise WebhookRejected("malformed", status=400)
        update_type = data.get("update_type")
        if update_type != "invoice_paid":
            raise WebhookIgnored(f"update {str(update_type)[:40]}")
        invoice = data.get("payload")
        if not isinstance(invoice, Mapping):
            raise WebhookRejected("malformed", status=400)
        status = self._invoice_status(invoice)
        if status is None:
            raise WebhookIgnored("unknown invoice status")
        return ProviderEvent(
            state=status.state,
            external_id=status.external_id,
            payment_id=status.payment_id,
            amount=status.amount,
            currency=status.currency,
            paid_at=status.paid_at,
            is_test=status.is_test,
            signed_at=_when(data.get("request_date")),
            summary={
                "update_id": data.get("update_id") if isinstance(data.get("update_id"), int) else None,
                "invoice_id": status.external_id,
                "status": status.state.value,
                "amount": None if status.amount is None else str(status.amount),
                "currency": status.currency,
                "paid_asset": _text(invoice.get("paid_asset"), 16),
            },
        )

    def _invoice_status(self, invoice: Mapping[str, Any]) -> ProviderStatus | None:
        """One invoice object → status; ``None`` for an unknown status. ``WebhookRejected`` (400) when the
        invoice cannot be identified or its amount is unreadable."""
        state = STATUS_MAP.get(str(invoice.get("status") or "").strip().lower())
        if state is None:
            return None
        external = _text(invoice.get("invoice_id"))
        payload = _text(invoice.get("payload"))
        if external is None:
            raise WebhookRejected("no invoice id", status=400)
        if invoice.get("currency_type") == "crypto":  # legacy invoices priced in an asset
            raw_amount, currency = invoice.get("amount"), invoice.get("asset")
        else:
            raw_amount, currency = invoice.get("amount"), invoice.get("fiat")
        amount: Decimal | None = None
        if raw_amount not in (None, ""):
            try:
                amount = parse_amount(raw_amount)
            except (TypeError, ValueError):
                raise WebhookRejected("bad amount", status=400) from None
        cur = _text(currency, 8)
        if cur is not None and not re.fullmatch(r"[A-Za-z]{3,8}", cur):
            raise WebhookRejected("bad currency", status=400)
        return ProviderStatus(
            state=state,
            external_id=external,
            payment_id=payload if payload and _UUID_RE.fullmatch(payload) else None,
            amount=amount,
            currency=cur,
            paid_at=_when(invoice.get("paid_at")) if state is PaymentState.PAID else None,
            is_test=self.ctx.is_test,
        )

    # ----------------------------------------------------------------------------------------- status

    async def fetch_status(self, ids: Sequence[str]) -> list[ProviderStatus]:
        numeric = [i for i in dict.fromkeys(ids) if i and i.isdigit()]
        result: list[ProviderStatus] = []
        for start in range(0, len(numeric), BATCH_LIMIT):
            chunk = numeric[start : start + BATCH_LIMIT]
            answer = await self._call(
                "GET", "getInvoices", params={"invoice_ids": ",".join(chunk), "count": str(len(chunk))}
            )
            items = answer.get("items") if isinstance(answer, Mapping) else answer
            if not isinstance(items, list):
                raise ProviderError(_T["bad_answer"], retryable=True)
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                try:
                    status = self._invoice_status(item)
                except WebhookRejected:
                    self.ctx.log.warning("cryptobot: unreadable invoice skipped")
                    continue
                if status is not None and status.external_id in chunk:
                    result.append(status)
        return result

    # ------------------------------------------------------------------------------------------ probe

    async def test_credentials(self) -> Probe:
        try:
            me = await self._call("GET", "getMe")
        except ProviderError as exc:
            if exc.retryable:
                raise
            return Probe(False, exc.human)
        name = str(me.get("name") or "без названия") if isinstance(me, Mapping) else "без названия"
        net = " (testnet)" if self._base == TESTNET_URL else ""
        return Probe(True, _T["probe_ok"].format(name=name[:64], net=net))
