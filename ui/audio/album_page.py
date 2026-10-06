"""专辑批处理页面：批量给音频写入封面与元数据。"""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QVBoxLayout,
    QWidget,
)

from qfluentwidgets import (
    CaptionLabel,
    InfoBar,
    LineEdit,
    PushButton,
    StrongBodyLabel,
)

from app.audio.album_planner import build_album_plan as BuildAlbumPlan
from ui.media_presentation import summarize_album_plan as SummarizeAlbumPlan
from app.audio.models import AlbumPlan
from ui.tasks.controller import TaskController, TaskState
from ui.tasks.preview import PreviewController, PreviewState
from ui.widgets.log_panel import LogPanel
from ui.widgets.task_panel import TaskPanel
from ui.widgets.page_layout import make_card, build_page_content
from ui.services.desktop_actions import DesktopActions
from ui.tasks.jobs import album_inputs_job
from ui.widgets.switch_button import MakeSwitchButton
from ui.widgets.smooth_scroll import SmoothScrollArea as ScrollArea
from app.task_models import LOG_ERROR, LOG_INFO, LOG_WARN, TaskProgress, TaskResult, TaskStatistics


class AlbumPage(QWidget):
    """专辑批处理页面：作为“音频处理”模块中的一个子页面使用。"""

    backRequested = pyqtSignal()

    def __init__(self, parent=None, *, settings=None, desktop_actions=None):
        super().__init__(parent)
        self.setObjectName("albumPage")

        self._task_controller = TaskController(self)
        self._preview_controller = PreviewController(self)
        self._desktop_actions = desktop_actions if desktop_actions is not None else DesktopActions()
        self._root_path: Path | None = None
        self._plan: AlbumPlan | None = None
        self._last_output_dirs: list[str] = []
        self._error_logs: list[str] = []
        self._skipped_logs: list[str] = []
        self._ffmpeg_path: str | None = None
        self._closing = False
        self._close_requested = False

        self._BuildUi()
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
        self._UpdateStartState()

    # ---- 界面组装 ----------------------------------------------------
    def _BuildUi(self) -> None:
        self.setAcceptDrops(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        back_row = QHBoxLayout()
        back_row.setContentsMargins(30, 16, 30, 0)
        self.back_button = PushButton("← 返回音频处理", self)
        back_row.addWidget(self.back_button)
        back_row.addStretch(1)
        layout.addLayout(back_row)

        self.scroll_area = ScrollArea(self)
        layout.addWidget(self.scroll_area, 1)
        _, self.content_layout = build_page_content(
            self.scroll_area, "专辑批处理", "基于 FFmpeg · 给每个专辑文件夹里的音频批量写入封面、唱片集与艺术家元数据",
            object_name="albumContent", margins=(30, 14, 30, 26),
        )
        self._BuildInputCard()
        self._BuildParamsCard()
        self._BuildActionCard()
        self._BuildLogCard()

    def _MakeCard(self, title: str):
        card, layout = make_card(self.scroll_area, title)
        return card, layout

    def _BuildInputCard(self) -> None:
        card, layout = self._MakeCard("输入")
        self.content_layout.addWidget(card)

        path_row = QHBoxLayout()
        self.root_line = LineEdit(card)
        self.root_line.setClearButtonEnabled(True)
        self.root_line.setPlaceholderText("选择专辑根目录")
        self.root_line.setMinimumHeight(36)
        browse_button = PushButton("选择文件夹…", card)
        path_row.addWidget(self.root_line, 1)
        path_row.addWidget(browse_button)
        layout.addLayout(path_row)

        self.preview_label = CaptionLabel("", card)
        layout.addWidget(self.preview_label)

        hint = CaptionLabel(
            "根目录下每个一级子文件夹视为一张专辑；专辑内递归查找音频与封面图片。"
            "封面图片所在目录必须同时包含 album.txt 与 artist.txt，且每张专辑只能有一张图片。",
            card,
        )
        layout.addWidget(hint)

        self.browse_root_button = browse_button

    def _BuildParamsCard(self) -> None:
        card, layout = self._MakeCard("处理参数")
        self.content_layout.addWidget(card)

        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)

        grid.addWidget(self._MakeFieldLabel("同名输出", card), 0, 0)
        self.overwrite_switch = MakeSwitchButton("覆盖已存在的输出文件", card)
        self.overwrite_switch.setChecked(True)
        grid.addWidget(self.overwrite_switch, 0, 1)

        grid.addWidget(self._MakeFieldLabel("ffmpeg", card), 1, 0)
        self.ffmpeg_status_label = CaptionLabel("正在检测 ffmpeg…", card)
        grid.addWidget(self.ffmpeg_status_label, 1, 1)

        output_hint = CaptionLabel(
            "输出规则：每个音频输出到其所在目录下的 <专辑文件夹名> 子目录；"
            ".wav 会转为 .mp3，其余格式保持原扩展名并复制原音频流。",
            card,
        )
        layout.addWidget(output_hint)

    @staticmethod
    def _MakeFieldLabel(text: str, parent: QWidget) -> StrongBodyLabel:
        label = StrongBodyLabel(text, parent)
        label.setFixedWidth(88)
        return label

    def _BuildActionCard(self):
        self.task_panel = TaskPanel(self.scroll_area, finish_tooltip="处理完当前正在处理的专辑后停止，不再开始新的专辑")
        self.content_layout.addWidget(self.task_panel)
        for name in ("start_button", "cancel_button", "end_button", "open_folder_button", "export_error_button", "export_skipped_button", "progress_bar", "status_label"):
            setattr(self, name, getattr(self.task_panel, name))

    def _BuildLogCard(self):
        self.log_panel = LogPanel(self.scroll_area)
        self.content_layout.addWidget(self.log_panel)
        self.log_browser = self.log_panel.browser
        self.log_statistics_label = self.log_panel.statistics_label
        self.clear_log_button = self.log_panel.clear_button

    def _ConnectSignals(self) -> None:
        self.back_button.clicked.connect(self.backRequested.emit)
        self.browse_root_button.clicked.connect(self._OnBrowseRoot)
        self.root_line.editingFinished.connect(self._OnRootEdited)
        self.root_line.textEdited.connect(self._OnRootTextEdited)
        self.start_button.clicked.connect(self._OnStartClicked)
        self.cancel_button.clicked.connect(self._OnCancelClicked)
        self.end_button.clicked.connect(self._OnEndClicked)
        self.open_folder_button.clicked.connect(self._OnOpenFolder)
        self.export_error_button.clicked.connect(self._ExportErrorLog)
        self.export_skipped_button.clicked.connect(self._ExportSkippedLog)

    # ---- 拖放 ---------------------------------------------------------
    def dragEnterEvent(self, event) -> None:  # noqa: N802 —— Qt 事件
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802 —— Qt 事件
        urls = event.mimeData().urls()
        if not urls:
            return
        paths = [Path(url.toLocalFile()) for url in urls if url.isLocalFile()]
        dirs = [path for path in paths if path.is_dir()]
        if not dirs:
            self._ShowInfoBar("选择无效", "请拖入一个文件夹作为专辑根目录", error=True)
            return
        if len(dirs) > 1:
            self._ShowInfoBar("选择无效", "一次只能处理一个专辑根目录，已使用第一个文件夹", warning=True)
        self._SetRootPath(dirs[0])
        event.acceptProposedAction()

    # ---- 输入路径 -----------------------------------------------------
    def _OnBrowseRoot(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "选择专辑根目录", self.root_line.text().strip() or ""
        )
        if directory:
            self._SetRootPath(Path(directory))

    def _SetRootPath(self, path: Path) -> None:
        self._root_path = path
        self.root_line.setText(str(path))
        self._RefreshPreview()

    def _CurrentRootPath(self) -> Path | None:
        if self._root_path is not None:
            return self._root_path
        text = self.root_line.text().strip()
        return Path(text) if text else None

    def _OnRootEdited(self) -> None:
        text = self.root_line.text().strip()
        self._root_path = Path(text) if text else None
        self._RefreshPreview()

    def _OnRootTextEdited(self, text: str):
        self._root_path = Path(text.strip()) if text.strip() else None
        self._RefreshPreview()

    def _RefreshPreview(self):
        if self._closing:
            return
        root = self._CurrentRootPath()
        self._plan = None
        if root is None:
            self._preview_controller.invalidate()
            self.preview_label.setText("")
            self._UpdateStartState()
            return
        self.preview_label.setText("正在准备预览…")
        self._preview_controller.request(lambda cancel_check: BuildAlbumPlan(root, cancel_check=cancel_check))
        self._UpdateStartState()

    # ---- ffmpeg -------------------------------------------------------
    def SetFfmpegPath(self, path: str | None) -> None:
        self._ffmpeg_path = path
        if path:
            self.ffmpeg_status_label.setText(f"✓ {path}")
        else:
            self.ffmpeg_status_label.setText("✗ FFmpeg 未就绪，请在“设置”页检查 FFmpeg 路径")
        self._UpdateStartState()

    # ---- 任务执行 -----------------------------------------------------
    def _OnStartClicked(self):
        if self._closing or self._task_controller.active:
            return
        if self._preview_controller.state != PreviewState.VALID or self._plan is None:
            self._ShowInfoBar("无法开始", "输入预览尚未准备完成或输入无效", error=True)
            return
        if not self._plan.tasks:
            self._ShowInfoBar("没有可处理的专辑", f"{len(self._plan.skipped_dirs)} 个文件夹将被跳过，请检查目录结构是否满足规则", warning=True)
            return
        if self._ffmpeg_path is None:
            self._ShowInfoBar("无法开始", "FFmpeg 尚未就绪，请在设置页检查路径", error=True)
            return
        self.log_panel.clear()
        self._error_logs = []
        self._skipped_logs = []
        self._last_output_dirs = []
        total = self._plan.file_count
        self._SetStatistics(total, 0, 0, 0)
        self.progress_bar.setRange(0, max(1, total))
        self.progress_bar.setValue(0)
        self._AppendLog(LOG_INFO, "任务开始：后台重新校验专辑输入并顺序处理")
        self.status_label.setText("正在校验输入…")
        self._task_controller.start(album_inputs_job(self._CurrentRootPath(), self.overwrite_switch.isChecked(), self._ffmpeg_path), total=total, supports_finish=True)
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
        self.task_panel.set_result(summary, boundary="专辑")
        if summary.cancelled:
            self._ShowInfoBar("任务已取消", "本次处理被手动中止", warning=True)
        elif summary.early_stopped:
            self._ShowInfoBar(
                "已按“结束”停止",
                f"完成当前专辑后停止：成功 {summary.ok}，失败 {summary.failed}，"
                f"跳过 {summary.skipped}；剩余专辑未处理",
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
                f"成功 {summary.ok} 个音频，跳过 {summary.skipped}",
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

        self._UpdateStartState()

    def _OnTaskIdle(self):
        if self._close_requested and not self._task_controller.active and not self._preview_controller.active:
            QTimer.singleShot(0, self.close)

    def _OnCancelClicked(self) -> None:
        if self._task_controller.request_cancel():
            self.status_label.setText("正在取消…")
            self._UpdateStartState()

    def _OnEndClicked(self) -> None:
        if self._task_controller.request_finish_after_current_batch():
            self.status_label.setText("正在处理当前专辑，之后将停止…")
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
        self.log_panel.set_statistics(TaskStatistics(total, ok, failed, skipped, ok + failed + skipped))

    def _UpdateLogExportButtons(self):
        self._UpdateStartState()

    def _UpdateStartState(self):
        running = self._task_controller.active or self._closing
        ready = self._ffmpeg_path is not None and self._preview_controller.state == PreviewState.VALID and self._plan is not None and bool(self._plan.tasks)
        self.task_panel.update_state(self._task_controller, ready, self._closing, bool(self._last_output_dirs), bool(self._error_logs), bool(self._skipped_logs))
        self.browse_root_button.setEnabled(not running)
        self.root_line.setEnabled(not running)

    def _ExportLog(self, title: str, default_name: str, entries: list[str]):
        if not self._task_controller.active and not self._closing:
            self.log_panel.export_entries(self, title, default_name, entries, self._ShowInfoBar)

    def _ExportErrorLog(self) -> None:
        self._ExportLog("导出错误日志", "error_log.txt", self._error_logs)

    def _ExportSkippedLog(self) -> None:
        self._ExportLog("导出跳过日志", "skipped_log.txt", self._skipped_logs)

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

    # ---- 生命周期 -----------------------------------------------------
    def Shutdown(self) -> None:
        """请求停止后台任务，保留线程引用直到真实结束。"""
        if self._closing:
            return
        self._closing = True
        self._task_controller.shutdown()
        self._preview_controller.shutdown()
        self._UpdateStartState()

    def closeEvent(self, event) -> None:  # noqa: N802
        self._close_requested = True
        self.Shutdown()
        if self._task_controller.active or self._preview_controller.active:
            event.ignore()
        else:
            event.accept()

    def _OnPreviewReady(self, plan):
        self._plan = plan
        self.preview_label.setText(SummarizeAlbumPlan(plan))
        self._UpdateStartState()

    def _OnPreviewError(self, message):
        self._plan = None
        self.preview_label.setText(f"⚠ {message}")
        self._UpdateStartState()

    def _OnPreviewState(self, _state):
        self._UpdateStartState()

    def SetEnvironment(self, environment):
        self.SetFfmpegPath(environment.ffmpeg_path)
