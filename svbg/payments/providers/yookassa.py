"""ЮKassa (API v3) — cards and SBP with 54-FZ receipts, wave B (07 §4.2, 04 D12–D14).

Ported from Remnashop (MIT, © 2024 snoups) ``src/infrastructure/payment_gateways/yookassa.py`` and rewritten
for the SvBG SDK — see ``THIRD_PARTY_NOTICES.md``; specification: ``docs/providers/yookassa.md`` (checked
against the official documentation <https://yookassa.ru/developers> on 2026-10-02). Imports only
:mod:`svbg.sdk`.

* ``POST /v3/payments`` with HTTP Basic (``shopId:secret key``), ``Idempotence-Key = payment id`` (a retry of
  the same invoice never creates a second payment; ``202``/``500``/transport errors are retried with the
  **same** key), ``capture: true``, ``confirmation: redirect``; ``payment_method_data`` = ``sbp`` /
  ``bank_card`` by the button the user pressed (or the owner's choice, or the cash desk page); ``metadata``
  carries **only** the opaque payment id; optional 54-FZ ``receipt`` (VAT code, payment subject and mode, tax
  system, the receipt contact — e-mail or phone — from the settings).
* Webhooks are **not signed** by ЮKassa: the plugin accepts a notification only from the official ЮKassa
  networks (seen directly or behind a trusted reverse proxy via ``X-Forwarded-For``) and declares
  ``WebhookAuth.IP_ONLY``, so the core never changes a payment from a notification: it always re-reads it with
  ``GET /v3/payments/{id}`` (``fetch_status``). Remnashop trusted the IP (taken from forgeable headers) alone.
* ``fetch_status``: one ``GET`` per invoice (the list API has no id filter, so no batch). ``succeeded`` →
  paid (fully refunded → refunded); ``canceled`` → canceled (``expired_on_*`` → expired); a stray
  ``waiting_for_capture`` (two-stage payments switched on in the shop) is captured with a deterministic
  idempotence key, so the user's money is not held and released.
* Amounts are strings (``"179.00"``) parsed into :class:`~decimal.Decimal`; ``test: true`` marks a test shop.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
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
    flag,
    parse_amount,
    parse_timestamp,
    secret,
    text,
    url,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_TRUSTED_PROXIES",
    "YOOKASSA_NETWORKS",
    "YooKassa",
    "YooKassaConfig",
    "client_ip",
    "parse_networks",
]

DEFAULT_BASE_URL: Final = "https://api.yookassa.ru/v3"
#: Official notification sources (https://yookassa.ru/developers/using-api/webhooks, 2026-10-02).
YOOKASSA_NETWORKS: Final = (
    "185.71.76.0/27",
    "185.71.77.0/27",
    "77.75.153.0/25",
    "77.75.156.11/32",
    "77.75.156.35/32",
    "77.75.154.128/25",
    "2a02:5180::/32",
)
DEFAULT_TRUSTED_PROXIES: Final = "127.0.0.1/32, ::1/128"
DESCRIPTION_MAX: Final = 128  # payment description and receipt item name
CREATE_ATTEMPTS: Final = 3
RETRY_DELAY_MAX_S: Final = 1.5
_ID_MAX: Final = 200
_UUID_RE: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_EXPIRED_REASONS: Final = frozenset({"expired_on_confirmation", "expired_on_capture"})
_METHOD_BY_KIND: Final = {MethodKind.SBP: "sbp", MethodKind.CARD: "bank_card"}
_NETS_RE: Final = r"[0-9A-Fa-f:./,;\s]*"
_EMAIL_RE: Final = r"[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,63}"
_PHONE_RE: Final = r"\+?\d{11,15}"

VAT_CODES: Final = tuple(str(n) for n in range(1, 13))
PAYMENT_SUBJECTS: Final = (
    "service", "commodity", "job", "payment", "intellectual_activity", "property_right", "agent_commission",
    "another", "excise", "casino", "gambling_bet", "gambling_prize", "lottery", "lottery_prize",
    "non_operating_gain", "insurance_premium", "sales_tax", "resort_fee", "marked", "non_marked",
    "marked_excise", "non_marked_excise", "fine", "tax", "lien", "cost", "agent_withdrawals",
    "pension_insurance_without_payouts", "pension_insurance_with_payouts",
    "health_insurance_without_payouts", "health_insurance_with_payouts", "health_insurance",
)  # fmt: skip
PAYMENT_MODES: Final = (
    "full_payment", "full_prepayment", "partial_prepayment", "advance", "partial_payment", "credit",
    "credit_payment",
)  # fmt: skip
TAX_SYSTEMS: Final = ("1", "2", "3", "4", "5", "6")

_T: Final = {
    "bad_key": "ЮKassa отклонила shopId или секретный ключ — проверьте их в личном кабинете",
    "forbidden": "ЮKassa запретила операцию — включите её в личном кабинете или у менеджера ЮKassa",
    "rejected": "ЮKassa отклонила запрос: {error}",
    "unavailable": "ЮKassa временно недоступна (HTTP {status})",
    "unknown": "ЮKassa не подтвердила результат (HTTP {status}) — повторите позже",
    "bad_answer": "ЮKassa вернула непонятный ответ",
    "no_return_url": "не задан адрес возврата после оплаты — укажите «Адрес возврата» (ссылку на бота)",
    "probe_ok": "Ключи приняты: магазин {account}{mode}",
    "probe_test_mismatch": " — внимание: это {shop} магазин, а инстанс в {inst} режиме",
    "probe_no_fiscal": " — внимание: чеки включены в настройках, но в магазине не настроена отправка чеков",
    "no_contact": "для чеков нужен e-mail или телефон, куда ЮKassa отправит чек",
    "bad_networks": "ожидались IP-адреса или подсети через запятую, например 127.0.0.1/32, 172.16.0.0/12",
}


class YooKassaConfig(ConfigModel):
    shop_id = text(
        "Идентификатор магазина (shopId)",
        "Номер магазина в ЮKassa — логин для API.",
        where="личный кабинет ЮKassa → Интеграция → Ключи API → «shopId» (или Настройки → Магазин)",
        pattern=r"\d{1,20}",
    )
    secret_key = secret(
        "Секретный ключ",
        "Пароль для API. У тестового магазина свой ключ (начинается с test_), у боевого — свой (live_).",
        where="личный кабинет ЮKassa → Интеграция → Ключи API → «Выпустить секретный ключ»",
    )
    return_url = url(
        "Адрес возврата",
        "Куда ЮKassa вернёт покупателя после оплаты, если бот не передал свой адрес. Обычно ссылка на бота: "
        "https://t.me/<имя_бота>.",
        where="ссылка на вашего бота (@BotFather → имя бота)",
        required=False,
    )
    payment_method = choice(
        "Способ оплаты по умолчанию",
        ("auto", "sbp", "bank_card"),
        "Если покупатель нажал «СБП» или «Карта», используется выбранный им способ. Иначе: auto — покупатель "
        "выбирает на странице ЮKassa, sbp — СБП, bank_card — карта.",
        where="способы, подключённые в личном кабинете ЮKassa → Настройки → Способы оплаты",
        default="auto",
    )
    receipt_enabled = flag(
        "Передавать чек (54-ФЗ)",
        "Включите, если в ЮKassa настроены «Чеки от ЮKassa» или онлайн-касса: к каждому платежу будет "
        "приложен чек с одной позицией.",
        where="личный кабинет ЮKassa → Настройки → Отправка чеков",
    )
    receipt_contact = choice(
        "Куда отправлять чек",
        ("email", "phone"),
        "Бот не спрашивает контакты покупателя, поэтому чек уходит на указанный ниже e-mail или телефон "
        "магазина. «Чеки от ЮKassa» отправляют чек только на e-mail.",
        where="ваш e-mail или телефон для чеков",
        default="email",
    )
    receipt_email = text(
        "E-mail для чеков",
        "Адрес, на который ЮKassa отправит чек (customer.email).",
        where="ваш рабочий e-mail",
        required=False,
        pattern=_EMAIL_RE,
    )
    receipt_phone = text(
        "Телефон для чеков",
        "Телефон в формате 79991234567 (customer.phone), только для сторонней онлайн-кассы.",
        where="ваш рабочий телефон",
        required=False,
        pattern=_PHONE_RE,
    )
    vat_code = choice(
        "Ставка НДС (vat_code)",
        VAT_CODES,
        "1 — без НДС, 2 — 0%, 3 — 10%, 4 — 20%, 5 — 10/110, 6 — 20/120, 7 — 5%, 8 — 7%, 9 — 5/105, "
        "10 — 7/107, 11 — 22% (с 2026 года), 12 — 22/122.",
        where="у вашего бухгалтера; справочник — yookassa.ru/developers/54fz/parameters-values",
        default="1",
    )
    payment_subject = choice(
        "Признак предмета расчёта",
        PAYMENT_SUBJECTS,
        "Для подписки VPN обычно service (услуга).",
        where="у вашего бухгалтера; справочник — yookassa.ru/developers/54fz/parameters-values",
        default="service",
        advanced=True,
    )
    payment_mode = choice(
        "Признак способа расчёта",
        PAYMENT_MODES,
        "full_payment — полный расчёт (услуга оказана сразу), full_prepayment — 100% предоплата.",
        where="у вашего бухгалтера; справочник — yookassa.ru/developers/54fz/parameters-values",
        default="full_payment",
        advanced=True,
    )
    tax_system_code = choice(
        "Система налогообложения",
        TAX_SYSTEMS,
        "Нужна, только если у магазина несколько систем налогообложения: 1 — ОСН, 2 — УСН доходы, "
        "3 — УСН доходы минус расходы, 4 — ЕНВД, 5 — ЕСХН, 6 — патент. Пусто — не передаётся.",
        where="у вашего бухгалтера",
        required=False,
        advanced=True,
    )
    receipt_item_name = text(
        "Название позиции в чеке",
        "До 128 символов. Пусто — описание платежа (например, «Пополнение баланса на 179 ₽»).",
        where="придумайте сами, например «Доступ к VPN-сервису»",
        required=False,
        advanced=True,
    )
    auto_capture = flag(
        "Списывать удержанные платежи",
        "Если в магазине включены двухстадийные платежи, бот сам подтвердит (capture) оплату в статусе "
        "waiting_for_capture, чтобы деньги не вернулись покупателю.",
        default=True,
        advanced=True,
    )
    verify_ip = flag(
        "Проверять IP уведомлений",
        "Принимать уведомления только из сетей ЮKassa. Статус платежа бот всё равно перепроверяет запросом "
        "к ЮKassa, поэтому выключать стоит, только если прокси не передаёт адрес клиента.",
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
        "Сети ЮKassa",
        "Откуда ЮKassa отправляет уведомления. Меняйте, только если ЮKassa опубликовала новый список.",
        where="yookassa.ru/developers/using-api/webhooks → «IP-адреса»",
        default=", ".join(YOOKASSA_NETWORKS),
        required=False,
        advanced=True,
        pattern=_NETS_RE,
    )
    base_url = url(
        "Адрес API",
        "Базовый адрес API ЮKassa. Меняйте, только если ЮKassa сообщила другой.",
        default=DEFAULT_BASE_URL,
        required=False,
        advanced=True,
    )


Network = ipaddress.IPv4Network | ipaddress.IPv6Network
Address = ipaddress.IPv4Address | ipaddress.IPv6Address


def parse_networks(value: str | None) -> tuple[Network, ...]:
    """``"1.2.3.4, 10.0.0.0/8"`` → networks; ``ValueError`` on a bad entry."""
    nets: list[Network] = []
    for part in re.split(r"[,;\s]+", value or ""):
        if part:
            nets.append(ipaddress.ip_network(part, strict=False))
    return tuple(nets)


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
    """The address that sent the notification. Forwarded headers are read **only** when the direct peer is
    a trusted proxy; then the right-most ``X-Forwarded-For`` hop that is not a trusted proxy wins (proxies
    append to the right, so a client-supplied left part is ignored); ``X-Real-IP`` only without
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


