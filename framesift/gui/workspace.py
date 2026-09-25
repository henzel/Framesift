"""GUI-side workspace: the open catalog, background jobs, serialized file actions, loaders."""

from __future__ import annotations

import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal

from framesift import __version__
from framesift.engine import actions, api
from framesift.engine.catalog import Catalog, LockHeld
from framesift.engine.config import ClassifyConfig, Roots
from framesift.engine.jobs import JobControl
from framesift.engine.paths import os_path
from framesift.engine.thumbs import ThumbCache
from framesift.gui.prefs import Prefs
from framesift.gui.workers import ImageLoader, JobThread, TaskPool, ThumbnailLoader


class _ActionSignals(QObject):
    done = Signal(object, object)  # tag, result
    failed = Signal(object, str)


class _ActionTask:
    def __init__(self, signals: _ActionSignals, tag: Any, fn: Callable[[], Any]):
        self.signals, self.tag, self.fn = signals, tag, fn

    def run(self) -> None:
        try:
            self.signals.done.emit(self.tag, self.fn())
        except Exception as exc:
            traceback.print_exc()
            self.signals.failed.emit(self.tag, f"{type(exc).__name__}: {exc}")


class GuiWorkspace(QObject):
    """Owns the engine workspace for the window. All engine calls go through here."""

    opened = Signal()
    closed = Signal()
    changed = Signal()  # files moved / marks changed: views should refresh
    scan_batch = Signal()  # new rows arrived during a scan
    job_progress = Signal(str, dict)
    job_finished = Signal(str, object)
    job_failed = Signal(str, str)
    action_done = Signal(object, object)
    action_failed = Signal(object, str)

    def __init__(self, prefs: Prefs, cache_dir: Path | None = None, parent: QObject | None = None):
        super().__init__(parent)
        self.prefs = prefs
        self.ws: api.Workspace | None = None
        limit = int(float(prefs.get("cache_limit_gb", 2.0)) * 1024**3)
        self.thumb_cache = ThumbCache(cache_dir, limit_bytes=max(limit, 64 * 1024 * 1024))
        self.thumb_loader = ThumbnailLoader(self.thumb_cache, self)
        self.image_loader = ImageLoader(self)
        self._jobs: dict[str, JobThread] = {}
        self._live_threads: list[JobThread] = []
        self._actions = TaskPool(1, "framesift-action")  # one thread: actions stay ordered
        self._action_signals = _ActionSignals()
        self._action_signals.done.connect(self._on_action_done)
        self._action_signals.failed.connect(self.action_failed)
        self.last_issues: list = []

    # ------------------------------------------------------------------ open / close
    @property
    def catalog(self) -> Catalog:
        assert self.ws is not None
        return self.ws.catalog

    @property
    def roots(self) -> Roots:
        assert self.ws is not None
        return self.ws.roots

    @property
    def is_open(self) -> bool:
        return self.ws is not None

    @property
    def read_only(self) -> bool:
        return self.ws is not None and self.ws.catalog.read_only

    def open_source(
        self,
        source: Path,
        review: Path | None = None,
        delete: Path | None = None,
        *,
        include_subfolders: bool = True,
        force_lock: bool = False,
        read_only: bool = False,
    ) -> None:
        """Raises WorkspaceError (refused roots) or LockHeld; the window turns those into dialogs."""
        self.close()
        try:
            ws = api.open_workspace(
                source,
                review,
                delete,
                include_subfolders=include_subfolders,
                force_lock=force_lock,
                read_only=read_only,
                app=f"gui {__version__}",
                session_kind="manual",
            )
        except LockHeld:
            raise
        self.ws = ws
        self.last_issues = ws.issues
        self.prefs.set_source_settings(
            str(ws.roots.source),
            review=str(ws.roots.review),
            delete=str(ws.roots.delete),
            include_subfolders=include_subfolders,
        )
        self.opened.emit()

    def close(self) -> None:
        for job in list(self._jobs.values()):
            job.control.cancel.set()
        for job in list(self._live_threads):
            job.wait(10000)
        self._jobs.clear()
        self._live_threads.clear()
        self.thumb_loader.cancel_all()
        self._actions.wait(10000)
        if self.ws is not None:
            self.ws.close()
            self.ws = None
            self.closed.emit()

    def shutdown(self) -> None:
        if getattr(self, "_shut", False):
            return
        self._shut = True
        self.thumb_loader.shutdown()
        self.image_loader.shutdown()
        self.close()
        self._actions.shutdown(10000)
        self.thumb_cache.close()

    # ------------------------------------------------------------------ jobs
    def run_job(self, kind: str, fn: Callable[..., Any]) -> JobThread | None:
        previous = self._jobs.get(kind)
        if previous is not None:
            if previous.isRunning() and not previous.isFinished():
                return None
            previous.wait(5000)
            self._jobs.pop(kind, None)
        # No Qt parent: the Python reference in _jobs keeps the thread alive until it has
        # fully finished (a parented QThread collected mid-shutdown crashed on Windows).
        thread = JobThread(fn)
        thread.progress.connect(lambda data, k=kind: self.job_progress.emit(k, data))
        thread.finished_ok.connect(lambda result, k=kind: self._job_done(k, result))
        thread.failed.connect(lambda msg, k=kind: self._job_fail(k, msg))
        thread.finished.connect(lambda k=kind, t=thread: self._thread_finished(k, t))
        self._jobs[kind] = thread
        self._live_threads.append(thread)
        thread.start()
        return thread

    def _thread_finished(self, kind: str, thread: JobThread) -> None:
        if self._jobs.get(kind) is thread:
            self._jobs.pop(kind, None)
        if thread in self._live_threads:
            self._live_threads.remove(thread)

    def _job_done(self, kind: str, result: Any) -> None:
        self.job_finished.emit(kind, result)
        self.changed.emit()

    def _job_fail(self, kind: str, message: str) -> None:
        self.job_failed.emit(kind, message)

    def job(self, kind: str) -> JobThread | None:
        thread = self._jobs.get(kind)
        if thread is not None and thread.isFinished():
            return None
        return thread

    def scan(self) -> JobThread | None:
        ws = self.ws
        if ws is None or ws.catalog.read_only:
            return None

        def fn(progress: Callable[[dict], None], control: JobControl):
            return api.run_scan(ws, progress=progress, control=control, threads=self._threads())

        return self.run_job("scan", fn)

    def classify(self, cfg: ClassifyConfig) -> JobThread | None:
        ws = self.ws
        if ws is None or ws.catalog.read_only:
            return None
        workers = int(self.prefs.get("workers", 0)) or None
        low = bool(self.prefs.get("low_priority", False))

        def fn(progress: Callable[[dict], None], control: JobControl):
            return api.run_classify(
                ws, cfg, progress=progress, control=control, workers=workers, low_priority=low
            )

        return self.run_job("classify", fn)

    def _threads(self) -> int | None:
        workers = int(self.prefs.get("workers", 0))
        return workers or None

    # ------------------------------------------------------------------ item actions (serialized)
    def _submit(self, tag: Any, fn: Callable[[], Any]) -> None:
        self._actions.start(_ActionTask(self._action_signals, tag, fn).run)

    def _on_action_done(self, tag: Any, result: Any) -> None:
        self.action_done.emit(tag, result)
        self.changed.emit()

    def keep(self, item_id: int) -> None:
        ws = self.ws
        assert ws is not None
        self._submit(
            ("keep", item_id), lambda: actions.keep(ws.catalog, ws.roots, item_id, ws.session_id)
        )

    def to_delete(self, item_id: int) -> None:
        ws = self.ws
        assert ws is not None
        self._submit(
            ("to_delete", item_id),
            lambda: actions.to_delete(ws.catalog, ws.roots, item_id, ws.session_id),
        )

    def restore(self, item_id: int) -> None:
        ws = self.ws
        assert ws is not None
        self._submit(
            ("restore", item_id),
            lambda: actions.restore(ws.catalog, ws.roots, item_id, ws.session_id),
        )

    def undo_last(self) -> None:
        ws = self.ws
        assert ws is not None
        self._submit(
            ("undo_last", None), lambda: actions.undo_last(ws.catalog, ws.roots, ws.session_id)
        )

    def wait_actions(self, ms: int = 10000) -> bool:
        return self._actions.wait(ms)

    # ------------------------------------------------------------------ helpers
    def file_row(self, file_id: int) -> dict[str, Any] | None:
        row = self.catalog.file(file_id)
        return dict(row) if row else None

    def meta(self, file_id: int) -> dict[str, Any]:
        row = self.catalog.meta(file_id)
        return dict(row) if row else {}

    def abs_path(self, row: dict[str, Any]) -> Path:
        return os_path(self.roots.path(row["root"]), row["rel_path"], row.get("rel_path_os"))

    def session_counters(self) -> dict[str, Any]:
        if self.ws is None or self.ws._session_id is None:
            return {"kept": 0, "to_delete": 0, "bytes_to_delete": 0, "restored": 0}
        return self.catalog.session_counters(self.ws.session_id)

    def candidate_for(self, file_id: int) -> dict[str, Any] | None:
        row = self.catalog.one(
            "SELECT * FROM candidates WHERE file_id=? ORDER BY id DESC LIMIT 1", (file_id,)
        )
        return dict(row) if row else None
