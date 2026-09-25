"""Content hashes: partial (head + tail) and full BLAKE3."""

from __future__ import annotations

from pathlib import Path

import blake3

PARTIAL_CHUNK = 64 * 1024
FULL_CHUNK = 1024 * 1024


def partial_hash(path: Path, size: int | None = None) -> str:
    """BLAKE3 of the first and last 64 KB (the whole file when it is smaller than 128 KB)."""
    h = blake3.blake3()
    with open(path, "rb") as fh:
        if size is None:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(0)
        if size <= 2 * PARTIAL_CHUNK:
            h.update(fh.read())
        else:
            h.update(fh.read(PARTIAL_CHUNK))
            fh.seek(size - PARTIAL_CHUNK)
            h.update(fh.read(PARTIAL_CHUNK))
    return h.hexdigest()


def full_hash(path: Path) -> str:
    h = blake3.blake3()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(FULL_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def hash_bytes(data: bytes) -> str:
    return blake3.blake3(data).hexdigest()
