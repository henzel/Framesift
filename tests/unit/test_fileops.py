from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from framesift import TEMP_SUFFIX
from framesift.engine import fileops
from framesift.engine.fileops import MoveError, cleanup_temp_files, safe_move, unique_destination


def test_unique_destination_suffixes(tmp_path: Path) -> None:
    target = tmp_path / "IMG_1.JPG"
    assert unique_destination(target) == (target, False)
    target.write_bytes(b"x")
    assert unique_destination(target) == (tmp_path / "IMG_1~2.JPG", True)
    (tmp_path / "IMG_1~2.JPG").write_bytes(b"y")
    assert unique_destination(target) == (tmp_path / "IMG_1~3.JPG", True)
    dot = tmp_path / ".DS_Store"
    dot.write_bytes(b"z")
    assert unique_destination(dot) == (tmp_path / ".DS_Store~2", True)


def test_safe_move_rename_keeps_mtime(tmp_path: Path) -> None:
    src = tmp_path / "a" / "f.bin"
    src.parent.mkdir()
    src.write_bytes(b"hello")
    os.utime(src, ns=(1_600_000_000_000_000_000, 1_600_000_000_000_000_000))
    result = safe_move(src, tmp_path / "b" / "c" / "f.bin")
    assert result.method == "rename" and not result.conflict
    assert not src.exists() and result.dst.read_bytes() == b"hello"
    assert result.dst.stat().st_mtime_ns == 1_600_000_000_000_000_000


def test_safe_move_never_overwrites(tmp_path: Path) -> None:
    src = tmp_path / "f.bin"
    src.write_bytes(b"new")
    dst = tmp_path / "out" / "f.bin"
    dst.parent.mkdir()
    dst.write_bytes(b"old")
    result = safe_move(src, dst)
    assert result.conflict and result.dst.name == "f~2.bin"
    assert dst.read_bytes() == b"old" and result.dst.read_bytes() == b"new"


def test_cross_volume_copy_verify(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "src" / "big.bin"
    src.parent.mkdir()
    payload = os.urandom(3 * 1024 * 1024 + 123)
    src.write_bytes(payload)
    os.utime(src, ns=(1_500_000_000_000_000_000, 1_500_000_000_000_000_000))
    real_rename = os.rename
    calls = {"n": 0}

    def fake_rename(a: str, b: str) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        real_rename(a, b)

    monkeypatch.setattr(fileops.os, "rename", fake_rename)
    result = safe_move(src, tmp_path / "dst" / "big.bin")
    assert result.method == "copy_verify"
    assert not src.exists()
    assert result.dst.read_bytes() == payload
    assert result.dst.stat().st_mtime_ns == 1_500_000_000_000_000_000
    assert not list((tmp_path / "dst").glob("*" + TEMP_SUFFIX))


def test_copy_verify_hash_mismatch_keeps_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "f.bin"
    src.write_bytes(b"payload" * 1000)
    monkeypatch.setattr(
        fileops.os, "rename", lambda a, b: (_ for _ in ()).throw(OSError(errno.EXDEV, "x"))
    )

    class BadHash:
        def __init__(self, *a, **k):
            self.n = 0

        def update(self, chunk):
            self.n += len(chunk)

        def hexdigest(self):
            BadHash.counter = getattr(BadHash, "counter", 0) + 1
            return f"h{BadHash.counter}"

    monkeypatch.setattr(fileops.blake3, "blake3", BadHash)
    with pytest.raises(MoveError):
        safe_move(src, tmp_path / "out" / "f.bin")
    assert src.exists() and not (tmp_path / "out" / "f.bin").exists()
    assert not list((tmp_path / "out").glob("*" + TEMP_SUFFIX))


def test_cleanup_temp_files(tmp_path: Path) -> None:
    (tmp_path / "x").mkdir()
    leftover = tmp_path / "x" / ("a.jpg" + TEMP_SUFFIX)
    leftover.write_bytes(b"partial")
    keep = tmp_path / "x" / "a.jpg"
    keep.write_bytes(b"ok")
    removed = cleanup_temp_files(tmp_path)
    assert removed == [leftover] and keep.exists() and not leftover.exists()
