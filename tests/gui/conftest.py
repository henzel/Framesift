from __future__ import annotations

import gc
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


@pytest.fixture(autouse=True)
def _collect_garbage_on_gui_thread():
    """The GUI switches automatic garbage collection off (it only collects on the GUI thread);
    collect after every test so windows from earlier tests are finalized here, not later on a
    worker thread."""
    yield
    gc.collect()
