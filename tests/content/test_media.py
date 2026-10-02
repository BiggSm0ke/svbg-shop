from __future__ import annotations

import hashlib
import io
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
from PIL import Image

from svbg.content.media import (
    MediaError,
    MediaLibrary,
    MediaLimits,
    PublicMedia,
    media_rel_path,
    prepare_media,
    public_token,
    resolve_media_path,
    sniff,
    write_media_file,
)
from svbg.content.model import Media
from svbg.content.store import ContentSnapshot
from tests.dbkit import CountingDatabase

KEY = b"k" * 32


def _image(fmt: str, size: tuple[int, int] = (64, 48), mode: str = "RGB", **save: Any) -> bytes:
    color: Any = (10, 200, 30, 128) if mode == "RGBA" else (10, 200, 30)
    im = Image.new(mode, size, color)
    buf = io.BytesIO()
    im.save(buf, fmt, **save)
    return buf.getvalue()


def _jpeg_with_exif() -> bytes:
    exif = Image.Exif()
    exif[0x0112] = 6  # orientation: rotate 90
    return _image("JPEG", (40, 20), exif=exif.tobytes())


GIF = _image("GIF")
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 100
MOV = b"\x00\x00\x00\x14ftypqt  " + b"\x00" * 100


# ---------------------------------------------------------------- sniff / prepare (pure)


@pytest.mark.parametrize(
    ("data", "mime"),
    [
        (_image("JPEG"), "image/jpeg"),
        (_image("PNG"), "image/png"),
        (_image("WEBP"), "image/webp"),
        (GIF, "image/gif"),
        (MP4, "video/mp4"),
        (MOV, "video/quicktime"),
        (b"%PDF-1.7 ...", "application/pdf"),
        (b"MZ\x90\x00 not an image", "application/octet-stream"),
        (b"", "application/octet-stream"),
    ],
)
def test_sniff_by_magic_numbers(data: bytes, mime: str) -> None:
    assert sniff(data).mime == mime


def test_small_clean_jpeg_is_kept_byte_for_byte() -> None:
    data = _image("JPEG", (120, 80))
    p = prepare_media(data, "photo")
    assert not p.reencoded
    assert p.data == data
    assert p.sha256 == hashlib.sha256(data).hexdigest()
    assert (p.width, p.height, p.mime, p.ext) == (120, 80, "image/jpeg", "jpg")


def test_png_with_alpha_becomes_jpeg_on_white() -> None:
    p = prepare_media(_image("PNG", (30, 20), mode="RGBA"), "photo")
    assert p.reencoded and p.mime == "image/jpeg" and p.ext == "jpg"
    with Image.open(io.BytesIO(p.data)) as im:
        assert im.format == "JPEG" and im.mode == "RGB" and im.size == (30, 20)


def test_exif_is_stripped_and_orientation_applied() -> None:
    p = prepare_media(_jpeg_with_exif(), "photo")
    assert p.reencoded
    with Image.open(io.BytesIO(p.data)) as im:
        assert im.size == (20, 40)  # rotated by the orientation tag
        assert len(im.getexif()) == 0


def test_large_photo_is_resized_to_max_side() -> None:
    limits = MediaLimits(photo_max_side=100)
    p = prepare_media(_image("PNG", (400, 200)), "photo", limits)
    assert (p.width, p.height) == (100, 50)


def test_reencoding_is_deterministic() -> None:
    data = _image("PNG", (50, 50))
    assert prepare_media(data, "photo").sha256 == prepare_media(data, "photo").sha256


