from __future__ import annotations

from pathlib import Path

import pytest
from helpers import needs_ffmpeg
from make_dataset import BIN_DIR

from framesift.engine.external import ffmpeg_supports_heif
from framesift.engine.imaging import (
    decode_heif,
    ffmpeg_decode_image,
    open_full,
    small_image,
    video_frame,
)
from framesift.engine.metadata import read_media_info
from framesift.engine.thumbs import ThumbCache, render_thumbnail


def _meta(path: Path) -> dict:
    st = path.stat()
    info = read_media_info(
        path, path.suffix.lower().lstrip("."), st.st_size, st.st_mtime_ns, allow_external=False
    )
    data = info.as_fields()
    data["ext"] = path.suffix.lower().lstrip(".")
    data["kind"] = "video" if data["ext"] in ("mov", "mp4") else "photo"
    return data


def test_small_image_uses_embedded_preview(dataset) -> None:
    root, _ = dataset
    im, source = small_image(
        root / "2020" / "IMG_0001.JPG", _meta(root / "2020" / "IMG_0001.JPG"), 256
    )
    assert source == "embedded" and max(im.size) <= 256
    im2, source2 = small_image(
        root / "messenger" / "photo_no_camera.jpg",
        _meta(root / "messenger" / "photo_no_camera.jpg"),
        256,
    )
    assert source2 == "decode" and max(im2.size) <= 256


def test_small_image_heic_via_pi_heif() -> None:
    path = BIN_DIR / "live_photo.heic"
    if not path.exists():
        pytest.skip("HEIC fixture missing")
    im, source = small_image(path, _meta(path), 256)
    assert im.size == (96, 72) and source in ("decode", "heif_thumb")


def test_heic_exif_orientation_is_applied_once() -> None:
    path = BIN_DIR / "rotated.heic"
    if not path.exists():
        pytest.skip("HEIC fixture missing")
    im, source = decode_heif(path, prefer_thumbnail=False)
    assert source == "decode" and im.size == (72, 96)
    assert open_full(path, "heic", "heic").size == (72, 96)


@pytest.mark.skipif(not ffmpeg_supports_heif(), reason="needs FFmpeg >= 7.1 for HEIF")
def test_heic_ffmpeg_fallback() -> None:
    raw = ffmpeg_decode_image(BIN_DIR / "rotated.heic")
    assert raw is not None and raw.size == (96, 72)  # -noautorotate: raw pixels
    im, source = decode_heif(BIN_DIR / "rotated.heic", force_ffmpeg=True)
    assert source == "ffmpeg" and im.size == (72, 96)  # orientation applied by us


@needs_ffmpeg
def test_video_frame_and_thumbnail_cache(dataset, tmp_path: Path) -> None:
    root, manifest = dataset
    if not manifest["short_videos"]:
        pytest.skip("no videos generated")
    video = root / "2020" / "short.mp4"
    frame = video_frame(video, at_seconds=0.5, max_side=120)
    assert frame is not None and max(frame.size) <= 120
    data = render_thumbnail(video, _meta(video), side=160)
    assert data[:2] == b"\xff\xd8"
    cache = ThumbCache(tmp_path / "thumbs", limit_bytes=10_000_000)
    st = video.stat()
    assert cache.get("cat", 1, st.st_size, st.st_mtime_ns) is None
    cache.put("cat", 1, st.st_size, st.st_mtime_ns, data)
    assert cache.get("cat", 1, st.st_size, st.st_mtime_ns) == data
    assert cache.get("cat", 1, st.st_size + 1, st.st_mtime_ns) is None  # stale entry ignored
    for i in range(2, 40):
        cache.put("cat", i, 1, 1, b"\xff\xd8" + b"x" * 400_000)
    assert cache.total_bytes() > 10_000_000
    removed = cache.trim()
    assert removed > 0 and cache.total_bytes() <= 10_000_000
    cache.close()
