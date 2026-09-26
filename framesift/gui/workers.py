"""Background work for the GUI.

Rule: Qt is touched only on the GUI thread. Worker threads (one `threading.Thread` per engine
job, `TaskPool` threads for thumbnails, image decodes and file actions) run plain Python and
hand their results to a `ResultQueue`; `GuiDispatcher` drains it from a GUI-thread timer and
emits the Qt signals there. Worker code holds no reference to any Qt object, so no Qt object is
created, used or released off the GUI thread. `GuiGarbageCollector` runs Python's cyclic
collector on the GUI thread only, for the same reason: automatic collection runs in whichever
thread happens to allocate, and would finalize Qt wrappers there.

This replaced QThreadPool/QRunnable and QThread workers, which crashed with heap corruption
under PySide6 6.11 on Windows (ARCHITECTURE.md decision 25)."""

from __future__ import annotations

import gc
import itertools
import queue
import threading
import time
import traceback
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import wait as wait_futures
from functools import partial
from pathlib import Path
from typing import Any

from PIL import Image
from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QImage

from framesift.engine.catalog import Catalog
from framesift.engine.config import Roots
from framesift.engine.imaging import open_full
from framesift.engine.jobs import Cancelled, JobControl
from framesift.engine.paths import os_path
from framesift.engine.thumbs import ThumbCache, render_thumbnail

# ---------------------------------------------------------------------- GUI-thread plumbing


class ResultQueue:
    """Thread-safe hand-off from worker threads to the GUI thread. Plain Python, no Qt."""

    def __init__(self) -> None:
        self._queue: queue.SimpleQueue[tuple[int, tuple[Any, ...]]] = queue.SimpleQueue()

    def put(self, key: int, *args: Any) -> None:
        self._queue.put((key, args))

    def get_nowait(self) -> tuple[int, tuple[Any, ...]]:
        return self._queue.get_nowait()


class GuiDispatcher(QObject):
    """Delivers worker results on the GUI thread to handlers registered by key."""

    BUSY_MS = 15
    IDLE_MS = 40
    IDLE_TICKS = 50
    BUDGET_S = 0.025

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.queue = ResultQueue()
        self._handlers: dict[int, Callable[..., None]] = {}
        self._keys = itertools.count(1)
        self._draining = False
        self._idle_ticks = 0
        self._timer = QTimer(self)
        self._timer.setInterval(self.BUSY_MS)
        self._timer.timeout.connect(self.drain)
        self._timer.start()

    def register(self, handler: Callable[..., None]) -> int:
        key = next(self._keys)
        self._handlers[key] = handler
        return key

    def unregister(self, key: int) -> None:
        self._handlers.pop(key, None)

    def drain(self) -> None:
        """Run queued results for up to BUDGET_S, keeping the GUI responsive."""
        if self._draining:  # a handler opened a nested event loop (a modal dialog)
            return
        self._draining = True
        delivered = 0
        deadline = time.monotonic() + self.BUDGET_S
        try:
            while time.monotonic() < deadline:
                try:
                    key, args = self.queue.get_nowait()
                except queue.Empty:
                    break
                delivered += 1
                handler = self._handlers.get(key)
                if handler is None:
                    continue
                try:
                    handler(*args)
                except Exception:
                    traceback.print_exc()
        finally:
            self._draining = False
        if delivered:
            self._idle_ticks = 0
            if self._timer.interval() != self.BUSY_MS:
                self._timer.setInterval(self.BUSY_MS)
        else:
            self._idle_ticks += 1
            if self._idle_ticks > self.IDLE_TICKS and self._timer.interval() != self.IDLE_MS:
                self._timer.setInterval(self.IDLE_MS)

    def shutdown(self) -> None:
        """Stop delivering; results still queued are dropped here, on the GUI thread."""
        self._timer.stop()
        self._handlers.clear()
        while True:
            try:
                self.queue.get_nowait()
            except queue.Empty:
                break


