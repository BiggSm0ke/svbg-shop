"""Password encryption of backups: scrypt → Fernet, streamed in authenticated chunks.

File format (all integers big-endian)::

    b"SVBGENC1"                       magic, 8 bytes
    u16 header_len, header (JSON)     {"v": 1, "kdf": "scrypt", "n", "r", "p", "salt", "check", "chunk"}
    frames: u32 len, token            token = the raw bytes of one Fernet token (base64-decoded)

The plaintext of every frame is ``u64 seq | u8 final | data``: frames must come in order starting at 0 and
the last one carries ``final = 1``, so a truncated, reordered or spliced file is rejected, not silently
restored short. Each token is authenticated by Fernet (AES-128-CBC + HMAC-SHA256). ``check`` is a keyed hash
of the derived key: it tells a wrong password from a damaged file without weakening scrypt.

Reading is streaming with bounded memory: the KDF parameters and frame sizes of the header are capped, so a
crafted file cannot make the restore allocate gigabytes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import struct
from typing import IO, Any, Final

from cryptography.fernet import Fernet, InvalidToken

__all__ = [
    "CHUNK",
    "MAGIC",
    "BackupCryptoError",
    "DecryptingReader",
    "EncryptingWriter",
    "KdfParams",
    "WrongPasswordError",
    "decrypt_bytes",
    "encrypt_bytes",
    "is_encrypted_head",
]

MAGIC: Final = b"SVBGENC1"
CHUNK: Final = 1024 * 1024
_MAX_CHUNK: Final = 8 * 1024 * 1024
_MAX_HEADER: Final = 4096
_FRAME_OVERHEAD: Final = 1024  # Fernet: version + timestamp + IV + padding + HMAC; plus our 9-byte prefix
_PREFIX: Final = struct.Struct(">QB")
_LEN: Final = struct.Struct(">I")
_HLEN: Final = struct.Struct(">H")
_CHECK_DOMAIN: Final = b"svbg:backup-password-check:v1"
# Upper bounds for parameters read from a file (n = 2**20 with r = 8 needs 1 GiB: refused).
_MAX_N: Final = 2**18
_MAX_R: Final = 16
_MAX_P: Final = 4


class BackupCryptoError(Exception):
    """The file is not an encrypted backup or is damaged. ``str()`` is owner-facing (Russian)."""


class WrongPasswordError(BackupCryptoError):
    """The password does not match the one the file was encrypted with."""


class KdfParams:
    """scrypt cost: ``n`` (power of two), ``r``, ``p``. The default needs 32 MiB and ~0.1 s."""

    __slots__ = ("n", "p", "r")

    def __init__(self, n: int = 2**15, r: int = 8, p: int = 1) -> None:
        if n < 2 or n & (n - 1) or n > _MAX_N or not 1 <= r <= _MAX_R or not 1 <= p <= _MAX_P:
            raise ValueError("invalid scrypt parameters")
        self.n, self.r, self.p = n, r, p

    def maxmem(self) -> int:
        return 128 * self.r * (self.n + self.p + 2) + 1024 * 1024


def _derive(password: str, salt: bytes, kdf: KdfParams) -> bytes:
    if not password:
        raise ValueError("password is empty")
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=kdf.n, r=kdf.r, p=kdf.p, maxmem=kdf.maxmem(), dklen=32
    )


def _check(key: bytes) -> str:
    return hmac.new(key, _CHECK_DOMAIN, hashlib.sha256).hexdigest()[:32]


def is_encrypted_head(head: bytes) -> bool:
    """True if ``head`` (the first bytes of a file) starts with the encrypted-backup magic."""
    return head[: len(MAGIC)] == MAGIC


class EncryptingWriter(io.RawIOBase):
    """Write-only stream: plaintext in, encrypted frames out to ``raw``. ``close()`` writes the final frame
    (``raw`` itself is not closed)."""

    def __init__(
        self, raw: IO[bytes], password: str, *, chunk: int = CHUNK, kdf: KdfParams | None = None
    ) -> None:
        super().__init__()
        if not 1 <= chunk <= _MAX_CHUNK:
            raise ValueError("chunk size out of range")
        kdf = kdf or KdfParams()
        salt = os.urandom(16)
        key = _derive(password, salt, kdf)
        self._fernet = Fernet(base64.urlsafe_b64encode(key))
        self._raw = raw
        self._chunk = chunk
        self._buf = bytearray()
        self._seq = 0
        header = json.dumps(
            {
                "v": 1,
                "kdf": "scrypt",
                "n": kdf.n,
                "r": kdf.r,
                "p": kdf.p,
                "salt": base64.b64encode(salt).decode("ascii"),
                "check": _check(key),
                "chunk": chunk,
            },
            separators=(",", ":"),
        ).encode("ascii")
        raw.write(MAGIC + _HLEN.pack(len(header)) + header)

    def writable(self) -> bool:
        return True

    def write(self, data: Any) -> int:
        if self.closed:
            raise ValueError("write to a closed stream")
        view = memoryview(data).cast("B")
        self._buf += view
        while len(self._buf) >= self._chunk:
            piece = bytes(self._buf[: self._chunk])
            del self._buf[: self._chunk]
            self._frame(piece, final=False)
        return len(view)

    def _frame(self, data: bytes, *, final: bool) -> None:
        token = self._fernet.encrypt(_PREFIX.pack(self._seq, 1 if final else 0) + data)
        raw = base64.urlsafe_b64decode(token)
        self._raw.write(_LEN.pack(len(raw)) + raw)
        self._seq += 1

    def close(self) -> None:
        if self.closed:
            return
        try:
            self._frame(bytes(self._buf), final=True)
            self._buf.clear()
            self._raw.flush()
        finally:
            super().close()


def _read_exact(raw: IO[bytes], n: int) -> bytes:
    out = bytearray()
    while len(out) < n:
        piece = raw.read(n - len(out))
        if not piece:
            break
        out += piece
    return bytes(out)


class DecryptingReader(io.RawIOBase):
    """Read-only stream over an encrypted file: verifies every frame, the order and the final marker."""

    def __init__(self, raw: IO[bytes], password: str) -> None:
        super().__init__()
        self._raw = raw
        magic = _read_exact(raw, len(MAGIC))
        if magic != MAGIC:
            raise BackupCryptoError("это не зашифрованный бэкап SvBG (неизвестный формат файла)")
        hraw = _read_exact(raw, _HLEN.size)
        (hlen,) = _HLEN.unpack(hraw) if len(hraw) == _HLEN.size else (0,)
        if not 0 < hlen <= _MAX_HEADER:
            raise BackupCryptoError("заголовок бэкапа повреждён")
        try:
            header = json.loads(_read_exact(raw, hlen))
        except ValueError:
            raise BackupCryptoError("заголовок бэкапа повреждён") from None
        if not isinstance(header, dict):
            raise BackupCryptoError("заголовок бэкапа повреждён")
        if header.get("v") != 1 or header.get("kdf") != "scrypt":
            raise BackupCryptoError("версия формата бэкапа не поддерживается — обновите бота")
        try:
            kdf = KdfParams(int(header["n"]), int(header["r"]), int(header["p"]))
            salt = base64.b64decode(header["salt"], validate=True)
            check = str(header["check"])
            chunk = int(header["chunk"])
        except (ValueError, KeyError, TypeError, AttributeError):
            raise BackupCryptoError("заголовок бэкапа повреждён") from None
        if not 8 <= len(salt) <= 64 or not 1 <= chunk <= _MAX_CHUNK:
            raise BackupCryptoError("заголовок бэкапа повреждён")
        try:
            key = _derive(password, salt, kdf)
        except ValueError:
            raise WrongPasswordError("пароль не задан") from None
        if not hmac.compare_digest(_check(key), check):
            raise WrongPasswordError("неверный пароль бэкапа")
        self._fernet = Fernet(base64.urlsafe_b64encode(key))
        self._max_frame = chunk + _FRAME_OVERHEAD + chunk // 2
        self._seq = 0
        self._buf = b""
        self._pos = 0
        self._done = False

    def readable(self) -> bool:
        return True

    def _next_frame(self) -> None:
        head = _read_exact(self._raw, _LEN.size)
        if len(head) < _LEN.size:
            raise BackupCryptoError("бэкап обрезан: нет завершающего блока (файл скачан не полностью?)")
        (size,) = _LEN.unpack(head)
        if not 0 < size <= self._max_frame:
            raise BackupCryptoError("бэкап повреждён: неверный размер блока")
        raw = _read_exact(self._raw, size)
        if len(raw) < size:
            raise BackupCryptoError("бэкап обрезан (файл скачан не полностью?)")
        try:
            plain = self._fernet.decrypt(base64.urlsafe_b64encode(raw))
        except InvalidToken:
            raise BackupCryptoError(f"бэкап повреждён: блок {self._seq} не прошёл проверку") from None
        if len(plain) < _PREFIX.size:
            raise BackupCryptoError("бэкап повреждён: короткий блок")
        seq, final = _PREFIX.unpack(plain[: _PREFIX.size])
        if seq != self._seq:
            raise BackupCryptoError("бэкап повреждён: блоки перепутаны или пропущены")
        self._seq += 1
        self._buf, self._pos = plain[_PREFIX.size :], 0
        if final:
            self._done = True
            if self._raw.read(1):
                raise BackupCryptoError("бэкап повреждён: лишние данные после завершающего блока")

    def readinto(self, b: Any) -> int:
        view = memoryview(b).cast("B")
        while self._pos >= len(self._buf):
            if self._done:
                return 0
            self._next_frame()
        n = min(len(view), len(self._buf) - self._pos)
        view[:n] = self._buf[self._pos : self._pos + n]
        self._pos += n
        return n


def encrypt_bytes(data: bytes, password: str, *, kdf: KdfParams | None = None) -> bytes:
    out = io.BytesIO()
    with EncryptingWriter(out, password, kdf=kdf) as writer:
        writer.write(data)
    return out.getvalue()


def decrypt_bytes(blob: bytes, password: str, *, limit: int = 64 * 1024 * 1024) -> bytes:
    reader = DecryptingReader(io.BytesIO(blob), password)
    out = bytearray()
    while piece := reader.read(CHUNK):
        out += piece
        if len(out) > limit:
            raise BackupCryptoError("расшифрованные данные слишком большие")
    return bytes(out)
