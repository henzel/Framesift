"""Desktop window, navigation, native menus and archive lifecycle."""

from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QCloseEvent, QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QSizePolicy,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from framesift import APP_NAME
from framesift.engine import api
from framesift.engine.catalog import LockHeld
from framesift.engine.i18n import t
from framesift.engine.jobs import format_eta
from framesift.engine.purge import purge_preview
from framesift.gui.i18n import tr
from framesift.gui.models.tree import human_bytes
from framesift.gui.prefs import Prefs
from framesift.gui.theme import is_dark
from framesift.gui.ui import button, icon, label, workspace_style
from framesift.gui.views.auto import AutoView
from framesift.gui.views.browser import BrowserView
from framesift.gui.views.groups import GroupsView
from framesift.gui.views.journal import JournalView
from framesift.gui.views.manual import ReviewView
from framesift.gui.views.settings import SettingsView
from framesift.gui.workspace import GuiWorkspace

NAVIGATION = {
    "browser": ("Library", "library"),
    "review": ("Manual review", "review"),
    "groups": ("Compare groups", "compare"),
    "auto": ("Analysis", "analysis"),
    "journal": ("Activity", "history"),
    "settings": ("Settings", "settings"),
}
JOB_LABELS = {
    "scan": "Scanning archive",
    "classify": "Analyzing media",
    "recompute": "Updating suggestions",
    "apply": "Moving candidates",
    "live_photos": "Moving Live Photo videos",
    "undo": "Undoing changes",
    "purge": "Emptying Delete folder",
    "selection": "Processing selection",
}


