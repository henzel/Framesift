"""Folder tree: Source subfolders, Review categories with counts, Delete with counts."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QAbstractItemModel, QModelIndex, QObject, QPersistentModelIndex, Qt

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


class TreeNode:
    """One row of the folder tree.

    Plain Python on purpose: the model creates no Qt item objects, so PySide keeps no wrapper
    per row whose lifetime Qt could end behind its back (ARCHITECTURE.md decision 26)."""

    __slots__ = ("children", "node_id", "parent", "payload", "row", "text")

    def __init__(
        self, node_id: int, text: str, payload: dict[str, Any], parent: TreeNode | None, row: int
    ):
        self.node_id = node_id
        self.text = text
        self.payload = payload
        self.parent = parent
        self.row = row
        self.children: list[TreeNode] = []


class FolderTreeModel(QAbstractItemModel):
    """Source folders, Review categories and Delete, with counts. Indexes carry node ids."""

    def __init__(self, gws: GuiWorkspace, parent: QObject | None = None):
        super().__init__(parent)
        self.gws = gws
        self._top: list[TreeNode] = []
        self._nodes: list[TreeNode] = []  # indexed by node_id
        self.source_item: TreeNode | None = None
        self.review_item: TreeNode | None = None
        self.delete_item: TreeNode | None = None

    def rebuild(self) -> None:
        self.beginResetModel()
        self._top = []
        self._nodes = []
        self.source_item = self.review_item = self.delete_item = None
        try:
            if self.gws.is_open:
                self._build()
        finally:
            self.endResetModel()

    def _build(self) -> None:
        catalog = self.gws.catalog
        stats = catalog.root_stats()
        src = stats.get("source", {"files": 0, "bytes": 0})
        self.source_item = self._add(
            None,
            f"{tr('Source')}  ({src['files']:,}, {human_bytes(src['bytes'])})",
            {"root": "source", "dir_path": None},
        )
        dirs = [
            r[0]
            for r in catalog.query(
                "SELECT DISTINCT dir_path FROM files WHERE root='source' AND status='present' AND dir_path<>'' ORDER BY dir_path"
            )
        ]
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
        rev = stats.get("review", {"files": 0, "bytes": 0})
        self.review_item = self._add(
            None,
            f"{tr('Review')}  ({rev['files']:,}, {human_bytes(rev['bytes'])})",
            {"root": "review", "dir_path": None},
        )
        for cat in catalog.review_category_stats():
            name = cat["category"]
            title = t(f"category.{name}") if name else "(root)"
            self._add(
                self.review_item,
                f"{title}  ({cat['files']:,}, {human_bytes(cat['bytes'])})",
                {"root": "review", "category": name},
            )
        dele = stats.get("delete", {"files": 0, "bytes": 0})
        self.delete_item = self._add(
            None,
            f"{tr('To delete')}  ({dele['files']:,}, {human_bytes(dele['bytes'])})",
            {"root": "delete", "dir_path": None},
        )

    def _add(self, parent: TreeNode | None, text: str, payload: dict[str, Any]) -> TreeNode:
        siblings = self._top if parent is None else parent.children
        node = TreeNode(len(self._nodes), text, payload, parent, len(siblings))
        siblings.append(node)
        self._nodes.append(node)
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
        for node in self._nodes:
            if node.payload == payload:
                return self.index_of(node)
        return QModelIndex()

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
        if role == NodeRole:
            return node.payload
        return None

    def flags(self, index: QModelIndex | QPersistentModelIndex) -> Qt.ItemFlag:
        if self.node(index) is None:
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
