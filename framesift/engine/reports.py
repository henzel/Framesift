"""Full report files (JSON + CSV per category) next to the catalog."""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from framesift.engine.catalog import Catalog
from framesift.engine.i18n import reason_text


def report_dir(catalog_dir: Path, command: str) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    folder = catalog_dir / "reports" / f"{stamp}-{command}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def write_json(folder: Path, name: str, payload: dict[str, Any]) -> Path:
    path = folder / name
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    return path


def write_csv(folder: Path, name: str, rows: list[dict[str, Any]], columns: list[str]) -> Path:
    path = folder / name
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def write_candidate_lists(
    catalog: Catalog, folder: Path, run_id: int | None, lang: str | None = None
) -> dict[str, str]:
    """One CSV per category with every candidate of the run."""
    if run_id is None:
        return {}
    by_cat: dict[str, list[dict[str, Any]]] = {}
    for row in catalog.candidates(run_id):
        by_cat.setdefault(row["category"], []).append(
            {
                "file_id": row["file_id"],
                "path": row["f_rel_path"],
                "root": row["f_root"],
                "size": row["f_size"],
                "confidence": row["confidence"],
                "group": row["group_id"] or "",
                "keeper": int(row["is_keeper"]),
                "reason": reason_text(
                    row["reason_key"], json.loads(row["reason_params"] or "{}"), lang
                ),
                "also": ",".join(json.loads(row["also"] or "[]")),
                "applied": row["applied_op"] or "",
            }
        )
    out: dict[str, str] = {}
    for cat, rows in by_cat.items():
        path = write_csv(
            folder,
            f"{cat}.csv",
            rows,
            [
                "file_id",
                "path",
                "root",
                "size",
                "confidence",
                "group",
                "keeper",
                "reason",
                "also",
                "applied",
            ],
        )
        out[cat] = str(path)
    return out


def write_journal_csv(catalog: Catalog, folder: Path, session_id: int | None = None) -> Path:
    rows = [dict(r) for r in catalog.journal(session_id=session_id, limit=10_000_000)]
    return write_csv(
        folder,
        "journal.csv",
        rows,
        [
            "id",
            "op_id",
            "session_id",
            "ts",
            "action",
            "file_id",
            "item_id",
            "from_root",
            "from_rel",
            "to_root",
            "to_rel",
            "method",
            "conflict",
            "category",
            "size",
            "undoes",
            "undone_by",
        ],
    )
