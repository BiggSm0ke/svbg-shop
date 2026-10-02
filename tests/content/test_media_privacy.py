"""Metadata stripping and bounded memory of the media upload path (review findings 1 and 3)."""

from __future__ import annotations

import io
import struct
import threading
from typing import Any

import pytest
from PIL import Image

from svbg.content import media as media_mod
from svbg.content.media import (
    MediaError,
    MediaLimits,
    prepare_media,
    strip_gif_metadata,
    strip_isobmff_metadata,
)

SECRET = b"55.7558N 37.6173E Ivan Petrov iPhone"


def _jpeg(size: tuple[int, int] = (64, 48), mode: str = "RGB", **save: Any) -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, (10, 200, 30)).save(buf, "JPEG", **save)
    return buf.getvalue()


def _meta_of(data: bytes) -> dict[str, Any]:
    with Image.open(io.BytesIO(data)) as im:
        return {
            k: v for k, v in im.info.items() if k in ("exif", "xmp", "comment", "icc_profile", "photoshop")
        }


# ---------------------------------------------------------------- photos: no metadata survives


@pytest.mark.parametrize(
    "save",
    [
        {"xmp": b"<x:xmpmeta>" + SECRET + b"</x:xmpmeta>"},
        {"comment": SECRET},
        {"icc_profile": b"\x00" * 128 + SECRET},
    ],
    ids=["xmp", "comment", "icc"],
)
def test_small_jpeg_with_metadata_but_no_exif_is_reencoded_clean(save: dict[str, Any]) -> None:
    dirty = _jpeg(**save)
    assert SECRET in dirty
    p = prepare_media(dirty, "photo")
    assert p.reencoded and SECRET not in p.data
    assert _meta_of(p.data) == {}


def test_bytes_after_end_of_image_are_not_kept() -> None:
    dirty = _jpeg() + SECRET
    p = prepare_media(dirty, "photo")
    assert p.reencoded and SECRET not in p.data


def test_png_text_chunks_do_not_reach_the_output() -> None:
    from PIL.PngImagePlugin import PngInfo

    info = PngInfo()
    info.add_text("Author", SECRET.decode())
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (1, 2, 3)).save(buf, "PNG", pnginfo=info)
    p = prepare_media(buf.getvalue(), "photo")
    assert SECRET not in p.data and _meta_of(p.data) == {}


def test_clean_jpeg_stays_byte_for_byte_and_output_is_kept_on_reimport() -> None:
    out = prepare_media(_jpeg(xmp=b"<x>" + SECRET + b"</x>"), "photo")
    again = prepare_media(out.data, "photo")
    assert not again.reencoded and again.data == out.data  # our own output is already clean


@pytest.mark.parametrize(
    ("orientation", "size"), [(6, (20, 40)), (8, (20, 40)), (3, (40, 20)), (1, (40, 20))]
)
def test_orientation_is_applied_after_shrinking(orientation: int, size: tuple[int, int]) -> None:
    exif = Image.Exif()
    exif[0x0112] = orientation
    p = prepare_media(_jpeg((40, 20), exif=exif.tobytes()), "photo")
    with Image.open(io.BytesIO(p.data)) as im:
        assert im.size == size and len(im.getexif()) == 0


def test_rotated_large_photo_is_shrunk_and_upright() -> None:
    exif = Image.Exif()
    exif[0x0112] = 6
    p = prepare_media(_jpeg((400, 200), exif=exif.tobytes()), "photo", MediaLimits(photo_max_side=100))
    assert (p.width, p.height) == (50, 100)


@pytest.mark.parametrize("mode", ["P", "LA", "RGBA", "L", "CMYK", "I;16"])
def test_every_mode_becomes_rgb_jpeg(mode: str) -> None:
    im = Image.new(mode, (300, 120))
    if mode == "P":
        im.info["transparency"] = 0
    buf = io.BytesIO()
    im.save(buf, "JPEG" if mode == "CMYK" else "PNG")
    p = prepare_media(buf.getvalue(), "photo", MediaLimits(photo_max_side=150))
    with Image.open(io.BytesIO(p.data)) as out:
        assert out.mode == "RGB" and out.size == (150, 60)


