"""Overpay — cards and SBP through the Overpay hosted payment page (Checkout).

Written from scratch by the specification ``docs/providers/overpay.md`` (official documentation
<https://docs.overpay.io/ru/>, checked on 2026-10-02). No third-party code was used. Imports only
:mod:`svbg.sdk` and the standard library.

* Every request carries HTTP Basic ``<shop id>:<secret key>``, JSON in UTF-8. Checkout requests also carry
  ``X-API-Version: 2``.
* ``POST https://checkout.overpay.io/ctp/api/checkouts`` creates a payment token: ``order.amount`` is an
  integer in minor units, ``order.tracking_id`` is **our opaque payment id** (it comes back in every
  notification and status), ``settings.notification_url`` is the instance's webhook. The buyer chooses card or
  SBP on Overpay's page, so the bot never sends a name, phone or e-mail (the direct SBP request would need
  them). The token is the external id. Checkout has no idempotency, so a failed create is never re-sent
  blindly (a ``429`` — «not processed» — is retried with a growing pause).
* ``GET /ctp/api/checkouts/{token}`` reads the status (``fetch_status``); there is no batch method. Checkout
  sends a notification only twice, so the status poll is the safety net.
* Webhooks are strong: ``Content-Signature = base64(RSA-SHA256 PKCS#1 v1.5 (raw body))`` made with the shop's
  private key at Overpay, verified here with the shop's public key from the cabinet (PEM or the bare base64
  DER the cabinet shows) — implemented on the standard library (``pow``) so the plugin stays SDK-only — plus
  ``Authorization: Basic <shop id>:<secret key>`` compared in constant time. Only then is the body parsed.
  There is no signed time (``replay_window_s=None``); replays are dropped by the core's deduplication.
* Bodies: ``{"transaction": {...}}`` (payments, refunds, chargebacks — linked by ``tracking_id``, a chargeback
  by ``parent_transaction.tracking_id``), a Checkout object (token expiry) and subscription events (ignored).
* Statuses: ``successful`` → paid, ``failed``/``error`` → failed, ``incomplete``/``pending`` → processing,
  ``expired``, ``deleted`` → canceled; a chargeback → chargeback. ``test: true`` marks test-mode operations
  (the instance's test mode sends ``checkout.test = true``).
* Refunds: ``POST /transactions/refunds`` (cards, ``gateway``) or ``/beyag/transactions/refunds`` (APM such as
  SBP, ``api``) with the payment's ``uid`` and a deterministic ``RequestID``.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
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
    ConfigError,
    ConfigModel,
    HttpResponse,
    Manifest,
    MethodKind,
    PaymentIntent,
    PaymentProvider,
    PaymentState,
    PluginContext,
    Probe,
    ProviderError,
    ProviderEvent,
    ProviderStatus,
    RefundResult,
    WebhookAuth,
    WebhookIgnored,
    WebhookRejected,
    WebhookRequest,
    choice,
    integer,
    number,
    parse_timestamp,
    secret,
    text,
    url,
)

__all__ = [
    "DEFAULT_APM_URL",
    "DEFAULT_CHECKOUT_URL",
    "DEFAULT_GATEWAY_URL",
    "TX_STATUS",
    "Overpay",
    "OverpayConfig",
    "RsaPublicKey",
    "basic_auth",
    "load_public_key",
    "rsa_sha256_verify",
]

DEFAULT_CHECKOUT_URL: Final = "https://checkout.overpay.io"
DEFAULT_GATEWAY_URL: Final = "https://gateway.overpay.io"
DEFAULT_APM_URL: Final = "https://api.overpay.io"
DESCRIPTION_MAX: Final = 255
ATTEMPTS_429: Final = 3
RETRY_DELAY_MAX_S: Final = 4.0
_ID_MAX: Final = 200
_UUID_RE: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_TYPES_RE: Final = r"\s*(credit_card|sbp)(\s*,\s*(credit_card|sbp))*\s*"
_TYPE_BY_KIND: Final = {MethodKind.CARD: "credit_card", MethodKind.SBP: "sbp"}
_MIN_RSA_BITS: Final = 1024
#: ISO 4217 minor-unit exponents other than 2 (Overpay amounts are integers in minor units).
_EXPONENTS: Final[Mapping[str, int]] = {
    **dict.fromkeys(
        ("BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW", "PYG", "RWF", "UGX", "VND", "VUV", "XAF"), 0
    ),
    **dict.fromkeys(("XOF", "XPF"), 0),
    **dict.fromkeys(("BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"), 3),
}

#: Transaction status → SDK state (for ``type == "payment"``; specification §6.1).
TX_STATUS: Final[Mapping[str, PaymentState]] = {
    "successful": PaymentState.PAID,
    "failed": PaymentState.FAILED,
    "incomplete": PaymentState.PROCESSING,
    "pending": PaymentState.PROCESSING,
    "expired": PaymentState.EXPIRED,
    "error": PaymentState.FAILED,
    "deleted": PaymentState.CANCELED,
}

_T: Final = {
    "bad_key": "Overpay отклонила ID магазина или секретный ключ — проверьте их в кабинете Overpay",
    "rate_limited": "Overpay ограничила частоту запросов (HTTP 429), повторим позже",
    "unavailable": "Overpay временно недоступна (HTTP {status})",
    "rejected": "Overpay отклонила запрос: {error}",
    "bad_answer": "Overpay вернула непонятный ответ",
    "currency": "этот инстанс Overpay принимает только RUB",
    "too_small": "сумма меньше минимальной для этой кассы Overpay ({min} ₽)",
    "too_large": "сумма больше максимальной для этой кассы Overpay ({max} ₽)",
    "bad_public_key": "это не публичный RSA-ключ — скопируйте его из кабинета Overpay целиком",
    "bad_limits": "минимальная сумма больше максимальной",
    "refund_unpaid": "Overpay: платёж ещё не оплачен — возвращать нечего",
    "refund_denied": "Overpay: возврат невозможен — {error}",
    "refund_done": "Overpay: возврат {status}",
    "probe_ok": "Ключи приняты: магазин {shop}{mode}",
    "probe_test": " (тестовый режим: счета создаются с test=true)",
    "probe_not_overpay": "по адресу Checkout отвечает не Overpay — проверьте «Адрес Checkout»",
}


class OverpayConfig(ConfigModel):
    shop_id = text(
        "ID магазина",
        "Числовой идентификатор магазина в Overpay — логин для API (HTTP Basic).",
        where="кабинет Overpay → Магазины → «Подробнее» → строка «ID»",
        pattern=r"\d{1,20}",
    )
    secret_key = secret(
        "Секретный ключ магазина",
        "Пароль для API (HTTP Basic); им же Overpay подписывает Basic-авторизацию уведомлений.",
        where="кабинет Overpay → Магазины → «Подробнее» → «Показать секретный ключ магазина»",
    )
    public_key = text(
        "Публичный ключ магазина",
        "RSA-ключ для проверки подписи уведомлений (заголовок Content-Signature). Можно вставить как есть — "
        "одной строкой base64 или блоком -----BEGIN PUBLIC KEY-----.",
        where="кабинет Overpay → Магазины → «Подробнее» → «Публичный ключ»",
    )
    payment_types = text(
        "Способы оплаты на странице",
        "Через запятую: credit_card (карта), sbp (СБП). Если покупатель нажал «Карта» или «СБП», на странице "
        "будет только этот способ. Пусто — все способы, включённые в магазине.",
        where="способы, подключённые вашему магазину в кабинете Overpay (уточняет менеджер)",
        required=False,
        pattern=_TYPES_RE,
    )
    return_url = url(
        "Адрес возврата",
        "Куда Overpay вернёт покупателя после оплаты. Обычно ссылка на бота: https://t.me/<имя_бота>. "
        "Пусто — Overpay покажет свою страницу результата.",
        where="ссылка на вашего бота (@BotFather → имя бота)",
        required=False,
    )
    min_amount = number(
        "Минимальная сумма, ₽",
        "Лимит магазина в Overpay: счёт на меньшую сумму бот не создаёт. Пусто — без проверки.",
        where="у менеджера Overpay (лимиты по картам и СБП публично не указаны)",
        required=False,
        min=0.01,
    )
    max_amount = number(
        "Максимальная сумма, ₽",
        "Лимит магазина в Overpay: счёт на большую сумму бот не создаёт. Пусто — без проверки.",
        where="у менеджера Overpay (лимиты по картам и СБП публично не указаны)",
        required=False,
        min=0.01,
    )
    language = choice(
        "Язык платёжной страницы",
        ("ru", "en"),
        where="на ваше усмотрение",
        default="ru",
        advanced=True,
    )
    attempts = integer(
        "Попыток оплаты на странице",
        "Сколько раз покупатель может повторить оплату по одной ссылке (1–3).",
        where="на ваше усмотрение; по умолчанию 1",
        default=1,
        required=False,
        min=1,
        max=3,
        advanced=True,
    )
    payment_lifetime_min = integer(
        "Срок оплаты, минут",
        "Сколько минут ссылка принимает оплату (order.expired_at). Пусто — 24 часа (по умолчанию Overpay).",
        where="на ваше усмотрение, например 60",
        required=False,
        min=5,
        max=43_200,
        advanced=True,
    )
    checkout_url = url(
        "Адрес Checkout",
        "Базовый адрес платёжной страницы и токенов. Меняйте, только если Overpay сообщила другой.",
        where="docs.overpay.io → Интеграция → Виджет → «Получение токена платежа»",
        default=DEFAULT_CHECKOUT_URL,
        required=False,
        advanced=True,
    )
    gateway_url = url(
        "Адрес шлюза (карты)",
        "Используется для возвратов по картам.",
        where="docs.overpay.io → Интеграция → API карт",
        default=DEFAULT_GATEWAY_URL,
        required=False,
        advanced=True,
    )
    apm_url = url(
        "Адрес API альтернативных методов (СБП)",
        "Используется для возвратов по СБП.",
        where="docs.overpay.io → Интеграция → API альтернативных методов",
        default=DEFAULT_APM_URL,
        required=False,
        advanced=True,
    )


# ------------------------------------------------------------------------------------- RSA (stdlib)

_RSA_OID: Final = bytes.fromhex("2a864886f70d010101")  # 1.2.840.113549.1.1.1 rsaEncryption
#: DER DigestInfo prefix of SHA-256 (RFC 8017 §9.2, note 1).
_SHA256_PREFIX: Final = bytes.fromhex("3031300d060960864801650304020105000420")


class RsaPublicKey:
    """An RSA public key ``(n, e)``."""

    __slots__ = ("e", "n", "size")

    def __init__(self, n: int, e: int) -> None:
        if n.bit_length() < _MIN_RSA_BITS or e < 3 or e % 2 == 0 or e >= n:
            raise ValueError("unacceptable RSA public key")
        self.n = n
        self.e = e
        self.size = (n.bit_length() + 7) // 8


def _der(buf: bytes, pos: int, tag: int) -> tuple[int, int]:
    """Bounds ``(start, end)`` of the DER value with ``tag`` at ``pos``."""
    if pos + 2 > len(buf) or buf[pos] != tag:
        raise ValueError("bad DER")
    first = buf[pos + 1]
    pos += 2
    if first < 0x80:
        length = first
    else:
        count = first & 0x7F
        if not 1 <= count <= 4 or pos + count > len(buf):
            raise ValueError("bad DER length")
        length = int.from_bytes(buf[pos : pos + count], "big")
        pos += count
    if pos + length > len(buf):
        raise ValueError("truncated DER")
    return pos, pos + length


def _from_pkcs1(der: bytes) -> RsaPublicKey:
    start, end = _der(der, 0, 0x30)
    n_start, n_end = _der(der, start, 0x02)
    e_start, e_end = _der(der, n_end, 0x02)
    if e_end != end:
        raise ValueError("bad RSAPublicKey")
    return RsaPublicKey(int.from_bytes(der[n_start:n_end], "big"), int.from_bytes(der[e_start:e_end], "big"))


def _from_spki(der: bytes) -> RsaPublicKey:
    start, end = _der(der, 0, 0x30)
    alg_start, alg_end = _der(der, start, 0x30)
    oid_start, oid_end = _der(der, alg_start, 0x06)
    if der[oid_start:oid_end] != _RSA_OID:
        raise ValueError("not an RSA key")
    bits_start, bits_end = _der(der, alg_end, 0x03)
    if bits_end != end or bits_end == bits_start or der[bits_start] != 0:
        raise ValueError("bad BIT STRING")
    return _from_pkcs1(der[bits_start + 1 : bits_end])


def load_public_key(value: str) -> RsaPublicKey:
    """The shop's key as the cabinet shows it — bare base64 of a SubjectPublicKeyInfo DER — or a PEM block
    (``PUBLIC KEY`` or ``RSA PUBLIC KEY``); line breaks, spaces and literal ``\\n`` are tolerated."""
    textual = value.replace("\\n", "\n").strip()
    pkcs1 = "BEGIN RSA PUBLIC KEY" in textual
    body = re.sub(r"-----(BEGIN|END)[A-Z ]*-----", "", textual)
    try:
        der = base64.b64decode(re.sub(r"\s+", "", body), validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("public key is not base64") from None
    if pkcs1:
        return _from_pkcs1(der)
    try:
        return _from_spki(der)
    except ValueError:
        if "BEGIN" not in textual:
            return _from_pkcs1(der)  # bare base64 of a PKCS#1 key
        raise


def rsa_sha256_verify(key: RsaPublicKey, signature: bytes, message: bytes) -> bool:
    """RSASSA-PKCS1-v1_5 with SHA-256 (RFC 8017 §8.2.2) — what ``openssl_verify(..., OPENSSL_ALGO_SHA256)``
    checks. The encoded message is rebuilt and compared in constant time (no parsing of the decrypted
    block)."""
    if len(signature) != key.size:
        return False
    s = int.from_bytes(signature, "big")
    if s >= key.n:
        return False
    em = pow(s, key.e, key.n).to_bytes(key.size, "big")
    t = _SHA256_PREFIX + hashlib.sha256(message).digest()
    if key.size < len(t) + 11:
        return False
    expected = b"\x00\x01" + b"\xff" * (key.size - len(t) - 3) + b"\x00" + t
    return hmac.compare_digest(em, expected)


# --------------------------------------------------------------------------------------------- helpers


def basic_auth(shop_id: str, secret_key: str) -> str:
    """``Authorization`` value: ``Basic base64("<shop_id>:<secret_key>")``."""
    return "Basic " + base64.b64encode(f"{shop_id}:{secret_key}".encode()).decode("ascii")


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


def _exponent(currency: str) -> int:
    return _EXPONENTS.get(currency, 2)


def _minor_to_major(value: Any, currency: str) -> Decimal:
    """Overpay's integer minor units → :class:`Decimal` major units (``17900`` RUB → ``179.00``)."""
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise TypeError("amount is not an integer")
    if isinstance(value, str):
        if not value.strip().isdigit():
            raise ValueError("amount is not an integer")
        value = int(value.strip())
    if value < 0:
        raise ValueError("negative amount")
    return Decimal(value).scaleb(-_exponent(currency))


