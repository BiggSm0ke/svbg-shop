"""AuraPay — SBP and card payment links in roubles (07 §4.2).

Specification: ``docs/providers/aurapay.md`` (official OpenAPI <https://docs.aurapay.tech/openapi.yaml>,
API 1.2.1, checked 2026-10-02). Written from scratch by the specification; imports only :mod:`svbg.sdk` and
the standard library.

* Every request carries ``X-ApiKey`` and ``X-ShopId``; bodies and answers are JSON.
* ``POST /invoice/create``: ``order_id`` carries **only** our opaque payment id, ``custom_fields`` is never
  sent (no user data leaves the bot), ``service`` = ``sbp`` / ``card`` when the user picked a method kind,
  ``callback_url`` = the instance's webhook address. The AuraPay invoice ``id`` is our ``external_id``, the
  buyer goes to ``payment_data.url``. ``400 Order id is not unique`` after a lost answer → the invoice is
  found again with ``POST /invoice/status`` by ``order_id`` (spec §3).
* ``POST /invoice/status`` reads one invoice (``fetch_status``; no batch API; ``404`` → unknown).
* **Webhooks** carry ``X-SIGNATURE = hex(HMAC-SHA256(secret key #2, values of the top-level keys sorted by
  name, concatenated without separators))`` (spec §5.2). The values are glued without key names or
  boundaries and nothing signs the time, so a valid signature does not pin the fields: the scheme is declared
  **weak** and every webhook only makes the core re-read the invoice with ``fetch_status`` before anything
  changes. Compared in constant time with the lower-cased header.
* Amounts are read from JSON as :class:`~decimal.Decimal` (``parse_float=Decimal``), never through ``float``.
* No refund API (``REFUNDED`` is only visible by polling), no test mode, no published webhook IPs.
"""

from __future__ import annotations

import hashlib
import hmac
import json
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
    constant_time_equal,
    integer,
    parse_amount,
    secret,
    text,
    url,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_LIFETIME_MIN",
    "MAX_LIFETIME_MIN",
    "SERVICES",
    "STATUS_MAP",
    "AuraPay",
    "AuraPayConfig",
    "signature_candidates",
    "signing_strings",
]

DEFAULT_BASE_URL: Final = "https://app.aurapay.tech"
#: SDK method kind → AuraPay ``service``.
SERVICES: Final[Mapping[MethodKind, str]] = {MethodKind.SBP: "sbp", MethodKind.CARD: "card"}
#: AuraPay invoice status → SDK state (spec §6).
STATUS_MAP: Final[Mapping[str, PaymentState]] = {
    "PENDING": PaymentState.CREATED,
    "PAID": PaymentState.PAID,
    "EXPIRED": PaymentState.EXPIRED,
    "REFUNDED": PaymentState.REFUNDED,
}
DEFAULT_LIFETIME_MIN: Final = 60
MAX_LIFETIME_MIN: Final = 43_200  # 30 days (spec §3)
_COMMENT_MAX: Final = 255
_ID_MAX: Final = 200
_UUID_RE: Final = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_HEX64_RE: Final = re.compile(r"[0-9a-f]{64}")
_NOT_UNIQUE: Final = "not unique"

_T: Final = {
    "bad_key": "AuraPay отклонила API-ключ или ID кассы — проверьте их в кабинете AuraPay",
    "rejected": "AuraPay отклонила запрос (HTTP {status}){detail}",
    "unavailable": "AuraPay временно недоступна (HTTP {status})",
    "bad_answer": "AuraPay вернула непонятный ответ",
    "currency": "AuraPay принимает только рубли",
    "duplicate": "счёт с этим номером уже есть в AuraPay, но с другой суммой",
    "probe_ok": "API-ключ и ID кассы приняты, AuraPay доступна (баланс прочитан)",
    "probe_not_aurapay": "по адресу API отвечает не AuraPay — проверьте «Адрес API»",
}


