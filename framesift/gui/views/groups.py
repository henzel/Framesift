"""Group mode: similar shots and duplicate groups, one group at a time, keeper pre-marked."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QImage, QKeyEvent, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from framesift.engine.apply import apply_group_decision
from framesift.engine.i18n import t
from framesift.gui.i18n import tr
from framesift.gui.media.image_view import ImageView
from framesift.gui.models.catalog_list import meta_for_thumbnail
from framesift.gui.models.tree import human_bytes
from framesift.gui.workspace import GuiWorkspace

FIT_SIDE = 2560
GROUP_SQL = (
    "SELECT c.group_id, c.category, COUNT(*) AS n FROM candidates c JOIN files f ON f.id=c.file_id "
    "WHERE c.group_id IS NOT NULL AND f.status='present' AND c.run_id=? GROUP BY c.group_id, c.category "
    "HAVING COUNT(*) >= 2 ORDER BY c.category, MIN(c.id)"
)
MEMBER_SQL = (
    "SELECT f.*, c.is_keeper, c.reason_key, c.reason_params, "
    "m.format AS m_format, m.orientation AS m_orientation, m.preview_kind AS m_preview_kind, "
    "m.preview_offset AS m_preview_offset, m.preview_length AS m_preview_length, m.duration_ms AS m_duration_ms, "
    "m.width AS m_width, m.height AS m_height, m.date_taken AS m_date_taken "
    "FROM candidates c JOIN files f ON f.id=c.file_id LEFT JOIN file_meta m ON m.file_id=f.id "
    "WHERE c.group_id=? AND f.status='present' ORDER BY c.is_keeper DESC, f.rel_path"
)


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
        self.undo_stack: list[tuple[int, str]] = []
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self.image_view = ImageView(self)
        self.strip = QListWidget(self)
        self.strip.setFlow(QListWidget.Flow.LeftToRight)
        self.strip.setIconSize(QSize(160, 160))
        self.strip.setFixedHeight(210)
        self.strip.setSpacing(6)
        self.strip.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.strip.currentRowChanged.connect(self._select)
        self.header = QLabel(self)
        self.detail = QLabel(self)
        self.detail.setWordWrap(True)
        self.status_label = QLabel(self)
        self.buttons: dict[str, QPushButton] = {}
        bar = QHBoxLayout()
        for key, label in (
            ("mark", "Keep (K)"),
            ("apply", "Apply (Enter)"),
            ("skip", "Skip group (↓)"),
            ("undo", "Undo (Ctrl+Z)"),
            ("back", "Back (Esc)"),
        ):
            btn = QPushButton(tr(label), self)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self.buttons[key] = btn
            bar.addWidget(btn)
        bar.addStretch(1)
        bar.addWidget(self.status_label)
        self.buttons["mark"].clicked.connect(self.toggle_mark)
        self.buttons["apply"].clicked.connect(self.apply_group)
        self.buttons["skip"].clicked.connect(self.skip_group)
        self.buttons["undo"].clicked.connect(self.undo)
        self.buttons["back"].clicked.connect(self.closed)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.header)
        layout.addWidget(self.image_view, 1)
        layout.addWidget(self.detail)
        layout.addWidget(self.strip)
        layout.addLayout(bar)
        gws.thumb_loader.signals.ready.connect(self._thumb_ready)
        gws.image_loader.signals.ready.connect(self._image_ready)

    # ------------------------------------------------------------------ loading
    def start(self) -> None:
        run_id = self.gws.catalog.latest_run_id() if self.gws.is_open else None
        self.groups = (
            [dict(r) for r in self.gws.catalog.query(GROUP_SQL, (run_id,))] if run_id else []
        )
        self.gpos = 0
        self.undo_stack = []
        self.load_group()
        self.setFocus()

    def _clear_strip(self) -> None:
        # QListWidget.clear() has crashed under pytest-qt with icon items; take items out one by one
        self.strip.blockSignals(True)
        while self.strip.count():
            self.strip.takeItem(self.strip.count() - 1)
        self.strip.blockSignals(False)
        self.selected = 0

    def load_group(self) -> None:
        self._clear_strip()
        self.members = []
        self.marked = set()
        if self.gpos >= len(self.groups):
            self.header.setText(tr("No groups to review."))
            self.detail.setText("")
            self.image_view.clear()
            self._status()
            return
        group = self.groups[self.gpos]
        self.members = [dict(r) for r in self.gws.catalog.query(MEMBER_SQL, (group["group_id"],))]
        if len(self.members) < 2:
            self.gpos += 1
            self.load_group()
            return
        for m in self.members:
            if m["is_keeper"]:
                self.marked.add(int(m["id"]))
            item = QListWidgetItem(m["name"])
            item.setData(Qt.ItemDataRole.UserRole, int(m["id"]))
            self.strip.addItem(item)
            data = self.gws.thumb_cache.get(
                self.gws.catalog.uuid, int(m["id"]), m["size"], m["mtime_ns"]
            )
            if data:
                img = QImage.fromData(data, "JPEG")
                item.setIcon(QPixmap.fromImage(img))
            else:
                self.gws.thumb_loader.request(
                    self.gws.catalog, self.gws.roots, m, meta_for_thumbnail(m)
                )
        self._refresh_marks()
        self.strip.setCurrentRow(0)
        self.header.setText(
            f"{t('category.' + group['category'])} — "
            + tr(
                "Group {group}: {n} items, {k} marked to keep",
                group=group["group_id"],
                n=len(self.members),
                k=len(self.marked),
            )
        )
        self._status()

    def _thumb_ready(self, file_id: int, image: QImage) -> None:
        for i in range(self.strip.count()):
            item = self.strip.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == file_id:
                item.setIcon(QPixmap.fromImage(image))

    def _select(self, row: int) -> None:
        if row < 0 or row >= len(self.members):
            return
        self.selected = row
        m = self.members[row]
        meta = self.gws.meta(int(m["id"]))
        self.gws.image_loader.request(
            int(m["id"]),
            self.gws.abs_path(m),
            m.get("m_format"),
            m["ext"],
            m.get("m_orientation"),
            max_side=FIT_SIDE,
        )
        data = self.gws.thumb_cache.get(
            self.gws.catalog.uuid, int(m["id"]), m["size"], m["mtime_ns"]
        )
        if data:
            img = QImage.fromData(data, "JPEG")
            if not img.isNull():
                self.image_view.set_image(img, full=False, native_size=(img.width(), img.height()))
        w, h = meta.get("width"), meta.get("height")
        analysis = self.gws.catalog.analysis(int(m["id"]))
        sharp = (
            f"  sharpness {analysis['sharpness']:.1f}"
            if analysis and analysis["sharpness"] is not None
            else ""
        )
        self.detail.setText(
            f"{m['rel_path']}   {human_bytes(m['size'])}   {f'{w}×{h}' if w and h else ''}   "
            f"{(m.get('m_date_taken') or '').replace('T', ' ')}{sharp}"
            + (
                "   ★ " + t("reason.similar_keeper", group=self.groups[self.gpos]["group_id"])
                if m["is_keeper"]
                else ""
            )
        )

    def _image_ready(self, fid: int, img: QImage, full: bool) -> None:
        if (
            self.members
            and 0 <= self.selected < len(self.members)
            and int(self.members[self.selected]["id"]) == fid
        ):
            self.image_view.set_image(img, full=full)

    def _refresh_marks(self) -> None:
        for i in range(self.strip.count()):
            item = self.strip.item(i)
            fid = item.data(Qt.ItemDataRole.UserRole)
            name = self.members[i]["name"]
            item.setText(("✓ " if fid in self.marked else "") + name)
        if self.groups and self.gpos < len(self.groups):
            g = self.groups[self.gpos]
            self.header.setText(
                f"{t('category.' + g['category'])} — "
                + tr(
                    "Group {group}: {n} items, {k} marked to keep",
                    group=g["group_id"],
                    n=len(self.members),
                    k=len(self.marked),
                )
            )

    def _status(self) -> None:
        self.status_label.setText(f"{min(self.gpos + 1, len(self.groups))} / {len(self.groups)}")

    # ------------------------------------------------------------------ actions
    def toggle_mark(self) -> None:
        if not self.members:
            return
        fid = int(self.members[self.selected]["id"])
        if fid in self.marked:
            self.marked.discard(fid)
        else:
            self.marked.add(fid)
        self._refresh_marks()

    def apply_group(self) -> None:
        if not self.members or self.gws.read_only:
            return
        ws = self.gws.ws
        assert ws is not None
        keep = [
            int(m["item_id"])
            for m in self.members
            if int(m["id"]) in self.marked and m["root"] != "source"
        ]
        keep_source = [
            int(m["item_id"])
            for m in self.members
            if int(m["id"]) in self.marked and m["root"] == "source"
        ]
        delete = [int(m["item_id"]) for m in self.members if int(m["id"]) not in self.marked]
        group_id = self.groups[self.gpos]["group_id"]
        self.gws.wait_actions()
        apply_group_decision(
            ws.catalog,
            ws.roots,
            session_id=ws.session_id,
            keep_items=keep + keep_source,
            delete_items=delete,
        )
        self.undo_stack.append((self.gpos, group_id))
        self.gws.changed.emit()
        self.gpos += 1
        self.load_group()

    def skip_group(self) -> None:
        if self.gpos < len(self.groups):
            self.gpos += 1
            self.load_group()

    def undo(self) -> None:
        if not self.undo_stack or self.gws.read_only:
            return
        ws = self.gws.ws
        assert ws is not None
        gpos, _group_id = self.undo_stack.pop()
        members = [m for m in self.members]  # noqa: C416
        del members
        from framesift.engine.journal import undo_ops

        ops = ws.catalog.undoable_ops(session_id=ws.session_id)
        # a group decision produced up to len(members) ops; undo them until the group's files are back
        rows_per_group = ws.catalog.query(
            "SELECT DISTINCT j.op_id FROM journal j JOIN candidates c ON c.file_id=j.file_id "
            "WHERE j.session_id=? AND j.undone_by IS NULL AND j.action IN ('keep','to_delete','restore') AND c.group_id=? "
            "ORDER BY j.id DESC",
            (ws.session_id, _group_id),
        )
        wanted = [r[0] for r in rows_per_group if r[0] in ops]
        if wanted:
            undo_ops(ws.catalog, ws.roots, wanted, session_id=ws.session_id)
        self.gws.changed.emit()
        self.gpos = gpos
        self.load_group()

    # ------------------------------------------------------------------ keys
    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        key = event.key()
        ctrl = event.modifiers() & (
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
        elif key == Qt.Key.Key_Z and ctrl:
            self.undo()
        elif key == Qt.Key.Key_Escape:
            self.closed.emit()
        else:
            super().keyPressEvent(event)
