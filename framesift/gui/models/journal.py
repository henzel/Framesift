"""Table model over the journal."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt

from framesift.engine.i18n import t
from framesift.gui.i18n import tr
from framesift.gui.workspace import GuiWorkspace

COLUMNS = ("Time", "Action", "Category", "From", "To", "Session")


class JournalModel(QAbstractTableModel):
    def __init__(self, gws: GuiWorkspace, parent=None):
        super().__init__(parent)
        self.gws = gws
        self.rows: list[dict[str, Any]] = []

    def reload(
        self,
        *,
        text: str | None = None,
        category: str | None = None,
        action: str | None = None,
        session_id: int | None = None,
    ) -> None:
        self.beginResetModel()
        if self.gws.is_open:
            self.rows = [
                dict(r)
                for r in self.gws.catalog.journal(
                    text=text or None,
                    category=category,
                    action=action,
                    session_id=session_id,
                    limit=5000,
                )
            ]
        else:
            self.rows = []
        self.endResetModel()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008
        return len(COLUMNS)

    def headerData(
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> Any:
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return tr(COLUMNS[section])
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        row = self.rows[index.row()]
        if role == Qt.ItemDataRole.UserRole:
            return row
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        col = index.column()
        if col == 0:
            return row["ts"].replace("T", " ").rstrip("Z")
        if col == 1:
            label = t(f"action.{row['action']}")
            return f"{label} ✗" if row["undone_by"] else label
        if col == 2:
            return t(f"category.{row['category']}") if row["category"] else ""
        if col == 3:
            return f"{row['from_root']}: {row['from_rel']}" if row["from_rel"] else ""
        if col == 4:
            return f"{row['to_root']}: {row['to_rel']}" if row["to_rel"] else ""
        if col == 5:
            return str(row["session_id"])
        return None
