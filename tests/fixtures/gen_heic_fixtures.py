"""One-time generator for the tiny HEIC/HEVC fixtures in tests/fixtures/bin/.

Needs `pillow-heif` (which bundles the GPL x265 encoder, so it is NOT a project dependency)
and an ffmpeg with libx265. Run it in a throwaway environment:

    uv venv /tmp/heifgen && VIRTUAL_ENV=/tmp/heifgen uv pip install pillow-heif Pillow
    /tmp/heifgen/bin/python tests/fixtures/gen_heic_fixtures.py

The resulting files are synthetic solid-colour images, a few KB each, committed to the repo.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
from exifwriter import ExifSpec, build_exif  # noqa: E402

BIN = Path(__file__).resolve().parent / "bin"
LIVE_ID_B = "8F2A0C1E-2222-4A2B-9C3D-000000000002"


def scene(seed: int) -> Image.Image:
    im = Image.new("RGB", (96, 72), (30 + seed * 20, 90, 160))
    draw = ImageDraw.Draw(im)
    draw.rectangle([10, 10, 50, 40], fill=(240, 200, 40))
    draw.ellipse([40, 30, 90, 70], fill=(200, 40, 60))
    return im


def main() -> None:
    import pillow_heif

    pillow_heif.register_heif_opener()
    BIN.mkdir(exist_ok=True)
    live = ExifSpec(
        make="Apple",
        model="iPhone 13",
        software="16.1",
        datetime_original="2020:06:05 15:00:00",
        offset_time="+03:00",
        content_id=LIVE_ID_B,
        width=96,
        height=72,
    )
    scene(0).save(BIN / "live_photo.heic", "HEIF", quality=60, exif=build_exif(live))
    scene(1).save(BIN / "plain.heic", "HEIF", quality=60)
    rotated = ExifSpec(
        make="Apple",
        model="iPhone 13",
        datetime_original="2020:06:05 15:01:00",
        orientation=6,
        width=96,
        height=72,
    )
    scene(2).save(BIN / "rotated.heic", "HEIF", quality=60, exif=build_exif(rotated))
    exe = shutil.which("ffmpeg")
    if exe:
        subprocess.run(
            [
                exe,
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=160x120:rate=10",
                "-t",
                "1.5",
                "-c:v",
                "libx265",
                "-x265-params",
                "log-level=none",
                "-pix_fmt",
                "yuv420p",
                "-tag:v",
                "hvc1",
                "-movflags",
                "use_metadata_tags",
                "-metadata",
                "com.apple.quicktime.make=Apple",
                "-metadata",
                "com.apple.quicktime.model=iPhone 13",
                str(BIN / "hevc.mov"),
            ],
            check=True,
        )
    for p in sorted(BIN.iterdir()):
        print(p.name, p.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
