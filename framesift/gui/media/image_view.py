"""Zoomable, pannable image view (fit / 100 %)."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QImage, QPainter, QPixmap
from PySide6.QtWidgets import QGraphicsPixmapItem, QGraphicsScene, QGraphicsView


class ImageView(QGraphicsView):
    need_full_resolution = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._item = QGraphicsPixmapItem()
        self._item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        self._scene.addItem(self._item)
        self.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
        )
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.SmartViewportUpdate)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self.setBackgroundBrush(Qt.GlobalColor.black)
        self.fit_mode = True
        self.is_full = False
        self.native_size = (0, 0)

    def set_image(
        self, image: QImage | QPixmap, *, full: bool, native_size: tuple[int, int] | None = None
    ) -> None:
        pix = image if isinstance(image, QPixmap) else QPixmap.fromImage(image)
        self._item.setPixmap(pix)
        self.is_full = full
        self.native_size = native_size or (pix.width(), pix.height())
        self._scene.setSceneRect(QRectF(pix.rect()))
        if self.fit_mode:
            self.fit()
        else:
            self.zoom_100()

    def clear(self) -> None:
        self._item.setPixmap(QPixmap())
        self._scene.setSceneRect(QRectF())

    def fit(self) -> None:
        self.fit_mode = True
        if self._item.pixmap().isNull():
            return
        self.resetTransform()
        rect = self._scene.sceneRect()
        if rect.width() and rect.height():
            self.fitInView(rect, Qt.AspectRatioMode.KeepAspectRatio)
            # never upscale small images beyond 100 %
            scale = self.transform().m11()
            if scale > 1.0:
                self.resetTransform()

    def zoom_100(self) -> None:
        self.fit_mode = False
        self.resetTransform()
        pix = self._item.pixmap()
        if pix.isNull():
            return
        if not self.is_full and self.native_size[0] > pix.width():
            factor = self.native_size[0] / pix.width()
            self.scale(factor, factor)
            self.need_full_resolution.emit()

    def toggle_zoom(self) -> None:
        if self.fit_mode:
            self.zoom_100()
        else:
            self.fit()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self.fit_mode:
            self.fit()
