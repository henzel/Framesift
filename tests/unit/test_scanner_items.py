from __future__ import annotations

import os
import time
import unicodedata
from pathlib import Path

from conftest import needs_ffmpeg
from make_dataset import BIN_DIR

from framesift.engine.catalog import Catalog
from framesift.engine.config import Roots
from framesift.engine.scanner import scan


def _open(root: Path, include_subfolders: bool = True) -> tuple[Catalog, Roots]:
    roots = Roots.for_source(root, include_subfolders=include_subfolders)
    catalog = Catalog.open(roots.review)
    catalog.record_roots(roots)
    return catalog, roots


def test_scan_counts_exclusions_and_unicode(fresh) -> None:
    root, manifest = fresh
    catalog, roots = _open(root)
    try:
        stats = scan(catalog, roots)
        rows = catalog.files("source")
        paths = {r["rel_path"] for r in rows}
        assert stats.new == len(rows) and stats.errors == 0
        assert not any(
            p.startswith(("@eaDir", ".Trashes", "#recycle", "#snapshot", "$RECYCLE.BIN"))
            for p in paths
        )
        assert stats.skipped_dirs >= 5
        assert "кириллица/фото пробел.jpg" in paths
        assert "emoji/📷 snap.jpg" in paths
        nfd_row = catalog.file_by_path("source", "nfd/ёлка.jpg")
        assert nfd_row is not None
        on_disk = os.listdir(root / "nfd")[0]
        if unicodedata.normalize("NFC", on_disk) != on_disk:
            assert nfd_row["rel_path_os"] == "nfd/" + on_disk
        assert "nested/deep/IMG_0300.JPG" in paths
        # metadata was extracted for media files
        assert catalog.meta(nfd_row["id"])["make"] == "Apple"
    finally:
        catalog.close()


def test_scan_is_incremental(fresh) -> None:
    root, _ = fresh
    catalog, roots = _open(root)
    try:
        first = scan(catalog, roots)
        second = scan(catalog, roots)
        assert second.new == 0 and second.changed == 0 and second.unchanged == first.new
        assert second.meta_extracted == 0
        target = root / "2020" / "IMG_0003.JPG"
        data = target.read_bytes()
        time.sleep(0.01)
        target.write_bytes(data + b"\x00")
        (root / "messenger" / "sticker.webp").unlink()
        third = scan(catalog, roots)
        assert third.changed == 1 and third.missing == 1 and third.meta_extracted == 1
        assert catalog.file_by_path("source", "messenger/sticker.webp")["status"] == "missing"
    finally:
        catalog.close()


def test_scan_skips_review_delete_and_respects_no_subfolders(fresh) -> None:
    root, _ = fresh
    (root / "_framesift_review" / "junk").mkdir(parents=True)
    (root / "_framesift_review" / "junk" / "x.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    (root / "_framesift_delete").mkdir()
    (root / "_framesift_delete" / "y.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    (root / "top.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    catalog, roots = _open(root, include_subfolders=False)
    try:
        scan(catalog, roots)
        source = {r["rel_path"] for r in catalog.files("source")}
        assert source == {"top.jpg"}
        assert {r["rel_path"] for r in catalog.files("review")} == {"junk/x.jpg"}
        assert {r["rel_path"] for r in catalog.files("delete")} == {"y.jpg"}
    finally:
        catalog.close()
    catalog, roots = _open(root, include_subfolders=True)
    try:
        scan(catalog, roots)
        source = {r["rel_path"] for r in catalog.files("source")}
        assert "top.jpg" in source and "2020/IMG_0001.JPG" in source
        assert not any(p.startswith("_framesift_") for p in source)
    finally:
        catalog.close()


@needs_ffmpeg
def test_items_live_pairs_sidecars_edited(fresh) -> None:
    root, manifest = fresh
    catalog, roots = _open(root)
    try:
        scan(catalog, roots)
        photo = catalog.file_by_path("source", "2020/IMG_0001.JPG")
        mov = catalog.file_by_path("source", "2020/IMG_0001.MOV")
        assert mov["item_id"] == photo["id"] and mov["companion_role"] == "live_video"
        if (BIN_DIR / "live_photo.heic").exists():
            heic = catalog.file_by_path("source", "2020/IMG_0002.HEIC")
            mov2 = catalog.file_by_path("source", "2020/IMG_0002.MOV")
            assert mov2["item_id"] == heic["id"] and mov2["companion_role"] == "live_video"
        aae = catalog.file_by_path("source", "2020/IMG_0003.AAE")
        orig = catalog.file_by_path("source", "2020/IMG_0003.JPG")
        assert aae["item_id"] == orig["id"] and aae["companion_role"] == "aae"
        edited = catalog.file_by_path("source", "2020/IMG_E0003.JPG")
        assert edited["edited_of"] == orig["id"] and edited["item_id"] == edited["id"]
        orphan = catalog.file_by_path("source", "junk/orphan.AAE")
        assert orphan["item_id"] == orphan["id"]
        long_video = catalog.file_by_path("source", "2020/long.mov")
        assert long_video["item_id"] == long_video["id"]
        assert len(catalog.item_files(photo["id"])) == 2
    finally:
        catalog.close()
