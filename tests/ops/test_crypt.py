"""Password encryption of backups: round trip, wrong password, tampering, truncation, hostile headers."""

from __future__ import annotations

import base64
import io
import json
import struct

import pytest

from svbg.ops.crypt import (
    MAGIC,
    BackupCryptoError,
    DecryptingReader,
    EncryptingWriter,
    KdfParams,
    WrongPasswordError,
    decrypt_bytes,
    encrypt_bytes,
    is_encrypted_head,
)
from tests.ops.conftest import FAST_KDF, PASSWORD

CHUNK = 64


def _encrypt(data: bytes, *, chunk: int = CHUNK, password: str = PASSWORD) -> bytes:
    out = io.BytesIO()
    with EncryptingWriter(out, password, chunk=chunk, kdf=FAST_KDF) as w:
        # Uneven writes: frames must not depend on how the caller slices the data.
        for i in range(0, len(data), 37):
            w.write(data[i : i + 37])
    return out.getvalue()


def _read_all(blob: bytes, password: str = PASSWORD) -> bytes:
    reader = io.BufferedReader(DecryptingReader(io.BytesIO(blob), password))
    return reader.read()


def _frames(blob: bytes) -> tuple[bytes, list[bytes]]:
    """Split an encrypted blob into the header part and the raw frames."""
    (hlen,) = struct.unpack(">H", blob[8:10])
    pos = 10 + hlen
    head, frames = blob[:pos], []
    while pos < len(blob):
        (n,) = struct.unpack(">I", blob[pos : pos + 4])
        frames.append(blob[pos : pos + 4 + n])
        pos += 4 + n
    return head, frames


@pytest.mark.parametrize("size", [0, 1, CHUNK - 1, CHUNK, CHUNK + 1, CHUNK * 5, 1000])
def test_round_trip(size: int) -> None:
    data = bytes(range(256)) * (size // 256 + 1)
    data = data[:size]
    blob = _encrypt(data)
    assert is_encrypted_head(blob)
    assert size < 16 or data not in blob
    assert _read_all(blob) == data


def test_bytes_helpers() -> None:
    blob = encrypt_bytes(b"SECRET_KEY=abc\n", PASSWORD, kdf=FAST_KDF)
    assert decrypt_bytes(blob, PASSWORD) == b"SECRET_KEY=abc\n"
    with pytest.raises(BackupCryptoError):
        decrypt_bytes(encrypt_bytes(b"x" * 100, PASSWORD, kdf=FAST_KDF), PASSWORD, limit=10)


def test_wrong_password_is_told_apart() -> None:
    blob = _encrypt(b"data" * 100)
    with pytest.raises(WrongPasswordError, match="неверный пароль"):
        _read_all(blob, "another password")
    with pytest.raises(WrongPasswordError):
        _read_all(blob, "")


def test_salt_is_random() -> None:
    assert _encrypt(b"same") != _encrypt(b"same")


def test_truncated_file_is_rejected() -> None:
    blob = _encrypt(b"z" * (CHUNK * 4))
    head, frames = _frames(blob)
    without_final = head + b"".join(frames[:-1])
    with pytest.raises(BackupCryptoError, match="обрезан"):
        _read_all(without_final)
    with pytest.raises(BackupCryptoError, match="обрезан"):
        _read_all(blob[:-5])


def test_reordered_and_duplicated_frames_are_rejected() -> None:
    blob = _encrypt(b"q" * (CHUNK * 4))
    head, frames = _frames(blob)
    swapped = head + frames[1] + frames[0] + b"".join(frames[2:])
    with pytest.raises(BackupCryptoError, match="перепутаны"):
        _read_all(swapped)
    duplicated = head + frames[0] + frames[0] + b"".join(frames[1:])
    with pytest.raises(BackupCryptoError, match="перепутаны"):
        _read_all(duplicated)


def test_flipped_byte_is_rejected() -> None:
    blob = bytearray(_encrypt(b"w" * (CHUNK * 3)))
    blob[len(blob) // 2] ^= 0x01
    with pytest.raises(BackupCryptoError, match="повреждён"):
        _read_all(bytes(blob))


def test_trailing_garbage_is_rejected() -> None:
    blob = _encrypt(b"e" * 10)
    with pytest.raises(BackupCryptoError, match="лишние данные"):
        _read_all(blob + b"\0" * 8)


def test_spliced_from_another_file_is_rejected() -> None:
    a_head, a_frames = _frames(_encrypt(b"a" * (CHUNK * 3)))
    _b_head, b_frames = _frames(_encrypt(b"b" * (CHUNK * 3)))
    with pytest.raises(BackupCryptoError):
        _read_all(a_head + a_frames[0] + b_frames[1] + a_frames[2] + a_frames[3])


def _with_header(header: dict[str, object]) -> bytes:
    raw = json.dumps(header).encode()
    return MAGIC + struct.pack(">H", len(raw)) + raw


@pytest.mark.parametrize(
    "header",
    [
        {"v": 1, "kdf": "scrypt", "n": 2**22, "r": 8, "p": 1, "salt": "A" * 22 + "==", "check": "x",
         "chunk": 64},
        {"v": 1, "kdf": "scrypt", "n": 1024, "r": 64, "p": 1, "salt": "A" * 22 + "==", "check": "x",
         "chunk": 64},
        {"v": 1, "kdf": "scrypt", "n": 1000, "r": 8, "p": 1, "salt": "A" * 22 + "==", "check": "x",
         "chunk": 64},
        {"v": 1, "kdf": "scrypt", "n": 1024, "r": 8, "p": 1, "salt": "AA==", "check": "x", "chunk": 64},
        {"v": 1, "kdf": "scrypt", "n": 1024, "r": 8, "p": 1, "salt": "AAAAAAAAAAAAAAAAAAAAAA==", "check": "x",
         "chunk": 10**9},
        {"v": 1, "kdf": "scrypt"},
    ],
)  # fmt: skip
def test_hostile_headers_are_refused_cheaply(header: dict[str, object]) -> None:
    with pytest.raises(BackupCryptoError, match="заголовок"):
        DecryptingReader(io.BytesIO(_with_header(header)), PASSWORD)


def test_unknown_version_and_foreign_files() -> None:
    with pytest.raises(BackupCryptoError, match="версия формата"):
        DecryptingReader(io.BytesIO(_with_header({"v": 2, "kdf": "scrypt"})), PASSWORD)
    with pytest.raises(BackupCryptoError, match="не зашифрованный бэкап"):
        DecryptingReader(io.BytesIO(b"\x1f\x8b\x08rest"), PASSWORD)
    assert not is_encrypted_head(b"\x1f\x8b")


def test_oversized_frame_is_refused() -> None:
    blob = _encrypt(b"r" * 10)
    head, _ = _frames(blob)
    with pytest.raises(BackupCryptoError, match="размер блока"):
        _read_all(head + struct.pack(">I", 10**8) + b"x")


def test_kdf_params_are_validated() -> None:
    with pytest.raises(ValueError):
        KdfParams(n=3)
    with pytest.raises(ValueError):
        KdfParams(n=2**10, r=0)
    assert KdfParams().maxmem() > 32 * 1024 * 1024


def test_frames_are_raw_tokens_not_base64() -> None:
    """No base64 inflation on disk: a 64-byte chunk costs ~one Fernet overhead, not 4/3 of the data."""
    blob = _encrypt(b"\0" * (CHUNK * 100))
    _head, frames = _frames(blob)
    assert all(len(f) < CHUNK + 120 for f in frames)
    with pytest.raises(ValueError):
        base64.b64decode(frames[0][4:], validate=True)
