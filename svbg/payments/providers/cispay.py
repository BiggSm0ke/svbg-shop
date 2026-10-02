"""cisPay — Russian cards and SBP through the cisPay Merchant API (07 §4.2, §4.4).

Written from scratch from our specification ``docs/providers/cispay.md`` (official OpenAPI 3.1.0 specification
<https://cispay.app/api/merchant/v1/openapi.json>, developer page <https://cispay.app/developers>, checked on
2026-10-02). Imports only :mod:`svbg.sdk` and the standard library.

* Every request carries ``X-Shop-ID`` and ``X-Api-Key`` (no request signature). Amounts are integer kopecks.
* ``POST /payments``: ``order_id`` and ``payload`` are **only** the opaque payment id; cisPay's transaction id
  (UUIDv7) is our ``external_id``. ``payment_method`` is mandatory: the user's method kind, otherwise the
  owner's default, otherwise SBP when the shop has it active (``GET /store/capabilities``, cached). SBP needs
  a ``customer_id``: the opaque per-user ``customer_ref`` (never a Telegram id). CARD below 50 ₽ is refused
  before any request.
* Webhook: ``X-Signature = hex(HMAC-SHA256(key = API key, raw body))``, compared in constant time; the body's
  ``store_id`` must be our shop. The signed ``timestamp`` is logged as ``signed_at`` but no freshness window
  is declared — it is not documented whether retries (1, 5, 15, 60 min) re-sign it; replays are dropped by the
  core's dedup and monotonic transitions. Sandbox events (``is_sandbox: true``) are reported ``is_test``.
* ``fetch_status``: ``GET /payments/status?id=`` per invoice. A ``block_reason`` (cisPay blocked the buyer —
  «a real payment will never be created») closes the invoice as ``failed``.
* Refund: full only, ``POST /payments/refund?id=`` with ``confirm_amount`` = the transaction's
  ``charged_amount`` (read first). The same API key also allows payouts — the plugin never calls them.
* Direct SBP QR (needs the buyer's IP) and subscriptions are not used.
"""

from __future__ import annotations

import json
import re
import time
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
    constant_time_equal,
    hmac_sha256_hex,
    parse_timestamp,
    secret,
    text,
    url,
)

__all__ = [
    "CARD_MIN_MINOR",
    "DEFAULT_BASE_URL",
    "STATUS_MAP",
    "CisPay",
    "CisPayConfig",
    "webhook_signature",
]

DEFAULT_BASE_URL: Final = "https://api.cispay.app"
#: «Минимальная сумма для CARD — 50 ₽ (5000 копеек), для SBP минимальная сумма не ограничена».
CARD_MIN_MINOR: Final = 5000
#: ``TransactionStatus`` → SDK state (spec §4.4).
STATUS_MAP: Final[Mapping[str, PaymentState]] = {
    "PENDING": PaymentState.CREATED,
    "PAID": PaymentState.PAID,
    "FAILED": PaymentState.FAILED,
    "EXPIRED": PaymentState.EXPIRED,
    "REFUNDED": PaymentState.REFUNDED,
}
_METHODS: Final[Mapping[MethodKind, str]] = {MethodKind.CARD: "CARD", MethodKind.SBP: "SBP"}
_CAPS_TTL_S: Final = 3600.0
_ID_MAX: Final = 200
_ORDER_MAX: Final = 255
_DESCRIPTION_MAX: Final = 512
_URL_MAX: Final = 1024
_UUID_RE: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_HEX64_RE: Final = re.compile(r"[0-9a-fA-F]{64}")

_T: Final = {
    "bad_key": "cisPay отклонила Shop ID или API-ключ (HTTP {status}) — проверьте их в «Настройках магазина»",
    "rejected": "cisPay отклонила запрос (HTTP {status}){detail}",
    "unavailable": "cisPay временно недоступна (HTTP {status})",
    "bad_answer": "cisPay вернула непонятный ответ",
    "currency": "cisPay принимает только RUB",
    "card_min": "оплата картой в cisPay — от 50 ₽",
    "no_method": "в магазине cisPay нет активного способа оплаты (карта или СБП)",
    "method_off": "способ оплаты {method} не активен в магазине cisPay",
    "taken": "номер счёта уже занят в cisPay (счёт {id} создан раньше, ссылка на оплату не возвращается)",
    "not_found": "платёж не найден в cisPay",
    "partial": "cisPay делает только полный возврат",
    "blocked": "оплата заблокирована cisPay",
    "probe_ok": "Ключи приняты, магазин «{name}» активен; способы: {methods}",
    "probe_inactive": "Ключи приняты, но магазин cisPay не активен",
    "probe_other_shop": "API-ключ относится к другому магазину — проверьте Shop ID",
    "probe_not_cispay": "по адресу API отвечает не cisPay — проверьте «Адрес API»",
    "methods_none": "нет активных",
}


