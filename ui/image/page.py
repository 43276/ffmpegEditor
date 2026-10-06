"""图片处理页面：输入、转换任务、日志及图片页偏好。"""
from __future__ import annotations

import json
from subprocess import TimeoutExpired
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
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
    InfoBar,
    LineEdit,
    PushButton,
    RadioButton,
    Slider,
    SpinBox,
    StrongBodyLabel,
)

from app.ffmpeg_environment import FfmpegCapabilities
from app.image.commands import supports_image_format
from app.image.planner import (
    build_batches_for_inputs as BuildBatchesForInputs, InputError,
)
from app.image.formats import is_lossless_extension as IsLosslessExtension
from ui.media_presentation import summarize_image_batches as SummarizeBatches, TARGET_FORMAT_OPTIONS
from app.image.models import ConvertOptions
from ui.widgets.switch_button import MakeSwitchButton
from ui.widgets.smooth_scroll import SmoothScrollArea as ScrollArea
from app.task_models import LOG_ERROR, LOG_INFO, LOG_WARN, TaskProgress, TaskResult, TaskStatistics
from ui.tasks.controller import TaskController, TaskState
from ui.tasks.preview import PreviewController, PreviewState
from ui.widgets.log_panel import LogPanel
from ui.widgets.task_panel import TaskPanel
from ui.widgets.page_layout import make_card, build_page_content
from ui.services.settings_service import SettingsService
from ui.services.desktop_actions import DesktopActions
from ui.tasks.jobs import image_inputs_job


