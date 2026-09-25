from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_LOGGING_RULES", "qt.multimedia.*=false")

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")


@pytest.fixture(autouse=True)
def _isolated_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMESIFT_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("FRAMESIFT_CONFIG_DIR", str(tmp_path / "config"))
