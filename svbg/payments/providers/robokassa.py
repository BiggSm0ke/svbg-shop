"""Robokassa — payment link with a signed Result URL, wave B (07 §4.2).

Specification: ``docs/providers/robokassa.md`` (official pages docs.robokassa.ru/ru/…, checked 2026-10-02).
Ported from Remnashop (MIT, © 2024 snoups) ``src/infrastructure/payment_gateways/robokassa.py`` and rewritten
for the SvBG SDK — see ``THIRD_PARTY_NOTICES.md``. Imports only :mod:`svbg.sdk`.

* ``create`` builds the payment link ``{base}/Index.aspx`` locally (no request):
  ``SignatureValue = H(MerchantLogin:OutSum:InvId:Password#1:Shp_svbg=<payment id>)``, ``H`` is the shop's
  hash algorithm (MD5 by default; RIPEMD160, SHA1, SHA256, SHA384, SHA512). ``InvId`` is a 63-bit number
  derived from our UUIDv7 payment id (:func:`inv_id_for`); ``Shp_svbg`` carries the payment id itself. On a
  test instance the link carries ``IsTest=1`` (the owner enters the *test* passwords there).
* Result URL (form body, or query): ``SignatureValue = H(OutSum:InvId:Password#2[:Shp_k=v…])`` with every
  ``Shp_*`` parameter sorted by name, compared case-insensitively in constant time over the values **as
  received** (``OutSum`` like ``179.000000``). The answer is ``OK<InvId>``.
* ``fetch_status`` reads ``OpStateExt`` (XML; ``Signature = H(MerchantLogin:InvoiceID:Password#2)``) —
  it works for live payments only, so a test instance returns nothing. State ``60`` (returned to the buyer)
  is reported as ``refunded``.
"""

from __future__ import annotations

import hashlib
import re
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Final
from urllib.parse import parse_qsl, quote, urlencode

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
    WebhookRejected,
    WebhookRequest,
    WebhookResponse,
    choice,
    constant_time_equal,
    parse_amount,
    parse_timestamp,
    secret,
    text,
    url,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "HASH_ALGORITHMS",
    "SHP_KEY",
    "STATE_MAP",
    "Robokassa",
    "RobokassaConfig",
    "digest",
    "inv_id_for",
    "parse_op_state",
    "payment_signature",
    "result_signature",
    "state_signature",
]

DEFAULT_BASE_URL: Final = "https://auth.robokassa.ru/Merchant"
HASH_ALGORITHMS: Final = ("md5", "ripemd160", "sha1", "sha256", "sha384", "sha512")
SHP_KEY: Final = "Shp_svbg"
#: ``OpStateExt`` ``State/Code`` → SDK state.
STATE_MAP: Final[Mapping[int, PaymentState]] = {
    5: PaymentState.CREATED,  # initiated, not paid
    10: PaymentState.CANCELED,  # cancelled, no money received
    20: PaymentState.PROCESSING,  # HOLD
    50: PaymentState.PROCESSING,  # money received, being credited to the shop
    60: PaymentState.REFUNDED,  # crediting refused, money returned to the buyer
    80: PaymentState.PROCESSING,  # suspended (security check)
    100: PaymentState.PAID,
}
_DESCRIPTION_MAX: Final = 100
_ID_MAX: Final = 200
_MAX_FIELDS: Final = 64
_INV_MAX: Final = 2**63 - 1
_UUID_RE: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_SHP_NAME_RE: Final = re.compile(r"Shp_[A-Za-z0-9_]+", re.IGNORECASE)
_TRUE: Final = frozenset({"1", "true"})
_SUMMARY_FIELDS: Final = ("InvId", "OutSum", "Fee", "PaymentMethod", "IncCurrLabel", "IsTest")
_ISO_FRACTION_RE: Final = re.compile(r"(T\d{2}:\d{2}:\d{2})\.(\d{1,6})\d*")