# ---------------------------------------------------------------- photos: bounded decoding


@pytest.mark.parametrize(
    ("w", "h", "expected"),
    [
        (2000, 1500, 1),  # already small enough
        (6000, 4000, 2),  # 3000 px after 1/2 still covers the 2560 output
        (8000, 6000, 2),
        (12000, 3000, 4),
        (5000, 5000, 2),  # 25 MP does not fit the decode budget: 1/2 even though output is 2500
        (6, 6, 1),
    ],
)
def test_jpeg_draft_scale(w: int, h: int, expected: int) -> None:
    assert media_mod._jpeg_draft_scale(w, h, MediaLimits()) == expected


def test_big_jpeg_is_decoded_downscaled_but_same_size_png_is_refused() -> None:
    limits = MediaLimits(photo_max_side=100, photo_decode_max_pixels=40_000)
    jpeg = _jpeg((400, 300))  # 120 000 px: libjpeg decodes it at 1/4
    p = prepare_media(jpeg, "photo", limits)
    assert (p.width, p.height) == (100, 75)
    buf = io.BytesIO()
    Image.new("RGB", (400, 300)).save(buf, "PNG")
    with pytest.raises(MediaError) as exc:
        prepare_media(buf.getvalue(), "photo", limits)
    assert exc.value.code == "too_many_pixels"


def test_webp_gets_half_the_decode_budget() -> None:
    buf = io.BytesIO()
    Image.new("RGB", (200, 150)).save(buf, "WEBP")  # 30 000 px
    with pytest.raises(MediaError) as exc:
        prepare_media(buf.getvalue(), "photo", MediaLimits(photo_decode_max_pixels=40_000))
    assert exc.value.code == "too_many_pixels"
    assert prepare_media(buf.getvalue(), "photo", MediaLimits(photo_decode_max_pixels=60_000)).reencoded


def test_default_limits_refuse_a_huge_png_before_decoding() -> None:
    buf = io.BytesIO()
    Image.new("1", (4100, 4100)).save(buf, "PNG")  # 16.8 MP, tiny file
    with pytest.raises(MediaError) as exc:
        prepare_media(buf.getvalue(), "photo")
    assert exc.value.code == "too_many_pixels"


def test_photo_lock_is_released_after_failures() -> None:
    with pytest.raises(MediaError):
        prepare_media(b"\xff\xd8\xff\xe0 truncated jpeg", "photo")
    with pytest.raises(MediaError):
        prepare_media(_jpeg((100, 100), comment=b"x"), "photo", MediaLimits(photo_decode_max_pixels=10))
    assert media_mod._PHOTO_LOCK.acquire(blocking=False)
    media_mod._PHOTO_LOCK.release()


