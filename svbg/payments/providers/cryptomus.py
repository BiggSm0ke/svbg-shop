"""Cryptomus — crypto payments for invoices priced in the shop currency (07 §4.2, 04 §8).

Ported from Remnashop (MIT, © 2024 snoups) ``src/infrastructure/payment_gateways/cryptomus.py`` and
rewritten for the SvBG SDK — see ``THIRD_PARTY_NOTICES.md``; specification: ``docs/providers/cryptomus.md``.
Imports only :mod:`svbg.sdk`. Heleket (``heleket.py``) speaks the same protocol; the two modules are kept
separate on purpose (a plugin may import only the SDK).

* Every API call: headers ``merchant: <uuid>`` and ``sign = md5(base64(raw_body) + payment_api_key)``; the
  signature is computed over the exact bytes sent (an empty body signs the empty string).
* ``POST /v1/payment`` — the invoice; ``order_id`` is the opaque payment id only, ``is_payment_multiple`` is
  off so an underpayment ends as ``wrong_amount``.
* Webhook: ``sign`` inside the JSON body = ``md5(base64(json_encode(body without "sign",
  JSON_UNESCAPED_UNICODE)) + key)`` — PHP encoding, i.e. ``/`` escaped as ``\\/``. Checked against (1) the raw
  body with the ``"sign"`` member cut out textually and (2) a PHP-exact re-encoding of the parsed body (number
  literals kept verbatim), in constant time. Remnashop re-encoded with Python's default (unescaped ``/``,
  ASCII-escaped Unicode) — that fails on any slash or non-ASCII text — and trusted a hard-coded IP; fixed
  here (the IP check is optional, allowed_ips).
* Statuses: ``paid``/``paid_over`` → paid for the invoice amount; ``wrong_amount`` → paid with what the
  client actually sent (``payment_amount`` + ``payer_currency``) so the core turns it into ``mismatch``;
  ``cancel`` → expired (a late payment still wins); ``fail``/``system_fail`` → failed; ``refund_paid`` →
  refunded; the rest are intermediate.
* ``POST /v1/payment/info`` per invoice (no batch method exists) for ``fetch_status``.
* No sandbox: ``/v1/test-webhook/payment`` webhooks are signed with the live key and carry a random
  ``order_id`` (≤ 32 chars, never our UUID) — events whose ``order_id`` is not a payment id of the bot are
  reported as ``is_test`` and rejected by a live instance.
"""

from __future__ import annotations

import base64
import hashlib
import json
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
    integer,
    parse_amount,
    parse_timestamp,
    secret,
    text,
    url,
)

__all__ = [
    "API_URL",
    "WEBHOOK_IP",
    "Cryptomus",
    "CryptomusConfig",
    "php_json",
    "sign",
    "verify_webhook_body",
]

BRAND: Final = "Cryptomus"
API_URL: Final = "https://api.cryptomus.com"
WEBHOOK_IP: Final = "91.227.144.54"
DOCS_URL: Final = "https://doc.cryptomus.com/merchant-api/payments/creating-invoice"
ORDER_ID_MAX: Final = 100

#: Provider status → state. ``wrong_amount`` is handled separately (paid with the actual amount → mismatch);
#: ``refund_process`` / ``refund_fail`` (the payment stays paid) are not reported.
STATUS_MAP: Final[Mapping[str, PaymentState]] = {
    "check": PaymentState.CREATED,
    "process": PaymentState.PROCESSING,
    "confirm_check": PaymentState.PROCESSING,
    "wrong_amount_waiting": PaymentState.PROCESSING,
    "locked": PaymentState.PROCESSING,
    "paid": PaymentState.PAID,
    "paid_over": PaymentState.PAID,
    "wrong_amount": PaymentState.PAID,
    "cancel": PaymentState.EXPIRED,
    "fail": PaymentState.FAILED,
    "system_fail": PaymentState.FAILED,
    "refund_paid": PaymentState.REFUNDED,
}
#: Invoice currencies the shop can price in (the core's currency table ∩ what the provider accepts).
CURRENCIES: Final = ("RUB", "USD", "EUR", "USDT", "USDC")

