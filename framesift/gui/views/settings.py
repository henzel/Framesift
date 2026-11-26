"""Settings page: the three folders, language, cache, workers, purge."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from framesift import DEFAULT_DELETE_DIRNAME, DEFAULT_REVIEW_DIRNAME
from framesift.gui.i18n import tr
from framesift.gui.prefs import Prefs
from framesift.gui.ui import button, label, page


class FolderPicker(QWidget):
    changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.edit = QLineEdit(self)
        self.edit.setProperty("role", "path")
        self.button = button("Choose…", variant="quiet")
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
        self.workers.setSpecialValueText(tr("Automatic workers"))
        self.workers.setValue(int(prefs.get("workers", 0)))
        self.low_priority = QCheckBox(tr("Low priority"), self)
        self.low_priority.setChecked(bool(prefs.get("low_priority", False)))
        self.open_btn = button("Open archive", variant="primary")
        self.open_btn.setEnabled(False)
        self.purge_btn = button("Empty the Delete folder…", variant="danger")
        self.purge_btn.setEnabled(False)
        self.recent = QListWidget(self)
        self.recent.setAccessibleName(tr("Recent"))
        self.recent.setMaximumHeight(108)

        content = QWidget(self)
        content.setObjectName("scrollContent")
        content.setMaximumWidth(860)
        form = QFormLayout()
        form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setVerticalSpacing(10)
        for key, picker in (
            ("Source folder", self.source),
            ("Review folder", self.review),
            ("Delete folder", self.delete),
        ):
            title = label(tr(key))
            title.setBuddy(picker.edit)
            picker.edit.setAccessibleName(tr(key))
            form.addRow(title, picker)
        form.addRow("", self.subfolders)
        general = QFormLayout()
        general.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        general.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        general.setVerticalSpacing(10)
        general.addRow(tr("Language"), self.language)
        general.addRow(tr("Thumbnail cache limit (GB)"), self.cache_limit)
        general.addRow(tr("Worker processes"), self.workers)
        general.addRow("", self.low_priority)
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(12)
        body.addWidget(label(tr("Archive folders"), "section"))
        body.addLayout(form)
        body.addWidget(self.open_btn, 0, Qt.AlignmentFlag.AlignLeft)
        body.addSpacing(7)
        body.addWidget(label(tr("General"), "section"))
        body.addLayout(general)
        body.addSpacing(7)
        body.addWidget(label(tr("Storage"), "section"))
        body.addWidget(self.purge_btn, 0, Qt.AlignmentFlag.AlignLeft)
        body.addSpacing(7)
        self.recent_label = label(tr("Recent"), "section")
        body.addWidget(self.recent_label)
        body.addWidget(self.recent)
        body.addStretch(1)

        scroll = QScrollArea(self)
        scroll.setProperty("role", "flat")
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        layout = page(self)
        layout.addWidget(label(tr("Settings"), "title"))
        layout.addWidget(scroll, 1)

        idx = self.language.findData(prefs.get("language", "system"))
        self.language.setCurrentIndex(max(idx, 0))
        self.source.changed.connect(self._source_changed)
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
        recent = self.prefs.get("recent", [])
        self.recent.setVisible(bool(recent))
        self.recent_label.setVisible(bool(recent))
        for s in recent:
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
        self.open_btn.setEnabled(bool(source))

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
