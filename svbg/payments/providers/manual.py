"""Manual payment by bank details («Перевод»), wave A (07 §4.2, 04 §8, §9.1). Written from scratch.

Specification: ``docs/providers/manual.md``. Imports only :mod:`svbg.sdk`.

The plugin only shows the details: ``create`` returns a ``details`` checkout (owner's details, the exact
amount, a short payment code for the transfer comment and what to do next). The rest is the regular pipeline:

1. the user sends a receipt (photo or PDF/image document; :func:`receipt_problem` says what is acceptable);
2. billing stores it in ``manual_receipts`` and a card with «✅ Подтвердить» / «❌ Отклонить» is posted to the
   «💳 Оплаты» topic (:func:`receipt_card`);
3. a press is decided by :meth:`~svbg.payments.core.PaymentCore.confirm_manual` (role re-checked in the
   database — ``payments.confirm`` or owner; a group member without rights gets «Нет прав»; the amount from
   the receipt is mandatory and a difference becomes ``mismatch``) or ``reject_manual`` (reason mandatory).
   A second press is a no-op thanks to the CAS on the payment (and on ``manual_receipts``).

No webhook, no status API, never polled.
"""

from __future__ import annotations

import html
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
    WebhookAuth,
    integer,
    text,
)

__all__ = [
    "RECEIPT_MIME_TYPES",
    "ManualConfig",
    "ManualTransfer",
    "format_amount",
    "receipt_card",
    "receipt_code",
    "receipt_problem",
]

CURRENCIES: Final = (
    "RUB",
    "USD",
    "EUR",
    "GBP",
    "KZT",
    "UAH",
    "BYN",
    "UZS",
    "KGS",
    "AMD",
    "GEL",
    "AZN",
    "TRY",
)
_SYMBOLS: Final = {"RUB": "₽", "USD": "$", "EUR": "€", "GBP": "£", "KZT": "₸", "UAH": "₴", "BYN": "Br"}
#: Document types accepted as a receipt (a photo is always accepted).
RECEIPT_MIME_TYPES: Final = frozenset(
    {"application/pdf", "image/jpeg", "image/png", "image/webp", "image/heic"}
)
DEFAULT_INSTRUCTIONS: Final = (
    "После перевода пришлите сюда фото или PDF чека — администратор проверит и зачислит оплату."
)

_T: Final = {
    "pay": "Переведите {amount} по реквизитам:",
    "code": "Код платежа: {code} — укажите его в комментарии к переводу, если банк позволяет.",
    "too_big": "Файл слишком большой: до {mb} МБ.",
    "bad_kind": "Пришлите чек фотографией или файлом PDF/JPG/PNG.",
    "probe_ok": "Реквизиты заполнены. Пользователь присылает чек, администратор подтверждает оплату.",
    "card_title": "💳 <b>Чек на проверку</b> · {amount}",
    "card_user": "Пользователь: {user}",
    "card_code": "Код платежа: <code>{code}</code>",
    "card_created": "Счёт создан: {created}",
    "card_note": "Подтверждая, введите сумму из чека. Другая сумма — оплата уйдёт в «Требует внимания».",
}
#: English of the messages the **user** sees about a receipt file.
_T_EN: Final = {
    "too_big": "The file is too large: up to {mb} MB.",
    "bad_kind": "Please send the receipt as a photo or a PDF/JPG/PNG file.",
}


class ManualConfig(ConfigModel):
    details = text(
        "Реквизиты для перевода",
        "Что увидит пользователь: номер карты или телефон для СБП, банк, получатель. "
        "Можно в несколько строк.",
        where="ваш банк",
    )
    instructions = text(
        "Что делать после перевода",
        "Пояснение под реквизитами. Пусто — стандартный текст про чек.",
        required=False,
        advanced=True,
    )
    max_receipt_mb = integer(
        "Максимальный размер чека, МБ",
        "Файлы больше не принимаются.",
        default=10,
        min=1,
        max=20,
        advanced=True,
    )


