"""GUI smoke tests (offscreen): start, open a folder, review with → / ← / undo, journal, purge dialog."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import Qt

from framesift.gui.main_window import MainWindow
from framesift.gui.prefs import Prefs

pytestmark = pytest.mark.gui


@pytest.fixture
def window(qtbot, tmp_path: Path):
    prefs = Prefs(tmp_path / "prefs.json")
    win = MainWindow(prefs, cache_dir=tmp_path / "thumbs")
    qtbot.addWidget(win)
    win.show()
    yield win
    win.gws.shutdown()


def open_and_scan(qtbot, window: MainWindow, root: Path) -> None:
    with qtbot.waitSignal(window.gws.job_finished, timeout=60000):
        assert window.open_source(str(root))
    qtbot.waitUntil(lambda: window.browser.model.rowCount() > 0, timeout=10000)


def test_open_folder_and_browse(qtbot, window: MainWindow, fresh) -> None:
    root, manifest = fresh
    open_and_scan(qtbot, window, root)
    assert window.pages.currentWidget() is window.browser
    model = window.browser.model
    assert model.rowCount() >= 20
    # rows resolve lazily and thumbnails arrive asynchronously
    row = model.row_at(0)
    assert row is not None and row["root"] == "source"
    model.data(model.index(0), Qt.ItemDataRole.DecorationRole)
    qtbot.waitUntil(lambda: model.has_thumbnail(model.ids[0]), timeout=30000)
    # filter to photos and sort by name
    window.browser.filter_box.setCurrentIndex(1)
    assert all(model.row_at(i)["kind"] in ("photo", "raw") for i in range(model.rowCount()))
    window.browser.sort_box.setCurrentIndex(1)
    names = [model.row_at(i)["name"].lower() for i in range(model.rowCount())]
    assert names == sorted(names)
    # tree shows the three roots
    tree = window.browser.tree_model
    assert tree.rowCount() == 3 and len(tree.source_item.children) >= 5


def test_review_keys_keep_delete_undo(qtbot, window: MainWindow, fresh) -> None:
    root, _ = fresh
    open_and_scan(qtbot, window, root)
    window.browser.filter_box.setCurrentIndex(1)  # photos only: no video playback offscreen
    window.browser.sort_box.setCurrentIndex(1)
    model = window.browser.model
    first_id, second_id = model.ids[0], model.ids[1]
    window.browser._open_at(0)
    assert window.pages.currentWidget() is window.review
    review = window.review
    assert review.current_id == first_id
    qtbot.keyClick(review, Qt.Key.Key_Right)  # keep
    assert window.gws.wait_actions()
    assert window.gws.file_row(first_id)["reviewed_at"]
    assert review.current_id == second_id
    second_row = window.gws.file_row(second_id)
    qtbot.keyClick(review, Qt.Key.Key_Left)  # to delete
    assert window.gws.wait_actions()
    moved = window.gws.file_row(second_id)
    assert (
        moved["root"] == "delete" and (root / "_framesift_delete" / second_row["rel_path"]).exists()
    )
    assert second_id not in review.ids
    qtbot.keyClick(review, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)  # undo the delete
    assert window.gws.wait_actions()
    assert window.gws.file_row(second_id)["root"] == "source"
    assert review.current_id == second_id
    qtbot.keyClick(review, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)  # undo the keep
    assert window.gws.wait_actions()
    assert window.gws.file_row(first_id)["reviewed_at"] is None
    assert review.current_id == first_id
    qtbot.keyClick(review, Qt.Key.Key_Down)  # skip
    assert review.current_id == second_id
    qtbot.keyClick(review, Qt.Key.Key_I)
    assert not review.info.isVisible()
    qtbot.keyClick(review, Qt.Key.Key_Escape)
    assert window.pages.currentWidget() is window.browser
    counters = window.gws.session_counters()
    assert counters["kept"] == 0 and counters["to_delete"] == 0


def test_journal_lists_actions_and_purge_preview(qtbot, window: MainWindow, fresh) -> None:
    root, _ = fresh
    open_and_scan(qtbot, window, root)
    model = window.browser.model
    fid = model.ids[0]
    window.gws.to_delete(fid)
    assert window.gws.wait_actions()
    window.show_page("journal")
    qtbot.waitUntil(lambda: window.journal.model.rowCount() >= 1, timeout=5000)
    assert window.journal.model.rows[0]["action"] == "to_delete"
    from framesift.engine import api

    assert api.status(window.gws.ws)["purge"]["files"] >= 1
    # undo everything through the journal page's engine path (no dialog)
    with qtbot.waitSignal(window.gws.job_finished, timeout=30000):
        window.gws.run_job(
            "undo",
            lambda progress, control: api.run_undo(
                window.gws.ws, "all", dry_run=False, progress=progress, control=control
            ),
        )
    assert window.gws.file_row(fid)["root"] == "source"


def test_refuses_apple_photos_library(
    qtbot, window: MainWindow, tmp_path: Path, monkeypatch
) -> None:
    from PySide6.QtWidgets import QMessageBox

    lib = tmp_path / "Photos Library.photoslibrary" / "originals"
    lib.mkdir(parents=True)
    shown: list[str] = []
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: shown.append(a[2]))
    assert not window.open_source(str(lib))
    assert shown and "Apple Photos" in shown[0]
