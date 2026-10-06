"""共享 FFmpeg 路径、自定义背景与滚动说明。"""
from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QImageReader
from PyQt6.QtWidgets import QFileDialog, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, InfoBar, LineEdit, PushButton, Slider, StrongBodyLabel

from ui.services.settings_service import SettingsService
from ui.widgets.smooth_scroll import SmoothScrollArea
from ui.widgets.page_layout import build_page_content, make_card


class SettingsPage(QWidget):
    ffmpegPathRequested = pyqtSignal(str)
    backgroundChanged = pyqtSignal(str, int, int)

    def __init__(self, settings: SettingsService, parent=None):
        super().__init__(parent)
        self.setObjectName("settingsPage")
        self._settings = settings
        self._background_timer = QTimer(self)
        self._background_timer.setSingleShot(True)
        self._background_timer.setInterval(100)
        self._background_timer.timeout.connect(self._ApplyBackground)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll = SmoothScrollArea(self)
        self.scroll.setWidgetResizable(True)
        outer.addWidget(self.scroll)
        _content, self.content_layout = build_page_content(self.scroll, "设置", "", object_name="settingsContent")
        self._BuildFfmpegCard()
        self._BuildBackgroundCard()
        card, layout = self._MakeCard("滚动")
        layout.addWidget(CaptionLabel("滚动动画随窗口更新，自动适应当前显示器的刷新节奏。", card))
        self.content_layout.addStretch(1)

    def _MakeCard(self, title):
        card, layout = make_card(self.scroll, title)
        self.content_layout.addWidget(card)
        return card, layout

    def _BuildFfmpegCard(self) -> None:
        card, layout = self._MakeCard("FFmpeg")
        row = QHBoxLayout()
        self.ffmpeg_line = LineEdit(card)
        self.ffmpeg_line.setClearButtonEnabled(True)
        self.ffmpeg_line.setPlaceholderText("留空时从 PATH 自动查找")
        self.ffmpeg_line.setText(self._settings.value("ffmpeg/path", "", type=str))
        self.browse_ffmpeg_button = PushButton("浏览…", card)
        self.apply_ffmpeg_button = PushButton("应用 / 重新检测", card)
        row.addWidget(self.ffmpeg_line, 1)
        row.addWidget(self.browse_ffmpeg_button)
        row.addWidget(self.apply_ffmpeg_button)
        layout.addLayout(row)
        layout.addWidget(CaptionLabel("所有处理页面共用此路径；音频元数据功能需要同目录或 PATH 中的 ffprobe。", card))
        self.ffmpeg_status = CaptionLabel("正在检测 FFmpeg…", card)
        layout.addWidget(self.ffmpeg_status)
        self.browse_ffmpeg_button.clicked.connect(self._BrowseFfmpeg)
        self.apply_ffmpeg_button.clicked.connect(self._ApplyFfmpeg)
        self.ffmpeg_line.returnPressed.connect(self._ApplyFfmpeg)

    def _BuildBackgroundCard(self) -> None:
        card, layout = self._MakeCard("背景")
        layout.addWidget(CaptionLabel("默认使用系统背景（Win11 Mica）。选择图片后铺满整个窗口，清除后恢复系统背景。", card))
        row = QHBoxLayout()
        self.background_line = LineEdit(card)
        self.background_line.setReadOnly(True)
        self.background_line.setPlaceholderText("未选择自定义背景")
        self.background_line.setText(self._settings.value("background/path", "", type=str))
        self.browse_background_button = PushButton("选择图片…", card)
        self.clear_background_button = PushButton("清除", card)
        row.addWidget(self.background_line, 1)
        row.addWidget(self.browse_background_button)
        row.addWidget(self.clear_background_button)
        layout.addLayout(row)
        self.transparency_slider, self.transparency_value = self._SliderRow(
            card, layout, "图片透明度", 100, self._settings.value("background/transparency", 25, type=int), "%"
        )
        self.blur_slider, self.blur_value = self._SliderRow(
            card, layout, "模糊程度", 40, self._settings.value("background/blur", 12, type=int), ""
        )
        layout.addWidget(CaptionLabel("透明度 0% 为完全显示图片，100% 为完全透明；模糊 0 为关闭。", card))
        self.background_status = CaptionLabel("", card)
        layout.addWidget(self.background_status)
        self.browse_background_button.clicked.connect(self._BrowseBackground)
        self.clear_background_button.clicked.connect(self._ClearBackground)
        self.transparency_slider.valueChanged.connect(self._ScheduleBackground)
        self.blur_slider.valueChanged.connect(self._ScheduleBackground)
        self._UpdateBackgroundControls()

    def _SliderRow(self, card, layout, text, maximum, value, suffix):
        row = QHBoxLayout()
        label = StrongBodyLabel(text, card)
        label.setFixedWidth(100)
        slider = Slider(Qt.Orientation.Horizontal, card)
        slider.setRange(0, maximum)
        slider.setValue(value)
        value_label = CaptionLabel(f"{slider.value()}{suffix}", card)
        value_label.setFixedWidth(45)
        slider.valueChanged.connect(lambda current: value_label.setText(f"{current}{suffix}"))
        row.addWidget(label)
        row.addWidget(slider, 1)
        row.addWidget(value_label)
        layout.addLayout(row)
        return slider, value_label

    def _BrowseFfmpeg(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择 ffmpeg.exe", self.ffmpeg_line.text(), "FFmpeg (ffmpeg.exe);;所有文件 (*)")
        if path:
            self.ffmpeg_line.setText(path)
            self._ApplyFfmpeg()

    def _ApplyFfmpeg(self) -> None:
        self.ffmpegPathRequested.emit(self.ffmpeg_line.text().strip())

    def SetFfmpegStatus(self, message: str) -> None:
        self.ffmpeg_status.setText(message)

    def SetEnvironment(self, snapshot) -> None:
        self.SetFfmpegStatus(snapshot.message)

    def SetBackgroundStatus(self, message: str) -> None:
        self.background_status.setText(message)

    def _BrowseBackground(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择背景图片", self.background_line.text(), "图片 (*.jpg *.jpeg *.png *.webp *.bmp);;所有文件 (*)")
        if not path:
            return
        reader = QImageReader(path)
        if not reader.canRead():
            InfoBar.error("无法使用背景", reader.errorString(), parent=self)
            return
        self.background_line.setText(path)
        self._ApplyBackground()

    def _ClearBackground(self) -> None:
        self.background_line.clear()
        self._ApplyBackground()

    def _ScheduleBackground(self, _value: int) -> None:
        self._background_timer.start()

    def _UpdateBackgroundControls(self) -> None:
        has_image = bool(self.background_line.text())
        self.transparency_slider.setEnabled(has_image)
        self.blur_slider.setEnabled(has_image)
        self.clear_background_button.setEnabled(has_image)

    def _ApplyBackground(self) -> None:
        self._background_timer.stop()
        path = self.background_line.text()
        transparency, blur = self.transparency_slider.value(), self.blur_slider.value()
        self._settings.setValue("background/path", path)
        self._settings.setValue("background/transparency", transparency)
        self._settings.setValue("background/blur", blur)
        self._settings.sync()
        self._UpdateBackgroundControls()
        self.backgroundChanged.emit(path, transparency, blur)

    def RestoreBackground(self) -> None:
        self._ApplyBackground()

    def Shutdown(self) -> None:
        self._ApplyBackground()
