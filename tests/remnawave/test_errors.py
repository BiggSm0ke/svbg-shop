"""Error classification of panel responses (02 §2.5, §8.2 A «Классификатор ошибок»)."""

from __future__ import annotations

import json

import pytest

from svbg.core.errors import classify
from svbg.core.errors.classify import Severity
from svbg.core.log import register_secret
from svbg.remnawave.errors import (
    ErrorKind,
    PanelNotConfiguredError,
    PanelUnavailableError,
    RemnawaveError,
    WriteBlockedError,
    error_from_response,
    install_classifiers,
)


def body(code: str | None = None, message: str = "x", **extra: object) -> bytes:
    data: dict[str, object] = {"timestamp": "2026-10-01T00:00:00.000Z", "path": "/api/x", "message": message}
    if code is not None:
        data["errorCode"] = code
    data.update(extra)
    return json.dumps(data).encode()


def zod(message: str, path: list[str]) -> bytes:
    return json.dumps(
        {
            "message": "Validation failed",
            "statusCode": 400,
            "errors": [{"validation": "", "code": "custom", "message": message, "path": path}],
        }
    ).encode()


@pytest.mark.parametrize("code", ["A025", "A063"])
def test_user_not_found_codes_on_user_methods(code: str) -> None:
    err = error_from_response(404, body(code), user_scoped=True)
    assert err.kind is ErrorKind.NOT_FOUND
    assert err.code == code


@pytest.mark.parametrize("code", ["A118", "A182", "A204", None])
def test_other_404_on_user_methods_is_not_user_missing(code: str | None) -> None:
    # Bedolaga #3277: a missing squad must never look like "the user is gone".
    err = error_from_response(404, body(code), user_scoped=True)
    assert err.kind is not ErrorKind.NOT_FOUND
    assert err.kind is ErrorKind.VALIDATION
    assert "не значит, что пользователя нет" in err.hint_ru


def test_404_on_non_user_method_is_not_found() -> None:
    assert error_from_response(404, body("A204"), user_scoped=False).kind is ErrorKind.NOT_FOUND


def test_401_is_auth_and_403_is_scope_with_scope_in_hint() -> None:
    assert error_from_response(
        401, b'{"message":"Unauthorized","statusCode":401}', user_scoped=True
    ).kind is (ErrorKind.AUTH)
    err = error_from_response(403, body("E000", "Forbidden"), user_scoped=True, scope="users:create")
    assert err.kind is ErrorKind.FORBIDDEN_SCOPE
    assert err.code is None  # E000 is the masked generic code
    assert "users:create" in err.hint_ru


@pytest.mark.parametrize(("code", "kind"), [("A019", ErrorKind.CONFLICT), ("A020", ErrorKind.CONFLICT)])
def test_conflicts(code: str, kind: ErrorKind) -> None:
    assert error_from_response(400, body(code), user_scoped=True).kind is kind


@pytest.mark.parametrize("code", ["A029", "A030"])
def test_already_is_success_like(code: str) -> None:
    err = error_from_response(400, body(code), user_scoped=True)
    assert err.kind is ErrorKind.ALREADY
    assert err.is_success_like
    assert not err.retryable


def test_zod_validation_and_expire_in_past() -> None:
    err = error_from_response(
        400, zod("Expiration date cannot be in the past", ["expireAt"]), user_scoped=True
    )
    assert err.kind is ErrorKind.VALIDATION
    assert err.is_expire_in_past
    assert err.issues[0].path == "expireAt"
    other = error_from_response(400, zod("Invalid uuid", ["activeInternalSquads", "0"]), user_scoped=True)
    assert not other.is_expire_in_past
    assert other.issues[0].path == "activeInternalSquads.0"


@pytest.mark.parametrize("code", ["A018", "A039", None])
def test_500_is_server(code: str | None) -> None:
    err = error_from_response(500, body(code), user_scoped=True)
    assert err.kind is ErrorKind.SERVER
    assert not err.retryable


@pytest.mark.parametrize("status", [502, 503, 504])
def test_gateway_errors_are_transient(status: int) -> None:
    err = error_from_response(status, b"<html>bad gateway</html>", user_scoped=False)
    assert err.kind is ErrorKind.TRANSIENT
    assert err.retryable


def test_429_keeps_retry_after() -> None:
    err = error_from_response(429, b"", user_scoped=False, retry_after=7.0)
    assert err.kind is ErrorKind.TRANSIENT
    assert err.retry_after == 7.0


def test_redirect_means_access_layer() -> None:
    err = error_from_response(302, b"", user_scoped=False)
    assert err.kind is ErrorKind.AUTH
    assert "Cloudflare" in err.hint_ru


def test_garbage_body_does_not_break_classification() -> None:
    assert error_from_response(400, b"\xff\xfe not json", user_scoped=True).kind is ErrorKind.VALIDATION
    assert error_from_response(404, b"[1,2,3]", user_scoped=True).kind is ErrorKind.VALIDATION


def test_message_is_masked_truncated_and_body_not_included() -> None:
    secret = "sk_live_very_secret_value_123456"
    register_secret(secret)
    err = error_from_response(
        400, body("A089", f"bad {secret} " + "x" * 1000, trojanPassword="pw-123"), user_scoped=True
    )
    text = str(err)
    assert secret not in text
    assert "pw-123" not in text
    assert len(err.message) <= 300


def test_str_contains_method_path_and_kind() -> None:
    err = error_from_response(404, body("A063"), user_scoped=True, method="GET", path="/users/5")
    assert str(err).startswith("Remnawave not_found 404 A063 GET /users/5")


def test_special_errors() -> None:
    unavailable = PanelUnavailableError(method="GET", path="/users/1", retry_in=12)
    assert unavailable.kind is ErrorKind.TRANSIENT and unavailable.code == "BREAKER_OPEN"
    assert PanelNotConfiguredError().kind is ErrorKind.AUTH
    blocked = WriteBlockedError("4.0.0")
    assert blocked.kind is ErrorKind.TRANSIENT and "4.0.0" in blocked.message


def test_error_hub_classifiers_say_what_to_check() -> None:
    install_classifiers()  # idempotent
    auth = classify(RemnawaveError(ErrorKind.AUTH, 401))
    assert "токен" in auth.title_ru.lower()
    assert "REMNAWAVE_TOKEN" in auth.hint_ru
    proxy = classify(RemnawaveError(ErrorKind.PROXY_CHECK))
    assert "http://remnawave:3000" in proxy.hint_ru
    transient = classify(RemnawaveError(ErrorKind.TRANSIENT, 503))
    assert transient.severity is Severity.WARN
    assert "приостановлены" in classify(PanelUnavailableError()).title_ru
    assert "не подключена" in classify(PanelNotConfiguredError()).title_ru
    assert "заблокирована" in classify(WriteBlockedError("4.0.0")).title_ru


def test_unregister_and_reinstall() -> None:
    unregister = install_classifiers()
    unregister()
    assert classify(RemnawaveError(ErrorKind.AUTH, 401)).title_ru == "Непредвиденная ошибка"
    install_classifiers()
    assert "токен" in classify(RemnawaveError(ErrorKind.AUTH, 401)).title_ru.lower()
