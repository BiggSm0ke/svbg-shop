"""Encryption of secrets at rest (settings, payment keys) — MultiFernet with an ``enc:v1:`` prefix.

* The first key encrypts; every key decrypts (key rotation: ``Crypto([new, previous])``).
* Decryption failure is loud: :class:`CryptoError`, never the ciphertext as a value (03 §6.1).
* Values without the prefix are plain text and returned unchanged (legacy/unencrypted rows).
* Keys are registered in :class:`svbg.core.log.SecretRegistry`, so they cannot leak into logs.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
from collections.abc import Iterable

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from svbg.core.log import register_secret

__all__ = ["PREFIX", "Crypto", "CryptoError", "fingerprint", "generate_key", "is_encrypted"]

PREFIX = "enc:v1:"
_FP_DOMAIN = b"svbg:key-fingerprint:v1:"
_HMAC_DOMAIN = b"svbg:value-fingerprint:v1"


class CryptoError(Exception):
    """Invalid key material or a value that cannot be decrypted with the configured keys."""


def generate_key() -> str:
    """A new random Fernet key (url-safe base64, 44 chars)."""
    return Fernet.generate_key().decode("ascii")


def fingerprint(key: str) -> str:
    """Short public identifier of a key (8 hex chars); reveals nothing usable about the key."""
    return hashlib.sha256(_FP_DOMAIN + key.strip().encode("utf-8")).hexdigest()[:8]


def is_encrypted(value: object) -> bool:
    """True for strings produced by :meth:`Crypto.encrypt`."""
    return isinstance(value, str) and value.startswith(PREFIX)


def _load_key(key: str, index: int) -> tuple[Fernet, bytes]:
    raw = b""
    fernet: Fernet | None = None
    try:
        raw = base64.urlsafe_b64decode(key.encode("ascii"))
        fernet = Fernet(key.encode("ascii"))  # validates length (32 bytes) itself
    except (ValueError, binascii.Error, TypeError):
        pass
    if fernet is None or len(raw) != 32:
        # Never echo the key itself: only its position and fingerprint.
        raise CryptoError(
            f"key #{index + 1} (fp {fingerprint(key)}) is not a valid Fernet key "
            "(expected 32 bytes in url-safe base64, see `svbg show-key`)"
        )
    return fernet, raw


class Crypto:
    """MultiFernet wrapper. ``Crypto([SECRET_KEY, SECRET_KEY_PREVIOUS])``; blank entries are skipped."""

    __slots__ = ("_fernet", "_fingerprints", "_hmac_key")

    def __init__(self, keys: Iterable[str | None]) -> None:
        if isinstance(keys, str):
            raise TypeError("Crypto expects a list of keys, not a single string")
        cleaned: list[str] = []
        for key in keys:
            if key is None:
                continue
            k = key.strip()
            if k and k not in cleaned:
                cleaned.append(k)
        if not cleaned:
            raise CryptoError("no encryption key configured (SECRET_KEY is empty)")
        loaded = [_load_key(k, i) for i, k in enumerate(cleaned)]
        for k in cleaned:
            register_secret(k)
        self._fernet = MultiFernet([f for f, _ in loaded])
        self._fingerprints = tuple(fingerprint(k) for k in cleaned)
        # Separate subkey for value fingerprints (audit), derived from the primary key.
        self._hmac_key = hmac.new(loaded[0][1], _HMAC_DOMAIN, hashlib.sha256).digest()

    def __repr__(self) -> str:
        return f"Crypto(keys={list(self._fingerprints)})"

    @property
    def primary_fingerprint(self) -> str:
        return self._fingerprints[0]

    @property
    def fingerprints(self) -> tuple[str, ...]:
        return self._fingerprints

    def encrypt(self, plaintext: str) -> str:
        """Encrypt with the primary key → ``"enc:v1:<fernet token>"``."""
        if not isinstance(plaintext, str):
            raise TypeError("encrypt expects str")
        token = self._fernet.encrypt(plaintext.encode("utf-8"))
        return PREFIX + token.decode("ascii")

    def decrypt(self, value: str) -> str:
        """Plain values are returned as-is; ``enc:v1:`` values are decrypted or raise :class:`CryptoError`."""
        if not isinstance(value, str):
            raise TypeError("decrypt expects str")
        if not value.startswith(PREFIX):
            return value
        try:
            data = self._fernet.decrypt(value[len(PREFIX) :].encode("ascii"))
            return data.decode("utf-8")
        except (InvalidToken, UnicodeError):
            raise CryptoError(
                "value cannot be decrypted with the configured keys "
                f"(primary fp {self.primary_fingerprint}); SECRET_KEY probably changed — re-enter the value"
            ) from None

    def rotate(self, value: str) -> str:
        """Re-encrypt an ``enc:v1:`` value under the primary key; plain values are returned unchanged."""
        if not is_encrypted(value):
            return value
        try:
            token = self._fernet.rotate(value[len(PREFIX) :].encode("ascii"))
        except (InvalidToken, UnicodeError):
            raise CryptoError(
                "value cannot be re-encrypted: no configured key decrypts it "
                f"(primary fp {self.primary_fingerprint})"
            ) from None
        return PREFIX + token.decode("ascii")

    def is_encrypted(self, value: object) -> bool:
        return is_encrypted(value)

    def value_fingerprint(self, value: str) -> str:
        """Keyed 8-hex fingerprint of a secret value for audit: shows *that* it changed, not *to what*."""
        return hmac.new(self._hmac_key, value.encode("utf-8"), hashlib.sha256).hexdigest()[:8]
