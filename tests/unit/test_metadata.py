from __future__ import annotations

import struct
from pathlib import Path

import pytest
from conftest import needs_ffmpeg
from make_dataset import BIN_DIR, LIVE_ID_A, LIVE_ID_B

from framesift.engine.metadata import detect_format, read_media_info
from framesift.engine.metadata.base import FileReader
from framesift.engine.metadata.isobmff import parse_heif_meta, read_heif


def info(path: Path):
    st = path.stat()
    return read_media_info(
        path, path.suffix.lower().lstrip("."), st.st_size, st.st_mtime_ns, allow_external=False
    )


def test_jpeg_with_camera_exif(dataset) -> None:
    root, _ = dataset
    m = info(root / "2020" / "IMG_0001.JPG")
    assert m.format == "jpeg" and m.extractor == "jpeg"
    assert (m.make, m.model) == ("Apple", "iPhone 12")
    assert m.date_taken == "2020-06-05T14:03:22" and m.date_source == "exif"
    assert m.content_id == LIVE_ID_A
    assert (m.width, m.height) == (320, 240)
    assert m.has_preview and m.preview_kind == "jpeg" and m.preview_offset and m.preview_length
    assert m.structure_ok is True and m.camera_known is True
    with open(root / "2020" / "IMG_0001.JPG", "rb") as fh:
        fh.seek(m.preview_offset)
        assert fh.read(2) == b"\xff\xd8"


def test_jpeg_without_exif_and_broken(dataset) -> None:
    root, _ = dataset
    m = info(root / "messenger" / "photo_no_camera.jpg")
    assert m.make is None and m.camera_known and m.structure_ok is True
    assert m.date_source == "mtime" and m.date_taken
    broken = info(root / "junk" / "broken.jpg")
    assert broken.structure_ok is False
    empty = info(root / "junk" / "empty.jpg")
    assert empty.structure_ok is False and empty.extractor == "empty"


def test_png_exif_after_idat_and_plain(dataset) -> None:
    root, _ = dataset
    m = info(root / "2020" / "IMG_0005.PNG")
    assert m.format == "png" and (m.width, m.height) == (1170, 2532)
    assert m.user_comment == "Screenshot" and m.software == "15.1"
    assert m.structure_ok is True
    plain = info(root / "2020" / "Screenshot_20200608.png")
    assert (plain.width, plain.height) == (1080, 2400) and plain.make is None and plain.camera_known


def test_webp_and_gif(dataset) -> None:
    root, _ = dataset
    w = info(root / "messenger" / "sticker.webp")
    assert w.format == "webp" and (w.width, w.height) == (200, 200) and w.structure_ok
    g = info(root / "messenger" / "anim.gif")
    assert g.format == "gif" and (g.width, g.height) == (120, 90) and g.structure_ok


def test_heic_fixtures() -> None:
    live = BIN_DIR / "live_photo.heic"
    if not live.exists():
        pytest.skip("HEIC fixtures missing")
    m = info(live)
    assert m.format == "heic" and m.extractor == "isobmff" and m.codec == "hevc"
    assert (m.make, m.model) == ("Apple", "iPhone 13")
    assert m.content_id == LIVE_ID_B
    assert m.date_taken == "2020-06-05T15:00:00"
    assert (m.width, m.height) == (96, 72)
    assert m.structure_ok is True
    plain = info(BIN_DIR / "plain.heic")
    assert plain.make is None and plain.camera_known and plain.structure_ok
    rotated = info(BIN_DIR / "rotated.heic")
    assert rotated.orientation == 6


@needs_ffmpeg
def test_mov_with_apple_keys(dataset) -> None:
    root, _ = dataset
    m = info(root / "2020" / "IMG_0001.MOV")
    assert m.format in ("mov", "mp4") and m.extractor == "isobmff"
    assert 1800 <= (m.duration_ms or 0) <= 2200
    assert m.content_id == LIVE_ID_A
    assert (m.make, m.model) == ("Apple", "iPhone 12")
    assert m.date_taken == "2020-06-05T14:03:22" and m.date_source == "quicktime"
    assert m.codec == "mpeg4" and (m.width, m.height) == (320, 240)
    assert m.fps and 9 <= m.fps <= 11
    assert m.bitrate and m.structure_ok


@needs_ffmpeg
def test_mp4_without_keys_and_hevc(dataset) -> None:
    root, _ = dataset
    m = info(root / "2020" / "short.mp4")
    assert 1400 <= (m.duration_ms or 0) <= 1600
    assert m.make is None and m.content_id is None and m.camera_known
    assert m.date_source in ("quicktime_utc", "mtime")
    hevc = BIN_DIR / "hevc.mov"
    if hevc.exists():
        h = info(hevc)
        assert h.codec == "hevc" and (h.make, h.model) == ("Apple", "iPhone 13")


