"""Journal page: searchable table of every move plus undo by scope."""

from __future__ import annotations

from functools import partial

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QMessageBox,
    QTableView,
    QWidget,
)

from framesift.engine import api
from framesift.engine.config import CATEGORY_ORDER, LIVE_PHOTOS_CATEGORY
from framesift.engine.i18n import t
from framesift.gui.i18n import tr
from framesift.gui.models.journal import JournalModel
from framesift.gui.ui import button, label, page
from framesift.gui.workspace import GuiWorkspace


class JournalView(QWidget):
    def __init__(self, gws: GuiWorkspace, parent=None):
        super().__init__(parent)
        self.gws = gws
        self.model = JournalModel(gws, self)
        self._timer = QTimer(self, singleShot=True, interval=190)
        self._timer.timeout.connect(self.reload)
        self.search = QLineEdit(self)
        self.search.setProperty("role", "search")
        self.search.setPlaceholderText(tr("Search path…"))
        self.search.setAccessibleName(tr("Search path…"))
        self.search.setClearButtonEnabled(True)
        self.category = QComboBox(self)
        self.category.setAccessibleName(tr("Category"))
        self.category.addItem(tr("All categories"), None)
        for c in (*CATEGORY_ORDER, LIVE_PHOTOS_CATEGORY):
            self.category.addItem(t(f"category.{c}"), c)
        self.action = QComboBox(self)
        self.action.setAccessibleName(tr("Action"))
        self.action.addItem(tr("All actions"), None)
        for a in ("keep", "to_delete", "to_review", "restore", "detach_live", "undo", "purge"):
            self.action.addItem(t(f"action.{a}"), a)
        self.table = QTableView(self)
        self.table.setObjectName("history")
        self.table.setAccessibleName(tr("Activity"))
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setDefaultSectionSize(29)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 145)
        self.table.setColumnWidth(1, 94)
        self.table.setColumnWidth(2, 117)
        self.table.setColumnWidth(5, 62)
        self.table.verticalHeader().setVisible(False)
        self.undo_last_btn = button("Undo last", variant="quiet")
        self.undo_session_btn = button("Undo session", variant="quiet")
        self.undo_category_btn = button("Undo category", variant="quiet")
        self.undo_all_btn = button("Undo everything", variant="quiet")
        self.empty = label(tr("File moves and undo actions appear here."), "notice")
        top = QHBoxLayout()
        top.addWidget(self.search, 1)
        top.addWidget(self.category)
        top.addWidget(self.action)
        bottom = QHBoxLayout()
        for b in (
            self.undo_last_btn,
            self.undo_session_btn,
            self.undo_category_btn,
            self.undo_all_btn,
        ):
            bottom.addWidget(b)
        bottom.addStretch(1)
        layout = page(self)
        layout.addWidget(label(tr("Activity"), "title"))
        layout.addLayout(top)
        layout.addWidget(self.empty)
        layout.addWidget(self.table, 1)
        layout.addLayout(bottom)
        self.search.textChanged.connect(lambda _t: self._timer.start())
        self.category.currentIndexChanged.connect(lambda _i: self.reload())
        self.action.currentIndexChanged.connect(lambda _i: self.reload())
        self.undo_last_btn.clicked.connect(lambda: self._undo("last"))
        self.undo_session_btn.clicked.connect(lambda: self._undo("session"))
        self.undo_category_btn.clicked.connect(lambda: self._undo("category"))
        self.undo_all_btn.clicked.connect(lambda: self._undo("all"))
        self.model.modelReset.connect(self._update_buttons)
        gws.closed.connect(lambda: self.model.replace_rows([]))
        gws.state_changed.connect(self._update_buttons)
        gws.changed.connect(self._schedule_reload)
        self._update_buttons()

    def reload(self) -> None:
        self.model.reload(
            text=self.search.text().strip(),
            category=self.category.currentData(),
            action=self.action.currentData(),
        )
        self._update_buttons()

    def _schedule_reload(self) -> None:
        if self.isVisible():
            self._timer.start()

    def _update_buttons(self) -> None:
        self.empty.setVisible(not self.model.rows)
        enabled = self.gws.can_write and bool(self.model.rows)
        for b in (
            self.undo_last_btn,
            self.undo_session_btn,
            self.undo_category_btn,
            self.undo_all_btn,
        ):
            b.setEnabled(enabled)

    def _undo(self, scope: str) -> None:
        ws = self.gws.ws
        if ws is None or not self.gws.can_write:
            return
        category = None
        session_id = None
        if scope == "category":
            idx = self.table.currentIndex()
            row = idx.data(Qt.ItemDataRole.UserRole) if idx.isValid() else None
            category = (row or {}).get("category") or self.category.currentData()
            if not category:
                return
        if scope == "session":
            idx = self.table.currentIndex()
            row = idx.data(Qt.ItemDataRole.UserRole) if idx.isValid() else None
            session_id = (row or {}).get("session_id")
        filters = {"session_id": session_id, "category": category}
        self.gws.request(
            "journal.undo",
            partial(api.select_undo_ops, ws, scope, **filters),
            lambda ops: self._confirm_undo(ws, scope, filters, ops),
            after_actions=True,
        )

    def _confirm_undo(self, ws, scope: str, filters: dict, ops: list[str]) -> None:
        if (
            ops
            and self.gws.can_write
            and QMessageBox.question(
                self,
                tr("Activity"),
                tr("Undo {n} operation(s)?", n=len(ops)),
            )
            == QMessageBox.StandardButton.Yes
            and self.gws.ws is ws
        ):
            self.gws.run_job("undo", partial(api.run_undo, ws, scope, dry_run=False, **filters))