class GuiGarbageCollector(QObject):
    """Runs Python's cyclic garbage collector on the GUI thread only.

    Automatic collection is switched off and the interpreter's own thresholds are checked from
    a GUI-thread timer instead (the approach used by pyqtgraph and calibre)."""

    def __init__(self, parent: QObject | None = None, interval_ms: int = 500):
        super().__init__(parent)
        self.threshold = gc.get_threshold()
        gc.disable()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.check)
        self._timer.start(interval_ms)

    def check(self) -> None:
        count0, count1, count2 = gc.get_count()
        if count0 > self.threshold[0]:
            gc.collect(0)
            if count1 > self.threshold[1]:
                gc.collect(1)
                if count2 > self.threshold[2]:
                    gc.collect(2)


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


# ---------------------------------------------------------------------- engine jobs


def _run_job(
    fn: Callable[..., Any],
    control: JobControl,
    out: ResultQueue,
    key: int,
    done: threading.Event,
) -> None:
    """Job thread body: plain Python only, every result goes through `out`."""
    try:
        result = fn(progress=partial(out.put, key, "progress"), control=control)
        out.put(key, "ok", result)
    except Cancelled:
        out.put(key, "ok", None)
    except Exception as exc:
        traceback.print_exc()
        out.put(key, "failed", f"{type(exc).__name__}: {exc}")
    finally:
        done.set()
        out.put(key, "finished")


