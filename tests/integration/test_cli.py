from __future__ import annotations

import json
from pathlib import Path

from helpers import snapshot
from typer.testing import CliRunner

from framesift.cli.main import app

runner = CliRunner()


def run(*args: str) -> tuple[int, str]:
    result = runner.invoke(app, list(args), catch_exceptions=False)
    return result.exit_code, result.output


def test_full_cli_flow(fresh) -> None:
    root, manifest = fresh
    catalog = str(root / "_framesift_review")
    before = snapshot(root)
    code, out = run("scan", "--source", str(root))
    assert code == 0 and "Scanned" in out and "Report:" in out
    code, out = run("classify", "--catalog", catalog, "--workers", "1")
    assert code == 0 and "duplicates" in out and "Report only" in out
    code, out = run("report", "--catalog", catalog, "--json")
    assert code == 0
    plan = json.loads(out)["plan"]
    assert {c["category"]: c["count"] for c in plan["categories"]}["screenshots"] == 3
    reports = sorted((root / "_framesift_review" / ".framesift" / "reports").iterdir())
    assert any((r / "screenshots.csv").exists() for r in reports)
    code, out = run("apply", "--catalog", catalog)
    assert code == 0 and "DRY RUN" in out and "--yes" in out
    assert snapshot(root) == before
    code, out = run("apply", "--catalog", catalog, "--yes")
    assert code == 0 and out.startswith("Moved")
    assert (root / "_framesift_review" / "screenshots" / "2020" / "IMG_0005.PNG").exists()
    code, out = run("status", "--catalog", catalog, "--json")
    assert code == 0 and json.loads(out)["files"]["review"]["files"] > 0
    code, out = run("undo", "--catalog", catalog, "--all")
    assert code == 0 and "DRY RUN" in out
    code, out = run("undo", "--catalog", catalog, "--all", "--yes")
    assert code == 0 and out.startswith("Undone")
    assert snapshot(root) == before
    code, out = run("purge", "--catalog", catalog)
    assert code == 0 and "DRY RUN: would delete 0 files" in out
    assert (root / "_framesift_review" / ".framesift" / "logs").exists()


def test_cli_refuses_apple_photos_library(tmp_path: Path) -> None:
    lib = tmp_path / "Photos Library.photoslibrary" / "originals"
    lib.mkdir(parents=True)
    result = runner.invoke(app, ["scan", "--source", str(lib)])
    assert result.exit_code == 4


def test_cli_usage_errors(tmp_path: Path) -> None:
    result = runner.invoke(app, ["scan"])
    assert result.exit_code == 2
    result = runner.invoke(app, ["undo", "--source", str(tmp_path)])
    assert result.exit_code == 2
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0 and "framesift" in result.output


def test_cli_lang_ru(fresh) -> None:
    root, _ = fresh
    code, _ = run("scan", "--source", str(root))
    assert code == 0
    code, out = run(
        "classify", "--catalog", str(root / "_framesift_review"), "--workers", "1", "--lang", "ru"
    )
    assert code == 0 and "Точная копия" in out
