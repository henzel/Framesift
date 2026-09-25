"""Synthetic test dataset generator. No real photos: everything is drawn or rendered here.

Usage:  python tests/fixtures/make_dataset.py <folder> [--many N]
"""

from __future__ import annotations

import argparse
import io
import os
import random
import shutil
import struct
import subprocess
import sys
import unicodedata
import zlib
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

sys.path.insert(0, str(Path(__file__).resolve().parent))
from exifwriter import ExifSpec, build_exif  # noqa: E402

BIN_DIR = Path(__file__).resolve().parent / "bin"
LIVE_ID_A = "8F2A0C1E-1111-4A2B-9C3D-000000000001"
LIVE_ID_B = "8F2A0C1E-2222-4A2B-9C3D-000000000002"  # baked into bin/live_photo.heic
IPHONE = {"make": "Apple", "model": "iPhone 12"}


def _ffmpeg() -> str | None:
    return os.environ.get("FRAMESIFT_FFMPEG") or shutil.which("ffmpeg")


def picture(
    seed: int, size: tuple[int, int] = (320, 240), *, blur: float = 0, dark: bool = False
) -> Image.Image:
    rnd = random.Random(seed)
    im = Image.new("RGB", size, (rnd.randint(40, 200), rnd.randint(40, 200), rnd.randint(40, 200)))
    draw = ImageDraw.Draw(im)
    for _ in range(25):
        x0, y0 = rnd.randint(0, size[0] - 1), rnd.randint(0, size[1] - 1)
        x1, y1 = (
            min(size[0] - 1, x0 + rnd.randint(5, 80)),
            min(size[1] - 1, y0 + rnd.randint(5, 80)),
        )
        color = (rnd.randint(0, 255), rnd.randint(0, 255), rnd.randint(0, 255))
        if rnd.random() < 0.5:
            draw.rectangle([x0, y0, x1, y1], fill=color)
        else:
            draw.ellipse([x0, y0, x1, y1], fill=color)
    if blur:
        im = im.filter(ImageFilter.GaussianBlur(blur))
    if dark:
        im = im.point(lambda v: v // 40)
    return im


def save_jpeg(path: Path, im: Image.Image, exif: ExifSpec | None = None, quality: int = 85) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if exif is not None:
        if exif.thumbnail is None:
            thumb = im.copy()
            thumb.thumbnail((160, 120))
            buf = io.BytesIO()
            thumb.save(buf, "JPEG", quality=70)
            exif.thumbnail = buf.getvalue()
        if exif.width is None:
            exif.width, exif.height = im.size
        im.save(path, "JPEG", quality=quality, exif=build_exif(exif))
    else:
        im.save(path, "JPEG", quality=quality)


def save_png(
    path: Path, im: Image.Image, exif: ExifSpec | None = None, exif_after_idat: bool = False
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if exif is None:
        im.save(path, "PNG", optimize=True)
        return
    exif_bytes = build_exif(exif)[6:]  # eXIf chunk holds the raw TIFF
    if not exif_after_idat:
        im.save(path, "PNG", exif=b"Exif\x00\x00" + exif_bytes, optimize=True)
        return
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    data = buf.getvalue()
    iend = data.rfind(b"IEND") - 4
    chunk = struct.pack(">I", len(exif_bytes)) + b"eXIf" + exif_bytes
    chunk += struct.pack(">I", zlib.crc32(b"eXIf" + exif_bytes) & 0xFFFFFFFF)
    path.write_bytes(data[:iend] + chunk + data[iend:])


def make_video(
    path: Path,
    seconds: float = 2.0,
    size: str = "320x240",
    *,
    tags: dict[str, str] | None = None,
    codec: str = "mpeg4",
) -> bool:
    exe = _ffmpeg()
    if not exe:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    args = [
        exe,
        "-v",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"testsrc=size={size}:rate=10",
        "-t",
        f"{seconds}",
        "-c:v",
        codec,
        "-pix_fmt",
        "yuv420p",
    ]
    if tags:
        args += ["-movflags", "use_metadata_tags"]
        for k, v in tags.items():
            args += ["-metadata", f"{k}={v}"]
    args.append(str(path))
    try:
        subprocess.run(args, check=True, capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def make_dataset(root: Path, *, with_videos: bool = True) -> dict[str, list[str]]:
    """Create the spec §10 dataset under `root`. Returns a manifest of expectations."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, list[str]] = {
        "duplicates": [],
        "screenshots": [],
        "screen_recordings": [],
        "short_videos": [],
        "no_camera_media": [],
        "junk": [],
        "blurry_dark": [],
        "similar": [],
        "live_pairs": [],
        "edited": [],
        "keep": [],
    }
    y = root / "2020"

    # Camera photo + Live Photo MOV (JPEG variant)
    save_jpeg(
        y / "IMG_0001.JPG",
        picture(1),
        ExifSpec(
            **IPHONE,
            datetime_original="2020:06:05 14:03:22",
            offset_time="+03:00",
            content_id=LIVE_ID_A,
        ),
    )
    if with_videos and make_video(
        y / "IMG_0001.MOV",
        2.0,
        tags={
            "com.apple.quicktime.content.identifier": LIVE_ID_A,
            "com.apple.quicktime.make": "Apple",
            "com.apple.quicktime.model": "iPhone 12",
            "com.apple.quicktime.creationdate": "2020-06-05T14:03:22+0300",
        },
    ):
        manifest["live_pairs"].append("2020/IMG_0001.JPG")
    manifest["keep"].append("2020/IMG_0001.JPG")

    # HEIC Live Photo (binary fixture) + MOV
    heic = BIN_DIR / "live_photo.heic"
    if heic.exists():
        shutil.copyfile(heic, y / "IMG_0002.HEIC")
        manifest["keep"].append("2020/IMG_0002.HEIC")
        if with_videos and make_video(
            y / "IMG_0002.MOV",
            1.8,
            tags={
                "com.apple.quicktime.content.identifier": LIVE_ID_B,
                "com.apple.quicktime.make": "Apple",
                "com.apple.quicktime.model": "iPhone 13",
            },
        ):
            manifest["live_pairs"].append("2020/IMG_0002.HEIC")
    plain_heic = BIN_DIR / "plain.heic"
    if plain_heic.exists():
        shutil.copyfile(plain_heic, y / "IMG_0006.HEIC")
        manifest["no_camera_media"].append("2020/IMG_0006.HEIC")

    # Original + .AAE + edited version
    save_jpeg(
        y / "IMG_0003.JPG", picture(3), ExifSpec(**IPHONE, datetime_original="2020:06:06 10:00:00")
    )
    (y / "IMG_0003.AAE").write_text('<?xml version="1.0"?><plist version="1.0"><dict/></plist>')
    save_jpeg(
        y / "IMG_E0003.JPG",
        picture(3).rotate(3),
        ExifSpec(**IPHONE, software="Photos 5.0", datetime_original="2020:06:06 10:00:00"),
    )
    manifest["edited"].append("2020/IMG_E0003.JPG")
    manifest["keep"] += ["2020/IMG_0003.JPG", "2020/IMG_E0003.JPG"]

    # Exact duplicate with " (1)"
    save_jpeg(
        y / "IMG_0004.JPG", picture(4), ExifSpec(**IPHONE, datetime_original="2020:06:07 09:00:00")
    )
    shutil.copyfile(y / "IMG_0004.JPG", y / "IMG_0004 (1).JPG")
    manifest["duplicates"].append("2020/IMG_0004 (1).JPG")
    manifest["keep"].append("2020/IMG_0004.JPG")

    # Screenshots
    save_png(
        y / "IMG_0005.PNG",
        picture(5, (1170, 2532)),
        ExifSpec(user_comment="Screenshot", software="15.1"),
        exif_after_idat=True,
    )
    manifest["screenshots"].append("2020/IMG_0005.PNG")
    save_png(y / "Screenshot_20200608.png", picture(6, (1080, 2400)))
    manifest["screenshots"].append("2020/Screenshot_20200608.png")
    save_png(y / "IMG_0007.PNG", picture(7, (640, 480)), ExifSpec(user_comment="Screenshot"))
    manifest["screenshots"].append("2020/IMG_0007.PNG")

    if with_videos:
        if make_video(y / "RPReplay_Final1591700000.mp4", 2.0, "320x240"):
            manifest["screen_recordings"].append("2020/RPReplay_Final1591700000.mp4")
        if make_video(y / "screenrec.mp4", 4.0, "720x1280"):
            manifest["screen_recordings"].append("2020/screenrec.mp4")
        if make_video(y / "short.mp4", 1.5, "320x240"):
            manifest["short_videos"].append("2020/short.mp4")
        if make_video(
            y / "long.mov",
            5.0,
            "320x240",
            tags={"com.apple.quicktime.make": "Apple", "com.apple.quicktime.model": "iPhone 12"},
        ):
            manifest["keep"].append("2020/long.mov")
        hevc = BIN_DIR / "hevc.mov"
        if hevc.exists():
            shutil.copyfile(hevc, y / "hevc_clip.mov")

    # Messenger saves without camera data
    save_jpeg(root / "messenger" / "photo_no_camera.jpg", picture(8))
    manifest["no_camera_media"].append("messenger/photo_no_camera.jpg")
    (root / "messenger").mkdir(exist_ok=True)
    picture(9, (200, 200)).save(root / "messenger" / "sticker.webp", "WEBP", quality=80)
    manifest["no_camera_media"].append("messenger/sticker.webp")
    picture(10, (120, 90)).save(root / "messenger" / "anim.gif", "GIF")
    manifest["no_camera_media"].append("messenger/anim.gif")

    # Junk
    junk = root / "junk"
    junk.mkdir(exist_ok=True)
    (junk / ".DS_Store").write_bytes(b"\x00" * 64)
    (junk / "._IMG_0001.JPG").write_bytes(b"\x00\x05\x16\x07" + b"\x00" * 60)
    (junk / "Thumbs.db").write_bytes(b"\xd0\xcf\x11\xe0" + b"\x00" * 60)
    (junk / "empty.jpg").write_bytes(b"")
    (junk / "broken.jpg").write_bytes(bytes(random.Random(11).getrandbits(8) for _ in range(4000)))
    (junk / "orphan.AAE").write_text("<plist/>")
    manifest["junk"] += [
        "junk/.DS_Store",
        "junk/._IMG_0001.JPG",
        "junk/Thumbs.db",
        "junk/empty.jpg",
        "junk/broken.jpg",
        "junk/orphan.AAE",
    ]

    # Blurry and dark
    save_jpeg(
        root / "blur" / "blurry.jpg",
        picture(12, blur=12),
        ExifSpec(**IPHONE, datetime_original="2020:07:01 12:00:00"),
    )
    save_jpeg(
        root / "blur" / "dark.jpg",
        picture(13, dark=True),
        ExifSpec(**IPHONE, datetime_original="2020:07:02 12:00:00"),
    )
    manifest["blurry_dark"] += ["blur/blurry.jpg", "blur/dark.jpg"]

    # Similar burst (3 shots within 60 s) + the same scene an hour later (not grouped)
    base = picture(14)
    for n, (secs, tweak) in enumerate([(0, 0), (12, 1), (40, 2)]):
        im = base.copy()
        draw = ImageDraw.Draw(im)
        draw.rectangle([5 + tweak * 3, 5, 12 + tweak * 3, 12], fill=(255, 255, 255))
        save_jpeg(
            root / "similar" / f"IMG_{100 + n:04d}.JPG",
            im,
            ExifSpec(**IPHONE, datetime_original=f"2020:08:01 12:00:{secs:02d}"),
            quality=90 - n,
        )
    manifest["similar"] += ["similar/IMG_0101.JPG", "similar/IMG_0102.JPG"]
    save_jpeg(
        root / "similar" / "IMG_0200.JPG",
        base.copy(),
        ExifSpec(**IPHONE, datetime_original="2020:08:01 13:00:00"),
    )
    manifest["keep"].append("similar/IMG_0200.JPG")

    # Unicode names: Cyrillic + space, NFD, emoji, nested folders
    save_jpeg(
        root / "кириллица" / "фото пробел.jpg",
        picture(15),
        ExifSpec(**IPHONE, datetime_original="2020:09:01 12:00:00"),
    )
    nfd_name = unicodedata.normalize("NFD", "ёлка.jpg")
    save_jpeg(
        root / "nfd" / nfd_name,
        picture(16),
        ExifSpec(**IPHONE, datetime_original="2020:09:02 12:00:00"),
    )
    save_jpeg(
        root / "emoji" / "📷 snap.jpg",
        picture(17),
        ExifSpec(**IPHONE, datetime_original="2020:09:03 12:00:00"),
    )
    save_jpeg(
        root / "nested" / "deep" / "IMG_0300.JPG",
        picture(18),
        ExifSpec(**IPHONE, datetime_original="2020:09:04 12:00:00"),
    )
    manifest["keep"] += [
        "кириллица/фото пробел.jpg",
        "nfd/ёлка.jpg",
        "emoji/📷 snap.jpg",
        "nested/deep/IMG_0300.JPG",
    ]

    # Service folders that must be skipped
    for folder in ("@eaDir", ".Trashes", "#recycle", "#snapshot", "$RECYCLE.BIN"):
        save_jpeg(root / folder / "hidden.jpg", picture(19))
    return manifest


def make_many(root: Path, count: int, *, seed: int = 0) -> None:
    """Many tiny camera JPEGs spread over folders, for performance runs."""
    root = Path(root)
    rnd = random.Random(seed)
    template = picture(seed, (64, 48))
    buf = io.BytesIO()
    template.save(buf, "JPEG", quality=60)
    body = buf.getvalue()
    for i in range(count):
        folder = root / f"{2010 + i % 12}" / f"{1 + (i // 12) % 12:02d}"
        folder.mkdir(parents=True, exist_ok=True)
        ts = f"{2010 + i % 12}:{1 + (i // 12) % 12:02d}:{1 + rnd.randint(0, 27):02d} {rnd.randint(0, 23):02d}:{rnd.randint(0, 59):02d}:{rnd.randint(0, 59):02d}"
        exif = build_exif(ExifSpec(**IPHONE, datetime_original=ts, width=64, height=48))
        # splice the EXIF APP1 segment after SOI
        app1 = b"\xff\xe1" + struct.pack(">H", len(exif) + 2) + exif
        (folder / f"IMG_{i:06d}.JPG").write_bytes(body[:2] + app1 + body[2:])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--many", type=int, default=0, help="also create N tiny JPEGs")
    parser.add_argument("--no-videos", action="store_true")
    args = parser.parse_args()
    manifest = make_dataset(args.folder, with_videos=not args.no_videos)
    if args.many:
        make_many(args.folder / "many", args.many)
    for key, items in manifest.items():
        print(f"{key}: {len(items)}")


if __name__ == "__main__":
    main()
