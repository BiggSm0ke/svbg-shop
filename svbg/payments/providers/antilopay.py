"""Antilopay — Russian cards and SBP through the Antilopay H2H API (07 §4.2, §4.4).

Written from scratch from our specification ``docs/providers/antilopay.md`` (official documentation «Antilopay
API: Описание интерфейса для Мерчантов», PDF v1.53 of 2026-09-25, <https://antilopay.com/api-docs>, checked on
2026-10-02). Imports only :mod:`svbg.sdk` and the standard library.

* **Requests** (``https://lk.antilopay.com/api/v1/``): the JSON body is serialized **once**, compactly, signed
  ``X-Apay-Sign = base64(RSASSA-PKCS1-v1_5(SHA-256(body), merchant private key))`` and sent as exactly those
  bytes with ``X-Apay-Secret-Id`` and ``X-Apay-Sign-Version: 1``. The business result is the ``code`` field
  (``0`` = success). RSA signing and verification are implemented here on the standard library (``pow``, CRT
  with blinding, every signature checked before use), so the plugin stays SDK-only.
* ``payment/create``: ``order_id`` is **only** the opaque payment id. It is also our ``external_id``, because
  ``payment/check`` finds a payment only by the merchant's ``order_id`` (Antilopay's ``APAY…`` id goes to the
  event summary). A lost answer (transport error, 5xx) or ``code 5`` (``order_id`` not unique) → the invoice
  is read back with ``payment/check`` before giving up. Antilopay requires the buyer's email or phone: the bot
  knows neither, so the owner's address from the settings is sent (spec §8 q.4 — owner decision).
* **Callback**: ``X-Apay-Callback = base64(RSA-SHA256(raw body))`` verified with the project's public key,
  ``X-Apay-Callback-Version`` must be ``1``, the sender must be one of Antilopay's published addresses
  (switchable). The documented key «starts with ``MF``», i.e. RSA ≤ 512 bits, which can be factored: with a
  key shorter than 2048 bits (or ``confirm_via_api`` on, the default) a paid callback carries no currency, so
  the core re-reads the payment with ``payment/check`` and reconciles ``original_amount`` before crediting;
  a chargeback/reversal callback is confirmed inline by ``payment/check``.
* ``type=refund`` callbacks carry Antilopay's payment id only (no ``order_id``), so they cannot be matched to
  an invoice and are acknowledged; refunds are seen through ``refunds[]`` of ``payment/check``. Withdrawal and
  Steam top-up callbacks are acknowledged and ignored.
* No test mode, no batch status, no published amount limits (spec §4.7, §7).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import math
import re
import secrets
from collections.abc import Mapping, Sequence
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
    RefundResult,
    WebhookAuth,
    WebhookIgnored,
    WebhookRejected,
    WebhookRequest,
    choice,
    flag,
    parse_amount,
    secret,
    text,
    url,
)

__all__ = [
    "ANTILOPAY_IPS",
    "DEFAULT_BASE_URL",
    "STATUS_MAP",
    "STRONG_KEY_BITS",
    "Antilopay",
    "AntilopayConfig",
    "AntilopayError",
    "RsaKeyError",
    "RsaPrivateKey",
    "RsaPublicKey",
    "amount_json",
    "client_ip",
    "load_private_key",
    "load_public_key",
    "rsa_sha256_sign",
    "rsa_sha256_verify",
    "signed_body",
]

DEFAULT_BASE_URL: Final = "https://lk.antilopay.com/api/v1/"
#: Callback senders published by Antilopay (spec §4.3, document §10).
ANTILOPAY_IPS: Final = ("81.177.221.226", "87.228.9.243")
#: A callback key shorter than this is treated as forgeable: every paid callback is re-read via the API.
STRONG_KEY_BITS: Final = 2048
_MIN_RSA_BITS: Final = 512  # the documented example keys are 512-bit; anything shorter is refused
#: ``payment/check`` / callback ``status`` → SDK state (spec §4.4).
STATUS_MAP: Final[Mapping[str, PaymentState]] = {
    "PENDING": PaymentState.CREATED,
    "SUCCESS": PaymentState.PAID,
    "FAIL": PaymentState.FAILED,
    "CANCEL": PaymentState.CANCELED,
    "EXPIRED": PaymentState.EXPIRED,
    "CHARGEBACK": PaymentState.CHARGEBACK,
    "REVERSED": PaymentState.REFUNDED,
}
_TAKEN_BACK: Final = (PaymentState.CHARGEBACK, PaymentState.REFUNDED)
_IGNORED_TYPES: Final = frozenset(
    {"withdraw", "topup", "popup"}
)  # «popup» — a typo in the document's example
_PREFER: Final[Mapping[MethodKind, tuple[str, ...]]] = {
    MethodKind.CARD: ("CARD_RU",),
    MethodKind.SBP: ("SBP",),
}
_CODE_NOT_FOUND: Final = 8
_CODE_ORDER_TAKEN: Final = 5
_CODE_REFUND_ORDER_TAKEN: Final = 18
_BAD_KEY_CODES: Final = frozenset({1, 2, 3})
_RETRY_CODES: Final = frozenset({500, 1000})
_ID_MAX: Final = 200
_ORDER_MAX: Final = 100
_URL_MAX: Final = 500
_TEXT_MAX: Final = 128
_UUID_RE: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_IP_CHECK_OFF: Final = frozenset({"off", "-", "*", "нет", "выкл"})

_T: Final = {
    "bad_key": "Antilopay отклонила ключи (код {code}) — проверьте идентификатор мерчанта и секретный ключ",
    "ip": "Antilopay не разрешает запросы с IP сервера бота (код 22) — добавьте его в настройках проекта",
    "project": "Antilopay не нашла проект или он не подтверждён (код {code}) — проверьте ID проекта",
    "customer_ip": "Antilopay требует IP покупателя (код 32) — заполните «IP покупателя для кассы»",
    "too_small": "сумма меньше минимальной для этого проекта Antilopay (код 20)",
    "too_large": "сумма больше максимальной для этого проекта Antilopay (код {code})",
    "limit": "исчерпан лимит платежей проекта Antilopay (код {code})",
    "rejected": "Antilopay отклонила запрос (код {code}){detail}",
    "unavailable": "Antilopay временно недоступна ({what})",
    "http": "Antilopay отклонила запрос (HTTP {status})",
    "bad_answer": "Antilopay вернула непонятный ответ",
    "currency": "Antilopay принимает только RUB",
    "closed": "счёт с этим номером в Antilopay уже закрыт ({status})",
    "taken": "номер счёта уже занят в Antilopay, а сам счёт не найден",
    "not_found": "платёж не найден в Antilopay",
    "probe_ok": "Ключи приняты, Antilopay доступна",
    "probe_weak": "; внимание: ключ проверки callback всего {bits} бит — такую подпись можно подделать, "
    "поэтому каждая оплата перепроверяется запросом статуса (запросите у Antilopay ключ 2048 бит)",
    "probe_not_apay": "по адресу API отвечает не Antilopay — проверьте «Адрес API»",
    # key problems (shown in the wizard)
    "key_non_ascii": "{what}: в ключе есть не-латинские буквы (например, кириллическая «а», если ключ "
    "скопирован из PDF) — скопируйте ключ заново из кабинета",
    "key_base64": "{what}: ключ должен быть в Base64 (одна строка без пробелов)",
    "key_swapped_private": "секретный ключ: вставлен публичный ключ — нужен секретный ключ мерчанта "
    "(начинается с MII)",
    "key_swapped_public": "ключ проверки callback: вставлен секретный ключ — нужен публичный ключ проекта",
    "key_format": "{what}: это не RSA-ключ в ожидаемом формате",
    "key_small": "{what}: ключ RSA короче {bits} бит не принимается",
}
_WHAT_PRIVATE: Final = "секретный ключ"
_WHAT_PUBLIC: Final = "ключ проверки callback"


class AntilopayConfig(ConfigModel):
    secret_id = text(
        "Идентификатор мерчанта (Secret ID)",
        "Передаётся в заголовке X-Apay-Secret-Id каждого запроса.",
        where="личный кабинет Antilopay (lk.antilopay.com) → раздел API → идентификатор Мерчанта (Secret ID)",
    )
    project_id = text(
        "Идентификатор проекта",
        "project_identificator — проект (магазин), в котором создаются платежи.",
        where="кабинет Antilopay → «Проекты» → ваш проект → идентификатор вида PE8BED46C045139256",
        pattern=r"[A-Za-z0-9_-]{1,64}",
    )
    private_key = secret(
        "Секретный ключ мерчанта",
        "RSA-ключ в Base64 (PKCS#8), им подписываются запросы к API. Всегда начинается с MII. Не путайте с "
        "публичным ключом проекта и с отдельным ключом для выплат (он боту не нужен).",
        where="кабинет Antilopay → раздел API → секретный ключ (выдаётся при подключении API)",
    )
    callback_public_key = text(
        "Публичный ключ проекта для callback",
        "Им проверяется подпись уведомлений Antilopay (заголовок X-Apay-Callback). Выдаётся отдельно для "
        "каждого подтверждённого проекта.",
        where="кабинет Antilopay → «Проекты» → ваш проект → настройки callback → публичный ключ",
    )
    customer_email = text(
        "Email покупателя для кассы",
        "Antilopay требует email или телефон покупателя в каждом платеже, а бот их не знает. Передаётся этот "
        "адрес (например, почта поддержки магазина) — данные покупателя кассе не уходят.",
        where="ваша почта поддержки или почта для чеков",
        pattern=r"[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,24}",
    )
    customer_ip = text(
        "IP покупателя для кассы",
        "Только если Antilopay отвечает «Данные Покупателя должны содержать ip» (код 32): бот не знает IP "
        "покупателя, укажите внешний IP сервера бота.",
        where="внешний IP вашего сервера (нужен не всем проектам)",
        required=False,
        advanced=True,
        pattern=r"[0-9A-Fa-f:.]{3,45}",
    )
    product_type = choice(
        "Тип предмета расчёта",
        ("services", "goods"),
        "product_type платежа: services — услуги (подписка, пополнение баланса), goods — товары.",
        where="по договору с Antilopay; для VPN-подписки — services",
        default="services",
        required=False,
        advanced=True,
    )
    vat = choice(
        "НДС, %",
        ("10", "22"),
        "Обязателен, только если ваша система налогообложения — ОСНО. Пусто — не передаётся.",
        where="ваша система налогообложения (у бухгалтера)",
        required=False,
        advanced=True,
    )
    confirm_via_api = flag(
        "Перепроверять оплату через API",
        "Да (рекомендуется): callback только запускает проверку платежа запросом payment/check, деньги "
        "зачисляются по ответу API. Нет: зачисление прямо по подписанному callback — только если ключ "
        "проверки callback не короче 2048 бит (с коротким ключом проверка через API включена всегда).",
        where="на ваше усмотрение; по умолчанию включено",
        default=True,
        advanced=True,
    )
    allowed_ips = text(
        "IP-адреса Antilopay",
        "С каких адресов принимаются callback, через запятую (можно подсети). off — проверка по IP выключена "
        "(например, за Cloudflare: адрес отправителя там не виден).",
        where="документация Antilopay → раздел «Callback» → IP-адреса отправителя",
        default=", ".join(ANTILOPAY_IPS),
        required=False,
        advanced=True,
    )
    base_url = url(
        "Адрес API",
        "Точка входа API. Меняйте, только если Antilopay сообщила другую.",
        where="документация Antilopay → «Точка входа API»",
        default=DEFAULT_BASE_URL,
        required=False,
        advanced=True,
    )


# ------------------------------------------------------------------------------------- RSA (stdlib)

_RSA_OID: Final = bytes.fromhex("2a864886f70d010101")  # 1.2.840.113549.1.1.1 rsaEncryption
#: DER DigestInfo prefix of SHA-256 (RFC 8017 §9.2, note 1).
_SHA256_PREFIX: Final = bytes.fromhex("3031300d060960864801650304020105000420")


class RsaKeyError(ValueError):
    """A key that cannot be used; ``human`` is shown to the owner (Russian)."""

    def __init__(self, human: str) -> None:
        super().__init__(human)
        self.human = human


class RsaPublicKey:
    """An RSA public key ``(n, e)``."""

    __slots__ = ("bits", "e", "n", "size")

    def __init__(self, n: int, e: int) -> None:
        if n.bit_length() < _MIN_RSA_BITS or e < 3 or e % 2 == 0 or e >= n:
            raise ValueError("unacceptable RSA public key")
        self.n = n
        self.e = e
        self.bits = n.bit_length()
        self.size = (self.bits + 7) // 8


class RsaPrivateKey:
    """An RSA private key with its CRT components (checked for consistency)."""

    __slots__ = ("d", "dp", "dq", "p", "public", "q", "qinv")

    def __init__(self, n: int, e: int, d: int, p: int, q: int) -> None:
        self.public = RsaPublicKey(n, e)
        if p * q != n or not 1 < d < n or pow(pow(2, e, n), d, n) != 2:
            raise ValueError("inconsistent RSA private key")
        self.d, self.p, self.q = d, p, q
        self.dp, self.dq = d % (p - 1), d % (q - 1)
        self.qinv = pow(q, -1, p)

    def __repr__(self) -> str:
        return f"RsaPrivateKey(bits={self.public.bits})"  # never the numbers


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


def _integers(der: bytes, start: int, end: int) -> list[int]:
    values: list[int] = []
    pos = start
    while pos < end:
        v_start, v_end = _der(der, pos, 0x02)
        if v_end == v_start or der[v_start] & 0x80:
            raise ValueError("bad INTEGER")
        values.append(int.from_bytes(der[v_start:v_end], "big"))
        pos = v_end
    if pos != end:
        raise ValueError("bad SEQUENCE")
    return values


def _seq(der: bytes) -> tuple[int, int]:
    start, end = _der(der, 0, 0x30)
    if end != len(der):
        raise ValueError("trailing data")
    return start, end


def _pkcs1_public_numbers(der: bytes) -> tuple[int, int]:
    values = _integers(der, *_seq(der))
    if len(values) != 2:
        raise ValueError("bad RSAPublicKey")
    return values[0], values[1]


def _spki_numbers(der: bytes) -> tuple[int, int]:
    start, end = _seq(der)
    alg_start, alg_end = _der(der, start, 0x30)
    oid_start, oid_end = _der(der, alg_start, 0x06)
    if der[oid_start:oid_end] != _RSA_OID:
        raise ValueError("not an RSA key")
    bits_start, bits_end = _der(der, alg_end, 0x03)
    if bits_end != end or bits_end == bits_start or der[bits_start] != 0:
        raise ValueError("bad BIT STRING")
    return _pkcs1_public_numbers(der[bits_start + 1 : bits_end])


def _pkcs1_private_numbers(der: bytes) -> tuple[int, ...]:
    values = _integers(der, *_seq(der))
    if len(values) < 9 or values[0] != 0:  # two-prime keys only (version 0)
        raise ValueError("bad RSAPrivateKey")
    return tuple(values[1:6])  # n, e, d, p, q (the CRT values are recomputed)


def _pkcs8_numbers(der: bytes) -> tuple[int, ...]:
    start, _end = _seq(der)
    v_start, v_end = _der(der, start, 0x02)
    if der[v_start:v_end] not in (b"\x00", b"\x01"):
        raise ValueError("bad PrivateKeyInfo version")
    alg_start, alg_end = _der(der, v_end, 0x30)
    oid_start, oid_end = _der(der, alg_start, 0x06)
    if der[oid_start:oid_end] != _RSA_OID:
        raise ValueError("not an RSA key")
    key_start, key_end = _der(der, alg_end, 0x04)
    return _pkcs1_private_numbers(der[key_start:key_end])


def _first(der: bytes, *parsers: Any) -> tuple[int, ...] | None:
    for parser in parsers:
        try:
            return tuple(parser(der))
        except ValueError:
            continue
    return None


def _key_der(value: str, what: str) -> bytes:
    """DER bytes of a key given as PEM or bare Base64 (literal ``\\n`` tolerated)."""
    textual = value.replace("\\n", "\n").strip()
    if not textual.isascii():
        raise RsaKeyError(_T["key_non_ascii"].format(what=what))
    body = re.sub(r"-----(BEGIN|END)[A-Z ]*-----", "", textual)
    try:
        der = base64.b64decode(re.sub(r"\s+", "", body), validate=True)
    except (binascii.Error, ValueError):
        raise RsaKeyError(_T["key_base64"].format(what=what)) from None
    if not der:
        raise RsaKeyError(_T["key_base64"].format(what=what))
    return der


def load_public_key(value: str) -> RsaPublicKey:
    """The callback key: Base64 DER SubjectPublicKeyInfo (as Antilopay issues it), PKCS#1, or either as PEM.
    :class:`RsaKeyError` with an owner-facing reason otherwise (a private key pasted here is recognized)."""
    der = _key_der(value, _WHAT_PUBLIC)
    numbers = _first(der, _spki_numbers, _pkcs1_public_numbers)
    if numbers is None:
        if _first(der, _pkcs8_numbers, _pkcs1_private_numbers) is not None:
            raise RsaKeyError(_T["key_swapped_public"])
        raise RsaKeyError(_T["key_format"].format(what=_WHAT_PUBLIC))
    n, e = numbers
    if n.bit_length() < _MIN_RSA_BITS:
        raise RsaKeyError(_T["key_small"].format(what=_WHAT_PUBLIC, bits=_MIN_RSA_BITS))
    try:
        return RsaPublicKey(n, e)
    except ValueError:
        raise RsaKeyError(_T["key_format"].format(what=_WHAT_PUBLIC)) from None


def load_private_key(value: str) -> RsaPrivateKey:
    """The merchant key: Base64 DER PKCS#8 (``MII…``, as Antilopay issues it), PKCS#1, or either as PEM.
    :class:`RsaKeyError` with an owner-facing reason otherwise (a public key pasted here is recognized)."""
    der = _key_der(value, _WHAT_PRIVATE)
    numbers = _first(der, _pkcs8_numbers, _pkcs1_private_numbers)
    if numbers is None:
        if _first(der, _spki_numbers, _pkcs1_public_numbers) is not None:
            raise RsaKeyError(_T["key_swapped_private"])
        raise RsaKeyError(_T["key_format"].format(what=_WHAT_PRIVATE))
    n, e, d, p, q = numbers
    if n.bit_length() < _MIN_RSA_BITS:
        raise RsaKeyError(_T["key_small"].format(what=_WHAT_PRIVATE, bits=_MIN_RSA_BITS))
    try:
        return RsaPrivateKey(n, e, d, p, q)
    except (ValueError, ZeroDivisionError):
        raise RsaKeyError(_T["key_format"].format(what=_WHAT_PRIVATE)) from None


def _emsa_sha256(message: bytes, size: int) -> bytes:
    """EMSA-PKCS1-v1_5 encoding with SHA-256 (RFC 8017 §9.2)."""
    t = _SHA256_PREFIX + hashlib.sha256(message).digest()
    if size < len(t) + 11:
        raise ValueError("RSA key too short for SHA-256")
    return b"\x00\x01" + b"\xff" * (size - len(t) - 3) + b"\x00" + t


def rsa_sha256_sign(key: RsaPrivateKey, message: bytes) -> bytes:
    """RSASSA-PKCS1-v1_5 with SHA-256 (RFC 8017 §8.2.1). Deterministic output; the private operation is
    blinded (random ``r``) and done with CRT, and the result is verified before it is returned."""
    pub = key.public
    m = int.from_bytes(_emsa_sha256(message, pub.size), "big")
    while True:
        r = secrets.randbelow(pub.n - 3) + 2
        if math.gcd(r, pub.n) == 1:
            break
    blinded = (m * pow(r, pub.e, pub.n)) % pub.n
    m1 = pow(blinded, key.dp, key.p)
    m2 = pow(blinded, key.dq, key.q)
    h = (key.qinv * (m1 - m2)) % key.p
    s = ((m2 + h * key.q) * pow(r, -1, pub.n)) % pub.n
    if pow(s, pub.e, pub.n) != m:
        raise ValueError("RSA signature self-check failed")
    return s.to_bytes(pub.size, "big")


def rsa_sha256_verify(key: RsaPublicKey, signature: bytes, message: bytes) -> bool:
    """RSASSA-PKCS1-v1_5 with SHA-256 (RFC 8017 §8.2.2): the encoded message is rebuilt and compared in
    constant time (no parsing of the decrypted block)."""
    if len(signature) != key.size:
        return False
    s = int.from_bytes(signature, "big")
    if s >= key.n:
        return False
    em = pow(s, key.e, key.n).to_bytes(key.size, "big")
    try:
        expected = _emsa_sha256(message, key.size)
    except ValueError:
        return False
    return hmac.compare_digest(em, expected)


# --------------------------------------------------------------------------------------------- helpers


def amount_json(value: Decimal) -> int | float:
    """A rouble amount as the JSON number Antilopay expects (≤ 2 decimals, no binary noise): ``100``,
    ``100.1``, ``100.01``. ``ValueError`` for more decimals."""
    if value != value.quantize(Decimal("0.01")) or value < 0:
        raise ValueError(f"not a rouble amount: {value}")
    if value == value.to_integral_value():
        return int(value)
    number = float(value)
    if Decimal(repr(number)) != value:  # the shortest repr of a ≤ 2-decimal float is that decimal
        raise ValueError(f"amount {value} has no exact JSON form")
    return number


def signed_body(payload: Mapping[str, Any]) -> bytes:
    """The request body serialized once: compact JSON, UTF-8 (these exact bytes are signed and sent)."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _ip(value: str | None) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    if not value:
        return None
    try:
        addr = ipaddress.ip_address(value.strip().strip("[]"))
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def client_ip(req: WebhookRequest) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The sender's address: the TCP peer when it is public; behind a local reverse proxy (loopback or private
    peer) the right-most ``X-Forwarded-For`` entry — the one our proxy added — or ``X-Real-IP``."""
    peer = _ip(req.remote)
    if peer is None:
        return None
    if not (peer.is_loopback or peer.is_private):
        return peer
    forwarded = req.header("X-Forwarded-For")
    if forwarded:
        entries = [e.strip() for e in forwarded.split(",") if e.strip()]
        return _ip(entries[-1]) if entries else None
    real = req.header("X-Real-IP")
    return _ip(real) if real else peer


def _networks(raw: str | None) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    if raw is None or raw.strip().lower() in _IP_CHECK_OFF:
        return []
    nets = []
    for part in re.split(r"[,\s;]+", raw):
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            continue
    return nets


def _id(value: Any, limit: int = _ID_MAX) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if 0 < len(value) <= limit else None


def _our_payment_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower()
    return candidate if _UUID_RE.fullmatch(candidate) else None


def _currency(value: Any) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z]{3}", value.strip()):
        return value.strip().upper()  # «rub» in requests, «RUB» in answers (spec §8 q.7)
    return None


def _decimal_json(body: bytes) -> Any:
    return json.loads(body.decode("utf-8"), parse_float=Decimal)


def _header(resp: HttpResponse, name: str) -> str | None:
    low = name.lower()
    for key, value in resp.headers.items():
        if key.lower() == low:
            return value
    return None


def _clip(value: str, limit: int) -> str:
    value = " ".join(value.split())
    return value[:limit] if value else "Оплата"


class AntilopayError(ProviderError):
    """A business error of the Antilopay API (``code ≠ 0``)."""

    def __init__(self, human: str, *, code: int, retryable: bool = False) -> None:
        super().__init__(human, retryable=retryable, status=200)
        self.code = code


def _code_error(code: int, data: Mapping[str, Any]) -> AntilopayError:
    detail = data.get("error")
    detail_text = f": {detail.strip()[:120]}" if isinstance(detail, str) and detail.strip() else ""
    if code in _BAD_KEY_CODES:
        return AntilopayError(_T["bad_key"].format(code=code), code=code)
    if code == 22:
        return AntilopayError(_T["ip"], code=code)
    if code == 32:
        return AntilopayError(_T["customer_ip"], code=code)
    if code in (4, 6):
        return AntilopayError(_T["project"].format(code=code), code=code)
    if code == 20:
        return AntilopayError(_T["too_small"], code=code)
    if code in (26, 31):
        return AntilopayError(_T["too_large"].format(code=code), code=code)
    if code == 23:
        return AntilopayError(_T["limit"].format(code=code), code=code)
    if code in _RETRY_CODES:
        return AntilopayError(_T["unavailable"].format(what=f"код {code}"), code=code, retryable=True)
    return AntilopayError(_T["rejected"].format(code=code, detail=detail_text), code=code)


class Antilopay(PaymentProvider):
    """See module docstring."""

    manifest = Manifest(
        slug="antilopay",
        title="Antilopay",
        method_kinds=(MethodKind.CARD, MethodKind.SBP),
        currencies=("RUB",),
        config=AntilopayConfig,
        docs_url="https://antilopay.com/api-docs",
    )
    capabilities = Capabilities(
        webhook_auth=WebhookAuth.SIGNATURE,
        replay_window_s=None,  # the callback signs no time (ctime is the payment's creation time)
        fetch_status=True,
        batch_status=False,
        refund=True,
        recurring=False,  # the «recurrent» object needs the desk's permission and is not wired in
        receipt_54fz=False,
        in_chat_invoice=False,
        redirect=True,
        webhook=True,
    )

    def __init__(self, config: ConfigModel, ctx: PluginContext) -> None:
        super().__init__(config, ctx)
        self._private: RsaPrivateKey | None = None
        self._public: RsaPublicKey | None = None
        self._private_problem: str | None = None
        self._public_problem: str | None = None
        try:
            self._private = load_private_key(str(self.config.private_key))
        except RsaKeyError as exc:
            self._private_problem = exc.human
            self.ctx.log.error("antilopay: the merchant private key is unusable")  # noqa: TRY400
        try:
            self._public = load_public_key(str(self.config.callback_public_key))
        except RsaKeyError as exc:
            self._public_problem = exc.human
            self.ctx.log.error("antilopay: the callback public key is unusable")  # noqa: TRY400
        if self._public is not None and self._public.bits < STRONG_KEY_BITS:
            self.ctx.log.warning(
                "antilopay: the callback key has %d bits — every paid callback is re-read via payment/check",
                self._public.bits,
            )
        # A paid callback is credited directly only with a strong key and the owner's explicit choice.
        self.confirm_via_api = (
            bool(self.config.confirm_via_api) or self._public is None or self._public.bits < STRONG_KEY_BITS
        )
        raw = str(self.config.allowed_ips or "").strip().lower()
        self._ip_check_on = raw not in _IP_CHECK_OFF
        self._nets = _networks(self.config.allowed_ips)

    # ------------------------------------------------------------------------------------------- HTTP

    def _url(self, method: str) -> str:
        return str(self.config.base_url or DEFAULT_BASE_URL).rstrip("/") + "/" + method

    async def _post(self, method: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """A signed POST; the answer's ``code`` must be ``0`` (:class:`AntilopayError` otherwise)."""
        if self._private is None:
            raise ProviderError(self._private_problem or _T["bad_key"].format(code="—"), retryable=False)
        body = signed_body(payload)
        signature = base64.b64encode(rsa_sha256_sign(self._private, body)).decode("ascii")
        resp = await self.ctx.http.request(
            "POST",
            self._url(method),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-Apay-Secret-Id": str(self.config.secret_id),
                "X-Apay-Sign": signature,
                "X-Apay-Sign-Version": "1",
            },
            data=body,
        )
        request_id = (_header(resp, "X-Apay-Request-Id") or "-")[:64]
        if resp.status in (408, 429) or resp.status >= 500:
            self.ctx.log.warning(
                "antilopay: %s answered HTTP %s (request %s)", method, resp.status, request_id
            )
            raise ProviderError(
                _T["unavailable"].format(what=f"HTTP {resp.status}"), retryable=True, status=resp.status
            )
        try:
            data = _decimal_json(resp.body)
        except (UnicodeDecodeError, ValueError):
            data = None
        if resp.status != 200 or not isinstance(data, dict):
            self.ctx.log.warning(
                "antilopay: %s answered HTTP %s (request %s)", method, resp.status, request_id
            )
            if resp.status == 200:
                raise ProviderError(_T["bad_answer"], retryable=True, status=resp.status)
            raise ProviderError(_T["http"].format(status=resp.status), retryable=False, status=resp.status)
        code = data.get("code", 0)
        if isinstance(code, bool) or not isinstance(code, int):
            raise ProviderError(_T["bad_answer"], retryable=True, status=resp.status)
        if code != 0:
            if code != _CODE_NOT_FOUND:
                self.ctx.log.warning("antilopay: %s answered code %s (request %s)", method, code, request_id)
            raise _code_error(code, data)
        return data

    # ----------------------------------------------------------------------------------------- create

    async def create(self, intent: PaymentIntent) -> Checkout:
        if intent.currency.upper() != "RUB":
            raise ProviderError(_T["currency"], retryable=False)
        order = intent.payment_id  # opaque: never a Telegram id or a subscription link
        description = _clip(intent.description or "Оплата", _TEXT_MAX)
        customer: dict[str, Any] = {"email": str(self.config.customer_email)}
        if self.config.customer_ip:
            customer["ip"] = str(self.config.customer_ip)
        payload: dict[str, Any] = {
            "project_identificator": str(self.config.project_id),
            "amount": amount_json(intent.amount),
            "order_id": order,
            "currency": "RUB",
            "product_name": description,
            "product_type": str(self.config.product_type or "services"),
            "description": description,
            "customer": customer,
        }
        if self.config.vat:
            payload["vat"] = int(self.config.vat)
        if intent.method_hint in _PREFER:
            payload["prefer_methods"] = list(_PREFER[intent.method_hint])
        back = intent.return_url
        if back and back.startswith("https://") and len(back) <= _URL_MAX:
            payload["success_url"] = back
            payload["fail_url"] = back
        try:
            data = await self._post("payment/create", payload)
        except AntilopayError as exc:
            if exc.code != _CODE_ORDER_TAKEN:
                raise
            existing = await self._existing(order)  # a retry after a lost answer
            if existing is None:
                raise ProviderError(_T["taken"], retryable=False) from None
            return existing
        except ProviderError as exc:
            if not exc.retryable:
                raise
            # The invoice may have been created although the answer was lost: look before a retry.
            try:
                existing = await self._existing(order)
            except ProviderError:
                existing = None
            if existing is None:
                raise
            return existing
        pay_url = data.get("payment_url")
        if _id(data.get("payment_id")) is None or not isinstance(pay_url, str):
            raise ProviderError(_T["bad_answer"], retryable=False)
        if not re.match(r"https?://", pay_url.strip()):
            raise ProviderError(_T["bad_answer"], retryable=False)
        return Checkout(kind="url", external_id=order, pay_url=pay_url.strip())

    async def _existing(self, order: str) -> Checkout | None:
        data = await self._check_raw(order)
        if data is None:
            return None
        raw_status = str(data.get("status") or "").strip().upper()
        pay_url = data.get("payment_url")
        if raw_status != "PENDING":
            raise ProviderError(_T["closed"].format(status=raw_status[:20] or "?"), retryable=False)
        if not isinstance(pay_url, str) or not re.match(r"https?://", pay_url.strip()):
            raise ProviderError(_T["bad_answer"], retryable=False)
        return Checkout(kind="url", external_id=order, pay_url=pay_url.strip())

    # ---------------------------------------------------------------------------------------- webhook

    async def parse_webhook(self, req: WebhookRequest) -> ProviderEvent:
        if self._nets or self._ip_check_on:
            sender = client_ip(req)
            if sender is None or not any(sender in net for net in self._nets):
                raise WebhookRejected("sender not in the Antilopay IP list", status=403)
        version = (req.header("X-Apay-Callback-Version") or "").strip()
        raw_signature = (req.header("X-Apay-Callback") or "").strip()
        if not raw_signature:
            raise WebhookRejected("missing signature", status=401)
        if version != "1":
            raise WebhookRejected("unknown signature version", status=401)
        try:
            signature = base64.b64decode(raw_signature, validate=True)
        except (binascii.Error, ValueError):
            raise WebhookRejected("signature is not base64", status=401) from None
        if self._public is None:
            # Misconfiguration: 503 makes Antilopay retry (every 3 min for an hour) after the owner fixes it.
            raise WebhookRejected("callback key is not configured", status=503)
        if not rsa_sha256_verify(self._public, signature, req.body):
            raise WebhookRejected("bad signature", status=401)
        try:
            data = _decimal_json(req.body)
        except (UnicodeDecodeError, ValueError):
            raise WebhookRejected("malformed", status=400) from None
        if not isinstance(data, dict):
            raise WebhookRejected("malformed", status=400)
        kind = str(data.get("type") or "payment").strip().lower()
        if kind in _IGNORED_TYPES:
            raise WebhookIgnored(f"{kind} callback")
        if kind == "refund":
            self.ctx.log.info(
                "antilopay: refund %s of payment %s is %s (seen through payment/check)",
                _id(data.get("refund_id"), 64),
                _id(data.get("payment_id"), 64),
                str(data.get("status") or "")[:20],
            )
            raise WebhookIgnored("refund callback")
        if kind != "payment":
            self.ctx.log.warning("antilopay: callback of type %r ignored", kind[:20])
            raise WebhookIgnored("unknown type")
        raw_status = str(data.get("status") or "").strip().upper()
        state = STATUS_MAP.get(raw_status)
        if state is None:
            self.ctx.log.warning("antilopay: callback with unknown status %r ignored", raw_status[:20])
            raise WebhookIgnored("unknown status")
        order = _id(data.get("order_id"), _ORDER_MAX)
        if order is None:
            raise WebhookIgnored("no order_id")
        amount: Decimal | None = None
        if data.get("original_amount") not in (None, ""):
            try:
                amount = parse_amount(data["original_amount"])
            except (TypeError, ValueError):
                raise WebhookRejected("bad amount", status=400) from None
        currency = _currency(data.get("currency")) or "RUB"
        summary = {
            "payment_id": _id(data.get("payment_id"), 64),
            "order_id": order,
            "status": raw_status,
            "amount": _text_amount(data.get("amount")),
            "original_amount": None if amount is None else str(amount),
            "fee": _text_amount(data.get("fee")),
            "pay_method": _id(data.get("pay_method"), 20),
            "confirm_via_api": self.confirm_via_api,
        }
        if self.confirm_via_api and state in _TAKEN_BACK:
            confirmed = await self._confirm_taken_back(order)
            return ProviderEvent(
                state=confirmed.state,
                external_id=order,
                payment_id=_our_payment_id(order),
                amount=confirmed.amount,
                currency=confirmed.currency,
                summary={**summary, "confirmed": True},
            )
        return ProviderEvent(
            state=state,
            external_id=order,  # order_id is our external id (see module docstring)
            payment_id=_our_payment_id(order),
            amount=amount,
            # no currency → the core re-reads the payment (fetch_status) before crediting
            currency=None if self.confirm_via_api and state is PaymentState.PAID else currency,
            signed_at=None,
            summary=summary,
        )

    async def _confirm_taken_back(self, order: str) -> ProviderStatus:
        try:
            status = await self._check(order)
        except ProviderError as exc:
            raise WebhookRejected(f"not confirmed yet: {exc.human}", status=503) from None
        if status is None or status.state not in _TAKEN_BACK:
            self.ctx.log.warning("antilopay: chargeback/reversal of %s not confirmed by the API", order[:40])
            raise WebhookIgnored("not confirmed")
        return status

    # ----------------------------------------------------------------------------------------- status

    async def _check_raw(self, order: str) -> dict[str, Any] | None:
        try:
            data = await self._post(
                "payment/check", {"project_identificator": str(self.config.project_id), "order_id": order}
            )
        except AntilopayError as exc:
            if exc.code == _CODE_NOT_FOUND:
                return None
            raise
        if _id(data.get("order_id"), _ORDER_MAX) not in (None, order):
            self.ctx.log.warning("antilopay: payment/check answered for another order")
            return None
        return data

    async def _check(self, order: str) -> ProviderStatus | None:
        data = await self._check_raw(order)
        if data is None:
            return None
        raw_status = str(data.get("status") or "").strip().upper()
        state = STATUS_MAP.get(raw_status)
        if state is None:
            self.ctx.log.warning("antilopay: unknown status %r of %s skipped", raw_status[:20], order[:40])
            return None
        try:
            amount = parse_amount(data.get("original_amount"))
        except (TypeError, ValueError):
            self.ctx.log.warning("antilopay: unreadable amount of %s skipped", order[:40])
            return None
        if state is PaymentState.PAID:
            refunded = _refunded_total(data.get("refunds"))
            if refunded >= amount > 0:
                state = PaymentState.REFUNDED
            elif refunded > 0:
                self.ctx.log.warning("antilopay: payment %s is partially refunded", order[:40])
        return ProviderStatus(
            state=state,
            external_id=order,
            payment_id=_our_payment_id(order),
            amount=amount,
            currency=_currency(data.get("currency")) or "RUB",
        )

    async def fetch_status(self, ids: Sequence[str]) -> list[ProviderStatus]:
        """``payment/check`` per invoice (no batch API); unknown orders (code 8) are absent."""
        result: list[ProviderStatus] = []
        for order in dict.fromkeys(ids):
            if not order:
                continue
            status = await self._check(order)
            if status is not None:
                result.append(status)
        return result

    # ----------------------------------------------------------------------------------------- refund

    async def refund(self, external_id: str, amount_minor: int, currency: str) -> RefundResult:
        """``refund/create`` by Antilopay's payment id (read with ``payment/check``). The refund's own
        ``order_id`` is derived from the invoice and the amount, so a repeated request is recognized (code 18)
        and answered with the existing refund."""
        if currency.upper() != "RUB":
            return RefundResult(False, None, _T["currency"])
        refund_order = f"{external_id}-refund-{amount_minor}"[:_ORDER_MAX]
        project = str(self.config.project_id)
        try:
            data = await self._check_raw(external_id)
            transaction = None if data is None else _id(data.get("payment_id"), 64)
            if transaction is None:
                return RefundResult(False, None, _T["not_found"])
            try:
                answer = await self._post(
                    "refund/create",
                    {
                        "project_identificator": project,
                        "transaction_id": transaction,
                        "order_id": refund_order,
                        "amount": amount_json(Decimal(amount_minor).scaleb(-2)),
                    },
                )
            except AntilopayError as exc:
                if exc.code != _CODE_REFUND_ORDER_TAKEN:
                    raise
                answer = await self._post(
                    "refund/check", {"project_identificator": project, "order_id": refund_order}
                )
        except ProviderError as exc:
            return RefundResult(False, None, exc.human)
        return RefundResult(True, _id(answer.get("refund_id")), "")

    # ------------------------------------------------------------------------------------------ probe

    async def test_credentials(self) -> Probe:
        """No side effects: ``signature/check`` with an empty object (checks Secret ID + private key) and a
        local parse of the callback key (its length is reported)."""
        if self._private_problem:
            return Probe(False, self._private_problem)
        if self._public_problem or self._public is None:
            return Probe(False, self._public_problem or _T["key_format"].format(what=_WHAT_PUBLIC))
        try:
            data = await self._post("signature/check", {})
        except AntilopayError as exc:
            return Probe(False, exc.human)
        except ProviderError as exc:
            if exc.status in (404, 405):
                return Probe(False, _T["probe_not_apay"])
            return Probe(False, exc.human)
        if "code" not in data and str(data.get("status") or "").lower() != "ok":
            return Probe(False, _T["probe_not_apay"])
        bits = self._public.bits
        message = _T["probe_ok"] + (_T["probe_weak"].format(bits=bits) if bits < STRONG_KEY_BITS else "")
        return Probe(
            True,
            message,
            {
                "callback_key_bits": bits,
                "private_key_bits": self._private.public.bits if self._private else None,
                "confirm_via_api": self.confirm_via_api,
            },
        )


def _refunded_total(refunds: Any) -> Decimal:
    total = Decimal(0)
    if not isinstance(refunds, list):
        return total
    for item in refunds:
        if not isinstance(item, Mapping) or str(item.get("status") or "").strip().upper() != "COMPLETE":
            continue
        try:
            total += parse_amount(item.get("amount"))
        except (TypeError, ValueError):
            continue
    return total


def _text_amount(value: Any) -> str | None:
    try:
        return str(parse_amount(value))
    except (TypeError, ValueError):
        return None