def _external_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if 0 < len(value) <= _ID_MAX else None


def _our_payment_id(metadata: Any) -> str | None:
    if not isinstance(metadata, Mapping):
        return None
    value = metadata.get("payment_id")
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower()
    return candidate if _UUID_RE.fullmatch(candidate) else None


def _money(obj: Any) -> tuple[Decimal | None, str | None]:
    """``{"value": "179.00", "currency": "RUB"}`` → ``(Decimal, "RUB")``; ``ValueError`` when unreadable."""
    if obj is None:
        return None, None
    if not isinstance(obj, Mapping):
        raise TypeError("amount is not an object")
    amount = parse_amount(obj.get("value"))
    currency = obj.get("currency")
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Za-z]{3}", currency.strip()):
        raise ValueError("bad currency")
    return amount, currency.strip().upper()


def _state(data: Mapping[str, Any]) -> PaymentState | None:
    """Payment object → SDK state (``None`` for an unknown status)."""
    status = str(data.get("status") or "").strip().lower()
    if status == "pending":
        return PaymentState.CREATED
    if status == "waiting_for_capture":
        return PaymentState.PROCESSING
    if status == "canceled":
        details = data.get("cancellation_details")
        reason = details.get("reason") if isinstance(details, Mapping) else None
        return PaymentState.EXPIRED if reason in _EXPIRED_REASONS else PaymentState.CANCELED
    if status == "succeeded":
        try:
            paid, _ = _money(data.get("amount"))
            refunded, _ = _money(data.get("refunded_amount"))
        except (TypeError, ValueError):
            return PaymentState.PAID
        if paid is not None and refunded is not None and refunded > 0 and refunded >= paid:
            return PaymentState.REFUNDED
        return PaymentState.PAID
    return None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return isinstance(value, str) and value.strip().lower() in ("true", "1")


