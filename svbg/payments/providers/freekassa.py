"""Freekassa — cards, SBP, crypto and wallets via the Freekassa payment form or API, wave B (07 §4.2, §4.4).

Specification: ``docs/providers/freekassa.md`` (official documentation <https://docs.freekassa.net/>, checked
on 2026-10-02). Ported from Remnashop (MIT, © 2024 snoups)
``src/infrastructure/payment_gateways/freekassa.py``, reworked for the SDK — see ``THIRD_PARTY_NOTICES.md``.
Imports only :mod:`svbg.sdk`.

* **Checkout.** ``form`` (default, no request): a link to the SCI form ``https://pay.fk.money/?m&oa&currency&o&s``
  with ``s = md5("m:oa:secret_word:currency:o")``. ``api``: ``POST /orders/create`` (needs ``i``, ``email``,
  ``ip`` of the payer — taken from the settings, never from the user). The order number ``o`` /
  ``paymentId`` is **only** the opaque payment id; it is also our ``external_id`` (Freekassa's own number is
  unknown in form mode, and ``/orders`` filters by ``paymentId``).
* **Notification** (form-data, ``application/x-www-form-urlencoded`` or ``multipart/form-data``):
  ``SIGN = md5("MERCHANT_ID:AMOUNT:secret_word_2:MERCHANT_ORDER_ID")`` compared in constant time over the
  values exactly as received; ``MERCHANT_ID`` must be our shop; the sender must be in Freekassa's published IP
  list (behind a local reverse proxy the right-most ``X-Forwarded-For`` entry). Freekassa notifies only about
  a **successful** payment, without time and without currency; the answer is ``YES``.
* **Verification.** The notification does not carry the currency, so the plugin passes ``currency=None``
  unless ``confirm_via_api`` is off and our own ``us_cur`` echo is present: the core then re-reads the order
  with ``fetch_status`` (``POST /orders`` by ``paymentId``) before crediting (07 §4.2: MD5 → re-read).
  Remnashop credited straight from the notification.
* API requests: JSON with ``shopId``, a strictly increasing ``nonce`` and
  ``signature = hex(HMAC_SHA256(api_key, "|".join(values sorted by key)))``.
* KassaAI: no public documentation was found (2026-10-02) — see ``docs/providers/freekassa.md`` §7; the form,
  API addresses and IP list are settings, so an instance ``PAY_KASSAAI_PROVIDER=freekassa`` can be configured
  once the owner confirms the protocol from the KassaAI cabinet.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import re
import time
from collections.abc import Mapping, Sequence
from decimal import Decimal
from email.parser import BytesParser
from email.policy import HTTP
from typing import Any, Final
from urllib.parse import parse_qsl, urlencode

from svbg.sdk import (
    Capabilities,
    Checkout,
    ConfigModel,
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
    WebhookResponse,
    choice,
    constant_time_equal,
    flag,
    integer,
    parse_amount,
    secret,
    text,
    url,
)

__all__ = [
    "CURRENCIES",
    "DEFAULT_API_URL",
    "DEFAULT_FORM_URL",
    "FREEKASSA_IPS",
    "STATUS_MAP",
    "Freekassa",
    "FreekassaConfig",
    "api_signature",
    "client_ip",
    "form_signature",
    "notification_signature",
]

DEFAULT_FORM_URL: Final = "https://pay.fk.money/"
DEFAULT_API_URL: Final = "https://api.fk.life/v1"
#: Notification senders published by Freekassa (docs §1.4 «Проверка IP»).
FREEKASSA_IPS: Final = ("168.119.157.136", "168.119.60.227", "178.154.197.79", "51.250.54.238")
CURRENCIES: Final = ("RUB", "USD", "EUR", "UAH", "KZT")
#: ``/orders`` status → SDK state (docs §2.3).
STATUS_MAP: Final[Mapping[int, PaymentState]] = {
    0: PaymentState.CREATED,
    1: PaymentState.PAID,
    6: PaymentState.REFUNDED,
    8: PaymentState.FAILED,
    9: PaymentState.CANCELED,
}
#: When one order number has several Freekassa orders, the most final one wins.
_STATUS_RANK: Final = {1: 5, 6: 6, 8: 2, 9: 3, 0: 1}
_ID_MAX: Final = 200
_UUID_RE: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_MAX_FIELDS: Final = 64

_T: Final = {
    "bad_key": "Freekassa отклонила API-ключ или ID магазина — проверьте их в кабинете Freekassa",
    "rejected": "Freekassa отклонила запрос (HTTP {status}){detail}",
    "unavailable": "Freekassa временно недоступна (HTTP {status})",
    "bad_answer": "Freekassa вернула непонятный ответ",
    "currency": "Freekassa принимает только {cur}",
    "api_fields": "для режима «API» заполните «ID платёжной системы», «Email плательщика» и «IP плательщика»",
    "probe_ok": "API-ключ принят, Freekassa доступна{form}",
    "probe_form": "; ссылки ведут на форму {url}",
    "probe_not_fk": "по адресу API отвечает не Freekassa — проверьте «Адрес API»",
}


class FreekassaConfig(ConfigModel):
    shop_id = text(
        "ID магазина",
        "Номер магазина в Freekassa (параметр m формы и MERCHANT_ID уведомлений).",
        where="кабинет Freekassa → «Настройки» магазина → ID магазина (merchant.freekassa.net/settings)",
        pattern=r"\d{1,12}",
    )
    secret_word = secret(
        "Секретное слово 1",
        "Им подписывается ссылка на форму оплаты.",
        where="кабинет Freekassa → «Настройки» → «Секретное слово»",
    )
    secret_word_2 = secret(
        "Секретное слово 2",
        "Им Freekassa подписывает уведомления об оплате (поле SIGN).",
        where="кабинет Freekassa → «Настройки» → «Секретное слово 2»",
    )
    api_key = secret(
        "API-ключ",
        "Нужен, чтобы перепроверять оплату через API Freekassa и для режима «API».",
        where="кабинет Freekassa → «Настройки» → «API ключ»",
    )
    checkout_mode = choice(
        "Как создавать оплату",
        ("form", "api"),
        "form — ссылка на форму Freekassa, покупатель сам выбирает способ (без запросов к API); "
        "api — заказ через POST /orders/create с выбранной платёжной системой.",
        default="form",
        required=False,
        advanced=True,
    )
    payment_system = integer(
        "ID платёжной системы",
        "Параметр i: в режиме «form» — предлагаемый способ (покупатель может сменить), в режиме «api» — "
        "обязателен. Например: 42 — СБП, 44 — СБП (API), 36 — карта RUB (API), 12 — МИР, 15 — USDT TRC20.",
        where="документация Freekassa → «Список доступных валют» или запрос /currencies",
        required=False,
        min=1,
        max=100_000,
        advanced=True,
    )
    payer_email = text(
        "Email плательщика (режим API)",
        "Freekassa требует email покупателя в /orders/create. Бот его не знает, поэтому подставляется этот "
        "адрес — например, почта поддержки магазина.",
        where="ваша почта для чеков (только для режима «api»)",
        required=False,
        advanced=True,
        pattern=r"[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,24}",
    )
    payer_ip = text(
        "IP плательщика (режим API)",
        "Freekassa требует IP покупателя в /orders/create. Бот его не знает — укажите IP сервера бота.",
        where="внешний IP вашего сервера (только для режима «api»)",
        required=False,
        advanced=True,
        pattern=r"[0-9A-Fa-f:.]{3,45}",
    )
    confirm_via_api = flag(
        "Перепроверять оплату через API",
        "Да (рекомендуется): уведомление Freekassa (MD5) только запускает проверку заказа через API, деньги "
        "зачисляются по ответу API. Нет: зачисление прямо по подписанному уведомлению.",
        default=True,
        advanced=True,
    )
    allowed_ips = text(
        "IP-адреса Freekassa",
        "С каких адресов принимаются уведомления, через запятую (можно подсети). off — проверка по IP "
        "выключена (например, за Cloudflare: адрес отправителя там не виден).",
        where="документация Freekassa → «Оповещение о платеже» → «Проверка IP»",
        default=", ".join(FREEKASSA_IPS),
        required=False,
        advanced=True,
    )
    form_url = url(
        "Адрес формы оплаты",
        "Адрес платёжной формы (SCI). Меняйте, только если касса сообщила другой.",
        default=DEFAULT_FORM_URL,
        required=False,
        advanced=True,
    )
    api_url = url(
        "Адрес API",
        "Базовый адрес API. Меняйте, только если касса сообщила другой.",
        default=DEFAULT_API_URL,
        required=False,
        advanced=True,
    )


# ------------------------------------------------------------------------------------------- signatures


def form_signature(shop_id: str, amount: str, secret_word: str, currency: str, order_id: str) -> str:
    """``md5("m:oa:secret_word:currency:o")`` (docs §1.5)."""
    raw = f"{shop_id}:{amount}:{secret_word}:{currency}:{order_id}"
    return hashlib.md5(raw.encode("utf-8"), usedforsecurity=True).hexdigest()  # noqa: S324 - protocol


def notification_signature(shop_id: str, amount: str, secret_word_2: str, order_id: str) -> str:
    """``md5("MERCHANT_ID:AMOUNT:secret_word_2:MERCHANT_ORDER_ID")`` (docs §1.7)."""
    raw = f"{shop_id}:{amount}:{secret_word_2}:{order_id}"
    return hashlib.md5(raw.encode("utf-8"), usedforsecurity=True).hexdigest()  # noqa: S324 - protocol


def api_signature(api_key: str, data: Mapping[str, Any]) -> str:
    """``hex(HMAC_SHA256(api_key, "|".join(str(v) for k, v in sorted(data))))`` (docs §2.2)."""
    message = "|".join(str(data[k]) for k in sorted(data))
    return hmac.new(api_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


# ----------------------------------------------------------------------------------------------- helpers


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
    """The sender's address. The TCP peer when it is public; behind a local reverse proxy (loopback or private
    peer) the right-most ``X-Forwarded-For`` entry — the one our proxy added — or ``X-Real-IP``.
    Client-supplied entries further left are never used."""
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


_IP_CHECK_OFF: Final = frozenset({"off", "-", "*", "нет", "выкл"})


def _networks(raw: str | None) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Allowed sender networks; ``off`` → no check. Unparsable entries are skipped."""
    if raw is None or raw.strip().lower() in _IP_CHECK_OFF:
        return []
    nets = []
    for part in re.split(r"[,\s;]+", raw or ""):
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            continue
    return nets