@pytest.mark.parametrize(
    ("data", "kind", "limits", "code"),
    [
        (b"", "photo", None, "empty"),
        (GIF, "photo", None, "bad_type"),
        (b"MZ\x90 exe", "photo", None, "bad_type"),
        (_image("PNG"), "animation", None, "bad_type"),
        (_image("JPEG"), "video", None, "bad_type"),
        (_image("PNG", (100, 100)), "photo", MediaLimits(photo_max_pixels=5000), "too_many_pixels"),
        (_image("PNG", (420, 20)), "photo", None, "bad_aspect"),
        (b"\xff\xd8\xff\xe0 truncated jpeg", "photo", None, "broken"),
        (_image("JPEG", (10, 10)), "photo", MediaLimits(photo_max_bytes=10), "too_large"),
        (MP4, "video", MediaLimits(video_max_bytes=50), "too_large"),
        (b"x", "sticker", None, "bad_kind"),
    ],
)
def test_refusals(data: bytes, kind: str, limits: MediaLimits | None, code: str) -> None:
    with pytest.raises(MediaError) as exc:
        prepare_media(data, kind, limits)
    assert exc.value.code == code
    assert exc.value.message  # a Russian text for the admin


def test_truncated_clean_jpeg_is_refused() -> None:
    data = _image("JPEG", (200, 200), quality=95)
    with pytest.raises(MediaError) as exc:
        prepare_media(data[: len(data) // 2], "photo")
    assert exc.value.code == "broken"


def test_animation_video_document_keep_bytes_and_hints() -> None:
    a = prepare_media(GIF, "animation", width=64, height=48, duration=3)
    assert (a.mime, a.ext, a.width, a.duration) == ("image/gif", "gif", 64, 3)
    v = prepare_media(MOV, "video", width=-1, duration=True)  # type: ignore[arg-type]
    assert (v.mime, v.width, v.duration) == ("video/quicktime", None, None)
    d = prepare_media(b"plain text", "document")
    assert (d.mime, d.ext, d.data) == ("application/octet-stream", "bin", b"plain text")


# ---------------------------------------------------------------- disk


def test_write_is_atomic_content_addressed_and_idempotent(tmp_path: Path) -> None:
    data = b"hello"
    sha = hashlib.sha256(data).hexdigest()
    rel = write_media_file(tmp_path, sha, "bin", data)
    assert rel == f"{sha[:2]}/{sha}.bin" == media_rel_path(sha, "bin")
    assert (tmp_path / rel).read_bytes() == data
    assert write_media_file(tmp_path, sha, "bin", data) == rel
    assert not list(tmp_path.rglob("*.tmp"))


def test_write_refuses_hash_mismatch_and_bad_names(tmp_path: Path) -> None:
    with pytest.raises(MediaError):
        write_media_file(tmp_path, "0" * 64, "bin", b"other")
    with pytest.raises(MediaError):
        media_rel_path("../../etc/passwd", "bin")
    with pytest.raises(MediaError):
        media_rel_path("a" * 64, "../x")
    assert not any(tmp_path.iterdir())


def test_resolve_media_path_stays_inside_root(tmp_path: Path) -> None:
    assert resolve_media_path(tmp_path, "ab/x.jpg") == (tmp_path / "ab/x.jpg").resolve()
    assert resolve_media_path(tmp_path, "../outside.jpg") is None
    assert resolve_media_path(tmp_path, "") is None
    assert resolve_media_path(tmp_path, None) is None
    assert resolve_media_path(tmp_path, ".") is None


# ---------------------------------------------------------------- library (DB)


async def test_add_stores_file_and_row_and_dedupes(db: CountingDatabase, tmp_path: Path) -> None:
    lib = MediaLibrary(db, tmp_path)
    data = _image("JPEG", (32, 32))
    first = await lib.add(data, "photo", bot_id=42, file_id="AgAD-original")
    assert first.created and not first.reencoded
    assert lib.path_of(first.media) == (tmp_path / first.media.path).resolve()  # type: ignore[operator]
    again = await lib.add(data, "photo", bot_id=7, file_id="other-bot")
    assert not again.created and again.media.id == first.media.id
    rows = await db.raw("select file_ids, size, mime, width from media")
    assert len(rows) == 1
    assert rows[0]["file_ids"] == {"42": "AgAD-original", "7": "other-bot"}
    assert (rows[0]["size"], rows[0]["mime"], rows[0]["width"]) == (len(data), "image/jpeg", 32)


async def test_reencoded_upload_does_not_cache_foreign_file_id(db: CountingDatabase, tmp_path: Path) -> None:
    lib = MediaLibrary(db, tmp_path)
    stored = await lib.add(_image("PNG"), "photo", bot_id=42, file_id="id-of-the-png")
    assert stored.reencoded
    rows = await db.raw("select file_ids from media where id = $1", stored.media.id)
    assert rows[0]["file_ids"] == {}


async def test_refused_upload_writes_nothing(db: CountingDatabase, tmp_path: Path) -> None:
    lib = MediaLibrary(db, tmp_path)
    with pytest.raises(MediaError):
        await lib.add(b"MZ\x90 exe", "photo")
    assert await db.raw("select id from media") == []
    assert not tmp_path.exists() or not any(tmp_path.rglob("*"))


async def test_limits_are_read_live_and_checked_before_download(db: CountingDatabase, tmp_path: Path) -> None:
    current = {"limits": MediaLimits()}
    lib = MediaLibrary(db, tmp_path, limits=lambda: current["limits"])
    lib.check_declared("video", 10 * 1024 * 1024)
    current["limits"] = MediaLimits(video_max_bytes=1024)
    with pytest.raises(MediaError) as exc:
        lib.check_declared("video", 2048)
    assert exc.value.code == "too_large"
    lib.check_declared("video", None)  # unknown size: checked after download
    with pytest.raises(MediaError):
        lib.check_declared("voice", 1)


async def test_path_of_ignores_missing_and_escaping_paths(db: CountingDatabase, tmp_path: Path) -> None:
    lib = MediaLibrary(db, tmp_path)
    assert lib.path_of(Media(1, "photo", "a" * 64, path="../../secret.jpg")) is None
    assert lib.path_of(Media(1, "photo", "a" * 64, path="aa/missing.jpg")) is None


# ---------------------------------------------------------------- public ids


def test_public_token_is_keyed_stable_and_url_safe() -> None:
    sha = "a" * 64
    t = public_token(KEY, sha)
    assert t == public_token(KEY, sha)
    assert t != public_token(b"x" * 32, sha)
    assert t != public_token(KEY, "b" * 64)
    assert len(t) == 22 and t.replace("-", "").replace("_", "").isalnum()
    assert sha[:8] not in t


class _Source:
    def __init__(self) -> None:
        self.snapshot = _snap(1, {})


def _snap(version: int, items: dict[int, Media]) -> ContentSnapshot:
    return ContentSnapshot(version, MappingProxyType({}), MappingProxyType({}), MappingProxyType(items))


def test_public_media_resolves_from_snapshot_only(tmp_path: Path) -> None:
    data = _image("JPEG")
    sha = hashlib.sha256(data).hexdigest()
    rel = write_media_file(tmp_path, sha, "jpg", data)
    photo = Media(5, "photo", sha, path=rel, mime="image/jpeg")
    doc = Media(6, "document", "d" * 64, path=rel, mime="application/pdf")
    gone = Media(7, "video", "e" * 64, path="ee/missing.mp4", mime="video/mp4")
    src = _Source()
    pub = PublicMedia(src, tmp_path, KEY)
    assert pub.resolve(pub.token(photo)) is None  # not in the snapshot yet
    src.snapshot = _snap(2, {5: photo, 6: doc, 7: gone})
    found = pub.resolve(pub.token(photo))
    assert found is not None and found[0] is photo and found[1].read_bytes() == data
    assert pub.resolve(pub.token(doc)) is None  # documents are never public
    assert pub.resolve(pub.token(gone)) is None  # file missing on disk
    assert pub.resolve("5") is None
    assert pub.path(photo) == f"/m/{pub.token(photo)}.jpg"
    assert pub.url("https://bot.example.com/", photo) == f"https://bot.example.com/m/{pub.token(photo)}.jpg"
    src.snapshot = _snap(3, {})
    assert pub.resolve(pub.token(photo)) is None  # removed from content → gone from the web


def test_public_media_needs_a_real_key(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="16 bytes"):
        PublicMedia(_Source(), tmp_path, b"short")
