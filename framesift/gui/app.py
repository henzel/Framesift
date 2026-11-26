"""GUI entry point."""

from __future__ import annotations

import sys
from multiprocessing import freeze_support

from framesift import APP_ID, APP_NAME, __version__


def main(argv: list[str] | None = None) -> int:
    freeze_support()
    from PySide6.QtCore import QCoreApplication, QTimer
    from PySide6.QtWidgets import QApplication

    from framesift.engine.i18n import set_language
    from framesift.gui.main_window import MainWindow
    from framesift.gui.prefs import Prefs
    from framesift.gui.theme import apply_theme
    from framesift.gui.ui import application_icon

    argv = list(sys.argv[1:] if argv is None else argv)
    smoke_test = "--smoke-test" in argv
    argv = [argument for argument in argv if argument != "--smoke-test"]
    QCoreApplication.setOrganizationName(APP_NAME)
    QCoreApplication.setApplicationName(APP_NAME)
    QCoreApplication.setApplicationVersion(__version__)
    app = QApplication([APP_ID, *argv])
    app.setWindowIcon(application_icon())
    apply_theme(app)
    prefs = Prefs()
    lang = prefs.get("language", "system")
    set_language(None if lang == "system" else lang)
    window = MainWindow(prefs)
    window.show()
    if smoke_test:
        QTimer.singleShot(100, window.close)
        return app.exec()
    folder = next((arg for arg in argv if not arg.startswith("-")), None)
    if folder:
        QTimer.singleShot(0, lambda: window.open_saved(folder))
    return app.exec()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
