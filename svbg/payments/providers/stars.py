"""Telegram Stars — in-chat invoices in ``XTR``, wave A (07 §4.2, 04 §8, 06 §2.4.4).

Ported from Remnashop (MIT, © 2024 snoups) ``src/infrastructure/payment_gateways/telegram_stars.py`` and
rewritten for the SvBG SDK — see ``THIRD_PARTY_NOTICES.md``; specification: ``docs/providers/stars.md``.
Imports only :mod:`svbg.sdk`.

Stars have no webhook and no status API: Telegram itself is the trusted channel.

* Payments of this instance are in ``XTR``: ``amount_minor`` **is** the number of stars. Billing quotes a
  top-up with ``PAY_STARS_RATE`` (declared here as the ``rate`` field: shop currency per ⭐, whole star
  rounded up) and credits ``stars × rate`` to the wallet; the plugin never converts.
* ``create`` returns the parameters of ``createInvoiceLink`` / ``sendInvoice``: ``currency="XTR"``,
  ``provider_token=""``, one price, ``payload`` = the opaque payment id. The bot (which owns the token — the
  plugin never sees it) turns them into a link for the pay button of the same message.
* ``pre_checkout_query`` → :meth:`TelegramStars.check_pre_checkout`: the last moment to refuse, so the
  payload, the payment status, the currency and the exact star count are checked.
* ``successful_payment`` → :meth:`~svbg.payments.core.PaymentCore.credit_external` with
  ``external_id = telegram_payment_charge_id`` and the stars as paid (``total_amount``, ``XTR``): the core's
  CAS credits once, a different count becomes ``mismatch``.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Final

from svbg.sdk import (
    Capabilities,
    Checkout,
    ConfigModel,
    Manifest,
    MethodKind,
    PaymentIntent,
    PaymentProvider,
    Probe,
    ProviderError,
    WebhookAuth,
    integer,
    text,
)

__all__ = [
    "LATE_PAYABLE",
    "STARS_CURRENCY",
    "TEXTS",
    "PayloadKind",
    "StarsConfig",
    "TelegramStars",
    "classify_payload",
]

STARS_CURRENCY: Final = "XTR"
#: Payment statuses a star invoice may still be paid in («поздняя оплата побеждает»; same set as the core).
LATE_PAYABLE: Final = frozenset({"pending", "expired", "canceled", "failed"})
_TITLE_MAX: Final = 32
_DESCRIPTION_MAX: Final = 255
_RATE_RE: Final = r"\d{1,6}([.,]\d{1,2})?"
_UUID_RE: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_LEGACY_BALANCE_RE: Final = re.compile(r"balance_(\d{1,12})_(\d{1,12})")
_REFUSED_PREFIXES: Final = ("trial_", "guest_purchase_", "wheel_spin_")

#: Texts for ``answerPreCheckoutQuery(ok=False, error_message=…)`` — shown to the buyer by Telegram.
TEXTS: Final = {
    "not_found": "Счёт не найден. Создайте новый в боте.",
    "paid": "Этот счёт уже оплачен.",
    "closed": "Счёт больше недействителен. Создайте новый в боте.",
    "changed": "Сумма счёта изменилась. Создайте новый в боте.",
    "outdated": "Счёт устарел. Создайте новый в боте.",
    "too_large": "Слишком большая сумма для оплаты звёздами (больше {max} ⭐). Выберите другой способ.",
    "currency": "Звёздами оплачиваются только счета в ⭐.",
    "probe_ok": "Курс {rate} за 1 ⭐. Счета выставляет сам бот, ключи не нужны.",
}


class PayloadKind(enum.StrEnum):
    """What an invoice payload is (06 §2.4.4)."""

    OURS = "ours"  # an opaque payment id of this bot
    LEGACY_BALANCE = "legacy_balance"  # Bedolaga's balance_<user_id>_<kopeks>
    REFUSED = "refused"  # Bedolaga features that are not carried over (trial_, guest_purchase_, wheel_spin_)
    UNKNOWN = "unknown"


def classify_payload(payload: str | None) -> PayloadKind:
    value = (payload or "").strip()
    if _UUID_RE.fullmatch(value):
        return PayloadKind.OURS
    if _LEGACY_BALANCE_RE.fullmatch(value):
        return PayloadKind.LEGACY_BALANCE
    if value.startswith(_REFUSED_PREFIXES):
        return PayloadKind.REFUSED
    return PayloadKind.UNKNOWN


class StarsConfig(ConfigModel):
    rate = text(
        "Курс: цена 1 ⭐ в валюте магазина",
        "Сколько рублей (валюты магазина) зачисляется на баланс за одну звезду; не больше двух знаков после "
        "запятой. Сумма пополнения в звёздах округляется вверх до целой звезды.",
        where="ваше решение; у Bedolaga — TELEGRAM_STARS_RATE_RUB",
        default="1",
        pattern=_RATE_RE,
    )
    max_stars = integer(
        "Максимум звёзд в одном счёте",
        "Больше — счёт звёздами не выставляется (ограничение Telegram на один платёж).",
        default=10_000,
        min=1,
        max=1_000_000,
        advanced=True,
    )


def _invoice_stars(invoice: Mapping[str, Any] | None) -> int | None:
    """The star count of stored invoice parameters, or ``None`` when they are not a star invoice."""
    if not invoice or invoice.get("currency") != STARS_CURRENCY:
        return None
    prices = invoice.get("prices")
    if not isinstance(prices, list) or len(prices) != 1 or not isinstance(prices[0], Mapping):
        return None
    amount = prices[0].get("amount")
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        return None
    return amount


def _shorten(value: str, limit: int) -> str:
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


class TelegramStars(PaymentProvider):
    """See module docstring."""

    manifest = Manifest(
        slug="stars",
        title="Telegram Stars",
        method_kinds=(MethodKind.STARS,),
        currencies=(STARS_CURRENCY,),
        config=StarsConfig,
        docs_url="https://core.telegram.org/bots/payments-stars",
    )
    capabilities = Capabilities(
        webhook_auth=WebhookAuth.NONE,
        webhook=False,
        fetch_status=False,
        refund=False,
        in_chat_invoice=True,
        redirect=False,
    )

    @property
    def rate(self) -> Decimal:
        """Shop currency per star, exact (``"1,79"`` → ``Decimal("1.79")``)."""
        return Decimal(str(self.config.rate).replace(",", "."))

    # ------------------------------------------------------------------------------------------ create

    async def create(self, intent: PaymentIntent) -> Checkout:
        if intent.currency != STARS_CURRENCY:
            raise ProviderError(TEXTS["currency"])
        stars = intent.amount_minor
        if stars > int(self.config.max_stars):
            raise ProviderError(TEXTS["too_large"].format(max=int(self.config.max_stars)))
        description = intent.description.strip() or "Оплата"
        invoice = {
            "title": _shorten(description, _TITLE_MAX),
            "description": _shorten(f"{description} — {stars} ⭐", _DESCRIPTION_MAX),
            "payload": intent.payment_id,
            "provider_token": "",  # Telegram Stars: empty token
            "currency": STARS_CURRENCY,
            "prices": [{"label": _shorten(description, _TITLE_MAX), "amount": stars}],
        }
        return Checkout(kind="invoice", invoice=invoice)

    # ------------------------------------------------------------------------------- trusted channel

    def check_pre_checkout(
        self,
        *,
        payload: str,
        currency: str,
        total_amount: int,
        payment_status: str | None,
        invoice: Mapping[str, Any] | None,
    ) -> str | None:
        """Decide a ``pre_checkout_query``. ``payment_status`` / ``invoice`` are the payment's ``status`` and
        ``checkout["invoice"]`` (``None`` — no such payment of this instance). Returns ``None`` to answer
        ``ok=True`` or the Russian ``error_message`` for ``ok=False``. The caller additionally runs the
        spending guard (``PaymentCore.can_spend``): a frozen user is refused here, before money moves.
        Legacy payloads (06 §2.4.4) are decided by the caller (it knows the switch-over date)."""
        if classify_payload(payload) is not PayloadKind.OURS:
            return TEXTS["outdated"]
        if payment_status is None or invoice is None:
            return TEXTS["not_found"]
        if payment_status == "paid":
            return TEXTS["paid"]
        if payment_status not in LATE_PAYABLE:
            return TEXTS["closed"]
        expected = _invoice_stars(invoice)
        if (
            expected is None
            or invoice.get("payload") != payload
            or currency != STARS_CURRENCY
            or isinstance(total_amount, bool)
            or total_amount != expected
        ):
            return TEXTS["changed"]
        return None

    async def test_credentials(self) -> Probe:
        return Probe(True, TEXTS["probe_ok"].format(rate=self.rate.normalize()))
