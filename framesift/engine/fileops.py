"""Safe moves: rename on the same volume, copy + hash verify + unlink across volumes, never overwrite."""

from __future__ import annotations

import errno
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import blake3

from framesift import TEMP_SUFFIX
from framesift.engine.paths import to_os

COPY_CHUNK = 4 * 1024 * 1024


class MoveError(Exception):
    pass


@dataclass(frozen=True)
class MoveResult:
    src: Path
    dst: Path
    method: str  # rename | copy_verify
    conflict: bool


def unique_destination(dst: Path) -> tuple[Path, bool]:
    """Return a free path: `dst` itself or `name~2.ext`, `name~3.ext`, …"""
    if not dst.exists() and not dst.is_symlink():
        return dst, False
    stem, suffix = dst.stem, dst.suffix
    if dst.name.startswith(".") and dst.name.count(".") == 1:
        stem, suffix = dst.name, ""
    n = 2
    while True:
        cand = dst.with_name(f"{stem}~{n}{suffix}")
        if not cand.exists() and not cand.is_symlink():
            return cand, True
        n += 1
        if n > 10000:
            raise MoveError(f"cannot find a free name for {dst}")


def suffixed_name(name: str, n: int) -> str:
    p = Path(name)
    if name.startswith(".") and name.count(".") == 1:
        return f"{name}~{n}"
    return f"{p.stem}~{n}{p.suffix}"


def safe_move(src: Path, dst: Path, *, dry_run: bool = False) -> MoveResult:
    """Move `src` to `dst` (or a suffixed sibling). Contents are never overwritten or altered."""
    src_os = to_os(src)
    if not os.path.lexists(src_os):
        raise MoveError(f"source vanished: {src}")
    final, conflict = unique_destination(dst)
    if dry_run:
        return MoveResult(src, final, "dry_run", conflict)
    os.makedirs(to_os(final.parent), exist_ok=True)
    final_os = to_os(final)
    try:
        os.rename(src_os, final_os)
        return MoveResult(src, final, "rename", conflict)
    except OSError as exc:
        if exc.errno not in (errno.EXDEV,) and not _is_cross_device(exc):
            raise MoveError(f"rename failed: {exc}") from exc
    copy_verify(src, final)
    return MoveResult(src, final, "copy_verify", conflict)


def _is_cross_device(exc: OSError) -> bool:
    winerror = getattr(exc, "winerror", None)
    return winerror == 17  # ERROR_NOT_SAME_DEVICE


def copy_verify(src: Path, dst: Path) -> None:
    """Copy with attributes to a temp name, verify BLAKE3 of the copy, rename into place, unlink source."""
    tmp = dst.with_name(dst.name + TEMP_SUFFIX)
    src_os, tmp_os, dst_os = to_os(src), to_os(tmp), to_os(dst)
    h_src = blake3.blake3()
    try:
        with open(src_os, "rb") as fin, open(tmp_os, "wb") as fout:
            while True:
                chunk = fin.read(COPY_CHUNK)
                if not chunk:
                    break
                h_src.update(chunk)
                fout.write(chunk)
            fout.flush()
            os.fsync(fout.fileno())
        shutil.copystat(src_os, tmp_os, follow_symlinks=False)
        h_dst = blake3.blake3()
        with open(tmp_os, "rb") as fin:
            while True:
                chunk = fin.read(COPY_CHUNK)
                if not chunk:
                    break
                h_dst.update(chunk)
        if h_src.hexdigest() != h_dst.hexdigest():
            raise MoveError("hash mismatch after copy")
        os.rename(tmp_os, dst_os)
    except Exception:
        try:
            if os.path.exists(tmp_os):
                os.unlink(tmp_os)
        except OSError:
            pass
        raise
    os.unlink(src_os)


def cleanup_temp_files(root: Path) -> list[Path]:
    """Remove leftover *.framesift-part files from interrupted cross-volume moves."""
    removed: list[Path] = []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if name.endswith(TEMP_SUFFIX):
                p = Path(dirpath) / name
                try:
                    p.unlink()
                    removed.append(p)
                except OSError:
                    pass
    return removed
