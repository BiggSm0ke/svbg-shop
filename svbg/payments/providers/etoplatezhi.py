"""«Это платежи» (EtoPlatezhi) — SBP and cards through a signed Payment Page link (07 §4.2).

Specification: ``docs/providers/etoplatezhi.md`` (official developer portal
<https://developers.etoplatezhi.ru/ru/index.html>, Gate API 3.0.7 and the official MIT SDK
<https://github.com/etoedto/paymentpage-sdk-python>, checked 2026-10-02). Written from scratch by the
specification; imports only :mod:`svbg.sdk` and the standard library.

* **Signature** (requests, Gate answers and callbacks alike, spec §2): every scalar of the JSON tree becomes
  ``<node>:…:<name>:<value>`` (``true`` → ``1``, ``false`` → ``0``, ``null`` → empty, array items by index,
  empty arrays / objects give nothing, ``:`` in a key → ``::``, ``signature`` removed from every object
  outside arrays, top-level ``frame_mode`` ignored); the lines are sorted by path (byte order, as the SDK) and
  joined with ``;``; the result is ``Base64(HMAC-SHA512(project secret key, UTF-8))``. Compared in constant
  time.
* **Create** makes no request: the plugin builds ``https://paymentpage.etoplatezhi.ru/payment?…&signature=…``
  (``payment_id`` = our opaque id, ``customer_id`` = the opaque per-user hash of the core — never a Telegram
  id, ``payment_amount`` in kopecks, ``best_before`` = now + lifetime, ``force_payment_method`` = ``sbp-qr`` /
  ``card`` when the user picked a method kind, ``merchant_callback_url`` = the instance's webhook address,
  ``operation_type=sale``). The payment exists at the provider only after the user confirms the form, so our
  payment id doubles as the ``external_id``.
* **Status**: ``POST /v2/payment/status`` (signed ``general{project_id, payment_id}``); the answer is signed
  too and checked. ``3061 Transaction not found`` (form not confirmed) → absent from the result. No batch.
* **Callbacks**: JSON with ``signature`` in the root; the signed tree covers the whole body, so the scheme is
  strong (no time is signed: ``replay_window_s=None``; duplicates are deduplicated by the core). The project
  id must be ours. ``payment.status`` decides: ``success`` → paid, ``decline`` → failed, ``reversed`` /
  ``refunded`` (and the partial ones) → refunded, ``processing`` / ``awaiting …`` → processing, ``error``
  (not registered) → created. Chargebacks are not sent by callbacks (Dashboard only).
* No test flag: a test project is a separate project id + key (the instance's test mode).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final
from urllib.parse import quote_plus, urlencode

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
    integer,
    secret,
    url,
)

__all__ = [
    "DEFAULT_API_URL",
    "DEFAULT_LIFETIME_MIN",
    "DEFAULT_PAYMENT_PAGE_URL",
    "MAX_LIFETIME_MIN",
    "METHOD_CODES",
    "STATUS_MAP",
    "Etoplatezhi",
    "EtoplatezhiConfig",
    "payment_page_url",
    "sign",
    "signing_string",
]

DEFAULT_PAYMENT_PAGE_URL: Final = "https://paymentpage.etoplatezhi.ru"
DEFAULT_API_URL: Final = "https://api.etoplatezhi.ru"
#: SDK method kind → ``force_payment_method`` (``ru_pm_codes.html``).
METHOD_CODES: Final[Mapping[MethodKind, str]] = {MethodKind.SBP: "sbp-qr", MethodKind.CARD: "card"}
#: ``payment.status`` → SDK state (spec §6.1).
STATUS_MAP: Final[Mapping[str, PaymentState]] = {
    "error": PaymentState.CREATED,  # not registered (bad request / form not confirmed): may still be paid
    "processing": PaymentState.PROCESSING,
    "awaiting 3ds result": PaymentState.PROCESSING,
    "awaiting redirect result": PaymentState.PROCESSING,
    "awaiting clarification": PaymentState.PROCESSING,
    "awaiting customer": PaymentState.PROCESSING,
    "awaiting capture": PaymentState.PROCESSING,  # two-stage only; we always send operation_type=sale
    "decline": PaymentState.FAILED,
    "success": PaymentState.PAID,
    "partially reversed": PaymentState.REFUNDED,
    "reversed": PaymentState.REFUNDED,
    "partially refunded": PaymentState.REFUNDED,
    "refunded": PaymentState.REFUNDED,
    "cancelled": PaymentState.CANCELED,  # two-stage only
}
#: Minor-unit digits of the currencies a Russian project may report (``ru_currency_units.html``).
_EXPONENTS: Final[Mapping[str, int]] = {
    "RUB": 2, "USD": 2, "EUR": 2, "KZT": 2, "BYN": 2, "UAH": 2, "UZS": 2, "AMD": 2, "KGS": 2, "GBP": 2,
    "CNY": 2, "TRY": 2, "JPY": 0, "KRW": 0,
}  # fmt: skip
DEFAULT_LIFETIME_MIN: Final = 60
MAX_LIFETIME_MIN: Final = 43_000  # best_before is ignored beyond 30 days from the request (spec §3.1)
NOT_FOUND_CODE: Final = "3061"
_DESCRIPTION_MAX: Final = 255
_ID_MAX: Final = 200
_UUID_RE: Final = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

_T: Final = {
    "bad_key": "«Это платежи» не приняли подпись — проверьте ID проекта и секретный ключ",
    "forbidden": (
        "«Это платежи» отказали в доступе к Gate (HTTP 403): возможно, IP-адрес сервера бота не добавлен в "
        "список разрешённых — попросите поддержку «Это платежи» его добавить"
    ),
    "rejected": "«Это платежи» отклонили запрос (HTTP {status}){detail}",
    "unavailable": "«Это платежи» временно недоступны (HTTP {status})",
    "bad_answer": "«Это платежи» вернули непонятный ответ",
    "bad_signature": "ответ «Это платежи» не прошёл проверку подписи — проверьте секретный ключ проекта",
    "currency": "проект «Это платежи» в боте настроен только на рубли",
    "no_customer": "нет обезличенного идентификатора покупателя (customer_id обязателен)",
    "probe_ok": "ID проекта и секретный ключ приняты, Gate «Это платежи» доступен",
}


class EtoplatezhiConfig(ConfigModel):
    project_id = integer(
        "ID проекта",
        "Числовой идентификатор проекта (project_id). Для тестового режима — ID тестового проекта.",
        where="выдаёт поддержка «Это платежи» при подключении (тестовый и рабочий проект отдельно); "
        "также виден в Dashboard → «Проекты»",
        min=1,
    )
    secret_key = secret(
        "Секретный ключ проекта",
        "Ключ подписи ссылок на платёжную форму, запросов к Gate и оповещений (HMAC-SHA512).",
        where="выдаёт поддержка «Это платежи» вместе с ID проекта (у тестового проекта свой ключ)",
    )
    lifetime_min = integer(
        "Время на оплату, минут",
        "Сколько минут платёжная форма принимает оплату (best_before; 1…43000, по умолчанию 60).",
        default=DEFAULT_LIFETIME_MIN,
        required=False,
        min=1,
        max=MAX_LIFETIME_MIN,
        advanced=True,
    )
    language = choice(
        "Язык платёжной формы",
        ("ru", "en"),
        "Язык страницы оплаты (language_code).",
        default="ru",
        required=False,
        advanced=True,
    )
    return_url = url(
        "Куда вернуть покупателя",
        "Страница, куда ведёт кнопка возврата после оплаты. Пусто — ссылка, которую передаёт ядро.",
        where="обычно ссылка на вашего бота: https://t.me/<имя_бота>",
        required=False,
        advanced=True,
    )
    payment_page_url = url(
        "Адрес платёжной формы",
        "Адрес Payment Page. Меняйте, только если «Это платежи» сообщили другой.",
        default=DEFAULT_PAYMENT_PAGE_URL,
        required=False,
        advanced=True,
    )
    api_url = url(
        "Адрес Gate API",
        "Адрес серверного API (запрос статуса). Меняйте, только если «Это платежи» сообщили другой.",
        default=DEFAULT_API_URL,
        required=False,
        advanced=True,
    )


# ------------------------------------------------------------------------------------------- signature


def _scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def _walk(value: Any, path: tuple[str, ...], out: list[tuple[str, str]], *, in_array: bool) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            name = str(key)
            if name == "signature" and not in_array:
                continue
            if not path and name == "frame_mode":
                continue
            _walk(item, (*path, name.replace(":", "::")), out, in_array=False)
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _walk(item, (*path, str(index)), out, in_array=True)
    else:
        out.append((":".join(path), _scalar(value)))


def signing_string(data: Mapping[str, Any]) -> str:
    """The string to sign of a request, a Gate answer or a callback (spec §2)."""
    lines: list[tuple[str, str]] = []
    _walk(data, (), lines, in_array=False)
    lines.sort(key=lambda line: line[0])
    return ";".join(f"{path}:{value}" for path, value in lines)


def sign(data: Mapping[str, Any], key: str) -> str:
    """``Base64(HMAC-SHA512(key, signing_string(data)))``."""
    digest = hmac.new(key.encode("utf-8"), signing_string(data).encode("utf-8"), hashlib.sha512).digest()
    return base64.b64encode(digest).decode("ascii")


def payment_page_url(base: str, params: Mapping[str, Any], key: str) -> str:
    """A signed Payment Page link: the parameters as they go into the form, then ``&signature=``."""
    query = urlencode({k: _scalar(v) for k, v in params.items()})
    return f"{base.rstrip('/')}/payment?{query}&signature={quote_plus(sign(params, key))}"


# --------------------------------------------------------------------------------------------- helpers


def _decimal_json(body: bytes) -> Any:
    return json.loads(body.decode("utf-8"), parse_float=Decimal)


def _our_payment_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower()
    return candidate if _UUID_RE.fullmatch(candidate) else None


def _minor(value: Any) -> int | None:
    """An amount in minor units: a JSON integer or a string of digits; ``None`` otherwise."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and re.fullmatch(r"\d{1,15}", value.strip()):
        return int(value.strip())
    return None


