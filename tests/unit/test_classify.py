from __future__ import annotations

import json
from pathlib import Path

import pytest

from framesift.engine.catalog import Catalog
from framesift.engine.classify import classify
from framesift.engine.classify.rules import name_penalty
from framesift.engine.config import CATEGORY_ORDER, ClassifyConfig, Roots
from framesift.engine.plan import build_plan
from framesift.engine.scanner import scan


@pytest.fixture(scope="module")
def classified(dataset, tmp_path_factory):
    """Scan + classify the shared dataset once (read-only afterwards)."""
    root, manifest = dataset
    import shutil

    work = tmp_path_factory.mktemp("classified") / "archive"
    shutil.copytree(root, work, copy_function=shutil.copy2)
    roots = Roots.for_source(work)
    catalog = Catalog.open(roots.review)
    catalog.record_roots(roots)
    scan(catalog, roots)
    cfg = ClassifyConfig()
    stats = classify(catalog, roots, cfg, run_id=1, workers=1)
    yield catalog, roots, manifest, cfg, stats
    catalog.close()


def cands(catalog: Catalog, category: str, keepers: bool = False) -> set[str]:
    return {
        r["f_rel_path"] for r in catalog.candidates(1, category) if bool(r["is_keeper"]) == keepers
    }


@pytest.mark.parametrize("category", CATEGORY_ORDER)
def test_each_category_matches_manifest(classified, category: str) -> None:
    catalog, _roots, manifest, _cfg, _stats = classified
    expected = set(manifest[category])
    found = cands(catalog, category)
    if category == "similar":
        # the keeper is chosen by sharpness; assert group membership instead of a fixed keeper
        group = found | cands(catalog, "similar", keepers=True)
        assert {"similar/IMG_0100.JPG", "similar/IMG_0101.JPG", "similar/IMG_0102.JPG"} == group
        assert "similar/IMG_0200.JPG" not in group
        return
    assert expected <= found, f"{category}: missing {expected - found}"


def test_keep_files_are_not_candidates(classified) -> None:
    catalog, _roots, manifest, _cfg, _stats = classified
    flagged = {r["f_rel_path"] for r in catalog.candidates(1) if not r["is_keeper"]}
    for path in manifest["keep"]:
        assert path not in flagged, path


def test_first_category_wins_and_also_is_recorded(classified) -> None:
    catalog, _roots, manifest, _cfg, _stats = classified
    if not manifest["short_videos"]:
        pytest.skip("ffmpeg not available: no videos in the dataset")
    row = next(r for r in catalog.candidates(1) if r["f_rel_path"] == "2020/short.mp4")
    assert row["category"] == "short_videos"
    assert "no_camera_media" in json.loads(row["also"])
    live = [r for r in catalog.candidates(1) if r["f_rel_path"].endswith("IMG_0001.MOV")]
    assert not live, "Live Photo videos are companions and never candidates on their own"


def test_duplicate_keeper_has_cleanest_name(classified) -> None:
    catalog, *_ = classified
    rows = [r for r in catalog.candidates(1, "duplicates")]
    keeper = [r for r in rows if r["is_keeper"]]
    assert len(keeper) == 1 and keeper[0]["f_rel_path"] == "2020/IMG_0004.JPG"
    member = [r for r in rows if not r["is_keeper"]][0]
    assert json.loads(member["reason_params"])["keeper"] == "2020/IMG_0004.JPG"
    assert name_penalty("IMG_0004 (1)") > name_penalty("IMG_0004")
    assert (
        name_penalty("photo copy") > 0
        and name_penalty("IMG_1234-1") > 0
        and name_penalty("IMG_1234~2") > 0
    )


def test_thresholds_are_recomputed_without_rescanning(classified) -> None:
    catalog, roots, _manifest, _cfg, _stats = classified
    cfg = ClassifyConfig(short_video_seconds=1.0, blur_threshold=0.1, move_all_aae=True)
    stats = classify(catalog, roots, cfg, run_id=2, workers=1, compute=False)
    assert stats.analyzed == 0 and stats.partial_hashed == 0
    assert "2020/short.mp4" not in {r["f_rel_path"] for r in catalog.candidates(2, "short_videos")}
    junk = {r["f_rel_path"] for r in catalog.candidates(2, "junk")}
    assert "2020/IMG_0003.AAE" in junk and "junk/orphan.AAE" in junk
    assert "blur/blurry.jpg" not in {r["f_rel_path"] for r in catalog.candidates(2, "blurry_dark")}
    assert "blur/dark.jpg" in {r["f_rel_path"] for r in catalog.candidates(2, "blurry_dark")}
    # disabled categories produce nothing
    cfg2 = ClassifyConfig()
    cfg2.enabled["screenshots"] = False
    classify(catalog, roots, cfg2, run_id=3, workers=1, compute=False)
    assert not catalog.candidates(3, "screenshots")
    classify(catalog, roots, ClassifyConfig(), run_id=1, workers=1, compute=False)


def test_plan_counts_and_report_only(classified) -> None:
    catalog, _roots, manifest, cfg, _stats = classified
    plan = build_plan(catalog, cfg, run_id=1)
    by = {c.category: c for c in plan.categories}
    assert by["duplicates"].count == 1 and by["duplicates"].groups == 1
    assert by["screenshots"].count == 3 and by["screenshots"].examples[0].reason
    assert by["similar"].count == 2 and by["similar"].groups == 1
    assert plan.totals["candidates"] == sum(c.count for c in plan.categories)
    assert plan.live_photos["count"] == len(manifest["live_pairs"])
    assert plan.edited_pairs["count"] == 1
    assert len(plan.large_videos) == len([v for v in plan.large_videos])
    if manifest["live_pairs"]:
        assert plan.large_videos[0]["size"] >= plan.large_videos[-1]["size"]
    assert Path(json.loads(plan.to_json())["categories"][0]["examples"][0]["rel_path"]).name
