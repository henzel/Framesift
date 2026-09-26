"""Smoke-test a PyInstaller bundle: the CLI reports its version and the app starts and exits.

Usage (from the repository root, after pyinstaller): python packaging/smoke_bundle.py <platform>
where <platform> is macos, windows or linux.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

from framesift import __version__


def fail(message: str) -> None:
    print(f"bundle smoke test FAILED: {message}")
    sys.exit(1)


def main() -> None:
    platform = sys.argv[1] if len(sys.argv) > 1 else ""
    if platform not in {"macos", "windows", "linux"}:
        fail(f"unknown platform {platform!r}")
    if platform == "macos":
        app = Path("dist/Framesift.app")
        folder = app / "Contents" / "MacOS"
        info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
        if info.get("CFBundleExecutable") != "framesift-gui":
            fail(f"CFBundleExecutable is {info.get('CFBundleExecutable')!r}, not the app")
    else:
        folder = Path("dist/Framesift")
    suffix = ".exe" if platform == "windows" else ""
    cli = folder / f"framesift{suffix}"
    gui = folder / f"framesift-gui{suffix}"
    names = sorted(p.name for p in folder.iterdir())
    for exe in (cli, gui):
        if not exe.is_file():
            fail(f"{exe} is missing; {folder} has {names[:20]}")
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}

    out = subprocess.run(
        [str(cli), "--version"], capture_output=True, text=True, timeout=120, env=env
    )
    if out.returncode != 0 or __version__ not in out.stdout:
        fail(
            f"CLI --version: exit {out.returncode}, output {out.stdout.strip()!r} {out.stderr[-500:]!r}"
        )
    print(f"CLI ok: {out.stdout.strip()}")

    run = subprocess.run(
        [str(gui), "--smoke-test"], capture_output=True, text=True, timeout=300, env=env
    )
    if run.returncode != 0:
        fail(f"app --smoke-test: exit {run.returncode}, stderr {run.stderr[-2000:]!r}")
    print("app ok: started, built its window and exited")


if __name__ == "__main__":
    main()