class JobThread(QObject):
    """Runs `fn(progress=cb, control=control)` on a Python thread and reports through signals.

    The signals are emitted on the GUI thread by the dispatcher; `fn` must not touch Qt."""

    progress = Signal(dict)
    finished_ok = Signal(object)
    failed = Signal(str)
    finished = Signal()  # always emitted last, after finished_ok or failed

    def __init__(
        self,
        fn: Callable[..., Any],
        dispatcher: GuiDispatcher,
        *,
        control: JobControl | None = None,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self.control = control or JobControl()
        self._fn: Callable[..., Any] | None = fn
        self._dispatcher = dispatcher
        self._key = dispatcher.register(self._deliver)
        self._done = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        fn, self._fn = self._fn, None
        if self._thread is not None or fn is None:
            return
        self._thread = threading.Thread(
            target=_run_job,
            args=(fn, self.control, self._dispatcher.queue, self._key, self._done),
            name="framesift-job",
            daemon=True,
        )
        self._thread.start()

    def _deliver(self, kind: str, *payload: Any) -> None:
        if kind == "progress":
            self.progress.emit(payload[0])
        elif kind == "ok":
            self.finished_ok.emit(payload[0])
        elif kind == "failed":
            self.failed.emit(payload[0])
        elif kind == "finished":
            self._dispatcher.unregister(self._key)
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


# ---------------------------------------------------------------------- thumbnails


def _render_thumb(
    cache: ThumbCache,
    catalog_uuid: str,
    roots: Roots,
    row: dict[str, Any],
    meta: dict[str, Any],
    closing: threading.Event,
    out: ResultQueue,
    key: int,
    file_id: int,
) -> None:
    """Thumbnail worker: JPEG bytes for one file, or None; plain Python only."""
    data: bytes | None = None
    if not closing.is_set():
        try:
            data = cache.get(catalog_uuid, file_id, row["size"], row["mtime_ns"])
            if data is None:
                path = os_path(roots.path(row["root"]), row["rel_path"], row.get("rel_path_os"))
                data = render_thumbnail(Path(path), meta)
                cache.put(catalog_uuid, file_id, row["size"], row["mtime_ns"], data)
        except Exception:
            data = None
    out.put(key, file_id, data)


class ThumbSignals(QObject):
    ready = Signal(int, QImage)
    failed = Signal(int)


class ThumbnailLoader(QObject):
    """Loads thumbnails through the on-disk cache in a bounded thread pool."""

    def __init__(
        self,
        cache: ThumbCache,
        dispatcher: GuiDispatcher,
        parent: QObject | None = None,
        threads: int = 4,
    ):
        super().__init__(parent)
        self.cache = cache
        self.signals = ThumbSignals(self)
        self.pool = TaskPool(threads, "framesift-thumb")
        self.pending: set[int] = set()
        self._closing = threading.Event()
        self._queue = dispatcher.queue
        self._key = dispatcher.register(self._deliver)

    def request(
        self, catalog: Catalog, roots: Roots, row: dict[str, Any], meta: dict[str, Any]
    ) -> None:
        file_id = int(row["id"])
        if file_id in self.pending or self._closing.is_set():
            return
        self.pending.add(file_id)
        task = partial(
            _render_thumb,
            self.cache,
            catalog.uuid,
            roots,
            dict(row),
            dict(meta),
            self._closing,
            self._queue,
            self._key,
            file_id,
        )
        if not self.pool.start(task):
            self.pending.discard(file_id)

    def _deliver(self, file_id: int, data: bytes | None) -> None:
        self.pending.discard(file_id)
        if self._closing.is_set():
            return
        image = QImage.fromData(data, "JPEG") if data else QImage()
        if image.isNull():
            self.signals.failed.emit(file_id)
        else:
            self.signals.ready.emit(file_id, image)

    def cancel_all(self) -> None:
        self.pool.clear()
        self.pending.clear()

    def shutdown(self, timeout_ms: int = 30000) -> None:
        """Stop feeding results into the GUI and wait for running tasks to finish."""
        self._closing.set()
        self.pool.shutdown(timeout_ms)


# ---------------------------------------------------------------------- full images


def _decode_image(
    path: Path,
    fmt: str | None,
    ext: str,
    orientation: int | None,
    max_side: int | None,
    closing: threading.Event,
    out: ResultQueue,
    key: int,
    request_key: tuple[int, bool],
    file_id: int,
) -> None:
    """Image worker: upright RGB pixels (fit to max_side when given); plain Python only."""
    if closing.is_set():
        out.put(key, request_key, file_id, None, "closing")
        return
    try:
        im = open_full(path, fmt, ext, orientation)
        full = True
        if max_side and max(im.size) > max_side:
            im = im.copy()
            im.thumbnail((max_side, max_side), Image.Resampling.BILINEAR)
            full = False
        if im.mode != "RGB":
            im = im.convert("RGB")
        frame = (im.tobytes("raw", "RGB"), im.width, im.height, full)
        out.put(key, request_key, file_id, frame, None)
    except Exception as exc:
        out.put(key, request_key, file_id, None, str(exc) or type(exc).__name__)


def rgb_to_qimage(data: bytes, width: int, height: int) -> QImage:
    """Owned QImage from packed RGB bytes (GUI thread)."""
    return QImage(data, width, height, width * 3, QImage.Format.Format_RGB888).copy()


def pil_to_qimage(im: Image.Image) -> QImage:
    if im.mode != "RGB":
        im = im.convert("RGB")
    return rgb_to_qimage(im.tobytes("raw", "RGB"), im.width, im.height)


class ImageSignals(QObject):
    ready = Signal(int, QImage, bool)  # file id, image, is_full_resolution
    failed = Signal(int, str)


class ImageLoader(QObject):
    """Full-size (or fit-size) decodes for the review view, with prefetch."""

    def __init__(self, dispatcher: GuiDispatcher, parent: QObject | None = None, threads: int = 3):
        super().__init__(parent)
        self.signals = ImageSignals(self)
        self.pool = TaskPool(threads, "framesift-image")
        self.pending: set[tuple[int, bool]] = set()
        self._closing = threading.Event()
        self._queue = dispatcher.queue
        self._key = dispatcher.register(self._deliver)

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
        if self._closing.is_set():
            return
        request_key = (file_id, max_side is None)
        if request_key in self.pending:
            return
        self.pending.add(request_key)
        task = partial(
            _decode_image,
            path,
            fmt,
            ext,
            orientation,
            max_side,
            self._closing,
            self._queue,
            self._key,
            request_key,
            file_id,
        )
        if not self.pool.start(task):
            self.pending.discard(request_key)

    def _deliver(
        self,
        request_key: tuple[int, bool],
        file_id: int,
        frame: tuple[bytes, int, int, bool] | None,
        error: str | None,
    ) -> None:
        self.pending.discard(request_key)
        if self._closing.is_set():
            return
        if frame is None:
            self.signals.failed.emit(file_id, error or "")
            return
        data, width, height, full = frame
        self.signals.ready.emit(file_id, rgb_to_qimage(data, width, height), full)

    def shutdown(self, timeout_ms: int = 30000) -> None:
        self._closing.set()
        self.pool.shutdown(timeout_ms)