class CisPayConfig(ConfigModel):
    shop_id = text(
        "Shop ID",
        "Идентификатор магазина (UUID), заголовок X-Shop-ID.",
        where="кабинет cisPay (cispay.app) → «Настройки магазина» → Shop ID",
        pattern=r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}",
    )
    api_key = secret(
        "Секретный API-ключ",
        "Ключ cis_sec_… (заголовок X-Api-Key); им же cisPay подписывает вебхуки. Внимание: этот ключ даёт и "
        "вывод баланса (POST /payouts) — он равносилен доступу к деньгам магазина, храните его как пароль.",
        where="кабинет cisPay → «Настройки магазина» → секретный ключ магазина",
    )
    default_method = choice(
        "Способ оплаты по умолчанию",
        ("auto", "sbp", "card"),
        "cisPay требует способ при создании счёта. Если пользователь не выбрал способ: auto — СБП, если он "
        "активен в магазине, иначе карта; sbp или card — всегда этот способ.",
        where="на ваше усмотрение; по умолчанию auto",
        default="auto",
        required=False,
        advanced=True,
    )
    return_url = url(
        "Адрес возврата после оплаты",
        "Куда cisPay вернёт покупателя после оплаты или отказа. Пусто — адрес, который передаёт бот, или "
        "настройки магазина в cisPay.",
        where="например, ссылка на бота https://t.me/<имя_бота>",
        required=False,
        advanced=True,
    )
    base_url = url(
        "Адрес API",
        "Меняйте, только если cisPay сообщила другой.",
        where="спецификация cisPay (cispay.app/developers) → servers",
        default=DEFAULT_BASE_URL,
        required=False,
        advanced=True,
    )


def webhook_signature(api_key: str, body: bytes) -> str:
    """``hex(HMAC-SHA256(key = UTF-8(API key), msg = raw body))`` (spec §4.3)."""
    return hmac_sha256_hex(api_key, body)


# --------------------------------------------------------------------------------------------- helpers


def _id(value: Any, limit: int = _ID_MAX) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if 0 < len(value) <= limit else None


