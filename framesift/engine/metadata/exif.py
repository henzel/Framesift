"""Minimal TIFF/EXIF parser and the Apple MakerNote reader. Pure Python, bounds-checked."""

from __future__ import annotations

import struct
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from framesift.engine.metadata.base import clean_text

TYPE_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4}
MAX_ENTRIES = 512

TAG_MAKE = 0x010F
TAG_MODEL = 0x0110
TAG_SOFTWARE = 0x0131
TAG_ORIENTATION = 0x0112
TAG_DATETIME = 0x0132
TAG_IMAGE_WIDTH = 0x0100
TAG_IMAGE_LENGTH = 0x0101
TAG_NEW_SUBFILE_TYPE = 0x00FE
TAG_COMPRESSION = 0x0103
TAG_STRIP_OFFSETS = 0x0111
TAG_STRIP_BYTE_COUNTS = 0x0117
TAG_SUB_IFDS = 0x014A
TAG_EXIF_IFD = 0x8769
TAG_GPS_IFD = 0x8825
TAG_EXIF_VERSION = 0x9000
TAG_DATETIME_ORIGINAL = 0x9003
TAG_OFFSET_TIME_ORIGINAL = 0x9011
TAG_MAKERNOTE = 0x927C
TAG_USER_COMMENT = 0x9286
TAG_PIXEL_X = 0xA002
TAG_PIXEL_Y = 0xA003
TAG_THUMB_OFFSET = 0x0201
TAG_THUMB_LENGTH = 0x0202

APPLE_TAG_CONTENT_ID = 0x0011
APPLE_TAG_BURST_UUID = 0x000B
APPLE_TAG_CAPTURE_TYPE = 0x0014
APPLE_TAG_LIVE_INDEX = 0x0017
APPLE_MAKERNOTE_MAGIC = b"Apple iOS\x00"


@dataclass
class IfdEntry:
    tag: int
    type: int
    count: int
    raw: bytes  # inline (<=4 bytes) or the referenced bytes
    offset: int  # position of the value (relative to the TIFF base)


class Tiff:
    """TIFF structure over any byte source. `read(offset, length)` returns bytes relative to base."""

    def __init__(
        self,
        read: Callable[[int, int], bytes],
        *,
        byteorder: str | None = None,
        first_ifd: int | None = None,
    ):
        self._read = read
        if byteorder is None:
            hdr = read(0, 8)
            if len(hdr) < 8 or hdr[:2] not in (b"II", b"MM"):
                raise ValueError("not a TIFF header")
            self.bo = "<" if hdr[:2] == b"II" else ">"
            magic = struct.unpack(self.bo + "H", hdr[2:4])[0]
            if magic not in (42, 0x4F52, 0x5352, 0x55):  # TIFF, ORF (OR/RO), RW2
                raise ValueError("bad TIFF magic")
            self.first_ifd = struct.unpack(self.bo + "I", hdr[4:8])[0]
        else:
            self.bo = byteorder
            self.first_ifd = first_ifd or 8

    def u16(self, data: bytes, off: int = 0) -> int:
        return struct.unpack_from(self.bo + "H", data, off)[0]

    def u32(self, data: bytes, off: int = 0) -> int:
        return struct.unpack_from(self.bo + "I", data, off)[0]

    def ifd(self, offset: int) -> tuple[dict[int, IfdEntry], int | None]:
        """Entries of the IFD at `offset` and the offset of the next IFD (None if absent)."""
        head = self._read(offset, 2)
        if len(head) < 2:
            return {}, None
        count = self.u16(head)
        if count == 0 or count > MAX_ENTRIES:
            return {}, None
        table = self._read(offset + 2, count * 12 + 4)
        if len(table) < count * 12:
            return {}, None
        entries: dict[int, IfdEntry] = {}
        for i in range(count):
            base = i * 12
            tag = self.u16(table, base)
            typ = self.u16(table, base + 2)
            cnt = self.u32(table, base + 4)
            size = TYPE_SIZES.get(typ, 0) * cnt
            if size == 0 or size > 64 * 1024 * 1024:
                continue
            if size <= 4:
                raw = table[base + 8 : base + 8 + size]
                val_off = offset + 2 + base + 8
            else:
                val_off = self.u32(table, base + 8)
                raw = self._read(val_off, size) if size <= 4 * 1024 * 1024 else b""
                if len(raw) < size:
                    continue
            entries[tag] = IfdEntry(tag, typ, cnt, raw, val_off)
        next_ifd = None
        if len(table) >= count * 12 + 4:
            nxt = self.u32(table, count * 12)
            next_ifd = nxt if nxt else None
        return entries, next_ifd

    def value(self, e: IfdEntry) -> Any:
        if e.type == 2:
            return clean_text(e.raw, 1000)
        if e.type in (1, 6, 7):
            return e.raw if e.count > 1 or e.type == 7 else e.raw[0]
        fmt = {3: "H", 4: "I", 8: "h", 9: "i", 11: "f", 12: "d", 13: "I"}.get(e.type)
        if fmt:
            vals = struct.unpack(self.bo + fmt * e.count, e.raw[: struct.calcsize(fmt) * e.count])
            return vals[0] if e.count == 1 else list(vals)
        if e.type in (5, 10):
            fmt = "I" if e.type == 5 else "i"
            nums = struct.unpack(self.bo + fmt * (2 * e.count), e.raw[: 8 * e.count])
            pairs = [(nums[i], nums[i + 1]) for i in range(0, len(nums), 2)]
            return pairs[0] if e.count == 1 else pairs
        return e.raw

    def get(self, entries: dict[int, IfdEntry], tag: int) -> Any:
        e = entries.get(tag)
        return self.value(e) if e else None


