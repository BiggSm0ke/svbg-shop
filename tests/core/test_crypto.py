from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from cryptography.fernet import Fernet

from svbg.core import log
from svbg.core.crypto import PREFIX, Crypto, CryptoError, fingerprint, generate_key, is_encrypted


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    yield
    log.SecretRegistry.clear()


def test_generate_key_is_valid_fernet_key() -> None:
    key = generate_key()
    assert isinstance(key, str)
    assert len(key) == 44
    Fernet(key)  # does not raise
    assert generate_key() != key


def test_encrypt_decrypt_roundtrip() -> None:
    c = Crypto([generate_key()])
    for plain in ("", "s3cr3t-token", "пароль 🔑", "x" * 10_000):
        enc = c.encrypt(plain)
        assert enc.startswith("enc:v1:")
        assert is_encrypted(enc)
        assert c.is_encrypted(enc)
        assert plain not in enc[len(PREFIX) :] or plain == ""
        assert c.decrypt(enc) == plain


def test_encryption_is_randomized() -> None:
    c = Crypto([generate_key()])
    assert c.encrypt("same") != c.encrypt("same")


def test_plain_values_pass_through() -> None:
    c = Crypto([generate_key()])
    assert c.decrypt("plain value") == "plain value"
    assert c.decrypt("") == ""
    assert c.decrypt("enc:v2:something") == "enc:v2:something"
    assert not is_encrypted("plain")
    assert not is_encrypted(None)
    assert not is_encrypted(123)


def test_wrong_key_is_loud_and_never_returns_ciphertext() -> None:
    enc = Crypto([generate_key()]).encrypt("secret-value")
    other = Crypto([generate_key()])
    with pytest.raises(CryptoError, match="SECRET_KEY") as info:
        other.decrypt(enc)
    assert enc[len(PREFIX) :] not in str(info.value)
    assert "secret-value" not in str(info.value)


@pytest.mark.parametrize("garbage", ["enc:v1:", "enc:v1:not-a-token", "enc:v1:пусто", "enc:v1:gAAAAA=="])
def test_corrupted_values_raise(garbage: str) -> None:
    c = Crypto([generate_key()])
    with pytest.raises(CryptoError):
        c.decrypt(garbage)


def test_multifernet_rotation() -> None:
    old_key, new_key = generate_key(), generate_key()
    old = Crypto([old_key])
    enc_old = old.encrypt("payment-secret")

    rotated = Crypto([new_key, old_key])
    assert rotated.decrypt(enc_old) == "payment-secret"
    re_enc = rotated.rotate(enc_old)
    assert re_enc != enc_old and re_enc.startswith(PREFIX)

    only_new = Crypto([new_key])
    assert only_new.decrypt(re_enc) == "payment-secret"
    with pytest.raises(CryptoError):
        only_new.decrypt(enc_old)
    # Rotation is idempotent: rotating an already-rotated value still decrypts.
    assert only_new.decrypt(rotated.rotate(re_enc)) == "payment-secret"
    # New values are always encrypted with the first key.
    assert only_new.decrypt(rotated.encrypt("fresh")) == "fresh"


def test_rotate_plain_and_undecryptable() -> None:
    c = Crypto([generate_key()])
    assert c.rotate("plain") == "plain"
    with pytest.raises(CryptoError):
        c.rotate(Crypto([generate_key()]).encrypt("x"))


def test_blank_and_duplicate_keys_are_skipped() -> None:
    key = generate_key()
    c = Crypto([key, "", None, f"  {key}  "])
    assert c.fingerprints == (fingerprint(key),)


@pytest.mark.parametrize("keys", [[], [""], [None], ["   "]])
def test_no_keys(keys: list[str | None]) -> None:
    with pytest.raises(CryptoError, match="no encryption key"):
        Crypto(keys)


@pytest.mark.parametrize(
    "bad",
    ["short", "x" * 44, "пароль-ключ", "Owu-3PXolKpp8x8MaXsp26ShYlL0LLpulnpLzHQwi5"],  # last: truncated
)
def test_invalid_key_error_does_not_leak_key(bad: str) -> None:
    with pytest.raises(CryptoError) as info:
        Crypto([generate_key(), bad])
    assert "key #2" in str(info.value)
    assert bad not in str(info.value)


def test_single_string_is_rejected() -> None:
    with pytest.raises(TypeError):
        Crypto(generate_key())


def test_fingerprint_shape_and_stability() -> None:
    key = generate_key()
    fp = fingerprint(key)
    assert re.fullmatch(r"[0-9a-f]{8}", fp)
    assert fingerprint(key) == fp
    assert fingerprint(generate_key()) != fp
    c = Crypto([key])
    assert c.primary_fingerprint == fp
    assert key not in repr(c)
    assert fp in repr(c)


def test_value_fingerprint() -> None:
    c = Crypto([generate_key()])
    fp = c.value_fingerprint("token-1")
    assert re.fullmatch(r"[0-9a-f]{8}", fp)
    assert c.value_fingerprint("token-1") == fp
    assert c.value_fingerprint("token-2") != fp
    # Keyed: another key gives another fingerprint for the same value.
    assert Crypto([generate_key()]).value_fingerprint("token-1") != fp


def test_keys_are_registered_for_log_masking() -> None:
    key = generate_key()
    Crypto([key])
    assert log.mask(f"SECRET_KEY is {key}") == "SECRET_KEY is ***"


def test_type_errors() -> None:
    c = Crypto([generate_key()])
    with pytest.raises(TypeError):
        c.encrypt(b"bytes")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        c.decrypt(None)  # type: ignore[arg-type]