def _form_fields(req: WebhookRequest) -> dict[str, str]:
    """Notification fields from the body (urlencoded or multipart) or, for a GET-style call, the query.
    A repeated field is ambiguous → ``400``."""
    ctype = (req.header("Content-Type") or "").strip()
    pairs: list[tuple[str, str]] = []
    if ctype.lower().startswith("multipart/form-data"):
        message = BytesParser(policy=HTTP).parsebytes(
            b"Content-Type: " + ctype.encode("latin-1", "replace") + b"\r\n\r\n" + req.body
        )
        if not message.is_multipart():
            raise WebhookRejected("malformed", status=400)
        for part in message.iter_parts():  # type: ignore[attr-defined]
            name = part.get_param("name", header="content-disposition")
            if not name or part.get_filename():
                continue
            payload = part.get_payload(decode=True) or b""
            pairs.append((str(name), payload.decode("utf-8", errors="strict")))
    elif req.body.strip():
        try:
            text_body = req.body.decode("utf-8")
        except UnicodeDecodeError:
            raise WebhookRejected("malformed", status=400) from None
        try:
            pairs = parse_qsl(
                text_body, keep_blank_values=True, strict_parsing=True, max_num_fields=_MAX_FIELDS
            )
        except ValueError:
            raise WebhookRejected("malformed", status=400) from None
    else:
        pairs = list(req.query.items())
    fields: dict[str, str] = {}
    for key, value in pairs:
        if key in fields:
            raise WebhookRejected(f"repeated field {key[:20]}", status=400)
        fields[key] = value
    return fields


