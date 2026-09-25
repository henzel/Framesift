"""Shared fixtures: a synthetic dataset built once per session, copied per test when mutated."""

from __future__ import annotations

import hashlib
import shutil
import sys
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(FIXTURES))

from make_dataset import make_dataset  # noqa: E402

from framesift.engine.external import have  # noqa: E402


@pytest.fixture(scope="session")
def dataset(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, list[str]]]:
    root = tmp_path_factory.mktemp("dataset") / "archive"
    manifest = make_dataset(root, with_videos=have("ffmpeg"))
    return root, manifest


@pytest.fixture
def fresh(
    dataset: tuple[Path, dict[str, list[str]]], tmp_path: Path
) -> tuple[Path, dict[str, list[str]]]:
    """A private copy of the dataset (mtimes preserved) for tests that move files."""
    src, manifest = dataset
    dst = tmp_path / "archive"
    shutil.copytree(src, dst, copy_function=shutil.copy2)
    return dst, manifest


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
