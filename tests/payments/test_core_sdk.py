"""SDK 1.0-beta value types and helpers: config models, amounts, timestamps, checkouts, requests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from svbg.sdk import (
    SDK_VERSION,
    Checkout,
    ConfigError,
    ConfigModel,
    MethodKind,
    PaymentIntent,
    PaymentState,
    ProviderEvent,
    ProviderStatus,
    WebhookAuth,
    WebhookRejected,
    WebhookRequest,
    WebhookResponse,
    choice,
    constant_time_equal,
    flag,
    hmac_sha256_hex,
    integer,
    number,
    parse_amount,
    parse_timestamp,
    secret,
    text,
    url,
)


class Cfg(ConfigModel):
    token = secret("Токен", where="кабинет → API")
    shop = text("Магазин", required=False)
    base = url("API", default="https://api.example/v1")
    retries = integer("Повторы", default=3, min=0, max=10)
    rate = number("Курс", default=1.0, min=0.01)
    sandbox = flag("Песочница")
    network = choice("Сеть", ("TON", "TRC20"), default="TON")


def test_config_model_parses_and_masks() -> None:
    cfg = Cfg.parse({"TOKEN": "tok-123", "retries": "5", "rate": "1,5", "sandbox": "да"})
    assert cfg.token == "tok-123" and cfg.shop is None and cfg.base == "https://api.example/v1"
    assert cfg.retries == 5 and cfg.rate == 1.5 and cfg.sandbox is True and cfg.network == "TON"
    assert "tok-123" not in repr(cfg) and "***" in repr(cfg)
    assert cfg.secret_values() == ["tok-123"]
    assert cfg == Cfg(token="tok-123", retries=5, rate=1.5, sandbox=True) and hash(cfg)
    with pytest.raises(AttributeError):
        cfg.token = "x"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        _ = cfg.missing
    fields = Cfg.fields()
    assert list(fields) == ["token", "shop", "base", "retries", "rate", "sandbox", "network"]
    assert fields["token"].is_secret and fields["token"].where == "кабинет → API"
    assert fields["retries"].env_suffix == "RETRIES"


@pytest.mark.parametrize(
    "value",
    [
        "https://api.rollypay.io/api/v1",
        "https://10.0.0.5:8443/api",
        "http://127.0.0.1:5000/api",
        "http://localhost:8080",
        "http://svbg.localhost/x",
        "http://[::1]:9000/",
    ],
)
def test_url_field_accepts_https_and_local_http(value: str) -> None:
    assert Cfg(token="t", base=value).base == value


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("http://api.rollypay.io/api/v1", "ожидался адрес"),  # the API key would travel in clear text
        ("http://10.0.0.5/api", "ожидался адрес"),
        ("https://user:pass@api.example/v1", "ожидался адрес"),
        ("https://api.example:99999/v1", "ожидался адрес"),
        ("https:///v1", "ожидался адрес"),
        ("https://169.254.169.254/latest/meta-data", "служебный адрес"),
        ("https://[fe80::1]/", "служебный адрес"),
        ("https://[::ffff:169.254.169.254]/", "служебный адрес"),
        ("https://metadata.google.internal/computeMetadata", "служебный адрес"),
        ("https://0.0.0.0/", "служебный адрес"),
    ],
    ids=[
        "http-remote",
        "http-private",
        "credentials",
        "bad-port",
        "no-host",
        "metadata-ip",
        "link-local-v6",
        "mapped-metadata",
        "metadata-name",
        "unspecified",
    ],
)
def test_url_field_refuses_clear_text_and_internal_addresses(value: str, reason: str) -> None:
    with pytest.raises(ConfigError) as err:
        Cfg(token="t", base=value)
    assert reason in err.value.errors["base"]


def test_status_currency_is_normalized() -> None:
    status = ProviderStatus(state=PaymentState.PAID, external_id="e", currency=" rub ")
    assert status.currency == "RUB"


def test_config_model_errors_are_per_field_and_russian() -> None:
    with pytest.raises(ConfigError) as err:
        Cfg.parse({"retries": "11", "rate": "abc", "sandbox": "maybe", "network": "BTC", "base": "ftp://x"})
    errors = err.value.errors
    assert errors["token"] == "обязательное поле"
    assert errors["retries"] == "не больше 10"
    assert errors["rate"] == "ожидалось число"
    assert errors["sandbox"] == "ожидалось true или false"
    assert errors["network"].startswith("допустимые значения")
    assert errors["base"].startswith("ожидался адрес")
    with pytest.raises(ConfigError, match="неизвестное поле"):
        Cfg(token="t", nope=1)
    with pytest.raises(ConfigError):
        Cfg(token="t", rate="nan")
    with pytest.raises(ConfigError):
        Cfg(token="t", retries=True)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("179", "179"),
        ("179.00", "179"),
        (179, "179"),
        (179.0, "179"),
        ("179,5", "179.5"),
        (Decimal("1.10"), "1.1"),
        ("0.1", "0.1"),
        (0.1, "0.1"),
    ],
    ids=["str-int", "str-dec", "int", "float", "comma", "decimal", "small", "float-small"],
)
def test_parse_amount(raw: object, expected: str) -> None:
    assert parse_amount(raw) == Decimal(expected)


@pytest.mark.parametrize("raw", ["", "abc", "-1", "NaN", "Infinity", None, True, "1,234.5"], ids=str)
def test_parse_amount_rejects(raw: object) -> None:
    with pytest.raises((ValueError, TypeError)):
        parse_amount(raw)
    with pytest.raises(TypeError):
        parse_amount([1])


def test_parse_timestamp_forms() -> None:
    at = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
    secs = int(at.timestamp())
    for raw in (
        secs,
        str(secs),
        secs * 1000,
        str(secs * 1000),
        f"{secs}.0",
        "2026-10-01T12:00:00",
        "2026-10-01T12:00:00Z",
        "2026-10-01T15:00:00+03:00",
        "2026-10-01 12:00:00",
    ):
        assert parse_timestamp(raw) == at, raw
    msk = parse_timestamp(datetime(2026, 10, 1, 15, tzinfo=timezone(timedelta(hours=3))).isoformat())
    assert msk.tzinfo is UTC
    for bad in ("yesterday", None, True, "", [1]):
        with pytest.raises(ValueError):
            parse_timestamp(bad)


def test_signature_helpers() -> None:
    assert hmac_sha256_hex("key", b"The quick brown fox jumps over the lazy dog") == (
        "f7bc83f430538424b13298e6aa6fb143ef4d59a14946175997479dbc2d1a3cd8"
    )
    assert constant_time_equal("abc", "abc") and constant_time_equal(b"abc", "abc")
    assert (
        not constant_time_equal("abc", "abd")
        and not constant_time_equal("", "")
        and not constant_time_equal(None, "a")
    )


def test_intent_amount_text() -> None:
    intent = PaymentIntent(
        payment_id="p", amount_minor=17_900, currency="RUB", description="d", customer_ref="c"
    )
    assert intent.amount == Decimal("179") and intent.amount_text() == "179.00"
    stars = PaymentIntent(payment_id="p", amount_minor=100, currency="XTR", description="d", customer_ref="c")
    assert stars.amount_text() == "100"


def test_checkout_validation_and_json() -> None:
    exp = datetime(2026, 10, 1, tzinfo=UTC)
    co = Checkout(kind="url", external_id="e", pay_url="https://p/1", expires_at=exp)
    assert co.as_json() == {
        "kind": "url",
        "pay_url": "https://p/1",
        "invoice": None,
        "details": None,
        "expires_at": exp.isoformat(),
    }
    for bad in (
        {"kind": "url", "pay_url": "javascript:alert(1)"},
        {"kind": "invoice"},
        {"kind": "details", "details": "  "},
        {"kind": "url", "pay_url": "https://p", "external_id": ""},
        {"kind": "url", "pay_url": "https://p", "expires_at": datetime(2026, 1, 1)},
    ):
        with pytest.raises(ValueError):
            Checkout(**bad)  # type: ignore[arg-type]


def test_status_and_event_validation() -> None:
    st = ProviderStatus(state="paid", external_id="e", amount=Decimal("1"), currency="rub")  # type: ignore[arg-type]
    assert st.state is PaymentState.PAID and st.currency == "RUB"
    with pytest.raises(ValueError, match="external_id or payment_id"):
        ProviderStatus(state=PaymentState.PAID)
    with pytest.raises(TypeError):
        ProviderStatus(state=PaymentState.PAID, external_id="e", amount=1.0)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ProviderEvent(state=PaymentState.PAID, external_id="e", signed_at=datetime(2026, 1, 1))
    ev = ProviderEvent(state=PaymentState.PAID, payment_id="p", summary={"a": 1})
    with pytest.raises(TypeError):
        ev.summary["b"] = 2  # type: ignore[index]


def test_webhook_request_and_response() -> None:
    req = WebhookRequest(body=b'{"a": 1}', headers=[("X-Sig", "1"), ("x-sig", "2")])
    assert req.header("x-SIG") == "1" and "X-SIG" in req.headers and req.json() == {"a": 1}
    with pytest.raises(WebhookRejected) as err:
        WebhookRequest(body=b"\xff").json()
    assert err.value.status == 400
    assert WebhookResponse.json({"ok": True}).body == b'{"ok": true}'
    assert WebhookResponse.ok("YES").body == b"YES"


def test_enums() -> None:
    assert SDK_VERSION == "1.0-beta"
    assert WebhookAuth.SIGNATURE.is_weak is False
    assert all(a.is_weak for a in (WebhookAuth.SECRET_HEADER, WebhookAuth.IP_ONLY, WebhookAuth.NONE))
    assert {k.value for k in MethodKind} == {
        "sbp",
        "card",
        "intl_card",
        "crypto",
        "stars",
        "wallet",
        "manual",
    }
