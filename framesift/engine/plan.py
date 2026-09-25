"""The dry-run report: what apply() would do, computed from stored candidates only."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from framesift.engine.catalog import Catalog
from framesift.engine.config import CATEGORY_CONFIDENCE, CATEGORY_ORDER, ClassifyConfig
from framesift.engine.i18n import reason_text, t

H264_TO_HEVC_SAVINGS = 0.45


@dataclass
class Example:
    file_id: int
    item_id: int
    rel_path: str
    size: int
    reason: str
    group_id: str | None = None
    is_keeper: bool = False


@dataclass
class CategoryReport:
    category: str
    title: str
    confidence: str
    enabled: bool
    count: int = 0
    bytes: int = 0
    groups: int = 0
    examples: list[Example] = field(default_factory=list)


@dataclass
class Plan:
    run_id: int | None
    categories: list[CategoryReport]
    live_photos: dict[str, Any]
    edited_pairs: dict[str, Any]
    large_videos: list[dict[str, Any]]
    totals: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, indent=2)

    def category(self, name: str) -> CategoryReport | None:
        return next((c for c in self.categories if c.category == name), None)


def item_sizes(catalog: Catalog, root: str = "source") -> dict[int, int]:
    sizes: dict[int, int] = {}
    for r in catalog.query(
        "SELECT item_id, SUM(size) AS s FROM files WHERE root=? AND status='present' GROUP BY item_id",
        (root,),
    ):
        sizes[int(r["item_id"])] = int(r["s"] or 0)
    return sizes


def build_plan(
    catalog: Catalog,
    cfg: ClassifyConfig,
    run_id: int | None = None,
    examples: int = 12,
    lang: str | None = None,
) -> Plan:
    run_id = run_id if run_id is not None else catalog.latest_run_id()
    sizes = item_sizes(catalog)
    reports: dict[str, CategoryReport] = {
        c: CategoryReport(c, t(f"category.{c}", lang), CATEGORY_CONFIDENCE[c], cfg.is_enabled(c))
        for c in CATEGORY_ORDER
    }
    groups: dict[str, set[str]] = {c: set() for c in CATEGORY_ORDER}
    if run_id is not None:
        for row in catalog.candidates(run_id, unapplied_only=True):
            if row["f_root"] != "source" or row["f_status"] != "present":
                continue
            rep = reports.get(row["category"])
            if rep is None:
                continue
            if row["group_id"]:
                groups[row["category"]].add(row["group_id"])
            if row["is_keeper"]:
                continue
            rep.count += 1
            is_companion = row["f_item_id"] != row["file_id"]
            rep.bytes += (
                row["f_size"] if is_companion else sizes.get(int(row["f_item_id"]), row["f_size"])
            )
            if len(rep.examples) < examples:
                rep.examples.append(
                    Example(
                        row["file_id"],
                        row["f_item_id"],
                        row["f_rel_path"],
                        row["f_size"],
                        reason_text(
                            row["reason_key"], json.loads(row["reason_params"] or "{}"), lang
                        ),
                        row["group_id"],
                        bool(row["is_keeper"]),
                    )
                )
    for c, rep in reports.items():
        rep.groups = len(groups[c])
    live = catalog.one(
        "SELECT COUNT(*) AS n, COALESCE(SUM(size),0) AS b FROM files WHERE root='source' AND status='present' AND companion_role='live_video'"
    )
    edited = catalog.one(
        "SELECT COUNT(*) AS n, COALESCE(SUM(f.size),0) AS b FROM files f WHERE f.root='source' AND f.status='present' AND f.edited_of IS NOT NULL"
    )
    large_rows = catalog.query(
        "SELECT f.id, f.rel_path, f.size, m.codec, m.width, m.height, m.bitrate, m.duration_ms FROM files f "
        "LEFT JOIN file_meta m ON m.file_id=f.id WHERE f.root='source' AND f.status='present' AND f.kind='video' "
        "ORDER BY f.size DESC LIMIT ?",
        (cfg.large_video_top,),
    )
    large = []
    for r in large_rows:
        entry = {
            "file_id": r["id"],
            "rel_path": r["rel_path"],
            "size": r["size"],
            "codec": r["codec"],
            "width": r["width"],
            "height": r["height"],
            "bitrate": r["bitrate"],
            "duration_ms": r["duration_ms"],
            "hevc_savings_estimate": int(r["size"] * H264_TO_HEVC_SAVINGS)
            if r["codec"] == "h264"
            else 0,
        }
        large.append(entry)
    enabled_reports = [r for r in reports.values() if r.enabled]
    totals = {
        "candidates": sum(r.count for r in enabled_reports),
        "bytes": sum(r.bytes for r in enabled_reports),
        "all_candidates": sum(r.count for r in reports.values()),
        "all_bytes": sum(r.bytes for r in reports.values()),
    }
    return Plan(
        run_id=run_id,
        categories=list(reports.values()),
        live_photos={"count": int(live["n"]), "bytes": int(live["b"])}
        if live
        else {"count": 0, "bytes": 0},
        edited_pairs={"count": int(edited["n"]), "bytes": int(edited["b"])}
        if edited
        else {"count": 0, "bytes": 0},
        large_videos=large,
        totals=totals,
    )
