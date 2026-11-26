"""Render README screenshots from the synthetic dataset with QWidget.grab.

Usage:  python tests/fixtures/make_screenshots.py [docs/screenshots]
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path
from time import monotonic

os.environ.setdefault("QT_QPA_PLATFORM", "cocoa" if sys.platform == "darwin" else "offscreen")
os.environ.setdefault("QT_LOGGING_RULES", "qt.multimedia.*=false")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from make_dataset import make_dataset  # noqa: E402
from PySide6.QtCore import QEventLoop, Qt, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from framesift.engine.i18n import set_language  # noqa: E402
from framesift.gui.main_window import MainWindow  # noqa: E402
from framesift.gui.prefs import Prefs  # noqa: E402
from framesift.gui.theme import apply_theme  # noqa: E402


def wait(ms: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def wait_until(predicate, timeout: int = 60) -> None:
    deadline = monotonic() + timeout
    while not predicate():
        if monotonic() >= deadline:
            raise TimeoutError(
                f"Screenshot stage did not finish ({predicate.__code__.co_firstlineno})"
            )
        wait(35)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", nargs="?", type=Path, default=Path("docs/screenshots"))
    parser.add_argument("--appearance", choices=["light", "dark", "system"], default="light")
    parser.add_argument("--language", choices=["en", "ru"], default="en")
    parser.add_argument("--size", default="1280x800")
    args = parser.parse_args()
    out = args.output
    out.mkdir(parents=True, exist_ok=True)
    set_language(args.language)
    app = QApplication([])
    app.styleHints().setColorScheme(
        {
            "light": Qt.ColorScheme.Light,
            "dark": Qt.ColorScheme.Dark,
            "system": Qt.ColorScheme.Unknown,
        }[args.appearance]
    )
    apply_theme(app)
    work = Path(tempfile.mkdtemp(prefix="framesift-shots-"))
    try:
        archive = work / ("Фотоархив" if args.language == "ru" else "Photo Archive")
        make_dataset(archive, with_videos=shutil.which("ffmpeg") is not None)
        prefs = Prefs(work / "prefs.json")
        prefs.set("workers", 1)
        win = MainWindow(prefs, cache_dir=work / "cache")
        win.resize(*(int(side) for side in args.size.split("x")))
        win.show()
        wait(160)
        win.grab().save(str(out / "welcome.png"))
        win.open_source(str(archive))
        wait_until(
            lambda: win.gws.is_open and not win.gws.busy and win.browser.model.rowCount() > 0
        )
        win.browser.sort_box.setCurrentIndex(1)
        wait_until(lambda: not win.browser.model.loading and len(win.browser.model._thumbs) >= 12)
        wait(450)
        win.grab().save(str(out / "browser.png"))
        win.browser.filter_box.setCurrentIndex(1)
        wait_until(lambda: not win.browser.model.loading)
        names = [win.browser.model.index(pos).data() for pos in range(win.browser.model.rowCount())]
        win.browser._open_at(names.index("IMG_0001.JPG"))
        wait_until(lambda: win.review.current_id in win.review.images)
        win.review.toggle_info()
        wait(350)
        win.grab().save(str(out / "review.png"))
        win.show_page("auto")
        wait_until(lambda: win.auto.run_btn.isEnabled())
        win.auto.run_btn.click()
        wait_until(lambda: win.auto.apply_btn.isEnabled())
        wait(450)
        win.grab().save(str(out / "auto.png"))
        for name, card in win.auto.cards.items():
            card.enabled_box.setChecked(name in ("similar", "duplicates"))
        QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)  # type: ignore[assignment]
        wait_until(lambda: win.auto.apply_btn.isEnabled())
        win.auto.apply_btn.click()
        wait_until(lambda: not win.gws.busy)
        win.show_page("groups")
        wait_until(lambda: bool(win.groups.members))
        win.groups.skip_group()  # the similar group is the more telling one
        wait_until(lambda: bool(win.groups.members))
        wait(450)
        win.grab().save(str(out / "groups.png"))
        win.show_page("journal")
        wait_until(lambda: win.journal.model.rowCount() > 0)
        win.grab().save(str(out / "journal.png"))
        win.show_page("settings")
        wait(200)
        win.grab().save(str(out / "settings.png"))
        win.gws.shutdown()
        for p in sorted(out.glob("*.png")):
            print(p, p.stat().st_size, "bytes")
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
