"""Analysis, suggestions and confirmed moves into the candidates folder."""

from __future__ import annotations

from dataclasses import replace
from functools import partial
from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
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
    QScrollArea,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from framesift.engine import api
from framesift.engine.classify import classify
from framesift.engine.config import CATEGORY_ORDER, ClassifyConfig
from framesift.engine.i18n import t
from framesift.engine.plan import Plan
from framesift.gui.i18n import tr
from framesift.gui.models.catalog_list import catalog_rows, meta_for_thumbnail
from framesift.gui.models.tree import human_bytes
from framesift.gui.ui import button, label, page
from framesift.gui.workers import ThumbKey, thumbnail_key
from framesift.gui.workspace import GuiWorkspace

EXAMPLES = 12
EXAMPLE_COLUMNS = 6
ANALYSIS_JOBS = {"classify", "apply", "scan", "live_photos"}


def _plan_payload(ws: api.Workspace, config: ClassifyConfig | None) -> tuple:
    cfg = config or ws.config()
    plan = api.get_plan(ws, cfg, examples=EXAMPLES)
    ids = [ex.file_id for category in plan.categories for ex in category.examples]
    apply_jobs = ws.catalog.jobs(kind="apply", limit=1)
    interrupted = bool(apply_jobs and apply_jobs[0]["state"] == "interrupted")
    return cfg, plan, catalog_rows(ws.catalog, ids), interrupted


def _update_rules(ws: api.Workspace, cfg: ClassifyConfig, *, progress, control) -> bool:
    control.check()
    run_id = ws.catalog.latest_run_id()
    if run_id is not None:
        classify(ws.catalog, ws.roots, cfg, run_id=run_id, compute=False, cancel=control.cancel)
    control.check()
    ws.catalog.save_classify_config(cfg)
    return True


