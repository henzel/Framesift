"""GUI entry point."""

from __future__ import annotations

import sys
from pathlib import Path

from framesift import APP_ID, APP_NAME, __version__


def main(argv: list[str] | None = None) -> int:
    from PySide6.QtCore import QCoreApplication, QTimer
    from PySide6.QtWidgets import QApplication

    from framesift.engine.i18n import set_language
    from framesift.gui.main_window import MainWindow
    from framesift.gui.prefs import Prefs
    from framesift.gui.theme import apply_theme

    argv = list(sys.argv[1:] if argv is None else argv)
    # Packaging check used by the release workflow: start, build the window, close, exit 0.
    smoke_test = "--smoke-test" in argv
    argv = [a for a in argv if a != "--smoke-test"]
    QCoreApplication.setOrganizationName(APP_NAME)
    QCoreApplication.setApplicationName(APP_NAME)
    QCoreApplication.setApplicationVersion(__version__)
    app = QApplication([APP_ID, *argv])
    apply_theme(app)
    prefs = Prefs()
    lang = prefs.get("language", "system")
    set_language(None if lang == "system" else lang)
    window = MainWindow(prefs)
    window.show()
    if smoke_test:
        QTimer.singleShot(1500, window.close)
        return app.exec()
    folder = next((a for a in argv if not a.startswith("-")), None) or next(
        iter(prefs.get("recent", [])), None
    )
    if folder and Path(folder).is_dir():
        saved = prefs.source_settings(folder)
        window.settings.set_source(folder)
        window.open_source(
            folder,
            saved.get("review", ""),
            saved.get("delete", ""),
            bool(saved.get("include_subfolders", True)),
        )
    return app.exec()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