def receipt_code(payment_id: str) -> str:
    """Short code of a payment for the transfer comment and the admin card: ``"7C3A-9D4E"``-like (the last
    8 hex digits of the opaque id, upper-case)."""
    tail = "".join(ch for ch in payment_id if ch.isalnum())[-8:].upper()
    return f"{tail[:4]}-{tail[4:]}" if len(tail) == 8 else tail


def format_amount(amount: Decimal, currency: str) -> str:
    """``Decimal("1699.50"), "RUB"`` → ``"1 699,50 ₽"``; whole amounts without kopecks."""
    cur = currency.upper()
    quantized = amount.quantize(Decimal("0.01"))
    units, _, cents = f"{quantized:f}".partition(".")
    groups: list[str] = []
    while len(units) > 3:
        groups.insert(0, units[-3:])
        units = units[:-3]
    groups.insert(0, units)
    number = "\N{NO-BREAK SPACE}".join(groups)
    if cents and cents != "00":
        number += "," + cents
    return f"{number}\N{NO-BREAK SPACE}{_SYMBOLS.get(cur, cur)}"


def receipt_problem(
    kind: str,
    *,
    mime_type: str | None = None,
    size: int | None = None,
    max_mb: int = 10,
    lang: str | None = "ru",
) -> str | None:
    """Why a user's message cannot be a receipt (Russian, English for ``lang="en"``), or ``None`` when it can.
    ``kind`` is ``"photo"`` or ``"document"``."""
    texts = _T_EN if lang == "en" else _T
    if size is not None and size > max_mb * 1024 * 1024:
        return texts["too_big"].format(mb=max_mb)
    if kind == "photo":
        return None
    if kind == "document" and (mime_type or "").lower() in RECEIPT_MIME_TYPES:
        return None
    return texts["bad_kind"]


def receipt_card(
    *,
    payment_id: str,
    amount: Decimal,
    currency: str,
    user_label: str,
    created_text: str | None = None,
) -> str:
    """HTML text of the receipt card in the «💳 Оплаты» topic (the receipt itself is attached by the caller).
    ``user_label`` and ``created_text`` (already formatted in the shop's time zone) are escaped here."""
    lines = [
        _T["card_title"].format(amount=html.escape(format_amount(amount, currency))),
        _T["card_user"].format(user=html.escape(user_label)),
        _T["card_code"].format(code=html.escape(receipt_code(payment_id))),
    ]
    if created_text:
        lines.append(_T["card_created"].format(created=html.escape(created_text)))
    lines.append(_T["card_note"])
    return "\n".join(lines)


class ManualTransfer(PaymentProvider):
    """See module docstring."""

    manifest = Manifest(
        slug="manual",
        title="Перевод по реквизитам",
        method_kinds=(MethodKind.MANUAL,),
        currencies=CURRENCIES,
        config=ManualConfig,
        docs_url="docs/providers/manual.md",
    )
    capabilities = Capabilities(
        webhook_auth=WebhookAuth.NONE,
        webhook=False,
        fetch_status=False,
        refund=False,
        in_chat_invoice=False,
        redirect=False,
    )

    async def create(self, intent: PaymentIntent) -> Checkout:
        details = str(self.config.details).strip()
        instructions = str(self.config.instructions or DEFAULT_INSTRUCTIONS).strip()
        body = "\n".join(
            [
                _T["pay"].format(amount=format_amount(intent.amount, intent.currency)),
                details,
                "",
                _T["code"].format(code=receipt_code(intent.payment_id)),
                instructions,
            ]
        )
        return Checkout(kind="details", details=body)

    def receipt_rules(self) -> Mapping[str, Any]:
        """Limits for the receipt upload (used by the user path)."""
        return {"max_mb": int(self.config.max_receipt_mb), "mime_types": sorted(RECEIPT_MIME_TYPES)}

    async def test_credentials(self) -> Probe:
        return Probe(True, _T["probe_ok"])
