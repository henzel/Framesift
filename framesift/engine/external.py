"""Locating the external binaries (ffmpeg, ffprobe, exiftool) in bundles, Docker or PATH."""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
import sys
from pathlib import Path

_ENV_OVERRIDES = {
    "ffmpeg": "FRAMESIFT_FFMPEG",
    "ffprobe": "FRAMESIFT_FFPROBE",
    "exiftool": "FRAMESIFT_EXIFTOOL",
}


def bundled_bin_dirs() -> list[Path]:
    dirs: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        dirs.append(Path(meipass) / "bin")
    exe_dir = Path(sys.executable).resolve().parent
    dirs += [exe_dir / "bin", exe_dir, exe_dir.parent / "Resources" / "bin"]
    return dirs


@functools.lru_cache(maxsize=8)
def find_binary(name: str) -> str | None:
    override = os.environ.get(_ENV_OVERRIDES.get(name, ""), "")
    if override and Path(override).exists():
        return override
    exe = name + (".exe" if sys.platform == "win32" else "")
    for folder in bundled_bin_dirs():
        candidate = folder / exe
        if candidate.is_file():
            return str(candidate)
    # exiftool shipped as a Perl distribution: <bin>/exiftool + <bin>/lib
    if name == "exiftool":
        for folder in bundled_bin_dirs():
            script = folder / "exiftool"
            if script.is_file() and shutil.which("perl"):
                return str(script)
    return shutil.which(exe) or shutil.which(name)


def have(name: str) -> bool:
    return find_binary(name) is not None


def run_hidden(
    args: list[str], *, timeout: float = 120, input_bytes: bytes | None = None
) -> subprocess.CompletedProcess[bytes]:
    """Run a helper binary without a console window on Windows."""
    kwargs: dict = {}
    if sys.platform == "win32":  # pragma: no cover
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(
        args, capture_output=True, timeout=timeout, input=input_bytes, check=False, **kwargs
    )


@functools.lru_cache(maxsize=1)
def ffmpeg_version() -> tuple[int, int] | None:
    exe = find_binary("ffmpeg")
    if not exe:
        return None
    try:
        out = run_hidden([exe, "-version"], timeout=20).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return None
    first = out.split("\n", 1)[0]
    parts = first.split()
    for token in parts:
        if token.startswith("N-"):  # git master snapshot (BtbN "master-latest" builds)
            return (99, 0)
        if token[:1].isdigit():
            nums = token.lstrip("n").split(".")
            try:
                return int(nums[0]), int(nums[1]) if len(nums) > 1 else 0
            except ValueError:
                return None
    return None


def ffmpeg_supports_heif() -> bool:
    """FFmpeg ≥ 7.1 demuxes HEIF/HEIC (including tiled grids)."""
    version = ffmpeg_version()
    return version is not None and version >= (7, 1)
