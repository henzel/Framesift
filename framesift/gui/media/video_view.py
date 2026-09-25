"""Video playback: Qt Multimedia first, ffmpeg frame streaming as the fallback."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QProcess, QSize, Qt, QUrl, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import QLabel, QStackedWidget, QVBoxLayout, QWidget

from framesift.engine.external import find_binary
from framesift.gui.i18n import tr


class FfmpegFrameView(QLabel):
    """Streams raw RGB frames from an ffmpeg process (no audio). Used when Qt cannot decode."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet("background: black; color: #ddd;")
        self.proc: QProcess | None = None
        self.frame_size = QSize(0, 0)
        self._buffer = bytearray()
        self.paused = False

    def play(self, path: Path, width: int, height: int, fps: float = 15.0) -> None:
        self.stop()
        exe = find_binary("ffmpeg")
        if not exe:
            self.setText(tr("Cannot decode this file."))
            return
        max_w = max(64, min(width, 1280))
        scale = max_w / max(width, 1)
        w, h = int(width * scale) // 2 * 2, int(height * scale) // 2 * 2
        self.frame_size = QSize(w, h)
        self._buffer = bytearray()
        self.proc = QProcess(self)
        self.proc.readyReadStandardOutput.connect(self._read)
        self.proc.start(
            exe,
            [
                "-v",
                "error",
                "-nostdin",
                "-re",
                "-i",
                str(path),
                "-an",
                "-vf",
                f"scale={w}:{h},fps={fps}",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "-",
            ],
        )
        self.paused = False

    def _read(self) -> None:
        if self.proc is None:
            return
        self._buffer += bytes(self.proc.readAllStandardOutput().data())
        frame_bytes = self.frame_size.width() * self.frame_size.height() * 3
        if frame_bytes <= 0:
            return
        while len(self._buffer) >= frame_bytes:
            frame = bytes(self._buffer[:frame_bytes])
            del self._buffer[:frame_bytes]
            img = QImage(
                frame,
                self.frame_size.width(),
                self.frame_size.height(),
                self.frame_size.width() * 3,
                QImage.Format.Format_RGB888,
            )
            self.setPixmap(
                QPixmap.fromImage(img.copy()).scaled(
                    self.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )

    def toggle_pause(self) -> None:
        if self.proc is None:
            return
        # QProcess has no suspend; emulate by (re)starting is overkill: we stop reading instead
        self.paused = not self.paused
        if self.paused:
            self.proc.readyReadStandardOutput.disconnect(self._read)
        else:
            self.proc.readyReadStandardOutput.connect(self._read)

    def stop(self) -> None:
        if self.proc is not None:
            self.proc.readyReadStandardOutput.disconnect(self._read) if not self.paused else None
            self.proc.kill()
            self.proc.waitForFinished(1000)
            self.proc.deleteLater()
            self.proc = None
        self.clear()


class VideoView(QWidget):
    """QMediaPlayer + QVideoWidget with automatic ffmpeg fallback."""

    fallback_used = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.stack = QStackedWidget(self)
        self.video_widget = QVideoWidget()
        self.fallback = FfmpegFrameView()
        self.stack.addWidget(self.video_widget)
        self.stack.addWidget(self.fallback)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.stack)
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video_widget)
        self.player.errorOccurred.connect(self._on_error)
        self.player.mediaStatusChanged.connect(self._on_status)
        self.current: Path | None = None
        self.size_hint = (0, 0)
        self.loop = False
        self.using_fallback = False

    def play(self, path: Path, *, width: int = 0, height: int = 0, loop: bool = False) -> None:
        self.stop()
        self.current = path
        self.size_hint = (width, height)
        self.loop = loop
        self.using_fallback = False
        self.stack.setCurrentWidget(self.video_widget)
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        self.player.play()

    def _on_status(self, status: QMediaPlayer.MediaStatus) -> None:
        if status == QMediaPlayer.MediaStatus.EndOfMedia and self.loop and not self.using_fallback:
            self.player.setPosition(0)
            self.player.play()

    def _on_error(self, error: QMediaPlayer.Error, message: str) -> None:
        if self.current is None or self.using_fallback or error == QMediaPlayer.Error.NoError:
            return
        self.using_fallback = True
        self.player.stop()
        self.stack.setCurrentWidget(self.fallback)
        w, h = self.size_hint
        self.fallback.play(self.current, w or 640, h or 360)
        self.fallback_used.emit(True)

    def toggle_pause(self) -> None:
        if self.using_fallback:
            self.fallback.toggle_pause()
            return
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def toggle_mute(self) -> None:
        self.audio.setMuted(not self.audio.isMuted())

    def stop(self) -> None:
        self.player.stop()
        self.player.setSource(QUrl())
        self.fallback.stop()
        self.current = None
        self.using_fallback = False
