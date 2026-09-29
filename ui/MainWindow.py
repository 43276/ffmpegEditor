"""应用主窗口：窗口壳、侧栏导航与跨页面资源协调。"""
from __future__ import annotations

from PyQt6.QtCore import QSettings

from qfluentwidgets import FluentIcon, FluentWindow, MSFluentWindow
from qfluentwidgets.common.icon import toQIcon

from ui.AudioPage import AudioPage
from ui.ImagePage import ImagePage


class _MainWindowMixin:
    """供 Mica 与降级窗口基类共享的最小窗口协调逻辑。"""

    def _SetupUi(self) -> None:
        self.setWindowTitle("ffmpeg工具")
        self.resize(1100, 800)
        self.setMinimumSize(900, 650)
        self.setWindowIcon(toQIcon(FluentIcon.PHOTO))

        self._settings = QSettings("CompressImages", "ImageConverter")
        geometry = self._settings.value("window/geometry")
        if geometry:
            self.restoreGeometry(geometry)

        self.image_page = ImagePage(self)
        self.audio_page = AudioPage(self)
        self.image_page.ffmpegPathChanged.connect(self._SyncFfmpegToDependentPages)

        self.addSubInterface(self.image_page, FluentIcon.PHOTO, "图片处理")
        self.addSubInterface(self.audio_page, FluentIcon.MUSIC, "音频处理")
        self.image_page.StartFfmpegProbe()
        self._SyncFfmpegToDependentPages(self.image_page.ffmpeg_path)

    def _SyncFfmpegToDependentPages(self, path: str | None) -> None:
        """让所有依赖 FFmpeg 的页面始终使用同一可执行文件。"""
        self.audio_page.SetFfmpegPath(path)

    def closeEvent(self, event) -> None:  # noqa: N802 -- Qt event name
        self._settings.setValue("window/geometry", self.saveGeometry())
        self._settings.sync()
        self.image_page.Shutdown()
        self.audio_page.Shutdown()
        event.accept()


class MainWindow(_MainWindowMixin, MSFluentWindow):
    """微软商店风格主窗口（Win11 Mica）。"""

    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self._SetupUi()


class FallbackMainWindow(_MainWindowMixin, FluentWindow):
    """Mica 不可用时的常规 Fluent 窗口。"""

    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self._SetupUi()
