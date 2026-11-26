"""GUI smoke tests (offscreen): start, open a folder, review with → / ← / undo, journal, purge dialog."""

from __future__ import annotations

from pathlib import Path
from threading import Event, get_ident

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
    qtbot.waitUntil(lambda: window.browser.tree_model.source_item is not None, timeout=5000)


def wait_gallery(qtbot, window: MainWindow) -> None:
    model = window.browser.model
    qtbot.waitUntil(lambda: not model.loading)
    qtbot.waitUntil(lambda: model.row_at(0) is not None)


def test_open_folder_and_browse(qtbot, window: MainWindow, fresh) -> None:
    root, manifest = fresh
    open_and_scan(qtbot, window, root)
    assert window.pages.currentWidget() is window.browser
    model = window.browser.model
    assert model.rowCount() >= 20
    # rows resolve lazily and thumbnails arrive asynchronously
    wait_gallery(qtbot, window)
    row = model.row_at(0)
    assert row is not None and row["root"] == "source"
    pos = next(i for i in range(model.rowCount()) if model.index(i).data() == "IMG_0001.JPG")
    index = model.index(pos)
    window.browser.list.scrollTo(index)
    qtbot.waitUntil(
        lambda: model.data(index, Qt.ItemDataRole.DecorationRole) is not None, timeout=10000
    )
    # filter to photos and sort by name
    window.browser.filter_box.setCurrentIndex(1)
    wait_gallery(qtbot, window)
    assert all(model.row_at(i)["kind"] in ("photo", "raw") for i in range(model.rowCount()))
    window.browser.sort_box.setCurrentIndex(1)
    wait_gallery(qtbot, window)
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
    wait_gallery(qtbot, window)
    model = window.browser.model
    first_id, second_id = model.ids[0], model.ids[1]
    window.browser._open_at(0)
    assert window.pages.currentWidget() is window.review
    review = window.review
    assert review.current_id == first_id
    qtbot.waitUntil(lambda: review.current_row is not None)
    qtbot.keyClick(review, Qt.Key.Key_Right)  # keep
    qtbot.waitUntil(lambda: review.current_id == second_id)
    assert window.gws.file_row(first_id)["reviewed_at"]
    assert review.current_id == second_id
    second_row = window.gws.file_row(second_id)
    qtbot.keyClick(review, Qt.Key.Key_Left)  # to delete
    qtbot.waitUntil(lambda: second_id not in review.ids)
    moved = window.gws.file_row(second_id)
    assert (
        moved["root"] == "delete" and (root / "_framesift_delete" / second_row["rel_path"]).exists()
    )
    assert second_id not in review.ids
    qtbot.keyClick(review, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)  # undo the delete
    qtbot.waitUntil(lambda: review.current_id == second_id)
    assert window.gws.file_row(second_id)["root"] == "source"
    assert review.current_id == second_id
    qtbot.keyClick(review, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)  # undo the keep
    qtbot.waitUntil(lambda: review.current_id == first_id)
    assert window.gws.file_row(first_id)["reviewed_at"] is None
    assert review.current_id == first_id
    qtbot.keyClick(review, Qt.Key.Key_Down)  # skip
    assert review.current_id == second_id
    qtbot.keyClick(review, Qt.Key.Key_I)
    qtbot.waitUntil(review.info.isVisible)
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
    assert window.open_source(str(lib))
    qtbot.waitUntil(lambda: bool(shown))
    assert shown and "Apple Photos" in shown[0]


def test_open_keeps_gui_responsive_while_catalog_is_unavailable(
    qtbot, window: MainWindow, tmp_path: Path, monkeypatch
) -> None:
    from framesift.engine import api

    root = tmp_path / "Archive"
    root.mkdir()
    original = api.open_workspace
    entered = Event()
    release = Event()
    gui_thread = get_ident()

    def slow_open(*args, **kwargs):
        assert get_ident() != gui_thread
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(api, "open_workspace", slow_open)
    try:
        assert window.open_source(str(root))
        qtbot.waitUntil(entered.is_set)
        window.nav_actions["settings"].trigger()
        assert window.pages.currentWidget() is window.settings
        assert window.gws.opening and not window.gws.is_open
    finally:
        release.set()
    qtbot.waitUntil(lambda: window.gws.is_open)


def test_rapid_filter_and_filename_search(qtbot, window: MainWindow, tmp_path: Path) -> None:
    from PIL import Image

    root = tmp_path / "Archive"
    root.mkdir()
    for name in ("Кадр-1.jpg", "КАДР-2.jpg", "vacation.jpg"):
        Image.new("RGB", (64, 64), "blue").save(root / name)
    open_and_scan(qtbot, window, root)
    browser = window.browser
    browser.search.setText("vacation")
    browser.search.setText("кадр")
    browser.sort_box.setCurrentIndex(1)
    qtbot.waitUntil(lambda: not browser.model.loading and browser.model.rowCount() == 2)
    assert {browser.model.index(i).data() for i in range(browser.model.rowCount())} == {
        "Кадр-1.jpg",
        "КАДР-2.jpg",
    }


def test_reopen_catalog_preserves_archive_and_replaces_thumbnail(
    qtbot, window: MainWindow, tmp_path: Path
) -> None:
    from PIL import Image

    from framesift.engine import api

    def cached_color() -> str:
        pixmap = window.browser.model.cached_thumbnail(0)
        return pixmap.toImage().pixelColor(32, 32).name() if pixmap else ""

    for folder, color in (("First", "#ff0000"), ("Second", "#0000ff")):
        root = tmp_path / folder
        root.mkdir()
        Image.new("RGB", (64, 64), color).save(root / "same-name.png")
        with api.open_workspace(root) as ws:
            api.run_scan(ws, threads=1)
        with qtbot.waitSignal(window.gws.opened, timeout=5000):
            assert window.open_saved(str(root))
        qtbot.waitUntil(lambda: bool(cached_color()), timeout=5000)
        assert window.browser.model.ids == [1]  # file IDs are only unique inside a catalog
        assert not window.gws.busy
        expected_color = "#fe0000" if folder == "First" else "#0000fe"
        actual = cached_color()
        channel = 1 if folder == "First" else 5
        assert (
            abs(
                int(actual[channel : channel + 2], 16)
                - int(expected_color[channel : channel + 2], 16)
            )
            <= 1
        )


def test_move_selection_in_background_and_undo(qtbot, window: MainWindow, tmp_path: Path) -> None:
    from PIL import Image

    from framesift.engine import api

    root = tmp_path / "Archive"
    root.mkdir()
    for n in range(4):
        Image.new("RGB", (64, 64), "red").save(root / f"image-{n}.png")
    open_and_scan(qtbot, window, root)
    window.browser.list.selectAll()
    assert len(window.browser.selected_ids()) == 4
    with qtbot.waitSignal(window.gws.job_finished):
        window.browser.delete_btn.click()
    assert len(list((root / "_framesift_delete").glob("*.png"))) == 4
    qtbot.waitUntil(lambda: window.browser.model.rowCount() == 0, timeout=5000)
    assert len(window.gws.catalog.undoable_ops(session_id=window.gws.ws.session_id)) == 4
    with qtbot.waitSignal(window.gws.job_finished):
        window.gws.run_job(
            "undo",
            lambda progress, control: api.run_undo(
                window.gws.ws, "all", dry_run=False, progress=progress, control=control
            ),
        )
    assert len(list(root.glob("*.png"))) == 4
