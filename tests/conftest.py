"""Shared fixtures: a synthetic dataset built once per session, copied per test when mutated."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures"
if str(FIXTURES) not in sys.path:
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
