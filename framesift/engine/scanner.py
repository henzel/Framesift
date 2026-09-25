"""Streaming, incremental filesystem scan plus metadata extraction."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from framesift import CATALOG_DIRNAME
from framesift.engine.catalog import Catalog, FileEntry, utcnow
from framesift.engine.config import Roots
from framesift.engine.items import link_items
from framesift.engine.metadata import read_media_info
from framesift.engine.paths import (
    is_excluded_dir,
    is_inside,
    is_network_path,
    is_temp_file,
    kind_for_ext,
    nfc,
    os_path,
    split_ext,
)

ProgressFn = Callable[[dict], None]
BATCH = 500


@dataclass
class ScanStats:
    files_seen: int = 0
    new: int = 0
    changed: int = 0
    unchanged: int = 0
    missing: int = 0
    bytes: int = 0
    dirs: int = 0
    skipped_dirs: int = 0
    errors: int = 0
    meta_extracted: int = 0
    meta_errors: int = 0
    cancelled: bool = False
    failed_dirs: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k != "failed_dirs"}


class Cancelled(Exception):
    pass


def walk_root(
    roots: Roots,
    root: str,
    *,
    stats: ScanStats,
    on_issue: Callable[[str, str, str], None],
    cancel: threading.Event | None = None,
) -> Iterator[FileEntry]:
    """Yield FileEntry objects for one root, iteratively, never following symlinks."""
    base = roots.path(root)
    recursive = roots.include_subfolders or root != "source"
    excluded_paths = {roots.review, roots.delete} if root == "source" else set()
    stack: list[tuple[Path, str]] = [(base, "")]
    while stack:
        if cancel is not None and cancel.is_set():
            stats.cancelled = True
            raise Cancelled()
        folder, rel_dir = stack.pop()
        stats.dirs += 1
        try:
            with os.scandir(folder) as it:
                entries = list(it)
        except OSError as exc:
            stats.errors += 1
            stats.failed_dirs.append(rel_dir)
            on_issue(rel_dir, "list_error", f"{type(exc).__name__}: {exc}")
            continue
        for entry in entries:
            try:
                name_os = entry.name
                name = nfc(name_os)
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if not recursive:
                        continue
                    if is_excluded_dir(name) or (
                        root != "source" and rel_dir == "" and name == CATALOG_DIRNAME
                    ):
                        stats.skipped_dirs += 1
                        continue
                    child = Path(entry.path)
                    if excluded_paths and any(
                        child == ex or is_inside(child, ex) for ex in excluded_paths
                    ):
                        stats.skipped_dirs += 1
                        continue
                    stack.append((child, f"{rel_dir}/{name}" if rel_dir else name))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                if is_temp_file(name):
                    continue
                st = entry.stat(follow_symlinks=False)
            except OSError as exc:
                stats.errors += 1
                on_issue(f"{rel_dir}/{entry.name}", "stat_error", f"{type(exc).__name__}: {exc}")
                continue
            rel_path = f"{rel_dir}/{name}" if rel_dir else name
            rel_os = None
            if name_os != name:
                rel_os = f"{rel_dir}/{name_os}" if rel_dir else name_os
            stem, ext = split_ext(name)
            stats.files_seen += 1
            stats.bytes += st.st_size
            yield FileEntry(
                root=root,
                rel_path=rel_path,
                rel_path_os=rel_os,
                dir_path=rel_dir,
                name=name,
                stem=stem,
                ext=ext,
                kind=kind_for_ext(ext),
                size=st.st_size,
                mtime_ns=st.st_mtime_ns,
            )


def scan(
    catalog: Catalog,
    roots: Roots,
    *,
    progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
    job_id: int | None = None,
    extract_metadata: bool = True,
    threads: int | None = None,
    on_entries: Callable[[list[FileEntry]], None] | None = None,
) -> ScanStats:
    """Scan the three roots into the catalog, then extract metadata and link items."""
    stats = ScanStats()
    scan_ts = utcnow()
    gen = catalog.next_scan_generation()

    def issue(path: str, kind: str, message: str) -> None:
        catalog.add_issue(job_id, path, kind, message)

    def report(stage: str) -> None:
        if progress:
            data = stats.as_dict()
            data["stage"] = stage
            progress(data)

    try:
        for root in ("source", "review", "delete"):
            if not roots.path(root).is_dir():
                continue
            batch: list[FileEntry] = []
            for entry in walk_root(roots, root, stats=stats, on_issue=issue, cancel=cancel):
                batch.append(entry)
                if len(batch) >= BATCH:
                    n, c, u = catalog.upsert_files(batch, scan_ts, gen)
                    stats.new += n
                    stats.changed += c
                    stats.unchanged += u
                    if on_entries:
                        on_entries(batch)
                    batch = []
                    report("walk")
            if batch:
                n, c, u = catalog.upsert_files(batch, scan_ts, gen)
                stats.new += n
                stats.changed += c
                stats.unchanged += u
                if on_entries:
                    on_entries(batch)
            stats.missing += catalog.mark_missing(root, gen, stats.failed_dirs)
            report("walk")
    except Cancelled:
        catalog.commit()
        return stats
    catalog.commit()
    if extract_metadata:
        extract_all(
            catalog,
            roots,
            stats=stats,
            progress=progress,
            cancel=cancel,
            job_id=job_id,
            threads=threads,
        )
    if not stats.cancelled:
        link_items(catalog)
    report("done")
    return stats


def extract_all(
    catalog: Catalog,
    roots: Roots,
    *,
    stats: ScanStats | None = None,
    progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
    job_id: int | None = None,
    threads: int | None = None,
) -> int:
    """Extract metadata for every media file whose metadata is missing or stale."""
    stats = stats or ScanStats()
    rows = catalog.files_needing_meta()
    total = len(rows)
    if total == 0:
        return 0
    if threads is None:
        threads = 16 if is_network_path(roots.source) else 8
    done = 0

    def work(row: dict) -> tuple[dict, object | None, str | None]:
        path = os_path(roots.path(row["root"]), row["rel_path"], row["rel_path_os"])
        try:
            info = read_media_info(path, row["ext"], row["size"], row["mtime_ns"])
            return row, info, None
        except OSError as exc:
            return row, None, f"{type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=max(1, threads)) as pool:
        pending = 0
        futures = []
        for row in rows:
            futures.append(pool.submit(work, dict(row)))
            pending += 1
            if pending >= threads * 4:
                _drain(futures, catalog, stats, cancel, job_id)
                done += pending
                pending = 0
                futures = []
                if progress:
                    progress(
                        {
                            **stats.as_dict(),
                            "stage": "metadata",
                            "meta_done": done,
                            "meta_total": total,
                        }
                    )
                if cancel is not None and cancel.is_set():
                    stats.cancelled = True
                    pool.shutdown(cancel_futures=True)
                    break
        if futures:
            _drain(futures, catalog, stats, cancel, job_id)
            done += len(futures)
    catalog.commit()
    if progress:
        progress({**stats.as_dict(), "stage": "metadata", "meta_done": done, "meta_total": total})
    return done


def _drain(
    futures: list,
    catalog: Catalog,
    stats: ScanStats,
    cancel: threading.Event | None,
    job_id: int | None,
) -> None:
    for fut in futures:
        row, info, error = fut.result()
        if error:
            stats.meta_errors += 1
            catalog.add_issue(job_id, row["rel_path"], "read_error", error)
            catalog.set_file_meta(
                row["id"],
                row["size"],
                row["mtime_ns"],
                {"extractor": "error", "structure_ok": None, "camera_known": 0},
            )
            continue
        stats.meta_extracted += 1
        assert info is not None
        catalog.set_file_meta(row["id"], row["size"], row["mtime_ns"], info.as_fields())
    catalog.commit()
