"""Lava Business (lava.ru) — card and SBP invoices through the Lava payment page (07 §4.2).

Specification: ``docs/providers/lava.md`` (official documentation «Lava Business API» v2.1.0
<https://developer.lava.ru/>, the official PHP SDK github.com/LavaDevelop/lava-sdk and the archived official
«WebHook» page, checked 2026-10-02). Written from scratch by the specification; imports only :mod:`svbg.sdk`
and the standard library. Not LAVA.TOP (``gate.lava.top``) — another product.

* Requests are ``POST`` with a raw JSON body to ``https://api.lava.ru/business/...``; the header
  ``Signature = hex(HMAC_SHA256(secret key, exact body bytes))`` (spec §1): the body is serialized once,
  signed and sent as the very same bytes.
* ``invoice/create`` takes ``sum`` (a JSON number made from the :class:`~decimal.Decimal`, never a float),
  ``orderId`` (**only** our opaque payment id), ``shopId``, ``hookUrl`` (our webhook address, per invoice) and
  ``expire`` in **minutes**; the buyer goes to ``data.url``; ``data.id`` is our ``external_id``.
* ``invoice/status`` reads one invoice (no batch method); ``get-available-tariffs`` is the side-effect-free
  probe.
* **Webhook** (JSON, signed with the project's *additional* key): the official sources disagree on the header
  (``Signature`` in the current documentation, ``Authorization`` in the SDK and the archived page) and on what
  is signed (the raw body, or PHP ``json_encode`` of the body with top-level keys sorted — the only form the
  SDK's official test vector matches). Both headers and both candidates are accepted, compared in constant
  time. The webhook carries no currency and no signed time: a ``success`` webhook never credits by itself —
  the core re-reads ``invoice/status`` (spec §4.2 step 4) before anything is paid.
"""

from __future__ import annotations

import json
import math
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
    WebhookResponse,
    constant_time_equal,
    hmac_sha256_hex,
    integer,
    parse_amount,
    secret,
    text,
    url,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "SERVICES",
    "STATUS_MAP",
    "Lava",
    "LavaConfig",
    "json_body",
    "php_canonical",
    "request_signature",
    "webhook_candidates",
]

DEFAULT_BASE_URL: Final = "https://api.lava.ru"
#: ``includeService`` / ``excludeService`` values (spec §2).
SERVICES: Final = ("card", "sbp", "lava_pay_in", "sber_pay", "mir_pay", "mir_card")
#: Invoice ``status`` (``invoice/status`` and the webhook) → SDK state (spec §3, §5).
STATUS_MAP: Final[Mapping[str, PaymentState]] = {
    "created": PaymentState.CREATED,
    "success": PaymentState.PAID,
    "fail": PaymentState.FAILED,
    "expired": PaymentState.EXPIRED,
    "refund": PaymentState.REFUNDED,
}
_INVOICE_TYPE: Final = 1  # webhook ``type``: 1 invoice, 3 payoff, 4 recurring subscription
_ID_MAX: Final = 200
_COMMENT_MAX: Final = 255
_EXPIRE_MAX: Final = 7200
_INT64_MAX: Final = 2**63 - 1
_UUID_RE: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

_T: Final = {
    "bad_key": "Lava отклонила подпись запроса — проверьте «Секретный ключ» и «ID проекта» в кабинете Lava",
    "forbidden": "Lava запретила доступ (HTTP 403) — проверьте, что проект активен и ключи от него",
    "no_shop": "Lava не нашла проект — проверьте «ID проекта»",
    "invalid": "Lava отклонила параметры счёта (HTTP 422){detail}",
    "rejected": "Lava отклонила запрос (HTTP {status}){detail}",
    "unavailable": "Lava временно недоступна (HTTP {status})",
    "bad_answer": "Lava вернула непонятный ответ",
    "currency": "Lava принимает только рубли",
    "amount": "Lava принимает суммы от 1 до 2 000 000 ₽",
    "probe_ok": "Ключ и проект приняты, доступно способов оплаты: {n}. Дополнительный ключ проверится на "
    "первом вебхуке",
    "probe_not_lava": "по адресу API отвечает не Lava — проверьте «Адрес API»",
}


