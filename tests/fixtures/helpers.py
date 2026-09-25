"""Helpers shared by the test modules (kept out of conftest.py so both conftests stay unique)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from framesift.engine.external import have


def snapshot(root: Path, *, skip_catalog: bool = True) -> dict[str, tuple[int, int, str]]:
    """{relative path: (size, mtime_ns, hash)} for every file under root."""
    out: dict[str, tuple[int, int, str]] = {}
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if skip_catalog and "/.framesift/" in f"/{rel}":
            continue
        st = p.stat()
        out[rel] = (st.st_size, st.st_mtime_ns, hashlib.blake2b(p.read_bytes()).hexdigest())
    return out


def content_multiset(root: Path) -> dict[str, int]:
    """Hash → count of every file under root (catalog excluded): what 'nothing deleted' means."""
    counts: dict[str, int] = {}
    for p in root.rglob("*"):
        if p.is_file() and "/.framesift/" not in f"/{p.relative_to(root).as_posix()}":
            h = hashlib.blake2b(p.read_bytes()).hexdigest()
            counts[h] = counts.get(h, 0) + 1
    return counts


needs_ffmpeg = pytest.mark.skipif(not have("ffmpeg"), reason="ffmpeg not available")
