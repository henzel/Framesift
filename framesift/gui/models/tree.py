"""Folder tree: Source subfolders, Review categories with counts, Delete with counts."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QStandardItem, QStandardItemModel

from framesift.engine.i18n import t
from framesift.gui.i18n import tr
from framesift.gui.workspace import GuiWorkspace

NodeRole = Qt.ItemDataRole.UserRole + 1  # dict(root=..., dir_path=..., category=...)


def human_bytes(n: int | float | None) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


class FolderTreeModel(QStandardItemModel):
    def __init__(self, gws: GuiWorkspace, parent=None):
        super().__init__(parent)
        self.gws = gws
        self.source_item: QStandardItem | None = None
        self.review_item: QStandardItem | None = None
        self.delete_item: QStandardItem | None = None

    def rebuild(self) -> None:
        self.clear()
        if not self.gws.is_open:
            return
        catalog = self.gws.catalog
        stats = catalog.root_stats()
        src = stats.get("source", {"files": 0, "bytes": 0})
        self.source_item = self._node(
            f"{tr('Source')}  ({src['files']:,}, {human_bytes(src['bytes'])})",
            {"root": "source", "dir_path": None},
        )
        self.appendRow(self.source_item)
        dirs = [
            r[0]
            for r in catalog.query(
                "SELECT DISTINCT dir_path FROM files WHERE root='source' AND status='present' AND dir_path<>'' ORDER BY dir_path"
            )
        ]
        nodes: dict[str, QStandardItem] = {"": self.source_item}
        for d in dirs:
            parts = d.split("/")
            path = ""
            for part in parts:
                child_path = f"{path}/{part}" if path else part
                if child_path not in nodes:
                    item = self._node(part, {"root": "source", "dir_path": child_path})
                    nodes[path].appendRow(item)
                    nodes[child_path] = item
                path = child_path
        rev = stats.get("review", {"files": 0, "bytes": 0})
        self.review_item = self._node(
            f"{tr('Review')}  ({rev['files']:,}, {human_bytes(rev['bytes'])})",
            {"root": "review", "dir_path": None},
        )
        self.appendRow(self.review_item)
        for cat in catalog.review_category_stats():
            name = cat["category"]
            title = t(f"category.{name}") if name else "(root)"
            self.review_item.appendRow(
                self._node(
                    f"{title}  ({cat['files']:,}, {human_bytes(cat['bytes'])})",
                    {"root": "review", "category": name},
                )
            )
        dele = stats.get("delete", {"files": 0, "bytes": 0})
        self.delete_item = self._node(
            f"{tr('To delete')}  ({dele['files']:,}, {human_bytes(dele['bytes'])})",
            {"root": "delete", "dir_path": None},
        )
        self.appendRow(self.delete_item)

    @staticmethod
    def _node(text: str, payload: dict[str, Any]) -> QStandardItem:
        item = QStandardItem(text)
        item.setEditable(False)
        item.setData(payload, NodeRole)
        return item
