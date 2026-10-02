"""TabPay — SBP and Russian cards through the TabPay payment page, wave C (07 §4.2).

Written from scratch by the specification ``docs/providers/tabpay.md`` (official TabPay documentation
<https://tabpay.org/docs>, OpenAPI <https://tabpay.org/openapi.json>, checked on 2026-10-02). No third-party
code was used. Imports only :mod:`svbg.sdk`.

* Every request carries ``X-Api-Key: tp_…``; base address ``https://tabpay.org/api``.
* ``POST /v1/payments`` creates an invoice: ``orderId`` is **our opaque payment id** (unique per shop, a
  repeat gives ``409``), ``amountKopecks`` an integer number of kopecks (only RUB). No ``telegramId``, no
  ``metadata``, no ``email`` is sent. When the answer is lost (transport error, ``5xx``) or ``409`` comes
  back, the plugin first looks the order up with ``GET /v1/payments?orderId=`` (the documented recovery)
  and reuses the existing invoice instead of creating blindly.
* ``GET /v1/payments/{id}`` reads one status (``fetch_status``). The list endpoint filters by creation
  time, not by ids, so there is no ``batch_status``.
* Webhooks (scheme v2): ``X-Signature-V2 = hex(HMAC_SHA256(secret, X-Timestamp + "." + raw_body))``,
  ``X-Timestamp`` — Unix seconds (``^\\d+$``), compared in constant time over the raw bytes. The timestamp is
  ``signed_at``; the core rejects every webhook outside ±300 s. The legacy ``X-Signature`` (v1, body only,
  no time) is **not** accepted on its own. ``"test": true`` marks a sandbox / «test webhook» event.
* Statuses: ``CREATED``/``PENDING`` → pending, ``SUCCESS`` → paid (also after ``EXPIRED``/``FAILED`` — late
  SBP payment), ``FAILED``, ``EXPIRED``, ``CANCELED``, ``REFUNDED``. TabPay has no chargeback status; an
  unknown status is acknowledged with ``200`` and logged.
* Amounts: integer kopecks → :class:`~decimal.Decimal` rubles (``19900`` → ``Decimal("199.00")``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final, NoReturn
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
    choice,
    constant_time_equal,
    hmac_sha256_hex,
    parse_timestamp,
    secret,
    url,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "MAX_KOPECKS",
    "MIN_KOPECKS",
    "REPLAY_WINDOW_S",
    "STATUS_MAP",
    "TabPay",
    "TabPayConfig",
    "kopecks_to_rub",
    "sign_v2",
]

DEFAULT_BASE_URL: Final = "https://tabpay.org/api"
REPLAY_WINDOW_S: Final = 300
MIN_KOPECKS: Final = 100
MAX_KOPECKS: Final = 10_000_000_000
#: TabPay payment status → SDK state.
STATUS_MAP: Final[Mapping[str, PaymentState]] = {
    "CREATED": PaymentState.CREATED,
    "PENDING": PaymentState.PROCESSING,
    "SUCCESS": PaymentState.PAID,
    "FAILED": PaymentState.FAILED,
    "EXPIRED": PaymentState.EXPIRED,
    "CANCELED": PaymentState.CANCELED,
    "REFUNDED": PaymentState.REFUNDED,
}
#: States in which an existing invoice found by ``orderId`` can still be paid.
_REUSABLE: Final = frozenset({"CREATED", "PENDING"})
_DESCRIPTION_MAX: Final = 255
_ORDER_ID_MAX: Final = 64
_ID_MAX: Final = 200
_STAMP_RE: Final = re.compile(r"\d{1,12}")
_SIG_RE: Final = re.compile(r"[0-9a-f]{64}")
_UUID_RE: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

_T: Final = {
    "bad_key": "TabPay отклонила API-ключ (или магазин не активен) — проверьте ключ в кабинете TabPay",
    "rejected": "TabPay отклонила запрос (HTTP {status}){detail}",
    "unavailable": "TabPay временно недоступна (HTTP {status})",
    "bad_answer": "TabPay вернула непонятный ответ",
    "currency": "TabPay принимает только рубли",
    "range": "TabPay принимает суммы от 1 до 100 000 000 ₽",
    "order_id": "идентификатор платежа длиннее 64 символов — TabPay его не примет",
    "conflict": "TabPay отказала (HTTP 409): способ оплаты не включён магазину или номер заказа уже занят",
    "taken": "номер заказа уже использован в TabPay для другого счёта",
    "lost": "TabPay не ответила на создание счёта, и счёт не найден — попробуйте ещё раз",
    "probe_ok": "API-ключ принят, TabPay доступна. Секрет подписи проверится на первом вебхуке",
    "probe_same": "секрет подписи совпадает с API-ключом — в TabPay это разные значения (вкладка «Webhook»)",
    "probe_not_tabpay": "по адресу API отвечает не TabPay — проверьте «Адрес API»",
}


class TabPayConfig(ConfigModel):
    api_key = secret(
        "API-ключ",
        "Ключ магазина для запросов к API TabPay (заголовок X-Api-Key, вид tp_…). Показывается один раз; "
        "перевыпуск отзывает старый ключ.",
        where="кабинет TabPay → карточка активного магазина → вкладка «API-ключи» → «Выпустить ключ»",
        pattern=r"tp_\S{4,}",
    )
    webhook_secret = secret(
        "Секрет подписи вебхуков",
        "Им TabPay подписывает уведомления (заголовок X-Signature-V2). Это не API-ключ — значения разные. "
        "URL вебхука из бота впишите там же, на вкладке «Webhook».",
        where="кабинет TabPay → карточка магазина → вкладка «Webhook» → «Секрет подписи»",
    )
    method = choice(
        "Способ оплаты",
        ("SBP", "CARD"),
        "Зафиксировать способ на странице TabPay. Пусто — покупатель выбирает сам (СБП или карта). "
        "Способ должен быть включён магазину, иначе TabPay отказывает в создании счёта.",
        where="кабинет TabPay → карточка магазина → включённые способы оплаты",
        required=False,
        advanced=True,
    )
    base_url = url(
        "Адрес API",
        "Базовый адрес API TabPay. Меняйте, только если TabPay сообщила другой.",
        where="документация TabPay (tabpay.org/docs) или поддержка t.me/tabpaysupport; по умолчанию "
        "https://tabpay.org/api",
        default=DEFAULT_BASE_URL,
        required=False,
        advanced=True,
    )


def sign_v2(webhook_secret: str, timestamp: str, body: bytes) -> str:
    """``hex(HMAC_SHA256(secret, timestamp + "." + raw_body))`` — TabPay signature scheme v2."""
    return hmac_sha256_hex(webhook_secret, timestamp.encode("ascii") + b"." + body)


def kopecks_to_rub(value: Any) -> Decimal:
    """An integer number of kopecks as rubles (``19900`` → ``Decimal("199.00")``); ``ValueError`` else."""
    if isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value():
        value = int(value)  # ``19900.0`` in the JSON
    if type(value) is not int or value < 0:  # bool is excluded: type(True) is bool
        raise ValueError("not kopecks")
    return Decimal(value).scaleb(-2)


def _decimal_json(body: bytes) -> Any:
    """JSON with non-integer numbers as :class:`Decimal` (never ``float``)."""
    return json.loads(body.decode("utf-8"), parse_float=Decimal)


def _our_payment_id(value: Any) -> str | None:
    """Our opaque payment id when ``orderId`` looks like one (a UUID); anything else → ``None``."""
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower()
    return candidate if _UUID_RE.fullmatch(candidate) else None


def _external_id(value: Any) -> str | None:
    """TabPay ``id`` (a UUID; a ``test-…`` string for the cabinet's «test webhook» button)."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if 0 < len(value) <= _ID_MAX else None


class TabPay(PaymentProvider):
    """See module docstring."""

    manifest = Manifest(
        slug="tabpay",
        title="TabPay",
        method_kinds=(MethodKind.SBP, MethodKind.CARD),
        currencies=("RUB",),
        config=TabPayConfig,
        docs_url="https://tabpay.org/docs",
        min_minor=MIN_KOPECKS,
        max_minor=MAX_KOPECKS,
    )
    capabilities = Capabilities(
        webhook_auth=WebhookAuth.SIGNATURE,
        replay_window_s=REPLAY_WINDOW_S,
        fetch_status=True,
        batch_status=False,  # the list endpoint filters by creation time, not by ids
        refund=False,  # refunds only through TabPay support
        recurring=False,  # the separate /recurrent API is not wired into the plugin
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
        return {"X-Api-Key": self.config.api_key, "Accept": "application/json"}

    def _raise_for(self, resp: HttpResponse, what: str) -> NoReturn:
        if resp.status in (401, 403):
            raise ProviderError(_T["bad_key"], retryable=False, status=resp.status)
        if resp.status in (408, 429) or resp.status >= 500:
            raise ProviderError(
                _T["unavailable"].format(status=resp.status), retryable=True, status=resp.status
            )
        self.ctx.log.warning("tabpay: %s answered HTTP %s", what, resp.status)
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
        kopecks = intent.amount.scaleb(2)  # Decimal rubles → kopecks, exact
        if kopecks != kopecks.to_integral_value() or not MIN_KOPECKS <= int(kopecks) <= MAX_KOPECKS:
            raise ProviderError(_T["range"], retryable=False)
        order_id = intent.payment_id
        if not 0 < len(order_id) <= _ORDER_ID_MAX:
            raise ProviderError(_T["order_id"], retryable=False)
        body: dict[str, Any] = {
            "orderId": order_id,  # opaque: never a Telegram id or a subscription link
            "amountKopecks": int(kopecks),
            "description": (intent.description or "Оплата")[:_DESCRIPTION_MAX],
        }
        # ``method_hint`` is not used: the core always fills it with the first kind, which would pin SBP.
        if self.config.method:
            body["method"] = self.config.method
        if intent.return_url and intent.return_url.startswith(("https://", "tg://")):
            body["successUrl"] = intent.return_url[:300]
            body["failUrl"] = intent.return_url[:300]
        resp = await self._post(body)
        if resp is not None and resp.status in (200, 201):
            return self._checkout(self._json(resp), int(kopecks), order_id)
        if resp is not None and resp.status != 409 and resp.status < 500:
            self._raise_for(resp, "create")
        # 409, or the answer was lost (transport error, 5xx): look the order up, never create blindly.
        found = await self._find_order(order_id)
        if found is not None:
            return self._checkout(found, int(kopecks), order_id, existing=True)
        if resp is not None and resp.status == 409:
            raise ProviderError(_T["conflict"], retryable=False, status=409)
        # Not found (404): the first request never reached TabPay — create once more.
        resp = await self._post(body)
        if resp is None:
            raise ProviderError(_T["lost"], retryable=True)
        if resp.status in (200, 201):
            return self._checkout(self._json(resp), int(kopecks), order_id)
        self._raise_for(resp, "create")

    async def _post(self, body: Mapping[str, Any]) -> HttpResponse | None:
        """``POST /v1/payments``; ``None`` when the transport failed (the answer is unknown)."""
        try:
            return await self.ctx.http.request(
                "POST", f"{self._base}/v1/payments", headers=self._headers(), json=dict(body)
            )
        except ProviderError as exc:
            if not exc.retryable:
                raise
            self.ctx.log.warning("tabpay: create got no answer (%s), looking the order up", exc.human)
            return None

    async def _find_order(self, order_id: str) -> dict[str, Any] | None:
        """``GET /v1/payments?orderId=`` — the invoice of our order, or ``None`` (404)."""
        resp = await self.ctx.http.request(
            "GET", f"{self._base}/v1/payments", headers=self._headers(), params={"orderId": order_id}
        )
        if resp.status == 404:
            return None
        if resp.status != 200:
            self._raise_for(resp, "lookup")
        return self._json(resp)

    def _checkout(
        self, data: Mapping[str, Any], kopecks: int, order_id: str, *, existing: bool = False
    ) -> Checkout:
        external = _external_id(data.get("id"))
        pay_url = data.get("payUrl")
        if external is None or not isinstance(pay_url, str) or not re.match(r"https?://", pay_url):
            raise ProviderError(_T["bad_answer"], retryable=False)
        if existing:
            status = str(data.get("status") or "").upper()
            same = data.get("orderId") == order_id and data.get("amountKopecks") == kopecks
            if status not in _REUSABLE or not same:
                self.ctx.log.warning("tabpay: order %s already exists with status %s", order_id, status[:20])
                raise ProviderError(_T["taken"], retryable=False, status=409)
        # ``payUrl`` does not expire until the buyer starts paying (then 20 minutes): no expires_at.
        return Checkout(kind="url", external_id=external, pay_url=pay_url)

    # ---------------------------------------------------------------------------------------- webhook

    async def parse_webhook(self, req: WebhookRequest) -> ProviderEvent:
        given = (req.header("X-Signature-V2") or "").strip().lower()
        stamp = req.header("X-Timestamp") or ""
        if not given or not stamp:
            raise WebhookRejected("missing signature", status=401)
        if not _STAMP_RE.fullmatch(stamp) or not _SIG_RE.fullmatch(given):
            raise WebhookRejected("bad signature format", status=401)
        if not constant_time_equal(sign_v2(self.config.webhook_secret, stamp, req.body), given):
            raise WebhookRejected("bad signature", status=401)
        signed_at = datetime.fromtimestamp(int(stamp), tz=UTC)
        try:
            data = _decimal_json(req.body)
        except (UnicodeDecodeError, ValueError):
            raise WebhookRejected("malformed", status=400) from None
        if not isinstance(data, dict):
            raise WebhookRejected("malformed", status=400)
        raw_status = str(data.get("status") or "").strip().upper()
        state = STATUS_MAP.get(raw_status)
        if state is None:
            # Authentic, but not a status we know (the set may grow): 200 so TabPay stops retrying.
            self.ctx.log.warning("tabpay: webhook with unknown status %r ignored", raw_status[:40])
            raise WebhookIgnored("unknown status")
        status = self._status(data, state, malformed=WebhookRejected)
        is_test = data.get("test") is True
        return ProviderEvent(
            state=status.state,
            external_id=status.external_id,
            payment_id=status.payment_id,
            amount=status.amount,
            currency=status.currency,
            is_test=is_test,
            signed_at=signed_at,
            summary={  # no telegramId / metadata: personal data stays out of the event log
                "id": status.external_id,
                "orderId": status.payment_id,
                "status": raw_status,
                "amount": None if status.amount is None else str(status.amount),
                "test": is_test,
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
        ours = _our_payment_id(data.get("orderId"))
        if external is None and ours is None:
            raise _malformed(malformed, "no payment id")
        amount: Decimal | None = None
        if data.get("amountKopecks") is not None:
            try:
                amount = kopecks_to_rub(data["amountKopecks"])
            except ValueError:
                raise _malformed(malformed, "bad amount") from None
        paid_at: datetime | None = None
        if state is PaymentState.PAID and isinstance(data.get("paidAt"), str):
            try:
                paid_at = parse_timestamp(data["paidAt"])
            except ValueError:
                paid_at = None
        return ProviderStatus(
            state=state,
            external_id=external,
            payment_id=ours,
            amount=amount,
            currency="RUB" if amount is not None else None,
            paid_at=paid_at,
            is_test=data.get("isTest") is True or data.get("test") is True,
        )

    # ----------------------------------------------------------------------------------------- status

    async def fetch_status(self, ids: Sequence[str]) -> list[ProviderStatus]:
        result: list[ProviderStatus] = []
        for external in dict.fromkeys(ids):
            if not external:
                continue
            resp = await self.ctx.http.request(
                "GET", f"{self._base}/v1/payments/{quote(external, safe='')}", headers=self._headers()
            )
            if resp.status in (400, 404):
                continue  # not a TabPay id (400) or unknown / another shop's (404): absent from the result
            if resp.status != 200:
                self._raise_for(resp, "status")
            data = self._json(resp)
            state = STATUS_MAP.get(str(data.get("status") or "").strip().upper())
            if state is None:
                continue
            try:
                result.append(self._status(data, state, external_id=external, malformed=ProviderError))
            except ProviderError:
                self.ctx.log.warning("tabpay: unreadable status of %s skipped", external[:40])
        return result

    # ------------------------------------------------------------------------------------------ probe

    async def test_credentials(self) -> Probe:
        """No side effects: ``GET /v1/balance`` (200 — the key works, 401 — it does not)."""
        if constant_time_equal(self.config.webhook_secret, self.config.api_key):
            return Probe(False, _T["probe_same"])
        resp = await self.ctx.http.request("GET", f"{self._base}/v1/balance", headers=self._headers())
        if resp.status in (401, 403):
            return Probe(False, _T["bad_key"])
        if resp.status in (408, 429) or resp.status >= 500:
            return Probe(False, _T["unavailable"].format(status=resp.status))
        if resp.status == 404:
            return Probe(False, _T["probe_not_tabpay"])
        if resp.status != 200:
            return Probe(False, _T["rejected"].format(status=resp.status, detail=_detail(resp)))
        try:
            data = resp.json()
        except ValueError:
            return Probe(False, _T["probe_not_tabpay"])
        if not isinstance(data, dict) or "availableKopecks" not in data:
            return Probe(False, _T["probe_not_tabpay"])
        return Probe(True, _T["probe_ok"])


def _detail(resp: HttpResponse) -> str:
    """A short provider message for the owner (``: …``); ``message`` may be a string or a list."""
    try:
        data = json.loads(resp.body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    message = data.get("message")
    if isinstance(message, list):
        message = "; ".join(str(m) for m in message[:3])
    if isinstance(message, str) and message.strip():
        return ": " + message.strip()[:160]
    return ""


def _malformed(kind: type[Exception], reason: str) -> Exception:
    if kind is WebhookRejected:
        return WebhookRejected(reason, status=400)
    return ProviderError(_T["bad_answer"], retryable=False)
