"""主窗口：类 Windows 11（WinUI）风格的单页主界面。

窗口基类优先 MSFluentWindow（微软商店风格 + Win11 Mica），
构造失败时由入口回退到 FluentWindow。
"""
from __future__ import annotations

import html
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QSettings, Qt, QThread, pyqtSignal
from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import (
    QButtonGroup,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QVBoxLayout,
    QWidget,
)

from qfluentwidgets import (
    CaptionLabel,
    ComboBox,
    FluentIcon,
    FluentWindow,
    HeaderCardWidget,
    InfoBar,
    LineEdit,
    MSFluentWindow,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    RadioButton,
    ScrollArea,
    Slider,
    SpinBox,
    StrongBodyLabel,
    SwitchButton,
    TextBrowser,
    TitleLabel,
)
from qfluentwidgets.common.icon import toQIcon
from qfluentwidgets.common.style_sheet import isDarkTheme

from app.Converter import (
    ConverterError,
    FfmpegCapabilities,
    LocateFfmpeg,
    ProbeFfmpeg,
)
from app.Core import (
    BuildBatches,
    ConvertOptions,
    InputError,
    IsLosslessExtension,
    RelocateBatchOutputs,
    SummarizeBatches,
    TARGET_FORMAT_OPTIONS,
)
from ui.Worker import LOG_ERROR, LOG_INFO, LOG_OK, LOG_WARN, ConvertWorker

# 深浅主题下的日志颜色
_LOG_COLORS = {
    LOG_OK: ("#0f7b0f", "#7adfa0"),
    LOG_WARN: ("#9a6700", "#f5c26b"),
    LOG_ERROR: ("#c42b1c", "#ff9aa2"),
}
_LEVEL_MARKS = {LOG_OK: "✓ ", LOG_WARN: "⚠ ", LOG_ERROR: "✗ "}


class _ProbeThread(QThread):
    """探测 ffmpeg 能力的后台线程。"""

    probeFinished = pyqtSignal(object)
    probeFailed = pyqtSignal(str)

    def __init__(self, ffmpeg_path: str, parent=None):
        super().__init__(parent)
        self._ffmpeg_path = ffmpeg_path

    def run(self) -> None:
        try:
            self.probeFinished.emit(ProbeFfmpeg(self._ffmpeg_path))
        except Exception as exc:  # noqa: BLE001 —— 统一转成用户可见消息
            self.probeFailed.emit(str(exc))