class LavaConfig(ConfigModel):
    shop_id = text(
        "ID проекта",
        "UUID проекта (shopId) в Lava Business.",
        where="кабинет Lava Business (business.lava.ru) → «Проекты» → ваш проект → «Настройки» → ID проекта",
        pattern=r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}",
    )
    secret_key = secret(
        "Секретный ключ",
        "Им подписываются запросы к API Lava (заголовок Signature, HMAC-SHA256 тела).",
        where="кабинет Lava Business → ваш проект → «Настройки» → «Секретный ключ»",
    )
    additional_key = secret(
        "Дополнительный ключ",
        "Им Lava подписывает вебхуки об оплате. Не путайте с секретным ключом.",
        where="кабинет Lava Business → ваш проект → «Настройки» → «Дополнительный ключ»",
    )
    expire_minutes = integer(
        "Время жизни счёта, минут",
        "Параметр expire: сколько минут счёт можно оплатить (Lava: по умолчанию 300, максимум 7200).",
        default=300,
        required=False,
        min=1,
        max=_EXPIRE_MAX,
        advanced=True,
    )
    services = text(
        "Способы оплаты",
        "Какие способы показать на странице Lava (includeService), через запятую: card, sbp, lava_pay_in, "
        "sber_pay, mir_pay, mir_card. Пусто — все, что включены в проекте.",
        where="кабинет Lava Business → ваш проект → «Тарифы» (какие способы подключены)",
        required=False,
        pattern=r"\s*(card|sbp|lava_pay_in|sber_pay|mir_pay|mir_card)(\s*,\s*(card|sbp|lava_pay_in|sber_pay|"
        r"mir_pay|mir_card))*\s*",
        advanced=True,
    )
    base_url = url(
        "Адрес API",
        "Базовый адрес API Lava Business. Меняйте, только если Lava сообщила другой.",
        where="документация Lava (developer.lava.ru) → servers",
        default=DEFAULT_BASE_URL,
        required=False,
        advanced=True,
    )


# ------------------------------------------------------------------------------------------- signatures


class _Raw(str):
    """A JSON token written as is (a number made from a Decimal)."""

    __slots__ = ()


def json_body(fields: Mapping[str, Any]) -> bytes:
    """Compact JSON (``,`` / ``:``) in the given key order; ``None`` values are left out; :class:`_Raw`
    values are written verbatim. These are the exact bytes that are signed and sent."""
    parts = []
    for key, value in fields.items():
        if value is None:
            continue
        if isinstance(value, _Raw):
            token = str(value)
        else:
            token = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        parts.append(json.dumps(key, ensure_ascii=False) + ":" + token)
    return ("{" + ",".join(parts) + "}").encode("utf-8")


def request_signature(secret_key: str, body: bytes) -> str:
    """``hex(HMAC_SHA256(secret key, exact body bytes))`` — header ``Signature`` (spec §1)."""
    return hmac_sha256_hex(secret_key, body)


def _php_float(value: float) -> str:
    """PHP ≥ 7.1 ``json_encode`` of a float (``serialize_precision = -1``): the shortest round-trip digits,
    ``179.0`` for an integral value, ``1.0e-5`` / ``1.0e+25`` outside PHP's fixed-notation range."""
    if math.isnan(value) or math.isinf(value):
        raise ValueError("not a JSON number")
    prefix = "-" if math.copysign(1.0, value) < 0 else ""
    if value == 0:
        return prefix + "0.0"
    _, digits_t, exp = Decimal(repr(abs(value))).as_tuple()
    digits = "".join(map(str, digits_t))
    stripped = digits.rstrip("0")  # repr("179.0") → digits 1790, exponent -1
    power = int(exp) + len(digits) - len(stripped)
    digits = stripped
    decpt = len(digits) + power  # position of the decimal point, as in PHP's zend_gcvt
    if decpt < -3 or decpt > 17:
        exponent = decpt - 1
        return f"{prefix}{digits[0]}.{digits[1:] or '0'}e{'+' if exponent >= 0 else '-'}{abs(exponent)}"
    if decpt <= 0:
        return f"{prefix}0.{'0' * -decpt}{digits}"
    if decpt >= len(digits):
        return f"{prefix}{digits}{'0' * (decpt - len(digits))}.0"
    return f"{prefix}{digits[:decpt]}.{digits[decpt:]}"


