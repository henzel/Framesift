"""Background work for the GUI on plain Python threads.

Engine jobs run on `JobThread` (one `threading.Thread` each); thumbnails, image decodes and
file actions run on `TaskPool` (a thin wrapper over `concurrent.futures.ThreadPoolExecutor`).
Qt thread classes are avoided on purpose: Python QRunnable subclasses deleted by Qt from its
pool threads corrupted the heap under PySide6 6.11, and releasing QThread objects right after
`finished` crashed on Windows. Every Qt object here is created on the GUI thread; results
reach it through Qt signals, which Qt queues onto the GUI thread."""

from __future__ import annotations

import threading
import traceback
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import wait as wait_futures
from pathlib import Path
from typing import Any

from PIL import Image
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage

from framesift.engine.catalog import Catalog
from framesift.engine.config import Roots
from framesift.engine.imaging import DecodeError, open_full
from framesift.engine.jobs import Cancelled, JobControl
from framesift.engine.paths import os_path
from framesift.engine.thumbs import ThumbCache, render_thumbnail


class JobThread(QObject):
    """Runs `fn(progress=cb, control=control)` on a Python thread and reports through signals."""

    progress = Signal(dict)
    finished_ok = Signal(object)
    failed = Signal(str)
    finished = Signal()  # always emitted last, after finished_ok or failed

    def __init__(
        self,
        fn: Callable[..., Any],
        *,
        control: JobControl | None = None,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self.fn: Callable[..., Any] | None = fn
        self.control = control or JobControl()
        self.result: Any = None
        self._thread: threading.Thread | None = None
        self._done = threading.Event()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="framesift-job", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        fn = self.fn
        assert fn is not None
        try:
            self.result = fn(progress=self.progress.emit, control=self.control)
            self.finished_ok.emit(self.result)
        except Cancelled:
            self.finished_ok.emit(None)
        except Exception as exc:
            traceback.print_exc()
            self.failed.emit(f"{type(exc).__name__}: {exc}")
        finally:
            self._done.set()
            self.finished.emit()

    def is_running(self) -> bool:
        return self._thread is not None and not self._done.is_set()

    def is_finished(self) -> bool:
        return self._done.is_set()

    def wait(self, timeout_ms: int | None = None) -> bool:
        """Join the thread; True when it is not running any more."""
        thread = self._thread
        if thread is None:
            return True
        thread.join(None if timeout_ms is None else timeout_ms / 1000)
        return not thread.is_alive()

    def release(self) -> None:
        """Drop the job function and its result once the GUI has consumed them."""
        self.fn = None
        self.result = None


class TaskPool:
    """A bounded pool of plain Python threads with QThreadPool-like clear/wait semantics."""

    def __init__(self, threads: int, name: str):
        self._executor = ThreadPoolExecutor(max_workers=max(1, threads), thread_name_prefix=name)
        self._futures: set[Future[None]] = set()
        self._lock = threading.Lock()
        self._closed = False

    def start(self, fn: Callable[[], None]) -> bool:
        with self._lock:
            if self._closed:
                return False
            future = self._executor.submit(fn)
            self._futures.add(future)
        future.add_done_callback(self._forget)
        return True

    def _forget(self, future: Future[None]) -> None:
        with self._lock:
            self._futures.discard(future)

    def clear(self) -> None:
        """Drop the tasks that have not started yet."""
        with self._lock:
            futures = list(self._futures)
        for future in futures:
            future.cancel()

    def wait(self, timeout_ms: int | None = None) -> bool:
        """Wait for the tasks submitted so far; True when all of them finished in time."""
        with self._lock:
            futures = list(self._futures)
        if not futures:
            return True
        timeout = None if timeout_ms is None else timeout_ms / 1000
        _done, pending = wait_futures(futures, timeout=timeout)
        return not pending

    def shutdown(self, timeout_ms: int = 30000) -> bool:
        with self._lock:
            self._closed = True
        self.clear()
        finished = self.wait(timeout_ms)
        self._executor.shutdown(wait=False, cancel_futures=True)
        return finished


def pil_to_qimage(im: Image.Image) -> QImage:
    if im.mode != "RGB":
        im = im.convert("RGB")
    data = im.tobytes("raw", "RGB")
    qimg = QImage(data, im.width, im.height, im.width * 3, QImage.Format.Format_RGB888)
    return qimg.copy()  # detach from the Python buffer


class ThumbSignals(QObject):
    ready = Signal(int, QImage)
    failed = Signal(int)


class ThumbTask:
    def __init__(
        self,
        signals: ThumbSignals,
        cache: ThumbCache,
        catalog_uuid: str,
        roots: Roots,
        row: dict[str, Any],
        meta: dict[str, Any],
    ):
        self.signals, self.cache, self.uuid, self.roots, self.row, self.meta = (
            signals,
            cache,
            catalog_uuid,
            roots,
            row,
            meta,
        )

    def run(self) -> None:
        row = self.row
        file_id = int(row["id"])
        if getattr(self.signals, "closing", False):
            return
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
            if not getattr(self.signals, "closing", False):
                self.signals.ready.emit(file_id, img)
        except Exception:
            if not getattr(self.signals, "closing", False):
                self.signals.failed.emit(file_id)


class ThumbnailLoader(QObject):
    """Loads thumbnails through the on-disk cache in a bounded thread pool."""

    def __init__(self, cache: ThumbCache, parent: QObject | None = None, threads: int = 4):
        super().__init__(parent)
        self.cache = cache
        self.signals = ThumbSignals()
        self.pool = TaskPool(threads, "framesift-thumb")
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
        if not self.pool.start(task.run):
            self.pending.discard(file_id)

    def _done(self, file_id: int, *_args: Any) -> None:
        self.pending.discard(file_id)

    def cancel_all(self) -> None:
        self.pool.clear()
        self.pending.clear()

    def shutdown(self, timeout_ms: int = 30000) -> None:
        """Stop feeding results into the GUI and wait for running tasks to finish."""
        self.signals.closing = True  # type: ignore[attr-defined]
        self.pool.shutdown(timeout_ms)


class ImageSignals(QObject):
    ready = Signal(int, QImage, bool)  # file id, image, is_full_resolution
    failed = Signal(int, str)


class ImageTask:
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
        if getattr(self.signals, "closing", False):
            return
        try:
            im = open_full(self.path, self.fmt, self.ext, self.orientation)
            full = True
            if self.max_side and max(im.size) > self.max_side:
                im = im.copy()
                im.thumbnail((self.max_side, self.max_side), Image.Resampling.BILINEAR)
                full = False
            if not getattr(self.signals, "closing", False):
                self.signals.ready.emit(self.file_id, pil_to_qimage(im), full)
        except Exception as exc:
            if not getattr(self.signals, "closing", False):
                self.signals.failed.emit(self.file_id, str(exc))


class ImageLoader(QObject):
    """Full-size (or fit-size) decodes for the review view, with prefetch."""

    def __init__(self, parent: QObject | None = None, threads: int = 3):
        super().__init__(parent)
        self.signals = ImageSignals()
        self.pool = TaskPool(threads, "framesift-image")
        self.pending: set[tuple[int, bool]] = set()
        self.signals.ready.connect(lambda fid, _img, full: self.pending.discard((fid, full)))
        self.signals.failed.connect(
            lambda fid, _msg: (
                self.pending.discard((fid, True)) or self.pending.discard((fid, False))
            )
        )

    def shutdown(self, timeout_ms: int = 30000) -> None:
        self.signals.closing = True  # type: ignore[attr-defined]
        self.pool.shutdown(timeout_ms)

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
        if getattr(self.signals, "closing", False):
            return
        key = (file_id, max_side is None)
        if key in self.pending:
            return
        self.pending.add(key)
        task = ImageTask(self.signals, file_id, path, fmt, ext, orientation, max_side)
        if not self.pool.start(task.run):
            self.pending.discard(key)