class _MainUiMixin:
    """共享的界面组装与交互逻辑（供两种窗口基类复用）。"""

    # ---- 初始化 -------------------------------------------------------
    def _SetupUi(self) -> None:
        self.setWindowTitle("图片压缩转换工具")
        self.resize(1100, 800)
        self.setMinimumSize(900, 650)
        self.setWindowIcon(toQIcon(FluentIcon.PHOTO))

        self._settings = QSettings("CompressImages", "ImageConverter")
        self._caps: FfmpegCapabilities | None = None
        self._active_ffmpeg_path: str | None = None
        self._workers: list[ConvertWorker] = []
        self._worker_summaries: dict[int, dict] = {}
        self._thread_progress: dict[int, tuple[int, int]] = {}
        self._planned_total = 0
        self._probe_thread: _ProbeThread | None = None
        self._pending_probe_path: str | None = None
        self._preview_batches = None
        self._last_output_dirs: list[str] = []

        self._BuildScrollContent()
        self._BuildInputCard()
        self._BuildOutputCard()
        self._BuildParamsCard()
        self._BuildActionCard()
        self._BuildLogCard()
        self._ConnectSignals()

        # 首页导航项
        self.home_interface.setObjectName("homeInterface")
        self.addSubInterface(self.home_interface, FluentIcon.PHOTO, "图片压缩")

        # 初始状态
        self._RestoreSettings()
        self._OnOutputModeChanged()
        self._UpdateQualityUi()
        self._UpdateStartState()
        self._ProbeCurrentFfmpeg()

    def _BuildScrollContent(self) -> None:
        self.home_interface = ScrollArea(self)
        self.home_interface.setWidgetResizable(True)
        content = QWidget(self.home_interface)
        content.setObjectName("homeContent")
        self.home_interface.setWidget(content)

        self.content_layout = QVBoxLayout(content)
        self.content_layout.setContentsMargins(30, 22, 30, 26)
        self.content_layout.setSpacing(14)

        header_title = TitleLabel("图片压缩与格式转换", content)
        header_caption = CaptionLabel(
            "基于 FFmpeg · 支持 JPG / PNG / WebP / GIF / AVIF / BMP / TIFF · 单文件或文件夹批量处理",
            content,
        )
        self.content_layout.addWidget(header_title)
        self.content_layout.addWidget(header_caption)

    def _MakeCard(self, title: str) -> tuple[HeaderCardWidget, QVBoxLayout]:
        card = HeaderCardWidget(self.home_interface)
        card.setTitle(title)
        body = QWidget(card)
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(10)
        card.viewLayout.addWidget(body)
        return card, body_layout

    def _BuildInputCard(self) -> None:
        card, layout = self._MakeCard("输入（单个文件或文件夹）")
        self.content_layout.addWidget(card)

        # 路径行
        path_row = QHBoxLayout()
        self.path_line = LineEdit(card)
        self.path_line.setClearButtonEnabled(True)
        self.path_line.setPlaceholderText("选择图片文件 / 文件夹，或直接拖拽到这里")
        self.path_line.setMinimumHeight(36)
        browse_file_button = PushButton("选择文件…", card)
        browse_dir_button = PushButton("选择文件夹…", card)
        path_row.addWidget(self.path_line, 1)
        path_row.addWidget(browse_file_button)
        path_row.addWidget(browse_dir_button)
        layout.addLayout(path_row)

        self.preview_label = CaptionLabel("", card)
        layout.addWidget(self.preview_label)

        # 重复处理开关：对“自动 *_output”与“指定输出目录”两种模式都生效
        self.include_output_switch = SwitchButton("重复处理：把已有的 *_output 输出目录也作为输入", card)
        self.include_output_switch.setChecked(False)
        layout.addWidget(self.include_output_switch)
        include_hint = CaptionLabel(
            "默认忽略上次生成的 *_output 目录（避免把上次输出再次转换）；"
            "勾选后自动与指定目录两种模式都会把它们当作普通文件夹处理，"
            "例如把上次输出的 WebP 再次压缩为 JPG",
            card,
        )
        layout.addWidget(include_hint)

        self.browse_file_button = browse_file_button
        self.browse_dir_button = browse_dir_button

    def _BuildOutputCard(self) -> None:
        card, layout = self._MakeCard("输出位置")
        self.content_layout.addWidget(card)

        self.auto_radio = RadioButton("自动：按规则在源位置生成 *_output 文件夹", card)
        self.custom_radio = RadioButton("指定输出目录（各批次 *_output 子目录统一放在此处）", card)
        self.output_radio_group = QButtonGroup(card)
        self.output_radio_group.addButton(self.auto_radio)
        self.output_radio_group.addButton(self.custom_radio)
        self.auto_radio.setChecked(True)

        layout.addWidget(self.auto_radio)
        layout.addWidget(self.custom_radio)

        custom_row = QHBoxLayout()
        self.output_root_line = LineEdit(card)
        self.output_root_line.setClearButtonEnabled(True)
        self.output_root_line.setPlaceholderText("选择输出目录（不存在时会自动创建，勿与源目录混淆）")
        browse_root_button = PushButton("浏览…", card)
        custom_row.addWidget(self.output_root_line, 1)
        custom_row.addWidget(browse_root_button)
        layout.addLayout(custom_row)

        self.output_mode_hint = CaptionLabel(
            "自动模式：单文件 → <文件名>_output；文件夹 → <文件夹名>_output，与源同位置",
            card,
        )
        layout.addWidget(self.output_mode_hint)

        self.browse_root_button = browse_root_button

    def _BuildParamsCard(self) -> None:
        card, layout = self._MakeCard("转换参数")
        self.content_layout.addWidget(card)

        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)

        row = 0
        # 目标格式
        grid.addWidget(self._MakeFieldLabel("目标格式", card), row, 0)
        self.format_combo = ComboBox(card)
        for text, ext in TARGET_FORMAT_OPTIONS:
            self.format_combo.addItem(text, userData=ext)
        self.format_combo.setMinimumWidth(280)
        grid.addWidget(self.format_combo, row, 1)
        row += 1

        # 质量
        self.quality_label = self._MakeFieldLabel("质量", card)
        grid.addWidget(self.quality_label, row, 0)
        quality_row = QHBoxLayout()
        self.quality_slider = Slider(Qt.Orientation.Horizontal, card)
        self.quality_slider.setRange(1, 100)
        self.quality_slider.setValue(80)
        self.quality_value_label = CaptionLabel("80", card)
        self.quality_value_label.setFixedWidth(32)
        quality_row.addWidget(self.quality_slider, 1)
        quality_row.addWidget(self.quality_value_label)
        grid.addLayout(quality_row, row, 1)
        row += 1

        # 最长边
        grid.addWidget(self._MakeFieldLabel("最长边", card), row, 0)
        dimension_row = QHBoxLayout()
        self.dimension_spin = SpinBox(card)
        self.dimension_spin.setRange(0, 100000)
        self.dimension_spin.setValue(0)
        self.dimension_spin.setSuffix(" px")
        self.dimension_spin.setFixedWidth(130)
        dimension_hint = CaptionLabel("限制输出图片最长边，0 = 不缩放", card)
        dimension_row.addWidget(self.dimension_spin)
        dimension_row.addWidget(dimension_hint)
        dimension_row.addStretch(1)
        grid.addLayout(dimension_row, row, 1)
        row += 1

        # 覆盖
        grid.addWidget(self._MakeFieldLabel("同名输出", card), row, 0)
        self.overwrite_switch = SwitchButton("覆盖已存在的输出文件", card)
        self.overwrite_switch.setChecked(True)
        grid.addWidget(self.overwrite_switch, row, 1)
        row += 1

        # ffmpeg
        grid.addWidget(self._MakeFieldLabel("ffmpeg", card), row, 0)
        ffmpeg_row = QHBoxLayout()
        self.ffmpeg_line = LineEdit(card)
        self.ffmpeg_line.setPlaceholderText("未设置时自动从 PATH 查找")
        self.ffmpeg_line.setClearButtonEnabled(True)
        browse_ffmpeg_button = PushButton("浏览…", card)
        ffmpeg_row.addWidget(self.ffmpeg_line, 1)
        ffmpeg_row.addWidget(browse_ffmpeg_button)
        grid.addLayout(ffmpeg_row, row, 1)
        row += 1

        self.quality_hint_label = CaptionLabel("", card)
        layout.addWidget(self.quality_hint_label)
        self.ffmpeg_status_label = CaptionLabel("正在检测 ffmpeg…", card)
        layout.addWidget(self.ffmpeg_status_label)

        self.browse_ffmpeg_button = browse_ffmpeg_button

    @staticmethod
    def _MakeFieldLabel(text: str, parent: QWidget) -> StrongBodyLabel:
        label = StrongBodyLabel(text, parent)
        label.setFixedWidth(88)
        return label

    def _BuildActionCard(self) -> None:
        card, layout = self._MakeCard("任务")
        self.content_layout.addWidget(card)

        button_row = QHBoxLayout()
        self.start_button = PrimaryPushButton("开始处理", card)
        self.cancel_button = PushButton("取消", card)
        self.end_button = PushButton("结束", card)
        self.end_button.setToolTip("处理完当前正在处理的文件夹后停止，不再开始新的文件夹")
        self.open_folder_button = PushButton("打开输出目录", card)
        button_row.addWidget(self.start_button)
        button_row.addWidget(self.cancel_button)
        button_row.addWidget(self.end_button)
        button_row.addWidget(self.open_folder_button)
        button_row.addStretch(1)
        layout.addLayout(button_row)

        self.progress_bar = ProgressBar(card)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        self.status_label = CaptionLabel("就绪", card)
        layout.addWidget(self.status_label)

        # 任务完成后的自动关机选项（默认关闭，避免误触）
        shutdown_row = QHBoxLayout()
        self.auto_shutdown_switch = SwitchButton("任务完成后自动关机", card)
        self.auto_shutdown_switch.setChecked(False)
        shutdown_hint = CaptionLabel("仅 Windows · 全部结束后 60 秒倒计时关机，可在系统提示中取消（shutdown /a）", card)
        shutdown_row.addWidget(self.auto_shutdown_switch)
        shutdown_row.addSpacing(8)
        shutdown_row.addWidget(shutdown_hint, 1)
        shutdown_row.addStretch(0)
        layout.addLayout(shutdown_row)

    def _BuildLogCard(self) -> None:
        card, layout = self._MakeCard("处理日志")
        self.content_layout.addWidget(card)

        top_row = QHBoxLayout()
        top_hint = CaptionLabel("转换过程与 ffmpeg 输出", card)
        clear_button = PushButton("清空", card)
        clear_button.setFixedWidth(72)
        top_row.addWidget(top_hint)
        top_row.addStretch(1)
        top_row.addWidget(clear_button)
        layout.addLayout(top_row)

        self.log_browser = TextBrowser(card)
        self.log_browser.setMinimumHeight(150)
        self.log_browser.setPlaceholderText("暂无日志")
        layout.addWidget(self.log_browser, 1)

        self.clear_log_button = clear_button

    def _ConnectSignals(self) -> None:
        self.browse_file_button.clicked.connect(self._OnBrowseFile)
        self.browse_dir_button.clicked.connect(self._OnBrowseDir)
        self.browse_ffmpeg_button.clicked.connect(self._OnBrowseFfmpeg)
        self.browse_root_button.clicked.connect(self._OnBrowseOutputRoot)
        self.path_line.editingFinished.connect(self._OnPathEdited)
        self.ffmpeg_line.editingFinished.connect(self._ProbeCurrentFfmpeg)
        self.output_root_line.editingFinished.connect(self._RefreshPreview)
        self.auto_radio.toggled.connect(lambda _checked: self._OnOutputModeChanged())
        self.custom_radio.toggled.connect(lambda _checked: self._OnOutputModeChanged())
        self.format_combo.currentIndexChanged.connect(lambda _i: self._UpdateQualityUi())
        self.quality_slider.valueChanged.connect(self._OnQualityChanged)
        self.start_button.clicked.connect(self._OnStartClicked)
        self.cancel_button.clicked.connect(self._OnCancelClicked)
        self.end_button.clicked.connect(self._OnEndClicked)
        self.open_folder_button.clicked.connect(self._OnOpenFolder)
        self.clear_log_button.clicked.connect(self.log_browser.clear)
        self.include_output_switch.checkedChanged.connect(lambda _checked: self._RefreshPreview())

    # ---- 拖放 ---------------------------------------------------------
    def dragEnterEvent(self, event):  # noqa: N802 —— Qt 事件
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):  # noqa: N802 —— Qt 事件
        urls = event.mimeData().urls()
        if not urls:
            return
        local_path = urls[0].toLocalFile()
        if local_path:
            self._SetInputPath(local_path)
        event.acceptProposedAction()

    # ---- 输入路径 -----------------------------------------------------
    def _OnBrowseFile(self) -> None:
        file_path, _filter = QFileDialog.getOpenFileName(
            self,
            "选择图片文件",
            self.path_line.text().strip() or "",
            "图片文件 (*.jpg *.jpeg *.png *.webp *.gif *.avif *.bmp *.tif *.tiff);;所有文件 (*.*)",
        )
        if file_path:
            self._SetInputPath(file_path)

    def _OnBrowseDir(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "选择文件夹", self.path_line.text().strip() or ""
        )
        if directory:
            self._SetInputPath(directory)

    def _SetInputPath(self, path_text: str) -> None:
        self.path_line.setText(path_text)
        self._RefreshPreview()
        self._UpdateStartState()

    def _OnPathEdited(self) -> None:
        self._RefreshPreview()
        self._UpdateStartState()

    def _RefreshPreview(self) -> None:
        path_text = self.path_line.text().strip()
        if not path_text:
            self.preview_label.setText("")
            self._preview_batches = None
            return
        try:
            batches = BuildBatches(path_text, include_output_dirs=self.include_output_switch.isChecked())
            if self.custom_radio.isChecked():
                output_root = self.output_root_line.text().strip()
                if not output_root:
                    self._preview_batches = None
                    self.preview_label.setText("⚠ 已选择“指定输出目录”，请先填写输出目录路径")
                    self._UpdateStartState()
                    return
                batches = RelocateBatchOutputs(batches, output_root)
            self._preview_batches = batches
            self.preview_label.setText(SummarizeBatches(batches))
        except InputError as exc:
            self._preview_batches = None
            self.preview_label.setText(f"⚠ {exc}")
        self._UpdateStartState()

    # ---- 输出位置 -----------------------------------------------------
    def _OnBrowseOutputRoot(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "选择输出目录", self.output_root_line.text().strip() or ""
        )
        if directory:
            self.output_root_line.setText(directory)
            self._RefreshPreview()

    def _OnOutputModeChanged(self) -> None:
        is_custom = self.custom_radio.isChecked()
        self.output_root_line.setEnabled(is_custom)
        self.browse_root_button.setEnabled(is_custom)
        if is_custom:
            self.output_mode_hint.setText(
                "指定模式：每个批次仍输出到自己的 <名称>_output 子目录，统一放到指定目录下；"
                "文件命名规则不变，同名输出子目录自动加序号"
            )
        else:
            self.output_mode_hint.setText(
                "自动模式：单文件 → <文件名>_output；文件夹 → <文件夹名>_output，与源同位置"
            )
        self._RefreshPreview()

    # ---- ffmpeg 探测 --------------------------------------------------
    def _OnBrowseFfmpeg(self) -> None:
        file_path, _filter = QFileDialog.getOpenFileName(
            self, "选择 ffmpeg.exe", self.ffmpeg_line.text().strip() or "", "ffmpeg (ffmpeg.exe);;所有文件 (*)"
        )
        if file_path:
            self.ffmpeg_line.setText(file_path)
            self._ProbeCurrentFfmpeg()

    def _ProbeCurrentFfmpeg(self) -> None:
        explicit = self.ffmpeg_line.text().strip() or None
        try:
            ffmpeg_path = LocateFfmpeg(explicit)
        except ConverterError as exc:
            self._active_ffmpeg_path = None
            self.ffmpeg_status_label.setText(f"✗ {exc}")
            self._caps = None
            self._UpdateStartState()
            return
        self._active_ffmpeg_path = ffmpeg_path

        if self._probe_thread is not None and self._probe_thread.isRunning():
            self._pending_probe_path = ffmpeg_path
            return
        self._probe_thread = _ProbeThread(ffmpeg_path, self)
        self._probe_thread.probeFinished.connect(self._OnProbeFinished)
        self._probe_thread.probeFailed.connect(self._OnProbeFailed)
        self._probe_thread.finished.connect(self._OnProbeThreadFinished)
        self.ffmpeg_status_label.setText("正在检测 ffmpeg…")
        self._probe_thread.start()

    def _OnProbeThreadFinished(self) -> None:
        pending = self._pending_probe_path
        self._pending_probe_path = None
        if pending:
            self._ProbeCurrentFfmpeg()

    def _OnProbeFinished(self, caps: FfmpegCapabilities) -> None:
        self._caps = caps
        enabled = "，AVIF 可用" if caps.avif_ok else "（不支持 AVIF）"
        self.ffmpeg_status_label.setText(f"✓ {caps.version}{enabled}")
        self._UpdateFormatCombo()
        self._settings.setValue("ffmpeg/path", self.ffmpeg_line.text().strip())
        self._UpdateStartState()

    def _OnProbeFailed(self, message: str) -> None:
        self._caps = None
        self.ffmpeg_status_label.setText(f"✗ {message}")
        self._UpdateStartState()
        self._ShowInfoBar("ffmpeg 检测失败", message, error=True)

    def _UpdateFormatCombo(self) -> None:
        if self._caps is None:
            return
        self._DisableUnavailableFormat(".avif", self._caps.avif_ok, "AVIF")
        self._DisableUnavailableFormat(".gif", self._caps.gif_ok, "GIF")

    def _DisableUnavailableFormat(self, extension: str, available: bool, name: str) -> None:
        """目标格式在当前 ffmpeg 不可用时置灰；若正被选中则回退到“保持原格式”。"""
        index = self.format_combo.findData(extension)
        if index < 0:
            return
        self.format_combo.setItemEnabled(index, available)
        if not available and self.format_combo.currentIndex() == index:
            self.format_combo.setCurrentIndex(0)
            self._ShowInfoBar(
                f"{name} 不可用",
                f"当前 ffmpeg 不支持 {name} 编码，已自动切回“保持原格式”",
                warning=True,
            )

    # ---- 质量联动 -----------------------------------------------------
    def _UpdateQualityUi(self) -> None:
        target = self.format_combo.currentData()
        applicable = not IsLosslessExtension(target)
        self.quality_label.setEnabled(applicable)
        self.quality_slider.setEnabled(applicable)
        self.quality_value_label.setEnabled(applicable)

        if not applicable:
            self.quality_hint_label.setText("PNG / BMP / TIFF 为无损编码，质量设置不适用")
            return
        if target is None:
            self.quality_hint_label.setText("质量 1~100，越大画质越好、文件越大（仅对 JPG/WebP/GIF/AVIF 源生效）")
        elif target == ".webp":
            self.quality_hint_label.setText(
                "质量 1~100，越大画质越好、文件越大；WebP 单边上限 16383px，超大图会自动等比缩小"
            )
        elif target == ".gif":
            self.quality_hint_label.setText(
                "GIF 画质由调色板决定：质量 1~100 映射为 32~256 色，颜色越多画质越好、文件越大；动画会整段保留"
            )
        else:
            self.quality_hint_label.setText("质量 1~100，越大画质越好、文件越大")

    def _OnQualityChanged(self, value: int) -> None:
        self.quality_value_label.setText(str(value))

    # ---- 任务执行 -----------------------------------------------------
    def _OnStartClicked(self) -> None:
        path_text = self.path_line.text().strip()
        if not path_text:
            self._ShowInfoBar("无法开始", "请先选择输入文件或文件夹", error=True)
            return
        if self._caps is None:
            self._ShowInfoBar("无法开始", "ffmpeg 尚未就绪，请检查 ffmpeg 路径", error=True)
            return

        try:
            batches = BuildBatches(path_text, include_output_dirs=self.include_output_switch.isChecked())
            if self.custom_radio.isChecked():
                output_root = self.output_root_line.text().strip()
                if not output_root:
                    self._ShowInfoBar("无法开始", "请先指定输出目录", error=True)
                    return
                batches = RelocateBatchOutputs(batches, output_root)
        except InputError as exc:
            self._ShowInfoBar("无法开始", str(exc), error=True)
            return

        target = self.format_combo.currentData()
        quality = self.quality_slider.value() if self.quality_slider.isEnabled() else None
        options = ConvertOptions(
            ffmpeg_path=self._active_ffmpeg_path or "",
            target_extension=target,
            quality=quality,
            max_dimension=self.dimension_spin.value(),
            overwrite=self.overwrite_switch.isChecked(),
        )

        self.log_browser.clear()
        self._last_output_dirs = []
        self.open_folder_button.setEnabled(False)

        total = sum(len(batch.files) for batch in batches)
        self._planned_total = total
        self._worker_summaries = {}
        self._thread_progress = {}
        self.progress_bar.setRange(0, total)
        self.progress_bar.setValue(0)

        # 只保留一个后台线程：避免多个 FFmpeg 进程争抢 CPU 和磁盘 I/O。
        worker = ConvertWorker(1, batches, options, self._caps, self)
        worker.logMessage.connect(self._OnWorkerLog)
        worker.progressChanged.connect(self._OnWorkerProgress)
        worker.taskFinished.connect(self._OnWorkerTaskFinished)
        worker.finished.connect(lambda w=worker: self._OnWorkerThreadFinished(w))
        self._workers = [worker]

        self._AppendLog(
            LOG_INFO,
            f"任务开始：{len(batches)} 个批次、{total} 个文件，"
            "由后台线程顺序处理",
        )
        for worker in self._workers:
            worker.start()
        self._UpdateStartState()

    def _OnWorkerLog(self, _thread_id: int, level: int, text: str) -> None:
        self._AppendLog(level, text)

    def _OnWorkerProgress(
        self, thread_id: int, done: int, _total: int, _current: str
    ) -> None:
        self._thread_progress[thread_id] = (done, _total)
        sum_done = sum(value[0] for value in self._thread_progress.values())
        self.progress_bar.setValue(min(sum_done, self._planned_total))
        self.status_label.setText(f"进度 {sum_done}/{self._planned_total}")

    def _OnWorkerTaskFinished(self, thread_id: int, summary: dict) -> None:
        """收集各线程的汇总；全部线程结束后统一收尾。"""
        self._worker_summaries[thread_id] = summary
        if self._workers and len(self._worker_summaries) >= len(self._workers):
            self._OnAllWorkersFinished(self._MergeSummaries())

    def _MergeSummaries(self) -> dict:
        merged = {
            "total": 0,
            "ok": 0,
            "failed": 0,
            "skipped": 0,
            "cancelled": False,
            "early_stopped": False,
            "output_dirs": [],
        }
        seen_dirs: set[str] = set()
        for summary in self._worker_summaries.values():
            merged["total"] += summary["total"]
            merged["ok"] += summary["ok"]
            merged["failed"] += summary["failed"]
            merged["skipped"] += summary["skipped"]
            merged["cancelled"] = merged["cancelled"] or summary["cancelled"]
            merged["early_stopped"] = (
                merged["early_stopped"] or summary["early_stopped"]
            )
            for directory in summary["output_dirs"]:
                if directory not in seen_dirs:
                    seen_dirs.add(directory)
                    merged["output_dirs"].append(directory)
        return merged

    def _OnAllWorkersFinished(self, summary: dict) -> None:
        if summary["cancelled"]:
            self._ShowInfoBar("任务已取消", "本次处理被手动中止", error=False, warning=True)
        elif summary["early_stopped"]:
            self._ShowInfoBar(
                "已按“结束”停止",
                f"完成当前文件夹后停止：成功 {summary['ok']}，失败 {summary['failed']}，"
                f"跳过 {summary['skipped']}；剩余文件夹未处理",
                warning=True,
            )
        elif summary["failed"]:
            self._ShowInfoBar(
                "处理完成（有失败项）",
                f"成功 {summary['ok']}，失败 {summary['failed']}，跳过 {summary['skipped']}",
                warning=True,
            )
        else:
            self._ShowInfoBar(
                "处理完成",
                f"成功 {summary['ok']} 个文件，跳过 {summary['skipped']}",
            )
        self._last_output_dirs = summary["output_dirs"]

        # 任务真正全部结束后（取消 / 提前结束都不算完成）才考虑自动关机
        if (
            not summary["cancelled"]
            and not summary["early_stopped"]
            and self.auto_shutdown_switch.isChecked()
        ):
            self._ScheduleShutdown()

    def _ScheduleShutdown(self) -> None:
        """任务全部结束后执行系统关机：60 秒倒计时，可用 shutdown /a 取消。"""
        if sys.platform != "win32":
            self._AppendLog(LOG_WARN, "自动关机仅支持 Windows 系统，已跳过")
            return
        self._AppendLog(
            LOG_WARN,
            "所有任务已完成，系统将在 60 秒后关机；如需取消请尽快执行 shutdown /a",
        )
        try:
            # 不携带 /f：给系统与其它程序优雅退出的机会
            subprocess.run(["shutdown", "/s", "/t", "60"], check=False, timeout=10)
        except OSError as exc:
            self._AppendLog(LOG_ERROR, f"自动关机命令执行失败：{exc}")

    def _OnWorkerThreadFinished(self, worker: ConvertWorker) -> None:
        """单个线程结束后从活动列表移除；全部线程结束才恢复界面。"""
        if worker in self._workers:
            self._workers.remove(worker)
        if not self._workers:
            if self._last_output_dirs:
                self.open_folder_button.setEnabled(True)
            self._worker_summaries = {}
            self._thread_progress = {}
            self._UpdateStartState()

    def _OnCancelClicked(self) -> None:
        if self._workers:
            self.status_label.setText("正在取消…")
            self.cancel_button.setEnabled(False)
            self.end_button.setEnabled(False)
            for worker in self._workers:
                worker.RequestCancel()

    def _OnEndClicked(self) -> None:
        """结束：处理完当前正在处理的文件夹后停止，不再开始新文件夹。"""
        if self._workers:
            self.status_label.setText("正在处理当前文件夹，之后将停止…")
            self.end_button.setEnabled(False)
            for worker in self._workers:
                worker.RequestFinishAfterCurrentBatch()

    def _OnOpenFolder(self) -> None:
        opened = 0
        for directory in self._last_output_dirs:
            if opened >= 5:
                self._AppendLog(LOG_WARN, "输出目录较多，已打开前 5 个")
                break
            if Path(directory).is_dir():
                os.startfile(directory)  # noqa: S606 —— 打开资源管理器是用户显式动作
                opened += 1

    # ---- 状态辅助 -----------------------------------------------------
    def _UpdateStartState(self) -> None:
        has_path = bool(self.path_line.text().strip())
        running = bool(self._workers)
        self.start_button.setEnabled(self._caps is not None and has_path and not running)
        self.cancel_button.setEnabled(running)
        self.end_button.setEnabled(running)
        self.browse_file_button.setEnabled(not running)
        self.browse_dir_button.setEnabled(not running)

    def _RestoreSettings(self) -> None:
        saved_path = self._settings.value("ffmpeg/path", "", type=str)
        if saved_path:
            self.ffmpeg_line.setText(saved_path)

    def _AppendLog(self, level: int, text: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        body = f"[{timestamp}] {_LEVEL_MARKS.get(level, '')}{html.escape(text)}"
        if level == LOG_INFO:
            line = f"<p style='margin:0'>{body}</p>"
        else:
            light, dark = _LOG_COLORS[level]
            color = dark if isDarkTheme() else light
            line = f"<p style='margin:0;color:{color}'>{body}</p>"
        cursor = self.log_browser.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertHtml(line)
        self.log_browser.setTextCursor(cursor)
        self.log_browser.ensureCursorVisible()

    def _ShowInfoBar(
        self,
        title: str,
        content: str,
        error: bool = False,
        warning: bool = False,
    ) -> None:
        info_bar = InfoBar.new
        if error:
            info_bar = InfoBar.error
        elif warning:
            info_bar = InfoBar.warning
        else:
            info_bar = InfoBar.success
        info_bar(title, content, parent=self)


class MainWindow(_MainUiMixin, MSFluentWindow):
    """微软商店风格主窗口（Win11 Mica）。"""

    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self._SetupUi()


class FallbackMainWindow(_MainUiMixin, FluentWindow):
    """常规 Fluent 主窗口（构造 MSFluentWindow 失败时使用）。"""

    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self._SetupUi()
