"""Shared types and helpers for the header-only metadata readers."""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HEAD_SIZE = 256 * 1024
TAIL_SIZE = 64 * 1024
MAX_HEAD = 8 * 1024 * 1024


@dataclass
class MediaInfo:
    format: str | None = None
    width: int | None = None
    height: int | None = None
    orientation: int | None = None
    make: str | None = None
    model: str | None = None
    software: str | None = None
    date_taken: str | None = None
    date_taken_ts: int | None = None
    date_source: str | None = None
    user_comment: str | None = None
    content_id: str | None = None
    duration_ms: int | None = None
    codec: str | None = None
    bitrate: int | None = None
    fps: float | None = None
    has_preview: bool = False
    preview_kind: str | None = None
    preview_offset: int | None = None
    preview_length: int | None = None
    camera_known: bool = False
    structure_ok: bool | None = None
    extractor: str = "none"
    extra: dict[str, Any] = field(default_factory=dict)

    def as_fields(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("extra", None)
        data["has_preview"] = int(bool(self.has_preview))
        return data

    def finish_dates(self, mtime_ns: int) -> None:
        """Fill date_taken from mtime when no metadata date exists; compute the sortable timestamp."""
        if not self.date_taken:
            self.date_taken = mtime_to_iso(mtime_ns)
            self.date_source = "mtime"
        self.date_taken_ts = iso_to_ts(self.date_taken)


class FileReader:
    """Random access reader that keeps the head and tail chunks in memory."""

    def __init__(self, path: Path | str, size: int | None = None):
        self.path = Path(path)
        self._fh = open(self.path, "rb", buffering=0)  # noqa: SIM115 - explicit close()
        self.size = size if size is not None else os.fstat(self._fh.fileno()).st_size
        self._head = b""
        self._tail: bytes | None = None

    def close(self) -> None:
        try:
            self._fh.close()
        except OSError:
            pass

    def __enter__(self) -> FileReader:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def head(self, n: int = HEAD_SIZE) -> bytes:
        n = min(max(n, 0), self.size, MAX_HEAD)
        if len(self._head) < n:
            self._fh.seek(0)
            self._head = self._fh.read(max(n, HEAD_SIZE if n <= HEAD_SIZE else n))
        return self._head[:n] if len(self._head) >= n else self._head

    def tail(self, n: int = TAIL_SIZE) -> bytes:
        n = min(n, self.size)
        if self._tail is None or len(self._tail) < n:
            self._fh.seek(self.size - n)
            self._tail = self._fh.read(n)
        return self._tail[-n:]

    def read_at(self, offset: int, length: int) -> bytes:
        if offset < 0 or length <= 0:
            return b""
        end = min(offset + length, self.size)
        if end <= len(self._head):
            return self._head[offset:end]
        if self._tail is not None and offset >= self.size - len(self._tail):
            start = offset - (self.size - len(self._tail))
            return self._tail[start : start + (end - offset)]
        self._fh.seek(offset)
        return self._fh.read(end - offset)


_EXIF_DT = re.compile(r"^(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})")


def exif_datetime_to_iso(text: str | None, offset: str | None = None) -> str | None:
    """'2021:06:05 14:03:22' → '2021-06-05T14:03:22' (device wall-clock time)."""
    if not text:
        return None
    m = _EXIF_DT.match(text.strip())
    if not m:
        return None
    y, mo, d, h, mi, s = (int(x) for x in m.groups())
    if y < 1900 or not (1 <= mo <= 12) or not (1 <= d <= 31) or h > 23 or mi > 59 or s > 60:
        return None
    return f"{y:04d}-{mo:02d}-{d:02d}T{h:02d}:{mi:02d}:{s:02d}"


def iso_local_from_offset_string(text: str) -> str | None:
    """'2021-06-05T14:03:22+0300' → '2021-06-05T14:03:22' (keeps the local wall-clock)."""
    m = re.match(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})", text.strip())
    if not m:
        return None
    return f"{m.group(1)}T{m.group(2)}"


def iso_to_ts(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso[:19])
    except ValueError:
        return None
    return int(dt.replace(tzinfo=UTC).timestamp())


def mtime_to_iso(mtime_ns: int) -> str:
    dt = datetime.fromtimestamp(mtime_ns / 1e9)
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def clean_text(value: bytes | str | None, limit: int = 200) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.split(b"\x00", 1)[0].decode("utf-8", "replace")
    value = value.strip().strip("\x00").strip()
    return value[:limit] if value else None
