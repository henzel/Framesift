"""Automatic mode: scan → classify → dry-run report → confirm categories → apply, with thresholds."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from framesift.engine import api
from framesift.engine.config import CATEGORY_ORDER, ClassifyConfig
from framesift.engine.i18n import t
from framesift.engine.jobs import format_eta
from framesift.engine.plan import Plan
from framesift.gui.i18n import tr
from framesift.gui.models.catalog_list import meta_for_thumbnail
from framesift.gui.models.tree import human_bytes
from framesift.gui.workspace import GuiWorkspace


class CategoryCard(QGroupBox):
    def __init__(self, category: str, parent=None):
        super().__init__(t(f"category.{category}"), parent)
        self.category = category
        self.enabled_box = QCheckBox(tr("Apply"), self)
        self.enabled_box.setChecked(True)
        self.count_label = QLabel("0", self)
        self.size_label = QLabel("0 B", self)
        self.confidence_label = QLabel("", self)
        self.examples = QWidget(self)
        self.grid = QGridLayout(self.examples)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(4)
        head = QHBoxLayout()
        head.addWidget(self.enabled_box)
        head.addWidget(QLabel(tr("Count") + ":"))
        head.addWidget(self.count_label)
        head.addWidget(QLabel(tr("Size") + ":"))
        head.addWidget(self.size_label)
        head.addWidget(QLabel(tr("Confidence") + ":"))
        head.addWidget(self.confidence_label)
        head.addStretch(1)
        layout = QVBoxLayout(self)
        layout.addLayout(head)
        layout.addWidget(self.examples)
        self.thumbs: dict[int, QLabel] = {}

    def set_report(self, report: Any) -> None:
        self.count_label.setText(f"{report.count:,}")
        self.size_label.setText(human_bytes(report.bytes))
        self.confidence_label.setText(t(f"confidence.{report.confidence}"))
        self.enabled_box.setChecked(report.enabled)
        for i in reversed(range(self.grid.count())):
            w = self.grid.itemAt(i).widget()
            if w:
                w.setParent(None)
        self.thumbs.clear()
        for i, ex in enumerate(report.examples[:12]):
            lab = QLabel(self.examples)
            lab.setFixedSize(96, 96)
            lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lab.setStyleSheet("background: rgba(0,0,0,30);")
            lab.setToolTip(f"{ex.rel_path}\n{ex.reason}")
            self.grid.addWidget(lab, i // 12, i % 12)
            self.thumbs[ex.file_id] = lab


class AutoView(QWidget):
    open_groups = Signal()

    def __init__(self, gws: GuiWorkspace, parent=None):
        super().__init__(parent)
        self.gws = gws
        self.plan: Plan | None = None
        self.cards: dict[str, CategoryCard] = {}

        self.scan_btn = QPushButton(tr("Scan"), self)
        self.run_btn = QPushButton(tr("Run analysis"), self)
        self.apply_btn = QPushButton(tr("Apply selected categories"), self)
        self.live_btn = QPushButton(tr("Move all Live Photo videos to Review"), self)
        self.groups_btn = QPushButton(tr("Groups"), self)
        self.pause_btn = QPushButton(tr("Pause"), self)
        self.cancel_btn = QPushButton(tr("Cancel"), self)
        self.pause_btn.setEnabled(False)
        self.cancel_btn.setEnabled(False)
        self.progress = QProgressBar(self)
        self.progress.setVisible(False)
        self.progress_label = QLabel(self)

        self.short_video = QDoubleSpinBox(self)
        self.short_video.setRange(0.5, 60.0)
        self.short_video.setSingleStep(0.5)
        self.similar_window = QSpinBox(self)
        self.similar_window.setRange(1, 3600)
        self.similar_distance = QSlider(Qt.Orientation.Horizontal, self)
        self.similar_distance.setRange(0, 20)
        self.similar_distance_label = QLabel(self)
        self.blur = QDoubleSpinBox(self)
        self.blur.setRange(0.0, 500.0)
        self.blur.setSingleStep(1.0)
        self.dark = QDoubleSpinBox(self)
        self.dark.setRange(0.0, 0.5)
        self.dark.setSingleStep(0.01)
        self.dark.setDecimals(2)
        self.move_aae = QCheckBox(tr("Move all .AAE files to junk"), self)
        self.low_priority = QCheckBox(tr("Low priority"), self)
        self.workers = QSpinBox(self)
        self.workers.setRange(0, 64)
        self.workers.setSpecialValueText("auto")
        form = QFormLayout()
        form.addRow(tr("Short video threshold (s)"), self.short_video)
        form.addRow(tr("Similar window (s)"), self.similar_window)
        dist = QHBoxLayout()
        dist.addWidget(self.similar_distance)
        dist.addWidget(self.similar_distance_label)
        form.addRow(tr("Similar distance"), dist)
        form.addRow(tr("Blur threshold"), self.blur)
        form.addRow(tr("Dark threshold"), self.dark)
        form.addRow("", self.move_aae)
        form.addRow(tr("Worker processes"), self.workers)
        form.addRow("", self.low_priority)
        thresholds = QGroupBox(tr("Settings"), self)
        thresholds.setLayout(form)

        cards_widget = QWidget(self)
        self.cards_layout = QVBoxLayout(cards_widget)
        for c in CATEGORY_ORDER:
            card = CategoryCard(c, cards_widget)
            self.cards[c] = card
            self.cards_layout.addWidget(card)
        self.report_only = QLabel(self)
        self.report_only.setWordWrap(True)
        self.cards_layout.addWidget(self.report_only)
        self.cards_layout.addStretch(1)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setWidget(cards_widget)

        top = QHBoxLayout()
        for b in (
            self.scan_btn,
            self.run_btn,
            self.apply_btn,
            self.live_btn,
            self.groups_btn,
            self.pause_btn,
            self.cancel_btn,
        ):
            top.addWidget(b)
        top.addStretch(1)
        prog = QHBoxLayout()
        prog.addWidget(self.progress)
        prog.addWidget(self.progress_label, 1)
        body = QHBoxLayout()
        body.addWidget(thresholds)
        body.addWidget(scroll, 1)
        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addLayout(prog)
        layout.addLayout(body, 1)

        self.scan_btn.clicked.connect(lambda: self.gws.scan())
        self.run_btn.clicked.connect(self.run_classify)
        self.apply_btn.clicked.connect(self.apply)
        self.live_btn.clicked.connect(self.move_live)
        self.groups_btn.clicked.connect(self.open_groups)
        self.pause_btn.clicked.connect(self.toggle_pause)
        self.cancel_btn.clicked.connect(self.cancel)
        self.similar_distance.valueChanged.connect(
            lambda v: self.similar_distance_label.setText(str(v))
        )
        for w in (self.short_video, self.similar_window, self.blur, self.dark):
            w.valueChanged.connect(lambda _v: self.recompute())
        self.similar_distance.valueChanged.connect(lambda _v: self.recompute())
        self.move_aae.toggled.connect(lambda _c: self.recompute())
        for card in self.cards.values():
            card.enabled_box.toggled.connect(lambda _c: self.recompute())
        gws.opened.connect(self.on_opened)
        gws.closed.connect(self.on_opened)
        gws.job_progress.connect(self.on_progress)
        gws.job_finished.connect(self.on_finished)
        gws.job_failed.connect(self.on_failed)
        gws.thumb_loader.signals.ready.connect(self._thumb_ready)
        self._loading = False
        self.on_opened()

    # ------------------------------------------------------------------ config
    def config(self) -> ClassifyConfig:
        cfg = self.gws.catalog.classify_config() if self.gws.is_open else ClassifyConfig()
        cfg.short_video_seconds = float(self.short_video.value())
        cfg.similar_window_seconds = float(self.similar_window.value())
        cfg.similar_distance = int(self.similar_distance.value())
        cfg.blur_threshold = float(self.blur.value())
        cfg.dark_mean = float(self.dark.value())
        cfg.move_all_aae = self.move_aae.isChecked()
        cfg.enabled = {c: self.cards[c].enabled_box.isChecked() for c in CATEGORY_ORDER}
        return cfg

    def load_config(self, cfg: ClassifyConfig) -> None:
        self._loading = True
        self.short_video.setValue(cfg.short_video_seconds)
        self.similar_window.setValue(int(cfg.similar_window_seconds))
        self.similar_distance.setValue(cfg.similar_distance)
        self.similar_distance_label.setText(str(cfg.similar_distance))
        self.blur.setValue(cfg.blur_threshold)
        self.dark.setValue(cfg.dark_mean)
        self.move_aae.setChecked(cfg.move_all_aae)
        for c, card in self.cards.items():
            card.enabled_box.setChecked(cfg.is_enabled(c))
        self.workers.setValue(int(self.gws.prefs.get("workers", 0)))
        self.low_priority.setChecked(bool(self.gws.prefs.get("low_priority", False)))
        self._loading = False

    def on_opened(self) -> None:
        enabled = self.gws.is_open and not self.gws.read_only
        for b in (self.scan_btn, self.run_btn, self.apply_btn, self.live_btn):
            b.setEnabled(enabled)
        self.groups_btn.setEnabled(self.gws.is_open)
        if self.gws.is_open:
            self.load_config(self.gws.catalog.classify_config())
            self.refresh_plan()
            unfinished = self.gws.catalog.jobs(states=["interrupted"], limit=1)
            if unfinished and unfinished[0]["kind"] == "apply" and not self.gws.read_only:
                self.progress_label.setText(
                    tr("Resume unfinished job?")
                    + f"  (#{unfinished[0]['id']} apply → "
                    + tr("Apply")
                    + ")"
                )
        else:
            self.plan = None

    # ------------------------------------------------------------------ jobs
    def run_classify(self) -> None:
        if not self.gws.is_open:
            return
        self.gws.prefs.set("workers", int(self.workers.value()))
        self.gws.prefs.set("low_priority", self.low_priority.isChecked())
        thread = self.gws.classify(self.config())
        if thread is not None:
            self._job_started()

    def apply(self) -> None:
        ws = self.gws.ws
        if ws is None or self.gws.read_only or self.plan is None:
            return
        cfg = self.config()
        ws.catalog.save_classify_config(cfg)
        selected = [c for c in CATEGORY_ORDER if cfg.is_enabled(c)]
        enabled_plan = [c for c in self.plan.categories if c.category in selected]
        count = sum(c.count for c in enabled_plan)
        size = human_bytes(sum(c.bytes for c in enabled_plan))
        if count == 0:
            return
        if (
            QMessageBox.question(
                self, tr("Automatic"), f"{tr('would move')}: {count:,} ({size}) → {tr('Review')}?"
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self.gws.wait_actions()

        def fn(progress, control):
            return api.run_apply(
                ws, cfg, categories=selected, dry_run=False, progress=progress, control=control
            )

        if self.gws.run_job("apply", fn) is not None:
            self._job_started()

    def move_live(self) -> None:
        ws = self.gws.ws
        if ws is None or self.gws.read_only or self.plan is None:
            return
        n, size = self.plan.live_photos["count"], human_bytes(self.plan.live_photos["bytes"])
        if n == 0:
            return
        text = (
            tr("Live Photo videos: {n} ({size})", n=n, size=size)
            + "\n\n"
            + tr(
                "After this the photos become still images; the videos stay in Review until you decide."
            )
        )
        if QMessageBox.question(self, tr("Automatic"), text) != QMessageBox.StandardButton.Yes:
            return
        self.gws.wait_actions()

        def fn(progress, control):
            return api.run_live_photos(ws, dry_run=False, progress=progress, control=control)

        if self.gws.run_job("live_photos", fn) is not None:
            self._job_started()

    def _job_started(self) -> None:
        self.pause_btn.setEnabled(True)
        self.cancel_btn.setEnabled(True)
        self.pause_btn.setText(tr("Pause"))
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)

    def _current_job(self):
        for kind in ("classify", "apply", "live_photos", "scan", "undo", "purge"):
            job = self.gws.job(kind)
            if job is not None:
                return job
        return None

    def toggle_pause(self) -> None:
        job = self._current_job()
        if job is None:
            return
        if job.control.paused:
            job.control.resume()
            self.pause_btn.setText(tr("Pause"))
        else:
            job.control.pause()
            self.pause_btn.setText(tr("Resume"))

    def cancel(self) -> None:
        job = self._current_job()
        if job is not None:
            job.control.resume()
            job.control.cancel.set()

    def on_progress(self, kind: str, data: dict[str, Any]) -> None:
        stage = data.get("stage", "")
        if data.get("total"):
            self.progress.setRange(0, int(data["total"]))
            self.progress.setValue(int(data.get("done", 0)))
            self.progress_label.setText(
                f"{kind}: {stage}  {data.get('done', 0):,}/{data['total']:,}   {tr('ETA')} {format_eta(data.get('eta_seconds'))}"
            )
        else:
            self.progress.setRange(0, 0)
            self.progress_label.setText(f"{kind}: {stage}")

    def on_finished(self, kind: str, result: Any) -> None:
        self.progress.setVisible(False)
        self.pause_btn.setEnabled(False)
        self.cancel_btn.setEnabled(False)
        label = tr("Done") if result is not None else tr("Cancelled")
        if kind == "apply" and result is not None:
            label = tr(
                "Moved {items} item(s) ({size}) to Review.",
                items=result.items,
                size=human_bytes(result.bytes),
            )
        self.progress_label.setText(f"{kind}: {label}")
        self.refresh_plan()

    def on_failed(self, kind: str, message: str) -> None:
        self.progress.setVisible(False)
        self.pause_btn.setEnabled(False)
        self.cancel_btn.setEnabled(False)
        self.progress_label.setText(f"{kind}: {tr('Failed')} — {message}")

    # ------------------------------------------------------------------ report
    def recompute(self) -> None:
        """Thresholds changed: re-evaluate the rules from stored analysis (no filesystem access)."""
        if self._loading or not self.gws.is_open or self.gws.read_only:
            return
        ws = self.gws.ws
        assert ws is not None
        run_id = ws.catalog.latest_run_id()
        if run_id is None:
            return
        from framesift.engine.classify import classify

        cfg = self.config()
        ws.catalog.save_classify_config(cfg)
        classify(ws.catalog, ws.roots, cfg, run_id=run_id, compute=False)
        self.refresh_plan()

    def refresh_plan(self) -> None:
        if not self.gws.is_open:
            return
        self.plan = api.get_plan(self.gws.ws, self.config(), examples=12)
        for rep in self.plan.categories:
            card = self.cards[rep.category]
            was = card.enabled_box.blockSignals(True)
            card.set_report(rep)
            card.enabled_box.blockSignals(was)
            for ex in rep.examples:
                row = self.gws.catalog.one(
                    "SELECT f.*, m.format AS m_format, m.orientation AS m_orientation, m.preview_kind AS m_preview_kind, "
                    "m.preview_offset AS m_preview_offset, m.preview_length AS m_preview_length, m.duration_ms AS m_duration_ms "
                    "FROM files f LEFT JOIN file_meta m ON m.file_id=f.id WHERE f.id=?",
                    (ex.file_id,),
                )
                if row is None:
                    continue
                row = dict(row)
                data = self.gws.thumb_cache.get(
                    self.gws.catalog.uuid, ex.file_id, row["size"], row["mtime_ns"]
                )
                if data:
                    self._thumb_ready(ex.file_id, QImage.fromData(data, "JPEG"))
                else:
                    self.gws.thumb_loader.request(
                        self.gws.catalog, self.gws.roots, row, meta_for_thumbnail(row)
                    )
        p = self.plan
        top = "\n".join(
            f"  {v['rel_path']}  {human_bytes(v['size'])}  {v['codec'] or '?'} {v['width'] or '?'}×{v['height'] or '?'}"
            + (
                f"  (HEVC ≈ −{human_bytes(v['hevc_savings_estimate'])})"
                if v["hevc_savings_estimate"]
                else ""
            )
            for v in p.large_videos[:10]
        )
        self.report_only.setText(
            f"{tr('Report only')}:\n"
            + tr(
                "Live Photo videos: {n} ({size})",
                n=p.live_photos["count"],
                size=human_bytes(p.live_photos["bytes"]),
            )
            + "\n"
            + tr(
                "Original + edited pairs: {n} ({size})",
                n=p.edited_pairs["count"],
                size=human_bytes(p.edited_pairs["bytes"]),
            )
            + "\n"
            + f"{tr('Largest videos')}:\n{top}"
        )
        self.live_btn.setEnabled(p.live_photos["count"] > 0 and not self.gws.read_only)

    def _thumb_ready(self, file_id: int, image: QImage) -> None:
        for card in self.cards.values():
            lab = card.thumbs.get(file_id)
            if lab is not None:
                lab.setPixmap(
                    QPixmap.fromImage(image).scaled(
                        96,
                        96,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
