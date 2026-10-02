"""SeverPay — payment link cash desk (SBP, NSPK E-com, T-Pay, SberPay, MirPay…), pay-in only (07 §4.2, §4.4).

Specification: ``docs/providers/severpay.md`` (official documentation <https://docs.severpay.io/ru/payin/> and
<https://docs.severpay.io/ru/webhook/>, checked on 2026-10-02). Written from scratch from that specification.
Imports only :mod:`svbg.sdk`.

* Every request is ``POST`` with ``mid``, a random ``salt`` and ``sign`` **in the body**:
  ``sign = hex(HMAC_SHA256(token, JSON))`` where JSON is the body with top-level keys sorted, no spaces, ``/``
  and non-ASCII **not** escaped (PHP ``JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES``). The plugin sends
  exactly the signed bytes with ``"sign"`` appended as the last member.
* ``POST /payin/create`` — ``order_id`` is the opaque payment id; ``client_id`` and the local part of
  ``client_email`` (``…@telegram.org``) are the opaque per-user ``customer_ref`` — never a Telegram id.
* ``POST /payin/get`` reads one status by ``id`` (``fetch_status``; no batch API).
* Webhooks are signed **not over the raw bytes**: SeverPay's handler re-encodes the decoded body without
  ``sign`` as PHP ``json_encode`` without flags (key order of the body, ``/`` → ``\\/``, non-ASCII →
  ``\\uXXXX``). That canonicalization is fragile, so a ``success`` webhook never credits by itself: the event
  carries no currency and the core re-reads the payment with ``POST /payin/get`` (``payments.verify``) before
  crediting. Other states are applied from the webhook. The flagged form (as for requests) is accepted too.
* The answer SeverPay expects is ``200`` + ``Content-Type: application/json`` + ``{"status": true}``.
* SeverPay publishes the webhook source addresses; checking them is optional (behind a reverse proxy the
  source address may be the proxy's), otherwise a mismatch is only logged.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
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
    flag,
    hmac_sha256_hex,
    integer,
    parse_amount,
    parse_timestamp,
    secret,
    url,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "STATUS_MAP",
    "WEBHOOK_IPS",
    "SeverPay",
    "SeverPayConfig",
    "php_json",
    "request_body",
    "request_json",
    "webhook_candidates",
]

DEFAULT_BASE_URL: Final = "https://severpay.io/api/merchant"
#: Official webhook source addresses (docs «Вебхуки: базовая информация»).
WEBHOOK_IPS: Final = (
    "45.76.81.14",
    "2001:19f0:6c01:878:5400:5ff:fe38:50d1",
    "207.148.69.64",
    "2401:c080:1400:109b:5400:5ff:fe95:20d3",
)
#: SeverPay payment status → SDK state (spec §3.5). ``fail`` is both «expired» and «declined by the bank».
STATUS_MAP: Final[Mapping[str, PaymentState]] = {
    "new": PaymentState.CREATED,
    "process": PaymentState.PROCESSING,
    "success": PaymentState.PAID,
    "decline": PaymentState.FAILED,
    "fail": PaymentState.FAILED,
}
LIFETIME_MIN: Final = 30
LIFETIME_MAX: Final = 4320
_ID_MAX: Final = 200
_UUID_RE: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_SIGN_RE: Final = re.compile(r"[0-9a-fA-F]{64}")
_SAFE_ID_RE: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")
_ACK: Final = {"status": True}

_T: Final = {
    "bad_key": "SeverPay отклонил MID или ключ{detail} — проверьте их в кабинете severpay.io/panel",
    "rejected": "SeverPay отклонил запрос{detail}",
    "unavailable": "SeverPay временно недоступен (HTTP {status})",
    "bad_answer": "SeverPay вернул непонятный ответ",
    "currency": "SeverPay в боте принимает только рубли",
    "probe_ok": "MID и ключ приняты: магазин «{name}», валюта {currency}, сумма {min}–{max}",
    "probe_mid": "SeverPay вернул настройки другого магазина (mid {got}) — проверьте MID",
    "probe_currency": "валюта аккаунта SeverPay — {currency}, а бот принимает оплату в рублях",
}


class SeverPayConfig(ConfigModel):
    mid = integer(
        "ID магазина (MID)",
        "Числовой идентификатор магазина в SeverPay (поле mid в каждом запросе).",
        where="кабинет SeverPay (severpay.io/panel) → магазин → ID магазина (mid)",
        min=1,
    )
    token = secret(
        "Секретный ключ (token)",
        "Им подписываются запросы к SeverPay и проверяются уведомления (HMAC-SHA256).",
        where="кабинет SeverPay (severpay.io/panel) → магазин → секретный ключ (token)",
    )
    base_url = url(
        "Адрес API",
        "Базовый адрес API SeverPay. Меняйте, только если SeverPay сообщил другой.",
        default=DEFAULT_BASE_URL,
        required=False,
        advanced=True,
    )
    lifetime_min = integer(
        "Срок жизни ссылки, минут",
        f"От {LIFETIME_MIN} до {LIFETIME_MAX}. Пусто — по умолчанию SeverPay (1440 минут = сутки).",
        where="выберите сами: сколько ссылка на оплату остаётся действительной",
        required=False,
        min=LIFETIME_MIN,
        max=LIFETIME_MAX,
        advanced=True,
    )
    return_url = url(
        "Куда вернуть покупателя",
        "Страница после оплаты (url_return). Пусто — ссылка на бота, которую передаёт ядро.",
        where="обычно ссылка на вашего бота: https://t.me/<имя_бота>",
        required=False,
        advanced=True,
    )
    check_ip = flag(
        "Проверять IP-адрес уведомлений",
        "Принимать уведомления только с официальных адресов SeverPay. Включайте, только если бот видит "
        "настоящий адрес отправителя (без обратного прокси); подпись проверяется всегда.",
        where="адреса опубликованы в документации SeverPay: " + ", ".join(WEBHOOK_IPS),
        advanced=True,
    )


# --------------------------------------------------------------------------------------------- signing


def request_json(params: Mapping[str, Any]) -> str:
    """The signed form of a request body: top-level keys sorted, compact, ``/`` and Unicode unescaped."""
    return json.dumps(dict(sorted(params.items())), ensure_ascii=False, separators=(",", ":"))


def request_body(token: str, params: Mapping[str, Any]) -> tuple[bytes, str]:
    """``(bytes to send, sign)``: exactly the signed JSON with ``"sign"`` appended as the last member."""
    canonical = request_json(params)
    signature = hmac_sha256_hex(token, canonical.encode("utf-8"))
    return (canonical[:-1] + f',"sign":"{signature}"}}').encode("utf-8"), signature


def _php_float(value: float) -> str:
    """PHP ``json_encode`` of a float (``serialize_precision = -1``): shortest round-trip, ``179.0`` keeps its
    ``.0``, exponents as ``1.0e+25``."""
    text_value = repr(value)
    if "e" in text_value:
        mantissa, exp = text_value.split("e")
        if "." not in mantissa:
            mantissa += ".0"
        return f"{mantissa}e{int(exp):+d}"
    return text_value


def php_json(value: Any, *, escape: bool = True) -> str:
    """PHP ``json_encode`` of a decoded JSON value: ``escape=True`` — without flags (``/`` → ``\\/``,
    non-ASCII → ``\\uXXXX``); ``escape=False`` — ``JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES``. Key
    order is kept; an empty or ``0..n-1``-keyed object is a PHP list (``[]``)."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _php_float(value)
    if isinstance(value, str):
        if escape:
            return json.dumps(value, ensure_ascii=True).replace("/", "\\/")
        return json.dumps(value, ensure_ascii=False).replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    if isinstance(value, Mapping):
        if list(value) == [str(i) for i in range(len(value))]:
            return "[" + ",".join(php_json(v, escape=escape) for v in value.values()) + "]"
        members = (
            f"{php_json(str(k), escape=escape)}:{php_json(v, escape=escape)}" for k, v in value.items()
        )
        return "{" + ",".join(members) + "}"
    if isinstance(value, list | tuple):
        return "[" + ",".join(php_json(v, escape=escape) for v in value) + "]"
    raise TypeError(f"cannot encode {type(value).__name__}")