@dataclass
class ExifData:
    make: str | None = None
    model: str | None = None
    software: str | None = None
    orientation: int | None = None
    datetime_original: str | None = None
    datetime: str | None = None
    offset_time: str | None = None
    user_comment: str | None = None
    width: int | None = None
    height: int | None = None
    thumbnail_offset: int | None = None  # relative to TIFF start
    thumbnail_length: int | None = None
    makernote: bytes | None = None
    apple: dict[str, Any] = field(default_factory=dict)
    ifd0: dict[int, IfdEntry] = field(default_factory=dict)
    exif_ifd: dict[int, IfdEntry] = field(default_factory=dict)


def parse_exif(data: bytes) -> ExifData:
    """Parse an EXIF blob (with or without the 'Exif\\0\\0' prefix)."""
    if data[:6] == b"Exif\x00\x00":
        data = data[6:]
    view = memoryview(data)

    def read(off: int, length: int) -> bytes:
        if off < 0 or off >= len(view):
            return b""
        return bytes(view[off : off + length])

    tiff = Tiff(read)
    return _exif_from_tiff(tiff)


def _exif_from_tiff(tiff: Tiff) -> ExifData:
    out = ExifData()
    ifd0, next_ifd = tiff.ifd(tiff.first_ifd)
    out.ifd0 = ifd0
    out.make = _s(tiff.get(ifd0, TAG_MAKE))
    out.model = _s(tiff.get(ifd0, TAG_MODEL))
    out.software = _s(tiff.get(ifd0, TAG_SOFTWARE))
    out.datetime = _s(tiff.get(ifd0, TAG_DATETIME))
    orientation = tiff.get(ifd0, TAG_ORIENTATION)
    if isinstance(orientation, int) and 1 <= orientation <= 8:
        out.orientation = orientation
    exif_off = tiff.get(ifd0, TAG_EXIF_IFD)
    if isinstance(exif_off, int) and exif_off > 0:
        exif_ifd, _ = tiff.ifd(exif_off)
        out.exif_ifd = exif_ifd
        out.datetime_original = _s(tiff.get(exif_ifd, TAG_DATETIME_ORIGINAL))
        out.offset_time = _s(tiff.get(exif_ifd, TAG_OFFSET_TIME_ORIGINAL))
        px, py = tiff.get(exif_ifd, TAG_PIXEL_X), tiff.get(exif_ifd, TAG_PIXEL_Y)
        if isinstance(px, int) and isinstance(py, int) and px > 0 and py > 0:
            out.width, out.height = px, py
        uc = exif_ifd.get(TAG_USER_COMMENT)
        if uc:
            out.user_comment = decode_user_comment(uc.raw, tiff.bo)
        mn = exif_ifd.get(TAG_MAKERNOTE)
        if mn and mn.raw:
            out.makernote = mn.raw
            if mn.raw.startswith(APPLE_MAKERNOTE_MAGIC):
                out.apple = parse_apple_makernote(mn.raw)
    if next_ifd:
        ifd1, _ = tiff.ifd(next_ifd)
        t_off, t_len = tiff.get(ifd1, TAG_THUMB_OFFSET), tiff.get(ifd1, TAG_THUMB_LENGTH)
        if isinstance(t_off, int) and isinstance(t_len, int) and t_off > 0 and t_len > 0:
            out.thumbnail_offset, out.thumbnail_length = t_off, t_len
    return out


def _s(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def decode_user_comment(raw: bytes, byteorder: str = ">") -> str | None:
    """EXIF UserComment: 8-byte character code prefix, then the text."""
    if not raw:
        return None
    code, body = raw[:8], raw[8:]
    try:
        if code.startswith(b"ASCII"):
            text = body.decode("ascii", "replace")
        elif code.startswith(b"UNICODE"):
            enc = "utf-16-be" if byteorder == ">" else "utf-16-le"
            if body[:2] in (b"\xff\xfe", b"\xfe\xff"):
                enc = "utf-16"
            text = body.decode(enc, "replace")
        elif code.startswith(b"JIS"):
            text = body.decode("iso2022_jp", "replace")
        elif code == b"\x00" * 8:
            text = body.decode("utf-8", "replace")
        else:
            text = raw.decode("utf-8", "replace")
    except (UnicodeDecodeError, LookupError):
        return None
    return clean_text(text, 1000)


def parse_apple_makernote(mn: bytes) -> dict[str, Any]:
    """Apple MakerNote: 'Apple iOS\\0' + version + byte order at 12, IFD at 14, offsets from the note start."""
    out: dict[str, Any] = {}
    if len(mn) < 16 or not mn.startswith(APPLE_MAKERNOTE_MAGIC):
        return out
    bo = "<" if mn[12:14] == b"II" else ">"
    view = memoryview(mn)

    def read(off: int, length: int) -> bytes:
        if off < 0 or off >= len(view):
            return b""
        return bytes(view[off : off + length])

    tiff = Tiff(read, byteorder=bo, first_ifd=14)
    try:
        entries, _ = tiff.ifd(14)
    except (struct.error, ValueError):
        return out
    cid = tiff.get(entries, APPLE_TAG_CONTENT_ID)
    if isinstance(cid, str) and cid:
        out["content_id"] = cid
    burst = tiff.get(entries, APPLE_TAG_BURST_UUID)
    if isinstance(burst, str) and burst:
        out["burst_uuid"] = burst
    capture = tiff.get(entries, APPLE_TAG_CAPTURE_TYPE)
    if isinstance(capture, int):
        out["capture_type"] = capture
    live = tiff.get(entries, APPLE_TAG_LIVE_INDEX)
    if isinstance(live, int):
        out["live_photo_video_index"] = live
    return out