class MainWindow(QMainWindow):
    def __init__(self, prefs: Prefs | None = None, cache_dir: Path | None = None):
        super().__init__()
        self.prefs = prefs or Prefs()
        self.gws = GuiWorkspace(self.prefs, cache_dir, self)
        self._open_args: tuple[str, str, str, bool] | None = None
        self._rescan_on_open = True
        self._closing = False
        self.setWindowTitle(APP_NAME)
        self.resize(1220, 790)
        self.setMinimumSize(870, 580)

        self.browser = BrowserView(self.gws, self)
        self.review = ReviewView(self.gws, self)
        self.journal = JournalView(self.gws, self)
        self.settings = SettingsView(self.prefs, self)
        self.groups = GroupsView(self.gws, self)
        self.auto = AutoView(self.gws, self)
        self.pages = QStackedWidget(self)
        self.pages.setObjectName("pages")
        self.page_index = {
            name: self.pages.addWidget(widget)
            for name, widget in (
                ("browser", self.browser),
                ("review", self.review),
                ("groups", self.groups),
                ("auto", self.auto),
                ("journal", self.journal),
                ("settings", self.settings),
            )
        }
        self.nav_actions: dict[str, QAction] = {}
        self.nav_buttons: dict[str, QToolButton] = {}
        self.sidebar = self._sidebar()
        workspace = QWidget(self)
        workspace.setObjectName("workspace")
        layout = QHBoxLayout(workspace)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.sidebar)
        layout.addWidget(self.pages, 1)
        self.setCentralWidget(workspace)
        self._menus()

        self.progress = QProgressBar(self)
        self.progress.setFixedWidth(145)
        self.progress.setMaximumHeight(12)
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        self.status_label = label("", "caption")
        self.status_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.statusBar().setSizeGripEnabled(False)
        self.statusBar().addWidget(self.status_label, 1)
        self.statusBar().addPermanentWidget(self.progress)
        self.cancel_selection = button("Cancel", variant="quiet")
        self.cancel_selection.clicked.connect(self._cancel_selection)
        self.statusBar().addPermanentWidget(self.cancel_selection)
        self.cancel_selection.hide()
        self.statusBar().messageChanged.connect(self._update_statusbar)
        self.statusBar().hide()

        self.browser.open_review.connect(self._open_review)
        self.browser.open_folder.connect(self.choose_folder)
        self.browser.open_recent.connect(self.open_saved)
        self.browser.tree.clicked.connect(self._folder_clicked)
        self.browser.status.connect(self._show_message)
        self.review.closed.connect(lambda: self.show_page("browser"))
        self.groups.closed.connect(lambda: self.show_page("auto"))
        self.auto.open_groups.connect(lambda: self.show_page("groups"))
        self.settings.open_requested.connect(self.open_source)
        self.settings.purge_requested.connect(self.purge_dialog)
        self.settings.language_changed.connect(self._language_changed)
        self.gws.opened.connect(self._archive_opened)
        self.gws.open_failed.connect(self._archive_failed)
        self.gws.closed.connect(self._archive_closed)
        self.gws.settled.connect(self._workspace_settled)
        self.gws.state_changed.connect(self._state_changed)
        self.gws.job_progress.connect(self._job_progress)
        self.gws.job_finished.connect(self._job_finished)
        self.gws.job_failed.connect(self._job_failed)
        self.gws.action_failed.connect(lambda _tag, message: self._show_message(message))
        self.gws.action_done.connect(self._selection_action_done)

        app = QApplication.instance()
        app.styleHints().colorSchemeChanged.connect(self._appearance_changed)
        self._appearance_changed()
        self.show_page("browser")
        self._state_changed()

    def _sidebar(self) -> QWidget:
        sidebar = QWidget(self)
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(211)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(12, 16, 12, 13)
        layout.setSpacing(4)
        self.open_button = button("Open folder…", variant="quiet")
        self.open_button.clicked.connect(self.choose_folder)
        layout.addWidget(self.open_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addSpacing(11)
        self.nav_group = QActionGroup(self)
        self.nav_group.setExclusive(True)
        for name, (title, _) in NAVIGATION.items():
            action = QAction(tr(title), self)
            action.setCheckable(True)
            action.triggered.connect(partial(self._navigate, name))
            self.nav_group.addAction(action)
            self.nav_actions[name] = action
            nav = QToolButton(sidebar)
            nav.setDefaultAction(action)
            nav.setProperty("role", "navigation")
            nav.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            nav.setIconSize(QSize(18, 18))
            nav.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            nav.setAccessibleName(tr(title))
            self.nav_buttons[name] = nav
            if name != "settings":
                layout.addWidget(nav)
        layout.addSpacing(16)
        self.folder_label = label(tr("Folders"), "folderSection")
        self.folder_label.setContentsMargins(8, 0, 0, 2)
        layout.addWidget(self.folder_label)
        layout.addWidget(self.browser.tree, 1)
        layout.addWidget(self.nav_buttons["settings"])
        return sidebar

    def _menus(self) -> None:
        file_menu = self.menuBar().addMenu(tr("File"))
        self.open_action = QAction(tr("Open folder…"), self)
        self.open_action.setShortcut(QKeySequence.StandardKey.Open)
        self.open_action.triggered.connect(self.choose_folder)
        file_menu.addAction(self.open_action)
        self.rescan_action = file_menu.addAction(tr("Rescan folder"), self.gws.scan)
        self.rescan_action.setShortcut(QKeySequence("Ctrl+R"))
        self.nav_actions["settings"].setMenuRole(QAction.MenuRole.PreferencesRole)
        self.nav_actions["settings"].setShortcut(QKeySequence.StandardKey.Preferences)
        file_menu.addAction(self.nav_actions["settings"])
        view = self.menuBar().addMenu(tr("View"))
        for name in ("browser", "review", "groups", "auto", "journal"):
            view.addAction(self.nav_actions[name])
        view.addSeparator()
        toggle = view.addAction(tr("Show Sidebar"))
        toggle.setCheckable(True)
        toggle.setChecked(True)
        toggle.setShortcut(QKeySequence("Ctrl+Alt+S"))
        toggle.toggled.connect(self.sidebar.setVisible)
        find = QShortcut(QKeySequence.StandardKey.Find, self)
        find.activated.connect(self._focus_search)

    def _appearance_changed(self, *_args: Any) -> None:
        dark = is_dark(QApplication.instance())
        self.setStyleSheet(workspace_style(dark))
        color = QColor("#d4dae5" if dark else "#404a59")
        for name, (_, symbol) in NAVIGATION.items():
            self.nav_actions[name].setIcon(icon(symbol, color))
        self.open_button.setIcon(icon("folder", color))
        self.browser.rescan_btn.setIcon(icon("refresh", color))

    def _navigate(self, name: str, _checked: bool = False) -> None:
        if name == "review" and not self.review.ids and self.browser.open_btn.isEnabled():
            self.browser.open_btn.click()
        else:
            self.show_page(name)

    def show_page(self, name: str) -> None:
        previous = self.pages.currentWidget()
        if previous is self.review and name != "review":
            self.review.deactivate()
        if previous is self.groups and name != "groups":
            self.groups.deactivate()
        if previous is self.auto and name != "auto":
            self.auto.deactivate()
        self.pages.setCurrentIndex(self.page_index[name])
        self.nav_actions[name].setChecked(True)
        if name == "review":
            self.review.show_current()
            self.review.setFocus()
        elif name == "groups":
            self.groups.start()
        elif name == "auto":
            self.auto.activate()
        elif name == "journal":
            self.journal.reload()
        elif name == "browser":
            self.browser.list.setFocus()

    def _open_review(self, ids: list[int], position: int, query: Any) -> None:
        self.review.start(ids, position, query, self.browser.model.cached_thumbnail(position))
        self.show_page("review")

    def _focus_search(self) -> None:
        if self.gws.is_open:
            self.show_page("browser")
            self.browser.search.setFocus()
            self.browser.search.selectAll()

    def _folder_clicked(self, _index) -> None:
        if self.pages.currentWidget() is not self.browser:
            self.show_page("browser")
            self.browser.tree.setFocus()

    def choose_folder(self) -> None:
        if self.gws.opening:
            return
        folder = QFileDialog.getExistingDirectory(self, tr("Choose Folder…"), str(Path.home()))
        if folder:
            self.open_saved(folder)

    def open_saved(self, folder: str) -> bool:
        saved = self.prefs.source_settings(folder)
        self.settings.set_source(folder)
        return self.open_source(
            folder,
            saved.get("review", ""),
            saved.get("delete", ""),
            bool(saved.get("include_subfolders", True)),
            rescan=False,
        )

    def open_source(
        self,
        source: str,
        review: str = "",
        delete: str = "",
        include_subfolders: bool = True,
        *,
        force_lock: bool = False,
        read_only: bool = False,
        rescan: bool = True,
    ) -> bool:
        if not source or self._closing or self.gws.opening:
            return False
        self._open_args = source, review, delete, include_subfolders
        self._rescan_on_open = rescan
        self.show_page("browser")
        self._set_status(tr("Opening {folder}…", folder=Path(source).name or source))
        self.progress.setRange(0, 0)
        self.progress.show()
        return self.gws.open_source(
            Path(source),
            Path(review) if review else None,
            Path(delete) if delete else None,
            include_subfolders=include_subfolders,
            force_lock=force_lock,
            read_only=read_only,
        )

    def _archive_opened(self) -> None:
        if self._closing:
            return
        source = self.gws.roots.source
        self.settings.set_source(str(source))
        self.settings.reload_recent()
        self.setWindowTitle(f"{source.name or source} — {APP_NAME}")
        self._open_args = None
        self.progress.hide()
        self._set_status(tr("Read only") if self.gws.read_only else "")
        self.show_page("browser")
        if not self.gws.read_only and (self._rescan_on_open or not self.gws.has_catalog_files):
            self.gws.scan()
        warnings = [
            t(issue.key, detail=issue.detail)
            for issue in self.gws.last_issues
            if issue.level == "warning"
        ]
        if warnings:
            QMessageBox.warning(self, tr("Warning"), "\n\n".join(warnings))

    def _archive_failed(self, error: Exception) -> None:
        if self._closing:
            return
        self.progress.hide()
        self._set_status(tr("Could not open the archive."))
        args, self._open_args = self._open_args, None
        if isinstance(error, LockHeld) and args is not None:
            box = QMessageBox(
                QMessageBox.Icon.Warning,
                tr("Warning"),
                tr(
                    "The catalog is locked by {host} (pid {pid}). Open read-only, or take over the lock if that process is gone?",
                    host=error.owner.get("host"),
                    pid=error.owner.get("pid"),
                ),
                parent=self,
            )
            read = box.addButton(tr("Read only"), QMessageBox.ButtonRole.AcceptRole)
            take = box.addButton(tr("Take over"), QMessageBox.ButtonRole.DestructiveRole)
            cancel = box.addButton(tr("Cancel"), QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(read)
            box.setEscapeButton(cancel)
            box.exec()
            if box.clickedButton() == read:
                self.open_source(*args, read_only=True, rescan=self._rescan_on_open)
            elif box.clickedButton() == take:
                self.open_source(*args, force_lock=True, rescan=self._rescan_on_open)
        else:
            message = (
                "\n".join(t(i.key, detail=i.detail) for i in error.issues)
                if isinstance(error, api.WorkspaceError)
                else str(error)
            )
            QMessageBox.critical(self, tr("Error"), message)

    def _archive_closed(self) -> None:
        self.setWindowTitle(APP_NAME)
        self.review.reset()
        self.cancel_selection.hide()
        if not self._closing:
            self.show_page("browser")

    def _state_changed(self) -> None:
        self.open_action.setEnabled(not self.gws.opening and not self._closing)
        self.open_button.setEnabled(self.open_action.isEnabled())
        self.rescan_action.setEnabled(self.gws.can_write)
        self.settings.open_btn.setEnabled(
            not self.gws.opening and bool(self.settings.source.path())
        )
        self.settings.purge_btn.setEnabled(self.gws.can_write)
        for name in ("review", "groups", "auto", "journal"):
            self.nav_actions[name].setEnabled(self.gws.is_open)
        # Retain an expanding sidebar slot while there is no tree.
        self.folder_label.setVisible(self.gws.is_open)

    def _show_message(self, message: str) -> None:
        self.statusBar().showMessage(message, 10000)

    def _set_status(self, message: str) -> None:
        self.status_label.setText(message)
        self._update_statusbar()

    def _update_statusbar(self, *_args: Any) -> None:
        self.statusBar().setVisible(
            bool(self.status_label.text() or self.statusBar().currentMessage())
        )

    def _cancel_selection(self) -> None:
        job = self.gws.job("selection")
        if job is not None:
            job.control.cancel.set()
        self.cancel_selection.setEnabled(False)

    def _selection_action_done(self, tag: Any, result: Any) -> None:
        if tag[0] in ("keep", "to_delete", "restore"):
            self._report_errors(result)

    def _job_progress(self, kind: str, data: dict[str, Any]) -> None:
        self.progress.show()
        if kind == "selection" and not self.cancel_selection.isVisible():
            self.cancel_selection.setEnabled(True)
            self.cancel_selection.show()
        title = tr(JOB_LABELS.get(kind, kind))
        done = data.get("done", data.get("meta_done", 0))
        total = data.get("total", data.get("meta_total", 0))
        if total:
            self.progress.setRange(0, int(total))
            self.progress.setValue(int(done))
            message = f"{title}  ·  {done:,}/{total:,}"
            if data.get("eta_seconds") is not None:
                message += f"  ·  {tr('ETA')} {format_eta(data['eta_seconds'])}"
        else:
            self.progress.setRange(0, 0)
            seen = data.get("files_seen")
            message = f"{title}  ·  {seen:,}" if seen else title
        self._set_status(message)
        if kind == "scan" and data.get("stage") == "walk":
            self.gws.scan_batch.emit()

    def _job_finished(self, kind: str, result: Any) -> None:
        self.progress.hide()
        self.cancel_selection.hide()
        self._set_status("")
        if result is None or getattr(result, "cancelled", False):
            self._show_message(tr("Cancelled"))
        self._report_errors(result)

    def _report_errors(self, result: Any) -> None:
        errors = getattr(result, "errors", None)
        if errors:
            self._show_message(
                tr("{n} items could not be processed", n=len(errors)) + " · " + str(errors[0])
            )

    def _job_failed(self, kind: str, message: str) -> None:
        self.progress.hide()
        self.cancel_selection.hide()
        self._set_status(f"{tr('Failed')} — {message}")
        self.status_label.setToolTip(message)

    def purge_dialog(self) -> None:
        ws = self.gws.ws
        if ws is not None and self.gws.can_write:
            self.gws.request(
                "purge.preview",
                partial(purge_preview, ws.catalog, ws.roots),
                self._confirm_purge,
                after_actions=True,
            )

    def _confirm_purge(self, preview: dict) -> None:
        ws = self.gws.ws
        if ws is None or not self.gws.can_write:
            return
        if preview["files"] == 0:
            QMessageBox.information(
                self, tr("Empty the Delete folder…"), tr("Nothing to review here.")
            )
            return
        size = human_bytes(preview["bytes"])
        network = preview["network"]
        if network:
            text = tr("Delete {n} file(s) ({size}) permanently?", n=preview["files"], size=size)
            text += "\n\n" + t("purge.network_warning")
        else:
            text = tr(
                "Move {n} file(s) ({size}) to the system trash?", n=preview["files"], size=size
            )
        box = QMessageBox(
            QMessageBox.Icon.Warning, tr("Empty the Delete folder…"), text, parent=self
        )
        go = box.addButton(
            tr("Empty" if network else "Move to trash"), QMessageBox.ButtonRole.DestructiveRole
        )
        cancel = box.addButton(tr("Cancel"), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.exec()
        if box.clickedButton() == go and self.gws.ws is ws:
            self.gws.run_job(
                "purge", partial(api.run_purge, ws, dry_run=False, use_trash=not network)
            )

    def _language_changed(self, _code: str) -> None:
        QMessageBox.information(self, APP_NAME, tr("Language applies after restart."))

    def _workspace_settled(self) -> None:
        if not self._closing or self.gws.opening:
            return
        if self.gws.is_open:
            self.gws.close_async()
        else:
            QTimer.singleShot(0, self.close)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.gws.is_open or self.gws.opening or self.gws.media_busy:
            if not self._closing:
                self._closing = True
                self.review.deactivate()
                self.groups.deactivate()
                self._set_status(tr("Closing archive…"))
                self.centralWidget().setEnabled(False)
                if not self.gws.opening:
                    self.gws.close_async()
            event.ignore()
            return
        self.gws.shutdown()
        super().closeEvent(event)
