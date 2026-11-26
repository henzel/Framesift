"""Shared presentation for the desktop workspace."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget

from framesift.gui.i18n import tr

PAGE_MARGIN = 24


def application_icon() -> QIcon:
    return QIcon(str(Path(__file__).with_name("resources") / "framesift.png"))


def label(text: str = "", role: str = "", *, wrap: bool = False) -> QLabel:
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(wrap)
    if role:
        widget.setProperty("role", role)
    return widget


def button(text: str, *, variant: str = "") -> QPushButton:
    widget = QPushButton(tr(text))
    widget.setAutoDefault(False)
    if variant:
        widget.setProperty("variant", variant)
    return widget


def page(widget: QWidget) -> QVBoxLayout:
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(PAGE_MARGIN, 22, PAGE_MARGIN, 16)
    layout.setSpacing(14)
    return layout


def draw_icon(painter: QPainter, rect: QRectF, name: str, color: QColor) -> None:
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.translate(rect.topLeft())
    painter.scale(rect.width() / 24, rect.height() / 24)
    painter.setPen(
        QPen(color, 1.65, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    )
    path = QPainterPath()
    if name == "library":
        path.addRoundedRect(3, 4, 18, 16, 3, 3)
        path.addEllipse(7, 7, 3, 3)
        path.moveTo(4, 17)
        path.lineTo(9, 13)
        path.lineTo(12, 15)
        path.lineTo(16, 11)
        path.lineTo(21, 15)
    elif name == "review":
        path.addRoundedRect(3, 4, 18, 16, 3, 3)
        path.moveTo(8, 12)
        path.lineTo(11, 15)
        path.lineTo(16, 9)
    elif name == "compare":
        path.addRoundedRect(2, 5, 8, 14, 2, 2)
        path.addRoundedRect(14, 5, 8, 14, 2, 2)
    elif name == "analysis":
        path.moveTo(12, 2)
        path.quadTo(13, 11, 22, 12)
        path.quadTo(13, 13, 12, 22)
        path.quadTo(11, 13, 2, 12)
        path.quadTo(11, 11, 12, 2)
    elif name == "history":
        path.addEllipse(3.5, 3.5, 17, 17)
        path.moveTo(12, 7)
        path.lineTo(12, 12)
        path.lineTo(16, 14)
    elif name == "settings":
        for y, x in ((6, 8), (12, 16), (18, 10)):
            path.moveTo(3, y)
            path.lineTo(x - 2, y)
            path.addEllipse(x - 2, y - 2, 4, 4)
            path.moveTo(x + 2, y)
            path.lineTo(21, y)
    elif name == "trash":
        path.moveTo(4, 6)
        path.lineTo(20, 6)
        path.moveTo(9, 6)
        path.lineTo(9, 3)
        path.lineTo(15, 3)
        path.lineTo(15, 6)
        path.moveTo(6, 6)
        path.lineTo(7, 20)
        path.lineTo(17, 20)
        path.lineTo(18, 6)
        path.moveTo(11, 10)
        path.lineTo(11, 16)
        path.moveTo(14, 10)
        path.lineTo(14, 16)
    elif name == "search":
        path.addEllipse(4, 4, 11, 11)
        path.moveTo(14, 14)
        path.lineTo(20, 20)
    elif name == "refresh":
        path.arcMoveTo(4, 4, 16, 16, 33)
        path.arcTo(4, 4, 16, 16, 33, 308)
        path.moveTo(20, 4)
        path.lineTo(20, 10)
        path.lineTo(14, 10)
    elif name == "play":
        path.moveTo(8, 5)
        path.lineTo(19, 12)
        path.lineTo(8, 19)
        path.closeSubpath()
    else:
        path.moveTo(2.5, 8)
        path.quadTo(2.5, 5, 5, 5)
        path.lineTo(10, 5)
        path.lineTo(12, 7)
        path.lineTo(19, 7)
        path.quadTo(21.5, 7, 21.5, 9.5)
        path.lineTo(21.5, 17)
        path.quadTo(21.5, 19.5, 19, 19.5)
        path.lineTo(5, 19.5)
        path.quadTo(2.5, 19.5, 2.5, 17)
        path.closeSubpath()
    painter.drawPath(path)
    painter.restore()


def icon(name: str, color: QColor, size: int = 17) -> QIcon:
    pixmap = QPixmap(size * 2, size * 2)
    pixmap.setDevicePixelRatio(2)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    draw_icon(painter, QRectF(0, 0, size, size), name, color)
    painter.end()
    return QIcon(pixmap)


def workspace_style(dark: bool) -> str:
    if dark:
        surface, sidebar, panel = "#1d1e21", "#25262a", "#27292e"
        text, secondary, border = "#f2f3f5", "#b4b9c4", "#3b3e45"
        hover, selected, selected_text = "#30323a", "#253a57", "#afd3ff"
        accent, accent_hover, danger = "#0967ce", "#1577df", "#ff9c9c"
        disabled = "#7b818d"
    else:
        surface, sidebar, panel = "#fcfcfd", "#f0f1f4", "#f2f4f7"
        text, secondary, border = "#20232a", "#5a6371", "#dde1e7"
        hover, selected, selected_text = "#e7eaf0", "#dcecff", "#07529f"
        accent, accent_hover, danger = "#0865ce", "#0759b7", "#ae292e"
        disabled = "#747b86"
    return f"""
        QWidget#workspace, QStackedWidget#pages, QWidget#scrollContent {{
            background: {surface}; color: {text};
        }}
        QWidget#sidebar {{ background: {sidebar}; border-right: 1px solid {border}; }}
        QWidget#sidebar QLabel {{ background: transparent; }}
        QLabel[role="title"] {{ font-size: 25px; font-weight: 700; color: {text}; }}
        QLabel[role="hero"] {{ font-size: 28px; font-weight: 700; color: {text}; }}
        QLabel[role="section"] {{ font-size: 15px; font-weight: 650; color: {text}; }}
        QLabel[role="context"], QLabel[role="caption"] {{ color: {secondary}; }}
        QLabel[role="caption"] {{ font-size: 12px; }}
        QLabel[role="folderSection"] {{ font-weight: 650; font-size: 12px; color: {secondary}; }}
        QLabel[role="notice"] {{ background: {panel}; color: {text}; border-radius: 8px; padding: 8px 12px; }}
        QLabel[role="error"] {{ color: {danger}; }}
        QToolButton[role="navigation"] {{
            border: 1px solid transparent; background: transparent; border-radius: 7px;
            color: {text}; padding: 6px 9px; text-align: left; font-size: 13px; font-weight: 500;
        }}
        QToolButton[role="navigation"]:hover {{ background: {hover}; }}
        QToolButton[role="navigation"]:checked {{
            background: {selected}; color: {selected_text}; font-weight: 650;
        }}
        QToolButton[role="navigation"]:focus {{ border-color: {accent}; }}
        QToolButton[role="navigation"]:disabled {{ color: {disabled}; }}
        QPushButton[variant] {{
            color: {text}; background: {panel}; border: 1px solid {border};
            border-radius: 7px; padding: 5px 11px; min-height: 18px;
        }}
        QPushButton[variant]:hover {{ background: {hover}; }}
        QPushButton[variant]:focus {{ border-color: {accent}; }}
        QPushButton[variant="primary"] {{
            color: white; background: {accent}; border-color: {accent}; font-weight: 650;
        }}
        QPushButton[variant="primary"]:hover {{ background: {accent_hover}; }}
        QPushButton[variant="primary"]:focus {{ border-color: white; }}
        QPushButton[variant="quiet"] {{ background: transparent; border-color: transparent; }}
        QPushButton[variant="quiet"]:hover {{ background: {hover}; }}
        QPushButton[variant="quiet"]:checked {{ background: {selected}; color: {selected_text}; }}
        QPushButton[variant="danger"] {{ color: {danger}; }}
        QPushButton[variant]:disabled {{
            background: {panel}; color: {disabled}; border-color: {border};
        }}
        QLineEdit[role="search"], QLineEdit[role="path"] {{
            color: {text}; background: {panel}; border: 1px solid {border};
            border-radius: 8px; padding: 6px 9px; selection-background-color: {accent};
        }}
        QLineEdit[role="search"]:focus, QLineEdit[role="path"]:focus {{ border-color: {accent}; }}
        QTreeView#folders, QListView#gallery, QListWidget#filmstrip, QScrollArea[role="flat"] {{
            border: 0; background: transparent;
        }}
        QTreeView#folders::item {{ border-radius: 6px; min-height: 25px; color: {text}; }}
        QTreeView#folders::item:hover {{ background: {hover}; }}
        QTreeView#folders::item:selected {{ background: {selected}; color: {selected_text}; }}
        QListView#gallery:focus, QTreeView#folders:focus {{ outline: 0; }}
        QWidget#reviewCanvas {{ background: #111214; border-radius: 10px; }}
        QWidget#inspector {{ background: {panel}; border-radius: 10px; }}
        QGroupBox[role="section"] {{
            background: {panel}; border: 1px solid {border}; border-radius: 10px;
            margin-top: 10px; padding: 14px 4px 4px; font-weight: 650;
        }}
        QGroupBox[role="section"]::title {{ subcontrol-origin: margin; left: 12px; padding: 0 4px; }}
        QTableView#history {{
            background: {surface}; alternate-background-color: {panel};
            border: 1px solid {border}; border-radius: 8px; gridline-color: transparent;
        }}
        QHeaderView::section {{ background: {panel}; color: {secondary}; border: 0; padding: 7px; }}
        QStatusBar {{ background: {sidebar}; border-top: 1px solid {border}; }}
        QStatusBar::item {{ border: 0; }}
    """
