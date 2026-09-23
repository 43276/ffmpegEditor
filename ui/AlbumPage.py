"""专辑批处理页面：批量给音频写入封面与元数据。"""
from __future__ import annotations

import html
import os
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import (
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QVBoxLayout,
    QWidget,
)

from qfluentwidgets import (
    CaptionLabel,
    HeaderCardWidget,
    InfoBar,
    LineEdit,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    ScrollArea,
    StrongBodyLabel,
    SwitchButton,
    TextBrowser,
    TitleLabel,
)
from qfluentwidgets.common.style_sheet import isDarkTheme

from app.AddCover import AlbumPlan, BuildAlbumPlan, SummarizeAlbumPlan
from app.Converter import ConverterError, LocateFfmpeg
from ui.AlbumWorker import AlbumWorker
from ui.Worker import LOG_ERROR, LOG_INFO, LOG_OK, LOG_WARN

# 深浅主题下的日志颜色
_LOG_COLORS = {
    LOG_OK: ("#0f7b0f", "#7adfa0"),
    LOG_WARN: ("#9a6700", "#f5c26b"),
    LOG_ERROR: ("#c42b1c", "#ff9aa2"),
}
_LEVEL_MARKS = {LOG_OK: "✓ ", LOG_WARN: "⚠ ", LOG_ERROR: "✗ "}


