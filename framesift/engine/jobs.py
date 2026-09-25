"""Job runner: progress persistence, ETA, pause/cancel, low priority, tail-friendly logging."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from framesift.engine.catalog import Catalog

log = logging.getLogger("framesift")


class Cancelled(Exception):
    pass


class JobControl:
    """Shared cancel/pause flags. `check()` blocks while paused and raises Cancelled when cancelled."""

    def __init__(self) -> None:
        self.cancel = threading.Event()
        self._resume = threading.Event()
        self._resume.set()

    @property
    def paused(self) -> bool:
        return not self._resume.is_set()

    def pause(self) -> None:
        self._resume.clear()

    def resume(self) -> None:
        self._resume.set()

    def check(self) -> None:
        while not self._resume.wait(0.2):
            if self.cancel.is_set():
                raise Cancelled()
        if self.cancel.is_set():
            raise Cancelled()


def setup_logging(log_file: Path | None, level: int = logging.INFO) -> None:
    root = logging.getLogger("framesift")
    root.setLevel(level)
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        for h in list(root.handlers):
            if isinstance(h, logging.FileHandler) and Path(h.baseFilename) == log_file:
                return
        handler = logging.FileHandler(log_file, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        root.addHandler(handler)


def format_eta(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "?"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


class JobRunner:
    """Context manager around one engine job: creates the row, persists progress, finishes it."""

    def __init__(
        self,
        catalog: Catalog,
        kind: str,
        session_id: int | None,
        params: dict[str, Any] | None = None,
        *,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
        control: JobControl | None = None,
        throttle: float = 1.0,
    ):
        self.catalog = catalog
        self.kind = kind
        self.session_id = session_id
        self.params = params or {}
        self.on_progress = on_progress
        self.control = control or JobControl()
        self.throttle = throttle
        self.job_id: int | None = None
        self.started = time.monotonic()
        self._last_write = 0.0
        self._last_log = 0.0
        self.progress: dict[str, Any] = {}
        self.progress_path = catalog.dir / "progress.json"

    def __enter__(self) -> JobRunner:
        self.job_id = self.catalog.create_job(self.kind, self.session_id, self.params)
        log.info(
            "job %s #%s started %s",
            self.kind,
            self.job_id,
            json.dumps(self.params, ensure_ascii=False),
        )
        self.report({"stage": "start"}, force=True)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if exc_type is None:
            self.finish("done")
        elif exc_type is Cancelled or exc_type is KeyboardInterrupt:
            self.finish("cancelled")
            return exc_type is Cancelled
        else:
            self.finish("failed", error=f"{exc_type.__name__}: {exc}")
        return False

    def report(self, update: dict[str, Any], *, force: bool = False) -> None:
        self.progress.update(update)
        now = time.monotonic()
        elapsed = now - self.started
        done, total = self.progress.get("done"), self.progress.get("total")
        if isinstance(done, int | float) and isinstance(total, int | float) and done and total:
            rate = done / max(elapsed, 1e-6)
            self.progress["eta_seconds"] = (total - done) / rate if rate > 0 else None
            self.progress["percent"] = round(100.0 * done / total, 1)
        self.progress["elapsed"] = round(elapsed, 1)
        self.progress["job_id"] = self.job_id
        self.progress["kind"] = self.kind
        if self.on_progress:
            self.on_progress(dict(self.progress))
        if force or now - self._last_write >= self.throttle:
            self._last_write = now
            self._persist()
        if force or now - self._last_log >= 2.0:
            self._last_log = now
            self._log_line()

    def _persist(self) -> None:
        if self.job_id is not None:
            try:
                self.catalog.update_job(self.job_id, progress=self.progress)
            except Exception:  # pragma: no cover - never fail a job on progress persistence
                log.debug("progress persistence failed", exc_info=True)
        try:
            tmp = self.progress_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.progress, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.progress_path)
        except OSError:
            pass

    def _log_line(self) -> None:
        p = self.progress
        parts = [f"[{self.kind}]", str(p.get("stage", ""))]
        if "done" in p and "total" in p:
            parts.append(f"{p['done']}/{p['total']}")
            if p.get("percent") is not None:
                parts.append(f"{p['percent']}%")
            parts.append(f"ETA {format_eta(p.get('eta_seconds'))}")
        for key in ("files_seen", "new", "changed", "missing", "errors", "items", "candidates"):
            if key in p:
                parts.append(f"{key}={p[key]}")
        log.info(" ".join(parts))

    def checkpoint(self, data: dict[str, Any]) -> None:
        if self.job_id is not None:
            self.catalog.update_job(self.job_id, checkpoint=data)

    def finish(self, state: str, error: str | None = None) -> None:
        if self.job_id is None:
            return
        self.progress["stage"] = state
        self.catalog.update_job(self.job_id, state=state, progress=self.progress, error=error)
        self._persist()
        log.info("job %s #%s %s%s", self.kind, self.job_id, state, f": {error}" if error else "")
        self.job_id = None
