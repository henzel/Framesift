"""Journal page: searchable table of every move plus undo by scope."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from framesift.engine import api
from framesift.engine.config import CATEGORY_ORDER, LIVE_PHOTOS_CATEGORY
from framesift.engine.i18n import t
from framesift.gui.i18n import tr
from framesift.gui.models.journal import JournalModel
from framesift.gui.workspace import GuiWorkspace


class JournalView(QWidget):
    def __init__(self, gws: GuiWorkspace, parent=None):
        super().__init__(parent)
        self.gws = gws
        self.model = JournalModel(gws, self)
        self.search = QLineEdit(self)
        self.search.setPlaceholderText(tr("Search path…"))
        self.category = QComboBox(self)
        self.category.addItem(tr("All"), None)
        for c in (*CATEGORY_ORDER, LIVE_PHOTOS_CATEGORY):
            self.category.addItem(t(f"category.{c}"), c)
        self.action = QComboBox(self)
        self.action.addItem(tr("All"), None)
        for a in ("keep", "to_delete", "to_review", "restore", "detach_live", "undo", "purge"):
            self.action.addItem(t(f"action.{a}"), a)
        self.table = QTableView(self)
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.undo_last_btn = QPushButton(tr("Undo last"), self)
        self.undo_session_btn = QPushButton(tr("Undo session"), self)
        self.undo_category_btn = QPushButton(tr("Undo category"), self)
        self.undo_all_btn = QPushButton(tr("Undo everything"), self)
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
        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.table, 1)
        layout.addLayout(bottom)
        self.search.textChanged.connect(lambda _t: self.reload())
        self.category.currentIndexChanged.connect(lambda _i: self.reload())
        self.action.currentIndexChanged.connect(lambda _i: self.reload())
        self.undo_last_btn.clicked.connect(lambda: self._undo("last"))
        self.undo_session_btn.clicked.connect(lambda: self._undo("session"))
        self.undo_category_btn.clicked.connect(lambda: self._undo("category"))
        self.undo_all_btn.clicked.connect(lambda: self._undo("all"))
        gws.opened.connect(self.reload)
        gws.closed.connect(self.reload)
        gws.changed.connect(self.reload)
        gws.job_finished.connect(lambda _k, _r: self.reload())

    def reload(self) -> None:
        self.model.reload(
            text=self.search.text().strip(),
            category=self.category.currentData(),
            action=self.action.currentData(),
        )
        self.table.resizeColumnsToContents()
        enabled = self.gws.is_open and not self.gws.read_only
        for b in (
            self.undo_last_btn,
            self.undo_session_btn,
            self.undo_category_btn,
            self.undo_all_btn,
        ):
            b.setEnabled(enabled)

    def _undo(self, scope: str) -> None:
        ws = self.gws.ws
        if ws is None or self.gws.read_only:
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
        ops = api.select_undo_ops(ws, scope, session_id=session_id, category=category)
        if not ops:
            return
        if (
            QMessageBox.question(self, tr("Journal"), tr("Undo {n} operation(s)?", n=len(ops)))
            != QMessageBox.StandardButton.Yes
        ):
            return
        self.gws.wait_actions()

        def fn(progress, control):
            return api.run_undo(
                ws,
                scope,
                session_id=session_id,
                category=category,
                dry_run=False,
                progress=progress,
                control=control,
            )

        self.gws.run_job("undo", fn)
