"""Apply a plan: move candidates into <Review>/<category>/…; the Live Photo action; group decisions."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from framesift.engine.catalog import Catalog, new_op_id, utcnow
from framesift.engine.config import CATEGORY_ORDER, LIVE_PHOTOS_CATEGORY, ClassifyConfig, Roots
from framesift.engine.journal import MoveSpec, item_move_specs, perform_moves

ProgressFn = Callable[[dict[str, Any]], None]


@dataclass
class ApplyStats:
    items: int = 0
    files: int = 0
    bytes: int = 0
    conflicts: int = 0
    errors: list[str] = field(default_factory=list)
    skipped: int = 0
    per_category: dict[str, int] = field(default_factory=dict)
    dry_run: bool = True
    cancelled: bool = False

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def review_destination(category: str, rel_path: str, group_id: str | None) -> str:
    if category == "similar" and group_id:
        return f"{category}/{group_id}"
    parent = rel_path.rsplit("/", 1)[0] if "/" in rel_path else ""
    return f"{category}/{parent}" if parent else category


def apply_plan(
    catalog: Catalog,
    roots: Roots,
    cfg: ClassifyConfig,
    *,
    session_id: int,
    categories: Sequence[str] | None = None,
    run_id: int | None = None,
    dry_run: bool = True,
    progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
    job_id: int | None = None,
) -> ApplyStats:
    """Move every unapplied non-keeper candidate of the selected categories into the Review folder."""
    stats = ApplyStats(dry_run=dry_run)
    run_id = run_id if run_id is not None else catalog.latest_run_id()
    if run_id is None:
        return stats
    selected = set(categories) if categories else {c for c in CATEGORY_ORDER if cfg.is_enabled(c)}
    rows = [
        r
        for r in catalog.candidates(run_id, unapplied_only=True, include_keepers=False)
        if r["category"] in selected and r["f_root"] == "source" and r["f_status"] == "present"
    ]
    total = len(rows)
    done_items: set[int] = set()
    for n, row in enumerate(rows, 1):
        if cancel is not None and cancel.is_set():
            stats.cancelled = True
            break
        is_companion = row["f_item_id"] != row["file_id"]
        item_id = int(row["f_item_id"])
        if not is_companion and item_id in done_items:
            continue
        origin = row["f_rel_path"]
        dst_dir = review_destination(row["category"], origin, row["group_id"])
        specs = item_move_specs(
            catalog, item_id, "review", dst_dir, only_file=row["file_id"] if is_companion else None
        )
        if not specs:
            stats.skipped += 1
            continue
        op_id = new_op_id()
        outcome = perform_moves(
            catalog,
            roots,
            specs,
            session_id=session_id,
            action="to_review",
            category=row["category"],
            reason_key=row["reason_key"],
            reason_params=json.loads(row["reason_params"] or "{}"),
            op_id=op_id,
            dry_run=dry_run,
        )
        stats.errors.extend(outcome.errors)
        stats.conflicts += outcome.conflicts
        if outcome.moved:
            stats.items += 1
            stats.files += outcome.moved
            stats.bytes += outcome.bytes
            stats.per_category[row["category"]] = stats.per_category.get(row["category"], 0) + 1
            if not dry_run:
                catalog.mark_candidate_applied(row["id"], op_id)
                catalog.commit()
        if not is_companion:
            done_items.add(item_id)
        if progress and (n % 20 == 0 or n == total):
            progress({"stage": "apply", "done": n, "total": total, **stats.as_dict()})
        if job_id is not None and not dry_run and n % 50 == 0:
            catalog.update_job(
                job_id, progress={"done": n, "total": total}, checkpoint={"candidate_id": row["id"]}
            )
    catalog.commit()
    return stats


def move_live_videos(
    catalog: Catalog,
    roots: Roots,
    *,
    session_id: int,
    dry_run: bool = True,
    progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
) -> ApplyStats:
    """The explicit action: move every Live Photo MOV alone into <Review>/live_photos/… (photos become still)."""
    stats = ApplyStats(dry_run=dry_run)
    rows = catalog.query(
        "SELECT f.*, p.rel_path AS photo_rel FROM files f JOIN files p ON p.id=f.item_id "
        "WHERE f.root='source' AND f.status='present' AND f.companion_role='live_video' ORDER BY f.id"
    )
    total = len(rows)
    for n, f in enumerate(rows, 1):
        if cancel is not None and cancel.is_set():
            stats.cancelled = True
            break
        dst_rel = f"{LIVE_PHOTOS_CATEGORY}/{f['rel_path']}"
        spec = MoveSpec(
            file_id=f["id"],
            item_id=f["item_id"],
            src_root="source",
            src_rel=f["rel_path"],
            src_rel_os=f["rel_path_os"],
            dst_root="review",
            dst_rel=dst_rel,
            size=f["size"],
            origin_after=f["rel_path"],
        )
        outcome = perform_moves(
            catalog,
            roots,
            [spec],
            session_id=session_id,
            action="detach_live",
            category=LIVE_PHOTOS_CATEGORY,
            reason_key="reason.live_video",
            reason_params={"photo": f["photo_rel"], "item_id": f["item_id"]},
            dry_run=dry_run,
        )
        stats.errors.extend(outcome.errors)
        stats.conflicts += outcome.conflicts
        if outcome.moved:
            stats.items += 1
            stats.files += 1
            stats.bytes += f["size"]
            if not dry_run:
                catalog.execute(
                    "UPDATE files SET item_id=id, companion_role=NULL, detached_at=? WHERE id=?",
                    (utcnow(), f["id"]),
                )
        if progress and (n % 20 == 0 or n == total):
            progress({"stage": "live_photos", "done": n, "total": total, **stats.as_dict()})
    catalog.commit()
    return stats


def apply_group_decision(
    catalog: Catalog,
    roots: Roots,
    *,
    session_id: int,
    keep_items: Sequence[int],
    delete_items: Sequence[int],
) -> ApplyStats:
    """Group mode Enter: marked items stay (Review members return home), the rest go to Delete."""
    from framesift.engine.actions import keep as keep_action
    from framesift.engine.actions import to_delete

    stats = ApplyStats(dry_run=False)
    for item_id in keep_items:
        try:
            result = keep_action(catalog, roots, item_id, session_id)
        except Exception as exc:
            stats.errors.append(f"{item_id}: {exc}")
            continue
        stats.items += 1
        if not isinstance(result, str):
            stats.files += result.moved
    for item_id in delete_items:
        try:
            outcome = to_delete(catalog, roots, item_id, session_id)
        except Exception as exc:
            stats.errors.append(f"{item_id}: {exc}")
            continue
        if outcome:
            stats.items += 1
            stats.files += outcome.moved
            stats.bytes += outcome.bytes
    return stats
