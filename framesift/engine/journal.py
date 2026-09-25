"""Journaled moves and undo. Every filesystem move goes through `perform_moves`."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from framesift.engine.catalog import Catalog, new_op_id, utcnow
from framesift.engine.config import Roots
from framesift.engine.fileops import MoveError, safe_move
from framesift.engine.paths import nfc, os_path

ProgressFn = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class MoveSpec:
    file_id: int
    item_id: int
    src_root: str
    src_rel: str
    src_rel_os: str | None
    dst_root: str
    dst_rel: str
    size: int
    origin_after: str | None  # origin_rel_path to store after the move


@dataclass
class MoveOutcome:
    op_id: str
    journal_ids: list[int] = field(default_factory=list)
    moved: int = 0
    conflicts: int = 0
    errors: list[str] = field(default_factory=list)
    bytes: int = 0
    dry_run: bool = False


def item_move_specs(
    catalog: Catalog,
    item_id: int,
    dst_root: str,
    dst_dir_rel: str | None = None,
    *,
    only_file: int | None = None,
) -> list[MoveSpec]:
    """Specs for moving a whole item (or one file) to `dst_root` keeping the source-relative path."""
    files = catalog.item_files(item_id)
    specs = []
    for f in files:
        if f["status"] != "present":
            continue
        if only_file is not None and f["id"] != only_file:
            continue
        origin = f["origin_rel_path"] if f["root"] != "source" else f["rel_path"]
        if origin is None:
            origin = f["rel_path"]
        if dst_dir_rel is not None:
            dst_rel = f"{dst_dir_rel}/{f['name']}" if dst_dir_rel else f["name"]
        else:
            dst_rel = origin
        origin_after = None if dst_root == "source" else origin
        specs.append(
            MoveSpec(
                file_id=f["id"],
                item_id=f["item_id"],
                src_root=f["root"],
                src_rel=f["rel_path"],
                src_rel_os=f["rel_path_os"],
                dst_root=dst_root,
                dst_rel=dst_rel,
                size=f["size"],
                origin_after=origin_after,
            )
        )
    return specs


def perform_moves(
    catalog: Catalog,
    roots: Roots,
    specs: Sequence[MoveSpec],
    *,
    session_id: int,
    action: str,
    category: str | None = None,
    reason_key: str | None = None,
    reason_params: dict[str, Any] | None = None,
    op_id: str | None = None,
    dry_run: bool = False,
    undoes: dict[int, int] | None = None,
) -> MoveOutcome:
    """Move the files of one logical operation; write journal rows and update the catalog."""
    op_id = op_id or new_op_id()
    outcome = MoveOutcome(op_id=op_id, dry_run=dry_run)
    rows: list[dict[str, Any]] = []
    for spec in specs:
        src = os_path(roots.path(spec.src_root), spec.src_rel, spec.src_rel_os)
        dst_root_path = roots.path(spec.dst_root)
        dst = os_path(dst_root_path, spec.dst_rel)
        if spec.dst_root == spec.src_root and spec.dst_rel == spec.src_rel:
            continue
        try:
            result = safe_move(src, dst, dry_run=dry_run)
        except (MoveError, OSError) as exc:
            outcome.errors.append(f"{spec.src_rel}: {exc}")
            catalog.add_issue(None, spec.src_rel, "move_error", str(exc))
            continue
        final_rel = nfc("/".join(result.dst.relative_to(dst_root_path).parts))
        if result.conflict:
            outcome.conflicts += 1
            catalog.add_issue(None, spec.dst_rel, "name_conflict", f"stored as {final_rel}")
        outcome.moved += 1
        outcome.bytes += spec.size
        rows.append(
            {
                "op_id": op_id,
                "session_id": session_id,
                "ts": utcnow(),
                "action": action,
                "file_id": spec.file_id,
                "item_id": spec.item_id,
                "from_root": spec.src_root,
                "from_rel": spec.src_rel,
                "to_root": spec.dst_root,
                "to_rel": final_rel,
                "method": result.method,
                "conflict": result.conflict,
                "category": category,
                "reason_key": reason_key,
                "reason_params": reason_params,
                "size": spec.size,
                "undoes": (undoes or {}).get(spec.file_id),
            }
        )
        if not dry_run:
            catalog.update_file_location(
                spec.file_id,
                root=spec.dst_root,
                rel_path=final_rel,
                rel_path_os=None,
                origin_rel_path=spec.origin_after,
            )
    if not dry_run and rows:
        outcome.journal_ids = catalog.add_journal(rows)
        if undoes:
            for jid, row in zip(outcome.journal_ids, rows, strict=True):
                if row["undoes"]:
                    catalog.set_undone(int(row["undoes"]), jid)
        catalog.commit()
    return outcome


def record_keep(catalog: Catalog, session_id: int, item_id: int, *, reviewed: bool = True) -> str:
    """Journal a 'keep' (no move) for the item and mark it reviewed."""
    op_id = new_op_id()
    files = catalog.item_files(item_id)
    rows = [
        {
            "op_id": op_id,
            "session_id": session_id,
            "action": "keep",
            "file_id": f["id"],
            "item_id": item_id,
            "from_root": f["root"],
            "from_rel": f["rel_path"],
            "to_root": f["root"],
            "to_rel": f["rel_path"],
            "method": "none",
            "size": f["size"],
        }
        for f in files
    ]
    catalog.add_journal(rows)
    if reviewed:
        for f in files:
            catalog.set_reviewed(f["id"], True)
    catalog.commit()
    return op_id


@dataclass
class UndoStats:
    ops: int = 0
    files: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    bytes: int = 0
    dry_run: bool = False
    cancelled: bool = False

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def undo_ops(
    catalog: Catalog,
    roots: Roots,
    op_ids: Sequence[str],
    *,
    session_id: int,
    dry_run: bool = False,
    progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
) -> UndoStats:
    """Reverse the given operations (newest first). Each reversal is journaled as 'undo'."""
    stats = UndoStats(dry_run=dry_run)
    relink = False
    for n, op_id in enumerate(op_ids, 1):
        if cancel is not None and cancel.is_set():
            stats.cancelled = True
            break
        rows = [
            r
            for r in catalog.op_rows(op_id)
            if r["undone_by"] is None and r["action"] not in ("undo", "purge")
        ]
        if not rows:
            continue
        stats.ops += 1
        specs: list[MoveSpec] = []
        undoes: dict[int, int] = {}
        keep_rows = []
        for r in reversed(rows):
            f = catalog.file(r["file_id"])
            if f is None:
                stats.skipped += 1
                continue
            if r["action"] == "keep":
                keep_rows.append(r)
                continue
            if (
                f["root"] != r["to_root"]
                or f["rel_path"] != r["to_rel"]
                or f["status"] != "present"
            ):
                stats.skipped += 1
                stats.errors.append(f"{r['to_rel']}: moved or missing since, cannot undo")
                continue
            if not os.path.lexists(os_path(roots.path(f["root"]), f["rel_path"], f["rel_path_os"])):
                stats.skipped += 1
                stats.errors.append(f"{r['to_rel']}: not on disk any more, cannot undo")
                continue
            if r["action"] == "detach_live":
                relink = True
            origin_after = None
            if r["from_root"] != "source":
                origin_after = f["origin_rel_path"]
            specs.append(
                MoveSpec(
                    file_id=f["id"],
                    item_id=f["item_id"],
                    src_root=f["root"],
                    src_rel=f["rel_path"],
                    src_rel_os=f["rel_path_os"],
                    dst_root=r["from_root"],
                    dst_rel=r["from_rel"],
                    size=f["size"],
                    origin_after=origin_after,
                )
            )
            undoes[f["id"]] = r["id"]
        if specs:
            outcome = perform_moves(
                catalog,
                roots,
                specs,
                session_id=session_id,
                action="undo",
                category=rows[0]["category"],
                dry_run=dry_run,
                undoes=undoes,
            )
            stats.files += outcome.moved
            stats.bytes += outcome.bytes
            stats.errors.extend(outcome.errors)
            if not dry_run:
                for r in rows:
                    if r["action"] == "restore":
                        catalog.set_reviewed(r["file_id"], False)
            if not dry_run and relink:
                for r in rows:
                    if r["action"] == "detach_live":
                        params = json.loads(r["reason_params"] or "{}")
                        photo_id = params.get("item_id")
                        if photo_id:
                            catalog.execute(
                                "UPDATE files SET item_id=?, companion_role='live_video', detached_at=NULL WHERE id=?",
                                (photo_id, r["file_id"]),
                            )
        if keep_rows and not dry_run:
            op = new_op_id()
            new_rows = []
            for r in keep_rows:
                catalog.set_reviewed(r["file_id"], False)
                new_rows.append(
                    {
                        "op_id": op,
                        "session_id": session_id,
                        "action": "undo",
                        "file_id": r["file_id"],
                        "item_id": r["item_id"],
                        "method": "none",
                        "size": 0,
                        "undoes": r["id"],
                    }
                )
            ids = catalog.add_journal(new_rows)
            for jid, r in zip(ids, keep_rows, strict=True):
                catalog.set_undone(r["id"], jid)
            stats.files += len(keep_rows)
        elif keep_rows:
            stats.files += len(keep_rows)
        catalog.commit()
        if progress:
            progress({"stage": "undo", "done": n, "total": len(op_ids), **stats.as_dict()})
    return stats
