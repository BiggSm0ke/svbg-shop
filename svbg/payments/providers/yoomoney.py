"""ЮMoney p2p quickpay — a personal ЮMoney wallet as a cash desk, wave B (07 §4.2).

Specification: ``docs/providers/yoomoney.md`` (official pages yoomoney.ru/docs/payment-buttons/…, checked
2026-10-02). Ported from Remnashop (MIT, © 2024 snoups) ``src/infrastructure/payment_gateways/yoomoney.py``
and rewritten for the SvBG SDK — see ``THIRD_PARTY_NOTICES.md``. Imports only :mod:`svbg.sdk`.

* ``create`` POSTs the quickpay form (``receiver``, ``quickpay-form=button``, ``paymentType``, ``sum``,
  ``label`` = our opaque payment id, ``successURL``) to ``/quickpay/confirm`` — the form accepts POST only —
  and hands the user the ``Location`` of the redirect (the ЮMoney payment page).
* HTTP notifications (``application/x-www-form-urlencoded``) are authenticated by ``sign`` =
  ``hex(HMAC_SHA256(secret, "k1=v1&k2=v2…"))`` over every parameter except ``sign``, sorted by name, values
  percent-encoded per RFC 3986. The legacy ``sha1_hash`` (retired by ЮMoney on 2026-05-18) is accepted only
  when the owner explicitly enables it.
* Amount reconciliation is strict: the event amount is ``withdraw_amount`` (what the payer was charged — the
  form's ``sum``); ``amount`` (credited after the fee) must not exceed it. ``label`` must be our UUID —
  transfers without it (a friend sending money to the wallet) are acknowledged and ignored.
* Without an OAuth token there is **no status API**: ``fetch_status`` is not offered (``Capabilities``); a
  notification lost after ЮMoney's three attempts is not recovered automatically (see the docs).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from decimal import Decimal
from typing import Final
from urllib.parse import parse_qsl, quote, urljoin

from svbg.sdk import (
    Capabilities,
    Checkout,
    ConfigModel,
    Manifest,
    MethodKind,
    PaymentIntent,
    PaymentProvider,
    PaymentState,
    Probe,
    ProviderError,
    ProviderEvent,
    WebhookAuth,
    WebhookIgnored,
    WebhookRejected,
    WebhookRequest,
    WebhookResponse,
    choice,
    constant_time_equal,
    flag,
    hmac_sha256_hex,
    parse_amount,
    parse_timestamp,
    secret,
    text,
    url,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "INCOMING_TYPES",
    "YooMoney",
    "YooMoneyConfig",
    "legacy_sha1",
    "sign",
    "signing_string",
]

DEFAULT_BASE_URL: Final = "https://yoomoney.ru"
FORM_PATH: Final = "/quickpay/confirm"
#: Notification types of incoming transfers (official list).
INCOMING_TYPES: Final = frozenset({"p2p-incoming", "card-incoming"})
#: ISO 4217 numeric codes ЮMoney uses in ``currency`` (always 643 for wallets).
_CURRENCIES: Final[Mapping[str, str]] = {"643": "RUB"}
_LEGACY_FIELDS: Final = (
    "notification_type",
    "operation_id",
    "amount",
    "currency",
    "datetime",
    "sender",
    "codepro",
)
_LABEL_MAX: Final = 64
_ID_MAX: Final = 200
_MAX_FIELDS: Final = 64
_UUID_RE: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_TRUE: Final = frozenset({"true", "1"})
_SUMMARY_FIELDS: Final = (
    "notification_type",
    "operation_id",
    "amount",
    "withdraw_amount",
    "currency",
    "datetime",
    "codepro",
    "unaccepted",
    "test_notification",
)

_T: Final = {
    "rejected": "ЮMoney отклонил форму оплаты (HTTP {status}) — проверьте номер кошелька",
    "unavailable": "ЮMoney временно недоступен (HTTP {status})",
    "no_link": "ЮMoney не выдал ссылку на оплату — проверьте номер кошелька",
    "currency": "ЮMoney принимает только рубли",
    "probe_ok": (
        "Настройки заполнены. У ЮMoney нет API для проверки без OAuth-токена: нажмите «Протестировать» "
        "на странице HTTP-уведомлений ЮMoney — бот должен ответить 200."
    ),
    "probe_no_url": "У бота нет публичного адреса — ЮMoney не сможет присылать уведомления об оплате",
}


class YooMoneyConfig(ConfigModel):
    wallet = text(
        "Номер кошелька ЮMoney",
        "Кошелёк-получатель (receiver формы quickpay). Деньги приходят на него напрямую.",
        where="yoomoney.ru → «Кошелёк» → номер счёта под балансом (начинается с 4100)",
        pattern=r"4100\d{7,16}",
    )
    notification_secret = secret(
        "Секрет HTTP-уведомлений",
        "Им ЮMoney подписывает уведомления о переводах (параметр sign, HMAC-SHA256). На той же странице "
        "укажите адрес уведомлений бота и включите галочку «Отправлять HTTP-уведомления».",
        where="yoomoney.ru/transfer/myservices/http-notification → «Показать секрет»",
    )
    payment_type = choice(
        "Способ оплаты по умолчанию",
        ("AC", "PC"),
        "AC — банковская карта, PC — кошелёк ЮMoney. Кнопка «кошелёк ЮMoney» в боте всегда открывает PC.",
        default="AC",
        required=False,
        advanced=True,
    )
    allow_legacy_sha1 = flag(
        "Принимать устаревшую подпись sha1_hash",
        "ЮMoney перестал присылать sha1_hash 18.05.2026. Включайте, только если ваши уведомления всё ещё "
        "приходят без параметра sign (SHA-1 слабее HMAC-SHA256).",
        default=False,
        advanced=True,
    )
    base_url = url(
        "Адрес ЮMoney",
        "Базовый адрес формы quickpay. Не меняйте без необходимости.",
        default=DEFAULT_BASE_URL,
        required=False,
        advanced=True,
    )


def signing_string(params: Mapping[str, str]) -> str:
    """``k1=v1&k2=v2…``: every parameter except ``sign``, sorted by name, values percent-encoded (RFC 3986,
    UTF-8), empty values kept as ``k=``."""
    return "&".join(f"{k}={quote(params[k], safe='')}" for k in sorted(params) if k != "sign")


def sign(secret_key: str, params: Mapping[str, str]) -> str:
    """``hex(HMAC_SHA256(secret, signing_string(params)))`` in lower case."""
    return hmac_sha256_hex(secret_key, signing_string(params).encode("utf-8"))


def legacy_sha1(secret_key: str, params: Mapping[str, str]) -> str:
    """Retired ``sha1_hash``: SHA-1 of ``notification_type&operation_id&amount&currency&datetime&sender&
    codepro&notification_secret&label``."""
    parts = [params.get(k, "") for k in _LEGACY_FIELDS] + [secret_key, params.get("label", "")]
    return hashlib.sha1("&".join(parts).encode("utf-8")).hexdigest()  # noqa: S324 - protocol-mandated


def _form(req: WebhookRequest) -> dict[str, str]:
    try:
        text_body = req.body.decode("utf-8")
        pairs = parse_qsl(
            text_body, keep_blank_values=True, strict_parsing=bool(text_body), max_num_fields=_MAX_FIELDS
        )
    except (UnicodeDecodeError, ValueError):
        raise WebhookRejected("malformed form", status=400) from None
    form: dict[str, str] = {}
    for key, value in pairs:
        if key in form:
            raise WebhookRejected(f"duplicate parameter {key[:40]}", status=400)
        form[key] = value
    return form


def _flag(value: str | None) -> bool:
    return value is not None and value.strip().lower() in _TRUE


def _amount(form: Mapping[str, str], key: str) -> Decimal | None:
    raw = form.get(key)
    if raw is None or raw.strip() == "":
        return None
    try:
        return parse_amount(raw)
    except (TypeError, ValueError):
        raise WebhookRejected(f"bad {key}", status=400) from None


class YooMoney(PaymentProvider):
    """See module docstring."""

    manifest = Manifest(
        slug="yoomoney",
        title="ЮMoney (кошелёк)",
        method_kinds=(MethodKind.CARD, MethodKind.WALLET),
        currencies=("RUB",),
        config=YooMoneyConfig,
        docs_url="https://yoomoney.ru/docs/payment-buttons/using-api/forms",
    )
    capabilities = Capabilities(
        webhook_auth=WebhookAuth.SIGNATURE,
        replay_window_s=None,  # retries come +10 and +60 min later with the same operation time
        fetch_status=False,  # the operation-history API needs an OAuth token
        redirect=True,
    )

    # ----------------------------------------------------------------------------------------- create

    def _payment_type(self, intent: PaymentIntent) -> str:
        if intent.method_hint is MethodKind.WALLET:
            return "PC"
        if intent.method_hint is MethodKind.CARD:
            return "AC"
        return str(self.config.payment_type or "AC")

    def form(self, intent: PaymentIntent) -> dict[str, str]:
        """The quickpay form; ``label`` carries only the opaque payment id."""
        if intent.currency != "RUB":
            raise ProviderError(_T["currency"])
        data = {
            "receiver": str(self.config.wallet),
            "quickpay-form": "button",
            "paymentType": self._payment_type(intent),
            "sum": intent.amount_text(),
            "label": intent.payment_id[:_LABEL_MAX],
        }
        if intent.return_url:
            data["successURL"] = intent.return_url
        return data

    async def create(self, intent: PaymentIntent) -> Checkout:
        base = str(self.config.base_url or DEFAULT_BASE_URL).rstrip("/")
        resp = await self.ctx.http.request("POST", base + FORM_PATH, data=self.form(intent))
        if resp.status == 429 or resp.status >= 500:
            raise ProviderError(
                _T["unavailable"].format(status=resp.status), retryable=True, status=resp.status
            )
        if resp.status >= 400:
            raise ProviderError(_T["rejected"].format(status=resp.status), status=resp.status)
        location = next((v for k, v in resp.headers.items() if k.lower() == "location"), None)
        if not (300 <= resp.status < 400 and location):
            raise ProviderError(_T["no_link"], status=resp.status)
        pay_url = urljoin(base + "/", location.strip())
        if not re.match(r"https?://", pay_url) or "error" in pay_url.lower().split("?", 1)[0]:
            raise ProviderError(_T["no_link"], status=resp.status)
        return Checkout(kind="url", pay_url=pay_url)

    # ---------------------------------------------------------------------------------------- webhook

    def _authentic(self, form: Mapping[str, str]) -> bool:
        key = str(self.config.notification_secret)
        given = form.get("sign", "").strip().lower()
        if given:
            return constant_time_equal(sign(key, form), given)
        legacy = form.get("sha1_hash", "").strip().lower()
        if legacy and self.config.allow_legacy_sha1:
            return constant_time_equal(legacy_sha1(key, form), legacy)
        return False

    async def parse_webhook(self, req: WebhookRequest) -> ProviderEvent:
        form = _form(req)
        if not self._authentic(form):
            raise WebhookRejected("bad signature")
        is_test = _flag(form.get("test_notification")) or form.get("operation_id") == "test-notification"
        if form.get("notification_type") not in INCOMING_TYPES:
            raise WebhookIgnored("not an incoming transfer")
        label = form.get("label", "").strip().lower()
        if not _UUID_RE.fullmatch(label):
            # A transfer that is not ours (no label: someone sent money to the wallet; a foreign label:
            # another shop on the same wallet) or the settings page «Протестировать» button.
            raise WebhookIgnored("test notification" if is_test else "no payment label")
        operation_id = form.get("operation_id", "").strip()
        if not 0 < len(operation_id) <= _ID_MAX:
            raise WebhookRejected("no operation_id", status=400)
        currency = _CURRENCIES.get(form.get("currency", "").strip())
        if currency is None:
            raise WebhookRejected("unknown currency", status=400)
        charged = _amount(form, "withdraw_amount")
        credited = _amount(form, "amount")
        if charged is not None and credited is not None and credited > charged:
            raise WebhookRejected("amount exceeds withdraw_amount", status=400)
        held = _flag(form.get("unaccepted")) or _flag(form.get("codepro"))
        state = PaymentState.PROCESSING if held else PaymentState.PAID
        paid_at = None
        if state is PaymentState.PAID and form.get("datetime"):
            try:
                paid_at = parse_timestamp(form["datetime"])
            except ValueError:
                paid_at = None
        return ProviderEvent(
            state=state,
            external_id=operation_id,
            payment_id=label,
            amount=charged,  # what the payer was charged == the form's sum; None → mismatch (no status API)
            currency=currency,
            paid_at=paid_at,
            is_test=is_test,
            summary={k: form[k] for k in _SUMMARY_FIELDS if k in form} | {"label": label},
        )

    def ack(self, event: ProviderEvent | None) -> WebhookResponse:
        return WebhookResponse.ok()

    # ------------------------------------------------------------------------------------------ probe

    async def test_credentials(self) -> Probe:
        """No side effects and no API without OAuth: the fields are validated by the config model; the
        signature is checked by ЮMoney's «Протестировать» button (a test notification is acknowledged)."""
        if not self.ctx.webhook_url:
            return Probe(False, _T["probe_no_url"])
        return Probe(True, _T["probe_ok"], {"offline": True, "webhook_url": self.ctx.webhook_url})
