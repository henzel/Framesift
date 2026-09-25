"""Zip dist/Framesift into packaging/out/Framesift-<version>-windows-x64.zip; sign first if a certificate is present."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from framesift import __version__  # noqa: E402

DIST = ROOT / "dist" / "Framesift"
OUT = ROOT / "packaging" / "out"


def sign_if_possible() -> None:
    pfx_b64 = os.environ.get("WINDOWS_CERT_PFX")
    password = os.environ.get("WINDOWS_CERT_PASSWORD")
    if not pfx_b64 or not password:
        print(
            "No Windows signing secrets: producing an unsigned build (see README for SmartScreen)"
        )
        return
    import base64

    pfx = OUT / "cert.pfx"
    OUT.mkdir(parents=True, exist_ok=True)
    pfx.write_bytes(base64.b64decode(pfx_b64))
    signtool = shutil.which("signtool") or _find_signtool()
    if not signtool:
        print("signtool not found; skipping signature")
        return
    for exe in DIST.rglob("*.exe"):
        subprocess.run(
            [
                signtool,
                "sign",
                "/f",
                str(pfx),
                "/p",
                password,
                "/tr",
                "http://timestamp.digicert.com",
                "/td",
                "sha256",
                "/fd",
                "sha256",
                str(exe),
            ],
            check=True,
        )
    pfx.unlink()


def _find_signtool() -> str | None:
    kits = (
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
        / "Windows Kits"
        / "10"
        / "bin"
    )
    candidates = sorted(kits.glob("*/x64/signtool.exe"), reverse=True) if kits.exists() else []
    return str(candidates[0]) if candidates else None


def main() -> None:
    if not DIST.is_dir():
        raise SystemExit(f"missing {DIST} (run pyinstaller first)")
    sign_if_possible()
    OUT.mkdir(parents=True, exist_ok=True)
    target = OUT / f"Framesift-{__version__}-windows-x64.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in DIST.rglob("*"):
            if path.is_file():
                zf.write(path, Path("Framesift") / path.relative_to(DIST))
    print(f"created {target}")


if __name__ == "__main__":
    main()
