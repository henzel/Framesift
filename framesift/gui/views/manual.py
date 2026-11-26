"""Manual review mode: one item full screen, keyboard driven, with prefetch and multi-level undo."""

from __future__ import annotations

import json
import sys
from collections import OrderedDict
from functools import partial
from typing import Any

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QImage, QKeyEvent, QPixmap
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

from framesift.engine import actions
from framesift.engine.catalog import Catalog
from framesift.engine.i18n import reason_text, t
from framesift.engine.journal import undo_ops
from framesift.gui.i18n import tr
from framesift.gui.media.image_view import ImageView
from framesift.gui.media.video_view import VideoView
from framesift.gui.models.catalog_list import ListQuery, catalog_rows
from framesift.gui.models.tree import human_bytes
from framesift.gui.ui import button, label, page
from framesift.gui.workspace import GuiWorkspace, workspace_counters

FIT_SIDE = 1920
LOOK_BEHIND = 1
LOOK_AHEAD = 4
IMAGE_BUDGET = 56 * 1024 * 1024
IS_MAC = sys.platform == "darwin"


class InfoPanel(QScrollArea):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setProperty("role", "flat")
        self.setFixedWidth(246)
        inner = QWidget()
        inner.setObjectName("inspector")
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.addWidget(label(tr("File details"), "section"))
        self.form = QFormLayout()
        self.form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        layout.addLayout(self.form)
        layout.addStretch(1)
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
            lab = label(wrap=True)
            lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.labels[key] = lab
            self.form.addRow(label(tr(key), "caption"), lab)

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
        location = tr(
            {"source": "Source", "review": "Candidates", "delete": "To delete"}[row["root"]]
        )
        L["Path"].setText(f"{location} / {row['rel_path']}")
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
        for value in L.values():
            self.form.setRowVisible(value, bool(value.text()))


def _review_rows(catalog: Catalog, ids: list[int], query: ListQuery) -> dict[int, dict[str, Any]]:
    return {
        fid: row
        for fid, row in catalog_rows(catalog, ids).items()
        if row["status"] == "present"
        and row["root"] == query.root
        and (query.show_reviewed or not row["reviewed_at"])
    }


def _details(catalog: Catalog, fid: int, edited_of: int | None) -> tuple[dict | None, str | None]:
    candidate = catalog.one(
        "SELECT * FROM candidates WHERE file_id=? ORDER BY id DESC LIMIT 1", (fid,)
    )
    original = catalog.file(edited_of) if edited_of is not None else None
    return dict(candidate) if candidate else None, original["name"] if original else None


def _live_video(catalog: Catalog, fid: int) -> dict | None:
    row = catalog.one(
        "SELECT id FROM files WHERE item_id=? AND companion_role='live_video' AND status='present'",
        (fid,),
    )
    if row is not None:
        return catalog_rows(catalog, [row["id"]]).get(row["id"])
    return None


def _review_action(ws, action: str, fid: int, operation: str | None = None) -> tuple:
    if operation is not None:
        result = undo_ops(ws.catalog, ws.roots, [operation], session_id=ws.session_id)
    else:
        method = actions.keep if action == "keep" else actions.to_delete
        result = method(ws.catalog, ws.roots, fid, ws.session_id)
    present = ws.catalog.file(fid)
    # Use the resulting catalog location even when a companion move encountered an error.
    return result, dict(present) if present else None


def _image_cost(image: QImage | QPixmap) -> int:
    return image.width() * image.height() * 4


