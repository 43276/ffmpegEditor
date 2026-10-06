"""应用主窗口：窗口壳、侧栏导航与跨页面资源协调。"""
from __future__ import annotations

from PyQt6.QtCore import QSettings, QTimer
from PyQt6.QtWidgets import QAbstractScrollArea

from qfluentwidgets import FluentIcon, FluentWindow, MSFluentWindow, NavigationItemPosition
from qfluentwidgets.common.icon import toQIcon

from ui.audio.page import AudioPage
from ui.widgets.background import BackgroundLayer
from ui.widgets.cached_icon import CachedIcon
from ui.services.ffmpeg_service import FfmpegService
from ui.services.settings_service import SettingsService
from ui.services.desktop_actions import DesktopActions
from ui.image.page import ImagePage
from ui.settings.page import SettingsPage
from ui.widgets.smooth_scroll import EnableSmoothScrolling
from ui.video.page import VideoPage
from ui.tasks.controller import TaskController
from ui.tasks.preview import PreviewController


class _MainWindowMixin:
    """供 Mica 与降级窗口基类共享的最小窗口协调逻辑。"""

    def _SetupUi(self) -> None:
        self._closing = False
        self._shutdown_started = False
        self._close_scheduled = False
        self._shutdown_sources = []
        self.setWindowTitle("ffmpeg工具")
        self.resize(1100, 800)
        self.setMinimumSize(900, 650)
        self.setWindowIcon(toQIcon(FluentIcon.PHOTO))

        self._settings = SettingsService(QSettings("CompressImages", "ImageConverter"))
        self._desktop_actions = DesktopActions()
        geometry = self._settings.value("window/geometry")
        if geometry:
            self.restoreGeometry(geometry)

        self._background_layer = BackgroundLayer(self)
        self._background_layer.setGeometry(self.rect())
        self._ffmpeg_service = FfmpegService(self._settings, self)
        self.image_page = ImagePage(self, settings=self._settings, desktop_actions=self._desktop_actions)
        self.audio_page = AudioPage(self, settings=self._settings, desktop_actions=self._desktop_actions)
        self.video_page = VideoPage(self, settings=self._settings, desktop_actions=self._desktop_actions)
        self.settings_page = SettingsPage(self._settings, self)
        for area in self.findChildren(QAbstractScrollArea):
            EnableSmoothScrolling(area)
        self.settings_page.ffmpegPathRequested.connect(self._ffmpeg_service.SetPath)
        self.settings_page.backgroundChanged.connect(self._ConfigureBackground)
        self._ffmpeg_service.environmentChanged.connect(self._SyncEnvironment)

        self.addSubInterface(self.image_page, CachedIcon(FluentIcon.PHOTO), "图片处理", isTransparent=True)
        self.addSubInterface(self.audio_page, CachedIcon(FluentIcon.MUSIC), "音频处理", isTransparent=True)
        self.addSubInterface(self.video_page, CachedIcon(FluentIcon.VIDEO), "视频处理", isTransparent=True)
        self.addSubInterface(self.settings_page, CachedIcon(FluentIcon.SETTING), "设置", position=NavigationItemPosition.BOTTOM, isTransparent=True)
        self._background_layer.lower()
        self.settings_page.RestoreBackground()
        QTimer.singleShot(0, self._ffmpeg_service.Start)

    def _SyncEnvironment(self, snapshot) -> None:
        self.image_page.SetEnvironment(snapshot)
        self.audio_page.SetEnvironment(snapshot)
        self.video_page.SetEnvironment(snapshot)
        self.settings_page.SetEnvironment(snapshot)

    def _ConfigureBackground(self, path: str, transparency: int, blur: int) -> None:
        error = self._background_layer.Configure(path, transparency, blur)
        self.settings_page.SetBackgroundStatus(f"✗ {error}" if error else "")

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if hasattr(self, "_background_layer"):
            self._background_layer.setGeometry(self.rect())

    def closeEvent(self, event) -> None:  # noqa: N802 -- Qt event name
        if not self._closing:
            self._closing = True
            # 弹窗中的导出任务也属于窗口资源；先登记，再同时请求停止。
            self._shutdown_sources = [*self.findChildren(TaskController),
                                      *self.findChildren(PreviewController), self._ffmpeg_service]
            for source in self._shutdown_sources:
                source.idle.connect(self._TryCompleteClose)
            self._settings.setValue("window/geometry", self.saveGeometry())
            self.settings_page.Shutdown()
            self._settings.sync()
            self.image_page.Shutdown()
            self.audio_page.Shutdown()
            self.video_page.Shutdown()
            self._ffmpeg_service.Shutdown()
            self._shutdown_started = True
        if any(source.active for source in self._shutdown_sources):
            event.ignore()
            self.setWindowTitle("ffmpeg工具 · 正在停止后台任务…")
        else:
            event.accept()

    def _TryCompleteClose(self) -> None:
        if (self._shutdown_started and not self._close_scheduled
                and not any(source.active for source in self._shutdown_sources)):
            self._close_scheduled = True
            QTimer.singleShot(0, self.close)


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
