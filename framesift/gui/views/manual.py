"""Manual review mode: one item full screen, keyboard driven, with prefetch and multi-level undo."""

from __future__ import annotations

import json
import sys
from collections import OrderedDict
from typing import Any

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QImage, QKeyEvent
from PySide6.QtWidgets import (
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from framesift.engine.i18n import reason_text, t
from framesift.gui.i18n import tr
from framesift.gui.media.image_view import ImageView
from framesift.gui.media.video_view import VideoView
from framesift.gui.models.catalog_list import ListQuery
from framesift.gui.models.tree import human_bytes
from framesift.gui.workspace import GuiWorkspace

FIT_SIDE = 2560
PREFETCH = 5
IMAGE_BUDGET = 12
IS_MAC = sys.platform == "darwin"


class InfoPanel(QScrollArea):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setMinimumWidth(260)
        self.setMaximumWidth(360)
        inner = QWidget()
        self.form = QFormLayout(inner)
        self.form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.setWidget(inner)
        self.labels: dict[str, QLabel] = {}
        for key in (
            "Name",
            "Path",
            "Original location",
            "Date taken",
            "Size",
            "Resolution",
            "Camera",
            "Duration",
            "Codec",
            "Category",
            "Reason",
            "Confidence",
            "Live Photo",
            "Reviewed",
        ):
            lab = QLabel("")
            lab.setWordWrap(True)
            lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.labels[key] = lab
            self.form.addRow(tr(key) + ":", lab)

    def set_item(
        self,
        row: dict[str, Any],
        meta: dict[str, Any],
        candidate: dict[str, Any] | None,
        edited_of_name: str | None,
    ) -> None:
        L = self.labels
        L["Name"].setText(
            row["name"]
            + (
                f"  ({tr('Edited version of {name}', name=edited_of_name)})"
                if edited_of_name
                else ""
            )
        )
        L["Path"].setText(f"{row['root']}: {row['rel_path']}")
        L["Original location"].setText(row.get("origin_rel_path") or "")
        L["Date taken"].setText(
            (meta.get("date_taken") or "").replace("T", " ")
            + (f"  [{meta.get('date_source')}]" if meta.get("date_source") else "")
        )
        L["Size"].setText(human_bytes(row["size"]))
        w, h = meta.get("width"), meta.get("height")
        L["Resolution"].setText(f"{w}×{h}" if w and h else "")
        L["Camera"].setText(" ".join(x for x in (meta.get("make"), meta.get("model")) if x))
        dur = meta.get("duration_ms")
        L["Duration"].setText(f"{dur / 1000:.1f} s" if dur else "")
        L["Codec"].setText(meta.get("codec") or "")
        if candidate:
            L["Category"].setText(t(f"category.{candidate['category']}"))
            L["Reason"].setText(
                reason_text(
                    candidate["reason_key"], json.loads(candidate.get("reason_params") or "{}")
                )
            )
            L["Confidence"].setText(t(f"confidence.{candidate['confidence']}"))
        else:
            L["Category"].setText("")
            L["Reason"].setText("")
            L["Confidence"].setText("")
        L["Live Photo"].setText("✓" if row.get("live_count") else "")
        L["Reviewed"].setText("✓" if row.get("reviewed_at") else "")


class ReviewView(QWidget):
    closed = Signal()

    def __init__(self, gws: GuiWorkspace, parent=None):
        super().__init__(parent)
        self.gws = gws
        self.ids: list[int] = []
        self.pos = 0
        self.query = ListQuery()
        self.undo_stack: list[tuple[int, int]] = []
        self.images: OrderedDict[int, tuple[QImage, bool]] = OrderedDict()
        self.current_row: dict[str, Any] | None = None
        self.live_playing = False
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self.image_view = ImageView(self)
        self.video_view = VideoView(self)
        self.message = QLabel(self)
        self.message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.message.setStyleSheet("background: black; color: #ccc; font-size: 16px;")
        self.stack = QStackedWidget(self)
        self.stack.addWidget(self.image_view)
        self.stack.addWidget(self.video_view)
        self.stack.addWidget(self.message)
        self.stack.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.info = InfoPanel(self)

        self.buttons: dict[str, QPushButton] = {}
        bar = QHBoxLayout()
        undo_label = "Undo (⌘Z)" if IS_MAC else "Undo (Ctrl+Z)"
        for key, label in (
            ("keep", "Keep (→)"),
            ("delete", "Delete (←)"),
            ("skip", "Skip (↓)"),
            ("undo", undo_label),
            ("play", "Play/Pause (Space)"),
            ("mute", "Mute (M)"),
            ("zoom", "Zoom (Z)"),
            ("live", "Live (hold L)"),
            ("info", "Info (I)"),
            ("back", "Back (Esc)"),
        ):
            btn = QPushButton(tr(label), self)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self.buttons[key] = btn
            bar.addWidget(btn)
        self.status_label = QLabel(self)
        bar.addStretch(1)
        bar.addWidget(self.status_label)
        self.buttons["keep"].clicked.connect(self.act_keep)
        self.buttons["delete"].clicked.connect(self.act_delete)
        self.buttons["skip"].clicked.connect(self.act_skip)
        self.buttons["undo"].clicked.connect(self.act_undo)
        self.buttons["play"].clicked.connect(self.video_view.toggle_pause)
        self.buttons["mute"].clicked.connect(self.video_view.toggle_mute)
        self.buttons["zoom"].clicked.connect(self.image_view.toggle_zoom)
        self.buttons["live"].pressed.connect(self.live_start)
        self.buttons["live"].released.connect(self.live_stop)
        self.buttons["info"].clicked.connect(self.toggle_info)
        self.buttons["back"].clicked.connect(self.leave)

        centre = QHBoxLayout()
        centre.setContentsMargins(0, 0, 0, 0)
        centre.addWidget(self.stack, 1)
        centre.addWidget(self.info)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(centre, 1)
        layout.addLayout(bar)

        self.image_view.need_full_resolution.connect(self._request_full)
        gws.image_loader.signals.ready.connect(self._image_ready)
        gws.image_loader.signals.failed.connect(self._image_failed)
        gws.action_done.connect(lambda *_a: self._update_status())
        self.video_view.fallback_used.connect(
            lambda _u: self.message.setText(
                tr("Video cannot be played by Qt; showing frames via ffmpeg (no audio).")
            )
        )

    # ------------------------------------------------------------------ session
    def start(self, ids: list[int], position: int, query: ListQuery) -> None:
        self.ids = list(ids)
        self.query = query
        self.undo_stack = []
        self.images.clear()
        self.pos = min(max(position, 0), max(len(self.ids) - 1, 0))
        self.show_current()
        self.setFocus()

    def leave(self) -> None:
        self.video_view.stop()
        self.closed.emit()

    @property
    def current_id(self) -> int | None:
        return self.ids[self.pos] if 0 <= self.pos < len(self.ids) else None

    def show_current(self) -> None:
        self.video_view.stop()
        self.live_playing = False
        fid = self.current_id
        if fid is None:
            self.current_row = None
            self.stack.setCurrentWidget(self.message)
            self.message.setText(
                tr("All items reviewed. Enable “Show reviewed” to see them again.")
                if self.query.root == "source" and not self.query.show_reviewed
                else tr("Nothing to review here.")
            )
            self._update_status()
            return
        row = self._row(fid)
        if row is None:
            del self.ids[self.pos]
            self.show_current()
            return
        self.current_row = row
        meta = self.gws.meta(fid)
        edited_name = None
        if row.get("edited_of"):
            orig = self.gws.file_row(int(row["edited_of"]))
            edited_name = orig["name"] if orig else None
        self.info.set_item(row, meta, self.gws.candidate_for(fid), edited_name)
        path = self.gws.abs_path(row)
        if row["kind"] == "video":
            self.stack.setCurrentWidget(self.video_view)
            self.video_view.play(path, width=meta.get("width") or 0, height=meta.get("height") or 0)
        else:
            self.stack.setCurrentWidget(self.image_view)
            cached = self.images.get(fid)
            if cached is not None:
                self.image_view.set_image(
                    cached[0], full=cached[1], native_size=self._native(meta, cached[0])
                )
            else:
                self._show_placeholder(row)
                self._request(fid, row, meta, full=False)
        self._prefetch()
        self._update_status()

    def _row(self, fid: int) -> dict[str, Any] | None:
        row = self.gws.catalog.one(
            "SELECT f.*, (SELECT COUNT(*) FROM files c WHERE c.item_id=f.id AND c.companion_role='live_video' AND c.status='present') AS live_count "
            "FROM files f WHERE f.id=? AND f.status='present'",
            (fid,),
        )
        return dict(row) if row else None

    def _native(self, meta: dict[str, Any], img: QImage) -> tuple[int, int]:
        w, h = meta.get("width"), meta.get("height")
        if w and h:
            if meta.get("orientation") in (5, 6, 7, 8):
                w, h = h, w
            return int(w), int(h)
        return img.width(), img.height()

    def _show_placeholder(self, row: dict[str, Any]) -> None:
        data = self.gws.thumb_cache.get(
            self.gws.catalog.uuid, int(row["id"]), row["size"], row["mtime_ns"]
        )
        if data:
            img = QImage.fromData(data, "JPEG")
            if not img.isNull():
                self.image_view.set_image(img, full=False, native_size=(img.width(), img.height()))
                return
        self.image_view.clear()

    def _request(self, fid: int, row: dict[str, Any], meta: dict[str, Any], *, full: bool) -> None:
        self.gws.image_loader.request(
            fid,
            self.gws.abs_path(row),
            meta.get("format"),
            row["ext"],
            meta.get("orientation"),
            max_side=None if full else FIT_SIDE,
        )

    def _request_full(self) -> None:
        fid = self.current_id
        if fid is None or self.current_row is None:
            return
        self._request(fid, self.current_row, self.gws.meta(fid), full=True)

    def _prefetch(self) -> None:
        window = [
            self.ids[i]
            for i in range(max(0, self.pos - PREFETCH), min(len(self.ids), self.pos + PREFETCH + 1))
        ]
        for fid in list(self.images):
            if fid not in window:
                del self.images[fid]
        for fid in window:
            if fid in self.images:
                continue
            row = self._row(fid)
            if row is None or row["kind"] == "video":
                continue
            self._request(fid, row, self.gws.meta(fid), full=False)

    def _image_ready(self, fid: int, img: QImage, full: bool) -> None:
        window = set(self.ids[max(0, self.pos - PREFETCH) : self.pos + PREFETCH + 1])
        if fid in window or fid == self.current_id:
            existing = self.images.get(fid)
            if existing is None or full or not existing[1]:
                self.images[fid] = (img, full)
                while len(self.images) > IMAGE_BUDGET:
                    self.images.popitem(last=False)
        if fid == self.current_id and self.current_row and self.current_row["kind"] != "video":
            self.image_view.set_image(
                img, full=full, native_size=self._native(self.gws.meta(fid), img)
            )

    def _image_failed(self, fid: int, message: str) -> None:
        if fid == self.current_id:
            self.stack.setCurrentWidget(self.message)
            self.message.setText(tr("Cannot decode this file.") + f"\n{message}")

    # ------------------------------------------------------------------ actions
    def _advance_after_removal(self) -> None:
        if self.pos >= len(self.ids):
            self.pos = max(len(self.ids) - 1, 0) if self.ids else 0
        self.show_current()

    def act_keep(self) -> None:
        fid = self.current_id
        if fid is None or self.gws.read_only:
            return
        self.gws.keep(fid)
        self.undo_stack.append((self.pos, fid))
        if self.query.root == "source" and self.query.show_reviewed:
            self.pos += 1
            if self.pos >= len(self.ids):
                self.pos = len(self.ids)
            self.show_current()
        else:
            del self.ids[self.pos]
            self._advance_after_removal()

    def act_delete(self) -> None:
        fid = self.current_id
        if fid is None or self.gws.read_only:
            return
        if self.query.root == "delete":
            self.act_skip()
            return
        self.gws.to_delete(fid)
        self.undo_stack.append((self.pos, fid))
        del self.ids[self.pos]
        self._advance_after_removal()

    def act_skip(self) -> None:
        if not self.ids:
            return
        self.pos = min(self.pos + 1, len(self.ids))
        self.show_current()

    def act_undo(self) -> None:
        if not self.undo_stack or self.gws.read_only:
            return
        pos, fid = self.undo_stack.pop()
        self.gws.undo_last()
        if fid not in self.ids:
            self.ids.insert(min(pos, len(self.ids)), fid)
        self.pos = min(pos, len(self.ids) - 1)
        self.gws.wait_actions(5000)
        self.show_current()

    def live_start(self) -> None:
        row = self.current_row
        if not row or not row.get("live_count") or self.live_playing:
            return
        comp = self.gws.catalog.one(
            "SELECT * FROM files WHERE item_id=? AND companion_role='live_video' AND status='present'",
            (row["id"],),
        )
        if comp is None:
            return
        self.live_playing = True
        meta = self.gws.meta(int(comp["id"]))
        self.stack.setCurrentWidget(self.video_view)
        self.video_view.play(
            self.gws.abs_path(dict(comp)),
            width=meta.get("width") or 0,
            height=meta.get("height") or 0,
            loop=True,
        )

    def live_stop(self) -> None:
        if not self.live_playing:
            return
        self.live_playing = False
        self.video_view.stop()
        self.stack.setCurrentWidget(self.image_view)

    def toggle_info(self) -> None:
        self.info.setVisible(not self.info.isVisible())

    def _update_status(self) -> None:
        c = (
            self.gws.session_counters()
            if self.gws.is_open
            else {"kept": 0, "to_delete": 0, "bytes_to_delete": 0}
        )
        position = (
            f"{self.pos + 1} / {len(self.ids)}"
            if self.ids and self.pos < len(self.ids)
            else f"{len(self.ids)} / {len(self.ids)}"
        )
        self.status_label.setText(
            f"{position}   ·   {c['kept']} {tr('kept')}   ·   {c['to_delete']} {tr('to delete')}   ·   {tr('to free')} {human_bytes(c['bytes_to_delete'])}"
        )

    # ------------------------------------------------------------------ keys
    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        key = event.key()
        mods = event.modifiers()
        ctrl = mods & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier)
        if key == Qt.Key.Key_Right:
            self.act_keep()
        elif key == Qt.Key.Key_Left:
            self.act_delete()
        elif key == Qt.Key.Key_Down:
            self.act_skip()
        elif key == Qt.Key.Key_Z and ctrl:
            self.act_undo()
        elif key == Qt.Key.Key_Z:
            self.image_view.toggle_zoom()
        elif key == Qt.Key.Key_Space:
            self.video_view.toggle_pause()
        elif key == Qt.Key.Key_M:
            self.video_view.toggle_mute()
        elif key == Qt.Key.Key_L and not event.isAutoRepeat():
            self.live_start()
        elif key == Qt.Key.Key_I:
            self.toggle_info()
        elif key == Qt.Key.Key_Escape:
            self.leave()
        else:
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_L and not event.isAutoRepeat():
            self.live_stop()
        else:
            super().keyReleaseEvent(event)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(1100, 700)
