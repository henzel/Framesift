"""GUI-side workspace: the open catalog, background jobs, serialized file actions, loaders."""

from __future__ import annotations

import traceback
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from functools import partial
from itertools import count
from pathlib import Path
from threading import Thread
from typing import Any

from PySide6.QtCore import QObject, Signal

from framesift import __version__
from framesift.engine import actions, api
from framesift.engine.catalog import Catalog
from framesift.engine.config import ClassifyConfig, Roots
from framesift.engine.jobs import JobControl
from framesift.engine.paths import os_path
from framesift.engine.thumbs import ThumbCache
from framesift.gui.prefs import Prefs
from framesift.gui.workers import (
    GuiDispatcher,
    GuiGarbageCollector,
    ImageLoader,
    JobThread,
    ResultQueue,
    TaskPool,
    ThumbnailLoader,
)


def _run_action(fn: Callable[[], Any], out: ResultQueue, key: int, tag: Any) -> None:
    """Action worker (one thread, submission order): plain Python only."""
    try:
        result = fn()
    except Exception as exc:
        traceback.print_exc()
        out.put(key, tag, False, f"{type(exc).__name__}: {exc}")
    else:
        out.put(key, tag, True, result)


def _read_result(fn: Callable[[], Any], out: ResultQueue, key: int, name: str, ticket: int) -> None:
    try:
        result = fn()
    except Exception as exc:
        out.put(key, name, ticket, False, str(exc) or type(exc).__name__)
    else:
        out.put(key, name, ticket, True, result)


def _replace_workspace(
    old: api.Workspace | None,
    jobs: list[Thread],
    actions_pool: TaskPool,
    reads_pool: TaskPool,
    media_pools: tuple[TaskPool, ...],
    opener: Callable[[], api.Workspace] | None,
    out: ResultQueue,
    key: int,
) -> None:
    try:
        for thread in jobs:
            thread.join()
        actions_pool.wait()
        reads_pool.wait()
        for pool in media_pools:
            pool.wait()
        if old is not None:
            old.close()
        if opener is not None:
            workspace = opener()
            try:
                populated = bool(
                    workspace.catalog.scalar(
                        "SELECT EXISTS(SELECT 1 FROM files WHERE status='present')"
                    )
                )
                out.put(key, workspace, workspace.catalog.uuid, populated, None)
            except Exception:
                workspace.close()
                raise
            return
    except Exception as exc:
        out.put(key, None, "", False, exc.with_traceback(None))
    else:
        out.put(key, None, "", False, None)


def _job_after_actions(actions_pool: TaskPool, fn: Callable[..., Any], **kwargs: Any) -> Any:
    actions_pool.wait()
    kwargs["control"].check()
    return fn(**kwargs)


def _read_after_actions(actions_pool: TaskPool, fn: Callable[[], Any]) -> Any:
    actions_pool.wait()
    return fn()


def workspace_counters(ws: api.Workspace | None) -> dict[str, int]:
    if ws is None or ws._session_id is None:
        return {"kept": 0, "to_delete": 0, "bytes_to_delete": 0, "restored": 0}
    return ws.catalog.session_counters(ws._session_id)


@dataclass
class SelectionResult:
    done: int = 0
    cancelled: bool = False
    errors: list[str] = field(default_factory=list)


def _apply_selection(
    ws: api.Workspace, action: str, ids: list[int], *, progress, control: JobControl
) -> SelectionResult:
    method = {"keep": actions.keep, "to_delete": actions.to_delete, "restore": actions.restore}[
        action
    ]
    result = SelectionResult()
    session = ws.session_id
    progress({"done": 0, "total": len(ids)})
    for i, fid in enumerate(ids, 1):
        if control.cancel.is_set():
            result.cancelled = True
            break
        try:
            outcome = method(ws.catalog, ws.roots, fid, session)
        except Exception as exc:
            result.errors.append(f"{fid}: {exc}")
        else:
            result.errors.extend(getattr(outcome, "errors", []))
        result.done = i
        if i % 25 == 0 or i == len(ids):
            progress({"done": i, "total": len(ids)})
    return result


