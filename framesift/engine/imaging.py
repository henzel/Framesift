"""Decoding helpers: small representations for analysis, thumbnails, previews.

Decoders, cheapest first: embedded JPEG preview → HEIF thumbnail (pi-heif) → reduced JPEG decode →
full decode → ffmpeg (HEIC fallback and video frames).
"""

from __future__ import annotations

import io
import subprocess
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from framesift.engine.external import find_binary, run_hidden
from framesift.engine.paths import to_os

_HEIF_REGISTERED = False


def register_heif() -> bool:
    global _HEIF_REGISTERED
    if _HEIF_REGISTERED:
        return True
    try:
        from pi_heif import register_heif_opener

        register_heif_opener()
        _HEIF_REGISTERED = True
    except ImportError:  # pragma: no cover
        return False
    return True


class DecodeError(Exception):
    pass


def apply_orientation(im: Image.Image, orientation: int | None = None) -> Image.Image:
    """Rotate/flip according to EXIF orientation (from the image or the given value)."""
    if orientation and orientation != 1:
        method = {
            2: Image.Transpose.FLIP_LEFT_RIGHT,
            3: Image.Transpose.ROTATE_180,
            4: Image.Transpose.FLIP_TOP_BOTTOM,
            5: Image.Transpose.TRANSPOSE,
            6: Image.Transpose.ROTATE_270,
            7: Image.Transpose.TRANSVERSE,
            8: Image.Transpose.ROTATE_90,
        }.get(orientation)
        if method is not None:
            return im.transpose(method)
        return im
    try:
        return ImageOps.exif_transpose(im) or im
    except Exception:
        return im


def load_embedded_jpeg(path: Path, offset: int, length: int) -> Image.Image | None:
    if not offset or not length or length > 32 * 1024 * 1024:
        return None
    with open(to_os(path), "rb") as fh:
        fh.seek(offset)
        data = fh.read(length)
    if data[:2] != b"\xff\xd8":
        return None
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
        return im
    except Exception:
        return None


def heif_orientation(path: Path) -> tuple[bool, int | None]:
    """(libheif applies a transform: irot/imir present, EXIF-style orientation of the file)."""
    from framesift.engine.metadata.base import FileReader
    from framesift.engine.metadata.isobmff import parse_heif_meta, read_heif

    try:
        with FileReader(path) as reader:
            meta = parse_heif_meta(reader.read_at, reader.size)
            primary = meta.primary
            has_transform = primary is not None and (
                meta.find_prop(primary, b"irot") is not None
                or meta.find_prop(primary, b"imir") is not None
            )
            info = read_heif(reader)
            return has_transform, info.orientation
    except Exception:
        return False, None


def decode_heif(
    path: Path,
    *,
    max_side: int | None = None,
    prefer_thumbnail: bool = True,
    force_ffmpeg: bool = False,
) -> tuple[Image.Image, str]:
    """Decode a HEIF/HEIC image upright. Returns (image, source) with source in
    heif_thumb | decode | ffmpeg. libheif applies irot/imir itself; the EXIF orientation
    is applied here when no irot/imir exists; the ffmpeg path decodes raw (-noautorotate)
    and the orientation is applied here for every FFmpeg version alike."""
    has_transform, orientation = heif_orientation(path)
    last: Exception = ImportError("pi_heif")
    if not force_ffmpeg and register_heif():
        try:
            import pi_heif

            heif = pi_heif.open_heif(to_os(path), convert_hdr_to_8bit=True)
            im: Image.Image | None = None
            source = "decode"
            if max_side and prefer_thumbnail:
                thumbs = [
                    t for t in getattr(heif, "thumbnails", []) if max(t.size) >= max_side // 2
                ]
                if thumbs:
                    im = min(thumbs, key=lambda t: max(t.size)).to_pillow()
                    source = "heif_thumb"
            if im is None:
                im = heif.to_pillow()
            if not has_transform and orientation and orientation != 1:
                im = apply_orientation(im, orientation)
            return im, source
        except Exception as exc:
            last = exc
    im2 = ffmpeg_decode_image(path)
    if im2 is None:
        raise DecodeError(f"HEIF decode failed: {last}")
    if orientation and orientation != 1:
        im2 = apply_orientation(im2, orientation)
    return im2, "ffmpeg"


