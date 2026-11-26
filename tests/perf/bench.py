"""Performance run on a synthetic archive (spec §5 / §11 targets).

Usage:  python tests/perf/bench.py [--files 50000] [--root /tmp/framesift-bench]
Prints a Markdown table: scan, rescan, classify, report times, peak RSS, catalog size,
GUI latency (offscreen by default). Use --gui-only with an already scanned archive to compare
GUI builds against the same catalog without modifying it.
"""

from __future__ import annotations

import argparse
import os
import resource
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))


def run_cli(args: list[str]) -> tuple[float, str]:
    t = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, "-m", "framesift.cli.main", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return time.perf_counter() - t, (proc.stdout + proc.stderr).strip().splitlines()[-1] if (
        proc.stdout + proc.stderr
    ).strip() else ""


def peak_rss_mb() -> float:
    unit = 1024**2 if sys.platform == "darwin" else 1024
    return resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / unit


def gui_numbers(root: Path) -> dict[str, float]:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("QT_LOGGING_RULES", "qt.multimedia.*=false")
    t0 = time.perf_counter()

    from PySide6.QtWidgets import QApplication

    from framesift.gui.main_window import MainWindow
    from framesift.gui.prefs import Prefs

    app = QApplication.instance() or QApplication([])
    with tempfile.TemporaryDirectory(prefix="framesift-ui-bench-") as temp:
        path = Path(temp)
        win = MainWindow(Prefs(path / "prefs.json"), cache_dir=path / "thumbs")
        win.show_page("browser")
        win.show()
        app.processEvents()
        startup = time.perf_counter() - t0

        try:
            return measure_gui(app, win, root / "archive", startup)
        finally:
            win.gws.shutdown()
            win.close()


def measure_gui(app, win, archive: Path, startup: float) -> dict[str, float]:
    from PySide6.QtCore import QTimer

    def pump_until(predicate, label: str, timeout: float = 15) -> None:
        until = time.perf_counter() + timeout
        while not predicate():
            app.processEvents()
            if time.perf_counter() > until:
                raise TimeoutError(label)
            time.sleep(0.001)
        app.processEvents()

    model = win.browser.model
    last_tick = time.perf_counter()
    gaps = []

    def tick() -> None:
        nonlocal last_tick
        now = time.perf_counter()
        gaps.append((now - last_tick) * 1000)
        last_tick = now

    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(10)
    thumbnails: list[float] = []
    win.gws.thumb_loader.signals.ready.connect(lambda *_: thumbnails.append(time.perf_counter()))
    opened = time.perf_counter()
    win.gws.open_source(archive, read_only=True)
    open_call = time.perf_counter() - opened
    pump_until(lambda: model.rowCount() > 0, "gallery rows")
    listed = time.perf_counter() - opened
    pump_until(lambda: bool(thumbnails), "first thumbnail")
    first_image = thumbnails[0] - opened

    sort_times = []
    for i in range(4):
        before = time.perf_counter()
        win.browser.sort_box.setCurrentIndex((i + 1) % 3)
        sort_times.append(1000 * (time.perf_counter() - before))
        # Include work dispatched after the callback returned, in both builds.
        until = time.perf_counter() + 0.24
        while time.perf_counter() < until:
            app.processEvents()
            time.sleep(0.001)

    timer.stop()
    win.browser.filter_box.setCurrentIndex(1)
    pump_until(
        lambda: not getattr(model, "loading", False) and model.rowCount() > 3, "photo filter"
    )
    win.browser._open_at(0)
    review = win.review
    pump_until(lambda: review.current_id in review.images, "first frame in review")
    next_times = []
    for _ in range(min(20, len(review.ids) - 1)):
        before = time.perf_counter()
        review.act_skip()
        pump_until(lambda: review.current_id in review.images, "next frame in review")
        next_times.append(1000 * (time.perf_counter() - before))
    return {
        "startup_ms": startup * 1000,
        "open_call_ms": open_call * 1000,
        "gallery_ms": listed * 1000,
        "first_thumb_ms": first_image * 1000,
        "sort_call_median_ms": median(sort_times),
        "max_tick_gap_ms": max(gaps, default=0),
        "next_item_ms": median(next_times),
        "rows": model.rowCount(),
    }


def print_gui(root: Path) -> None:
    figures = gui_numbers(root)
    print(f"\nGUI measurement ({figures['rows']:,.0f} photos)")
    print("| Measure | Time |")
    print("|---|---:|")
    for key, label in (
        ("startup_ms", "Process imports + visible window"),
        ("open_call_ms", "Open callback on the GUI thread"),
        ("gallery_ms", "Open → gallery rows"),
        ("first_thumb_ms", "Open → first thumbnail"),
        ("sort_call_median_ms", "Sort callback, median"),
        ("max_tick_gap_ms", "Largest gap in a 10 ms UI timer during open and sorting"),
        ("next_item_ms", "Next review frame, median"),
    ):
        print(f"| {label} | {figures[key]:.1f} ms |")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, default=50000)
    parser.add_argument("--root", type=Path, default=Path("/tmp/framesift-bench"))
    parser.add_argument("--keep", action="store_true")
    parser.add_argument(
        "--gui-only", action="store_true", help="measure an existing scanned archive"
    )
    args = parser.parse_args()
    root: Path = args.root
    archive = root / "archive"
    if args.gui_only:
        if not (archive / "_framesift_review" / ".framesift" / "catalog.db").is_file():
            parser.error("--gui-only requires a scanned archive under ROOT/archive")
        print_gui(root)
        return
    if not args.keep and root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    from make_dataset import make_dataset, make_many

    t = time.perf_counter()
    make_dataset(archive, with_videos=shutil.which("ffmpeg") is not None)
    make_many(archive / "many", args.files)
    gen = time.perf_counter() - t
    catalog = archive / "_framesift_review"
    rows = []
    for label, cli in (
        ("scan (first run)", ["scan", "--source", str(archive)]),
        ("scan (rescan, unchanged)", ["scan", "--catalog", str(catalog)]),
        ("classify (all categories)", ["classify", "--catalog", str(catalog)]),
        ("report", ["report", "--catalog", str(catalog)]),
        ("apply (dry run)", ["apply", "--catalog", str(catalog)]),
    ):
        dt, last = run_cli(cli)
        rows.append((label, dt, peak_rss_mb()))
    db_mb = (catalog / ".framesift" / "catalog.db").stat().st_size / 1048576
    print(
        f"\n### Synthetic archive: {args.files:,} tiny JPEGs (+ the test dataset), generated in {gen:.0f} s\n"
    )
    print("| Step | Time | Peak RSS (children so far) |\n|---|---:|---:|")
    for label, dt, rss in rows:
        print(f"| {label} | {dt:.1f} s | {rss:.0f} MB |")
    print(f"| catalog size | {db_mb:.1f} MB | |")
    print_gui(root)
    if not args.keep:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