class GuiWorkspace(QObject):
    """Owns the engine workspace for the window. All engine calls go through here."""

    opened = Signal()
    open_failed = Signal(object)
    closed = Signal()
    state_changed = Signal()
    settled = Signal()  # a workspace transition has finished
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
        self.catalog_uuid = ""
        self.has_catalog_files = False
        self.opening = False
        self._shutting_down = False
        self._shut = False
        limit = int(float(prefs.get("cache_limit_gb", 2.0)) * 1024**3)
        self.thumb_cache = ThumbCache(cache_dir, limit_bytes=max(limit, 64 * 1024 * 1024))
        # Qt only on the GUI thread: workers hand results to the dispatcher, and the cyclic
        # garbage collector runs here too (see framesift/gui/workers.py).
        self.dispatcher = GuiDispatcher(self)
        self._gc = GuiGarbageCollector(self)
        self.thumb_loader = ThumbnailLoader(self.thumb_cache, self.dispatcher, self)
        self.image_loader = ImageLoader(self.dispatcher, self)
        self._jobs: dict[str, JobThread] = {}
        self._live_threads: list[JobThread] = []
        self._actions = TaskPool(1, "framesift-action")  # one thread: actions stay ordered
        self._action_key = self.dispatcher.register(self._on_action_event)
        self._reads = TaskPool(3, "framesift-read")
        self._reads_key = self.dispatcher.register(self._on_read)
        self._tickets = count()
        self._requests: dict[str, tuple[int, Future, Callable, Callable | None]] = {}
        self._lifecycle = TaskPool(1, "framesift-workspace")
        self._lifecycle_key = self.dispatcher.register(self._on_workspace)
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

    @property
    def busy(self) -> bool:
        return bool(self._jobs)

    @property
    def can_write(self) -> bool:
        return self.is_open and not self.read_only and not self.busy

    @property
    def media_busy(self) -> bool:
        return not (self.thumb_loader.pool.wait(0) and self.image_loader.pool.wait(0))

    def open_source(
        self,
        source: Path,
        review: Path | None = None,
        delete: Path | None = None,
        *,
        include_subfolders: bool = True,
        force_lock: bool = False,
        read_only: bool = False,
    ) -> bool:
        opener = partial(
            api.open_workspace,
            source,
            review,
            delete,
            include_subfolders=include_subfolders,
            force_lock=force_lock,
            read_only=read_only,
            app=f"gui {__version__}",
            session_kind="manual",
        )
        return self._transition(opener)

    def _on_workspace(
        self, ws: api.Workspace | None, catalog_uuid: str, populated: bool, error: Exception | None
    ) -> None:
        self.opening = False
        self.ws = ws
        self.catalog_uuid = catalog_uuid
        self.has_catalog_files = populated
        if error is not None and not self._shutting_down:
            self.open_failed.emit(error)
        elif ws is not None and not self._shutting_down:
            self._finish_open(ws)
        self.state_changed.emit()
        self.settled.emit()

    def _finish_open(self, ws: api.Workspace) -> None:
        self.last_issues = ws.issues
        self.prefs.set_source_settings(
            str(ws.roots.source),
            review=str(ws.roots.review),
            delete=str(ws.roots.delete),
            include_subfolders=ws.roots.include_subfolders,
        )
        self.opened.emit()

    def _transition(self, opener: Callable[[], api.Workspace] | None) -> bool:
        if self.opening or self._shut:
            return False
        self.opening = True
        old, self.ws = self.ws, None
        for job in self._live_threads:
            job.control.cancel.set()
            job.control.resume()
        threads = [job.thread for job in self._live_threads if job.thread is not None]
        self._jobs.clear()
        self.cancel_requests()
        self.thumb_loader.cancel_all()
        self.image_loader.cancel_all()
        if old is not None:
            self.closed.emit()
        self.state_changed.emit()
        self._lifecycle.start(
            partial(
                _replace_workspace,
                old,
                threads,
                self._actions,
                self._reads,
                (self.thumb_loader.pool, self.image_loader.pool) if opener is None else (),
                opener,
                self.dispatcher.queue,
                self._lifecycle_key,
            )
        )
        return True

    def close_async(self) -> bool:
        return self._transition(None)

    def shutdown(self) -> None:
        if self._shut:
            return
        self._shutting_down = True
        self._lifecycle.wait()
        self.dispatcher.drain()
        if (self.ws is not None or self.media_busy) and self.close_async():
            self._lifecycle.wait()
            self.dispatcher.drain()
        self._shut = True
        self.thumb_loader.shutdown()
        self.image_loader.shutdown()
        self._actions.shutdown(10000)
        self._reads.shutdown(10000)
        self._lifecycle.shutdown(10000)
        self.dispatcher.shutdown()
        self.thumb_cache.close()

    # ------------------------------------------------------------------ non-blocking catalog reads
    def request(
        self,
        name: str,
        work: Callable[[], Any],
        ready: Callable[[Any], None],
        failed: Callable[[str], None] | None = None,
        *,
        after_actions: bool = False,
    ) -> None:
        previous = self._requests.pop(name, None)
        if previous is not None:
            previous[1].cancel()
        ticket = next(self._tickets)
        if after_actions:
            work = partial(_read_after_actions, self._actions, work)
        future = self._reads.submit(
            partial(_read_result, work, self.dispatcher.queue, self._reads_key, name, ticket)
        )
        if future is not None:
            self._requests[name] = (ticket, future, ready, failed)

    def cancel_requests(self, prefix: str = "") -> None:
        for name in list(self._requests):
            if name.startswith(prefix):
                self._requests.pop(name)[1].cancel()

    def _on_read(self, name: str, ticket: int, ok: bool, payload: Any) -> None:
        request = self._requests.get(name)
        if request is None or ticket != request[0]:
            return
        del self._requests[name]
        if ok:
            request[2](payload)
        elif request[3] is not None:
            request[3](payload)
        else:
            self.action_failed.emit(("read", name), payload)

    # ------------------------------------------------------------------ jobs
    def run_job(self, kind: str, fn: Callable[..., Any]) -> JobThread | None:
        if not self.can_write:
            return None
        thread = JobThread(
            partial(_job_after_actions, self._actions, fn), self.dispatcher, parent=self
        )
        thread.progress.connect(lambda data: self._job_progress(kind, thread, data))
        thread.finished_ok.connect(lambda result: self._job_done(kind, thread, result))
        thread.failed.connect(lambda msg: self._job_fail(kind, thread, msg))
        thread.finished.connect(lambda k=kind, t=thread: self._thread_finished(k, t))
        self._jobs[kind] = thread
        self._live_threads.append(thread)
        thread.start()
        self.state_changed.emit()
        return thread

    def _thread_finished(self, kind: str, thread: JobThread) -> None:
        if self._jobs.get(kind) is thread:
            self._jobs.pop(kind, None)
            self.state_changed.emit()
        if thread in self._live_threads:
            self._live_threads.remove(thread)
        thread.progress.disconnect()
        thread.finished_ok.disconnect()
        thread.failed.disconnect()
        thread.finished.disconnect()
        thread.deleteLater()

    def _job_progress(self, kind: str, thread: JobThread, data: dict[str, Any]) -> None:
        if self._jobs.get(kind) is thread:
            self.job_progress.emit(kind, data)

    def _job_done(self, kind: str, thread: JobThread, result: Any) -> None:
        if self._jobs.get(kind) is thread:
            self.job_finished.emit(kind, result)
            self.changed.emit()

    def _job_fail(self, kind: str, thread: JobThread, message: str) -> None:
        if self._jobs.get(kind) is thread:
            self.job_failed.emit(kind, message)

    def job(self, kind: str) -> JobThread | None:
        thread = self._jobs.get(kind)
        if thread is not None and thread.is_finished():
            return None
        return thread

    def scan(self) -> JobThread | None:
        ws = self.ws
        if ws is None or ws.catalog.read_only:
            return None

        threads = self._threads()

        def fn(progress: Callable[[dict], None], control: JobControl):
            return api.run_scan(ws, progress=progress, control=control, threads=threads)

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
    def submit_action(self, tag: Any, fn: Callable[[], Any]) -> bool:
        if not self.can_write:
            return False
        return self._actions.start(
            partial(_run_action, fn, self.dispatcher.queue, self._action_key, (self.ws, tag))
        )

    def _on_action_event(self, tag: Any, ok: bool, payload: Any) -> None:
        origin, tag = tag
        if origin is not self.ws:
            return
        if ok:
            self.action_done.emit(tag, payload)
            self.changed.emit()
        else:
            self.action_failed.emit(tag, payload)

    def keep(self, item_id: int) -> None:
        ws = self.ws
        assert ws is not None
        self.submit_action(
            ("keep", item_id), lambda: actions.keep(ws.catalog, ws.roots, item_id, ws.session_id)
        )

    def to_delete(self, item_id: int) -> None:
        ws = self.ws
        assert ws is not None
        self.submit_action(
            ("to_delete", item_id),
            lambda: actions.to_delete(ws.catalog, ws.roots, item_id, ws.session_id),
        )

    def restore(self, item_id: int) -> None:
        ws = self.ws
        assert ws is not None
        self.submit_action(
            ("restore", item_id),
            lambda: actions.restore(ws.catalog, ws.roots, item_id, ws.session_id),
        )

    def apply_selection(self, action: str, ids: list[int]) -> None:
        if not ids or not self.can_write:
            return
        if len(ids) == 1:
            method = {"keep": self.keep, "to_delete": self.to_delete, "restore": self.restore}[
                action
            ]
            method(ids[0])
        else:
            self.run_job("selection", partial(_apply_selection, self.ws, action, ids))

    def undo_last(self) -> None:
        ws = self.ws
        assert ws is not None
        self.submit_action(
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
        return workspace_counters(self.ws)

    def candidate_for(self, file_id: int) -> dict[str, Any] | None:
        row = self.catalog.one(
            "SELECT * FROM candidates WHERE file_id=? ORDER BY id DESC LIMIT 1", (file_id,)
        )
        return dict(row) if row else None
