from __future__ import annotations

import io

from exifwriter import ExifSpec, apple_makernote, build_exif, build_tiff
from PIL import Image

from framesift.engine.metadata.exif import decode_user_comment, parse_apple_makernote, parse_exif


def test_parse_exif_roundtrip() -> None:
    thumb = io.BytesIO()
    Image.new("RGB", (16, 12), (1, 2, 3)).save(thumb, "JPEG")
    spec = ExifSpec(
        make="Apple",
        model="iPhone 12",
        software="14.4",
        datetime_original="2020:06:05 14:03:22",
        offset_time="+03:00",
        orientation=6,
        user_comment="Screenshot",
        content_id="ABC-123",
        width=4032,
        height=3024,
        thumbnail=thumb.getvalue(),
    )
    data = build_exif(spec)
    exif = parse_exif(data)
    assert exif.make == "Apple" and exif.model == "iPhone 12" and exif.software == "14.4"
    assert exif.datetime_original == "2020:06:05 14:03:22"
    assert exif.offset_time == "+03:00"
    assert exif.orientation == 6
    assert exif.user_comment == "Screenshot"
    assert exif.width == 4032 and exif.height == 3024
    assert exif.apple["content_id"] == "ABC-123"
    assert exif.apple["capture_type"] == 2
    tiff = build_tiff(spec)
    assert exif.thumbnail_offset and exif.thumbnail_length == len(thumb.getvalue())
    assert tiff[exif.thumbnail_offset : exif.thumbnail_offset + 2] == b"\xff\xd8"


def test_parse_exif_without_prefix_and_garbage() -> None:
    tiff = build_tiff(ExifSpec(make="Canon"))
    assert parse_exif(tiff).make == "Canon"
    assert parse_exif(b"Exif\x00\x00" + tiff).make == "Canon"
    truncated = parse_exif((b"Exif\x00\x00" + tiff)[:20])
    assert truncated.make is None


def test_apple_makernote_parser() -> None:
    mn = apple_makernote("8F2A0C1E-2222-4A2B-9C3D-000000000002", capture_type=10)
    parsed = parse_apple_makernote(mn)
    assert parsed == {"content_id": "8F2A0C1E-2222-4A2B-9C3D-000000000002", "capture_type": 10}
    assert parse_apple_makernote(b"Nikon\x00" + b"\x00" * 20) == {}
    assert parse_apple_makernote(mn[:15]) == {}


def test_user_comment_encodings() -> None:
    assert decode_user_comment(b"ASCII\x00\x00\x00Screenshot") == "Screenshot"
    assert decode_user_comment(b"UNICODE\x00" + "Скрин".encode("utf-16-be"), ">") == "Скрин"
    assert decode_user_comment(b"UNICODE\x00" + "Скрин".encode("utf-16-le"), "<") == "Скрин"
    assert decode_user_comment(b"\x00" * 8 + b"plain") == "plain"
    assert decode_user_comment(b"") is None