def webhook_candidates(data: Mapping[str, Any]) -> list[bytes]:
    """Byte strings a genuine webhook (``data`` = the decoded body without ``sign``) may be signed over: the
    documented PHP ``json_encode`` without flags first, the flagged form used for requests second."""
    first = php_json(data).encode("utf-8")
    second = php_json(data, escape=False).encode("utf-8")
    return [first] if first == second else [first, second]


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _constant(name: str) -> Any:
    raise ValueError(f"non-standard JSON constant {name}")


def _load(body: bytes, *, decimal: bool = False) -> Any:
    return json.loads(
        body.decode("utf-8"),
        object_pairs_hook=_pairs,
        parse_constant=_constant,
        parse_float=Decimal if decimal else float,
    )


# --------------------------------------------------------------------------------------------- helpers


def _json_amount(amount: Decimal) -> int | float:
    """A JSON number PHP re-encodes identically: integers without a point, fractions in shortest form."""
    return int(amount) if amount == amount.to_integral_value() else float(amount)


def _client_id(customer_ref: str) -> str:
    """The opaque per-user reference, restricted to ``[A-Za-z0-9_-]`` (no escaping questions)."""
    if _SAFE_ID_RE.fullmatch(customer_ref):
        return customer_ref
    return hashlib.sha256(customer_ref.encode("utf-8")).hexdigest()[:24]


