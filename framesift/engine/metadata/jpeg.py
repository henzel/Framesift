"""JPEG header reader: SOF dimensions, APP1 EXIF, embedded thumbnail location."""

from __future__ import annotations

import struct

from framesift.engine.metadata.base import FileReader, MediaInfo, exif_datetime_to_iso
from framesift.engine.metadata.exif import ExifData, parse_exif

SOF_MARKERS = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
STANDALONE = {0xD8, 0x01, *range(0xD0, 0xD8)}


def read_jpeg(reader: FileReader) -> MediaInfo:
    info = MediaInfo(format="jpeg", extractor="jpeg")
    head = reader.head()
    if head[:2] != b"\xff\xd8":
        info.structure_ok = False
        return info
    pos = 2
    found_sof = found_sos = False
    exif: ExifData | None = None
    exif_file_offset = 0
    while pos + 4 <= len(head) or pos + 4 <= reader.size:
        if pos + 4 > len(head):
            head = reader.head(min(len(head) * 2, reader.size))
            if pos + 4 > len(head):
                break
        if head[pos] != 0xFF:
            pos += 1
            continue
        marker = head[pos + 1]
        if marker == 0xFF:
            pos += 1
            continue
        if marker in STANDALONE:
            pos += 2
            continue
        if marker == 0xD9:
            break
        length = struct.unpack(">H", head[pos + 2 : pos + 4])[0]
        if length < 2:
            break
        seg_start, seg_end = pos + 4, pos + 2 + length
        if seg_end > len(head):
            head = reader.head(seg_end)
            if seg_end > len(head):
                break
        segment = head[seg_start:seg_end]
        if marker == 0xE1 and segment[:6] == b"Exif\x00\x00" and exif is None:
            try:
                exif = parse_exif(segment)
                exif_file_offset = seg_start + 6
            except (ValueError, struct.error):
                exif = None
        elif marker in SOF_MARKERS and len(segment) >= 5:
            info.height, info.width = struct.unpack(">HH", segment[1:5])
            found_sof = True
        elif marker == 0xDA:
            found_sos = True
            break
        pos = seg_end
    info.structure_ok = found_sof and found_sos
    info.camera_known = True
    if exif:
        apply_exif(info, exif)
        if exif.thumbnail_offset and exif.thumbnail_length:
            off = exif_file_offset + exif.thumbnail_offset
            if off + exif.thumbnail_length <= reader.size:
                info.has_preview = True
                info.preview_kind = "jpeg"
                info.preview_offset = off
                info.preview_length = exif.thumbnail_length
    return info


def apply_exif(info: MediaInfo, exif: ExifData) -> None:
    info.make = exif.make
    info.model = exif.model
    info.software = exif.software
    if exif.orientation:
        info.orientation = exif.orientation
    info.user_comment = exif.user_comment
    iso = exif_datetime_to_iso(exif.datetime_original) or exif_datetime_to_iso(exif.datetime)
    if iso:
        info.date_taken = iso
        info.date_source = "exif"
    if exif.apple.get("content_id"):
        info.content_id = exif.apple["content_id"]
    if exif.apple:
        info.extra["apple"] = exif.apple
    if not info.width and exif.width:
        info.width, info.height = exif.width, exif.height