class CategoryCard(QGroupBox):
    def __init__(self, category: str, parent=None):
        super().__init__(parent)
        self.category = category
        self.setProperty("role", "section")
        self.setTitle(t(f"category.{category}"))
        self.enabled_box = QCheckBox(tr("Select for review"), self)
        self.enabled_box.setAccessibleName(
            f"{t(f'category.{category}')} · {tr('Select for review')}"
        )
        self.enabled_box.setChecked(True)
        self.count_label = label("0", "section")
        self.summary = label("", "caption")
        top = QHBoxLayout()
        top.addWidget(self.count_label)
        top.addWidget(self.summary, 1)
        top.addWidget(self.enabled_box)

        self.examples = QWidget(self)
        self.grid = QGridLayout(self.examples)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(6)
        self.grid.setColumnStretch(EXAMPLE_COLUMNS, 1)
        self._labels: list[QLabel] = []
        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.addLayout(top)
        layout.addWidget(self.examples)
        self.thumbs: dict[int, QLabel] = {}

    def set_report(self, report: Any) -> None:
        self.count_label.setText(f"{report.count:,}")
        self.summary.setText(
            f"{human_bytes(report.bytes)}  ·  {t(f'confidence.{report.confidence}')}"
        )
        for i in range(len(self._labels), min(len(report.examples), EXAMPLES)):
            image = QLabel(self.examples)
            image.setFixedSize(75, 58)
            image.setAlignment(Qt.AlignmentFlag.AlignCenter)
            image.setStyleSheet("background: palette(midlight); border-radius: 5px;")
            self.grid.addWidget(image, i // EXAMPLE_COLUMNS, i % EXAMPLE_COLUMNS)
            self._labels.append(image)
        self.thumbs.clear()
        for image in self._labels:
            image.clear()
            image.hide()
        for image, ex in zip(self._labels, report.examples, strict=False):
            image.setToolTip(ex.rel_path + "\n" + ex.reason)
            image.setAccessibleName(ex.rel_path)
            image.show()
            self.thumbs[ex.file_id] = image
        self.examples.setVisible(bool(self.thumbs))


class AutoView(QWidget):
    open_groups = Signal()

    def __init__(self, gws: GuiWorkspace, parent=None):
        super().__init__(parent)
        self.gws = gws
        self.plan: Plan | None = None
        self.cards: dict[str, CategoryCard] = {}
        self._active = self._loading = self._edited = self._initialized = False
        self._config = ClassifyConfig()
        self._example_keys: dict[int, ThumbKey] = {}
        self._rules_timer = QTimer(self, singleShot=True, interval=250)
        self._rules_timer.timeout.connect(self._recompute_now)

        self.scan_btn = button("Rescan folder", variant="quiet")
        self.run_btn = button("Run analysis", variant="primary")
        self.apply_btn = button("Move selected", variant="primary")
        self.live_btn = button("Move Live Photo videos…", variant="quiet")
        self.groups_btn = button("Compare groups", variant="quiet")
        self.pause_btn = button("Pause", variant="quiet")
        self.cancel_btn = button("Cancel", variant="quiet")
        self.options_btn = button("Analysis options", variant="quiet")
        self.options_btn.setCheckable(True)
        self.progress_label = label("", "context", wrap=True)
        self.empty = label(
            tr("Run an analysis to find photos and videos worth reviewing."), "notice", wrap=True
        )
        self.options = self._make_options()

        cards_widget = QWidget(self)
        cards_widget.setObjectName("scrollContent")
        self.cards_layout = QVBoxLayout(cards_widget)
        self.cards_layout.setContentsMargins(0, 0, 8, 5)
        self.cards_layout.setSpacing(10)
        self.cards_layout.addWidget(self.empty)
        self.cards_layout.addWidget(self.options)
        self.options.hide()
        for category in CATEGORY_ORDER:
            card = CategoryCard(category, cards_widget)
            self.cards[category] = card
            self.cards_layout.addWidget(card)
            card.hide()
        self.report_only = label("", "context", wrap=True)
        self.cards_layout.addWidget(self.report_only)
        self.videos_btn = button("Largest videos", variant="quiet")
        self.videos_btn.setCheckable(True)
        self.videos = label("", "caption", wrap=True)
        self.videos.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.videos_btn.toggled.connect(self.videos.setVisible)
        self.cards_layout.addWidget(self.videos_btn, 0, Qt.AlignmentFlag.AlignLeft)
        self.cards_layout.addWidget(self.videos)
        self.cards_layout.addWidget(self.live_btn, 0, Qt.AlignmentFlag.AlignLeft)
        self.cards_layout.addStretch(1)
        report = QScrollArea(self)
        report.setProperty("role", "flat")
        report.setWidgetResizable(True)
        report.setWidget(cards_widget)

        header = QHBoxLayout()
        header.addWidget(label(tr("Analysis"), "title"), 1)
        header.addWidget(self.options_btn)
        header.addWidget(self.run_btn)
        footer = QHBoxLayout()
        footer.addWidget(self.scan_btn)
        footer.addWidget(self.pause_btn)
        footer.addWidget(self.cancel_btn)
        footer.addStretch(1)
        footer.addWidget(self.groups_btn)
        footer.addWidget(self.apply_btn)
        layout = page(self)
        layout.addLayout(header)
        layout.addWidget(self.progress_label)
        layout.addWidget(report, 1)
        layout.addLayout(footer)

        self.scan_btn.clicked.connect(lambda: self.gws.scan())
        self.run_btn.clicked.connect(self.run_classify)
        self.apply_btn.clicked.connect(self.apply)
        self.live_btn.clicked.connect(self.move_live)
        self.groups_btn.clicked.connect(self.open_groups)
        self.pause_btn.clicked.connect(self.toggle_pause)
        self.cancel_btn.clicked.connect(self.cancel)
        self.options_btn.toggled.connect(self.options.setVisible)
        for w in (
            self.short_video,
            self.similar_window,
            self.blur,
            self.dark,
            self.similar_distance,
        ):
            w.valueChanged.connect(self.recompute)
        self.move_aae.toggled.connect(self.recompute)
        for card in self.cards.values():
            card.enabled_box.toggled.connect(self.recompute)
        gws.closed.connect(self._reset)
        gws.state_changed.connect(self._update_buttons)
        gws.job_progress.connect(self.on_progress)
        gws.job_finished.connect(self.on_finished)
        gws.job_failed.connect(self.on_failed)
        gws.action_done.connect(self._refresh_after_action)
        gws.thumb_loader.signals.ready.connect(self._thumb_ready)
        self._update_buttons()

    def _make_options(self) -> QGroupBox:
        self.short_video = QDoubleSpinBox(self)
        self.short_video.setRange(0.5, 60.0)
        self.short_video.setSingleStep(0.5)
        self.similar_window = QSpinBox(self)
        self.similar_window.setRange(1, 3600)
        self.similar_distance = QSlider(Qt.Orientation.Horizontal, self)
        self.similar_distance.setRange(0, 20)
        self.similar_distance_label = label("", "caption")
        self.similar_distance.valueChanged.connect(
            lambda value: self.similar_distance_label.setText(str(value))
        )
        self.blur = QDoubleSpinBox(self)
        self.blur.setRange(0, 500)
        self.dark = QDoubleSpinBox(self)
        self.dark.setRange(0, 0.5)
        self.dark.setSingleStep(0.01)
        self.move_aae = QCheckBox(tr("Move all .AAE files to junk"), self)
        self.low_priority = QCheckBox(tr("Low priority"), self)
        self.workers = QSpinBox(self)
        self.workers.setRange(0, 64)
        self.workers.setSpecialValueText(tr("Automatic workers"))
        form = QFormLayout()
        form.setVerticalSpacing(8)
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
        options = QGroupBox(tr("Analysis options"), self)
        options.setProperty("role", "section")
        options.setLayout(form)
        return options

    def config(self) -> ClassifyConfig:
        return replace(
            self._config,
            short_video_seconds=float(self.short_video.value()),
            similar_window_seconds=float(self.similar_window.value()),
            similar_distance=int(self.similar_distance.value()),
            blur_threshold=float(self.blur.value()),
            dark_mean=float(self.dark.value()),
            move_all_aae=self.move_aae.isChecked(),
            enabled={key: card.enabled_box.isChecked() for key, card in self.cards.items()},
        )

    def load_config(self, cfg: ClassifyConfig) -> None:
        self._loading = True
        self._config = cfg
        self.short_video.setValue(cfg.short_video_seconds)
        self.similar_window.setValue(int(cfg.similar_window_seconds))
        self.similar_distance.setValue(cfg.similar_distance)
        self.similar_distance_label.setText(str(cfg.similar_distance))
        self.blur.setValue(cfg.blur_threshold)
        self.dark.setValue(cfg.dark_mean)
        self.move_aae.setChecked(cfg.move_all_aae)
        for key, card in self.cards.items():
            card.enabled_box.setChecked(cfg.is_enabled(key))
        self.workers.setValue(int(self.gws.prefs.get("workers", 0)))
        self.low_priority.setChecked(bool(self.gws.prefs.get("low_priority", False)))
        self._loading = False

    def _reset(self) -> None:
        self._rules_timer.stop()
        self._edited = self._initialized = self._loading = False
        self.plan = None
        self._example_keys.clear()
        self.progress_label.clear()
        self.report_only.clear()
        self.videos.clear()
        self.empty.show()
        for card in self.cards.values():
            card.hide()
        self._update_buttons()

    def activate(self) -> None:
        self._active = True
        if self._edited:
            self._rules_timer.start()
        else:
            self.refresh_plan()

    def deactivate(self) -> None:
        self._active = False
        self._rules_timer.stop()
        self._loading = False
        self.gws.cancel_requests("analysis.")
        self.gws.thumb_loader.discard_waiting("analysis")

    def refresh_plan(self) -> None:
        if not self._active or self.gws.ws is None or self._edited:
            return
        self._loading = True
        self.gws.request(
            "analysis.plan",
            partial(_plan_payload, self.gws.ws, self.config() if self._initialized else None),
            self._set_plan,
            self._plan_failed,
        )
        self._update_buttons()

    def _set_plan(self, payload: tuple) -> None:
        self._loading = False
        cfg, self.plan, rows, interrupted = payload
        if not self._initialized:
            self.load_config(cfg)
            self._initialized = True
            if interrupted and not self.gws.read_only:
                self.progress_label.setText(tr("Resume unfinished job?"))
        have_results = self.plan.run_id is not None
        self.empty.setVisible(not have_results)
        self._example_keys.clear()
        for report in self.plan.categories:
            card = self.cards[report.category]
            card.setVisible(have_results)
            card.set_report(report)
        for row in rows.values():
            self._example_keys[row["id"]] = thumbnail_key(row)
            self.gws.thumb_loader.request(
                self.gws.catalog_uuid,
                self.gws.roots,
                row,
                meta_for_thumbnail(row),
                scope="analysis",
            )
        n_live = self.plan.live_photos["count"]
        n_pairs = self.plan.edited_pairs["count"]
        self.report_only.setText(
            tr(
                "Live Photo videos: {n} ({size})",
                n=n_live,
                size=human_bytes(self.plan.live_photos["bytes"]),
            )
            + "   ·   "
            + tr(
                "Original + edited pairs: {n} ({size})",
                n=n_pairs,
                size=human_bytes(self.plan.edited_pairs["bytes"]),
            )
        )
        self.report_only.setVisible(have_results or bool(n_live or n_pairs))
        lines = []
        for video in self.plan.large_videos:
            estimate = video["hevc_savings_estimate"]
            lines.append(
                f"{video['rel_path']}  ·  {human_bytes(video['size'])}  ·  "
                f"{video['codec'] or '?'}  ·  {video['width'] or '?'} × {video['height'] or '?'}"
                + (f"  ·  HEVC ≈ −{human_bytes(estimate)}" if estimate else "")
            )
        self.videos.setText("\n".join(lines))
        self._update_buttons()

    def _plan_failed(self, message: str) -> None:
        self._loading = self._initialized = False
        self.plan = None
        self.progress_label.setText(message)
        self._update_buttons()

    def _thumb_ready(self, key: ThumbKey, image: QImage) -> None:
        fid = key[0]
        if not self._active or self._example_keys.get(fid) != key:
            return
        for card in self.cards.values():
            target = card.thumbs.get(fid)
            if target is not None:
                target.setPixmap(
                    QPixmap.fromImage(image).scaled(
                        target.size(),
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )

    def recompute(self, *_args: Any) -> None:
        if self._loading or not self._initialized or not self.gws.is_open or self.gws.read_only:
            return
        self._edited = True
        self._rules_timer.start()
        self._update_buttons()

    def _recompute_now(self) -> None:
        ws = self.gws.ws
        if self._edited and self._active and ws is not None and self.gws.can_write:
            self._edited = False
            if self.gws.run_job("recompute", partial(_update_rules, ws, self.config())) is None:
                self._edited = True

    def run_classify(self) -> None:
        if not self.gws.can_write or not self._initialized:
            return
        self.gws.prefs.set("workers", int(self.workers.value()))
        self.gws.prefs.set("low_priority", self.low_priority.isChecked())
        self._rules_timer.stop()
        self._edited = False
        if self.gws.classify(self.config()) is not None:
            self.progress_label.setText(tr("Analyzing media"))

    def apply(self) -> None:
        ws, plan = self.gws.ws, self.plan
        if ws is None or plan is None or not self.apply_btn.isEnabled():
            return
        cfg = self.config()
        selected = [key for key in CATEGORY_ORDER if cfg.is_enabled(key)]
        reports = [report for report in plan.categories if report.category in selected]
        count = sum(report.count for report in reports)
        if not count:
            return
        size = human_bytes(sum(report.bytes for report in reports))
        if (
            QMessageBox.question(
                self,
                tr("Analysis"),
                tr("Move {n} items ({size}) to Candidates?", n=f"{count:,}", size=size),
            )
            == QMessageBox.StandardButton.Yes
            and self.gws.ws is ws
        ):
            self.gws.run_job(
                "apply", partial(api.run_apply, ws, cfg, categories=selected, dry_run=False)
            )

    def move_live(self) -> None:
        ws, plan = self.gws.ws, self.plan
        if ws is None or plan is None or not self.gws.can_write:
            return
        n = plan.live_photos["count"]
        if not n:
            return
        text = tr(
            "Live Photo videos: {n} ({size})", n=n, size=human_bytes(plan.live_photos["bytes"])
        )
        text += "\n\n" + tr(
            "After this the photos become still images; the videos stay in Review until you decide."
        )
        if (
            QMessageBox.question(self, tr("Analysis"), text) == QMessageBox.StandardButton.Yes
            and self.gws.ws is ws
        ):
            self.gws.run_job("live_photos", partial(api.run_live_photos, ws, dry_run=False))

    def _current_job(self):
        return next(
            (job for kind in ANALYSIS_JOBS if (job := self.gws.job(kind)) is not None), None
        )

    def toggle_pause(self) -> None:
        job = self._current_job()
        if job is not None:
            if job.control.paused:
                job.control.resume()
            else:
                job.control.pause()
        self._update_buttons()

    def cancel(self) -> None:
        job = self._current_job()
        if job is not None:
            job.control.cancel.set()
            job.control.resume()

    def _update_buttons(self) -> None:
        writable = self.gws.can_write and self._initialized and not self._loading
        plan = self.plan
        self.run_btn.setEnabled(writable)
        self.scan_btn.setEnabled(self.gws.can_write)
        self.apply_btn.setEnabled(
            bool(
                writable
                and not self._edited
                and plan
                and plan.run_id is not None
                and any(
                    report.count and self.cards[report.category].enabled_box.isChecked()
                    for report in plan.categories
                )
            )
        )
        self.live_btn.setVisible(bool(plan and plan.live_photos["count"]))
        self.videos_btn.setVisible(bool(plan and plan.large_videos))
        self.videos.setVisible(self.videos_btn.isChecked() and bool(plan and plan.large_videos))
        self.live_btn.setEnabled(writable)
        self.groups_btn.setEnabled(bool(plan and plan.run_id is not None and not self._edited))
        self.options_btn.setEnabled(self._initialized)
        self.options.setEnabled(not self.gws.read_only and not self.gws.busy and not self._loading)
        for card in self.cards.values():
            card.enabled_box.setEnabled(writable)
        job = self._current_job()
        self.pause_btn.setVisible(job is not None and self.gws.job("scan") is None)
        self.cancel_btn.setVisible(job is not None)
        self.pause_btn.setText(tr("Resume") if job and job.control.paused else tr("Pause"))
        if (
            self._active
            and self._edited
            and self.gws.can_write
            and not self._rules_timer.isActive()
        ):
            self._rules_timer.start()

    def on_progress(self, kind: str, _data: dict) -> None:
        if kind == "recompute":
            self.progress_label.setText(tr("Updating suggestions"))

    def on_finished(self, kind: str, result: Any) -> None:
        if kind == "apply" and result is not None:
            self.progress_label.setText(
                tr(
                    "Moved {items} item(s) ({size}) to Review.",
                    items=result.items,
                    size=human_bytes(result.bytes),
                )
            )
        elif kind == "recompute":
            self.progress_label.clear()
        elif kind in ANALYSIS_JOBS:
            self.progress_label.setText(tr("Done") if result is not None else tr("Cancelled"))
        self.refresh_plan()

    def on_failed(self, _kind: str, message: str) -> None:
        self.progress_label.setText(message)
        self.refresh_plan()

    def _refresh_after_action(self, _tag: Any, _payload: Any) -> None:
        if self._active:
            self.refresh_plan()