_T: Final = {
    "currency": "Robokassa в этом плагине принимает только рубли",
    "algorithm": "алгоритм {name} недоступен на сервере — выберите другой в Robokassa и в боте",
    "bad_signature": (
        "Robokassa отклонила подпись запроса статуса — проверьте Пароль #2 и алгоритм хеша "
        "(они должны совпадать с «Техническими настройками» магазина)"
    ),
    "no_shop": "Robokassa не нашла магазин или он не активирован — проверьте идентификатор магазина",
    "unavailable": "Robokassa временно недоступна ({detail})",
    "rejected": "Robokassa отклонила запрос (HTTP {status})",
    "bad_answer": "Robokassa вернула непонятный ответ",
    "probe_ok": (
        "Идентификатор магазина, Пароль #2 и алгоритм приняты. Пароль #1 проверится первой оплатой: при "
        "ошибке Robokassa покажет покупателю «неверная подпись»."
    ),
    "probe_test": (
        "Тестовый режим: Robokassa не отвечает на запросы статуса тестовых платежей, поэтому ключи "
        "проверятся первой тестовой оплатой. Используйте тестовые пароли из «Технических настроек»."
    ),
    "probe_not_robokassa": "по адресу Robokassa отвечает что-то другое — проверьте «Адрес Robokassa»",
}


class RobokassaConfig(ConfigModel):
    merchant_login = text(
        "Идентификатор магазина",
        "MerchantLogin — латинское имя магазина в Robokassa.",
        where="robokassa.com → «Мои магазины» → магазин → «Технические настройки» → «Идентификатор магазина»",
        pattern=r"[A-Za-z0-9_.\-]{1,64}",
    )
    password1 = secret(
        "Пароль #1",
        "Им подписывается ссылка на оплату. Для тестового инстанса — тестовый Пароль #1.",
        where="«Технические настройки» магазина → «Пароль #1» (тестовый — в блоке «Параметры проведения "
        "тестовых платежей»)",
    )
    password2 = secret(
        "Пароль #2",
        "Им Robokassa подписывает уведомление на Result URL, им же подписываются запросы статуса. Для "
        "тестового инстанса — тестовый Пароль #2.",
        where="«Технические настройки» магазина → «Пароль #2» (тестовый — в блоке «Параметры проведения "
        "тестовых платежей»)",
    )
    hash_algorithm = choice(
        "Алгоритм хеша",
        HASH_ALGORITHMS,
        "Должен совпадать с «Алгоритм расчёта хеша» в Robokassa (для боевых и тестовых платежей один). "
        "Рекомендуем sha256.",
        where="«Технические настройки» магазина → «Алгоритм расчёта хеша»",
        default="md5",
        required=False,
    )
    culture = choice(
        "Язык страницы оплаты",
        ("ru", "en"),
        default="ru",
        required=False,
        advanced=True,
    )
    base_url = url(
        "Адрес Robokassa",
        "Базовый адрес Merchant-интерфейса. Не меняйте без необходимости.",
        default=DEFAULT_BASE_URL,
        required=False,
        advanced=True,
    )


# ------------------------------------------------------------------------------------------ signatures


def digest(algorithm: str, raw: str) -> str:
    """Lower-case hex ``algorithm(raw)`` (UTF-8). ``ProviderError`` when the server lacks the algorithm."""
    name = algorithm.lower().replace("-", "")
    if name not in HASH_ALGORITHMS:
        raise ProviderError(_T["algorithm"].format(name=algorithm))
    try:
        return hashlib.new(name, raw.encode("utf-8")).hexdigest()
    except ValueError:
        raise ProviderError(_T["algorithm"].format(name=algorithm)) from None


def _shp_tail(shp: Mapping[str, str]) -> str:
    return "".join(f":{k}={shp[k]}" for k in sorted(shp))


def payment_signature(  # noqa: PLR0917 - the protocol's fields
    algorithm: str, login: str, out_sum: str, inv_id: str, password1: str, shp: Mapping[str, str]
) -> str:
    """``H(MerchantLogin:OutSum:InvId:Password#1[:Shp_k=v…])`` (Shp sorted by name)."""
    return digest(algorithm, f"{login}:{out_sum}:{inv_id}:{password1}{_shp_tail(shp)}")