def _decimal_json(body: bytes) -> Any:
    return json.loads(body.decode("utf-8"), parse_float=Decimal)


def _our_payment_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower()
    return candidate if _UUID_RE.fullmatch(candidate) else None


def _status_code(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


class Freekassa(PaymentProvider):
    """See module docstring."""

    manifest = Manifest(
        slug="freekassa",
        title="Freekassa",
        method_kinds=(MethodKind.CARD, MethodKind.SBP, MethodKind.CRYPTO),
        currencies=CURRENCIES,
        config=FreekassaConfig,
        docs_url="https://docs.freekassa.net/",
    )
    capabilities = Capabilities(
        webhook_auth=WebhookAuth.SIGNATURE,
        replay_window_s=None,
        fetch_status=True,
        batch_status=False,
        refund=True,
        in_chat_invoice=False,
        redirect=True,
        webhook=True,
    )

    def __init__(self, config: ConfigModel, ctx: PluginContext) -> None:
        super().__init__(config, ctx)
        self._last_nonce = 0
        # A configured list that parses to nothing still means «check»: nothing is accepted then.
        raw = str(self.config.allowed_ips or "").strip().lower()
        self._ip_check_on = raw not in _IP_CHECK_OFF

    # ------------------------------------------------------------------------------------------- HTTP

    @property
    def _api(self) -> str:
        return str(self.config.api_url or DEFAULT_API_URL).rstrip("/")

    def _nonce(self) -> int:
        """Strictly increasing per process (docs: «должен всегда быть больше предыдущего значения»)."""
        self._last_nonce = max(time.time_ns(), self._last_nonce + 1)
        return self._last_nonce

    def _signed(self, **fields: Any) -> dict[str, Any]:
        data: dict[str, Any] = {"shopId": int(self.config.shop_id), "nonce": self._nonce()}
        data.update({k: v for k, v in fields.items() if v is not None})
        data["signature"] = api_signature(self.config.api_key, data)
        return data

    async def _call(self, path: str, what: str, **fields: Any) -> dict[str, Any]:
        resp = await self.ctx.http.request(
            "POST", f"{self._api}{path}", json=self._signed(**fields), headers={"Accept": "application/json"}
        )
        if resp.status in (401, 403):
            raise ProviderError(_T["bad_key"], retryable=False, status=resp.status)
        if resp.status in (408, 429) or resp.status >= 500:
            raise ProviderError(
                _T["unavailable"].format(status=resp.status), retryable=True, status=resp.status
            )
        try:
            data = _decimal_json(resp.body)
        except (UnicodeDecodeError, ValueError):
            raise ProviderError(_T["bad_answer"], retryable=True, status=resp.status) from None
        if not isinstance(data, dict):
            raise ProviderError(_T["bad_answer"], retryable=True, status=resp.status)
        if resp.status != 200 or data.get("type") != "success":
            self.ctx.log.warning("freekassa: %s answered HTTP %s", what, resp.status)
            raise ProviderError(
                _T["rejected"].format(status=resp.status, detail=_detail(data)),
                retryable=False,
                status=resp.status,
            )
        return data

    # ----------------------------------------------------------------------------------------- create

    async def create(self, intent: PaymentIntent) -> Checkout:
        if intent.currency not in CURRENCIES:
            raise ProviderError(_T["currency"].format(cur=", ".join(CURRENCIES)), retryable=False)
        if self.config.checkout_mode == "api":
            return await self._create_api(intent)
        return self._create_form(intent)

    def _create_form(self, intent: PaymentIntent) -> Checkout:
        shop, amount, cur, order = (
            str(self.config.shop_id),
            intent.amount_text(),
            intent.currency,
            intent.payment_id,
        )
        params: dict[str, str] = {
            "m": shop,
            "oa": amount,
            "currency": cur,
            "o": order,
            "s": form_signature(shop, amount, self.config.secret_word, cur, order),
            "lang": "ru",
            "us_cur": cur,  # echoed back in the notification (which has no currency of its own)
        }
        if self.config.payment_system:
            params["i"] = str(int(self.config.payment_system))
        base = str(self.config.form_url or DEFAULT_FORM_URL)
        sep = "&" if "?" in base else "?"
        return Checkout(kind="url", external_id=order, pay_url=f"{base}{sep}{urlencode(params)}")

    async def _create_api(self, intent: PaymentIntent) -> Checkout:
        if not (self.config.payment_system and self.config.payer_email and self.config.payer_ip):
            raise ProviderError(_T["api_fields"], retryable=False)
        data = await self._call(
            "/orders/create",
            "create",
            paymentId=intent.payment_id,
            i=int(self.config.payment_system),
            email=self.config.payer_email,
            ip=self.config.payer_ip,
            amount=intent.amount_text(),
            currency=intent.currency,
        )
        location = data.get("location")
        if not isinstance(location, str) or not re.match(r"https?://", location.strip()):
            raise ProviderError(_T["bad_answer"], retryable=False)
        return Checkout(kind="url", external_id=intent.payment_id, pay_url=location.strip())

    # ---------------------------------------------------------------------------------------- webhook

    async def parse_webhook(self, req: WebhookRequest) -> ProviderEvent:
        nets = _networks(self.config.allowed_ips)
        if nets or self._ip_check_on:
            sender = client_ip(req)
            if sender is None or not any(sender in net for net in nets):
                raise WebhookRejected("sender not in the Freekassa IP list", status=403)
        fields = _form_fields(req)
        if not fields:
            raise WebhookIgnored("empty notification")
        sign = fields.get("SIGN", "").strip().lower()
        merchant = fields.get("MERCHANT_ID", "").strip()
        amount_raw = fields.get("AMOUNT", "")
        order = fields.get("MERCHANT_ORDER_ID", "")
        if not sign:
            raise WebhookRejected("missing signature", status=401)
        expected = notification_signature(merchant, amount_raw, self.config.secret_word_2, order)
        if not constant_time_equal(expected, sign):
            raise WebhookRejected("bad signature", status=401)
        if merchant != str(self.config.shop_id):
            raise WebhookRejected("another shop", status=401)
        order = order.strip()
        if not order or len(order) > _ID_MAX:
            raise WebhookRejected("no order id", status=400)
        try:
            amount = parse_amount(amount_raw)
        except (TypeError, ValueError):
            raise WebhookRejected("bad amount", status=400) from None
        currency: str | None = None
        echoed = fields.get("us_cur", "").strip().upper()
        if not self.config.confirm_via_api and echoed in CURRENCIES:
            currency = echoed
        return ProviderEvent(
            state=PaymentState.PAID,
            external_id=order,
            payment_id=_our_payment_id(order),
            amount=amount,
            currency=currency,
            signed_at=None,
            summary={
                "intid": fields.get("intid", "")[:40] or None,
                "order": order,
                "amount": str(amount),
                "cur_id": fields.get("CUR_ID", "")[:10] or None,
                "us_cur": echoed[:8] or None,
                "commission": fields.get("commission", "")[:20] or None,
            },
        )

    def ack(self, event: ProviderEvent | None) -> WebhookResponse:
        return WebhookResponse.ok("YES")

    # ----------------------------------------------------------------------------------------- status

    async def fetch_status(self, ids: Sequence[str]) -> list[ProviderStatus]:
        result: list[ProviderStatus] = []
        for order in dict.fromkeys(ids):
            if not order:
                continue
            data = await self._call("/orders", "status", paymentId=order)
            best: Mapping[str, Any] | None = None
            for item in data.get("orders") or []:
                if not isinstance(item, Mapping) or str(item.get("merchant_order_id", "")).strip() != order:
                    continue
                code = _status_code(item.get("status"))
                if code not in STATUS_MAP:
                    continue
                if best is None or _STATUS_RANK[code] > _STATUS_RANK[_status_code(best.get("status")) or 0]:
                    best = item
            if best is None:
                continue  # unknown to Freekassa (the user never opened the form): absent from the result
            try:
                amount = parse_amount(best.get("amount"))
            except (TypeError, ValueError):
                self.ctx.log.warning("freekassa: unreadable amount of %s skipped", order[:40])
                continue
            currency = best.get("currency")
            result.append(
                ProviderStatus(
                    state=STATUS_MAP[_status_code(best.get("status")) or 0],
                    external_id=order,
                    payment_id=_our_payment_id(order),
                    amount=amount,
                    currency=currency.strip().upper()
                    if isinstance(currency, str) and re.fullmatch(r"[A-Za-z]{3,8}", currency.strip())
                    else None,
                )
            )
        return result

    # ----------------------------------------------------------------------------------------- refund

    async def refund(self, external_id: str, amount_minor: int, currency: str) -> RefundResult:
        """``POST /orders/refund`` by our order number (``orderAmount`` in major units; every Freekassa
        currency has two decimals)."""
        amount = f"{Decimal(amount_minor).scaleb(-2):.2f}"
        try:
            data = await self._call("/orders/refund", "refund", paymentId=external_id, orderAmount=amount)
        except ProviderError as exc:
            return RefundResult(False, None, exc.human)
        ref = data.get("id")
        return RefundResult(True, None if ref is None else str(ref)[:_ID_MAX], "")

    # ------------------------------------------------------------------------------------------ probe

    async def test_credentials(self) -> Probe:
        """No side effects: ``POST /balance``. The form link needs no request (only secret word 1)."""
        try:
            data = await self._call("/balance", "balance")
        except ProviderError as exc:
            if exc.status in (404, 405) or exc.human == _T["bad_answer"]:
                return Probe(False, _T["probe_not_fk"])
            return Probe(False, exc.human)
        form = ""
        if self.config.checkout_mode == "form":
            form = _T["probe_form"].format(url=str(self.config.form_url or DEFAULT_FORM_URL))
        elif not (self.config.payment_system and self.config.payer_email and self.config.payer_ip):
            return Probe(False, _T["api_fields"])
        balance = data.get("balance")
        wallets = len(balance) if isinstance(balance, list) else None
        return Probe(True, _T["probe_ok"].format(form=form), {"wallets": wallets})


def _detail(data: Mapping[str, Any]) -> str:
    for key in ("message", "error", "msg"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return ": " + value.strip()[:120]
    return ""
