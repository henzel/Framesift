from __future__ import annotations

import unicodedata
from pathlib import Path

from framesift.engine.paths import (
    check_roots,
    is_excluded_dir,
    kind_for_ext,
    nfc,
    os_path,
    split_ext,
)


def test_split_ext_and_kinds() -> None:
    assert split_ext("IMG_0001.HEIC") == ("IMG_0001", "heic")
    assert split_ext(".DS_Store") == (".DS_Store", "")
    assert split_ext("archive.tar.gz") == ("archive.tar", "gz")
    assert split_ext("noext") == ("noext", "")
    assert kind_for_ext("jpg") == "photo"
    assert kind_for_ext("cr2") == "raw"
    assert kind_for_ext("mov") == "video"
    assert kind_for_ext("aae") == "sidecar"
    assert kind_for_ext("txt") == "other"


def test_nfc_and_os_path(tmp_path: Path) -> None:
    nfd = unicodedata.normalize("NFD", "ёлка.jpg")
    assert nfc(nfd) == "ёлка.jpg"
    assert os_path(tmp_path, "a/ёлка.jpg", "a/" + nfd) == tmp_path / "a" / nfd
    assert os_path(tmp_path, "a/b.jpg", None) == tmp_path / "a" / "b.jpg"
    assert os_path(tmp_path, "", None) == tmp_path


def test_excluded_dirs() -> None:
    for name in (
        "@eaDir",
        "#recycle",
        "#snapshot",
        ".Trashes",
        ".Spotlight-V100",
        ".fseventsd",
        "$RECYCLE.BIN",
        "System Volume Information",
        ".framesift",
    ):
        assert is_excluded_dir(name), name
    assert is_excluded_dir("Photos Library.photoslibrary")
    assert is_excluded_dir("Lightroom Catalog Previews.lrdata")
    assert not is_excluded_dir("2020")
    assert not is_excluded_dir("recycle")


def test_check_roots_refuses_apple_photos(tmp_path: Path) -> None:
    lib = tmp_path / "Photos Library.photoslibrary" / "originals"
    lib.mkdir(parents=True)
    issues = check_roots(lib, lib / "_r", lib / "_d")
    assert any(i.level == "error" and i.key == "roots.apple_photos_library" for i in issues)


def test_check_roots_lightroom_warning_and_layout(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "Catalog.lrcat").write_bytes(b"")
    issues = check_roots(src, src / "_framesift_review", src / "_framesift_delete")
    keys = {i.key for i in issues}
    assert "roots.lightroom_catalog" in keys
    assert not any(i.level == "error" for i in issues)
    bad = check_roots(src, tmp_path, src / "_d")
    assert any(i.key == "roots.review_contains_source" for i in bad)
    same = check_roots(src, src / "_x", src / "_x")
    assert any(i.key == "roots.review_equals_delete" for i in same)
