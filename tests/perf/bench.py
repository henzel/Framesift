"""Performance run on a synthetic archive (spec §5 / §11 targets).

Usage:  python tests/perf/bench.py [--files 50000] [--root /tmp/framesift-bench]
Prints a Markdown table: scan, rescan, classify, report times, peak RSS, catalog size,
GUI first-thumbnail latency and next-item latency (offscreen).
"""

from __future__ import annotations

import argparse
import os
import resource
import shutil
import subprocess
import sys
import time
from pathlib import Path

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
    return resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024


def gui_numbers(root: Path) -> dict[str, float]:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("QT_LOGGING_RULES", "qt.multimedia.*=false")
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication

    from framesift.gui.main_window import MainWindow
    from framesift.gui.prefs import Prefs

    app = QApplication.instance() or QApplication([])
    prefs = Prefs(root / "prefs.json")
    t0 = time.perf_counter()
    win = MainWindow(prefs, cache_dir=root / "cache")
    win.show()
    startup = time.perf_counter() - t0
    t1 = time.perf_counter()
    got: list[float] = []

    def on_thumb(_fid, _img):
        got.append(time.perf_counter() - t1)

    win.gws.thumb_loader.signals.ready.connect(on_thumb)
    win.gws.open_source(root / "archive")  # catalog exists already: no scan here
    model = win.browser.model
    open_time = time.perf_counter() - t1
    rows = model.rowCount()
    for i in range(40):
        model.data(model.index(i), 1)  # DecorationRole → requests thumbnails
    loop = QEventLoop()
    QTimer.singleShot(15000, loop.quit)

    def poll():
        if len(got) >= 20:
            loop.quit()
        else:
            QTimer.singleShot(20, poll)

    poll()
    loop.exec()
    first_thumb = got[0] if got else float("nan")
    twenty_thumbs = got[19] if len(got) >= 20 else float("nan")
    win.browser._open_at(0)
    review = win.review
    # let the prefetch fill
    loop2 = QEventLoop()
    QTimer.singleShot(3000, loop2.quit)
    loop2.exec()
    latencies = []
    for _ in range(20):
        t = time.perf_counter()
        review.act_skip()
        app.processEvents()
        latencies.append(time.perf_counter() - t)
    win.gws.shutdown()
    return {
        "startup_s": startup,
        "open_s": open_time,
        "first_thumb_s": first_thumb,
        "twenty_thumbs_s": twenty_thumbs,
        "next_item_ms": 1000 * sorted(latencies)[len(latencies) // 2],
        "rows": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, default=50000)
    parser.add_argument("--root", type=Path, default=Path("/tmp/framesift-bench"))
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    root: Path = args.root
    archive = root / "archive"
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
    gui = gui_numbers(root)
    print(
        f"\n### Synthetic archive: {args.files:,} tiny JPEGs (+ the test dataset), generated in {gen:.0f} s\n"
    )
    print("| Step | Time | Peak RSS (children so far) |\n|---|---:|---:|")
    for label, dt, rss in rows:
        print(f"| {label} | {dt:.1f} s | {rss:.0f} MB |")
    print(f"| catalog size | {db_mb:.1f} MB | |")
    print(f"| GUI start (window shown) | {gui['startup_s']:.2f} s | |")
    print(f"| GUI open folder ({gui['rows']:,} rows listed) | {gui['open_s']:.2f} s | |")
    print(f"| GUI open folder → first thumbnail | {gui['first_thumb_s']:.2f} s | |")
    print(f"| GUI first 20 thumbnails | {gui['twenty_thumbs_s']:.2f} s | |")
    print(f"| GUI next item (median, prefetched) | {gui['next_item_ms']:.0f} ms | |")
    if not args.keep:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