class AuraPayConfig(ConfigModel):
    api_key = secret(
        "API-ключ (X-ApiKey)",
        "Ключ для запросов к API AuraPay — заголовок X-ApiKey.",
        where="кабинет AuraPay (cabinet.aurapay.tech) → касса → настройки → API-ключ",
    )
    shop_id = text(
        "ID кассы (X-ShopId)",
        "Идентификатор кассы (UUID) — заголовок X-ShopId.",
        where="кабинет AuraPay (cabinet.aurapay.tech) → касса → настройки → ID кассы",
        pattern=r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}",
    )
    webhook_secret = secret(
        "Секретный ключ #2",
        "Ключ, которым AuraPay подписывает уведомления (заголовок X-SIGNATURE). Это не API-ключ.",
        where="кабинет AuraPay (cabinet.aurapay.tech) → касса → настройки → «Секретный ключ #2»",
    )
    base_url = url(
        "Адрес API",
        "Базовый адрес API AuraPay. Меняйте, только если AuraPay сообщила другой.",
        default=DEFAULT_BASE_URL,
        required=False,
        advanced=True,
    )
    lifetime_min = integer(
        "Время жизни счёта, минут",
        "Сколько минут счёт ждёт оплаты (1…43200, по умолчанию 60).",
        default=DEFAULT_LIFETIME_MIN,
        required=False,
        min=1,
        max=MAX_LIFETIME_MIN,
        advanced=True,
    )
    return_url = url(
        "Куда вернуть покупателя",
        "Страница после оплаты (success_url / fail_url). Пусто — ссылка, которую передаёт ядро.",
        where="обычно ссылка на вашего бота: https://t.me/<имя_бота>",
        required=False,
        advanced=True,
    )


# ------------------------------------------------------------------------------------------- signature


class _Num(str):
    """A JSON number kept exactly as written in the body (``1250.00`` stays ``"1250.00"``)."""

    __slots__ = ()


def _raw_json(body: bytes) -> Any:
    """JSON with every number kept as its source text (:class:`_Num`); ``ValueError`` when not JSON."""
    return json.loads(body.decode("utf-8"), parse_float=_Num, parse_int=_Num)


def _php_number(raw: str) -> str:
    """PHP's string form of a float (``299.0`` → ``299``, ``1.50`` → ``1.5``)."""
    try:
        dec = Decimal(raw)
    except ArithmeticError:
        return raw
    if not dec.is_finite():
        return raw
    if dec == dec.to_integral_value():
        return str(int(dec))
    return format(dec.normalize(), "f")


def _value_forms(value: Any) -> tuple[str, ...] | None:
    """Textual forms of one top-level value (spec §5.2): ``None`` for nested values (not signable)."""
    if value is None:
        return ("",)
    if isinstance(value, bool):
        return ("1" if value else "", "True" if value else "False")  # PHP implode / Python str()
    if isinstance(value, _Num):
        raw = str(value)
        if re.fullmatch(r"-?\d+", raw):
            return (raw,)
        return tuple(dict.fromkeys((raw, _php_number(raw), repr(float(raw)))))
    if isinstance(value, str):
        return (value,)
    return None


def signing_strings(data: Mapping[str, Any]) -> list[str]:
    """Candidate strings of a webhook body (parsed with numbers as text): values of the top-level keys in
    byte order of the keys, glued without separators. Several candidates only when a float or a boolean
    makes the documented algorithm ambiguous (PHP vs Python); ``[]`` when a value is nested."""
    variants: list[str] = [""]
    for key in sorted(data, key=lambda k: k.encode("utf-8")):
        forms = _value_forms(data[key])
        if forms is None:
            return []
        variants = [v + f for v in variants for f in forms]
        if len(variants) > 16:
            variants = variants[:16]
    return list(dict.fromkeys(variants))


def signature_candidates(data: Mapping[str, Any], key: str) -> list[str]:
    """``hex(HMAC-SHA256(key, string))`` of every candidate string (lower-case hex)."""
    secret_key = key.encode("utf-8")
    return [
        hmac.new(secret_key, s.encode("utf-8"), hashlib.sha256).hexdigest() for s in signing_strings(data)
    ]


# --------------------------------------------------------------------------------------------- helpers


def _decimal_json(body: bytes) -> Any:
    """JSON with every non-integer number as :class:`Decimal` (``ValueError`` when not JSON)."""
    return json.loads(body.decode("utf-8"), parse_float=Decimal)


def _our_payment_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower()
    return candidate if _UUID_RE.fullmatch(candidate) else None


def _external_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if 0 < len(value) <= _ID_MAX else None


def _json_amount(amount: Decimal) -> int | float:
    """A JSON number for AuraPay (roubles with kopecks): integral amounts as integers."""
    return int(amount) if amount == amount.to_integral_value() else float(amount)


def _text(value: Any) -> str:
    return str(value) if value is not None else ""


