"""Tiny EXIF (TIFF) writer for synthetic test files. Big-endian like Apple devices.

Not used by the application; the engine only reads EXIF.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

BYTE, ASCII, SHORT, LONG, UNDEFINED = 1, 2, 3, 4, 7


@dataclass
class Entry:
    tag: int
    type: int
    data: bytes
    count: int


def ascii_entry(tag: int, text: str) -> Entry:
    data = text.encode("utf-8") + b"\x00"
    return Entry(tag, ASCII, data, len(data))


def short_entry(tag: int, value: int) -> Entry:
    return Entry(tag, SHORT, struct.pack(">H", value), 1)


def long_entry(tag: int, value: int) -> Entry:
    return Entry(tag, LONG, struct.pack(">I", value), 1)


def undefined_entry(tag: int, data: bytes) -> Entry:
    return Entry(tag, UNDEFINED, data, len(data))


def _ifd_size(entries: list[Entry]) -> tuple[int, int]:
    table = 2 + 12 * len(entries) + 4
    data = sum(len(e.data) + (len(e.data) & 1) for e in entries if len(e.data) > 4)
    return table, data


def _serialize_ifd(entries: list[Entry], ifd_offset: int, next_ifd: int) -> bytes:
    entries = sorted(entries, key=lambda e: e.tag)
    table_size, _ = _ifd_size(entries)
    data_cursor = ifd_offset + table_size
    out = bytearray(struct.pack(">H", len(entries)))
    data = bytearray()
    for e in entries:
        if len(e.data) <= 4:
            value = e.data + b"\x00" * (4 - len(e.data))
        else:
            value = struct.pack(">I", data_cursor + len(data))
            data += e.data
            if len(e.data) & 1:
                data += b"\x00"
        out += struct.pack(">HHI", e.tag, e.type, e.count) + value
    out += struct.pack(">I", next_ifd)
    return bytes(out) + bytes(data)


@dataclass
class ExifSpec:
    make: str | None = None
    model: str | None = None
    software: str | None = None
    datetime_original: str | None = None  # "2020:06:05 14:03:22"
    offset_time: str | None = None
    orientation: int | None = None
    user_comment: str | None = None
    content_id: str | None = None
    width: int | None = None
    height: int | None = None
    thumbnail: bytes | None = None
    extra_ifd0: list[Entry] = field(default_factory=list)


def apple_makernote(content_id: str, capture_type: int = 2) -> bytes:
    """Apple MakerNote with ContentIdentifier (0x0011) and ImageCaptureType (0x0014)."""
    entries = [ascii_entry(0x0011, content_id), long_entry(0x0014, capture_type)]
    head = b"Apple iOS\x00" + b"\x00\x01" + b"MM"
    return head + _serialize_ifd(entries, 14, 0)


def build_tiff(spec: ExifSpec) -> bytes:
    """TIFF structure (IFD0 + Exif IFD + optional IFD1 thumbnail), offsets from the TIFF header."""
    ifd0: list[Entry] = list(spec.extra_ifd0)
    if spec.make:
        ifd0.append(ascii_entry(0x010F, spec.make))
    if spec.model:
        ifd0.append(ascii_entry(0x0110, spec.model))
    if spec.software:
        ifd0.append(ascii_entry(0x0131, spec.software))
    if spec.orientation:
        ifd0.append(short_entry(0x0112, spec.orientation))
    if spec.datetime_original:
        ifd0.append(ascii_entry(0x0132, spec.datetime_original))
    exif: list[Entry] = [undefined_entry(0x9000, b"0232")]
    if spec.datetime_original:
        exif.append(ascii_entry(0x9003, spec.datetime_original))
    if spec.offset_time:
        exif.append(ascii_entry(0x9011, spec.offset_time))
    if spec.user_comment is not None:
        exif.append(
            undefined_entry(0x9286, b"ASCII\x00\x00\x00" + spec.user_comment.encode("ascii"))
        )
    if spec.content_id:
        exif.append(undefined_entry(0x927C, apple_makernote(spec.content_id)))
    if spec.width and spec.height:
        exif.append(long_entry(0xA002, spec.width))
        exif.append(long_entry(0xA003, spec.height))
    ifd0.append(long_entry(0x8769, 0))  # placeholder, patched below
    t0, d0 = _ifd_size(ifd0)
    exif_offset = 8 + t0 + d0
    te, de = _ifd_size(exif)
    ifd1_offset = exif_offset + te + de
    ifd1: list[Entry] = []
    next_ifd = 0
    thumb_offset = 0
    if spec.thumbnail:
        ifd1 = [
            long_entry(0x0201, 0),
            long_entry(0x0202, len(spec.thumbnail)),
            short_entry(0x0103, 6),
        ]
        t1, d1 = _ifd_size(ifd1)
        thumb_offset = ifd1_offset + t1 + d1
        ifd1[0] = long_entry(0x0201, thumb_offset)
        next_ifd = ifd1_offset
    for i, e in enumerate(ifd0):
        if e.tag == 0x8769:
            ifd0[i] = long_entry(0x8769, exif_offset)
    out = bytearray(b"MM\x00\x2a" + struct.pack(">I", 8))
    out += _serialize_ifd(ifd0, 8, next_ifd)
    assert len(out) == exif_offset, (len(out), exif_offset)
    out += _serialize_ifd(exif, exif_offset, 0)
    if spec.thumbnail:
        assert len(out) == ifd1_offset
        out += _serialize_ifd(ifd1, ifd1_offset, 0)
        assert len(out) == thumb_offset
        out += spec.thumbnail
    return bytes(out)


def build_exif(spec: ExifSpec) -> bytes:
    """EXIF payload as Pillow expects it for `save(exif=...)`: 'Exif\\0\\0' + TIFF."""
    return b"Exif\x00\x00" + build_tiff(spec)