_UUID_RE: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_SIGN_RE: Final = re.compile(r"[0-9a-fA-F]{32}")
_SIGN_TAIL_RE: Final = re.compile(r'\s*,\s*"sign"\s*:\s*"[0-9a-fA-F]{32}"')
_SIGN_HEAD_RE: Final = re.compile(r'"sign"\s*:\s*"[0-9a-fA-F]{32}"\s*,\s*')
_COIN_RE: Final = r"[A-Za-z0-9]{2,10}(:[A-Za-z0-9_-]{2,20})?"
_COINS_RE: Final = rf"{_COIN_RE}(\s*,\s*{_COIN_RE})*"
_CURRENCY_RE: Final = re.compile(r"[A-Za-z0-9]{2,10}")
_IPS_RE: Final = r"[0-9A-Fa-f.:]+(\s*,\s*[0-9A-Fa-f.:]+)*"
_URL_MIN, _URL_MAX = 6, 255

_T: Final = {
    "bad_keys": BRAND + " отклонил ключи: проверьте UUID мерчанта и платёжный API-ключ (не ключ выплат)",
    "rejected": BRAND + " отклонил запрос: {error}",
    "unavailable": BRAND + " временно недоступен (HTTP {status})",
    "bad_answer": BRAND + " вернул непонятный ответ",
    "probe_ok": "Ключи приняты: доступно способов оплаты — {n}",
}


class CryptomusConfig(ConfigModel):
    merchant_id = text(
        "UUID мерчанта",
        "Идентификатор мерчанта, уходит в заголовке merchant каждого запроса.",
        where="Личный кабинет Cryptomus → Бизнес → ваш мерчант → Настройки → «Merchant ID» (UUID)",
        pattern=r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}",
    )
    api_key = secret(
        "Платёжный API-ключ",
        "Ключ для приёма платежей (Payment API key). Им подписываются запросы и проверяются вебхуки. "
        "Ключ выплат (Payout) не подойдёт.",
        where="Личный кабинет Cryptomus → Бизнес → мерчант → Настройки → API-интеграция → «Payment API key» "
        "(сгенерировать, если ещё нет)",
    )
    invoice_minutes = integer(
        "Срок жизни счёта, минут",
        "Сколько минут счёт можно оплатить (5–720). Поздняя оплата после истечения всё равно зачтётся.",
        default=60,
        min=5,
        max=720,
        advanced=True,
    )
    accepted_coins = text(
        "Принимаемые монеты",
        "Через запятую, при желании с сетью: USDT:tron, TON, BTC. Пусто — все монеты мерчанта.",
        required=False,
        advanced=True,
        pattern=_COINS_RE,
    )
    subtract = integer(
        "Комиссия на покупателе, %",
        "Какую часть комиссии Cryptomus доплачивает покупатель (0–100).",
        where="Тарифы — в личном кабинете Cryptomus; 0 — комиссию платит магазин",
        default=0,
        min=0,
        max=100,
        advanced=True,
    )
    allowed_ips = text(
        "IP-адреса вебхуков",
        "Через запятую. Пусто — без проверки адреса (подпись проверяется всегда). Включайте, только если бот "
        "видит настоящий адрес отправителя (без обратного прокси).",
        where=f"Официальный адрес вебхуков Cryptomus: {WEBHOOK_IP} (страница «Webhook» документации)",
        required=False,
        advanced=True,
        pattern=_IPS_RE,
    )
    base_url = url(
        "Адрес API",
        f"Пусто — {API_URL}.",
        required=False,
        advanced=True,
    )


# --------------------------------------------------------------------------------------------- signing


def sign(api_key: str, payload: bytes) -> str:
    """``md5(base64(payload) + api_key)`` as lowercase hex — the request and webhook signature."""
    raw = base64.b64encode(payload) + api_key.encode("utf-8")
    return hashlib.md5(raw).hexdigest()  # noqa: S324 - mandated by the provider's protocol


class _Number(str):
    """A JSON number kept as its literal text (re-encoded verbatim)."""

    __slots__ = ()


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _constant(name: str) -> Any:
    raise ValueError(f"non-standard JSON constant {name}")