def _currency(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z]{3}", value.strip()):
        raise ValueError("bad currency")
    return value.strip().upper()


def _money(amount: Any, currency: Any) -> tuple[Decimal, str]:
    cur = _currency(currency)
    return _minor_to_major(amount, cur), cur


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return isinstance(value, str) and value.strip().lower() in ("true", "1")


def _lower(value: Any) -> str:
    return str(value or "").strip().lower()


def _time(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        return parse_timestamp(value)
    except ValueError:
        return None


def _shop_matches(value: Any, shop_id: str) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value == int(shop_id)
    return isinstance(value, str) and value.strip().isdigit() and int(value.strip()) == int(shop_id)


def _clip(value: str, limit: int = DESCRIPTION_MAX) -> str:
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def _gateway_payment(checkout: Mapping[str, Any]) -> Mapping[str, Any]:
    response = checkout.get("gateway_response")
    if not isinstance(response, Mapping):
        return {}
    for key in ("payment", "authorization"):
        tx = response.get(key)
        if isinstance(tx, Mapping):
            return tx
    return {}


def _checkout_state(checkout: Mapping[str, Any]) -> PaymentState | None:
    """Checkout (token) object → SDK state (specification §6.1, last paragraph)."""
    finished = _truthy(checkout.get("finished"))
    if _truthy(checkout.get("expired")) and not finished:
        return PaymentState.EXPIRED
    tx = _gateway_payment(checkout)
    status = _lower(checkout.get("status"))
    if status == "error" and tx:
        status = _lower(tx.get("status"))
    if not status:
        return PaymentState.FAILED if finished else PaymentState.CREATED
    return TX_STATUS.get(status)


class Overpay(PaymentProvider):
    """See module docstring."""

    manifest = Manifest(
        slug="overpay",
        title="Overpay",
        method_kinds=(MethodKind.CARD, MethodKind.SBP),
        currencies=("RUB",),
        config=OverpayConfig,
        docs_url="https://docs.overpay.io/ru/",
    )
    capabilities = Capabilities(
        webhook_auth=WebhookAuth.SIGNATURE,  # RSA-SHA256 over the raw body + Basic with the shop's secret
        replay_window_s=None,  # the signed body has no «sent at» time; the core deduplicates replays
        fetch_status=True,  # Checkout retries a notification only twice
        batch_status=False,  # no lookup by a list of ids (reports by dates only)
        refund=True,
        recurring=False,  # documented, not used in this version
        receipt_54fz=False,
        in_chat_invoice=False,
        redirect=True,
        webhook=True,
    )

    #: Base pause between ``429`` retries, doubled each time (tests set it to 0).
    retry_delay_s: float = 0.5

    def __init__(self, config: ConfigModel, ctx: PluginContext) -> None:
        super().__init__(config, ctx)
        errors: dict[str, str] = {}
        try:
            self._key = load_public_key(str(self.config.public_key))
        except ValueError:
            errors["public_key"] = _T["bad_public_key"]
        self._min = None if self.config.min_amount is None else Decimal(str(self.config.min_amount))
        self._max = None if self.config.max_amount is None else Decimal(str(self.config.max_amount))
        if self._min is not None and self._max is not None and self._min > self._max:
            errors["min_amount"] = _T["bad_limits"]
        if errors:
            raise ConfigError(errors)
        self._types = tuple(t.strip() for t in str(self.config.payment_types or "").split(",") if t.strip())

    # ------------------------------------------------------------------------------------------- HTTP

    def _url(self, name: str, default: str) -> str:
        return str(getattr(self.config, name) or default).rstrip("/")

    def _headers(self, *, checkout: bool = False, request_id: str | None = None) -> dict[str, str]:
        headers = {
            "Authorization": basic_auth(self.config.shop_id, self.config.secret_key),
            "Accept": "application/json",
        }
        if checkout:
            headers["X-API-Version"] = "2"
        if request_id is not None:
            headers["RequestID"] = request_id
        return headers

    async def _request(
        self, method: str, url_: str, headers: Mapping[str, str], json: Any = None
    ) -> HttpResponse:
        """One request; ``429`` (the request was not processed) is retried with a growing pause."""
        delay = self.retry_delay_s
        resp = await self.ctx.http.request(method, url_, headers=headers, json=json)
        for _ in range(ATTEMPTS_429 - 1):
            if resp.status != 429:
                break
            if delay:
                await asyncio.sleep(min(delay, RETRY_DELAY_MAX_S))
            delay *= 2
            resp = await self.ctx.http.request(method, url_, headers=headers, json=json)
        return resp

    @staticmethod
    def _body(resp: HttpResponse) -> Any:
        try:
            return resp.json()
        except ValueError:
            return None

    def _error(self, resp: HttpResponse, what: str) -> ProviderError:
        if resp.status in (401, 403):
            return ProviderError(_T["bad_key"], retryable=False, status=resp.status)
        if resp.status == 429:
            return ProviderError(_T["rate_limited"], retryable=True, status=429)
        if resp.status >= 500:
            return ProviderError(
                _T["unavailable"].format(status=resp.status), retryable=True, status=resp.status
            )
        # Only the short «message»: the «errors» part may echo request data.
        data = self._body(resp)
        message = data.get("message") if isinstance(data, dict) else None
        detail = (
            _clip(str(message), 200)
            if isinstance(message, str) and message.strip()
            else f"HTTP {resp.status}"
        )
        self.ctx.log.warning("overpay: %s answered HTTP %s (%s)", what, resp.status, detail)
        return ProviderError(_T["rejected"].format(error=detail), retryable=False, status=resp.status)

    # ----------------------------------------------------------------------------------------- create

    def _payment_types(self, intent: PaymentIntent) -> list[str] | None:
        wanted = _TYPE_BY_KIND.get(intent.method_hint) if intent.method_hint else None
        if wanted is not None and (not self._types or wanted in self._types):
            return [wanted]
        return list(self._types) or None

    async def create(self, intent: PaymentIntent) -> Checkout:
        currency = intent.currency.upper()
        if currency not in self.manifest.currencies:
            raise ProviderError(_T["currency"], retryable=False)
        amount = intent.amount
        if self._min is not None and amount < self._min:
            raise ProviderError(_T["too_small"].format(min=self._min), retryable=False)
        if self._max is not None and amount > self._max:
            raise ProviderError(_T["too_large"].format(max=self._max), retryable=False)
        order: dict[str, Any] = {
            "amount": intent.amount_minor,
            "currency": currency,
            "description": _clip(intent.description or "Оплата"),
            "tracking_id": intent.payment_id,  # opaque: never a Telegram id or a subscription link
        }
        expires: datetime | None = None
        if self.config.payment_lifetime_min:
            expires = datetime.now(UTC) + timedelta(minutes=int(self.config.payment_lifetime_min))
            order["expired_at"] = expires.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        settings: dict[str, Any] = {"language": self.config.language or "ru"}
        return_url = intent.return_url or self.config.return_url
        if return_url:
            settings["return_url"] = return_url
        if self.ctx.webhook_url:
            settings["notification_url"] = self.ctx.webhook_url
        checkout: dict[str, Any] = {
            "test": bool(self.ctx.is_test or intent.is_test),
            "transaction_type": "payment",
            "attempts": int(self.config.attempts or 1),
            "settings": settings,
            "order": order,
        }
        types = self._payment_types(intent)
        if types:
            checkout["payment_method"] = {"types": types}
        resp = await self._request(
            "POST",
            f"{self._url('checkout_url', DEFAULT_CHECKOUT_URL)}/ctp/api/checkouts",
            self._headers(checkout=True),
            {"checkout": checkout},
        )
        if resp.status not in (200, 201):
            raise self._error(resp, "create")
        data = self._body(resp)
        result = data.get("checkout") if isinstance(data, dict) else None
        token = _id(result.get("token")) if isinstance(result, Mapping) else None
        pay_url = result.get("redirect_url") if isinstance(result, Mapping) else None
        if token is None or not isinstance(pay_url, str) or not re.match(r"https?://", pay_url):
            raise ProviderError(_T["bad_answer"], retryable=False, status=resp.status)
        return Checkout(
            kind="url",
            external_id=token,
            pay_url=pay_url,
            expires_at=expires or datetime.now(UTC) + timedelta(hours=24),
        )

    # ---------------------------------------------------------------------------------------- webhook

    def _authorized(self, header: str | None) -> bool:
        """``Basic <shop id>:<secret key>``, both compared in constant time."""
        if not header or not header.startswith("Basic "):
            return False
        try:
            user, sep, password = base64.b64decode(header[6:].strip(), validate=True).decode().partition(":")
        except (binascii.Error, ValueError):
            return False
        user_ok = hmac.compare_digest(user.encode(), str(self.config.shop_id).encode())
        secret_ok = hmac.compare_digest(password.encode(), str(self.config.secret_key).encode())
        return bool(sep) and user_ok and secret_ok

    async def parse_webhook(self, req: WebhookRequest) -> ProviderEvent:
        if not self._authorized(req.header("Authorization")):
            raise WebhookRejected("bad basic auth", status=401)
        raw_signature = re.sub(r"\s+", "", req.header("Content-Signature") or "")
        if not raw_signature:
            raise WebhookRejected("missing signature", status=401)
        try:
            signature = base64.b64decode(raw_signature, validate=True)
        except (binascii.Error, ValueError):
            raise WebhookRejected("signature is not base64", status=401) from None
        if not rsa_sha256_verify(self._key, signature, req.body):
            raise WebhookRejected("bad signature", status=401)
        data = req.json()
        if not isinstance(data, dict):
            raise WebhookRejected("malformed", status=400)
        if isinstance(data.get("transaction"), dict):
            return self._transaction_event(data["transaction"])
        if "token" in data and isinstance(data.get("order"), dict):
            return self._checkout_event(data)
        if _id(data.get("id")) is not None and ("state" in data or "event" in data):
            raise WebhookIgnored("subscription event")
        raise WebhookRejected("malformed", status=400)

    def _transaction_event(self, tx: Mapping[str, Any]) -> ProviderEvent:
        kind = _lower(tx.get("type")) or "payment"
        status = _lower(tx.get("status"))
        if kind == "chargeback":
            if status != "successful":
                raise WebhookIgnored(f"chargeback {status[:20]}")
            parent = tx.get("parent_transaction")
            tracking = parent.get("tracking_id") if isinstance(parent, Mapping) else None
            pid = _our_payment_id(tracking) or _our_payment_id(tx.get("tracking_id"))
            if pid is None:
                self.ctx.log.warning("overpay: chargeback without our tracking_id ignored")
                raise WebhookIgnored("chargeback of an unknown payment")
            return ProviderEvent(
                state=PaymentState.CHARGEBACK,
                payment_id=pid,
                is_test=_truthy(tx.get("test")),
                summary=self._summary(tx, kind, status),
            )
        if kind != "payment":
            # refunds are synchronous (refund()); authorizations, captures, payouts are not used
            raise WebhookIgnored(f"transaction {kind[:20]}")
        state = TX_STATUS.get(status)
        if state is None:
            self.ctx.log.warning("overpay: transaction status %r ignored", status[:20])
            raise WebhookIgnored("unknown status")
        pid = _our_payment_id(tx.get("tracking_id"))
        if pid is None:
            # authentic but not ours (a payment made from the cabinet): 200, so Overpay stops retrying
            raise WebhookIgnored("no tracking_id of ours")
        try:
            amount, currency = _money(tx.get("amount"), tx.get("currency"))
        except (TypeError, ValueError):
            raise WebhookRejected("bad amount", status=400) from None
        return ProviderEvent(
            state=state,
            payment_id=pid,  # the transaction uid is not our external id (the token is)
            amount=amount,
            currency=currency,
            paid_at=_time(tx.get("paid_at")) if state is PaymentState.PAID else None,
            is_test=_truthy(tx.get("test")),
            summary=self._summary(tx, kind, status),
        )

    def _checkout_event(self, data: Mapping[str, Any]) -> ProviderEvent:
        if "shop_id" in data and not _shop_matches(data.get("shop_id"), self.config.shop_id):
            raise WebhookRejected("foreign shop_id", status=401)
        state = _checkout_state(data)
        if state is None:
            raise WebhookIgnored("unknown status")
        order = data["order"]
        token = _id(data.get("token"))
        pid = _our_payment_id(order.get("tracking_id"))
        if token is None and pid is None:
            raise WebhookIgnored("no token of ours")
        amount: Decimal | None = None
        currency: str | None = None
        if state is PaymentState.PAID:
            tx = _gateway_payment(data) or order
            try:
                amount, currency = _money(tx.get("amount"), tx.get("currency"))
            except (TypeError, ValueError):
                raise WebhookRejected("bad amount", status=400) from None
        return ProviderEvent(
            state=state,
            external_id=token,
            payment_id=pid,
            amount=amount,
            currency=currency,
            is_test=_truthy(data.get("test")),
            summary={"kind": "checkout", "status": _lower(data.get("status"))[:20], "expired": state.value},
        )

    @staticmethod
    def _summary(tx: Mapping[str, Any], kind: str, status: str) -> dict[str, Any]:
        return {
            "kind": kind[:20],
            "status": status[:20],
            "uid": _id(tx.get("uid")),
            "method": _id(tx.get("method_type") or tx.get("payment_method_type")),
            "amount": tx.get("amount") if isinstance(tx.get("amount"), int) else None,
            "currency": str(tx.get("currency") or "")[:3] or None,
        }

    # ----------------------------------------------------------------------------------------- status

    async def _checkout(self, token: str) -> Mapping[str, Any] | None:
        resp = await self._request(
            "GET",
            f"{self._url('checkout_url', DEFAULT_CHECKOUT_URL)}/ctp/api/checkouts/{quote(token, safe='')}",
            self._headers(checkout=True),
        )
        if resp.status in (404, 422):
            return None
        if resp.status != 200:
            raise self._error(resp, "status")
        data = self._body(resp)
        checkout = data.get("checkout") if isinstance(data, dict) else None
        if not isinstance(checkout, Mapping):
            raise ProviderError(_T["bad_answer"], retryable=True, status=resp.status)
        if "shop_id" in checkout and not _shop_matches(checkout.get("shop_id"), self.config.shop_id):
            self.ctx.log.warning("overpay: token %s belongs to another shop, skipped", token[:16])
            return None
        return checkout

    async def fetch_status(self, ids: Sequence[str]) -> list[ProviderStatus]:
        """``GET /ctp/api/checkouts/{token}`` per invoice (no batch method)."""
        result: list[ProviderStatus] = []
        for token in dict.fromkeys(ids):
            if not token:
                continue
            checkout = await self._checkout(token)
            if checkout is None:
                continue
            status = self._status(checkout, token)
            if status is not None:
                result.append(status)
        return result

    def _status(self, checkout: Mapping[str, Any], token: str) -> ProviderStatus | None:
        state = _checkout_state(checkout)
        if state is None:
            self.ctx.log.warning("overpay: token %s has an unknown status, skipped", token[:16])
            return None
        order = checkout.get("order") if isinstance(checkout.get("order"), Mapping) else {}
        tx = _gateway_payment(checkout)
        amount: Decimal | None = None
        currency: str | None = None
        try:
            if tx.get("amount") is not None:
                amount, currency = _money(tx.get("amount"), tx.get("currency") or order.get("currency"))
            elif order.get("amount") is not None:
                amount, currency = _money(order.get("amount"), order.get("currency"))
        except (TypeError, ValueError):
            self.ctx.log.warning("overpay: unreadable amount of token %s", token[:16])
            amount, currency = None, None
        return ProviderStatus(
            state=state,
            external_id=_id(checkout.get("token")) or token,
            payment_id=_our_payment_id(order.get("tracking_id")),
            amount=amount,
            currency=currency,
            paid_at=_time(tx.get("paid_at")) if state is PaymentState.PAID else None,
            is_test=_truthy(checkout.get("test")),
        )

    # ----------------------------------------------------------------------------------------- refund

    async def refund(self, external_id: str, amount_minor: int, currency: str) -> RefundResult:
        """Find the paid transaction of the token, then refund it on the card gateway or the APM API."""
        try:
            checkout = await self._checkout(external_id)
        except ProviderError as exc:
            return RefundResult(False, message=exc.human)
        tx = _gateway_payment(checkout or {})
        uid = _id(tx.get("uid"))
        if checkout is None or uid is None or _lower(tx.get("status")) != "successful":
            return RefundResult(False, message=_T["refund_unpaid"])
        method = _lower(tx.get("payment_method_type") or tx.get("method_type"))
        if method in ("", "credit_card"):
            base, path = self._url("gateway_url", DEFAULT_GATEWAY_URL), "/transactions/refunds"
        else:
            base, path = self._url("apm_url", DEFAULT_APM_URL), "/beyag/transactions/refunds"
        order = checkout.get("order") if isinstance(checkout.get("order"), Mapping) else {}
        request: dict[str, Any] = {
            "parent_uid": uid,
            "amount": amount_minor,
            "reason": "Возврат по запросу магазина",
        }
        tracking = _our_payment_id(order.get("tracking_id"))
        if tracking:
            request["tracking_id"] = tracking
        request_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"svbg:overpay:refund:{uid}:{amount_minor}:{currency}")
        )
        try:
            resp = await self._request(
                "POST", f"{base}{path}", self._headers(request_id=request_id), {"request": request}
            )
        except ProviderError as exc:
            return RefundResult(False, message=exc.human)
        data = self._body(resp)
        if resp.status not in (200, 201):
            message = data.get("message") if isinstance(data, dict) else None
            if resp.status in (401, 403, 429) or resp.status >= 500 or not isinstance(message, str):
                return RefundResult(False, message=self._error(resp, "refund").human)
            return RefundResult(False, message=_T["refund_denied"].format(error=_clip(message, 200)))
        result = data.get("transaction") if isinstance(data, dict) else None
        if not isinstance(result, Mapping):
            return RefundResult(False, message=_T["bad_answer"])
        status = _lower(result.get("status"))
        ok = status in ("successful", "pending", "incomplete")
        return RefundResult(
            ok, _id(result.get("uid")), _T["refund_done"].format(status=status or "неизвестен")
        )

    # ------------------------------------------------------------------------------------------ probe

    async def test_credentials(self) -> Probe:
        """No side effects: reads a token that cannot exist (``404`` = the keys were accepted). The public
        key has already been parsed when the instance was built."""
        probe_token = "0" * 64
        resp = await self.ctx.http.request(
            "GET",
            f"{self._url('checkout_url', DEFAULT_CHECKOUT_URL)}/ctp/api/checkouts/{probe_token}",
            headers=self._headers(checkout=True),
        )
        if resp.status in (401, 403):
            return Probe(False, _T["bad_key"])
        if resp.status not in (200, 404, 422):
            return Probe(False, self._error(resp, "probe").human)
        if not isinstance(self._body(resp), dict):
            return Probe(False, _T["probe_not_overpay"])
        mode = _T["probe_test"] if self.ctx.is_test else ""
        return Probe(
            True, _T["probe_ok"].format(shop=self.config.shop_id, mode=mode), {"key_bits": self._key.size * 8}
        )
