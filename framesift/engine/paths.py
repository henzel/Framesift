"""Path normalization, exclusions, long paths, volume checks."""

from __future__ import annotations

import os
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from framesift import TEMP_SUFFIX
from framesift.engine.config import (
    APPLE_PHOTOS_SUFFIX,
    EXCLUDED_DIR_NAMES,
    EXCLUDED_DIR_SUFFIXES,
    LIGHTROOM_CATALOG_SUFFIX,
    PHOTO_EXTS,
    RAW_EXTS,
    SIDECAR_EXTS,
    VIDEO_EXTS,
)

IS_WINDOWS = sys.platform == "win32"
NETWORK_FSTYPES = {
    "smbfs",
    "cifs",
    "smb3",
    "nfs",
    "nfs4",
    "afpfs",
    "webdav",
    "fuse.sshfs",
    "sshfs",
    "davfs",
    "fuse.rclone",
    "9p",
    "ncpfs",
    "coda",
}


def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def nfd(text: str) -> str:
    return unicodedata.normalize("NFD", text)


def split_ext(name: str) -> tuple[str, str]:
    """('IMG_0001', 'heic') for 'IMG_0001.HEIC'; ('.DS_Store', '') for dotfiles."""
    if name.startswith(".") and name.count(".") == 1:
        return name, ""
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        return name, ""
    return stem, ext.lower()


def kind_for_ext(ext: str) -> str:
    if ext in PHOTO_EXTS:
        return "photo"
    if ext in RAW_EXTS:
        return "raw"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in SIDECAR_EXTS:
        return "sidecar"
    return "other"


def is_excluded_dir(name: str) -> bool:
    if name in EXCLUDED_DIR_NAMES:
        return True
    lower = name.lower()
    return lower.endswith(EXCLUDED_DIR_SUFFIXES)


def is_temp_file(name: str) -> bool:
    return name.endswith(TEMP_SUFFIX)


def os_path(root: Path, rel_path: str, rel_path_os: str | None = None) -> Path:
    """Absolute path for OS calls: the exact on-disk spelling when it differs from NFC."""
    rel = rel_path_os or rel_path
    if not rel:
        return root
    return root.joinpath(*rel.split("/"))


def to_os(path: Path | str) -> str:
    """String for OS calls. Adds the long-path prefix on Windows when needed."""
    text = str(path)
    if IS_WINDOWS and len(text) >= 240 and not text.startswith("\\\\?\\"):
        text = os.path.abspath(text)
        if text.startswith("\\\\"):
            return "\\\\?\\UNC\\" + text[2:]
        return "\\\\?\\" + text
    return text


def is_inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


@dataclass(frozen=True)
class VolumeInfo:
    mountpoint: str
    fstype: str
    is_network: bool


def volume_info(path: Path) -> VolumeInfo:
    """Best-effort volume information for a path (walks up to an existing ancestor)."""
    p = Path(path)
    while not p.exists() and p.parent != p:
        p = p.parent
    if IS_WINDOWS:
        return _volume_info_windows(p)
    try:
        import psutil

        parts = psutil.disk_partitions(all=True)
    except Exception:  # pragma: no cover - psutil missing or failing
        return VolumeInfo("/", "unknown", False)
    best: VolumeInfo | None = None
    p_str = str(p.resolve())
    for part in parts:
        mp = part.mountpoint
        if p_str == mp or p_str.startswith(mp.rstrip("/") + "/") or mp == "/":
            if best is None or len(mp) > len(best.mountpoint):
                fstype = part.fstype.lower()
                best = VolumeInfo(
                    mp, fstype, fstype in NETWORK_FSTYPES or fstype.startswith("fuse.s")
                )
    return best or VolumeInfo("/", "unknown", False)


def _volume_info_windows(p: Path) -> VolumeInfo:  # pragma: no cover - Windows only
    import ctypes

    text = str(p.resolve())
    if text.startswith("\\\\"):
        return VolumeInfo(text, "unc", True)
    drive = os.path.splitdrive(text)[0] + "\\"
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return VolumeInfo(drive, "unknown", False)
    kind = windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(drive))
    return VolumeInfo(
        drive, {4: "remote", 3: "fixed", 2: "removable"}.get(kind, "unknown"), kind == 4
    )


def is_network_path(path: Path) -> bool:
    return volume_info(path).is_network


def same_volume(a: Path, b: Path) -> bool:
    """True when `a` and `b` are on the same volume (so rename is possible)."""

    def dev(p: Path) -> int | str:
        q = p
        while not q.exists() and q.parent != q:
            q = q.parent
        if IS_WINDOWS:
            return os.path.splitdrive(str(q.resolve()))[0].lower() or str(q.resolve()).lower()
        return os.stat(q).st_dev

    return dev(a) == dev(b)


@dataclass(frozen=True)
class RootIssue:
    level: str  # "error" | "warning"
    key: str  # i18n key
    detail: str = ""


def check_roots(source: Path, review: Path, delete: Path) -> list[RootIssue]:
    """Validate the three folders before any work. Errors block, warnings inform."""
    issues: list[RootIssue] = []
    src = Path(source)
    for part in (src, *src.parents):
        if part.name.lower().endswith(APPLE_PHOTOS_SUFFIX):
            issues.append(RootIssue("error", "roots.apple_photos_library", str(part)))
            break
    if not src.is_dir():
        issues.append(RootIssue("error", "roots.source_missing", str(src)))
        return issues
    try:
        if any(child.name.lower().endswith(LIGHTROOM_CATALOG_SUFFIX) for child in src.iterdir()):
            issues.append(RootIssue("warning", "roots.lightroom_catalog", str(src)))
    except OSError as exc:
        issues.append(RootIssue("error", "roots.source_unreadable", str(exc)))
        return issues
    for name, folder in (("review", review), ("delete", delete)):
        if folder == src or is_inside(src, folder):
            issues.append(RootIssue("error", f"roots.{name}_contains_source", str(folder)))
    if review == delete:
        issues.append(RootIssue("error", "roots.review_equals_delete", str(review)))
    if not (same_volume(src, review) and same_volume(src, delete)):
        issues.append(RootIssue("warning", "roots.different_volumes", ""))
    if is_network_path(src):
        issues.append(RootIssue("info", "roots.network_volume", ""))
    return issues