def _sum(payment: Mapping[str, Any]) -> tuple[Decimal | None, str | None, bool]:
    """``(amount in major units, currency, readable)`` of ``payment.sum`` (or ``sum_real``)."""
    block = payment.get("sum")
    if not isinstance(block, Mapping):
        block = payment.get("sum_real")
    if not isinstance(block, Mapping):
        return None, None, True
    currency = block.get("currency")
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Za-z]{3}", currency.strip()):
        return None, None, False
    code = currency.strip().upper()
    minor = _minor(block.get("amount"))
    if minor is None:
        return None, code, False
    exp = _EXPONENTS.get(code)
    if exp is None:
        return None, code, True  # unknown digits: no amount (the core verifies or reports a mismatch)
    return Decimal(minor).scaleb(-exp), code, True


class Etoplatezhi(PaymentProvider):
    """See module docstring."""

    manifest = Manifest(
        slug="etoplatezhi",
        title="Это платежи",
        method_kinds=(MethodKind.SBP, MethodKind.CARD),
        currencies=("RUB",),
        config=EtoplatezhiConfig,
        docs_url="https://developers.etoplatezhi.ru/ru/index.html",
        min_minor=1,  # Gate schema: amount ≥ 1; real limits depend on the project (spec §10.5)
    )
    capabilities = Capabilities(
        webhook_auth=WebhookAuth.SIGNATURE,  # HMAC-SHA512 over the whole body tree
        replay_window_s=None,  # nothing in a callback signs the time
        fetch_status=True,
        batch_status=False,  # one payment per /v2/payment/status
        refund=False,  # Gate refunds exist; the first version refunds in the Dashboard (spec §7)
        recurring=False,  # spec §9: not needed in the first version
        receipt_54fz=False,
        in_chat_invoice=False,
        redirect=True,
        webhook=True,
    )

    # ------------------------------------------------------------------------------------------ create

    async def create(self, intent: PaymentIntent) -> Checkout:
        if intent.currency != "RUB":
            raise ProviderError(_T["currency"], retryable=False)
        customer = (intent.customer_ref or "").strip()
        if not customer:
            raise ProviderError(_T["no_customer"], retryable=False)
        lifetime = int(self.config.lifetime_min or DEFAULT_LIFETIME_MIN)
        best_before = (datetime.now(UTC) + timedelta(minutes=lifetime)).replace(microsecond=0)
        params: dict[str, Any] = {
            "project_id": int(self.config.project_id),
            "payment_id": intent.payment_id,  # opaque: never a Telegram id or a subscription link
            "customer_id": customer[:255],  # the core's opaque per-user hash (spec §3.1, §10.4)
            "payment_amount": intent.amount_minor,
            "payment_currency": intent.currency,
            "payment_description": (intent.description or "Оплата")[:_DESCRIPTION_MAX],
        }
        method = METHOD_CODES.get(intent.method_hint) if intent.method_hint else None
        if method is not None:
            params["force_payment_method"] = method
        params["best_before"] = best_before.strftime("%Y-%m-%dT%H:%M:%S+00")
        if self.ctx.webhook_url:
            params["merchant_callback_url"] = self.ctx.webhook_url
        back = self.config.return_url or intent.return_url
        if back:
            params["merchant_success_url"] = back
            params["merchant_fail_url"] = back
        params["language_code"] = self.config.language or "ru"
        params["operation_type"] = "sale"
        base = str(self.config.payment_page_url or DEFAULT_PAYMENT_PAGE_URL)
        return Checkout(
            kind="url",
            external_id=intent.payment_id,  # the provider knows the payment only by our id
            pay_url=payment_page_url(base, params, self.config.secret_key),
            expires_at=best_before,
        )

    # ---------------------------------------------------------------------------------------- webhook

    def _signed_by_us(self, data: Mapping[str, Any]) -> bool:
        given = data.get("signature")
        if given is None and isinstance(data.get("general"), Mapping):
            given = data["general"].get("signature")  # token callbacks sign in general.signature
        if not isinstance(given, str) or not given.strip():
            return False
        return constant_time_equal(sign(data, self.config.secret_key), given.strip())

    def _project_ok(self, data: Mapping[str, Any]) -> bool:
        project = data.get("project_id")
        if project is None and isinstance(data.get("general"), Mapping):
            project = data["general"].get("project_id")
        if project is None:
            return True  # a project may drop the field (spec §5.1); the signature still binds our key
        return (
            isinstance(project, int | str)
            and not isinstance(project, bool)
            and (str(project).strip() == str(self.config.project_id))
        )

    async def parse_webhook(self, req: WebhookRequest) -> ProviderEvent:
        try:
            data = _decimal_json(req.body)
        except (UnicodeDecodeError, ValueError):
            raise WebhookRejected("malformed", status=400) from None
        if not isinstance(data, dict):
            raise WebhookRejected("malformed", status=400)
        if not self._signed_by_us(data):
            raise WebhookRejected("bad signature", status=401)
        if not self._project_ok(data):
            raise WebhookRejected("foreign project", status=401)
        payment = data.get("payment")
        if not isinstance(payment, Mapping):
            raise WebhookIgnored("no payment")  # token / recurring / payment-link callbacks
        raw_status = str(payment.get("status") or "").strip().lower()
        state = STATUS_MAP.get(raw_status)
        if state is None:
            self.ctx.log.warning("etoplatezhi: callback with unknown status %r ignored", raw_status[:40])
            raise WebhookIgnored("unknown status")
        status = self._status(payment, state, malformed=WebhookRejected)
        operation = data.get("operation") if isinstance(data.get("operation"), Mapping) else {}
        return ProviderEvent(
            state=status.state,
            external_id=status.external_id,
            payment_id=status.payment_id,
            amount=status.amount,
            currency=status.currency,
            is_test=self.ctx.is_test,
            signed_at=None,
            summary={
                "payment_id": status.payment_id or status.external_id,
                "status": raw_status,
                "amount": None if status.amount is None else str(status.amount),
                "currency": status.currency,
                "method": _short(payment.get("method")),
                "operation_type": _short(operation.get("type")),
                "operation_status": _short(operation.get("status")),
                "code": _short(operation.get("code")),
            },
        )

    def _status(
        self,
        payment: Mapping[str, Any],
        state: PaymentState,
        *,
        external_id: str | None = None,
        malformed: type[Exception],
    ) -> ProviderStatus:
        raw_id = payment.get("id")
        ours = _our_payment_id(raw_id)
        external = external_id
        if (
            ours is None
            and external is None
            and isinstance(raw_id, str | int)
            and not isinstance(raw_id, bool)
        ):
            text_id = str(raw_id).strip()
            external = text_id if 0 < len(text_id) <= _ID_MAX else None
        if ours is None and external is None:
            raise _malformed(malformed, "no payment id")
        amount, currency, readable = _sum(payment)
        if not readable:
            raise _malformed(malformed, "bad amount")
        return ProviderStatus(
            state=state,
            external_id=external,
            payment_id=ours,
            amount=amount,
            currency=currency,
            is_test=self.ctx.is_test,
        )

    # ----------------------------------------------------------------------------------------- status

    def _status_request(self, payment_id: str) -> dict[str, Any]:
        general: dict[str, Any] = {"project_id": int(self.config.project_id), "payment_id": payment_id}
        body: dict[str, Any] = {"general": general}
        general["signature"] = sign(body, self.config.secret_key)
        return body

    async def _ask_status(self, payment_id: str) -> dict[str, Any]:
        base = str(self.config.api_url or DEFAULT_API_URL).rstrip("/")
        resp = await self.ctx.http.request(
            "POST",
            f"{base}/v2/payment/status",
            headers={"Accept": "application/json"},
            json=self._status_request(payment_id),
        )
        if resp.status != 200:
            self._raise_for(resp)
        try:
            data = _decimal_json(resp.body)
        except (UnicodeDecodeError, ValueError):
            raise ProviderError(_T["bad_answer"], retryable=True, status=resp.status) from None
        if not isinstance(data, dict):
            raise ProviderError(_T["bad_answer"], retryable=True, status=resp.status)
        if not self._signed_by_us(data):
            self.ctx.log.warning("etoplatezhi: a status answer failed the signature check")
            raise ProviderError(_T["bad_signature"], retryable=False, status=resp.status)
        return data

    def _raise_for(self, resp: HttpResponse) -> None:
        if resp.status == 403:
            raise ProviderError(_T["forbidden"], retryable=False, status=resp.status)
        if resp.status == 401:
            raise ProviderError(_T["bad_key"], retryable=False, status=resp.status)
        if resp.status in (408, 429) or resp.status >= 500:
            raise ProviderError(
                _T["unavailable"].format(status=resp.status), retryable=True, status=resp.status
            )
        self.ctx.log.warning("etoplatezhi: status answered HTTP %s", resp.status)
        raise ProviderError(
            _T["rejected"].format(status=resp.status, detail=_detail(resp)),
            retryable=False,
            status=resp.status,
        )

    async def fetch_status(self, ids: Sequence[str]) -> list[ProviderStatus]:
        result: list[ProviderStatus] = []
        for external in dict.fromkeys(ids):
            if not external:
                continue
            data = await self._ask_status(external)
            if _not_found(data):
                continue  # the form was never confirmed: unknown to the platform
            if not self._project_ok(data):
                self.ctx.log.warning("etoplatezhi: status of %s answered for another project", external[:40])
                continue
            payment = data.get("payment")
            if not isinstance(payment, Mapping):
                continue
            state = STATUS_MAP.get(str(payment.get("status") or "").strip().lower())
            if state is None:
                continue
            reported = _our_payment_id(payment.get("id"))
            if reported is not None and reported != external.strip().lower():
                self.ctx.log.warning("etoplatezhi: status of %s answered for another payment", external[:40])
                continue
            try:
                status = self._status(
                    {**payment, "id": payment.get("id", external)}, state, external_id=external,
                    malformed=ProviderError,
                )  # fmt: skip
            except ProviderError:
                self.ctx.log.warning("etoplatezhi: unreadable status of %s skipped", external[:40])
                continue
            result.append(status)
        return result

    # ------------------------------------------------------------------------------------------ probe

    async def test_credentials(self) -> Probe:
        """No side effects: the status of a payment that does not exist (a signed request; the signed answer
        «3061 Transaction not found» proves both the project id and the key)."""
        try:
            data = await self._ask_status(str(uuid.uuid4()))
        except ProviderError as exc:
            return Probe(False, exc.human)
        if not self._project_ok(data):
            return Probe(False, _T["bad_key"])
        return Probe(True, _T["probe_ok"], {"found": not _not_found(data)})


def _not_found(data: Mapping[str, Any]) -> bool:
    errors = data.get("errors")
    if not isinstance(errors, list):
        return False
    return any(isinstance(e, Mapping) and str(e.get("code")) == NOT_FOUND_CODE for e in errors)


def _short(value: Any) -> str | None:
    if value is None or isinstance(value, bool | Mapping | list):
        return None
    return str(value)[:40]


def _detail(resp: HttpResponse) -> str:
    """A short provider message for the owner (``: …``), never the request itself."""
    try:
        data = json.loads(resp.body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    messages = [data.get("message")]
    errors = data.get("errors")
    if isinstance(errors, list):
        messages += [e.get("message") for e in errors if isinstance(e, Mapping)]
    for value in messages:
        if isinstance(value, str) and value.strip():
            return ": " + value.strip()[:120]
    return ""


def _malformed(kind: type[Exception], reason: str) -> Exception:
    if kind is WebhookRejected:
        return WebhookRejected(reason, status=400)
    return ProviderError(_T["bad_answer"], retryable=False)
