"""Constants, thresholds and root configuration."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from framesift import CATALOG_DIRNAME, DEFAULT_DELETE_DIRNAME, DEFAULT_REVIEW_DIRNAME

PHOTO_EXTS = frozenset(
    {"jpg", "jpeg", "jpe", "heic", "heif", "hif", "png", "gif", "webp", "tif", "tiff"}
)
RAW_EXTS = frozenset({"dng", "cr2", "cr3", "nef", "arw", "orf", "rw2", "raf", "pef", "srw"})
VIDEO_EXTS = frozenset({"mov", "mp4", "m4v", "3gp", "3g2"})
SIDECAR_EXTS = frozenset({"aae", "xmp"})
MEDIA_EXTS = PHOTO_EXTS | RAW_EXTS | VIDEO_EXTS

JUNK_NAMES = frozenset({".ds_store", "thumbs.db", "desktop.ini"})
JUNK_PREFIXES = ("._",)

EXCLUDED_DIR_NAMES = frozenset(
    {
        "@eaDir",
        "#recycle",
        "#snapshot",
        ".Trashes",
        ".Spotlight-V100",
        ".fseventsd",
        "$RECYCLE.BIN",
        "System Volume Information",
        CATALOG_DIRNAME,
        ".thumbnails",
        "@Recycle",
        "@Recently-Snapshot",
        ".TemporaryItems",
    }
)
EXCLUDED_DIR_SUFFIXES = (".photoslibrary", ".lrdata")
APPLE_PHOTOS_SUFFIX = ".photoslibrary"
LIGHTROOM_CATALOG_SUFFIX = ".lrcat"

CATEGORY_ORDER = (
    "duplicates",
    "screenshots",
    "screen_recordings",
    "short_videos",
    "no_camera_media",
    "junk",
    "blurry_dark",
    "similar",
)
REPORT_ONLY_CATEGORIES = ("live_photos", "edited_pairs", "large_videos")
CATEGORY_CONFIDENCE = {
    "duplicates": "high",
    "screenshots": "high",
    "screen_recordings": "high",
    "short_videos": "medium",
    "no_camera_media": "low",
    "junk": "high",
    "blurry_dark": "low",
    "similar": "medium",
}
LIVE_PHOTOS_CATEGORY = "live_photos"

SCREEN_RECORDING_RE = re.compile(r"^RPReplay_Final\d+$", re.IGNORECASE)
EDITED_STEM_RE = re.compile(r"^(?P<prefix>.*)_E(?P<num>\d+)$", re.IGNORECASE)
LIVE_VIDEO_MAX_SECONDS = 5.0

# Portrait pixel dimensions of phone/tablet screens. Matched in both orientations.
# Extend freely; one tuple per device screen.
PHONE_SCREENS: frozenset[tuple[int, int]] = frozenset(
    {
        # iPhone
        (640, 960),
        (640, 1136),
        (750, 1334),
        (1080, 1920),
        (1242, 2208),
        (1125, 2436),
        (1242, 2688),
        (828, 1792),
        (1170, 2532),
        (1080, 2340),
        (1284, 2778),
        (1179, 2556),
        (1290, 2796),
        (1206, 2622),
        (1320, 2868),
        (1260, 2736),
        # iPad
        (768, 1024),
        (1536, 2048),
        (1620, 2160),
        (1640, 2360),
        (1668, 2224),
        (1668, 2388),
        (1668, 2420),
        (2048, 2732),
        (2064, 2752),
        # common Android
        (720, 1280),
        (720, 1520),
        (720, 1600),
        (1080, 2160),
        (1080, 2220),
        (1080, 2280),
        (1080, 2400),
        (1440, 2560),
        (1440, 2960),
        (1440, 3040),
        (1440, 3120),
        (1440, 3200),
    }
)


def is_phone_screen(width: int | None, height: int | None) -> bool:
    if not width or not height:
        return False
    return (width, height) in PHONE_SCREENS or (height, width) in PHONE_SCREENS


@dataclass
class ClassifyConfig:
    """Thresholds and switches for the classifier. Stored in the catalog `settings` table."""

    enabled: dict[str, bool] = field(default_factory=lambda: dict.fromkeys(CATEGORY_ORDER, True))
    short_video_seconds: float = 3.0
    similar_window_seconds: float = 60.0
    similar_distance: int = 6
    blur_threshold: float = 12.0
    dark_mean: float = 0.03
    dark_p99: float = 0.12
    move_all_aae: bool = False
    large_video_top: int = 100

    def is_enabled(self, category: str) -> bool:
        return self.enabled.get(category, True)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, text: str | None) -> ClassifyConfig:
        cfg = cls()
        if not text:
            return cfg
        data: dict[str, Any] = json.loads(text)
        names = {f.name for f in fields(cls)}
        for key, value in data.items():
            if key in names:
                setattr(cfg, key, value)
        cfg.enabled = {c: bool(cfg.enabled.get(c, True)) for c in CATEGORY_ORDER}
        return cfg

    def with_overrides(self, **kwargs: Any) -> ClassifyConfig:
        cfg = ClassifyConfig.from_json(self.to_json())
        for key, value in kwargs.items():
            if value is not None:
                setattr(cfg, key, value)
        return cfg


@dataclass(frozen=True)
class Roots:
    """The three folders. All paths are absolute."""

    source: Path
    review: Path
    delete: Path
    include_subfolders: bool = True

    @classmethod
    def for_source(
        cls,
        source: Path,
        review: Path | None = None,
        delete: Path | None = None,
        include_subfolders: bool = True,
    ) -> Roots:
        source = Path(source).expanduser().resolve()
        review = Path(review).expanduser().resolve() if review else source / DEFAULT_REVIEW_DIRNAME
        delete = Path(delete).expanduser().resolve() if delete else source / DEFAULT_DELETE_DIRNAME
        return cls(
            source=source, review=review, delete=delete, include_subfolders=include_subfolders
        )

    def path(self, root: str) -> Path:
        if root == "source":
            return self.source
        if root == "review":
            return self.review
        if root == "delete":
            return self.delete
        raise ValueError(f"unknown root {root!r}")

    @property
    def catalog_dir(self) -> Path:
        return self.review / CATALOG_DIRNAME

    def relative_layout(self) -> dict[str, str]:
        """Source/Delete expressed relative to Review when possible, for sharing across hosts."""

        def rel(target: Path) -> str:
            try:
                return _relpath(target, self.review)
            except ValueError:
                return str(target)

        return {"source": rel(self.source), "delete": rel(self.delete)}


def _relpath(target: Path, base: Path) -> str:
    import os

    r = os.path.relpath(target, base)
    if len(r) > 200 or r.count("..") > 6:
        raise ValueError("too far")
    return r.replace("\\", "/")