def test_detect_format_by_magic() -> None:
    assert detect_format(b"\xff\xd8\xff\xe1", "png") == "jpeg"
    assert detect_format(b"\x89PNG\r\n\x1a\n", "jpg") == "png"
    assert detect_format(b"RIFF\x00\x00\x00\x00WEBPVP8 ", "webp") == "webp"
    assert detect_format(b"\x00\x00\x00\x18ftypheic", "heic") == "heif"
    assert detect_format(b"\x00\x00\x00\x18ftypqt  ", "mov") == "isobmff"
    assert detect_format(b"\x00\x00\x00\x08wide", "mov") == "isobmff"
    assert detect_format(b"II*\x00", "dng") == "tiff"
    assert detect_format(b"garbage", "jpg") == "unknown"


def _box(btype: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + btype + payload


def _fullbox(btype: bytes, payload: bytes, version: int = 0, flags: int = 0) -> bytes:
    return _box(btype, bytes([version]) + flags.to_bytes(3, "big") + payload)


def test_synthetic_heif_grid_with_irot_and_idat_exif(tmp_path: Path) -> None:
    """Hand-built HEIF: grid primary, irot, Exif stored in idat (construction method 1), thumbnail ref."""
    from exifwriter import ExifSpec, build_tiff

    tiff = build_tiff(
        ExifSpec(
            make="Apple",
            model="iPhone 15 Pro",
            datetime_original="2024:01:02 03:04:05",
            content_id="CID-1",
        )
    )
    exif_payload = struct.pack(">I", 0) + tiff
    ftyp = _box(b"ftyp", b"heic" + struct.pack(">I", 0) + b"mif1heic")
    hdlr = _fullbox(b"hdlr", b"\x00" * 4 + b"pict" + b"\x00" * 12 + b"\x00")
    pitm = _fullbox(b"pitm", struct.pack(">H", 1))
    infe = b"".join(
        _fullbox(b"infe", struct.pack(">HH", iid, 0) + typ + b"\x00", version=2)
        for iid, typ in ((1, b"grid"), (2, b"hvc1"), (3, b"hvc1"), (4, b"Exif"), (5, b"hvc1"))
    )
    iinf = _fullbox(b"iinf", struct.pack(">H", 5) + infe)
    # iloc v1: offset_size=4,length_size=4,base_offset_size=0,index_size=0; Exif via idat (method 1)
    entries = b""
    for iid, method, off, length in (
        (1, 1, 0, 8),
        (2, 0, 1000, 10),
        (3, 0, 1010, 10),
        (4, 1, 8, len(exif_payload)),
        (5, 0, 2000, 5),
    ):
        entries += struct.pack(">HHHH", iid, method, 0, 1) + struct.pack(">II", off, length)
    iloc = _fullbox(b"iloc", bytes([0x44, 0x00]) + struct.pack(">H", 5) + entries, version=1)
    idat = _box(b"idat", b"\x00\x00\x00\x00\x00\x02\x00\x02" + exif_payload)  # grid config + exif
    ispe_full = _fullbox(b"ispe", struct.pack(">II", 4032, 3024))
    ispe_tile = _fullbox(b"ispe", struct.pack(">II", 512, 512))
    irot = _box(b"irot", bytes([1]))
    hvcc = _box(b"hvcC", b"\x01" + b"\x00" * 22)
    ispe_thumb = _fullbox(b"ispe", struct.pack(">II", 320, 240))
    ipco = _box(b"ipco", ispe_full + irot + hvcc + ispe_tile + ispe_thumb)
    assoc = b""
    for iid, props in ((1, [1, 2]), (2, [3, 4]), (3, [3, 4]), (5, [3, 5])):
        assoc += struct.pack(">HB", iid, len(props)) + bytes(props)
    ipma = _fullbox(b"ipma", struct.pack(">I", 4) + assoc)
    iprp = _box(b"iprp", ipco + ipma)
    dimg = _box(b"dimg", struct.pack(">HHHH", 1, 2, 2, 3))
    cdsc = _box(b"cdsc", struct.pack(">HHH", 4, 1, 1))
    thmb = _box(b"thmb", struct.pack(">HHH", 5, 1, 1))
    iref = _fullbox(b"iref", dimg + cdsc + thmb)
    meta = _fullbox(b"meta", hdlr + pitm + iinf + iloc + idat + iprp + iref)
    data = ftyp + meta
    data += b"\x00" * (2100 - len(data))
    path = tmp_path / "grid.heic"
    path.write_bytes(data)
    with FileReader(path) as reader:
        parsed = parse_heif_meta(reader.read_at, reader.size)
        assert parsed.primary == 1 and parsed.items[4].construction == 1
        m = read_heif(reader)
    assert (m.width, m.height) == (4032, 3024)
    assert m.orientation == 8  # irot 1 = 90° counter-clockwise
    assert m.codec == "hevc"
    assert (m.make, m.model) == ("Apple", "iPhone 15 Pro")
    assert m.content_id == "CID-1" and m.date_taken == "2024-01-02T03:04:05"
    assert m.has_preview and m.preview_kind == "heif_thumb" and m.preview_offset == 2000
