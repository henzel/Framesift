# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller one-dir bundle: GUI executable + console CLI sharing one collected tree.

Run from the repository root:  pyinstaller packaging/pyinstaller/framesift.spec
External binaries are picked up from packaging/bin/<platform>/ (see packaging/fetch_binaries.py).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

ROOT = Path(SPECPATH).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from framesift import APP_NAME, __version__  # noqa: E402

PLATFORM = {"darwin": "macos", "win32": "windows"}.get(sys.platform, "linux")
BIN_DIR = ROOT / "packaging" / "bin" / PLATFORM

# Both executables land in one folder (Contents/MacOS on macOS). macOS and Windows file
# systems ignore case, so the names must differ case-insensitively: in 0.1.0 "Framesift"
# and "framesift" were one file there and the CLI replaced the app.
GUI_EXE = "framesift-gui"
CLI_EXE = "framesift"
assert GUI_EXE.lower() != CLI_EXE.lower(), "GUI and CLI executable names collide"

binaries = []
datas = collect_data_files("framesift", includes=["engine/schema/*.sql"])
if BIN_DIR.is_dir():
    for entry in BIN_DIR.iterdir():
        if entry.is_file():
            binaries.append((str(entry), "bin"))
        elif entry.is_dir():
            datas.append((str(entry), f"bin/{entry.name}"))
binaries += collect_dynamic_libs("pi_heif")

hiddenimports = [
    *collect_submodules("framesift"),
    "pi_heif",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PIL.HeifImagePlugin",
    "psutil",
    "send2trash",
]
excludes = ["tkinter", "matplotlib", "scipy", "pandas", "IPython", "PySide6.QtWebEngineCore", "PySide6.Qt3DCore", "PySide6.QtQuick3D"]

gui_a = Analysis(
    [str(ROOT / "framesift" / "gui" / "app.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
    noarchive=False,
)
cli_a = Analysis(
    [str(ROOT / "framesift" / "cli" / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=[*collect_submodules("framesift.engine"), *collect_submodules("framesift.cli"), "pi_heif"],
    excludes=[*excludes, "PySide6"],
    noarchive=False,
)
MERGE((gui_a, GUI_EXE, GUI_EXE), (cli_a, CLI_EXE, CLI_EXE))

gui_pyz = PYZ(gui_a.pure)
cli_pyz = PYZ(cli_a.pure)

icon = None
if PLATFORM == "windows" and (ROOT / "framesift" / "resources" / "framesift.ico").exists():
    icon = str(ROOT / "framesift" / "resources" / "framesift.ico")
elif PLATFORM == "macos" and (ROOT / "framesift" / "resources" / "framesift.icns").exists():
    icon = str(ROOT / "framesift" / "resources" / "framesift.icns")

gui_exe = EXE(
    gui_pyz,
    gui_a.scripts,
    [],
    exclude_binaries=True,
    name=GUI_EXE,
    debug=False,
    strip=False,
    upx=False,
    console=False,
    icon=icon,
    target_arch=None,
)
cli_exe = EXE(
    cli_pyz,
    cli_a.scripts,
    [],
    exclude_binaries=True,
    name=CLI_EXE,
    debug=False,
    strip=False,
    upx=False,
    console=True,
    icon=icon,
)
coll = COLLECT(
    gui_exe,
    gui_a.binaries,
    gui_a.datas,
    cli_exe,
    cli_a.binaries,
    cli_a.datas,
    strip=False,
    upx=False,
    name="Framesift",
)

if PLATFORM == "macos":
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        icon=icon,
        bundle_identifier="dev.framesift.app",
        version=__version__.split("+")[0].replace(".dev0", ""),
        info_plist={
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "12.0",
            "NSRequiresAquaSystemAppearance": False,
            "CFBundleShortVersionString": __version__.replace(".dev0", ""),
            "NSAppleEventsUsageDescription": "Framesift moves files between the folders you chose.",
        },
    )