def result_signature(
    algorithm: str, out_sum: str, inv_id: str, password2: str, shp: Mapping[str, str]
) -> str:
    """Result URL: ``H(OutSum:InvId:Password#2[:Shp_k=v…])`` over the values as received."""
    return digest(algorithm, f"{out_sum}:{inv_id}:{password2}{_shp_tail(shp)}")


def state_signature(algorithm: str, login: str, invoice_id: str, password2: str) -> str:
    """``OpStateExt``: ``H(MerchantLogin:InvoiceID:Password#2)``."""
    return digest(algorithm, f"{login}:{invoice_id}:{password2}")


def inv_id_for(payment_id: str) -> int:
    """A stable ``InvId`` (1..2⁶³−1) for our payment id.

    UUIDv7: the 48-bit millisecond timestamp shifted left by 15 bits plus the low 15 bits of the random part —
    the core's generator steps the random part by 1..64 within one millisecond, so ids made in the same
    millisecond differ. Anything else: 63 bits of SHA-256 of the id."""
    try:
        value = uuid.UUID(payment_id)
    except ValueError:
        value = None
    if value is not None and value.version == 7:
        inv = ((value.int >> 80) << 15) | (value.int & 0x7FFF)
    else:
        inv = int.from_bytes(hashlib.sha256(payment_id.encode("utf-8")).digest()[:8], "big") >> 1
    return min(max(inv, 1), _INV_MAX)


# ------------------------------------------------------------------------------------------- OpStateExt


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(node: ET.Element | None, name: str) -> ET.Element | None:
    if node is None:
        return None
    return next((c for c in node if _local(c.tag) == name), None)


def _text(node: ET.Element | None, *path: str) -> str | None:
    for name in path:
        node = _child(node, name)
    if node is None or node.text is None:
        return None
    return node.text.strip() or None


def _robokassa_time(raw: str | None) -> datetime | None:
    """``2026-10-02T12:00:00.1234567+03:00`` (7-digit fraction) → aware UTC; ``None`` when unreadable."""
    if not raw:
        return None
    value = _ISO_FRACTION_RE.sub(lambda m: f"{m.group(1)}.{m.group(2)}", raw.replace(";", "").strip())
    try:
        return parse_timestamp(value)
    except ValueError:
        return None


