"""Render README screenshots from the synthetic dataset with QWidget.grab (offscreen).

Usage:  python tests/fixtures/make_screenshots.py [docs/screenshots]
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_LOGGING_RULES", "qt.multimedia.*=false")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from make_dataset import make_dataset  # noqa: E402
from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from framesift.engine.i18n import set_language  # noqa: E402
from framesift.gui.main_window import MainWindow  # noqa: E402
from framesift.gui.prefs import Prefs  # noqa: E402
from framesift.gui.theme import apply_theme  # noqa: E402


def wait(ms: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def wait_job(win: MainWindow, timeout: int = 60000) -> None:
    loop = QEventLoop()
    win.gws.job_finished.connect(lambda *_a: loop.quit())
    win.gws.job_failed.connect(lambda *_a: loop.quit())
    QTimer.singleShot(timeout, loop.quit)
    loop.exec()


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "docs/screenshots")
    out.mkdir(parents=True, exist_ok=True)
    set_language("en")
    app = QApplication([])
    apply_theme(app)
    work = Path(tempfile.mkdtemp(prefix="framesift-shots-"))
    try:
        archive = work / "Archive"
        make_dataset(archive, with_videos=shutil.which("ffmpeg") is not None)
        prefs = Prefs(work / "prefs.json")
        prefs.set("workers", 1)
        win = MainWindow(prefs, cache_dir=work / "cache")
        win.resize(1280, 800)
        win.show()
        win.open_source(str(archive))
        wait_job(win)
        win.browser.sort_box.setCurrentIndex(1)
        wait(4000)  # thumbnails
        win.grab().save(str(out / "browser.png"))
        win.browser.filter_box.setCurrentIndex(1)
        win.browser._open_at(3)
        wait(2500)
        win.grab().save(str(out / "review.png"))
        win.show_page("auto")
        win.auto.run_btn.click()
        wait_job(win)
        wait(3000)
        win.grab().save(str(out / "auto.png"))
        for name, card in win.auto.cards.items():
            card.enabled_box.setChecked(name in ("similar", "duplicates"))
        QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)  # type: ignore[assignment]
        win.auto.apply_btn.click()
        wait_job(win)
        win.show_page("groups")
        win.groups.skip_group()  # the similar group is the more telling one
        wait(2500)
        win.grab().save(str(out / "groups.png"))
        win.show_page("journal")
        wait(500)
        win.grab().save(str(out / "journal.png"))
        win.gws.shutdown()
        for p in sorted(out.glob("*.png")):
            print(p, p.stat().st_size, "bytes")
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
