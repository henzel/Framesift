"""TIFF / DNG (and TIFF-based RAW best effort) reader."""

from __future__ import annotations

import struct

from framesift.engine.metadata.base import FileReader, MediaInfo
from framesift.engine.metadata.exif import (
    TAG_COMPRESSION,
    TAG_IMAGE_LENGTH,
    TAG_IMAGE_WIDTH,
    TAG_NEW_SUBFILE_TYPE,
    TAG_STRIP_BYTE_COUNTS,
    TAG_STRIP_OFFSETS,
    TAG_SUB_IFDS,
    Tiff,
    _exif_from_tiff,
)
from framesift.engine.metadata.jpeg import apply_exif


def read_tiff(reader: FileReader, fmt: str = "tiff", camera_known: bool = True) -> MediaInfo:
    info = MediaInfo(format=fmt, extractor="tiff", camera_known=camera_known)
    try:
        tiff = Tiff(reader.read_at)
        exif = _exif_from_tiff(tiff)
    except (ValueError, struct.error):
        info.structure_ok = False
        return info
    info.structure_ok = bool(exif.ifd0)
    apply_exif(info, exif)
    ifds = [exif.ifd0]
    sub = tiff.get(exif.ifd0, TAG_SUB_IFDS)
    if isinstance(sub, int):
        sub = [sub]
    if isinstance(sub, list):
        for off in sub[:8]:
            try:
                entries, _ = tiff.ifd(int(off))
            except (ValueError, struct.error):
                continue
            if entries:
                ifds.append(entries)
    best_w = best_h = 0
    for entries in ifds:
        w, h = tiff.get(entries, TAG_IMAGE_WIDTH), tiff.get(entries, TAG_IMAGE_LENGTH)
        if isinstance(w, int) and isinstance(h, int) and w * h > best_w * best_h:
            best_w, best_h = w, h
        subtype = tiff.get(entries, TAG_NEW_SUBFILE_TYPE)
        comp = tiff.get(entries, TAG_COMPRESSION)
        if subtype == 1 and comp in (6, 7) and not info.has_preview:
            offs, counts = (
                tiff.get(entries, TAG_STRIP_OFFSETS),
                tiff.get(entries, TAG_STRIP_BYTE_COUNTS),
            )
            if isinstance(offs, int) and isinstance(counts, int) and counts > 0:
                if reader.read_at(offs, 2) == b"\xff\xd8":
                    info.has_preview = True
                    info.preview_kind = "jpeg"
                    info.preview_offset = offs
                    info.preview_length = counts
    if best_w and best_h:
        info.width, info.height = best_w, best_h
    if not info.has_preview and exif.thumbnail_offset and exif.thumbnail_length:
        info.has_preview = True
        info.preview_kind = "jpeg"
        info.preview_offset = exif.thumbnail_offset
        info.preview_length = exif.thumbnail_length
    return info
