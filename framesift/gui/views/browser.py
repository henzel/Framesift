"""The media library, its folder selection, and the viewport-driven thumbnail gallery."""

from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import Any

from PySide6.QtCore import QModelIndex, QRect, QRectF, QSignalBlocker, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QKeyEvent, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QMenu,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from framesift.gui.i18n import tr
from framesift.gui.models.catalog_list import CatalogListModel, FileIdRole, ListQuery, RowRole
from framesift.gui.models.tree import FolderSnapshot, FolderTreeModel, NodeRole, folder_snapshot
from framesift.gui.theme import is_dark
from framesift.gui.ui import application_icon, button, draw_icon, label, page
from framesift.gui.workspace import GuiWorkspace

SOURCE = {"root": "source", "dir_path": None}


class ThumbDelegate(QStyledItemDelegate):
    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        row = index.data(RowRole)
        pixmap = index.data(Qt.ItemDataRole.DecorationRole) if row else None
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        focused = bool(option.state & QStyle.StateFlag.State_HasFocus)
        dark = is_dark(QApplication.instance())
        accent = QColor("#78baff" if dark else "#0865ce")
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        rect = option.rect.adjusted(3, 3, -3, -3)
        box = rect.adjusted(4, 4, -4, -26)
        if selected or focused:
            painter.setPen(QPen(accent, 1) if focused else Qt.PenStyle.NoPen)
            painter.setBrush(
                QColor("#283e5b" if dark else "#e2efff") if selected else Qt.BrushStyle.NoBrush
            )
            painter.drawRoundedRect(QRectF(rect), 9, 9)

        clip = QPainterPath()
        clip.addRoundedRect(QRectF(box), 7, 7)
        painter.save()
        painter.setClipPath(clip)
        painter.fillRect(box, QColor("#2a2d34" if dark else "#edf0f5"))
        if pixmap is not None and not pixmap.isNull():
            size = pixmap.size().scaled(box.size(), Qt.AspectRatioMode.KeepAspectRatio)
            target = QRect(
                box.center().x() - size.width() // 2,
                box.center().y() - size.height() // 2,
                size.width(),
                size.height(),
            )
            painter.drawPixmap(target, pixmap)
        else:
            placeholder = QRectF(box.center().x() - 13, box.center().y() - 13, 26, 26)
            draw_icon(
                painter,
                placeholder,
                "play" if row and row["kind"] == "video" else "library",
                QColor("#8e99ab" if dark else "#7c8798"),
            )
        if row:
            badges = []
            if row["kind"] == "video":
                duration = (row.get("m_duration_ms") or 0) // 1000
                badges.append(f"▶ {duration // 60}:{duration % 60:02d}" if duration else "▶")
            if row.get("live_count"):
                badges.append("LIVE")
            if row.get("edited_of"):
                badges.append("✎")
            if row.get("reviewed_at"):
                badges.append("✓")
            if badges:
                text = "  ".join(badges)
                font = QFont(option.font)
                font.setPixelSize(10)
                font.setBold(True)
                painter.setFont(font)
                width = QFontMetrics(font).horizontalAdvance(text) + 12
                tag = QRect(box.x() + 6, box.y() + 6, width, 18)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(18, 20, 25, 215))
                painter.drawRoundedRect(QRectF(tag), 5, 5)
                painter.setPen(Qt.GlobalColor.white)
                painter.drawText(tag, Qt.AlignmentFlag.AlignCenter, text)
        painter.restore()

        text_rect = QRect(rect.x() + 5, box.bottom() + 2, rect.width() - 10, 22)
        font = QFont(option.font)
        font.setPixelSize(12)
        painter.setFont(font)
        painter.setPen(accent if selected else QColor("#f2f3f5" if dark else "#20232a"))
        name = index.data(Qt.ItemDataRole.DisplayRole) or ""
        painter.drawText(
            text_rect,
            Qt.AlignmentFlag.AlignCenter,
            QFontMetrics(font).elidedText(name, Qt.TextElideMode.ElideMiddle, text_rect.width()),
        )
        painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        view = self.parent()
        return view.gridSize()


