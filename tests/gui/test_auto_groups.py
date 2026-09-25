"""GUI tests for the automatic mode page and the group mode (offscreen)."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

from framesift.gui.main_window import MainWindow
from framesift.gui.prefs import Prefs

pytestmark = pytest.mark.gui


@pytest.fixture
def window(qtbot, tmp_path: Path):
    prefs = Prefs(tmp_path / "prefs.json")
    prefs.set("workers", 1)
    win = MainWindow(prefs, cache_dir=tmp_path / "thumbs")
    qtbot.addWidget(win)
    win.show()
    yield win
    win.gws.shutdown()


def open_and_scan(qtbot, window: MainWindow, root: Path) -> None:
    with qtbot.waitSignal(window.gws.job_finished, timeout=60000):
        assert window.open_source(str(root))
    qtbot.waitUntil(lambda: window.browser.model.rowCount() > 0, timeout=10000)


def classify(qtbot, window: MainWindow) -> None:
    window.show_page("auto")
    with qtbot.waitSignal(window.gws.job_finished, timeout=120000):
        window.auto.run_btn.click()
    qtbot.waitUntil(
        lambda: window.auto.plan is not None and window.auto.plan.run_id is not None, timeout=10000
    )


def test_auto_mode_report_thresholds_and_apply(
    qtbot, window: MainWindow, fresh, monkeypatch
) -> None:
    root, manifest = fresh
    open_and_scan(qtbot, window, root)
    classify(qtbot, window)
    auto = window.auto
    assert auto.cards["screenshots"].count_label.text() == "3"
    assert auto.cards["junk"].count_label.text() == "6"
    assert len(auto.cards["screenshots"].thumbs) == 3
    if manifest["short_videos"]:
        assert auto.cards["short_videos"].count_label.text() != "0"
        auto.short_video.setValue(1.0)  # recompute from the catalog, no rescanning
        assert auto.cards["short_videos"].count_label.text() == "0"
        auto.short_video.setValue(3.0)
    assert "Live Photo" in auto.report_only.text()
    # disable everything but screenshots and junk, then apply
    for name, card in auto.cards.items():
        card.enabled_box.setChecked(name in ("screenshots", "junk"))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    with qtbot.waitSignal(window.gws.job_finished, timeout=60000):
        auto.apply_btn.click()
    review = root / "_framesift_review"
    assert (review / "screenshots" / "2020" / "IMG_0005.PNG").exists()
    assert (review / "junk" / "junk" / "empty.jpg").exists()
    assert not (review / "duplicates").exists()
    assert auto.cards["screenshots"].count_label.text() == "0"
    assert "Moved" in auto.progress_label.text() or "Перенесено" in auto.progress_label.text()
    if manifest["live_pairs"]:
        with qtbot.waitSignal(window.gws.job_finished, timeout=60000):
            auto.live_btn.click()
        assert (review / "live_photos" / "2020" / "IMG_0001.MOV").exists()
    # the browser tree shows the new review categories
    window.show_page("browser")
    qtbot.waitUntil(
        lambda: (
            window.browser.tree_model.review_item is not None
            and window.browser.tree_model.review_item.rowCount() >= 2
        ),
        timeout=5000,
    )


def test_group_mode_keys(qtbot, window: MainWindow, fresh, monkeypatch) -> None:
    root, _ = fresh
    open_and_scan(qtbot, window, root)
    classify(qtbot, window)
    auto = window.auto
    for name, card in auto.cards.items():
        card.enabled_box.setChecked(name in ("similar", "duplicates"))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    with qtbot.waitSignal(window.gws.job_finished, timeout=60000):
        auto.apply_btn.click()
    window.show_page("groups")
    groups = window.groups
    assert len(groups.groups) == 2  # one duplicate group, one similar group
    assert groups.groups[0]["category"] == "duplicates"
    members = groups.members
    keeper = next(m for m in members if m["is_keeper"])
    assert int(keeper["id"]) in groups.marked and keeper["root"] == "source"
    other = next(m for m in members if not m["is_keeper"])
    assert other["root"] == "review"
    # mark the review copy too, unmark the keeper: the copy comes home, the keeper goes to Delete
    qtbot.keyClick(groups, Qt.Key.Key_Right)
    assert groups.selected == 1
    qtbot.keyClick(groups, Qt.Key.Key_K)
    qtbot.keyClick(groups, Qt.Key.Key_Left)
    qtbot.keyClick(groups, Qt.Key.Key_K)
    assert int(other["id"]) in groups.marked and int(keeper["id"]) not in groups.marked
    qtbot.keyClick(groups, Qt.Key.Key_Return)
    assert window.gws.wait_actions()
    assert window.gws.file_row(int(other["id"]))["root"] == "source"
    assert window.gws.file_row(int(keeper["id"]))["root"] == "delete"
    assert groups.gpos == 1 and groups.groups[1]["category"] == "similar"
    # undo the group decision
    qtbot.keyClick(groups, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert window.gws.file_row(int(other["id"]))["root"] == "review"
    assert window.gws.file_row(int(keeper["id"]))["root"] == "source"
    assert groups.gpos == 0
    qtbot.keyClick(groups, Qt.Key.Key_Down)  # skip
    assert groups.gpos == 1 and len(groups.members) == 3
    qtbot.keyClick(groups, Qt.Key.Key_Escape)
    assert window.pages.currentWidget() is window.auto
