"""Light and dark palettes that follow the system colour scheme."""

from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication


def _palette(dark: bool) -> QPalette:
    p = QPalette()
    if dark:
        window, base, text = QColor(37, 37, 40), QColor(28, 28, 30), QColor(230, 230, 230)
        alt, button, highlight = QColor(44, 44, 48), QColor(52, 52, 56), QColor(52, 120, 246)
        disabled = QColor(120, 120, 120)
    else:
        window, base, text = QColor(242, 242, 244), QColor(255, 255, 255), QColor(20, 20, 20)
        alt, button, highlight = QColor(236, 236, 238), QColor(230, 230, 232), QColor(0, 122, 255)
        disabled = QColor(150, 150, 150)
    p.setColor(QPalette.ColorRole.Window, window)
    p.setColor(QPalette.ColorRole.WindowText, text)
    p.setColor(QPalette.ColorRole.Base, base)
    p.setColor(QPalette.ColorRole.AlternateBase, alt)
    p.setColor(QPalette.ColorRole.Text, text)
    p.setColor(QPalette.ColorRole.Button, button)
    p.setColor(QPalette.ColorRole.ButtonText, text)
    p.setColor(QPalette.ColorRole.Highlight, highlight)
    p.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    p.setColor(QPalette.ColorRole.ToolTipBase, base)
    p.setColor(QPalette.ColorRole.ToolTipText, text)
    p.setColor(QPalette.ColorRole.PlaceholderText, disabled)
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, disabled)
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, disabled)
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, disabled)
    return p


def is_dark(app: QApplication) -> bool:
    return app.styleHints().colorScheme() == Qt.ColorScheme.Dark


def apply_theme(app: QApplication) -> None:
    """Fusion + our palettes on Windows/Linux; macOS keeps its native look and follows the system itself."""
    if sys.platform != "darwin":
        app.setStyle("Fusion")
        app.setPalette(_palette(is_dark(app)))
        app.styleHints().colorSchemeChanged.connect(
            lambda _scheme: app.setPalette(_palette(is_dark(app)))
        )