def test_photos_are_processed_one_at_a_time(monkeypatch: pytest.MonkeyPatch) -> None:
    active = 0
    peak = 0
    guard = threading.Lock()
    real_open = Image.open

    def counting_open(*args: Any, **kwargs: Any) -> Any:
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        try:
            return real_open(*args, **kwargs)
        finally:
            threading.Event().wait(0.01)
            with guard:
                active -= 1

    monkeypatch.setattr(Image, "open", counting_open)
    data = _jpeg((64, 64), comment=b"x")
    threads = [threading.Thread(target=prepare_media, args=(data, "photo")) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak == 1


# ---------------------------------------------------------------- MP4 / MOV


def _box(kind: bytes, payload: bytes = b"", *, large: bool = False) -> bytes:
    if large:
        return struct.pack(">I4sQ", 1, kind, 16 + len(payload)) + payload
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def _mp4(*, dirty: bool, large_udta: bool = False) -> bytes:
    ftyp = _box(b"ftyp", b"isom\x00\x00\x02\x00isomiso2mp41")
    xyz = _box(b"\xa9xyz", SECRET)
    udta = _box(b"udta", xyz, large=large_udta)
    trak = _box(
        b"trak", _box(b"tkhd", b"\x00" * 20) + (_box(b"udta", _box(b"name", SECRET)) if dirty else b"")
    )
    moov_children = _box(b"mvhd", b"\x00" * 100) + trak + (udta + _box(b"meta", SECRET) if dirty else b"")
    xmp = _box(b"uuid", bytes.fromhex("be7acfcb97a942e89c71999491e3afac") + SECRET)
    other_uuid = _box(b"uuid", b"\x11" * 16 + b"keep me")
    return (
        ftyp + _box(b"moov", moov_children) + (xmp if dirty else b"") + other_uuid + _box(b"mdat", b"frames")
    )


@pytest.mark.parametrize("large", [False, True])
def test_mp4_metadata_is_blanked_in_place(large: bool) -> None:
    dirty = _mp4(dirty=True, large_udta=large)
    clean = strip_isobmff_metadata(dirty)
    assert len(clean) == len(dirty)  # offsets (stco) stay valid
    assert SECRET not in clean
    assert b"udta" not in clean and b"meta" not in clean
    assert clean.endswith(_box(b"mdat", b"frames")) and b"keep me" in clean
    assert clean[:4] == dirty[:4] and clean.count(b"free") == 4
    p = prepare_media(dirty, "video", duration=5)
    assert p.reencoded and p.data == clean and p.duration == 5


def test_clean_mp4_is_returned_unchanged() -> None:
    data = _mp4(dirty=False)
    assert strip_isobmff_metadata(data) is data
    p = prepare_media(data, "animation")
    assert not p.reencoded and p.data == data


@pytest.mark.parametrize(
    "data",
    [
        _box(b"ftyp", b"isom") + struct.pack(">I4s", 999, b"moov"),  # box longer than the file
        _box(b"ftyp", b"isom") + struct.pack(">I4s", 4, b"moov"),  # box shorter than its header
        _box(b"ftyp", b"isom") + b"\x00\x00\x00",  # trailing garbage
        _box(b"ftyp", b"isom") + _box(b"moov", struct.pack(">I4s", 50, b"udta")),  # broken child
    ],
)
def test_broken_mp4_is_refused(data: bytes) -> None:
    with pytest.raises(MediaError) as exc:
        prepare_media(data, "video")
    assert exc.value.code == "broken"


def test_documents_are_not_touched() -> None:
    data = _mp4(dirty=True)
    p = prepare_media(data, "document")
    assert p.data == data and not p.reencoded


# ---------------------------------------------------------------- GIF


def _gif(*, extras: bytes = b"", trailer: bytes = b"") -> bytes:
    frames = [Image.new("RGB", (8, 8), c) for c in ((255, 0, 0), (0, 0, 255))]
    buf = io.BytesIO()
    frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:], loop=0, duration=50)
    data = buf.getvalue()
    # insert the extras right after the header + global colour table, before the first extension
    flags = data[10]
    pos = 13 + (3 * (2 << (flags & 7)) if flags & 0x80 else 0)
    return data[:pos] + extras + data[pos:] + trailer


def _ext(label: int, *chunks: bytes) -> bytes:
    return bytes([0x21, label]) + b"".join(bytes([len(c)]) + c for c in chunks) + b"\x00"


def test_gif_comments_xmp_and_trailing_bytes_are_removed() -> None:
    plain = _gif()
    dirty = _gif(extras=_ext(0xFE, SECRET) + _ext(0xFF, b"XMP DataXMP", SECRET), trailer=SECRET)
    clean = strip_gif_metadata(dirty)
    assert SECRET not in clean
    assert clean == plain
    assert b"NETSCAPE2.0" in clean  # the loop extension stays
    with Image.open(io.BytesIO(clean)) as im:
        assert im.n_frames == 2
    p = prepare_media(dirty, "animation")
    assert p.reencoded and p.data == clean
    assert strip_gif_metadata(plain) is plain


@pytest.mark.parametrize(
    "data",
    [b"GIF89a\x01\x00", b"GIF89a" + b"\x00" * 7 + b"\x21\xfe\x05ab", b"GIF89a" + b"\x00" * 7 + b"\x99"],
)
def test_broken_gif_is_refused(data: bytes) -> None:
    with pytest.raises(MediaError) as exc:
        prepare_media(data, "animation")
    assert exc.value.code == "broken"
