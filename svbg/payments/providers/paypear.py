"""PayPear (Pear, ``paypear.ru``) — SBP and Russian cards through the Pear payment page.

Written from scratch by the specification ``docs/providers/paypear.md`` (official documentation
<https://paypear.ru/docs/>, checked on 2026-10-02). No third-party code was used. Imports only :mod:`svbg.sdk`
and the standard library.

* Every request carries HTTP Basic ``<shop id>:<secret key>``; base address ``https://api.paypear.ru/v1``.
* ``POST /payment/`` creates an invoice: ``order_id`` is **our opaque payment id** (a UUID, exactly 36
  characters — Pear's limit), ``amount.value`` a string with two decimals, ``payment_method_data.type`` —
  ``sbp`` or ``card`` (the button the user pressed, else the owner's choice), ``metadata`` carries only the
  payment id. No customer data is sent. The idempotency key is the payment id and goes in **both** spellings
  the documentation uses (``Idempotence-Key`` and ``Idempotency-Key``), so a retry never makes a second
  invoice. A ``500`` / lost answer means «result unknown»: the order is looked up with
  ``GET /payment/order/{order_id}/`` before anything else is decided.
* Answers are ``{"success": true, "result": {...}}``; some pages say ``response`` — both are read.
  The rate limit comes as ``409 TOO_MANY_REQUESTS`` or ``429`` — both are «retry later».
* Webhooks: the ``signature`` field's algorithm is **not published**, so it is ignored. A notification is
  accepted only from Pear's published address (``158.160.85.101``, directly or behind a trusted reverse
  proxy) and only with our ``shop_id`` in the body; the scheme is ``WebhookAuth.IP_ONLY``, therefore the core
  never applies the notification itself — it re-reads the payment with ``GET /payment/{id}/``
  (``fetch_status``) and credits only by that answer.
* Statuses: ``NEW`` → created, ``PROCESS`` → processing, ``CONFIRMED`` → paid (fully refunded → refunded),
  ``CANCELED``, ``EXPIRED``, ``REFUNDED``. Pear has no test mode, no chargebacks and no recurring payments.
* Amounts come as numbers (``1000.99``) or strings (``"100.00"``) and are parsed into
  :class:`~decimal.Decimal`.
"""

from __future__ import annotations

import base64
import ipaddress
import re
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
    WebhookAuth,
    WebhookIgnored,
    WebhookRejected,
    WebhookRequest,
    choice,
    flag,
    integer,
    number,
    parse_amount,
    parse_timestamp,
    secret,
    text,
    url,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_TRUSTED_PROXIES",
    "PEAR_NETWORKS",
    "STATUS_MAP",
    "PayPear",
    "PayPearConfig",
    "basic_auth",
    "client_ip",
    "parse_networks",
]

DEFAULT_BASE_URL: Final = "https://api.paypear.ru/v1"
#: Notification sources published by Pear (https://paypear.ru/docs/get-started/webhook/, 2026-10-02).
PEAR_NETWORKS: Final = ("158.160.85.101/32",)
DEFAULT_TRUSTED_PROXIES: Final = "127.0.0.1/32, ::1/128"
DESCRIPTION_MAX: Final = 128
ORDER_ID_MAX: Final = 36
_ID_MAX: Final = 200
_UUID_RE: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_NETS_RE: Final = r"[0-9A-Fa-f:./,;\s]*"
_METHOD_BY_KIND: Final = {MethodKind.SBP: "sbp", MethodKind.CARD: "card"}

#: Pear payment status → SDK state (§6 of the specification).
STATUS_MAP: Final[Mapping[str, PaymentState]] = {
    "NEW": PaymentState.CREATED,
    "PROCESS": PaymentState.PROCESSING,
    "CONFIRMED": PaymentState.PAID,
    "CANCELED": PaymentState.CANCELED,
    "EXPIRED": PaymentState.EXPIRED,
    "REFUNDED": PaymentState.REFUNDED,
}

