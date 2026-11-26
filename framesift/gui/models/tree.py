"""Folder tree: Source subfolders, Review categories with counts, Delete with counts."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QAbstractItemModel, QModelIndex, QObject, QPersistentModelIndex, Qt
from PySide6.QtWidgets import QApplication

from framesift.engine.catalog import Catalog
from framesift.engine.i18n import t
from framesift.gui.i18n import tr
from framesift.gui.ui import icon
from framesift.gui.workspace import GuiWorkspace

NodeRole = Qt.ItemDataRole.UserRole + 1  # dict(root=..., dir_path=..., category=...)

FolderSnapshot = tuple[dict[str, dict[str, int]], list[str], list[dict[str, Any]]]


def folder_snapshot(catalog: Catalog) -> FolderSnapshot:
    return (
        catalog.root_stats(),
        [
            row[0]
            for row in catalog.query(
                "SELECT DISTINCT dir_path FROM files WHERE root='source' AND status='present' AND dir_path<>'' ORDER BY dir_path"
            )
        ],
        catalog.review_category_stats(),
    )


def human_bytes(n: int | float | None) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


class TreeNode:
    """One row of the folder tree.

    Plain Python on purpose: the model creates no Qt item objects, so PySide keeps no wrapper
    per row whose lifetime Qt could end behind its back (ARCHITECTURE.md decision 26)."""

    __slots__ = ("children", "node_id", "parent", "payload", "row", "text", "details")

    def __init__(
        self, node_id: int, text: str, payload: dict[str, Any], parent: TreeNode | None, row: int
    ):
        self.node_id = node_id
        self.text = text
        self.payload = payload
        self.parent = parent
        self.row = row
        self.details = ""
        self.children: list[TreeNode] = []


class FolderTreeModel(QAbstractItemModel):
    """Source folders, Review categories and Delete, with counts. Indexes carry node ids."""

    def __init__(self, gws: GuiWorkspace, parent: QObject | None = None):
        super().__init__(parent)
        self.gws = gws
        self._top: list[TreeNode] = []
        self._nodes: list[TreeNode] = []  # indexed by node_id
        self._paths: dict[tuple, TreeNode] = {}
        self._icons: dict[str, Any] = {}
        self.source_item: TreeNode | None = None
        self.review_item: TreeNode | None = None
        self.delete_item: TreeNode | None = None

    def rebuild(self, snapshot: FolderSnapshot | None = None) -> None:
        if snapshot is None and self.gws.is_open:
            snapshot = folder_snapshot(self.gws.catalog)
        if snapshot is not None and snapshot == getattr(self, "_snapshot", None):
            return
        self._snapshot = snapshot
        self.beginResetModel()
        self._top = []
        self._nodes = []
        self._paths = {}
        self.source_item = self.review_item = self.delete_item = None
        try:
            if snapshot is not None:
                self._build(snapshot)
        finally:
            self.endResetModel()

    def _build(self, snapshot: FolderSnapshot) -> None:
        stats, dirs, categories = snapshot
        self.source_item = self._add(
            None,
            tr("Source"),
            {"root": "source", "dir_path": None},
        )
        self.source_item.details = self._details(stats.get("source", {}))
        nodes: dict[str, TreeNode] = {"": self.source_item}
        for d in dirs:
            parts = d.split("/")
            path = ""
            for part in parts:
                child_path = f"{path}/{part}" if path else part
                if child_path not in nodes:
                    nodes[child_path] = self._add(
                        nodes[path], part, {"root": "source", "dir_path": child_path}
                    )
                path = child_path
        self.review_item = self._add(
            None,
            tr("Candidates"),
            {"root": "review", "dir_path": None},
        )
        self.review_item.details = self._details(stats.get("review", {}))
        for cat in categories:
            name = cat["category"]
            title = t(f"category.{name}") if name else tr("Root folder")
            node = self._add(
                self.review_item,
                title,
                {"root": "review", "category": name},
            )
            node.details = self._details(cat)
        self.delete_item = self._add(
            None,
            tr("To delete"),
            {"root": "delete", "dir_path": None},
        )
        self.delete_item.details = self._details(stats.get("delete", {}))

    @staticmethod
    def _details(stats: dict[str, Any]) -> str:
        return f"{stats.get('files', 0):,} · {human_bytes(stats.get('bytes'))}"

    @staticmethod
    def _path_key(payload: dict[str, Any]) -> tuple:
        return payload["root"], payload.get("dir_path"), payload.get("category")

    def _add(self, parent: TreeNode | None, text: str, payload: dict[str, Any]) -> TreeNode:
        siblings = self._top if parent is None else parent.children
        node = TreeNode(len(self._nodes), text, payload, parent, len(siblings))
        siblings.append(node)
        self._nodes.append(node)
        self._paths[self._path_key(payload)] = node
        return node

    # ------------------------------------------------------------------ lookups
    def node(self, index: QModelIndex | QPersistentModelIndex) -> TreeNode | None:
        if not index.isValid() or index.model() is not self:
            return None
        node_id = index.internalId()
        return self._nodes[node_id] if 0 <= node_id < len(self._nodes) else None

    def index_of(self, node: TreeNode | None) -> QModelIndex:
        if node is None:
            return QModelIndex()
        return self.createIndex(node.row, 0, node.node_id)

    def find(self, payload: dict[str, Any]) -> QModelIndex:
        """Index of the node carrying this payload, or an invalid index."""
        return self.index_of(self._paths.get(self._path_key(payload)))

    # ------------------------------------------------------------------ QAbstractItemModel
    def index(
        self,
        row: int,
        column: int,
        parent: QModelIndex | QPersistentModelIndex = QModelIndex(),  # noqa: B008
    ) -> QModelIndex:
        if column != 0 or row < 0:
            return QModelIndex()
        if parent.isValid():
            node = self.node(parent)
            siblings = node.children if node is not None else []
        else:
            siblings = self._top
        if row >= len(siblings):
            return QModelIndex()
        return self.createIndex(row, 0, siblings[row].node_id)

    def parent(self, index: QModelIndex | QPersistentModelIndex | None = None):  # type: ignore[override]
        if index is None:  # QObject.parent()
            return super().parent()
        node = self.node(index)
        if node is None or node.parent is None:
            return QModelIndex()
        return self.index_of(node.parent)

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: B008
        if not parent.isValid():
            return len(self._top)
        node = self.node(parent)
        return len(node.children) if node is not None and parent.column() == 0 else 0

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 1

    def data(
        self, index: QModelIndex | QPersistentModelIndex, role: int = Qt.ItemDataRole.DisplayRole
    ) -> Any:
        node = self.node(index)
        if node is None:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return node.text
        if role == Qt.ItemDataRole.ToolTipRole:
            return node.text + (" · " + node.details if node.details else "")
        if role == Qt.ItemDataRole.DecorationRole:
            name = "trash" if node.payload["root"] == "delete" else "folder"
            color = QApplication.palette().text().color()
            key = name + color.name()
            if key not in self._icons:
                self._icons[key] = icon(name, color, 15)
            return self._icons[key]
        if role == NodeRole:
            return node.payload
        return None

    def flags(self, index: QModelIndex | QPersistentModelIndex) -> Qt.ItemFlag:
        if self.node(index) is None:
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
