"""PNG, WebP and GIF readers."""

from __future__ import annotations

import struct
import zlib

from framesift.engine.metadata.base import FileReader, MediaInfo
from framesift.engine.metadata.exif import parse_exif
from framesift.engine.metadata.jpeg import apply_exif

PNG_SIG = b"\x89PNG\r\n\x1a\n"


def read_png(reader: FileReader) -> MediaInfo:
    info = MediaInfo(format="png", extractor="png", camera_known=True)
    head = reader.head()
    if head[:8] != PNG_SIG:
        info.structure_ok = False
        return info
    pos = 8
    exif_blob: bytes | None = None
    saw_ihdr = saw_idat = False
    texts: dict[str, str] = {}
    while pos + 8 <= len(head):
        length, ctype = struct.unpack(">I4s", head[pos : pos + 8])
        data_start, data_end = pos + 8, pos + 8 + length
        if ctype == b"IDAT":
            saw_idat = True
            break
        if data_end + 4 > len(head):
            if data_end + 4 <= reader.size and length < 4 * 1024 * 1024:
                head = reader.head(data_end + 4)
                if data_end + 4 > len(head):
                    break
            else:
                break
        chunk = head[data_start:data_end]
        if ctype == b"IHDR" and length >= 8:
            info.width, info.height = struct.unpack(">II", chunk[:8])
            saw_ihdr = True
        elif ctype == b"eXIf":
            exif_blob = chunk
        elif ctype == b"tEXt":
            key, _, value = chunk.partition(b"\x00")
            texts[key.decode("latin-1", "replace")] = value.decode("latin-1", "replace")[:200]
        elif ctype == b"iTXt":
            key, _, rest = chunk.partition(b"\x00")
            if len(rest) >= 2:
                comp_flag = rest[0]
                rest = rest[2:]
                _lang, _, rest = rest.partition(b"\x00")
                _tkey, _, text = rest.partition(b"\x00")
                if comp_flag == 1:
                    try:
                        text = zlib.decompress(text)
                    except zlib.error:
                        text = b""
                texts[key.decode("latin-1", "replace")] = text.decode("utf-8", "replace")[:200]
        pos = data_end + 4
    if exif_blob is None:
        exif_blob = _exif_from_tail(reader.tail())
    tail = reader.tail(64)
    info.structure_ok = saw_ihdr and saw_idat and b"IEND" in tail
    if texts:
        info.extra["png_text"] = texts
    if exif_blob:
        try:
            apply_exif(info, parse_exif(exif_blob))
        except (ValueError, struct.error):
            pass
    return info


def _exif_from_tail(tail: bytes) -> bytes | None:
    idx = tail.rfind(b"eXIf")
    if idx < 4:
        return None
    length = struct.unpack(">I", tail[idx - 4 : idx])[0]
    data = tail[idx + 4 : idx + 4 + length]
    return data if len(data) == length and length > 8 else None


def read_webp(reader: FileReader) -> MediaInfo:
    info = MediaInfo(format="webp", extractor="webp", camera_known=True)
    head = reader.head()
    if len(head) < 16 or head[:4] != b"RIFF" or head[8:12] != b"WEBP":
        info.structure_ok = False
        return info
    pos = 12
    exif_blob: bytes | None = None
    while pos + 8 <= len(head):
        fourcc = head[pos : pos + 4]
        size = struct.unpack("<I", head[pos + 4 : pos + 8])[0]
        data = head[pos + 8 : pos + 8 + size]
        if fourcc == b"VP8X" and len(data) >= 10:
            info.width = 1 + (data[4] | data[5] << 8 | data[6] << 16)
            info.height = 1 + (data[7] | data[8] << 8 | data[9] << 16)
        elif fourcc == b"VP8 " and len(data) >= 10 and info.width is None:
            info.width = struct.unpack("<H", data[6:8])[0] & 0x3FFF
            info.height = struct.unpack("<H", data[8:10])[0] & 0x3FFF
        elif fourcc == b"VP8L" and len(data) >= 5 and info.width is None:
            bits = struct.unpack("<I", data[1:5])[0]
            info.width = (bits & 0x3FFF) + 1
            info.height = ((bits >> 14) & 0x3FFF) + 1
        elif fourcc == b"EXIF":
            exif_blob = data
        pos += 8 + size + (size & 1)
        if fourcc in (b"VP8 ", b"VP8L", b"ALPH", b"ANIM"):
            if exif_blob is not None or reader.size <= len(head):
                break
    info.structure_ok = info.width is not None
    if exif_blob:
        try:
            apply_exif(info, parse_exif(exif_blob))
        except (ValueError, struct.error):
            pass
    return info


def read_gif(reader: FileReader) -> MediaInfo:
    info = MediaInfo(format="gif", extractor="gif", camera_known=True)
    head = reader.head(16)
    if len(head) < 10 or head[:6] not in (b"GIF87a", b"GIF89a"):
        info.structure_ok = False
        return info
    info.width, info.height = struct.unpack("<HH", head[6:10])
    info.structure_ok = reader.tail(1) == b";" if reader.size > 16 else True
    return info