_T: Final = {
    "bad_key": "Pear отклонила ID магазина или секретный ключ — проверьте их в кабинете Pear",
    "forbidden": "Pear запретила операцию (HTTP 403) — уточните права магазина у менеджера Pear",
    "rate_limited": "Pear ограничила частоту запросов, повторим позже",
    "unavailable": "Pear временно недоступна (HTTP {status})",
    "unknown": "Pear не подтвердила создание счёта (HTTP {status}) — повторите позже",
    "rejected": "Pear отклонила запрос: {error}",
    "bad_answer": "Pear вернула непонятный ответ",
    "currency": "Pear принимает только RUB",
    "no_return_url": "не задан адрес возврата после оплаты — укажите «Адрес возврата» (ссылку на бота)",
    "too_small": "сумма меньше минимальной для этой кассы Pear ({min} ₽)",
    "too_large": "сумма больше максимальной для этой кассы Pear ({max} ₽)",
    "order_closed": "счёт в Pear уже закрыт ({status}) — создайте новый платёж",
    "probe_ok": "Ключи приняты: магазин {shop}",
    "probe_not_pear": "по адресу API отвечает не Pear — проверьте «Адрес API»",
    "bad_networks": "ожидались IP-адреса или подсети через запятую, например 127.0.0.1/32, 172.16.0.0/12",
    "bad_limits": "минимальная сумма больше максимальной",
}


class PayPearConfig(ConfigModel):
    shop_id = text(
        "ID магазина",
        "Числовой идентификатор магазина в Pear — логин для API (HTTP Basic).",
        where="кабинет Pear (paypear.ru) → Настройки — Магазин → «ID магазина»",
        pattern=r"\d{1,20}",
    )
    secret_key = secret(
        "Секретный ключ",
        "Пароль для API (HTTP Basic). Только ключ настоящего магазина: тестового режима у Pear нет.",
        where="кабинет Pear → Интеграция — Ключи API",
    )
    payment_method = choice(
        "Способ оплаты по умолчанию",
        ("sbp", "card"),
        "Pear требует указать способ в каждом счёте. Если покупатель нажал «СБП» или «Карта», используется "
        "выбранный им способ, иначе — этот.",
        where="способы, подключённые в кабинете Pear → Настройки — Способы оплаты",
        default="sbp",
    )
    return_url = url(
        "Адрес возврата",
        "Куда Pear вернёт покупателя после оплаты или отмены, если бот не передал свой адрес. Обычно ссылка "
        "на бота: https://t.me/<имя_бота>.",
        where="ссылка на вашего бота (@BotFather → имя бота)",
        required=False,
    )
    payment_lifetime_min = integer(
        "Срок оплаты, минут",
        "Сколько минут счёт ждёт оплату (передаётся как expires_at). Пусто — срок по умолчанию Pear.",
        where="на ваше усмотрение, например 60",
        required=False,
        min=5,
        max=43_200,
        advanced=True,
    )
    min_amount = number(
        "Минимальная сумма, ₽",
        "Лимит магазина в Pear: счёт на меньшую сумму бот не создаёт. Пусто — без проверки.",
        where="кабинет Pear → Настройки — Способы оплаты → лимиты",
        required=False,
        min=0.01,
    )
    max_amount = number(
        "Максимальная сумма, ₽",
        "Лимит магазина в Pear: счёт на большую сумму бот не создаёт. Пусто — без проверки.",
        where="кабинет Pear → Настройки — Способы оплаты → лимиты",
        required=False,
        min=0.01,
    )
    verify_ip = flag(
        "Проверять IP уведомлений",
        "Принимать уведомления только с адресов Pear. Статус платежа бот всё равно перепроверяет запросом к "
        "Pear, поэтому выключать стоит, только если прокси не передаёт адрес клиента.",
        where="оставьте включённым",
        default=True,
        advanced=True,
    )
    trusted_proxies = text(
        "Доверенные прокси",
        "IP или подсети вашего reverse proxy (Caddy, nginx), через запятую. Только от них бот читает адрес "
        "отправителя из X-Forwarded-For / X-Real-IP. По умолчанию — этот же сервер.",
        where="адрес контейнера или сети прокси, например 172.16.0.0/12 для docker",
        default=DEFAULT_TRUSTED_PROXIES,
        required=False,
        advanced=True,
        pattern=_NETS_RE,
    )
    allowed_networks = text(
        "IP-адреса Pear",
        "Откуда Pear отправляет уведомления. Меняйте, только если Pear опубликовала новый список.",
        where="paypear.ru/docs → Начало работы → Уведомления → «IP-адреса»",
        default=", ".join(PEAR_NETWORKS),
        required=False,
        advanced=True,
        pattern=_NETS_RE,
    )
    base_url = url(
        "Адрес API",
        "Базовый адрес API Pear. Меняйте, только если Pear сообщила другой.",
        where="paypear.ru/docs → Начало работы → Формат взаимодействия",
        default=DEFAULT_BASE_URL,
        required=False,
        advanced=True,
    )


