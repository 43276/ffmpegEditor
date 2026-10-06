"""视频处理主页：首期提供无损源文件保护的 MP4 批量压缩。"""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import QFileDialog, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    CaptionLabel, ComboBox, InfoBar, LineEdit, PushButton, Slider, StrongBodyLabel, InfoBarPosition,
)

from app.ffmpeg_environment import FfmpegCapabilities
from app.video.planner import build_video_batches_for_inputs as BuildVideoBatchesForInputs
from ui.media_presentation import summarize_video_batches as SummarizeVideoBatches
from app.video.models import VideoCompressOptions
from ui.widgets.switch_button import MakeSwitchButton
from ui.widgets.smooth_scroll import SmoothScrollArea as ScrollArea
from ui.tasks.controller import TaskController, TaskState
from ui.tasks.preview import PreviewController, PreviewState
from ui.widgets.log_panel import LogPanel
from ui.widgets.task_panel import TaskPanel
from ui.widgets.page_layout import make_card, build_page_content
from ui.services.settings_service import SettingsService
from ui.services.desktop_actions import DesktopActions
from ui.tasks.jobs import video_inputs_job
from app.task_models import LOG_ERROR, TaskProgress, TaskResult, TaskStatistics


class VideoPage(QWidget):
    """视频压缩页；FFmpeg 可执行路径与图片、音频页面保持同步。"""

    def __init__(self, parent=None, *, settings=None, desktop_actions=None):
        super().__init__(parent)
        self.setObjectName("videoPage")
        self.setAcceptDrops(True)
        self._settings = settings if settings is not None else SettingsService()
        self._ffmpeg_path: str | None = None
        self._caps: FfmpegCapabilities | None = None
        self._inputs: list[Path] = []
        self._preview_batches = None
        self._task_controller = TaskController(self)
        self._preview_controller = PreviewController(self)
        self._desktop_actions = desktop_actions if desktop_actions is not None else DesktopActions()
        self._closing = False
        self._close_requested = False
        self._last_output_dirs: list[str] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll = ScrollArea(self)
        outer.addWidget(self.scroll)
        _, self.content_layout = build_page_content(self.scroll, "视频压缩", "基于 FFmpeg · 批量压缩为 MP4（H.264 / H.265）· 保留源文件不修改")
        self._BuildInputCard()
        self._BuildParamsCard()
        self._BuildTaskCard()
        self._BuildLogCard()
        self._RestoreSettings()
        self._ConnectSignals()
        self._task_controller.logMessage.connect(lambda event: self._AppendLog(event.level, event.message))
        self._task_controller.progressChanged.connect(self._Progress)
        self._task_controller.statisticsChanged.connect(
            lambda stats: self._SetStatistics(stats.total, stats.ok, stats.failed, stats.skipped))
        self._task_controller.completed.connect(self._Finished)
        self._task_controller.stateChanged.connect(self._UpdateState)
        self._task_controller.idle.connect(self._OnTaskIdle)
        self._preview_controller.resultReady.connect(self._OnPreviewReady)
        self._preview_controller.errorOccurred.connect(self._OnPreviewError)
        self._preview_controller.stateChanged.connect(self._OnPreviewState)
        self._preview_controller.idle.connect(self._OnTaskIdle)
        self._UpdateState()

    def _MakeCard(self, title: str):
        card, layout = make_card(self.scroll, title)
        self.content_layout.addWidget(card)
        return card, layout

    def _BuildInputCard(self) -> None:
        card, layout = self._MakeCard("输入")
        row = QHBoxLayout()
        self.path_line = LineEdit(card)
        self.path_line.setClearButtonEnabled(True)
        self.path_line.setMinimumHeight(36)
        self.path_line.setPlaceholderText("选择视频文件 / 文件夹，也可直接拖入")
        self.file_button = PushButton("选择视频…", card)
        self.dir_button = PushButton("选择文件夹…", card)
        row.addWidget(self.path_line, 1)
        row.addWidget(self.file_button)
        row.addWidget(self.dir_button)
        layout.addLayout(row)
        self.preview_label = CaptionLabel("", card)
        layout.addWidget(self.preview_label)
        self.include_output_switch = MakeSwitchButton("重复处理：把已有的 *_output 输出目录也作为输入", card)
        layout.addWidget(self.include_output_switch)
        layout.addWidget(CaptionLabel("目录会递归到包含视频且没有子目录的叶子文件夹；中间目录的视频不会参与处理。", card))

    def _BuildParamsCard(self) -> None:
        card, layout = self._MakeCard("压缩参数")
        for label_text, widget, hint in (
            ("编码器", None, "H.264 兼容性最好；H.265 体积通常更小，但编码更慢、旧设备兼容性较弱。"),
            ("编码速度", None, "越慢通常压缩率越好；中等是适合批量任务的默认值。"),
            ("音频码率", None, "音频将转为 AAC；视频以外的数据流不写入 MP4。"),
        ):
            row = QHBoxLayout()
            field = StrongBodyLabel(label_text, card)
            field.setFixedWidth(88)
            row.addWidget(field)
            if label_text == "编码器":
                self.encoder_combo = ComboBox(card)
                self.encoder_combo.addItem("H.264（兼容优先）", userData="libx264")
                self.encoder_combo.addItem("H.265 / HEVC（体积优先）", userData="libx265")
                row.addWidget(self.encoder_combo, 1)
            elif label_text == "编码速度":
                self.preset_combo = ComboBox(card)
                for preset in ("ultrafast", "veryfast", "faster", "fast", "medium", "slow", "slower"):
                    self.preset_combo.addItem(preset, userData=preset)
                self.preset_combo.setCurrentText("medium")
                row.addWidget(self.preset_combo, 1)
            else:
                self.audio_combo = ComboBox(card)
                for bitrate in ("128k", "192k", "256k", "320k"):
                    self.audio_combo.addItem(bitrate, userData=bitrate)
                self.audio_combo.setCurrentText("192k")
                row.addWidget(self.audio_combo, 1)
            layout.addLayout(row)
            layout.addWidget(CaptionLabel(hint, card))

        crf_row = QHBoxLayout()
        label = StrongBodyLabel("画质 / CRF", card)
        label.setFixedWidth(88)
        self.crf_slider = Slider(Qt.Orientation.Horizontal, card)
        self.crf_slider.setRange(16, 35)
        self.crf_slider.setValue(23)
        self.crf_value = CaptionLabel("23", card)
        self.crf_value.setFixedWidth(32)
        crf_row.addWidget(label)
        crf_row.addWidget(self.crf_slider, 1)
        crf_row.addWidget(self.crf_value)
        layout.addLayout(crf_row)
        layout.addWidget(CaptionLabel("数值越小画质越好、文件越大。23 是 H.264 的常用平衡值；H.265 可先从 26 左右尝试。", card))
        overwrite_row = QHBoxLayout()
        overwrite_label = StrongBodyLabel("同名输出", card)
        overwrite_label.setFixedWidth(88)
        self.overwrite_switch = MakeSwitchButton("覆盖已存在的输出文件", card)
        self.overwrite_switch.setChecked(True)
        overwrite_row.addWidget(overwrite_label)
        overwrite_row.addWidget(self.overwrite_switch, 1)
        layout.addLayout(overwrite_row)
        self.ffmpeg_status = CaptionLabel("正在等待 FFmpeg 检测…", card)
        layout.addWidget(self.ffmpeg_status)

    def _BuildTaskCard(self):
        self.task_panel = TaskPanel(self.scroll, start_text="开始压缩", finish_tooltip="处理完当前文件夹后停止，不再开始后续文件夹", show_exports=False)
        self.content_layout.addWidget(self.task_panel)
        for name in ("start_button", "cancel_button", "end_button", "open_folder_button"):
            setattr(self, name, getattr(self.task_panel, name))
        self.progress = self.task_panel.progress_bar
        self.status = self.task_panel.status_label

    def _BuildLogCard(self):
        self.log_panel = LogPanel(self.scroll)
        self.content_layout.addWidget(self.log_panel)
        self.log_browser = self.log_panel.browser
        self.statistics = self.log_panel.statistics_label
        self.clear_log_button = self.log_panel.clear_button

    def _ConnectSignals(self) -> None:
        self.file_button.clicked.connect(self._BrowseFiles)
        self.dir_button.clicked.connect(self._BrowseDirectory)
        self.path_line.editingFinished.connect(self._SetPathFromText)
        self.path_line.textEdited.connect(lambda _text: self._SetPathFromText())
        self.include_output_switch.checkedChanged.connect(lambda _value: self._RefreshPreview())
        self.crf_slider.valueChanged.connect(lambda value: self.crf_value.setText(str(value)))
        self.start_button.clicked.connect(self._Start)
        self.cancel_button.clicked.connect(self._Cancel)
        self.end_button.clicked.connect(self._End)
        self.open_folder_button.clicked.connect(self._OpenOutputFolders)

    def dragEnterEvent(self, event):  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):  # noqa: N802
        paths = [Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()]
        if paths:
            self._SetInputs(paths)
            event.acceptProposedAction()

    def _BrowseFiles(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "选择视频", self.path_line.text().strip(), _VIDEO_FILTER)
        if paths:
            self._SetInputs([Path(path) for path in paths])

    def _BrowseDirectory(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择视频文件夹", self.path_line.text().strip())
        if path:
            self._SetInputs([Path(path)])

    def _SetPathFromText(self):
        if self.path_line.isReadOnly():
            return
        text = self.path_line.text().strip()
        self._inputs = [Path(text)] if text else []
        self._RefreshPreview()

    def _SetInputs(self, paths: list[Path]):
        self._inputs = list(paths)
        self.path_line.setReadOnly(len(paths) > 1)
        self.path_line.setClearButtonEnabled(len(paths) <= 1)
        self.path_line.setText("; ".join(str(path) for path in paths))
        self._RefreshPreview()

    def _RefreshPreview(self):
        if self._closing:
            return
        paths = tuple(self._inputs)
        self._preview_batches = None
        if not paths:
            self._preview_controller.invalidate()
            self.preview_label.setText("")
            self._UpdateState()
            return
        include_output = self.include_output_switch.isChecked()
        self.preview_label.setText("正在准备预览…")
        self._preview_controller.request(lambda cancel_check: BuildVideoBatchesForInputs(paths, include_output, cancel_check=cancel_check))
        self._UpdateState()


    def SetFfmpegPath(self, path: str | None, capabilities: FfmpegCapabilities | None = None) -> None:
        self._ffmpeg_path = path
        self._caps = capabilities
        if path and capabilities:
            self.ffmpeg_status.setText(f"✓ {capabilities.version}")
        elif path:
            self.ffmpeg_status.setText(f"✓ {path}（正在等待编码器能力检测）")
        else:
            self.ffmpeg_status.setText("✗ FFmpeg 未就绪，请在“设置”页检查 FFmpeg 路径")
        self._UpdateEncoderAvailability()
        self._UpdateState()

    def _UpdateEncoderAvailability(self) -> None:
        has_h265 = bool(self._caps and "libx265" in self._caps.encoders)
        self.encoder_combo.setItemEnabled(1, has_h265)
        if self._caps is not None and not has_h265 and self.encoder_combo.currentData() == "libx265":
            self.encoder_combo.setCurrentIndex(0)

    def _Start(self):
        if self._closing or self._task_controller.active:
            return
        if self._preview_controller.state != PreviewState.VALID or not self._preview_batches:
            self._ShowInfo("无法开始", "输入预览尚未准备完成或输入无效", error=True)
            return
        if not self._caps or not self._ffmpeg_path:
            self._ShowInfo("无法开始", "FFmpeg 尚未就绪，请检查设置页中的 FFmpeg 路径", error=True)
            return
        options = VideoCompressOptions(
            ffmpeg_path=self._ffmpeg_path, encoder=self.encoder_combo.currentData(),
            crf=self.crf_slider.value(), preset=self.preset_combo.currentData(),
            audio_bitrate=self.audio_combo.currentData(), overwrite=self.overwrite_switch.isChecked(),
        )
        total = sum(len(batch.files) for batch in self._preview_batches)
        self._last_output_dirs = []
        self.status.setText("正在校验输入…")
        self.log_panel.clear()
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(0)
        self._SetStatistics(total, 0, 0, 0)
        self._task_controller.start(video_inputs_job(tuple(self._inputs), options, self._caps, include_output_dirs=self.include_output_switch.isChecked()), total=total, supports_finish=True)
        self._UpdateState()

    def _Progress(self, progress: TaskProgress):
        if self._task_controller.state == TaskState.RUNNING:
            self.task_panel.set_progress(progress, show_file=True)
        else:
            self.progress.setRange(0, max(1, progress.total))
            self.progress.setValue(progress.done)

    def _Finished(self, _kind: str, summary: TaskResult) -> None:
        self.task_panel.set_result(summary, action_text="压缩")
        self._last_output_dirs = summary.output_dirs
        self._SetStatistics(summary.total, summary.ok, summary.failed, summary.skipped)
        if summary.cancelled:
            self._ShowInfo("任务已取消", "本次压缩已中止")
        elif summary.early_stopped:
            self._ShowInfo("已按“结束”停止", "当前文件夹已完成，后续文件夹未开始", warning=True)
        elif summary.failed:
            self._ShowInfo("压缩完成（有失败项）", f"成功 {summary.ok}，失败 {summary.failed}，跳过 {summary.skipped}", warning=True)
        else:
            self._ShowInfo("压缩完成", f"成功 {summary.ok} 个视频，跳过 {summary.skipped}")

        self._UpdateState()

    def _OnTaskIdle(self):
        if self._close_requested and not self._task_controller.active and not self._preview_controller.active:
            QTimer.singleShot(0, self.close)

    def _Cancel(self) -> None:
        if self._task_controller.request_cancel():
            self.status.setText("正在取消…")
            self._UpdateState()

    def _End(self) -> None:
        if self._task_controller.request_finish_after_current_batch():
            self.status.setText("正在处理当前文件夹，之后将停止…")
            self._UpdateState()

    def _OpenOutputFolders(self):
        for directory in self._last_output_dirs[:5]:
            try:
                self._desktop_actions.open_output_directory(directory)
            except OSError as exc:
                self._AppendLog(LOG_ERROR, f"打开输出目录失败：{exc}")

    def _SetStatistics(self, total: int, ok: int, failed: int, skipped: int):
        self.log_panel.set_statistics(TaskStatistics(total, ok, failed, skipped, ok + failed + skipped))

    def _AppendLog(self, level: int, text: str):
        self.log_panel.append(level, text)

    def _ShowInfo(self, title: str, content: str, error: bool = False, warning: bool = False) -> None:
        if self._closing:
            return
        method = InfoBar.error if error else InfoBar.warning if warning else InfoBar.success
        method(title=title, content=content, orient=Qt.Orientation.Horizontal, isClosable=True, position=InfoBarPosition.TOP, duration=3500, parent=self)

    def _UpdateState(self):
        running = self._task_controller.active or self._closing
        ready = bool(self._caps and self._ffmpeg_path and self._preview_batches) and self._preview_controller.state == PreviewState.VALID
        self.task_panel.update_state(self._task_controller, ready, self._closing, bool(self._last_output_dirs))
        self.file_button.setEnabled(not running)
        self.dir_button.setEnabled(not running)
        self.path_line.setEnabled(not running)
        self.include_output_switch.setEnabled(not running)

    def _RestoreSettings(self) -> None:
        self.include_output_switch.setChecked(self._settings.value("video/include_output", False, type=bool))
        self.overwrite_switch.setChecked(self._settings.value("video/overwrite", True, type=bool))
        self.crf_slider.setValue(self._settings.value("video/crf", 23, type=int))
        self.preset_combo.setCurrentText(self._settings.value("video/preset", "medium", type=str))
        self.audio_combo.setCurrentText(self._settings.value("video/audio_bitrate", "192k", type=str))
        saved_encoder = self._settings.value("video/encoder", "libx264", type=str)
        encoder_index = self.encoder_combo.findData(saved_encoder)
        if encoder_index >= 0:
            self.encoder_combo.setCurrentIndex(encoder_index)

    def Shutdown(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._settings.setValue("video/include_output", self.include_output_switch.isChecked())
        self._settings.setValue("video/overwrite", self.overwrite_switch.isChecked())
        self._settings.setValue("video/crf", self.crf_slider.value())
        self._settings.setValue("video/preset", self.preset_combo.currentData())
        self._settings.setValue("video/audio_bitrate", self.audio_combo.currentData())
        self._settings.setValue("video/encoder", self.encoder_combo.currentData())
        self._settings.sync()
        self._task_controller.shutdown()
        self._preview_controller.shutdown()
        self._UpdateState()

    def closeEvent(self, event) -> None:  # noqa: N802
        self._close_requested = True
        self.Shutdown()
        if self._task_controller.active or self._preview_controller.active:
            event.ignore()
        else:
            event.accept()

    def _OnPreviewReady(self, batches):
        self._preview_batches = batches
        self.preview_label.setText(SummarizeVideoBatches(batches))
        self._UpdateState()

    def _OnPreviewError(self, message):
        self._preview_batches = None
        self.preview_label.setText(f"✗ {message}")
        self._UpdateState()

    def _OnPreviewState(self, _state):
        self._UpdateState()

    def SetEnvironment(self, environment):
        self.SetFfmpegPath(environment.ffmpeg_path, environment.capabilities)