class AlbumPage(QWidget):
    """专辑批处理页面：作为“音频处理”模块中的一个子页面使用。"""

    backRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("albumPage")

        self._workers: list[AlbumWorker] = []
        self._worker_summaries: dict[int, dict] = {}
        self._thread_progress: dict[int, tuple[int, int]] = {}
        self._planned_total = 0
        self._root_path: Path | None = None
        self._plan: AlbumPlan | None = None
        self._last_output_dirs: list[str] = []
        self._error_logs: list[str] = []
        self._skipped_logs: list[str] = []
        self._live_statistics = {"total": 0, "ok": 0, "failed": 0, "skipped": 0}
        self._ffmpeg_path: str | None = None
        self._closing = False

        self._BuildUi()
        self._ConnectSignals()
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
        self.scroll_area.setWidgetResizable(True)
        content = QWidget(self.scroll_area)
        content.setObjectName("albumContent")
        self.scroll_area.setWidget(content)
        layout.addWidget(self.scroll_area, 1)

        self.content_layout = QVBoxLayout(content)
        self.content_layout.setContentsMargins(30, 14, 30, 26)
        self.content_layout.setSpacing(14)

        header_title = TitleLabel("专辑批处理", content)
        header_caption = CaptionLabel(
            "基于 FFmpeg · 给每个专辑文件夹里的音频批量写入封面、唱片集与艺术家元数据",
            content,
        )
        self.content_layout.addWidget(header_title)
        self.content_layout.addWidget(header_caption)

        self._BuildInputCard()
        self._BuildParamsCard()
        self._BuildActionCard()
        self._BuildLogCard()

    def _MakeCard(self, title: str) -> tuple[HeaderCardWidget, QVBoxLayout]:
        card = HeaderCardWidget(self.scroll_area)
        card.setTitle(title)
        body = QWidget(card)
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(10)
        card.viewLayout.addWidget(body)
        return card, body_layout

    def _BuildInputCard(self) -> None:
        card, layout = self._MakeCard("输入（专辑根目录）")
        self.content_layout.addWidget(card)

        path_row = QHBoxLayout()
        self.root_line = LineEdit(card)
        self.root_line.setClearButtonEnabled(True)
        self.root_line.setPlaceholderText("选择专辑根目录，或直接拖拽文件夹到这里")
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
        self.overwrite_switch = SwitchButton("覆盖已存在的输出文件", card)
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

    def _BuildActionCard(self) -> None:
        card, layout = self._MakeCard("任务")
        self.content_layout.addWidget(card)

        button_row = QHBoxLayout()
        self.start_button = PrimaryPushButton("开始处理", card)
        self.cancel_button = PushButton("取消", card)
        self.end_button = PushButton("结束", card)
        self.end_button.setToolTip("处理完当前正在处理的专辑后停止，不再开始新的专辑")
        self.open_folder_button = PushButton("打开输出目录", card)
        self.export_error_button = PushButton("导出错误日志", card)
        self.export_skipped_button = PushButton("导出跳过日志", card)
        button_row.addWidget(self.start_button)
        button_row.addWidget(self.cancel_button)
        button_row.addWidget(self.end_button)
        button_row.addWidget(self.open_folder_button)
        button_row.addWidget(self.export_error_button)
        button_row.addWidget(self.export_skipped_button)
        button_row.addStretch(1)
        layout.addLayout(button_row)

        self.progress_bar = ProgressBar(card)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        self.status_label = CaptionLabel("就绪", card)
        layout.addWidget(self.status_label)

    def _BuildLogCard(self) -> None:
        card, layout = self._MakeCard("处理日志")
        self.content_layout.addWidget(card)

        top_row = QHBoxLayout()
        self.log_statistics_label = StrongBodyLabel("总数：0    成功：0    失败：0    跳过：0", card)
        top_hint = CaptionLabel("转换过程与 ffmpeg 输出", card)
        clear_button = PushButton("清空", card)
        clear_button.setFixedWidth(72)
        top_row.addWidget(self.log_statistics_label)
        top_row.addSpacing(16)
        top_row.addWidget(top_hint)
        top_row.addStretch(1)
        top_row.addWidget(clear_button)
        layout.addLayout(top_row)

        self.log_browser = TextBrowser(card)
        self.log_browser.setMinimumHeight(150)
        self.log_browser.setPlaceholderText("暂无日志")
        layout.addWidget(self.log_browser, 1)

        self.clear_log_button = clear_button
        self._UpdateLogExportButtons()

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
        self.clear_log_button.clicked.connect(self.log_browser.clear)

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

    def _OnRootTextEdited(self, text: str) -> None:
        self._root_path = Path(text.strip()) if text.strip() else None

    def _RefreshPreview(self) -> None:
        root = self._CurrentRootPath()
        if root is None:
            self.preview_label.setText("")
            self._plan = None
            self._UpdateStartState()
            return
        try:
            plan = BuildAlbumPlan(root)
            self._plan = plan
            self.preview_label.setText(SummarizeAlbumPlan(plan))
        except (FileNotFoundError, NotADirectoryError, OSError, ValueError) as exc:
            self._plan = None
            self.preview_label.setText(f"⚠ {exc}")
        self._UpdateStartState()

    # ---- ffmpeg -------------------------------------------------------
    def SetFfmpegPath(self, path: str | None) -> None:
        self._ffmpeg_path = path
        if path:
            self.ffmpeg_status_label.setText(f"✓ {path}")
        else:
            self.ffmpeg_status_label.setText("✗ ffmpeg 未就绪，请先在“图片处理”页设置 ffmpeg")
        self._UpdateStartState()

    # ---- 任务执行 -----------------------------------------------------
    def _OnStartClicked(self) -> None:
        root = self._CurrentRootPath()
        if root is None:
            self._ShowInfoBar("无法开始", "请先选择专辑根目录", error=True)
            return

        ffmpeg_path = self._ffmpeg_path
        if ffmpeg_path is None:
            try:
                ffmpeg_path = LocateFfmpeg(None)
            except ConverterError as exc:
                self._ShowInfoBar("无法开始", str(exc), error=True)
                return
            self._ffmpeg_path = ffmpeg_path
            self.ffmpeg_status_label.setText(f"✓ {ffmpeg_path}")

        try:
            plan = BuildAlbumPlan(root)
        except (FileNotFoundError, NotADirectoryError, OSError, ValueError) as exc:
            self._ShowInfoBar("无法开始", str(exc), error=True)
            return

        if not plan.tasks:
            self._ShowInfoBar(
                "没有可处理的专辑",
                f"{len(plan.skipped_dirs)} 个文件夹将被跳过，请检查目录结构是否满足规则",
                warning=True,
            )
            return

        self._plan = plan
        self.log_browser.clear()
        self._error_logs = []
        self._skipped_logs = []
        self._last_output_dirs = []
        self.open_folder_button.setEnabled(False)

        total = plan.file_count
        self._SetStatistics(total=total, ok=0, failed=0, skipped=0)
        self._planned_total = total
        self._worker_summaries = {}
        self._thread_progress = {}
        self.progress_bar.setRange(0, total)
        self.progress_bar.setValue(0)

        worker = AlbumWorker(1, plan, self.overwrite_switch.isChecked(), ffmpeg_path, self)
        worker.logMessage.connect(self._OnWorkerLog)
        worker.progressChanged.connect(self._OnWorkerProgress)
        worker.statisticsChanged.connect(self._OnWorkerStatistics)
        worker.taskFinished.connect(self._OnWorkerTaskFinished)
        worker.finished.connect(lambda w=worker: self._OnWorkerThreadFinished(w))
        worker.finished.connect(worker.deleteLater)
        self._workers = [worker]

        self._AppendLog(
            LOG_INFO,
            f"任务开始：{len(plan.tasks)} 张专辑、{total} 个音频，由后台线程顺序处理",
        )
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

    def _OnWorkerStatistics(
        self,
        _thread_id: int,
        total: int,
        ok: int,
        failed: int,
        skipped: int,
        _done: int,
    ) -> None:
        self._SetStatistics(total=total, ok=ok, failed=failed, skipped=skipped)

    def _OnWorkerTaskFinished(self, thread_id: int, summary: dict) -> None:
        self._worker_summaries[thread_id] = summary
        if self._workers and len(self._worker_summaries) >= len(self._workers):
            self._OnAllWorkersFinished(self._MergeSummaries())

    def _MergeSummaries(self) -> dict:
        merged = {
            "total": 0,
            "ok": 0,
            "failed": 0,
            "skipped": 0,
            "skipped_dirs": 0,
            "cancelled": False,
            "early_stopped": False,
            "output_dirs": [],
            "error_logs": [],
            "skipped_logs": [],
        }
        seen_dirs: set[str] = set()
        for summary in self._worker_summaries.values():
            merged["total"] += summary["total"]
            merged["ok"] += summary["ok"]
            merged["failed"] += summary["failed"]
            merged["skipped"] += summary["skipped"]
            merged["skipped_dirs"] += summary.get("skipped_dirs", 0)
            merged["cancelled"] = merged["cancelled"] or summary["cancelled"]
            merged["early_stopped"] = (
                merged["early_stopped"] or summary["early_stopped"]
            )
            merged["error_logs"].extend(summary.get("error_logs", []))
            merged["skipped_logs"].extend(summary.get("skipped_logs", []))
            for directory in summary["output_dirs"]:
                if directory not in seen_dirs:
                    seen_dirs.add(directory)
                    merged["output_dirs"].append(directory)
        return merged

    def _OnAllWorkersFinished(self, summary: dict) -> None:
        if summary["cancelled"]:
            self._ShowInfoBar("任务已取消", "本次处理被手动中止", warning=True)
        elif summary["early_stopped"]:
            self._ShowInfoBar(
                "已按“结束”停止",
                f"完成当前专辑后停止：成功 {summary['ok']}，失败 {summary['failed']}，"
                f"跳过 {summary['skipped']}；剩余专辑未处理",
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
                f"成功 {summary['ok']} 个音频，跳过 {summary['skipped']}",
            )
        self._last_output_dirs = summary["output_dirs"]
        self._error_logs = summary.get("error_logs", [])
        self._skipped_logs = summary.get("skipped_logs", [])
        self._SetStatistics(
            total=summary["total"],
            ok=summary["ok"],
            failed=summary["failed"],
            skipped=summary["skipped"],
        )
        self._UpdateLogExportButtons()

    def _OnWorkerThreadFinished(self, worker: AlbumWorker) -> None:
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
        if self._workers:
            self.status_label.setText("正在处理当前专辑，之后将停止…")
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
    def _SetStatistics(self, total: int, ok: int, failed: int, skipped: int) -> None:
        self._live_statistics = {
            "total": total,
            "ok": ok,
            "failed": failed,
            "skipped": skipped,
        }
        self.log_statistics_label.setText(
            f"总数：{total}    成功：{ok}    失败：{failed}    跳过：{skipped}"
        )

    def _UpdateLogExportButtons(self) -> None:
        running = bool(self._workers)
        self.export_error_button.setEnabled(not running and bool(self._error_logs))
        self.export_skipped_button.setEnabled(not running and bool(self._skipped_logs))

    def _UpdateStartState(self) -> None:
        has_root = self._CurrentRootPath() is not None
        running = bool(self._workers)
        self.start_button.setEnabled(
            self._ffmpeg_path is not None and has_root and not running
        )
        self.cancel_button.setEnabled(running)
        self.end_button.setEnabled(running)
        self.browse_root_button.setEnabled(not running)
        self._UpdateLogExportButtons()

    def _ExportLog(self, title: str, default_name: str, entries: list[str]) -> None:
        if not entries or self._workers:
            return
        file_path, _filter = QFileDialog.getSaveFileName(
            self, title, default_name, "文本文件 (*.txt);;所有文件 (*.*)"
        )
        if not file_path:
            return
        try:
            Path(file_path).write_text("\n".join(entries) + "\n", encoding="utf-8")
        except OSError as exc:
            self._ShowInfoBar("导出失败", str(exc), error=True)
            return
        self._ShowInfoBar("导出成功", f"日志已保存到：{file_path}")

    def _ExportErrorLog(self) -> None:
        self._ExportLog("导出错误日志", "error_log.txt", self._error_logs)

    def _ExportSkippedLog(self) -> None:
        self._ExportLog("导出跳过日志", "skipped_log.txt", self._skipped_logs)

    def _AppendLog(self, level: int, text: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        body = f"[{timestamp}] {_LEVEL_MARKS.get(level, '')}{html.escape(text).replace(chr(10), '<br>')}"
        if level == LOG_INFO:
            line = f"<div style='margin:0'>{body}</div>"
        else:
            light, dark = _LOG_COLORS[level]
            color = dark if isDarkTheme() else light
            line = f"<div style='margin:0;color:{color}'>{body}</div>"
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

    # ---- 生命周期 -----------------------------------------------------
    def Shutdown(self) -> None:
        """关闭窗口前取消并等待所有后台线程结束。"""
        self._closing = True
        for worker in list(self._workers):
            worker.RequestCancel()
        for worker in list(self._workers):
            worker.wait()
        self._workers.clear()
