"""One function per category. Each takes plain dict rows (files + m_* meta + a_* analysis)."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from framesift.engine.analysis import hamming, phash_unsigned
from framesift.engine.config import (
    JUNK_NAMES,
    JUNK_PREFIXES,
    SCREEN_RECORDING_RE,
    ClassifyConfig,
    is_phone_screen,
)

Row = dict[str, Any]


@dataclass
class Candidate:
    file_id: int
    category: str
    confidence: str
    reason_key: str
    reason_params: dict[str, Any] = field(default_factory=dict)
    group_id: str | None = None
    is_keeper: bool = False
    also: list[str] = field(default_factory=list)

    def as_row(self) -> dict[str, Any]:
        return {
            "file_id": self.file_id,
            "category": self.category,
            "confidence": self.confidence,
            "reason_key": self.reason_key,
            "reason_params": self.reason_params,
            "group_id": self.group_id,
            "is_keeper": self.is_keeper,
            "also": self.also,
        }


def is_primary(row: Row) -> bool:
    return row["item_id"] == row["id"]


def has_camera(row: Row) -> bool:
    if row.get("m_make") or row.get("m_model"):
        return True
    software = row.get("m_software") or ""
    return software.startswith("Android")


def valid_analysis(row: Row) -> bool:
    return bool(row.get("a_valid"))


# ----------------------------------------------------------------------------- duplicates

_NAME_PENALTIES: list[tuple[re.Pattern[str], int]] = [
    (re.compile(r"~\d+$"), 4),
    (re.compile(r"\s\(\d+\)$"), 3),
    (re.compile(r"\(\d+\)"), 2),
    (re.compile(r"(?i)(^|[\s_-])copy([\s_-]?\d+)?$"), 3),
    (re.compile(r"(?i)^copy of "), 3),
    (re.compile(r"(?<=\d)[-_ ]\d{1,2}$"), 2),
    (re.compile(r"(?i)[\s_-](dup|duplicate)$"), 3),
]


def name_penalty(stem: str) -> int:
    score = 0
    for pattern, weight in _NAME_PENALTIES:
        if pattern.search(stem):
            score += weight
    return score


def keeper_sort_key(row: Row) -> tuple:
    return (
        name_penalty(row["stem"]),
        row.get("m_date_taken_ts") or 2**62,
        len(row["rel_path"]),
        row["rel_path"].count("/"),
        row["id"],
    )


def rule_duplicates(rows: list[Row], run_id: int) -> list[Candidate]:
    groups: dict[str, list[Row]] = defaultdict(list)
    for r in rows:
        if not is_primary(r) or r["kind"] not in ("photo", "raw", "video"):
            continue
        if valid_analysis(r) and r.get("a_full_hash"):
            groups[r["a_full_hash"]].append(r)
    out: list[Candidate] = []
    n = 0
    for _h, members in sorted(groups.items(), key=lambda kv: min(m["id"] for m in kv[1])):
        if len(members) < 2:
            continue
        n += 1
        gid = f"d{run_id}-{n}"
        members.sort(key=keeper_sort_key)
        keeper = members[0]
        out.append(
            Candidate(
                keeper["id"],
                "duplicates",
                "high",
                "reason.duplicate_keeper",
                {"group": gid},
                gid,
                True,
            )
        )
        for m in members[1:]:
            out.append(
                Candidate(
                    m["id"],
                    "duplicates",
                    "high",
                    "reason.duplicate_of",
                    {"keeper": keeper["rel_path"]},
                    gid,
                )
            )
    return out


# ----------------------------------------------------------------------------- screenshots / recordings


def rule_screenshots(rows: list[Row]) -> list[Candidate]:
    out = []
    for r in rows:
        if not is_primary(r) or r["kind"] != "photo":
            continue
        comment = (r.get("m_user_comment") or "").strip().lower()
        if comment == "screenshot":
            out.append(Candidate(r["id"], "screenshots", "high", "reason.screenshot_comment"))
        elif (
            r.get("m_format") == "png"
            and r.get("m_camera_known")
            and not has_camera(r)
            and is_phone_screen(r.get("m_width"), r.get("m_height"))
        ):
            out.append(
                Candidate(
                    r["id"],
                    "screenshots",
                    "high",
                    "reason.screenshot_resolution",
                    {"width": r["m_width"], "height": r["m_height"]},
                )
            )
    return out


def rule_screen_recordings(rows: list[Row]) -> list[Candidate]:
    out = []
    for r in rows:
        if not is_primary(r) or r["kind"] != "video":
            continue
        if SCREEN_RECORDING_RE.match(r["stem"]):
            out.append(
                Candidate(r["id"], "screen_recordings", "high", "reason.screen_recording_name")
            )
        elif (
            r.get("m_camera_known")
            and not has_camera(r)
            and is_phone_screen(r.get("m_width"), r.get("m_height"))
        ):
            out.append(
                Candidate(
                    r["id"],
                    "screen_recordings",
                    "high",
                    "reason.screen_recording_resolution",
                    {"width": r["m_width"], "height": r["m_height"]},
                )
            )
    return out


# ----------------------------------------------------------------------------- short videos / no camera


def rule_short_videos(rows: list[Row], cfg: ClassifyConfig) -> list[Candidate]:
    limit = cfg.short_video_seconds * 1000
    out = []
    for r in rows:
        if not is_primary(r) or r["kind"] != "video":
            continue
        dur = r.get("m_duration_ms")
        if dur is not None and dur < limit:
            out.append(
                Candidate(
                    r["id"],
                    "short_videos",
                    "medium",
                    "reason.short_video",
                    {"seconds": dur / 1000, "threshold": cfg.short_video_seconds},
                )
            )
    return out


def rule_no_camera(rows: list[Row]) -> list[Candidate]:
    out = []
    for r in rows:
        if not is_primary(r) or r["kind"] not in ("photo", "video"):
            continue
        if r.get("m_camera_known") and not has_camera(r) and r.get("m_structure_ok") is not False:
            out.append(Candidate(r["id"], "no_camera_media", "low", "reason.no_camera"))
    return out


# ----------------------------------------------------------------------------- junk


def rule_junk(rows: list[Row], cfg: ClassifyConfig) -> list[Candidate]:
    out = []
    for r in rows:
        name = r["name"]
        lower = name.lower()
        if lower in JUNK_NAMES or lower.startswith(JUNK_PREFIXES):
            out.append(Candidate(r["id"], "junk", "high", "reason.junk_name"))
        elif r["size"] == 0:
            out.append(Candidate(r["id"], "junk", "high", "reason.junk_empty"))
        elif r["kind"] in ("photo", "raw", "video") and r.get("m_structure_ok") == 0:
            out.append(
                Candidate(
                    r["id"],
                    "junk",
                    "high",
                    "reason.junk_broken",
                    {"detail": "invalid file structure"},
                )
            )
        elif r["kind"] in ("photo", "raw") and valid_analysis(r) and r.get("a_decode_error"):
            out.append(
                Candidate(
                    r["id"],
                    "junk",
                    "high",
                    "reason.junk_broken",
                    {"detail": r["a_decode_error"][:80]},
                )
            )
        elif r["kind"] == "sidecar" and r["ext"] == "aae":
            if is_primary(r):
                out.append(Candidate(r["id"], "junk", "high", "reason.junk_orphan_aae"))
            elif cfg.move_all_aae:
                out.append(Candidate(r["id"], "junk", "high", "reason.junk_aae"))
    return out


# ----------------------------------------------------------------------------- blurry / dark


def rule_blurry_dark(rows: list[Row], cfg: ClassifyConfig) -> list[Candidate]:
    out = []
    for r in rows:
        if not is_primary(r) or r["kind"] not in ("photo", "raw") or not valid_analysis(r):
            continue
        sharp, mean, p99 = r.get("a_sharpness"), r.get("a_brightness"), r.get("a_dark_p99")
        if mean is not None and p99 is not None and mean < cfg.dark_mean and p99 < cfg.dark_p99:
            out.append(
                Candidate(r["id"], "blurry_dark", "low", "reason.dark", {"brightness": mean})
            )
        elif (
            sharp is not None
            and sharp < cfg.blur_threshold
            and (mean is None or mean >= cfg.dark_mean)
        ):
            out.append(
                Candidate(
                    r["id"],
                    "blurry_dark",
                    "low",
                    "reason.blurry",
                    {"sharpness": sharp, "threshold": cfg.blur_threshold},
                )
            )
    return out


# ----------------------------------------------------------------------------- similar


def _similar_keeper_key(r: Row) -> tuple[float, float, int]:
    sharp = r.get("a_sharpness")
    sharp_value = float(sharp) if isinstance(sharp, int | float) else -1.0
    pixels = float((r.get("m_width") or 0) * (r.get("m_height") or 0))
    return (-sharp_value, -pixels, int(r["id"]))


def rule_similar(
    rows: list[Row], cfg: ClassifyConfig, run_id: int, excluded: set[int]
) -> list[Candidate]:
    items = [
        r
        for r in rows
        if is_primary(r)
        and r["kind"] == "photo"
        and r["id"] not in excluded
        and valid_analysis(r)
        and r.get("a_phash") is not None
        and r.get("m_date_taken_ts") is not None
    ]
    items.sort(key=lambda r: (r["m_date_taken_ts"], r["id"]))
    window = cfg.similar_window_seconds
    parent: dict[int, int] = {r["id"]: r["id"] for r in items}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    edited_links = {(r["id"], r["edited_of"]) for r in rows if r.get("edited_of")}
    hashes = {r["id"]: phash_unsigned(r["a_phash"]) or 0 for r in items}
    for i, a in enumerate(items):
        ta = a["m_date_taken_ts"]
        for b in items[i + 1 :]:
            if b["m_date_taken_ts"] - ta > window:
                break
            if (a["id"], b["id"]) in edited_links or (b["id"], a["id"]) in edited_links:
                continue
            if hamming(hashes[a["id"]], hashes[b["id"]]) <= cfg.similar_distance:
                union(a["id"], b["id"])
    groups: dict[int, list[Row]] = defaultdict(list)
    for r in items:
        groups[find(r["id"])].append(r)
    out: list[Candidate] = []
    n = 0
    for _root, members in sorted(groups.items()):
        if len(members) < 2:
            continue
        n += 1
        gid = f"s{run_id}-{n}"
        members.sort(key=_similar_keeper_key)
        keeper = members[0]
        out.append(
            Candidate(
                keeper["id"],
                "similar",
                "medium",
                "reason.similar_keeper",
                {"group": gid},
                gid,
                True,
            )
        )
        for m in members[1:]:
            delta = abs((m["m_date_taken_ts"] or 0) - (keeper["m_date_taken_ts"] or 0))
            out.append(
                Candidate(
                    m["id"],
                    "similar",
                    "medium",
                    "reason.similar_member",
                    {"keeper": keeper["rel_path"], "delta": delta},
                    gid,
                )
            )
    return out