def parse_op_state(body: bytes) -> ET.Element:
    """The ``OperationStateResponse`` root; ``ValueError`` for anything else (DTDs and entities refused)."""
    head = body[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in body.lower():
        raise ValueError("DTD in answer")
    try:
        root = ET.fromstring(body)  # noqa: S314 - no DTD/entities (checked above), bounded size
    except ET.ParseError:
        raise ValueError("not XML") from None
    if _local(root.tag) != "OperationStateResponse":
        raise ValueError("unexpected XML")
    return root


def _form(req: WebhookRequest) -> dict[str, str]:
    """Result URL parameters: the form body, or the query string (GET mode)."""
    pairs: list[tuple[str, str]]
    try:
        text_body = req.body.decode("utf-8").strip()
        if text_body:
            pairs = parse_qsl(
                text_body, keep_blank_values=True, strict_parsing=True, max_num_fields=_MAX_FIELDS
            )
        else:
            pairs = list(req.query.items())
    except (UnicodeDecodeError, ValueError):
        raise WebhookRejected("malformed form", status=400) from None
    form: dict[str, str] = {}
    for key, value in pairs:
        if key in form:
            raise WebhookRejected(f"duplicate parameter {key[:40]}", status=400)
        form[key] = value
    return form


class Robokassa(PaymentProvider):
    """See module docstring."""

    manifest = Manifest(
        slug="robokassa",
        title="Robokassa",
        method_kinds=(MethodKind.CARD, MethodKind.SBP),
        currencies=("RUB",),
        config=RobokassaConfig,
        docs_url="https://docs.robokassa.ru/ru/pay-interface",
    )
    capabilities = Capabilities(
        webhook_auth=WebhookAuth.SIGNATURE,
        replay_window_s=None,  # Result URL carries no signed time
        fetch_status=True,  # OpStateExt, one invoice per request, live payments only
        batch_status=False,
        refund=False,
        receipt_54fz=False,  # Receipt (54-ФЗ) is not sent yet
        redirect=True,
    )

    @property
    def _algo(self) -> str:
        return str(self.config.hash_algorithm or "md5")

    @property
    def _base(self) -> str:
        return str(self.config.base_url or DEFAULT_BASE_URL).rstrip("/")

    # ----------------------------------------------------------------------------------------- create

    def link_params(self, intent: PaymentIntent) -> dict[str, str]:
        """Query of the payment link (also used by the tests)."""
        if intent.currency != "RUB":
            raise ProviderError(_T["currency"])
        login = str(self.config.merchant_login)
        out_sum = intent.amount_text()
        inv_id = str(inv_id_for(intent.payment_id))
        shp = {SHP_KEY: intent.payment_id}
        params = {
            "MerchantLogin": login,
            "OutSum": out_sum,
            "InvId": inv_id,
            "Description": intent.description[:_DESCRIPTION_MAX],
            "SignatureValue": payment_signature(
                self._algo, login, out_sum, inv_id, str(self.config.password1), shp
            ),
            "Culture": str(self.config.culture or "ru"),
            "Encoding": "utf-8",
            **shp,
        }
        if self.ctx.is_test:
            params["IsTest"] = "1"
        return params

    async def create(self, intent: PaymentIntent) -> Checkout:
        params = self.link_params(intent)
        pay_url = f"{self._base}/Index.aspx?{urlencode(params, quote_via=quote)}"
        return Checkout(kind="url", external_id=params["InvId"], pay_url=pay_url)

    # ---------------------------------------------------------------------------------------- webhook

    async def parse_webhook(self, req: WebhookRequest) -> ProviderEvent:
        form = _form(req)
        out_sum, inv_id, given = form.get("OutSum"), form.get("InvId"), form.get("SignatureValue", "").strip()
        if not out_sum or not inv_id or not given:
            raise WebhookRejected("no signature")
        shp = {k: v for k, v in form.items() if k[:4].lower() == "shp_" and _SHP_NAME_RE.fullmatch(k)}
        expected = result_signature(self._algo, out_sum, inv_id, str(self.config.password2), shp)
        if not constant_time_equal(expected.upper(), given.upper()):
            raise WebhookRejected("bad signature")
        if len(inv_id) > _ID_MAX:
            raise WebhookRejected("InvId too long", status=400)
        try:
            amount = parse_amount(out_sum)
        except (TypeError, ValueError):
            raise WebhookRejected("bad OutSum", status=400) from None
        pid = shp.get(SHP_KEY, "").strip().lower()
        return ProviderEvent(
            state=PaymentState.PAID,  # Result URL is called only for a successful payment
            external_id=inv_id,
            payment_id=pid if _UUID_RE.fullmatch(pid) else None,
            amount=amount,
            currency="RUB",
            is_test=form.get("IsTest", "").strip().lower() in _TRUE,
            summary={k: form[k] for k in _SUMMARY_FIELDS if k in form},  # never EMail
        )

    def ack(self, event: ProviderEvent | None) -> WebhookResponse:
        """``OK<InvId>`` — anything else makes Robokassa treat the notification as failed."""
        inv_id = event.external_id if event is not None and event.external_id else ""
        return WebhookResponse.ok(f"OK{inv_id}")

    # ----------------------------------------------------------------------------------------- status

    async def _op_state(self, invoice_id: str) -> ET.Element:
        login = str(self.config.merchant_login)
        params = {
            "MerchantLogin": login,
            "InvoiceID": invoice_id,
            "Signature": state_signature(self._algo, login, invoice_id, str(self.config.password2)),
        }
        resp: HttpResponse = await self.ctx.http.request(
            "GET", f"{self._base}/WebService/Service.asmx/OpStateExt", params=params
        )
        if resp.status == 429 or resp.status >= 500:
            detail = f"HTTP {resp.status}"
            raise ProviderError(_T["unavailable"].format(detail=detail), retryable=True, status=resp.status)
        if not resp.ok:
            raise ProviderError(_T["rejected"].format(status=resp.status), status=resp.status)
        try:
            return parse_op_state(resp.body)
        except ValueError:
            raise ProviderError(_T["bad_answer"], retryable=True) from None

    @staticmethod
    def _result_code(root: ET.Element) -> int | None:
        raw = _text(root, "Result", "Code")
        return int(raw) if raw is not None and raw.lstrip("-").isdigit() else None

    def _status(self, invoice_id: str, root: ET.Element) -> ProviderStatus | None:
        raw_state = _text(root, "State", "Code")
        state = STATE_MAP.get(int(raw_state)) if raw_state and raw_state.isdigit() else None
        if state is None:
            self.ctx.log.warning("robokassa: unknown state %r of invoice %s", raw_state, invoice_id)
            return None
        amount: Decimal | None = None
        raw_sum = _text(root, "Info", "OutSum")
        if raw_sum is not None:
            try:
                amount = parse_amount(raw_sum)
            except (TypeError, ValueError):
                amount = None
        pid: str | None = None
        fields = _child(root, "UserFields")
        for item in fields if fields is not None else ():
            name = (_text(item, "Name") or "").lower()
            if name in (SHP_KEY.lower(), SHP_KEY.lower().removeprefix("shp_")):
                candidate = (_text(item, "Value") or "").lower()
                pid = candidate if _UUID_RE.fullmatch(candidate) else None
        return ProviderStatus(
            state=state,
            external_id=invoice_id,
            payment_id=pid,
            amount=amount,
            currency="RUB",
            paid_at=_robokassa_time(_text(root, "State", "StateDate"))
            if state is PaymentState.PAID
            else None,
        )

    async def fetch_status(self, ids: Sequence[str]) -> list[ProviderStatus]:
        if self.ctx.is_test:
            return []  # OpStateExt does not know test payments
        result: list[ProviderStatus] = []
        for invoice_id in dict.fromkeys(i for i in ids if i):
            root = await self._op_state(invoice_id)
            code = self._result_code(root)
            if code == 0:
                status = self._status(invoice_id, root)
                if status is not None:
                    result.append(status)
            elif code == 1:
                raise ProviderError(_T["bad_signature"])
            elif code == 2:
                raise ProviderError(_T["no_shop"])
            elif code == 3:
                continue
            elif code == 4:
                self.ctx.log.warning("robokassa: two operations share InvId %s", invoice_id)
            else:
                raise ProviderError(_T["unavailable"].format(detail=f"код {code}"), retryable=True)
        return result

    # ------------------------------------------------------------------------------------------ probe

    async def test_credentials(self) -> Probe:
        """``OpStateExt`` for a random invoice: «not found» (3) means the login, Password #2 and the
        algorithm are right. No invoice is created."""
        if self.ctx.is_test:
            return Probe(True, _T["probe_test"])
        probe_id = str(int.from_bytes(uuid.uuid4().bytes[:7], "big") + 10**17)
        try:
            root = await self._op_state(probe_id)
        except ProviderError as exc:
            if exc.human == _T["bad_answer"]:
                return Probe(False, _T["probe_not_robokassa"])
            return Probe(False, exc.human)
        code = self._result_code(root)
        if code in (0, 3):
            return Probe(True, _T["probe_ok"])
        if code == 1:
            return Probe(False, _T["bad_signature"])
        if code == 2:
            return Probe(False, _T["no_shop"])
        return Probe(False, _T["unavailable"].format(detail=f"код {code}"))