def ffmpeg_decode_image(path: Path, max_side: int | None = None) -> Image.Image | None:
    """Decode a still image (HEIC/AVIF/…) through ffmpeg (LGPL build, FFmpeg ≥ 7.1 for HEIF grids)."""
    exe = find_binary("ffmpeg")
    if not exe:
        return None
    args = [exe, "-v", "error", "-nostdin", "-noautorotate", "-i", str(path), "-frames:v", "1"]
    if max_side:
        args += [
            "-vf",
            f"scale='min({max_side},iw)':'min({max_side},ih)':force_original_aspect_ratio=decrease",
        ]
    args += ["-f", "image2pipe", "-vcodec", "ppm", "-"]
    try:
        proc = run_hidden(args, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    try:
        im = Image.open(io.BytesIO(proc.stdout))
        im.load()
        return im
    except Exception:
        return None


def video_frame(path: Path, *, at_seconds: float = 1.0, max_side: int = 320) -> Image.Image | None:
    """One frame of a video as a PIL image, via ffmpeg."""
    exe = find_binary("ffmpeg")
    if not exe:
        return None
    args = [
        exe,
        "-v",
        "error",
        "-nostdin",
        "-ss",
        f"{at_seconds:.3f}",
        "-i",
        str(path),
        "-frames:v",
        "1",
        "-vf",
        f"scale='min({max_side},iw)':'min({max_side},ih)':force_original_aspect_ratio=decrease",
        "-f",
        "image2pipe",
        "-vcodec",
        "ppm",
        "-",
    ]
    try:
        proc = run_hidden(args, timeout=120)
        if (proc.returncode != 0 or not proc.stdout) and at_seconds > 0:
            proc = run_hidden(args[:5] + ["0"] + args[6:], timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    try:
        im = Image.open(io.BytesIO(proc.stdout))
        im.load()
        return im
    except Exception:
        return None


def open_full(path: Path, fmt: str | None, ext: str, orientation: int | None = None) -> Image.Image:
    """Full, upright decode of a photo (any supported format). Raises DecodeError."""
    family = fmt or ext
    try:
        if family in ("heic", "heif", "hif", "avif"):
            im, _source = decode_heif(path, prefer_thumbnail=False)
            return im
        im = Image.open(to_os(path))
        im.load()
        return apply_orientation(im, orientation)
    except DecodeError:
        raise
    except Exception as exc:
        raise DecodeError(f"{type(exc).__name__}: {exc}") from exc


def small_image(path: Path, meta: dict[str, Any], max_side: int = 256) -> tuple[Image.Image, str]:
    """Small RGB/gray image for analysis plus the name of the source used. Raises DecodeError."""
    fmt = meta.get("format") or ""
    ext = (meta.get("ext") or "").lower()
    orientation = meta.get("orientation")
    kind = meta.get("preview_kind")
    if kind == "jpeg" and meta.get("preview_offset"):
        im = load_embedded_jpeg(path, int(meta["preview_offset"]), int(meta["preview_length"] or 0))
        if im is not None and max(im.size) >= 96:
            return _shrink(apply_orientation(im, orientation), max_side), "embedded"
    if fmt in ("heic", "heif", "avif") or ext in ("heic", "heif", "hif"):
        im, source = decode_heif(path, max_side=max_side, prefer_thumbnail=True)
        return _shrink(im, max_side), source
    if fmt in ("cr2", "nef", "arw", "orf", "rw2", "raf", "pef", "srw", "cr3") or ext in (
        "cr2",
        "nef",
        "arw",
        "orf",
        "rw2",
        "raf",
        "pef",
        "srw",
        "cr3",
    ):
        from framesift.engine.metadata.tools import exiftool_preview

        data = exiftool_preview(path)
        if not data:
            raise DecodeError("no RAW preview available")
        im = Image.open(io.BytesIO(data))
        im.load()
        return _shrink(apply_orientation(im, orientation), max_side), "raw_preview"
    try:
        im = Image.open(to_os(path))
        if im.format == "JPEG":
            im.draft("RGB", (max_side * 2, max_side * 2))
        im.load()
    except Exception as exc:
        raise DecodeError(f"{type(exc).__name__}: {exc}") from exc
    return _shrink(apply_orientation(im, orientation), max_side), "decode"


def _shrink(im: Image.Image, max_side: int) -> Image.Image:
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    if max(im.size) > max_side:
        im = im.copy()
        im.thumbnail((max_side, max_side), Image.Resampling.BILINEAR)
    return im


def to_jpeg_bytes(im: Image.Image, quality: int = 85) -> bytes:
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()