def _our_payment_id(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and _UUID_RE.fullmatch(value.strip().lower()):
            return value.strip().lower()
    return None


def _kopecks(value: Any) -> int | None:
    """An integer amount in kopecks (an integral JSON number such as ``100000`` or ``100000.0``)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, Decimal | float) and value == int(value) and value >= 0:
        return int(value)
    return None


def _when(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        return parse_timestamp(value)
    except ValueError:
        return None


def _detail(resp: HttpResponse) -> str:
    try:
        data = resp.json()
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    detail = data.get("detail")
    if isinstance(detail, str) and detail.strip():
        return ": " + detail.strip()[:160]
    if isinstance(detail, list):
        parts = []
        for item in detail[:3]:
            if isinstance(item, Mapping) and isinstance(item.get("msg"), str):
                loc = item.get("loc")
                where = ".".join(str(x) for x in loc[1:]) if isinstance(loc, list) else ""
                parts.append(f"{where}: {item['msg']}" if where else str(item["msg"]))
        if parts:
            return ": " + "; ".join(parts)[:160]
    return ""


class CisPay(PaymentProvider):
    """See module docstring."""

    manifest = Manifest(
        slug="cispay",
        title="cisPay",
        method_kinds=(MethodKind.SBP, MethodKind.CARD),
        currencies=("RUB",),
        config=CisPayConfig,
        docs_url="https://cispay.app/developers",
        # CARD ≥ 50 ₽ is checked in create(); SBP has no minimum; no maximum is published.
    )
    capabilities = Capabilities(
        webhook_auth=WebhookAuth.SIGNATURE,
        replay_window_s=None,  # whether retries re-sign «timestamp» is not documented (spec §4.3, §7 q.2)
        fetch_status=True,
        batch_status=False,
        refund=True,
        recurring=False,  # /subscriptions need the administrator's permission and are not wired in
        receipt_54fz=False,
        in_chat_invoice=False,
        redirect=True,
        webhook=True,
    )

    def __init__(self, config: ConfigModel, ctx: PluginContext) -> None:
        super().__init__(config, ctx)
        self._caps: dict[str, Any] | None = None
        self._caps_at = 0.0

    # ------------------------------------------------------------------------------------------- HTTP

    @property
    def _base(self) -> str:
        return str(self.config.base_url or DEFAULT_BASE_URL).rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {
            "X-Shop-ID": str(self.config.shop_id),
            "X-Api-Key": str(self.config.api_key),
            "Accept": "application/json",
        }

    def _raise_for(self, resp: HttpResponse, what: str) -> None:
        if resp.status in (401, 403):
            raise ProviderError(_T["bad_key"].format(status=resp.status), retryable=False, status=resp.status)
        if resp.status in (408, 429) or resp.status >= 500:
            raise ProviderError(
                _T["unavailable"].format(status=resp.status), retryable=True, status=resp.status
            )
        self.ctx.log.warning("cispay: %s answered HTTP %s", what, resp.status)
        raise ProviderError(
            _T["rejected"].format(status=resp.status, detail=_detail(resp)),
            retryable=False,
            status=resp.status,
        )

    @staticmethod
    def _json(resp: HttpResponse) -> dict[str, Any]:
        try:
            data = json.loads(resp.body.decode("utf-8"), parse_float=Decimal)
        except (UnicodeDecodeError, ValueError):
            raise ProviderError(_T["bad_answer"], retryable=True, status=resp.status) from None
        if not isinstance(data, dict):
            raise ProviderError(_T["bad_answer"], retryable=True, status=resp.status)
        return data

    async def _get(self, path: str, params: Mapping[str, str] | None = None) -> HttpResponse:
        return await self.ctx.http.request(
            "GET", f"{self._base}{path}", headers=self._headers(), params=dict(params or {})
        )

    async def _capabilities(self, *, fresh: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if not fresh and self._caps is not None and now - self._caps_at < _CAPS_TTL_S:
            return self._caps
        resp = await self._get("/store/capabilities")
        if resp.status != 200:
            self._raise_for(resp, "capabilities")
        self._caps, self._caps_at = self._json(resp), now
        return self._caps

    @staticmethod
    def _active(caps: Mapping[str, Any]) -> set[str]:
        methods = caps.get("payment_methods")
        if not isinstance(methods, list):
            return set()
        return {
            str(m.get("payment_method") or "").upper()
            for m in methods
            if isinstance(m, Mapping) and m.get("is_active") is True
        }

    # ----------------------------------------------------------------------------------------- create

    async def _method(self, intent: PaymentIntent) -> str:
        if intent.method_hint in _METHODS:
            return _METHODS[intent.method_hint]
        configured = str(self.config.default_method or "auto")
        if configured in ("sbp", "card"):
            return configured.upper()
        active = self._active(await self._capabilities())
        if "SBP" in active:
            return "SBP"
        if "CARD" in active:
            return "CARD"
        raise ProviderError(_T["no_method"], retryable=False)

    async def create(self, intent: PaymentIntent) -> Checkout:
        if intent.currency.upper() != "RUB":
            raise ProviderError(_T["currency"], retryable=False)
        method = await self._method(intent)
        if method == "CARD" and intent.amount_minor < CARD_MIN_MINOR:
            raise ProviderError(_T["card_min"], retryable=False)
        pid = intent.payment_id  # opaque: never a Telegram id or a subscription link
        body: dict[str, Any] = {
            "amount": int(intent.amount_minor),
            "currency": "RUB",
            "order_id": pid,
            "payment_method": method,
            "description": " ".join((intent.description or "Оплата").split())[:_DESCRIPTION_MAX],
            "payload": pid,
        }
        if method == "SBP":
            body["customer_id"] = intent.customer_ref[:255]  # an opaque per-user hash (spec §7 q.4)
        back = intent.return_url or self.config.return_url
        if back and re.match(r"https?://", back) and len(back) <= _URL_MAX:
            body["redirect_success_url"] = back
            body["redirect_fail_url"] = back
        resp = await self.ctx.http.request(
            "POST", f"{self._base}/payments", headers=self._headers(), json=body
        )
        if resp.status == 400:
            await self._taken(pid, resp)
        if resp.status not in (200, 201):
            if resp.status == 403:
                self._caps = None  # a method may have been switched off
            self._raise_for(resp, "create")
        data = self._json(resp)
        external = _id(data.get("id"))
        pay_url = data.get("payment_url")
        if external is None or not isinstance(pay_url, str) or not re.match(r"https?://", pay_url.strip()):
            raise ProviderError(_T["bad_answer"], retryable=False, status=resp.status)
        if _id(data.get("order_id"), _ORDER_MAX) not in (None, pid):
            raise ProviderError(_T["bad_answer"], retryable=False, status=resp.status)
        return Checkout(kind="url", external_id=external, pay_url=pay_url.strip())

    async def _taken(self, pid: str, resp: HttpResponse) -> None:
        """``400`` — usually a repeated ``order_id`` after a lost answer: the transaction is looked up, but
        the status answer has no payment link, so the invoice cannot be resumed."""
        try:
            found = await self._get("/payments/status", {"order_id": pid})
        except ProviderError:
            return
        if found.status != 200:
            return
        try:
            data = self._json(found)
        except ProviderError:
            return
        external = _id(data.get("id"))
        if external is not None and _id(data.get("order_id"), _ORDER_MAX) == pid:
            self.ctx.log.warning("cispay: order %s already exists as %s", pid[:40], external[:40])
            raise ProviderError(_T["taken"].format(id=external[:40]), retryable=False, status=resp.status)

    # ---------------------------------------------------------------------------------------- webhook

    async def parse_webhook(self, req: WebhookRequest) -> ProviderEvent:
        given = (req.header("X-Signature") or "").strip()
        if not given:
            raise WebhookRejected("missing signature", status=401)
        if not _HEX64_RE.fullmatch(given):
            raise WebhookRejected("signature is not hex", status=401)
        expected = webhook_signature(str(self.config.api_key), req.body)
        if not constant_time_equal(expected, given.lower()):
            raise WebhookRejected("bad signature", status=401)
        try:
            data = json.loads(req.body.decode("utf-8"), parse_float=Decimal)
        except (UnicodeDecodeError, ValueError):
            raise WebhookRejected("malformed", status=400) from None
        if not isinstance(data, dict):
            raise WebhookRejected("malformed", status=400)
        if str(data.get("store_id") or "").strip().lower() != str(self.config.shop_id).strip().lower():
            raise WebhookRejected("another shop", status=401)
        raw_status = str(data.get("status") or "").strip().upper()
        state = STATUS_MAP.get(raw_status)
        if state is None:
            self.ctx.log.warning("cispay: webhook with unknown status %r ignored", raw_status[:20])
            raise WebhookIgnored("unknown status")
        if data.get("subscription_id") not in (None, ""):
            # A subscription cycle (subscriptions are not used by the bot): acknowledged, never credited.
            self.ctx.log.info("cispay: subscription webhook acknowledged and ignored")
            raise WebhookIgnored("subscription cycle")
        status = self._status(data, state, malformed=WebhookRejected)
        return ProviderEvent(
            state=status.state,
            external_id=status.external_id,
            payment_id=status.payment_id,
            amount=status.amount,
            currency=status.currency,
            paid_at=status.paid_at,
            is_test=status.is_test,
            signed_at=_when(data.get("timestamp")),  # logged only: no freshness window is declared
            summary={
                "id": status.external_id,
                "order_id": _id(data.get("order_id"), 64),
                "status": raw_status,
                "method": _id(data.get("payment_method"), 10),
                "amount": _id(data.get("amount"), 20),
                "charged_amount": _id(data.get("charged_amount"), 20),
                "merchant_revenue": _id(data.get("merchant_revenue"), 20),
                "is_sandbox": data.get("is_sandbox") is True,
            },
        )

    def _status(
        self, data: Mapping[str, Any], state: PaymentState, *, malformed: type[Exception]
    ) -> ProviderStatus:
        external = _id(data.get("id"))
        if external is None:
            raise _malformed(malformed, "no id")
        kopecks = _kopecks(data.get("amount"))
        if kopecks is None:
            raise _malformed(malformed, "bad amount")
        currency = data.get("currency") if data.get("currency") not in (None, "") else "RUB"
        if not isinstance(currency, str) or not re.fullmatch(r"[A-Za-z]{3}", currency.strip()):
            raise _malformed(malformed, "bad currency")
        block = data.get("block_reason")
        blocked = isinstance(block, str) and bool(block.strip())
        if blocked:
            # cisPay blocked this buyer: «a real payment will never be created» — whatever the status says.
            state = PaymentState.FAILED
        return ProviderStatus(
            state=state,
            external_id=external,
            payment_id=_our_payment_id(data.get("payload"), data.get("order_id")),
            amount=Decimal(kopecks).scaleb(-2),
            currency=currency.strip().upper(),
            paid_at=_when(data.get("paid_at")) if state is PaymentState.PAID else None,
            is_test=data.get("is_sandbox") is True and not blocked,
        )

    # ----------------------------------------------------------------------------------------- status

    async def _status_raw(self, external: str) -> dict[str, Any] | None:
        resp = await self._get("/payments/status", {"id": external})
        if resp.status in (404, 422):
            return None  # unknown id (the code is not documented: 404 expected, 422 for a non-UUID)
        if resp.status != 200:
            self._raise_for(resp, "status")
        data = self._json(resp)
        if _id(data.get("id")) != external:
            self.ctx.log.warning("cispay: status answered for another transaction")
            return None
        return data

    async def fetch_status(self, ids: Sequence[str]) -> list[ProviderStatus]:
        """``GET /payments/status?id=`` per invoice; unknown ids are absent."""
        result: list[ProviderStatus] = []
        for external in dict.fromkeys(ids):
            if not external:
                continue
            data = await self._status_raw(external)
            if data is None:
                continue
            raw_status = str(data.get("status") or "").strip().upper()
            state = STATUS_MAP.get(raw_status)
            if state is None:
                self.ctx.log.warning(
                    "cispay: unknown status %r of %s skipped", raw_status[:20], external[:40]
                )
                continue
            try:
                result.append(self._status(data, state, malformed=ProviderError))
            except ProviderError:
                self.ctx.log.warning("cispay: unreadable transaction %s skipped", external[:40])
        return result

    # ----------------------------------------------------------------------------------------- refund

    async def refund(self, external_id: str, amount_minor: int, currency: str) -> RefundResult:
        """Full refund only: ``confirm_amount`` must equal the transaction's ``charged_amount``."""
        if currency.upper() != "RUB":
            return RefundResult(False, None, _T["currency"])
        try:
            data = await self._status_raw(external_id)
            if data is None:
                return RefundResult(False, None, _T["not_found"])
            charged = _kopecks(data.get("charged_amount"))
            net = _kopecks(data.get("amount"))
            if charged is None or net is None:
                return RefundResult(False, None, _T["bad_answer"])
            if amount_minor not in (net, charged):
                return RefundResult(False, None, _T["partial"])
            resp = await self.ctx.http.request(
                "POST",
                f"{self._base}/payments/refund",
                headers=self._headers(),
                params={"id": external_id},
                json={"confirm_amount": charged},
            )
            if resp.status != 200:
                self._raise_for(resp, "refund")
            answer = self._json(resp)
        except ProviderError as exc:
            return RefundResult(False, None, exc.human)
        return RefundResult(True, _id(answer.get("transaction_id")) or external_id, "")

    # ------------------------------------------------------------------------------------------ probe

    async def test_credentials(self) -> Probe:
        """No side effects: ``GET /store/capabilities`` (shop, its activity and payment methods)."""
        try:
            caps = await self._capabilities(fresh=True)
        except ProviderError as exc:
            if exc.status in (404, 405) or exc.human == _T["bad_answer"]:
                return Probe(False, _T["probe_not_cispay"])
            return Probe(False, exc.human)
        if str(caps.get("store_id") or "").lower() != str(self.config.shop_id).lower():
            return Probe(False, _T["probe_other_shop"])
        if caps.get("is_active") is not True:
            return Probe(False, _T["probe_inactive"])
        active = sorted(self._active(caps) & {"CARD", "SBP"})
        name = str(caps.get("store_name") or "")[:60]
        methods = ", ".join(active) if active else _T["methods_none"]
        return Probe(
            bool(active),
            _T["probe_ok"].format(name=name, methods=methods),
            {"methods": active, "direct_sbp": caps.get("direct_sbp_enabled") is True},
        )


def _malformed(kind: type[Exception], reason: str) -> Exception:
    if kind is WebhookRejected:
        return WebhookRejected(reason, status=400)
    return ProviderError(_T["bad_answer"], retryable=False)
