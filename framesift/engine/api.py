"""Engine façade used by the CLI and the GUI."""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from framesift import __version__
from framesift.engine.analysis import set_low_priority
from framesift.engine.apply import ApplyStats, apply_plan, move_live_videos
from framesift.engine.catalog import Catalog, CatalogError, utcnow
from framesift.engine.classify import ClassifyStats, classify
from framesift.engine.config import ClassifyConfig, Roots
from framesift.engine.fileops import cleanup_temp_files
from framesift.engine.jobs import JobControl, JobRunner
from framesift.engine.journal import UndoStats, undo_ops
from framesift.engine.paths import RootIssue, check_roots
from framesift.engine.plan import Plan, build_plan
from framesift.engine.purge import PurgeStats, purge, purge_preview
from framesift.engine.scanner import ScanStats, scan

ProgressFn = Callable[[dict[str, Any]], None]


class WorkspaceError(Exception):
    def __init__(self, issues: list[RootIssue]):
        self.issues = issues
        super().__init__("; ".join(f"{i.key} {i.detail}".strip() for i in issues))


@dataclass
class Workspace:
    catalog: Catalog
    roots: Roots
    issues: list[RootIssue] = field(default_factory=list)
    app: str = f"cli {__version__}"
    _session_id: int | None = None
    _session_kind: str = "cli"

    @property
    def session_id(self) -> int:
        if self._session_id is None:
            self._session_id = self.catalog.start_session(
                self._session_kind, self.app, {"source": str(self.roots.source)}
            )
        return self._session_id

    def close(self) -> None:
        if self._session_id is not None and not self.catalog.read_only:
            self.catalog.end_session(self._session_id)
        self.catalog.close()

    def __enter__(self) -> Workspace:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def config(self) -> ClassifyConfig:
        return self.catalog.classify_config()


def open_workspace(
    source: Path | None = None,
    review: Path | None = None,
    delete: Path | None = None,
    *,
    include_subfolders: bool = True,
    create: bool = True,
    read_only: bool = False,
    force_lock: bool = False,
    app: str = f"cli {__version__}",
    session_kind: str = "cli",
) -> Workspace:
    """Open (or create) the catalog for a source folder, or re-open an existing catalog by its Review folder."""
    if source is not None:
        roots = Roots.for_source(source, review, delete, include_subfolders)
        issues = check_roots(roots.source, roots.review, roots.delete)
        if any(i.level == "error" for i in issues):
            raise WorkspaceError([i for i in issues if i.level == "error"])
        catalog = Catalog.open(
            roots.review, create=create, read_only=read_only, force_lock=force_lock
        )
        catalog.record_roots(roots)
    elif review is not None:
        review = Path(review).expanduser().resolve()
        catalog = Catalog.open(review, create=False, read_only=read_only, force_lock=force_lock)
        resolved = catalog.resolve_roots(review)
        if resolved is None:
            catalog.close()
            raise CatalogError("cannot resolve the source folder for this catalog; pass --source")
        roots = resolved
        if delete is not None:
            roots = Roots(
                roots.source, roots.review, Path(delete).resolve(), roots.include_subfolders
            )
        issues = check_roots(roots.source, roots.review, roots.delete)
        if not read_only:
            catalog.record_roots(roots)
    else:
        raise CatalogError("either a source folder or a catalog (review) folder is required")
    if not read_only:
        for root in (roots.source, roots.review, roots.delete):
            if root.is_dir():
                cleanup_temp_files(root)
        # we hold the exclusive lock, so jobs still marked running belong to a dead process
        catalog.execute(
            "UPDATE jobs SET state='interrupted', finished_at=? WHERE state IN ('running','paused')",
            (utcnow(),),
        )
        catalog.commit()
    ws = Workspace(catalog, roots, issues, app=app, _session_kind=session_kind)
    return ws


def run_scan(
    ws: Workspace,
    *,
    progress: ProgressFn | None = None,
    control: JobControl | None = None,
    low_priority: bool = False,
    threads: int | None = None,
    extract_metadata: bool = True,
) -> ScanStats:
    if low_priority:
        set_low_priority()
    with JobRunner(
        ws.catalog,
        "scan",
        ws.session_id,
        {"source": str(ws.roots.source)},
        on_progress=progress,
        control=control,
    ) as job:
        stats = scan(
            ws.catalog,
            ws.roots,
            progress=job.report,
            cancel=job.control.cancel,
            job_id=job.job_id,
            extract_metadata=extract_metadata,
            threads=threads,
        )
        job.report({"stage": "done", **stats.as_dict()}, force=True)
    return stats


def run_classify(
    ws: Workspace,
    cfg: ClassifyConfig | None = None,
    *,
    progress: ProgressFn | None = None,
    control: JobControl | None = None,
    workers: int | None = None,
    low_priority: bool = False,
) -> ClassifyStats:
    cfg = cfg or ws.config()
    ws.catalog.save_classify_config(cfg)
    if low_priority:
        set_low_priority()
    with JobRunner(
        ws.catalog,
        "classify",
        ws.session_id,
        {"config": cfg.to_json()},
        on_progress=progress,
        control=control,
    ) as job:
        stats = classify(
            ws.catalog,
            ws.roots,
            cfg,
            run_id=job.job_id or 0,
            workers=workers,
            low_priority=low_priority,
            progress=job.report,
            cancel=job.control.cancel,
        )
        job.report({"stage": "done", **stats.as_dict()}, force=True)
    return stats


