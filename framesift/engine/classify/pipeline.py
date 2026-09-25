"""Classification pipeline: compute what the rules need, then assign categories in order."""

from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from framesift.engine.analysis import run_tasks
from framesift.engine.catalog import Catalog
from framesift.engine.classify.rules import (
    Candidate,
    Row,
    is_primary,
    rule_blurry_dark,
    rule_duplicates,
    rule_junk,
    rule_no_camera,
    rule_screen_recordings,
    rule_screenshots,
    rule_short_videos,
    rule_similar,
    valid_analysis,
)
from framesift.engine.config import CATEGORY_ORDER, ClassifyConfig, Roots

ProgressFn = Callable[[dict[str, Any]], None]


@dataclass
class ClassifyStats:
    files: int = 0
    partial_hashed: int = 0
    full_hashed: int = 0
    analyzed: int = 0
    candidates: int = 0
    per_category: dict[str, int] = field(default_factory=dict)
    cancelled: bool = False

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _load(catalog: Catalog) -> list[Row]:
    return [dict(r) for r in catalog.files_with_meta("source")]


def classify(
    catalog: Catalog,
    roots: Roots,
    cfg: ClassifyConfig,
    *,
    run_id: int,
    workers: int | None = None,
    low_priority: bool = False,
    progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
    compute: bool = True,
) -> ClassifyStats:
    stats = ClassifyStats()
    rows = _load(catalog)
    stats.files = len(rows)

    def cancelled() -> bool:
        if cancel is not None and cancel.is_set():
            stats.cancelled = True
            return True
        return False

    if compute and cfg.is_enabled("duplicates"):
        rows = _ensure_hashes(catalog, roots, rows, stats, workers, low_priority, progress, cancel)
        if cancelled():
            return stats
    if compute and (
        cfg.is_enabled("blurry_dark") or cfg.is_enabled("similar") or cfg.is_enabled("junk")
    ):
        rows = _ensure_pixels(
            catalog, roots, rows, cfg, stats, workers, low_priority, progress, cancel
        )
        if cancelled():
            return stats

    assigned: dict[int, Candidate] = {}

    def take(cands: list[Candidate]) -> None:
        for c in cands:
            prev = assigned.get(c.file_id)
            if prev is None:
                assigned[c.file_id] = c
            elif c.category not in prev.also and c.category != prev.category:
                prev.also.append(c.category)

    for category in CATEGORY_ORDER:
        if not cfg.is_enabled(category):
            continue
        if category == "duplicates":
            take(rule_duplicates(rows, run_id))
        elif category == "screenshots":
            take(rule_screenshots(rows))
        elif category == "screen_recordings":
            take(rule_screen_recordings(rows))
        elif category == "short_videos":
            take(rule_short_videos(rows, cfg))
        elif category == "no_camera_media":
            take(rule_no_camera(rows))
        elif category == "junk":
            take(rule_junk(rows, cfg))
        elif category == "blurry_dark":
            take(rule_blurry_dark(rows, cfg))
        elif category == "similar":
            take(rule_similar(rows, cfg, run_id, set(assigned)))
    candidates = list(assigned.values())
    stats.candidates = catalog.replace_candidates(run_id, (c.as_row() for c in candidates))
    per: dict[str, int] = defaultdict(int)
    for c in candidates:
        if not c.is_keeper:
            per[c.category] += 1
    stats.per_category = dict(per)
    if progress:
        progress({"stage": "classified", **stats.as_dict()})
    return stats


def _ensure_hashes(
    catalog: Catalog,
    roots: Roots,
    rows: list[Row],
    stats: ClassifyStats,
    workers: int | None,
    low_priority: bool,
    progress: ProgressFn | None,
    cancel: threading.Event | None,
) -> list[Row]:
    media = [
        r
        for r in rows
        if is_primary(r) and r["kind"] in ("photo", "raw", "video") and r["size"] > 0
    ]
    by_size: dict[int, list[Row]] = defaultdict(list)
    for r in media:
        by_size[r["size"]].append(r)
    need_partial = [
        ("partial", r)
        for group in by_size.values()
        if len(group) > 1
        for r in group
        if not (valid_analysis(r) and r.get("a_partial_hash"))
    ]
    if need_partial:
        stats.partial_hashed += run_tasks(
            catalog,
            roots,
            need_partial,
            workers=workers,
            low_priority=low_priority,
            progress=progress,
            cancel=cancel,
            stage="partial_hash",
        )
        if cancel is not None and cancel.is_set():
            return rows
        rows = _load(catalog)
        media = [
            r
            for r in rows
            if is_primary(r) and r["kind"] in ("photo", "raw", "video") and r["size"] > 0
        ]
    by_partial: dict[tuple[int, str], list[Row]] = defaultdict(list)
    for r in media:
        if valid_analysis(r) and r.get("a_partial_hash"):
            by_partial[(r["size"], r["a_partial_hash"])].append(r)
    need_full = [
        ("full", r)
        for group in by_partial.values()
        if len(group) > 1
        for r in group
        if not r.get("a_full_hash")
    ]
    if need_full:
        stats.full_hashed += run_tasks(
            catalog,
            roots,
            need_full,
            workers=workers,
            low_priority=low_priority,
            progress=progress,
            cancel=cancel,
            stage="full_hash",
        )
        rows = _load(catalog)
    return rows


def _ensure_pixels(
    catalog: Catalog,
    roots: Roots,
    rows: list[Row],
    cfg: ClassifyConfig,
    stats: ClassifyStats,
    workers: int | None,
    low_priority: bool,
    progress: ProgressFn | None,
    cancel: threading.Event | None,
) -> list[Row]:
    photos = [
        r
        for r in rows
        if is_primary(r)
        and r["kind"] in ("photo", "raw")
        and r["size"] > 0
        and r.get("m_structure_ok") != 0
    ]
    if cfg.is_enabled("blurry_dark") or cfg.is_enabled("junk"):
        wanted = photos
    else:
        # similar only: photos that have a neighbour inside the time window
        wanted = []
        dated = sorted(
            (r for r in photos if r.get("m_date_taken_ts") is not None),
            key=lambda r: r["m_date_taken_ts"],
        )
        window = cfg.similar_window_seconds
        for i, r in enumerate(dated):
            prev_ok = i > 0 and r["m_date_taken_ts"] - dated[i - 1]["m_date_taken_ts"] <= window
            next_ok = (
                i + 1 < len(dated)
                and dated[i + 1]["m_date_taken_ts"] - r["m_date_taken_ts"] <= window
            )
            if prev_ok or next_ok:
                wanted.append(r)
    if not cfg.is_enabled("junk") and not cfg.is_enabled("blurry_dark"):
        wanted = [r for r in wanted if r["kind"] == "photo"]
    need = [
        ("pixels", r)
        for r in wanted
        if not (valid_analysis(r) and (r.get("a_phash") is not None or r.get("a_decode_error")))
    ]
    if need:
        stats.analyzed += run_tasks(
            catalog,
            roots,
            need,
            workers=workers,
            low_priority=low_priority,
            progress=progress,
            cancel=cancel,
            stage="pixels",
        )
        rows = _load(catalog)
    return rows
