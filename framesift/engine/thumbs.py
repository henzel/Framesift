"""Local thumbnail cache: JPEG thumbnails in the app cache folder, LRU-limited, keyed by catalog file."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from platformdirs import user_cache_dir

from framesift import APP_ID
from framesift.engine.imaging import DecodeError, small_image, to_jpeg_bytes, video_frame

THUMB_SIDE = 320
DEFAULT_LIMIT = 2 * 1024 * 1024 * 1024


def default_cache_dir() -> Path:
    return Path(os.environ.get("FRAMESIFT_CACHE_DIR") or user_cache_dir(APP_ID, appauthor=False))


class ThumbCache:
    """Thread-safe on-disk cache. Entries are (catalog_uuid, file_id) → JPEG file, validated by size+mtime."""

    def __init__(self, root: Path | None = None, limit_bytes: int = DEFAULT_LIMIT):
        self.root = Path(root) if root else default_cache_dir() / "thumbs"
        self.root.mkdir(parents=True, exist_ok=True)
        self.limit = limit_bytes
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.root / "index.db"), check_same_thread=False, timeout=30
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=OFF")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS thumbs (catalog TEXT NOT NULL, file_id INTEGER NOT NULL, size INTEGER, "
            "mtime_ns INTEGER, bytes INTEGER NOT NULL, last_access REAL NOT NULL, PRIMARY KEY (catalog, file_id))"
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS thumbs_access ON thumbs(last_access)")
        self._conn.commit()
        self._writes = 0

    def close(self) -> None:
        with self._lock:
            if getattr(self, "_closed", False):
                return
            self._closed = True
            try:
                self._conn.commit()
            finally:
                self._conn.close()

    def _path(self, catalog: str, file_id: int) -> Path:
        return self.root / catalog[:12] / f"{file_id % 256:02x}" / f"{file_id}.jpg"

    def get(self, catalog: str, file_id: int, size: int, mtime_ns: int) -> bytes | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT size, mtime_ns FROM thumbs WHERE catalog=? AND file_id=?",
                (catalog, file_id),
            ).fetchone()
        if row is None or row[0] != size or row[1] != mtime_ns:
            return None
        path = self._path(catalog, file_id)
        try:
            data = path.read_bytes()
        except OSError:
            return None
        with self._lock:
            self._conn.execute(
                "UPDATE thumbs SET last_access=? WHERE catalog=? AND file_id=?",
                (time.time(), catalog, file_id),
            )
            self._writes += 1
            if self._writes % 200 == 0:
                self._conn.commit()
        return data

    def put(self, catalog: str, file_id: int, size: int, mtime_ns: int, data: bytes) -> None:
        path = self._path(catalog, file_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO thumbs(catalog,file_id,size,mtime_ns,bytes,last_access) VALUES(?,?,?,?,?,?)",
                (catalog, file_id, size, mtime_ns, len(data), time.time()),
            )
            self._writes += 1
            if self._writes % 50 == 0:
                self._conn.commit()
                self.trim()

    def total_bytes(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COALESCE(SUM(bytes),0) FROM thumbs").fetchone()
        return int(row[0]) if row else 0

    def trim(self, limit: int | None = None) -> int:
        """Evict least recently used entries until the total is under the limit. Returns entries removed."""
        limit = self.limit if limit is None else limit
        removed = 0
        with self._lock:
            total = self.total_bytes()
            if total <= limit * 1.05:
                return 0
            rows = self._conn.execute(
                "SELECT catalog, file_id, bytes FROM thumbs ORDER BY last_access ASC LIMIT 5000"
            ).fetchall()
            for catalog, file_id, nbytes in rows:
                if total <= limit:
                    break
                try:
                    self._path(catalog, file_id).unlink()
                except OSError:
                    pass
                self._conn.execute(
                    "DELETE FROM thumbs WHERE catalog=? AND file_id=?", (catalog, file_id)
                )
                total -= nbytes
                removed += 1
            self._conn.commit()
        return removed

    def clear(self) -> None:
        with self._lock:
            rows = self._conn.execute("SELECT catalog, file_id FROM thumbs").fetchall()
            for catalog, file_id in rows:
                try:
                    self._path(catalog, file_id).unlink()
                except OSError:
                    pass
            self._conn.execute("DELETE FROM thumbs")
            self._conn.commit()


def render_thumbnail(path: Path, meta: dict[str, Any], *, side: int = THUMB_SIDE) -> bytes:
    """JPEG thumbnail bytes for a photo or video. Raises DecodeError."""
    kind = meta.get("kind")
    if kind == "video":
        duration = meta.get("duration_ms") or 0
        at = min(1.0, duration / 10000) if duration else 0.0
        im = video_frame(path, at_seconds=at, max_side=side)
        if im is None:
            raise DecodeError("ffmpeg could not extract a frame")
        return to_jpeg_bytes(im)
    im, _source = small_image(path, meta, side)
    return to_jpeg_bytes(im)