def get_plan(
    ws: Workspace, cfg: ClassifyConfig | None = None, *, examples: int = 12, lang: str | None = None
) -> Plan:
    cfg = cfg or ws.config()
    return build_plan(ws.catalog, cfg, examples=examples, lang=lang)


def run_apply(
    ws: Workspace,
    cfg: ClassifyConfig | None = None,
    *,
    categories: Sequence[str] | None = None,
    dry_run: bool = True,
    progress: ProgressFn | None = None,
    control: JobControl | None = None,
) -> ApplyStats:
    cfg = cfg or ws.config()
    with JobRunner(
        ws.catalog,
        "apply",
        ws.session_id,
        {"categories": list(categories or []), "dry_run": dry_run},
        on_progress=progress,
        control=control,
    ) as job:
        stats = apply_plan(
            ws.catalog,
            ws.roots,
            cfg,
            session_id=ws.session_id,
            categories=categories,
            dry_run=dry_run,
            progress=job.report,
            cancel=job.control.cancel,
            job_id=job.job_id,
        )
        job.report({"stage": "done", **stats.as_dict()}, force=True)
    return stats


def run_live_photos(
    ws: Workspace,
    *,
    dry_run: bool = True,
    progress: ProgressFn | None = None,
    control: JobControl | None = None,
) -> ApplyStats:
    with JobRunner(
        ws.catalog,
        "live_photos",
        ws.session_id,
        {"dry_run": dry_run},
        on_progress=progress,
        control=control,
    ) as job:
        stats = move_live_videos(
            ws.catalog,
            ws.roots,
            session_id=ws.session_id,
            dry_run=dry_run,
            progress=job.report,
            cancel=job.control.cancel,
        )
        job.report({"stage": "done", **stats.as_dict()}, force=True)
    return stats


def select_undo_ops(
    ws: Workspace, scope: str, *, session_id: int | None = None, category: str | None = None
) -> list[str]:
    catalog = ws.catalog
    if scope == "last":
        sid = session_id if session_id is not None else _last_session_with_ops(catalog)
        return catalog.undoable_ops(session_id=sid, limit=1) if sid is not None else []
    if scope == "session":
        sid = session_id if session_id is not None else _last_session_with_ops(catalog)
        return catalog.undoable_ops(session_id=sid) if sid is not None else []
    if scope == "category":
        return catalog.undoable_ops(category=category)
    if scope == "all":
        return catalog.undoable_ops()
    raise ValueError(f"unknown undo scope {scope!r}")


def _last_session_with_ops(catalog: Catalog) -> int | None:
    row = catalog.one(
        "SELECT session_id FROM journal WHERE undone_by IS NULL AND action NOT IN ('undo','purge') ORDER BY id DESC LIMIT 1"
    )
    return int(row[0]) if row else None


def run_undo(
    ws: Workspace,
    scope: str,
    *,
    session_id: int | None = None,
    category: str | None = None,
    dry_run: bool = True,
    progress: ProgressFn | None = None,
    control: JobControl | None = None,
) -> UndoStats:
    ops = select_undo_ops(ws, scope, session_id=session_id, category=category)
    with JobRunner(
        ws.catalog,
        "undo",
        ws.session_id,
        {"scope": scope, "ops": len(ops), "dry_run": dry_run},
        on_progress=progress,
        control=control,
    ) as job:
        stats = undo_ops(
            ws.catalog,
            ws.roots,
            ops,
            session_id=ws.session_id,
            dry_run=dry_run,
            progress=job.report,
            cancel=job.control.cancel,
        )
        job.report({"stage": "done", **stats.as_dict()}, force=True)
    return stats


def run_purge(
    ws: Workspace,
    *,
    dry_run: bool = True,
    use_trash: bool | None = None,
    progress: ProgressFn | None = None,
    control: JobControl | None = None,
) -> PurgeStats:
    with JobRunner(
        ws.catalog,
        "purge",
        ws.session_id,
        {"dry_run": dry_run},
        on_progress=progress,
        control=control,
    ) as job:
        stats = purge(
            ws.catalog,
            ws.roots,
            session_id=ws.session_id,
            dry_run=dry_run,
            use_trash=use_trash,
            progress=job.report,
            cancel=job.control.cancel,
        )
        job.report({"stage": "done", **stats.as_dict()}, force=True)
    return stats


def status(ws: Workspace) -> dict[str, Any]:
    catalog = ws.catalog
    lock_owner = catalog.lock.owner() if catalog.lock else None
    return {
        "version": __version__,
        "catalog": str(catalog.path),
        "catalog_uuid": catalog.uuid,
        "schema_version": catalog.schema_version(),
        "roots": {
            "source": str(ws.roots.source),
            "review": str(ws.roots.review),
            "delete": str(ws.roots.delete),
            "include_subfolders": ws.roots.include_subfolders,
        },
        "files": catalog.root_stats(),
        "review_categories": catalog.review_category_stats(),
        "last_jobs": [dict(j) for j in catalog.jobs(limit=5)],
        "unfinished_jobs": [dict(j) for j in catalog.jobs(states=["running", "paused"], limit=5)],
        "lock": lock_owner,
        "purge": purge_preview(catalog, ws.roots),
        "issues": ws.issues and [i.__dict__ for i in ws.issues],
    }


__all__ = [
    "Workspace",
    "WorkspaceError",
    "open_workspace",
    "run_scan",
    "run_classify",
    "get_plan",
    "run_apply",
    "run_live_photos",
    "run_undo",
    "select_undo_ops",
    "run_purge",
    "status",
    "JobControl",
    "threading",
]
