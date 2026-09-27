"""Main window: pages, toolbar, status bar, dialogs for opening folders and purging."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QStackedWidget,
    QToolBar,
)

from framesift import APP_NAME
from framesift.engine import api
from framesift.engine.catalog import LockHeld
from framesift.engine.i18n import t
from framesift.engine.jobs import format_eta
from framesift.gui.i18n import tr
from framesift.gui.models.tree import human_bytes
from framesift.gui.prefs import Prefs
from framesift.gui.views.auto import AutoView
from framesift.gui.views.browser import BrowserView
from framesift.gui.views.groups import GroupsView
from framesift.gui.views.journal import JournalView
from framesift.gui.views.manual import ReviewView
from framesift.gui.views.settings import SettingsView
from framesift.gui.workspace import GuiWorkspace


class MainWindow(QMainWindow):
    def __init__(self, prefs: Prefs | None = None, cache_dir: Path | None = None):
        super().__init__()
        self.prefs = prefs or Prefs()
        self.gws = GuiWorkspace(self.prefs, cache_dir, self)
        self.setWindowTitle(APP_NAME)
        self.resize(1280, 800)

        self.browser = BrowserView(self.gws, self)
        self.review = ReviewView(self.gws, self)
        self.journal = JournalView(self.gws, self)
        self.settings = SettingsView(self.prefs, self)
        self.groups = GroupsView(self.gws, self)
        self.auto = AutoView(self.gws, self)
        self.pages = QStackedWidget(self)
        self.page_index: dict[str, int] = {}
        for name, widget in (
            ("browser", self.browser),
            ("review", self.review),
            ("groups", self.groups),
            ("auto", self.auto),
            ("journal", self.journal),
            ("settings", self.settings),
        ):
            self.page_index[name] = self.pages.addWidget(widget)
        self.setCentralWidget(self.pages)

        self.toolbar = QToolBar(self)
        self.toolbar.setMovable(False)
        self.toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.addToolBar(self.toolbar)
        self.nav_actions: dict[str, QAction] = {}
        for name, label in (
            ("browser", "Browser"),
            ("review", "Review"),
            ("groups", "Groups"),
            ("auto", "Automatic"),
            ("journal", "Journal"),
            ("settings", "Settings"),
        ):
            act = QAction(tr(label), self)
            act.setCheckable(True)
            act.triggered.connect(lambda _c=False, n=name: self.show_page(n))
            self.toolbar.addAction(act)
            self.nav_actions[name] = act
        self.toolbar.addSeparator()
        self.open_action = QAction(tr("Open folder…"), self)
        self.open_action.triggered.connect(lambda: self.show_page("settings"))
        self.toolbar.addAction(self.open_action)

        self.progress = QProgressBar(self)
        self.progress.setMaximumWidth(220)
        self.progress.setVisible(False)
        self.status_label = QLabel(self)
        self.statusBar().addWidget(self.status_label, 1)
        self.statusBar().addPermanentWidget(self.progress)

        self.browser.open_review.connect(self._open_review)
        self.browser.status.connect(self.status_label.setText)
        self.review.closed.connect(lambda: self.show_page("browser"))
        self.groups.closed.connect(lambda: self.show_page("auto"))
        self.auto.open_groups.connect(lambda: self.show_page("groups"))
        self.settings.open_requested.connect(self.open_source)
        self.settings.purge_requested.connect(self.purge_dialog)
        self.settings.language_changed.connect(self._language_changed)
        self.gws.job_progress.connect(self._job_progress)
        self.gws.job_finished.connect(self._job_finished)
        self.gws.job_failed.connect(self._job_failed)
        self.gws.action_failed.connect(lambda _tag, msg: self.statusBar().showMessage(msg, 8000))
        self.gws.opened.connect(lambda: self.settings.purge_btn.setEnabled(not self.gws.read_only))
        self.show_page("settings")

    # ------------------------------------------------------------------ pages
    def show_page(self, name: str) -> None:
        self.pages.setCurrentIndex(self.page_index[name])
        for n, act in self.nav_actions.items():
            act.setChecked(n == name)
        if name == "review":
            self.review.setFocus()
        elif name == "groups":
            self.groups.start()
        elif name == "auto":
            self.auto.refresh_plan()
        elif name == "journal":
            self.journal.reload()

    def _open_review(self, ids: list[int], position: int, query: Any) -> None:
        self.review.start(ids, position, query)
        self.show_page("review")

    # ------------------------------------------------------------------ opening
    def open_source(
        self,
        source: str,
        review: str = "",
        delete: str = "",
        include_subfolders: bool = True,
        *,
        force_lock: bool = False,
        read_only: bool = False,
    ) -> bool:
        src = Path(source)
        try:
            # opening a catalog on a network share can take a few seconds
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                self.gws.open_source(
                    src,
                    Path(review) if review else None,
                    Path(delete) if delete else None,
                    include_subfolders=include_subfolders,
                    force_lock=force_lock,
                    read_only=read_only,
                )
            finally:
                QApplication.restoreOverrideCursor()
        except api.WorkspaceError as exc:
            QMessageBox.critical(
                self, tr("Error"), "\n".join(t(i.key, detail=i.detail) for i in exc.issues)
            )
            return False
        except LockHeld as exc:
            box = QMessageBox(
                QMessageBox.Icon.Warning,
                tr("Warning"),
                tr(
                    "The catalog is locked by {host} (pid {pid}). Open read-only, or take over the lock if that process is gone?",
                    host=exc.owner.get("host"),
                    pid=exc.owner.get("pid"),
                ),
                parent=self,
            )
            ro = box.addButton(tr("Read only"), QMessageBox.ButtonRole.AcceptRole)
            take = box.addButton(tr("Take over"), QMessageBox.ButtonRole.DestructiveRole)
            box.addButton(tr("Cancel"), QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() == ro:
                return self.open_source(source, review, delete, include_subfolders, read_only=True)
            if box.clickedButton() == take:
                return self.open_source(source, review, delete, include_subfolders, force_lock=True)
            return False
        except Exception as exc:
            QMessageBox.critical(self, tr("Error"), str(exc))
            return False
        warnings = [t(i.key, detail=i.detail) for i in self.gws.last_issues if i.level == "warning"]
        if warnings:
            QMessageBox.warning(self, tr("Warning"), "\n\n".join(warnings))
        self.settings.reload_recent()
        self.setWindowTitle(
            f"{APP_NAME} — {src}" + (f" ({tr('Read only')})" if self.gws.read_only else "")
        )
        self.show_page("browser")
        if not self.gws.read_only:
            self.gws.scan()
        return True

    # ------------------------------------------------------------------ jobs
    def _job_progress(self, kind: str, data: dict[str, Any]) -> None:
        stage = data.get("stage", "")
        if "done" in data and "total" in data and data["total"]:
            self.progress.setVisible(True)
            self.progress.setRange(0, int(data["total"]))
            self.progress.setValue(int(data["done"]))
            text = f"{kind}: {stage} {data['done']:,}/{data['total']:,}  {tr('ETA')} {format_eta(data.get('eta_seconds'))}"
        else:
            self.progress.setVisible(True)
            self.progress.setRange(0, 0)
            seen = data.get("files_seen")
            text = f"{kind}: {stage}" + (f" {seen:,}" if seen else "")
        self.status_label.setText(text)
        if kind == "scan" and stage == "walk":
            self.gws.scan_batch.emit()

    def _job_finished(self, kind: str, result: Any) -> None:
        self.progress.setVisible(False)
        self.status_label.setText(f"{kind}: {tr('Done')}")

    def _job_failed(self, kind: str, message: str) -> None:
        self.progress.setVisible(False)
        self.status_label.setText(f"{kind}: {tr('Failed')} — {message}")

    # ------------------------------------------------------------------ purge
    def purge_dialog(self) -> None:
        ws = self.gws.ws
        if ws is None or self.gws.read_only:
            return
        preview = api.status(ws)["purge"]
        if preview["files"] == 0:
            QMessageBox.information(
                self, tr("Empty the Delete folder…"), tr("Nothing to review here.")
            )
            return
        size = human_bytes(preview["bytes"])
        if preview["network"]:
            text = (
                tr("Delete {n} file(s) ({size}) permanently?", n=preview["files"], size=size)
                + "\n\n"
                + t("purge.network_warning")
            )
            button = tr("Empty")
        else:
            text = tr(
                "Move {n} file(s) ({size}) to the system trash?", n=preview["files"], size=size
            )
            button = tr("Move to trash")
        box = QMessageBox(
            QMessageBox.Icon.Warning, tr("Empty the Delete folder…"), text, parent=self
        )
        go = box.addButton(button, QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(tr("Cancel"), QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() != go:
            return
        self.gws.wait_actions()

        def fn(progress, control):
            return api.run_purge(ws, dry_run=False, progress=progress, control=control)

        self.gws.run_job("purge", fn)

    # ------------------------------------------------------------------ misc
    def _language_changed(self, code: str) -> None:
        from framesift.engine.i18n import set_language

        set_language(None if code == "system" else code)
        QMessageBox.information(
            self, APP_NAME, "Language applies after restart. / Язык применится после перезапуска."
        )

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        self.review.video_view.stop()
        self.gws.shutdown()
        super().closeEvent(event)
