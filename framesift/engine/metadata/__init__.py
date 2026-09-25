"""Header-only metadata extraction: dispatch by magic bytes, fall back to external tools."""

from __future__ import annotations

from pathlib import Path

from framesift.engine.config import RAW_EXTS, VIDEO_EXTS
from framesift.engine.metadata.base import FileReader, MediaInfo
from framesift.engine.metadata.isobmff import HEIF_BRANDS, read_heif, read_mp4
from framesift.engine.metadata.jpeg import read_jpeg
from framesift.engine.metadata.simple import read_gif, read_png, read_webp
from framesift.engine.metadata.tiff_dng import read_tiff
from framesift.engine.metadata.tools import exiftool_info, ffprobe_info

__all__ = ["FileReader", "MediaInfo", "read_media_info", "detect_format"]

TIFF_RAW_FORMATS = {
    "cr2": "cr2",
    "nef": "nef",
    "arw": "arw",
    "pef": "pef",
    "srw": "srw",
    "dng": "dng",
    "orf": "orf",
    "rw2": "rw2",
}


def detect_format(head: bytes, ext: str) -> str:
    """Container family from magic bytes: jpeg png webp gif tiff heif isobmff raf crx unknown."""
    if head[:2] == b"\xff\xd8":
        return "jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:4] in (b"II*\x00", b"MM\x00*", b"IIRO", b"MMOR", b"IIRS", b"IIU\x00"):
        return "tiff"
    if head[:16] == b"FUJIFILMCCD-RAW ":
        return "raf"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand == b"crx ":
            return "crx"
        if brand in HEIF_BRANDS:
            return "heif"
        return "isobmff"
    if len(head) >= 8 and head[4:8] in (b"moov", b"mdat", b"wide", b"free", b"skip", b"pnot"):
        return "isobmff"
    return "unknown"


def read_media_info(
    path: Path, ext: str, size: int, mtime_ns: int, *, allow_external: bool = True
) -> MediaInfo:
    """Extract metadata for one file. Raises OSError for unreadable files."""
    if size == 0:
        empty = MediaInfo(format=None, extractor="empty", structure_ok=False)
        empty.finish_dates(mtime_ns)
        return empty
    with FileReader(path, size) as reader:
        head = reader.head(64)
        family = detect_format(head, ext)
        info: MediaInfo | None = None
        try:
            if family == "jpeg":
                info = read_jpeg(reader)
            elif family == "png":
                info = read_png(reader)
            elif family == "webp":
                info = read_webp(reader)
            elif family == "gif":
                info = read_gif(reader)
            elif family == "heif":
                info = read_heif(reader)
            elif family == "isobmff":
                info = read_mp4(reader)
            elif family == "tiff":
                fmt = TIFF_RAW_FORMATS.get(ext, "tiff")
                # TIFF-based RAW: our parser gets what it can; camera data is trusted only via exiftool
                info = read_tiff(reader, fmt=fmt, camera_known=(fmt in ("tiff", "dng")))
                if fmt not in ("tiff", "dng") and allow_external:
                    ext_info = exiftool_info(path, fmt)
                    if ext_info:
                        ext_info.structure_ok = True
                        info = ext_info
            elif family in ("raf", "crx"):
                raw_info = (
                    exiftool_info(path, "raf" if family == "raf" else "cr3")
                    if allow_external
                    else None
                )
                info = raw_info
                if info is None:
                    info = MediaInfo(
                        format="raf" if family == "raf" else "cr3",
                        extractor="magic",
                        camera_known=False,
                        structure_ok=True,
                    )
        except Exception as exc:  # parser bug or hostile file: never fail the scan
            info = MediaInfo(
                format=family, extractor="error", camera_known=False, structure_ok=None
            )
            info.extra["error"] = f"{type(exc).__name__}: {exc}"[:200]
        if info is None:
            info = MediaInfo(format=None, extractor="magic", camera_known=False, structure_ok=False)
            if ext in RAW_EXTS and allow_external:
                alt = exiftool_info(path)
                if alt:
                    info = alt
        # External fallbacks when the pure-Python reader found nothing useful
        if (
            allow_external
            and info.extractor in ("magic", "error")
            and (ext in VIDEO_EXTS or family == "isobmff")
        ):
            alt = ffprobe_info(path)
            if alt:
                info = alt
        if (
            allow_external
            and info.extractor == "isobmff"
            and info.structure_ok
            and family == "isobmff"
            and info.duration_ms is None
        ):
            alt = ffprobe_info(path)
            if alt and alt.duration_ms:
                info = alt
    info.finish_dates(mtime_ns)
    return info
