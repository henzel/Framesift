"""Virtualized list of items: ids in memory, rows and thumbnails fetched on demand."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QAbstractListModel, QModelIndex, QSize, Qt
from PySide6.QtGui import QImage, QPixmap

from framesift.gui.workspace import GuiWorkspace

ROW_CACHE = 20_000
THUMB_BUDGET = 200 * 1024 * 1024
FileIdRole = Qt.ItemDataRole.UserRole + 1
RowRole = Qt.ItemDataRole.UserRole + 2


@dataclass
class ListQuery:
    root: str = "source"
    dir_path: str | None = None  # None = whole root; "" = root folder only when recursive is False
    recursive: bool = True
    sort: str = "date"  # date | name | size
    descending: bool = False
    kind: str = "all"  # all | photo | video
    show_reviewed: bool = True
    category: str | None = None  # review root: first path component

    def sql(self) -> tuple[str, list[Any]]:
        where = ["f.root=?", "f.status='present'", "f.item_id=f.id"]
        params: list[Any] = [self.root]
        if self.kind == "photo":
            where.append("f.kind IN ('photo','raw')")
        elif self.kind == "video":
            where.append("f.kind='video'")
        else:
            where.append("f.kind IN ('photo','raw','video')")
        if self.category is not None:
            where.append("(f.dir_path=? OR f.dir_path LIKE ?)")
            params += [self.category, self.category + "/%"]
        elif self.dir_path is not None:
            if self.recursive:
                where.append("(f.dir_path=? OR f.dir_path LIKE ?)")
                params += [self.dir_path, (self.dir_path + "/%") if self.dir_path else "%"]
            else:
                where.append("f.dir_path=?")
                params.append(self.dir_path)
        if not self.show_reviewed:
            where.append("f.reviewed_at IS NULL")
        order = {
            "date": "COALESCE(m.date_taken_ts, f.mtime_ns/1000000000)",
            "name": "f.name COLLATE NOCASE",
            "size": "f.size",
        }[self.sort]
        direction = "DESC" if self.descending else "ASC"
        sql = (
            f"SELECT f.id FROM files f LEFT JOIN file_meta m ON m.file_id=f.id WHERE {' AND '.join(where)} "
            f"ORDER BY {order} {direction}, f.id {direction}"
        )
        return sql, params


ROW_SQL = (
    "SELECT f.*, m.format AS m_format, m.width AS m_width, m.height AS m_height, m.orientation AS m_orientation, "
    "m.make AS m_make, m.model AS m_model, m.date_taken AS m_date_taken, m.duration_ms AS m_duration_ms, "
    "m.codec AS m_codec, m.preview_kind AS m_preview_kind, m.preview_offset AS m_preview_offset, "
    "m.preview_length AS m_preview_length, m.content_id AS m_content_id, "
    "(SELECT COUNT(*) FROM files c WHERE c.item_id=f.id AND c.companion_role='live_video' AND c.status='present') AS live_count "
    "FROM files f LEFT JOIN file_meta m ON m.file_id=f.id WHERE f.id IN (%s)"
)


def meta_for_thumbnail(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": row["kind"],
        "format": row.get("m_format"),
        "ext": row["ext"],
        "orientation": row.get("m_orientation"),
        "preview_kind": row.get("m_preview_kind"),
        "preview_offset": row.get("m_preview_offset"),
        "preview_length": row.get("m_preview_length"),
        "duration_ms": row.get("m_duration_ms"),
    }


class CatalogListModel(QAbstractListModel):
    def __init__(self, gws: GuiWorkspace, parent=None):
        super().__init__(parent)
        self.gws = gws
        self.query = ListQuery()
        self.ids: list[int] = []
        self._pos: dict[int, int] = {}
        self._rows: OrderedDict[int, dict[str, Any]] = OrderedDict()
        self._thumbs: OrderedDict[int, QPixmap] = OrderedDict()
        self._thumb_bytes = 0
        self._failed: set[int] = set()
        self.icon_size = QSize(160, 160)
        self._placeholder = QPixmap(self.icon_size)
        self._placeholder.fill(Qt.GlobalColor.transparent)
        gws.thumb_loader.signals.ready.connect(self._thumb_ready)
        gws.thumb_loader.signals.failed.connect(self._thumb_failed)

    # ------------------------------------------------------------------ loading
    def load(self, query: ListQuery | None = None) -> None:
        if query is not None:
            self.query = query
        self.beginResetModel()
        self._rows.clear()
        self._failed.clear()
        if self.gws.is_open:
            sql, params = self.query.sql()
            self.ids = [int(r[0]) for r in self.gws.catalog.query(sql, params)]
        else:
            self.ids = []
        self._pos = {fid: i for i, fid in enumerate(self.ids)}
        self.endResetModel()

    def refresh_keep_position(self) -> None:
        """Reload ids after files moved, keeping cached rows for ids that still exist."""
        if not self.gws.is_open:
            return
        sql, params = self.query.sql()
        new_ids = [int(r[0]) for r in self.gws.catalog.query(sql, params)]
        if new_ids == self.ids:
            self._rows.clear()
            if self.ids:
                self.dataChanged.emit(self.index(0), self.index(len(self.ids) - 1))
            return
        self.beginResetModel()
        self.ids = new_ids
        self._pos = {fid: i for i, fid in enumerate(self.ids)}
        self._rows.clear()
        self.endResetModel()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self.ids)

    def row_at(self, index: int) -> dict[str, Any] | None:
        if index < 0 or index >= len(self.ids):
            return None
        fid = self.ids[index]
        row = self._rows.get(fid)
        if row is None:
            self._fetch_around(index)
            row = self._rows.get(fid)
        return row

    def row_for_id(self, file_id: int) -> dict[str, Any] | None:
        pos = self._pos.get(file_id)
        return self.row_at(pos) if pos is not None else None

    def index_of(self, file_id: int) -> int:
        return self._pos.get(file_id, -1)

    def _fetch_around(self, index: int, span: int = 120) -> None:
        lo, hi = max(0, index - span // 4), min(len(self.ids), index + span)
        wanted = [fid for fid in self.ids[lo:hi] if fid not in self._rows]
        if not wanted:
            return
        catalog = self.gws.catalog
        for i in range(0, len(wanted), 400):
            chunk = wanted[i : i + 400]
            sql = ROW_SQL % ",".join("?" * len(chunk))
            for r in catalog.query(sql, chunk):
                self._rows[int(r["id"])] = dict(r)
        while len(self._rows) > ROW_CACHE:
            self._rows.popitem(last=False)

    # ------------------------------------------------------------------ data
    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        row = self.row_at(index.row())
        if row is None:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return row["name"]
        if role == FileIdRole:
            return int(row["id"])
        if role == RowRole:
            return row
        if role == Qt.ItemDataRole.DecorationRole:
            return self.thumbnail(row)
        if role == Qt.ItemDataRole.ToolTipRole:
            return row["rel_path"]
        if role == Qt.ItemDataRole.SizeHintRole:
            return QSize(self.icon_size.width() + 16, self.icon_size.height() + 34)
        return None

    def thumbnail(self, row: dict[str, Any]) -> QPixmap:
        fid = int(row["id"])
        pix = self._thumbs.get(fid)
        if pix is not None:
            self._thumbs.move_to_end(fid)
            return pix
        if fid not in self._failed and self.gws.is_open:
            self.gws.thumb_loader.request(
                self.gws.catalog, self.gws.roots, row, meta_for_thumbnail(row)
            )
        return self._placeholder

    def has_thumbnail(self, file_id: int) -> bool:
        return file_id in self._thumbs

    def _thumb_ready(self, file_id: int, image: QImage) -> None:
        pix = QPixmap.fromImage(image)
        self._thumbs[file_id] = pix
        self._thumb_bytes += pix.width() * pix.height() * 4
        while self._thumb_bytes > THUMB_BUDGET and len(self._thumbs) > 1:
            _fid, old = self._thumbs.popitem(last=False)
            self._thumb_bytes -= old.width() * old.height() * 4
        pos = self._pos.get(file_id)
        if pos is not None:
            idx = self.index(pos)
            self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.DecorationRole])

    def _thumb_failed(self, file_id: int) -> None:
        self._failed.add(file_id)

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def remove_id(self, file_id: int) -> None:
        pos = self._pos.get(file_id)
        if pos is None:
            return
        self.beginRemoveRows(QModelIndex(), pos, pos)
        del self.ids[pos]
        self._pos = {fid: i for i, fid in enumerate(self.ids)}
        self._rows.pop(file_id, None)
        self.endRemoveRows()

    def invalidate_row(self, file_id: int) -> None:
        self._rows.pop(file_id, None)
        pos = self._pos.get(file_id)
        if pos is not None:
            idx = self.index(pos)
            self.dataChanged.emit(idx, idx)
