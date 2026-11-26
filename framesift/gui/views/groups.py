"""Compare duplicate and similar-shot groups and journal each decision."""

from __future__ import annotations

from collections import OrderedDict
from functools import partial
from typing import Any

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon, QImage, QKeyEvent, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QListWidget, QListWidgetItem, QStackedWidget, QWidget

from framesift.engine.api import Workspace
from framesift.engine.apply import apply_group_decision
from framesift.engine.catalog import Catalog
from framesift.engine.i18n import t
from framesift.engine.journal import undo_ops
from framesift.gui.i18n import tr
from framesift.gui.media.image_view import ImageView
from framesift.gui.models.catalog_list import meta_for_thumbnail
from framesift.gui.models.tree import human_bytes
from framesift.gui.ui import button, label, page
from framesift.gui.workers import ThumbKey, thumbnail_key
from framesift.gui.workspace import GuiWorkspace

GROUP_SQL = (
    "SELECT c.group_id, c.category, COUNT(*) AS n, c.run_id FROM candidates c JOIN files f ON f.id=c.file_id "
    "WHERE c.group_id IS NOT NULL AND f.status='present' AND c.run_id=? GROUP BY c.group_id, c.category "
    "HAVING COUNT(*) >= 2 ORDER BY c.category, MIN(c.id)"
)
MEMBER_SQL = (
    "SELECT f.*, c.is_keeper, c.reason_key, c.reason_params, "
    "m.format AS m_format, m.orientation AS m_orientation, m.preview_kind AS m_preview_kind, "
    "m.preview_offset AS m_preview_offset, m.preview_length AS m_preview_length, m.duration_ms AS m_duration_ms, "
    "m.width AS m_width, m.height AS m_height, m.date_taken AS m_date_taken "
    "FROM candidates c JOIN files f ON f.id=c.file_id LEFT JOIN file_meta m ON m.file_id=f.id "
    "WHERE c.group_id=? AND c.run_id=? AND f.status='present' ORDER BY c.is_keeper DESC, f.rel_path"
)


def _available_groups(catalog: Catalog) -> list[dict]:
    run_id = catalog.latest_run_id()
    return [dict(row) for row in catalog.query(GROUP_SQL, (run_id,))] if run_id else []


def _members(catalog: Catalog, group: dict) -> list[dict]:
    return [dict(row) for row in catalog.query(MEMBER_SQL, (group["group_id"], group["run_id"]))]


def _apply_group(ws: Workspace, keep: list[int], delete: list[int]) -> tuple:
    session = ws.session_id
    start = ws.catalog.scalar("SELECT COALESCE(MAX(id),0) FROM journal")
    stats = apply_group_decision(
        ws.catalog,
        ws.roots,
        session_id=session,
        keep_items=keep,
        delete_items=delete,
    )
    ops = [
        row[0]
        for row in ws.catalog.query(
            "SELECT op_id FROM journal WHERE session_id=? AND id>? GROUP BY op_id ORDER BY MAX(id) DESC",
            (session, start),
        )
    ]
    return stats, ops


def _undo_group(ws: Workspace, ops: list[str]):
    return undo_ops(ws.catalog, ws.roots, ops, session_id=ws.session_id)


