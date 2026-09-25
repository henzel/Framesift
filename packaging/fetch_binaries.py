"""Fetch the LGPL ffmpeg/ffprobe builds and exiftool for bundling into packaging/bin/<platform>/.

Sources (all verified against published checksums where the publisher provides them):
  * Windows and Linux: BtbN FFmpeg-Builds, the *-lgpl variants (no GPL components).
  * macOS: built from source by packaging/macos/build_ffmpeg.sh (there is no LGPL prebuilt).
  * exiftool: the Windows 64-bit package and the Perl distribution for macOS/Linux
    (Perl Artistic License / GPL 1+ dual; called as a separate process).

Usage:  python packaging/fetch_binaries.py [--platform windows|linux|macos] [--arch x86_64|arm64]
"""

from __future__ import annotations

import argparse
import hashlib
import io
import platform
import re
import shutil
import stat
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FFMPEG_SERIES = "master"  # BtbN publishes master-latest-* assets under the "latest" release
BTBN = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
EXIFTOOL_SITE = "https://exiftool.org/"


def download(url: str) -> bytes:
    print(f"  fetching {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "framesift-packaging"})
    with urllib.request.urlopen(req, timeout=300) as resp:  # noqa: S310
        return resp.read()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def btbn_asset(plat: str, arch: str) -> str:
    suffix = "" if FFMPEG_SERIES == "master" else f"-{FFMPEG_SERIES.lstrip('n')}"
    if plat == "windows":
        return f"ffmpeg-{FFMPEG_SERIES}-latest-win64-lgpl{suffix}.zip"
    target = "linuxarm64" if arch == "arm64" else "linux64"
    return f"ffmpeg-{FFMPEG_SERIES}-latest-{target}-lgpl{suffix}.tar.xz"


def fetch_ffmpeg(plat: str, arch: str, out: Path) -> None:
    if plat == "macos":
        print("macOS: run packaging/macos/build_ffmpeg.sh (LGPL build from source)")
        return
    asset = btbn_asset(plat, arch)
    data = download(BTBN + asset)
    try:
        expected = download(BTBN + asset + ".sha256").decode().split()[0]
        if sha256(data) != expected:
            raise SystemExit(f"checksum mismatch for {asset}")
    except Exception as exc:  # the .sha256 companion file is optional upstream
        print(f"  (no checksum verification: {exc})")
    names = ("ffmpeg", "ffprobe")
    if plat == "windows":
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for member in zf.namelist():
                base = Path(member).name.lower()
                if base in {n + ".exe" for n in names}:
                    (out / base).write_bytes(zf.read(member))
                elif base == "license.txt" and "/" in member:
                    (out / "LICENSE-ffmpeg.txt").write_bytes(zf.read(member))
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:xz") as tf:
            for member in tf.getmembers():
                base = Path(member.name).name
                if base in names and member.isfile():
                    extracted = tf.extractfile(member)
                    assert extracted is not None
                    target = out / base
                    target.write_bytes(extracted.read())
                    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                elif base == "LICENSE.txt" and member.isfile():
                    extracted = tf.extractfile(member)
                    assert extracted is not None
                    (out / "LICENSE-ffmpeg.txt").write_bytes(extracted.read())
    print(f"  ffmpeg/ffprobe -> {out}")


def exiftool_version() -> str:
    text = download(EXIFTOOL_SITE + "ver.txt").decode().strip()
    if not re.match(r"^\d+\.\d+$", text):
        raise SystemExit(f"unexpected exiftool version string {text!r}")
    return text


def _first_available(urls: list[str]) -> bytes | None:
    for url in urls:
        try:
            return download(url)
        except Exception as exc:  # 404, timeouts, TLS: try the next mirror
            print(f"  ({exc})")
    return None


def _extract_perl_dist(data: bytes, out: Path) -> bool:
    """Unpack the exiftool script and lib/ from a Perl distribution tarball (exiftool.org or GitHub)."""
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        found = False
        for member in tf.getmembers():
            parts = Path(member.name).parts[1:]
            if not parts or not member.isfile():
                continue
            extracted = tf.extractfile(member)
            if extracted is None:
                continue
            if parts[0] == "exiftool" and len(parts) == 1:
                target = out / "exiftool"
                target.write_bytes(extracted.read())
                target.chmod(0o755)
                found = True
            elif parts[0] == "lib":
                target = out.joinpath(*parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(extracted.read())
            elif parts[0] in ("README", "LICENSE") and len(parts) == 1:
                (out / f"{parts[0]}-exiftool.txt").write_bytes(extracted.read())
        return found


def fetch_exiftool(plat: str, arch: str, out: Path) -> None:
    """Best effort: exiftool is optional at runtime, so a missing download is a warning, not an error."""
    try:
        ver = exiftool_version()
    except Exception as exc:
        print(f"  WARNING: cannot resolve the exiftool version ({exc}); bundling without exiftool")
        return
    if plat == "windows":
        asset = f"exiftool-{ver}_64.zip"
        data = _first_available(
            [
                EXIFTOOL_SITE + asset,
                f"https://sourceforge.net/projects/exiftool/files/{asset}/download",
            ]
        )
        if data is None:
            print("  WARNING: exiftool for Windows not available; bundling without exiftool")
            return
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for member in zf.namelist():
                path = Path(member)
                if path.name.lower() in ("exiftool(-k).exe", "exiftool.exe"):
                    (out / "exiftool.exe").write_bytes(zf.read(member))
                elif "exiftool_files" in member and not member.endswith("/"):
                    rel = member.split("exiftool_files/", 1)[1]
                    target = out / "exiftool_files" / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(zf.read(member))
    else:
        asset = f"Image-ExifTool-{ver}.tar.gz"
        data = _first_available(
            [
                EXIFTOOL_SITE + asset,
                f"https://sourceforge.net/projects/exiftool/files/{asset}/download",
                f"https://github.com/exiftool/exiftool/archive/refs/tags/{ver}.tar.gz",
            ]
        )
        if data is None or not _extract_perl_dist(data, out):
            print("  WARNING: exiftool distribution not available; bundling without exiftool")
            return
    print(f"  exiftool {ver} -> {out}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    default_plat = {"darwin": "macos", "win32": "windows"}.get(sys.platform, "linux")
    default_arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x86_64"
    parser.add_argument("--platform", default=default_plat, choices=["windows", "linux", "macos"])
    parser.add_argument("--arch", default=default_arch, choices=["x86_64", "arm64"])
    parser.add_argument("--skip-ffmpeg", action="store_true")
    parser.add_argument("--skip-exiftool", action="store_true")
    args = parser.parse_args()
    out = ROOT / "packaging" / "bin" / args.platform
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    if not args.skip_ffmpeg:
        fetch_ffmpeg(args.platform, args.arch, out)
    if not args.skip_exiftool:
        fetch_exiftool(args.platform, args.arch, out)
    print("done:", sorted(p.name for p in out.iterdir()))


if __name__ == "__main__":
    main()