class ReviewView(QWidget):
    closed = Signal()

    def __init__(self, gws: GuiWorkspace, parent=None):
        super().__init__(parent)
        self.gws = gws
        self.ids: list[int] = []
        self.pos = 0
        self.query = ListQuery()
        self.undo_stack: list[tuple[int, int, str]] = []
        self.images: OrderedDict[int, tuple[QImage, bool]] = OrderedDict()
        self.rows: dict[int, dict[str, Any]] = {}
        self.current_row: dict[str, Any] | None = None
        self._presented_id: int | None = None
        self._placeholder: QPixmap | None = None
        self._placeholder_id: int | None = None
        self._active = False
        self._session = 0
        self._pending = False
        self._counters = workspace_counters(None)
        self._counter_timer = QTimer(self, singleShot=True, interval=120)
        self._counter_timer.timeout.connect(self._request_counters)
        self._live_held = self.live_playing = False
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self.image_view = ImageView(self)
        self.video_view = VideoView(self)
        self.message = label("", wrap=True)
        self.message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.message.setStyleSheet("background: #111214; color: #d7dce5; padding: 24px;")
        self.stack = QStackedWidget(self)
        self.stack.setObjectName("reviewCanvas")
        self.stack.addWidget(self.image_view)
        self.stack.addWidget(self.video_view)
        self.stack.addWidget(self.message)
        self.stack.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.info = InfoPanel(self)
        self.info.hide()
        self._show_info = False

        self.title = label(tr("Manual review"), "title")
        self.title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.context = label("", "context")
        self.status_label = label("", "caption")
        self.status_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.feedback = label("", "error", wrap=True)
        self.feedback.hide()
        self.buttons: dict[str, QPushButton] = {}
        commands = {
            "keep": ("Keep", "Keep (→)", "primary", self.act_keep),
            "delete": ("Move to Delete", "Delete (←)", "danger", self.act_delete),
            "skip": ("Skip", "Skip (↓)", "quiet", self.act_skip),
            "undo": ("Undo", "Undo (⌘Z)" if IS_MAC else "Undo (Ctrl+Z)", "quiet", self.act_undo),
            "play": ("Play / Pause", "Play/Pause (Space)", "quiet", self.video_view.toggle_pause),
            "mute": ("Sound", "Mute (M)", "quiet", self.video_view.toggle_mute),
            "zoom": ("Zoom", "Zoom (Z)", "quiet", self.image_view.toggle_zoom),
            "live": ("Live Photo", "Live (hold L)", "quiet", None),
            "info": ("Info", "Info (I)", "quiet", self.toggle_info),
            "back": ("Back to Library", "Back (Esc)", "quiet", self.leave),
        }
        for key, (name, hint, variant, action) in commands.items():
            widget = self.buttons[key] = button(name, variant=variant)
            widget.setToolTip(tr(hint))
            widget.setAccessibleDescription(tr(hint))
            if action is not None:
                widget.clicked.connect(action)
        self.buttons["info"].setCheckable(True)
        self.buttons["live"].pressed.connect(self.live_start)
        self.buttons["live"].released.connect(self.live_stop)

        identity = QVBoxLayout()
        identity.setSpacing(2)
        identity.addWidget(self.title)
        identity.addWidget(self.context)
        header = QHBoxLayout()
        header.addLayout(identity, 1)
        for key in ("back", "zoom", "play", "mute", "live", "info"):
            header.addWidget(self.buttons[key])
        center = QHBoxLayout()
        center.setSpacing(12)
        center.addWidget(self.stack, 1)
        center.addWidget(self.info)
        footer = QHBoxLayout()
        footer.addWidget(self.status_label, 1)
        for key in ("undo", "skip", "delete", "keep"):
            footer.addWidget(self.buttons[key])
        layout = page(self)
        layout.addLayout(header)
        layout.addLayout(center, 1)
        layout.addWidget(self.feedback)
        layout.addLayout(footer)

        self.image_view.need_full_resolution.connect(self._request_full)
        gws.image_loader.signals.ready.connect(self._image_ready)
        gws.image_loader.signals.failed.connect(self._image_failed)
        gws.action_done.connect(self._action_done)
        gws.action_failed.connect(self._action_failed)
        gws.changed.connect(self._schedule_counters)
        gws.state_changed.connect(self._update_buttons)
        self.video_view.fallback_used.connect(self._video_fallback)
        self._update_buttons()

    def reset(self) -> None:
        self.deactivate()
        self._session += 1
        self._pending = False
        self._counters = workspace_counters(None)
        self.ids = []
        self.pos = 0
        self.undo_stack.clear()
        self.images.clear()
        self.rows.clear()
        self._placeholder = None
        self._placeholder_id = None
        self._set_feedback("")

    def deactivate(self) -> None:
        if not self._active:
            return
        self._active = False
        self.video_view.stop()
        self.gws.cancel_requests("review.")
        self._counter_timer.stop()
        self.gws.image_loader.cancel_all()
        self.rows.clear()
        self.images.clear()
        self.current_row = None
        self._presented_id = None
        self.live_playing = self._live_held = False

    def start(
        self, ids: list[int], position: int, query: ListQuery, preview: QPixmap | None = None
    ) -> None:
        if self._pending:
            return
        self.reset()
        self.ids = list(ids)
        self.query = query
        self.pos = min(max(position, 0), max(len(self.ids) - 1, 0))
        self._placeholder = preview
        self._placeholder_id = self.current_id if preview is not None else None

    def leave(self) -> None:
        self.closed.emit()

    @property
    def current_id(self) -> int | None:
        return self.ids[self.pos] if 0 <= self.pos < len(self.ids) else None

    def _window(self) -> list[int]:
        return self.ids[max(0, self.pos - LOOK_BEHIND) : self.pos + LOOK_AHEAD + 1]

    def show_current(self) -> None:
        was_active = self._active
        self._active = True
        if not was_active:
            self._schedule_counters()
        self.video_view.stop()
        self.live_playing = self._live_held = False
        self._presented_id = None
        self.current_row = None
        fid = self.current_id
        self.info.setVisible(False)
        self._update_status()
        if fid is None or not self.gws.is_open:
            self.title.setText(tr("Manual review"))
            self.context.clear()
            self.message.setText(
                tr("All items reviewed. Enable “Show reviewed” to see them again.")
                if self.query.root == "source" and not self.query.show_reviewed
                else tr("Choose an item in the Library to start reviewing.")
            )
            self.stack.setCurrentWidget(self.message)
            self._update_buttons()
            return

        window = self._window()
        wanted = set(window)
        self.rows = {key: value for key, value in self.rows.items() if key in wanted}
        for key in list(self.images):
            if key not in wanted:
                self.images.pop(key)
        self.gws.image_loader.retain(wanted)
        if fid in self.rows:
            self._present(self.rows[fid])
        else:
            self.title.setText(tr("Manual review"))
            self.context.setText(tr("Loading media…"))
            if self._placeholder_id == fid and self._placeholder is not None:
                self.image_view.set_image(self._placeholder, full=False)
                self.stack.setCurrentWidget(self.image_view)
            else:
                self.message.setText(tr("Loading media…"))
                self.stack.setCurrentWidget(self.message)
        missing = [key for key in window if key not in self.rows]
        if missing:
            self.gws.request(
                "review.rows",
                partial(_review_rows, self.gws.catalog, missing, self.query),
                self._receive_rows,
                self._set_feedback,
            )
        else:
            self.gws.cancel_requests("review.rows")
        self._prefetch()
        self._update_buttons()

    def _receive_rows(self, rows: dict[int, dict[str, Any]]) -> None:
        if not self._active or not self.gws.is_open:
            return
        self.rows.update(rows)
        fid = self.current_id
        if fid is None:
            return
        row = self.rows.get(fid)
        if row is None:
            self.ids.pop(self.pos)
            self.pos = min(self.pos, max(0, len(self.ids) - 1))
            self.show_current()
            return
        if self._presented_id != fid:
            self._present(row)
        self._prefetch()
        self._update_buttons()

    def _present(self, row: dict[str, Any]) -> None:
        fid = self._presented_id = int(row["id"])
        self.current_row = row
        self.title.setText(row["name"])
        self.title.setToolTip(row["rel_path"])
        context = human_bytes(row["size"])
        if row["m_width"] and row["m_height"]:
            w, h = self._native_size(row, (0, 0))
            context = f"{w:,} × {h:,}  ·  {context}"
        self.context.setText(context)
        self._show_details()
        if row["kind"] == "video":
            self.stack.setCurrentWidget(self.video_view)
            self.video_view.play(
                self.gws.abs_path(row), width=row["m_width"] or 0, height=row["m_height"] or 0
            )
        else:
            self.stack.setCurrentWidget(self.image_view)
            cached = self.images.get(fid)
            if cached is not None:
                img, full = cached
                self.image_view.set_image(img, full=full, native_size=self._native(row, img))
            elif self._placeholder_id == fid and self._placeholder is not None:
                img, self._placeholder = self._placeholder, None
                self._placeholder_id = None
                self.image_view.set_image(img, full=False, native_size=self._native(row, img))
            else:
                self.image_view.clear()
            if cached is None:
                self._request(row, full=False)

    def _native_size(self, row: dict[str, Any], fallback: tuple[int, int]) -> tuple[int, int]:
        w, h = row["m_width"], row["m_height"]
        if not w or not h:
            return fallback
        return (h, w) if row.get("m_orientation") in (5, 6, 7, 8) else (w, h)

    def _native(self, row: dict[str, Any], img: QImage | QPixmap) -> tuple[int, int]:
        return self._native_size(row, (img.width(), img.height()))

    def _show_details(self) -> None:
        row = self.current_row
        if not self._show_info or row is None:
            self.info.hide()
            return
        meta = {key[2:]: value for key, value in row.items() if key.startswith("m_")}
        self.info.set_item(row, meta, None, None)
        self.info.show()
        self.gws.request(
            "review.details",
            partial(_details, self.gws.catalog, row["id"], row.get("edited_of")),
            lambda result: self._set_details(row, meta, result),
        )

    def _set_details(self, row: dict, meta: dict, result: tuple) -> None:
        if self._active and self.current_row is row:
            self.info.set_item(row, meta, *result)

    def _request(self, row: dict[str, Any], *, full: bool) -> None:
        self.gws.image_loader.request(
            int(row["id"]),
            self.gws.abs_path(row),
            row["m_format"],
            row["ext"],
            row["m_orientation"],
            max_side=None if full else FIT_SIDE,
        )

    def _request_full(self) -> None:
        if self.current_row is not None and self.current_row["kind"] != "video":
            self._request(self.current_row, full=True)

    def _prefetch(self) -> None:
        # Submit the selected frame first, then the next two in expected review order.
        for fid in self.ids[self.pos : self.pos + 3]:
            row = self.rows.get(fid)
            if row is not None and row["kind"] != "video" and fid not in self.images:
                self._request(row, full=False)

    def _image_ready(self, fid: int, img: QImage, full: bool) -> None:
        if not self._active or fid not in self._window():
            return
        existing = self.images.get(fid)
        if existing and existing[1] and not full:
            return
        self.images[fid] = (img, full)
        self.images.move_to_end(fid)
        while (
            len(self.images) > 1
            and sum(_image_cost(image) for image, _ in self.images.values()) > IMAGE_BUDGET
        ):
            self.images.popitem(last=False)
        if fid == self.current_id and self.current_row and self.current_row["kind"] != "video":
            self.image_view.set_image(
                img, full=full, native_size=self._native(self.current_row, img)
            )
            if not self.live_playing:
                self.stack.setCurrentWidget(self.image_view)

    def _image_failed(self, fid: int, message: str) -> None:
        if self._active and fid == self.current_id:
            if fid not in self.images:
                self.message.setText(tr("Cannot decode this file."))
                self.stack.setCurrentWidget(self.message)
            self._set_feedback(message)

    def _video_fallback(self, _fallback: bool) -> None:
        self._set_feedback(
            tr("Video cannot be played by Qt; showing frames via ffmpeg (no audio).")
        )

    def act_keep(self) -> None:
        self._decide("keep")

    def act_delete(self) -> None:
        self._decide("to_delete")

    def _decide(self, action: str) -> None:
        self.setFocus()
        ws, fid = self.gws.ws, self.current_id
        if ws is None or fid is None or self.current_row is None or self._pending:
            return
        if action == "to_delete" and self.query.root == "delete":
            self.act_skip()
            return
        self._set_feedback("")
        self._pending = self.gws.submit_action(
            ("review", self._session, action, fid), partial(_review_action, ws, action, fid)
        )
        self._update_buttons()

    def act_skip(self) -> None:
        self.setFocus()
        if not self._pending and self.current_id is not None:
            self._set_feedback("")
            self.pos += 1
            self.show_current()

    def act_undo(self) -> None:
        self.setFocus()
        ws = self.gws.ws
        if ws is None or not self.undo_stack or self._pending:
            return
        _, fid, operation = self.undo_stack[-1]
        self._set_feedback("")
        self._pending = self.gws.submit_action(
            ("review", self._session, "undo", fid),
            partial(_review_action, ws, "undo", fid, operation),
        )
        self._update_buttons()

    def _action_done(self, tag: Any, outcome: tuple) -> None:
        if tag[:2] != ("review", self._session):
            return
        self._pending = False
        action, fid = tag[2:]
        result, row = outcome
        self.rows.pop(fid, None)
        self.images.pop(fid, None)
        errors = getattr(result, "errors", [])
        if action == "undo":
            if errors:
                self._set_feedback(errors[0])
            else:
                pos, _, _ = self.undo_stack.pop()
                if row and row["status"] == "present" and row["root"] == self.query.root:
                    if fid not in self.ids:
                        self.ids.insert(min(pos, len(self.ids)), fid)
                    self.pos = self.ids.index(fid)
        else:
            op = (
                result
                if isinstance(result, str)
                else result.op_id
                if result and result.journal_ids
                else None
            )
            if op:
                self.undo_stack.append((self.pos, fid, op))
            moved_out = row is not None and row["root"] != self.query.root
            hidden = op and action == "keep" and not self.query.show_reviewed
            if moved_out or hidden:
                self.ids.pop(self.pos)
                self.pos = min(self.pos, max(len(self.ids) - 1, 0))
            elif op and not errors:
                self.pos += 1
            if errors:
                self._set_feedback(errors[0])
        if self._active:
            self.show_current()
        self._update_buttons()

    def _action_failed(self, tag: Any, message: str) -> None:
        if tag[:2] == ("review", self._session):
            self._pending = False
            self._set_feedback(message)
            self._update_buttons()

    def live_start(self) -> None:
        row = self.current_row
        if not row or not row["live_count"] or self.live_playing:
            return
        self._live_held = True
        self.gws.request(
            "review.live",
            partial(_live_video, self.gws.catalog, row["id"]),
            lambda result: self._play_live(row, result),
        )

    def _play_live(self, row: dict, companion: dict | None) -> None:
        if (
            companion is None
            or not self._active
            or not self._live_held
            or row is not self.current_row
        ):
            return
        self.live_playing = True
        self.stack.setCurrentWidget(self.video_view)
        self.video_view.play(
            self.gws.abs_path(companion),
            width=companion["m_width"] or 0,
            height=companion["m_height"] or 0,
            loop=True,
        )

    def live_stop(self) -> None:
        self._live_held = False
        self.gws.cancel_requests("review.live")
        if self.live_playing:
            self.live_playing = False
            self.video_view.stop()
            self.stack.setCurrentWidget(self.image_view)

    def toggle_info(self) -> None:
        self._show_info = not self._show_info
        self.buttons["info"].setChecked(self._show_info)
        self._show_details()
        self.setFocus()

    def _set_feedback(self, text: str) -> None:
        self.feedback.setText(text)
        self.feedback.setVisible(bool(text))

    def _update_status(self) -> None:
        count = len(self.ids)
        text = (
            tr("{current} of {total}", current=min(self.pos + 1, count), total=f"{count:,}")
            if count
            else ""
        )
        size = self._counters["bytes_to_delete"]
        if text and size:
            text += "  ·  " + tr("{size} to free", size=human_bytes(size))
        self.status_label.setText(text)
        self.status_label.setToolTip(
            tr(
                "{kept} kept, {deleted} to delete",
                kept=self._counters["kept"],
                deleted=self._counters["to_delete"],
            )
        )

    def _schedule_counters(self) -> None:
        if self._active and self.gws.is_open and not self._counter_timer.isActive():
            self._counter_timer.start()

    def _request_counters(self) -> None:
        if self._active and self.gws.is_open:
            self.gws.request(
                "review.counters", partial(workspace_counters, self.gws.ws), self._set_counters
            )

    def _set_counters(self, counters: dict[str, int]) -> None:
        self._counters = counters
        self._update_status()

    def _update_buttons(self) -> None:
        row = self.current_row
        has = row is not None
        ready = self.gws.can_write and not self._pending
        video = has and row["kind"] == "video"
        for key in ("play", "mute"):
            self.buttons[key].setVisible(video)
        self.buttons["live"].setVisible(bool(has and row.get("live_count")))
        self.buttons["zoom"].setVisible(has and not video)
        self.buttons["info"].setEnabled(has)
        self.buttons["keep"].setEnabled(has and ready)
        self.buttons["delete"].setEnabled(has and ready and self.query.root != "delete")
        self.buttons["skip"].setEnabled(has and not self._pending)
        self.buttons["undo"].setEnabled(bool(self.undo_stack) and ready)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        ctrl = event.modifiers() & (
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier
        )
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

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_L and not event.isAutoRepeat():
            self.live_stop()
        else:
            super().keyReleaseEvent(event)

    def sizeHint(self) -> QSize:
        return QSize(1000, 690)