class GroupsView(QWidget):
    closed = Signal()

    def __init__(self, gws: GuiWorkspace, parent=None):
        super().__init__(parent)
        self.gws = gws
        self.groups: list[dict[str, Any]] = []
        self.gpos = 0
        self.members: list[dict[str, Any]] = []
        self.marked: set[int] = set()
        self.selected = 0
        self.undo_stack: list[tuple[int, list[str]]] = []
        self._active = self._pending = False
        self._items: dict[ThumbKey, QListWidgetItem] = {}
        self._pixmaps: OrderedDict[ThumbKey, QPixmap] = OrderedDict()
        self._current_loaded = False
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self.image_view = ImageView(self)
        self.image_view.need_full_resolution.connect(lambda: self._request_image(True))
        self.message = label(tr("No groups to review."), "context", wrap=True)
        self.message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.canvas = QStackedWidget(self)
        self.canvas.addWidget(self.image_view)
        self.canvas.addWidget(self.message)
        self.canvas.setCurrentWidget(self.message)
        self.strip = QListWidget(self)
        self.strip.setObjectName("filmstrip")
        self.strip.setAccessibleName(tr("Frames in this group"))
        self.strip.setViewMode(QListWidget.ViewMode.IconMode)
        self.strip.setFlow(QListWidget.Flow.LeftToRight)
        self.strip.setWrapping(False)
        self.strip.setMovement(QListWidget.Movement.Static)
        self.strip.setHorizontalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.strip.setIconSize(QSize(104, 66))
        self.strip.setGridSize(QSize(112, 88))
        self.strip.setFixedHeight(108)
        self.strip.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.strip.currentRowChanged.connect(self._select)
        self.strip.horizontalScrollBar().valueChanged.connect(self._request_visible)
        self.header = label(tr("Compare groups"), "title")
        self.detail = label("", "context")
        self.status_label = label("", "caption")
        self.feedback = label("", "error", wrap=True)
        self.feedback.hide()
        self.buttons = {
            "mark": button("Toggle keep", variant="quiet"),
            "apply": button("Apply decisions", variant="primary"),
            "skip": button("Skip group", variant="quiet"),
            "undo": button("Undo", variant="quiet"),
            "back": button("Back to analysis", variant="quiet"),
        }
        for key, (hint, action) in {
            "mark": ("Keep (K)", self.toggle_mark),
            "apply": ("Apply (Enter)", self.apply_group),
            "skip": ("Skip group (↓)", self.skip_group),
            "undo": ("Undo (Ctrl+Z)", self.undo),
            "back": ("Back (Esc)", self.closed.emit),
        }.items():
            self.buttons[key].setToolTip(tr(hint))
            self.buttons[key].clicked.connect(action)

        header = QHBoxLayout()
        header.addWidget(self.header, 1)
        header.addWidget(self.buttons["back"])
        bar = QHBoxLayout()
        bar.addWidget(self.status_label, 1)
        for key in ("undo", "skip", "mark", "apply"):
            bar.addWidget(self.buttons[key])
        layout = page(self)
        layout.addLayout(header)
        layout.addWidget(self.detail)
        layout.addWidget(self.canvas, 1)
        layout.addWidget(self.strip)
        layout.addWidget(self.feedback)
        layout.addLayout(bar)
        gws.thumb_loader.signals.ready.connect(self._thumb_ready)
        gws.image_loader.signals.ready.connect(self._image_ready)
        gws.image_loader.signals.failed.connect(self._image_failed)
        gws.action_done.connect(self._action_done)
        gws.action_failed.connect(self._action_failed)
        gws.state_changed.connect(self._update_buttons)
        gws.closed.connect(self._clear_workspace)
        self._update_buttons()

    def start(self) -> None:
        self._active = True
        self.setFocus()
        if not self.gws.is_open:
            self._empty(tr("No groups to review."))
            return
        if self._pending:
            return
        self._empty(tr("Loading groups…"))
        self.gws.request(
            "groups.list",
            partial(_available_groups, self.gws.catalog),
            self._set_groups,
            self._empty,
        )

    def _set_groups(self, groups: list[dict]) -> None:
        old_keys = [(group["run_id"], group["group_id"]) for group in self.groups]
        new_keys = [(group["run_id"], group["group_id"]) for group in groups]
        if old_keys != new_keys:
            self.gpos = 0
            self.undo_stack.clear()
        self.groups = groups
        self.load_group()

    def deactivate(self) -> None:
        if self._active:
            self._active = False
            self.gws.cancel_requests("groups.")
            self.gws.thumb_loader.discard_waiting("groups")
            self.gws.image_loader.cancel_all()

    def _clear_workspace(self) -> None:
        self.deactivate()
        self.groups.clear()
        self.undo_stack.clear()
        self.gpos = 0
        self._pending = False
        self._empty(tr("No groups to review."))

    def _empty(self, text: str) -> None:
        self.strip.blockSignals(True)
        self._items.clear()
        self._pixmaps.clear()
        while self.strip.count():
            self.strip.takeItem(self.strip.count() - 1)
        self.strip.blockSignals(False)
        self.strip.hide()
        self.selected = 0
        self.members = []
        self.marked.clear()
        self.header.setText(tr("Compare groups"))
        self.detail.clear()
        self.message.setText(text)
        self.canvas.setCurrentWidget(self.message)
        self.image_view.clear()
        self._status()
        self._update_buttons()

    def load_group(self) -> None:
        if not self._active or not self.gws.is_open:
            return
        self.gws.image_loader.cancel_all()
        self.gws.thumb_loader.discard_waiting("groups")
        if self.gpos >= len(self.groups):
            self._empty(tr("No groups to review."))
            return
        self._empty(tr("Loading groups…"))
        self.gws.request(
            "groups.members",
            partial(_members, self.gws.catalog, self.groups[self.gpos]),
            self._set_members,
            self._empty,
        )

    def _set_members(self, members: list[dict]) -> None:
        if len(members) < 2:
            self.gpos += 1
            self.load_group()
            return
        self.members = members
        self.strip.show()
        self.strip.blockSignals(True)
        for member in members:
            if member["is_keeper"]:
                self.marked.add(member["id"])
            item = QListWidgetItem(member["name"])
            item.setToolTip(member["rel_path"])
            self._items[thumbnail_key(member)] = item
            self.strip.addItem(item)
        self.strip.blockSignals(False)
        self._refresh_marks()
        self.strip.setCurrentRow(0)
        self._status()
        self._update_buttons()

    def _thumb_ready(self, key: ThumbKey, image: QImage) -> None:
        if not self._active or key not in self._items:
            return
        pix = QPixmap.fromImage(image)
        self._pixmaps[key] = pix
        while len(self._pixmaps) > 48:
            evicted, _ = self._pixmaps.popitem(last=False)
            self._items[evicted].setIcon(QIcon())
        self._items[key].setIcon(QIcon(pix))
        if (
            not self._current_loaded
            and self.members
            and thumbnail_key(self.members[self.selected]) == key
        ):
            self.image_view.set_image(pix, full=False)
            self.canvas.setCurrentWidget(self.image_view)

    def _request_visible(self, *_args: Any) -> None:
        if not self.members or not self._active or not self.gws.is_open:
            return
        self.gws.thumb_loader.discard_waiting("groups")
        viewport = self.strip.viewport().rect()
        cell = self.strip.gridSize().width()
        first = max(0, self.strip.horizontalScrollBar().value() // cell - 1)
        visible = viewport.width() // cell + 3
        for member in self.members[first : first + visible]:
            key = thumbnail_key(member)
            if key not in self._pixmaps and self.strip.visualItemRect(self._items[key]).intersects(
                viewport
            ):
                self.gws.thumb_loader.request(
                    self.gws.catalog_uuid,
                    self.gws.roots,
                    member,
                    meta_for_thumbnail(member),
                    scope="groups",
                )

    def _select(self, row: int) -> None:
        if not 0 <= row < len(self.members) or not self._active:
            return
        self.selected = row
        self._current_loaded = False
        member = self.members[row]
        self._set_feedback("")
        self.gws.image_loader.retain({member["id"]})
        preview = self._pixmaps.get(thumbnail_key(member))
        if preview is not None:
            self.image_view.set_image(
                preview, full=False, native_size=self._native(member, preview)
            )
            self.canvas.setCurrentWidget(self.image_view)
        else:
            self.image_view.clear()
            self.message.setText(tr("Loading media…"))
            self.canvas.setCurrentWidget(self.message)
        self._request_image(False)
        self._request_visible()
        parts = [member["name"], human_bytes(member["size"])]
        if member["m_width"] and member["m_height"]:
            parts.append(f"{member['m_width']} × {member['m_height']}")
        if member["m_date_taken"]:
            parts.append(member["m_date_taken"].replace("T", " "))
        self.detail.setText("  ·  ".join(parts))
        self.detail.setToolTip(member["rel_path"])

    @staticmethod
    def _native(member: dict, image: QImage | QPixmap) -> tuple[int, int]:
        w, h = member["m_width"], member["m_height"]
        if not w or not h:
            return image.width(), image.height()
        return (h, w) if member["m_orientation"] in (5, 6, 7, 8) else (w, h)

    def _request_image(self, full: bool) -> None:
        if not self._active or not self.members:
            return
        member = self.members[self.selected]
        if member["kind"] == "video":
            return
        self.gws.image_loader.request(
            member["id"],
            self.gws.abs_path(member),
            member["m_format"],
            member["ext"],
            member["m_orientation"],
            max_side=None if full else 1920,
        )

    def _image_ready(self, fid: int, img: QImage, full: bool) -> None:
        if self._active and self.members and self.members[self.selected]["id"] == fid:
            self._current_loaded = True
            if not self.image_view.is_full or full:
                self.image_view.set_image(
                    img, full=full, native_size=self._native(self.members[self.selected], img)
                )
            self.canvas.setCurrentWidget(self.image_view)

    def _image_failed(self, fid: int, message: str) -> None:
        if self._active and self.members and self.members[self.selected]["id"] == fid:
            if self.canvas.currentWidget() is self.message:
                self.message.setText(tr("Cannot decode this file."))
            self._set_feedback(message)

    def _refresh_marks(self) -> None:
        for member in self.members:
            item = self._items[thumbnail_key(member)]
            item.setText(("✓ " if member["id"] in self.marked else "") + member["name"])
        if self.gpos < len(self.groups):
            self.header.setText(t("category." + self.groups[self.gpos]["category"]))
        self._status()

    def _status(self) -> None:
        count = len(self.groups)
        text = (
            tr("{current} of {total}", current=min(self.gpos + 1, count), total=count)
            if count
            else ""
        )
        if self.members:
            text += "  ·  " + tr("{n} marked to keep", n=len(self.marked))
        self.status_label.setText(text)

    def _update_buttons(self) -> None:
        editable = self.gws.can_write and not self._pending
        self.buttons["mark"].setEnabled(editable and bool(self.members))
        self.buttons["apply"].setEnabled(editable and bool(self.members))
        self.buttons["skip"].setEnabled(not self._pending and bool(self.members))
        self.buttons["undo"].setEnabled(editable and bool(self.undo_stack))

    def toggle_mark(self) -> None:
        self.setFocus()
        if self.members and self.gws.can_write and not self._pending:
            fid = self.members[self.selected]["id"]
            self.marked.symmetric_difference_update({fid})
            self._refresh_marks()

    def apply_group(self) -> None:
        self.setFocus()
        ws = self.gws.ws
        if ws is None or not self.members or self._pending:
            return
        keep = [member["item_id"] for member in self.members if member["id"] in self.marked]
        delete = [member["item_id"] for member in self.members if member["id"] not in self.marked]
        self._set_feedback("")
        self._pending = self.gws.submit_action(
            ("groups", "apply"), partial(_apply_group, ws, keep, delete)
        )
        self._update_buttons()

    def skip_group(self) -> None:
        self.setFocus()
        if self.members and not self._pending:
            self._set_feedback("")
            self.gpos += 1
            self.load_group()

    def undo(self) -> None:
        self.setFocus()
        ws = self.gws.ws
        if ws is None or not self.undo_stack or self._pending:
            return
        _, ops = self.undo_stack[-1]
        self._set_feedback("")
        self._pending = self.gws.submit_action(("groups", "undo"), partial(_undo_group, ws, ops))
        self._update_buttons()

    def _action_done(self, tag: Any, result: Any) -> None:
        if tag[0] != "groups":
            return
        self._pending = False
        if tag[1] == "apply":
            stats, ops = result
            if ops:
                self.undo_stack.append((self.gpos, ops))
            if not stats.errors:
                self.gpos += 1
        else:
            stats = result
            if not stats.errors:
                self.gpos, _ = self.undo_stack.pop()
        if stats.errors:
            self._set_feedback(stats.errors[0])
        self.load_group()
        self._update_buttons()

    def _action_failed(self, tag: Any, message: str) -> None:
        if tag[0] == "groups":
            self._pending = False
            self._set_feedback(message)
            self._update_buttons()

    def _set_feedback(self, text: str) -> None:
        self.feedback.setText(text)
        self.feedback.setVisible(bool(text))

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        control = event.modifiers() & (
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier
        )
        if key == Qt.Key.Key_Right and self.members:
            self.strip.setCurrentRow(min(self.selected + 1, len(self.members) - 1))
        elif key == Qt.Key.Key_Left and self.members:
            self.strip.setCurrentRow(max(self.selected - 1, 0))
        elif key == Qt.Key.Key_K:
            self.toggle_mark()
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.apply_group()
        elif key == Qt.Key.Key_Down:
            self.skip_group()
        elif key == Qt.Key.Key_Z and control:
            self.undo()
        elif key == Qt.Key.Key_Escape:
            self.closed.emit()
        else:
            super().keyPressEvent(event)