class GridView(QListView):
    activated_item = Signal(int)

    def resizeEvent(self, event) -> None:
        width = max(155, self.viewport().width())
        columns = max(1, width // 154)
        cell = width // columns
        size = QSize(cell, round(cell * 0.72) + 30)
        if self.gridSize() != size:
            self.setGridSize(size)
        super().resizeEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            if self.currentIndex().isValid():
                self.activated_item.emit(self.currentIndex().row())
                return
        super().keyPressEvent(event)


class BrowserView(QWidget):
    open_review = Signal(list, int, object)
    open_folder = Signal()
    open_recent = Signal(str)
    status = Signal(str)

    def __init__(self, gws: GuiWorkspace, parent=None):
        super().__init__(parent)
        self.gws = gws
        self.model = CatalogListModel(gws, self)
        self.tree_model = FolderTreeModel(gws, self)
        self.current_node = dict(SOURCE)
        self._refresh_pending = False
        self._return_to: tuple[int, int] | None = None
        self._expanded: dict[tuple, dict] = {}
        self._refresh_timer = QTimer(self, singleShot=True, interval=650)
        self._refresh_timer.timeout.connect(self._refresh)
        self._search_timer = QTimer(self, singleShot=True, interval=160)
        self._search_timer.timeout.connect(self.reload)

        self.tree = QTreeView(self)
        self.tree.setObjectName("folders")
        self.tree.setModel(self.tree_model)
        self.tree.setAccessibleName(tr("Folders"))
        self.tree.setHeaderHidden(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setIndentation(13)
        self.tree.setIconSize(QSize(15, 15))
        self.tree.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.tree.selectionModel().currentChanged.connect(self._on_tree_current)
        self.tree.expanded.connect(lambda index: self._remember_expanded(index, True))
        self.tree.collapsed.connect(lambda index: self._remember_expanded(index, False))

        self.title = label(tr("Library"), "title")
        self.context = label("", "context")
        self.search = QLineEdit(self)
        self.search.setProperty("role", "search")
        self.search.setPlaceholderText(tr("Search filenames…"))
        self.search.setAccessibleName(tr("Search filenames…"))
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(200)
        self.search.textChanged.connect(lambda _text: self._search_timer.start())
        self.sort_box = QComboBox(self)
        self.sort_box.setAccessibleName(tr("Sort"))
        for key, label_key in (
            ("date", "Newest first"),
            ("name", "Sort by name"),
            ("size", "Largest first"),
        ):
            self.sort_box.addItem(tr(label_key), key)
        self.filter_box = QComboBox(self)
        self.filter_box.setAccessibleName(tr("Media type"))
        for key, label_key in (("all", "All media"), ("photo", "Photos"), ("video", "Videos")):
            self.filter_box.addItem(tr(label_key), key)
        self.show_reviewed = QCheckBox(tr("Show reviewed"), self)
        self.show_reviewed.setChecked(True)
        self.keep_btn = button("Keep", variant="quiet")
        self.delete_btn = button("Move to Delete", variant="danger")
        self.restore_btn = button("Restore", variant="quiet")
        self.open_btn = button("Review items", variant="primary")
        self.rescan_btn = button("", variant="quiet")
        self.rescan_btn.setFixedWidth(33)
        self.rescan_btn.setToolTip(tr("Rescan folder"))
        self.rescan_btn.setAccessibleName(tr("Rescan folder"))
        self.rescan_btn.clicked.connect(lambda: self.gws.scan())
        self.count_label = label("", "caption")

        self.list = GridView(self)
        self.list.setObjectName("gallery")
        self.list.setAccessibleName(tr("Media gallery"))
        self.list.setModel(self.model)
        self.list.setItemDelegate(ThumbDelegate(self.list))
        self.list.setViewMode(QListView.ViewMode.IconMode)
        self.list.setGridSize(QSize(170, 150))
        self.list.setResizeMode(QListView.ResizeMode.Adjust)
        self.list.setMovement(QListView.Movement.Static)
        self.list.setUniformItemSizes(True)
        self.list.setLayoutMode(QListView.LayoutMode.Batched)
        self.list.setBatchSize(180)
        self.list.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self.list.setSelectionMode(QListView.SelectionMode.ExtendedSelection)
        self.list.setSelectionRectVisible(True)
        self.list.setWordWrap(False)
        self.list.doubleClicked.connect(lambda idx: self._open_at(idx.row()))
        self.list.activated_item.connect(self._open_at)
        selection = self.list.selectionModel()
        selection.selectionChanged.connect(self._update_buttons)
        self.list.verticalScrollBar().valueChanged.connect(self._scrolling)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._item_menu)

        self.empty = label("", "context", wrap=True)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.content = QStackedWidget(self)
        self.content.addWidget(self.list)
        self.content.addWidget(self.empty)

        header = QHBoxLayout()
        text = QVBoxLayout()
        text.setSpacing(1)
        text.addWidget(self.title)
        text.addWidget(self.context)
        header.addLayout(text, 1)
        header.addWidget(self.search)
        header.addWidget(self.rescan_btn)

        filters = QHBoxLayout()
        filters.addWidget(self.filter_box)
        filters.addWidget(self.sort_box)
        filters.addWidget(self.show_reviewed)
        filters.addStretch(1)

        footer = QHBoxLayout()
        footer.addWidget(self.count_label, 1)
        for widget in (self.keep_btn, self.restore_btn, self.delete_btn, self.open_btn):
            footer.addWidget(widget)

        self.workspace = QWidget(self)
        workspace_layout = page(self.workspace)
        workspace_layout.addLayout(header)
        workspace_layout.addLayout(filters)
        workspace_layout.addWidget(self.content, 1)
        workspace_layout.addLayout(footer)
        self.welcome = self._make_welcome()
        self.surface = QStackedWidget(self)
        self.surface.addWidget(self.welcome)
        self.surface.addWidget(self.workspace)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.surface)

        self.sort_box.currentIndexChanged.connect(lambda _i: self.reload())
        self.filter_box.currentIndexChanged.connect(lambda _i: self.reload())
        self.show_reviewed.toggled.connect(lambda _c: self.reload())
        self.keep_btn.clicked.connect(lambda: self._act("keep"))
        self.delete_btn.clicked.connect(lambda: self._act("to_delete"))
        self.restore_btn.clicked.connect(lambda: self._act("restore"))
        self.open_btn.clicked.connect(
            lambda: self._open_at(
                self.list.currentIndex().row() if self.list.currentIndex().isValid() else 0
            )
        )
        self.model.loaded.connect(self._loaded)
        self.model.load_failed.connect(self.status)
        gws.opened.connect(self.rebuild)
        gws.closed.connect(self.clear)
        gws.changed.connect(self.schedule_refresh)
        gws.scan_batch.connect(self.schedule_refresh)
        gws.state_changed.connect(self._update_state)
        self._update_state()

    def _make_welcome(self) -> QWidget:
        widget = QWidget(self)
        layout = page(widget)
        layout.setSpacing(12)
        layout.addStretch(1)
        mark = QLabel(widget)
        mark.setPixmap(application_icon().pixmap(QSize(80, 80), self.devicePixelRatioF()))
        layout.addWidget(mark, 0, Qt.AlignmentFlag.AlignHCenter)
        title = label(tr("Open a photo archive"), "hero")
        layout.addWidget(title, 0, Qt.AlignmentFlag.AlignHCenter)
        detail = label(
            tr(
                "Choose a folder to browse photos and videos, find duplicates, and review what to keep."
            ),
            "context",
            wrap=True,
        )
        detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        detail.setFixedWidth(365)
        detail.setMinimumHeight(42)
        layout.addWidget(detail, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(6)
        self.welcome_open = button("Choose Folder…", variant="primary")
        self.welcome_open.clicked.connect(self.open_folder)
        layout.addWidget(self.welcome_open, 0, Qt.AlignmentFlag.AlignHCenter)
        self.recent_btn = button("Open recent archive", variant="quiet")
        self.recent_btn.clicked.connect(self._open_recent)
        layout.addWidget(self.recent_btn, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch(2)
        return widget

    def _open_recent(self) -> None:
        recent = self.gws.prefs.get("recent", [])
        if recent:
            self.open_recent.emit(recent[0])

    def query(self) -> ListQuery:
        node = self.current_node
        sort = self.sort_box.currentData() or "date"
        return ListQuery(
            root=node["root"],
            dir_path=node.get("dir_path"),
            sort=sort,
            descending=sort != "name",
            kind=self.filter_box.currentData() or "all",
            show_reviewed=self.show_reviewed.isChecked(),
            category=node.get("category"),
            search=self.search.text().strip(),
        )

    def rebuild(self) -> None:
        self.current_node = dict(SOURCE)
        self.surface.setCurrentWidget(self.workspace)
        self.reload()
        self._load_folders()

    def clear(self) -> None:
        self._refresh_timer.stop()
        self._refresh_pending = False
        self._return_to = None
        self._expanded.clear()
        self.current_node = dict(SOURCE)
        self.tree_model.rebuild()
        self.model.clear()
        self.surface.setCurrentWidget(self.welcome)

    def reload(self) -> None:
        if not self.gws.is_open:
            return
        self._return_to = None
        self._scrolling()
        self.model.load(self.query())
        self._update_state()

    def schedule_refresh(self) -> None:
        if self.gws.is_open and not self._refresh_timer.isActive():
            self._refresh_timer.start()

    def _refresh(self) -> None:
        if not self.gws.is_open:
            return
        if self.model.loading:
            self._refresh_pending = True
            return
        current = self.list.currentIndex().data(FileIdRole)
        self._return_to = (current or -1, self.list.verticalScrollBar().value())
        self.model.load(self.query())
        self._load_folders()

    def _load_folders(self) -> None:
        self.gws.request(
            "gallery.tree", partial(folder_snapshot, self.gws.catalog), self._set_folders
        )

    def _set_folders(self, snapshot: FolderSnapshot) -> None:
        with QSignalBlocker(self.tree.selectionModel()), QSignalBlocker(self.tree):
            self.tree_model.rebuild(snapshot)
            source = self.tree_model.index_of(self.tree_model.source_item)
            self.tree.expand(source)
            for node in self._expanded.values():
                index = self.tree_model.find(node)
                if index.isValid():
                    self.tree.expand(index)
            current = self.tree_model.find(self.current_node)
            self.tree.setCurrentIndex(current if current.isValid() else source)
        if not current.isValid() and self.current_node != SOURCE:
            self.current_node = dict(SOURCE)
            self.reload()
        self._set_heading()

    def _remember_expanded(self, index: QModelIndex, expanded: bool) -> None:
        node = index.data(NodeRole)
        if node:
            key = node["root"], node.get("dir_path"), node.get("category")
            if expanded:
                self._expanded[key] = node
            else:
                self._expanded.pop(key, None)

    def _on_tree_current(self, current: QModelIndex, _previous: QModelIndex) -> None:
        node = current.data(NodeRole)
        if node and node != self.current_node:
            self.current_node = node
            self.reload()

    def _set_heading(self) -> None:
        if not self.gws.is_open:
            return
        archive = self.gws.roots.source.name or str(self.gws.roots.source)
        node = self.current_node
        if node == SOURCE:
            self.title.setText(archive)
            self.context.setText(
                tr("Source") + (f"  ·  {tr('Read only')}" if self.gws.read_only else "")
            )
        else:
            current = self.tree_model.find(node)
            self.title.setText(current.data() or tr("Library"))
            self.context.setText(
                archive + (f"  ·  {tr('Read only')}" if self.gws.read_only else "")
            )

    def _loaded(self) -> None:
        if self._return_to is not None:
            fid, scroll = self._return_to
            pos = self.model.index_of(fid)
            if pos >= 0:
                self.list.setCurrentIndex(self.model.index(pos))
            self.list.verticalScrollBar().setValue(scroll)
            self._return_to = None
        else:
            self.list.scrollToTop()
        self._update_state()
        if self._refresh_pending:
            self._refresh_pending = False
            self.schedule_refresh()

    def _update_state(self) -> None:
        self.welcome_open.setEnabled(not self.gws.opening)
        recent = self.gws.prefs.get("recent", [])
        self.recent_btn.setVisible(bool(recent))
        self.recent_btn.setEnabled(not self.gws.opening)
        if recent:
            self.recent_btn.setText(tr("Open {folder}", folder=Path(recent[0]).name))
            self.recent_btn.setToolTip(recent[0])
        self._set_heading()
        self.rescan_btn.setEnabled(self.gws.can_write)
        self.list.setEnabled(not self.model.loading or self._return_to is not None)
        if self.model.error:
            self.empty.setText(tr("Could not read the library.") + "\n" + self.model.error)
        elif self.model.loading:
            self.empty.setText(tr("Loading media…"))
        elif self.model.query.search:
            self.empty.setText(tr("No files match your search."))
        elif self.gws.job("scan") is not None:
            self.empty.setText(tr("Looking for photos and videos…"))
        else:
            self.empty.setText(tr("No media in this view."))
        self.content.setCurrentWidget(self.list if self.model.rowCount() else self.empty)
        self._update_buttons()

    def _selection_count(self) -> int:
        return sum(block.height() for block in self.list.selectionModel().selection())

    def _update_buttons(self, *_args: Any) -> None:
        n = self.model.rowCount()
        selected = self._selection_count()
        idle = self.gws.is_open and not self.model.loading
        writable = idle and self.gws.can_write and selected > 0
        root = self.current_node["root"]
        self.keep_btn.setVisible(root == "source")
        self.restore_btn.setVisible(root != "source")
        self.delete_btn.setVisible(root != "delete")
        self.keep_btn.setEnabled(writable)
        self.delete_btn.setEnabled(writable and root != "delete")
        self.restore_btn.setEnabled(writable and root != "source")
        self.open_btn.setEnabled(idle and n > 0)
        text = tr("{n} media items", n=f"{n:,}")
        if selected:
            text += "  ·  " + tr("{n} selected", n=f"{selected:,}")
        self.count_label.setText(text)

    def _scrolling(self, *_args: Any) -> None:
        self.gws.thumb_loader.discard_waiting("gallery")

    def selected_ids(self) -> list[int]:
        return [
            fid
            for group in self.list.selectionModel().selection()
            for fid in self.model.ids[group.top() : group.bottom() + 1]
        ]

    def _item_menu(self, point) -> None:
        index = self.list.indexAt(point)
        if not index.isValid() or not self.open_btn.isEnabled():
            return
        if not self.list.selectionModel().isSelected(index):
            self.list.setCurrentIndex(index)
        fid = index.data(FileIdRole)
        menu = QMenu(self)
        menu.addAction(tr("Review items"), lambda: self._open_at(self.model.index_of(fid)))
        for widget, action in (
            (self.keep_btn, "keep"),
            (self.restore_btn, "restore"),
            (self.delete_btn, "to_delete"),
        ):
            if widget.isEnabled() and widget.isVisible():
                menu.addAction(widget.text(), partial(self._act, action))
        menu.exec(self.list.viewport().mapToGlobal(point))
        menu.deleteLater()

    def _act(self, action: str) -> None:
        if self.gws.can_write and not self.model.loading:
            self.gws.apply_selection(action, self.selected_ids())

    def _open_at(self, position: int) -> None:
        if self.model.loading or not 0 <= position < len(self.model.ids):
            return
        self.open_review.emit(self.model.ids, position, self.model.query)