class AuraPay(PaymentProvider):
    """See module docstring."""

    manifest = Manifest(
        slug="aurapay",
        title="AuraPay",
        method_kinds=(MethodKind.SBP, MethodKind.CARD),
        currencies=("RUB",),
        config=AuraPayConfig,
        docs_url="https://docs.aurapay.tech/",
        min_minor=1,  # schema: amount minimum 0.01
    )
    capabilities = Capabilities(
        # HMAC over values glued without names or boundaries and without time (spec §5.2): characters can be
        # moved between neighbouring values keeping the signature — declared weak, every webhook is re-read
        # with POST /invoice/status before anything changes.
        webhook_auth=WebhookAuth.SECRET_HEADER,
        replay_window_s=None,
        fetch_status=True,
        batch_status=False,  # one invoice per request
        refund=False,  # no refund API (spec §6)
        recurring=False,  # subscriptions exist for cards, the core does not use them yet (spec §8)
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
        return {
            "X-ApiKey": self.config.api_key,
            "X-ShopId": self.config.shop_id,
            "Accept": "application/json",
        }

    async def _post(self, path: str, body: Mapping[str, Any]) -> HttpResponse:
        return await self.ctx.http.request(
            "POST", f"{self._base}{path}", headers=self._headers(), json=dict(body)
        )

    def _raise_for(self, resp: HttpResponse, what: str) -> None:
        if resp.status in (401, 403):
            raise ProviderError(_T["bad_key"], retryable=False, status=resp.status)
        if resp.status in (408, 429) or resp.status >= 500:
            raise ProviderError(
                _T["unavailable"].format(status=resp.status), retryable=True, status=resp.status
            )
        self.ctx.log.warning("aurapay: %s answered HTTP %s", what, resp.status)
        raise ProviderError(
            _T["rejected"].format(status=resp.status, detail=_detail(resp)),
            retryable=False,
            status=resp.status,
        )

    @staticmethod
    def _json(resp: HttpResponse) -> dict[str, Any]:
        try:
            data = _decimal_json(resp.body)
        except (UnicodeDecodeError, ValueError):
            raise ProviderError(_T["bad_answer"], retryable=True, status=resp.status) from None
        if not isinstance(data, dict):
            raise ProviderError(_T["bad_answer"], retryable=True, status=resp.status)
        return data

    # ----------------------------------------------------------------------------------------- create

    async def create(self, intent: PaymentIntent) -> Checkout:
        if intent.currency != "RUB":
            raise ProviderError(_T["currency"], retryable=False)
        pid = intent.payment_id
        lifetime = int(self.config.lifetime_min or DEFAULT_LIFETIME_MIN)
        body: dict[str, Any] = {
            "amount": _json_amount(intent.amount),
            "order_id": pid,  # opaque: never a Telegram id or a subscription link
            "comment": (intent.description or "Оплата")[:_COMMENT_MAX],
            "lifetime": lifetime,
        }
        back = self.config.return_url or intent.return_url
        if back:
            body["success_url"] = back
            body["fail_url"] = back
        if self.ctx.webhook_url:
            body["callback_url"] = self.ctx.webhook_url
        service = SERVICES.get(intent.method_hint) if intent.method_hint else None
        if service is not None:
            body["service"] = service
        expires_at = datetime.now(UTC) + timedelta(minutes=lifetime)  # the zone of expires_at is unknown
        resp = await self._post("/invoice/create", body)
        if resp.status == 400 and _NOT_UNIQUE in _detail(resp).lower():
            return await self._recover(intent, expires_at)
        if resp.status not in (200, 201):
            self._raise_for(resp, "create")
        return self._checkout(self._json(resp), resp.status, expires_at)

    async def _recover(self, intent: PaymentIntent, expires_at: datetime) -> Checkout:
        """The invoice exists already (a retry after a lost answer): read it by ``order_id``."""
        resp = await self._post("/invoice/status", {"order_id": intent.payment_id})
        if resp.status != 200:
            self._raise_for(resp, "status by order_id")
        data = self._json(resp)
        try:
            amount = parse_amount(data.get("amount"))
        except (TypeError, ValueError):
            raise ProviderError(_T["bad_answer"], retryable=False, status=resp.status) from None
        if amount != intent.amount or _our_payment_id(data.get("order_id")) != intent.payment_id.lower():
            raise ProviderError(_T["duplicate"], retryable=False, status=resp.status)
        return self._checkout(data, resp.status, expires_at)

    @staticmethod
    def _checkout(data: Mapping[str, Any], status: int, expires_at: datetime) -> Checkout:
        external = _external_id(data.get("id"))
        payment_data = data.get("payment_data")
        pay_url = payment_data.get("url") if isinstance(payment_data, Mapping) else None
        if external is None or not isinstance(pay_url, str) or not re.match(r"https?://", pay_url):
            raise ProviderError(_T["bad_answer"], retryable=False, status=status)
        return Checkout(kind="url", external_id=external, pay_url=pay_url, expires_at=expires_at)

    # ---------------------------------------------------------------------------------------- webhook

    async def parse_webhook(self, req: WebhookRequest) -> ProviderEvent:
        given = (req.header("X-SIGNATURE") or "").strip().lower()
        if not given:
            raise WebhookRejected("missing signature", status=401)
        try:
            data = _raw_json(req.body)
        except (UnicodeDecodeError, ValueError):
            raise WebhookRejected("malformed", status=400) from None
        if not isinstance(data, dict):
            raise WebhookRejected("malformed", status=400)
        if not _HEX64_RE.fullmatch(given):
            raise WebhookRejected("bad signature", status=401)
        matched = False
        for candidate in signature_candidates(data, self.config.webhook_secret):
            matched = constant_time_equal(candidate, given) or matched  # check every candidate
        if not matched:
            raise WebhookRejected("bad signature", status=401)
        shop = data.get("shop_id")
        if shop is not None and not constant_time_equal(
            _text(shop).strip().lower(), str(self.config.shop_id).lower()
        ):
            raise WebhookRejected("foreign shop", status=401)
        if "event" in data:  # a subscription webhook: subscriptions are not created by this plugin
            raise WebhookIgnored("subscription event")
        raw_status = _text(data.get("status")).strip().upper()
        state = STATUS_MAP.get(raw_status)
        if state is None:
            self.ctx.log.warning("aurapay: webhook with unknown status %r ignored", raw_status[:40])
            raise WebhookIgnored("unknown status")
        status = self._status(_plain(data), state, malformed=WebhookRejected)
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
                "order_id": status.payment_id,
                "status": raw_status,
                "amount": None if status.amount is None else str(status.amount),
                "service": _text(data.get("service"))[:10] or None,
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
            raise _malformed(malformed, "no invoice id")
        amount: Decimal | None = None
        if data.get("amount") not in (None, ""):
            try:
                amount = parse_amount(data["amount"])
            except (TypeError, ValueError):
                raise _malformed(malformed, "bad amount") from None
        return ProviderStatus(
            state=state,
            external_id=external,
            payment_id=ours,
            amount=amount,
            currency="RUB",  # AuraPay works only in roubles (spec §1)
            is_test=self.ctx.is_test,
        )

    # ----------------------------------------------------------------------------------------- status

    async def fetch_status(self, ids: Sequence[str]) -> list[ProviderStatus]:
        result: list[ProviderStatus] = []
        for external in dict.fromkeys(ids):
            if not external:
                continue
            resp = await self._post("/invoice/status", {"id": external})
            if resp.status == 404:
                continue  # unknown to AuraPay: absent from the result
            if resp.status != 200:
                self._raise_for(resp, "status")
            data = self._json(resp)
            state = STATUS_MAP.get(_text(data.get("status")).strip().upper())
            if state is None:
                continue
            try:
                status = self._status(data, state, external_id=external, malformed=ProviderError)
            except ProviderError:
                self.ctx.log.warning("aurapay: unreadable status of %s skipped", external[:40])
                continue
            if status.external_id != external:
                self.ctx.log.warning("aurapay: status of %s answered for another invoice", external[:40])
                continue
            result.append(status)
        return result

    # ------------------------------------------------------------------------------------------ probe

    async def test_credentials(self) -> Probe:
        """No side effects: ``GET /shop/balance`` (spec §2)."""
        resp = await self.ctx.http.request("GET", f"{self._base}/shop/balance", headers=self._headers())
        if resp.status in (401, 403):
            return Probe(False, _T["bad_key"])
        if resp.status in (408, 429) or resp.status >= 500:
            return Probe(False, _T["unavailable"].format(status=resp.status))
        ctype = str(resp.headers.get("Content-Type") or resp.headers.get("content-type") or "")
        if "html" in ctype.lower() or resp.body.lstrip()[:1] == b"<":
            return Probe(False, _T["probe_not_aurapay"])
        if resp.status == 400:  # «Error get balance»: the documented failure of this call
            return Probe(False, _T["bad_key"])
        if resp.status == 200:
            try:
                data = _decimal_json(resp.body)
            except (UnicodeDecodeError, ValueError):
                return Probe(False, _T["bad_answer"])
            if isinstance(data, dict) and "balance" in data:
                return Probe(True, _T["probe_ok"], {"status": resp.status})
            return Probe(False, _T["bad_answer"])
        return Probe(False, _T["rejected"].format(status=resp.status, detail=""))


def _plain(data: Mapping[str, Any]) -> dict[str, Any]:
    """Numbers kept as text (:class:`_Num`) → plain ``str`` (amounts go through ``parse_amount``)."""
    return {k: (str(v) if isinstance(v, _Num) else v) for k, v in data.items()}


def _detail(resp: HttpResponse) -> str:
    """A short provider message for the owner (``: …``), never the request itself."""
    try:
        data = json.loads(resp.body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return ""
    if isinstance(data, dict):
        for key in ("error", "message"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return ": " + value.strip()[:120]
    return ""


def _malformed(kind: type[Exception], reason: str) -> Exception:
    if kind is WebhookRejected:
        return WebhookRejected(reason, status=400)
    return ProviderError(_T["bad_answer"], retryable=False)
