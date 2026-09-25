"""Folder browser: tree on the left, virtualized thumbnail grid in the centre."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QModelIndex, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFontMetrics, QKeyEvent, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QListView,
    QPushButton,
    QSplitter,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from framesift.gui.i18n import tr
from framesift.gui.models.catalog_list import CatalogListModel, FileIdRole, ListQuery, RowRole
from framesift.gui.models.tree import FolderTreeModel, NodeRole
from framesift.gui.workspace import GuiWorkspace


class ThumbDelegate(QStyledItemDelegate):
    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        model = index.model()
        row: dict[str, Any] | None = index.data(RowRole)
        painter.save()
        rect = option.rect
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(rect.adjusted(2, 2, -2, -2), option.palette.highlight())
        icon_size: QSize = model.icon_size  # type: ignore[attr-defined]
        box = QRect(rect.x() + 8, rect.y() + 6, icon_size.width(), icon_size.height())
        pix = index.data(Qt.ItemDataRole.DecorationRole)
        painter.fillRect(box, QColor(0, 0, 0, 30))
        if pix is not None and not pix.isNull():
            scaled = pix.scaled(
                box.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            x = box.x() + (box.width() - scaled.width()) // 2
            y = box.y() + (box.height() - scaled.height()) // 2
            painter.drawPixmap(x, y, scaled)
        text_rect = QRect(rect.x() + 4, box.bottom() + 4, rect.width() - 8, 22)
        fm = QFontMetrics(option.font)
        name = index.data(Qt.ItemDataRole.DisplayRole) or ""
        color = (
            option.palette.highlightedText().color()
            if option.state & QStyle.StateFlag.State_Selected
            else option.palette.text().color()
        )
        painter.setPen(QPen(color))
        painter.drawText(
            text_rect,
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
            fm.elidedText(name, Qt.TextElideMode.ElideMiddle, text_rect.width()),
        )
        if row:
            badges = []
            if row["kind"] == "video":
                badges.append("▶")
            if row.get("live_count"):
                badges.append("LIVE")
            if row.get("edited_of"):
                badges.append("✎")
            if row.get("reviewed_at"):
                badges.append("✓")
            if badges:
                label = " ".join(badges)
                brect = QRect(
                    box.x() + 4, box.y() + 4, fm.horizontalAdvance(label) + 10, fm.height() + 4
                )
                painter.fillRect(brect, QColor(0, 0, 0, 150))
                painter.setPen(QPen(QColor(255, 255, 255)))
                painter.drawText(brect, Qt.AlignmentFlag.AlignCenter, label)
        painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        return index.data(Qt.ItemDataRole.SizeHintRole) or QSize(176, 194)


class GridView(QListView):
    activated_item = Signal(int)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.currentIndex().isValid():
            self.activated_item.emit(self.currentIndex().row())
            return
        super().keyPressEvent(event)


class BrowserView(QWidget):
    open_review = Signal(list, int, object)  # ids, position, ListQuery
    status = Signal(str)

    def __init__(self, gws: GuiWorkspace, parent=None):
        super().__init__(parent)
        self.gws = gws
        self.model = CatalogListModel(gws, self)
        self.tree_model = FolderTreeModel(gws, self)
        self.current_node: dict[str, Any] = {"root": "source", "dir_path": None}
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(400)
        self._refresh_timer.timeout.connect(self._refresh)

        self.tree = QTreeView(self)
        self.tree.setModel(self.tree_model)
        self.tree.setHeaderHidden(True)
        self.tree.setMinimumWidth(180)
        self.tree.selectionModel().currentChanged.connect(self._on_tree_current)

        self.sort_box = QComboBox(self)
        for key, label in (
            ("date", "Sort by date"),
            ("name", "Sort by name"),
            ("size", "Sort by size"),
        ):
            self.sort_box.addItem(tr(label), key)
        self.filter_box = QComboBox(self)
        for key, label in (("all", "All"), ("photo", "Photos"), ("video", "Videos")):
            self.filter_box.addItem(tr(label), key)
        self.show_reviewed = QCheckBox(tr("Show reviewed"), self)
        self.show_reviewed.setChecked(True)
        self.rescan_btn = QPushButton(tr("Rescan folder"), self)
        self.keep_btn = QPushButton(tr("Keep"), self)
        self.delete_btn = QPushButton(tr("Delete"), self)
        self.restore_btn = QPushButton(tr("Restore"), self)
        self.open_btn = QPushButton(tr("Open in review mode"), self)
        self.count_label = QLabel(self)

        self.list = GridView(self)
        self.list.setModel(self.model)
        self.list.setItemDelegate(ThumbDelegate(self.list))
        self.list.setViewMode(QListView.ViewMode.IconMode)
        self.list.setResizeMode(QListView.ResizeMode.Adjust)
        self.list.setMovement(QListView.Movement.Static)
        self.list.setUniformItemSizes(True)
        self.list.setLayoutMode(QListView.LayoutMode.Batched)
        self.list.setBatchSize(300)
        self.list.setSpacing(4)
        self.list.setSelectionMode(QListView.SelectionMode.ExtendedSelection)
        self.list.setSelectionRectVisible(True)
        self.list.setWordWrap(False)
        self.list.doubleClicked.connect(lambda idx: self._open_at(idx.row()))
        self.list.activated_item.connect(self._open_at)
        self.list.selectionModel().selectionChanged.connect(self._update_buttons)

        top = QHBoxLayout()
        top.addWidget(self.sort_box)
        top.addWidget(self.filter_box)
        top.addWidget(self.show_reviewed)
        top.addStretch(1)
        top.addWidget(self.keep_btn)
        top.addWidget(self.delete_btn)
        top.addWidget(self.restore_btn)
        top.addWidget(self.open_btn)
        top.addWidget(self.rescan_btn)
        right = QWidget(self)
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addLayout(top)
        rl.addWidget(self.list, 1)
        rl.addWidget(self.count_label)
        splitter = QSplitter(self)
        splitter.addWidget(self.tree)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(splitter)

        self.sort_box.currentIndexChanged.connect(lambda _i: self.reload())
        self.filter_box.currentIndexChanged.connect(lambda _i: self.reload())
        self.show_reviewed.toggled.connect(lambda _c: self.reload())
        self.rescan_btn.clicked.connect(lambda: self.gws.scan())
        self.keep_btn.clicked.connect(lambda: self._act("keep"))
        self.delete_btn.clicked.connect(lambda: self._act("to_delete"))
        self.restore_btn.clicked.connect(lambda: self._act("restore"))
        self.open_btn.clicked.connect(
            lambda: self._open_at(
                self.list.currentIndex().row() if self.list.currentIndex().isValid() else 0
            )
        )
        gws.opened.connect(self.rebuild)
        gws.closed.connect(self.rebuild)
        gws.changed.connect(self.schedule_refresh)
        gws.scan_batch.connect(self.schedule_refresh)
        self._update_buttons()

    # ------------------------------------------------------------------ queries
    def query(self) -> ListQuery:
        node = self.current_node
        return ListQuery(
            root=node.get("root", "source"),
            dir_path=node.get("dir_path"),
            recursive=True,
            sort=self.sort_box.currentData() or "date",
            kind=self.filter_box.currentData() or "all",
            show_reviewed=self.show_reviewed.isChecked(),
            category=node.get("category"),
        )

    def rebuild(self) -> None:
        self.tree_model.rebuild()
        self.tree.expandToDepth(0)
        if self.tree_model.source_item is not None:
            self.tree.setCurrentIndex(self.tree_model.source_item.index())
        self.reload()

    def reload(self) -> None:
        self.model.load(self.query())
        self._update_count()

    def schedule_refresh(self) -> None:
        self._refresh_timer.start()

    def _refresh(self) -> None:
        if not self.gws.is_open:
            return
        current = self.tree.currentIndex()
        node = current.data(NodeRole) if current.isValid() else None
        self.tree_model.rebuild()
        self.tree.expandToDepth(0)
        self._select_node(node or {"root": "source", "dir_path": None})
        self.model.refresh_keep_position()
        self._update_count()

    def _select_node(self, node: dict[str, Any]) -> None:
        for item in (
            self.tree_model.source_item,
            self.tree_model.review_item,
            self.tree_model.delete_item,
        ):
            if item is None:
                continue
            found = self._find(item, node)
            if found is not None:
                self.tree.blockSignals(True)
                self.tree.setCurrentIndex(found.index())
                self.tree.blockSignals(False)
                self.current_node = node
                return

    def _find(self, item, node: dict[str, Any]):
        if item.data(NodeRole) == node:
            return item
        for i in range(item.rowCount()):
            found = self._find(item.child(i), node)
            if found is not None:
                return found
        return None

    def _on_tree_current(self, current: QModelIndex, _previous: QModelIndex) -> None:
        node = current.data(NodeRole)
        if node:
            self.current_node = node
            self.reload()

    def _update_count(self) -> None:
        n = self.model.rowCount()
        sel = len(self.list.selectionModel().selectedRows()) if self.list.selectionModel() else 0
        text = tr("%n item(s)").replace("%n", f"{n:,}")
        if sel:
            text += "   ·   " + tr("{n} items selected", n=sel)
        self.count_label.setText(text)
        self.status.emit(text)

    def _update_buttons(self, *_args: Any) -> None:
        has = (
            bool(self.list.selectionModel() and self.list.selectionModel().selectedRows())
            and self.gws.is_open
            and not self.gws.read_only
        )
        root = self.current_node.get("root", "source")
        self.keep_btn.setEnabled(has)
        self.delete_btn.setEnabled(has and root != "delete")
        self.restore_btn.setEnabled(has and root != "source")
        self.open_btn.setEnabled(self.model.rowCount() > 0)
        self._update_count()

    # ------------------------------------------------------------------ actions
    def selected_ids(self) -> list[int]:
        return [idx.data(FileIdRole) for idx in self.list.selectionModel().selectedRows()]

    def _act(self, action: str) -> None:
        ids = self.selected_ids()
        for fid in ids:
            if action == "keep":
                self.gws.keep(fid)
            elif action == "to_delete":
                self.gws.to_delete(fid)
            elif action == "restore":
                self.gws.restore(fid)

    def _open_at(self, position: int) -> None:
        if self.model.rowCount() == 0:
            return
        self.open_review.emit(list(self.model.ids), max(0, position), self.query())