def _php_encode(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value) if abs(value) <= _INT64_MAX else _php_float(float(value))
    if isinstance(value, float):
        return _php_float(value)
    if isinstance(value, str):
        # ASCII-only with \uXXXX (lower-case hex, surrogate pairs) and "/" escaped as "\/"
        return json.dumps(value, ensure_ascii=True).replace("/", "\\/")
    if isinstance(value, list):
        return "[" + ",".join(_php_encode(v) for v in value) + "]"
    if isinstance(value, dict):
        keys = list(value)
        if keys == [str(i) for i in range(len(keys))]:  # PHP: an empty or 0..n-1 array is a JSON list
            return "[" + ",".join(_php_encode(value[k]) for k in keys) + "]"
        return "{" + ",".join(_php_encode(str(k)) + ":" + _php_encode(v) for k, v in value.items()) + "}"
    raise TypeError(f"not JSON: {type(value).__name__}")


def php_canonical(body: bytes) -> bytes | None:
    """Candidate A (official SDK): ``json_encode(ksort(json_decode(body, true)))`` with PHP's default flags;
    ``None`` when the body is not a JSON object."""
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    ordered = dict(sorted(data.items(), key=lambda kv: kv[0].encode("utf-8")))
    try:
        return _php_encode(ordered).encode("ascii")
    except (TypeError, ValueError):
        return None


def webhook_candidates(additional_key: str, body: bytes) -> list[str]:
    """Expected webhook signatures: A — over the PHP canonical form (official SDK and its test vector), B —
    over the raw body (current documentation)."""
    found: list[str] = []
    canonical = php_canonical(body)
    if canonical is not None:
        found.append(hmac_sha256_hex(additional_key, canonical))
    found.append(hmac_sha256_hex(additional_key, body))
    return found


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


def _sum_token(amount: Decimal) -> _Raw:
    """``179`` / ``179.5`` / ``179.99``: a JSON number with at most two decimals and no binary noise."""
    value = amount.quantize(Decimal("0.01"))
    if value == value.to_integral_value():
        return _Raw(str(int(value)))
    return _Raw(format(value.normalize(), "f"))


def _signature_header(req: WebhookRequest) -> str | None:
    for name in ("Signature", "Authorization"):
        value = req.header(name)
        if value and value.strip():
            value = value.strip()
            if value[:7].lower() == "bearer ":
                value = value[7:].strip()
            return value.lower() or None
    return None