# ------------------------------------------------------------------------------------------ helpers

Network = ipaddress.IPv4Network | ipaddress.IPv6Network
Address = ipaddress.IPv4Address | ipaddress.IPv6Address


def basic_auth(shop_id: str, secret_key: str) -> str:
    """``Authorization`` value: ``Basic base64("<shop_id>:<secret_key>")``."""
    return "Basic " + base64.b64encode(f"{shop_id}:{secret_key}".encode()).decode("ascii")


def parse_networks(value: str | None) -> tuple[Network, ...]:
    """``"1.2.3.4, 10.0.0.0/8"`` → networks; ``ValueError`` on a bad entry."""
    return tuple(ipaddress.ip_network(p, strict=False) for p in re.split(r"[,;\s]+", value or "") if p)


def _addr(value: Any) -> Address | None:
    """An IP from ``remote`` or a forwarded hop: ``1.2.3.4``, ``1.2.3.4:5678``, ``[::1]:80``, ``::ffff:…``."""
    if not isinstance(value, str):
        return None
    raw = value.strip().strip('"')
    if raw.startswith("["):
        raw = raw[1 : raw.find("]")] if "]" in raw else ""
    elif raw.count(":") == 1:
        raw = raw.split(":", 1)[0]
    try:
        ip = ipaddress.ip_address(raw.split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def _within(ip: Address, nets: Sequence[Network]) -> bool:
    return any(ip.version == net.version and ip in net for net in nets)


def client_ip(req: WebhookRequest, trusted: Sequence[Network]) -> Address | None:
    """The sender's address. Forwarded headers count **only** when the direct peer is a trusted proxy; then
    the right-most ``X-Forwarded-For`` hop that is not a trusted proxy wins; ``X-Real-IP`` only without
    ``X-Forwarded-For``. ``None`` when the address cannot be determined."""
    peer = _addr(req.remote)
    if peer is None or not _within(peer, trusted):
        return peer
    forwarded = req.header("X-Forwarded-For")
    if forwarded is not None and forwarded.strip():
        for hop in reversed([h for h in forwarded.split(",") if h.strip()]):
            ip = _addr(hop)
            if ip is None:
                return None  # garbage in the chain: refuse to guess
            if not _within(ip, trusted):
                return ip
        return peer
    real = _addr(req.header("X-Real-IP"))
    return real if real is not None else peer


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


def _shop_matches(value: Any, shop_id: str) -> bool:
    """``shop_id`` from Pear (a number, sometimes a string) equals ours, compared as integers."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        got: int | None = value
    elif isinstance(value, str) and value.strip().isdigit():
        got = int(value.strip())
    else:
        got = None
    return got is not None and got == int(shop_id)


def _money(obj: Any) -> tuple[Decimal | None, str | None]:
    """``{"value": "179.00" | 179.0, "currency": "RUB"}`` → ``(Decimal, "RUB")``; ``ValueError`` if bad."""
    if obj is None:
        return None, None
    if not isinstance(obj, Mapping):
        raise TypeError("amount is not an object")
    amount = parse_amount(obj.get("value"))
    currency = obj.get("currency")
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Za-z]{3}", currency.strip()):
        raise ValueError("bad currency")
    return amount, currency.strip().upper()


def _state(obj: Mapping[str, Any]) -> PaymentState | None:
    """Payment object → SDK state; ``CONFIRMED`` with refunds covering the whole amount → refunded."""
    state = STATUS_MAP.get(str(obj.get("status") or "").strip().upper())
    if state is PaymentState.PAID:
        try:
            paid, _ = _money(obj.get("amount"))
            refunded, _ = _money(obj.get("refunded_amount"))
        except (TypeError, ValueError):
            return state
        if paid is not None and refunded is not None and refunded > 0 and refunded >= paid:
            return PaymentState.REFUNDED
    return state


def _clip(value: str, limit: int = DESCRIPTION_MAX) -> str:
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def _iso_utc(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class PayPear(PaymentProvider):
    """See module docstring."""

    manifest = Manifest(
        slug="paypear",
        title="Pear",
        method_kinds=(MethodKind.SBP, MethodKind.CARD),
        currencies=("RUB",),
        config=PayPearConfig,
        docs_url="https://paypear.ru/docs/",
    )
    capabilities = Capabilities(
        webhook_auth=WebhookAuth.IP_ONLY,  # the «signature» algorithm is not published
        replay_window_s=None,  # notifications carry no signed time
        fetch_status=True,
        batch_status=False,  # no list / batch method
        refund=False,  # «API refunds are disabled by default» — switched on by Pear's manager
        recurring=False,
        receipt_54fz=False,
        in_chat_invoice=False,
        redirect=True,
        webhook=True,
    )

    def __init__(self, config: ConfigModel, ctx: PluginContext) -> None:
        super().__init__(config, ctx)
        errors: dict[str, str] = {}
        try:
            self._trusted = parse_networks(self.config.trusted_proxies)
        except ValueError:
            errors["trusted_proxies"] = _T["bad_networks"]
        try:
            self._allowed = parse_networks(self.config.allowed_networks or ", ".join(PEAR_NETWORKS))
        except ValueError:
            errors["allowed_networks"] = _T["bad_networks"]
        self._min = None if self.config.min_amount is None else Decimal(str(self.config.min_amount))
        self._max = None if self.config.max_amount is None else Decimal(str(self.config.max_amount))
        if self._min is not None and self._max is not None and self._min > self._max:
            errors["min_amount"] = _T["bad_limits"]
        if errors:
            raise ConfigError(errors)

    # ------------------------------------------------------------------------------------------- HTTP

    @property
    def _base(self) -> str:
        return str(self.config.base_url or DEFAULT_BASE_URL).rstrip("/")

    def _headers(self, idempotence_key: str | None = None) -> dict[str, str]:
        headers = {
            "Authorization": basic_auth(self.config.shop_id, self.config.secret_key),
            "Accept": "application/json",
        }
        if idempotence_key is not None:
            # The documentation spells the header both ways; one value in both is harmless.
            headers["Idempotence-Key"] = idempotence_key
            headers["Idempotency-Key"] = idempotence_key
        return headers

    @staticmethod
    def _body(resp: HttpResponse) -> Any:
        try:
            return resp.json()
        except ValueError:
            return None

    def _object(self, resp: HttpResponse) -> dict[str, Any]:
        """The object inside ``{"success": true, "result" | "response": {...}}``."""
        data = self._body(resp)
        if not isinstance(data, dict) or data.get("success") is False:
            raise ProviderError(_T["bad_answer"], retryable=True, status=resp.status)
        obj = data.get("result")
        if not isinstance(obj, dict):
            obj = data.get("response")
        if not isinstance(obj, dict):
            raise ProviderError(_T["bad_answer"], retryable=True, status=resp.status)
        return obj

    def _error(self, resp: HttpResponse, what: str) -> ProviderError:
        data = self._body(resp)
        error = data.get("error") if isinstance(data, dict) else None
        code = str(error.get("code") or "") if isinstance(error, Mapping) else ""
        if resp.status == 401:
            return ProviderError(_T["bad_key"], retryable=False, status=401)
        if resp.status == 403:
            return ProviderError(_T["forbidden"], retryable=False, status=403)
        if resp.status == 429 or (resp.status == 409 and code.upper() == "TOO_MANY_REQUESTS"):
            return ProviderError(_T["rate_limited"], retryable=True, status=resp.status)
        if resp.status >= 500:
            return ProviderError(
                _T["unavailable"].format(status=resp.status), retryable=True, status=resp.status
            )
        detail = f"HTTP {resp.status}"
        if isinstance(error, Mapping):
            parts = [str(error.get(k)) for k in ("code", "message") if error.get(k)]
            if parts:
                detail = _clip(" — ".join(parts), 200)
        self.ctx.log.warning("paypear: %s answered HTTP %s (%s)", what, resp.status, detail)
        return ProviderError(_T["rejected"].format(error=detail), retryable=False, status=resp.status)

    # ----------------------------------------------------------------------------------------- create

    def _method(self, intent: PaymentIntent) -> str:
        if intent.method_hint in _METHOD_BY_KIND:
            return _METHOD_BY_KIND[intent.method_hint]
        return str(self.config.payment_method or "sbp")

    async def create(self, intent: PaymentIntent) -> Checkout:
        if intent.currency.upper() != "RUB":
            raise ProviderError(_T["currency"], retryable=False)
        amount = intent.amount
        if self._min is not None and amount < self._min:
            raise ProviderError(_T["too_small"].format(min=self._min), retryable=False)
        if self._max is not None and amount > self._max:
            raise ProviderError(_T["too_large"].format(max=self._max), retryable=False)
        return_url = intent.return_url or self.config.return_url
        if not return_url:
            raise ProviderError(_T["no_return_url"], retryable=False)
        order_id = intent.payment_id[:ORDER_ID_MAX]
        body: dict[str, Any] = {
            "order_id": order_id,  # opaque: never a Telegram id or a subscription link
            "description": _clip(intent.description or "Оплата"),
            "amount": {"value": intent.amount_text(), "currency": "RUB"},
            "payment_method_data": {"type": self._method(intent)},
            "confirmation": {"type": "redirect", "return_url": return_url},
            "metadata": {"payment_id": intent.payment_id},
        }
        if self.config.payment_lifetime_min:
            body["expires_at"] = _iso_utc(
                datetime.now(UTC) + timedelta(minutes=int(self.config.payment_lifetime_min))
            )
        if self.ctx.webhook_url:
            body["webhook_url"] = self.ctx.webhook_url
        try:
            resp = await self.ctx.http.request(
                "POST", f"{self._base}/payment/", headers=self._headers(intent.payment_id), json=body
            )
        except ProviderError as exc:
            if not exc.retryable:
                raise
            return await self._recover(order_id, exc)  # the answer was lost: did Pear create it?
        if resp.status == 200:
            return self._checkout(self._object(resp), resp.status)
        if resp.status >= 500:
            # 500: «the result is unknown» (Pear tries to cancel it) — look the order up before deciding.
            return await self._recover(
                order_id, ProviderError(_T["unknown"].format(status=resp.status), retryable=True, status=500)
            )
        raise self._error(resp, "create")

    async def _recover(self, order_id: str, cause: ProviderError) -> Checkout:
        """After a lost / ``5xx`` answer: reuse the invoice if Pear made it and it is still payable."""
        try:
            resp = await self.ctx.http.request(
                "GET", f"{self._base}/payment/order/{quote(order_id, safe='')}/", headers=self._headers()
            )
        except ProviderError:
            raise cause from None
        if resp.status != 200:
            raise cause
        try:
            obj = self._object(resp)
        except ProviderError:
            raise cause from None
        state = _state(obj)
        if state not in (PaymentState.CREATED, PaymentState.PROCESSING):
            status = str(obj.get("status") or "?")[:20]
            raise ProviderError(_T["order_closed"].format(status=status), retryable=True)
        return self._checkout(obj, resp.status)

    def _checkout(self, obj: Mapping[str, Any], status: int) -> Checkout:
        if not _shop_matches(obj.get("shop_id", self.config.shop_id), self.config.shop_id):
            raise ProviderError(_T["bad_answer"], retryable=False, status=status)
        external = _id(obj.get("id"))
        confirmation = obj.get("confirmation")
        pay_url = confirmation.get("confirmation_url") if isinstance(confirmation, Mapping) else None
        if external is None or not isinstance(pay_url, str) or not re.match(r"https?://", pay_url):
            raise ProviderError(_T["bad_answer"], retryable=False, status=status)
        expires_at: datetime | None = None
        if obj.get("expires_at") not in (None, ""):
            try:
                expires_at = parse_timestamp(obj["expires_at"])
            except ValueError:
                expires_at = None
        return Checkout(kind="url", external_id=external, pay_url=pay_url, expires_at=expires_at)

    # ---------------------------------------------------------------------------------------- webhook

    async def parse_webhook(self, req: WebhookRequest) -> ProviderEvent:
        if self.config.verify_ip:
            ip = client_ip(req, self._trusted)
            if ip is None or not _within(ip, self._allowed):
                self.ctx.log.warning("paypear: notification from a foreign address %s rejected", ip)
                raise WebhookRejected("source address is not a Pear address", status=403)
        data = req.json()
        if not isinstance(data, dict) or data.get("type") != "notification":
            raise WebhookRejected("malformed", status=400)
        event = data.get("event")
        obj = data.get("object")
        if not isinstance(event, str) or not isinstance(obj, dict):
            raise WebhookRejected("malformed", status=400)
        # «signature» is ignored on purpose: its algorithm is not published (specification §5.3 p.6).
        if event.startswith("refund."):
            return self._refund_event(event, obj)
        if not event.startswith("payment."):
            raise WebhookIgnored(f"event {event[:40]}")
        if not _shop_matches(obj.get("shop_id"), self.config.shop_id):
            raise WebhookRejected("foreign shop_id", status=403)
        external = _id(obj.get("id"))
        pid = _our_payment_id(obj.get("order_id"))
        if external is None and pid is None:
            raise WebhookRejected("no payment id", status=400)
        state = _state(obj) or STATUS_MAP.get(event.removeprefix("payment.").upper())
        if state is None:
            self.ctx.log.warning("paypear: notification %r with an unknown status ignored", event[:40])
            raise WebhookIgnored("unknown status")
        try:
            amount, currency = _money(obj.get("amount"))
        except (TypeError, ValueError):
            raise WebhookRejected("bad amount", status=400) from None
        return ProviderEvent(
            state=state,
            external_id=external,
            payment_id=pid,
            amount=amount,
            currency=currency,
            is_test=False,  # Pear has no test mode
            summary={
                "event": event[:40],
                "id": external,
                "status": str(obj.get("status") or "")[:20],
                "amount": None if amount is None else str(amount),
                "currency": currency,
            },
        )

    def _refund_event(self, event: str, obj: Mapping[str, Any]) -> ProviderEvent:
        """A refund object has no ``shop_id``; it only points the core at the payment to re-read."""
        if "shop_id" in obj and not _shop_matches(obj.get("shop_id"), self.config.shop_id):
            raise WebhookRejected("foreign shop_id", status=403)
        if event != "refund.confirmed":
            raise WebhookIgnored(f"event {event[:40]}")
        external = _id(obj.get("payment_id"))
        if external is None:
            raise WebhookRejected("no payment id", status=400)
        return ProviderEvent(
            state=PaymentState.REFUNDED,
            external_id=external,
            summary={"event": event, "id": external, "refund_id": _id(obj.get("id"))},
        )

    # ----------------------------------------------------------------------------------------- status

    async def fetch_status(self, ids: Sequence[str]) -> list[ProviderStatus]:
        """``GET /payment/{id}/`` per invoice (there is no batch method)."""
        result: list[ProviderStatus] = []
        for external in dict.fromkeys(ids):
            if not external:
                continue
            resp = await self.ctx.http.request(
                "GET", f"{self._base}/payment/{quote(external, safe='')}/", headers=self._headers()
            )
            if resp.status in (400, 404):
                continue  # not a payment of this shop
            if resp.status != 200:
                raise self._error(resp, "status")
            status = self._status(self._object(resp), external)
            if status is not None:
                result.append(status)
        return result

    def _status(self, obj: Mapping[str, Any], external: str) -> ProviderStatus | None:
        if "shop_id" in obj and not _shop_matches(obj.get("shop_id"), self.config.shop_id):
            self.ctx.log.warning("paypear: payment %s belongs to another shop, skipped", external[:40])
            return None
        state = _state(obj)
        if state is None:
            self.ctx.log.warning("paypear: payment %s has an unknown status, skipped", external[:40])
            return None
        try:
            amount, currency = _money(obj.get("amount"))
        except (TypeError, ValueError):
            self.ctx.log.warning("paypear: unreadable amount of %s", external[:40])
            amount, currency = None, None
        return ProviderStatus(
            state=state,
            external_id=_id(obj.get("id")) or external,
            payment_id=_our_payment_id(obj.get("order_id")),
            amount=amount,
            currency=currency,
            is_test=False,
        )

    # ------------------------------------------------------------------------------------------ probe

    async def test_credentials(self) -> Probe:
        """No side effects: looks up an order that cannot exist (``404`` = the keys were accepted)."""
        probe_order = "svbg-probe-0000000000000000000000000"[:ORDER_ID_MAX]
        resp = await self.ctx.http.request(
            "GET", f"{self._base}/payment/order/{probe_order}/", headers=self._headers()
        )
        if resp.status == 401:
            return Probe(False, _T["bad_key"])
        if resp.status == 403:
            return Probe(False, _T["forbidden"])
        data = self._body(resp)
        if resp.status not in (200, 404):
            return Probe(False, self._error(resp, "probe").human)
        if not isinstance(data, dict) or "success" not in data:
            return Probe(False, _T["probe_not_pear"])
        return Probe(True, _T["probe_ok"].format(shop=self.config.shop_id), {"no_test_mode": True})