def _load(body: bytes) -> Any:
    return json.loads(
        body.decode("utf-8"),
        object_pairs_hook=_pairs,
        parse_float=_Number,
        parse_int=_Number,
        parse_constant=_constant,
    )


_ESCAPES: Final = {
    '"': '\\"',
    "\\": "\\\\",
    "/": "\\/",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
    "\u2028": "\\u2028",
    "\u2029": "\\u2029",
}


def _php_string(value: str) -> str:
    out = ['"']
    for ch in value:
        esc = _ESCAPES.get(ch)
        if esc is not None:
            out.append(esc)
        elif ord(ch) < 0x20:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def php_json(value: Any) -> str:
    """``json_encode($value, JSON_UNESCAPED_UNICODE)`` of PHP for decoded JSON: compact, ``/`` → ``\\/``,
    Unicode as is (U+2028/U+2029 escaped), an empty or ``0..n-1``-keyed object as a list (PHP arrays)."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, _Number):
        return str(value)
    if isinstance(value, str):
        return _php_string(value)
    if isinstance(value, int | Decimal):
        return str(value)
    if isinstance(value, Mapping):
        if list(value) == [str(i) for i in range(len(value))]:
            return "[" + ",".join(php_json(v) for v in value.values()) + "]"
        return "{" + ",".join(f"{_php_string(str(k))}:{php_json(v)}" for k, v in value.items()) + "}"
    if isinstance(value, list | tuple):
        return "[" + ",".join(php_json(v) for v in value) + "]"
    raise TypeError(f"cannot encode {type(value).__name__}")


def _without_sign_member(text_body: str) -> str | None:
    """The raw body with its single top-level ``"sign"`` member cut out (``None`` when ambiguous)."""
    tail = list(_SIGN_TAIL_RE.finditer(text_body))
    head = list(_SIGN_HEAD_RE.finditer(text_body))
    if len(tail) + len(head) != 1:
        return None
    match = tail[0] if tail else head[0]
    return text_body[: match.start()] + text_body[match.end() :]


def verify_webhook_body(api_key: str, body: bytes) -> dict[str, Any]:
    """Authenticate a webhook body and return its fields without ``sign``. ``WebhookRejected``: 400 for a
    body that is not a JSON object, 401 for a missing or wrong signature."""
    try:
        data = _load(body)
    except (UnicodeDecodeError, ValueError):
        raise WebhookRejected("malformed", status=400) from None
    if not isinstance(data, dict):
        raise WebhookRejected("malformed", status=400)
    signature = data.pop("sign", None)
    if not isinstance(signature, str) or not _SIGN_RE.fullmatch(signature):
        raise WebhookRejected("missing signature", status=401)
    signature = signature.lower()
    candidates = [php_json(data).encode("utf-8")]
    stripped = _without_sign_member(body.decode("utf-8"))
    if stripped is not None:
        candidates.append(stripped.encode("utf-8"))
    ok = False
    for candidate in candidates:
        ok |= constant_time_equal(sign(api_key, candidate), signature)
    if not ok:
        raise WebhookRejected("bad signature", status=401)
    return data


# --------------------------------------------------------------------------------------------- helpers


def _text(value: Any, limit: int = 200) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if 0 < len(value) <= limit else None


def _amount(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return parse_amount(str(value) if isinstance(value, _Number) else value)
    except (TypeError, ValueError):
        raise WebhookRejected("bad amount", status=400) from None


def _currency(value: Any) -> str | None:
    cur = _text(value, 10)
    if cur is not None and not _CURRENCY_RE.fullmatch(cur):
        raise WebhookRejected("bad currency", status=400)
    return cur.upper() if cur else None


def _when(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        return parse_timestamp(str(value) if isinstance(value, _Number) else value)
    except ValueError:
        return None


def _fits_url(value: str | None) -> bool:
    return bool(value and re.match(r"https?://", value) and _URL_MIN <= len(value) <= _URL_MAX)


def _first_error(data: Mapping[str, Any]) -> str:
    message = data.get("message")
    if isinstance(message, str) and message.strip():
        return message.strip()[:120]
    errors = data.get("errors")
    if isinstance(errors, Mapping):
        for field_name, reasons in errors.items():
            reason = reasons[0] if isinstance(reasons, list) and reasons else reasons
            return f"{field_name}: {reason}"[:120]
    return ""


class Cryptomus(PaymentProvider):
    """See module docstring."""

    manifest = Manifest(
        slug="cryptomus",
        title="Cryptomus",
        method_kinds=(MethodKind.CRYPTO,),
        currencies=CURRENCIES,
        config=CryptomusConfig,
        docs_url=DOCS_URL,
    )
    capabilities = Capabilities(
        webhook_auth=WebhookAuth.SIGNATURE,
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
        return str(self.config.base_url or API_URL).rstrip("/")

    async def _call(self, path: str, payload: Mapping[str, Any] | None, *, missing_ok: bool = False) -> Any:
        """POST a signed request; returns ``result`` (``None`` for a missing invoice when ``missing_ok``)."""
        body = b"" if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = {
            "merchant": self.config.merchant_id,
            "sign": sign(self.config.api_key, body),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        resp = await self.ctx.http.request("POST", f"{self._base}{path}", headers=headers, data=body)
        return self._result(resp, path, missing_ok=missing_ok)

    def _result(self, resp: HttpResponse, path: str, *, missing_ok: bool) -> Any:
        if resp.status == 429 or resp.status >= 500:
            raise ProviderError(
                _T["unavailable"].format(status=resp.status), retryable=True, status=resp.status
            )
        if resp.status in (401, 403):
            raise ProviderError(_T["bad_keys"], retryable=False, status=resp.status)
        try:
            data = resp.json()
        except ValueError:
            raise ProviderError(_T["bad_answer"], status=resp.status) from None
        if not isinstance(data, dict):
            raise ProviderError(_T["bad_answer"], status=resp.status)
        if resp.ok and data.get("state") == 0:
            return data.get("result")
        if missing_ok and resp.status in (404, 422):
            return None
        error = _first_error(data)
        self.ctx.log.warning("%s: %s failed: HTTP %s %s", self.manifest.slug, path, resp.status, error[:80])
        raise ProviderError(
            _T["rejected"].format(error=error or f"HTTP {resp.status}"), retryable=False, status=resp.status
        )

    # ----------------------------------------------------------------------------------------- create

    async def create(self, intent: PaymentIntent) -> Checkout:
        if len(intent.payment_id) > ORDER_ID_MAX:
            raise ProviderError(_T["rejected"].format(error="order_id too long"))
        body: dict[str, Any] = {
            "amount": intent.amount_text(),
            "currency": intent.currency,
            "order_id": intent.payment_id,  # opaque id only
            "lifetime": int(self.config.invoice_minutes) * 60,
            "is_payment_multiple": False,
        }
        if _fits_url(self.ctx.webhook_url):
            body["url_callback"] = self.ctx.webhook_url
        if _fits_url(intent.return_url):
            body["url_return"] = intent.return_url
            body["url_success"] = intent.return_url
        if self.config.subtract:
            body["subtract"] = int(self.config.subtract)
        if self.config.accepted_coins:
            coins: list[dict[str, str]] = []
            for item in str(self.config.accepted_coins).split(","):
                coin, _, network = item.strip().partition(":")
                entry = {"currency": coin.upper()}
                if network:
                    entry["network"] = network.lower()
                coins.append(entry)
            body["currencies"] = coins
        invoice = await self._call("/v1/payment", body)
        if not isinstance(invoice, Mapping):
            raise ProviderError(_T["bad_answer"])
        external = _text(invoice.get("uuid"))
        pay_url = invoice.get("url")
        if external is None or not isinstance(pay_url, str) or not re.match(r"https?://", pay_url):
            raise ProviderError(_T["bad_answer"])
        return Checkout(
            kind="url", external_id=external, pay_url=pay_url, expires_at=_when(invoice.get("expired_at"))
        )

    # ---------------------------------------------------------------------------------------- webhook

    def _check_ip(self, req: WebhookRequest) -> None:
        allowed = self.config.allowed_ips
        if not allowed:
            return
        ips = {ip.strip() for ip in str(allowed).split(",") if ip.strip()}
        if (req.remote or "").strip() not in ips:
            raise WebhookRejected("source address not allowed", status=403)

    async def parse_webhook(self, req: WebhookRequest) -> ProviderEvent:
        self._check_ip(req)
        data = verify_webhook_body(self.config.api_key, req.body)
        kind = data.get("type")
        if kind not in (None, "payment"):
            raise WebhookIgnored(f"type {str(kind)[:20]}")
        status = self._invoice_status(data)
        if status is None:
            raise WebhookIgnored(f"status {str(data.get('status'))[:30]}")
        return ProviderEvent(
            state=status.state,
            external_id=status.external_id,
            payment_id=status.payment_id,
            amount=status.amount,
            currency=status.currency,
            paid_at=status.paid_at,
            is_test=status.is_test,
            signed_at=None,  # nothing time-related is signed
            summary={
                "uuid": status.external_id,
                "status": _text(data.get("status"), 30),
                "is_final": data.get("is_final") if isinstance(data.get("is_final"), bool) else None,
                "amount": _text(data.get("amount"), 40),
                "currency": _text(data.get("currency"), 10),
                "payment_amount": _text(data.get("payment_amount"), 40),
                "payer_currency": _text(data.get("payer_currency"), 10),
                "network": _text(data.get("network"), 30),
                "txid": _text(data.get("txid"), 128),
            },
        )

    def _invoice_status(self, invoice: Mapping[str, Any]) -> ProviderStatus | None:
        """A webhook body or an ``info`` result → status (``None`` for statuses we do not report)."""
        raw_status = str(invoice.get("status") or invoice.get("payment_status") or "").strip().lower()
        state = STATUS_MAP.get(raw_status)
        if state is None:
            return None
        external = _text(invoice.get("uuid"))
        if external is None:
            raise WebhookRejected("no invoice uuid", status=400)
        order_id = _text(invoice.get("order_id"))
        ours = order_id is not None and _UUID_RE.fullmatch(order_id) is not None
        amount = _amount(invoice.get("amount"))
        currency = _currency(invoice.get("currency"))
        if raw_status == "wrong_amount":
            # An underpayment: report what was actually sent so the core records a mismatch.
            paid = _amount(invoice.get("payment_amount"))
            paid_currency = _currency(invoice.get("payer_currency"))
            if paid is not None and paid_currency is not None and (paid, paid_currency) != (amount, currency):
                amount, currency = paid, paid_currency
            else:
                amount, currency = None, None
        return ProviderStatus(
            state=state,
            external_id=external,
            payment_id=order_id if ours else None,
            amount=amount,
            currency=currency,
            paid_at=_when(invoice.get("updated_at")) if state is PaymentState.PAID else None,
            is_test=self.ctx.is_test or not ours,
        )

    # ----------------------------------------------------------------------------------------- status

    async def fetch_status(self, ids: Sequence[str]) -> list[ProviderStatus]:
        result: list[ProviderStatus] = []
        for external in dict.fromkeys(i for i in ids if i):
            try:
                invoice = await self._call("/v1/payment/info", {"uuid": external}, missing_ok=True)
            except ProviderError as exc:
                if exc.retryable or exc.status in (401, 403):
                    raise
                self.ctx.log.warning("%s: status of one invoice is unavailable", self.manifest.slug)
                continue
            if invoice is None:
                continue
            if not isinstance(invoice, Mapping):
                raise ProviderError(_T["bad_answer"], retryable=True)
            try:
                status = self._invoice_status(invoice)
            except WebhookRejected:
                self.ctx.log.warning("%s: unreadable invoice skipped", self.manifest.slug)
                continue
            if status is not None and status.external_id == external:
                result.append(status)
        return result

    # ------------------------------------------------------------------------------------------ probe

    async def test_credentials(self) -> Probe:
        try:
            services = await self._call("/v1/payment/services", None)
        except ProviderError as exc:
            if exc.retryable:
                raise
            return Probe(False, exc.human)
        count = sum(
            1
            for s in (services if isinstance(services, list) else [])
            if isinstance(s, Mapping) and s.get("is_available") is not False
        )
        return Probe(True, _T["probe_ok"].format(n=count))