class ImagePage(QWidget):
    """独立的图片处理页面；共享 FFmpeg 状态由主窗口提供。"""

    # ---- 初始化 -------------------------------------------------------
    def __init__(self, parent=None, *, settings=None, desktop_actions=None):
        super().__init__(parent)
        self.setObjectName("imagePage")
        self.setAcceptDrops(True)

        self._settings = settings if settings is not None else SettingsService()
        self._caps: FfmpegCapabilities | None = None
        self._active_ffmpeg_path: str | None = None
        self._task_controller = TaskController(self)
        self._preview_controller = PreviewController(self)
        self._desktop_actions = desktop_actions if desktop_actions is not None else DesktopActions()
        self._input_paths: list[Path] = []
        self._preview_batches = None
        self._shutdown_requested = False
        self._last_output_dirs: list[str] = []
        self._error_logs: list[str] = []
        self._skipped_logs: list[str] = []
        self._closing = False
        self._close_requested = False

        page_layout = QVBoxLayout(self)
        page_layout.setContentsMargins(0, 0, 0, 0)
        self._BuildScrollContent()
        page_layout.addWidget(self.home_interface)
        self._BuildInputCard()
        self._BuildOutputCard()
        self._BuildParamsCard()
        self._BuildActionCard()
        self._BuildLogCard()
        self._ConnectSignals()
        self._task_controller.logMessage.connect(lambda event: self._AppendLog(event.level, event.message))
        self._task_controller.progressChanged.connect(self._OnTaskProgress)
        self._task_controller.statisticsChanged.connect(self._OnTaskStatistics)
        self._task_controller.completed.connect(self._OnTaskFinished)
        self._task_controller.stateChanged.connect(self._UpdateStartState)
        self._task_controller.idle.connect(self._OnTaskIdle)
        self._preview_controller.resultReady.connect(self._OnPreviewReady)
        self._preview_controller.errorOccurred.connect(self._OnPreviewError)
        self._preview_controller.stateChanged.connect(self._OnPreviewState)
        self._preview_controller.idle.connect(self._OnTaskIdle)

        self.home_interface.setObjectName("homeInterface")

        # 初始状态
        self._RestoreSettings()
        self._OnOutputModeChanged()
        self._UpdateQualityUi()
        self._UpdateStartState()

    def _BuildScrollContent(self):
        self.home_interface = ScrollArea(self)
        _, self.content_layout = build_page_content(
            self.home_interface, "图片压缩与格式转换",
            "基于 FFmpeg · 支持 JPG / PNG / WebP / GIF / AVIF / BMP / TIFF · 单文件或文件夹批量处理",
            object_name="homeContent",
        )

    def _MakeCard(self, title: str):
        card, layout = make_card(self.home_interface, title)
        return card, layout

    def _BuildInputCard(self) -> None:
        card, layout = self._MakeCard("输入")
        self.content_layout.addWidget(card)

        # 路径行
        path_row = QHBoxLayout()
        self.path_line = LineEdit(card)
        self.path_line.setClearButtonEnabled(True)
        self.path_line.setPlaceholderText("选择图片文件 / 文件夹")
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
        self.include_output_switch = MakeSwitchButton("重复处理：把已有的 *_output 输出目录也作为输入", card)
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
        self.overwrite_switch = MakeSwitchButton("覆盖已存在的输出文件", card)
        self.overwrite_switch.setChecked(True)
        grid.addWidget(self.overwrite_switch, row, 1)
        row += 1

        self.quality_hint_label = CaptionLabel("", card)
        layout.addWidget(self.quality_hint_label)
        self.ffmpeg_status_label = CaptionLabel("正在检测 ffmpeg…", card)
        layout.addWidget(self.ffmpeg_status_label)

    @staticmethod
    def _MakeFieldLabel(text: str, parent: QWidget) -> StrongBodyLabel:
        label = StrongBodyLabel(text, parent)
        label.setFixedWidth(88)
        return label

    def _BuildActionCard(self):
        self.task_panel = TaskPanel(self.home_interface, finish_tooltip="处理完当前正在处理的文件夹后停止，不再开始新的文件夹")
        self.content_layout.addWidget(self.task_panel)
        for name in ("start_button", "cancel_button", "end_button", "open_folder_button", "export_error_button", "export_skipped_button", "progress_bar", "status_label"):
            setattr(self, name, getattr(self.task_panel, name))
        shutdown_row = QHBoxLayout()
        self.auto_shutdown_switch = MakeSwitchButton("任务完成后自动关机", self.task_panel)
        self.auto_shutdown_switch.setChecked(False)
        shutdown_row.addWidget(self.auto_shutdown_switch)
        shutdown_row.addSpacing(8)
        shutdown_row.addWidget(CaptionLabel("仅 Windows · 全部结束后 60 秒倒计时关机，可在系统提示中取消（shutdown /a）", self.task_panel), 1)
        self.task_panel.body_layout.addLayout(shutdown_row)

    def _BuildLogCard(self):
        self.log_panel = LogPanel(self.home_interface)
        self.content_layout.addWidget(self.log_panel)
        self.log_browser = self.log_panel.browser
        self.log_statistics_label = self.log_panel.statistics_label
        self.clear_log_button = self.log_panel.clear_button

    def _ConnectSignals(self) -> None:
        self.browse_file_button.clicked.connect(self._OnBrowseFile)
        self.browse_dir_button.clicked.connect(self._OnBrowseDir)
        self.browse_root_button.clicked.connect(self._OnBrowseOutputRoot)
        self.path_line.editingFinished.connect(self._OnPathEdited)
        self.path_line.textEdited.connect(self._OnPathTextEdited)
        self.output_root_line.editingFinished.connect(self._RefreshPreview)
        self.output_root_line.textEdited.connect(lambda _text: self._RefreshPreview())
        self.auto_radio.toggled.connect(lambda _checked: self._OnOutputModeChanged())
        self.custom_radio.toggled.connect(lambda _checked: self._OnOutputModeChanged())
        self.format_combo.currentIndexChanged.connect(lambda _i: self._UpdateQualityUi())
        self.quality_slider.valueChanged.connect(self._OnQualityChanged)
        self.start_button.clicked.connect(self._OnStartClicked)
        self.cancel_button.clicked.connect(self._OnCancelClicked)
        self.end_button.clicked.connect(self._OnEndClicked)
        self.open_folder_button.clicked.connect(self._OnOpenFolder)
        self.export_error_button.clicked.connect(self._ExportErrorLog)
        self.export_skipped_button.clicked.connect(self._ExportSkippedLog)
        self.include_output_switch.checkedChanged.connect(lambda _checked: self._RefreshPreview())

    # ---- 拖放 ---------------------------------------------------------
    def dragEnterEvent(self, event):  # noqa: N802 —— Qt 事件
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):  # noqa: N802 —— Qt 事件
        urls = event.mimeData().urls()
        if not urls:
            return
        paths = [Path(url.toLocalFile()) for url in urls if url.isLocalFile()]
        if paths:
            self._SetInputPaths(paths)
        event.acceptProposedAction()

    # ---- 输入路径 -----------------------------------------------------
    def _OnBrowseFile(self) -> None:
        file_paths, _filter = QFileDialog.getOpenFileNames(
            self,
            "选择图片文件",
            self.path_line.text().strip() or "",
            "图片文件 (*.jpg *.jpeg *.png *.webp *.gif *.avif *.bmp *.tif *.tiff);;所有文件 (*.*)",
        )
        if file_paths:
            self._SetInputPaths([Path(path) for path in file_paths])

    def _OnBrowseDir(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "选择图片文件夹", self.path_line.text().strip() or ""
        )
        if directory:
            self._SetInputPaths([Path(directory)])

    def _SetInputPath(self, path_text: str) -> None:
        self._SetInputPaths([Path(path_text)])

    def _SetInputPaths(self, paths: list[Path]):
        self._input_paths = list(paths)
        self.path_line.setReadOnly(len(paths) > 1)
        self.path_line.setClearButtonEnabled(len(paths) <= 1)
        self.path_line.setText(str(paths[0]) if len(paths) == 1 else f"已选择 {len(paths)} 个输入" if paths else "")
        self._RefreshPreview()

    def _OnPathEdited(self) -> None:
        if self.path_line.isReadOnly():
            return
        text = self.path_line.text().strip()
        if len(self._input_paths) != 1 or not self._input_paths or str(self._input_paths[0]) != text:
            self._input_paths = [Path(text)] if text else []
        self._RefreshPreview()
        self._UpdateStartState()

    def _OnPathTextEdited(self, text: str):
        if not self.path_line.isReadOnly():
            self._input_paths = [Path(text.strip())] if text.strip() else []
            self._RefreshPreview()

    def _CurrentInputPaths(self) -> list[Path]:
        if self._input_paths:
            return list(self._input_paths)
        text = self.path_line.text().strip()
        return [Path(text)] if text else []


    def _RefreshPreview(self):
        if self._closing:
            return
        paths = tuple(self._CurrentInputPaths())
        self._preview_batches = None
        if not paths:
            self._preview_controller.invalidate()
            self.preview_label.setText("")
            self._UpdateStartState()
            return
        output_root = self.output_root_line.text().strip() if self.custom_radio.isChecked() else None
        custom = self.custom_radio.isChecked()
        include_output = self.include_output_switch.isChecked()
        def plan(cancel_check):
            if custom and not output_root:
                raise InputError("已选择“指定输出目录”，请先填写输出目录路径")
            return BuildBatchesForInputs(paths, include_output_dirs=include_output, output_root=output_root, cancel_check=cancel_check)
        self.preview_label.setText("正在准备预览…")
        self._preview_controller.request(plan)
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

    # ---- 共享 FFmpeg 状态 ---------------------------------------------
    def SetFfmpegPath(self, path: str | None, capabilities: FfmpegCapabilities | None, message: str) -> None:
        self._active_ffmpeg_path = path
        self._caps = capabilities
        self.ffmpeg_status_label.setText(message)
        self._UpdateFormatCombo()
        self._UpdateStartState()

    def _UpdateFormatCombo(self) -> None:
        if self._caps is None:
            return
        self._DisableUnavailableFormat(".avif", supports_image_format(self._caps, ".avif"), "AVIF")
        self._DisableUnavailableFormat(".gif", supports_image_format(self._caps, ".gif"), "GIF")

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
    def _OnStartClicked(self):
        if self._closing or self._task_controller.active:
            return
        if self._preview_controller.state != PreviewState.VALID or not self._preview_batches:
            self._ShowInfoBar("无法开始", "输入预览尚未准备完成或输入无效", error=True)
            return
        if self._caps is None:
            self._ShowInfoBar("无法开始", "ffmpeg 尚未就绪，请检查 ffmpeg 路径", error=True)
            return
        paths = tuple(self._CurrentInputPaths())
        output_root = self.output_root_line.text().strip() if self.custom_radio.isChecked() else None
        options = ConvertOptions(
            ffmpeg_path=self._active_ffmpeg_path or "", target_extension=self.format_combo.currentData(),
            quality=self.quality_slider.value() if self.quality_slider.isEnabled() else None,
            max_dimension=self.dimension_spin.value(), overwrite=self.overwrite_switch.isChecked(),
        )
        self._shutdown_requested = self.auto_shutdown_switch.isChecked()
        self.log_panel.clear()
        self._error_logs = []
        self._skipped_logs = []
        self._last_output_dirs = []
        total = sum(len(batch.files) for batch in self._preview_batches)
        self._SetStatistics(total, 0, 0, 0)
        self.progress_bar.setRange(0, max(1, total))
        self.progress_bar.setValue(0)
        self._AppendLog(LOG_INFO, "任务开始：后台重新校验输入并顺序处理")
        self.status_label.setText("正在校验输入…")
        self._task_controller.start(image_inputs_job(paths, options, self._caps, include_output_dirs=self.include_output_switch.isChecked(), output_root=output_root), total=total, supports_finish=True)
        self._UpdateStartState()

    def _OnTaskProgress(self, progress: TaskProgress):
        if self._task_controller.state == TaskState.RUNNING:
            self.task_panel.set_progress(progress)
        else:
            self.progress_bar.setRange(0, max(1, progress.total))
            self.progress_bar.setValue(progress.done)

    def _OnTaskStatistics(self, stats: TaskStatistics) -> None:
        self._SetStatistics(stats.total, stats.ok, stats.failed, stats.skipped)

    def _OnTaskFinished(self, _kind: str, summary: TaskResult) -> None:
        self._last_result = summary
        self.task_panel.set_result(summary)
        if summary.cancelled:
            self._ShowInfoBar("任务已取消", "本次处理被手动中止", error=False, warning=True)
        elif summary.early_stopped:
            self._ShowInfoBar(
                "已按“结束”停止",
                f"完成当前文件夹后停止：成功 {summary.ok}，失败 {summary.failed}，"
                f"跳过 {summary.skipped}；剩余文件夹未处理",
                warning=True,
            )
        elif summary.failed:
            self._ShowInfoBar(
                "处理完成（有失败项）",
                f"成功 {summary.ok}，失败 {summary.failed}，跳过 {summary.skipped}",
                warning=True,
            )
        else:
            self._ShowInfoBar(
                "处理完成",
                f"成功 {summary.ok} 个文件，跳过 {summary.skipped}",
            )
        self._last_output_dirs = summary.output_dirs
        self._error_logs = summary.error_logs
        self._skipped_logs = summary.skipped_logs
        self._SetStatistics(
            total=summary.total,
            ok=summary.ok,
            failed=summary.failed,
            skipped=summary.skipped,
        )
        self._UpdateLogExportButtons()

        # 任务真正全部结束后（取消 / 提前结束都不算完成）才考虑自动关机
        if (
            not self._closing
            and not summary.cancelled
            and not summary.early_stopped
            and self._shutdown_requested
        ):
            self._ScheduleShutdown()
        self._UpdateStartState()

    def _ScheduleShutdown(self):
        try:
            if self._desktop_actions.schedule_shutdown(self._last_result, self._shutdown_requested):
                self._AppendLog(LOG_WARN, "所有任务已完成，系统将在 60 秒后关机；如需取消请尽快执行 shutdown /a")
        except (OSError, TimeoutError, TimeoutExpired) as exc:
            self._AppendLog(LOG_ERROR, f"自动关机命令执行失败：{exc}")

    def _OnTaskIdle(self):
        if self._close_requested and not self._task_controller.active and not self._preview_controller.active:
            QTimer.singleShot(0, self.close)

    def _OnCancelClicked(self) -> None:
        if self._task_controller.request_cancel():
            self.status_label.setText("正在取消…")
            self._UpdateStartState()

    def _OnEndClicked(self) -> None:
        """结束：处理完当前正在处理的文件夹后停止，不再开始新文件夹。"""
        if self._task_controller.request_finish_after_current_batch():
            self.status_label.setText("正在处理当前文件夹，之后将停止…")
            self._UpdateStartState()

    def _OnOpenFolder(self):
        for index, directory in enumerate(self._last_output_dirs):
            if index >= 5:
                self._AppendLog(LOG_WARN, "输出目录较多，已打开前 5 个")
                break
            try:
                self._desktop_actions.open_output_directory(directory)
            except OSError as exc:
                self._AppendLog(LOG_ERROR, f"打开输出目录失败：{exc}")

    # ---- 状态辅助 -----------------------------------------------------
    def _SetStatistics(self, total: int, ok: int, failed: int, skipped: int):
        stats = TaskStatistics(total, ok, failed, skipped, ok + failed + skipped)
        self.log_panel.set_statistics(stats)

    def _UpdateLogExportButtons(self):
        self._UpdateStartState()

    def _ExportLog(self, title: str, default_name: str, entries: list[str]):
        if not self._task_controller.active and not self._closing:
            self.log_panel.export_entries(self, title, default_name, entries, self._ShowInfoBar)

    def _ExportErrorLog(self) -> None:
        self._ExportLog("导出错误日志", "error_log.txt", self._error_logs)

    def _ExportSkippedLog(self) -> None:
        self._ExportLog("导出跳过日志", "skipped_log.txt", self._skipped_logs)

    def _UpdateStartState(self):
        running = self._task_controller.active or self._closing
        ready = self._caps is not None and self._preview_controller.state == PreviewState.VALID and bool(self._preview_batches)
        self.task_panel.update_state(self._task_controller, ready, self._closing, bool(self._last_output_dirs), bool(self._error_logs), bool(self._skipped_logs))
        self.browse_file_button.setEnabled(not running)
        self.browse_dir_button.setEnabled(not running)
        self.path_line.setEnabled(not running)
        self.include_output_switch.setEnabled(not running)
        self.auto_radio.setEnabled(not running)
        self.custom_radio.setEnabled(not running)
        self.output_root_line.setEnabled(not running and self.custom_radio.isChecked())
        self.browse_root_button.setEnabled(not running and self.custom_radio.isChecked())

    def _RestoreSettings(self) -> None:
        saved_inputs = self._settings.value("input/paths", "", type=str)
        if saved_inputs:
            try:
                self._SetInputPaths([Path(path) for path in json.loads(saved_inputs)])
            except (TypeError, ValueError, json.JSONDecodeError):
                self._input_paths = []

        saved_output_root = self._settings.value("output/root", "", type=str)
        self.output_root_line.setText(saved_output_root)
        self.custom_radio.setChecked(
            self._settings.value("output/custom", False, type=bool)
        )
        self.include_output_switch.setChecked(
            self._settings.value("input/include_output", False, type=bool)
        )
        self.overwrite_switch.setChecked(
            self._settings.value("output/overwrite", True, type=bool)
        )
        self.auto_shutdown_switch.setChecked(
            self._settings.value("task/auto_shutdown", False, type=bool)
        )
        self.quality_slider.setValue(
            self._settings.value("convert/quality", 80, type=int)
        )
        self.dimension_spin.setValue(
            self._settings.value("convert/max_dimension", 0, type=int)
        )
        format_index = self._settings.value("convert/format_index", 0, type=int)
        if 0 <= format_index < self.format_combo.count():
            self.format_combo.setCurrentIndex(format_index)

    def _SaveSettings(self) -> None:
        self._settings.setValue(
            "input/paths", json.dumps([str(path) for path in self._input_paths], ensure_ascii=False)
        )
        self._settings.setValue("output/root", self.output_root_line.text().strip())
        self._settings.setValue("output/custom", self.custom_radio.isChecked())
        self._settings.setValue("input/include_output", self.include_output_switch.isChecked())
        self._settings.setValue("output/overwrite", self.overwrite_switch.isChecked())
        self._settings.setValue("task/auto_shutdown", self.auto_shutdown_switch.isChecked())
        self._settings.setValue("convert/quality", self.quality_slider.value())
        self._settings.setValue("convert/max_dimension", self.dimension_spin.value())
        self._settings.setValue("convert/format_index", self.format_combo.currentIndex())
        self._settings.sync()

    def Shutdown(self) -> None:
        """保存页面偏好并请求停止；线程结束由 Controller 通知。"""
        if self._closing:
            return
        self._closing = True
        self._SaveSettings()

        self._task_controller.shutdown()
        self._preview_controller.shutdown()
        self._UpdateStartState()

    def closeEvent(self, event) -> None:  # noqa: N802 —— Qt 事件
        self._close_requested = True
        self.Shutdown()
        if self._task_controller.active or self._preview_controller.active:
            event.ignore()
        else:
            event.accept()

    def _AppendLog(self, level: int, text: str):
        self.log_panel.append(level, text)

    def _ShowInfoBar(
        self,
        title: str,
        content: str,
        error: bool = False,
        warning: bool = False,
    ) -> None:
        if self._closing:
            return
        info_bar = InfoBar.new
        if error:
            info_bar = InfoBar.error
        elif warning:
            info_bar = InfoBar.warning
        else:
            info_bar = InfoBar.success
        info_bar(title, content, parent=self)

    def _OnPreviewReady(self, batches):
        self._preview_batches = batches
        self.preview_label.setText(SummarizeBatches(batches))
        self._UpdateStartState()

    def _OnPreviewError(self, message):
        self._preview_batches = None
        self.preview_label.setText(f"⚠ {message}")
        self._UpdateStartState()

    def _OnPreviewState(self, _state):
        self._UpdateStartState()

    def SetEnvironment(self, environment):
        self.SetFfmpegPath(environment.ffmpeg_path, environment.capabilities, environment.message)