def _detail(data: Any) -> str:
    if isinstance(data, Mapping):
        for key in ("error", "message"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return ": " + value.strip()[:120]
            if isinstance(value, Mapping):
                return ": " + json.dumps(value, ensure_ascii=False)[:120]
    return ""


class Lava(PaymentProvider):
    """See module docstring."""

    manifest = Manifest(
        slug="lava",
        title="Lava Business",
        method_kinds=(MethodKind.CARD, MethodKind.SBP),
        currencies=("RUB",),
        config=LavaConfig,
        docs_url="https://developer.lava.ru/",
        min_minor=100,  # sum: min 1 (schema)
        max_minor=200_000_000,  # sum: max 2 000 000 (schema)
    )
    capabilities = Capabilities(
        webhook_auth=WebhookAuth.SIGNATURE,  # HMAC-SHA256 on the additional key
        replay_window_s=None,  # no signed time; replays are dropped by the core's dedup
        fetch_status=True,
        batch_status=False,  # invoice/status takes one invoice
        refund=False,  # refund methods exist only in the SDK, not in the documentation (spec §6)
        recurring=False,  # recurring needs Lava products mirroring the tariffs — outside v1 (spec §7)
        receipt_54fz=False,
        in_chat_invoice=False,
        redirect=True,
        webhook=True,
    )

    # ------------------------------------------------------------------------------------------- HTTP

    @property
    def _base(self) -> str:
        return str(self.config.base_url or DEFAULT_BASE_URL).rstrip("/") + "/business"

    async def _call(self, path: str, what: str, fields: Mapping[str, Any]) -> tuple[HttpResponse, Any]:
        body = json_body(fields)
        resp = await self.ctx.http.request(
            "POST",
            f"{self._base}{path}",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Signature": request_signature(self.config.secret_key, body),
            },
            data=body,
        )
        if resp.status == 429 or resp.status >= 500:
            raise ProviderError(
                _T["unavailable"].format(status=resp.status), retryable=True, status=resp.status
            )
        try:
            data = json.loads(resp.body.decode("utf-8"), parse_float=Decimal)
        except (UnicodeDecodeError, ValueError):
            data = None
        if resp.status != 404 or data is not None:
            self._check(resp, data, what)
        return resp, data

    def _check(self, resp: HttpResponse, data: Any, what: str) -> None:
        status = resp.status
        if status == 200 and isinstance(data, Mapping) and data.get("status_check") is True:
            return
        if status == 404:
            return  # the caller decides: unknown invoice or unknown project
        if status == 401:
            raise ProviderError(_T["bad_key"], retryable=False, status=status)
        if status == 403:
            raise ProviderError(_T["forbidden"], retryable=False, status=status)
        self.ctx.log.warning("lava: %s answered HTTP %s", what, status)
        if data is None:
            raise ProviderError(_T["bad_answer"], retryable=status == 200, status=status)
        if status == 422:
            raise ProviderError(_T["invalid"].format(detail=_detail(data)), retryable=False, status=status)
        raise ProviderError(
            _T["rejected"].format(status=status, detail=_detail(data)), retryable=False, status=status
        )

    # ----------------------------------------------------------------------------------------- create

    async def create(self, intent: PaymentIntent) -> Checkout:
        if intent.currency.upper() != "RUB":
            raise ProviderError(_T["currency"], retryable=False)
        amount = intent.amount
        if not Decimal(1) <= amount <= Decimal(2_000_000):
            raise ProviderError(_T["amount"], retryable=False)
        expire = int(self.config.expire_minutes or 300)
        services = [s.strip() for s in str(self.config.services or "").split(",") if s.strip()]
        fields: dict[str, Any] = {
            "sum": _sum_token(amount),
            "orderId": intent.payment_id,  # opaque: never a Telegram id or a subscription link
            "shopId": str(self.config.shop_id),
            "hookUrl": self.ctx.webhook_url,
            "successUrl": intent.return_url,
            "failUrl": intent.return_url,
            "expire": expire,
            "comment": (intent.description or "")[:_COMMENT_MAX] or None,
            "includeService": list(dict.fromkeys(services)) or None,
        }
        resp, data = await self._call("/invoice/create", "invoice/create", fields)
        if resp.status == 404:
            raise ProviderError(_T["no_shop"], retryable=False, status=404)
        invoice = data.get("data") if isinstance(data, Mapping) else None
        if not isinstance(invoice, Mapping):
            raise ProviderError(_T["bad_answer"], retryable=False, status=resp.status)
        external = _id(invoice.get("id"))
        pay_url = invoice.get("url")
        if external is None or not isinstance(pay_url, str) or not re.match(r"https?://", pay_url.strip()):
            self.ctx.log.warning("lava: invoice/create answered without id/url")
            raise ProviderError(_T["bad_answer"], retryable=False, status=resp.status)
        return Checkout(
            kind="url",
            external_id=external,
            pay_url=pay_url.strip(),
            expires_at=datetime.now(UTC) + timedelta(minutes=expire),
        )

    # ---------------------------------------------------------------------------------------- webhook

    async def parse_webhook(self, req: WebhookRequest) -> ProviderEvent:
        given = _signature_header(req)
        if not given:
            raise WebhookRejected("missing signature", status=401)
        matched = False
        for candidate in webhook_candidates(str(self.config.additional_key), req.body):
            matched |= constant_time_equal(candidate, given)
        if not matched:
            raise WebhookRejected("bad signature", status=401)
        try:
            data = json.loads(req.body.decode("utf-8"), parse_float=Decimal)
        except (UnicodeDecodeError, ValueError):
            raise WebhookRejected("malformed", status=400) from None
        if not isinstance(data, Mapping):
            raise WebhookRejected("malformed", status=400)
        kind = data.get("type")
        if kind is not None and str(kind).strip() != str(_INVOICE_TYPE):
            self.ctx.log.info("lava: webhook of type %r acknowledged without changes", str(kind)[:10])
            raise WebhookIgnored("not an invoice webhook")
        raw_status = str(data.get("status") or "").strip().lower()
        state = STATUS_MAP.get(raw_status)
        if state is None:
            self.ctx.log.warning("lava: webhook with unknown status %r ignored", raw_status[:40])
            raise WebhookIgnored("unknown status")
        external = _id(data.get("invoice_id"))
        ours = _our_payment_id(data.get("order_id"))
        if external is None and ours is None:
            raise WebhookRejected("no invoice id", status=400)
        amount: Decimal | None = None
        if data.get("amount") not in (None, ""):
            try:
                amount = parse_amount(data.get("amount"))
            except (TypeError, ValueError):
                raise WebhookRejected("bad amount", status=400) from None
        credited = data.get("credited")
        return ProviderEvent(
            state=state,
            external_id=external,
            payment_id=ours,
            amount=amount,
            # The webhook names no currency: a «success» is re-read with invoice/status before crediting.
            currency=None,
            is_test=self.ctx.is_test,
            signed_at=None,
            summary={
                "invoice_id": external,
                "order_id": ours or (str(data.get("order_id"))[:60] if data.get("order_id") else None),
                "status": raw_status,
                "amount": None if amount is None else str(amount),
                "credited": None if credited in (None, "") else str(credited)[:20],
                "pay_service": str(data.get("pay_service") or "")[:20] or None,
                "pay_time": str(data.get("pay_time") or "")[:25] or None,
            },
        )

    def ack(self, event: ProviderEvent | None) -> WebhookResponse:
        return WebhookResponse.ok("OK")

    # ----------------------------------------------------------------------------------------- status

    async def fetch_status(self, ids: Sequence[str]) -> list[ProviderStatus]:
        result: list[ProviderStatus] = []
        for external in dict.fromkeys(ids):
            if not external:
                continue
            fields = {"shopId": str(self.config.shop_id), "invoiceId": external}
            resp, data = await self._call("/invoice/status", "invoice/status", fields)
            if resp.status == 404:
                continue  # unknown to Lava: absent from the result
            invoice = data.get("data") if isinstance(data, Mapping) else None
            if not isinstance(invoice, Mapping):
                raise ProviderError(_T["bad_answer"], retryable=True, status=resp.status)
            reported = _id(invoice.get("id"))
            if reported is not None and reported != external:
                self.ctx.log.warning("lava: status of %s answered for another invoice", external[:40])
                continue
            raw_status = str(invoice.get("status") or "").strip().lower()
            state = STATUS_MAP.get(raw_status)
            if state is None:
                self.ctx.log.warning("lava: unknown status %r of %s", raw_status[:20], external[:40])
                continue
            try:
                amount = parse_amount(invoice.get("amount"))
            except (TypeError, ValueError):
                self.ctx.log.warning("lava: unreadable amount of %s skipped", external[:40])
                continue
            result.append(
                ProviderStatus(
                    state=state,
                    external_id=external,
                    payment_id=_our_payment_id(invoice.get("order_id")),
                    amount=amount,
                    currency="RUB",  # Lava Business invoices are in roubles (no currency field)
                    is_test=self.ctx.is_test,
                )
            )
        return result

    # ------------------------------------------------------------------------------------------ probe

    async def test_credentials(self) -> Probe:
        """No side effects: ``invoice/get-available-tariffs`` of the project — checks the secret key and the
        project id. The additional key can only be checked by a webhook."""
        try:
            resp, data = await self._call(
                "/invoice/get-available-tariffs",
                "get-available-tariffs",
                {"shopId": str(self.config.shop_id)},
            )
        except ProviderError as exc:
            if exc.human == _T["bad_answer"]:
                return Probe(False, _T["probe_not_lava"])
            return Probe(False, exc.human)
        if resp.status == 404:
            return Probe(False, _T["no_shop"] if isinstance(data, Mapping) else _T["probe_not_lava"])
        tariffs = data.get("data") if isinstance(data, Mapping) else None
        if not isinstance(tariffs, list):
            return Probe(False, _T["probe_not_lava"])
        services = sorted(
            {str(t.get("service_id") or t.get("service_name")) for t in tariffs if isinstance(t, Mapping)}
        )
        return Probe(True, _T["probe_ok"].format(n=len(tariffs)), {"services": services})
