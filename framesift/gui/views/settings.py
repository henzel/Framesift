"""Settings page: the three folders, language, cache, workers, purge."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from framesift import DEFAULT_DELETE_DIRNAME, DEFAULT_REVIEW_DIRNAME
from framesift.engine.i18n import t
from framesift.engine.paths import check_roots
from framesift.gui.i18n import tr
from framesift.gui.prefs import Prefs


class FolderPicker(QWidget):
    changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.edit = QLineEdit(self)
        self.button = QPushButton(tr("Browse…"), self)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.edit, 1)
        layout.addWidget(self.button)
        self.button.clicked.connect(self._browse)
        self.edit.editingFinished.connect(lambda: self.changed.emit(self.edit.text()))

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, tr("Choose…"), self.edit.text() or str(Path.home())
        )
        if folder:
            self.edit.setText(folder)
            self.changed.emit(folder)

    def path(self) -> str:
        return self.edit.text().strip()

    def set_path(self, text: str) -> None:
        self.edit.setText(text)


class SettingsView(QWidget):
    open_requested = Signal(str, str, str, bool)  # source, review, delete, include_subfolders
    purge_requested = Signal()
    language_changed = Signal(str)

    def __init__(self, prefs: Prefs, parent=None):
        super().__init__(parent)
        self.prefs = prefs
        self.source = FolderPicker(self)
        self.review = FolderPicker(self)
        self.delete = FolderPicker(self)
        self.subfolders = QCheckBox(tr("Include subfolders"), self)
        self.subfolders.setChecked(True)
        self.language = QComboBox(self)
        self.language.addItem(tr("System"), "system")
        self.language.addItem("English", "en")
        self.language.addItem("Русский", "ru")
        self.cache_limit = QDoubleSpinBox(self)
        self.cache_limit.setRange(0.1, 200.0)
        self.cache_limit.setDecimals(1)
        self.cache_limit.setValue(float(prefs.get("cache_limit_gb", 2.0)))
        self.workers = QSpinBox(self)
        self.workers.setRange(0, 64)
        self.workers.setSpecialValueText("auto")
        self.workers.setValue(int(prefs.get("workers", 0)))
        self.low_priority = QCheckBox(tr("Low priority"), self)
        self.low_priority.setChecked(bool(prefs.get("low_priority", False)))
        self.open_btn = QPushButton(tr("Open"), self)
        self.purge_btn = QPushButton(tr("Empty the Delete folder…"), self)
        self.purge_btn.setEnabled(False)
        self.warnings = QLabel(self)
        self.warnings.setWordWrap(True)
        self.recent = QListWidget(self)
        self.recent.setMaximumHeight(120)

        form = QFormLayout()
        form.addRow(tr("Source folder"), self.source)
        form.addRow(tr("Review folder"), self.review)
        form.addRow(tr("Delete folder"), self.delete)
        form.addRow("", self.subfolders)
        form.addRow(tr("Language"), self.language)
        form.addRow(tr("Thumbnail cache limit (GB)"), self.cache_limit)
        form.addRow(tr("Worker processes"), self.workers)
        form.addRow("", self.low_priority)
        buttons = QHBoxLayout()
        buttons.addWidget(self.open_btn)
        buttons.addWidget(self.purge_btn)
        buttons.addStretch(1)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addLayout(buttons)
        layout.addWidget(self.warnings)
        layout.addWidget(QLabel(tr("Recent")))
        layout.addWidget(self.recent)
        layout.addStretch(1)

        idx = self.language.findData(prefs.get("language", "system"))
        self.language.setCurrentIndex(max(idx, 0))
        self.source.changed.connect(self._source_changed)
        self.review.changed.connect(lambda _t: self._validate())
        self.delete.changed.connect(lambda _t: self._validate())
        self.open_btn.clicked.connect(self._open)
        self.purge_btn.clicked.connect(self.purge_requested)
        self.language.currentIndexChanged.connect(self._language)
        self.cache_limit.valueChanged.connect(lambda v: prefs.set("cache_limit_gb", float(v)))
        self.workers.valueChanged.connect(lambda v: prefs.set("workers", int(v)))
        self.low_priority.toggled.connect(lambda c: prefs.set("low_priority", bool(c)))
        self.recent.itemDoubleClicked.connect(lambda item: self.set_source(item.text()))
        self.reload_recent()

    def reload_recent(self) -> None:
        self.recent.clear()
        for s in self.prefs.get("recent", []):
            self.recent.addItem(s)

    def set_source(self, source: str) -> None:
        self.source.set_path(source)
        self._source_changed(source)

    def _source_changed(self, source: str) -> None:
        saved = self.prefs.source_settings(source)
        if source:
            self.review.set_path(saved.get("review") or str(Path(source) / DEFAULT_REVIEW_DIRNAME))
            self.delete.set_path(saved.get("delete") or str(Path(source) / DEFAULT_DELETE_DIRNAME))
            self.subfolders.setChecked(bool(saved.get("include_subfolders", True)))
        self._validate()

    def _validate(self) -> list:
        src = self.source.path()
        if not src:
            self.warnings.setText("")
            return []
        issues = check_roots(
            Path(src), Path(self.review.path() or src), Path(self.delete.path() or src)
        )
        self.warnings.setText(
            "\n".join(
                f"{'⛔' if i.level == 'error' else '⚠'} {t(i.key, detail=i.detail)}" for i in issues
            )
        )
        self.open_btn.setEnabled(not any(i.level == "error" for i in issues))
        return issues

    def _open(self) -> None:
        if not self.source.path():
            return
        self.open_requested.emit(
            self.source.path(), self.review.path(), self.delete.path(), self.subfolders.isChecked()
        )

    def _language(self, _index: int) -> None:
        code = self.language.currentData()
        self.prefs.set("language", code)
        self.language_changed.emit(code)
