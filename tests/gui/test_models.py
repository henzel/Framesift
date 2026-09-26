"""Folder tree model and category cards: Qt model contract and wrapper hygiene (offscreen)."""

from __future__ import annotations

from typing import Any

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLayout, QLayoutItem
from shiboken6 import Shiboken

from framesift.engine.plan import CategoryReport, Example
from framesift.gui.models.tree import FolderTreeModel, NodeRole
from framesift.gui.views.auto import CategoryCard

pytestmark = pytest.mark.gui


class FakeCatalog:
    def __init__(self, dirs: list[str], categories: list[str]):
        self.dirs = dirs
        self.categories = categories

    def root_stats(self) -> dict[str, dict[str, int]]:
        return {"source": {"files": 10, "bytes": 2048}, "review": {"files": 3, "bytes": 300}}

    def query(self, sql: str, params: tuple = ()) -> list[tuple[str]]:
        return [(d,) for d in self.dirs]

    def review_category_stats(self) -> list[dict[str, Any]]:
        return [{"category": c, "files": 1, "bytes": 1} for c in self.categories]


class FakeWorkspace:
    def __init__(self, catalog: FakeCatalog):
        self.catalog = catalog
        self.is_open = True


def test_folder_tree_model(qtmodeltester) -> None:
    gws = FakeWorkspace(FakeCatalog(["a", "a/b", "a/b/c", "d"], ["junk", "screenshots"]))
    model = FolderTreeModel(gws)  # type: ignore[arg-type]
    model.rebuild()
    qtmodeltester.check(model)
    assert model.rowCount() == 3
    source = model.index_of(model.source_item)
    assert source == model.index(0, 0) and model.rowCount(source) == 2  # a, d
    a = model.index(0, 0, source)
    assert a.data() == "a" and model.parent(a) == source
    c = model.find({"root": "source", "dir_path": "a/b/c"})
    assert c.data() == "c" and c.parent().parent() == a
    junk = model.find({"root": "review", "category": "junk"})
    assert junk.parent() == model.index_of(model.review_item)
    assert junk.data(NodeRole) == {"root": "review", "category": "junk"}
    assert not model.flags(junk) & Qt.ItemFlag.ItemIsEditable
    # a rebuild replaces every node
    gws.catalog.dirs = ["x"]
    model.rebuild()
    qtmodeltester.check(model)
    assert model.rowCount(model.index_of(model.source_item)) == 1
    assert not model.find({"root": "source", "dir_path": "a/b/c"}).isValid()
    gws.is_open = False
    model.rebuild()
    assert model.rowCount() == 0 and model.source_item is None


def layout_item_wrappers() -> int:
    """Python wrappers of plain layout items (QWidgetItem, QSpacerItem) alive right now."""
    return sum(
        1
        for w in Shiboken.getAllValidWrappers()
        if isinstance(w, QLayoutItem) and not isinstance(w, QLayout)
    )


def report(ids: list[int]) -> CategoryReport:
    rep = CategoryReport("junk", "Junk", "high", True, count=len(ids), bytes=1024)
    rep.examples = [Example(i, i, f"junk/{i}.jpg", 10, "empty file") for i in ids]
    return rep


def test_category_card_replaces_example_labels(qtbot) -> None:
    card = CategoryCard("junk")
    qtbot.addWidget(card)
    card.set_report(report([1, 2, 3]))
    before = layout_item_wrappers()
    for ids in ([4, 5], [6, 7, 8], [9]):
        card.set_report(report(ids))
    assert card.grid.count() == 1 and list(card.thumbs) == [9]
    # Qt deletes the layout item of a removed label on its own; a Python wrapper of it would
    # stay behind in shiboken's wrapper map and corrupt the heap later (ARCHITECTURE.md 26).
    assert layout_item_wrappers() == before
