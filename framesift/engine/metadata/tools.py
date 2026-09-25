"""Optional external readers: exiftool (RAW and fallback) and ffprobe (video fallback)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from framesift.engine.external import find_binary, run_hidden
from framesift.engine.metadata.base import (
    MediaInfo,
    clean_text,
    exif_datetime_to_iso,
    iso_local_from_offset_string,
)

EXIFTOOL_TAGS = [
    "-Make",
    "-Model",
    "-Software",
    "-DateTimeOriginal",
    "-CreateDate",
    "-OffsetTimeOriginal",
    "-Orientation",
    "-ImageWidth",
    "-ImageHeight",
    "-UserComment",
    "-ContentIdentifier",
    "-Duration",
    "-CompressorID",
    "-VideoFrameRate",
    "-AvgBitrate",
    "-PreviewImageLength",
    "-JpgFromRawLength",
    "-ThumbnailLength",
    "-FileType",
    "-MIMEType",
]


def exiftool_info(path: Path, fmt_hint: str | None = None) -> MediaInfo | None:
    exe = find_binary("exiftool")
    if not exe:
        return None
    try:
        proc = run_hidden(
            [exe, "-j", "-n", "-fast2", "-charset", "filename=utf8", *EXIFTOOL_TAGS, str(path)],
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode not in (0, 1) or not proc.stdout:
        return None
    try:
        data = json.loads(proc.stdout.decode("utf-8", "replace"))[0]
    except (ValueError, IndexError, KeyError):
        return None
    info = MediaInfo(
        format=(fmt_hint or str(data.get("FileType", "")).lower() or None), extractor="exiftool"
    )
    info.camera_known = True
    info.make = clean_text(str(data["Make"])) if data.get("Make") else None
    info.model = clean_text(str(data["Model"])) if data.get("Model") else None
    info.software = clean_text(str(data["Software"])) if data.get("Software") else None
    orientation = data.get("Orientation")
    if isinstance(orientation, int) and 1 <= orientation <= 8:
        info.orientation = orientation
    w, h = data.get("ImageWidth"), data.get("ImageHeight")
    if isinstance(w, int) and isinstance(h, int):
        info.width, info.height = w, h
    dt = data.get("DateTimeOriginal") or data.get("CreateDate")
    if dt:
        iso = exif_datetime_to_iso(str(dt)) or iso_local_from_offset_string(str(dt))
        if iso:
            info.date_taken, info.date_source = iso, "exiftool"
    if data.get("UserComment"):
        info.user_comment = clean_text(str(data["UserComment"]))
    if data.get("ContentIdentifier"):
        info.content_id = clean_text(str(data["ContentIdentifier"]))
    duration = data.get("Duration")
    if isinstance(duration, int | float) and duration > 0:
        info.duration_ms = int(duration * 1000)
    if data.get("CompressorID"):
        info.codec = {"hvc1": "hevc", "hev1": "hevc", "avc1": "h264", "mp4v": "mpeg4"}.get(
            str(data["CompressorID"]), str(data["CompressorID"])
        )
    if isinstance(data.get("VideoFrameRate"), int | float):
        info.fps = float(data["VideoFrameRate"])
    if isinstance(data.get("AvgBitrate"), int | float):
        info.bitrate = int(data["AvgBitrate"])
    if any(data.get(k) for k in ("PreviewImageLength", "JpgFromRawLength", "ThumbnailLength")):
        info.has_preview = True
        info.preview_kind = "exiftool"
    info.structure_ok = True
    return info


def exiftool_preview(path: Path) -> bytes | None:
    """Largest embedded JPEG preview of a RAW file, as bytes."""
    exe = find_binary("exiftool")
    if not exe:
        return None
    for tag in ("-PreviewImage", "-JpgFromRaw", "-ThumbnailImage"):
        try:
            proc = run_hidden([exe, "-b", tag, str(path)], timeout=60)
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode == 0 and proc.stdout[:2] == b"\xff\xd8":
            return proc.stdout
    return None


def ffprobe_info(path: Path) -> MediaInfo | None:
    exe = find_binary("ffprobe")
    if not exe:
        return None
    try:
        proc = run_hidden(
            [
                exe,
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(path),
            ],
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    try:
        data = json.loads(proc.stdout.decode("utf-8", "replace"))
    except ValueError:
        return None
    fmt = data.get("format", {})
    info = MediaInfo(
        format=str(fmt.get("format_name", "")).split(",")[0] or None,
        extractor="ffprobe",
        camera_known=True,
    )
    info.structure_ok = True
    try:
        info.duration_ms = int(float(fmt.get("duration", 0)) * 1000) or None
    except ValueError:
        pass
    try:
        info.bitrate = int(fmt.get("bit_rate")) if fmt.get("bit_rate") else None
    except ValueError:
        pass
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    info.make = clean_text(tags.get("com.apple.quicktime.make"))
    info.model = clean_text(tags.get("com.apple.quicktime.model"))
    info.software = clean_text(tags.get("com.apple.quicktime.software") or tags.get("encoder"))
    info.content_id = clean_text(tags.get("com.apple.quicktime.content.identifier"))
    cdate = tags.get("com.apple.quicktime.creationdate") or tags.get("creation_time")
    if cdate:
        iso = iso_local_from_offset_string(str(cdate))
        if iso:
            info.date_taken, info.date_source = iso, "ffprobe"
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video":
            info.codec = stream.get("codec_name")
            info.width, info.height = stream.get("width"), stream.get("height")
            rate = stream.get("avg_frame_rate") or "0/1"
            try:
                num, den = rate.split("/")
                info.fps = round(int(num) / int(den), 3) if int(den) else None
            except (ValueError, ZeroDivisionError):
                pass
            rotation = None
            for sd in stream.get("side_data_list", []) or []:
                if "rotation" in sd:
                    rotation = int(sd["rotation"])
            if rotation is not None:
                info.orientation = {90: 6, -90: 8, 270: 8, 180: 3, -180: 3}.get(
                    rotation % 360 if rotation >= 0 else rotation
                )
            break
    return info
