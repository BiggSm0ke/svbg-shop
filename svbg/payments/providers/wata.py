"""WATA — payment links (cards, SBP, T-Pay, SberPay), H2H API (07 §4.2).

Specification: ``docs/providers/wata.md`` (official documentation <https://wata.pro/api>, checked 2026-10-02).
Ported from Remnashop (MIT, © 2024 snoups) ``payment_gateways/wata.py`` and corrected against the official
documentation — see ``THIRD_PARTY_NOTICES.md``. Imports only :mod:`svbg.sdk` and the standard library.

* Requests carry ``Authorization: Bearer <JWT>`` (the token is issued per terminal in the WATA cabinet).
* ``POST /links`` creates a one-time payment link. ``orderId`` carries **only** our opaque payment id; it is
  also used as the external id, because ``GET /transactions/?orderId=…`` is the documented status lookup
  (rate limit: one GET per 30 s per object).
* Webhooks: ``X-Signature = base64(RSA-SHA512-PKCS#1 v1.5 (raw body))`` verified with WATA's public key from
  ``GET /public-key`` (no auth). The key is cached in memory and in ``ctx.kv`` for 6 hours and re-fetched at
  most once a minute when a signature does not verify (key rotation). The owner may pin the key in the
  settings — then nothing is fetched. RSA verification is implemented here on the standard library
  (``pow``), so the plugin stays SDK-only.
* The sandbox (``api-sandbox.wata.pro``) has its own keys and tokens: a sandbox webhook cannot be verified
  by a live instance, and events of a test instance are reported ``is_test``.
* Pre-payment webhooks (``transactionStatus=Created``) must be answered ``200`` within 10 s, otherwise WATA
  declines the payment; they are parsed as ``created`` and acknowledged.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import time
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
    PluginContext,
    Probe,
    ProviderError,
    ProviderEvent,
    ProviderStatus,
    WebhookAuth,
    WebhookIgnored,
    WebhookRejected,
    WebhookRequest,
    integer,
    parse_amount,
    parse_timestamp,
    secret,
    text,
    url,
)

__all__ = [
    "LIVE_URL",
    "MIN_AMOUNT",
    "PUBLIC_KEY_TTL_S",
    "SANDBOX_URL",
    "RsaPublicKey",
    "Wata",
    "WataConfig",
    "load_public_key",
    "rsa_sha512_verify",
]

LIVE_URL: Final = "https://api.wata.pro/api/h2h"
SANDBOX_URL: Final = "https://api-sandbox.wata.pro/api/h2h"
PUBLIC_KEY_TTL_S: Final = 6 * 3600
#: A failed verification may re-fetch the key (rotation), but not more often than this.
KEY_REFRESH_COOLDOWN_S: Final = 60.0
KV_KEY: Final = "wata_public_key"
#: Minimum link amount per currency (official documentation); the maximum is 999 999.99.
MIN_AMOUNT: Final[Mapping[str, Decimal]] = {"RUB": Decimal(10), "USD": Decimal(1), "EUR": Decimal(1)}
MAX_AMOUNT: Final = Decimal("999999.99")
_DESCRIPTION_MAX: Final = 255
_ID_MAX: Final = 200
_UUID_RE: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_MIN_RSA_BITS: Final = 1024

#: transactionStatus of a payment → SDK state. ``Declined`` is one failed attempt: a one-time link stays open
#: until it is paid or expires, so the invoice is still ``created`` for us.
PAYMENT_STATUS: Final[Mapping[str, PaymentState]] = {
    "created": PaymentState.CREATED,
    "pending": PaymentState.PROCESSING,
    "paid": PaymentState.PAID,
    "declined": PaymentState.CREATED,
}

_T: Final = {
    "bad_key": "WATA отклонила токен — проверьте его в кабинете WATA (срок действия токена 1–12 месяцев)",
    "rejected": "WATA отклонила запрос (HTTP {status})",
    "unavailable": "WATA временно недоступна (HTTP {status})",
    "rate_limited": "WATA ограничила частоту запросов (HTTP 429), повторим позже",
    "bad_answer": "WATA вернула непонятный ответ",
    "currency": "WATA принимает только RUB, USD и EUR",
    "too_small": "сумма меньше минимальной для WATA ({min} {currency})",
    "too_large": "сумма больше максимальной для WATA (999 999.99)",
    "probe_ok": "Токен принят, WATA доступна",
    "probe_key": "Токен принят, но публичный ключ WATA для проверки вебхуков не загружается",
    "probe_not_wata": "по адресу API отвечает не WATA — проверьте «Адрес API»",
}


class WataConfig(ConfigModel):
    api_key = secret(
        "Токен API (JWT)",
        "Токен терминала для запросов к API WATA (заголовок Authorization: Bearer). Для тестового режима — "
        "токен песочницы.",
        where="кабинет WATA (merchant.wata.pro) → Терминалы → ваш терминал → «Токены» → «Создать токен»",
    )
    link_hours = integer(
        "Срок жизни ссылки, часов",
        "Сколько часов платёжная ссылка принимает оплату (WATA допускает от 10 минут до 30 дней).",
        where="на ваше усмотрение; по умолчанию 24 часа",
        default=24,
        required=False,
        min=1,
        max=720,
        advanced=True,
    )
    public_key = text(
        "Публичный ключ вебхуков (PEM)",
        "Необязательно. Если задан, подпись вебхуков проверяется только им, и бот не запрашивает ключ "
        "у WATA. "
        "Пусто — ключ загружается автоматически с WATA и кэшируется на 6 часов.",
        where="ответ GET https://api.wata.pro/api/h2h/public-key (поле value); для песочницы — "
        "api-sandbox.wata.pro",
        required=False,
        advanced=True,
    )
    base_url = url(
        "Адрес API",
        "Пусто — боевой https://api.wata.pro/api/h2h, а в тестовом режиме — песочница "
        "https://api-sandbox.wata.pro/api/h2h.",
        where="документация WATA (wata.pro/api) → «Базовый URL»",
        required=False,
        advanced=True,
    )


# ------------------------------------------------------------------------------------- RSA (stdlib)

_RSA_OID: Final = bytes.fromhex("2a864886f70d010101")  # 1.2.840.113549.1.1.1 rsaEncryption
#: DER DigestInfo prefix of SHA-512 (RFC 8017 §9.2, note 1).
_SHA512_PREFIX: Final = bytes.fromhex("3051300d060960864801650304020305000440")


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


def _rsa_from_pkcs1(der: bytes) -> RsaPublicKey:
    start, end = _der(der, 0, 0x30)
    n_start, n_end = _der(der, start, 0x02)
    e_start, e_end = _der(der, n_end, 0x02)
    if e_end != end:
        raise ValueError("bad RSAPublicKey")
    return RsaPublicKey(int.from_bytes(der[n_start:n_end], "big"), int.from_bytes(der[e_start:e_end], "big"))


def load_public_key(value: str) -> RsaPublicKey:
    """RSA key from PEM ``PUBLIC KEY`` (SubjectPublicKeyInfo), PEM ``RSA PUBLIC KEY`` (PKCS#1) or the bare
    base64 of either (as pasted into ``.env``; literal ``\\n`` are tolerated). ``ValueError`` otherwise."""
    textual = value.replace("\\n", "\n").strip()
    pkcs1 = "BEGIN RSA PUBLIC KEY" in textual
    body = re.sub(r"-----(BEGIN|END)[A-Z ]*-----", "", textual)
    try:
        der = base64.b64decode(re.sub(r"\s+", "", body), validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("public key is not base64") from None
    if pkcs1:
        return _rsa_from_pkcs1(der)
    try:
        return _rsa_from_spki(der)
    except ValueError:
        if "BEGIN" not in textual:
            return _rsa_from_pkcs1(der)  # bare base64 of a PKCS#1 key
        raise


def _rsa_from_spki(der: bytes) -> RsaPublicKey:
    start, end = _der(der, 0, 0x30)
    alg_start, alg_end = _der(der, start, 0x30)
    oid_start, oid_end = _der(der, alg_start, 0x06)
    if der[oid_start:oid_end] != _RSA_OID:
        raise ValueError("not an RSA key")
    bits_start, bits_end = _der(der, alg_end, 0x03)
    if bits_end != end or bits_end == bits_start or der[bits_start] != 0:
        raise ValueError("bad BIT STRING")
    return _rsa_from_pkcs1(der[bits_start + 1 : bits_end])


def rsa_sha512_verify(key: RsaPublicKey, signature: bytes, message: bytes) -> bool:
    """RSASSA-PKCS1-v1_5 with SHA-512 (RFC 8017 §8.2.2): the encoded message is rebuilt and compared in
    constant time (no parsing of the decrypted block)."""
    if len(signature) != key.size:
        return False
    s = int.from_bytes(signature, "big")
    if s >= key.n:
        return False
    em = pow(s, key.e, key.n).to_bytes(key.size, "big")
    t = _SHA512_PREFIX + hashlib.sha512(message).digest()
    if key.size < len(t) + 11:
        return False
    expected = b"\x00\x01" + b"\xff" * (key.size - len(t) - 3) + b"\x00" + t
    return hmac.compare_digest(em, expected)


# --------------------------------------------------------------------------------------------- helpers


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


def _iso_z(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{at.microsecond // 1000:03d}Z"


def _lower(value: Any) -> str:
    return str(value or "").strip().lower()


class Wata(PaymentProvider):
    """See module docstring."""

    manifest = Manifest(
        slug="wata",
        title="WATA",
        method_kinds=(MethodKind.CARD, MethodKind.SBP),
        currencies=("RUB", "USD", "EUR"),
        config=WataConfig,
        docs_url="https://wata.pro/api",
        min_minor=100,  # 1 USD / 1 EUR; RUB's 10 ₽ is checked in create()
        max_minor=99_999_999,
    )
    capabilities = Capabilities(
        webhook_auth=WebhookAuth.SIGNATURE,
        replay_window_s=None,  # the signature covers the body only; replays are dropped by the core's dedup
        fetch_status=True,
        batch_status=False,
        refund=False,
        in_chat_invoice=False,
        redirect=True,
        webhook=True,
    )

    def __init__(self, config: ConfigModel, ctx: PluginContext) -> None:
        super().__init__(config, ctx)
        self._key: RsaPublicKey | None = None
        self._key_at: float = 0.0  # monotonic time of the last successful fetch
        self._fetch_at: float | None = None  # monotonic time of the last fetch attempt
        self._pinned: RsaPublicKey | None = None
        if self.config.public_key:
            try:
                self._pinned = load_public_key(self.config.public_key)
            except ValueError:
                self.ctx.log.error("wata: the pinned public key is not a usable RSA key")  # noqa: TRY400

    # ------------------------------------------------------------------------------------------- HTTP

    @property
    def _base(self) -> str:
        base = self.config.base_url or (SANDBOX_URL if self.ctx.is_test else LIVE_URL)
        return str(base).rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.config.api_key}", "Accept": "application/json"}

    def _raise_for(self, resp: HttpResponse, what: str) -> None:
        if resp.status in (401, 403):
            raise ProviderError(_T["bad_key"], retryable=False, status=resp.status)
        if resp.status == 429:
            raise ProviderError(_T["rate_limited"], retryable=True, status=resp.status)
        if resp.status >= 500:
            raise ProviderError(
                _T["unavailable"].format(status=resp.status), retryable=True, status=resp.status
            )
        self.ctx.log.warning("wata: %s answered HTTP %s", what, resp.status)
        raise ProviderError(_T["rejected"].format(status=resp.status), retryable=False, status=resp.status)

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
        currency = intent.currency.upper()
        if currency not in MIN_AMOUNT:
            raise ProviderError(_T["currency"], retryable=False)
        amount = intent.amount
        if amount < MIN_AMOUNT[currency]:
            raise ProviderError(_T["too_small"].format(min=MIN_AMOUNT[currency], currency=currency))
        if amount > MAX_AMOUNT:
            raise ProviderError(_T["too_large"])
        pid = intent.payment_id
        expires = datetime.now(UTC) + timedelta(hours=int(self.config.link_hours or 24))
        body: dict[str, Any] = {
            "type": "OneTime",
            "amount": float(intent.amount_text()),  # a JSON number with ≤ 2 decimals, as documented
            "currency": currency,
            "description": (intent.description or "Оплата")[:_DESCRIPTION_MAX],
            "orderId": pid,  # opaque: never a Telegram id or a subscription link
            "expirationDateTime": _iso_z(expires),
        }
        if intent.return_url:
            body["successRedirectUrl"] = intent.return_url
            body["failRedirectUrl"] = intent.return_url
        resp = await self.ctx.http.request("POST", f"{self._base}/links", headers=self._headers(), json=body)
        if resp.status not in (200, 201):
            self._raise_for(resp, "create")
        data = self._json(resp)
        pay_url = data.get("url")
        if _id(data.get("id")) is None or not isinstance(pay_url, str) or not re.match(r"https?://", pay_url):
            raise ProviderError(_T["bad_answer"], retryable=False, status=resp.status)
        expires_at: datetime | None = expires
        if data.get("expirationDateTime") not in (None, ""):
            try:
                expires_at = parse_timestamp(data["expirationDateTime"])
            except ValueError:
                expires_at = expires
        return Checkout(kind="url", external_id=pid, pay_url=pay_url, expires_at=expires_at)

    # ----------------------------------------------------------------------------------- public key

    async def _public_key(self, *, refresh: bool = False) -> RsaPublicKey | None:
        """The verification key: pinned, cached (memory, then ``ctx.kv``) or fetched. ``refresh`` re-fetches
        unless a fetch happened within the cooldown. ``None`` when no key can be obtained."""
        if self._pinned is not None or self.config.public_key:
            return self._pinned
        now = time.monotonic()
        if not refresh and self._key is not None and now - self._key_at < PUBLIC_KEY_TTL_S:
            return self._key
        if not refresh and self._key is None:
            cached = await self._kv_key()
            if cached is not None:
                return cached
        if self._fetch_at is not None and now - self._fetch_at < KEY_REFRESH_COOLDOWN_S:
            return self._key
        self._fetch_at = now
        try:
            resp = await self.ctx.http.request(
                "GET", f"{self._base}/public-key", headers={"Accept": "application/json"}
            )
        except ProviderError:
            self.ctx.log.warning("wata: public key fetch failed (transport)")
            return self._key
        if resp.status != 200:
            self.ctx.log.warning("wata: public key fetch answered HTTP %s", resp.status)
            return self._key
        try:
            pem = resp.json().get("value")
            key = load_public_key(pem)
        except (ValueError, AttributeError, TypeError):
            self.ctx.log.warning("wata: public key answer is not an RSA key")
            return self._key
        self._key, self._key_at = key, now
        try:
            await self.ctx.kv.set(KV_KEY, {"pem": pem, "fetched_at": datetime.now(UTC).timestamp()})
        except Exception:  # noqa: BLE001 - the cache is an optimisation only
            self.ctx.log.warning("wata: could not cache the public key")
        return key

    async def _kv_key(self) -> RsaPublicKey | None:
        try:
            stored = await self.ctx.kv.get(KV_KEY)
        except Exception:  # noqa: BLE001 - the cache is an optimisation only
            return None
        if not isinstance(stored, dict):
            return None
        try:
            age = datetime.now(UTC).timestamp() - float(stored.get("fetched_at", 0))
            if not 0 <= age < PUBLIC_KEY_TTL_S:
                return None
            key = load_public_key(str(stored.get("pem") or ""))
        except (TypeError, ValueError):
            return None
        self._key, self._key_at = key, time.monotonic() - age
        return key

    # ---------------------------------------------------------------------------------------- webhook

    async def parse_webhook(self, req: WebhookRequest) -> ProviderEvent:
        raw_signature = (req.header("X-Signature") or "").strip()
        if not raw_signature:
            raise WebhookRejected("missing signature", status=401)
        try:
            signature = base64.b64decode(raw_signature, validate=True)
        except (binascii.Error, ValueError):
            raise WebhookRejected("signature is not base64", status=401) from None
        key = await self._public_key()
        if key is None:
            raise WebhookRejected("public key unavailable", status=503)  # WATA retries post-payment hooks
        if not rsa_sha512_verify(key, signature, req.body):
            fresh = await self._public_key(refresh=True)  # key rotation: at most one re-fetch a minute
            if fresh is None or fresh is key or not rsa_sha512_verify(fresh, signature, req.body):
                raise WebhookRejected("bad signature", status=401)
        data = req.json()
        if not isinstance(data, dict):
            raise WebhookRejected("malformed", status=400)
        kind = _lower(data.get("kind")) or "payment"
        raw_status = _lower(data.get("transactionStatus"))
        if kind == "refund":
            if raw_status != "paid":
                raise WebhookIgnored("refund in progress")
            state = PaymentState.REFUNDED
        elif kind == "payment" and raw_status in PAYMENT_STATUS:
            state = PAYMENT_STATUS[raw_status]
        else:
            self.ctx.log.warning("wata: webhook %r/%r ignored", kind[:20], raw_status[:20])
            raise WebhookIgnored("unknown status")
        if _id(data.get("orderId")) is None:
            # Authentic but not ours (a link made in the cabinet, a refund without orderId): answer 200 so
            # WATA does not retry for 32 hours.
            raise WebhookIgnored("no orderId")
        status = self._status(data, state, malformed=WebhookRejected)
        return ProviderEvent(
            state=status.state,
            external_id=status.external_id,
            payment_id=status.payment_id,
            amount=status.amount,
            currency=status.currency,
            paid_at=status.paid_at,
            is_test=self.ctx.is_test,
            signed_at=None,
            summary={
                "kind": kind,
                "status": raw_status,
                "transaction_id": _id(data.get("transactionId")),
                "link_id": _id(data.get("paymentLinkId")),
                "type": _id(data.get("transactionType")),
                "error_code": _id(data.get("errorCode")),
                "amount": None if status.amount is None else str(status.amount),
                "currency": status.currency,
            },
        )

    def _status(
        self, data: Mapping[str, Any], state: PaymentState, *, malformed: type[Exception]
    ) -> ProviderStatus:
        order = _id(data.get("orderId"))
        if order is None:
            raise _malformed(malformed, "no orderId")
        amount: Decimal | None = None
        if data.get("amount") not in (None, ""):
            try:
                amount = parse_amount(data["amount"])
            except (TypeError, ValueError):
                raise _malformed(malformed, "bad amount") from None
        currency = data.get("currency")
        if not isinstance(currency, str) or not re.fullmatch(r"[A-Za-z]{3}", currency.strip()):
            raise _malformed(malformed, "bad currency")
        paid_at: datetime | None = None
        if state is PaymentState.PAID and data.get("paymentTime") not in (None, ""):
            try:
                paid_at = parse_timestamp(data["paymentTime"])
            except ValueError:
                paid_at = None
        return ProviderStatus(
            state=state,
            external_id=order,  # orderId is our external id (see module docstring)
            payment_id=_our_payment_id(order),
            amount=amount,
            currency=currency.strip().upper(),
            paid_at=paid_at,
            is_test=self.ctx.is_test,
        )

    # ----------------------------------------------------------------------------------------- status

    async def fetch_status(self, ids: Sequence[str]) -> list[ProviderStatus]:
        """``GET /transactions/?orderId=<id>`` per invoice (no batch API; one GET per 30 s per object).
        Several attempts may exist for one link: a ``Paid`` payment wins, a later ``Paid`` refund of it makes
        the invoice ``refunded``."""
        result: list[ProviderStatus] = []
        for order in dict.fromkeys(ids):
            if not order:
                continue
            resp = await self.ctx.http.request(
                "GET",
                f"{self._base}/transactions/",
                headers=self._headers(),
                params={"orderId": order, "maxResultCount": "100"},
            )
            if resp.status == 404:
                continue
            if resp.status != 200:
                self._raise_for(resp, "status")
            items = self._json(resp).get("items")
            if not isinstance(items, list):
                raise ProviderError(_T["bad_answer"], retryable=True, status=resp.status)
            status = self._from_transactions(order, [i for i in items if isinstance(i, dict)])
            if status is not None:
                result.append(status)
        return result

    def _from_transactions(self, order: str, items: list[dict[str, Any]]) -> ProviderStatus | None:
        mine = [i for i in items if _id(i.get("orderId")) == order]  # never trust a server-side filter alone
        payments = [i for i in mine if (_lower(i.get("kind")) or "payment") == "payment"]
        paid = [i for i in payments if _lower(i.get("status")) == "paid"]
        try:
            if paid:
                tx = paid[0]
                paid_ids = {_id(p.get("id")) for p in paid} - {None}
                refunded = any(
                    _lower(i.get("kind")) == "refund"
                    and _lower(i.get("status")) == "paid"
                    and _id(i.get("originalTransactionId")) in paid_ids
                    for i in items
                )
                state = PaymentState.REFUNDED if refunded else PaymentState.PAID
                return self._status(tx, state, malformed=ProviderError)
            if payments:
                pending = any(_lower(i.get("status")) == "pending" for i in payments)
                state = PaymentState.PROCESSING if pending else PaymentState.CREATED
                return self._status(payments[0], state, malformed=ProviderError)
        except ProviderError:
            self.ctx.log.warning("wata: unreadable transaction of %s skipped", order[:40])
        return None

    # ------------------------------------------------------------------------------------------ probe

    async def test_credentials(self) -> Probe:
        """No side effects: lists at most one link (token check) and loads the webhook public key."""
        resp = await self.ctx.http.request(
            "GET", f"{self._base}/links/", headers=self._headers(), params={"maxResultCount": "1"}
        )
        if resp.status in (401, 403):
            return Probe(False, _T["bad_key"])
        if resp.status == 429 or resp.status >= 500:
            return Probe(False, _T["unavailable"].format(status=resp.status))
        try:
            data = resp.json()
        except ValueError:
            return Probe(False, _T["probe_not_wata"])
        if resp.status != 200 or not isinstance(data, dict):
            return Probe(False, _T["rejected"].format(status=resp.status))
        if await self._public_key() is None:
            return Probe(False, _T["probe_key"])
        return Probe(True, _T["probe_ok"], {"sandbox": self._base == SANDBOX_URL})


def _malformed(kind: type[Exception], reason: str) -> Exception:
    if kind is WebhookRejected:
        return WebhookRejected(reason, status=400)
    return ProviderError(_T["bad_answer"], retryable=False)