def _paid_at(data: Mapping[str, Any]) -> datetime | None:
    raw = data.get("captured_at")
    if raw in (None, ""):
        return None
    try:
        return parse_timestamp(raw)
    except ValueError:
        return None


def _clip(value: str, limit: int = DESCRIPTION_MAX) -> str:
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


class YooKassa(PaymentProvider):
    """See module docstring."""

    manifest = Manifest(
        slug="yookassa",
        title="ЮKassa",
        method_kinds=(MethodKind.SBP, MethodKind.CARD),
        currencies=("RUB",),
        config=YooKassaConfig,
        docs_url="https://yookassa.ru/developers",
        min_minor=100,  # 1 ₽ — the API minimum
    )
    capabilities = Capabilities(
        webhook_auth=WebhookAuth.IP_ONLY,
        replay_window_s=None,  # notifications carry no signed time
        fetch_status=True,
        batch_status=False,  # GET /v3/payments has no filter by ids
        refund=True,
        receipt_54fz=True,
        in_chat_invoice=False,
        redirect=True,
        webhook=True,
    )

    #: Pause before re-sending a create with the same key after ``202``/``500`` (tests set it to 0).
    retry_delay_s: float = 0.5

    def __init__(self, config: ConfigModel, ctx: PluginContext) -> None:
        super().__init__(config, ctx)
        errors: dict[str, str] = {}
        try:
            self._trusted = parse_networks(self.config.trusted_proxies)
        except ValueError:
            errors["trusted_proxies"] = _T["bad_networks"]
        try:
            self._allowed = parse_networks(self.config.allowed_networks or ", ".join(YOOKASSA_NETWORKS))
        except ValueError:
            errors["allowed_networks"] = _T["bad_networks"]
        if self.config.receipt_enabled and self._contact() is None:
            key = "receipt_email" if self.config.receipt_contact == "email" else "receipt_phone"
            errors[key] = _T["no_contact"]
        if errors:
            raise ConfigError(errors)

    # ------------------------------------------------------------------------------------------- HTTP

    @property
    def _base(self) -> str:
        return str(self.config.base_url or DEFAULT_BASE_URL).rstrip("/")

    def _headers(self, idempotence_key: str | None = None) -> dict[str, str]:
        token = base64.b64encode(f"{self.config.shop_id}:{self.config.secret_key}".encode()).decode("ascii")
        headers = {"Authorization": f"Basic {token}", "Accept": "application/json"}
        if idempotence_key is not None:
            headers["Idempotence-Key"] = idempotence_key
        return headers

    @staticmethod
    def _json(resp: HttpResponse) -> dict[str, Any]:
        try:
            data = resp.json()
        except ValueError:
            raise ProviderError(_T["bad_answer"], retryable=True, status=resp.status) from None
        if not isinstance(data, dict):
            raise ProviderError(_T["bad_answer"], retryable=True, status=resp.status)
        return data

    def _error(self, resp: HttpResponse, what: str) -> ProviderError:
        if resp.status == 401:
            return ProviderError(_T["bad_key"], retryable=False, status=401)
        if resp.status == 403:
            return ProviderError(_T["forbidden"], retryable=False, status=403)
        if resp.status in (202, 500):
            return ProviderError(_T["unknown"].format(status=resp.status), retryable=True, status=resp.status)
        if resp.status == 429 or resp.status >= 500:
            return ProviderError(
                _T["unavailable"].format(status=resp.status), retryable=True, status=resp.status
            )
        detail = f"HTTP {resp.status}"
        try:
            body = resp.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            parts = [str(body.get(k)) for k in ("code", "description", "parameter") if body.get(k)]
            if parts:
                detail = _clip(" — ".join(parts), 300)
        self.ctx.log.warning("yookassa: %s answered HTTP %s (%s)", what, resp.status, detail)
        return ProviderError(_T["rejected"].format(error=detail), retryable=False, status=resp.status)

    async def _post_idempotent(
        self, path: str, body: Mapping[str, Any], key: str, what: str
    ) -> dict[str, Any]:
        """POST with one idempotence key; ``202``, ``500`` and transport errors are re-sent with the **same**
        key (ЮKassa: «result unknown — repeat with the same key»)."""
        last: ProviderError | None = None
        delay = self.retry_delay_s
        for attempt in range(CREATE_ATTEMPTS):
            if attempt and self.retry_delay_s:
                await asyncio.sleep(min(delay, RETRY_DELAY_MAX_S))
            try:
                resp = await self.ctx.http.request(
                    "POST", f"{self._base}{path}", headers=self._headers(key), json=dict(body)
                )
            except ProviderError as exc:
                if not exc.retryable:
                    raise
                last, delay = exc, self.retry_delay_s
                continue
            if resp.status == 200:
                return self._json(resp)
            last = self._error(resp, what)
            if resp.status not in (202, 500):
                raise last
            hinted = _retry_after_s(resp)
            delay = self.retry_delay_s if hinted is None else hinted
        assert last is not None
        raise last

    # ----------------------------------------------------------------------------------------- create

    def _contact(self) -> dict[str, str] | None:
        if self.config.receipt_contact == "phone" and self.config.receipt_phone:
            return {"phone": str(self.config.receipt_phone).lstrip("+")}
        if self.config.receipt_contact == "email" and self.config.receipt_email:
            return {"email": str(self.config.receipt_email)}
        return None

    def _receipt(self, amount: Mapping[str, str], description: str) -> dict[str, Any]:
        item: dict[str, Any] = {
            "description": _clip(str(self.config.receipt_item_name or description or "Оплата")),
            "quantity": "1.00",
            "amount": dict(amount),
            "vat_code": int(self.config.vat_code),
            "payment_subject": self.config.payment_subject,
            "payment_mode": self.config.payment_mode,
        }
        receipt: dict[str, Any] = {"customer": self._contact() or {}, "items": [item]}
        if self.config.tax_system_code:
            receipt["tax_system_code"] = int(self.config.tax_system_code)
        return receipt

    def _method(self, intent: PaymentIntent) -> str | None:
        if intent.method_hint in _METHOD_BY_KIND:
            return _METHOD_BY_KIND[intent.method_hint]
        method = self.config.payment_method
        return None if method in (None, "auto") else str(method)

    async def create(self, intent: PaymentIntent) -> Checkout:
        return_url = intent.return_url or self.config.return_url
        if not return_url:
            raise ProviderError(_T["no_return_url"], retryable=False)
        description = _clip(intent.description or "Оплата")
        amount = {"value": intent.amount_text(), "currency": intent.currency}
        body: dict[str, Any] = {
            "amount": amount,
            "capture": True,
            "confirmation": {"type": "redirect", "return_url": return_url},
            "description": description,
            "metadata": {"payment_id": intent.payment_id},  # opaque: never a Telegram id
        }
        method = self._method(intent)
        if method is not None:
            body["payment_method_data"] = {"type": method}
        if self.config.receipt_enabled:
            body["receipt"] = self._receipt(amount, description)
        data = await self._post_idempotent("/payments", body, intent.payment_id, "create")
        external = _external_id(data.get("id"))
        confirmation = data.get("confirmation")
        pay_url = confirmation.get("confirmation_url") if isinstance(confirmation, Mapping) else None
        if external is None or not isinstance(pay_url, str) or not re.match(r"https?://", pay_url):
            raise ProviderError(_T["bad_answer"], retryable=False)
        expires_at: datetime | None = None
        if data.get("expires_at") not in (None, ""):
            try:
                expires_at = parse_timestamp(data["expires_at"])
            except ValueError:
                expires_at = None
        return Checkout(kind="url", external_id=external, pay_url=pay_url, expires_at=expires_at)

    # ---------------------------------------------------------------------------------------- webhook

    async def parse_webhook(self, req: WebhookRequest) -> ProviderEvent:
        if self.config.verify_ip:
            ip = client_ip(req, self._trusted)
            if ip is None or not _within(ip, self._allowed):
                self.ctx.log.warning("yookassa: notification from a foreign address %s rejected", ip)
                raise WebhookRejected("source address is not a ЮKassa network", status=403)
        data = req.json()
        if not isinstance(data, dict) or data.get("type") != "notification":
            raise WebhookRejected("malformed", status=400)
        event = data.get("event")
        obj = data.get("object")
        if not isinstance(event, str) or not isinstance(obj, dict):
            raise WebhookRejected("malformed", status=400)
        if event == "refund.succeeded":
            external = _external_id(obj.get("payment_id"))
            if external is None:
                raise WebhookRejected("no payment id", status=400)
            return ProviderEvent(
                state=PaymentState.REFUNDED,
                external_id=external,
                is_test=_truthy(obj.get("test")),
                summary={"event": event, "id": external, "refund_id": _external_id(obj.get("id"))},
            )
        if not event.startswith("payment."):
            raise WebhookIgnored(f"event {event[:40]}")
        external = _external_id(obj.get("id"))
        if external is None:
            raise WebhookRejected("no payment id", status=400)
        state = _state(obj)
        if state is None:
            self.ctx.log.warning("yookassa: notification with unknown status ignored")
            raise WebhookIgnored("unknown status")
        try:
            amount, currency = _money(obj.get("amount"))
        except (TypeError, ValueError):
            raise WebhookRejected("bad amount", status=400) from None
        is_test = _truthy(obj.get("test"))
        return ProviderEvent(
            state=state,
            external_id=external,
            payment_id=_our_payment_id(obj.get("metadata")),
            amount=amount,
            currency=currency,
            paid_at=_paid_at(obj) if state is PaymentState.PAID else None,
            is_test=is_test,
            summary={
                "event": event,
                "id": external,
                "status": str(obj.get("status"))[:40],
                "amount": None if amount is None else str(amount),
                "currency": currency,
                "test": is_test,
            },
        )

    # ----------------------------------------------------------------------------------------- status

    async def fetch_status(self, ids: Sequence[str]) -> list[ProviderStatus]:
        result: list[ProviderStatus] = []
        for external in dict.fromkeys(ids):
            if not external:
                continue
            resp = await self.ctx.http.request(
                "GET", f"{self._base}/payments/{quote(external, safe='')}", headers=self._headers()
            )
            if resp.status in (400, 404):
                continue  # not a payment of this shop
            if resp.status != 200:
                raise self._error(resp, "status")
            data = self._json(resp)
            if str(data.get("status")) == "waiting_for_capture" and self.config.auto_capture:
                data = await self._capture(external, data)
            status = self._status(data, external)
            if status is not None:
                result.append(status)
        return result

    async def _capture(self, external: str, data: dict[str, Any]) -> dict[str, Any]:
        """Capture a held payment in full (deterministic key: repeated checks never double-capture)."""
        try:
            captured = await self._post_idempotent(
                f"/payments/{quote(external, safe='')}/capture",
                {},
                f"svbg-capture-{external}"[:64],
                "capture",
            )
        except ProviderError as exc:
            self.ctx.log.warning("yookassa: capture of %s failed: %s", external[:40], exc.human)
            return data
        return captured if captured.get("id") == data.get("id") else data

    def _status(self, data: Mapping[str, Any], external: str) -> ProviderStatus | None:
        state = _state(data)
        if state is None:
            return None
        try:
            amount, currency = _money(data.get("amount"))
        except (TypeError, ValueError):
            self.ctx.log.warning("yookassa: unreadable amount of %s", external[:40])
            amount, currency = None, None
        return ProviderStatus(
            state=state,
            external_id=_external_id(data.get("id")) or external,
            payment_id=_our_payment_id(data.get("metadata")),
            amount=amount,
            currency=currency,
            paid_at=_paid_at(data) if state is PaymentState.PAID else None,
            is_test=_truthy(data.get("test")),
        )

    # ----------------------------------------------------------------------------------------- refund

    async def refund(self, external_id: str, amount_minor: int, currency: str) -> RefundResult:
        if currency != "RUB":
            return RefundResult(False, message=f"ЮKassa: возврат в {currency} не поддерживается")
        amount = {"value": f"{Decimal(amount_minor) / 100:.2f}", "currency": currency}
        body: dict[str, Any] = {"payment_id": external_id, "amount": amount}
        if self.config.receipt_enabled:
            body["receipt"] = self._receipt(amount, "Возврат")
        try:
            data = await self._post_idempotent(
                "/refunds", body, f"svbg-refund-{external_id}-{amount_minor}"[:64], "refund"
            )
        except ProviderError as exc:
            return RefundResult(False, message=exc.human)
        status = str(data.get("status") or "")
        ok = status in ("succeeded", "pending")
        return RefundResult(ok, _external_id(data.get("id")), f"ЮKassa: возврат {status or 'неизвестен'}")

    # ------------------------------------------------------------------------------------------ probe

    async def test_credentials(self) -> Probe:
        """``GET /v3/me`` — reads the shop settings, no side effects."""
        resp = await self.ctx.http.request("GET", f"{self._base}/me", headers=self._headers())
        if resp.status in (401, 403):
            return Probe(False, _T["bad_key"])
        if resp.status != 200:
            return Probe(False, self._error(resp, "probe").human)
        try:
            me = self._json(resp)
        except ProviderError as exc:
            return Probe(False, exc.human)
        shop_test = _truthy(me.get("test"))
        message = _T["probe_ok"].format(
            account=str(me.get("account_id") or self.config.shop_id)[:40],
            mode=" (тестовый)" if shop_test else "",
        )
        if shop_test != self.ctx.is_test:
            message += _T["probe_test_mismatch"].format(
                shop="тестовый" if shop_test else "боевой", inst="тестовом" if self.ctx.is_test else "боевом"
            )
        fiscal = me.get("fiscalization")
        fiscal_on = (
            _truthy(fiscal.get("enabled"))
            if isinstance(fiscal, Mapping)
            else _truthy(me.get("fiscalization_enabled"))
        )
        if self.config.receipt_enabled and not fiscal_on:
            message += _T["probe_no_fiscal"]
        return Probe(
            True,
            message,
            {
                "test": shop_test,
                "fiscalization": fiscal_on,
                "payment_methods": me.get("payment_methods") or [],
            },
        )


def _retry_after_s(resp: HttpResponse) -> float | None:
    """``retry_after`` (milliseconds) from a ``202`` body, in seconds."""
    try:
        body = resp.json()
    except ValueError:
        return None
    value = body.get("retry_after") if isinstance(body, dict) else None
    if isinstance(value, int | float) and not isinstance(value, bool) and value >= 0:
        return float(value) / 1000.0
    return None