def _our_payment_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower()
    return candidate if _UUID_RE.fullmatch(candidate) else None


def _external_id(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and 0 < len(value.strip()) <= _ID_MAX:
        return value.strip()
    return None


def _currency(value: Any) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z]{3,8}", value.strip()):
        return value.strip().upper()
    return None


def _msg(data: Any) -> str:
    if isinstance(data, Mapping):
        message = data.get("msg")
        if isinstance(message, str) and message.strip():
            return ": " + message.strip()[:160]
    return ""


def _ip_in(remote: str | None, allowed: Sequence[str]) -> bool:
    if not remote:
        return False
    try:
        ip = ipaddress.ip_address(remote.strip().strip("[]"))
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return any(ip == ipaddress.ip_address(a) for a in allowed)


class SeverPay(PaymentProvider):
    """See module docstring."""

    manifest = Manifest(
        slug="severpay",
        title="SeverPay",
        method_kinds=(MethodKind.SBP, MethodKind.CARD),
        currencies=("RUB",),
        config=SeverPayConfig,
        docs_url="https://docs.severpay.io/ru/payin/",
    )
    capabilities = Capabilities(
        webhook_auth=WebhookAuth.SIGNATURE,
        replay_window_s=None,  # only a random salt is signed, no time
        fetch_status=True,
        batch_status=False,
        refund=False,
        recurring=False,
        in_chat_invoice=False,
        redirect=True,
        webhook=True,
    )

    # ------------------------------------------------------------------------------------------- HTTP

    @property
    def _base(self) -> str:
        return str(self.config.base_url or DEFAULT_BASE_URL).rstrip("/")

    async def _call(self, path: str, params: Mapping[str, Any]) -> dict[str, Any]:
        """Signed ``POST``; returns ``data`` of ``{"status": true, …}``. ``ProviderError`` otherwise."""
        body, _sign = request_body(
            self.config.token, {**params, "mid": int(self.config.mid), "salt": secrets.token_hex(8)}
        )
        resp = await self.ctx.http.request(
            "POST",
            f"{self._base}{path}",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            data=body,
        )
        return self._result(resp, path)

    def _result(self, resp: HttpResponse, path: str) -> dict[str, Any]:
        if resp.status in (408, 429) or resp.status >= 500:
            raise ProviderError(
                _T["unavailable"].format(status=resp.status), retryable=True, status=resp.status
            )
        try:
            answer = _load(resp.body, decimal=True)
        except (UnicodeDecodeError, ValueError):
            if resp.status in (401, 403):
                raise ProviderError(
                    _T["bad_key"].format(detail=""), retryable=False, status=resp.status
                ) from None
            raise ProviderError(_T["bad_answer"], retryable=True, status=resp.status) from None
        if not isinstance(answer, dict) or not isinstance(answer.get("status"), bool):
            raise ProviderError(_T["bad_answer"], retryable=False, status=resp.status)
        if answer["status"] is not True:
            self.ctx.log.warning("severpay: %s answered status=false (HTTP %s)", path, resp.status)
            template = "bad_key" if resp.status in (401, 403) else "rejected"
            raise ProviderError(_T[template].format(detail=_msg(answer)), retryable=False, status=resp.status)
        data = answer.get("data")
        if not isinstance(data, dict):
            raise ProviderError(_T["bad_answer"], retryable=False, status=resp.status)
        return data

    # ----------------------------------------------------------------------------------------- create

    async def create(self, intent: PaymentIntent) -> Checkout:
        if intent.currency not in self.manifest.currencies:
            raise ProviderError(_T["currency"], retryable=False)
        client = _client_id(intent.customer_ref)
        params: dict[str, Any] = {
            "order_id": intent.payment_id,  # opaque: never a Telegram id or a subscription link
            "amount": _json_amount(intent.amount),
            "currency": intent.currency,
            "client_id": client,
            "client_email": f"{client}@telegram.org",
        }
        back = self.config.return_url or intent.return_url
        if back and re.match(r"https?://", back):
            params["url_return"] = back
        if self.config.lifetime_min is not None:
            params["lifetime"] = int(self.config.lifetime_min)
        data = await self._call("/payin/create", params)
        external = _external_id(data.get("id"))
        pay_url = data.get("url")
        if external is None or not isinstance(pay_url, str) or not re.match(r"https?://", pay_url):
            raise ProviderError(_T["bad_answer"], retryable=False)
        expires_at = None
        if data.get("expire_at") not in (None, ""):
            try:
                expires_at = parse_timestamp(data["expire_at"])
            except ValueError:
                expires_at = None
        return Checkout(kind="url", external_id=external, pay_url=pay_url, expires_at=expires_at)

    # ---------------------------------------------------------------------------------------- webhook

    def _check_ip(self, req: WebhookRequest) -> None:
        if _ip_in(req.remote, WEBHOOK_IPS):
            return
        if self.config.check_ip:
            raise WebhookRejected("source address not allowed", status=403)
        self.ctx.log.info("severpay: webhook from an unlisted address (signature is checked)")

    async def parse_webhook(self, req: WebhookRequest) -> ProviderEvent:
        self._check_ip(req)
        try:
            data = _load(req.body)
        except (UnicodeDecodeError, ValueError):
            raise WebhookRejected("malformed", status=400) from None
        if not isinstance(data, dict):
            raise WebhookRejected("malformed", status=400)
        signature = data.pop("sign", None)
        if not isinstance(signature, str) or not _SIGN_RE.fullmatch(signature.strip()):
            raise WebhookRejected("missing signature", status=401)
        signature = signature.strip().lower()
        ok = False
        for candidate in webhook_candidates(data):
            ok |= constant_time_equal(hmac_sha256_hex(self.config.token, candidate), signature)
        if not ok:
            raise WebhookRejected("bad signature", status=401)
        kind = data.get("type")
        if kind != "payin":
            if kind != "test":
                self.ctx.log.warning("severpay: webhook of unknown type %r acknowledged", str(kind)[:40])
            raise WebhookIgnored(f"type {str(kind)[:20]}", WebhookResponse.json(_ACK))
        # amounts exactly as written (Decimal), after authentication
        payload = _load(req.body, decimal=True).get("data")
        if not isinstance(payload, dict):
            raise WebhookRejected("no data", status=400)
        raw_status = str(payload.get("status") or "").strip().lower()
        state = STATUS_MAP.get(raw_status)
        if state is None:
            self.ctx.log.warning("severpay: webhook with unknown status %r ignored", raw_status[:40])
            raise WebhookIgnored("unknown status", WebhookResponse.json(_ACK))
        if payload.get("order_id") in (None, ""):
            raise WebhookRejected("no order_id", status=400)
        status = self._status(payload, state, malformed=WebhookRejected)
        # A «success» is never credited from the webhook alone: without a currency the core re-reads the
        # payment with POST /payin/get (payments.verify) and credits by that answer (spec §3.4).
        currency = None if state is PaymentState.PAID else status.currency
        return ProviderEvent(
            state=status.state,
            external_id=status.external_id,
            payment_id=status.payment_id,
            amount=status.amount,
            currency=currency,
            signed_at=None,
            summary={
                "id": status.external_id,
                "order_id": status.payment_id,
                "status": raw_status,
                "amount": None if status.amount is None else str(status.amount),
                "currency": status.currency,
            },
        )

    def _status(
        self,
        data: Mapping[str, Any],
        state: PaymentState,
        *,
        external_id: str | None = None,
        malformed: type[Exception],
    ) -> ProviderStatus:
        external = _external_id(data.get("id")) or external_id
        ours = _our_payment_id(data.get("order_id"))
        if external is None and ours is None:
            raise _malformed(malformed, "no payment id")
        amount: Decimal | None = None
        if data.get("amount") not in (None, ""):
            try:
                amount = parse_amount(data["amount"])
            except (TypeError, ValueError):
                raise _malformed(malformed, "bad amount") from None
        currency: str | None = None
        if data.get("currency") not in (None, ""):
            currency = _currency(data.get("currency"))
            if currency is None:
                raise _malformed(malformed, "bad currency")
        return ProviderStatus(
            state=state, external_id=external, payment_id=ours, amount=amount, currency=currency
        )

    def ack(self, event: ProviderEvent | None) -> WebhookResponse:
        return WebhookResponse.json(_ACK)

    # ----------------------------------------------------------------------------------------- status

    async def fetch_status(self, ids: Sequence[str]) -> list[ProviderStatus]:
        result: list[ProviderStatus] = []
        for external in dict.fromkeys(ids):
            if not external:
                continue
            ref: dict[str, Any] = {"id": int(external)} if external.isdigit() else {"uid": external}
            try:
                data = await self._call("/payin/get", ref)
            except ProviderError as exc:
                if exc.retryable or exc.status in (401, 403):
                    raise
                # status=false: most likely an unknown payment — absent from the result
                self.ctx.log.warning("severpay: status of %s not available", external[:40])
                continue
            state = STATUS_MAP.get(str(data.get("status") or "").strip().lower())
            if state is None:
                continue
            try:
                result.append(self._status(data, state, external_id=external, malformed=ProviderError))
            except ProviderError:
                self.ctx.log.warning("severpay: unreadable status of %s skipped", external[:40])
        return result

    # ------------------------------------------------------------------------------------------ probe

    async def test_credentials(self) -> Probe:
        """No side effects: ``POST /payin/settings`` — shop name, account currency and amount limits."""
        try:
            data = await self._call("/payin/settings", {})
        except ProviderError as exc:
            return Probe(False, exc.human)
        mid = data.get("mid")
        if mid is not None and str(mid) != str(self.config.mid):
            return Probe(False, _T["probe_mid"].format(got=str(mid)[:20]))
        currency = _currency(data.get("currency")) or "?"
        limits = data.get("amount") if isinstance(data.get("amount"), Mapping) else {}
        low = limits.get("min", data.get("min_amount"))
        high = limits.get("max", data.get("max_amount"))
        details = {
            "name": str(data.get("name") or "")[:80],
            "currency": currency,
            "min": None if low is None else str(low),
            "max": None if high is None else str(high),
        }
        if currency not in self.manifest.currencies:
            return Probe(False, _T["probe_currency"].format(currency=currency), details)
        message = _T["probe_ok"].format(
            name=details["name"] or "—",
            currency=currency,
            min=details["min"] or "?",
            max=details["max"] or "?",
        )
        return Probe(True, message, details)


def _malformed(kind: type[Exception], reason: str) -> Exception:
    if kind is WebhookRejected:
        return WebhookRejected(reason, status=400)
    return ProviderError(_T["bad_answer"], retryable=False)
