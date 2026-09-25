"""The only code that deletes files: empty the Delete folder (system trash or permanent delete)."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from framesift.engine.catalog import Catalog, new_op_id
from framesift.engine.config import Roots
from framesift.engine.paths import is_network_path, os_path, to_os

ProgressFn = Callable[[dict[str, Any]], None]


@dataclass
class PurgeStats:
    files: int = 0
    bytes: int = 0
    errors: list[str] = field(default_factory=list)
    method: str = "trash"
    dry_run: bool = True
    cancelled: bool = False
    dirs_removed: int = 0

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def purge_preview(catalog: Catalog, roots: Roots) -> dict[str, Any]:
    row = catalog.one(
        "SELECT COUNT(*) AS n, COALESCE(SUM(size),0) AS b FROM files WHERE root='delete' AND status='present'"
    )
    network = is_network_path(roots.delete)
    return {
        "files": int(row["n"]) if row else 0,
        "bytes": int(row["b"]) if row else 0,
        "network": network,
        "method": "unlink" if network else "trash",
    }


def purge(
    catalog: Catalog,
    roots: Roots,
    *,
    session_id: int,
    dry_run: bool = True,
    use_trash: bool | None = None,
    progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
) -> PurgeStats:
    """Delete every present file catalogued under the Delete root. Journaled as 'purge' (not undoable)."""
    if use_trash is None:
        use_trash = not is_network_path(roots.delete)
    stats = PurgeStats(method="trash" if use_trash else "unlink", dry_run=dry_run)
    rows = catalog.files("delete")
    total = len(rows)
    op_id = new_op_id()
    journal_rows = []
    for n, f in enumerate(rows, 1):
        if cancel is not None and cancel.is_set():
            stats.cancelled = True
            break
        path = os_path(roots.delete, f["rel_path"], f["rel_path_os"])
        if not dry_run:
            try:
                if use_trash:
                    from send2trash import send2trash

                    send2trash(str(path))
                else:
                    os.unlink(to_os(path))
            except Exception as exc:
                stats.errors.append(f"{f['rel_path']}: {exc}")
                catalog.add_issue(None, f["rel_path"], "purge_error", str(exc))
                continue
            catalog.set_status(f["id"], "purged")
            journal_rows.append(
                {
                    "op_id": op_id,
                    "session_id": session_id,
                    "action": "purge",
                    "file_id": f["id"],
                    "item_id": f["item_id"],
                    "from_root": "delete",
                    "from_rel": f["rel_path"],
                    "method": stats.method,
                    "size": f["size"],
                }
            )
        stats.files += 1
        stats.bytes += f["size"]
        if progress and (n % 50 == 0 or n == total):
            progress({"stage": "purge", "done": n, "total": total, **stats.as_dict()})
    if not dry_run:
        if journal_rows:
            catalog.add_journal(journal_rows)
        catalog.commit()
        stats.dirs_removed = _remove_empty_dirs(roots)
    return stats


def _remove_empty_dirs(roots: Roots) -> int:
    """Remove now-empty subfolders of the Delete root (directories only, never files)."""
    removed = 0
    for dirpath, dirnames, filenames in os.walk(roots.delete, topdown=False):
        if dirpath == str(roots.delete) or filenames or dirnames:
            continue
        try:
            os.rmdir(dirpath)
            removed += 1
        except OSError:
            pass
    return removed
