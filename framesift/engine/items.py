"""Items: Live Photo pairs, .AAE/.XMP sidecars and original/edited links."""

from __future__ import annotations

from collections import defaultdict

from framesift.engine.catalog import Catalog
from framesift.engine.config import EDITED_STEM_RE, LIVE_VIDEO_MAX_SECONDS


def link_items(catalog: Catalog) -> dict[str, int]:
    """Recompute item_id / companion_role / edited_of for all present files. Idempotent."""
    rows = catalog.query(
        "SELECT f.id, f.root, f.dir_path, f.stem, f.ext, f.kind, f.detached_at, "
        "m.content_id, m.duration_ms FROM files f LEFT JOIN file_meta m ON m.file_id=f.id "
        "WHERE f.status='present'"
    )
    stats = {"live_pairs": 0, "sidecars": 0, "edited": 0}
    item_of: dict[int, int] = {}
    role_of: dict[int, str] = {}
    edited_of: dict[int, int] = {}

    by_dir: dict[tuple[str, str], list] = defaultdict(list)
    photos_by_cid: dict[tuple[str, str], list] = defaultdict(list)
    for r in rows:
        by_dir[(r["root"], r["dir_path"])].append(r)
        if r["kind"] in ("photo", "raw") and r["content_id"]:
            photos_by_cid[(r["root"], r["content_id"])].append(r)

    paired_videos: set[int] = set()
    # 1. Live Photo pairs by Apple ContentIdentifier (same root; prefer the same folder)
    for r in rows:
        if r["kind"] != "video" or not r["content_id"] or r["detached_at"]:
            continue
        photos = photos_by_cid.get((r["root"], r["content_id"]))
        if not photos:
            continue
        same_dir = [p for p in photos if p["dir_path"] == r["dir_path"]]
        same_stem = [p for p in (same_dir or photos) if p["stem"].lower() == r["stem"].lower()]
        photo = (same_stem or same_dir or photos)[0]
        item_of[r["id"]] = photo["id"]
        role_of[r["id"]] = "live_video"
        paired_videos.add(r["id"])
        stats["live_pairs"] += 1

    # 2. Same-folder rules: stem fallback for Live pairs, sidecars, edited versions
    for (_root, _dir), members in by_dir.items():
        by_stem: dict[str, list] = defaultdict(list)
        for m in members:
            by_stem[m["stem"].lower()].append(m)
        for m in members:
            stem = m["stem"].lower()
            if (
                m["kind"] == "video"
                and m["id"] not in paired_videos
                and not m["content_id"]
                and not m["detached_at"]
            ):
                dur = m["duration_ms"]
                if dur is not None and dur > LIVE_VIDEO_MAX_SECONDS * 1000:
                    continue
                photos = [
                    p
                    for p in by_stem.get(stem, [])
                    if p["kind"] in ("photo", "raw") and not p["content_id"]
                ]
                if photos:
                    item_of[m["id"]] = photos[0]["id"]
                    role_of[m["id"]] = "live_video"
                    paired_videos.add(m["id"])
                    stats["live_pairs"] += 1
            elif m["kind"] == "sidecar":
                owner = _sidecar_owner(stem, by_stem)
                if owner is not None:
                    item_of[m["id"]] = owner
                    role_of[m["id"]] = m["ext"]
                    stats["sidecars"] += 1
            if m["kind"] in ("photo", "raw", "video"):
                em = EDITED_STEM_RE.match(m["stem"])
                if em:
                    original_stem = f"{em.group('prefix')}_{em.group('num')}".lower()
                    originals = [
                        p
                        for p in by_stem.get(original_stem, [])
                        if p["kind"] == m["kind"] or p["kind"] in ("photo", "raw")
                    ]
                    if originals:
                        edited_of[m["id"]] = originals[0]["id"]
                        stats["edited"] += 1

    with catalog._mutex:
        catalog.conn.execute(
            "UPDATE files SET item_id=id, companion_role=NULL, edited_of=NULL WHERE status='present'"
        )
        catalog.conn.executemany(
            "UPDATE files SET item_id=?, companion_role=? WHERE id=?",
            [(item_of[fid], role_of[fid], fid) for fid in item_of],
        )
        catalog.conn.executemany(
            "UPDATE files SET edited_of=? WHERE id=?",
            [(orig, fid) for fid, orig in edited_of.items()],
        )
        catalog.conn.commit()
    return stats


def _sidecar_owner(stem: str, by_stem: dict[str, list]) -> int | None:
    candidates = [stem]
    # IMG_O1234.AAE / IMG_E1234.AAE may belong to IMG_1234
    if "_" in stem:
        prefix, _, rest = stem.rpartition("_")
        if rest[:1] in ("o", "e") and rest[1:].isdigit():
            candidates.append(f"{prefix}_{rest[1:]}")
    for cand in candidates:
        media = [m for m in by_stem.get(cand, []) if m["kind"] in ("photo", "raw", "video")]
        if media:
            media.sort(key=lambda m: (m["kind"] != "photo", m["kind"] != "raw", m["id"]))
            return int(media[0]["id"])
    return None
