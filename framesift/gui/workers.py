"""Threads: engine jobs in a QThread, thumbnails and image decodes in a QThreadPool."""

from __future__ import annotations

import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PIL import Image
from PySide6.QtCore import QObject, QRunnable, QThread, QThreadPool, Signal
from PySide6.QtGui import QImage

from framesift.engine.catalog import Catalog
from framesift.engine.config import Roots
from framesift.engine.imaging import DecodeError, apply_orientation, open_full
from framesift.engine.jobs import Cancelled, JobControl
from framesift.engine.paths import os_path
from framesift.engine.thumbs import ThumbCache, render_thumbnail


class JobThread(QThread):
    """Runs `fn(progress=cb, control=control)` off the GUI thread."""

    progress = Signal(dict)
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        fn: Callable[..., Any],
        *,
        control: JobControl | None = None,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self.fn = fn
        self.control = control or JobControl()
        self.result: Any = None

    def run(self) -> None:  # pragma: no cover - exercised through the GUI tests
        try:
            self.result = self.fn(progress=self.progress.emit, control=self.control)
            self.finished_ok.emit(self.result)
        except Cancelled:
            self.finished_ok.emit(None)
        except Exception as exc:
            traceback.print_exc()
            self.failed.emit(f"{type(exc).__name__}: {exc}")


def pil_to_qimage(im: Image.Image) -> QImage:
    if im.mode != "RGB":
        im = im.convert("RGB")
    data = im.tobytes("raw", "RGB")
    qimg = QImage(data, im.width, im.height, im.width * 3, QImage.Format.Format_RGB888)
    return qimg.copy()  # detach from the Python buffer


class ThumbSignals(QObject):
    ready = Signal(int, QImage)
    failed = Signal(int)


class ThumbTask(QRunnable):
    def __init__(
        self,
        signals: ThumbSignals,
        cache: ThumbCache,
        catalog_uuid: str,
        roots: Roots,
        row: dict[str, Any],
        meta: dict[str, Any],
    ):
        super().__init__()
        self.signals, self.cache, self.uuid, self.roots, self.row, self.meta = (
            signals,
            cache,
            catalog_uuid,
            roots,
            row,
            meta,
        )
        self.setAutoDelete(True)

    def run(self) -> None:
        row = self.row
        file_id = int(row["id"])
        try:
            data = self.cache.get(self.uuid, file_id, row["size"], row["mtime_ns"])
            if data is None:
                path = os_path(
                    self.roots.path(row["root"]), row["rel_path"], row.get("rel_path_os")
                )
                data = render_thumbnail(Path(path), self.meta)
                self.cache.put(self.uuid, file_id, row["size"], row["mtime_ns"], data)
            img = QImage.fromData(data, "JPEG")
            if img.isNull():
                raise DecodeError("bad thumbnail data")
            self.signals.ready.emit(file_id, img)
        except Exception:
            self.signals.failed.emit(file_id)


class ThumbnailLoader(QObject):
    """Loads thumbnails through the on-disk cache in a bounded thread pool."""

    def __init__(self, cache: ThumbCache, parent: QObject | None = None, threads: int = 4):
        super().__init__(parent)
        self.cache = cache
        self.signals = ThumbSignals()
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(threads)
        self.pending: set[int] = set()
        self.signals.ready.connect(self._done)
        self.signals.failed.connect(self._done)

    def request(
        self, catalog: Catalog, roots: Roots, row: dict[str, Any], meta: dict[str, Any]
    ) -> None:
        file_id = int(row["id"])
        if file_id in self.pending:
            return
        self.pending.add(file_id)
        task = ThumbTask(self.signals, self.cache, catalog.uuid, roots, row, meta)
        self.pool.start(task)

    def _done(self, file_id: int, *_args: Any) -> None:
        self.pending.discard(file_id)

    def cancel_all(self) -> None:
        self.pool.clear()
        self.pending.clear()


class ImageSignals(QObject):
    ready = Signal(int, QImage, bool)  # file id, image, is_full_resolution
    failed = Signal(int, str)


class ImageTask(QRunnable):
    def __init__(
        self,
        signals: ImageSignals,
        file_id: int,
        path: Path,
        fmt: str | None,
        ext: str,
        orientation: int | None,
        max_side: int | None,
    ):
        super().__init__()
        (
            self.signals,
            self.file_id,
            self.path,
            self.fmt,
            self.ext,
            self.orientation,
            self.max_side,
        ) = signals, file_id, path, fmt, ext, orientation, max_side

    def run(self) -> None:
        try:
            im = open_full(self.path, self.fmt, self.ext)
            im = apply_orientation(im, self.orientation)
            full = True
            if self.max_side and max(im.size) > self.max_side:
                im = im.copy()
                im.thumbnail((self.max_side, self.max_side), Image.Resampling.BILINEAR)
                full = False
            self.signals.ready.emit(self.file_id, pil_to_qimage(im), full)
        except Exception as exc:
            self.signals.failed.emit(self.file_id, str(exc))


class ImageLoader(QObject):
    """Full-size (or fit-size) decodes for the review view, with prefetch."""

    def __init__(self, parent: QObject | None = None, threads: int = 3):
        super().__init__(parent)
        self.signals = ImageSignals()
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(threads)
        self.pending: set[tuple[int, bool]] = set()
        self.signals.ready.connect(lambda fid, _img, full: self.pending.discard((fid, full)))
        self.signals.failed.connect(
            lambda fid, _msg: (
                self.pending.discard((fid, True)) or self.pending.discard((fid, False))
            )
        )

    def request(
        self,
        file_id: int,
        path: Path,
        fmt: str | None,
        ext: str,
        orientation: int | None,
        *,
        max_side: int | None,
    ) -> None:
        key = (file_id, max_side is None)
        if key in self.pending:
            return
        self.pending.add(key)
        self.pool.start(ImageTask(self.signals, file_id, path, fmt, ext, orientation, max_side))
