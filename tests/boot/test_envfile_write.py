from __future__ import annotations

import errno
import os
import stat
import time
from pathlib import Path

import pytest

from svbg.boot import envfile
from svbg.boot.envfile import (
    EnvDocument,
    EnvFileError,
    read_text,
    read_text_async,
    write_atomic,
    write_atomic_async,
)

POSIX = os.name != "nt"


def leftovers(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if p.name.endswith(".tmp"))


# ------------------------------------------------------------------------------------------ read_text


def test_read_missing_returns_none(tmp_path: Path) -> None:
    assert read_text(tmp_path / ".env") is None


def test_read_keeps_bytes_crlf_and_bom(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    raw = "\ufeffA=1\r\nB=привет\r\n".encode()
    path.write_bytes(raw)
    text = read_text(path)
    assert text == "\ufeffA=1\r\nB=привет\r\n"
    doc = EnvDocument.parse(text)
    assert doc.bom is True
    assert doc.get("B") == "привет"
    assert doc.render().encode() == raw


def test_read_rejects_non_utf8(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_bytes("A=привет\n".encode("cp1251"))
    with pytest.raises(EnvFileError, match="UTF-8"):
        read_text(path)


def test_read_rejects_too_big(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_bytes(b"A=1\n" * 100)
    with pytest.raises(EnvFileError, match="слишком большой"):
        read_text(path, max_bytes=50)
    assert read_text(path, max_bytes=400) is not None


def test_read_directory_raises_oserror(tmp_path: Path) -> None:
    with pytest.raises(OSError):  # PermissionError on Windows, IsADirectoryError on POSIX
        read_text(tmp_path)


# ------------------------------------------------------------------------------------------ write_atomic


def test_write_creates_file_with_exact_bytes(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    text = "\ufeffA=1\r\nB=два\r\n"
    write_atomic(path, text)
    assert path.read_bytes() == text.encode()
    assert not (tmp_path / ".env.bak").exists()  # nothing to back up on the first write
    assert leftovers(tmp_path) == []


def test_write_keeps_backup_of_previous_version(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    write_atomic(path, "A=1\n")
    write_atomic(path, "A=2\n")
    assert path.read_text() == "A=2\n"
    assert (tmp_path / ".env.bak").read_text() == "A=1\n"
    write_atomic(path, "A=2\n")  # unchanged content must not clobber the useful backup
    assert (tmp_path / ".env.bak").read_text() == "A=1\n"
    write_atomic(path, "A=3\n", keep_backup=False)
    assert (tmp_path / ".env.bak").read_text() == "A=1\n"
    assert path.read_text() == "A=3\n"
    assert leftovers(tmp_path) == []


@pytest.mark.skipif(not POSIX, reason="POSIX permission bits")
def test_write_sets_mode_0600_for_file_and_backup(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("OLD=1\n")
    path.chmod(0o644)
    old_umask = os.umask(0)
    try:
        write_atomic(path, "NEW=1\n")
    finally:
        os.umask(old_umask)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / ".env.bak").stat().st_mode) == 0o600
    write_atomic(path, "NEW=2\n", mode=0o640)
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


@pytest.mark.skipif(POSIX, reason="Windows only")
def test_write_result_is_writable_on_windows(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    write_atomic(path, "A=1\n")
    assert os.access(path, os.W_OK)
    write_atomic(path, "A=2\n")
    assert path.read_text() == "A=2\n"


def test_failure_during_write_leaves_original_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / ".env"
    path.write_bytes(b"ORIGINAL=1\n")

    def broken_fsync(fd: int) -> None:
        raise OSError(errno.EIO, "disk on fire")

    monkeypatch.setattr(envfile.os, "fsync", broken_fsync)
    with pytest.raises(OSError, match="disk on fire"):
        write_atomic(path, "NEW=1\n" * 1000)
    assert path.read_bytes() == b"ORIGINAL=1\n"
    assert not (tmp_path / ".env.bak").exists()
    assert leftovers(tmp_path) == []


def test_failure_in_the_middle_of_writing_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / ".env"
    path.write_bytes(b"ORIGINAL=1\n")
    real_fdopen = os.fdopen

    class HalfWriter:
        def __init__(self, handle: object) -> None:
            self._handle = handle

        def __enter__(self) -> HalfWriter:
            return self

        def __exit__(self, *exc: object) -> None:
            self._handle.close()  # type: ignore[attr-defined]

        def fileno(self) -> int:
            return self._handle.fileno()  # type: ignore[attr-defined]

        def write(self, data: bytes) -> int:
            self._handle.write(data[: len(data) // 2])  # type: ignore[attr-defined]
            self._handle.flush()  # type: ignore[attr-defined]
            raise KeyboardInterrupt  # even a BaseException must not leave a partial file

    monkeypatch.setattr(envfile.os, "fdopen", lambda fd, mode: HalfWriter(real_fdopen(fd, mode)))
    with pytest.raises(KeyboardInterrupt):
        write_atomic(path, "NEW=1\n" * 1000)
    assert path.read_bytes() == b"ORIGINAL=1\n"
    assert leftovers(tmp_path) == []


def test_failure_on_replace_cleans_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / ".env"
    path.write_bytes(b"ORIGINAL=1\n")
    real_replace = os.replace

    def fail_main_replace(src: str | Path, dst: str | Path) -> None:
        if Path(dst).name == ".env":
            raise OSError(errno.EACCES, "read-only file system")
        real_replace(src, dst)

    monkeypatch.setattr(envfile.os, "replace", fail_main_replace)
    with pytest.raises(OSError, match="read-only"):
        write_atomic(path, "NEW=1\n")
    assert path.read_bytes() == b"ORIGINAL=1\n"
    assert leftovers(tmp_path) == []


def test_encoding_error_happens_before_touching_disk(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_bytes(b"A=1\n")
    with pytest.raises(UnicodeEncodeError):
        write_atomic(path, "A=\udc80\n")
    assert path.read_bytes() == b"A=1\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == [".env"]


def test_backup_failure_does_not_block_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / ".env"
    path.write_bytes(b"A=1\n")
    real_replace = os.replace

    def fail_backup(src: str | Path, dst: str | Path) -> None:
        if Path(dst).name == ".env.bak":
            raise OSError(errno.ENOSPC, "no space")
        real_replace(src, dst)

    monkeypatch.setattr(envfile.os, "replace", fail_backup)
    write_atomic(path, "A=2\n")
    assert path.read_bytes() == b"A=2\n"
    assert leftovers(tmp_path) == []


def test_bind_mount_busy_raises_unless_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / ".env"
    path.write_bytes(b"A=1\nLONGER_ORIGINAL_CONTENT=1\n")
    real_replace = os.replace

    def busy(src: str | Path, dst: str | Path) -> None:
        if Path(dst).name == ".env":
            raise OSError(errno.EBUSY, "Device or resource busy")
        real_replace(src, dst)

    monkeypatch.setattr(envfile.os, "replace", busy)
    with pytest.raises(OSError, match="bind-mounted"):
        write_atomic(path, "A=2\n")
    assert path.read_bytes() == b"A=1\nLONGER_ORIGINAL_CONTENT=1\n"
    write_atomic(path, "A=2\n", inplace_fallback=True)
    assert path.read_bytes() == b"A=2\n"
    assert (tmp_path / ".env.bak").read_bytes() == b"A=1\nLONGER_ORIGINAL_CONTENT=1\n"
    assert leftovers(tmp_path) == []


@pytest.mark.skipif(POSIX, reason="sharing violations are retried on Windows only")
def test_transient_permission_error_is_retried_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / ".env"
    path.write_bytes(b"A=1\n")
    real_replace = os.replace
    failures = {"left": 2}

    def flaky(src: str | Path, dst: str | Path) -> None:
        if Path(dst).name == ".env" and failures["left"]:
            failures["left"] -= 1
            raise PermissionError(errno.EACCES, "file in use")
        real_replace(src, dst)

    monkeypatch.setattr(envfile.os, "replace", flaky)
    monkeypatch.setattr(envfile.time, "sleep", lambda _s: None)
    write_atomic(path, "A=2\n")
    assert path.read_bytes() == b"A=2\n"
    assert failures["left"] == 0


def test_permanent_permission_error_propagates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / ".env"
    path.write_bytes(b"A=1\n")
    real_replace = os.replace

    def denied(src: str | Path, dst: str | Path) -> None:
        if Path(dst).name == ".env":
            raise PermissionError(errno.EACCES, "denied")
        real_replace(src, dst)

    monkeypatch.setattr(envfile.os, "replace", denied)
    monkeypatch.setattr(envfile.time, "sleep", lambda _s: None)
    with pytest.raises(PermissionError):
        write_atomic(path, "A=2\n")
    assert path.read_bytes() == b"A=1\n"
    assert leftovers(tmp_path) == []


def test_symlink_target_is_replaced(tmp_path: Path) -> None:
    real = tmp_path / "real.env"
    real.write_text("A=1\n")
    link = tmp_path / ".env"
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip("symlinks are not permitted here")
    write_atomic(link, "A=2\n")
    assert link.is_symlink()
    assert real.read_text() == "A=2\n"
    assert (tmp_path / "real.env.bak").read_text() == "A=1\n"


def test_stale_temp_files_are_removed(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    stale = tmp_path / "..env.deadbeef.tmp"
    stale.write_text("SECRET=1\n")
    old = time.time() - envfile.STALE_TEMP_SECONDS - 10
    os.utime(stale, (old, old))
    fresh = tmp_path / "..env.cafebabe.tmp"
    fresh.write_text("x")
    unrelated = tmp_path / "other.tmp"
    unrelated.write_text("x")
    os.utime(unrelated, (old, old))
    write_atomic(path, "A=1\n")
    assert not stale.exists()
    assert fresh.exists()
    assert unrelated.exists()


async def test_async_wrappers(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    assert await read_text_async(path) is None
    await write_atomic_async(path, "A=1\r\n")
    assert await read_text_async(path) == "A=1\r\n"


def test_full_cycle_read_edit_write_preserves_owner_formatting(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    original = "\ufeff# мой файл\r\nexport A='1'  # note\r\nB=2\r\n"
    path.write_bytes(original.encode())
    text = read_text(path)
    assert text is not None
    doc = EnvDocument.parse(text)
    doc.set("A", "5")
    doc.set("C", "новое значение")
    write_atomic(path, doc.render())
    assert path.read_bytes().decode() == (
        "\ufeff# мой файл\r\nexport A='5'  # note\r\nB=2\r\nC=\"новое значение\"\r\n"
    )
    assert (tmp_path / ".env.bak").read_bytes() == original.encode()
