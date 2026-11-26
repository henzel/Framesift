"""Virtualized list of items: ids in memory, rows and thumbnails fetched on demand."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from functools import partial
from typing import Any
from unicodedata import normalize

from PySide6.QtCore import QAbstractListModel, QModelIndex, QSize, Qt, Signal
from PySide6.QtGui import QImage, QPixmap

from framesift.engine.catalog import Catalog
from framesift.gui.workers import ThumbKey, thumbnail_key
from framesift.gui.workspace import GuiWorkspace

ROW_BATCH = 96
ROW_CACHE = 32
THUMB_BUDGET = 64 * 1024 * 1024
FileIdRole = Qt.ItemDataRole.UserRole + 1
RowRole = Qt.ItemDataRole.UserRole + 2


@dataclass(frozen=True)
class ListQuery:
    root: str = "source"
    dir_path: str | None = None  # None = whole root; "" = root folder only when recursive is False
    recursive: bool = True
    sort: str = "date"  # date | name | size
    descending: bool = False
    kind: str = "all"  # all | photo | video
    show_reviewed: bool = True
    category: str | None = None  # review root: first path component
    search: str = ""

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
            if self.category:
                prefix = self.category + "/"
                where.append("(f.dir_path=? OR substr(f.dir_path,1,?)=?)")
                params += [self.category, len(prefix), prefix]
            else:
                where.append("f.dir_path=''")
        elif self.dir_path is not None:
            if self.recursive and self.dir_path:
                prefix = self.dir_path + "/"
                where.append("(f.dir_path=? OR substr(f.dir_path,1,?)=?)")
                params += [self.dir_path, len(prefix), prefix]
            elif not self.recursive:
                where.append("f.dir_path=?")
                params.append(self.dir_path)
        if not self.show_reviewed:
            where.append("f.reviewed_at IS NULL")
        if self.search:
            where.append("instr(casefold(f.name),?)>0")
            params.append(normalize("NFC", self.search).casefold())
        order = {
            "date": "COALESCE(m.date_taken_ts, f.mtime_ns/1000000000)",
            "name": "casefold(f.name)",
            "size": "f.size",
        }[self.sort]
        direction = "DESC" if self.descending else "ASC"
        join = " LEFT JOIN file_meta m ON m.file_id=f.id" if self.sort == "date" else ""
        sql = f"SELECT f.id, f.name FROM files f{join} WHERE {' AND '.join(where)} ORDER BY {order} {direction}, f.id {direction}"
        return sql, params


ROW_SQL = (
    "SELECT f.*, m.format AS m_format, m.width AS m_width, m.height AS m_height, m.orientation AS m_orientation, "
    "m.make AS m_make, m.model AS m_model, m.date_taken AS m_date_taken, m.date_source AS m_date_source, m.duration_ms AS m_duration_ms, "
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


def catalog_rows(catalog: Catalog, ids: list[int]) -> dict[int, dict[str, Any]]:
    if not ids:
        return {}
    sql = ROW_SQL % ",".join("?" * len(ids))
    return {int(row["id"]): dict(row) for row in catalog.query(sql, ids)}


def _gallery_snapshot(catalog: Catalog, query: ListQuery) -> tuple:
    sql, params = query.sql()
    records = catalog.query(sql, params)
    ids = [int(row[0]) for row in records]
    names = [row[1] for row in records]
    positions = {fid: pos for pos, fid in enumerate(ids)}
    return ids, names, positions


class CatalogListModel(QAbstractListModel):
    loaded = Signal()
    load_failed = Signal(str)

    def __init__(self, gws: GuiWorkspace, parent=None):
        super().__init__(parent)
        self.gws = gws
        self.query = ListQuery()
        self.ids: list[int] = []
        self._names: list[str] = []
        self._pos: dict[int, int] = {}
        self._batches: OrderedDict[int, dict[int, dict[str, Any]]] = OrderedDict()
        self._pending: set[int] = set()
        self._thumbs: OrderedDict[ThumbKey, QPixmap] = OrderedDict()
        self._thumb_bytes = 0
        self._failed: set[ThumbKey] = set()
        self.icon_size = QSize(154, 116)
        self.loading = False
        self.error = ""
        gws.thumb_loader.signals.ready.connect(self._thumb_ready)
        gws.thumb_loader.signals.failed.connect(self._thumb_failed)

    # ------------------------------------------------------------------ loading
    def load(self, query: ListQuery | None = None) -> None:
        if query is not None:
            self.query = query
        if not self.gws.is_open:
            self.clear()
            return
        self.loading = True
        self.error = ""
        self.gws.request(
            "gallery.list",
            partial(_gallery_snapshot, self.gws.catalog, self.query),
            self._set_snapshot,
            self._load_error,
        )

    def clear(self) -> None:
        self.gws.cancel_requests("gallery.")
        self._failed.clear()
        self._thumbs.clear()
        self._thumb_bytes = 0
        self.error = ""
        self._set_snapshot(([], [], {}))

    def _set_snapshot(self, snapshot: tuple) -> None:
        ids, names, positions = snapshot
        self.loading = False
        self._pending.clear()
        self.gws.cancel_requests("gallery.rows.")
        self._batches.clear()
        if ids == self.ids and names == self._names:
            if self.ids:
                self.dataChanged.emit(self.index(0), self.index(len(self.ids) - 1))
            self.loaded.emit()
            return
        self.beginResetModel()
        self.ids, self._names, self._pos = ids, names, positions
        self.endResetModel()
        self.loaded.emit()

    def _load_error(self, message: str) -> None:
        self.error = message
        self._set_snapshot(([], [], {}))
        self.load_failed.emit(message)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self.ids)

    def row_at(self, index: int) -> dict[str, Any] | None:
        if index < 0 or index >= len(self.ids):
            return None
        batch = index // ROW_BATCH
        if batch in self._batches:
            self._batches.move_to_end(batch)
            return self._batches[batch].get(self.ids[index])
        if batch not in self._pending and self.gws.is_open:
            self._pending.add(batch)
            request = f"gallery.rows.{batch}"
            start = batch * ROW_BATCH
            self.gws.request(
                request,
                partial(catalog_rows, self.gws.catalog, self.ids[start : start + ROW_BATCH]),
                partial(self._set_rows, batch),
                partial(self._row_error, batch),
            )
        return None

    def row_for_id(self, file_id: int) -> dict[str, Any] | None:
        pos = self._pos.get(file_id)
        return self.row_at(pos) if pos is not None else None

    def index_of(self, file_id: int) -> int:
        return self._pos.get(file_id, -1)

    def _set_rows(self, batch: int, rows: dict[int, dict[str, Any]]) -> None:
        self._pending.discard(batch)
        self._batches[batch] = rows
        while len(self._batches) > ROW_CACHE:
            self._batches.popitem(last=False)
        start = batch * ROW_BATCH
        end = min(start + ROW_BATCH, len(self.ids)) - 1
        if end >= start:
            self.dataChanged.emit(
                self.index(start), self.index(end), [RowRole, Qt.ItemDataRole.DecorationRole]
            )

    def _row_error(self, batch: int, message: str) -> None:
        self._set_rows(batch, {})
        self.load_failed.emit(message)

    # ------------------------------------------------------------------ data
    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        pos = index.row()
        if not 0 <= pos < len(self.ids):
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return self._names[pos]
        if role == FileIdRole:
            return self.ids[pos]
        if role == Qt.ItemDataRole.SizeHintRole:
            return QSize(self.icon_size.width() + 14, self.icon_size.height() + 32)
        if role not in (RowRole, Qt.ItemDataRole.DecorationRole, Qt.ItemDataRole.ToolTipRole):
            return None
        row = self.row_at(pos)
        if row is None:
            return None
        if role == RowRole:
            return row
        if role == Qt.ItemDataRole.DecorationRole:
            return self.thumbnail(row)
        if role == Qt.ItemDataRole.ToolTipRole:
            return row["rel_path"]
        return None

    def thumbnail(self, row: dict[str, Any]) -> QPixmap | None:
        key = thumbnail_key(row)
        pix = self._thumbs.get(key)
        if pix is not None:
            self._thumbs.move_to_end(key)
            return pix
        if key not in self._failed and self.gws.is_open:
            self.gws.thumb_loader.request(
                self.gws.catalog_uuid, self.gws.roots, row, meta_for_thumbnail(row), scope="gallery"
            )
        return None

    def has_thumbnail(self, file_id: int) -> bool:
        return any(key[0] == file_id for key in self._thumbs)

    def cached_thumbnail(self, pos: int) -> QPixmap | None:
        row = (
            self._batches.get(pos // ROW_BATCH, {}).get(self.ids[pos])
            if 0 <= pos < len(self.ids)
            else None
        )
        return self._thumbs.get(thumbnail_key(row)) if row is not None else None

    def _thumb_ready(self, key: ThumbKey, image: QImage) -> None:
        pos = self._pos.get(key[0])
        if pos is None:
            return
        pix = QPixmap.fromImage(image)
        old = self._thumbs.pop(key, None)
        if old is not None:
            self._thumb_bytes -= old.width() * old.height() * 4
        self._thumbs[key] = pix
        self._thumb_bytes += pix.width() * pix.height() * 4
        while self._thumb_bytes > THUMB_BUDGET and len(self._thumbs) > 1:
            _fid, old = self._thumbs.popitem(last=False)
            self._thumb_bytes -= old.width() * old.height() * 4
        idx = self.index(pos)
        self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.DecorationRole])

    def _thumb_failed(self, key: ThumbKey) -> None:
        self._failed.add(key)
        pos = self._pos.get(key[0])
        if pos is not None:
            idx = self.index(pos)
            self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.DecorationRole])

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
